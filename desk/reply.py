"""The acknowledgement a tenant gets after the agent has looked at their text.

The agent only proposes which verified facts the text carries (draft_reply). The desk writes every sentence from its own catalog,
resolves each part from its records by the id the agent cited, leaves out any part it cannot confirm, checks the length, and sends the
text once through `send_text`. That function does the rest: it writes the intent first, refuses a number that texted STOP, looks again
just before the call, calls Cubby once, and never sends an unknown outcome again. Nothing a model writes reaches the tenant."""
from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .adapter import FMSError
from .liens import dollars
from .messaging import SMS_LIMIT, send_text
from .models import AgentRun, InboundMessage, Lease, LedgerSnapshot, OutboundMessage, PaymentCommand, WorkItem
from .sales import LISTED_KINDS
from .wording import REPLY_PART_LABELS, REPLY_PHRASES

log = logging.getLogger("desk.reply")

# the parts that rest on a record the agent must cite: the prefix of its id, and what the record is called in words
NEEDS_RECORD = {"payment_applied": ("payment:", "payment"), "balance_as_of": ("lease:", "account"), "sale_status": ("workitem:", "sale item")}
# a part with no sentence of its own for the facts: the opener is always first and the closing line always last
OPENER, CLOSING = "message_received", "acknowledgment"


@dataclass
class Part:
    kind: str
    text: str
    source: str            # what the sentence came from, written to follow the word "from"


def _at(when: datetime, zone: str) -> datetime:
    """A time the desk keeps in UTC, as the facility reads its clock."""
    return when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))


def _day(t: datetime) -> str:
    return f"{t:%b} {t.day}"


def _clock(t: datetime) -> str:
    return f"{_day(t)} at {t.hour % 12 or 12}:{t:%M} {t:%p}"


def _first_name(name: str | None) -> str:
    first = (name or "").split(" ")[0]
    return first if re.fullmatch(r"[A-Za-z][A-Za-z'\-]{0,29}", first) else "there"


def cited(kind: str, record_id: str, facts: dict) -> int | None:
    """The row number in a record id the agent cited, when it is the right kind of record and one this run's lookups returned."""
    prefix = NEEDS_RECORD[kind][0]
    if record_id not in facts or not re.fullmatch(re.escape(prefix) + r"\d+", record_id):
        return None
    return int(record_id[len(prefix):])


def resolve(session: Session, inv, kind: str, record_id: str) -> tuple[Part | None, str]:
    """One proposed part, written from the desk's own records, or None and why it was left out. Nothing the agent wrote is used: only the
    kind and the id it cited."""
    scope, inbound, run = inv.scope, inv.inbound, inv.row
    facility_zone = scope.facility.time_zone
    if kind == OPENER:
        when = _clock(_at(inbound.received_at, facility_zone))
        return Part(kind, REPLY_PHRASES[kind].format(first=_first_name(scope.name), when=when), "the message"), ""
    if kind == CLOSING:
        return Part(kind, REPLY_PHRASES[kind], "a fixed line"), ""
    said = {((run.packet or {}).get("reminder") or {}).get("kind"), ((run.verified or {}).get("reminder") or {}).get("kind")}
    if "wrong_number" in said:
        # whoever wrote this says it is not their number or account, so they may not be the tenant: no fact about the account goes to them, whether
        # or not the desk kept the agent's request (a quote that is not in the text refuses the request, not the reading)
        return None, "they say it is not their number or account"
    if kind == "staff_review":
        done = next((a for a in run.actions or [] if a.get("kind") == "staff_review" and a.get("outcome") in ("linked", "requested")
                     and a.get("task_status") == "OPEN"), None)
        if done is None:
            return None, "the FMS has not confirmed an open task"
        return Part(kind, REPLY_PHRASES[kind], "the FMS task"), ""
    number = cited(kind, record_id, inv.lookups.facts)
    if number is None:
        return None, "it did not cite a record this run read"
    if kind == "payment_applied":
        command = session.get(PaymentCommand, number)
        lease = session.get(Lease, command.lease_id) if command is not None else None
        if command is None or lease is None or lease.id not in {l.id for l in scope.leases}:
            return None, "that payment is not this tenant's"
        if command.state != "CONFIRMED":
            return None, "the desk has not confirmed that payment"
        return Part(kind, REPLY_PHRASES[kind].format(amount=dollars(command.amount)), "the desk's payment record"), ""
    if kind == "balance_as_of":
        lease = next((l for l in scope.leases if l.id == number), None)
        snap = session.scalar(select(LedgerSnapshot).where(LedgerSnapshot.lease_id == number).order_by(LedgerSnapshot.id.desc())) if lease else None
        if lease is None or snap is None:
            return None, "the desk has no ledger reading for that account"
        text = REPLY_PHRASES[kind].format(unit=lease.unit_name, balance=dollars(snap.total_due), when=_day(_at(snap.observed_at, facility_zone)))
        return Part(kind, text, "the account"), ""
    if kind == "sale_status":
        item = session.get(WorkItem, number)
        lease = next((l for l in scope.leases if item is not None and item.subject_id.endswith("/" + l.provider_id)), None)
        if item is None or lease is None or item.state != "open" or item.kind not in LISTED_KINDS or item.facility_id != scope.facility.id:
            return None, "that sale item is not open for this tenant"
        if (run.verified or {}).get("work_item_id") != item.id:
            return None, "the desk's own check did not confirm that sale"
        try:
            on = datetime.fromisoformat((item.observed or {}).get("auction_time") or "")
        except ValueError:
            return None, "the sale date could not be worked out"
        return Part(kind, REPLY_PHRASES[kind].format(unit=lease.unit_name, when=_day(on)), "the sale"), ""
    return None, "the desk cannot write that part"


