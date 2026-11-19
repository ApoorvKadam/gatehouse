"""Money carried through rental charges and lease endings."""

from datetime import date, timedelta

import pytest

from core.books import RENT, at_local

from .test_core_renting import accept, act, billing, books, checkout, facility, move_out, ok, pay, rent, rented


def test_credit_used_by_another_rental_follows_the_payment_when_returned():
    store, tenant, _ = facility()
    first = checkout(store, tenant, "A101")
    assert pay(store, tenant, first, 30000).allowed
    payment = rent(store).rentals[first].payments[0]
    assert accept(store, tenant, first).allowed

    second = checkout(store, tenant, "A102")
    assert pay(store, tenant, second, 1).allowed
    assert accept(store, tenant, second).allowed
    lease = rent(store).rentals[second].lease
    before = books(store)
    assert before.receivable(lease) == 0
    used = sum(u.cents for u in before.credit_uses[tenant] if u.lot == payment and u.lease == lease)
    assert used > 0

    billing(store, lambda led: led.return_payment(at_local(date(2026, 11, 11), 10), payment))
    assert books(store).receivable(lease) == used


def test_final_bill_rounds_used_days_from_the_monthly_rate_once():
    store, tenant, _ = facility()
    start = at_local(date(2026, 11, 11), 10)
    end = at_local(date(2026, 11, 13), 10)
    rental = rented(store, tenant, "A101", start)
    moveout = move_out(store, rental, end)
    lines = rent(store).moveouts[moveout].bill["rent"]
    assert lines == [{"month": "2026-11", "billed_cents": 6667, "kept_cents": 1000}]


def test_final_bill_never_keeps_more_than_the_original_charge_after_a_rate_change():
    from core.billing import Ledger
    from core.renting import end_lease
    store, tenant, _ = facility()
    start = at_local(date(2026, 11, 25), 10)
    rental = rented(store, tenant, "A101", start)
    lease = rent(store).rentals[rental].lease
    led = Ledger(books(store))
    led.books.leases[lease].rent_cents = 20000
    line = end_lease(led, lease, date(2026, 11, 29), at_local(date(2026, 11, 29), 10), "code")[0]
    assert line["billed_cents"] == 2000 and line["kept_cents"] == 2000


def test_transfer_rounds_prepaid_old_rent_from_the_monthly_rate():
    store, tenant, _ = facility()
    start = at_local(date(2026, 11, 11), 10)
    transfer_day = at_local(date(2026, 11, 14), 10)
    old = rented(store, tenant, "A101", start)
    old_lease = rent(store).rentals[old].lease
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), transfer_day)
    ok(store, act("approve_transfer", "staff:manager", transfer,
                  until=(transfer_day + timedelta(days=1)).isoformat()), transfer_day)
    state = rent(store)
    new = state.transfers[transfer].target
    state = rent(store)
    short = state.short(state.rentals[new], transfer_day, state.rentals[old])
    if short:
        billing(store, lambda led: led.receive_payment(tenant, short, transfer_day))
    assert accept(store, tenant, new, transfer_day).allowed
    ok(store, act("complete_transfer", "staff:manager", transfer), transfer_day)
    live_rent = [c for c in books(store).tenant_charges(tenant)
                 if c.lease == old_lease and c.kind == RENT and not c.closed]
    assert [c.cents for c in live_rent] == [1000]


def test_credit_alone_starts_an_accepted_rental_and_applies_the_lot():
    store, tenant, _ = facility()
    rental = checkout(store, tenant, "A101")
    total = rent(store).rentals[rental].quote["total_cents"]
    payment = billing(store, lambda led: led.receive_payment(tenant, total, at_local(date(2026, 11, 10), 10)))
    assert accept(store, tenant, rental).allowed
    r = rent(store).rentals[rental]
    assert r.state == "active"
    assert books(store).receivable(r.lease) == 0
    assert sum(u.cents for u in books(store).credit_uses[tenant] if u.lot == payment and u.lease == r.lease) == total


