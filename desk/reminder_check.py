"""The check in front of a reminder: which past-due tenants a collections text must not go to today, and why.

Gatehouse does not send reminders and does not decide when one is due. The operator's reminder tool does both, from what the FMS tells it. What
that tool cannot see is what the records say: a tenant who texted STOP, or whom the FMS marks opted out or undeliverable, must not be texted;
nor must one who is paying under an arrangement, whose payment is still being checked, or whose unit has a lien file or a sale. A text then
contradicts what staff or the law are already doing.

The worker reads all of it from the FMS once a pass and keeps one verdict per past-due lease. The Reminder check page and the API read those
verdicts and never ask the FMS. Quiet hours are not part of a verdict: they depend on the moment a text goes out, and the page says so."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .access import ensure_lease, facility_today
from .adapter import FMSAdapter, FMSError, NotFound, PermissionDenied, ProviderCustomer, ProviderLease
from .adapter.base import ProviderExemption
from .cases import IN_FLIGHT, current_case
from .db import utcnow
from .liens import dollars
from .messaging import Verdict, may_text
from .models import Facility, Lease, LienEvaluation, ObservedPayment, PaymentCommand, ReminderCheck, WorkItem
from .sales import SALE_KINDS

log = logging.getLogger("desk.reminder_check")

CLEAR, HOLD = "clear", "hold"
# what the page and the API call each reason. Only a tenant who must not be texted at all is labelled so; the rest are held for a person or for a fix.
LABELS = {"consent": "Must not be texted", "lien": "A person decides", "paused": "Texts paused in the FMS", "unpaused": "Not paused in the FMS",
          "tenant_exemption": "A person decides", "payment": "Payment in progress", "unread": "Not checked", "date": "A person decides",
          "clear": "Clear to text"}
UNREAD = "The last reading of this account failed, so the check cannot say a text is safe. It reads again on the next pass."
# a payment the desk is still working: a text now would tell a tenant who has paid, or is paying, that they owe
WORKING = IN_FLIGHT + ("NEEDS_STAFF", "NEEDS_REAUTHORIZATION")


@dataclass(frozen=True)
class Reading:
    """What one verdict rests on, read once. Each reason is a sentence, or None when it does not apply."""

    overdue: int | None                     # days past due per the FMS; None when the date could not be read
    consent: Verdict                        # may the tenant be texted at all, at any hour
    lien: str | None = None
    exemption: tuple[str, str] | None = None        # (code, sentence): paused, unpaused or tenant_exemption
    payment: str | None = None


def day(value: date | str) -> str:
    d = date.fromisoformat(value[:10]) if isinstance(value, str) else value
    return f"{d:%b} {d.day}"


def decide(r: Reading) -> dict:
    """May a collections text go to this tenant? Every reason that applies is kept; the first is the one shown, and a tenant who must not be
    texted at all outranks the rest. A reason a person has to settle holds the text: the check never guesses in favour of sending."""
    reasons = []
    if r.overdue is None:
        reasons.append(("date", "The FMS sent a delinquency date Gatehouse cannot read, so how late this account is cannot be said."))
    if not r.consent.ok:
        reasons.append(("consent", r.consent.reason))
    reasons += [(code, why) for code, why in (("lien", r.lien), r.exemption or ("exemption", None), ("payment", r.payment)) if why]
    if not reasons:
        return {"state": CLEAR, "code": "clear", "label": LABELS["clear"], "why": "Nothing in the records says this tenant should not be texted.", "also": []}
    (code, why), rest = reasons[0], reasons[1:]
    return {"state": HOLD, "code": code, "label": LABELS[code], "why": why, "also": [other for _, other in rest]}


@dataclass
class CheckPass:
    read: int = 0
    failures: list[str] = field(default_factory=list)


def _lien_words(session: Session, lease: Lease) -> str | None:
    """A lien file for the account, or a sale of the unit, whether or not the FMS still shows a balance."""
    case = current_case(session, lease)
    if case is not None and session.scalar(select(LienEvaluation.id).where(LienEvaluation.case_id == case.id).limit(1)) is not None:
        return "A lien file has begun for this account. A person decides what the tenant is told."
    sale = session.scalar(select(WorkItem.id).where(WorkItem.facility_id == lease.facility_id, WorkItem.kind.in_(SALE_KINDS),
                                                    WorkItem.state == "open", WorkItem.subject_id.like(f"%/{lease.provider_id}")).limit(1))
    return "The unit is in a lien sale. A person decides what the tenant is told." if sale is not None else None


def _payment_words(session: Session, leases: list[Lease]) -> str | None:
    """A payment still being worked for any of the tenant's units: one payment can cover several of them, so the whole tenant waits."""
    case_ids = [c.id for c in (current_case(session, lease) for lease in leases) if c is not None]
    working = session.scalars(select(PaymentCommand).where(PaymentCommand.case_id.in_(case_ids), PaymentCommand.state.in_(WORKING))).first() if case_ids else None
    if working is not None:
        return f"A payment of {dollars(working.amount)} is still being checked, so a text now could be wrong."
    mine = {lease.provider_id for lease in leases}
    pending = next((p for p in session.scalars(select(ObservedPayment).where(ObservedPayment.status == "PENDING")) if mine & set(p.lease_provider_ids or [])), None)
    return f"The FMS shows a pending payment of {dollars(pending.amount)}, so a text now could be wrong." if pending is not None else None


