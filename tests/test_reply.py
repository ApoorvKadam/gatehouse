"""The agent proposes which verified facts an acknowledgement carries. The desk writes every sentence from its catalog, resolves each part
from its own records, leaves out what it cannot confirm, and sends once through send_text. The model is scripted here; the desk, the
simulator and the page are the real ones."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from desk.investigator import investigate, run_view
from desk.messaging import SMS_LIMIT
from desk.models import Lease, OutboundMessage, PaymentCommand
from desk.wording import REPLY_PHRASES

from .conftest import ORLANDO
from .test_investigator import (DANA_LEASE, MARCUS_LEASE, Scripted, dana_facts, dana_packet, factory, message_ids, packet, text_of, tool)  # noqa: F401
from .test_live import A, click, live_app

OPEN, BALANCE, SALE, REVIEW, CLOSE = ({"kind": "message_received", "record_id": ""}, "balance_as_of", "sale_status", {"kind": "staff_review", "record_id": ""},
                                      {"kind": "acknowledgment", "record_id": ""})


def part(kind: str, record_id: str = "") -> dict:
    return {"kind": kind, "record_id": record_id}


def for_dana(s, components, review: bool = True) -> Scripted:
    """The lookups, then the review request and the proposed acknowledgement, then the packet."""
    sale = f"workitem:{dana_facts(s)['sale'].id}"
    second = ([tool("request_staff_review", {"record_id": sale}, id_="a4")] if review else []) + [tool("draft_reply", {"components": components}, id_="a5")]
    return Scripted([tool("get_account", id_="a1"), tool("get_sale_status", {"lease_id": DANA_LEASE}, id_="a2"), tool("get_payments", {"lease_id": DANA_LEASE}, id_="a3")],
                    second, [tool("submit_case_packet", dana_packet(s), id_="a6")])


def whole(s) -> list[dict]:
    """Everything Dana's records can support, in the order a person would say it."""
    facts = dana_facts(s)
    return [part("message_received"), part("balance_as_of", facts["lease"]), part("sale_status", f"workitem:{facts['sale'].id}"), part("staff_review"), part("acknowledgment")]


def texts_to(s, name: str) -> list[OutboundMessage]:
    return list(s.scalars(select(OutboundMessage).where(OutboundMessage.inbound_id == text_of(s, name).id).order_by(OutboundMessage.id)))


def reply_of(run) -> dict:
    return next(a for a in run.actions if a["kind"] == "reply")


def test_the_acknowledgement_is_written_from_the_parts_the_agent_proposed_and_sent_once(factory, adapter):
    with factory() as s:
        run, _ = investigate(s, for_dana(s, whole(s)), text_of(s, "Dana Whitfield"), "request-reply-01", adapter=adapter)
        assert run.status == "done" and [a["kind"] for a in run.actions] == ["staff_review", "reply"]
        reply = reply_of(run)
        body = reply["body"]
        assert reply["outcome"] == "sent" and body.startswith("Hi Dana, we got your message from ") and body.endswith("Call the office if anything changes.")
        assert "Your account shows $0.00 owed on unit B-1042 as of " in body and "Unit B-1042 is listed for a lien sale on " in body
        assert "A staff member has an open task to review this." in body
        assert [p["kind"] for p in reply["parts"]] == ["message_received", "balance_as_of", "sale_status", "staff_review", "acknowledgment"]
        [sent] = texts_to(s, "Dana Whitfield")                                        # one text, through send_text, with its own checks
        assert sent.state == "sent" and sent.author == "agent" and sent.body == body and sent.request_id
        shown = run_view(run)["actions"][1]
        assert shown["label"] == "Reply accepted by the messaging system" and shown["tone"] == "done"
        assert {p["source"] for p in shown["parts"]} == {"the message", "the account", "the sale", "the FMS task", "a fixed line"}


def test_a_second_exploration_never_sends_another_text(factory, adapter):
    with factory() as s:
        dana = text_of(s, "Dana Whitfield")
        investigate(s, for_dana(s, whole(s)), dana, "request-reply-02", adapter=adapter)
        again, _ = investigate(s, for_dana(s, whole(s)), dana, "request-reply-03", again=True, adapter=adapter)
        assert reply_of(again)["outcome"] == "not_sent" and "answered already" in reply_of(again)["reason"]
        assert len(texts_to(s, "Dana Whitfield")) == 1


