"""Play each failure against a running FMS simulator and show what the desk did about it.

    python -m desk.scenarios --fms http://localhost:8100

Every scenario resets the simulator, makes the FMS misbehave in one specific way, takes a payment the way
the desk always does, lets the worker reconcile, and checks the result against what should happen."""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from dataclasses import dataclass
from typing import Callable

import httpx
from sqlalchemy import select

from .adapter.cubby import CubbyAdapter
from .cases import current_case, sync_facility
from .db import make_session_factory
from .models import DelinquencyCase, Lease, PaymentCommand
from .payments import Authorization, PaymentRefused, PaymentService, tenant_message

ORLANDO = "fac_NSorlando01"
DANA, DANA_CARD = "lease_WhitB1042", "pm_DanaVisa4242"


@dataclass
class Result:
    name: str
    fms: str
    desk: str
    told: str
    passed: bool


class Run:
    def __init__(self, fms_url: str, key: str = "sim_org_northstar_full"):
        self.fms_url, self.key = fms_url, key

    def sim(self, path: str, body: dict | None = None) -> dict:
        r = httpx.post(f"{self.fms_url}{path}", json=body or {}, headers={"Authorization": f"Bearer {self.key}"}, timeout=15)
        return r.json()

    def fms_payments(self, lease_id: str) -> int:
        return len(self.sim("/v1/payments/search", {"where": {"leaseId": lease_id}})["data"]["payments"])

    def start(self, mode: str | None = None, lag: int = 2, key: str | None = None):
        self.sim("/sim/reset")
        if mode:
            self.sim("/sim/behaviors", {"leaseId": DANA, "mode": mode, "lag": lag})
        session = make_session_factory("sqlite:///:memory:")()
        adapter = CubbyAdapter(self.fms_url, key or self.key, timeout_seconds=1.0)
        sync_facility(session, adapter, ORLANDO)
        case = current_case(session, session.scalar(select(Lease).where(Lease.provider_id == DANA)))
        return session, adapter, case

    @staticmethod
    def settle(session, adapter, passes: int = 6) -> None:
        for _ in range(passes):
            PaymentService(session, adapter).reconcile_all()


def scenario(name: str, fms: str):
    def wrap(fn: Callable[[Run], tuple[str, str, bool]]):
        fn.title, fn.fms = name, fms
        return fn
    return wrap


@scenario("Payment applied", "approves and applies at once")
def approved(run: Run):
    session, adapter, case = run.start()
    c = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    return c.state, tenant_message(c), c.state == "CONFIRMED" and session.get(DelinquencyCase, case.id).state == "paid"


@scenario("Ledger lags the payment", "says SUCCEEDED, ledger updates three reads later")
def late(run: Run):
    session, adapter, case = run.start("late_application", lag=3)
    c = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    first, told = c.state, tenant_message(c)
    run.settle(session, adapter)
    final = session.get(PaymentCommand, c.id).state
    return f"{first}, then {final}", told, first == "AWAITING_APPLICATION" and final == "CONFIRMED"


@scenario("Card declined", "HTTP 200 with status DECLINED")
def declined(run: Run):
    session, adapter, case = run.start("decline")
    c = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    return c.state, tenant_message(c), c.state == "DECLINED" and session.get(DelinquencyCase, case.id).state == "open"


@scenario("Gateway pending", "PENDING, settles two reads later")
def pending(run: Run):
    session, adapter, case = run.start("pending_then_succeed", lag=2)
    c = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    first, told = c.state, tenant_message(c)
    run.settle(session, adapter)
    final = session.get(PaymentCommand, c.id).state
    return f"{first}, then {final}; {run.fms_payments(DANA)} charge", told, final == "CONFIRMED" and run.fms_payments(DANA) == 1


@scenario("Response lost after charging", "charges and applies, answers too late")
def lost(run: Run):
    session, adapter, case = run.start("lose_response")
    service = PaymentService(session, adapter)
    c = service.take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    run.settle(session, adapter)
    c = session.get(PaymentCommand, c.id)
    held, suggested = c.state, c.suggested_payment_id
    service.resolve_by_staff(c, True, "Matches the processor record", "J. Ortiz", suggested)
    return f"{held} with the FMS payment suggested; staff confirmed it: {c.state}; {run.fms_payments(DANA)} charge", \
        tenant_message(c), held == "NEEDS_STAFF" and suggested is not None and c.state == "CONFIRMED" and run.fms_payments(DANA) == 1