def mark_step(session: Session, run: AgentRun, name: str) -> None:
    """Show a step the desk takes after the packet (reading a task back, checking and sending a reply) in the run's own record, so a page
    waiting for the run can show it as it happens."""
    run.trace = deepcopy((run.trace or []) + [{"turn": len(run.trace or []) + 1, "asked": [name], "calls": []}])
    session.commit()


def _not_sent(reason: str, parts: list[Part] | None = None, dropped: list[dict] | None = None, proposed: list | None = None) -> dict:
    return {"kind": "reply", "outcome": "not_sent", "reason": reason, "body": "", "parts": [vars(p) | {"label": REPLY_PART_LABELS[p.kind]} for p in parts or []],
            "dropped": dropped or [], "proposed": [list(p) for p in proposed or []]}


def after_failure(session: Session, inv) -> dict:
    """What to record when something failed while the reply was being prepared or sent. A text that did go out is found by its request id and
    recorded as the send left it, so the page never says "no reply" about a text the tenant got; the proposal is kept for whoever looks."""
    sent = session.scalar(select(OutboundMessage).where(OutboundMessage.request_id == f"agent-reply:{inv.inbound.id}"))
    if sent is None:
        return _not_sent("the reply could not be prepared", proposed=inv.lookups.reply_parts)
    return {"kind": "reply", "outcome": _outcome(sent), "reason": sent.reason or "", "body": sent.body, "outbound_id": sent.id, "parts": [], "dropped": [],
            "proposed": [list(p) for p in inv.lookups.reply_parts]}


def _outcome(sent: OutboundMessage) -> str:
    return {"sent": "sent", "blocked": "blocked", "rejected": "rejected"}.get(sent.state, "not_confirmed")


def answer(session: Session, adapter, inv) -> dict | None:
    """Write, check and send the acknowledgement the agent proposed, once the packet is accepted and the staff review is done. Returns what
    happened, as it is stored on the run, or None when the agent proposed nothing. One reply per message: a second exploration never sends
    another, whatever it proposes. The check for an earlier reply is made just before the send and cannot be atomic with a person typing a reply
    in the same instant; two agent runs cannot both send, because the request id is unique per message."""
    proposal = inv.lookups.reply_parts
    if not proposal:
        return None
    inbound: InboundMessage = inv.inbound
    earlier = session.scalar(select(OutboundMessage).where(OutboundMessage.inbound_id == inbound.id,
                                                           (OutboundMessage.author == "agent") | OutboundMessage.state.in_(("sent", "outcome_unknown"))))
    if earlier is not None:
        return _not_sent("this message was answered already" if earlier.state in ("sent", "outcome_unknown")
                         else f"an earlier reply was blocked: {earlier.reason or 'the rule said no'}", proposed=proposal)
    mark_step(session, inv.row, "check_reply")
    chosen, dropped = [], []
    for kind, record_id in [(OPENER, "")] + [p for p in proposal if p[0] != OPENER]:
        part, why = resolve(session, inv, kind, record_id)
        if part is None:
            dropped.append({"label": REPLY_PART_LABELS[kind], "why": why})
        else:
            chosen.append(part)
    facts = [p for p in chosen if p.kind not in (OPENER, CLOSING)]
    if not facts:
        return _not_sent("nothing the records confirm was worth telling the tenant", chosen, dropped, proposal)
    ordered = [p for p in chosen if p.kind == OPENER] + facts + [p for p in chosen if p.kind == CLOSING]
    body = " ".join(p.text for p in ordered)
    if len(body) > SMS_LIMIT:
        return _not_sent(f"the text would be {len(body)} characters, and a text may be {SMS_LIMIT}", ordered, dropped, proposal)
    mark_step(session, inv.row, "send_reply")
    try:
        sent = send_text(session, adapter, inv.scope.facility, inv.scope.customer_id, body, "agent", reply_to=inbound,
                         request_id=f"agent-reply:{inbound.id}")
    except (ValueError, FMSError) as exc:
        return _not_sent(f"nothing was sent: {exc}", ordered, dropped, proposal)
    return {"kind": "reply", "outcome": _outcome(sent), "reason": sent.reason or "", "body": body, "outbound_id": sent.id,
            "parts": [vars(p) | {"label": REPLY_PART_LABELS[p.kind]} for p in ordered], "dropped": dropped, "proposed": [list(p) for p in proposal]}