def test_first_card_attempt_only_requests_the_shortfall():
    from core.delinquency import SimulatedProcessor
    from core.renting import pay_rental
    from core.store import BASE
    store, tenant, _ = facility()
    rental = checkout(store, tenant, "A101")
    now = at_local(date(2026, 11, 10), 10)
    total = rent(store).rentals[rental].quote["total_cents"]
    billing(store, lambda led: led.receive_payment(tenant, 8000, now))
    intent = pay_rental(store, BASE, SimulatedProcessor(), rental, "card_ok", now)
    assert intent.cents == total - 8000


def test_transfer_acceptance_requires_the_current_amount():
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    old = rented(store, tenant, "A101", now)
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), now)
    new = rent(store).transfers[transfer].target
    state = rent(store)
    amount = state.short(state.rentals[new], now, state.rentals[old])
    digest = state.agreements[state.rentals[new].agreement].sha256
    assert not ok_accept(store, tenant, new, digest, amount + 1, now).allowed
    assert ok_accept(store, tenant, new, digest, amount, now).allowed
    assert rent(store).agreements[state.rentals[new].agreement].amount_cents == amount


def ok_accept(store, tenant, rental, digest, amount, now):
    from .test_core_renting import go
    return go(store, act("accept_agreement", "renter", rental, tenant, sha256=digest, amount_cents=amount), now)


def test_changed_transfer_amount_requires_another_acceptance():
    from .test_core_renting import go
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    old = rented(store, tenant, "A101", now)
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), now)
    ok(store, act("approve_transfer", "staff:manager", transfer), now)
    new = rent(store).transfers[transfer].target
    assert accept(store, tenant, new, now).allowed
    billing(store, lambda led: led.receive_payment(tenant, 100, now))
    decision = go(store, act("complete_transfer", "staff:manager", transfer), now)
    assert not decision.allowed
    assert "amount to pay changed" in decision.check
    assert not rent(store).rentals[new].accepted
    assert accept(store, tenant, new, now).allowed


def test_transfer_payment_after_acceptance_preserves_the_accepted_amount():
    from core.delinquency import SimulatedProcessor
    from core.renting import pay_rental
    from core.store import BASE
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    old = rented(store, tenant, "A101", now)
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), now)
    ok(store, act("approve_transfer", "staff:manager", transfer), now)
    new = rent(store).transfers[transfer].target
    assert accept(store, tenant, new, now).allowed
    pay_rental(store, BASE, SimulatedProcessor(), new, "card_ok", now)
    ok(store, act("complete_transfer", "staff:manager", transfer), now)
    assert rent(store).rentals[new].state == "active"


def test_new_charge_after_transfer_payment_requires_acceptance_again():
    from core.books import ADMIN_FEE
    from core.delinquency import SimulatedProcessor
    from core.renting import pay_rental
    from core.store import BASE
    from .test_core_renting import go
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    old = rented(store, tenant, "A101", now)
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), now)
    ok(store, act("approve_transfer", "staff:manager", transfer), now)
    new = rent(store).transfers[transfer].target
    assert accept(store, tenant, new, now).allowed
    pay_rental(store, BASE, SimulatedProcessor(), new, "card_ok", now)
    lease = rent(store).rentals[old].lease
    billing(store, lambda led: led.charge_by_hand(now, lease, ADMIN_FEE, 100, None))
    assert not go(store, act("complete_transfer", "staff:manager", transfer), now).allowed
    assert not rent(store).rentals[new].accepted


def test_legacy_transfer_acceptance_is_reset_so_it_can_be_accepted_again():
    from core.renting import RentLedger, load
    from core.store import BASE
    from .test_core_renting import go
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    old = rented(store, tenant, "A101", now)
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), now)
    ok(store, act("approve_transfer", "staff:manager", transfer), now)
    state = rent(store)
    new = state.transfers[transfer].target
    agreement = state.rentals[new].agreement
    with store.write():
        b, state = load(store, BASE)
        led = RentLedger(b, state)
        led.emit("agreement.accepted", now, {"agreement": agreement, "rental": new, "by": tenant}, "legacy-accept")
        store.append(BASE, led.pending)
    assert not go(store, act("complete_transfer", "staff:manager", transfer), now).allowed
    assert not rent(store).rentals[new].accepted
    assert accept(store, tenant, new, now).allowed


