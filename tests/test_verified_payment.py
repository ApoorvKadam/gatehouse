"""Taking a payment from the past-due queue: each test is one way this goes wrong in real FMS integrations."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from desk.adapter import NotFound
from desk.adapter.cubby import CubbyAdapter
from desk.cases import current_case, refresh_case, sync_facility
from desk.models import CaseEvent, DelinquencyCase, LedgerSnapshot, Lease, PaymentCommand
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
    limited = CubbyAdapter(fms_url, "sim_org_northstar_nopii", timeout_seconds=5.0)
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
    assert len(sim.payments(DANA)) == 1 and sim.total_due(DANA) == 0
    # nothing ties a payment to the request that made it, so the match is a suggestion a person confirms
    command = session.get(PaymentCommand, command.id)
    assert command.state == "NEEDS_STAFF" and command.suggested_payment_id == sim.payments(DANA)[0]["paymentId"]
    PaymentService(session, adapter).resolve_by_staff(command, True, "Matches the processor record", "J. Ortiz", command.suggested_payment_id)
    assert command.state == "CONFIRMED" and len(sim.payments(DANA)) == 1


def test_a_request_the_fms_refuses_charged_nothing_and_holds_nothing(session, adapter, sim):
    sim.behave(MARCUS, "reject")
    command, case, _ = pay(session, adapter, MARCUS, "pm_MarcVisa1881", 18500)
    assert command.state == "REJECTED" and "expired" in command.provider_reason and sim.payments(MARCUS) == []
    sim.behave(MARCUS, "approve")
    again = PaymentService(session, adapter).take_payment(case.id, "pm_MarcVisa1881", Authorization.new(True, 18500, 18500))
    assert again.state == "CONFIRMED"


def test_an_fms_failure_is_looked_up_rather_than_called_not_charged(session, adapter, sim):
    sim.behave(MARCUS, "server_error")
    command, _, _ = pay(session, adapter, MARCUS, "pm_MarcVisa1881", 18500)
    assert command.state == "OUTCOME_UNKNOWN"
    settle(session, adapter)
    assert command.state == "NEEDS_STAFF"


def test_a_gateway_error_page_is_looked_up_and_then_held_for_staff(session, adapter, sim):
    sim.behave(MARCUS, "gateway_error")
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
    assert collected(session, "cubby", ORLANDO) == 22000 and collected(session, "cubby", "fac_NSalbany001") == 0


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
    read_only = CubbyAdapter(fms_url, "sim_org_northstar_readonly", timeout_seconds=5.0)
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
                             balance_before=ledger.total_due, prepaid_before=ledger.prepaid,
                             entries_paid_before={e["bookEntryId"]: e["amountPaid"] for e in ledger.raw["bookEntries"]})
    session.add(command)
    session.commit()
    adapter.make_payment(DANA, "pm_DanaVisa4242", 24000)   # the call went out just before the crash
    command.created_at = command.submitted_at = command.created_at - timedelta(minutes=5)
    session.commit()
    settle(session, adapter)
    command = session.get(PaymentCommand, command.id)
    assert command.state == "NEEDS_STAFF" and command.suggested_payment_id == sim.payments(DANA)[0]["paymentId"]
    assert len(sim.payments(DANA)) == 1   # looked up, never resent


def test_a_crash_before_the_fms_was_called_releases_the_account(session, adapter, sim):
    case, lease = case_for(session, adapter, DANA)
    command = PaymentCommand(case_id=case.id, lease_id=lease.id, idempotency_key="case-crash:auth-2", amount=24000,
                             payment_method_id="pm_DanaVisa4242", authorization={}, state="SUBMITTING", balance_before=0, prepaid_before=0)
    session.add(command)
    session.commit()
    command.created_at -= timedelta(minutes=5)
    session.commit()
    settle(session, adapter, times=1)
    assert command.state == "NOT_SUBMITTED" and sim.payments(DANA) == []
    assert pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)[0].state == "CONFIRMED"


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


# ------------------------------------------------------------------ what on the ledger counts as this payment

def test_a_fee_posted_after_the_payment_does_not_hold_it_unconfirmed(session, adapter, sim):
    sim.behave(DANA, "late_application", lag=2)
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.post("/sim/charges", {"leaseId": DANA, "amount": 2500, "description": "Late fee"})
    settle(session, adapter)
    assert session.get(PaymentCommand, command.id).state == "CONFIRMED"


def test_a_counter_payment_at_the_same_time_is_not_taken_for_this_one(session, adapter, sim):
    sim.behave(DANA, "late_application", lag=3)
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.post("/sim/payments/offline", {"allocations": {DANA: 24000}})
    PaymentService(session, adapter).reconcile(command)
    assert command.state == "AWAITING_APPLICATION"   # the counter payment explains the drop in the balance
    settle(session, adapter)
    # ours landed too, as prepaid, but the ledger cannot say which money is whose: never confirmed by inference
    assert command.state == "AWAITING_APPLICATION"
    command.submitted_at -= timedelta(minutes=16)
    session.commit()
    settle(session, adapter, times=1)
    assert command.state == "NEEDS_STAFF" and "could have put it there" in command.provider_reason


def test_a_payment_the_ledger_never_shows_goes_to_staff(session, adapter, sim):
    sim.behave(DANA, "late_application", lag=1000)
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    command.submitted_at -= timedelta(minutes=16)
    session.commit()
    settle(session, adapter, times=1)
    assert command.state == "NEEDS_STAFF" and "ledger does not show" in command.provider_reason
    assert session.get(DelinquencyCase, case.id).state == "needs staff"


def test_a_long_pending_payment_goes_to_staff_and_still_settles_later(session, adapter, sim):
    sim.behave(DANA, "pending_then_succeed", lag=3)
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert command.state == "PENDING_AT_FMS"
    command.submitted_at -= timedelta(hours=2)
    session.commit()
    PaymentService(session, adapter).reconcile(command)
    assert command.state == "NEEDS_STAFF"
    settle(session, adapter)
    assert command.state == "NEEDS_STAFF"   # it settled at the FMS, but a person owns it now
    PaymentService(session, adapter).resolve_by_staff(command, True, "Processor shows it settled", "J. Ortiz", command.provider_payment_id)
    assert command.state == "CONFIRMED" and len(sim.payments(DANA)) == 1


def test_two_payments_for_one_account_at_once_charge_it_once(adapter, sim, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from desk.db import make_session_factory

    sim.behave(DANA, "pending_then_succeed", lag=50)   # the first payment stays open while the others arrive
    factory = make_session_factory(f"sqlite:///{tmp_path / 'desk.db'}")
    with factory() as s:
        case, _ = case_for(s, adapter, DANA)
        s.commit()
        case_id = case.id

    def attempt(_):
        with factory() as s:
            try:
                return PaymentService(s, adapter).take_payment(case_id, "pm_DanaVisa4242", Authorization.new(True, 24000, 24000)).state
            except PaymentRefused:
                return "refused"

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert len(sim.payments(DANA)) == 1
    assert sorted(results) == ["PENDING_AT_FMS"] + ["refused"] * 5


def test_one_lease_the_fms_cannot_read_does_not_stop_the_pass(session, fms_url, sim):
    from desk.adapter import FMSError

    class Flaky(CubbyAdapter):
        broken = False

        def ledger(self, lease_provider_id):
            if self.broken and lease_provider_id == MARCUS:
                raise FMSError("timeout")
            return super().ledger(lease_provider_id)

    flaky = Flaky(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)
    marcus_case, _ = case_for(session, flaky, MARCUS)
    dana_case, _ = case_for(session, flaky, DANA)
    flaky.broken = True
    sim.post("/sim/payments/offline", {"allocations": {DANA: 24000}})
    PaymentService(session, flaky).reconcile_all()
    assert session.get(DelinquencyCase, dana_case.id).state == "paid"
    events = session.scalars(select(CaseEvent.kind).where(CaseEvent.case_id == marcus_case.id)).all()
    assert "fms read failed" in events


# ------------------------------------------------------------------ a facility that leaves the account

class Asking(CubbyAdapter):
    """Remembers which leases the desk asked the FMS about."""

    asked: list

    def payments_for_lease(self, lease_provider_id):
        self.asked.append(lease_provider_id)
        return super().payments_for_lease(lease_provider_id)


def asking_adapter(fms_url):
    adapter = Asking(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)
    adapter.asked = []
    return adapter


def bind_desk(session, fms_url, served, move_to_left=None):
    """Bind the desk to the facilities its key sees. A lease named in move_to_left sits at a facility that is not among them."""
    from desk.models import DeskBinding, Facility

    if move_to_left:
        lease = session.scalar(select(Lease).where(Lease.provider_id == move_to_left))
        home = session.get(Facility, lease.facility_id)
        left = Facility(provider=home.provider, provider_id="fac_left01", name="Left the account", jurisdiction=home.jurisdiction,
                        time_zone=home.time_zone)
        session.add(left)
        session.flush()
        lease.facility_id = left.id
    session.add(DeskBinding(id=1, fms_base_url=fms_url, facility_ids=served, org_ids=[]))
    session.commit()


def test_an_open_payment_at_a_facility_the_key_no_longer_sees_goes_to_staff_once(session, fms_url, sim):
    adapter = asking_adapter(fms_url)
    sim.behave(DANA, "pending_then_succeed", lag=5)
    sim.behave(MARCUS, "pending_then_succeed", lag=5)
    dana, dana_case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    marcus, _, _ = pay(session, adapter, MARCUS, "pm_MarcVisa1881", 18500)
    assert dana.state == marcus.state == "PENDING_AT_FMS"
    bind_desk(session, fms_url, [ORLANDO], move_to_left=DANA)
    adapter.asked.clear()
    service = PaymentService(session, adapter)
    service.reconcile_all()
    service.reconcile_all()
    dana = session.get(PaymentCommand, dana.id)
    assert dana.state == "NEEDS_STAFF" and "no longer served" in dana.provider_reason
    assert DANA not in adapter.asked and MARCUS in adapter.asked        # the leases the key still sees are read as before
    assert service.read_failures == 0
    events = session.scalars(select(CaseEvent.kind).where(CaseEvent.case_id == dana_case.id)).all()
    assert events.count("payment needs staff") == 1                      # said once, not on every pass


def test_a_settled_payment_at_a_facility_the_key_no_longer_sees_is_left_as_it_was(session, fms_url, sim):
    adapter = asking_adapter(fms_url)
    dana, dana_case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert dana.state == "CONFIRMED"
    bind_desk(session, fms_url, [ORLANDO], move_to_left=DANA)
    adapter.asked.clear()
    service = PaymentService(session, adapter)
    service.reconcile_all()
    assert DANA not in adapter.asked and service.read_failures == 0
    assert session.get(PaymentCommand, dana.id).state == "CONFIRMED"
    assert session.get(DelinquencyCase, dana_case.id).state == "paid"


def test_a_key_that_sees_no_facility_is_a_failing_read_not_every_payment_sent_to_staff(session, fms_url, sim):
    adapter = asking_adapter(fms_url)
    sim.behave(DANA, "pending_then_succeed", lag=5)
    dana, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    bind_desk(session, fms_url, [])
    adapter.asked.clear()
    PaymentService(session, adapter).reconcile_all()
    assert DANA in adapter.asked and session.get(PaymentCommand, dana.id).state == "PENDING_AT_FMS"


def test_the_hand_off_to_staff_asks_nothing_between_the_change_and_the_save(session, fms_url, sim):
    """A pass that loses a race on the payment's row has to be able to drop its change and go on. That works only in the save: a query
    in between would write the change earlier, outside the place that recovers from it, and end the whole pass."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    adapter = asking_adapter(fms_url)
    sim.behave(DANA, "pending_then_succeed", lag=5)
    dana, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    bind_desk(session, fms_url, [ORLANDO], move_to_left=DANA)
    engine, verbs = session.get_bind(), []

    def watch(conn, cursor, statement, parameters, context, executemany):
        verbs.append(statement.split()[0].upper())

    fresh = Session(bind=engine)           # a worker pass starts with a session that has not seen these rows
    service = PaymentService(fresh, adapter)
    event.listen(engine, "before_cursor_execute", watch)
    try:
        service._send_unserved_to_staff(service._served_facilities())
    finally:
        event.remove(engine, "before_cursor_execute", watch)
        fresh.close()
    assert "UPDATE" in verbs and "SELECT" not in verbs[verbs.index("UPDATE"):]
    session.expire_all()                   # the change was saved through the other session
    assert session.get(PaymentCommand, dana.id).state == "NEEDS_STAFF"


