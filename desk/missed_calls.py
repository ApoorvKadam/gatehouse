"""Missed calls: every caller the facility missed is worked to a callback, and a lead counts as won only when the FMS says so.

The calls come from the phone system's own log (the simulator plays it; Cubby documents no call events). Each caller is one work
item, however many times they rang. The caller is found by asking the FMS for customers with that number, then comparing whole
numbers here, because the FMS filter is a partial match. One customer: their open lead and their leases decide what the row says
(a current tenant may still want another unit, so their call is never written off). Several customers on one number: a person
decides who called. Nobody: an unknown caller. A withheld number is never searched for, because a partial search on nothing
could find everyone.

A person is asked to call back through an FMS task, made the one way every task here is made (intent first, a lost answer
looked up by its marker). Gatehouse creates no lead and no customer from a caller ID alone, and sends no text: a missed call
is neither confirmed details nor consent. An item closes only when a fresh reading shows the caller's lead CONVERTED,
UNQUALIFIED or DUPLICATE after the call; a completed task does not prove anyone spoke to the caller."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .access import ensure_task
from .adapter import FMSAdapter, FMSError
from .adapter.base import ProviderLead
from .db import utcnow
from .messaging import e164
from .models import Facility, WorkItem
from .wording import when as said_when

KIND = "missed_call"
HEAD = "Missed call, not yet returned"
WHO = "Front desk"
SOURCE = "the phone system's missed-call log, and customers/search and leads/search read fresh this pass"
PROOF = ("The caller's lead shows converted, unqualified or a duplicate in the FMS after the call. "
         "A completed task does not close it: it does not prove anyone spoke to the caller.")
# what would prove each kind of call done; a caller with no lead has no FMS fact that can, so a person records the outcome
PROOF_BY_KIND = {
    "lead": PROOF,
    "tenant": "A person records what came of the call back. Nothing in the FMS shows a call was returned, and a completed task does not close it.",
    "customer": "A person records what came of the call back. Nothing in the FMS shows a call was returned, and a completed task does not close it.",
    "unknown": "A person records what came of the call back. Nothing in the FMS shows a call was returned, and a completed task does not close it.",
    "shared": "A person picks which customer called; then the call back is asked for.",
    "withheld": "Nothing can: there is no number. It stays here for a person and is not counted on Today.",
    "short": "A person decides who called. A caller ID this short is never looked up.",
}
NOT_ON_TODAY = ("withheld", "short")       # no fact can prove these done, so they stay on the Missed calls page only
OPEN_LEAD = ("NEW", "ON_HOLD", "IN_PROGRESS_HOT", "IN_PROGRESS_COLD", "RESERVATION")
ENDED = {"CONVERTED": "Converted: the FMS shows a lease created for this lead",
         "UNQUALIFIED": "Closed in the FMS as unqualified",
         "DUPLICATE": "Closed in the FMS as a duplicate lead"}
TASK_TITLE = "Return a missed call"


def read_calls(feed_url: str | None, facility_provider_id: str) -> list[dict] | None:
    """The phone system's missed calls for a facility, or None when it is not configured or could not be read."""
    if not feed_url:
        return None
    try:
        r = httpx.get(f"{feed_url.rstrip('/')}/phone/v1/missed-calls", params={"facilityId": facility_provider_id}, timeout=5.0)
        body = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    data = body.get("data") if isinstance(body, dict) else None
    rows = data.get("calls") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    # a call without its id, a readable time, or a number written as text makes the whole log unreadable this pass: no caller is judged on half of it
    for c in rows:
        if not (isinstance(c, dict) and isinstance(c.get("callId"), str) and c["callId"] and _at(c.get("at")) is not None
                and (c.get("from") is None or isinstance(c.get("from"), str))):
            return None
    return rows


