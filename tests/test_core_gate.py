"""The gate against the generated year: the adversarial set must give zero violations, and the four stories must come out as
the design says. Each test works in its own visitor stream over the shared year."""
from datetime import timedelta

import pytest

from core.books import ADMIN_FEE, LATE_FEE, RENT, fold
from core.gate import Command, evaluate, evaluate_ask, execute, overdue
from core.seed import NOW, build
from core.store import Store


@pytest.fixture(scope="module")
def world():
    year = build()
    store = Store()
    with store.write():
        store.append("base", year.led.pending)
    return year, store


def fees(books, tenant, kind=LATE_FEE, month=None):
    out = [c for c in books.tenant_charges(tenant) if c.kind == kind and not c.closed]
    return [c for c in out if month is None or (c.due.year, c.due.month) == month]


def run(store, stream, **kw):
    kw.setdefault("source", "operator")
    kw.setdefault("id", f"{stream}:{kw['action']}:{kw.get('charge')}")
    return execute(store, stream, Command(**kw), NOW)


def books_of(store, stream):
    return fold(store.events(stream))


# the four stories -----------------------------------------------------------------------------------------------------
def test_story_late_fee_that_stands(world):
    year, store = world
    b = books_of(store, "base")
    m = year.t("maria")
    fee = fees(b, m, month=(2026, 11))[0]
    assert not evaluate(b, Command("x", "correct_fee_posted_in_error", m, "operator", fee.id), NOW).allowed
    assert not evaluate(b, Command("x", "waive_fee", m, "operator", fee.id, raised=True), NOW).allowed   # late in August
    assert run(store, "visitor:s1", action="explain_charge", tenant=m, charge=fee.id).allowed


def test_story_second_charge_for_the_same_fee_is_reversed(world):
    year, store = world
    j = year.t("james")
    first, second = books_of(store, "base").live(f"late_fee:{year.lease('james')}:2026-11")
    assert not run(store, "visitor:s2", action="reverse_duplicate", tenant=j, charge=first.id).allowed
    d = run(store, "visitor:s2", action="reverse_duplicate", tenant=j, charge=second.id, id="s2-second")
    assert d.allowed
    b = books_of(store, "visitor:s2")
    assert b.charges[second.id].closed == "reversed" and b.charges[first.id].closed is None


def test_story_fee_missing_from_the_signed_schedule_is_held(world):
    year, store = world
    p = year.t("priya")
    fee = fees(books_of(store, "base"), p, ADMIN_FEE)[0]
    d = run(store, "visitor:s3", action="open_dispute", tenant=p, charge=fee.id, quote="I never agreed to this fee")
    assert d.allowed and d.hold
    b = books_of(store, "visitor:s3")
    assert b.held(fee.id) and b.past_due(p, NOW) == 0
    # the next payment skips the held fee, and the operator cannot waive or correct it
    assert not evaluate(b, Command("y", "waive_fee", p, "operator", fee.id, raised=True), NOW).allowed


def test_story_forty_dollar_ask_is_refused_naming_the_rule(world):
    year, store = world
    b = books_of(store, "base")
    t = year.t("tom")
    sep, octo = (fees(b, t, month=(2026, m))[0] for m in (9, 10))
    asked = evaluate_ask(b, t, [sep.id, octo.id], NOW)
    assert [a.allowed for a in asked] == [True, False]
    assert "12 months" in asked[1].check and "one late fee per tenant" in asked[1].rule
    assert run(store, "visitor:s4", action="waive_fee", tenant=t, charge=sep.id, raised=True).allowed
    assert not run(store, "visitor:s4", action="waive_fee", tenant=t, charge=octo.id, raised=True).allowed


# the gate set -----------------------------------------------------------------------------------------------------------
def test_rent_is_never_waived(world):
    year, store = world
    b = books_of(store, "base")
    rent = fees(b, year.t("tom"), RENT)[0]
    assert evaluate(b, Command("x", "waive_fee", year.t("tom"), "operator", rent.id, raised=True), NOW).check == "rent is never waived"


@pytest.mark.parametrize("role, why", [("ben", "already had a waiver"), ("maria", "not on time"), ("gus", "not on time")])
def test_waivers_the_rule_forbids(world, role, why):
    year, store = world
    b = books_of(store, "base")
    fee = fees(b, year.t(role))[-1]
    d = evaluate(b, Command("x", "waive_fee", year.t(role), "operator", fee.id, raised=True), NOW)
    assert not d.allowed and why in d.check and d.rule


