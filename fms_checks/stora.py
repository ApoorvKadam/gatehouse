"""The Stora adapter: the only file that knows Stora's API, its field names and its status words. It implements `core.FMS` and turns
every reply into a neutral `Read` (a unit is open or restricted; an increase has a start, a notice date and two rents), keeping
Stora's own value in `raw` for the private record.

A token is asked for with the scopes its holder needs and no more: the truth reader and the careful agent hold read-only tokens,
and only set-up holds the write scope (Stora refuses a write with a read-only token: 403).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .core import OPEN, RESTRICTED, Read

API = "https://public-api.stora.co"
VERSION = "2025-09"
READ_ONLY = "public.unit:read"
SET_UP = "public.unit:read public.unit:write"
RENT_READ = "public.price_adjustment:read"
RENT_SET_UP = "public.price_adjustment:read public.price_adjustment:write"
# a token is renewed this long before Stora says it ends (tokens last two hours and there is no refresh token)
RENEW_EARLY = 300


# Stora's unit statuses: occupied is the only open one; these others are not held by a contact's lock
_NOT_HELD = ("available", "occupied", "reserved", "unavailable")


@dataclass
class Stora:
    scope: str
    client_id: str = field(default_factory=lambda: os.environ["STORA_CLIENT_ID"], repr=False)
    client_secret: str = field(default_factory=lambda: os.environ["STORA_CLIENT_SECRET"], repr=False)
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30.0), repr=False)
    _token: str | None = field(default=None, repr=False)
    _expires: float = field(default=0.0, repr=False)

    def token(self) -> str:
        """A bearer token for this client's scopes, reused until shortly before it ends, then asked for again (client credentials give
        no refresh token). Raises httpx.HTTPStatusError when Stora refuses the exchange."""
        if self._token and time.monotonic() < self._expires:
            return self._token
        r = self.http.post(f"{API}/oauth2/token", json={"grant_type": "client_credentials", "client_id": self.client_id,
                                                         "client_secret": self.client_secret, "scope": self.scope})
        r.raise_for_status()
        body = r.json()
        token: str = body["access_token"]
        self._token, self._expires = token, time.monotonic() + int(body.get("expires_in", 0)) - RENEW_EARLY
        return token

    def _call(self, what: str, method: str, path: str, body: dict | None = None, key: str | None = None) -> tuple[Read, dict]:
        headers = {"authorization": f"Bearer {self.token()}", "accept": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        r = self.http.request(method, f"{API}/{VERSION}{path}", json=body, headers=headers)
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        err = data.get("error") or {}
        request_id = (data.get("meta") or {}).get("request_id") or err.get("request_id")   # an error reply carries it in `error`
        return Read(what, r.status_code, request_id, at, error=err.get("code") if r.status_code >= 400 else None), data

    def _list(self, what: str, path: str, field: str, params: dict | None = None, most: int = 20) -> tuple[Read, list]:
        """Every page of a list. Any page that cannot be read makes the whole read fail, so a check never decides on half a list."""
        items, page, read = [], 1, None
        while page and page <= most:
            query = "&".join(f"{k}={v}" for k, v in {**(params or {}), "page": page, "limit": 100}.items())
            read, data = self._call(what, "GET", f"{path}?{query}")
            if read.http_status != 200 or not isinstance(data.get(field), list):
                status = read.http_status if read.http_status != 200 else 0      # a 200 without the list is no read
                return Read(read.what, status, read.request_id, read.at, None, read.error or "unreadable page"), []
            items += data[field]
            page = ((data.get("meta") or {}).get("pagination") or {}).get("next")
        if page:
            return Read(what, 0, read.request_id if read else None, read.at if read else "", None, "too many pages"), []
        return read, items

    def held_units(self) -> Read:
        """Units held in any state a contact-wide unlock could lift. A status this code does not know counts as held."""
        read, units = self._list("units", "/units", "units")
        held = [u.get("id") for u in units if u.get("status") not in _NOT_HELD]
        return Read(read.what, read.http_status, read.request_id, read.at, held if read.http_status == 200 else None, read.error)

    def unit_access(self, unit_id: str) -> Read:
        """Open only when Stora says occupied; any other word, known or not, is restricted. Stora's word stays in `raw`."""
        read, data = self._call("unit access", "GET", f"/units/{unit_id}")
        status = (data.get("unit") or {}).get("status")
        value = None if read.http_status != 200 or not isinstance(status, str) or not status.strip() else OPEN if status == "occupied" else RESTRICTED
        return Read(read.what, read.http_status, read.request_id, read.at, value, read.error, status)

    def restrict(self, contact_id: str, key: str) -> Read:
        """Lock every occupied unit of the contact. The reply's message is kept, but it proves nothing: a repeated key returns the
        stored success message without locking anything, so the caller reads the unit afterwards."""
        read, data = self._call("restrict", "POST", "/units/overlock", {"contact_id": contact_id}, key)
        return Read(read.what, read.http_status, read.request_id, read.at, None, read.error, (data.get("success") or {}).get("message"))

    def lift(self, contact_id: str, key: str) -> Read:
        """Lift every lock the contact holds, on every unit, not only one; callers must have checked that no other unit was held."""
        read, data = self._call("lift", "POST", "/units/remove_overlock", {"contact_id": contact_id}, key)
        return Read(read.what, read.http_status, read.request_id, read.at, None, read.error, (data.get("success") or {}).get("message"))

    # rent increases ("price adjustments"): what a check needs from one, kept as plain values

    @staticmethod
    def _adjustment(data: dict) -> dict | None:
        pa = data.get("price_adjustment")
        if not isinstance(pa, dict):
            return None
        note, est = pa.get("notification") or {}, pa.get("estimation") or {}
        return {"id": pa.get("id"), "status": pa.get("status"), "subscription": (pa.get("subscription") or {}).get("id"),
                "starts_on": pa.get("adjust_on"), "notice_period": note.get("notice_period"), "notice_on": note.get("scheduled_at"),
                "sent_at": note.get("sent_at"), "rent_before": (est.get("pre_amount") or {}).get("amount"),
                "rent_after": (est.get("post_amount") or {}).get("amount"), "currency": (est.get("post_amount") or {}).get("currency")}

    def _with_adjustment(self, read: Read, data: dict) -> Read:
        return Read(read.what, read.http_status, read.request_id, read.at, self._adjustment(data), read.error)

    def open_increases(self, subscription_id: str) -> Read:
        """The subscription's rent increases that are still draft or pending, as ids, read over every page (None when unreadable)."""
        read, items = self._list("rent increases", "/price_adjustments", "price_adjustments",
                                 {"subscription_id": subscription_id, "status": "draft,pending"})
        open_ = [pa.get("id") for pa in items if (pa.get("subscription") or {}).get("id") == subscription_id
                 and pa.get("status") in ("draft", "pending")]
        return Read(read.what, read.http_status, read.request_id, read.at, open_ if read.http_status == 200 else None, read.error)

    def draft_increase(self, subscription_id: str, percent: float, key: str) -> Read:
        """A draft increase on the operator's default notice: no notification is sent, so Stora resolves the notice itself."""
        body = {"subscription": {"id": subscription_id}, "effective_from": "next_billing_period", "percentages": {"unit_types": percent},
                "schedule": False}
        return self._with_adjustment(*self._call("draft increase", "POST", "/price_adjustments", body, key))

    def schedule_increase(self, adjustment_id: str, key: str) -> Read:
        """Schedule a draft. This sends the tenant the notice, at once when the notice date is today."""
        return self._with_adjustment(*self._call("schedule increase", "POST", f"/price_adjustments/{adjustment_id}/schedule", None, key))

    def increase(self, adjustment_id: str) -> Read:
        """The increase as neutral values: id, status, starts_on, notice_on, rent_before and rent_after in cents; 404 once deleted."""
        return self._with_adjustment(*self._call("rent increase", "GET", f"/price_adjustments/{adjustment_id}"))

    def delete_increase(self, adjustment_id: str) -> Read:
        """Delete a draft or pending increase. Stora sends no cancellation notice, and afterwards nothing shows the first notice went out."""
        read, data = self._call("delete increase", "DELETE", f"/price_adjustments/{adjustment_id}")
        return Read(read.what, read.http_status, read.request_id, read.at, None, read.error, (data.get("success") or {}).get("message"))
