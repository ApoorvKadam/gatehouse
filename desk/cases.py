"""Bring delinquent leases in from the FMS and keep each case's state derived from what the FMS reports."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, ProviderLedger
from .db import utcnow
from .models import CaseEvent, DelinquencyCase, Facility, Lease, LedgerSnapshot, PaymentCommand

IN_FLIGHT = ("SUBMITTING", "AWAITING_APPLICATION", "PENDING_AT_FMS", "OUTCOME_UNKNOWN")


def record(session: Session, case: DelinquencyCase, kind: str, detail: dict, actor: str = "desk", source: str = "desk") -> None:
    session.add(CaseEvent(case_id=case.id, kind=kind, actor=actor, source=source, detail=detail))


def snapshot(session: Session, lease: Lease, ledger: ProviderLedger) -> LedgerSnapshot:
    snap = LedgerSnapshot(lease_id=lease.id, total_due=ledger.total_due, prepaid=ledger.prepaid, raw=ledger.raw)
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


def refresh_case(session: Session, adapter: FMSAdapter, case: DelinquencyCase) -> DelinquencyCase:
    """Re-read the ledger and derive the case state. Nothing here writes to the FMS."""
    lease = session.get(Lease, case.lease_id)
    snap = snapshot(session, lease, adapter.ledger(lease.provider_id))
    case.last_snapshot_id = snap.id
    commands = session.scalars(select(PaymentCommand).where(PaymentCommand.case_id == case.id)).all()
    holds = []
    if any(c.state in IN_FLIGHT for c in commands):
        holds.append("a payment is not confirmed yet; do not take another one until it settles")
    staff = [c for c in commands if c.state == "NEEDS_STAFF"]
    if staff:
        holds.append("a payment outcome could not be confirmed with the FMS; check the FMS before any new attempt")
    returned = [c for c in commands if c.state in ("RETURNED", "REFUNDED", "REFUND_PENDING", "DISPUTED")]
    before = case.state
    if staff:
        case.state, case.owner = "needs staff", "collections lead"
    elif holds:
        case.state, case.owner = "payment in flight", "front desk"
    elif snap.total_due == 0:
        case.state, case.owner = "paid", "front desk"
        case.resolved_at = case.resolved_at or utcnow()
    else:
        case.state, case.owner = ("reopened" if returned or before == "paid" else "open"), "front desk"
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
