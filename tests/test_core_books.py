"""The books: append-only storage, balanced postings, a journal that rebuilds from events, and the billing contract."""
import sqlite3
from datetime import date

import pytest

from core.billing import Ledger
from core.books import ADMIN_FEE, LATE_FEE, RENT, at_local, fold, lines
from core.store import BASE, Event, Store, Unbalanced


def facility(schedule=(ADMIN_FEE, LATE_FEE)):
    """A facility with one tenant who moved in on Sep 1, 2026 at $100 a month."""
    led = Ledger()
    led.set_up(at_local(date(2026, 8, 1)), "Northstar Storage", "Orlando, FL", ["A101", "A102", "B201"])
    t = led.add_tenant(at_local(date(2026, 9, 1), 9), "Dana Reyes", "+14075550101")
    lease = led.sign_lease(at_local(date(2026, 9, 1), 10), t, "A101", 10000, list(schedule))
    return led, t, lease


def saved(led, stream=BASE):
    store = Store()
    with store.write():
        store.append(stream, led.pending)
    return store


def charge_of(led, obligation):
    return led.books.live(obligation)[0]


def test_every_event_balances_and_the_journal_rebuilds_from_events():
    led, t, lease = facility()
    led.post_rent("2026-10")
    led.receive_payment(t, 13000, at_local(date(2026, 10, 2), 12))
    led.assess_late_fees("2026-10")
    store = saved(led)
    stored = store.journal()
    rebuilt = [(e.id, a, c, ch) for e in store.events() for a, c, ch in lines(e.kind, e.payload)]
    assert stored == rebuilt
    for e in store.events():
        assert sum(c for _, c, _ in lines(e.kind, e.payload)) == 0


def test_the_database_refuses_edits_and_base_deletes():
    led, _, _ = facility()
    store = saved(led)
    for sql in ("UPDATE events SET kind = 'x'", "UPDATE journal_lines SET cents = 0", "DELETE FROM events WHERE stream = 'base'",
                "DELETE FROM journal_lines WHERE stream = 'base'"):
        with pytest.raises(sqlite3.DatabaseError):
            store.db.execute(sql)
    with pytest.raises(ValueError):
        store.drop_stream(BASE)


def test_an_unbalanced_event_writes_nothing():
    store = Store()
    # a payment that claims to pay more than it brought in
    bad = Event(kind="payment.received", at=at_local(date(2026, 9, 1)), key="payment:P9",
                payload={"payment": "P9", "tenant": "T1", "cents": 100, "applications": [{"charge": "C1", "lease": "L1", "cents": 150}]})
    with pytest.raises(Unbalanced):
        with store.write():
            store.append(BASE, [bad])
    assert store.events() == []


def test_business_effects_are_once_per_stream_and_streams_do_not_block_each_other():
    led, t, lease = facility()
    store = saved(led)
    c = led.books.live(f"admin_fee:{lease}")[0].id
    for stream in ("visitor:a", "visitor:b"):
        books = fold(store.events(stream))
        v = Ledger(books)
        v.reverse(at_local(date(2026, 9, 2)), c, "duplicate", "operator")
        with store.write():
            store.append(stream, v.pending)
    again = Ledger(fold(store.events("visitor:a")))
    again.pending.append(Event(kind="note", at=at_local(date(2026, 9, 3)), payload={}, key="other", effects=(f"reverse:{c}",)))
    with pytest.raises(sqlite3.IntegrityError):
        with store.write():
            store.append("visitor:a", again.pending)
    store.drop_stream("visitor:a")
    assert [e.stream for e in store.events("visitor:a")] == [BASE] * len(led.pending)
    assert any(e.stream == "visitor:b" for e in store.events("visitor:b"))


def test_rent_posts_on_the_first_and_the_late_fee_at_midnight_on_the_sixth():
    led, t, lease = facility()
    led.post_rent("2026-10")
    rent = charge_of(led, f"rent:{lease}:2026-10")
    assert rent.due == date(2026, 10, 1) and rent.posted_at == at_local(date(2026, 10, 1))
    # everything owed paid one minute before midnight on the 5th, local time: on time (older charges are paid first)
    led.receive_payment(t, led.books.receivable(lease), at_local(date(2026, 10, 5), 23, 58))
    led.assess_late_fees("2026-10")
    assert not led.books.live(f"late_fee:{lease}:2026-10")


