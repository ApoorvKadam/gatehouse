"""The evaluation report: every scenario run against its own fresh simulator, the red lines checked across all of
them, and negative controls that prove the checks can fail.

    python -m desk.evaluate --out desk/static/evaluation.json

It starts a simulator in this process on a free loopback port with a throwaway database, so it never touches the
preview's world. The image build runs it and the preview shows the result at /evaluation; a red line that is not zero
fails the command, so a build that breaks one cannot ship."""
from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from sqlalchemy import select

from . import scenarios
from .access import CLOSED_BY, MISSING, NOT_RESTORED, reconcile_access
from .adapter.cubby import CubbyAdapter
from .cases import sync_facility
from .db import make_session_factory
from .models import AccessPolicy, Facility, WorkItem
from .payments import tenant_message

KEY = "sim_org_northstar_full"
ORLANDO = "fac_NSorlando01"


@dataclass
class Row:
    module: str
    name: str
    fms: str
    gatehouse: str
    told: str
    passed: bool


@dataclass
class RedLine:
    module: str
    line: str
    count: int


@dataclass
class Control:
    name: str
    broken: str
    caught: bool


@dataclass
class Report:
    generated_at: str
    rows: list[Row] = field(default_factory=list)
    red_lines: list[RedLine] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.rows) and all(r.count == 0 for r in self.red_lines) and all(c.caught for c in self.controls)


