"""Bring delinquent leases in from the FMS and keep each case's state derived from what the FMS reports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, ProviderLedger
from .db import utcnow
from .models import FMS_REVERSAL_STATUSES, CaseEvent, DelinquencyCase, Facility, Lease, LedgerSnapshot, ObservedPayment, PaymentCommand

IN_FLIGHT = ("SUBMITTING", "AWAITING_APPLICATION", "PENDING_AT_FMS", "OUTCOME_UNKNOWN")


def record(session: Session, case: DelinquencyCase, kind: str, detail: dict, actor: str = "desk", source: str = "desk") -> None:
    session.add(CaseEvent(case_id=case.id, kind=kind, actor=actor, source=source, detail=detail))


def snapshot(session: Session, lease: Lease, ledger: ProviderLedger, read_at: datetime | None = None) -> LedgerSnapshot:
    """Keep one reading of a lease's ledger. `read_at` is when the read began, taken before the FMS was asked."""
    snap = LedgerSnapshot(lease_id=lease.id, total_due=ledger.total_due, prepaid=ledger.prepaid, raw=ledger.raw, read_at=read_at)
    session.add(snap)
    session.flush()
    return snap


def sync_facility(session: Session, adapter: FMSAdapter, facility_provider_id: str) -> list[DelinquencyCase]:
    """Open a case for every delinquent lease the FMS reports, and refresh the ones already open."""
    info = next((f for f in adapter.facilities() if f["facilityId"] == facility_provider_id), None)
    if info is None:
        return []
    facility = session.scalar(select(Facility).where(Facility.provider == adapter.provider, Facility.provider_id == facility_provider_id))
    if facility is None:
        facility = Facility(provider=adapter.provider, provider_id=facility_provider_id, name=info["name"],
                            jurisdiction=info.get("state", ""), time_zone=info.get("timeZone", "America/New_York"))
        session.add(facility)
        session.flush()
    cases = []
    for pl in adapter.delinquent_leases(facility_provider_id):
        lease = session.scalar(select(Lease).where(Lease.provider == adapter.provider, Lease.provider_id == pl.provider_id))
        if lease is None:
            lease = Lease(facility_id=facility.id, provider=adapter.provider, provider_id=pl.provider_id,
                          provider_customer_id=pl.customer_provider_id, unit_name=pl.unit_name, billing_cycle=pl.billing_cycle)
            session.add(lease)
            session.flush()
        case = current_case(session, lease)
        if case is None:
            case = DelinquencyCase(lease_id=lease.id, state="open")
            session.add(case)
            session.flush()
            record(session, case, "case opened", {"delinquent_since": pl.delinquent_since, "total_due": pl.total_due}, source="fms")
        refresh_case(session, adapter, case)
        cases.append(case)
    session.commit()
    return cases


def current_case(session: Session, lease: Lease) -> DelinquencyCase | None:
    return session.scalar(select(DelinquencyCase).where(DelinquencyCase.lease_id == lease.id, DelinquencyCase.state != "paid")
                          .order_by(DelinquencyCase.id.desc()))


def reversed_at_fms(session: Session, lease: Lease, case: DelinquencyCase, was_paid: bool) -> bool:
    """A payment on this lease, taken by anyone, came back after this case was settled.

    A reversal the desk saw happen counts when it happened after the case was settled (or opened). One the desk first
    saw already reversed has no known date: it counts only against a paid case, and only if the desk first saw it after
    the case was settled, so a settled case whose balance came back is never left looking paid, while an old reversal
    found when a new case opens does not mark that case reopened."""
    since = case.resolved_at or case.opened_at
    rows = session.scalars(select(ObservedPayment).where(ObservedPayment.status.in_(FMS_REVERSAL_STATUSES))).all()
    # the desk's own payments carry the time it saw them reversed; a slow read can store the FMS's record of the same
    # payment only later, and that later first sighting must not make an old return look new
    known = {c.provider_payment_id: c.reversed_at for c in session.scalars(select(PaymentCommand).where(
        PaymentCommand.lease_id == lease.id, PaymentCommand.reversed_at.is_not(None), PaymentCommand.provider_payment_id.is_not(None)))}
    for r in rows:
        if lease.provider_id not in r.lease_provider_ids:
            continue
        when = r.status_changed_at or known.get(r.provider_payment_id)
        if when is not None and (since is None or when >= since):
            return True
        if when is None and was_paid and case.resolved_at is not None and r.first_seen_at >= case.resolved_at:
            return True
    return False


def refresh_case(session: Session, adapter: FMSAdapter, case: DelinquencyCase) -> DelinquencyCase:
    """Re-read the ledger and derive the case state. Nothing here writes to the FMS."""
    lease = session.get(Lease, case.lease_id)
    read_at = utcnow()                      # before the FMS is asked: what it answers is no older than this
    snap = snapshot(session, lease, adapter.ledger(lease.provider_id), read_at)
    case.last_snapshot_id = snap.id
    commands = session.scalars(select(PaymentCommand).where(PaymentCommand.case_id == case.id)).all()
    holds = []
    if any(c.state in IN_FLIGHT for c in commands):
        holds.append("a payment is not confirmed yet; do not take another one until it settles")
    staff = [c for c in commands if c.state == "NEEDS_STAFF"]
    if staff:
        holds.append("a payment outcome could not be confirmed with the FMS; check the FMS before any new attempt")
    # only a reversal after the case was last settled says anything about this episode
    returned = [c for c in commands if c.state in ("RETURNED", "REFUNDED", "REFUND_PENDING", "DISPUTED")
                and (case.resolved_at is None or (c.reversed_at is not None and c.reversed_at >= case.resolved_at))]
    before = case.state
    if staff:
        case.state, case.owner = "waiting for staff", "collections lead"
    elif holds:
        case.state, case.owner = "payment in flight", "front desk"
    elif snap.total_due == 0:
        case.state, case.owner = "paid", "front desk"
        case.resolved_at = case.resolved_at or utcnow()
    else:
        reversed_here = bool(returned) or reversed_at_fms(session, lease, case, before == "paid")
        if before == "paid" and not reversed_here:
            # new charges on a settled account are the next delinquency, which gets its own case
            case.state = "paid"
        else:
            # a reversal keeps the case reopened through later attempts, declined or pending, until it is paid
            case.state, case.owner = ("reopened" if reversed_here or before in ("paid", "reopened") else "open"), "front desk"
            case.resolved_at = None
    case.hold_reasons = holds
    if case.state != before:
        record(session, case, "state changed", {"from": before, "to": case.state, "total_due": snap.total_due}, source="fms")
    return case


@dataclass
class QueueRow:
    case_id: int
    lease: str
    unit: str
    state: str
    total_due: int
    owner: str
    blocking: list[str]
    last_action: str | None


def queue(session: Session, facility_provider_id: str) -> list[QueueRow]:
    rows = []
    q = (select(DelinquencyCase, Lease).join(Lease, Lease.id == DelinquencyCase.lease_id)
         .join(Facility, Facility.id == Lease.facility_id).where(Facility.provider_id == facility_provider_id)
         .order_by(DelinquencyCase.id))
    for case, lease in session.execute(q).all():
        snap = session.get(LedgerSnapshot, case.last_snapshot_id) if case.last_snapshot_id else None
        last = session.scalar(select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.id.desc()))
        rows.append(QueueRow(case.id, lease.provider_id, lease.unit_name, case.state, snap.total_due if snap else 0,
                             case.owner, list(case.hold_reasons), last.kind if last else None))
    return rows
