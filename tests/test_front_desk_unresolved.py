"""A copy's card payment is never dropped unresolved. Start over, the daily rollover and the cleaning of idle copies all look at the
copy's waiting payments first; each asks the processor (settle what it settled, cancel what still processes) and goes ahead only when
none is left waiting. A processor that cannot answer keeps the copy, and the old day, where they are; a copy with nothing waiting never
costs a processor call."""
import json
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import delinquency as col, renting
from core.books import at_local
from core.stripe_processor import StripeProcessor
from desk import front_desk, stripe_hook
from desk.front_desk import FrontDesk
from tests.fake_stripe import FakeStripe, sign
from tests.test_core_delinquency import NOW, world
from tests.test_core_renting_pay import desk_rental

DAY = date(2026, 12, 3)
STREAM = "visitor:v1"


@pytest.fixture
def now():
    return [at_local(DAY, 10)]


@pytest.fixture
def stripe(now):
    fake = FakeStripe(clock=lambda: now[0].timestamp())
    yield fake
    fake.close()


@pytest.fixture
def desk(stripe, now):
    d = FrontDesk(":memory:", clock=lambda: now[0], processor=StripeProcessor("sk_test_fake", base_url=stripe.url, clock=lambda: now[0].timestamp()))
    d.ready()
    return d


def pay(desk, stripe, stream=STREAM, cents=1000, **hand):
    """A payment sent for the copy; hand sets how the fake answers it (keep_processing, fail_with, lose_reply)."""
    for k, v in hand.items():
        setattr(stripe, k, v)
    i = col.pay(desk.ready(), stream, desk.processor, desk.cast["maria"]["tenant"], cents, "card_ok", desk.clock())
    stripe.keep_processing = False
    return i


def intent(store, i, stream=STREAM):
    return col.state(store.books(stream)).intents[i.id]


def test_start_over_costs_no_processor_call_when_nothing_waits(desk, stripe):
    done = pay(desk, stripe)
    assert done.state == "succeeded"
    stripe.down = True
    calls = len(stripe.calls)
    assert desk.reset("v1") is True
    assert not desk.store.stream_events(STREAM) and len(stripe.calls) == calls


def test_start_over_is_refused_while_a_payment_cannot_be_settled_and_goes_ahead_once_the_processor_answers(desk, stripe):
    i = pay(desk, stripe, keep_processing=True)
    assert i.state == "pending"
    events = len(desk.store.stream_events(STREAM))
    stripe.down = True
    assert desk.reset("v1") is False
    assert len(desk.store.stream_events(STREAM)) >= events and intent(desk.store, i).state == "pending"
    note = desk.take_reset_note("v1")
    assert "not been settled by the card processor" in note and "nothing was thrown away" in note and "stripe" not in note.lower()
    assert desk.take_reset_note("v1") == ""                                        # said once
    assert len(stripe.intents) == 1 and stripe.intents[i.outside]["status"] == "processing"
    stripe.down = False
    assert desk.reset("v1") is True
    assert stripe.intents[i.outside]["status"] == "canceled" and not desk.store.stream_events(STREAM)
    assert desk.take_reset_note("v1") == ""


def test_an_unanswered_request_blocks_start_over_for_fifteen_minutes_and_then_is_closed(desk, stripe, now):
    i = pay(desk, stripe, fail_with=500)
    assert i.state == "open" and not stripe.intents                                   # the request never reached the processor
    assert desk.reset("v1") is False and intent(desk.store, i).state == "open"        # too soon to say it never will
    now[0] += timedelta(minutes=16)
    assert desk.reset("v1") is True and not desk.store.stream_events(STREAM)


def test_a_payment_whose_answer_was_lost_is_settled_by_start_over_not_dropped_open(desk, stripe):
    i = pay(desk, stripe, lose_reply=True)
    assert i.state == "open" and len(stripe.intents) == 1
    store = desk.store
    assert desk.reset("v1") is True
    assert not store.stream_events(STREAM) and stripe.intents[next(iter(stripe.intents))]["status"] == "succeeded"


def test_cleaning_idle_copies_keeps_a_copy_with_a_waiting_payment(desk, stripe, monkeypatch):
    i = pay(desk, stripe, keep_processing=True)
    pay(desk, stripe, "visitor:v2")
    monkeypatch.setattr(front_desk, "KEEP_FOR", timedelta(days=-1))                   # every copy counts as idle
    desk.clean()
    assert desk.store.stream_events(STREAM) and intent(desk.store, i).state == "pending"
    assert not desk.store.stream_events("visitor:v2")


def test_the_new_day_waits_for_the_old_days_payments_to_settle_and_settles_them_first(desk, stripe, now):
    paid = pay(desk, stripe, keep_processing=True)
    stripe.settle(paid.outside, "succeeded")
    old = desk.store
    now[0] = at_local(DAY + timedelta(days=1), 9)
    assert desk.ready() is not old and desk.day == DAY + timedelta(days=1)
    assert intent(old, paid).state == "succeeded" and intent(old, paid).payment             # recorded before the old day was left