def test_a_waiver_needs_the_tenant_to_raise_the_fee(world):
    year, store = world
    b = books_of(store, "base")
    fee = fees(b, year.t("wes"), month=(2026, 11))[0]
    assert not evaluate(b, Command("x", "waive_fee", year.t("wes"), "operator", fee.id), NOW).allowed
    assert evaluate(b, Command("x", "waive_fee", year.t("wes"), "operator", fee.id, raised=True), NOW).allowed
    assert evaluate(b, Command("x", "waive_fee", year.t("rosa"), "operator", fees(b, year.t("rosa"), month=(2026, 11))[0].id, raised=True), NOW).allowed


def test_only_late_fees_are_waived_and_only_on_open_leases(world):
    year, store = world
    b = books_of(store, "base")
    admin = fees(b, year.t("priya"), ADMIN_FEE)[0]
    assert "late fees only" in evaluate(b, Command("x", "waive_fee", year.t("priya"), "operator", admin.id, raised=True), NOW).check
    olga_fee = fees(b, year.t("olga"))[0]
    assert "ended" in evaluate(b, Command("x", "waive_fee", year.t("olga"), "operator", olga_fee.id, raised=True), NOW).check


def test_a_fee_with_no_second_charge_is_not_a_duplicate(world):
    year, store = world
    b = books_of(store, "base")
    fee = fees(b, year.t("maria"))[0]
    assert not evaluate(b, Command("x", "reverse_duplicate", year.t("maria"), "operator", fee.id), NOW).allowed
    # gus has three equal fees for three months: still not duplicates
    for f in fees(b, year.t("gus")):
        assert not evaluate(b, Command("x", "reverse_duplicate", year.t("gus"), "operator", f.id), NOW).allowed


def test_another_tenants_charge_is_refused(world):
    year, store = world
    b = books_of(store, "base")
    fee = fees(b, year.t("maria"))[0]
    d = evaluate(b, Command("x", "explain_charge", year.t("wes"), "operator", fee.id), NOW)
    assert not d.allowed and "another tenant" in d.check


@pytest.mark.parametrize("role, ok", [("ines", True), ("hugo", True), ("maria", False), ("tara", False), ("cole", False)])
def test_fee_posted_in_error_only_when_rent_was_paid_before_the_cutoff(world, role, ok):
    year, store = world
    b = books_of(store, "base")
    fee = fees(b, year.t(role), month=(2026, 11))[0]
    assert evaluate(b, Command("x", "correct_fee_posted_in_error", year.t(role), "operator", fee.id), NOW).allowed is ok


def test_disputing_rent_or_a_signed_fee_records_it_without_a_hold(world):
    year, store = world
    t = year.t("tom")
    b = books_of(store, "base")
    rent = fees(b, t, RENT, month=(2026, 11))[0]
    d = run(store, "visitor:g1", action="open_dispute", tenant=t, charge=rent.id, quote="I never agreed to this rent")
    assert d.allowed and not d.hold
    assert books_of(store, "visitor:g1").past_due(t, NOW) == books_of(store, "base").past_due(t, NOW)
    fee = fees(b, year.t("maria"))[0]
    d = run(store, "visitor:g1", action="open_dispute", tenant=year.t("maria"), charge=fee.id, quote="that is not fair")
    assert d.allowed and not d.hold


def test_a_held_fee_cannot_be_waived_and_a_decided_dispute_is_not_reopened_by_repeating_it(world):
    year, store = world
    p = year.t("priya")
    fee = fees(books_of(store, "base"), p, ADMIN_FEE)[0]
    assert run(store, "visitor:g2", action="open_dispute", tenant=p, charge=fee.id, quote="never agreed").hold
    b = books_of(store, "visitor:g2")
    assert "held" in evaluate(b, Command("x", "waive_fee", p, "operator", fee.id, raised=True), NOW).check
    # staff decide the dispute; the tenant repeating the complaint changes nothing by itself
    from core.billing import Ledger
    led = Ledger(b)
    led.decide_dispute(NOW + timedelta(hours=1), next(iter(b.disputes)), "charge stands")
    with store.write():
        store.append("visitor:g2", led.pending)
    again = run(store, "visitor:g2", action="open_dispute", tenant=p, charge=fee.id, quote="I still never agreed", id="again")
    assert not again.allowed and "staff" in again.check


