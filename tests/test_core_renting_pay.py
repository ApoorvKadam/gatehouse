"""A renter's move-in payment goes through the payment processor like any other payment: the rental's payment operation and the
processor intent are written together, the request goes out under the intent's key, and the processor's answer (at once, by
webhook, or by the daily check) drives the rental's own commands, once however the answer arrives."""
from datetime import date, timedelta

import pytest

from core import delinquency
from core.billing import Ledger, Refused
from core.books import at_local
from core.delinquency import SimulatedProcessor
from core.renting import Act, StandIn, advance, checkout_intent, load, pay_rental, run, settle_payments
from core.store import BASE
from desk.front_desk import FrontDesk

from .test_core_renting import NOW, accept, act, billing, books, checkout, facility, ok, pay, rent


def ready(store, tenant, unit="A101"):
    """A rental in checkout whose agreement is accepted, so only the payment is missing."""
    rn = checkout(store, tenant, unit)
    assert accept(store, tenant, rn).allowed
    return rn


def deliver(store, processor, now=NOW):
    """What the desk's pass does: hand over the processor's events, then carry each rental forward from its payment."""
    for event in processor.take_events():
        delinquency.webhook(store, processor, event, now)
    settle_payments(store, BASE, now)


def test_a_card_payment_starts_the_rental_under_the_intents_key():
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    intent = pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    r = rent(store)
    op = r.ops[r.rentals[rn].pay_ops[0]]
    assert intent.state == "succeeded" and r.rentals[rn].state == "active" and r.rentals[rn].payments == [intent.payment]
    assert op.intent == intent.id and op.how == "response" and r.rentals[rn].access
    (paid,) = processor.payments.values()
    assert paid["key"] == intent.key and paid["metadata"]["copy"] == BASE and paid["cents"] == r.rentals[rn].quote["total_cents"]
    with pytest.raises(Refused, match="the rental is active"):
        pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    assert len(processor.payments) == 1


def test_a_declined_card_is_recorded_once_and_the_renter_can_pay_again():
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    first = pay_rental(store, BASE, processor, rn, "card_declined", NOW)
    r = rent(store)
    assert first.state == "failed" and r.rentals[rn].state == "awaiting_payment" and not r.uncertain(r.rentals[rn])
    assert [d["tenant"] for d in books(store).declined] == [dana]          # the processor's decline, not recorded a second time
    second = pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    assert second.state == "succeeded" and rent(store).rentals[rn].state == "active"
    assert first.key != second.key and len(processor.payments) == 2 and len(books(store).declined) == 1


def test_a_lost_answer_keeps_the_unit_and_a_retry_sends_the_same_key():
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    processor.lose_next_reply = True
    lost = pay_rental(store, BASE, processor, rn, "card_ok", NOW)       # charged; the answer never came
    later = NOW + timedelta(minutes=45)
    assert lost.state == "open" and rent(store).uncertain(rent(store).rentals[rn]) and rent(store).status("A101", later) == "reserved"
    again = pay_rental(store, BASE, processor, rn, "card_ok", later)
    assert again.id == lost.id and again.state == "succeeded" and len(processor.payments) == 1
    assert rent(store).rentals[rn].state == "active" and len(rent(store).rentals[rn].pay_ops) == 1


def test_a_payment_still_processing_settles_by_webhook_once():
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    pending = pay_rental(store, BASE, processor, rn, "card_pending", NOW)
    assert pending.state == "pending" and rent(store).rentals[rn].state == "awaiting_payment"
    processor.settle(pending.outside, "succeeded", NOW)
    deliver(store, processor)
    assert rent(store).rentals[rn].state == "active" and rent(store).rentals[rn].payments == [delinquency.state(books(store)).intents[pending.id].payment]
    count = len(store.events())
    deliver(store, processor)
    assert len(store.events()) == count


def test_a_request_that_never_reached_the_processor_frees_the_unit_after_the_daily_check(monkeypatch):
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    real = delinquency.send

    def crash(*args, **kwargs):
        raise RuntimeError("the process stopped after the intent was written")
    monkeypatch.setattr(delinquency, "send", crash)
    with pytest.raises(RuntimeError):
        pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    monkeypatch.setattr(delinquency, "send", real)
    r = rent(store)
    assert r.uncertain(r.rentals[rn]) and not processor.payments
    later = NOW + timedelta(hours=1)
    delinquency.reconcile(store, BASE, processor, later)              # the processor has nothing for the key: the intent is closed
    settle_payments(store, BASE, later)
    r = rent(store)
    assert not r.uncertain(r.rentals[rn]) and r.rentals[rn].state == "awaiting_payment" and not r.rentals[rn].payments
    with pytest.raises(Refused, match="hold on the unit ran out"):      # twenty minutes are long gone: a new payment does not start on it
        pay_rental(store, BASE, processor, rn, "card_ok", later)
    advance(store, BASE, StandIn(), later)
    assert rent(store).rentals[rn].state == "cancelled" and rent(store).status("A101", later) == "vacant" and not processor.payments