# ------------------------------------------------------------------ answers that are not FMS answers

def test_a_proxy_error_page_is_not_taken_for_an_fms_answer():
    import httpx

    from desk.adapter import FMSError

    proxy = CubbyAdapter("http://fms", "key", transport=httpx.MockTransport(lambda r: httpx.Response(502, text="<html>Bad Gateway</html>")))
    with pytest.raises(FMSError):
        proxy.ledger(DANA)
    assert proxy.make_payment(DANA, "pm_DanaVisa4242", 24000).kind == "lost"


def test_a_success_with_no_payment_id_is_treated_as_lost():
    import httpx

    body = {"status": "success", "data": {"payment": {"status": "SUCCEEDED", "amount": 24000}}}
    odd = CubbyAdapter("http://fms", "key", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert odd.make_payment(DANA, "pm_DanaVisa4242", 24000).kind == "lost"


def test_a_second_authorization_after_the_first_payment_settled_charges_nothing(session, adapter, sim):
    first, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert first.state == "CONFIRMED"
    late = PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, 24000, 24000))
    assert late.state == "NEEDS_REAUTHORIZATION" and len(sim.payments(DANA)) == 1


def test_two_partial_payments_both_confirm(session, adapter, sim):
    first, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    second = PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, 14000, 14000))
    assert (first.state, second.state) == ("CONFIRMED", "CONFIRMED")


