"""Taking a payment from the past-due queue: each test is one way this goes wrong in real FMS integrations."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from desk.adapter import NotFound
from desk.adapter.cubby import CubbyAdapter
from desk.cases import current_case, refresh_case, sync_facility
from desk.models import CaseEvent, DelinquencyCase, Lease, PaymentCommand
from desk.payments import (
    Authorization, IdentityClaim, PaymentRefused, PaymentService, collected, tenant_message, verify_identity,
)

from .conftest import ORLANDO

DANA = "lease_WhitB1042"   # owes $240.00: $215 rent and a $25 late fee
MARCUS = "lease_BellC0207"  # owes $185.00
PRIYA_A, PRIYA_B = "lease_RamnA0012", "lease_RamnA0013"


def case_for(session, adapter, lease_provider_id):
    sync_facility(session, adapter, ORLANDO)
    lease = session.scalar(select(Lease).where(Lease.provider_id == lease_provider_id))
    return current_case(session, lease), lease


def pay(session, adapter, lease_provider_id, method, amount, balance_seen=None):
    case, lease = case_for(session, adapter, lease_provider_id)
    seen = adapter.ledger(lease_provider_id).total_due if balance_seen is None else balance_seen
    auth = Authorization.new(identity_verified=True, balance_seen=seen, amount=amount)
    return PaymentService(session, adapter).take_payment(case.id, method, auth), case, auth


def settle(session, adapter, times=6):
    service = PaymentService(session, adapter)
    for _ in range(times):
        service.reconcile_all()


# ------------------------------------------------------------------ who is paying

def test_wrong_person_gets_no_balance_and_cannot_pay(session, adapter, sim):
    case, lease = case_for(session, adapter, DANA)
    ok, why = verify_identity(adapter, lease, IdentityClaim("Whitfield", "9999", "B-1042"))
    assert not ok and "do not match" in why
    with pytest.raises(PaymentRefused):
        PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(False, 24000, 24000))
    assert sim.payments(DANA) == []


def test_identity_cannot_be_checked_without_access_to_tenant_details(session, fms_url, sim):
    limited = CubbyAdapter(fms_url, "sim_org_northstar_nopii", timeout_seconds=0.5)
    case, lease = case_for(session, limited, DANA)
    ok, why = verify_identity(limited, lease, IdentityClaim("Whitfield", "0142", "B-1042"))
    assert not ok and "contact details" in why


def test_the_right_person_is_verified(session, adapter, sim):
    _, lease = case_for(session, adapter, DANA)
    assert verify_identity(adapter, lease, IdentityClaim("whitfield", "0142", "b-1042"))[0]


# ------------------------------------------------------------------ the happy path, and what "done" means

def test_partial_payment_is_confirmed_but_does_not_clear_the_case(session, adapter, sim):
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    assert command.state == "CONFIRMED"
    assert sim.total_due(DANA) == 14000
    assert session.get(DelinquencyCase, case.id).state == "open"
    assert "has been applied" in tenant_message(command)


def test_paying_the_full_balance_resolves_the_case(session, adapter, sim):
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert command.state == "CONFIRMED"
    assert session.get(DelinquencyCase, case.id).state == "paid"


def test_a_succeeded_payment_is_not_called_applied_until_the_ledger_shows_it(session, adapter, sim):
    sim.behave(DANA, "late_application", lag=3)
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert command.state == "AWAITING_APPLICATION"
    assert "has been applied" not in tenant_message(command)
    assert session.get(DelinquencyCase, case.id).state == "payment in flight"
    settle(session, adapter)
    assert session.get(PaymentCommand, command.id).state == "CONFIRMED"
    assert session.get(DelinquencyCase, case.id).state == "paid"


# ------------------------------------------------------------------ declines, pending answers, lost responses

def test_a_decline_is_an_http_200_and_never_a_success(session, adapter, sim):
    sim.behave(MARCUS, "decline")
    command, case, _ = pay(session, adapter, MARCUS, "pm_MarcVisa1881", 18500)
    assert command.state == "DECLINED" and command.provider_reason == "Insufficient funds"
    assert sim.total_due(MARCUS) == 18500
    assert "did not go through" in tenant_message(command)
    assert session.get(DelinquencyCase, case.id).state == "open"


def test_a_pending_payment_is_never_retried_and_settles_from_the_fms_record(session, adapter, sim):
    sim.behave(DANA, "pending_then_succeed", lag=2)
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert command.state == "PENDING_AT_FMS"
    assert "do not pay again" in tenant_message(command).lower()
    settle(session, adapter)
    assert len(sim.payments(DANA)) == 1
    assert session.get(PaymentCommand, command.id).state == "CONFIRMED"


def test_a_pending_payment_that_ends_declined_leaves_the_balance_open(session, adapter, sim):
    sim.behave(DANA, "pending_then_decline", lag=1)
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    settle(session, adapter)
    assert session.get(PaymentCommand, command.id).state == "DECLINED"
    assert sim.total_due(DANA) == 24000
    assert session.get(DelinquencyCase, case.id).state == "open"


def test_a_lost_response_is_found_at_the_fms_instead_of_resent(session, adapter, sim):
    sim.behave(DANA, "lose_response")
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    # the FMS took and applied the money, but the answer never came back in time
    submitted = session.scalar(select(CaseEvent).where(CaseEvent.case_id == case.id, CaseEvent.kind == "payment submitted"))
    assert submitted.detail["fms_said"] == "lost"
    settle(session, adapter)
    assert len(sim.payments(DANA)) == 1
    assert sim.total_due(DANA) == 0
    assert session.get(PaymentCommand, command.id).state == "CONFIRMED"


def test_a_payment_the_fms_never_shows_goes_to_staff_and_blocks_new_attempts(session, adapter, sim):
    sim.behave(MARCUS, "server_error")
    command, case, _ = pay(session, adapter, MARCUS, "pm_MarcVisa1881", 18500)
    assert command.state == "OUTCOME_UNKNOWN"
    settle(session, adapter)
    command = session.get(PaymentCommand, command.id)
    assert command.state == "NEEDS_STAFF"
    assert session.get(DelinquencyCase, case.id).state == "needs staff"
    with pytest.raises(PaymentRefused):
        PaymentService(session, adapter).take_payment(case.id, "pm_MarcVisa1881", Authorization.new(True, 18500, 18500))
    assert sim.payments(MARCUS) == []


# ------------------------------------------------------------------ duplicates and changed facts

def test_the_same_authorization_submitted_twice_charges_once(session, adapter, sim):
    first, case, auth = pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    again = PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", auth)
    assert again.id == first.id
    assert len(sim.payments(DANA)) == 1
    assert sim.total_due(DANA) == 14000


def test_a_balance_that_changed_after_the_tenant_agreed_needs_a_new_yes(session, adapter, sim):
    case, _ = case_for(session, adapter, DANA)
    seen = adapter.ledger(DANA).total_due
    sim.post("/sim/charges", {"leaseId": DANA, "amount": 2500, "description": "Late fee"})
    command = PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, seen, seen))
    assert command.state == "NEEDS_REAUTHORIZATION"
    assert sim.payments(DANA) == []
    assert "nothing was charged" in tenant_message(command).lower()


def test_one_payment_covering_two_leases_is_counted_once(session, adapter, sim):
    sync_facility(session, adapter, ORLANDO)
    # taken at the counter, outside the desk, for both of the tenant's units
    sim.post("/sim/payments/offline", {"allocations": {PRIYA_A: 9500, PRIYA_B: 12500}})
    PaymentService(session, adapter).reconcile_all()
    for lease_id in (PRIYA_A, PRIYA_B):
        lease = session.scalar(select(Lease).where(Lease.provider_id == lease_id))
        case = session.scalar(select(DelinquencyCase).where(DelinquencyCase.lease_id == lease.id))
        assert case.state == "paid"
    assert collected(session, "cubby") == 22000


def test_a_returned_payment_reopens_the_case(session, adapter, sim):
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert session.get(DelinquencyCase, case.id).state == "paid"
    sim.post("/sim/payments/return", {"paymentId": command.provider_payment_id})
    settle(session, adapter, times=1)
    assert session.get(PaymentCommand, command.id).state == "RETURNED"
    assert session.get(DelinquencyCase, case.id).state == "reopened"
    assert sim.total_due(DANA) == 24000
    assert "returned by your bank" in tenant_message(session.get(PaymentCommand, command.id))


# ------------------------------------------------------------------ permissions

def test_a_key_that_may_not_take_payments_is_rejected_without_a_charge(session, fms_url, adapter, sim):
    read_only = CubbyAdapter(fms_url, "sim_org_northstar_readonly", timeout_seconds=0.5)
    case, _ = case_for(session, adapter, DANA)
    command = PaymentService(session, read_only).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, 24000, 24000))
    assert command.state == "REJECTED"
    assert sim.payments(DANA) == []
    assert "nothing was charged" in tenant_message(command).lower()


def test_another_operators_lease_is_invisible(session, adapter, sim):
    with pytest.raises(NotFound):
        adapter.ledger("lease_HBRuiz101")
    assert sync_facility(session, adapter, "fac_HBtampa0001") == []


# ------------------------------------------------------------------ crashes

def test_a_submission_interrupted_by_a_crash_is_looked_up_not_resent(session, adapter, sim):
    case, lease = case_for(session, adapter, DANA)
    ledger = adapter.ledger(DANA)
    command = PaymentCommand(case_id=case.id, lease_id=lease.id, idempotency_key="case-crash:auth-1", amount=24000,
                             payment_method_id="pm_DanaVisa4242", authorization={}, state="SUBMITTING",
                             balance_before=ledger.total_due, prepaid_before=ledger.prepaid)
    session.add(command)
    session.commit()
    adapter.make_payment(DANA, "pm_DanaVisa4242", 24000)   # the call went out just before the crash
    command.created_at = command.created_at - timedelta(minutes=5)
    session.commit()
    settle(session, adapter)
    assert session.get(PaymentCommand, command.id).state == "CONFIRMED"
    assert len(sim.payments(DANA)) == 1


def test_parallel_identity_guesses_are_counted_before_the_next_one_runs(adapter, sim, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from desk.db import make_session_factory
    from desk.payments import check_identity

    factory = make_session_factory(f"sqlite:///{tmp_path / 'desk.db'}")
    with factory() as s:
        case, _ = case_for(s, adapter, DANA)
        s.commit()
        case_id = case.id

    def guess(n):
        with factory() as s:
            return check_identity(s, adapter, s.get(DelinquencyCase, case_id), IdentityClaim("Whitfield", f"{1000 + n}", "B-1042"))

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(guess, range(8)))
    assert sum(r is not None for r in results) == 3
