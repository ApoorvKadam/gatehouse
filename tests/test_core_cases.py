"""The graded sets are well formed, every quote is the tenant's own words, every charge resolves, and every acceptable money
action is one the gate allows (labels must be possible, not merely written)."""
from collections import Counter

import pytest

from core.cases import labels_hash, load, resolve_any
from core.evidence import CLAIM_KINDS
from core.gate import ACTIONS, Command, evaluate
from core.seed import NOW, build


@pytest.fixture(scope="module")
def year():
    return build()


def all_items():
    return [(n, i) for n in ("cases", "shift", "outside") for i in load(n)["items"]]


def test_the_splits_are_the_designed_sizes():
    items = load("cases")["items"]
    ids = Counter(i["id"][0] for i in items)
    assert ids == {"d": 15, "h": 60}
    assert Counter(i["op"] for i in items) == {"dev": 24, "held": 36, None: 15}
    assert len(load("outside")["items"]) == 40
    assert len(load("shift")["items"]) == 8 and len({i["role"] for i in load("shift")["items"]}) == 8
    assert len(labels_hash()) == 12


@pytest.mark.parametrize("where, item", all_items(), ids=lambda x: x["id"] if isinstance(x, dict) else x)
def test_each_label_is_well_formed_and_possible(year, where, item):
    flags = set(load("cases")["flags"])
    assert set(item["flags"]) <= flags
    leases = year.cast[item["role"]]["leases"] if item["role"] else []
    t = year.cast[item["role"]]["tenant"] if item["role"] else None
    books = year.led.books
    if not item["claims"]:
        assert set(item["case_act"]) <= set(ACTIONS)
    for c in item["claims"]:
        assert c["kind"] in CLAIM_KINDS
        assert c["quote"] in item["text"], c["quote"]
        for q in ("date_quote", "amount_quote"):
            if c.get(q):
                assert c[q] in item["text"]
        assert set(c["act"]) <= set(ACTIONS) and set(c["critical"]) <= set(ACTIONS)
        assert not set(c["act"]) & set(c["critical"])
        charges = resolve_any(books, leases, c["charge"]) if c["charge"] else []
        if c["charge"]:
            assert charges, f"{item['id']}: {c['charge']} does not resolve"
        for action in set(c["act"]) & {"waive_fee", "correct_fee_posted_in_error", "reverse_duplicate", "open_dispute"}:
            target = charges[-1] if action == "reverse_duplicate" else charges[0]
            if action == "reverse_duplicate":
                target = books.live(books.charges[target].obligation)[-1].id
            d = evaluate(books, Command("check", action, t, "operator", target, quote=c["quote"], raised=True), NOW)
            # a later waiver in the same message is checked after the earlier one is spent
            if action == "waive_fee" and not d.allowed and "already had a waiver" in d.check:
                continue
            assert d.allowed, f"{item['id']}: {action} on {target} refused: {d.check}"
