"""Lien files kept by the desk: recording evidence, gathering the facts an evaluation reads, and keeping each
evaluation with those facts."""
from __future__ import annotations

import hashlib
from dataclasses import asdict

from sqlalchemy import DDL, event, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, FMSError
from .cases import record
from .db import utcnow
from .liens import RULES, CaseFacts, DateProblem, dollars, evaluate, facility_zone, local_date, on, parse_evidence
from .models import DelinquencyCase, Facility, Lease, LedgerSnapshot, LienEvaluation, LienEvidence

NOT_MONEY = ("DECLINED", "FAILED", "REJECTED")


@event.listens_for(LienEvidence, "before_update")
def _evidence_is_never_edited(mapper, connection, target):
    raise ValueError("lien evidence is never edited; record a correction that supersedes it")


@event.listens_for(LienEvidence, "before_delete")
def _evidence_is_never_deleted(mapper, connection, target):
    raise ValueError("lien evidence is never deleted; record a correction that supersedes it")


# the same rule in the database itself, so a bulk statement or another client cannot rewrite the record either
def _append_only_sql(table: str) -> list[str]:
    """Triggers that make a table append-only on SQLite: no update, no delete, and no insert that would replace an
    existing row (INSERT OR REPLACE deletes without firing delete triggers)."""
    clash = {"lien_evidence": "id = NEW.id OR (NEW.supersedes_id IS NOT NULL AND supersedes_id = NEW.supersedes_id)",
             "rule_source_rows": "id = NEW.id OR (file_sha256 = NEW.file_sha256 AND jurisdiction = NEW.jurisdiction)",
             "lien_evaluations": "id = NEW.id"}[table]
    out = [f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} "
           f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END" for op in ("UPDATE", "DELETE")]
    out.append(f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} "
               f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {clash}) BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")
    return out


for _table in ("lien_evidence", "rule_source_rows", "lien_evaluations"):
    for _sql in _append_only_sql(_table):
        event.listen(LienEvidence.metadata.tables[_table], "after_create", DDL(_sql).execute_if(dialect="sqlite"))