def test_a_repeated_command_gets_the_first_decision_and_writes_nothing(world):
    year, store = world
    j = year.t("james")
    second = books_of(store, "base").live(f"late_fee:{year.lease('james')}:2026-11")[1]
    first = run(store, "visitor:g3", action="reverse_duplicate", tenant=j, charge=second.id, id="same")
    count = len(store.events("visitor:g3"))
    again = run(store, "visitor:g3", action="reverse_duplicate", tenant=j, charge=second.id, id="same")
    assert again.repeated and again.allowed == first.allowed and len(store.events("visitor:g3")) == count


def test_two_cases_cannot_reverse_the_same_posting(world):
    year, store = world
    j = year.t("james")
    second = books_of(store, "base").live(f"late_fee:{year.lease('james')}:2026-11")[1]
    assert run(store, "visitor:g4", action="reverse_duplicate", tenant=j, charge=second.id, id="case-a").allowed
    later = run(store, "visitor:g4", action="reverse_duplicate", tenant=j, charge=second.id, id="case-b")
    assert not later.allowed and "already reversed" in later.check


def test_a_stale_proposal_is_refused_by_the_same_checks(world):
    year, store = world
    w = year.t("wes")
    fee = fees(books_of(store, "base"), w, month=(2026, 11))[0]
    old = books_of(store, "visitor:g5")                      # two proposals made on this state ...
    assert evaluate(old, Command("x", "waive_fee", w, "operator", fee.id, raised=True), NOW).allowed
    assert run(store, "visitor:g5", action="waive_fee", tenant=w, charge=fee.id, raised=True, id="first").allowed
    # ... the second is checked at execution against the current state, and refused
    later = run(store, "visitor:g5", action="waive_fee", tenant=w, charge=fee.id, raised=True, id="second")
    assert not later.allowed and "already waived" in later.check


def test_unknown_actions_and_unknown_tenants_change_nothing(world):
    year, store = world
    b = books_of(store, "base")
    fee = fees(b, year.t("maria"))[0]
    assert not evaluate(b, Command("x", "delete_charge", year.t("maria"), "operator", fee.id), NOW).allowed
    assert not evaluate(b, Command("x", "waive_fee", "T9999", "operator", fee.id, raised=True), NOW).allowed


def test_an_undecided_dispute_goes_overdue_and_its_hold_stays(world):
    year, store = world
    p = year.t("priya")
    fee = fees(books_of(store, "base"), p, ADMIN_FEE)[0]
    run(store, "visitor:g6", action="open_dispute", tenant=p, charge=fee.id, quote="never agreed")
    b = books_of(store, "visitor:g6")
    assert overdue(b, NOW) == []
    assert overdue(b, NOW + timedelta(days=11)) == list(b.disputes) and b.held(fee.id)


def test_visitor_streams_never_touch_each_other_or_the_year(world):
    year, store = world
    j = year.t("james")
    second = books_of(store, "base").live(f"late_fee:{year.lease('james')}:2026-11")[1]
    assert run(store, "visitor:g7", action="reverse_duplicate", tenant=j, charge=second.id).allowed
    assert books_of(store, "base").charges[second.id].closed is None
    assert books_of(store, "visitor:g8").charges[second.id].closed is None
    assert run(store, "visitor:g8", action="reverse_duplicate", tenant=j, charge=second.id).allowed


def test_owner_rules_only_move_forward_and_stay_in_bounds():
    from datetime import date

    from core.billing import Ledger, Refused
    from core.books import at_local
    from core.seed import WAIVER_TYPED
    led = Ledger()
    led.adopt_rule(at_local(date(2026, 1, 1)), "late_fee_waiver", 2, "v2", WAIVER_TYPED)
    for version, typed, by in ((1, WAIVER_TYPED, "owner"), (3, {**WAIVER_TYPED, "cap_cents": 10 ** 6}, "owner"),
                               (3, {**WAIVER_TYPED, "fee_kind": "rent"}, "owner"), (3, WAIVER_TYPED, "operator")):
        with pytest.raises(Refused):
            led.adopt_rule(at_local(date(2026, 1, 2)), "late_fee_waiver", version, "bad", typed, by=by)
    # an older version folded later never replaces the one in force
    led.books.apply("rule.adopted", at_local(date(2026, 1, 3)), {"rule": "late_fee_waiver", "version": 1, "sentence": "old", "typed": WAIVER_TYPED})
    assert led.books.rules["late_fee_waiver"]["sentence"] == "v2"


