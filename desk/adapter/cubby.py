"""An adapter for Cubby's Operator API shape: POST endpoints with a `where` body, a {"status", "data"} envelope,
{"code", "errors"} on failure, integer cents, and payment statuses where a decline is still an HTTP 200."""
from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from datetime import datetime, timezone

import httpx

from .base import (
    AccessCheck, Anomaly, FMSError, InboundText, NotFound, PermissionDenied, ProviderAccessCode, ProviderCustomer, ProviderExemption,
    ProviderLease, ProviderAuctionEntry, ProviderLedger, ProviderLock, ProviderPayment, ProviderTask, RentFacts, SmsOutcome, SmsPreference,
    SubmitOutcome, TaskCreateOutcome,
)

AUCTION_STATUSES = ("LISTED", "NO_BIDS", "SOLD", "PAYMENT_RECEIVED", "COMPLETE", "CANCELLED")
# the payment statuses Cubby's documentation lists for each call. Make Payment returns four; the later states (refunded, disputed, returned)
# appear only on Payments Search, so one of them in a Make Payment answer is outside that call's documentation too.
PAYMENT_SEARCH_STATUSES = ("SUCCEEDED", "DECLINED", "REJECTED", "FAILED", "REFUND_INITIATED", "REFUNDED", "DISPUTED", "DIRECT_DEBIT_RETURN", "PENDING")
MAKE_PAYMENT_STATUSES = ("SUCCEEDED", "DECLINED", "FAILED", "PENDING")
MAX_NOTES = 200


def _text(value, optional: bool = False) -> bool:
    """A non-empty string, or (optional) nothing at all."""
    return (optional and value is None) or (isinstance(value, str) and bool(value))


def _parse_time(value: str | None) -> datetime | None:
    """Times are compared as naive UTC across the desk; an offset or a trailing Z is converted, not dropped."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _safe(value) -> str:
    """A value for a note, kept only when it is shaped like a status code or a number, so an answer cannot put a name or other free text in the
    record; anything else is recorded as "(other text)"."""
    text = str(value)
    return text if re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}|-?\d{1,12}(?:\.\d{1,4})?|", text) else "(other text)"


def _json(response: httpx.Response) -> dict:
    """A proxy's HTML 502 page or a cut-off body is not an FMS answer; treat it as no body."""
    try:
        body = response.json() if response.content else {}
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


log = logging.getLogger("desk.adapter")

