"""An adapter for Cubby's Operator API shape: POST endpoints with a `where` body, a {"status", "data"} envelope,
{"code", "errors"} on failure, integer cents, and payment statuses where a decline is still an HTTP 200."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .base import (
    FMSError, NotFound, PermissionDenied, ProviderCustomer, ProviderLease, ProviderLedger, ProviderPayment, SubmitOutcome,
)


def _parse_time(value: str | None) -> datetime | None:
    """Times are compared as naive UTC across the desk; an offset or a trailing Z is converted, not dropped."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _payment(p: dict, amount: int = 0) -> ProviderPayment:
    return ProviderPayment(provider_id=p.get("paymentId", ""), amount=int(p.get("amount", amount)), status=p.get("status", ""),
                           reason=p.get("reason"), created_at=_parse_time(p.get("createdAt")),
                           method_type=p.get("methodType"), customer_provider_id=p.get("customerId"))


def _json(response: httpx.Response) -> dict:
    """A proxy's HTML 502 page or a cut-off body is not an FMS answer; treat it as no body."""
    try:
        body = response.json() if response.content else {}
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class CubbyAdapter:
    provider = "cubby"

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 5.0, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"},
                                  timeout=timeout_seconds, transport=transport)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ reads
    def _read(self, path: str, where: dict) -> dict:
        try:
            response = self._http.post(path, json={"where": where})
        except httpx.HTTPError as exc:
            raise FMSError(f"{path}: {exc.__class__.__name__}") from exc
        return self._unwrap(path, response)

    @staticmethod
    def _unwrap(path: str, response: httpx.Response) -> dict:
        body = _json(response)
        if response.status_code == 404:
            raise NotFound("; ".join(body.get("errors", [])) or path)
        if response.status_code in (401, 403):
            raise PermissionDenied("; ".join(body.get("errors", [])) or path)
        if response.status_code != 200:
            raise FMSError(f"{path}: HTTP {response.status_code} {body.get('errors')}")
        return body.get("data") or {}

    @staticmethod
    def _lease(raw: dict) -> ProviderLease:
        return ProviderLease(provider_id=raw["leaseId"], facility_provider_id=raw["facilityId"],
                             customer_provider_id=raw["customerId"], unit_name=raw.get("unitName") or "",
                             billing_cycle=raw.get("billingCycle") or "MONTHLY", total_due=int(raw.get("totalDue") or 0),
                             delinquent_since=raw.get("delinquencyStartDate"))

    def facilities(self) -> list[dict]:
        return self._read("/v1/facilities/search", {}).get("facilities", [])

    def delinquent_leases(self, facility_provider_id: str) -> list[ProviderLease]:
        data = self._read("/v1/leases/search", {"facilityId": facility_provider_id, "delinquent": True})
        return [self._lease(raw) for raw in data.get("leases", [])]

    def lease(self, lease_provider_id: str) -> ProviderLease:
        leases = self._read("/v1/leases/search", {"leaseId": lease_provider_id}).get("leases", [])
        if not leases:
            raise NotFound(lease_provider_id)
        return self._lease(leases[0])

    def ledger(self, lease_provider_id: str) -> ProviderLedger:
        ledgers = self._read("/v1/ledgers/search", {"leaseId": lease_provider_id}).get("ledgers", [])
        if not ledgers:
            raise NotFound(lease_provider_id)
        raw = ledgers[0]
        balances = raw.get("balances") or {}
        return ProviderLedger(lease_provider_id=lease_provider_id, total_due=int(balances.get("totalDue", 0)),
                              prepaid=int(balances.get("prepaid", 0)), raw=raw)

    def customer(self, customer_provider_id: str) -> ProviderCustomer:
        rows = self._read("/v1/customers/search", {"customerId": customer_provider_id}).get("customers", [])
        if not rows:
            raise NotFound(customer_provider_id)
        c = rows[0]
        return ProviderCustomer(provider_id=c["customerId"], first_name=c.get("firstName"), last_name=c.get("lastName"),
                                phone=c.get("phone"))

    def payments_for_lease(self, lease_provider_id: str) -> list[ProviderPayment]:
        data = self._read("/v1/payments/search", {"leaseId": lease_provider_id})
        try:
            return [_payment(p) for p in data.get("payments", [])]
        except (ValueError, TypeError) as exc:
            raise FMSError(f"/v1/payments/search: unreadable payment ({exc})") from exc

    def payment_methods(self, customer_provider_id: str) -> list[dict]:
        return self._read("/v1/payment-methods/search", {"customerId": customer_provider_id}).get("paymentMethods", [])

    # ------------------------------------------------------------------ the one write
    def make_payment(self, lease_provider_id: str, payment_method_id: str, amount: int) -> SubmitOutcome:
        body = {"leaseId": lease_provider_id, "paymentMethodId": payment_method_id, "amount": amount, "sendReceiptEmail": False}
        try:
            response = self._http.post("/v1/leases/make-payment", json=body)
        except httpx.TimeoutException:
            return SubmitOutcome(kind="lost", detail="no response before the timeout")
        except httpx.TransportError as exc:
            # the connection broke after the request may have reached the FMS
            return SubmitOutcome(kind="lost", detail=f"connection failed: {exc.__class__.__name__}")
        payload = _json(response)
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        if 400 <= response.status_code < 500 and "errors" in payload and "code" in payload:
            # Cubby refused the request itself (unknown lease or card, another customer's card, bad amount): its
            # documentation says nothing was attempted
            return SubmitOutcome(kind="rejected", detail=f"HTTP {response.status_code}", errors=errors)
        if response.status_code != 200:
            # A 5xx says the FMS failed while handling the request, possibly after the charge, and a gateway's error
            # page is not the FMS speaking at all. Wrongly calling it "nothing charged" invites a second charge;
            # wrongly holding it costs a staff check. So it is looked up, and staff can release it.
            return SubmitOutcome(kind="lost", detail=f"HTTP {response.status_code}", errors=errors)
        try:
            payment = _payment((payload.get("data") or {}).get("payment") or {}, amount)
        except (ValueError, TypeError):
            return SubmitOutcome(kind="lost", detail="a 200 answer whose payment could not be read")
        kind = {"SUCCEEDED": "accepted", "DECLINED": "declined", "FAILED": "declined", "PENDING": "pending"}.get(payment.status)
        if kind is None:
            return SubmitOutcome(kind="lost", payment=payment, detail=f"unrecognized status {payment.status!r}")
        if kind != "declined" and not payment.provider_id:
            # money may have moved, but without an id the payment can only be found by searching for it
            return SubmitOutcome(kind="lost", payment=payment, detail=f"{payment.status} with no payment id")
        return SubmitOutcome(kind=kind, payment=payment, detail=payment.reason)