def test_the_new_day_cancels_an_old_payment_that_is_still_processing(desk, stripe, now):
    i = pay(desk, stripe, keep_processing=True)
    old = desk.store
    now[0] = at_local(DAY + timedelta(days=1), 9)
    assert desk.ready() is not old
    assert stripe.intents[i.outside]["status"] == "canceled" and intent(old, i).state == "closed"


def test_the_old_day_stays_while_the_processor_cannot_answer_and_the_new_one_starts_when_it_can(desk, stripe, now):
    i = pay(desk, stripe, keep_processing=True)
    old = desk.store
    stripe.down = True
    now[0] = at_local(DAY + timedelta(days=1), 9)
    assert desk.ready() is old and desk.day == DAY
    calls = len(stripe.calls)
    assert desk.ready() is old and len(stripe.calls) == calls                          # inside the throttle window: no second call
    assert intent(old, i).state == "pending" and desk.take_reset_note("v1") == ""
    stripe.down = False
    desk._hold_until = 0                                                              # the window has passed
    assert desk.ready() is not old and desk.day == DAY + timedelta(days=1)
    assert intent(old, i).state == "closed" and stripe.intents[i.outside]["status"] == "canceled"


def test_a_day_with_nothing_waiting_rolls_over_without_asking_the_processor(desk, stripe, now):
    pay(desk, stripe)
    old = desk.store
    stripe.down = True
    calls = len(stripe.calls)
    now[0] = at_local(DAY + timedelta(days=1), 9)
    assert desk.ready() is not old and len(stripe.calls) == calls


def test_two_reconciliations_in_one_instant_both_record_the_second_settling_what_the_first_found_waiting():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    i = col.pay(store, "visitor:r", sim, t, 1000, "card_pending", NOW)
    col.reconcile(store, "visitor:r", sim, NOW)
    sim.settle(i.outside, "succeeded", NOW)
    col.reconcile(store, "visitor:r", sim, NOW)                       # the same instant: one key per run, never one per instant
    st = col.state(store.books("visitor:r"))
    assert len(st.reconciled) == 2 and st.intents[i.id].state == "succeeded"


def test_any_fault_while_settling_holds_the_day_and_refuses_start_over_and_never_reaches_a_page(desk, stripe, now, monkeypatch):
    i = pay(desk, stripe, keep_processing=True)
    old = desk.store

    def broken(*args, **kw):
        raise RuntimeError("a fault that is not the processor's")
    monkeypatch.setattr(col, "reconcile", broken)
    now[0] = at_local(DAY + timedelta(days=1), 9)
    assert desk.ready() is old and desk.day == DAY                                        # held, and nothing raised on the way to a page
    assert desk.reset("v1") is False and intent(old, i).state == "pending"


def test_a_payment_that_turns_up_after_its_intent_was_closed_and_is_then_cancelled_records_both_moves(desk, stripe, now):
    i = pay(desk, stripe, fail_with=500)
    now[0] += timedelta(minutes=16)
    desk.sweep()
    assert intent(desk.store, i).state == "closed"                                         # the processor had no payment for it
    nonce = col.state(desk.store.books(STREAM)).nonce
    stripe.keep_processing = True
    desk.processor.create(i.key, i.cents, "card_ok", {"copy": STREAM, "intent": i.id, "nonce": nonce}, now[0])    # the request got there after all
    stripe.keep_processing = False
    desk.sweep()
    assert intent(desk.store, i).state == "pending"
    assert desk.reset("v1") is True                                                       # cancelled: closed a second time, under its own key
    assert next(iter(stripe.intents.values()))["status"] == "canceled"


def test_a_webhook_for_a_held_day_is_applied_to_that_day(desk, stripe, now):
    i = pay(desk, stripe, keep_processing=True)
    old = desk.store
    stripe.down = True
    now[0] = at_local(DAY + timedelta(days=1), 9)
    assert desk.ready() is old
    stripe.down = False
    stripe.settle(i.outside, "succeeded")
    app = FastAPI()
    stripe_hook.mount(app, desk, "whsec_t")
    body = json.dumps({"id": "evt_h", "type": "payment_intent.succeeded", "data": {"object": dict(stripe.intents[i.outside], object="payment_intent")}}).encode()
    r = TestClient(app).post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign("whsec_t", body, int(now[0].timestamp()))})
    assert r.json() == {"result": "applied"} and intent(old, i).state == "succeeded"


def test_the_new_day_finds_a_payment_whose_answer_was_lost_and_records_it_on_the_old_day(desk, stripe, now):
    i = pay(desk, stripe, lose_reply=True)
    old = desk.store
    assert i.state == "open"
    now[0] = at_local(DAY + timedelta(days=1), 9)
    assert desk.ready() is not old
    assert intent(old, i).state == "succeeded" and intent(old, i).payment


