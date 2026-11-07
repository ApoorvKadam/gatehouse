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
    """A simulator of our own: the port is bound here and handed to the server, and nothing is sent until that server
    reports it started, so a reset can never reach another simulator that happened to take the port."""
    from fmssim.app import create_app
    db = Path(tempfile.mkdtemp(prefix="gatehouse-eval-")) / "fmssim.db"
    app = create_app(f"sqlite:///{db}", lose_response_seconds=3.0)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    # the simulator's own faults (a dropped connection) are the point of a scenario, not errors to print
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(400):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("the evaluation's simulator did not start")
        time.sleep(0.025)
    else:
        raise RuntimeError("the evaluation's simulator did not start in time")
    return f"http://127.0.0.1:{port}", server, thread


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
        recording = Recording()
        adapter = CubbyAdapter(self.url, KEY, timeout_seconds=5.0, transport=recording)
        adapter.recording = recording
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


RULE_DAYS = 30   # the rule the evaluation records for the facility (deny the gate code while any unit is past it)


def expected_mismatches(adapter) -> set[tuple[str, str, str]]:
    """Every mismatch the FMS shows against the evaluation's rule, worked out here, apart from desk/access.py: a unit
    past the rule with no operator lock, a unit not past it with one, a gate code that disagrees with the rule.
    Units where the FMS's own overlockNeeded disagrees with the rule are left to the review path."""
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).date()   # Orlando's own date, as the rule counts days
    out = set()
    by_customer: dict[str, list] = {}
    for pl in adapter.leases(ORLANDO):
        by_customer.setdefault(pl.customer_provider_id, []).append(pl)
    for customer, leases in by_customer.items():
        exempt = {e.lease_provider_id for e in adapter.exemptions_for_customer(customer)
                  if e.pause_overlock and e.end_date >= today.isoformat()}
        eligible, unsettled = {}, False
        for pl in leases:
            overdue = (today - date.fromisoformat(pl.delinquent_since[:10])).days if pl.delinquent_since else None
            eligible[pl.provider_id] = overdue is not None and overdue >= RULE_DAYS and pl.provider_id not in exempt
            if pl.overlock_needed is not None and pl.overlock_needed != eligible[pl.provider_id]:
                unsettled = True
                continue
            locked = any(k.status == "OPERATOR_USE" for k in adapter.locks_for_lease(pl.provider_id))
            if locked and not eligible[pl.provider_id]:
                out.add((NOT_RESTORED, "lease", pl.provider_id))
            if eligible[pl.provider_id] and not locked:
                out.add((MISSING, "lease", pl.provider_id))
        codes = adapter.access_codes(ORLANDO, customer)
        if len(codes) == 1 and not unsettled:
            allowed = adapter.check_code(ORLANDO, codes[0].code).allowed
            should = not any(eligible.values())
            if should and not allowed:
                out.add((NOT_RESTORED, "customer", customer))
            if allowed and not should:
                out.add((MISSING, "customer", customer))
    return out


def access_red_lines(session, adapter) -> dict[str, int]:
    """The plan's access red lines, counted against the FMS itself after a pass."""
    closed_with_lock = sum(1 for w in session.scalars(select(WorkItem).where(
        WorkItem.state == "closed", WorkItem.kind == NOT_RESTORED, WorkItem.subject_type == "lease", WorkItem.verification == CLOSED_BY))
        if any(k.status == "OPERATOR_USE" for k in adapter.locks_for_lease(w.subject_id)))
    verified = sum(1 for w in session.scalars(select(WorkItem))
                   if "verified" in (w.verification or "").lower() and "not verified" not in (w.verification or "").lower())
    open_keys = {(w.kind, w.subject_type, w.subject_id) for w in items(session)}
    unflagged = len(expected_mismatches(adapter) - open_keys)
    return {"an access item closed while Locks Search still shows the operator lock": closed_with_lock,
            "a physical outcome labeled verified": verified,
            "a mismatch (lock, missing lock or gate code) left without a work item after a pass": unflagged}


# what Gatehouse may send to the FMS during an access pass: reads, and one kind of write (a task for a person)
ALLOWED = {"/v1/facilities/search", "/v1/leases/search", "/v1/ledgers/search", "/v1/payments/search", "/v1/customers/search",
           "/v1/payment-methods/search", "/v1/access-codes/search", "/v1/access-codes/check", "/v1/locks/search",
           "/v1/delinquency-exemptions/search", "/v1/tasks/search", "/v1/tasks/create"}


