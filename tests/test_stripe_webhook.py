"""The card processor's webhook and the hourly pass. A signed event is settled from the processor's own record of the payment, once;
a bad signature, an old one, a tampered body or a missing header changes nothing; with no signing secret the endpoint is closed. With no
webhook at all (a visitor copy cannot register one) the hourly pass reads each waiting payment from the processor and settles it."""
import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import delinquency as col
from core.books import at_local
from core.stripe_processor import StripeProcessor
from desk import stripe_hook
from desk.front_desk import FrontDesk
from tests.fake_stripe import FakeStripe, sign

DAY = date(2026, 12, 3)
SECRET = "whsec_test"


def clock():
    return at_local(DAY, 10)


@pytest.fixture
def stripe():
    fake = FakeStripe(clock=lambda: clock().timestamp())
    yield fake
    fake.close()


@pytest.fixture
def desk(stripe):
    d = FrontDesk(":memory:", clock=clock, processor=StripeProcessor("sk_test_fake", base_url=stripe.url, clock=lambda: clock().timestamp()))
    d.ready()
    return d


def hook(desk, secret=SECRET) -> TestClient:
    app = FastAPI()
    stripe_hook.mount(app, desk, secret)
    return TestClient(app)


def send(client, payload: dict, secret=SECRET, at=None, header=True):
    body = json.dumps(payload).encode()
    headers = {"Stripe-Signature": sign(secret, body, at if at is not None else int(clock().timestamp()))} if header else {}
    return client.post("/webhooks/stripe", content=body, headers=headers)


def event(stripe, pid, kind="payment_intent.succeeded", ident="evt_1"):
    return {"id": ident, "type": kind, "data": {"object": dict(stripe.intents[pid], object="payment_intent")}}


def waiting(desk, stripe, stream="visitor:v1", cents=1000):
    """A payment sent and still processing at the processor."""
    stripe.keep_processing = True
    t = desk.cast["maria"]["tenant"]
    i = col.pay(desk.ready(), stream, desk.processor, t, cents, "card_ok", clock())
    stripe.keep_processing = False
    assert i.state == "pending"
    return i


def intent(desk, i, stream="visitor:v1"):
    return col.state(desk.store.books(stream)).intents[i.id]


def test_a_signed_event_settles_the_payment_from_the_processors_record_and_a_repeat_changes_nothing(desk, stripe):
    i = waiting(desk, stripe)
    stripe.settle(i.outside, "succeeded")
    c = hook(desk)
    r = send(c, event(stripe, i.outside))
    assert (r.status_code, r.json()) == (200, {"result": "applied"})
    assert intent(desk, i).state == "succeeded" and intent(desk, i).payment
    count = len(desk.store.stream_events("visitor:v1"))
    assert send(c, event(stripe, i.outside)).json() == {"result": "duplicate"}
    assert len(desk.store.stream_events("visitor:v1")) == count