def ensure_append_only(engine) -> None:
    """Install the append-only triggers on a database whose tables already existed (create_all does not)."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        for table in ("lien_evidence", "rule_source_rows", "lien_evaluations"):
            for sql in _append_only_sql(table):
                conn.exec_driver_sql(sql)


EVALUATORS = {RULES: evaluate}   # only the code that is running can replay; an evaluation from other code says so   # rule version -> the evaluator that implements it; replay refuses anything else


def record_evidence(session: Session, case: DelinquencyCase, kind: str, facts: dict, entered_by: str,
                    document_name: str | None = None, document_text: str | None = None, supersedes: int | None = None) -> LienEvidence:
    parsed = parse_evidence(kind, facts)   # raises ValueError or ValidationError with the exact problem
    notice_id = getattr(parsed, "notice_id", None)
    if notice_id is not None:
        notice = session.get(LienEvidence, notice_id)
        if notice is None or notice.case_id != case.id or notice.kind != "notice":
            raise ValueError(f"notice {notice_id} is not a notice on this case")
        newer = session.scalar(select(LienEvidence.id).where(LienEvidence.supersedes_id == notice_id).limit(1))
        if newer is not None and supersedes is None:
            # a corrected notice is served again: its delivery, posting and publications are recorded for the correction
            raise ValueError(f"notice {notice_id} was corrected by notice {newer}; record this for the current notice")
    if supersedes is not None:
        old = session.get(LienEvidence, supersedes)
        if old is None or old.case_id != case.id or old.kind != kind:
            raise ValueError("a correction must supersede a record of the same kind on the same case")
        if kind == "legal_check" and old.facts.get("check") != facts.get("check"):
            raise ValueError("a legal check can only be corrected by a check on the same subject")
        if old.facts.get("notice_id") != getattr(parsed, "notice_id", None):
            raise ValueError("a correction must belong to the same notice as the record it corrects")
        newer = session.scalar(select(LienEvidence.id).where(LienEvidence.supersedes_id == supersedes).limit(1))
        if newer is not None:
            raise ValueError(f"record {supersedes} was already corrected by record {newer}; correct the latest one")
    row = LienEvidence(case_id=case.id, kind=kind, facts=parsed.model_dump(mode="json"), entered_by=entered_by,
                       document_name=document_name, document_text=document_text, supersedes_id=supersedes,
                       document_sha256=hashlib.sha256(document_text.encode()).hexdigest() if document_text else None)
    session.add(row)
    try:
        session.flush()
    except (IntegrityError, OperationalError) as exc:   # the unique index, or the append-only trigger
        session.rollback()
        raise ValueError(f"record {supersedes} was already corrected; correct the latest one") from exc
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
    evidence = current_evidence(session, case)
    zone_name = total = ledger = None
    payments_after: list[str] | None = None   # None: the payments could not be read or dated, which blocks the evaluation
    try:
        info = next((f for f in adapter.facilities() if f["facilityId"] == facility.provider_id), {})
        zone_name = info.get("timeZone") or None
        ledger = adapter.ledger(lease.provider_id)
        total = ledger.total_due
        zone = facility_zone(zone_name)
        money = [p for p in adapter.payments_for_lease(lease.provider_id) if p.status not in NOT_MONEY]
        if all(p.created_at is not None for p in money):
            payments_after = sorted({local_date(p.created_at, zone).isoformat() for p in money})
    except (FMSError, DateProblem):
        pass   # the evaluation reports exactly what could not be read
    # the ledger snapshots the notices name, and what has changed since on the charges a notice claimed
    snapshots, changes = {}, None
    latest_notice = next((r for r in reversed(evidence) if r.kind == "notice"), None)
    for r in evidence:
        sid = r.facts.get("ledger_snapshot_id") if r.kind == "notice" else None
        snap = session.get(LedgerSnapshot, sid) if sid is not None else None
        if snap is None or snap.lease_id != lease.id:
            continue
        try:
            observed = local_date(snap.observed_at, facility_zone(zone_name)).isoformat()
        except DateProblem:
            continue
        open_charges = [{"name": ((e.get("details") or {}).get("description") or (e.get("details") or {}).get("category") or "").lower(),
                         "open_cents": e.get("amount", 0) - e.get("amountPaid", 0), "due_on": (e.get("details") or {}).get("accruesOn"),
                         "category": ((e.get("details") or {}).get("category") or "").upper()}
                        for e in (snap.raw.get("bookEntries") or []) if isinstance(e, dict) and e.get("type") == "CHARGE"
                        and e.get("amount", 0) > e.get("amountPaid", 0) and not (e.get("details") or {}).get("waive")]
        snapshots[str(sid)] = {"total_due": snap.total_due, "observed_on": observed, "open_charges": open_charges}
        if ledger is not None and r is latest_notice:   # only the notice the evaluation uses
            changes = ledger_changes(snap.raw, ledger.raw)
    try:
        today = local_date(utcnow(), facility_zone(zone_name)).isoformat()
    except DateProblem:
        today = utcnow().date().isoformat()
    records = [{"id": r.id, "kind": r.kind, "facts": r.facts, "document_sha256": r.document_sha256} for r in evidence]
    mine = {r.document_sha256 for r in evidence if r.document_sha256 and r.kind != "publication"}   # one ad may name several tenants
    shared = sorted(set(session.scalars(select(LienEvidence.document_sha256).where(
        LienEvidence.case_id != case.id, LienEvidence.document_sha256.in_(mine))).all())) if mine else []
    return CaseFacts(facility.jurisdiction, zone_name, total, payments_after, today, records, snapshots, changes, RULES, shared)


def ledger_changes(then: dict, now: dict) -> list[str]:
    """What changed on the charges that were open in an earlier ledger: payments applied or reversed, waivers, amounts,
    charges that disappeared. New charges posted since are not changes to the claim."""
    then_entries, now_entries = then.get("bookEntries") or [], now.get("bookEntries") or []
    current = {e["bookEntryId"]: e for e in now_entries if isinstance(e, dict) and e.get("bookEntryId")}
    out = []
    for e in then_entries:
        if not isinstance(e, dict) or e.get("type") != "CHARGE":
            continue
        if not e.get("bookEntryId"):
            out.append("a claimed charge has no entry id, so it cannot be compared with the current ledger")
            continue
        name = ((e.get("details") or {}).get("description") or (e.get("details") or {}).get("category") or e["bookEntryId"]).lower()
        later = current.get(e["bookEntryId"])
        if later is None:
            out.append(f"the {name} charge is no longer on the ledger")
            continue
        for key, label in (("amount", "amount"), ("amountPaid", "amount paid")):
            if later.get(key, 0) != e.get(key, 0):
                out.append(f"the {name} charge's {label} changed from {dollars(e.get(key, 0))} to {dollars(later.get(key, 0))}")
        for key, label in (("accruesOn", "due date"), ("category", "category"), ("description", "description")):
            was, now = (e.get("details") or {}).get(key), (later.get("details") or {}).get(key)
            if was != now:
                out.append(f"the {name} charge's {label} changed from {on(was) if key == 'accruesOn' else was} to {on(now) if key == 'accruesOn' else now}")
        if bool((later.get("details") or {}).get("waive")) != bool((e.get("details") or {}).get("waive")):
            out.append(f"the {name} charge was waived" if (later.get("details") or {}).get("waive") else f"the waiver on the {name} charge was removed")
    known = {e.get("bookEntryId") for e in then_entries if isinstance(e, dict)}
    for e in now_entries:
        if isinstance(e, dict) and e.get("bookEntryId") not in known and e.get("type") not in (None, "CHARGE"):
            out.append(f"a {str(e.get('type')).lower()} of {e.get('amount', 0)} cents was posted after the notice")
    return out


def evaluate_case(session: Session, adapter: FMSAdapter, case: DelinquencyCase) -> LienEvaluation:
    """Evaluate now. A new record is kept only when the facts differ from the last evaluation's."""
    facts = case_facts(session, adapter, case)
    digest = facts.digest()
    last = session.scalar(select(LienEvaluation).where(LienEvaluation.case_id == case.id).order_by(LienEvaluation.id.desc()).limit(1))
    if last is not None and last.facts_digest == digest and replay(last):
        return last
    result = EVALUATORS[facts.rules](facts)
    row = LienEvaluation(case_id=case.id, rule_version=result.rule_version, state=result.state, summary=result.summary, facts=asdict(facts),
                         facts_digest=digest, findings=[asdict(f) for f in result.findings], timing=result.timing)
    session.add(row)
    session.flush()
    record(session, case, "lien file evaluated", {"evaluation": row.id, "state": result.state, "rule_version": result.rule_version})
    session.commit()
    return row


def replay(row: LienEvaluation) -> bool:
    """Run the evaluator of the recorded rule version again on the facts the evaluation kept: the same facts must give
    the same findings. A rule version this desk no longer carries cannot be replayed."""
    facts = CaseFacts(**row.facts)
    if facts.rules not in EVALUATORS:
        return False
    again = EVALUATORS[facts.rules](facts)
    return (facts.digest() == row.facts_digest and again.rule_version == row.rule_version and again.state == row.state
            and again.summary == row.summary and [asdict(f) for f in again.findings] == row.findings and again.timing == row.timing)
