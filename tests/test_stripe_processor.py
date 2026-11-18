"""The card processor's test-mode adapter, run against a fake of its HTTP API on a real socket: one payment per idempotency key,
the key and the test card sent as documented, a decline read from the 402, an unknown answer when the reply is lost, payments found
again by listing (never by search), and a live key refused before anything is sent."""
from datetime import datetime, timedelta

import pytest

from core import delinquency as col
from core.books import ZONE
from core.stripe_processor import StripeProcessor, from_key
from tests.fake_stripe import FakeStripe
from tests.test_core_delinquency import NOW, world

KEY = "visitor:v1:PI0001:abc123"
META = {"copy": "visitor:v1", "intent": "PI0001", "nonce": "abc123"}


@pytest.fixture
def stripe():
    fake = FakeStripe(clock=lambda: NOW.timestamp())
    yield fake
    fake.close()


def processor(fake) -> StripeProcessor:
    return StripeProcessor("sk_test_fake", base_url=fake.url, clock=lambda: NOW.timestamp())


def test_a_payment_is_created_once_per_key_with_the_key_the_test_card_and_the_marks_sent_as_documented(stripe):
    p = processor(stripe)
    first = p.create(KEY, 2500, "card_ok", META, NOW)
    again = p.create(KEY, 2500, "card_ok", META, NOW)
    assert first == again and len(stripe.intents) == 1
    assert (first["status"], first["cents"], first["key"], first["metadata"], first["simulated"]) == ("succeeded", 2500, KEY, META, False)
    assert datetime.fromisoformat(first["at"]) == NOW.astimezone(ZONE)
    sent = stripe.calls[0]
    assert sent.headers["idempotency-key"] == KEY and sent.headers["authorization"] == "Bearer sk_test_fake"
    assert (sent.params["payment_method"], sent.params["confirm"], sent.params["currency"], sent.params["amount"]) == ("pm_card_visa", "true", "usd", "2500")
    assert sent.params["automatic_payment_methods[allow_redirects]"] == "never"
    assert "sk_test_fake" not in sent.raw_path                  # the key travels in a header, never in the address
    assert p.retrieve(first["id"]) == first


def test_a_declined_card_is_a_failed_payment_and_a_retry_of_it_is_the_same_failure(stripe):
    p = processor(stripe)
    declined = p.create(KEY, 2500, "card_declined", META, NOW)
    assert declined["status"] == "failed" and declined["id"].startswith("pi_") and declined["key"] == KEY
    assert p.create(KEY, 2500, "card_declined", META, NOW) == declined and len(stripe.intents) == 1
    assert p.retrieve(declined["id"])["status"] == "failed"


def test_a_lost_reply_is_unknown_and_the_payment_is_found_again_by_its_key(stripe):
    p = processor(stripe)
    assert p.find(KEY) is None
    stripe.lose_reply = True
    with pytest.raises(col.ProcessorTimeout):
        p.create(KEY, 2500, "card_ok", META, NOW)
    found = p.find(KEY)
    assert found and found["status"] == "succeeded" and found["metadata"] == META
    assert p.create(KEY, 2500, "card_ok", META, NOW) == found and len(stripe.intents) == 1
    assert all("search" not in c.path for c in stripe.calls)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_busy_or_failing_processor_leaves_the_outcome_unknown(stripe, status):
    stripe.fail_with = status
    with pytest.raises(col.ProcessorTimeout):
        processor(stripe).create(KEY, 2500, "card_ok", META, NOW)


def test_a_request_the_processor_refuses_outright_is_a_refusal_with_no_vendor_text(stripe):
    with pytest.raises(col.ProcessorRefused) as e:
        processor(stripe).create(KEY, 10, "card_ok", META, NOW)         # under the smallest card payment
    assert str(e.value) == "amount_too_small"
    with pytest.raises(col.ProcessorRefused):
        StripeProcessor("sk_test_other", base_url=stripe.url).create(KEY, 2500, "card_ok", META, NOW)     # a key the account does not know
    assert not stripe.intents


def test_a_card_the_processor_has_no_test_method_for_is_refused_and_closes_its_intent(stripe):
    p = processor(stripe)
    with pytest.raises(col.ProcessorRefused):
        p.create(KEY, 2500, "card_pending", META, NOW)                  # a token only the simulated processor knows
    store, t, _ = world()
    i = col.pay(store, "visitor:v1", p, t, 2500, "card_pending", NOW)
    assert i.state == "closed" and not col.unsettled(store.books("visitor:v1"), t) and not stripe.calls


def test_a_copys_payments_are_listed_across_pages_and_only_its_own(stripe):
    p = processor(stripe)
    for n in range(1, 4):
        p.create(f"visitor:v1:PI000{n}:abc123", 1000 * n, "card_ok", {**META, "intent": f"PI000{n}"}, NOW)
    p.create("visitor:v2:PI0001:zzz", 900, "card_ok", {"copy": "visitor:v2", "intent": "PI0001", "nonce": "zzz"}, NOW)
    p.create("bare", 700, "card_ok", {}, NOW)
    p.page = 2                                                  # two to a page, so the listing has to follow its cursor
    mine = p.for_copy("visitor:v1")
    assert sorted(x["cents"] for x in mine) == [1000, 2000, 3000]
    assert p.find("visitor:v2:PI0001:zzz")["cents"] == 900 and p.take_events() == []


