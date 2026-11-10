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
from . import sales
from .adapter.base import FMSError
from .evaluate_reminders import EARLY_PAYMENT, LIFTED, STOP_CLEAR, UNREAD_CLEAR, WRONG_FACT, reminders_part
from .evaluate_texts import CLAIM, OWN, STOP, TWICE, texts_part

KEY = "sim_org_northstar_full"
ORLANDO = "fac_NSorlando01"


@dataclass
class Row:
    module: str
    name: str
    trouble: str
    fms: str
    gatehouse: str
    told: str
    passed: bool


@dataclass
class RedLine:
    module: str
    line: str
    trouble: str
    count: int
    checked: int = 0     # how many times this run could have broken the line: the denominator of the count
    of: str = ""         # what was checked: passes, requests, scenarios


@dataclass
class Control:
    name: str
    short: str
    trouble: str
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
        # a red line nothing was checked against proves nothing, so it stops the build like a line above zero
        return all(r.passed for r in self.rows) and all(r.count == 0 and r.checked > 0 for r in self.red_lines) and all(c.caught for c in self.controls)


def start_simulator() -> tuple[str, uvicorn.Server, threading.Thread]:
    """A simulator of our own: the port is bound here and handed to the server, and nothing is sent until that server
    reports it started, so a reset can never reach another simulator that happened to take the port."""
    from fmssim.app import create_app
    db = Path(tempfile.mkdtemp(prefix="gatehouse-eval-")) / "fmssim.db"
    # a lost answer takes longer than Gatehouse's 5-second wait, as in the tests, so a busy build machine cannot turn an
    # ordinary slow read into a failure
    app = create_app(f"sqlite:///{db}", lose_response_seconds=8.0)
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


def access_red_lines(session, adapter, judged: bool = True) -> dict[str, int]:
    """The plan's access red lines, counted against the FMS itself after a pass. Where no rule is recorded nothing is
    judged, so the mismatch count is not even worked out."""
    closed_with_lock = sum(1 for w in session.scalars(select(WorkItem).where(
        WorkItem.state == "closed", WorkItem.kind == NOT_RESTORED, WorkItem.subject_type == "lease", WorkItem.verification == CLOSED_BY))
        if any(k.status == "OPERATOR_USE" for k in adapter.locks_for_lease(w.subject_id)))
    verified = sum(1 for w in session.scalars(select(WorkItem))
                   if "verified" in (w.verification or "").lower() and "not verified" not in (w.verification or "").lower())
    out = {"an access item closed while Locks Search still shows the operator lock": closed_with_lock,
           "a physical outcome labeled verified": verified}
    if judged:
        open_keys = {(w.kind, w.subject_type, w.subject_id) for w in items(session)}
        out["a mismatch (lock, missing lock or gate code) left without a work item after a pass"] = len(expected_mismatches(adapter) - open_keys)
    return out


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
        self.passes = self.judged = self.requests = 0       # what the red lines were counted over

    def reconcile(self, session, adapter, facility, passes: int = 1, judged: bool = True) -> None:
        """judged=False for a facility without a rule: nothing is judged there, so mismatches are not counted."""
        for _ in range(passes):
            sent = len(adapter.recording.paths)
            reconcile_access(session, adapter, facility, "eval")
            self.access_writes += sum(1 for path in adapter.recording.paths[sent:] if path not in ALLOWED)
            self.passes += 1
            self.judged += 1 if judged else 0
            self.requests += len(adapter.recording.paths) - sent
            for k, v in access_red_lines(session, adapter, judged).items():
                self.totals[k] = self.totals.get(k, 0) + v