def test_a_payment_is_refused_while_another_of_the_tenants_is_unanswered_or_the_rental_is_not_waiting():
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    delinquency.pay(store, BASE, processor, dana, 1000, "card_pending", NOW)
    with pytest.raises(Refused, match="never paid again"):
        pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    assert rent(store).rentals[rn].pay_ops == []                           # nothing was written, so no payment is left unanswered
    ok(store, act("cancel_reservation", "staff:manager", rent(store).rentals[rn].reservation))
    with pytest.raises(Refused, match="the rental is cancelled"):
        pay_rental(store, BASE, processor, rn, "card_ok", NOW)


def test_a_paid_rental_is_not_charged_again_while_it_waits_for_the_agreement():
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = checkout(store, dana, "A101")                          # not accepted yet
    pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    assert rent(store).rentals[rn].state == "awaiting_signature"
    with pytest.raises(Refused, match="already paid for"):
        pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    assert len(processor.payments) == 1 and len(rent(store).rentals[rn].pay_ops) == 1


def test_a_payment_that_came_up_short_is_followed_by_one_for_what_the_activation_task_names():
    from datetime import date as d
    from core.books import ADMIN_FEE
    store, _, eli = facility()
    billing(store, lambda led: led.sign_lease(at_local(d(2026, 11, 1), 9), eli, "A102", 9000, [ADMIN_FEE]))     # $115.00 open
    processor = SimulatedProcessor()
    rn = ready(store, eli, "B201")                                        # $130.00 to move in
    first = pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    task = next(t for t in rent(store).tasks.values() if t.about == rn and t.kind == "activation")
    assert first.cents == 13000 and "$115.00 short" in task.reason         # the older charges took $115.00 of it
    second = pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    assert second.cents == 11500                                          # exactly the shortfall, not the quote again
    assert rent(store).rentals[rn].state == "active" and not rent(store).tasks[task.id].open


def test_a_top_up_after_a_part_payment_charges_only_the_rest_and_a_second_press_charges_nothing():
    """A move-in of $120.00 with $80.00 already paid: the card is charged the $40.00 still missing, the rental starts, and pressing
    again charges nothing more."""
    first_day = at_local(date(2026, 12, 1), 10)                                # a whole month's rent is due, so the quote is exact
    store, dana, _ = facility()
    ok(store, act("price_unit", "staff:manager", "A101", rent_cents=9500, size="5x10"), first_day)
    rn = checkout(store, dana, "A101", first_day)
    assert rent(store).rentals[rn].quote["total_cents"] == 12000               # $95.00 rent and the $25.00 admin fee
    pay(store, dana, rn, 8000, first_day)
    accept(store, dana, rn, first_day)
    assert rent(store).rentals[rn].state == "awaiting_payment"
    processor = SimulatedProcessor()
    topped = pay_rental(store, BASE, processor, rn, "card_ok", first_day)
    assert topped.cents == 4000 and rent(store).rentals[rn].state == "active"
    with pytest.raises(Refused, match="the rental is active"):
        pay_rental(store, BASE, processor, rn, "card_ok", first_day)
    assert [p["cents"] for p in processor.payments.values()] == [4000]


def test_a_larger_older_balance_takes_more_than_one_top_up_and_each_cent_is_paid_once():
    """Older charges on the account take the money first, so the move-in can still be short after a paid checkout. The next payment
    asks for exactly what is still missing, however many rounds it takes, and the tenant ends up having paid each cent once."""
    from datetime import date as d
    from core.books import ADMIN_FEE
    store, _, eli = facility()
    billing(store, lambda led: led.sign_lease(at_local(d(2026, 11, 1), 9), eli, "A102", 20000, [ADMIN_FEE]))    # $225.00 open
    processor = SimulatedProcessor()
    rn = ready(store, eli, "B201")                                        # $130.00 due to move in
    cents = []
    while rent(store).rentals[rn].state != "active":
        cents.append(pay_rental(store, BASE, processor, rn, "card_ok", NOW).cents)
        assert len(cents) < 5
    assert cents == [13000, 13000, 9500]                                  # the full quote once, then only what was short
    assert sum(cents) == 22500 + 13000 and len(processor.payments) == 3


DAY = date(2026, 12, 3)