def test_credit_reserved_for_refund_is_not_available_to_start_a_rental():
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    rental = checkout(store, tenant, "A101")
    total = rent(store).rentals[rental].quote["total_cents"]
    billing(store, lambda led: (led.receive_payment(tenant, total, now),
                                led.reserve_refund(now, tenant, total, "refund:test")))
    assert accept(store, tenant, rental, now).allowed
    assert rent(store).rentals[rental].state != "active"
    assert rent(store).short(rent(store).rentals[rental], now) == total


def test_credit_pays_older_charges_before_the_new_lease():
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    old = billing(store, lambda led: led.sign_lease(at_local(date(2026, 11, 1), 9), tenant, "A102", 9000, []))
    charge = books(store).of_lease[old][0]
    dispute = billing(store, lambda led: led.open_dispute(now, charge, "check rent", True, "staff:office_manager"))
    payment = billing(store, lambda led: led.receive_payment(tenant, 30000, now))
    # A folded decision can leave credit and an unpaid charge together.
    billing(store, lambda led: led.emit("dispute.decided", now, {"dispute": dispute, "outcome": "upheld"}, "test-decision"))
    assert books(store).receivable(old) == 9000
    assert books(store).credit[tenant] == 30000
    rental = checkout(store, tenant, "A101", now)
    total = rent(store).rentals[rental].quote["total_cents"]
    assert accept(store, tenant, rental, now).allowed
    b = books(store)
    lease = rent(store).rentals[rental].lease
    uses = [u for u in b.credit_uses[tenant] if u.lot == payment]
    assert [(u.lease, u.cents) for u in uses if u.lease == old] == [(old, 9000)]
    assert sum(u.cents for u in uses if u.lease == lease) == total
    assert uses[0].lease == old
    assert b.receivable(old) == 0 and b.receivable(lease) == 0


def test_changed_balance_during_checkout_is_recalculated():
    from core.books import ADMIN_FEE
    from core.delinquency import SimulatedProcessor
    from core.renting import pay_rental
    from core.store import BASE
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    rental = checkout(store, tenant, "A101", now)
    total = rent(store).rentals[rental].quote["total_cents"]
    billing(store, lambda led: led.receive_payment(tenant, total, now))
    billing(store, lambda led: led.sign_lease(at_local(date(2026, 11, 1), 9), tenant, "A102", 9000, [ADMIN_FEE]))
    processor = SimulatedProcessor()
    intent = pay_rental(store, BASE, processor, rental, "card_ok", now)
    assert intent.cents == total
    assert accept(store, tenant, rental, now).allowed
    state = rent(store)
    assert state.rentals[rental].state == "awaiting_payment"
    assert state.short(state.rentals[rental], now) == 2000
    intent = pay_rental(store, BASE, processor, rental, "card_ok", now)
    assert intent.cents == 2000
    assert rent(store).rentals[rental].state == "active"


def test_return_after_credit_only_move_in_reopens_balance_once_after_restart(tmp_path):
    from core import delinquency
    from core.store import BASE, Store
    path = str(tmp_path / "books.db")
    processor_path = str(tmp_path / "processor.json")
    store, tenant, _ = facility(path)
    processor = delinquency.SimulatedProcessor(processor_path)
    now = at_local(date(2026, 11, 10), 10)
    rental = checkout(store, tenant, "A101", now)
    total = rent(store).rentals[rental].quote["total_cents"]
    intent = delinquency.pay(store, BASE, processor, tenant, total, "card_ok", now)
    assert accept(store, tenant, rental, now).allowed
    lease = rent(store).rentals[rental].lease
    assert books(store).receivable(lease) == 0
    processor.settle(intent.outside, "returned", now)
    event = processor.outbox[-1]
    delinquency.webhook(store, processor, event, now)
    assert books(store).receivable(lease) == total
    assert rent(store).rentals[rental].state == "active" and books(store).leases[lease].ended is None
    assert len(delinquency.open_tasks(books(store))) == 1
    store.db.close()
    store = Store(path)
    processor = delinquency.SimulatedProcessor(processor_path)
    assert delinquency.webhook(store, processor, event, now) == "duplicate"
    delinquency.reconcile(store, BASE, processor, now)
    assert books(store).receivable(lease) == total
    assert len(delinquency.open_tasks(books(store))) == 1
    assert len([e for e in store.events() if e.kind == "payment.returned"]) == 1