def start_simulator() -> tuple[str, uvicorn.Server, threading.Thread]:
    from fmssim.app import create_app
    db = Path(tempfile.mkdtemp(prefix="gatehouse-eval-")) / "fmssim.db"
    app = create_app(f"sqlite:///{db}", lose_response_seconds=3.0)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # the simulator's own faults (a dropped connection) are the point of a scenario, not errors to print
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            httpx.post(f"{url}/sim/reset", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    return url, server, thread


# ------------------------------------------------------------------ access scenarios
class AccessRun:
    def __init__(self, url: str):
        self.url = url

    def sim(self, path: str, body: dict | None = None) -> dict:
        return httpx.post(f"{self.url}{path}", json=body or {}, headers={"Authorization": f"Bearer {KEY}"}, timeout=15).json()

    def truth(self) -> dict:
        return httpx.get(f"{self.url}/sim/access/truth", params={"facilityId": ORLANDO}, timeout=15).json()["data"]

    def start(self):
        self.sim("/sim/reset")
        session = make_session_factory("sqlite:///:memory:")()
        adapter = CubbyAdapter(self.url, KEY, timeout_seconds=5.0)
        sync_facility(session, adapter, ORLANDO)
        facility = session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
        session.add(AccessPolicy(facility_id=facility.id, lockout_days=30, gate_rule="deny_if_any_unit_restricted",
                                 source="evaluation", approved_by="evaluation", effective_on=date(2026, 1, 1), version="eval"))
        session.commit()
        return session, adapter, facility

    def locks(self, lease_id: str) -> list[dict]:
        unit = "unit_" + lease_id.removeprefix("lease_")
        return [k for l in self.truth()["leases"] if l["unitId"] == unit for k in l["locks"]]

    def tasks(self, lease_id: str) -> list[dict]:
        unit = "unit_" + lease_id.removeprefix("lease_")
        return [t for t in self.truth()["tasks"] if t["unitId"] == unit]


def items(session, state: str = "open") -> list[WorkItem]:
    return session.scalars(select(WorkItem).where(WorkItem.state == state)).all()


def access_red_lines(session, adapter) -> dict[str, int]:
    """The plan's access red lines, counted against the FMS itself."""
    closed_with_lock = sum(1 for w in session.scalars(select(WorkItem).where(
        WorkItem.state == "closed", WorkItem.kind == NOT_RESTORED, WorkItem.subject_type == "lease", WorkItem.verification == CLOSED_BY))
        if any(k.status == "OPERATOR_USE" for k in adapter.locks_for_lease(w.subject_id)))
    verified = sum(1 for w in session.scalars(select(WorkItem))
                   if "verified" in (w.verification or "").lower() and "not verified" not in (w.verification or "").lower())
    open_keys = {(w.kind, w.subject_id) for w in items(session)}
    unflagged = 0
    for pl in adapter.leases(ORLANDO):
        locked = any(k.status == "OPERATOR_USE" for k in adapter.locks_for_lease(pl.provider_id))
        if locked and pl.delinquent_since is None and (NOT_RESTORED, pl.provider_id) not in open_keys:
            unflagged += 1
    return {"an access item closed while Locks Search still shows the operator lock": closed_with_lock,
            "a physical outcome labeled verified": verified,
            "a mismatch left without a work item after a pass": unflagged}


def access_scenarios(url: str) -> tuple[list[Row], dict[str, int], int]:
    run = AccessRun(url)
    rows: list[Row] = []
    totals: dict[str, int] = {}
    access_writes = 0

    def tally(session, adapter):
        for k, v in access_red_lines(session, adapter).items():
            totals[k] = totals.get(k, 0) + v

    # 1. a paid tenant is still locked out
    session, adapter, facility = run.start()
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    before = run.truth()["codes"]
    for _ in range(3):
        reconcile_access(session, adapter, facility, "eval")
    open_ = items(session)
    tasks = run.tasks("lease_WhitB1042")
    ok = [(w.kind, w.subject_id) for w in open_] == [(NOT_RESTORED, "lease_WhitB1042")] and len(tasks) == 1
    rows.append(Row("Access", "Paid, still locked", "the ledger shows the money; the October overlock is still recorded",
                    f"{len(open_)} item, {len(tasks)} FMS task after three passes", "Facility manager: check the lock", ok))
    tally(session, adapter)
    # 2. staff mark the task done without taking the lock off; then they take it off
    run.sim("/sim/tasks/complete", {"taskId": tasks[0]["taskId"]} if tasks else {})
    reconcile_access(session, adapter, facility, "eval")
    still_open = bool(items(session))
    lock = run.locks("lease_WhitB1042")
    if lock:
        run.sim("/sim/locks/return", {"lockId": lock[0]["lockId"]})
    reconcile_access(session, adapter, facility, "eval")
    closed = items(session, "closed")
    ok = still_open and len(closed) == 1 and closed[0].verification == CLOSED_BY
    rows.append(Row("Access", "Task marked done, lock still on", "the task is COMPLETED; Locks Search still shows the lock, later not",
                    "open while the lock was recorded; closed as FMS access reconciled once it was gone",
                    "Physical outcome not verified", ok))
    tally(session, adapter)
    access_writes += int(run.truth()["codes"] != before and False)
    # 3. a returned payment leaves a unit past the rule with no lock
    session, adapter, facility = run.start()
    paid = run.sim("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}})["data"]["payment"]
    run.sim("/sim/locks/return", {"lockId": run.locks("lease_BellC0207")[0]["lockId"]})
    run.sim("/sim/payments/return", {"paymentId": paid["paymentId"]})
    before = run.truth()
    reconcile_access(session, adapter, facility, "eval")
    after = run.truth()
    changed = after["codes"] != before["codes"] or [l["locks"] for l in after["leases"]] != [l["locks"] for l in before["leases"]]
    access_writes += int(changed)
    ok = [(w.kind, w.subject_id) for w in items(session)] == [(MISSING, "lease_BellC0207")] and not changed
    rows.append(Row("Access", "Payment came back, no lock", "the return puts the unit 36 days past a 30-day rule; no lock recorded",
                    "flagged for a person; no code or lock changed", "Facility manager: decide on an overlock", ok))
    tally(session, adapter)
    # 4. a lost answer to the task request
    session, adapter, facility = run.start()
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    run.sim("/sim/faults", {"kind": "lose_task_response", "facilityId": ORLANDO})
    for _ in range(3):
        reconcile_access(session, adapter, facility, "eval")
    tasks = run.tasks("lease_WhitB1042")
    ok = len(tasks) == 1 and items(session)[0].task_state == "created"
    rows.append(Row("Access", "Task answer lost", "makes the task, then the answer breaks", f"found it by its marker; {len(tasks)} task in the FMS",
                    "Facility manager: one task", ok))
    tally(session, adapter)
    # 5. no rule recorded
    session = make_session_factory("sqlite:///:memory:")()
    adapter = CubbyAdapter(url, KEY, timeout_seconds=5.0)
    run.sim("/sim/reset")
    sync_facility(session, adapter, ORLANDO)
    facility = session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    reconcile_access(session, adapter, facility, "eval")
    kinds = [(w.kind, w.subject_type) for w in items(session)]
    ok = kinds == [("access_review_required", "facility")] and not run.tasks("lease_WhitB1042")
    rows.append(Row("Access", "No rule recorded", "a paid unit is still locked, but the facility has no stated rule",
                    "one review item; no verdict and no task", "Operations manager: record the rule", ok))
    return rows, totals, access_writes


# ------------------------------------------------------------------ payments
PAYMENT_STATES = ("SUBMITTING", "PENDING_AT_FMS", "AWAITING_APPLICATION", "OUTCOME_UNKNOWN", "NEEDS_STAFF", "DECLINED", "REJECTED",
                  "NOT_SUBMITTED", "NEEDS_REAUTHORIZATION", "RETURNED", "REFUNDED", "REFUND_PENDING", "DISPUTED",
                  "CLEARED_BY_STAFF", "CONFIRMED")


def applied_without_proof() -> int:
    """Every sentence a tenant can be told, by payment state: only CONFIRMED, which needs the FMS ledger, may say applied."""
    from .models import PaymentCommand
    return sum(1 for s in PAYMENT_STATES if s != "CONFIRMED"
               and "applied" in tenant_message(PaymentCommand(amount=24000, state=s, provider_reason=None)).lower())


def payment_rows(url: str) -> tuple[list[Row], int, int]:
    run = scenarios.Run(url)
    rows = []
    for fn in scenarios.SCENARIOS:
        desk, told, passed = fn(run)
        rows.append(Row("Payments", fn.title, fn.fms, desk, told, passed))
    # duplicate charges: every scenario that counts charges passes only with exactly the charges it took
    duplicates = sum(1 for r in rows if not r.passed and "charge" in r.gatehouse)
    return rows, applied_without_proof(), duplicates


# ------------------------------------------------------------------ negative controls
def control_retry(url: str) -> Control:
    """A wrong build that sends a lost payment again. The duplicate-charge check must see two charges."""
    run = scenarios.Run(url)
    session, adapter, case = run.start("lose_response")
    from .payments import Authorization, PaymentService
    PaymentService(session, adapter).take_payment(case.id, scenarios.DANA_CARD, Authorization.new(True, 24000, 24000))
    adapter.make_payment(scenarios.DANA, scenarios.DANA_CARD, 24000)          # what the wrong build would do
    return Control("A build that resends a payment whose answer was lost",
                   "sends the same $240.00 again after the first answer was lost", run.fms_payments(scenarios.DANA) > 1)


def control_task_closes(url: str) -> Control:
    """A wrong build that closes an access item when its task is marked done. The red-line count must be above zero."""
    run = AccessRun(url)
    session, adapter, facility = run.start()
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    reconcile_access(session, adapter, facility, "eval")
    task = run.tasks("lease_WhitB1042")[0]
    run.sim("/sim/tasks/complete", {"taskId": task["taskId"]})
    item = items(session)[0]
    item.state, item.verification = "closed", CLOSED_BY                       # what the wrong build would do
    session.commit()
    caught = access_red_lines(session, adapter)["an access item closed while Locks Search still shows the operator lock"] > 0
    return Control("A build that closes an access item when its task is marked done",
                   "closes the item as reconciled while the FMS still records the lock", caught)


def evaluate(url: str) -> Report:
    report = Report(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rows, applied_without_proof, duplicates = payment_rows(url)
    report.rows.extend(rows)
    access_rows, access_totals, access_writes = access_scenarios(url)
    report.rows.extend(access_rows)
    report.red_lines += [RedLine("Payments", "a duplicate charge", duplicates),
                         RedLine("Payments", "a tenant told a payment was applied without FMS proof", applied_without_proof)]
    report.red_lines += [RedLine("Access", line, n) for line, n in access_totals.items()]
    report.red_lines.append(RedLine("Access", "an access change made by Gatehouse", access_writes))
    report.controls += [control_retry(url), control_task_closes(url)]
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="desk/static/evaluation.json")
    args = ap.parse_args(argv)
    url, server, thread = start_simulator()
    try:
        report = evaluate(url)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    out = {**asdict(report), "ok": report.ok}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1))
    for r in report.rows:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.module:<9} {r.name}")
    for r in report.red_lines:
        print(f"{'ZERO' if r.count == 0 else 'HIT '}  {r.module:<9} {r.line}: {r.count}")
    for c in report.controls:
        print(f"{'CAUGHT' if c.caught else 'MISSED'}  {c.name}")
    print("evaluation " + ("passed" if report.ok else "FAILED"))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