def clock():
    return at_local(DAY, 10)


def desk_rental(card, accepted, desk=None, stream="visitor:v1", through_desk=True):
    """The desk's own copy with a visitor renting a vacant unit: the rental after one card payment. Through the desk's own steps a
    rental that starts gets its gate code at once; with through_desk=False it starts by the core's commands alone, code not issued."""
    desk = desk or FrontDesk(":memory:", clock=clock)
    store, visitor = desk.ready(), stream.removeprefix("visitor:")
    b, r = load(store, stream)
    unit = next(u for u in b.units if r.status(u, clock()) == "vacant")
    assert run(store, stream, Act("p", "price_unit", "staff:manager", unit, args={"rent_cents": 10000, "size": "5x10"}), clock()).allowed
    with store.write():
        led = Ledger(store.books(stream))
        tenant = led.add_tenant(clock(), "Rae Quinn", "+14075550199")
        store.append(stream, led.pending)
    rs = run(store, stream, Act("r", "reserve", "renter", unit, tenant), clock()).made
    rn = run(store, stream, Act("c", "start_checkout", "renter", rs, tenant), clock()).made
    if accepted:
        sha = load(store, stream)[1].agreements[load(store, stream)[1].rentals[rn].agreement].sha256
        accept = Act("a", "accept_agreement", "renter", rn, tenant, {"sha256": sha})
        assert (desk.rent_step(visitor, accept) if through_desk else run(store, stream, accept, clock())).allowed
    pay = desk.pay_rental(visitor, rn, card) if through_desk else pay_rental(store, stream, desk.processor, rn, card, clock())
    return desk, store, stream, tenant, rs, rn, pay


def test_the_desks_pass_carries_a_rental_forward_when_its_payment_settles_later():
    desk, store, stream, _, _, rn, intent = desk_rental("card_pending", accepted=True)
    assert load(store, stream)[1].rentals[rn].state == "awaiting_payment"
    desk.processor.settle(intent.outside, "succeeded", clock())
    assert desk.sweep() == [stream]
    assert load(store, stream)[1].rentals[rn].state == "active"
    count = len(store.stream_events(stream))
    desk.sweep()
    assert len(store.stream_events(stream)) == count


def test_a_returned_payment_whose_credit_is_set_aside_becomes_a_task_never_an_error():
    desk, store, stream, tenant, rs, rn, intent = desk_rental("card_ok", accepted=False)
    assert load(store, stream)[1].rentals[rn].state == "awaiting_signature"           # paid, agreement not yet accepted
    assert run(store, stream, Act("x", "cancel_reservation", "staff:manager", rs), clock()).allowed
    assert load(store, stream)[1].rentals[rn].refund["state"] == "pending"            # the money is set aside to refund
    desk.processor.settle(intent.outside, "returned", clock())
    assert desk.sweep() == [stream]                                                   # no error stops the pass
    tasks = [t for t in delinquency.open_tasks(store.books(stream)) if t.tenant == tenant]
    assert [(t.kind, t.due) for t in tasks] == [("returned payment needs staff", DAY)]
    assert delinquency.state(store.books(stream)).intents[intent.id].state == "succeeded"
    count = len(store.stream_events(stream))
    desk.sweep()
    assert len(store.stream_events(stream)) == count                                  # the same difference writes nothing new


# a payment closed as absent that turns up after the hold ran out -------------------------------------------------------------
def lost_checkout(monkeypatch, answered=True):
    """A checkout whose request never reached the processor. The daily check, a quarter of an hour on, finds nothing for its key and
    closes the intent as absent; the renter's wait is answered as declined and the hold has not run out yet. With answered=False the
    rental step never ran on that pass, so the wait is still open."""
    store, dana, eli = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    real = delinquency.send

    def stopped(*args, **kwargs):
        raise RuntimeError("the process stopped after the intent was written")
    monkeypatch.setattr(delinquency, "send", stopped)
    with pytest.raises(RuntimeError):
        pay_rental(store, BASE, processor, rn, "card_ok", NOW)
    monkeypatch.setattr(delinquency, "send", real)
    closed = NOW + timedelta(minutes=16)
    delinquency.reconcile(store, BASE, processor, closed)
    if answered:
        settle_payments(store, BASE, closed)
    r = rent(store)
    intent = delinquency.state(books(store)).intents[r.ops[r.rentals[rn].pay_ops[0]].intent]
    assert intent.state == "closed" and r.uncertain(r.rentals[rn]) is not answered and r.rentals[rn].state == "awaiting_payment"
    return store, dana, eli, processor, rn, intent