def _at(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def usable(phone: str | None) -> bool:
    """A caller ID with at least ten digits. A shorter one is never searched or compared: the FMS's partial search could find anyone
    whose number contains it, and a short value stored on a customer could match it exactly."""
    return len(re.sub(r"\D", "", phone or "")) >= 10


def phone_of(call: dict) -> str:
    """The caller's whole number; a short caller ID or a withheld one is kept apart under the call itself."""
    if usable(call.get("from")):
        return e164(call["from"])
    return f"short:{call['callId']}" if call.get("from") else f"withheld:{call['callId']}"


def caller_key(call: dict) -> str:
    """What groups calls into one item: the caller, so a caller who rang three times is one item with one task."""
    return phone_of(call)


def last4(number: str) -> str:
    return number[-4:]


@dataclass
class Who:
    kind: str                      # lead, tenant, customer, shared, unknown, withheld, short
    name: str | None = None
    customer: str | None = None
    lead: ProviderLead | None = None   # the caller's open lead, or one that has ended
    count: int = 0                 # customers on a shared number


def identify(adapter: FMSAdapter, facility: Facility, number: str, tenants: set[str], watch: str | None = None) -> Who:
    """Who called. `watch` is the lead an open item saw while it was open: that lead, whatever its status now, is the one the item's
    end is read from, so a different lead of the same customer can never close it."""
    if number.startswith("withheld:"):
        return Who("withheld")
    if number.startswith("short:"):
        return Who("short")
    exact = [c for c in adapter.customers_by_phone(facility.provider_id, number) if c.phone and e164(c.phone) == number]
    if len(exact) > 1:
        return Who("shared", count=len(exact))
    if not exact:
        return Who("unknown")
    c = exact[0]
    name = " ".join(x for x in (c.first_name, c.last_name) if x) or None
    leads = [x for x in adapter.leads_for_customer(c.provider_id) if x.facility_provider_id == facility.provider_id]
    watched = next((x for x in leads if watch and x.provider_id == watch), None)
    # the lead the item already watches stays its lead while it is open; a newer lead of the same customer never takes over
    lead = watched if watched is not None and watched.status in OPEN_LEAD else \
        next((x for x in sorted(leads, key=lambda x: x.updated_at or datetime.min, reverse=True) if x.status in OPEN_LEAD), None)
    if watched is not None and watched.status not in OPEN_LEAD:
        return Who("tenant" if c.provider_id in tenants else "customer", name, c.provider_id, watched)
    ended = None              # an ended lead the item never saw open says nothing about this call
    if lead is not None:
        return Who("lead", name, c.provider_id, lead)
    if c.provider_id in tenants:
        return Who("tenant", name, c.provider_id, ended)
    return Who("customer", name, c.provider_id, ended)


def label(who: Who, number: str) -> str:
    if who.kind == "withheld":
        return "Caller ID withheld"
    if who.kind == "short":
        return "Caller ID too short to look up"
    if who.kind == "shared":
        return f"Number ending {last4(number)}, on {who.count} customers"
    if who.kind == "unknown":
        return f"Unknown caller, number ending {last4(number)}"
    return who.name or f"Customer, number ending {last4(number)}"


def problem(who: Who, calls: list[dict], zone: str | None) -> str:
    first, n = calls[0], len(calls)
    rang = f"Missed {'a call' if n == 1 else f'{n} calls'}, the first on {said_when(_at(first['at']), zone)}."
    said = {"lead": "The caller has an open lead in the FMS, so the call is recorded against it.",
            "tenant": "The caller is a current tenant. What they want is not known; it may be another unit.",
            "customer": "The caller is a customer in the FMS with no open lead and no lease here.",
            "shared": f"This number is on {who.count} customers in the FMS, so Gatehouse does not pick one: a person decides who called.",
            "unknown": "The FMS has no customer with this number. Gatehouse creates no lead or customer from a caller ID alone.",
            "withheld": "The caller withheld their number, so there is no one to look up or call back.",
            "short": "The caller ID has fewer than ten digits, so Gatehouse looks nobody up with it: a person decides who called."}[who.kind]
    voicemail = next((c["voicemail"] for c in calls if c.get("voicemail")), None)
    return f"{rang} {said}" + (f' Voicemail: "{voicemail}"' if voicemail else " No voicemail.")


def task_for(who: Who, calls: list[dict], number: str, zone: str | None) -> tuple | None:
    """The callback a person is asked for, or None when there is no one to call (a withheld number) or no one to name (a shared number)."""
    if who.kind in ("withheld", "shared", "short"):
        return None
    voicemail = next((c["voicemail"] for c in calls if c.get("voicemail")), None)
    lines = [f"Missed call from {number} on {said_when(_at(calls[0]['at']), zone)}" + (f", and {len(calls) - 1} more since." if len(calls) > 1 else ".")]
    if voicemail:
        lines.append(f'Voicemail: "{voicemail}"')
    lines.append("Call back and record what they want. Do not text this number about offers: a missed call is not consent.")
    return (TASK_TITLE, "\n".join(lines), None, who.customer)


@dataclass
class CallsPass:
    read: int = 0
    opened: int = 0
    closed: int = 0
    failures: list[str] = field(default_factory=list)


def reconcile_missed_calls(session: Session, adapter: FMSAdapter, facility: Facility, run_id: str = "local", feed_url: str | None = None) -> CallsPass:
    """One pass over the phone system's missed calls. Safe to repeat: a caller's open item gains their later calls, never a second item or task."""
    result = CallsPass()
    calls = read_calls(feed_url, facility.provider_id)
    if calls is None:
        return result
    by_caller: dict[str, list[dict]] = {}
    for c in sorted(calls, key=lambda c: c.get("at") or ""):
        by_caller.setdefault(caller_key(c), []).append(c)
    try:
        tenants = {pl.customer_provider_id for pl in adapter.leases(facility.provider_id)}
    except FMSError as exc:
        result.failures.append(f"leases: {exc}")
        return result
    zone = facility.time_zone
    now = utcnow()
    for key, rows in by_caller.items():
        number = phone_of(rows[0])
        items = session.scalars(select(WorkItem).where(WorkItem.facility_id == facility.id, WorkItem.kind == KIND,
                                                       WorkItem.subject_type == "phone", WorkItem.subject_id == key).order_by(WorkItem.id)).all()
        handled = {cid for w in items for cid in (w.observed or {}).get("calls", [])}
        item = next((w for w in items if w.state == "open"), None)
        new = [c for c in rows if c["callId"] not in handled]
        if item is None and not new:
            continue                                    # every call from this number belongs to a closed item
        try:
            who = identify(adapter, facility, number, tenants, (item.observed or {}).get("lead") if item else None)
        except FMSError as exc:
            result.failures.append(f"caller {last4(number)}: {exc}")
            continue                                    # not read this pass: nothing opened or closed on a guess
        result.read += 1
        mine = [c for c in rows if c["callId"] in set((item.observed or {}).get("calls", [])) | {x["callId"] for x in new}] if item else new
        seen = (item.observed or {}) if item else {}
        if (item is not None and who.lead is not None and who.lead.status in ENDED and who.lead.provider_id == seen.get("lead")
                and seen.get("lead_status") in OPEN_LEAD and item.task_state not in ("sending", "outcome_unknown")):
            # the lead this item saw open on an earlier pass, after the call, now reads ended: the end came after the call. Calls that came
            # before the end stay with this item; a call after it is a new episode. A task whose answer is still outstanding keeps it open.
            ended_at = who.lead.converted_at if who.lead.status == "CONVERTED" else who.lead.unqualified_at if who.lead.status == "UNQUALIFIED" else None
            covered = [c for c in mine if ended_at is None or (_at(c["at"]) or now) <= ended_at] or mine
            item.observed = {**seen, "calls": [c["callId"] for c in covered]}
            item.state, item.closed_at, item.updated_at, item.verification = "closed", now, now, ENDED[who.lead.status]
            session.commit()
            result.closed += 1
            continue
        observed = dict(item.observed or {}) if item else {}
        observed.update(calls=[c["callId"] for c in mine], who=who.kind, label=label(who, number),
                        lead=getattr(who.lead, "provider_id", None), customer=who.customer)
        if who.lead is not None and who.lead.status in OPEN_LEAD:
            observed["lead_status"] = who.lead.status     # what the lead was while the call was open; an end after it closes the item
        if item is None:
            item = WorkItem(run_id=run_id, facility_id=facility.id, kind=KIND, subject_type="phone", subject_id=key,
                            episode_key=f"{key}#{len(items) + 1}", verification="FMS reported", who_acts=WHO, evidence_source=SOURCE,
                            closing_evidence=PROOF, problem="", observed=observed, opened_at=_at(mine[0]["at"]) or now, updated_at=now)
            session.add(item)
            result.opened += 1
        item.observed, item.problem, item.updated_at = observed, problem(who, mine, zone), now
        item.closing_evidence = PROOF_BY_KIND[who.kind]
        session.commit()
        ensure_task(session, adapter, facility, item, task_for(who, mine, number, zone), result.failures, keep_note=True)
    return result


def open_calls(session: Session, facility: Facility) -> list[WorkItem]:
    return session.scalars(select(WorkItem).where(WorkItem.facility_id == facility.id, WorkItem.kind == KIND, WorkItem.state == "open")
                           .order_by(WorkItem.opened_at)).all()


CHIPS = {"lead": "act", "shared": "act", "tenant": "wait", "customer": "wait", "unknown": "wait", "withheld": "plain", "short": "plain"}
WHO_WORDS = {"lead": "Open lead", "shared": "Number on several customers", "tenant": "Current tenant", "customer": "Customer, no open lead",
             "unknown": "Unknown caller", "withheld": "Number withheld", "short": "Caller ID too short"}


def task_words(item: WorkItem) -> str:
    """The callback task in a sentence, as the FMS last answered about it."""
    who = (item.observed or {}).get("who")
    if item.task_state == "created":
        status = (item.task_status or "OPEN").upper()
        if status == "COMPLETED":
            return "An FMS task asked staff to call back; it is marked completed, which does not close this row."
        return f"An FMS task asks staff to call back. It is {'open' if status == 'OPEN' else status.lower().replace('_', ' ')}."
    if item.task_state in ("sending", "outcome_unknown"):
        return "Creating the FMS task gave no usable answer yet; Gatehouse does not ask again while the outcome is unknown."
    if item.task_state == "rejected":
        return "The FMS refused the callback task, so nothing was created."
    if who == "shared":
        return "No task yet: a person first decides who called."
    if who == "withheld":
        return "No task: there is no number to call back."
    if who == "short":
        return "No task yet: a person first decides who called."
    return ""


def closed_calls(session: Session, facility: Facility) -> list[dict]:
    """Callers whose item closed on an FMS lead outcome, newest first: a conversion here is one the FMS reported, never one inferred."""
    rows = session.scalars(select(WorkItem).where(WorkItem.facility_id == facility.id, WorkItem.kind == KIND, WorkItem.state == "closed")
                           .order_by(WorkItem.closed_at.desc())).all()
    return [{"label": (w.observed or {}).get("label") or "A caller", "verification": w.verification, "closed_at": w.closed_at} for w in rows]