def test_a_lost_payment_is_never_matched_to_one_that_existed_before_it(session, adapter, sim):
    sim.behave(DANA, "decline")
    earlier = adapter.make_payment(DANA, "pm_DanaVisa4242", 24000)   # a decline taken outside the desk minutes ago
    sim.behave(DANA, "lose_response")
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    settle(session, adapter)
    assert command.state == "NEEDS_STAFF"
    assert command.suggested_payment_id not in (None, earlier.payment.provider_id)


def test_a_returned_counter_payment_reopens_a_paid_account(session, adapter, sim):
    case, _ = case_for(session, adapter, DANA)
    counter = sim.post("/sim/payments/offline", {"allocations": {DANA: 24000}}).json()["data"]["payment"]
    PaymentService(session, adapter).reconcile_all()
    assert session.get(DelinquencyCase, case.id).state == "paid"
    assert sim.post("/sim/payments/return", {"paymentId": counter["paymentId"]}).status_code == 200
    PaymentService(session, adapter).reconcile_all()
    assert session.get(DelinquencyCase, case.id).state == "reopened"


def test_an_older_reconciliation_never_overwrites_a_newer_one(adapter, sim, tmp_path):
    from desk.db import make_session_factory

    sim.behave(DANA, "late_application", lag=1)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'desk.db'}")
    with factory() as a, factory() as b:
        command, _, _ = pay(a, adapter, DANA, "pm_DanaVisa4242", 24000)
        stale = a.get(PaymentCommand, command.id)
        assert stale.state == "AWAITING_APPLICATION"
        settle(b, adapter)                      # the worker confirms it
        assert b.get(PaymentCommand, command.id).state == "CONFIRMED"
        stale.state = "PENDING_AT_FMS"           # the API's pass, working from what it read earlier
        PaymentService(a, adapter)._save()
        b.expire_all()
        assert b.get(PaymentCommand, command.id).state == "CONFIRMED"