def turn_up(store, processor, intent, card, now):
    """The payment the processor had all along, created under the intent's own key and metadata, as a delayed request would."""
    nonce = delinquency.state(books(store)).nonce
    return processor.create(intent.key, intent.cents, card, {"copy": BASE, "intent": intent.id, "nonce": nonce}, now)


def never_started(store, dana, rn, intent, later, total):
    """The renter paid after the hold ran out: no lease, the unit free, and the money set aside for staff to refund, once."""
    r, b = rent(store), books(store)
    assert delinquency.state(b).intents[intent.id].state == "succeeded"
    assert r.rentals[rn].state == "cancelled" and not r.rentals[rn].payments and r.rentals[rn].lease is None and not r.rentals[rn].access
    assert not b.leases and (r.holder("A101", later) or ("free",))[0] != "rental"
    assert b.refunds[dana] == total and b.credit.get(dana, 0) == 0
    assert [(t.kind, t.about) for t in r.tasks.values() if t.open and t.kind == "refund"] == [("refund", rn)]


def test_a_payment_closed_as_absent_that_turns_up_after_the_hold_ran_out_never_starts_a_rental(monkeypatch):
    store, dana, eli, processor, rn, intent = lost_checkout(monkeypatch)
    total = rent(store).rentals[rn].quote["total_cents"]
    taken = NOW + timedelta(minutes=30)
    rs = ok(store, act("reserve", "renter", "A101", eli), taken)                  # the hold ran out, so another renter takes the unit
    late = NOW + timedelta(minutes=45)
    turn_up(store, processor, intent, "card_ok", late)
    delinquency.reconcile(store, BASE, processor, late)                          # the next daily check finds the payment
    settle_payments(store, BASE, late)
    never_started(store, dana, rn, intent, late, total)
    assert rent(store).holder("A101", late) == ("reservation", rs)               # the other renter still holds it
    count = len(store.events())
    delinquency.reconcile(store, BASE, processor, late)
    settle_payments(store, BASE, late)
    assert len(store.events()) == count


def test_a_payment_closed_as_absent_that_turns_up_by_webhook_after_the_hold_ran_out_never_starts_a_rental(monkeypatch):
    store, dana, _, processor, rn, intent = lost_checkout(monkeypatch)
    total = rent(store).rentals[rn].quote["total_cents"]
    late = NOW + timedelta(minutes=45)
    turn_up(store, processor, intent, "card_ok", late)
    deliver(store, processor, late)
    never_started(store, dana, rn, intent, late, total)
    count = len(store.events())
    deliver(store, processor, late)
    assert len(store.events()) == count


def test_a_payment_closed_as_absent_that_turns_up_processing_and_then_succeeds_never_starts_a_rental(monkeypatch):
    store, dana, _, processor, rn, intent = lost_checkout(monkeypatch)
    total = rent(store).rentals[rn].quote["total_cents"]
    late = NOW + timedelta(minutes=45)
    made = turn_up(store, processor, intent, "card_pending", late)
    delinquency.reconcile(store, BASE, processor, late)
    settle_payments(store, BASE, late)
    assert delinquency.state(books(store)).intents[intent.id].state == "pending" and not rent(store).rentals[rn].payments
    done = late + timedelta(minutes=5)
    processor.settle(made["id"], "succeeded", done)
    deliver(store, processor, done)
    never_started(store, dana, rn, intent, done, total)


def test_a_payment_closed_as_absent_that_turns_up_and_is_cancelled_closes_the_intent_twice_and_answers_the_wait_once(monkeypatch):
    store, dana, _, processor, rn, intent = lost_checkout(monkeypatch)
    late = NOW + timedelta(minutes=45)
    made = turn_up(store, processor, intent, "card_pending", late)
    delinquency.reconcile(store, BASE, processor, late)
    processor.cancel(made["id"])
    deliver(store, processor, late)
    b = books(store)
    moves = [(e.payload["state"]) for e in store.events() if e.kind == "intent.updated" and e.payload["intent"] == intent.id]
    assert moves == ["closed", "pending", "closed"] and delinquency.state(b).intents[intent.id].state == "closed"
    r = rent(store)
    assert [r.ops[o].how for o in r.rentals[rn].pay_ops] == ["response"] and not r.rentals[rn].payments and not b.refunds
    count = len(store.events())
    deliver(store, processor, late)
    settle_payments(store, BASE, late)
    assert len(store.events()) == count


