"""The live access check: each answer is judged against a read of the unit taken after it, a run that cannot trust its reads gives
no verdict, every model call is counted in the day's budget and the visitor's allowance, and a database with the old rule says the
check is off. The FMS and the model are scripted; the desk, its ledger and its limits are the real ones."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateTable

from desk import live_check, spend
from desk.api import create_app
from desk.config import Settings
from desk.db import make_session_factory
from desk.models import AgentRun
from fms_checks.agents import ASSUMING, LOOK_UP
from fms_checks.core import OPEN, RESTRICTED, Read

from .conftest import FULL_KEY
from .test_investigator import TOKEN

UNITS = ("unit_locked", "unit_open")


class Reader:
    """A read-only FMS: the units' access as set, with an optional change after some reads, a refusal, or a dead connection."""

    def __init__(self, units: dict, change_after: int | None = None, status: int = 200, down: bool = False):
        self.units, self.change_after, self.status, self.down, self.reads = dict(units), change_after, status, down, 0

    def unit_access(self, unit_id):
        if self.down:
            raise httpx.ConnectError("no route")
        self.reads += 1
        if self.change_after is not None and self.reads > self.change_after:
            self.units = {u: OPEN for u in self.units}
        if self.status != 200:
            return Read("unit", self.status, None, "t", None, "unauthorized")
        return Read("unit", 200, f"req-{self.reads}", "2026-11-13T23:00:00+00:00", self.units[unit_id])


def _resp(*content):
    return SimpleNamespace(content=list(content), usage=SimpleNamespace(input_tokens=400, output_tokens=60), stop_reason="end_turn")


def _text(t):
    return SimpleNamespace(type="text", text=t)


class Model:
    """Both sample agents and the judge: the careful one looks the unit up and goes by it, the wrong one says come in, and the judge
    names the claim it finds."""

    def __init__(self):
        self.messages, self.calls = self, []

    def with_options(self, **_):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["max_tokens"] and kwargs["timeout"]                     # every call goes through the desk's counter
        if kwargs.get("tool_choice"):
            said = kwargs["messages"][0]["content"]
            claim = ({"kind": "can_enter", "quote": "you can come in now"} if "you can come in now" in said
                     else {"kind": "cannot_enter", "quote": "still locked"})
            return _resp(SimpleNamespace(type="tool_use", input={"claims": [claim]}))
        if kwargs["system"] == ASSUMING:
            return _resp(_text("Thanks! Your unit is unlocked and you can come in now."))
        assert kwargs["tools"] == [LOOK_UP]                                   # the careful agent can read the unit and nothing else
        last = kwargs["messages"][-1]["content"]
        if isinstance(last, str):
            return _resp(SimpleNamespace(type="tool_use", id="tu1", name="look_up_access", input={}))
        if "restricted" in last[0]["content"]:
            return _resp(_text("Your unit is still locked; staff will lift it once the payment shows."))
        return _resp(_text("Your unit is open, so you can come in now."))


@pytest.fixture
def session():
    with make_session_factory("sqlite:///:memory:")() as s:
        yield s


def test_each_answer_is_judged_against_a_read_after_it_and_counted(session):
    model, reader = Model(), Reader({"unit_locked": RESTRICTED, "unit_open": OPEN})
    run, started = live_check.start(session, model, reader, UNITS, visitor="v1")
    assert started and run.status == "done" and run.verified["outcome"] == live_check.JUDGED
    got = {(a["agent"], a["unit"]): (a["verdict"], a["after"]) for a in run.verified["answers"]}
    assert got == {("reads the record first", "locked"): ("pass", "locked"), ("reads the record first", "open"): ("pass", "open"),
                   ("assumes paying unlocks", "locked"): ("fail", "locked"), ("assumes paying unlocks", "open"): ("pass", "open")}
    assert reader.reads == 2 + 4 + 2                       # both units first, one read after each answer, and the careful agent's own
    assert run.turns == len(model.calls) == 2 * 2 + 2 + 4  # the careful agent twice per unit, the wrong one once, the judge each time
    assert run.cost_usd > 0 and spend.cases_today(session, "v1") == 1 and spend.spent_today(session) == pytest.approx(run.cost_usd)
    shown = live_check.view(run)
    assert {a["verdict"] for a in shown["answers"]} == {"Right", "Wrong"} and shown["note"] is None
    wrong = next(a for a in shown["answers"] if a["verdict"] == "Wrong")
    assert (wrong["said"], wrong["after"], wrong["unit"]) == ("Your unit is unlocked and you can come in now.", "locked", "locked")
    assert "req-" not in json.dumps(shown, default=str) and "unit_" not in json.dumps(shown, default=str)   # no FMS request ids and no unit ids on the page


