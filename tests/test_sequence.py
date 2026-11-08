"""The steps one text went through: what the Messages page shows for each message, in the order it happened."""
from __future__ import annotations

from datetime import datetime

from desk.sequence import _first, sequence


def message(**more) -> dict:
    base = {"id": 1, "from": "+15550100", "body": "I paid on Friday. Why is my unit still on the sale list?", "received_at": datetime(2026, 11, 8, 15, 20),
            "match": {"state": "matched", "customer_name": "Dana Whitfield", "leases": [{"unit": "B-1042"}]}, "stopped": False,
            "investigation": None, "replies": []}
    return {**base, **more}


def run(status: str = "done", **more) -> dict:
    base = {"id": 7, "status": status, "started_at": datetime(2026, 11, 8, 15, 21), "finished_at": datetime(2026, 11, 8, 15, 21, 8),
            "notes": {"label": "Case notes", "intent": "sale_after_payment", "needs_person": True,
                      "summary": "Dana says she paid. Her unit is still on the sale list. A person has to ask for the sale to be cancelled.",
                      "reason": "The sale is still listed with nothing owed. A person has to act."},
            "records": [], "verified": {"checked": True, "sale_listed_nothing_owed": True, "says": "The FMS ledger shows nothing owed on unit B-1042."},
            "actions": []}
    return {**base, **more}


REVIEW = {"kind": "staff_review", "label": "Existing FMS task linked", "tone": "done", "text": "The FMS already had a task.", "at": datetime(2026, 11, 8, 15, 21, 9)}
REPLY = {"kind": "reply", "label": "Reply accepted by the messaging system", "tone": "done", "text": "", "body": "Hi Dana, we got your message.",
         "parts": [{"kind": "message_received", "label": "Message received", "text": "Hi Dana, we got your message.", "source": "the message"}], "dropped": []}
NOT_SENT = {"kind": "reply", "label": "No reply sent", "tone": "plain", "text": "Nothing the records confirm could be said.", "body": "", "parts": [], "dropped": []}


def keys(steps: list[dict]) -> list[str]:
    return [s["key"] for s in steps]


def test_a_message_nobody_has_explored_shows_the_steps_that_wait():
    steps = sequence(message())
    assert keys(steps) == ["received", "read", "check", "review", "reply"]
    assert [s["state"] for s in steps] == ["done", "pending", "pending", "pending", "pending"]
    assert steps[0]["line"] == "From Dana Whitfield, matched through the FMS to unit B-1042." and steps[0]["bubble"] == {"side": "in", "text": message()["body"]}
    assert steps[0]["at"] == datetime(2026, 11, 8, 15, 20) and all(s["at"] is None for s in steps[1:])        # a waiting step has no time


def test_a_number_that_sent_stop_is_told_nothing_will_be_sent():
    steps = sequence(message(stopped=True))
    assert keys(steps)[-1] == "reply" and steps[-1]["title"] == "No reply will be sent" and steps[0]["chip"] == "Texting stopped"
    assert "A short reply to the tenant" not in str(steps)


def test_a_text_from_a_stranger_has_only_its_own_step():
    steps = sequence(message(match={"state": "none", "customer_name": None, "leases": []}))
    assert keys(steps) == ["received"] and "No tenant was found for this number" in steps[0]["line"]
    assert sequence(message(match={"state": "ambiguous", "customer_name": None, "leases": []}))[0]["tone"] == "wait"


def test_a_finished_run_reads_in_order_with_the_reply_and_where_each_sentence_came_from():
    sent = {"author": "agent", "state": "sent", "body": REPLY["body"], "reason": None, "created_at": datetime(2026, 11, 8, 15, 21, 10),
            "sent_at": datetime(2026, 11, 8, 15, 21, 11)}
    steps = sequence(message(investigation=run(actions=[REVIEW, REPLY]), replies=[sent]))
    assert keys(steps) == ["received", "read", "check", "review", "reply"] and all(s["state"] == "done" for s in steps)
    read, check, review, reply = steps[1:]
    assert read["chip"] == "A person has to act" and read["tone"] == "act" and read["line"] == "The sale is still listed with nothing owed."   # one sentence, the rest is under the sequence
    assert check["chip"] == "The desk confirms the sale" and check["at"] == read["at"] == datetime(2026, 11, 8, 15, 21, 8)
    assert review["chip"] == "Existing FMS task linked" and review["at"] == datetime(2026, 11, 8, 15, 21, 9)
    assert reply["parts"] == REPLY["parts"] and reply["bubble"] == {"side": "out", "text": REPLY["body"]} and reply["at"] == sent["sent_at"]
    assert "The agent wrote no word of it" in reply["line"]


