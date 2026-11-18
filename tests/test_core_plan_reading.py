"""Replies about paying in parts: code decides from a confirmed reading, never the model. With the hand reading of every item in
the frozen set, the decision is one the labels call right and never critical; the model's quotes and days are kept only when
code finds them in the message; promises read their day forward; the fixed workflow runs on the same context."""
import hashlib
import json
from datetime import date
from pathlib import Path

from core.plan_reading import Context, Reading, check, context_of, decide, fixed_workflow, grade
from core.reader import read_date

SET = Path(__file__).resolve().parents[1] / "core" / "graded" / "collections.json"
DOC = json.loads(SET.read_text())
POLICY = DOC["policy"]
TODAY = date(2026, 11, 17)
TO_FLAG = {"unverified_authority": "claims_permission", "uncertain": "unsure"}


def hand_reading(item: dict) -> Reading:
    """The label's reading as the typed form a perfect reader would return."""
    r = item["reading"]
    kind, detail = r["kind"], r["detail"]
    flags = [TO_FLAG.get(f, f) for f in r["flags"]]
    out = Reading(kind, flags=[f for f in flags if f in __import__("core.plan_reading", fromlist=["FLAGS"]).FLAGS], text_seen=item["text"])
    if kind == "accept" and detail != "none":
        out.offer = detail
    if kind == "promise" and detail != "none":
        cents, day = detail.split(":")
        out.promise_cents, out.promise_day = int(cents), date.fromisoformat(day)
    if kind == "counter":
        if "x" in detail and detail.split("x")[0].isdigit():
            each, n = detail.split("x")
            out.upfront, out.instalment, out.instalments = int(each), int(each), int(n) if n.isdigit() else None
        elif "+" in detail:
            up, rest = detail.split("+")
            out.upfront, out.instalment = int(up), int("".join(c for c in rest if c.isdigit()))
            out.every = "month" if "monthly" in rest else None
        elif detail.isdigit():
            out.upfront = int(detail)
        elif detail.endswith("biweekly"):
            out.instalment, out.every = int(detail[:-8]), "two_weeks"
    return out


def test_the_set_is_the_frozen_one():
    assert hashlib.sha256(SET.read_bytes()).hexdigest() == "7a230fda32730818520a6e8b8e939207e39f5696aa06649171698c9cf0125501"
    assert len(DOC["graded"]) == 40 and len(DOC["practice"]) == 12


def test_code_decides_right_on_every_item_from_the_hand_reading():
    for item in DOC["graded"] + DOC["practice"]:
        g = grade(item, decide(hand_reading(item), context_of(item, POLICY)))
        assert g["right"] and not g["critical"], (item["id"], g)


def test_a_failed_reading_or_a_staff_flag_goes_to_staff():
    ctx = Context(TODAY, 30000, 400, POLICY, [("P1", 10000, 2)])
    assert decide(None, ctx) == ["hand_to_staff"]
    assert decide(Reading("accept", offer="P1", flags=["instructions_to_system"], text_seen="P1"), ctx) == ["hand_to_staff"]
    assert decide(Reading("accept", offer="P1", flags=["conditional"], text_seen="P1 if I get paid"), ctx) == ["clarify"]
    assert decide(Reading("accept", offer="P9", text_seen="P9"), ctx) == ["clarify"]
    assert decide(Reading("accept", offer="P1", text_seen="I'll take option 3"), ctx) == ["clarify"]     # the words do not point at P1
    assert decide(Reading("accept", offer="P1", text_seen="I accept P1"), ctx) == ["accept_plan:P1"]
    assert decide(Reading("accept", offer="P1", text_seen="the 100 today works"), ctx) == ["accept_plan:P1"]
    assert decide(Reading("promise", promise_cents=7000, promise_day=date(2026, 11, 25), flags=["conditional"]), ctx) == ["clarify"]