def test_an_event_that_cannot_be_trusted_changes_nothing(desk, stripe):
    i = waiting(desk, stripe)
    stripe.settle(i.outside, "succeeded")
    c, good = hook(desk), event(stripe, i.outside)
    assert send(c, good, secret="whsec_other").status_code == 400                                  # signed with another secret
    assert send(c, good, at=int(clock().timestamp()) - 3600).status_code == 400                    # an old one, replayed
    assert send(c, good, header=False).status_code == 400
    body = json.dumps(good).encode()
    tampered = c.post("/webhooks/stripe", content=body.replace(b"evt_1", b"evt_2"), headers={"Stripe-Signature": sign(SECRET, body, int(clock().timestamp()))})
    assert tampered.status_code == 400
    assert c.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=abc,v1=zz"}).status_code == 400
    assert c.post("/webhooks/stripe", content=b"x" * 300_000, headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 413
    assert intent(desk, i).state == "pending"
    assert hook(desk, secret=None).post("/webhooks/stripe", content=body).status_code == 503       # no secret set: closed


def test_an_event_not_about_one_of_our_copies_is_acknowledged_and_ignored(desk, stripe):
    c = hook(desk)
    stripe.intents["pi_x"] = {"id": "pi_x", "amount": 500, "status": "succeeded", "created": 1, "metadata": {}}
    assert send(c, event(stripe, "pi_x")).json() == {"result": "ignored"}
    assert send(c, {"id": "evt_9", "type": "customer.created", "data": {"object": {"object": "customer"}}}).json() == {"result": "ignored"}
    gone = waiting(desk, stripe, "visitor:old")
    desk.reset("old")                                                                              # its copy was started over
    assert send(c, event(stripe, gone.outside, ident="evt_3")).json() == {"result": "unknown"}


def test_a_failed_payment_event_is_recorded_as_a_decline_and_a_processor_that_cannot_answer_is_retried(desk, stripe):
    i = waiting(desk, stripe)
    stripe.settle(i.outside, "requires_payment_method")
    stripe.intents[i.outside]["last_payment_error"] = {"code": "card_declined"}
    c = hook(desk)
    stripe.fail_with = 500
    assert send(c, event(stripe, i.outside, "payment_intent.payment_failed", "evt_5")).status_code == 503     # the processor retries it later
    assert intent(desk, i).state == "pending"
    assert send(c, event(stripe, i.outside, "payment_intent.payment_failed", "evt_5")).json() == {"result": "applied"}
    assert intent(desk, i).state == "failed" and len(desk.store.books("visitor:v1").declined) >= 1


def test_the_hourly_pass_settles_a_waiting_payment_with_no_webhook(desk, stripe):
    i = waiting(desk, stripe)
    stripe.settle(i.outside, "succeeded")
    assert desk.sweep() == ["visitor:v1"]
    assert intent(desk, i).state == "succeeded" and intent(desk, i).payment
    count = len(desk.store.stream_events("visitor:v1"))
    desk.sweep()
    assert len(desk.store.stream_events("visitor:v1")) == count


def test_the_hourly_pass_finds_a_payment_whose_answer_was_lost_and_lists_one_with_no_intent(desk, stripe):
    t = desk.cast["maria"]["tenant"]
    stripe.lose_reply = True
    lost = col.pay(desk.ready(), "visitor:v2", desk.processor, t, 1500, "card_ok", clock())
    assert lost.state == "open" and col.unsettled(desk.store.books("visitor:v2"), t)
    desk.processor.create("visitor:v2:PI9999", 700, "card_ok", {"copy": "visitor:v2", "intent": "PI9999"}, clock())     # no mark of any earlier copy
    desk.sweep()
    st = col.state(desk.store.books("visitor:v2"))
    assert st.intents[lost.id].state == "succeeded" and st.reconciled[-1]["settled"] == [lost.id]
    assert st.reconciled[-1]["mismatches"] and "no intent here" in st.reconciled[-1]["mismatches"][0]


def test_a_reset_cancels_a_payment_still_processing(desk, stripe):
    i = waiting(desk, stripe)
    assert desk.reset("v1") is True
    assert stripe.intents[i.outside]["status"] == "canceled" and not desk.store.stream_events("visitor:v1")


def test_the_app_mounts_the_webhook_closed_without_a_secret_and_checking_it_with_one(fms_url, sim):
    from core.seed import NOW
    from tests.test_fms import site
    body = json.dumps({"id": "evt_1", "type": "customer.created", "data": {"object": {"object": "customer"}}}).encode()
    assert TestClient(site(fms_url, stripe_secret_key="sk_test_x")).post("/webhooks/stripe", content=body).status_code == 503
    c = TestClient(site(fms_url, stripe_secret_key="sk_test_x", stripe_webhook_secret=SECRET))
    assert c.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign("whsec_wrong", body, int(NOW.timestamp()))}).status_code == 400
    assert c.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign(SECRET, body, int(NOW.timestamp()))}).json() == {"result": "ignored"}


def test_a_header_that_is_not_plain_ascii_is_refused_and_never_a_crash(desk):
    for header in ("t=\u00b2,v1=\u00e9", "t=1,v1=\u00e9", "t=\u0661\u0662,v1=00"):
        assert stripe_hook.signed(SECRET, header, b"{}", 1) is False, header
    r = hook(desk).post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=\u00b2,v1=\u00e9".encode("utf-8")})
    assert r.status_code == 400
