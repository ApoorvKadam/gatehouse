"""Going back to an earlier version of an owner rule: the history is read from the rule.adopted events and never rewritten; going back
adopts the old typed form as the next version, names the version it restored, and is refused for anyone but the owner, for a version
that never existed and for one that already says what is in force."""
from datetime import timedelta

import pytest

from core.billing import Ledger, Refused
from core.gate import STAFF_RULE, Command, evaluate, execute
from core.seed import NOW, WAIVER_RULE, WAIVER_TYPED, build
from core.store import Store

TWO_MONTHS = {**WAIVER_TYPED, "on_time_months": 2}
STAFF_ONE = {"action": "send_to_staff", "flags": [], "claim_kinds": [], "actions": ["reverse_duplicate"], "several_units": False}
STAFF_TWO = {**STAFF_ONE, "actions": ["reverse_duplicate", "open_dispute"]}


@pytest.fixture(scope="module")
def year():
    return build()


def ledger(year) -> Ledger:
    led = Ledger()
    for e in year.led.pending:
        led.books.apply(e.kind, e.at, e.payload)
    return led


def after(n: int):
    return NOW - timedelta(minutes=10 - n)


def test_the_history_lists_every_version_that_took_effect_oldest_first_with_when_and_by_whom(year):
    led = ledger(year)
    assert [(h["version"], h["by"]) for h in led.books.rule_history["late_fee_waiver"]] == [(1, "owner")]
    led.adopt_rule(after(1), "late_fee_waiver", 2, "two good months", TWO_MONTHS)
    h = led.books.rule_history["late_fee_waiver"]
    assert [x["version"] for x in h] == [1, 2] and h[0]["sentence"] == WAIVER_RULE and h[1]["typed"] == TWO_MONTHS and h[1]["at"] == after(1)
    assert "rule_history" in vars(led.books) and led.books.rules["late_fee_waiver"]["version"] == 2


def test_the_history_keeps_what_was_adopted_when_the_caller_changes_its_dict_afterwards():
    typed = dict(WAIVER_TYPED)
    led = Ledger()
    led.adopt_rule(after(1), "late_fee_waiver", 1, "six good months", typed)
    typed["on_time_months"] = 2                                         # the same dict, changed in place, adopted as version 2
    led.adopt_rule(after(2), "late_fee_waiver", 2, "two good months", typed)
    history = led.books.rule_history["late_fee_waiver"]
    assert [h["typed"]["on_time_months"] for h in history] == [WAIVER_TYPED["on_time_months"], 2]
    led.restore_rule(after(3), "late_fee_waiver", 1)                    # not refused as "already in force"
    now = led.books.rules["late_fee_waiver"]
    assert now["version"] == 3 and now["typed"]["on_time_months"] == WAIVER_TYPED["on_time_months"] and now["restores"] == 1


def test_going_back_adopts_the_old_typed_form_as_the_next_version_and_says_which_it_restored(year):
    led = ledger(year)
    led.adopt_rule(after(1), "late_fee_waiver", 2, "two good months", TWO_MONTHS)
    led.restore_rule(after(2), "late_fee_waiver", 1)
    now = led.books.rules["late_fee_waiver"]
    assert now["version"] == 3 and now["typed"] == WAIVER_TYPED and now["sentence"] == WAIVER_RULE and now["restores"] == 1
    event = led.pending[-1]
    assert event.kind == "rule.adopted" and event.payload["restores"] == 1 and event.key == "rule:late_fee_waiver:3"
    assert [h["version"] for h in led.books.rule_history["late_fee_waiver"]] == [1, 2, 3]      # nothing rewritten, only added
    assert "restores" not in led.books.rule_history["late_fee_waiver"][1]


def test_going_back_to_a_staff_rule_replaces_what_it_sent_to_staff(year):
    led = ledger(year)
    led.adopt_rule(after(1), STAFF_RULE, 1, "copies go to staff", STAFF_ONE)
    led.adopt_rule(after(2), STAFF_RULE, 2, "copies and disputes go to staff", STAFF_TWO)
    led.restore_rule(after(3), STAFF_RULE, 1)
    assert led.books.rules[STAFF_RULE]["typed"] == STAFF_ONE and led.books.rules[STAFF_RULE]["version"] == 3


def test_going_back_is_refused_for_a_non_owner_an_unknown_version_and_the_version_in_force(year):
    led = ledger(year)
    led.adopt_rule(after(1), "late_fee_waiver", 2, "two good months", TWO_MONTHS)
    for args, by, why in [((1,), "staff:office_manager", "only the owner"), ((1,), "operator", "only the owner"), ((7,), "owner", "no version 7"),
                          ((2,), "owner", "already says"), ((0,), "owner", "no version 0")]:
        with pytest.raises(Refused, match=why):
            led.restore_rule(after(2), "late_fee_waiver", *args, by=by)
    with pytest.raises(Refused, match="no version"):
        led.restore_rule(after(2), "no_such_rule", 1)
    with pytest.raises(Refused, match="no version"):
        led.restore_rule(after(2), STAFF_RULE, 1)             # no staff rule has been adopted yet
    assert led.books.rules["late_fee_waiver"]["version"] == 2 and len(led.pending) == 1      # only the one adoption was written


def test_after_going_back_the_version_that_matches_it_is_refused_as_a_repeat(year):
    led = ledger(year)
    led.adopt_rule(after(1), "late_fee_waiver", 2, "two good months", TWO_MONTHS)
    led.restore_rule(after(2), "late_fee_waiver", 1)              # version 3 says what version 1 said
    with pytest.raises(Refused, match="already says"):
        led.restore_rule(after(3), "late_fee_waiver", 1)
    led.restore_rule(after(3), "late_fee_waiver", 2)              # but version 2 is still there to go back to
    assert led.books.rules["late_fee_waiver"]["version"] == 4 and led.books.rules["late_fee_waiver"]["typed"] == TWO_MONTHS


def test_marias_late_fee_stands_under_one_is_allowed_under_two_and_stands_again_after_going_back(year):
    led, maria = ledger(year), year.t("maria")
    fee = next(c for c in led.books.open_charges(maria) if c.kind == "late_fee")
    ask = lambda: evaluate(led.books, Command("w", "waive_fee", maria, "operator", fee.id, raised=True), NOW)
    assert not ask().allowed and "not on time" in ask().check              # version 1: she was late in August
    led.adopt_rule(after(1), "late_fee_waiver", 2, "two good months", TWO_MONTHS)
    assert ask().allowed                                                   # version 2: two on-time months are enough for her
    led.restore_rule(after(2), "late_fee_waiver", 1)
    assert not ask().allowed and "not on time" in ask().check              # version 3 says what version 1 said: the fee stands


def test_under_version_two_marias_waiver_is_given_exactly_once(year):
    store = Store()
    with store.write():
        store.append("base", year.led.pending)
    stream, maria = "visitor:once", year.t("maria")
    led = Ledger(store.books(stream))
    led.adopt_rule(after(1), "late_fee_waiver", 2, "two good months", TWO_MONTHS)
    with store.write():
        store.append(stream, led.pending)
    fee = next(c for c in store.books(stream).open_charges(maria) if c.kind == "late_fee")
    ask = lambda n: execute(store, stream, Command(f"w{n}", "waive_fee", maria, "operator", fee.id, raised=True), NOW)
    assert ask(1).allowed and not ask(2).allowed
    assert sum(e.kind == "charge.waived" for e in store.stream_events(stream)) == 1