def test_a_number_that_texted_stop_is_blocked_by_the_send_and_the_row_says_why(factory, adapter):
    with factory() as s:
        marcus = text_of(s, "Marcus Bell")
        lease = s.scalar(select(Lease).where(Lease.provider_id == MARCUS_LEASE))
        model = Scripted([tool("get_account", id_="a1")], [tool("draft_reply", {"components": [part("message_received"), part("balance_as_of", f"lease:{lease.id}")]}, id_="a2")],
                         [tool("submit_case_packet", packet("stop_request", [], False, "", "Marcus asked to stop texts."), id_="a3")])
        run, _ = investigate(s, model, marcus, "request-reply-04", adapter=adapter)
        reply = reply_of(run)
        assert reply["outcome"] == "blocked" and "STOP" in reply["reason"]
        [row] = [r for r in texts_to(s, "Marcus Bell") if r.author == "agent"]      # the seeded blocked reply is the desk's own
        assert row.state == "blocked" and "STOP" in (row.reason or "")                 # send_text wrote the refusal; nothing went to the FMS
        assert run_view(run)["actions"][0]["label"] == "Reply blocked"


def test_a_message_the_desk_already_answered_is_not_answered_again(factory, adapter):
    with factory() as s:
        priya = text_of(s, "Priya Raman")
        lease = s.scalar(select(Lease).where(Lease.provider_customer_id == "cust_PriyaRmn01", Lease.unit_name == "A-012"))
        before = len(texts_to(s, "Priya Raman"))
        model = Scripted([tool("get_account", id_="a1")], [tool("draft_reply", {"components": [part("message_received"), part("balance_as_of", f"lease:{lease.id}")]}, id_="a2")],
                         [tool("submit_case_packet", packet("payment_status", [], True, "not answered by the records", "Priya asks about her plan."), id_="a3")])
        run, _ = investigate(s, model, priya, "request-reply-05", adapter=adapter)
        assert reply_of(run)["outcome"] == "not_sent" and "answered already" in reply_of(run)["reason"] and len(texts_to(s, "Priya Raman")) == before


def test_a_part_the_records_do_not_confirm_is_left_out_and_the_row_says_why(factory, adapter):
    with factory() as s:
        facts = dana_facts(s)
        components = [part("message_received"), part("payment_applied", facts["payment"]), part("balance_as_of", facts["lease"]), part("staff_review")]
        run, _ = investigate(s, for_dana(s, components, review=False), text_of(s, "Dana Whitfield"), "request-reply-06", adapter=adapter)
        reply = reply_of(run)
        assert reply["outcome"] == "sent" and "applied to your account" not in reply["body"] and "open task" not in reply["body"]
        assert {(d["label"], d["why"]) for d in reply["dropped"]} == {("Payment applied", "the desk has not confirmed that payment"),
                                                                      ("Staff review", "the FMS has not confirmed an open task")}
        assert "has not confirmed that payment" in str(run_view(run)["actions"][0]["dropped"])


def test_only_a_payment_the_desk_confirmed_fills_the_payment_part(factory, adapter):
    with factory() as s:
        facts = dana_facts(s)
        command = s.scalar(select(PaymentCommand).where(PaymentCommand.id == int(facts["payment"].split(":")[1])))
        command.state = "CONFIRMED"
        s.commit()
        run, _ = investigate(s, for_dana(s, [part("message_received"), part("payment_applied", facts["payment"])], review=False),
                             text_of(s, "Dana Whitfield"), "request-reply-07", adapter=adapter)
        assert reply_of(run)["outcome"] == "sent" and "Your payment of $240.00 has been applied to your account." in reply_of(run)["body"]


def test_a_proposal_with_nothing_confirmed_sends_nothing(factory, adapter):
    with factory() as s:
        facts = dana_facts(s)
        run, _ = investigate(s, for_dana(s, [part("message_received"), part("payment_applied", facts["payment"]), part("acknowledgment")], review=False),
                             text_of(s, "Dana Whitfield"), "request-reply-08", adapter=adapter)
        assert reply_of(run)["outcome"] == "not_sent" and "nothing the records confirm" in reply_of(run)["reason"]
        assert texts_to(s, "Dana Whitfield") == []


def test_a_record_the_lookups_never_returned_is_refused_at_the_tool(factory, adapter):
    with factory() as s:
        model = for_dana(s, [part("message_received"), part("balance_as_of", "lease:9999")], review=False)
        run, _ = investigate(s, model, text_of(s, "Dana Whitfield"), "request-reply-09", adapter=adapter)
        answer = model.calls[2]["messages"][-1]["content"][0]["content"]
        assert "needs the record_id" in answer
        assert run.status == "done" and run.actions == [] and texts_to(s, "Dana Whitfield") == []


