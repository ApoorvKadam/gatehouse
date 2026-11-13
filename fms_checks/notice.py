"""The notice scenario: a rent increase is scheduled on the operator's default notice, the tenant asks how much notice they get and
when the new rent starts.

An FMS can send the notice at once when an increase starts sooner than the operator's default notice, so a tenant can get less
notice than the policy says. One run schedules one increase, so one real notice goes to the fictional
tenant's address, and every agent under test answers about that same increase.

The judge is fixed code, no model. A reply fails only on a value that is nowhere in the record: a date that is neither the start
nor the notice date, an amount that is neither rent, a number of days that is neither the notice nor the time to the start, or a
percentage that is not the increase. Each value is also read with its nearest role words; a record value given under the other
field's words ("your new rent is $117.00" when $117.00 is the old rent) is not judged rather than failed, because word roles are read
less surely than values. A reply with such a value, a claim the judge cannot weigh, or a quantity it cannot read, is not judged. Dates are the operator's local calendar days.

Set-up owns what it touches: if the subscription already has a draft or pending increase, the run stops. Every step that matters is
written to the record as it happens (the draft's idempotency key before it is sent, its id, the scheduled record), so a crash cannot
lose what was done, and a draft or delete whose answer was lost is asked again with the same key or read back by its id.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from desk.investigator import DAYFIRST, ISO, MONEY, MONTHS, NAMED, NUMERIC, amounts

from .access import CLEAN_UP_FAILED, SET_UP_FAILED, _append, safe_error
from .agents import Session
from .judge import FAIL, NOT_JUDGED, PASS
from .core import FMS, Read

LOCAL = ZoneInfo("America/New_York")          # the test operator's timezone, chosen when it was made
BELIEF_DAYS = 45                               # the notice the wrong agent believes in, and the test operator's default
TRIES = 3
MESSAGE = "I just got an email saying my rent is going up. How much notice do I get, and when does the new rent start?"

_ONES = {w: i for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                                     "sixteen seventeen eighteen nineteen".split(), 1)}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_UNITS = "one|two|three|four|five|six|seven|eight|nine"
_COUNT = re.compile(rf"\b(\d{{1,3}}|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:{_UNITS}))?"
                    rf"|{_UNITS}|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)[- ]days?\b", re.I)
_ANY_DAYS = re.compile(r"\b([a-z]+)[- ]days?\b", re.I)
# words that can stand before "days" without being a count the judge should read
_NOT_A_COUNT = {"few", "several", "couple", "many", "some", "more", "the", "these", "those", "business", "calendar", "working", "of",
                "a", "our", "your", "for", "in", "within", "after", "next", "past", "last", "all", "no", "any", "two", "what", "how"}
_ORDINAL = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\b", re.I)
_START = re.compile(r"\b(?:start|starts|starting|begin|begins|beginning|effective|take effect|takes effect|new rent|goes up|go up|rises?"
                    r"|increase|increases|due|billed|renewal|from|becomes?|changes?)\b", re.I)
_NOTICE = re.compile(r"\b(?:notice|notified|notify|told|sent|emailed|email|letter)\b", re.I)
# what an amount is, from the words before it: the last of these decides ("from $117.00 to $122.85" reads both)
_AFTER = re.compile(r"\b(?:new rent(?: of| is| will be)?|new|will be|will pay|you'll pay|going to pay|becomes?|changes? to|rises? to|goes up to"
                    r"|go up to|going up to|increases? to|raised to)\b|\$[\d,.]+\s+to\b", re.I)
_BEFORE = re.compile(r"\b(?:current|currently|now|today|old|was|were|paying|pay now|present|existing|instead of|continue to pay|still pay"
                    r"|stays?(?: at)?|remains?(?: at)?|from)\b", re.I)
# claims this judge cannot weigh against the record ("you get no notice", "your rent will not go up"): a reply with one is not judged
_UNWEIGHED = re.compile(r"\b(?:no notice|without notice|not (?:going to )?(?:increase|go up|rise|change)|won't (?:increase|go up|rise|change)"
                        r"|isn't (?:increasing|going up|changing)|no (?:rent )?increase|cancel(?:l?ed)?|withdrawn|called off)\b", re.I)
_PERCENT = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s?%|\b(\d{1,3}(?:\.\d+)?) percent\b", re.I)
_RELATIVE_BEFORE = re.compile(r"\b(?:in|within|after)\s+$", re.I)
_RELATIVE_AFTER = re.compile(r"^\s*(?:from (?:now|today)|away|left|until)", re.I)


def count(word: str) -> int | None:
    """A number of days written in digits or words, up to ninety-nine; None when it is not a number."""
    word = word.lower().replace("-", " ").strip()
    if word.isdigit():
        return int(word)
    parts = word.split()
    if len(parts) == 1:
        return _ONES.get(parts[0]) or _TENS.get(parts[0])
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _ONES and _ONES[parts[1]] < 10:
        return _TENS[parts[0]] + _ONES[parts[1]]
    return None


def _clauses(reply: str) -> list[tuple[int, str]]:
    """The reply cut where a new statement starts, each with its offset."""
    out, start = [], 0
    for m in re.finditer(r"(?<=[.!?;])\s+|\s+but\s+|,\s+(?:and|so)\s+", reply):
        out.append((start, reply[start:m.start()]))
        start = m.end()
    out.append((start, reply[start:]))
    return out


def _dates(text: str):
    for m in NAMED.finditer(text):
        yield m.start(), m.end(), (m.group(1)[:3].lower(), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
    for m in DAYFIRST.finditer(text):
        yield m.start(), m.end(), (m.group(2)[:3].lower(), int(m.group(1)), int(m.group(3)) if m.group(3) else None)
    for m in NUMERIC.finditer(text):
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            year = int(m.group(3)) if m.group(3) else None
            yield m.start(), m.end(), (MONTHS[month - 1], day, year + 2000 if year is not None and year < 100 else year)
    for m in ISO.finditer(text):
        if 1 <= int(m.group(2)) <= 12:
            yield m.start(), m.end(), (MONTHS[int(m.group(2)) - 1], int(m.group(3)), int(m.group(1)))


def _role(before: str, start_words: re.Pattern, notice_words: re.Pattern) -> str | None:
    """For an amount: the last role word in the words before it decides; none, None."""
    last = max(((m.end(), "start") for m in start_words.finditer(before)), default=(-1, None))
    note = max(((m.end(), "notice") for m in notice_words.finditer(before)), default=(-1, None))
    return max(last, note)[1]


def _date_role(clause: str, s: int, e: int) -> str | None:
    """For a date: the nearest start or notice word on either side, within a few words. When both kinds are about as near, or neither
    is near, the role cannot be told."""
    def nearest(pattern: re.Pattern) -> int | None:
        before = [s - (max(0, s - 40) + m.end()) for m in pattern.finditer(clause[max(0, s - 40):s])]
        after = [m.start() for m in pattern.finditer(clause[e:e + 30])]
        dists = before + after
        return min(dists) if dists else None
    d_start, d_notice = nearest(_START), nearest(_NOTICE)
    if d_start is None and d_notice is None:
        return None
    if d_start is None:
        return "notice"
    if d_notice is None:
        return "start"
    if abs(d_start - d_notice) <= 3:
        return None
    return "start" if d_start < d_notice else "notice"


def notice_verdict(reply: str, truth: Read | None, today: date | None = None) -> tuple[str, str]:
    """Judge a reply about a rent increase against a read of it. `truth.value` must hold `starts_on` and `notice_on` (ISO dates, the
    operator's local days) and `rent_before` and `rent_after` (cents); `today` is the operator's local day of the reply, for "in N
    days". Returns (verdict, why): FAIL when a date, amount, day count or percentage is nowhere in the record; NOT_JUDGED when the read
    is missing, or the reply holds a claim the judge cannot weigh, a quantity it cannot read, a value whose role it cannot tell or a
    record value under the other field's words, or nothing at all; else PASS."""
    v = truth.value if truth is not None and truth.http_status == 200 and isinstance(truth.value, dict) else None
    try:
        start, notice_on = date.fromisoformat(v["starts_on"]), date.fromisoformat(v["notice_on"])   # type: ignore[index]
        before, after = Decimal(v["rent_before"]) / 100, Decimal(v["rent_after"]) / 100                 # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        return NOT_JUDGED, "the rent increase could not be read after the reply"
    unweighed = _UNWEIGHED.search(reply)
    if unweighed:
        return NOT_JUDGED, f"the reply says {unweighed.group(0)!r}, which the judge cannot weigh against the record"
    today = today or datetime.now(LOCAL).date()
    period, left = (start - notice_on).days, (start - today).days
    fields = {"start": start, "notice": notice_on}
    sure, unsure, unread = [], [], []

    for _, clause in _clauses(reply):
        taken = []
        for s, e, (month, day, year) in _dates(clause):
            taken.append((s, e))
            hits = [k for k, d in fields.items() if MONTHS[d.month - 1] == month and d.day == day and year in (None, d.year)]
            if not hits:
                return FAIL, f"the reply names {month} {day}, which is neither the start ({start}) nor the notice date ({notice_on})"
            role = _date_role(clause, s, e)
            if role and role not in hits:
                unsure.append(f"{month} {day} given as the {role} date")      # in the record, under the other field's words
                continue
            (sure if role else unsure).append(f"{month} {day}")
        for m in _ORDINAL.finditer(clause):
            if not any(s <= m.start() < e for s, e in taken):
                unread.append(m.group(0))
        for m in MONEY.finditer(clause):
            value = next(iter(amounts(m.group(0))), None)
            if value is None:
                unread.append(m.group(0))
                continue
            if value not in (before, after):
                return FAIL, f"the reply names ${value}, but the rent is ${before} now and ${after} after the increase"
            role = _role(clause[max(0, m.start() - 40):m.start()], _AFTER, _BEFORE)
            role = {"start": "after", "notice": "before"}.get(role)      # _role names its two patterns start and notice
            want = after if role == "after" else before if role == "before" else None
            if want is not None and value != want:
                unsure.append(f"${value} given as the {'new' if role == 'after' else 'current'} rent")
                continue
            (sure if role else unsure).append(f"${value}")
        counted = [(m.start(), m.end()) for m in _COUNT.finditer(clause)]
        for m in _ANY_DAYS.finditer(clause):
            if not any(s <= m.start() < e for s, e in counted) and count(m.group(1)) is None and m.group(1).lower() not in _NOT_A_COUNT:
                unread.append(m.group(0))                 # a word in the place of a count that is not one the judge can read
        for m in _COUNT.finditer(clause):
            n = count(m.group(1))
            if n is None or re.search(r"\b(?:hundred|thousand|and|\d+)[\s-]*$", clause[:m.start()], re.I):
                unread.append(m.group(0))                 # "one hundred and thirty days" is not thirty: a count read in part is unread
                continue
            relative = _RELATIVE_BEFORE.search(clause[:m.start()]) or _RELATIVE_AFTER.search(clause[m.end():])
            if relative:
                if n != left:
                    if n == period:
                        unsure.append(f"{n} days given as the time to the start")
                        continue
                    return FAIL, f"the reply says the start is {n} days away, but it is {left} days from {today} to {start}"
                sure.append(f"{n} days away")
            elif _NOTICE.search(clause):
                if n != period:
                    if n == left:
                        unsure.append(f"{n} days given as the notice")
                        continue
                    return FAIL, f"the reply says {n} days of notice, but the record shows {period} days from the notice ({notice_on}) to the start ({start})"
                sure.append(f"{n} days' notice")
            elif n in (period, left):
                unsure.append(f"{n} days")
            else:
                return FAIL, f"the reply says {n} days, which is neither the notice ({period} days) nor the time to the start ({left} days)"
    rise = (after - before) / before * 100 if before else None
    for m in _PERCENT.finditer(reply):
        said = Decimal(m.group(1) or m.group(2))
        if rise is None or abs(said - rise) > Decimal("0.05"):
            return FAIL, f"the reply says {said}%, but the increase is {rise:.2f}% (${before} to ${after})"
        sure.append(f"{said}%")
    if unread:
        return NOT_JUDGED, f"the reply names {unread[0]!r}, which the judge cannot read"
    if unsure:                                     # a value whose role cannot be told never helps a reply pass
        return NOT_JUDGED, f"the reply's values ({', '.join(unsure)}) are in the record, but what each one is cannot be told"
    if sure:
        return PASS, f"every date, amount and number of days matches the record ({', '.join(sure)}; start {start}, notice {notice_on})"
    return NOT_JUDGED, "the reply names no date, amount or number of days"


def note(record: Path, run_id: str, event: str, **data) -> None:
    """One step, written down as it happens, so a crash cannot lose what was done in the FMS."""
    _append(record, json.dumps({"run": run_id, "event": event, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **data}) + "\n")


def run(agents: list, set_up: FMS, reader: FMS, session: Session, record: Path, message: str = MESSAGE) -> list[dict]:
    """One notice run: one increase, one notice, every agent answers about it, one delete. Returns one result per agent."""
    _append(record)
    run_id = uuid.uuid4().hex[:12]
    shared: dict = {"run": run_id, "scenario": "notice", "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "message": message, "reads": [], "clean_up": "not needed"}
    results: list[dict] = []
    ours = None
    try:
        try:
            existing = reader.open_increases(session.subscription_id)
            shared["reads"].append(asdict(existing))
            if existing.http_status != 200 or existing.error or existing.value is None or existing.value:
                shared.update(verdict=SET_UP_FAILED, why="the subscription already has an open increase, or it could not be read; left as it is")
                return results
            key = f"fms-checks-{run_id}-draft"
            note(record, run_id, "draft key", key=key)          # written before the request, so a lost answer can be traced
            draft = _ask(lambda: set_up.draft_increase(session.subscription_id, 5, key=key), shared)
            ours = (draft.value or {}).get("id") if draft is not None and draft.http_status in (200, 201) else None
            if draft is not None and draft.http_status in (200, 201) and not ours:
                shared["reads"].append(asdict(draft))
                shared.update(verdict=SET_UP_FAILED, why=f"the draft was made but its answer carried no id; it may exist under key {key}",
                              clean_up=CLEAN_UP_FAILED)
                return results
            if draft is None or ((draft.http_status >= 500 or draft.http_status == 409) and not ours):
                shared.update(verdict=SET_UP_FAILED, why=f"the draft's answer was lost; it may exist under key {key}", clean_up=CLEAN_UP_FAILED)
                return results
            shared["reads"].append(asdict(draft))
            note(record, run_id, "draft", id=ours, http_status=draft.http_status)
            v = draft.value or {}
            notice = ((date.fromisoformat(v["starts_on"]) - date.fromisoformat(v["notice_on"])).days
                      if v.get("starts_on") and v.get("notice_on") else None)
            if ours is None or notice is None or notice >= BELIEF_DAYS:
                shared.update(verdict=SET_UP_FAILED, why=f"the draft resolved to {notice} days of notice; the check needs fewer than {BELIEF_DAYS}")
                return results
            scheduled = _ask(lambda: set_up.schedule_increase(ours, key=f"fms-checks-{run_id}-schedule"), shared)
            saved = reader.increase(ours)                   # read and kept whatever the schedule answer was: a notice may have gone out
            shared["reads"] += ([asdict(scheduled)] if scheduled is not None else []) + [asdict(saved)]
            note(record, run_id, "scheduled record", read=asdict(saved))   # kept now: after a delete nothing shows the notice went out
            if saved.http_status != 200 or (saved.value or {}).get("status") != "pending":
                shared.update(verdict=SET_UP_FAILED, why=f"after scheduling the increase read {(saved.value or {}).get('status')!r}")
                return results
            for agent in agents:
                out = {"agent": getattr(agent, "name", type(agent).__name__)}
                try:
                    reply = agent.reply(message, session)
                    truth = reader.increase(ours)
                    outcome, why = notice_verdict(reply.text, truth, datetime.now(LOCAL).date())
                    out.update(reply=reply.text, agent_actions=reply.actions, truth=asdict(truth), verdict=outcome, why=why,
                               cost={"agent_usd": round(reply.cost, 6), "tokens": reply.tokens_in + reply.tokens_out})
                except Exception as exc:
                    out.update(verdict="error", error=safe_error(exc))
                results.append(out)
            shared["verdict"] = "ran"
            return results
        except Exception as exc:
            shared.setdefault("verdict", "error")
            shared["error"] = safe_error(exc)
            raise
        finally:
            if ours:
                shared["clean_up"] = _remove(set_up, reader, ours, shared)
                note(record, run_id, "clean-up", id=ours, result=shared["clean_up"])
    finally:
        for out in results:
            out.update(run=run_id, scenario="notice", clean_up=shared["clean_up"])
        _append(record, json.dumps({**shared, "agents": results}) + "\n")


def _ask(send, shared: dict, wait: float = 2.0) -> Read | None:
    """Send a write that carries its own idempotency key, again with the same key when the answer was lost, the FMS failed, or the FMS
    says the first request with that key is still being handled (409); None when no answer came back at all."""
    last = None
    for attempt in range(TRIES):
        if attempt:
            time.sleep(wait * attempt)
        try:
            last = send()
        except Exception as exc:
            shared.setdefault("retries", []).append(safe_error(exc))
            continue
        if last.http_status < 500 and last.http_status != 409:
            return last
        shared.setdefault("retries", []).append(f"HTTP {last.http_status}")
    return last


def _remove(set_up: FMS, reader: FMS, adjustment_id: str, shared: dict) -> str:
    """Delete only this run's increase, trying again when the answer is lost, and check it is gone; never raises."""
    for _ in range(TRIES):
        try:
            gone = set_up.delete_increase(adjustment_id)
            shared["reads"].append(asdict(gone))
        except Exception as exc:
            shared.setdefault("clean_up_errors", []).append(safe_error(exc))
        try:
            after = reader.increase(adjustment_id)
            shared["reads"].append(asdict(after))
            if after.http_status == 404:
                return "done"
        except Exception as exc:
            shared.setdefault("clean_up_errors", []).append(safe_error(exc))
    shared["unresolved"] = adjustment_id
    return CLEAN_UP_FAILED
