"""Access and overlocks: does what the FMS has configured, at the gate and on each unit, match what the tenant is
entitled to under the facility's recorded rule? Both directions are work: a paying tenant still shut out, and an
account past the rule whose code still opens the gate.

What Gatehouse knows, and from where:
- the rule (days overdue before lockout, and what one shared gate code does when only some of a customer's units are
  past it) is an operator's statement, recorded with who approved it; Cubby documents no such setting, so a facility
  without one gets a review item, never a verdict;
- the gate code and its check, the locks on each unit, the lease's overlockNeeded and any exemption come from the FMS,
  read in one pass per customer; a pass with a failed read changes nothing for that customer;
- a lock in OPERATOR_USE says the unit is restricted, not why.

Gatehouse never changes access. A mismatch opens a work item and one FMS task for a person; the item closes only when
a later complete reading shows the expected configuration. A task marked done does not close it. None of this says
the gate opened or a lock came off: the FMS records configuration, and the physical outcome stays unverified."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .adapter import FMSAdapter
from .adapter.base import FMSError, ProviderLease
from .db import utcnow
from .models import AccessPolicy, AccessView, Facility, Lease, WorkItem

log = logging.getLogger("desk.access")

NOT_RESTORED, MISSING, REVIEW = "access_not_restored", "access_restriction_missing", "access_review_required"
KIND_NAMES = {NOT_RESTORED: "Access not restored", MISSING: "Restriction not in place", REVIEW: "Access needs a person"}
GATE_RULES = {
    "deny_if_any_unit_restricted": "the gate code stays off while any of the customer's units here is past the rule",
    "allow_if_any_unit_in_good_standing": "the gate code works while any of the customer's units here is in good standing",
}
PHYSICAL = ("Physical outcome not verified: the FMS records configuration, not whether the gate opened or a lock came "
            "off. That needs the gate log or a staff note.")
CLOSED_BY = "FMS access reconciled"


@dataclass
class Discrepancy:
    kind: str
    subject_type: str            # facility, customer or lease
    subject_id: str
    problem: str
    evidence_source: str
    closing_evidence: str
    who_acts: str
    observed: dict = field(default_factory=dict)
    # what the FMS task asks for, and on what: (title, description, unit_id or None, customer id or None); None: no task
    task: tuple[str, str, str | None, str | None] | None = None


@dataclass
class AccessPass:
    opened: int = 0
    closed: int = 0
    failures: list[str] = field(default_factory=list)


def policy_for(session: Session, facility: Facility, today: date) -> AccessPolicy | None:
    return session.scalar(select(AccessPolicy).where(AccessPolicy.facility_id == facility.id, AccessPolicy.effective_on <= today)
                          .order_by(AccessPolicy.effective_on.desc(), AccessPolicy.id.desc()).limit(1))


def facility_today(facility: Facility) -> date:
    return datetime.now(ZoneInfo(facility.time_zone or "America/New_York")).date()


def days_overdue(lease: ProviderLease, today: date) -> int | None:
    """None when the FMS says the lease is current; raises ValueError on a date the FMS sent that is not a date."""
    if not lease.delinquent_since:
        return None
    return (today - date.fromisoformat(lease.delinquent_since[:10])).days


def ensure_lease(session: Session, facility: Facility, pl: ProviderLease, provider: str) -> Lease:
    lease = session.scalar(select(Lease).where(Lease.provider == provider, Lease.provider_id == pl.provider_id))
    if lease is None:
        lease = Lease(facility_id=facility.id, provider=provider, provider_id=pl.provider_id,
                      provider_customer_id=pl.customer_provider_id, unit_name=pl.unit_name, billing_cycle=pl.billing_cycle)
        session.add(lease)
        session.flush()
    return lease


def assess_customer(adapter: FMSAdapter, facility: Facility, policy: AccessPolicy, customer: str,
                    leases: list[ProviderLease], today: date) -> tuple[list[Discrepancy], dict[str, dict]]:
    """Read everything about one customer at one facility, then judge each unit and the shared gate code.
    Raises FMSError if any read fails: a partial picture is never judged."""
    codes = adapter.access_codes(facility.provider_id, customer)
    checks = {c.code: adapter.check_code(facility.provider_id, c.code) for c in codes}
    exemptions = {e.lease_provider_id: e for e in adapter.exemptions_for_customer(customer)}
    locks = {pl.provider_id: adapter.locks_for_lease(pl.provider_id) for pl in leases}
    out: list[Discrepancy] = []
    views: dict[str, dict] = {}
    restricted: dict[str, bool | None] = {}
    for pl in leases:
        unit = pl.unit_name or pl.provider_id
        ex = exemptions.get(pl.provider_id)
        exempt = bool(ex and ex.pause_overlock and (not ex.end_date or ex.end_date[:10] >= today.isoformat()))
        operator_locks = [k for k in locks[pl.provider_id] if k.status == "OPERATOR_USE"]
        try:
            overdue = days_overdue(pl, today)
        except ValueError:
            restricted[pl.provider_id] = None
            out.append(Discrepancy(REVIEW, "lease", pl.provider_id,
                                   f"Unit {unit}: the FMS sent a delinquency date Gatehouse cannot read ({pl.delinquent_since!r}), "
                                   "so whether the unit should be restricted is unknown.",
                                   "leases/search delinquencyStartDate", "A readable delinquency date from the FMS.",
                                   "Operations manager", {"delinquent_since": pl.delinquent_since}))
            continue
        should_restrict = overdue is not None and overdue >= policy.lockout_days and not exempt
        restricted[pl.provider_id] = should_restrict
        standing = ("current" if overdue is None else f"{overdue} days overdue")
        why_not = ("the account is current" if overdue is None
                   else f"its delinquency is paused by a {ex.reason.replace('_', ' ').lower()} exemption until {ex.end_date}" if exempt
                   else f"it is {overdue} days overdue, under the {policy.lockout_days}-day rule")
        views[pl.provider_id] = {
            "standing": standing, "overdue_days": overdue, "should_restrict": should_restrict,
            "operator_locks": [{"serial": k.serial} for k in operator_locks], "overlock_needed": pl.overlock_needed,
            "exemption": ({"reason": ex.reason, "end_date": ex.end_date, "pause_overlock": ex.pause_overlock, "note": ex.note} if ex else None),
        }
        observed = {"standing": standing, "operator_locks": [k.serial for k in operator_locks], "overlock_needed": pl.overlock_needed,
                    "exempt": exempt, "rule_days": policy.lockout_days, "policy": policy.version}
        if not should_restrict and operator_locks:
            out.append(Discrepancy(
                NOT_RESTORED, "lease", pl.provider_id,
                f"Unit {unit}: the FMS records an operator lock ({', '.join(k.serial for k in operator_locks)}) although "
                f"{why_not}. The lock says the unit is restricted, not why.",
                "locks/search by lease", "Locks Search shows no operator lock on the unit, on a later complete reading.",
                "Facility manager", observed,
                (f"Check the operator lock on unit {unit}",
                 f"The FMS records operator lock {', '.join(k.serial for k in operator_locks)} on unit {unit}, and {why_not}. "
                 "If it was put on for non-payment, take it off and return it in the FMS. If it is there for another reason, "
                 "leave it and note why.", pl.unit_id, None)))
        if should_restrict and not operator_locks:
            fms_says = (" The FMS itself calls for an overlock." if pl.overlock_needed
                        else " The FMS does not call for one either, so its own rule may differ." if pl.overlock_needed is False else "")
            out.append(Discrepancy(
                MISSING, "lease", pl.provider_id,
                f"Unit {unit} is {overdue} days overdue, past the facility's {policy.lockout_days}-day rule, and the FMS records "
                f"no operator lock on it.{fms_says} Gatehouse does not lock anyone out; a person decides.",
                "locks/search by lease", "Locks Search shows an operator lock on the unit, or the account is no longer past the rule.",
                "Facility manager", observed,
                (f"Decide on an overlock for unit {unit}",
                 f"Unit {unit} is {overdue} days overdue under the facility's {policy.lockout_days}-day rule and no operator lock is "
                 "recorded. Decide whether to overlock it; record the lock in the FMS if you do.", pl.unit_id, None)))
        if pl.overlock_needed is not None and pl.overlock_needed != should_restrict:
            out.append(Discrepancy(
                REVIEW, "lease", pl.provider_id,
                f"Unit {unit}: the recorded rule says the unit should {'be' if should_restrict else 'not be'} restricted, but the FMS "
                f"says an overlock is {'needed' if pl.overlock_needed else 'not needed'}. One of the two rules is out of date.",
                "leases/search overlockNeeded", "The FMS and the recorded rule agree on this unit.", "Operations manager", observed))
    # the shared gate code
    judged = [v for v in restricted.values() if v is not None]
    gate = {"codes": len(codes)}
    if len(codes) != 1:
        out.append(Discrepancy(
            REVIEW, "customer", customer,
            "No gate code is on file in the FMS for this customer." if not codes else
            f"{len(codes)} gate codes are on file for this customer; Gatehouse cannot tell which one the tenant uses.",
            "access-codes/search", "Exactly one gate code on file for the customer.", "Facility manager", {"codes": len(codes)}))
    elif len(judged) == len(restricted):
        check = checks[codes[0].code]
        if policy.gate_rule == "deny_if_any_unit_restricted":
            expected = not any(judged)
        elif policy.gate_rule == "allow_if_any_unit_in_good_standing":
            expected = not all(judged)
        else:
            expected = None
        gate.update({"allowed": check.allowed, "message": check.message, "expected_allowed": expected})
        units = ", ".join(pl.unit_name for pl in leases if restricted.get(pl.provider_id)) or "none"
        observed = {"check": check.message, "allowed": check.allowed, "expected_allowed": expected,
                    "restricted_units": units, "gate_rule": policy.gate_rule, "policy": policy.version}
        if expected is None:
            out.append(Discrepancy(REVIEW, "customer", customer, f"The recorded gate rule {policy.gate_rule!r} is not one Gatehouse knows.",
                                   "access policy", "A known gate rule recorded for the facility.", "Operations manager", observed))
        elif expected and not check.allowed:
            out.append(Discrepancy(
                NOT_RESTORED, "customer", customer,
                f"The customer's gate code answers \"{check.message}\" at the FMS, but under the recorded rule "
                f"({GATE_RULES[policy.gate_rule]}) it should open the gate.",
                "access-codes/check", "Check Access Code answers Access allowed, on a later complete reading.", "Facility manager", observed,
                ("Restore this customer's gate code",
                 f"The gate code answers \"{check.message}\" at the FMS, but none of the customer's units here is past the "
                 "facility's rule. Re-enable the code in the FMS.", None, customer)))
        elif not expected and check.allowed:
            out.append(Discrepancy(
                MISSING, "customer", customer,
                f"The customer's gate code still opens the gate, but unit(s) {units} are past the facility's rule "
                f"({GATE_RULES[policy.gate_rule]}). Gatehouse does not lock anyone out; a person decides.",
                "access-codes/check", "Check Access Code answers that access is not allowed, or no unit is past the rule.",
                "Facility manager", observed,
                ("Decide on gate access for this customer",
                 f"The gate code still opens the gate while unit(s) {units} are past the facility's rule. Decide whether to "
                 "disable it in the FMS.", None, customer)))
    for v in views.values():
        v["gate"] = gate
    return out, views


def reconcile_access(session: Session, adapter: FMSAdapter, facility: Facility, run_id: str) -> AccessPass:
    """One access pass over every lease at a facility. Safe to repeat: open items are updated, never duplicated."""
    result = AccessPass()
    today = facility_today(facility)
    policy = policy_for(session, facility, today)
    found: list[Discrepancy] = []
    complete: set[tuple[str, str]] = {("facility", facility.provider_id)}
    if policy is None:
        found.append(Discrepancy(
            REVIEW, "facility", facility.provider_id,
            f"No access rule is recorded for {facility.name}, so Gatehouse does not judge gate codes or overlocks here. "
            "Record the facility's lockout rule, who approved it and from when.",
            "access policy", "An approved access rule recorded for the facility.", "Operations manager"))
    else:
        try:
            leases = adapter.leases(facility.provider_id)
        except FMSError as exc:
            result.failures.append(f"leases: {exc}")
            complete.clear()
            leases = []
        by_customer: dict[str, list[ProviderLease]] = {}
        for pl in leases:
            by_customer.setdefault(pl.customer_provider_id, []).append(pl)
        for customer, cleases in sorted(by_customer.items()):
            try:
                items, views = assess_customer(adapter, facility, policy, customer, cleases, today)
            except FMSError as exc:
                result.failures.append(f"{customer}: {exc}")
                continue
            complete.add(("customer", customer))
            complete.update(("lease", pl.provider_id) for pl in cleases)
            found.extend(items)
            for pl in cleases:
                lease = ensure_lease(session, facility, pl, adapter.provider)
                view = session.get(AccessView, lease.id) or AccessView(lease_id=lease.id)
                view.observed_at, view.data = utcnow(), {**views.get(pl.provider_id, {}), "policy": policy.version,
                                                         "rule_days": policy.lockout_days, "gate_rule": policy.gate_rule}
                session.merge(view)
    _apply(session, facility, run_id, found, complete, result)
    session.commit()
    _tasks(session, adapter, facility, run_id, found, result)
    return result


def _apply(session: Session, facility: Facility, run_id: str, found: list[Discrepancy], complete: set, result: AccessPass) -> None:
    now = utcnow()
    open_items = session.scalars(select(WorkItem).where(WorkItem.run_id == run_id, WorkItem.facility_id == facility.id,
                                                        WorkItem.kind.in_(KIND_NAMES), WorkItem.state == "open")).all()
    by_key = {(w.kind, w.subject_type, w.subject_id): w for w in open_items}
    seen = set()
    for d in found:
        key = (d.kind, d.subject_type, d.subject_id)
        if key in seen:
            continue
        seen.add(key)
        item = by_key.get(key)
        if item is None:
            episodes = session.scalar(select(func.count()).select_from(WorkItem).where(
                WorkItem.run_id == run_id, WorkItem.facility_id == facility.id, WorkItem.kind == d.kind,
                WorkItem.subject_type == d.subject_type, WorkItem.subject_id == d.subject_id)) or 0
            item = WorkItem(run_id=run_id, facility_id=facility.id, kind=d.kind, subject_type=d.subject_type, subject_id=d.subject_id,
                            episode_key=str(episodes + 1), verification="FMS reported", who_acts=d.who_acts,
                            evidence_source=d.evidence_source, closing_evidence=d.closing_evidence, problem=d.problem,
                            observed=d.observed, opened_at=now, updated_at=now)
            session.add(item)
            result.opened += 1
        else:
            item.problem, item.observed, item.updated_at = d.problem, d.observed, now
    for (kind, stype, sid), item in by_key.items():
        if (kind, stype, sid) not in seen and (stype, sid) in complete:
            # the condition is gone on a complete reading of the subject: the FMS now shows what was expected
            item.state, item.closed_at, item.updated_at, item.verification = "closed", now, now, CLOSED_BY
            item.note = f"Closed: a complete FMS reading no longer shows this. {PHYSICAL}"
            result.closed += 1
    session.flush()


def _marker(item: WorkItem) -> str:
    return f"[gatehouse work item {item.run_id}-{item.id}]"


def _tasks(session: Session, adapter: FMSAdapter, facility: Facility, run_id: str, found: list[Discrepancy], result: AccessPass) -> None:
    """One FMS task per open item that asks a person to act. The intent and its marker are saved before the call; a
    call whose answer is lost is looked up by its marker, and never sent again while its outcome is unknown."""
    wanted = {(d.kind, d.subject_type, d.subject_id): d.task for d in found if d.task}
    items = session.scalars(select(WorkItem).where(WorkItem.run_id == run_id, WorkItem.facility_id == facility.id,
                                                   WorkItem.kind.in_((NOT_RESTORED, MISSING)), WorkItem.state == "open")).all()
    for item in items:
        task = wanted.get((item.kind, item.subject_type, item.subject_id))
        try:
            if item.task_state == "none" and task:
                item.task_state, item.task_marker, item.task_intent_at = "intent", _marker(item), utcnow()
                session.commit()
            if item.task_state == "intent" and task:
                title, description, unit_id, customer = task
                outcome = adapter.create_task(facility.provider_id, title, f"{description}\n\n{item.task_marker}",
                                              unit_id=unit_id, customer_provider_id=customer)
                if outcome.kind == "created":
                    item.task_state, item.task_id = "created", outcome.task_id
                elif outcome.kind == "rejected":
                    item.task_state, item.note = "rejected", f"The FMS refused the task ({outcome.detail}). Nothing was created."
                else:
                    _find_by_marker(adapter, item, task)
            elif item.task_state == "outcome_unknown" and task:
                _find_by_marker(adapter, item, task)
            if item.task_state == "created" and item.task_id:
                rows = adapter.tasks_for("taskId", item.task_id)
                item.task_status = rows[0].status if rows else "not found"
                if item.task_status == "COMPLETED":
                    item.note = ("The FMS task was marked completed, but the FMS still shows the problem on a complete "
                                 "reading. A completed task does not close this item.")
                elif item.task_status in ("ARCHIVED", "not found"):
                    item.note = (f"The FMS task is {item.task_status.lower()}, and the problem is still there. Gatehouse does not "
                                 "create another task for the same episode.")
            session.commit()
        except FMSError as exc:
            session.rollback()
            result.failures.append(f"task for item {item.id}: {exc}")


def _find_by_marker(adapter: FMSAdapter, item: WorkItem, task: tuple) -> None:
    _, _, unit_id, customer = task
    rows = adapter.tasks_for("unitId", unit_id) if unit_id else adapter.tasks_for("customerId", customer)
    match = next((t for t in rows if item.task_marker and item.task_marker in (t.description or "")), None)
    if match is not None:
        item.task_state, item.task_id, item.task_status = "created", match.task_id, match.status
        item.note = None
    else:
        item.task_state = "outcome_unknown"
        item.note = ("Creating the FMS task gave no usable answer, and no task with this item's marker is found yet. It may "
                     "still appear; Gatehouse does not create another while the outcome is unknown.")


def bound_facilities(session: Session) -> list[Facility]:
    from .models import DeskBinding
    binding = session.scalar(select(DeskBinding).limit(1))
    ids = list(binding.facility_ids) if binding and binding.facility_ids else []
    return session.scalars(select(Facility).where(Facility.provider_id.in_(ids)).order_by(Facility.id)).all() if ids else []