def test_only_words_found_in_the_message_count_and_the_day_is_read_forward():
    text = "I can't clear it today but I promise I'll send $70 on November 25th."
    raw = {"kind": "promise", "quote": "I promise I'll send $70 on November 25th", "offer": "P7", "upfront_quote": "$50", "instalment_quote": None,
           "instalments": None, "every": None, "promise_amount_quote": "$70", "promise_day_quote": "November 25th", "promise_day": "2026-11-25", "flags": ["hardship", "made_up"]}
    r = check(raw, text, ["P1"], TODAY)
    assert (r.promise_cents, r.promise_day, r.offer, r.upfront, r.flags) == (7000, date(2026, 11, 25), None, None, ["hardship"])
    assert any("offer" in d for d in r.dropped) and any("upfront" in d for d in r.dropped)
    wrong_day = check({**raw, "promise_day": "2026-12-25"}, text, [], TODAY)
    assert wrong_day.promise_day is None and decide(wrong_day, Context(TODAY, 26500, 120, POLICY)) == ["clarify"]
    assert read_date("tomorrow morning", TODAY, ahead=True) == date(2026, 11, 18)
    assert read_date("the 19th", TODAY, ahead=True) == date(2026, 11, 19)
    assert read_date("the 3rd", TODAY, ahead=True) == date(2026, 12, 3)
    assert read_date("December 10", TODAY, ahead=True) == date(2026, 12, 10)
    assert read_date("the 3rd", TODAY) == date(2026, 11, 3)                  # paid dates still look back


def test_a_counter_offer_becomes_the_one_plan_code_builds_or_a_refusal():
    ctx = Context(TODAY, 30000, 400, POLICY)
    assert decide(Reading("counter", upfront=7500, instalment=7500), ctx) == ["offer_plan:7500x3"]
    assert decide(Reading("counter", upfront=1500, instalment=7000), ctx) == ["refuse_counter"]
    assert decide(Reading("counter", upfront=5000, instalment=3500, every="month"), ctx) == ["refuse_counter"]
    assert decide(Reading("counter", upfront=5000, instalments=5), ctx) == ["refuse_counter"]
    assert decide(Reading("counter", upfront=10000, instalment=9000, instalments=2), ctx) == ["refuse_counter"]     # not what code builds


def test_a_wrong_plan_or_a_wrong_day_is_critical():
    item = next(i for i in DOC["graded"] if i["id"] == "c34")
    assert grade(item, ["accept_plan:P3"])["critical"] == ["accept_plan:P3"]
    item = next(i for i in DOC["graded"] if i["id"] == "c14")
    assert grade(item, ["record_promise:7000:2026-12-25"])["critical"] == ["record_promise:7000:2026-12-25"]
    assert grade(item, ["record_promise:7000:2026-11-25"]) == {"id": "c14", "right": True, "critical": [], "got": ["record_promise:7000:2026-11-25"]}


def test_the_fixed_workflow_runs_on_the_same_context_and_is_graded_the_same_way():
    item = next(i for i in DOC["graded"] if i["id"] == "c7")           # "I'll take option 3": the keywords accept P1, which is critical
    g = grade(item, fixed_workflow(item["text"], context_of(item, POLICY)))
    assert g["critical"] == ["accept_plan:P1"]
    item = next(i for i in DOC["graded"] if i["id"] == "c14")
    assert grade(item, fixed_workflow(item["text"], context_of(item, POLICY)))["right"]


def test_two_named_plans_or_a_shared_amount_are_asked_again():
    ctx = Context(TODAY, 30000, 400, POLICY, [("P1", 10000, 2), ("P2", 5000, 3)])
    assert decide(Reading("accept", offer="P1", text_seen="No to P1. I accept P2"), ctx) == ["clarify"]
    assert decide(Reading("accept", offer="P2", text_seen="I accept P2"), ctx) == ["accept_plan:P2"]
    assert decide(Reading("accept", offer="P1", text_seen="the one with 100 today"), ctx) == ["accept_plan:P1"]
    assert decide(Reading("accept", offer="P1", text_seen="50 today and 100 later"), ctx) == ["clarify"]     # 50 belongs to P2 only


def test_weekdays_are_read_only_when_they_name_one_day():
    friday = date(2026, 11, 20)
    assert read_date("this Friday", friday, ahead=True) == friday
    assert read_date("Friday", friday, ahead=True) is None
    assert read_date("next Friday", TODAY, ahead=True) is None
    assert read_date("on Friday", TODAY, ahead=True) == friday


def test_a_promise_without_the_hand_over_its_set_needs_is_critical():
    item = next(i for i in DOC["practice"] if i["id"] == "p12")
    assert grade(item, ["record_promise:10000:2026-11-24"])["critical"] == ["record_promise:10000:2026-11-24"]
    assert grade(item, ["record_promise:10000:2026-11-24", "hand_to_staff"]) == {"id": "p12", "right": True, "critical": [], "got": ["record_promise:10000:2026-11-24", "hand_to_staff"]}