def test_return_of_payment_used_for_transferred_rent_goes_to_staff_once(tmp_path):
    from core import delinquency
    from core.store import BASE, Store
    path = str(tmp_path / "books.db")
    processor_path = str(tmp_path / "processor.json")
    store, tenant, _ = facility(path)
    processor = delinquency.SimulatedProcessor(processor_path)
    now = at_local(date(2026, 11, 10), 10)
    old = checkout(store, tenant, "A101", now)
    total = rent(store).rentals[old].quote["total_cents"]
    intent = delinquency.pay(store, BASE, processor, tenant, total, "card_ok", now)
    assert accept(store, tenant, old, now).allowed
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), now)
    ok(store, act("approve_transfer", "staff:manager", transfer), now)
    new = rent(store).transfers[transfer].target
    state = rent(store)
    short = state.short(state.rentals[new], now, state.rentals[old])
    if short:
        billing(store, lambda led: led.receive_payment(tenant, short, now))
    assert accept(store, tenant, new, now).allowed
    ok(store, act("complete_transfer", "staff:manager", transfer), now)
    processor.settle(intent.outside, "returned", now)
    event = processor.outbox[-1]
    delinquency.webhook(store, processor, event, now)
    assert not books(store).payments[intent.payment].returned
    assert delinquency.state(books(store)).intents[intent.id].state == "succeeded"
    assert len(delinquency.open_tasks(books(store))) == 1
    store.db.close()
    store = Store(path)
    processor = delinquency.SimulatedProcessor(processor_path)
    assert delinquency.webhook(store, processor, event, now) == "duplicate"
    delinquency.reconcile(store, BASE, processor, now)
    assert len(delinquency.open_tasks(books(store))) == 1
    assert not books(store).payments[intent.payment].returned
    assert not [e for e in store.events() if e.kind == "payment.returned"]


def test_pending_return_credit_cannot_pay_for_another_rental():
    from core import delinquency
    from core.store import BASE
    from core.renting import pay_rental
    store, tenant, _ = facility()
    processor = delinquency.SimulatedProcessor()
    now = at_local(date(2026, 11, 10), 10)
    old = checkout(store, tenant, "A101", now)
    intent = delinquency.pay(store, BASE, processor, tenant, 50000, "card_ok", now)
    assert accept(store, tenant, old, now).allowed
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), now)
    ok(store, act("approve_transfer", "staff:manager", transfer), now)
    new = rent(store).transfers[transfer].target
    assert accept(store, tenant, new, now).allowed
    ok(store, act("complete_transfer", "staff:manager", transfer), now)
    processor.settle(intent.outside, "returned", now)
    event = processor.outbox[-1]
    delinquency.webhook(store, processor, event, now)
    assert not books(store).payments[intent.payment].returned
    assert len(delinquency.open_tasks(books(store))) == 1
    assert next(cents for source, cents in books(store).lots[tenant] if source == intent.payment) > 0

    third = checkout(store, tenant, "A102", now)
    assert rent(store).short(rent(store).rentals[third], now) > 0
    assert accept(store, tenant, third, now).allowed
    assert rent(store).rentals[third].state == "awaiting_payment"
    pay_rental(store, BASE, processor, third, "card_ok", now)
    third_lease = rent(store).rentals[third].lease
    assert rent(store).rentals[third].state == "active"
    assert not any(u.lot == intent.payment and u.lease == third_lease for u in books(store).credit_uses[tenant])
    delinquency.reconcile(store, BASE, processor, now)
    assert len(delinquency.open_tasks(books(store))) == 1