class Recording(httpx.BaseTransport):
    """Gatehouse's transport to the FMS, writing down every path it sends a request to."""

    def __init__(self):
        self.inner, self.paths = httpx.HTTPTransport(), []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        return self.inner.handle_request(request)


class Tally:
    """Runs each access pass and checks the red lines right after it, and counts any gate-code or lock change that
    happened during the pass itself (Gatehouse may only create tasks)."""

    def __init__(self, run: AccessRun):
        self.run, self.totals, self.access_writes = run, {}, 0

    def reconcile(self, session, adapter, facility, passes: int = 1, judged: bool = True) -> None:
        """judged=False for a facility without a rule: nothing is judged there, so mismatches are not counted."""
        for _ in range(passes):
            sent = len(adapter.recording.paths)
            reconcile_access(session, adapter, facility, "eval")
            self.access_writes += sum(1 for path in adapter.recording.paths[sent:] if path not in ALLOWED)
            for k, v in access_red_lines(session, adapter).items():
                if judged or "mismatch" not in k:
                    self.totals[k] = self.totals.get(k, 0) + v


def access_scenarios(url: str) -> tuple[list[Row], dict[str, int], int]:
    run = AccessRun(url)
    tally = Tally(run)
    rows: list[Row] = []

    # 1. a paid tenant is still locked out
    session, adapter, facility = run.start()
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    tally.reconcile(session, adapter, facility, passes=3)
    open_ = items(session)
    tasks = run.tasks("lease_WhitB1042")
    ok = [(w.kind, w.subject_id) for w in open_] == [(NOT_RESTORED, "lease_WhitB1042")] and len(tasks) == 1
    rows.append(Row("Access", "Paid, still locked", "the ledger shows the money; the October overlock is still recorded",
                    f"{len(open_)} item, {len(tasks)} FMS task after three passes", "Facility manager: check the lock", ok))
    # 2. staff mark the task done without taking the lock off; then they take it off
    if tasks:
        run.sim("/sim/tasks/complete", {"taskId": tasks[0]["taskId"]})
    tally.reconcile(session, adapter, facility)
    still_open = bool(items(session))
    lock = run.locks("lease_WhitB1042")
    if lock:
        run.sim("/sim/locks/return", {"lockId": lock[0]["lockId"]})
    tally.reconcile(session, adapter, facility)
    closed = items(session, "closed")
    ok = still_open and len(closed) == 1 and closed[0].verification == CLOSED_BY
    rows.append(Row("Access", "Task marked done, lock still on", "the task is COMPLETED; Locks Search still shows the lock, later not",
                    "open while the lock was recorded; closed as FMS access reconciled once it was gone",
                    "Physical outcome not verified", ok))
    # 3. a returned payment leaves a unit past the rule with no lock
    session, adapter, facility = run.start()
    paid = run.sim("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}})["data"]["payment"]
    run.sim("/sim/locks/return", {"lockId": run.locks("lease_BellC0207")[0]["lockId"]})
    run.sim("/sim/payments/return", {"paymentId": paid["paymentId"]})
    writes_before = tally.access_writes
    tally.reconcile(session, adapter, facility)
    ok = [(w.kind, w.subject_id) for w in items(session)] == [(MISSING, "lease_BellC0207")] and tally.access_writes == writes_before
    rows.append(Row("Access", "Payment came back, no lock", "the return puts the unit past a 30-day rule; no lock recorded",
                    "flagged for a person; no code or lock changed", "Facility manager: decide on an overlock", ok))
    # 4. a lost answer to the task request
    session, adapter, facility = run.start()
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    run.sim("/sim/faults", {"kind": "lose_task_response", "facilityId": ORLANDO})
    tally.reconcile(session, adapter, facility, passes=3)
    tasks = run.tasks("lease_WhitB1042")
    ok = len(tasks) == 1 and items(session)[0].task_state == "created"
    rows.append(Row("Access", "Task answer lost", "makes the task, then the answer breaks", f"found it by its marker; {len(tasks)} task in the FMS",
                    "Facility manager: one task", ok))
    # 5. no rule recorded: nothing is judged, but the red lines that apply are still counted
    run.sim("/sim/reset")
    session = make_session_factory("sqlite:///:memory:")()
    recording = Recording()
    adapter = CubbyAdapter(url, KEY, timeout_seconds=5.0, transport=recording)
    adapter.recording = recording
    sync_facility(session, adapter, ORLANDO)
    facility = session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    tally.reconcile(session, adapter, facility, judged=False)
    kinds = [(w.kind, w.subject_type) for w in items(session)]
    ok = kinds == [("access_review_required", "facility")] and not run.tasks("lease_WhitB1042")
    rows.append(Row("Access", "No rule recorded", "a paid unit is still locked, but the facility has no stated rule",
                    "one review item; no verdict and no task", "Operations manager: record the rule", ok))
    return rows, tally.totals, tally.access_writes


# ------------------------------------------------------------------ payments
# how many card charges each scenario should leave at the FMS: one per payment the tenant agreed to, none when nothing
# was charged. Counted from the simulator's own record, so a resent payment shows up whatever Gatehouse believes.
EXPECTED_CARD_CHARGES = {"Payment applied": 1, "Ledger lags the payment": 1, "Card declined": 0, "Gateway pending": 1,
                         "Response lost after charging": 1, "FMS refuses the request": 0, "FMS fails mid-request": 0,
                         "Counter payment at the same time": 1, "Same yes sent twice": 1, "Balance changed after the yes": 0,
                         "Payment returned later": 1, "Key without payment rights": 0}
NOT_CHARGED = ("DECLINED", "FAILED", "REJECTED")


def card_charges(url: str, lease_id: str) -> int:
    truth = httpx.get(f"{url}/sim/truth", params={"leaseId": lease_id}, timeout=15).json()["data"]
    return sum(1 for p in truth["payments"] if p["methodType"] != "CASH" and p["status"] not in NOT_CHARGED)


def applied_on_ledger(url: str, lease_id: str) -> bool:
    """The simulator's own record: a card payment that succeeded and reached the lease ledger."""
    truth = httpx.get(f"{url}/sim/truth", params={"leaseId": lease_id}, timeout=15).json()["data"]
    return any(p["methodType"] != "CASH" and p["status"] == "SUCCEEDED" and p["applied"] for p in truth["payments"])


def payment_rows(url: str) -> tuple[list[Row], int, int]:
    """Each payment scenario, and two red lines read from the simulator's own record: card charges beyond the ones the
    tenant agreed to, counted after the scenario; and a tenant sentence that says "applied", judged against the ledger
    at the moment the sentence is written, whenever that is in the scenario."""
    run = scenarios.Run(url)
    rows, duplicates, unproven = [], 0, []
    original = scenarios.tenant_message

    def judged(command):
        sentence = original(command)
        if "applied" in sentence.lower() and not applied_on_ledger(url, scenarios.DANA):
            unproven.append(sentence)
        return sentence
    scenarios.tenant_message = judged
    try:
        for fn in scenarios.SCENARIOS:
            desk, told, passed = fn(run)
            rows.append(Row("Payments", fn.title, fn.fms, desk, told, passed))
            duplicates += max(0, card_charges(url, scenarios.DANA) - EXPECTED_CARD_CHARGES[fn.title])
    finally:
        scenarios.tenant_message = original
    return rows, len(unproven), duplicates


# ------------------------------------------------------------------ negative controls
def control_retry(url: str) -> Control:
    """A wrong build that sends a lost payment again. The duplicate-charge count, the same one the red line uses, must
    see more card charges than the one the tenant agreed to."""
    run = scenarios.Run(url)
    session, adapter, case = run.start("lose_response")
    from .payments import Authorization, PaymentService
    PaymentService(session, adapter).take_payment(case.id, scenarios.DANA_CARD, Authorization.new(True, 24000, 24000))
    adapter.make_payment(scenarios.DANA, scenarios.DANA_CARD, 24000)          # what the wrong build would do
    caught = card_charges(url, scenarios.DANA) - EXPECTED_CARD_CHARGES["Response lost after charging"] > 0
    return Control("A build that resends a payment whose answer was lost",
                   "sends the same $240.00 again after the first answer was lost", caught)


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
    report.red_lines.append(RedLine("Access", "a request from Gatehouse that could change access (anything but a read or a task)",
                                    access_writes))
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
