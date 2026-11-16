"""One message, start to end, against the generated year: the fixed workflow on the shift, and our agent's flow with a
scripted client standing in for the model (no paid call is made here)."""
import json
from types import SimpleNamespace

import pytest

from core.case import FIXED, OPERATOR, handle
from core.cases import load
from core.books import fold
from core.seed import NOW, build
from core.store import Store


@pytest.fixture(scope="module")
def world():
    year = build()
    store = Store()
    with store.write():
        store.append("base", year.led.pending)
    return year, store


class Script:
    """Answers messages.create with the next scripted JSON, and records what it was asked."""
    def __init__(self, *answers, tokens=(900, 200)):
        self.answers, self.asked, self.tokens = list(answers), [], tokens
        self.messages = self

    def create(self, **kw):
        self.asked.append(kw)
        a = self.answers.pop(0)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(a))],
                               usage=SimpleNamespace(input_tokens=self.tokens[0], output_tokens=self.tokens[1], cache_read_input_tokens=0, cache_creation_input_tokens=0))


def phone(year, role):
    return year.cast[role]["phone"]


def charge(year, role, obligation_kind, month=None, copy=0):
    lease = year.lease(role)
    key = f"{obligation_kind}:{lease}:{month}" if month else f"{obligation_kind}:{lease}"
    return year.led.books.obligations[key][copy]


def claim(kind, charge_id, quote, date=None, date_quote=None, amount_quote=None):
    return {"kind": kind, "charge": charge_id, "quote": quote, "date": date, "date_quote": date_quote, "amount_quote": amount_quote}


def test_the_fixed_workflow_handles_the_shift_end_to_end(world):
    year, store = world
    replies = {}
    for item in load("shift")["items"]:
        r = handle(store, f"visitor:fx-{item['id']}", f"fx-{item['id']}", phone(year, item["role"]), item["text"], FIXED, NOW)
        replies[item["id"]] = r
        assert all(o.decision is not None for o in r.outcomes)
    assert "declined on Nov 3 at 10:15 am" in replies["s1"].reply and "stays on the account" in replies["s1"].reply
    assert "will not be collected until" in replies["s3"].reply
    assert "waived" in replies["s4"].reply and "12 months" in replies["s4"].reply
    assert replies["s6"].handed and replies["s8"].handed      # the fixed workflow cannot read Hugo's bank-date question
    # the reply speaks to the tenant, names each fee it decided, and never says the same thing twice
    tom = replies["s4"].reply
    assert "due Sep 6 has been waived" in tom and "due Oct 6 stays on the account: you already had a waiver" in tom
    assert "other fees" not in tom
    for r in replies.values():
        assert "the tenant" not in r.reply
        said = [s.strip() for s in r.reply.replace("\n", " ").split(".") if s.strip()]
        assert len(said) == len(set(said)), r.reply


def test_our_agent_reads_chooses_and_the_gate_acts(world):
    year, store = world
    fee = charge(year, "wes", "late_fee", "2026-11")
    text = "My card was declined on the 3rd so I paid on the 6th. Could you remove the late fee?"
    client = Script({"claims": [claim("wants_waiver", fee, "Could you remove the late fee?")], "flags": []},
                    {"decisions": [{"claim": 0, "action": "waive_fee", "reason": "rule_allows_waiver"}]})
    r = handle(store, "visitor:op1", "op1", phone(year, "wes"), text, OPERATOR, NOW, client=client)
    assert [(o.action, o.chosen_by, o.decision.allowed) for o in r.outcomes] == [("waive_fee", "model", True)]
    assert "waived" in r.reply and not r.handed
    assert len(r.calls) == 2 and 0 < r.cents < 0.5
    assert "<tenant_message>" in client.asked[0]["messages"][0]["content"]
    assert client.asked[0]["output_config"]["format"]["type"] == "json_schema"


def test_a_quote_not_in_the_message_is_dropped_and_the_case_goes_to_staff(world):
    year, store = world
    fee = charge(year, "maria", "late_fee", "2026-11")
    client = Script({"claims": [claim("paid_on_time", fee, "I paid on the 1st")], "flags": []})
    r = handle(store, "visitor:op2", "op2", phone(year, "maria"), "I paid on the 3rd", OPERATOR, NOW, client=client)
    assert r.dropped and r.handed and not r.outcomes


def test_a_charge_not_on_the_list_and_an_amount_not_in_its_words_are_not_trusted(world):
    year, store = world
    other = charge(year, "wes", "late_fee", "2026-11")                       # another tenant's charge
    client = Script({"claims": [claim("wants_waiver", other, "waive the fee", amount_quote="$500")], "flags": []})
    r = handle(store, "visitor:op3", "op3", phone(year, "maria"), "please waive the fee, it was $20", OPERATOR, NOW, client=client)
    assert r.claims[0].charge is None and r.claims[0].cents is None
    assert [o.action for o in r.outcomes] == ["hand_to_staff"] and r.handed


