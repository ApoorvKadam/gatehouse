"""The tenant conversation. The model is scripted here, so these tests show what the desk does with whatever the
model asks for: which lookups it allows, when a card is charged and what the tenant is told."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from desk.models import CaseEvent, IdentityCheck, PaymentCommand
from desk.conversation import Conversation

from .test_verified_payment import DANA, case_for, settle

DANA_ID = {"last_name": "Whitfield", "phone_last4": "0142", "unit_name": "B-1042"}


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(name, args=None, id_="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=args or {}, id=id_)


class ScriptedModel:
    """Plays back one response per call and keeps what it was sent."""

    def __init__(self, *turns, stop_reason=None):
        self.turns, self.sent, self.stop_reason = list(turns), [], stop_reason
        self.messages = self

    def create(self, **kwargs):
        self.sent.append(kwargs["messages"][:])
        content = self.turns.pop(0)
        stop = self.stop_reason or ("tool_use" if any(b.type == "tool_use" for b in content) else "end_turn")
        return SimpleNamespace(content=content, stop_reason=stop, usage=SimpleNamespace(input_tokens=900, output_tokens=60))


def start(session, adapter, model):
    case, _ = case_for(session, adapter, DANA)
    return Conversation(case.id, adapter, model), case


def commands(session):
    return session.scalars(select(PaymentCommand)).all()


def test_no_balance_before_the_caller_is_verified(session, adapter, sim):
    model = ScriptedModel([tool("get_balance")], [text("I need to confirm who you are first.")])
    conv, _ = start(session, adapter, model)
    reply = conv.say(session, "how much do I owe")
    assert not reply.verified
    tool_result = model.sent[1][-1]["content"][0]["content"]
    assert "not verified" in tool_result and "24000" not in tool_result


def test_verified_tenant_pays_after_an_explicit_yes(session, adapter, sim):
    model = ScriptedModel(
        [tool("verify_identity", DANA_ID)], [tool("get_balance", id_="t2")], [text("You owe $240.00. Want to pay it today?")],
        [tool("propose_payment", {"amount_cents": 24000}, id_="t3")],
    )
    conv, case = start(session, adapter, model)
    assert "$240.00" in conv.say(session, "Whitfield, 0142, unit B-1042").text
    asked = conv.say(session, "yes, all of it")
    assert asked.proposed_amount == 24000 and asked.card_last4 == "4242"
    assert "$240.00" in asked.text and "4242" in asked.text and commands(session) == []

    paid = conv.say(session, "Yes")
    assert paid.payment_state == "CONFIRMED" and len(sim.payments(DANA)) == 1
    assert "applied" in paid.text.lower() or "received" in paid.text.lower()
    checks = session.scalars(select(IdentityCheck).where(IdentityCheck.case_id == case.id)).all()
    assert [c.verified for c in checks] == [True]


def test_a_yes_to_some_other_question_never_charges(session, adapter, sim):
    model = ScriptedModel(
        [tool("verify_identity", DANA_ID)], [text("Thanks, you're verified.")],
        [tool("propose_payment", {"amount_cents": 24000})],
        [text("The $25 is a late fee from the 6th. Anything else?")], [text("Okay. Anything else I can help with?")],
    )
    conv, _ = start(session, adapter, model)
    conv.say(session, "Whitfield 0142 B-1042")
    conv.say(session, "pay it all")
    conv.say(session, "wait, what is the $25 for?")
    conv.say(session, "yes")   # answers "anything else?", not the old proposal
    assert commands(session) == [] and sim.payments(DANA) == []


def test_the_model_cannot_propose_before_verification_or_above_the_balance(session, adapter, sim):
    model = ScriptedModel(
        [tool("propose_payment", {"amount_cents": 24000})], [text("Let's confirm who you are first.")],
        [tool("verify_identity", DANA_ID)], [tool("propose_payment", {"amount_cents": 99999}, id_="t2")], [text("That is more than you owe.")],
        [text("Would you like to pay the $240.00 balance instead?")],
    )
    conv, _ = start(session, adapter, model)
    assert conv.say(session, "charge my card").proposed_amount is None
    assert conv.say(session, "Whitfield 0142 B-1042, charge $999.99").proposed_amount is None
    assert "between 1 and the balance" in model.sent[-1][-1]["content"][0]["content"]
    conv.say(session, "yes")
    assert commands(session) == []


def test_three_wrong_identity_answers_hand_the_thread_to_staff(session, adapter, sim):
    wrong = {"last_name": "Whitfield", "phone_last4": "1111", "unit_name": "B-1042"}
    model = ScriptedModel(*([[tool("verify_identity", wrong)], [text("That doesn't match. Try again?")]] * 2),
                          [tool("verify_identity", wrong)])
    conv, _ = start(session, adapter, model)
    conv.say(session, "Whitfield 1111 B-1042")
    conv.say(session, "Whitfield 1111 B-1042")
    reply = conv.say(session, "Whitfield 1111 B-1042")
    assert reply.handed_to_staff and not reply.verified
    calls = len(model.sent)
    assert "team member" in conv.say(session, "Whitfield 0142 B-1042").text
    assert len(model.sent) == calls   # the model is not asked again


def test_a_refusal_goes_to_staff_and_never_back_to_the_model(session, adapter, sim):
    model = ScriptedModel([text("")], stop_reason="refusal")
    conv, _ = start(session, adapter, model)
    assert conv.say(session, "something the model will not touch").handed_to_staff
    conv.say(session, "hello?")
    assert len(model.sent) == 1


def test_a_pending_payment_is_described_as_processing_and_never_retried(session, adapter, sim):
    sim.behave(DANA, "pending_then_succeed", lag=2)
    model = ScriptedModel([tool("verify_identity", DANA_ID)], [tool("propose_payment", {"amount_cents": 24000}, id_="t2")],
                          [tool("payment_status")], [text("It's still processing; please don't pay again.")])
    conv, _ = start(session, adapter, model)
    conv.say(session, "Whitfield 0142 B-1042, pay the full balance")
    reply = conv.say(session, "yes")
    assert reply.payment_state == "PENDING_AT_FMS" and "do not pay again" in reply.text.lower()
    conv.say(session, "did it go through?")
    assert "PENDING_AT_FMS" in model.sent[-1][-1]["content"][0]["content"]
    settle(session, adapter)
    assert len(sim.payments(DANA)) == 1


def test_the_assistant_turn_is_sent_back_exactly_as_received(session, adapter, sim):
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    first = [thinking, tool("verify_identity", DANA_ID)]
    model = ScriptedModel(first, [text("You're verified.")])
    conv, _ = start(session, adapter, model)
    conv.say(session, "Whitfield 0142 B-1042")
    assert model.sent[1][-2]["content"] is first


def test_every_message_is_kept_on_the_case(session, adapter, sim):
    model = ScriptedModel([text("Hi, can I get your last name, phone last four and unit?")])
    conv, case = start(session, adapter, model)
    conv.say(session, "hi")
    kinds = [e.kind for e in session.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id))]
    assert "tenant said" in kinds and "desk replied" in kinds


@pytest.mark.skipif(not os.environ.get("DESK_LIVE_MODEL"), reason="set DESK_LIVE_MODEL=1 to spend about a cent on a real model call")
def test_live_model_verifies_before_talking_money(session, adapter, sim):
    import anthropic

    conv, _ = start(session, adapter, anthropic.Anthropic())
    first = conv.say(session, "Hi, I got a text that I'm behind on my unit. How much do I owe?")
    assert "240" not in first.text and not first.verified
    second = conv.say(session, "Dana Whitfield, phone ends 0142, unit B-1042")
    assert second.verified and "240" in second.text
    third = conv.say(session, "I'll pay all of it with my card on file")
    assert third.proposed_amount == 24000 and commands(session) == []
    # the desk's own confirmation question sits in the history; the next model call must accept it
    fourth = conv.say(session, "Actually, before I pay, what is the extra $25?")
    assert fourth.proposed_amount is None and commands(session) == []
    assert conv.cost_usd < 0.05
