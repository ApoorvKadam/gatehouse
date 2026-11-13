"""The voicemail reader: a part is kept only when the caller's own words for it stand in the voicemail and say that kind of thing, a
reading changes nothing anywhere, and it counts in the same budget, allowance and limit on runs at once as a text's exploration. The model
is scripted; the desk, the simulator and the limits are the real ones."""
from __future__ import annotations

from types import SimpleNamespace

import httpx

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from desk import spend, voicemail
from desk.missed_calls import KIND, TASK_TITLE, reconcile_missed_calls
from desk.models import AgentRun, Facility, WorkItem

from .conftest import ORLANDO

from .test_investigator import factory  # noqa: F401
from .test_live import A, live_app

ROSA = "+14075550199"
SAID = "Hi, my name's Rosa. I'm looking for a five by ten for about three months starting next week. Can someone call me back?"


class Reports:
    """A model stand-in that reports the same parts on every call."""

    def __init__(self, parts: dict):
        self.parts, self.messages, self.calls = parts, self, 0

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs["tools"] == [voicemail.TOOL] and kwargs["tool_choice"]["name"] == "report_voicemail"     # one tool, and it only reports
        return SimpleNamespace(stop_reason="tool_use", usage=SimpleNamespace(input_tokens=500, output_tokens=80),
                               content=[SimpleNamespace(type="tool_use", name="report_voicemail", input=dict(self.parts))])


ROSA_PARTS = {"reason": "I'm looking for a five by ten", "unit_size": "five by ten", "move_in": "starting next week",
              "question": "Can someone call me back?", "callback_preference": None}


def item(s, number: str) -> WorkItem:
    return s.scalar(select(WorkItem).where(WorkItem.kind == KIND, WorkItem.subject_id == number))


def test_a_part_is_kept_only_in_the_callers_words_and_of_its_kind():
    said = "Hi, I need a five by ten, maybe. You can call me back at 407 555 0178 after six."
    got = voicemail.check(said, {"reason": "i NEED a   five by ten", "unit_size": "ten by ten", "move_in": "I need a five by ten",
                                 "question": "I need a five by ten", "callback_preference": "call me back at 407 555 0178"})
    assert [(k["part"], k["quote"]) for k in got["kept"]] == [("reason", "I need a five by ten")]      # the voicemail's words, not the model's
    why = {d["part"]: d["why"] for d in got["dropped"]}
    assert why == {"unit_size": "the words are not in the voicemail", "move_in": "the words do not say when",
                   "question": "the words are not a question",
                   "callback_preference": "the words hold a phone number; the number to call is the one the phone system gave"}
    assert voicemail.check(said, {"reason": "need a fiv"})["dropped"][0]["why"] == "the words are not in the voicemail"   # whole words only
    assert voicemail.check(said, {"callback_preference": "after six"})["kept"][0]["quote"] == "after six"
    other = "May I ask for a small payment extension? Call me at four zero seven five five five zero one seven eight."
    loose = voicemail.check(other, {"unit_size": "small", "move_in": "May", "callback_preference": "Call me at four zero seven five five five zero one seven eight"})
    assert not loose["kept"]                    # a loose keyword is not a size or a day, and a number said in words is still a number
    assert voicemail.check("We want to move in in May.", {"move_in": "in May"})["kept"]


def test_a_reading_changes_nothing_and_counts_with_the_texts(factory):  # noqa: F811
    with factory() as s:
        rosa = item(s, ROSA)
        assert rosa.observed["voicemail"] == SAID
        before = (rosa.state, rosa.verification, rosa.who_acts, rosa.task_id, dict(rosa.observed))
        model = Reports(ROSA_PARTS)
        run, started = voicemail.read_voicemail(s, model, rosa, voicemail.digest(SAID), visitor="v1")
        assert started and run.status == "done" and model.calls == 1
        assert [k["part"] for k in run.verified["kept"]] == ["reason", "unit_size", "move_in", "question"]
        s.refresh(rosa)
        assert (rosa.state, rosa.verification, rosa.who_acts, rosa.task_id, dict(rosa.observed)) == before
        assert spend.cases_today(s, "v1") == 1 and spend.spent_today(s) > 0                        # one ledger for every run of our agent
        again, started = voicemail.read_voicemail(s, model, rosa, voicemail.digest(SAID))
        assert not started and again.id == run.id and model.calls == 1                              # a finished reading of the same words is returned
        with pytest.raises(voicemail.Stale):
            voicemail.read_voicemail(s, model, rosa, voicemail.digest(SAID + " "))
        going = AgentRun(work_item_id=rosa.id, voicemail_hash=voicemail.digest(SAID), generation=9, model="m", effort="low")
        s.add(going)
        s.commit()
        assert spend.running(s) == 1                                                                 # a reading going counts in the limit on runs at once
        assert voicemail.read_voicemail(s, model, rosa, voicemail.digest(SAID), again=True)[0].id == going.id   # a second click gets the one going
        going.voicemail_hash = "0" * 16
        s.commit()
        with pytest.raises(voicemail.Busy):     # a reading of older words is never handed back as the reading of these
            voicemail.read_voicemail(s, model, rosa, voicemail.digest(SAID))