def test_cancel_stops_a_payment_still_processing_and_leaves_a_settled_one_as_it_is(stripe):
    stripe.keep_processing = True
    p = processor(stripe)
    waiting = p.create(KEY, 2500, "card_ok", META, NOW)
    assert waiting["status"] == "processing"
    assert p.cancel(waiting["id"])["status"] == "canceled"
    stripe.keep_processing = False
    done = p.create("visitor:v1:PI0002:abc123", 2500, "card_ok", {**META, "intent": "PI0002"}, NOW)
    assert p.cancel(done["id"])["status"] == "succeeded"
    assert col.close_copy(p, "visitor:v1") == []


def test_only_a_test_mode_key_is_ever_used_and_any_other_key_is_refused_without_naming_it():
    assert from_key(None) is None and from_key("") is None             # unset: the simulated processor stays
    assert isinstance(from_key("sk_test_abc"), StripeProcessor)
    # a live key, a restricted key and a typo are all refused: a mistyped key must not fall back to the simulated processor unseen
    for other in ("sk_live_secretvalue", "rk_live_secretvalue", "rk_test_secretvalue", "not-a-key-secretvalue", "SK_TEST_secretvalue"):
        with pytest.raises(ValueError) as e:
            from_key(other)
        assert "STRIPE_SECRET_KEY" in str(e.value) and "secretvalue" not in str(e.value), other
        with pytest.raises(ValueError) as e:
            StripeProcessor(other)
        assert "secretvalue" not in str(e.value)


def test_a_payment_through_the_processor_posts_once_and_a_decline_is_recorded_not_posted(stripe):
    store, t, _ = world()
    p = processor(stripe)
    paid = col.pay(store, "visitor:v1", p, t, 5000, "card_ok", NOW)
    assert paid.state == "succeeded" and paid.payment and paid.outside.startswith("pi_")
    before = store.books("visitor:v1").payments[paid.payment]
    assert before.cents == 5000
    again = col.send(store, "visitor:v1", p, paid.id, "card_ok", NOW)       # a retry of the same intent: nothing is paid again
    assert again.state == "succeeded" and len(stripe.intents) == 1
    bad = col.pay(store, "visitor:v1", p, t, 1000, "card_declined", NOW)
    assert bad.state == "failed" and bad.payment is None and len(store.books("visitor:v1").declined) == 2
    refused = col.pay(store, "visitor:v1", p, t, 10, "card_ok", NOW)       # the processor refuses the request: the intent is closed at once
    assert refused.state == "closed" and not col.unsettled(store.books("visitor:v1"), t)


def test_the_desk_uses_the_processor_only_for_a_test_key_refuses_a_live_one_and_keeps_the_key_out_of_its_settings(fms_url, sim, monkeypatch):
    from core.delinquency import SimulatedProcessor
    from desk.config import Settings
    from tests.test_fms import site
    assert isinstance(site(fms_url).state.front_desk.processor, SimulatedProcessor)
    assert isinstance(site(fms_url, stripe_secret_key="sk_test_abc").state.front_desk.processor, StripeProcessor)
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY") as e:
        site(fms_url, stripe_secret_key="sk_live_secretvalue")
    assert "secretvalue" not in str(e.value)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fromenv")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fromenv")
    settings = Settings.from_env()
    assert (settings.stripe_secret_key, settings.stripe_webhook_secret) == ("sk_test_fromenv", "whsec_fromenv")
    assert "fromenv" not in repr(settings)


def test_a_waiting_payment_older_than_the_listing_window_is_still_found_settled_and_cancelled(stripe):
    store, t, _ = world()
    p = processor(stripe)
    stripe.lose_reply = True
    lost = col.pay(store, "visitor:v1", p, t, 2500, "card_ok", NOW)                  # charged; the answer never came
    stripe.keep_processing = True
    waiting = col.pay(store, "visitor:v2", p, t, 1500, "card_ok", NOW)               # still processing
    stripe.keep_processing = False
    assert lost.state == "open" and waiting.state == "pending"
    later = NOW + timedelta(hours=40)                                                 # the hold has no limit: longer than the default window
    p.clock = lambda: later.timestamp()
    assert p.find(lost.key) is None and p.for_copy("visitor:v2") == []                # a plain lookup reads back 36 hours only
    assert p.find(lost.key, since=lost.created)["status"] == "succeeded"              # a waiting intent says how far back to read
    col.reconcile(store, "visitor:v1", p, later)
    assert col.state(store.books("visitor:v1")).intents[lost.id].state == "succeeded"           # not closed as absent
    assert col.close_copy(p, "visitor:v2", waiting.created) == [waiting.outside]
    assert stripe.intents[waiting.outside]["status"] == "canceled"