def test_the_new_day_cancels_a_payment_older_than_the_listing_window(desk, stripe, now):
    i = pay(desk, stripe, keep_processing=True)
    old = desk.store
    now[0] = at_local(DAY + timedelta(days=2), 9)                                     # more than 36 hours later
    assert desk.ready() is not old
    assert stripe.intents[i.outside]["status"] == "canceled" and intent(old, i).state == "closed"


@pytest.mark.parametrize("how", ["down", "refused"])
def test_one_attempt_to_leave_the_day_makes_one_failed_call_however_many_copies_wait(desk, stripe, now, how):
    pay(desk, stripe, "visitor:v1", keep_processing=True)
    pay(desk, stripe, "visitor:v2", keep_processing=True)
    pay(desk, stripe, "visitor:v3", keep_processing=True)
    if how == "down":
        stripe.down = True
    else:
        desk.processor = StripeProcessor("sk_test_wrong", base_url=stripe.url, clock=lambda: now[0].timestamp())      # every call is refused
    calls = len(stripe.calls)
    now[0] = at_local(DAY + timedelta(days=1), 9)
    old = desk.store
    assert desk.ready() is old
    assert len(stripe.calls) == calls + 1                                             # one wait under the lock, not one per copy


def test_a_payment_closed_as_absent_that_later_shows_up_succeeded_is_recorded_by_the_next_pass(desk, stripe, now):
    i = pay(desk, stripe, fail_with=500)
    now[0] += timedelta(minutes=16)
    desk.sweep()
    assert intent(desk.store, i).state == "closed"
    nonce = col.state(desk.store.books(STREAM)).nonce
    desk.processor.create(i.key, i.cents, "card_ok", {"copy": STREAM, "intent": i.id, "nonce": nonce}, now[0])      # it had gone through after all
    desk.sweep()
    late = intent(desk.store, i)
    assert late.state == "succeeded" and late.payment and desk.store.books(STREAM).payments[late.payment].cents == i.cents


def test_a_payment_closed_as_absent_that_later_shows_up_succeeded_is_recorded_by_its_webhook(desk, stripe, now):
    i = pay(desk, stripe, fail_with=500)
    now[0] += timedelta(minutes=16)
    desk.sweep()
    nonce = col.state(desk.store.books(STREAM)).nonce
    made = desk.processor.create(i.key, i.cents, "card_ok", {"copy": STREAM, "intent": i.id, "nonce": nonce}, now[0])
    app = FastAPI()
    stripe_hook.mount(app, desk, "whsec_t")
    body = json.dumps({"id": "evt_late", "type": "payment_intent.succeeded", "data": {"object": dict(stripe.intents[made["id"]], object="payment_intent")}}).encode()
    r = TestClient(app).post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign("whsec_t", body, int(now[0].timestamp()))})
    assert r.json() == {"result": "applied"} and intent(desk.store, i).state == "succeeded"


@pytest.mark.parametrize("how", ["down", "refused"])
def test_one_attempt_to_leave_the_day_makes_one_failed_call_however_many_rentals_wait(desk, stripe, now, how):
    """A rental's card payment is an intent like any other, so the same guard holds it: one wait under the lock per attempt."""
    stripe.keep_processing = True
    for v in ("v1", "v2", "v3"):
        desk_rental("card_ok", True, desk=desk, stream=f"visitor:{v}")
    stripe.keep_processing = False
    if how == "down":
        stripe.down = True
    else:
        desk.processor = StripeProcessor("sk_test_wrong", base_url=stripe.url, clock=lambda: now[0].timestamp())      # every call is refused
    calls = len(stripe.calls)
    now[0] = at_local(DAY + timedelta(days=1), 9)
    old = desk.store
    assert desk.ready() is old
    assert len(stripe.calls) == calls + 1
    assert [x.state for v in ("v1", "v2", "v3") for x in renting.load(old, f"visitor:{v}")[1].rentals.values() if not x.provenance] == ["awaiting_payment"] * 3


def test_a_checkout_payment_the_processor_refuses_outright_costs_one_call_and_leaves_the_rental_waiting_for_another_try(desk, stripe, now):
    good = desk.processor
    desk.processor = StripeProcessor("sk_test_wrong", base_url=stripe.url, clock=lambda: now[0].timestamp())
    calls = len(stripe.calls)
    _, store, stream, _, _, rn, refused = desk_rental("card_ok", True, desk=desk)
    assert len(stripe.calls) == calls + 1 and refused.state == "closed"
    r = renting.load(store, stream)[1]
    assert r.rentals[rn].state == "awaiting_payment" and not r.uncertain(r.rentals[rn]) and not r.rentals[rn].payments
    assert r.rentals[rn].lease is None and not r.rentals[rn].access
    desk.processor = good
    again = desk.pay_rental("v1", rn, "card_ok")
    assert again.state == "succeeded" and again.key != refused.key and renting.load(store, stream)[1].rentals[rn].state == "active"