def test_next_months_rent_does_not_reopen_a_settled_case(session, adapter, sim):
    _, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert session.get(DelinquencyCase, case.id).state == "paid"
    sim.post("/sim/charges", {"leaseId": DANA, "amount": 21500, "category": "RENT", "description": "December rent"})
    PaymentService(session, adapter).reconcile_all()
    assert session.get(DelinquencyCase, case.id).state == "paid"
    # still watched: if the November payment comes back now, this case reopens
    sim.post("/sim/payments/return", {"paymentId": sim.payments(DANA)[0]["paymentId"]})
    PaymentService(session, adapter).reconcile_all()
    assert session.get(DelinquencyCase, case.id).state == "reopened"


def test_a_status_the_desk_does_not_know_ends_with_staff(session, adapter, sim):
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.post("/sim/payments/return", {"paymentId": command.provider_payment_id, "status": "UNDER_REVIEW"})
    command.state = "AWAITING_APPLICATION"   # as if the desk had not seen it settle yet
    command.submitted_at -= timedelta(hours=2)
    session.commit()
    PaymentService(session, adapter).reconcile(command)
    assert command.state == "NEEDS_STAFF" and "does not know" in command.provider_reason


def test_a_stale_reconciliation_drops_out_without_an_error(adapter, sim, tmp_path):
    from desk.db import make_session_factory

    sim.behave(DANA, "late_application", lag=1)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'desk.db'}")
    with factory() as a, factory() as b:
        command, _, _ = pay(a, adapter, DANA, "pm_DanaVisa4242", 24000)
        stale = a.get(PaymentCommand, command.id)
        settle(b, adapter)
        assert b.get(PaymentCommand, command.id).state == "CONFIRMED"
        PaymentService(a, adapter).reconcile(stale)   # a's copy still says AWAITING_APPLICATION
        b.expire_all()
        assert b.get(PaymentCommand, command.id).state == "CONFIRMED"


def test_staff_cannot_clear_a_payment_the_fms_still_shows_pending(session, adapter, sim):
    sim.behave(DANA, "pending_then_succeed", lag=50)
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    command.submitted_at -= timedelta(hours=2)
    session.commit()
    PaymentService(session, adapter).reconcile(command)
    assert command.state == "NEEDS_STAFF"
    with pytest.raises(PaymentRefused, match="PENDING"):
        PaymentService(session, adapter).resolve_by_staff(command, False, "Customer says it did not go through", "J. Ortiz")


def test_staff_must_name_a_lead_before_calling_a_lost_payment_not_charged(session, adapter, sim):
    sim.behave(DANA, "lose_response")
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    settle(session, adapter)
    lead = command.suggested_payment_id
    with pytest.raises(PaymentRefused, match=lead):
        PaymentService(session, adapter).resolve_by_staff(command, False, "Nothing in the processor portal", "J. Ortiz")
    assert command.state == "NEEDS_STAFF"


