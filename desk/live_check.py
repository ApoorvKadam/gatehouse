"""The live access check on the Evaluation page: two sample agents answer a tenant's text about two fictional tenants on a real FMS
test account, and the desk judges each answer against a read of the unit taken after the answer.

It never writes to the FMS. Its credentials can read units and nothing else, so the two units are set up by hand once: one locked,
one open. A run reads both first and goes on only when that still holds; otherwise it says the check is not ready. The order of the
two units is drawn afresh each run. Each answer is followed by a fresh read of the unit; a read that differs from the one before the
answer stops the run without a verdict, because the record moved under it. Every model call, the agents' and the judge's, goes
through the desk's own counter, so the run counts in the day's budget and the visitor's allowance like any other run of our agent.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import asdict

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fms_checks.access import MESSAGE
from fms_checks.agents import AssumingAgent, CarefulAgent
from fms_checks.agents import Session as Tenant
from fms_checks.core import FMS, OPEN, RESTRICTED
from fms_checks.judge import DECLINED, FAIL, LATER, NOT_JUDGED, PASS, extract, verdict

from .investigator import CALL_TIMEOUT, MODEL, PaidRun, fail_stranded_runs, settle
from .models import AgentRun
from .spend import Refused
from .usage import PRICES

log = logging.getLogger("desk.live_check")

CHECK = "access"
UNIT_READ = "public.unit:read"          # the scope the page's credentials ask for: read units, nothing else
# the run's end, when it did not reach a verdict for every answer
NOT_READY, UNAVAILABLE, CHANGED, JUDGED = "not ready", "unavailable", "conditions changed", "judged"
_WORDS = {OPEN: "open", RESTRICTED: "locked"}
_VERDICTS = {PASS: "Right", FAIL: "Wrong", DECLINED: "Declined", LATER: "About later", NOT_JUDGED: "Not judged"}


class OverLimit(Exception):
    """The next model call could take the run past its cost limit."""


class CutOff(Exception):
    """The desk gave the run up as stranded while it was still going."""


class Off(Exception):
    """The database still carries the rule from before live checks existed, so it refuses the run's record."""


class Meter(PaidRun):
    """The model client the agents and the judge are handed: `messages.create` as the SDK has it, but every call reserves its worst
    cost against the run's limit first, is counted on the run's row the moment it answers, and beats the run's heartbeat."""

    @property
    def messages(self) -> "Meter":
        return self

    def create(self, **kwargs):
        if not self._beat():
            raise CutOff()
        kwargs.setdefault("timeout", CALL_TIMEOUT)
        price_in, price_out = PRICES[MODEL]
        shown = json.dumps([kwargs.get("system"), kwargs.get("tools"), kwargs.get("messages")], default=str)
        worst = ((len(shown.encode()) + 1024) * price_in + kwargs["max_tokens"] * price_out) / 1_000_000
        response = self._ask(kwargs, worst)
        if response is None:
            raise OverLimit()
        self._count(response, worst)
        self.session.commit()                  # a paid answer stays counted whatever happens next
        return response


def _said(why: str) -> str | None:
    """The reply's own words a verdict rests on: the judge quotes them first, in double quotes."""
    found = re.match(r'"(.*)"(?= but | matches |$| is about )', why, re.S)
    return found.group(1) if found else None


def _good(read) -> bool:
    return read.http_status == 200 and not read.error and read.value in (OPEN, RESTRICTED)