def access_scenarios(url: str) -> tuple[list[Row], dict[str, int], int, dict[str, int]]:
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
    rows.append(Row("Access", "Paid, still locked", "disagree", "the ledger shows the money; the October overlock is still recorded",
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
    rows.append(Row("Access", "Task marked done, lock still on", "disagree", "the task is COMPLETED; Locks Search still shows the lock, later not",
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
    rows.append(Row("Access", "Payment came back, no lock", "money", "the return puts the unit past a 30-day rule; no lock recorded",
                    "flagged for a person; no code or lock changed", "Facility manager: decide on an overlock", ok))
    # 4. a lost answer to the task request
    session, adapter, facility = run.start()
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    run.sim("/sim/faults", {"kind": "lose_task_response", "facilityId": ORLANDO})
    tally.reconcile(session, adapter, facility, passes=3)
    tasks = run.tasks("lease_WhitB1042")
    ok = len(tasks) == 1 and items(session)[0].task_state == "created"
    rows.append(Row("Access", "Task answer lost", "late", "makes the task, then the answer breaks", f"found it by its marker; {len(tasks)} task in the FMS",
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
    rows.append(Row("Access", "No rule recorded", "disagree", "a paid unit is still locked, but the facility has no stated rule",
                    "one review item; no verdict and no task", "Operations manager: record the rule", ok))
    return rows, tally.totals, tally.access_writes, {"passes": tally.passes, "judged": tally.judged, "requests": tally.requests}


# ------------------------------------------------------------------ lien sales
# a sales pass may read, and create one task; cancel-lien, settle and complete are never Gatehouse's to send
SALE_ALLOWED = ALLOWED | {"/v1/auctions/search"}
FEED_CONFIRMED = "Confirmed by the outside site's feed"


def sale_items(session, kind: str | None = None, state: str | None = "open") -> list[WorkItem]:
    q = select(WorkItem).where(WorkItem.kind.in_(sales.KIND_NAMES))
    if kind:
        q = q.where(WorkItem.kind == kind)
    if state:
        q = q.where(WorkItem.state == state)
    return session.scalars(q.order_by(WorkItem.id)).all()


def sales_red_lines(session, url: str) -> dict[str, int]:
    """Counted from the simulator's own record of each sale and the outside site's own feed, never from what Gatehouse
    believes."""
    truth = {f"{e['auctionId']}/{e['leaseId']}": e for e in
             httpx.get(f"{url}/sim/auctions/truth", params={"facilityId": ORLANDO}, timeout=15).json()["data"]["entries"]}
    feed = {f"{x['auctionId']}/{x['leaseId']}": x for x in
            httpx.get(f"{url}/marketplace/v1/listings", params={"facilityId": ORLANDO}, timeout=15).json()["data"]["listings"]}
    closed = sale_items(session, state="closed")
    return {
        "a sale item closed while the FMS does not show that auction and lease cancelled":
            sum(1 for w in closed if w.kind != sales.OUTSIDE and truth.get(w.subject_id, {}).get("status") != "CANCELLED"),
        "an outside listing closed without that site's own feed reporting it withdrawn":
            sum(1 for w in closed if w.kind == sales.OUTSIDE and feed.get(w.subject_id, {}).get("status") != "WITHDRAWN"),
        "a zero balance with no payment since the notice described as a tenant payment":
            sum(1 for w in sale_items(session, sales.LISTED_PAID, state=None)
                if not (w.observed or {}).get("payments") and "reports payments" in w.problem),
        "a settled sale shown with an order between the sale and the payments":
            sum(1 for w in sale_items(session, sales.SETTLED_REVIEW, state=None) if "makes no claim about the order" not in w.problem),
    }


def sale_calls(url: str) -> int:
    return len(httpx.get(f"{url}/sim/auctions/truth", params={"facilityId": ORLANDO}, timeout=15).json()["data"]["apiCalls"])


class SaleTally:
    def __init__(self, url: str):
        self.url, self.totals, self.sale_writes = url, {}, 0
        self.passes = self.requests = 0                      # what the red lines were counted over

    def reconcile(self, session, adapter, facility, passes: int = 1) -> None:
        for _ in range(passes):
            sent, calls = len(adapter.recording.paths), sale_calls(self.url)
            sales.reconcile_sales(session, adapter, facility, "eval", self.url)
            # the simulator's own record of sale-changing calls, whoever sent them, plus any other write through the adapter
            self.sale_writes += sale_calls(self.url) - calls
            self.sale_writes += sum(1 for path in adapter.recording.paths[sent:] if path not in SALE_ALLOWED and not path.startswith("/v1/auctions/"))
            self.passes += 1
            self.requests += len(adapter.recording.paths) - sent
            for k, v in sales_red_lines(session, self.url).items():
                self.totals[k] = self.totals.get(k, 0) + v


def schedule(run: AccessRun, lease_id: str, when: str | None = None, marketplace: bool = False) -> None:
    today = datetime.now(timezone.utc).date()
    body = {"facilityId": ORLANDO, "auctionTime": when or f"{date.fromordinal(today.toordinal() + 2)}T10:00:00",
            "leaseIds": [lease_id], "lienStartDate": date.fromordinal(today.toordinal() - 20).isoformat()}
    if marketplace:
        body["marketplace"] = "Online auction site (evaluation)"
    run.sim("/sim/auctions/schedule", body)


def sale_tasks(run: AccessRun, lease_id: str) -> list[dict]:
    return [t for t in run.tasks(lease_id) if t["title"].startswith("Cancel the lien sale")]


def sales_scenarios(url: str) -> tuple[list[Row], dict[str, int], int, dict[str, int]]:
    run, tally, rows = AccessRun(url), SaleTally(url), []

    # 1. paid in full, the sale still listed, then the task marked done, then the FMS cancellation, then the outside site
    session, adapter, facility = run.start()
    schedule(run, "lease_WhitB1042", marketplace=True)
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    tally.reconcile(session, adapter, facility, passes=3)
    listed, tasks = sale_items(session, sales.LISTED_PAID), sale_tasks(run, "lease_WhitB1042")
    ok = len(listed) == 1 and len(tasks) == 1
    rows.append(Row("Lien sales", "Paid, sale still listed", "disagree", "the ledger shows nothing owed; auctions/search still lists the sale",
                    f"{len(listed)} urgent item, {len(tasks)} FMS task after three passes", "Collections lead: cancel it or say why", ok))
    if tasks:
        run.sim("/sim/tasks/complete", {"taskId": tasks[0]["taskId"]})
    tally.reconcile(session, adapter, facility)
    still = bool(sale_items(session, sales.LISTED_PAID))
    run.sim("/sim/auctions/cancel", {"leaseId": "lease_WhitB1042"})
    tally.reconcile(session, adapter, facility)
    closed = sale_items(session, sales.LISTED_PAID, state="closed")
    outside_open = bool(sale_items(session, sales.OUTSIDE))
    ok = still and len(closed) == 1 and closed[0].verification == sales.CANCELLED_BY and outside_open
    rows.append(Row("Lien sales", "Task marked done, sale still listed", "disagree", "the task is COMPLETED; the sale is cancelled later",
                    "open while listed; closed as FMS lien cancellation verified; the outside listing stays open",
                    "The outside site is a separate check", ok))
    run.sim("/sim/listings/withdraw", {"leaseId": "lease_WhitB1042"})
    tally.reconcile(session, adapter, facility)
    out = sale_items(session, sales.OUTSIDE, state="closed")
    ok = len(out) == 1 and out[0].verification == FEED_CONFIRMED
    rows.append(Row("Lien sales", "Outside listing withdrawn", "disagree", "the outside site's feed reports the listing withdrawn",
                    "closed on that feed, not on the FMS cancellation", "Front desk: nothing more", ok))

    # 2. the payment comes back before anyone cancels
    session, adapter, facility = run.start()
    schedule(run, "lease_BellC0207")
    paid = run.sim("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}})["data"]["payment"]
    tally.reconcile(session, adapter, facility)
    run.sim("/sim/payments/return", {"paymentId": paid["paymentId"]})
    tally.reconcile(session, adapter, facility, passes=2)
    review = sale_items(session, sales.BALANCE_CHANGED)
    ok = len(review) == 1 and not sale_items(session, sales.LISTED_PAID) and len(sale_tasks(run, "lease_BellC0207")) == 1
    rows.append(Row("Lien sales", "Payment came back before cancelling", "money", "the full payment is returned; the sale is still listed",
                    "the same item moves to review; its task is marked as resting on the older reading; no second task",
                    "Collections lead: reassess the sale", ok))

    # 3. a payment after the notice that leaves a balance
    session, adapter, facility = run.start()
    schedule(run, "lease_OkfrE0505")
    run.sim("/sim/payments/offline", {"allocations": {"lease_OkfrE0505": 5000}})
    tally.reconcile(session, adapter, facility)
    after = sale_items(session, sales.AFTER_NOTICE)
    ok = len(after) == 1 and not sale_items(session, sales.LISTED_PAID) and not sale_tasks(run, "lease_OkfrE0505")
    rows.append(Row("Lien sales", "Payment after the notice, balance left", "money", "$50 paid after the notice; $115 still owed; still listed",
                    "a review item for a person and counsel; no cancellation task and no conclusion", "Collections lead, with counsel", ok))

    # 4. nothing owed because of a credit, not a payment
    session, adapter, facility = run.start()
    schedule(run, "lease_BellC0207")
    run.sim("/sim/charges", {"leaseId": "lease_BellC0207", "category": "CREDIT", "description": "Courtesy credit", "amount": -18500})
    tally.reconcile(session, adapter, facility)
    listed = sale_items(session, sales.LISTED_PAID)
    ok = len(listed) == 1 and "does not call it a tenant payment" in listed[0].problem
    rows.append(Row("Lien sales", "Nothing owed after a credit", "money", "a credit clears the balance; no payment since the notice",
                    "urgent item that says no payment was found", "Collections lead: check why", ok))

    # 5. a sale already settled on an account with nothing owed
    session, adapter, facility = run.start()
    schedule(run, "lease_BellC0207")
    run.sim("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}})
    run.sim("/sim/auctions/settle", {"leaseId": "lease_BellC0207", "outcome": "SOLD", "bidAmount": 40000,
                                     "winningBidderId": "cust_MarcBell01", "force": True})
    tally.reconcile(session, adapter, facility)
    settled = sale_items(session, sales.SETTLED_REVIEW)
    ok = len(settled) == 1 and not sale_items(session, sales.LISTED_PAID)
    rows.append(Row("Lien sales", "Sold, account paid", "disagree", "the FMS records the sale SOLD; the ledger shows nothing owed",
                    "urgent review that claims no order between the sale and the payment", "Manager, with counsel", ok))

    # 6. the ledger cannot be read
    session, adapter, facility = run.start()
    schedule(run, "lease_OkfrE0505")
    run.sim("/sim/payments/offline", {"allocations": {"lease_OkfrE0505": 5000}})
    tally.reconcile(session, adapter, facility)

    class LedgerDown:
        recording = adapter.recording

        def __getattr__(self, name):
            return getattr(adapter, name)

        def ledger(self, lease_id):
            raise FMSError("/v1/ledgers/search: ReadTimeout")

    tally.reconcile(session, LedgerDown(), facility)
    after = sale_items(session, sales.AFTER_NOTICE)
    ok = len(after) == 1 and "Not freshly verified" in (after[0].note or "")
    rows.append(Row("Lien sales", "Ledger read fails", "no", "ledgers/search times out", "the item stays open, marked not freshly verified",
                    "Nothing is decided on an old reading", ok))

    # 7. the answer to the task request is lost
    session, adapter, facility = run.start()
    schedule(run, "lease_WhitB1042")
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    run.sim("/sim/faults", {"kind": "lose_task_response", "facilityId": ORLANDO})
    tally.reconcile(session, adapter, facility, passes=3)
    listed, tasks = sale_items(session, sales.LISTED_PAID), sale_tasks(run, "lease_WhitB1042")
    ok = len(tasks) == 1 and len(listed) == 1 and listed[0].task_state == "created"
    rows.append(Row("Lien sales", "Task answer lost", "late", "makes the task, then the answer breaks", f"found it by its marker; {len(tasks)} task",
                    "Collections lead: one task", ok))

    # 8. a sale time that happens twice when the clocks go back
    session, adapter, facility = run.start()
    schedule(run, "lease_WhitB1042", when="2026-11-01T01:30:00")
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    tally.reconcile(session, adapter, facility)
    listed = sale_items(session, sales.LISTED_PAID)
    ok = len(listed) == 1 and listed[0].observed.get("sale_at") is None and bool(listed[0].observed.get("sale_time_problem"))
    rows.append(Row("Lien sales", "Sale time on a clock change", "disagree", "1:30 AM on the night the clocks go back",
                    "the time is refused, not guessed; the item still opens", "The page says the time could not be worked out", ok))
    return rows, tally.totals, tally.sale_writes, {"passes": tally.passes, "requests": tally.requests}


def control_sale_task_closes(url: str) -> Control:
    """A wrong build that closes a sale item when its task is marked done."""
    run = AccessRun(url)
    session, adapter, facility = run.start()
    schedule(run, "lease_WhitB1042")
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    sales.reconcile_sales(session, adapter, facility, "eval", url)
    run.sim("/sim/tasks/complete", {"taskId": sale_tasks(run, "lease_WhitB1042")[0]["taskId"]})
    item = sale_items(session, sales.LISTED_PAID)[0]
    item.state, item.verification = "closed", sales.CANCELLED_BY            # what the wrong build would do
    session.commit()
    caught = sales_red_lines(session, url)["a sale item closed while the FMS does not show that auction and lease cancelled"] > 0
    return Control("A build that closes a sale item when its task is marked done", "Closes a sale item", "disagree",
                   "calls the lien cancelled while the FMS still lists the sale", caught)


def control_sale_cancelled_by_gatehouse(url: str) -> Control:
    """A wrong build that cancels the sale itself instead of asking staff, through its own client."""
    run, tally = AccessRun(url), SaleTally(url)
    session, adapter, facility = run.start()
    schedule(run, "lease_WhitB1042")
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    real = sales.reconcile_sales

    def wrong(session, adapter, facility, run_id, feed_url=None):
        httpx.post(f"{url}/v1/auctions/cancel-lien", json={"leaseId": "lease_WhitB1042"},
                   headers={"Authorization": f"Bearer {KEY}"}, timeout=15)          # what the wrong build would do
        return real(session, adapter, facility, run_id, feed_url)
    sales.reconcile_sales = wrong
    try:
        tally.reconcile(session, adapter, facility)
    finally:
        sales.reconcile_sales = real
    return Control("A build that cancels a sale itself", "Cancels a sale itself", "disagree", "sends cancel-lien to the FMS through its own client", tally.sale_writes > 0)


def control_outside_from_fms(url: str) -> Control:
    """A wrong build that takes the FMS cancellation as the outside site's withdrawal."""
    run = AccessRun(url)
    session, adapter, facility = run.start()
    schedule(run, "lease_WhitB1042", marketplace=True)
    run.sim("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 24000}})
    sales.reconcile_sales(session, adapter, facility, "eval", url)
    run.sim("/sim/auctions/cancel", {"leaseId": "lease_WhitB1042"})
    sales.reconcile_sales(session, adapter, facility, "eval", url)
    item = sale_items(session, sales.OUTSIDE)[0]
    item.state, item.verification = "closed", FEED_CONFIRMED                 # what the wrong build would do
    session.commit()
    caught = sales_red_lines(session, url)["an outside listing closed without that site's own feed reporting it withdrawn"] > 0
    return Control("A build that takes the FMS cancellation as the outside listing's withdrawal", "Closes the outside listing", "disagree",
                   "closes the outside listing when the FMS shows the lien cancelled", caught)


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


def payment_rows(url: str) -> tuple[list[Row], int, int, int]:
    """Each payment scenario, and two red lines read from the simulator's own record: card charges beyond the ones the
    tenant agreed to, counted after the scenario; and a tenant sentence that says "applied", judged against the ledger
    at the moment the sentence is written, whenever that is in the scenario."""
    run = scenarios.Run(url, timeout=5.0)
    rows, duplicates, unproven, written = [], 0, [], []
    original = scenarios.tenant_message

    def judged(command):
        sentence = original(command)
        written.append(sentence)
        if "applied" in sentence.lower() and not applied_on_ledger(url, scenarios.DANA):
            unproven.append(sentence)
        return sentence
    scenarios.tenant_message = judged
    try:
        for fn in scenarios.SCENARIOS:
            desk, told, passed = fn(run)
            rows.append(Row("Payments", fn.title, fn.trouble, fn.fms, desk, told, passed))
            duplicates += max(0, card_charges(url, scenarios.DANA) - EXPECTED_CARD_CHARGES[fn.title])
    finally:
        scenarios.tenant_message = original
    return rows, len(unproven), duplicates, len(written)


# ------------------------------------------------------------------ negative controls
def control_retry(url: str) -> Control:
    """A wrong build that sends a lost payment again. The duplicate-charge count, the same one the red line uses, must
    see more card charges than the one the tenant agreed to."""
    run = scenarios.Run(url, timeout=5.0)
    session, adapter, case = run.start("lose_response")
    from .payments import Authorization, PaymentService
    PaymentService(session, adapter).take_payment(case.id, scenarios.DANA_CARD, Authorization.new(True, 24000, 24000))
    adapter.make_payment(scenarios.DANA, scenarios.DANA_CARD, 24000)          # what the wrong build would do
    caught = card_charges(url, scenarios.DANA) - EXPECTED_CARD_CHARGES["Response lost after charging"] > 0
    return Control("A build that resends a payment whose answer was lost", "Resends a payment", "late",
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
    return Control("A build that closes an access item when its task is marked done", "Closes an access item", "disagree",
                   "closes the item as reconciled while the FMS still records the lock", caught)


# What each red line guards against, as a kind of trouble (desk/wording.py TROUBLES). The two lines about requests Gatehouse could make
# belong to "disagree": when the records disagree, the tempting fix is to change the FMS directly, and the desk only reads and asks for tasks.
GUARDS = {
    "a duplicate charge": "twice",
    "a tenant told a payment was applied without FMS proof": "late",
    "an access item closed while Locks Search still shows the operator lock": "disagree",
    "a physical outcome labeled verified": "disagree",
    "a mismatch (lock, missing lock or gate code) left without a work item after a pass": "disagree",
    "a request from Gatehouse that could change access (anything but a read or a task)": "disagree",
    "a sale item closed while the FMS does not show that auction and lease cancelled": "disagree",
    "an outside listing closed without that site's own feed reporting it withdrawn": "disagree",
    "a zero balance with no payment since the notice described as a tenant payment": "money",
    "a settled sale shown with an order between the sale and the payments": "disagree",
    "a request from Gatehouse that could change a sale (cancel, settle or complete)": "disagree",
    # texts typed as a tenant (desk/evaluate_texts.py): a text of one visitor reached by another, a sentence the FMS does not bear out,
    # a send after STOP, and one press counted twice
    OWN: "twice",
    CLAIM: "disagree",
    STOP: "no",
    TWICE: "twice",
    # the Reminder check (desk/evaluate_reminders.py): a tenant who said STOP shown clear, an account shown clear after a failed read, a hold
    # lifted, a fact about the account sent to a wrong number, and a hold ended by a payment made before the text
    STOP_CLEAR: "no",
    UNREAD_CLEAR: "no",
    LIFTED: "disagree",
    WRONG_FACT: "disagree",
    EARLY_PAYMENT: "money",
}


def red_line(module: str, line: str, count: int, checked: int, of: str) -> RedLine:
    return RedLine(module, line, GUARDS[line], count, checked, of)


def evaluate(url: str) -> Report:
    report = Report(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rows, applied_without_proof, duplicates, sentences = payment_rows(url)
    report.rows.extend(rows)
    access_rows, access_totals, access_writes, access_checked = access_scenarios(url)
    report.rows.extend(access_rows)
    report.red_lines += [red_line("Payments", "a duplicate charge", duplicates, len(rows), "scenarios"),
                         red_line("Payments", "a tenant told a payment was applied without FMS proof", applied_without_proof, sentences, "messages")]
    # the mismatch line is judged only on a pass for a facility that has a rule recorded; the other two are counted after every pass
    report.red_lines += [red_line("Access", line, n, access_checked["judged" if "left without a work item" in line else "passes"], "passes")
                         for line, n in access_totals.items()]
    report.red_lines.append(red_line("Access", "a request from Gatehouse that could change access (anything but a read or a task)",
                                    access_writes, access_checked["requests"], "requests"))
    sale_rows, sale_totals, sale_writes, sale_checked = sales_scenarios(url)
    report.rows.extend(sale_rows)
    report.red_lines += [red_line("Lien sales", line, n, sale_checked["passes"], "passes") for line, n in sale_totals.items()]
    report.red_lines.append(red_line("Lien sales", "a request from Gatehouse that could change a sale (cancel, settle or complete)",
                                    sale_writes, sale_checked["requests"], "requests"))
    report.controls += [control_retry(url), control_task_closes(url), control_sale_task_closes(url), control_outside_from_fms(url),
                        control_sale_cancelled_by_gatehouse(url)]
    text_rows, text_lines, text_controls = texts_part(url)
    report.rows += [Row("Texts", *row) for row in text_rows]
    report.red_lines += [red_line("Texts", *line) for line in text_lines]
    report.controls += [Control(*control) for control in text_controls]
    reminder_rows, reminder_lines, reminder_controls = reminders_part(url)
    report.rows += [Row("Reminder check", *row) for row in reminder_rows]
    report.red_lines += [red_line("Reminder check", *line) for line in reminder_lines]
    report.controls += [Control(*control) for control in reminder_controls]
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
        print(f"{'ZERO' if r.count == 0 and r.checked else 'HIT '}  {r.module:<9} {r.line}: {r.count} of {r.checked} {r.of}")
    for c in report.controls:
        print(f"{'CAUGHT' if c.caught else 'MISSED'}  {c.name}")
    print("evaluation " + ("passed" if report.ok else "FAILED"))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