def test_a_lost_answer_is_not_confirmed_and_a_second_exploration_does_not_send_again(factory, adapter, sim):
    with factory() as s:
        assert sim.post("/sim/faults", {"kind": "lose_send_response", "facilityId": ORLANDO}).status_code == 200
        dana = text_of(s, "Dana Whitfield")
        run, _ = investigate(s, for_dana(s, whole(s)), dana, "request-reply-10", adapter=adapter)
        assert reply_of(run)["outcome"] == "not_confirmed" and run_view(run)["actions"][1]["tone"] == "wait"
        [row] = texts_to(s, "Dana Whitfield")
        assert row.state == "outcome_unknown"
        again, _ = investigate(s, for_dana(s, whole(s)), dana, "request-reply-11", again=True, adapter=adapter)
        assert reply_of(again)["outcome"] == "not_sent" and len(texts_to(s, "Dana Whitfield")) == 1


def test_the_phrases_make_no_promise_no_legal_claim_and_every_part_fits_one_text():
    for kind, phrase in REPLY_PHRASES.items():
        text = f" {phrase.lower()} "
        assert not [w for w in ("paid in full", "cancel", " will ", "within", "hour", "legal", "compliant", "refund", "guarantee") if w in text], kind
        assert set(re.findall(r"\{(\w+)\}", phrase)) <= {"first", "when", "amount", "unit", "balance"}, kind        # only values the desk fills from a record
    longest = " ".join([REPLY_PHRASES["message_received"].format(first="Wolfgang", when="Sep 28 at 12:59 PM"),
                        REPLY_PHRASES["payment_applied"].format(amount="$99,999.99"),
                        REPLY_PHRASES["balance_as_of"].format(balance="$99,999.99", unit="B-1042", when="Sep 28"),
                        REPLY_PHRASES["sale_status"].format(unit="B-1042", when="Sep 28"),
                        REPLY_PHRASES["staff_review"], REPLY_PHRASES["acknowledgment"]])
    assert len(longest) <= SMS_LIMIT, len(longest)


def test_the_page_shows_each_sentence_with_the_record_it_came_from(fms_url, sim):
    model = Scripted()
    with TestClient(live_app(fms_url, model)) as client:
        with client.app.state.session_factory() as s:
            model.turns = for_dana(s, whole(s)).turns
        dana = message_ids(client)["Dana Whitfield"]
        assert click(client, dana, "visitor-click-reply", A).status_code == 200
        html = client.get(f"/inbox?text={dana}").text
        assert "Reply accepted by the messaging system" in html and "Hi Dana, we got your message from" in html     # the sentence, in the conversation and in the parts
        assert 'class="rec-kind">Balance<' in html and "from the account" in html and "from the sale" in html and "from a fixed line" in html and "from the FMS task" in html
        assert "Replied" in html
        for code in ("workitem:", "lease:", "payment:", "lease_", "NEEDS_STAFF"):
            assert code not in html, code


def test_the_tool_schemas_use_only_what_strict_tool_use_accepts():
    """The scripted model never reaches the API, so its schema is checked here: a keyword the API refuses fails every run at the first call."""
    from desk.investigator import TOOLS
    refused = {"maxItems", "minItems", "maxLength", "minLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in refused, f"{path}.{key}"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for n, value in enumerate(node):
                walk(value, f"{path}[{n}]")
    for tool_ in TOOLS:
        walk(tool_["input_schema"], tool_["name"])


def test_a_failure_after_the_text_went_out_is_recorded_as_sent_and_a_failure_before_keeps_the_proposal(factory, adapter):
    from types import SimpleNamespace
    from desk.reply import after_failure
    with factory() as s:
        dana = text_of(s, "Dana Whitfield")
        run, _ = investigate(s, for_dana(s, whole(s)), dana, "request-reply-12", adapter=adapter)
        inv = SimpleNamespace(inbound=dana, lookups=SimpleNamespace(reply_parts=[("message_received", ""), ("staff_review", "")]))
        late = after_failure(s, inv)                                                      # the text exists, so the record says so
        assert late["outcome"] == "sent" and late["body"] == reply_of(run)["body"] and late["proposed"] == [["message_received", ""], ["staff_review", ""]]
        early = after_failure(s, SimpleNamespace(inbound=text_of(s, "Priya Raman"), lookups=inv.lookups))
        assert early["outcome"] == "not_sent" and early["proposed"] == [["message_received", ""], ["staff_review", ""]]


def test_a_record_of_the_right_kind_but_the_wrong_shape_is_refused_at_the_tool(factory):
    from desk.investigator import Lookups, scope_of
    with factory() as s:
        dana = text_of(s, "Dana Whitfield")
        lookups = Lookups(s, scope_of(s, dana), dana)
        lookups.facts["payment:none:3"] = "The desk has no payment recorded on unit B-1042."     # a record a lookup returned, but not a payment attempt
        answer = lookups.draft_reply([part("message_received"), part("payment_applied", "payment:none:3")])
        assert "needs the record_id" in answer["error"] and lookups.reply_parts == []