@scenario("FMS refuses the request", "HTTP 422 in its error envelope")
def refused(run: Run):
    session, adapter, case = run.start("reject")
    c = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    return f"{c.state}, account free", tenant_message(c), c.state == "REJECTED" and run.fms_payments(DANA) == 0


@scenario("FMS fails mid-request", "HTTP 500, nothing recorded")
def unknown(run: Run):
    session, adapter, case = run.start("server_error")
    service = PaymentService(session, adapter)
    c = service.take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    run.settle(session, adapter)
    c = session.get(PaymentCommand, c.id)
    held = c.state
    try:
        service.take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
        blocked = False
    except PaymentRefused:
        blocked = True
    service.resolve_by_staff(c, charged=False, note="Nothing in the FMS or the processor portal", staff="J. Ortiz")
    return f"{held}, new attempts blocked until staff cleared it", tenant_message(c), \
        held == "NEEDS_STAFF" and blocked and c.state == "CLEARED_BY_STAFF" and run.fms_payments(DANA) == 0


@scenario("Counter payment at the same time", "ours applies late; a cash payment lands first")
def counter(run: Run):
    session, adapter, case = run.start("late_application", lag=3)
    service = PaymentService(session, adapter)
    c = service.take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    run.sim("/sim/payments/offline", {"allocations": {DANA: 24000}})
    run.settle(session, adapter)
    c = session.get(PaymentCommand, c.id)
    waiting = c.state
    c.submitted_at -= timedelta(minutes=16)   # the wait for the ledger runs out
    session.commit()
    run.settle(session, adapter, passes=1)
    held = c.state
    service.resolve_by_staff(c, True, "FMS ledger shows the card payment and the cash, each applied", "J. Ortiz",
                             c.provider_payment_id, ledger_checked=True)
    return f"{waiting} (the ledger cannot say whose money is whose), {held}, staff checked the ledger: {c.state}", tenant_message(c), \
        waiting == "AWAITING_APPLICATION" and held == "NEEDS_STAFF" and c.state == "CONFIRMED"


@scenario("Same yes sent twice", "approves")
def twice(run: Run):
    session, adapter, case = run.start()
    auth = Authorization.new(True, 24000, 10000)
    a = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, auth)
    b = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, auth)
    return f"one command, {run.fms_payments(DANA)} charge", tenant_message(a), a.id == b.id and run.fms_payments(DANA) == 1


@scenario("Balance changed after the yes", "a late fee posts before submission")
def changed(run: Run):
    session, adapter, case = run.start()
    run.sim("/sim/charges", {"leaseId": DANA, "amount": 2500, "description": "Late fee"})
    c = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    return c.state, tenant_message(c), c.state == "NEEDS_REAUTHORIZATION" and run.fms_payments(DANA) == 0


@scenario("Payment returned later", "clears, then comes back as a direct debit return")
def returned(run: Run):
    session, adapter, case = run.start()
    c = PaymentService(session, adapter).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    run.sim("/sim/payments/return", {"paymentId": c.provider_payment_id})
    run.settle(session, adapter, passes=1)
    c = session.get(PaymentCommand, c.id)
    state = session.get(DelinquencyCase, case.id).state
    return f"{c.state}, case {state}", tenant_message(c), c.state == "RETURNED" and state == "reopened"


@scenario("Key without payment rights", "HTTP 403")
def read_only(run: Run):
    session, _, case = run.start()
    limited = CubbyAdapter(run.fms_url, "sim_org_northstar_readonly", timeout_seconds=1.0)
    c = PaymentService(session, limited).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, 24000))
    return c.state, tenant_message(c), c.state == "REJECTED" and run.fms_payments(DANA) == 0


SCENARIOS = [approved, late, declined, pending, lost, refused, unknown, counter, twice, changed, returned, read_only]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fms", default="http://localhost:8100")
    args = ap.parse_args(argv)
    run = Run(args.fms)
    results = []
    for fn in SCENARIOS:
        desk, told, passed = fn(run)
        results.append(Result(fn.title, fn.fms, desk, told, passed))
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.name:<{width}}  FMS: {r.fms}")
        print(f"      {'':<{width}}  desk: {r.desk}")
        print(f"      {'':<{width}}  tenant hears: {r.told}")
    failed = sum(not r.passed for r in results)
    print(f"\n{len(results) - failed} of {len(results)} scenarios behaved as they should.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