def test_an_action_outside_the_options_goes_to_staff(world):
    year, store = world
    fee = charge(year, "maria", "late_fee", "2026-11")
    client = Script({"claims": [claim("paid_on_time", fee, "I paid on the 3rd", "2026-11-03", "the 3rd")], "flags": []},
                    {"decisions": [{"claim": 0, "action": "reverse_duplicate", "reason": "same_charge_twice"}]})
    r = handle(store, "visitor:op4", "op4", phone(year, "maria"), "I paid on the 3rd", OPERATOR, NOW, client=client)
    assert [o.action for o in r.outcomes] == ["hand_to_staff"]


def test_a_waiver_the_rule_forbids_is_refused_by_the_gate_and_handed_over(world):
    year, store = world
    fee = charge(year, "maria", "late_fee", "2026-11")
    client = Script({"claims": [claim("wants_waiver", fee, "please waive it")], "flags": []},
                    {"decisions": [{"claim": 0, "action": "waive_fee", "reason": "rule_allows_waiver"}]})
    r = handle(store, "visitor:op5", "op5", phone(year, "maria"), "please waive it", OPERATOR, NOW, client=client)
    assert not r.outcomes[0].decision.allowed and fold(store.events("visitor:op5")).charges[fee].closed is None


def test_the_cost_limit_sends_the_case_to_staff(world):
    year, store = world
    fee = charge(year, "wes", "late_fee", "2026-11")
    client = Script({"claims": [claim("wants_waiver", fee, "remove the late fee")], "flags": []}, tokens=(9_000_000, 0))
    r = handle(store, "visitor:op6", "op6", phone(year, "wes"), "remove the late fee", OPERATOR, NOW, client=client)
    assert r.calls[-1].outcome == "limit" and [o.action for o in r.outcomes] == ["hand_to_staff"]


def test_model_off_hands_everything_to_staff_and_changes_nothing(world):
    year, store = world
    r = handle(store, "visitor:op7", "op7", phone(year, "james"), "you charged me twice", OPERATOR, NOW, model_off=True, client=object())
    assert r.handed and not r.outcomes
    assert not [e for e in store.events("visitor:op7") if e.stream == "visitor:op7" and e.kind.startswith("charge.")]


def test_a_shared_number_changes_nothing(world):
    year, store = world
    r = handle(store, "visitor:op8", "op8", phone(year, "kemi"), "why do I have a late fee", FIXED, NOW)
    assert r.tenant is None and r.handed and r.reply.startswith("A team member")


def test_two_claims_give_two_outcomes_and_one_reply_and_a_rerun_writes_nothing_twice(world):
    year, store = world
    first, second = charge(year, "james", "late_fee", "2026-11"), charge(year, "james", "late_fee", "2026-11", 1)
    text = "you guys charged me the november late fee twice, and honestly I paid rent only 2 days late. can you drop both?"
    answers = lambda: ({"claims": [claim("charged_twice", second, "you guys charged me the november late fee twice"),
                                   claim("wants_waiver", first, "can you drop both?")], "flags": []},
                       {"decisions": [{"claim": 1, "action": "waive_fee", "reason": "rule_allows_waiver"}]})
    r = handle(store, "visitor:op9", "op9", phone(year, "james"), text, OPERATOR, NOW, client=Script(*answers()))
    assert sorted(o.action for o in r.outcomes) == ["reverse_duplicate", "waive_fee"] and all(o.decision.allowed for o in r.outcomes)
    assert r.reply.count("Hi James") == 1
    count = len(store.events("visitor:op9"))
    again = handle(store, "visitor:op9", "op9", phone(year, "james"), text, OPERATOR, NOW, client=Script(*answers()))
    assert again.repeated and again.reply == r.reply and not again.calls and len(store.events("visitor:op9")) == count


def test_claims_merge_to_one_per_charge_keeping_the_most_specific():
    from core.agent import merge
    from core.evidence import Claim
    got = merge([Claim("what_is_this", "C1", "why"), Claim("paid_on_time", "C1", "I paid", "2026-11-03", "the 3rd"),
                 Claim("charged_twice", None, "twice"), Claim("charged_twice", "C2", "twice"), Claim("payment_issue", None, "card")])
    assert [(c.kind, c.charge, c.date) for c in got] == [("paid_on_time", "C1", "2026-11-03"), ("charged_twice", "C2", None), ("payment_issue", None, None)]
