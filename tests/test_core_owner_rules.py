"""Approved owner rules change what the gate allows: a staff rule sends a message to staff before the agent acts, and the new
waiver conditions narrow who may get a waiver. Staff are never stopped by a staff rule."""
from datetime import timedelta

import pytest

from core.billing import Ledger, Refused
from core.gate import Command, evaluate
from core.seed import NOW, WAIVER_TYPED, build


@pytest.fixture(scope="module")
def year():
    return build()


def books_with(year, rule, typed, version=2):
    led = Ledger()
    for e in year.led.pending:
        led.books.apply(e.kind, e.at, e.payload)
    led.adopt_rule(NOW - timedelta(minutes=1), rule, version, "test sentence", typed)
    return led.books


def fee_of(books, tenant):
    return next(c for c in books.open_charges(tenant) if c.kind == "late_fee")


def test_a_staff_rule_sends_the_agents_action_to_staff_and_never_stops_staff(year):
    b = books_with(year, "send_to_staff", {"action": "send_to_staff", "flags": [], "claim_kinds": [], "actions": ["reverse_duplicate"],
                                            "several_units": False}, version=1)
    james = year.t("james")
    second = [c for c in b.tenant_charges(james) if c.kind == "late_fee"][-1]
    d = evaluate(b, Command("x", "reverse_duplicate", james, "operator", second.id), NOW)
    assert not d.allowed and "leaves this action to staff" in d.check and d.rule == "test sentence"
    assert evaluate(b, Command("y", "explain_charge", james, "operator", second.id), NOW).allowed
    assert evaluate(b, Command("z", "reverse_duplicate", james, "staff:office_manager", second.id), NOW).allowed


def test_a_staff_rule_on_flags_claims_and_several_units(year):
    b = books_with(year, "send_to_staff", {"action": "send_to_staff", "flags": ["hardship"], "claim_kinds": ["what_is_this"], "actions": [],
                                            "several_units": True}, version=1)
    dev = year.t("dev")
    c = b.tenant_charges(dev)[-1]
    ask = lambda **kw: evaluate(b, Command("q", "explain_charge", dev, "operator", c.id, **kw), NOW)
    assert not ask(flags=("hardship",)).allowed and not ask(claim_kind="what_is_this").allowed and not ask(units=2).allowed
    assert ask(claim_kind="wrong_amount", units=1).allowed


def test_new_waiver_conditions_narrow_who_gets_a_waiver(year):
    wes = year.t("wes")
    base = books_with(year, "late_fee_waiver", dict(WAIVER_TYPED))
    fee = fee_of(base, wes)
    assert evaluate(base, Command("w", "waive_fee", wes, "operator", fee.id, raised=True), NOW).allowed
    long_tenure = books_with(year, "late_fee_waiver", {**WAIVER_TYPED, "min_tenure_days": 3000})
    assert "has not rented as long" in evaluate(long_tenure, Command("w", "waive_fee", wes, "operator", fee.id, raised=True), NOW).check
    fresh_only = books_with(year, "late_fee_waiver", {**WAIVER_TYPED, "max_fee_age_days": 1})
    assert "older than" in evaluate(fresh_only, Command("w", "waive_fee", wes, "operator", fee.id, raised=True), NOW).check


def test_a_staff_rule_on_one_claim_stops_the_whole_message_before_any_money_moves(year):
    from core.case import Arm, handle
    from core.evidence import Claim
    from core.store import Store
    store = Store()
    with store.write():
        store.append("base", year.led.pending)
    led = Ledger(store.books("visitor:m"))
    led.adopt_rule(NOW - timedelta(minutes=1), "send_to_staff", 1, "unknown charges go to the manager",
                   {"action": "send_to_staff", "flags": [], "claim_kinds": ["what_is_this"], "actions": [], "several_units": False})
    with store.write():
        store.append("visitor:m", led.pending)
    wes = year.t("wes")
    b = store.books("visitor:m")
    fee = fee_of(b, wes)
    rent = next(c for c in b.tenant_charges(wes) if c.kind == "rent")
    claims = [Claim("wants_waiver", fee.id, "please waive the fee"), Claim("what_is_this", rent.id, "and what is this charge")]
    res = handle(store, "visitor:m", "m1", year.cast["wes"]["phone"], "please waive the fee and what is this charge",
                 Arm("labels", reader="labels", chooser="table"), NOW, labelled=(claims, []))
    assert res.handed and all(o.action == "hand_to_staff" for o in res.outcomes)
    assert not store.books("visitor:m").charges[fee.id].closed                     # no waiver was written


def test_the_platform_refuses_rules_outside_its_bounds_or_vocabulary():
    led = Ledger()
    with pytest.raises(Refused):
        led.adopt_rule(NOW, "late_fee_waiver", 1, "s", {**WAIVER_TYPED, "max_fee_age_days": 0})
    with pytest.raises(Refused):
        led.adopt_rule(NOW, "send_to_staff", 1, "s", {"action": "send_to_staff", "flags": ["mood"], "claim_kinds": [], "actions": [], "several_units": False})
    with pytest.raises(Refused):
        led.adopt_rule(NOW, "send_to_staff", 1, "s", {"action": "send_to_staff", "flags": [], "claim_kinds": [], "actions": [], "several_units": False})