def test_an_old_return_does_not_reopen_the_next_settled_episode(session, adapter, sim):
    first, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.post("/sim/payments/return", {"paymentId": first.provider_payment_id})
    # more than one pass: on a loaded machine a single pass's FMS read can time out, and a failed read changes nothing
    settle(session, adapter, times=3)
    assert session.get(DelinquencyCase, case.id).state == "reopened"
    again = PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, 24000, 24000))
    assert again.state == "CONFIRMED" and session.get(DelinquencyCase, case.id).state == "paid"
    sim.post("/sim/charges", {"leaseId": DANA, "amount": 21500, "category": "RENT", "description": "December rent"})
    settle(session, adapter, times=3)
    assert session.get(DelinquencyCase, case.id).state == "paid"


def test_an_old_return_first_stored_late_still_does_not_reopen_the_next_episode(session, adapter, sim):
    """What a slow read used to cause about one run in ten: the FMS's record of the returned payment is stored only after
    the next payment settled the case, with no change time. The desk's own record of when it saw the return decides."""
    from desk.db import utcnow
    from desk.models import ObservedPayment
    first, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.post("/sim/payments/return", {"paymentId": first.provider_payment_id})
    settle(session, adapter, times=3)
    again = PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, 24000, 24000))
    assert again.state == "CONFIRMED" and session.get(DelinquencyCase, case.id).state == "paid"
    row = session.scalar(select(ObservedPayment).where(ObservedPayment.provider_payment_id == first.provider_payment_id))
    row.status_changed_at, row.first_seen_at = None, utcnow()
    session.commit()
    sim.post("/sim/charges", {"leaseId": DANA, "amount": 21500, "category": "RENT", "description": "December rent"})
    settle(session, adapter, times=2)
    assert session.get(DelinquencyCase, case.id).state == "paid"


def test_a_reopened_case_stays_reopened_on_the_next_pass(session, adapter, sim):
    case, _ = case_for(session, adapter, DANA)
    counter = sim.post("/sim/payments/offline", {"allocations": {DANA: 24000}}).json()["data"]["payment"]
    PaymentService(session, adapter).reconcile_all()
    sim.post("/sim/payments/return", {"paymentId": counter["paymentId"]})
    PaymentService(session, adapter).reconcile_all()
    PaymentService(session, adapter).reconcile_all()
    assert session.get(DelinquencyCase, case.id).state == "reopened"


def test_staff_must_account_for_every_possible_match_before_not_charged(session, adapter, sim):
    sim.behave(DANA, "lose_response")
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.behave(DANA, "decline")
    other = adapter.make_payment(DANA, "pm_DanaVisa4242", 24000).payment   # a second matching attempt, declined
    settle(session, adapter, times=1)
    suggested = command.suggested_payment_id
    with pytest.raises(PaymentRefused, match=other.provider_id):
        PaymentService(session, adapter).resolve_by_staff(command, False, f"{suggested} is the tenant's own app payment", "J. Ortiz")
    assert command.state == "NEEDS_STAFF"


def test_a_payment_staff_identify_completes_even_when_the_ledger_is_ambiguous(session, adapter, sim):
    sim.behave(DANA, "late_application", lag=3)
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.post("/sim/payments/offline", {"allocations": {DANA: 24000}})
    settle(session, adapter)
    command.submitted_at -= timedelta(minutes=16)
    session.commit()
    settle(session, adapter, times=1)
    assert command.state == "NEEDS_STAFF"
    # naming the payment says which one this attempt is, not that it posted: still ambiguous, still unconfirmed
    PaymentService(session, adapter).resolve_by_staff(command, True, "Processor shows this card payment", "J. Ortiz", command.provider_payment_id)
    assert command.state != "CONFIRMED"
    command.state = "NEEDS_STAFF"
    session.commit()
    PaymentService(session, adapter).resolve_by_staff(command, True, "FMS ledger shows both payments applied", "J. Ortiz",
                                                      command.provider_payment_id, ledger_checked=True)
    assert command.state == "CONFIRMED"


def test_a_return_after_the_watch_window_is_still_caught(session, adapter, sim):
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    long_ago = command.confirmed_at - timedelta(days=200)
    command.confirmed_at = long_ago
    session.get(DelinquencyCase, case.id).resolved_at = long_ago
    session.commit()
    sim.post("/sim/payments/return", {"paymentId": command.provider_payment_id})
    sync_facility(session, adapter, ORLANDO)   # the returned money makes the lease delinquent again: a new case
    PaymentService(session, adapter).reconcile_all()
    assert session.get(PaymentCommand, command.id).state == "RETURNED"