def test_a_payment_that_turns_up_after_the_hold_ran_out_is_set_aside_even_when_no_pass_answered_the_close(monkeypatch):
    """The intent was closed as absent but the rental step did not run on that pass: the close is in the records, so the wait is
    answered as declined before the late success is read, and the success never starts a rental."""
    store, dana, _, processor, rn, intent = lost_checkout(monkeypatch, answered=False)
    total = rent(store).rentals[rn].quote["total_cents"]
    late = NOW + timedelta(minutes=45)
    turn_up(store, processor, intent, "card_ok", late)
    delinquency.reconcile(store, BASE, processor, late)
    settle_payments(store, BASE, late)
    never_started(store, dana, rn, intent, late, total)
    r = rent(store)
    assert [r.ops[o].how for o in r.rentals[rn].pay_ops] == ["response"]
    count = len(store.events())
    delinquency.reconcile(store, BASE, processor, late)
    settle_payments(store, BASE, late)
    assert len(store.events()) == count


def test_a_payment_that_turns_up_inside_the_hold_after_an_unanswered_close_starts_the_rental(monkeypatch):
    """Paid in time still counts: the close was never answered and the payment shows up with the hold still running, so the
    rental starts as it would have had the first answer been the payment."""
    store, dana, _, processor, rn, intent = lost_checkout(monkeypatch, answered=False)
    soon = NOW + timedelta(minutes=17)
    turn_up(store, processor, intent, "card_ok", soon)
    delinquency.reconcile(store, BASE, processor, soon)
    settle_payments(store, BASE, soon)
    r = rent(store)
    assert r.rentals[rn].state == "active" and r.rentals[rn].lease and not r.uncertain(r.rentals[rn]) and not books(store).refunds


# review round: a late answer belongs to the operation that asked ---------------------------------------------------------------
def test_a_late_success_of_an_earlier_payment_never_answers_the_newer_one():
    """A card is declined (the first operation is answered declined), a second payment starts and is still processing, and then the
    declined payment goes through after all: it is recorded for the rental, and the second operation stays open for its own answer."""
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    first = pay_rental(store, BASE, processor, rn, "card_declined", NOW)
    second = pay_rental(store, BASE, processor, rn, "card_pending", NOW)
    r = rent(store)
    op1, op2 = r.rentals[rn].pay_ops
    assert first.state == "failed" and second.state == "pending" and r.ops[op1].how == "response" and r.ops[op2].how is None
    declined = dict(r.ops[op1].result)
    processor.settle(first.outside, "succeeded", NOW)
    deliver(store, processor)
    r, b = rent(store), books(store)
    assert r.ops[op1].result == declined and r.ops[op2].how is None and r.uncertain(r.rentals[rn])      # only the first answer, never twice
    paid = delinquency.state(b).intents[first.id].payment
    assert r.rentals[rn].payments == [paid] and r.rentals[rn].state == "active"
    count = len(store.events())
    deliver(store, processor)
    assert len(store.events()) == count


def test_a_new_payment_is_refused_once_the_hold_ran_out_even_before_the_next_pass_wrote_it_down():
    """The hold lasts twenty minutes. Until a pass writes the expiry the rental still reads as waiting, but a payment started after the
    deadline must not open a new intent: the intent would count as an unanswered payment and keep the expired hold alive."""
    store, dana, _ = facility()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    late = NOW + timedelta(minutes=25)
    assert rent(store).rentals[rn].state == "awaiting_payment" and rent(store).holder("A101", late) is None
    with pytest.raises(Refused, match="hold on the unit ran out"):
        pay_rental(store, BASE, processor, rn, "card_ok", late)
    with pytest.raises(Refused, match="hold on the unit ran out"):
        checkout_intent(store, BASE, rn, late)
    assert not processor.payments and not rent(store).rentals[rn].pay_ops and not delinquency.state(books(store)).intents
    advance(store, BASE, StandIn(), late)
    assert rent(store).rentals[rn].state == "cancelled" and rent(store).status("A101", late) == "vacant"


def test_a_payment_paid_for_and_waiting_may_still_be_topped_up_after_its_hold_time():
    """A paid hold stays until the rental starts or staff refund it, so a top-up of what is still short is not refused for the clock."""
    first_day = at_local(date(2026, 12, 1), 10)
    store, dana, _ = facility()
    ok(store, act("price_unit", "staff:manager", "A101", rent_cents=9500, size="5x10"), first_day)
    rn = checkout(store, dana, "A101", first_day)
    pay(store, dana, rn, 8000, first_day)
    accept(store, dana, rn, first_day)
    late = first_day + timedelta(hours=2)
    assert rent(store).holder("A101", late) == ("rental", rn)
    topped = pay_rental(store, BASE, SimulatedProcessor(), rn, "card_ok", late)
    assert topped.cents == 4000 and rent(store).rentals[rn].state == "active"