def test_a_reply_a_person_sent_before_the_run_comes_right_after_the_message():
    earlier = {"author": "staff", "state": "sent", "body": "Hi Dana, yes, we saw it.", "reason": None, "created_at": datetime(2026, 11, 8, 15, 20, 30), "sent_at": None}
    steps = sequence(message(investigation=run(actions=[NOT_SENT]), replies=[earlier]))
    assert keys(steps) == ["received", "reply", "read", "check", "reply"]
    assert steps[1]["line"] == "A person at the desk sent it." and steps[1]["chip"] == "Sent" and steps[1]["bubble"]["text"] == earlier["body"]
    assert steps[-1]["chip"] == "No reply sent" and steps[-1]["line"] == NOT_SENT["text"]


def test_a_text_a_person_answered_without_the_agent_shows_the_answer_and_no_waiting_steps():
    blocked = {"author": "staff", "state": "blocked", "body": "Hi", "reason": "This number texted STOP.", "created_at": datetime(2026, 11, 8, 15, 30), "sent_at": None}
    steps = sequence(message(stopped=True, replies=[blocked]))
    assert keys(steps) == ["received", "reply"] and steps[1]["chip"] == "Blocked" and steps[1]["line"] == "This number texted STOP." and steps[1]["tone"] == "act"


def test_a_run_still_going_adds_nothing_after_the_message_and_a_failed_one_says_so():
    assert keys(sequence(message(investigation=run("running", notes={"summary": None})))) == ["received"]
    failed = sequence(message(investigation=run("failed", notes={"summary": None})))
    assert keys(failed) == ["received", "read"] and failed[1]["title"] == "Our agent could not finish" and failed[1]["chip"] == "No case notes"


def test_no_step_carries_an_id_or_a_state_code():
    steps = sequence(message(investigation=run(verified={"checked": True, "sale_listed_nothing_owed": False,
                                                       "says": "The desk's sale item (workitem:4) for unit B-1042 is NEEDS_STAFF."}, actions=[REVIEW, REPLY])))
    text = str(steps)
    assert "workitem:" not in text and "NEEDS_STAFF" not in text


def test_the_first_sentence_keeps_a_step_to_one_thing():
    assert _first("One thing. Another thing.") == "One thing." and _first("") == "" and _first(None) == ""
    assert _first("No end of sentence here") == "No end of sentence here"
    assert _first("word " * 100).endswith("...") and len(_first("word " * 100)) <= 224


def test_a_step_that_is_hidden_stays_hidden():
    """`.step` sets its own display, which would show the waiting step (hidden until a run starts) on every page; the style must say so."""
    from pathlib import Path
    css = (Path(__file__).parent.parent / "desk" / "static" / "gatehouse.css").read_text()
    assert ".step[hidden] { display: none; }" in css


def test_a_copy_where_the_agent_is_off_shows_no_waiting_steps():
    assert keys(sequence(message(), explorable=False)) == ["received"]


def test_a_reply_an_earlier_exploration_sent_is_not_hidden_by_a_later_one_that_sent_nothing():
    """A message is answered once. Exploring it again proposes nothing, and the page must still say the tenant got the first text."""
    sent = {"author": "agent", "state": "sent", "body": REPLY["body"], "reason": None, "created_at": datetime(2026, 11, 8, 15, 21, 10),
            "sent_at": datetime(2026, 11, 8, 15, 21, 11)}
    later = run(actions=[NOT_SENT], started_at=datetime(2026, 11, 8, 15, 40), finished_at=datetime(2026, 11, 8, 15, 40, 7))
    steps = sequence(message(investigation=later, replies=[sent]))
    assert keys(steps) == ["received", "reply", "read", "check", "reply"]
    first, last = steps[1], steps[-1]
    assert first["chip"] == "Sent" and first["bubble"] == {"side": "out", "text": REPLY["body"]} and first["at"] == sent["sent_at"]
    assert last["chip"] == "No reply sent" and last["bubble"] is None