class LiveCheck(Meter):
    def __init__(self, session: Session, client, row: AgentRun, reader: FMS, units: tuple[str, str]):
        super().__init__(session, client, row)
        self.reader, self.units = reader, units

    def _step(self, text: str) -> None:
        self.row.trace = [*self.row.trace, {"step": text}]
        self.session.commit()

    def run(self) -> None:
        answers: list[dict] = []
        reads: list[dict] = []
        try:
            outcome = self._judge(answers, reads)
        except httpx.HTTPError as exc:
            log.warning("live check %s: the FMS could not be reached (%s)", self.row.id, type(exc).__name__)
            outcome = UNAVAILABLE
        except OverLimit:
            return self._end("needs_person", None, "the run reached its cost limit",
                             packet={"answers": [a["reply"] for a in answers]}, verified={"outcome": None, "answers": answers, "reads": reads})
        self._end("done", None, None, packet={"answers": [a["reply"] for a in answers]},
                  verified={"outcome": outcome, "answers": answers, "reads": reads})

    def _judge(self, answers: list[dict], reads: list[dict]) -> str:
        self._step("Reading both units on the FMS.")
        before = {u: self.reader.unit_access(u) for u in self.units}
        reads += [asdict(r) for r in before.values()]
        if not all(_good(r) for r in before.values()):
            return UNAVAILABLE
        if {r.value for r in before.values()} != {OPEN, RESTRICTED}:
            return NOT_READY
        agents = (CarefulAgent(self, self.reader), AssumingAgent(self))
        for unit in random.sample(self.units, 2):
            held = before[unit].value
            for agent in agents:
                self._step(f"Asking the agent that {agent.name} about the {_WORDS[held]} unit.")
                reply = agent.reply(MESSAGE, Tenant(contact_id="", unit_id=unit))
                truth = self.reader.unit_access(unit)              # read after the answer, apart from anything the agent read
                reads.append(asdict(truth))
                if not _good(truth):
                    return UNAVAILABLE
                if truth.value != held:
                    return CHANGED
                self._step(f"Checking that answer against the {_WORDS[held]} unit.")
                found = extract(self, reply.text)
                outcome, why = verdict(found.kept, truth, reply.text)
                answers.append({"unit": _WORDS[held], "agent": agent.name, "reply": reply.text, "verdict": outcome, "why": why,
                                "said": _said(why), "after": _WORDS[truth.value], "at": truth.at})
        return JUDGED


def _going(session: Session) -> AgentRun | None:
    return session.scalar(select(AgentRun).where(AgentRun.check_name == CHECK, AgentRun.status == "running"))


def latest(session: Session) -> AgentRun | None:
    """The newest run. A run whose process stopped is marked failed first, so a page never waits on it."""
    if fail_stranded_runs(session):
        session.expire_all()        # a run this session already holds must show the status the database now has
    return session.scalar(select(AgentRun).where(AgentRun.check_name == CHECK).order_by(AgentRun.id.desc()))


def start(session: Session, client, reader: FMS, units: tuple[str, str], visitor: str | None = None,
          allow=None) -> tuple[AgentRun, bool]:
    """Run the check once, inside this request. A run already going is returned to anyone who asks, so a double click costs one run.
    `allow` is asked before the run starts and again once it is written, with its id left out, as for a text or a voicemail."""
    fail_stranded_runs(session)
    going = _going(session)
    if going is not None:
        return going, False
    reason = allow(session, None) if allow is not None else None
    if reason:
        raise Refused(reason)
    row = AgentRun(check_name=CHECK, visitor=visitor, model=MODEL, effort="low")
    session.add(row)
    try:
        session.flush()             # the unique index decides a race between two requests here
        reason = allow(session, row.id) if allow is not None else None
        if reason:
            session.rollback()
            raise Refused(reason)
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = _going(session) or latest(session)
        if winner is None:          # no run at all, so the row broke the old rule on what a run is about
            raise Off("the live check is off on this database")
        return winner, False
    try:
        LiveCheck(session, client, row, reader, units).run()
    except CutOff:
        session.rollback()
    except Exception as exc:
        log.exception("live check %s failed", row.id)
        session.rollback()
        settle(session, row.id, status="failed", error=f"{type(exc).__name__}: the check did not finish")
    session.refresh(row)
    return row, True


def view(run: AgentRun | None) -> dict | None:
    """What the page shows of a run: its progress while it goes, then each answer with the words judged, the unit as read after the
    answer, and the verdict. No ids, no FMS request ids and no raw status words."""
    if run is None:
        return None
    checked = run.verified or {}
    steps = [t.get("step") for t in run.trace or [] if t.get("step")]
    out = {"id": run.id, "status": run.status, "step": steps[-1] if run.status == "running" and steps else None,
           "finished_at": run.finished_at, "outcome": checked.get("outcome"),
           "answers": [{"unit": a["unit"], "agent": a["agent"], "reply": a["reply"], "said": a["said"], "after": a["after"],
                        "verdict": _VERDICTS.get(a["verdict"], "Not judged")}
                       for a in (checked.get("answers", []) if checked.get("outcome") == JUDGED else [])]}
    out["note"] = {NOT_READY: "The two test units are not one locked and one open right now, so no answer was judged.",
                   UNAVAILABLE: "The FMS could not be read just now, so no answer was judged.",
                   CHANGED: "A unit changed while the check ran, so the run stopped without a verdict.",
                   None: "The check stopped before it finished." if run.status != "running" else None}.get(checked.get("outcome"))
    return out
