"""A stand-in for the card processor's test-mode API, on a real local socket, so the processor's own HTTP code (form bodies,
headers, status codes, paging) runs against something that answers as the documented API does: one payment per idempotency
key (the saved answer is replayed, a decline included), a decline answered 402 with the payment inside the error, payments
listed newest first, and a signing helper for the webhook. No test reaches the network."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

GOOD, DECLINED = "pm_card_visa", "pm_card_visa_chargeDeclinedInsufficientFunds"


def sign(secret: str, body: bytes, at: int) -> str:
    """The header the processor puts on a webhook: the time, then the HMAC-SHA256 of "time.body"."""
    mac = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={at},v1={mac}"


class FakeStripe:
    def __init__(self, key: str = "sk_test_fake", clock=lambda: 1_800_000_000):
        self.key, self.clock = key, clock
        self.intents: dict[str, dict] = {}
        self.saved: dict[str, tuple[dict, int, dict]] = {}     # idempotency key -> (params, status, body)
        self.calls: list[SimpleNamespace] = []
        self.keep_processing = False            # a confirmed payment stays processing until settle()
        self.lose_reply = False                 # the next request is carried out and its answer never sent
        self.fail_with: int | None = None       # the next request is answered with this status and nothing is done
        self.down = False                       # every request is answered 500 and nothing is done, until the test sets it back
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                owner._serve(self)

            def do_POST(self):
                owner._serve(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # the test's own hands on the payments ----------------------------------------------------------------------------
    def settle(self, pid: str, status: str) -> None:
        self.intents[pid]["status"] = status

    # the API ---------------------------------------------------------------------------------------------------------
    def _serve(self, h) -> None:
        url = urlsplit(h.path)
        body = h.rfile.read(int(h.headers.get("Content-Length") or 0)).decode()
        params = dict(parse_qsl(url.query if h.command == "GET" else body, keep_blank_values=True))
        call = SimpleNamespace(method=h.command, path=url.path, headers={k.lower(): v for k, v in h.headers.items()}, params=params,
                               raw_path=h.path)
        self.calls.append(call)
        if self.fail_with or self.down:
            status, self.fail_with = self.fail_with or 500, None
            return self._send(h, status, {"error": {"type": "api_error"}})
        if h.headers.get("Authorization") != f"Bearer {self.key}":
            return self._send(h, 401, {"error": {"type": "invalid_request_error", "code": "api_key_invalid"}})
        status, out = self._route(call)
        if self.lose_reply:
            self.lose_reply = False
            h.close_connection = True
            return
        self._send(h, status, out)

    @staticmethod
    def _send(h, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def _route(self, call) -> tuple[int, dict]:
        parts = call.path.strip("/").split("/")
        if parts[:2] != ["v1", "payment_intents"]:
            return 404, {"error": {"code": "resource_missing"}}
        if len(parts) == 2 and call.method == "POST":
            return self._create(call)
        if len(parts) == 2:
            return self._list(call.params)
        pi = self.intents.get(parts[2])
        if pi is None:
            return 404, {"error": {"type": "invalid_request_error", "code": "resource_missing"}}
        if len(parts) == 3:
            return 200, pi
        if parts[3] == "cancel" and pi["status"] in ("processing", "requires_payment_method"):
            pi["status"] = "canceled"
            return 200, pi
        return 400, {"error": {"type": "invalid_request_error", "code": "payment_intent_unexpected_state"}}

    def _create(self, call) -> tuple[int, dict]:
        p, key = call.params, call.headers.get("idempotency-key")
        if key in self.saved:
            before, status, out = self.saved[key]
            if before != p:
                return 400, {"error": {"type": "idempotency_error"}}
            return status, out
        amount = int(p.get("amount", 0))
        if amount < 50:
            return 400, {"error": {"type": "invalid_request_error", "code": "amount_too_small", "message": "Amount must be at least 50 cents"}}
        if p.get("payment_method") not in (GOOD, DECLINED):
            return 400, {"error": {"type": "invalid_request_error", "code": "resource_missing"}}
        pid = f"pi_{len(self.intents) + 1:05d}"
        pi = {"id": pid, "object": "payment_intent", "amount": amount, "currency": p.get("currency"), "livemode": False,
              "created": int(self.clock()), "last_payment_error": None, "status": "succeeded",
              "metadata": {k[9:-1]: v for k, v in p.items() if k.startswith("metadata[")}}
        self.intents[pid] = pi
        status, out = 200, pi
        if p["payment_method"] == DECLINED:
            pi.update(status="requires_payment_method", last_payment_error={"code": "card_declined", "decline_code": "insufficient_funds"})
            status, out = 402, {"error": {"type": "card_error", "code": "card_declined", "decline_code": "insufficient_funds",
                                         "message": "Your card has insufficient funds.", "payment_intent": pi}}
        elif self.keep_processing:
            pi["status"] = "processing"
        if key:
            self.saved[key] = (p, status, json.loads(json.dumps(out)))
        return status, out

    def _list(self, params: dict) -> tuple[int, dict]:
        rows = sorted(self.intents.values(), key=lambda x: (-x["created"], x["id"]), reverse=False)
        rows = [x for x in rows if x["created"] >= int(params.get("created[gte]", 0))]
        after = params.get("starting_after")
        if after:
            rows = rows[[x["id"] for x in rows].index(after) + 1:]
        page = rows[:int(params.get("limit", 10))]
        return 200, {"object": "list", "data": page, "has_more": len(rows) > len(page)}
