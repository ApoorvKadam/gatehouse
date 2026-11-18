"""Staff take a card payment on a tenant's page: the form is on the Collections section of a tenant who owes and has a card on file; the
gate refuses what it must, said once; a payment the processor has not confirmed shows as waiting and blocks a second one; a confirmed one
reaches the ledger; a declined card is shown as declined and nothing is posted. The processor here is a fake of its HTTP API; no page
names the vendor or shows one of its ids."""
import re

import pytest

from core.seed import NOW
from core.stripe_processor import StripeProcessor
from tests.fake_stripe import FakeStripe
from tests.test_fms import record_of, seen, sign_in, site
from tests.test_fms_collections import step


@pytest.fixture
def stripe():
    fake = FakeStripe(clock=lambda: NOW.timestamp())
    yield fake
    fake.close()


def app_on(fms_url, stripe=None):
    app = site(fms_url)
    if stripe is not None:
        app.state.front_desk.processor = StripeProcessor("sk_test_fake", base_url=stripe.url, clock=lambda: NOW.timestamp())
    return app


def card(page: str, last4: str) -> str:
    return re.search(rf'<option value="(CD\d+)">Visa ending {last4}', page).group(1)


def pay(c, url, last4, amount):
    page = c.get(url).text
    return step(c, url, page, action="take_payment", amount=amount, card=card(page, last4))


def test_the_form_offers_the_tenants_own_cards_and_a_tenant_with_none_is_told_so(fms_url, sim):
    with sign_in(app_on(fms_url)) as c:
        page = c.get(record_of(c, "Iris Nolan")).text
        text = seen(page)
        assert "Card payments" in text and "Take a card payment" in text
        assert "Visa ending 4242" in text and "Visa ending 9995, a test card that declines" in text
        assert re.search(r'name="amount"[^>]*placeholder="Amount, up to \$[\d,.]+"', page)
        assert "Stripe" not in page and "pi_" not in page
        pages = [c.get(u).text for u in dict.fromkeys(re.findall(r'href="(/tenants/[^"]+)"', c.get("/tenants?status=past").text))]
        bare = [p for p in pages if 'id="collections"' in p and "Take a card payment" not in p]
        assert bare and all("No card is on file for this tenant." in seen(p) for p in bare)


def test_a_confirmed_payment_reaches_the_ledger_once_even_if_the_button_is_pressed_twice(fms_url, sim, stripe):
    with sign_in(app_on(fms_url, stripe)) as c:
        url = record_of(c, "Iris Nolan")
        page = c.get(url).text
        before = seen(page)
        data = dict(action="take_payment", amount="10.00", card=card(page, "4242"))
        r = step(c, url, page, **data)
        assert (r.status_code, r.headers["location"]) == (303, url + "#collections")
        step(c, url, page, **data)                                              # the same page pressed again: the first decision comes back
        after = seen(c.get(url).text)
        assert len(stripe.intents) == 1 and stripe.calls[0].params["amount"] == "1000" and stripe.calls[0].params["metadata[copy]"].startswith("visitor:")
        assert re.search(r"\$10\.00 Paid", after) and "Nothing changed" not in after
        assert after.count("Card payment") >= before.count("Card payment") + 1
        assert "Stripe" not in c.get(url).text and "pi_0" not in c.get(url).text


def test_a_declined_card_is_shown_as_declined_and_posts_nothing(fms_url, sim, stripe):
    with sign_in(app_on(fms_url, stripe)) as c:
        url = record_of(c, "Iris Nolan")
        before = seen(c.get(url).text)
        pay(c, url, "9995", "20.00")
        after = seen(c.get(url).text)
        assert re.search(r"\$20\.00 Declined", after) and "Card declined, $20.00" in after
        assert re.search(r"Balance \$[\d,.]+", before).group() == re.search(r"Balance \$[\d,.]+", after).group()


def test_a_refusal_is_said_once_and_the_amount_must_be_dollars_and_cents(fms_url, sim, stripe):
    with sign_in(app_on(fms_url, stripe)) as c:
        url = record_of(c, "Iris Nolan")
        r = pay(c, url, "4242", "99999.00")
        assert r.headers["location"] == url + "#collections" and not stripe.intents
        first = seen(c.get(url).text)
        assert re.search(r"Nothing changed\. The tenant owes \$[\d,.]+, and a card payment is never more than that\.", first)
        assert "Nothing changed" not in seen(c.get(url).text)
        pay(c, url, "4242", "0.10")
        assert "Nothing changed. A card payment is at least $0.50." in seen(c.get(url).text)
        for bad in ("ten", "1.234", "-5", "1e3", ""):
            assert pay(c, url, "4242", bad).status_code == 422, bad
        assert not stripe.intents


def test_a_payment_the_processor_has_not_confirmed_waits_blocks_a_second_and_is_settled_by_the_hourly_pass(fms_url, sim, stripe):
    app = app_on(fms_url, stripe)
    with sign_in(app) as c:
        url = record_of(c, "Iris Nolan")
        stripe.lose_reply = True
        pay(c, url, "4242", "10.00")
        waiting = seen(c.get(url).text)
        assert re.search(r"\$10\.00 Waiting for the card processor", waiting)
        pay(c, url, "4242", "5.00")
        assert "Nothing changed. An earlier payment's outcome is not known yet" in seen(c.get(url).text) and len(stripe.intents) == 1
        app.state.front_desk.sweep()
        assert re.search(r"\$10\.00 Paid", seen(c.get(url).text))


def test_the_simulated_processor_takes_the_same_form_when_no_key_is_set(fms_url, sim):
    with sign_in(app_on(fms_url)) as c:
        url = record_of(c, "Iris Nolan")
        pay(c, url, "4242", "10.00")
        pay(c, url, "9995", "10.00")
        text = seen(c.get(url).text)
        assert re.search(r"\$10\.00 Paid", text) and re.search(r"\$10\.00 Declined", text)


def test_start_over_is_refused_while_a_payment_waits_and_home_says_so_once_with_the_copy_untouched(fms_url, sim, stripe):
    app = app_on(fms_url, stripe)
    with sign_in(app) as c:
        url = record_of(c, "Iris Nolan")
        stripe.lose_reply = True
        pay(c, url, "4242", "10.00")                                                 # the answer was lost: the payment waits
        stripe.down = True
        r = c.post("/start-over", follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/home")
        home = seen(c.get("/home").text)
        assert "Start over waits: a card payment on this copy has not been settled by the card processor yet, so nothing was thrown away." in home
        assert "Start over waits" not in seen(c.get("/home").text)                    # said once
        assert re.search(r"\$10\.00 Waiting for the card processor", seen(c.get(url).text))
        stripe.down = False
        c.post("/start-over", follow_redirects=False)
        assert "Start over waits" not in seen(c.get("/home").text)
        assert "Card payments Taken" not in seen(c.get(url).text)                      # the copy is a fresh one