def test_a_reopened_case_survives_a_declined_repayment(session, adapter, sim):
    case, _ = case_for(session, adapter, DANA)
    counter = sim.post("/sim/payments/offline", {"allocations": {DANA: 24000}}).json()["data"]["payment"]
    PaymentService(session, adapter).reconcile_all()
    sim.post("/sim/payments/return", {"paymentId": counter["paymentId"]})
    PaymentService(session, adapter).reconcile_all()
    sim.behave(DANA, "decline")
    PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, 24000, 24000))
    PaymentService(session, adapter).reconcile_all()
    assert session.get(DelinquencyCase, case.id).state == "reopened"


def test_a_payment_pending_at_the_fms_from_anyone_blocks_a_new_charge(session, adapter, sim):
    sim.behave(DANA, "pending_then_succeed", lag=50)
    adapter.make_payment(DANA, "pm_DanaVisa4242", 24000)   # autopay, still pending
    with pytest.raises(PaymentRefused, match="still pending"):
        pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    assert len(sim.payments(DANA)) == 1


def test_a_pending_lead_cannot_be_named_away(session, adapter, sim):
    sim.behave(DANA, "gateway_error")
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.behave(DANA, "pending_then_succeed", lag=50)
    lead = adapter.make_payment(DANA, "pm_DanaVisa4242", 24000).payment
    settle(session, adapter)
    assert command.state == "NEEDS_STAFF"
    with pytest.raises(PaymentRefused, match="still PENDING"):
        PaymentService(session, adapter).resolve_by_staff(command, False, f"{lead.provider_id} is the tenant's own app payment", "J. Ortiz")


def test_staff_cannot_name_a_payment_that_existed_before_the_attempt(session, adapter, sim):
    sim.behave(DANA, "decline")
    old = adapter.make_payment(DANA, "pm_DanaVisa4242", 24000).payment
    sim.behave(DANA, "gateway_error")
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    settle(session, adapter)
    with pytest.raises(PaymentRefused, match="already on the lease"):
        PaymentService(session, adapter).resolve_by_staff(command, True, "That one", "J. Ortiz", old.provider_id)


def test_an_fms_amount_that_differs_from_the_authorized_one_goes_to_staff(session, fms_url, sim):
    class Short(CubbyAdapter):
        def payments_for_lease(self, lease_provider_id):
            from dataclasses import replace
            return [replace(p, amount=p.amount // 2) for p in super().payments_for_lease(lease_provider_id)]

    short = Short(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)
    sim.behave(DANA, "late_application", lag=1)
    command, _, _ = pay(session, short, DANA, "pm_DanaVisa4242", 24000)
    settle(session, short)
    assert command.state == "NEEDS_STAFF" and "12000 cents" in command.provider_reason


def test_a_ledger_without_a_balance_is_not_a_zero_balance():
    import httpx

    from desk.adapter import FMSError

    body = {"status": 200, "data": {"ledgers": [{"leaseId": DANA, "balances": {}, "bookEntries": []}]}}
    odd = CubbyAdapter("http://fms", "key", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(FMSError, match="totalDue"):
        odd.ledger(DANA)


def test_a_counter_payment_not_yet_on_the_ledger_blocks_a_new_charge(session, fms_url, sim):
    class Unposted(CubbyAdapter):
        """The FMS lists a counter payment as SUCCEEDED, but its ledger has not caught up."""
        def payments_for_lease(self, lease_provider_id):
            from dataclasses import replace
            real = super().payments_for_lease(lease_provider_id)
            fake = replace(real[0], provider_id="payment_counter01", status="SUCCEEDED", amount=24000) if real else None
            return real + ([fake] if fake else [])

    case, _ = case_for(session, CubbyAdapter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0), DANA)
    sim.behave(DANA, "decline")
    CubbyAdapter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0).make_payment(DANA, "pm_DanaVisa4242", 100)
    sim.behave(DANA, "approve")
    unposted = Unposted(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)
    with pytest.raises(PaymentRefused, match="not visibly on the ledger"):
        PaymentService(session, unposted).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, 24000, 24000))
    assert all(p["status"] != "SUCCEEDED" for p in sim.payments(DANA))