def test_a_reopened_dispute_gets_its_new_review_date(world):
    year, store = world
    from core.billing import Ledger
    p = year.t("priya")
    fee = fees(books_of(store, "base"), p, ADMIN_FEE)[0]
    run(store, "visitor:g9", action="open_dispute", tenant=p, charge=fee.id, quote="never agreed")
    led = Ledger(books_of(store, "visitor:g9"))
    d = next(iter(led.books.disputes))
    led.decide_dispute(NOW + timedelta(days=1), d, "charge stands")
    led.reopen_dispute(NOW + timedelta(days=20), d, "the signed papers show no admin fee", hold=True)
    assert led.books.disputes[d].review_by == (NOW + timedelta(days=30)).date()
    assert overdue(led.books, NOW + timedelta(days=21)) == []


# staff decide a dispute ----------------------------------------------------------------------------------------------
def test_staff_remove_a_fee_she_never_signed_for_as_a_correction_not_a_waiver(world):
    year, store = world
    p = year.t("priya")
    fee = fees(books_of(store, "base"), p, ADMIN_FEE)[0]
    run(store, "visitor:s1", action="open_dispute", tenant=p, charge=fee.id, quote="never agreed")
    before = books_of(store, "visitor:s1")
    assert not evaluate(before, Command("x", "remove_disputed_fee", p, "operator", fee.id), NOW).allowed     # only staff decide
    d = run(store, "visitor:s1", action="remove_disputed_fee", tenant=p, charge=fee.id, source="staff:office_manager",
            reason="checked the signed schedule")
    assert d.allowed and "signed" in d.check
    b = books_of(store, "visitor:s1")
    dispute = next(iter(b.disputes.values()))
    assert b.charges[fee.id].closed == "reversed" and dispute.outcome and not b.held(fee.id)
    assert b.waived_within(p, NOW) == before.waived_within(p, NOW)          # a correction never uses up the yearly waiver
    written = len(store.stream_events("visitor:s1"))
    again = run(store, "visitor:s1", action="remove_disputed_fee", tenant=p, charge=fee.id, source="staff:office_manager",
                reason="checked the signed schedule")
    assert again.repeated and len(store.stream_events("visitor:s1")) == written


def test_staff_keep_a_dispute_under_review_and_the_books_stay_as_they_are(world):
    year, store = world
    p = year.t("priya")
    fee = fees(books_of(store, "base"), p, ADMIN_FEE)[0]
    run(store, "visitor:s2", action="open_dispute", tenant=p, charge=fee.id, quote="never agreed")
    lines_before = len(store.journal("visitor:s2"))
    d = run(store, "visitor:s2", action="keep_under_review", tenant=p, charge=fee.id, source="staff:office_manager", reason="asking the owner")
    assert d.allowed and "Nov" in d.check
    b = books_of(store, "visitor:s2")
    assert b.held(fee.id) and next(iter(b.disputes.values())).outcome is None
    assert len(store.journal("visitor:s2")) == lines_before
    recorded = [e for e in store.stream_events("visitor:s2") if e.kind == "gate.decided" and e.payload["action"] == "keep_under_review"]
    assert recorded[0].source == "staff:office_manager" and recorded[0].payload["reason"] == "asking the owner"
    again = run(store, "visitor:s2", action="keep_under_review", tenant=p, charge=fee.id, source="staff:office_manager", reason="again", id="s2:other")
    assert not again.allowed and "already" in again.check


def test_a_fee_in_the_signed_schedule_is_not_removed_as_a_correction_and_no_dispute_means_no_decision(world):
    year, store = world
    m = year.t("maria")
    fee = fees(books_of(store, "base"), m)[0]
    staff = dict(tenant=m, charge=fee.id, source="staff:office_manager", reason="looked")
    assert "no open dispute" in run(store, "visitor:s3", action="keep_under_review", **staff).check
    run(store, "visitor:s3", action="open_dispute", tenant=m, charge=fee.id, quote="that is not fair")
    d = run(store, "visitor:s3", action="remove_disputed_fee", id="s3:remove", **staff)
    assert not d.allowed and "signed schedule" in d.check
