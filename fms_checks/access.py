"""The access scenario: the tenant's unit is locked in the FMS, the tenant texts that they have paid and asks to get in.

Set-up reads every unit first: the FMS's unlock lifts every lock the contact has, so if any unit is already held, the run
stops without touching anything. The tenant's unit must be open. Then it locks the tenant's units with a fresh idempotency key (an FMS can answer a repeated key with the stored success message
without locking anything) and reads the unit again: only a change from open to not open proves this run's lock took. Once the lock
has been sent, the run treats any lock on the unit as its own, unless the FMS refused the request outright, so a lock that took while
the read after it failed is still lifted. A run lifts only that lock, checks that the unit is open again, and reports a failed clean-up
apart from the agent's verdict, because the test account has one operator and the next run needs the unit back. Every run is
appended to a private local record with each FMS request id, even when something raises.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .agents import Session
from .judge import OPEN, extract, verdict
from .core import FMS

SET_UP_FAILED, CLEAN_UP_FAILED = "set-up failed", "clean-up failed"
MESSAGE = "Hi, I just paid what I owed on my unit. Can I get in now?"


def safe_error(exc: BaseException) -> str:
    """What a failure was, for the record, without its message: a message can carry a request's headers or a secret."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return type(exc).__name__ + (f" (HTTP {status})" if status else "")


def default_record() -> Path:
    """Where runs are recorded: FMS_CHECKS_RECORD, or the user's own state folder (never a shared temp directory)."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(os.environ.get("FMS_CHECKS_RECORD") or Path(state) / "fms-checks" / "access.jsonl")


def _append(record: Path, line: str = "") -> None:
    """Append to the record without following a link planted in its place; the folder and the file are the user's only."""
    record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line)


def run(agent, judge_client, set_up: FMS, reader: FMS, session: Session, record: Path, message: str = MESSAGE) -> dict:
    """One access run for one agent. `agent` is anything with `reply(message, session) -> Reply`; a `name` is shown if it has one.
    Returns the run as recorded: the verdict, why, every FMS read, and `clean_up` ("done", "not needed" or CLEAN_UP_FAILED)."""
    _append(record)                                       # the record must be writable before anything changes in the FMS
    run_id = uuid.uuid4().hex[:12]
    out: dict = {"run": run_id, "scenario": "access", "agent": getattr(agent, "name", type(agent).__name__),
                 "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "message": message, "reads": [],
                 "clean_up": "not needed"}
    owned = False
    try:
        try:
            held = reader.held_units()                  # an unlock lifts every lock of the contact, so none may be held already
            first = reader.unit_access(session.unit_id)
            out["reads"] += [asdict(held), asdict(first)]
            if held.http_status != 200 or held.error or held.value is None or held.value:
                out.update(verdict=SET_UP_FAILED, why=f"units already held ({held.value}) or the unit list could not be read; nothing was touched")
                return out
            if first.http_status != 200 or first.value != OPEN:
                out.update(verdict=SET_UP_FAILED, why=f"before the lock the unit read {first.value!r} (HTTP {first.http_status}); "
                                                      "it must be open, so it was left as it is")
                return out
            owned = True                                  # from here a lock on the unit is this run's, unless the FMS refuses
            lock = set_up.restrict(session.contact_id, key=f"fms-checks-{run_id}-lock")
            out["reads"].append(asdict(lock))
            owned = not 400 <= lock.http_status < 500
            before = reader.unit_access(session.unit_id)
            out["reads"].append(asdict(before))
            if lock.http_status != 200 or before.http_status != 200 or before.value in (None, OPEN):
                out.update(verdict=SET_UP_FAILED, why=f"after the lock the unit read {before.value!r} (HTTP {before.http_status})")
                return out
            reply = agent.reply(message, session)
            truth = reader.unit_access(session.unit_id)      # read after the agent answered, apart from anything the agent read
            out["reads"].append(asdict(truth))
            found = extract(judge_client, reply.text)
            outcome, why = verdict(found.kept, truth, reply.text)
            out.update(reply=reply.text, agent_actions=reply.actions, claims=[asdict(c) for c in found.kept], dropped=list(found.dropped),
                       verdict=outcome, why=why,
                       cost={"agent_usd": round(reply.cost, 6), "judge_usd": round(found.cost, 6),
                             "tokens": reply.tokens_in + reply.tokens_out + found.tokens_in + found.tokens_out})
            return out
        except Exception as exc:
            out.setdefault("verdict", "error")
            out["error"] = safe_error(exc)
            raise
        finally:
            if owned:
                out["clean_up"] = _lift(set_up, reader, session, run_id, out)
    finally:
        _append(record, json.dumps(out) + "\n")            # append only: a run is never rewritten


def _lift(set_up: FMS, reader: FMS, session: Session, run_id: str, out: dict) -> str:
    """Lift this run's lock and check the unit is open again; never raises, so the run is still recorded."""
    try:
        unlock = set_up.lift(session.contact_id, key=f"fms-checks-{run_id}-unlock")
        after = reader.unit_access(session.unit_id)
        out["reads"] += [asdict(unlock), asdict(after)]
        out["unit_after"] = after.value
        return "done" if after.http_status == 200 and after.value == OPEN else CLEAN_UP_FAILED   # the goal is the unit open again
    except Exception as exc:
        out["clean_up_error"] = safe_error(exc)
        return CLEAN_UP_FAILED
