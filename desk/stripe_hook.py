"""The card processor's webhook. Its signature is checked on the raw bytes with the signing secret (HMAC-SHA256 over "time.body",
any one of the v1 values, constant time, within five minutes of the desk's clock); only then is the event read, and it is handed to
core/delinquency.py, which settles the payment from the processor's own record, never from the event. With no secret set the
endpoint is closed: a visitor copy cannot register a webhook, and the hourly pass settles payments by reading the processor."""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from core.delinquency import ProcessorTimeout

TOLERANCE = 300         # seconds a signed time may differ from now
MAX_BODY = 262_144      # a payment event is a few kilobytes
TYPES = ("payment_intent.succeeded", "payment_intent.payment_failed", "payment_intent.processing", "payment_intent.canceled")


def signed(secret: str, header: str, body: bytes, now: int) -> bool:
    """Whether the header carries a valid v1 signature of this exact body, made within the tolerance of now."""
    if not header.isascii():        # int() reads other scripts' digits and compare_digest refuses non-ASCII text: neither may crash a request
        return False
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    at = next((v for k, v in parts if k == "t"), "")
    if not at.isdigit() or abs(now - int(at)) > TOLERANCE:
        return False
    mac = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(mac, v) for k, v in parts if k == "v1")


def event_of(payload: dict) -> dict | None:
    """The core's shape of a payment event, or None when it is not about a payment made on one of our visitor copies."""
    obj = (payload.get("data") or {}).get("object") or {}
    marks = obj.get("metadata") or {}
    if payload.get("type") not in TYPES or obj.get("object") != "payment_intent" or not str(marks.get("copy", "")).startswith("visitor:"):
        return None
    return {"id": payload["id"], "type": payload["type"], "payment": obj["id"], "metadata": {k: marks.get(k) for k in ("copy", "intent", "nonce")}}


def mount(app, fd, secret: str | None) -> None:
    @app.post("/webhooks/stripe", include_in_schema=False)
    async def card_processor_webhook(request: Request):
        if not secret:
            raise HTTPException(503, "no webhook secret is set")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                raise HTTPException(413, "that body is too large")
        if not signed(secret, request.headers.get("stripe-signature", ""), body, int(fd.clock().timestamp())):
            raise HTTPException(400, "the signature does not match")
        try:
            event = event_of(json.loads(body))
        except (ValueError, AttributeError, KeyError):
            raise HTTPException(400, "that is not an event") from None
        if event is None:
            return {"result": "ignored"}
        try:
            return {"result": await run_in_threadpool(fd.processor_event, event)}
        except ProcessorTimeout:
            raise HTTPException(503, "the processor could not be read; send the event again") from None
