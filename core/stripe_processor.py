"""The card processor's test-mode adapter behind the Processor interface of core/delinquency.py, over plain HTTPS (urllib), with no
package. One payment per idempotency key: the key goes in the Idempotency-Key header and, because a payment does not carry it
back, into the payment's own marks beside the copy, intent and nonce. A card the desk knows by a neutral token (card_ok,
card_declined) is sent as the processor's documented test payment method. A decline comes back as 402 with the payment inside
the error and reads as failed. A payment is found again by listing, newest first, and filtering its marks here: the search
endpoint is not read-after-write consistent, and a miss would let a lost-reply payment be closed and paid again.

Only a test-mode key is ever used; a live key stops the desk at startup. The key travels in a header, never in an address, a
log line or an error message."""
from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .books import ZONE
from .delinquency import ProcessorRefused, ProcessorTimeout

API = "https://api.stripe.com"
# the processor's documented test payment methods: a card that pays, and one declined for insufficient funds
TEST_CARDS = {"card_ok": "pm_card_visa", "card_declined": "pm_card_visa_chargeDeclinedInsufficientFunds"}
WINDOW = 36 * 3600      # a copy lasts a day and is swept every hour, so every payment worth finding is newer than this
PAGES = 10              # ponytail: 1,000 payments inside the window; more than that fails the lookup loudly instead of answering "none"
log = logging.getLogger("core.stripe_processor")


class StripeProcessor:
    simulated = False

    def __init__(self, secret: str, base_url: str = API, timeout: float = 10.0, clock=time.time):
        if not secret.startswith("sk_test_"):
            raise ValueError("STRIPE_SECRET_KEY must be a test-mode key")
        self._secret, self.base, self.timeout, self.clock, self.page = secret, base_url.rstrip("/"), timeout, clock, 100

    def __repr__(self) -> str:
        return "StripeProcessor(test mode)"

    def _call(self, method: str, path: str, params: dict | None = None, key: str | None = None) -> tuple[int, dict]:
        url, data = self.base + path, None
        headers = {"Authorization": f"Bearer {self._secret}"}
        if method == "POST":
            data = urllib.parse.urlencode(params or {}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif params:
            url += "?" + urllib.parse.urlencode(params)
        if key:
            headers["Idempotency-Key"] = key
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=self.timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            log.warning("card processor request failed: %s", type(e).__name__)
            raise ProcessorTimeout(type(e).__name__) from None

    @staticmethod
    def _payment(pi: dict) -> dict:
        marks = dict(pi.get("metadata") or {})
        status = "failed" if pi["status"] == "requires_payment_method" and pi.get("last_payment_error") else pi["status"]
        return {"id": pi["id"], "key": marks.pop("key", None), "cents": pi["amount"], "status": status, "metadata": marks,
                "at": datetime.fromtimestamp(pi["created"], ZONE).isoformat(), "simulated": False}

    def _answer(self, status: int, body: dict) -> dict:
        err = body.get("error") or {}
        if status == 200:
            return self._payment(body)
        if status == 402 and err.get("payment_intent"):
            return self._payment(err["payment_intent"])
        if status in (402, 409, 429) or status >= 500:
            raise ProcessorTimeout(f"http {status}")        # unknown: reconciliation reads the processor's own record
        raise ProcessorRefused(err.get("code") or err.get("type") or f"http {status}")

    def create(self, key: str, cents: int, card: str, metadata: dict, now: datetime) -> dict:
        if card not in TEST_CARDS:
            raise ProcessorRefused("no test payment method for that card")      # nothing is sent, so send() closes the intent
        params = {"amount": cents, "currency": "usd", "payment_method": TEST_CARDS[card], "confirm": "true",
                  "automatic_payment_methods[enabled]": "true", "automatic_payment_methods[allow_redirects]": "never",
                  **{f"metadata[{k}]": v for k, v in {**metadata, "key": key}.items()}}
        return self._answer(*self._call("POST", "/v1/payment_intents", params, key))

    def retrieve(self, outside: str) -> dict:
        return self._answer(*self._call("GET", f"/v1/payment_intents/{urllib.parse.quote(outside, safe='')}"))

    def _listed(self, since: datetime | None = None):
        """Every payment inside the window, newest first. The window is the last 36 hours, or further back when the caller has an intent
        that old still waiting (a hold has no time limit), with a minute to spare."""
        floor = int(self.clock()) - WINDOW
        if since is not None:
            floor = min(floor, int(since.timestamp()) - 60)
        params = {"limit": self.page, "created[gte]": floor}
        for _ in range(PAGES):
            status, body = self._call("GET", "/v1/payment_intents", params)
            if status != 200:
                raise ProcessorTimeout(f"http {status}")
            for pi in body["data"]:
                yield self._payment(pi)
            if not body["has_more"]:
                return
            params["starting_after"] = body["data"][-1]["id"]
        raise ProcessorTimeout("too many payments to list")

    def find(self, key: str, since: datetime | None = None) -> dict | None:
        return next((p for p in self._listed(since) if p["key"] == key), None)

    def for_copy(self, copy: str, since: datetime | None = None) -> list[dict]:
        return [p for p in self._listed(since) if p["metadata"].get("copy") == copy]

    def cancel(self, outside: str) -> dict:
        status, body = self._call("POST", f"/v1/payment_intents/{urllib.parse.quote(outside, safe='')}/cancel", {})
        return self._payment(body) if status == 200 else self.retrieve(outside)      # one that cannot be cancelled is shown as it is

    def take_events(self) -> list[dict]:
        return []       # events arrive on the webhook; with none, reconciliation reads each payment from the processor


def from_key(key: str | None) -> StripeProcessor | None:
    """The processor for the configured key: none when unset (the simulated one stays), the test-mode one for a test key, and a
    refusal for any other key, so a live key, a restricted key or a typo is never mistaken for "not configured" and left to fall back
    to the simulated processor unseen. The message names the setting, never the value."""
    if not key:
        return None
    if "_live_" in key:
        raise ValueError("STRIPE_SECRET_KEY is a live-mode key; the desk takes card payments in test mode only")
    if not key.startswith("sk_test_"):
        raise ValueError("STRIPE_SECRET_KEY must be a test-mode secret key (it starts with sk_test_)")
    return StripeProcessor(key)