@pytest.mark.parametrize("reader, outcome", [
    (Reader({"unit_locked": OPEN, "unit_open": OPEN}), live_check.NOT_READY),
    (Reader({"unit_locked": RESTRICTED, "unit_open": OPEN}, status=401), live_check.UNAVAILABLE),
    (Reader({"unit_locked": RESTRICTED, "unit_open": OPEN}, down=True), live_check.UNAVAILABLE),
    (Reader({"unit_locked": RESTRICTED, "unit_open": OPEN}, change_after=3), live_check.CHANGED),
])
def test_a_run_that_cannot_trust_its_reads_gives_no_verdict(session, reader, outcome):
    run, _ = live_check.start(session, Model(), reader, UNITS)
    shown = live_check.view(run)
    assert run.status == "done" and run.verified["outcome"] == outcome and shown["answers"] == [] and shown["note"]


def test_a_run_going_is_returned_and_the_budget_refuses_before_any_call(session):
    model, reader = Model(), Reader({"unit_locked": RESTRICTED, "unit_open": OPEN})
    going = AgentRun(check_name=live_check.CHECK, model="m", effort="low")
    session.add(going)
    session.commit()
    run, started = live_check.start(session, model, reader, UNITS)
    assert not started and run.id == going.id and not model.calls and reader.reads == 0
    going.beat_at = going.beat_at - timedelta(minutes=10)
    session.commit()
    assert live_check.latest(session).status == "failed"            # a run whose process stopped is not waited on
    with pytest.raises(spend.Refused):
        live_check.start(session, model, reader, UNITS, allow=lambda s, exclude: "the day's budget is used up")
    assert not model.calls and reader.reads == 0


def test_a_database_with_the_old_rule_says_the_check_is_off(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    engine = create_engine(url)
    ddl = str(CreateTable(AgentRun.__table__).compile(engine))
    new = next(c for c in AgentRun.__table__.constraints if c.name == "one_subject").sqltext.text
    with engine.begin() as c:
        c.exec_driver_sql(ddl.replace(new, "(inbound_id IS NULL) <> (work_item_id IS NULL)"))
    with make_session_factory(url)() as s:
        model = Model()
        with pytest.raises(live_check.Off):
            live_check.start(s, model, Reader({"unit_locked": RESTRICTED, "unit_open": OPEN}), UNITS)
        assert not model.calls


def test_the_route_runs_once_per_visitor_allowance_and_is_off_without_set_up(fms_url, sim):
    def app(**more):
        settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                            reconcile_every_seconds=60, operator_token=TOKEN, public_view=True, demo_seed=True, public_investigate=True,
                            visitor_cases_per_day=1, **more)
        return create_app(settings, start_worker=False, chat_client=Model(),
                          live_reader=Reader({"unit_locked": RESTRICTED, "unit_open": OPEN}) if more else None)
    with TestClient(app(live_check_units=UNITS)) as client:
        assert client.get("/api/live-check").json() == {"run": None}
        assert client.post("/api/live-check", headers={"origin": "https://elsewhere.example"}).status_code == 403
        first = client.post("/api/live-check")
        assert first.status_code == 200 and len(first.json()["answers"]) == 4
        assert client.get("/api/live-check").json()["run"]["id"] == first.json()["id"]
        assert client.post("/api/live-check").status_code == 429                  # the visitor's allowance is the one every run uses
        page = client.get("/evaluation").text
        assert "Checked against a real FMS" in page and "you can come in now" in page and "req-" not in page
    with TestClient(app()) as client:
        assert client.post("/api/live-check").status_code == 503
        assert "Checked against a real FMS" not in client.get("/evaluation").text