def test_a_desk_payment_confirmed_while_a_ledger_read_was_being_stored_does_not_vouch_for_a_counter_payment(session, fms_url, sim):
    """A ledger read began before the desk's own payment B reached the ledger, and its row was stored after B was confirmed. B's money is in the
    ledger's later increase, so B must be subtracted from it. If it is not, B stands in for a counter payment A that never reached the ledger,
    and the desk charges the card on top of it."""
    from dataclasses import replace

    plain = CubbyAdapter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)
    b, case, _ = pay(session, plain, DANA, "pm_DanaVisa4242", 10000)         # B: $100 of the $240, taken and confirmed by the desk
    settle(session, plain)
    assert b.state == "CONFIRMED" and b.confirmed_at is not None
    lease = session.get(Lease, case.lease_id)
    snaps = session.scalars(select(LedgerSnapshot).where(LedgerSnapshot.lease_id == lease.id).order_by(LedgerSnapshot.id)).all()
    stale = snaps[0]
    assert stale.total_due == 24000                                           # the read that began before B was on the ledger
    session.execute(delete(LedgerSnapshot).where(LedgerSnapshot.lease_id == lease.id, LedgerSnapshot.id != stale.id))
    stale.observed_at = b.confirmed_at + timedelta(seconds=1)                 # and was stored after B was confirmed
    session.commit()

    class Counter(CubbyAdapter):
        """The FMS lists a $100 counter payment A, taken after that read was stored, as succeeded. Its ledger has not caught up."""
        def payments_for_lease(self, lease_provider_id):
            real = super().payments_for_lease(lease_provider_id)
            return real + [replace(real[0], provider_id="payment_counter01", status="SUCCEEDED", amount=10000, created_at=stale.observed_at + timedelta(seconds=2))]

    ledger_now = plain.ledger(DANA)
    assert ledger_now.total_due == 14000                                      # B is on the ledger; A is not
    with pytest.raises(PaymentRefused, match="not visibly on the ledger"):
        PaymentService(session, Counter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)).take_payment(
            case.id, "pm_DanaVisa4242", Authorization.new(True, 14000, 14000))
    assert sum(1 for p in sim.payments(DANA) if p["status"] == "SUCCEEDED") == 1          # nothing was charged on top of B


def test_a_ledger_reading_is_stamped_with_when_the_read_began_not_when_it_was_stored(session, fms_url, sim):
    """The read can finish, and the row be stored, after things happened that the reading does not show."""
    from datetime import datetime, timezone

    seen = {}

    class Slow(CubbyAdapter):
        def ledger(self, lease_provider_id):
            seen["asked"] = datetime.now(timezone.utc).replace(tzinfo=None)
            import time
            time.sleep(0.05)
            return super().ledger(lease_provider_id)

    case, lease = case_for(session, CubbyAdapter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0), DANA)
    refresh_case(session, Slow(fms_url, "sim_org_northstar_full", timeout_seconds=5.0), case)
    snap = session.get(LedgerSnapshot, case.last_snapshot_id)
    assert snap.read_at is not None and snap.read_at <= seen["asked"] < snap.observed_at
    assert (snap.observed_at - snap.read_at).total_seconds() >= 0.05            # the wait for the FMS lies between the two stamps


def test_staff_cannot_swap_the_payment_id_the_fms_gave_this_attempt(session, adapter, sim):
    sim.behave(DANA, "pending_then_succeed", lag=50)
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    command.submitted_at -= timedelta(hours=2)
    session.commit()
    PaymentService(session, adapter).reconcile(command)
    assert command.state == "NEEDS_STAFF"
    with pytest.raises(PaymentRefused, match="already payment"):
        PaymentService(session, adapter).resolve_by_staff(command, True, "It is this other one", "J. Ortiz", "payment_other")


def test_a_dispute_resolved_at_the_fms_is_rechecked_by_staff_not_reconfirmed(session, adapter, sim):
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    command.state, command.provider_status = "DISPUTED", "DISPUTED"   # as the desk recorded it during the dispute
    session.commit()
    PaymentService(session, adapter).reconcile(command)   # the FMS shows SUCCEEDED again
    assert command.state == "NEEDS_STAFF" and "SUCCEEDED again" in command.provider_reason


def test_two_outside_payments_cannot_lean_on_one_posting(session, fms_url, sim):
    class TwoCounter(CubbyAdapter):
        """Two counter payments listed as succeeded; the simulator has applied only one of them."""
        def payments_for_lease(self, lease_provider_id):
            from dataclasses import replace
            real = super().payments_for_lease(lease_provider_id)
            return real + [replace(real[0], provider_id="payment_counter02", amount=10000)]

    plain = CubbyAdapter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)
    case, _ = case_for(session, plain, DANA)
    sim.post("/sim/payments/offline", {"allocations": {DANA: 10000}})
    with pytest.raises(PaymentRefused, match="not visibly on the ledger"):
        PaymentService(session, TwoCounter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)).take_payment(
            case.id, "pm_DanaVisa4242", Authorization.new(True, 14000, 14000))


def test_a_reversed_payment_that_succeeds_again_blocks_a_new_charge(session, adapter, sim):
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    command.state, command.reversed_at = "DISPUTED", command.confirmed_at   # as if the dispute had been recorded
    session.commit()
    with pytest.raises(PaymentRefused, match="succeeded again"):
        PaymentService(session, adapter).take_payment(case.id, "pm_DanaVisa4242", Authorization.new(True, 0, 1))