def test_pending_return_cannot_fund_transfer_through_reversed_rent():
    from core import delinquency
    from core.renting import end_lease
    from core.store import BASE
    from .test_core_renting import go
    store, tenant, _ = facility()
    processor = delinquency.SimulatedProcessor()
    now = at_local(date(2026, 11, 10), 10)
    old = checkout(store, tenant, "A101", now)
    intent = delinquency.pay(store, BASE, processor, tenant, 20000, "card_ok", now)
    assert accept(store, tenant, old, now).allowed
    remaining = next(cents for source, cents in books(store).lots[tenant] if source == intent.payment)
    billing(store, lambda led: led.reserve_refund(now, tenant, remaining, "refund:pending"))
    processor.settle(intent.outside, "returned", now)
    delinquency.webhook(store, processor, processor.outbox[-1], now)
    assert intent.payment in books(store).pending_returns

    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="B201"), now)
    ok(store, act("approve_transfer", "staff:manager", transfer), now)
    state = rent(store)
    new = state.transfers[transfer].target
    assert state.short(state.rentals[new], now, state.rentals[old]) == state.rentals[new].quote["total_cents"]
    assert accept(store, tenant, new, now).allowed
    assert not go(store, act("complete_transfer", "staff:manager", transfer), now).allowed
    assert rent(store).rentals[new].lease is None
    old_lease = rent(store).rentals[old].lease
    billing(store, lambda led: end_lease(led, old_lease, date(2026, 11, 9), now, "code"))
    third = checkout(store, tenant, "A102", now)
    assert accept(store, tenant, third, now).allowed
    assert rent(store).rentals[third].state == "awaiting_payment"
    assert any(source.startswith("release:") and cents > 0 for source, cents in books(store).lots[tenant])


def test_expired_future_rental_cannot_accept_after_another_hold():
    from core.renting import DO, RentLedger, load
    from core.store import BASE
    store, tenant, other = facility()
    now = at_local(date(2026, 11, 10), 10)
    later = now + timedelta(hours=1)
    reservation = ok(store, act("reserve", "staff:manager", "A101", tenant,
                                start="2026-11-15", until=(now + timedelta(minutes=30)).isoformat()), now)
    rental = ok(store, act("start_checkout", "staff:manager", reservation, tenant), now)
    total = rent(store).rentals[rental].quote["total_cents"]
    billing(store, lambda led: led.receive_payment(tenant, total, now))
    with store.write():
        b, state = load(store, BASE)
        led = RentLedger(b, state)
        DO["reserve"](state, led, act("reserve", "renter", "A101", other), later)
        store.append(BASE, led.pending)
    state = rent(store)
    assert state.holder("A101", later)[0] == "reservation"
    digest = state.agreements[state.rentals[rental].agreement].sha256
    command = act("accept_agreement", "renter", rental, tenant, sha256=digest)
    assert state.refusal(command, later) == "the rental no longer holds the unit"
    from .test_core_renting import go
    assert not go(store, command, later).allowed
    assert rent(store).rentals[rental].lease is None


def test_future_rental_starts_on_its_day_with_account_credit():
    from core.renting import StandIn, advance
    from core.store import BASE
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    day = at_local(date(2026, 11, 15), 10)
    reservation = ok(store, act("reserve", "staff:manager", "A101", tenant,
                                start="2026-11-15", until=(now + timedelta(hours=1)).isoformat()), now)
    rental = ok(store, act("start_checkout", "staff:manager", reservation, tenant), now)
    total = rent(store).rentals[rental].quote["total_cents"]
    billing(store, lambda led: led.receive_payment(tenant, total, now))
    assert accept(store, tenant, rental, now).allowed
    assert rent(store).rentals[rental].state == "awaiting_payment"
    funded = rent(store).rentals[rental]
    assert funded.lease and books(store).receivable(funded.lease) == 0
    second = checkout(store, tenant, "A102", now)
    assert rent(store).short(rent(store).rentals[second], now) > 0
    advance(store, BASE, StandIn(), now + timedelta(days=1))
    assert rent(store).holder("A101", now + timedelta(days=1)) == ("rental", rental)
    advance(store, BASE, StandIn(), day)
    started = rent(store).rentals[rental]
    assert started.state == "active"
    assert books(store).receivable(started.lease) == 0