def test_the_route_reads_once_for_a_visitor_and_refuses_what_it_cannot_read(fms_url, sim):
    model = Reports(ROSA_PARTS)
    with TestClient(live_app(fms_url, model, visitor_cases_per_day=1)) as client:
        with client.app.state.session_factory() as s:
            rosa, dana = item(s, ROSA).id, item(s, "+14075550142").id
        path = f"/api/missed-calls/{rosa}/voicemail-reading"
        assert client.get(path).json() == {"reading": None}
        first = client.post(path, json={"voicemail": voicemail.digest(SAID)}, headers=A)
        assert first.status_code == 200 and first.json()["status"] == "done" and len(first.json()["kept"]) == 4
        assert client.get(path).json()["reading"]["id"] == first.json()["id"]
        over = client.post(path, json={"voicemail": voicemail.digest(SAID)}, headers=A)
        assert over.status_code == 429 and model.calls == 1                                          # the visitor's allowance is the same one
        stale = client.post(path, json={"voicemail": "0" * 16}, headers={"cf-connecting-ip": "203.0.113.50"})
        assert stale.status_code == 409
        none = client.post(f"/api/missed-calls/{dana}/voicemail-reading", json={"voicemail": "0" * 16}, headers={"cf-connecting-ip": "203.0.113.51"})
        assert none.status_code == 404                                                               # no voicemail, nothing to read
        assert client.get(f"/api/agent-runs/{first.json()['id']}").status_code == 404                # a reading is not a text's run


def test_the_calls_page_shows_what_the_caller_stated_and_what_was_not_kept(fms_url, sim):
    model = Reports({**ROSA_PARTS, "callback_preference": "call me at 407 555 0199"})
    with TestClient(live_app(fms_url, model)) as client:
        with client.app.state.session_factory() as s:
            rosa = item(s, ROSA).id
        before = client.get("/calls-and-reminders").text
        assert before.count("Read the voicemail with our agent") == 2              # Rosa and Jonah left voicemails; nobody else did
        client.post(f"/api/missed-calls/{rosa}/voicemail-reading", json={"voicemail": voicemail.digest(SAID)}, headers=A)
        page = client.get("/calls-and-reminders").text
        assert "What the caller stated" in page and page.count("Caller stated</span>") == 4 and "Read again" in page
        assert "&#34;five by ten&#34;" in page or '"five by ten"' in page
        assert "Not kept, how or when to call back" in page and "The words are not in the voicemail." in page
        assert "workitem:" not in page and "claude" not in page.lower() and "haiku" not in page.lower()


def test_a_later_voicemail_is_the_one_shown_and_read_and_asks_for_no_second_task(factory, adapter, fms_url):  # noqa: F811
    with factory() as s:
        rosa = item(s, ROSA)
        voicemail.read_voicemail(s, Reports(ROSA_PARTS), rosa, voicemail.digest(SAID))
        tasks = lambda: [t for t in adapter.tasks_for("facilityId", ORLANDO) if t.title == TASK_TITLE]
        before = len(tasks())
        later = "It's Rosa again. Actually a ten by ten would be better. Text me after five."
        httpx.post(f"{fms_url}/sim/phone/missed-call", json={"facilityId": ORLANDO, "from": ROSA, "voicemail": later}, timeout=5).raise_for_status()
        reconcile_missed_calls(s, adapter, s.scalar(select(Facility).where(Facility.provider_id == ORLANDO)), "local", fms_url)
        s.refresh(rosa)
        assert rosa.observed["voicemail"] == later and later in rosa.problem and SAID not in rosa.problem
        assert len(tasks()) == before                                                         # the same caller keeps one call back task
        assert voicemail.latest_reading(s, rosa) is None                                       # the reading of the older words is not shown as this one
        run, _ = voicemail.read_voicemail(s, Reports({"reason": None, "unit_size": "a ten by ten", "move_in": None, "question": None,
                                                     "callback_preference": "Text me after five"}), rosa, voicemail.digest(later))
        assert [k["part"] for k in run.verified["kept"]] == ["unit_size", "callback_preference"]


def test_a_paid_answer_keeps_its_cost_when_the_check_fails(factory, monkeypatch):  # noqa: F811
    def broken(said, raw):
        raise RuntimeError("the check failed")
    monkeypatch.setattr(voicemail, "check", broken)
    with factory() as s:
        run, _ = voicemail.read_voicemail(s, Reports(ROSA_PARTS), item(s, ROSA), voicemail.digest(SAID), visitor="v2")
        assert run.status == "failed" and run.cost_usd > 0 and run.input_tokens == 500   # the answer was paid for, so it stays counted
        assert spend.spent_today(s) >= run.cost_usd


def test_a_callback_part_needs_a_way_or_a_time_and_no_number():
    said = "Can someone call me back? Call me at 0178. Text me after five, or call my cell."
    got = voicemail.check(said, {"callback_preference": "Can someone call me back?"})
    assert got["dropped"][0]["why"] == "the words do not say how or when to call back"
    assert voicemail.check(said, {"callback_preference": "Call me at 0178"})["dropped"][0]["why"].startswith("the words hold a phone number")
    assert voicemail.check(said, {"callback_preference": "Text me after five"})["kept"][0]["quote"] == "Text me after five"
    assert voicemail.check(said, {"callback_preference": "call my cell"})["kept"]