def test_a_resolved_dispute_waits_while_another_payment_holds_the_lease(session, adapter, sim):
    a, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    a.state, a.reversed_at = "DISPUTED", a.confirmed_at
    lease = session.get(Lease, a.lease_id)
    b = PaymentCommand(case_id=case.id, lease_id=lease.id, idempotency_key="case-b:auth-b", amount=24000, payment_method_id="pm_DanaVisa4242",
                       authorization={}, state="PENDING_AT_FMS", balance_before=24000, prepaid_before=0, submitted_at=a.confirmed_at)
    session.add(b)
    session.commit()
    PaymentService(session, adapter).reconcile_all()   # no error, and the pass goes on
    assert a.state == "DISPUTED" and "SUCCEEDED again" in a.provider_reason


def test_staff_cannot_confirm_a_payment_the_fms_or_the_ledger_contradicts(session, adapter, sim):
    sim.behave(DANA, "gateway_error")
    command, _, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.behave(DANA, "decline")
    declined = adapter.make_payment(DANA, "pm_DanaVisa4242", 24000).payment
    settle(session, adapter)
    with pytest.raises(PaymentRefused, match="DECLINED"):
        PaymentService(session, adapter).resolve_by_staff(command, True, "Ledger checked", "J. Ortiz", declined.provider_id, ledger_checked=True)
    assert command.state == "NEEDS_STAFF" and command.provider_payment_id is None


def test_a_reversal_older_than_the_case_does_not_reopen_it(session, adapter, sim):
    first, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 24000)
    sim.post("/sim/payments/return", {"paymentId": first.provider_payment_id})
    from desk.db import make_session_factory
    with make_session_factory("sqlite:///:memory:")() as other:   # a desk that starts now sees the old return first
        new_case, _ = case_for(other, adapter, DANA)
        PaymentService(other, adapter).reconcile_all()
        assert other.get(DelinquencyCase, new_case.id).state == "open"


def test_a_desk_payment_posting_late_cannot_vouch_for_an_outside_payment(session, fms_url, sim):
    from desk.db import utcnow

    taken = {}

    class WithCounter(CubbyAdapter):
        """The FMS also lists outside payment A, taken after the snapshot and before B posted; A never reached the ledger."""
        def payments_for_lease(self, lease_provider_id):
            from dataclasses import replace
            real = super().payments_for_lease(lease_provider_id)
            return real + [replace(real[0], provider_id="payment_outsideA", amount=10000, created_at=taken["a"])]

    plain = CubbyAdapter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)
    sim.behave(DANA, "late_application", lag=2)
    b, case, _ = pay(session, plain, DANA, "pm_DanaVisa4242", 10000)   # B: succeeded, not on the ledger; a snapshot follows
    assert b.state == "AWAITING_APPLICATION"
    taken["a"] = utcnow()                                               # A is taken now
    settle(session, plain)                                              # then B posts
    assert b.state == "CONFIRMED" and sim.total_due(DANA) == 14000
    with pytest.raises(PaymentRefused, match="not visibly on the ledger"):
        PaymentService(session, WithCounter(fms_url, "sim_org_northstar_full", timeout_seconds=5.0)).take_payment(
            case.id, "pm_DanaVisa4242", Authorization.new(True, 14000, 14000))


def test_simulator_controls_refuse_bad_requests_without_a_500(sim):
    assert sim.post("/sim/behaviors", {"mode": "approve"}).status_code == 404
    assert sim.post("/sim/behaviors", {"leaseId": DANA, "mode": "approve", "lag": "soon"}).status_code == 400
    assert sim.post("/sim/charges", {"leaseId": "lease_nope", "amount": 100}).status_code == 404
    assert sim.post("/sim/payments/offline", {"allocations": {}}).status_code == 400
    assert sim.post("/sim/payments/offline", {"allocations": {DANA: 100, MARCUS: 100}}).status_code == 400


def test_a_settled_case_reopens_when_its_payment_is_first_seen_already_returned(session, adapter, sim):
    case, _ = case_for(session, adapter, DANA)
    counter = sim.post("/sim/payments/offline", {"allocations": {DANA: 24000}}).json()["data"]["payment"]
    sync_facility(session, adapter, ORLANDO)   # the ledger shows nothing due: paid, without the desk ever reading the payment
    refresh_case(session, adapter, session.get(DelinquencyCase, case.id))
    session.commit()
    assert session.get(DelinquencyCase, case.id).state == "paid"
    sim.post("/sim/payments/return", {"paymentId": counter["paymentId"]})
    PaymentService(session, adapter).reconcile_all()   # first sight of that payment: already returned
    assert session.get(DelinquencyCase, case.id).state == "reopened"