def _is_active(e: ProviderExemption, today: date) -> bool:
    """An exemption with no end date, one that ends today or later, or one whose end date nobody can read: the check holds the text rather than
    guess that it ended."""
    if not e.end_date:
        return True
    try:
        return date.fromisoformat(e.end_date[:10]) >= today
    except ValueError:
        return True


def _until(e: ProviderExemption) -> str:
    try:
        return f" until {day(e.end_date)}" if e.end_date else ""
    except ValueError:
        return ""


def _exemption_words(session: Session, lease_id: str, exemptions: list[ProviderExemption], today: date) -> tuple[str, str] | None:
    """An exemption on this unit, or on another unit of the same tenant. The flag Cubby documents says whether the FMS's own collections texts are
    paused; an arrangement that does not pause them is a catch in itself, because the FMS will keep texting a tenant who is on a payment plan."""
    active = [e for e in exemptions if _is_active(e, today)]
    own = next((e for e in active if e.lease_provider_id == lease_id), None)
    other = next((e for e in active if e.lease_provider_id != lease_id), None)
    e = own or other
    if e is None:
        return None
    until = _until(e)
    what = "a payment arrangement" if e.reason == "PAYMENT_ARRANGEMENT" else f"an exemption ({e.reason.replace('_', ' ').lower()})"
    if own is None:
        unit = session.scalar(select(Lease.unit_name).where(Lease.provider_id == e.lease_provider_id)) or "another unit"
        return "tenant_exemption", f"This tenant has {what} on unit {unit}{until}. A person decides whether a text about this unit goes."
    if own.pause_collections_messages:
        return "paused", f"The FMS has {what} on this unit{until} and has paused collections texts for it."
    return "unpaused", (f"The FMS has {what} on this unit{until} but has not paused collections texts for it. Pause them in the FMS, "
                        "or its reminder tool will still text this tenant.")


def _customer(adapter: FMSAdapter, customer_id: str) -> ProviderCustomer | None:
    try:
        return adapter.customer(customer_id)
    except (NotFound, PermissionDenied):
        return None


def _overdue(pl: ProviderLease, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(pl.delinquent_since[:10])).days if pl.delinquent_since else None
    except ValueError:
        return None


def _unread(session: Session, lease: Lease, pl: ProviderLease, today: date) -> None:
    """The reading of this account failed, so what was said about it before is not trusted: the text is held until a reading succeeds."""
    check = session.get(ReminderCheck, lease.id) or ReminderCheck(lease_id=lease.id)
    check.observed_at = utcnow()
    check.data = {"state": HOLD, "code": "unread", "label": LABELS["unread"], "why": UNREAD, "also": [], "unit": pl.unit_name, "owed": pl.total_due,
                  "overdue_days": _overdue(pl, today), "tenant": (check.data or {}).get("tenant")}
    session.merge(check)


def reconcile_reminder_checks(session: Session, adapter: FMSAdapter, facility: Facility) -> CheckPass:
    """One reading of every past-due lease at a facility, a verdict each, kept for the page and the API. A lease whose reading fails is named in
    `failures` and held, never judged on a partial picture and never left saying clear. A lease that is no longer past due loses its verdict."""
    result = CheckPass()
    today = facility_today(facility)
    try:
        past_due = [pl for pl in adapter.delinquent_leases(facility.provider_id) if pl.total_due > 0]
    except FMSError as exc:
        result.failures.append(f"past-due leases: {exc}")
        for old in session.scalars(select(ReminderCheck).join(Lease, Lease.id == ReminderCheck.lease_id).where(Lease.facility_id == facility.id)):
            if old.data.get("state") == CLEAR:
                old.data = {**old.data, "state": HOLD, "code": "unread", "label": LABELS["unread"], "why": UNREAD, "also": []}
        session.commit()
        return result
    kept: set[int] = set()
    exemptions: dict[str, list[ProviderExemption]] = {}
    for pl in past_due:
        lease = ensure_lease(session, facility, pl, adapter.provider)
        kept.add(lease.id)
        try:
            customer = _customer(adapter, pl.customer_provider_id)
            if pl.customer_provider_id not in exemptions:
                exemptions[pl.customer_provider_id] = adapter.exemptions_for_customer(pl.customer_provider_id)
        except FMSError as exc:
            result.failures.append(f"{pl.provider_id}: {exc}")
            _unread(session, lease, pl, today)
            continue
        theirs = session.scalars(select(Lease).where(Lease.facility_id == facility.id, Lease.provider_customer_id == pl.customer_provider_id)).all()
        reading = Reading(overdue=_overdue(pl, today), consent=may_text(session, customer, zone=facility.time_zone), lien=_lien_words(session, lease),
                          exemption=_exemption_words(session, pl.provider_id, exemptions[pl.customer_provider_id], today),
                          payment=_payment_words(session, theirs))
        view = session.get(ReminderCheck, lease.id) or ReminderCheck(lease_id=lease.id)
        view.observed_at = utcnow()
        view.data = {**decide(reading), "unit": pl.unit_name, "owed": pl.total_due, "overdue_days": reading.overdue,
                     "tenant": customer.first_name if customer else None}
        session.merge(view)
        result.read += 1
    for stale in session.scalars(select(ReminderCheck).join(Lease, Lease.id == ReminderCheck.lease_id).where(Lease.facility_id == facility.id)).all():
        if stale.lease_id not in kept:
            session.delete(stale)
    session.commit()
    return result
