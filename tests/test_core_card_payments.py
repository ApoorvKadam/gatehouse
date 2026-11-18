"""A card on file and the staff step that takes a payment on it: the gate checks the amount (whole cents, the processor's smallest
payment, never more than the tenant owes), that the card is this tenant's, and that no earlier payment is waiting; it writes the
decision, and the payment itself is made by pay() and counts only when the processor confirms it. Only the live copy's seed has
cards, so the fixed books are what they were."""
import pytest

from core import delinquency as col
from core.books import at_local
from core.seed import NOW as SEED_NOW, build
from tests.test_core_delinquency import NOW, do, world
from tests.test_core_delinquency_gate import run

CARDS = (("Visa", "4242", "card_ok"), ("Visa", "9995", "card_declined"))


def on_file(store, stream, tenant):
    return do(store, stream, lambda led: [col.save_card(led, tenant, NOW, *c) for c in CARDS])


def take(store, stream, tenant, cents, card, **kw):
    return run(store, stream, action="take_payment", tenant=tenant, source=kw.pop("source", "staff:front_desk"), form={"cents": cents, "card": card}, **kw)


def test_a_saved_card_belongs_to_one_copys_tenant_and_keeps_only_a_brand_the_last_four_and_a_token():
    store, t, _ = world()
    ok, declines = on_file(store, "visitor:c1", t)
    assert (ok, declines) == ("CD0001", "CD0002")
    cards = col.cards_of(store.books("visitor:c1"), t)
    assert cards == [{"card": "CD0001", "brand": "Visa", "last4": "4242", "token": "card_ok"},
                     {"card": "CD0002", "brand": "Visa", "last4": "9995", "token": "card_declined"}]
    assert col.cards_of(store.books("visitor:other"), t) == []              # another copy has none of this copy's


def test_the_gate_allows_a_card_payment_up_to_what_is_owed_and_writes_only_its_decision():
    store, t, _ = world()
    s = "visitor:c2"
    ok, _ = on_file(store, s, t)
    owing = sum(c.open_cents for c in store.books(s).open_charges(t))
    paid = len(store.books(s).payments)
    d = take(store, s, t, 5000, ok)
    assert d.allowed and d.check.startswith("a card payment of $50.00 goes to the card processor")
    assert len(store.books(s).payments) == paid                          # nothing is posted until the processor confirms it
    assert take(store, s, t, owing, ok).allowed
    assert col.owing(store.books(s), t) == owing


@pytest.mark.parametrize("cents,why", [(49, "a card payment is at least $0.50"), (0, "a card payment is at least $0.50"),
                                       (True, "a card payment is at least $0.50"), ("5000", "a card payment is at least $0.50")])
def test_the_gate_refuses_an_amount_that_is_not_whole_cents_the_processor_takes(cents, why):
    store, t, _ = world()
    ok, _ = on_file(store, "visitor:c3", t)
    d = take(store, "visitor:c3", t, cents, ok)
    assert not d.allowed and d.check == why


def test_the_gate_refuses_more_than_is_owed_another_tenants_card_a_missing_card_and_anyone_but_staff():
    store, t, _ = world()
    s = "visitor:c4"
    ok, _ = on_file(store, s, t)
    owing = col.owing(store.books(s), t)
    assert take(store, s, t, owing + 1, ok).check == f"the tenant owes ${owing / 100:,.2f}, and a card payment is never more than that"
    other = do(store, s, lambda led: led.add_tenant(NOW, "Bo Lin", "+15550002"))
    theirs = do(store, s, lambda led: col.save_card(led, other, NOW, "Visa", "4242", "card_ok"))
    assert take(store, s, t, 5000, theirs).check == "that card is not on file for this tenant"
    assert take(store, s, t, 5000, "CD9999").check == "that card is not on file for this tenant"
    assert take(store, s, t, 5000, ok, source="operator").check == "only staff take this step"
    assert take(store, s, t, 5000, ok, source="fixed_workflow").check == "only staff take this step"


def test_no_second_payment_while_one_waits_and_a_repeated_command_returns_its_first_decision():
    store, t, _ = world()
    s = "visitor:c5"
    ok, _ = on_file(store, s, t)
    first = take(store, s, t, 5000, ok, id="form:one")
    again = take(store, s, t, 5000, ok, id="form:one")
    assert first.allowed and again.repeated and again.check == first.check
    col.pay(store, s, col.SimulatedProcessor(), t, 1000, "card_pending", NOW)
    assert take(store, s, t, 5000, ok).check == "an earlier payment's outcome is not known yet; it is checked with the processor, never paid again"


def test_the_live_copys_seed_gives_a_plan_tenant_a_breached_one_and_past_due_tenants_both_test_cards_and_the_fixed_books_none():
    live = build(today=SEED_NOW.date(), cutoff=at_local(SEED_NOW.date()), standing=True)
    st = col.state(live.led.books)
    assert {live.plan_tenant["tenant"], live.breached_tenant["tenant"]} <= set(st.cards)
    assert len(st.cards) >= 5
    for tenant, cards in st.cards.items():
        assert [(c["last4"], c["token"]) for c in cards] == [("4242", "card_ok"), ("9995", "card_declined")], tenant
        assert live.led.books.past_due(tenant, SEED_NOW) > 0 or tenant in (live.plan_tenant["tenant"], live.breached_tenant["tenant"])
    assert col.state(build(today=SEED_NOW.date(), cutoff=at_local(SEED_NOW.date())).led.books).cards == {}