def test_a_partial_payment_before_the_cutoff_still_gets_the_fee():
    led, t, lease = facility()
    led.receive_payment(t, 10000 * 30 // 30 + 2500, at_local(date(2026, 9, 1), 11))      # September and the admin fee
    led.post_rent("2026-10")
    led.receive_payment(t, 6000, at_local(date(2026, 10, 3), 9))
    led.assess_late_fees("2026-10")
    fee = charge_of(led, f"late_fee:{lease}:2026-10")
    assert fee.cents == 2000 and fee.posted_at == at_local(date(2026, 10, 6))


def test_a_payment_learned_late_makes_the_fee_correctable_and_the_original_stays():
    led, t, lease = facility()
    led.receive_payment(t, 12500, at_local(date(2026, 9, 1), 11))
    led.post_rent("2026-10")
    eff, known = at_local(date(2026, 10, 5), 15), at_local(date(2026, 10, 7), 9)
    led.assess_late_fees("2026-10")                       # billing does not know of the payment yet
    fee = charge_of(led, f"late_fee:{lease}:2026-10")
    led.receive_payment(t, 10000, eff, known)
    assert led.books.rent_paid_before(lease, "2026-10", at_local(date(2026, 10, 6))) == 10000
    assert led.books.charges[fee.id].closed is None       # the fee stays on the books until a correction is recorded


def test_an_overpayment_becomes_credit_and_receivable_never_goes_below_zero():
    led, t, lease = facility()
    led.receive_payment(t, 12500 + 2000, at_local(date(2026, 9, 1), 11))
    assert led.books.receivable(lease) == 0 and led.books.credit[t] == 2000
    led.post_rent("2026-10")
    assert led.books.credit[t] == 0 and charge_of(led, f"rent:{lease}:2026-10").open_cents == 8000
    store = saved(led)
    total = {}
    for _, account, cents, _ in store.journal():
        total[account] = total.get(account, 0) + cents
    assert total[f"receivable:{lease}"] == led.books.receivable(lease) == 8000
    assert total.get(f"credit:{t}", 0) == 0


def test_one_payment_over_two_leases_splits_per_lease():
    led, t, lease = facility()
    second = led.sign_lease(at_local(date(2026, 9, 1), 11), t, "A102", 5000, [LATE_FEE])
    p = led.receive_payment(t, 12500 + 5000, at_local(date(2026, 9, 1), 12))
    assert {a["lease"] for a in led.books.payments[p].applications} == {lease, second}
    assert led.books.receivable(lease) == 0 and led.books.receivable(second) == 0


def test_applications_never_move_after_a_later_dispute():
    led, t, lease = facility()
    led.post_rent("2026-10")
    p = led.receive_payment(t, 5000, at_local(date(2026, 10, 2), 9))
    before = [dict(a) for a in led.books.payments[p].applications]
    admin = charge_of(led, f"admin_fee:{lease}")
    led.open_dispute(at_local(date(2026, 10, 3), 9), admin.id, "I never agreed to this", hold=True, source="operator")
    assert led.books.payments[p].applications == before


def test_a_paid_fee_waived_becomes_credit_and_pays_the_next_charge():
    led, t, lease = facility()
    led.receive_payment(t, 12500, at_local(date(2026, 9, 1), 11))
    admin = charge_of(led, f"admin_fee:{lease}")
    assert admin.open_cents == 0
    led.waive(at_local(date(2026, 9, 20), 10), admin.id, "goodwill", "staff:office_manager")
    assert led.books.credit[t] == 2500
    led.post_rent("2026-10")
    assert led.books.credit[t] == 0 and charge_of(led, f"rent:{lease}:2026-10").open_cents == 7500


def test_only_a_waived_late_fee_counts_toward_the_yearly_waiver():
    led, t, lease = facility()
    admin = charge_of(led, f"admin_fee:{lease}")
    led.waive(at_local(date(2026, 9, 20), 10), admin.id, "goodwill", "staff:office_manager")
    assert led.books.waived_within(t, at_local(date(2026, 9, 21))) == 0      # staff forgiving another fee leaves the late-fee waiver unused
    led.post_rent("2026-10")
    led.assess_late_fees("2026-10")
    late = charge_of(led, f"late_fee:{lease}:2026-10")
    led.waive(at_local(date(2026, 10, 8), 10), late.id, "owner rule", "agent")
    assert led.books.waived_within(t, at_local(date(2026, 10, 9))) == 1


def test_a_return_after_its_credit_paid_another_lease_reopens_that_charge():
    led, t, lease = facility()
    led.receive_payment(t, 12500, at_local(date(2026, 9, 1), 11))
    p = led.receive_payment(t, 4000, at_local(date(2026, 9, 2), 11))      # all of it credit
    second = led.sign_lease(at_local(date(2026, 9, 10), 10), t, "A102", 3000, [LATE_FEE])
    first_rent = led.books.live(f"rent:{second}:2026-09")[0]
    assert first_rent.open_cents == 0                                       # the credit paid it
    led.return_payment(at_local(date(2026, 9, 12), 10), p)
    assert first_rent.open_cents == first_rent.cents
    assert led.books.credit[t] == 0
    saved(led)                                                              # and every line still balances


def test_a_return_after_the_cutoff_posts_that_months_late_fee():
    led, t, lease = facility()
    led.receive_payment(t, 12500, at_local(date(2026, 9, 1), 11))
    led.post_rent("2026-10")
    p = led.receive_payment(t, 10000, at_local(date(2026, 10, 2), 11))
    led.assess_late_fees("2026-10")
    assert not led.books.live(f"late_fee:{lease}:2026-10")
    led.return_payment(at_local(date(2026, 10, 9), 10), p)
    fee = charge_of(led, f"late_fee:{lease}:2026-10")
    assert fee.posted_at == at_local(date(2026, 10, 9), 10)


def test_receivable_is_the_unpaid_parts_and_credit_is_created_minus_used():
    led, t, lease = facility()
    led.post_rent("2026-10")
    led.receive_payment(t, 30000, at_local(date(2026, 10, 2), 11))
    led.post_rent("2026-11")
    store = saved(led)
    books = fold(store.events())
    assert books.receivable(lease) == sum(c.open_cents for c in books.charges.values() if c.lease == lease)
    total = sum(c for _, a, c, _ in store.journal() if a == f"credit:{t}")
    assert -total == books.credit.get(t, 0)


def test_a_returned_move_in_payment_does_not_bring_a_late_fee():
    led = Ledger()
    led.set_up(at_local(date(2026, 8, 1)), "Northstar Storage", "Orlando, FL", ["A101"])
    t = led.add_tenant(at_local(date(2026, 10, 20), 9), "Dana Reyes", "+14075550101")
    lease = led.sign_lease(at_local(date(2026, 10, 20), 10), t, "A101", 10000, [LATE_FEE])
    p = led.receive_payment(t, led.books.receivable(lease), at_local(date(2026, 10, 20), 11))
    led.return_payment(at_local(date(2026, 10, 21), 9), p)
    assert not led.books.live(f"late_fee:{lease}:2026-10")


def test_rent_under_an_active_hold_never_brings_a_late_fee():
    led, t, lease = facility()
    led.receive_payment(t, 12500, at_local(date(2026, 9, 1), 11))
    led.post_rent("2026-10")
    rent = charge_of(led, f"rent:{lease}:2026-10")
    led.open_dispute(at_local(date(2026, 10, 2), 9), rent.id, "staff hold for review", hold=True, source="staff:office_manager")
    led.assess_late_fees("2026-10")
    assert not led.books.live(f"late_fee:{lease}:2026-10")


def test_a_payment_returned_after_its_fee_was_waived_is_still_recorded():
    led, t, lease = facility()
    led.receive_payment(t, 12500, at_local(date(2026, 9, 1), 11))
    led.post_rent("2026-10")
    led.assess_late_fees("2026-10")
    p = led.receive_payment(t, 10000, at_local(date(2026, 10, 8), 11))      # pays the rent; the fee stays open
    led.waive(at_local(date(2026, 10, 9), 10), charge_of(led, f"late_fee:{lease}:2026-10").id, "goodwill", "staff:office_manager")
    led.return_payment(at_local(date(2026, 10, 12), 10), p)
    assert led.books.payments[p].returned
    assert len(led.books.obligations[f"late_fee:{lease}:2026-10"]) == 1    # no second fee for the same month
    saved(led)
