"""Lien files kept by the desk: recording evidence, gathering the facts an evaluation reads, and keeping each
evaluation with those facts."""
from __future__ import annotations

import hashlib
from dataclasses import asdict

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, FMSError
from .cases import record
from .db import utcnow
from .liens import CaseFacts, DateProblem, evaluate, facility_zone, local_date, parse_evidence
from .models import DelinquencyCase, Facility, Lease, LienEvaluation, LienEvidence

NOT_MONEY = ("DECLINED", "FAILED", "REJECTED")


@event.listens_for(LienEvidence, "before_update")
def _evidence_is_never_edited(mapper, connection, target):
    raise ValueError("lien evidence is never edited; record a correction that supersedes it")


def record_evidence(session: Session, case: DelinquencyCase, kind: str, facts: dict, entered_by: str,
                    document_name: str | None = None, document_text: str | None = None, supersedes: int | None = None) -> LienEvidence:
    parsed = parse_evidence(kind, facts)   # raises ValueError or ValidationError with the exact problem
    if supersedes is not None:
        old = session.get(LienEvidence, supersedes)
        if old is None or old.case_id != case.id or old.kind != kind:
            raise ValueError("a correction must supersede a record of the same kind on the same case")
    row = LienEvidence(case_id=case.id, kind=kind, facts=parsed.model_dump(mode="json"), entered_by=entered_by,
                       document_name=document_name, document_text=document_text, supersedes_id=supersedes,
                       document_sha256=hashlib.sha256(document_text.encode()).hexdigest() if document_text else None)
    session.add(row)
    session.flush()
    record(session, case, "lien evidence recorded", {"evidence": row.id, "kind": kind, "supersedes": supersedes}, actor=entered_by)
    session.commit()
    return row


def current_evidence(session: Session, case: DelinquencyCase) -> list[LienEvidence]:
    rows = session.scalars(select(LienEvidence).where(LienEvidence.case_id == case.id).order_by(LienEvidence.id)).all()
    replaced = {r.supersedes_id for r in rows if r.supersedes_id is not None}
    return [r for r in rows if r.id not in replaced]


def case_facts(session: Session, adapter: FMSAdapter, case: DelinquencyCase) -> CaseFacts:
    """Read what the evaluation needs. The facility's zone comes from the FMS every time, with no default."""
    lease = session.get(Lease, case.lease_id)
    facility = session.get(Facility, lease.facility_id)
    zone_name = total = None
    payments_after: list[str] = []
    try:
        info = next((f for f in adapter.facilities() if f["facilityId"] == facility.provider_id), {})
        zone_name = info.get("timeZone") or None
        total = adapter.ledger(lease.provider_id).total_due
        zone = facility_zone(zone_name)
        payments_after = sorted({local_date(p.created_at, zone).isoformat() for p in adapter.payments_for_lease(lease.provider_id)
                                 if p.created_at is not None and p.status not in NOT_MONEY})
    except (FMSError, DateProblem):
        pass   # the evaluation reports exactly what could not be read
    try:
        today = local_date(utcnow(), facility_zone(zone_name)).isoformat()
    except DateProblem:
        today = utcnow().date().isoformat()
    evidence = [{"id": r.id, "kind": r.kind, "facts": r.facts} for r in current_evidence(session, case)]
    return CaseFacts(facility.jurisdiction, zone_name, total, payments_after, today, evidence)


def evaluate_case(session: Session, adapter: FMSAdapter, case: DelinquencyCase) -> LienEvaluation:
    """Evaluate now. A new record is kept only when the facts differ from the last evaluation's."""
    facts = case_facts(session, adapter, case)
    digest = facts.digest()
    last = session.scalar(select(LienEvaluation).where(LienEvaluation.case_id == case.id).order_by(LienEvaluation.id.desc()).limit(1))
    if last is not None and last.facts_digest == digest:
        return last
    result = evaluate(facts)
    row = LienEvaluation(case_id=case.id, rule_version=result.rule_version, state=result.state, facts=asdict(facts),
                         facts_digest=digest, findings=[asdict(f) for f in result.findings], timing=result.timing)
    session.add(row)
    session.flush()
    record(session, case, "lien file evaluated", {"evaluation": row.id, "state": result.state, "rule_version": result.rule_version})
    session.commit()
    return row


def replay(row: LienEvaluation) -> bool:
    """Run the evaluator again on the facts an evaluation kept: the same facts must give the same findings."""
    again = evaluate(CaseFacts(**row.facts))
    return again.state == row.state and [asdict(f) for f in again.findings] == row.findings and again.timing == row.timing