class CubbyAdapter:
    provider = "cubby"

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 5.0, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"},
                                  timeout=timeout_seconds, transport=transport)
        self._notes: Counter[Anomaly] = Counter()
        self._notes_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ answers the documentation does not describe
    def _note(self, endpoint: str, field: str, kind: str, value) -> None:
        """Keep a note that an answer departed from the documentation; the worker writes the notes down (desk/integration.py)."""
        self.restore_anomalies([Anomaly(endpoint, field, kind, value if kind == "not whole cents" else _safe(value))])

    def take_anomalies(self) -> list[Anomaly]:
        """The notes kept since the last call, a repeat once per time it came, which are then forgotten here."""
        with self._notes_lock:
            taken, self._notes = self._notes, Counter()
        return list(taken.elements())

    def restore_anomalies(self, notes: list[Anomaly]) -> None:
        """Keep notes (again, when saving them failed). Repeats are counted, so only the number of different notes is bounded."""
        with self._notes_lock:
            for note in notes:
                if note in self._notes or len(self._notes) < MAX_NOTES:
                    self._notes[note] += 1

    def _payment(self, p: dict, endpoint: str, listed: tuple[str, ...], amount: int = 0) -> ProviderPayment:
        """A payment as the FMS sent it, checked against what its documentation says the call returns. A status the documentation does not list is
        noted and kept exactly as written, never mapped to a known one: the payment service holds a payment with such a status for a person.
        An amount that is missing or not whole cents is refused, never guessed or rounded."""
        status = p.get("status", "")
        if status not in listed:
            self._note(endpoint, "status", "unlisted value", status)
        if "amount" not in p:
            self._note(endpoint, "amount", "missing", "")
            raise ValueError("the payment has no amount")
        if isinstance(p["amount"], bool) or not isinstance(p["amount"], int):
            # the shape, never the amount: one record per kind of change, and no payment amount in the record
            self._note(endpoint, "amount", "not whole cents", "text" if isinstance(p["amount"], str) else "decimal" if isinstance(p["amount"], float) else "other")
            raise ValueError(f"amount {p['amount']!r} is not whole cents")
        if "createdAt" not in p:
            self._note(endpoint, "createdAt", "missing", "")             # both calls document it, and a payment with no time cannot end a hold
        return ProviderPayment(provider_id=p.get("paymentId", ""), amount=p["amount"], status=status,
                               reason=p.get("reason"), created_at=_parse_time(p.get("createdAt")),
                               method_type=p.get("methodType"), customer_provider_id=p.get("customerId"))

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
        data = body.get("data")
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise FMSError(f"{path}: the answer's data is not an object")
        return data

    @staticmethod
    def _lease(raw: dict) -> ProviderLease:
        return ProviderLease(provider_id=raw["leaseId"], facility_provider_id=raw["facilityId"],
                             customer_provider_id=raw["customerId"], unit_name=raw.get("unitName") or "",
                             billing_cycle=raw.get("billingCycle") or "MONTHLY", total_due=int(raw.get("totalDue") or 0),
                             delinquent_since=raw.get("delinquencyStartDate"), unit_id=raw.get("unitId"),
                             overlock_needed=raw.get("overlockNeeded") if isinstance(raw.get("overlockNeeded"), bool) else None)

    def facilities(self) -> list[dict]:
        rows = self._read("/v1/facilities/search", {}).get("facilities")
        if not isinstance(rows, list):
            # an empty list is an answer; a missing one (a gateway page, a changed shape) is not
            raise FMSError("/v1/facilities/search: the answer has no facilities list")
        return rows

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
        try:
            # a missing balance is not a zero balance: only an explicit figure from the FMS can say nothing is due
            total_due = int(balances["totalDue"])
            prepaid = int(balances.get("prepaid") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise FMSError(f"/v1/ledgers/search: no readable totalDue for {lease_provider_id}") from exc
        return ProviderLedger(lease_provider_id=lease_provider_id, total_due=total_due, prepaid=prepaid, raw=raw)

    @staticmethod
    def _customer(c: dict) -> ProviderCustomer:
        contact = _dict(c.get("contact"))
        sms = _dict(_dict(c.get("communicationPreferences")).get("sms"))
        # a preference counts only when the FMS states both flags; a half-stated one is treated as not stated
        known = isinstance(sms.get("marketing"), bool) and isinstance(sms.get("suppressed"), bool)
        return ProviderCustomer(provider_id=c["customerId"], first_name=c.get("firstName"), last_name=c.get("lastName"),
                                phone=contact.get("phone"),
                                sms=SmsPreference(sms["marketing"], sms["suppressed"], sms.get("suppressionReason")) if known else None)

    def customer(self, customer_provider_id: str) -> ProviderCustomer:
        rows = self._read("/v1/customers/search", {"customerId": customer_provider_id}).get("customers", [])
        if not rows:
            raise NotFound(customer_provider_id)
        return self._customer(rows[0])

    def _customer_rows(self, where: dict) -> list:
        """The customers a search answered with. An answer with no list (a gateway page, a changed shape) is an error, so a
        text waiting to be matched stays pending instead of being called unmatched."""
        rows = self._read("/v1/customers/search", where).get("customers")
        if not isinstance(rows, list):
            raise FMSError("/v1/customers/search: the answer has no customers list")
        return rows

    def has_customer(self, facility_provider_id: str, customer_provider_id: str) -> bool:
        """Is this customer at this facility? Customers search by facility and customer id answers it, and it needs no
        personal data."""
        rows = self._customer_rows({"facilityId": facility_provider_id, "customerId": customer_provider_id})
        return any(isinstance(r, dict) and r.get("customerId") == customer_provider_id for r in rows)

    def customers_by_phone(self, facility_provider_id: str, phone: str) -> list[ProviderCustomer]:
        """Customers at a facility whose number contains this one. The FMS filter is a partial match, so the caller
        compares whole numbers before treating anyone as the sender."""
        return [self._customer(c) for c in self._customer_rows({"facilityId": facility_provider_id, "phone": phone})]

    def payments_for_lease(self, lease_provider_id: str) -> list[ProviderPayment]:
        data = self._read("/v1/payments/search", {"leaseId": lease_provider_id})
        if not isinstance(data.get("payments"), list):
            raise FMSError("/v1/payments/search: the answer has no payments list")
        try:
            return [self._payment(p, "payments search", PAYMENT_SEARCH_STATUSES) for p in data["payments"]]
        except (ValueError, TypeError) as exc:
            raise FMSError(f"/v1/payments/search: unreadable payment ({exc})") from exc

    def payment_methods(self, customer_provider_id: str) -> list[dict]:
        return self._read("/v1/payment-methods/search", {"customerId": customer_provider_id}).get("paymentMethods", [])

    def leases(self, facility_provider_id: str) -> list[ProviderLease]:
        return [self._lease(raw) for raw in self._read("/v1/leases/search", {"facilityId": facility_provider_id}).get("leases", [])]

    # ------------------------------------------------------------------ rent changes: reads only
    def _cents(self, endpoint: str, field: str, value, bad: list[str]) -> int | None:
        """An amount in whole cents, or None when the FMS sent none. A value of another shape is noted and named, never coerced."""
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        self._note(endpoint, field, "not whole cents", "text" if isinstance(value, str) else "decimal" if isinstance(value, float) else "other")
        bad.append(field)
        return None

    def rent_facts(self, facility_provider_id: str, today: str) -> list[RentFacts]:
        """Each lease's rent and scheduled change (Leases Search) beside its rent roll entry (the rent in the lease's own cycle, the net rate,
        the last completed change). The rent roll carries no lease id, so it is joined on the unit; a lease with no entry has in_roll False."""
        facility = next((f for f in self.facilities() if f.get("facilityId") == facility_provider_id), None)
        if facility is None:
            raise NotFound(facility_provider_id)
        leases = self._read("/v1/leases/search", {"facilityId": facility_provider_id}).get("leases")
        entries = self._read("/v1/reports/rent-roll", {"facilityIds": [facility_provider_id], "throughDate": today}).get("entries")
        if not isinstance(leases, list) or not isinstance(entries, list):
            raise FMSError("rent facts: the answer has no leases or no rent roll entries")
        by_unit = {(e.get("unitId"), e.get("customerId")): e for e in entries if isinstance(e, dict)}
        out = []
        for raw in leases:
            bad: list[str] = []
            roll = by_unit.get((raw.get("unitId"), raw.get("customerId")))
            out.append(RentFacts(
                lease_provider_id=raw["leaseId"], customer_provider_id=raw.get("customerId") or "", unit_name=raw.get("unitName") or "",
                facility_cycle=facility.get("billingCycle") if isinstance(facility.get("billingCycle"), str) else None,
                lease_cycle=raw.get("billingCycle") if isinstance(raw.get("billingCycle"), str) else None,
                rent=self._cents("leases search", "monthlyRentalAmount", raw.get("monthlyRentalAmount"), bad),
                scheduled_date=raw.get("rentChangeScheduledDate") if isinstance(raw.get("rentChangeScheduledDate"), str) else None,
                scheduled_amount=self._cents("leases search", "rentChangeNewRentAmount", raw.get("rentChangeNewRentAmount"), bad),
                in_roll=roll is not None,
                roll_rent=self._cents("rent roll", "monthlyRentalAmount", roll.get("monthlyRentalAmount"), bad) if roll else None,
                roll_effective=self._cents("rent roll", "effectiveRate", roll.get("effectiveRate"), bad) if roll else None,
                last_change=roll.get("lastRentChangeDate") if roll and isinstance(roll.get("lastRentChangeDate"), str) else None,
                unreadable=tuple(bad)))
        return out

    # ------------------------------------------------------------------ access: reads only, plus one kind of task
    def access_codes(self, facility_provider_id: str, customer_provider_id: str) -> list[ProviderAccessCode]:
        rows = self._read("/v1/access-codes/search", {"facilityId": facility_provider_id, "customerId": customer_provider_id}).get("accessCodes")
        if not isinstance(rows, list):
            raise FMSError("/v1/access-codes/search: the answer has no accessCodes list")
        return [ProviderAccessCode(code=str(r["code"]), enabled=bool(r["enabled"]), customer_provider_id=r.get("customerId") or "")
                for r in rows if r.get("customerId") == customer_provider_id]

    def check_code(self, facility_provider_id: str, code: str) -> AccessCheck:
        try:
            response = self._http.post("/v1/access-codes/check", json={"facilityId": facility_provider_id, "code": code})
        except httpx.HTTPError as exc:
            raise FMSError(f"/v1/access-codes/check: {exc.__class__.__name__}") from exc
        data = self._unwrap("/v1/access-codes/check", response)
        if not isinstance(data.get("accessAllowed"), bool):
            raise FMSError("/v1/access-codes/check: no accessAllowed in the answer")
        return AccessCheck(allowed=data["accessAllowed"], message=str(data.get("message") or ""))

    def locks_for_lease(self, lease_provider_id: str) -> list[ProviderLock]:
        rows = self._read("/v1/locks/search", {"leaseId": lease_provider_id}).get("locks")
        if not isinstance(rows, list):
            raise FMSError("/v1/locks/search: the answer has no locks list")
        return [ProviderLock(lock_id=r.get("lockId") or r.get("id") or "", status=r.get("status") or "", attached_to=r.get("attachedTo"),
                             serial=r.get("serial") or "") for r in rows]

    def exemptions_for_customer(self, customer_provider_id: str) -> list[ProviderExemption]:
        rows = self._read("/v1/delinquency-exemptions/search", {"customerId": customer_provider_id}).get("delinquencyExemptions")
        if not isinstance(rows, list):
            raise FMSError("/v1/delinquency-exemptions/search: the answer has no delinquencyExemptions list")
        return [ProviderExemption(lease_provider_id=r["leaseId"], reason=r.get("reason") or "", end_date=r.get("endDate") or "",
                                  pause_overlock=bool(r.get("pauseOverlock")), note=r.get("note"),
                                  pause_collections_messages=bool(r.get("pauseCollectionsMessages"))) for r in rows]

    def tasks_for(self, scope: str, value: str) -> list[ProviderTask]:
        rows = self._read("/v1/tasks/search", {scope: value}).get("tasks")
        if not isinstance(rows, list):
            raise FMSError("/v1/tasks/search: the answer has no tasks list")
        return [ProviderTask(task_id=r.get("taskId") or r.get("id") or "", title=r.get("title") or "", description=r.get("description"),
                             status=r.get("status") or "", customer_provider_id=r.get("customerId"), unit_id=r.get("unitId"),
                             updated_at=_parse_time(r.get("updatedAt"))) for r in rows]

    def auction_entries(self, facility_provider_id: str) -> list[ProviderAuctionEntry]:
        auctions = self._read("/v1/auctions/search", {"facilityId": facility_provider_id}).get("auctions")
        if not isinstance(auctions, list):
            raise FMSError("/v1/auctions/search: the answer has no auctions list")
        out = []
        for a in auctions:
            if (not isinstance(a, dict) or not _text(a.get("auctionId")) or not _text(a.get("auctionTime"))
                    or not isinstance(a.get("auctionedLeases"), list)):
                raise FMSError("/v1/auctions/search: an auction without its id, time or leases")
            for e in a["auctionedLeases"]:
                if (not isinstance(e, dict) or not _text(e.get("leaseId")) or e.get("status") not in AUCTION_STATUSES
                        or not _text(e.get("unitId"), optional=True) or not _text(e.get("lienStartDate"), optional=True)):
                    raise FMSError("/v1/auctions/search: an auctioned lease without its id, a known status or readable fields")
                out.append(ProviderAuctionEntry(auction_id=a["auctionId"], auction_time=a["auctionTime"], lease_provider_id=e["leaseId"],
                                                unit_id=e.get("unitId"), lien_start_date=e.get("lienStartDate"), status=e["status"]))
        return out

    def create_task(self, facility_provider_id: str, title: str, description: str, *, unit_id: str | None = None,
                    customer_provider_id: str | None = None) -> TaskCreateOutcome:
        body = {"facilityId": facility_provider_id, "title": title, "description": description}
        body.update({"unitId": unit_id} if unit_id else {"customerId": customer_provider_id})
        try:
            response = self._http.post("/v1/tasks/create", json=body)
        except httpx.TimeoutException:
            return TaskCreateOutcome(kind="lost", detail="no response before the timeout")
        except httpx.TransportError as exc:
            log.info("task request lost after sending: %s", exc.__class__.__name__)
            return TaskCreateOutcome(kind="lost", detail="the connection broke before the answer arrived")
        payload = _json(response)
        if 400 <= response.status_code < 500 and "errors" in payload:
            return TaskCreateOutcome(kind="rejected", detail=f"HTTP {response.status_code}: {'; '.join(payload.get('errors') or [])}")
        task_id = (payload.get("data") or {}).get("taskId") if response.status_code == 200 else None
        if not task_id:
            # a 5xx, a gateway page or a 200 without an id: the task may or may not exist
            return TaskCreateOutcome(kind="lost", detail=f"HTTP {response.status_code} without a task id")
        return TaskCreateOutcome(kind="created", task_id=task_id)

    # ------------------------------------------------------------------ texts
    @staticmethod
    def inbound_text(payload: dict) -> InboundText:
        """Cubby's SMS Received webhook. Raises ValueError for anything that is not one: the documented fields must all be
        there, the numbers must be phone numbers, and facilityId must be present (it is null when a number serves several
        facilities)."""
        def need(field: str) -> str:
            value = payload.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"the payload has no usable {field}")
            return value

        def number(field: str) -> str:
            value = need(field)
            if len(re.sub(r"\D", "", value)) < 10:
                raise ValueError(f"{field} is not a phone number")
            return value

        if "facilityId" not in payload or not (payload["facilityId"] is None or isinstance(payload["facilityId"], str)):
            raise ValueError("the payload has no usable facilityId")
        received = _parse_time(need("receivedAt"))
        if received is None:
            raise ValueError("the payload has no usable receivedAt")
        customer = payload.get("customer")
        named = isinstance(customer, dict) and isinstance(customer.get("customerId"), str)
        return InboundText(message_id=need("messageId"), org_id=need("orgId"), facility_provider_id=payload["facilityId"] or None, to=number("to"),
                           sender=number("from"), body=need("body"), received_at=received,
                           customer=CubbyAdapter._customer(customer) if named and isinstance(customer, dict) else None)

    def send_sms(self, facility_provider_id: str, customer_provider_id: str, body: str) -> SmsOutcome:
        """Cubby texts the customer's number on file. The call takes no idempotency key and Cubby documents no way to look a
        sent text up, so an answer that never arrives leaves the outcome unknown for good."""
        request = {"customerId": customer_provider_id, "facilityId": facility_provider_id, "body": body}
        try:
            response = self._http.post("/v1/messages/sms/send", json=request)
        except httpx.TimeoutException:
            return SmsOutcome("lost", "no response before the timeout")
        except httpx.HTTPError as exc:
            # the request may have reached Cubby before the connection broke
            log.info("text request lost after sending: %s", exc.__class__.__name__)
            return SmsOutcome("lost", "the connection broke before the answer arrived")
        payload = _json(response)
        if 400 <= response.status_code < 500 and "errors" in payload:
            return SmsOutcome("rejected", "; ".join(str(e) for e in payload.get("errors") or []) or f"HTTP {response.status_code}")
        if response.status_code != 200 or payload.get("status") != 200:
            # a 5xx, a gateway page, or a 200 that is not Cubby's envelope: the text may or may not have gone out
            return SmsOutcome("lost", f"HTTP {response.status_code} without a confirmation")
        return SmsOutcome("sent")

    # ------------------------------------------------------------------ the one write
    def make_payment(self, lease_provider_id: str, payment_method_id: str, amount: int) -> SubmitOutcome:
        body = {"leaseId": lease_provider_id, "paymentMethodId": payment_method_id, "amount": amount, "sendReceiptEmail": False}
        try:
            response = self._http.post("/v1/leases/make-payment", json=body)
        except httpx.TimeoutException:
            return SubmitOutcome(kind="lost", detail="no response before the timeout")
        except httpx.TransportError as exc:
            # the connection broke after the request may have reached the FMS
            log.info("payment request lost after sending: %s", exc.__class__.__name__)
            return SubmitOutcome(kind="lost", detail="the connection broke before the answer arrived")
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
            payment = self._payment((payload.get("data") or {}).get("payment") or {}, "make payment", MAKE_PAYMENT_STATUSES, amount)
        except (ValueError, TypeError):
            return SubmitOutcome(kind="lost", detail="a 200 answer whose payment could not be read")
        kind = {"SUCCEEDED": "accepted", "DECLINED": "declined", "FAILED": "declined", "PENDING": "pending"}.get(payment.status)
        if kind is None:
            return SubmitOutcome(kind="lost", payment=payment, detail=f"unrecognized status {payment.status!r}")
        if kind != "declined" and not payment.provider_id:
            # money may have moved, but without an id the payment can only be found by searching for it
            return SubmitOutcome(kind="lost", payment=payment, detail=f"{payment.status} with no payment id")
        return SubmitOutcome(kind=kind, payment=payment, detail=payment.reason)