@pytest.mark.parametrize("released", [False, True])
def test_future_rental_waits_for_a_return_of_its_funding(released):
    from core.renting import StandIn, advance
    from core.store import BASE
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    day = at_local(date(2026, 11, 15), 10)
    reservation = ok(store, act("reserve", "staff:manager", "A101", tenant, start="2026-11-15"), now)
    rental = ok(store, act("start_checkout", "staff:manager", reservation, tenant), now)
    total = rent(store).rentals[rental].quote["total_cents"]
    if released:
        old = billing(store, lambda led: led.sign_lease(now, tenant, "A102", total, [], start=date(2026, 11, 1)))
    payment = billing(store, lambda led: led.receive_payment(tenant, total, now))
    if released:
        charge = books(store).of_lease[old][0]
        billing(store, lambda led: led.reverse(now, charge, "correction", "staff:manager"))
    assert accept(store, tenant, rental, now).allowed
    lease = rent(store).rentals[rental].lease
    assert books(store).receivable(lease) == 0
    origins = [src for cid in books(store).of_lease[lease] for _, _, src in books(store).charges[cid].paid_effective]
    assert any(src.startswith("release:") for src in origins) if released else payment in origins
    billing(store, lambda led: led.emit("payment.return_pending", now, {"payment": payment}, "pending:test"))
    advance(store, BASE, StandIn(), day)
    assert rent(store).rentals[rental].state != "active"
    assert rent(store).holder("A101", day) == ("rental", rental)
    if not released:
        billing(store, lambda led: led.return_payment(day, payment))
        advance(store, BASE, StandIn(), day)
        assert rent(store).rentals[rental].state != "active"
        assert rent(store).holder("A101", day) == ("rental", rental)
        billing(store, lambda led: led.receive_payment(tenant, total, day))
        advance(store, BASE, StandIn(), day)
        assert rent(store).rentals[rental].state == "active"


def test_cancelling_a_future_credit_funded_rental_releases_its_charges():
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    reservation = ok(store, act("reserve", "staff:manager", "A101", tenant, start="2026-11-15"), now)
    rental = ok(store, act("start_checkout", "staff:manager", reservation, tenant), now)
    total = rent(store).rentals[rental].quote["total_cents"]
    billing(store, lambda led: led.receive_payment(tenant, total, now))
    assert accept(store, tenant, rental, now).allowed
    lease = rent(store).rentals[rental].lease
    ok(store, act("cancel_reservation", "staff:manager", reservation, tenant), now)
    assert rent(store).rentals[rental].state == "cancelled"
    assert books(store).leases[lease].ended is not None
    assert books(store).receivable(lease) == 0 and books(store).credit[tenant] == total


def test_return_of_direct_payment_for_rent_opens_one_staff_task():
    from core import delinquency
    from core.store import BASE
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 10), 10)
    rental = rented(store, tenant, "A101", now)
    lease = rent(store).rentals[rental].lease
    december = at_local(date(2026, 12, 1), 10)
    billing(store, lambda led: led.post_rent("2026-12"))
    processor = delinquency.SimulatedProcessor()
    intent = delinquency.pay(store, BASE, processor, tenant, 10000, "card_ok", december)
    assert books(store).payments[intent.payment].applications
    assert books(store).receivable(lease) == 0
    processor.settle(intent.outside, "returned", december)
    event = processor.outbox[-1]
    delinquency.webhook(store, processor, event, december)
    assert books(store).receivable(lease) == 10000
    assert len(delinquency.open_tasks(books(store))) == 1
    delinquency.webhook(store, processor, event, december)
    delinquency.reconcile(store, BASE, processor, december)
    assert len(delinquency.open_tasks(books(store))) == 1


def test_transfer_first_rent_rounds_half_up_in_quote_and_charge():
    store, tenant, _ = facility()
    now = at_local(date(2026, 11, 11), 10)
    old = rented(store, tenant, "A102", now)
    transfer = ok(store, act("request_transfer", "renter", old, tenant, unit="A101"), now)
    ok(store, act("approve_transfer", "staff:manager", transfer), now)
    state = rent(store)
    new = state.transfers[transfer].target
    assert state.rentals[new].quote["first_cents"] == 6667
    short = state.short(state.rentals[new], now, state.rentals[old])
    if short:
        billing(store, lambda led: led.receive_payment(tenant, short, now))
    assert accept(store, tenant, new, now).allowed
    ok(store, act("complete_transfer", "staff:manager", transfer), now)
    lease = rent(store).rentals[new].lease
    charges = [c for c in books(store).tenant_charges(tenant) if c.lease == lease and c.kind == RENT]
    assert [c.cents for c in charges] == [6667]
