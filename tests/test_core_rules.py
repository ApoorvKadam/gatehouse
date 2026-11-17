"""The owner-rule compiler's checks, with the model's answer written by hand: code keeps only what the owner's words support,
inside the platform's bounds, and says the rule back in words from the typed form."""
from core.rules import check, days, waiver_words
from core.seed import WAIVER_TYPED

NONE_W = {"per_tenant": None, "window": None, "cap_dollars": None, "on_time_months": None, "only_when_raised": None,
          "min_tenure": None, "max_fee_age": None, "no_rent_owed": None, "current_rent_paid": None}
NO_STAFF = {"flags": [], "claim_kinds": [], "actions": [], "several_units": False}


def raw(outcome="adopt", kind="waiver", waiver=None, staff=None, quotes=(), gaps=(), reason="ok"):
    return {"outcome": outcome, "kind": kind, "waiver": {**NONE_W, **(waiver or {})}, "staff": {**NO_STAFF, **(staff or {})},
            "quotes": [{"field": f, "words": w} for f, w in quotes], "not_expressed": list(gaps), "reason": reason}


def test_a_waiver_change_is_kept_only_with_the_owners_words_and_said_back_in_words():
    s = "Actually, once somebody gets a late fee forgiven, make them wait eighteen months before we forgive another."
    p = check(raw(waiver={"window": {"count": 18, "unit": "months"}}, quotes=[("window", "wait eighteen months")]), s, WAIVER_TYPED)
    assert p.outcome == "adopt" and p.typed["within_days"] == days({"count": 18, "unit": "months"}) == 548
    assert p.typed["on_time_months"] == 6 and "any rolling 18 months" in p.words[0]
    made_up = check(raw(waiver={"window": {"count": 18, "unit": "months"}}, quotes=[("window", "a year and a half")]), s, WAIVER_TYPED)
    assert made_up.outcome == "cannot"


def test_a_number_the_owner_did_not_write_is_never_adopted():
    s = "Allow one waiver a year."
    assert check(raw(waiver={"per_tenant": 12}, quotes=[("per_tenant", "one waiver")]), s, WAIVER_TYPED).outcome == "cannot"
    s2 = "Only waive late charges that happened in the last thirty days."
    p = check(raw(waiver={"max_fee_age": {"count": 30, "unit": "days"}}, quotes=[("max_fee_age", "in the last thirty days")]), s2, WAIVER_TYPED)
    assert p.outcome == "adopt" and p.typed["max_fee_age_days"] == 30
    s4 = "Change that once-a-year waiver to once every eight months."
    p4 = check(raw(waiver={"per_tenant": 1, "window": {"count": 8, "unit": "months"}}, quotes=[("window", "once every eight months")]), s4, WAIVER_TYPED)
    assert p4.outcome == "adopt" and p4.typed["within_days"] == 243          # the unchanged count of one needs no quote of its own
    s3 = "They need to have rented with us for at least a full year first."
    p3 = check(raw(waiver={"min_tenure": {"count": 1, "unit": "years"}}, quotes=[("min_tenure", "at least a full year")]), s3, WAIVER_TYPED)
    assert p3.outcome == "adopt"


def test_no_change_is_already_and_out_of_bounds_is_refused():
    s = "Keep six months of on-time payments."
    assert check(raw(waiver={"on_time_months": 6}, quotes=[("on_time_months", "six months")]), s, WAIVER_TYPED).outcome == "already"
    s2 = "Let them have fifty waivers a year."
    assert check(raw(waiver={"per_tenant": 50}, quotes=[("per_tenant", "fifty waivers")]), s2, WAIVER_TYPED).outcome == "refuse"


def test_a_staff_rule_already_true_is_not_adopted_and_a_new_condition_is():
    s = "The second somebody mentions getting a lawyer, it needs to go to the manager."
    assert check(raw(kind="staff", staff={"flags": ["legal_or_hostile"]}, quotes=[("flags", "getting a lawyer")]), s, WAIVER_TYPED).outcome == "already"
    s2 = "If a person is complaining about fees on two different storage units in one message, send it to the office."
    p = check(raw(kind="staff", staff={"several_units": True}, quotes=[("several_units", "two different storage units")]), s2, WAIVER_TYPED)
    assert p.outcome == "adopt" and p.typed["several_units"] and p.words == ["A message about charges on more than one unit goes to staff."]


def test_a_part_the_form_cannot_hold_blocks_adoption_and_a_refusal_stays_a_refusal():
    s = "Forgive a fee if they qualify and delete any dispute about it."
    assert check(raw(waiver={"per_tenant": 2}, quotes=[("per_tenant", "Forgive a fee")], gaps=["delete any dispute about it"]), s, WAIVER_TYPED).outcome == "cannot"
    assert check(raw(outcome="refuse", kind="none", reason="deletes disputes"), s, WAIVER_TYPED).outcome == "refuse"


def test_last_year_replayed_under_a_looser_rule_names_the_fees_that_change_and_is_the_same_twice():
    from core.rules import replay
    from core.seed import build
    year = build()
    same = replay(year.led.pending, WAIVER_TYPED, WAIVER_TYPED)
    assert same["fees"] > 20 and same["old"] == same["new"] and not same["to_allowed"] and not same["to_refused"]
    looser = replay(year.led.pending, WAIVER_TYPED, {**WAIVER_TYPED, "on_time_months": 2})
    assert looser["new"] > looser["old"] and not looser["to_refused"] and looser["to_allowed"]
    assert looser == replay(year.led.pending, WAIVER_TYPED, {**WAIVER_TYPED, "on_time_months": 2})
    stricter = replay(year.led.pending, WAIVER_TYPED, {**WAIVER_TYPED, "min_tenure_days": 400})
    assert stricter["to_refused"] and not stricter["to_allowed"]


def test_the_seeded_rule_reads_back_as_the_owner_wrote_it():
    words = waiver_words(WAIVER_TYPED)
    assert words[0] == "At most 1 late-fee waiver per tenant in any rolling 1 year, each up to $25."
    assert words[-1] == "Offered only when the tenant raises the fee."
