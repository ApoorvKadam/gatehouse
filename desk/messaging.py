"""Texts in and out of a facility.

Cubby sends each text a tenant writes to its SMS Received webhook, possibly more than once, so the desk keeps one copy per
message id. A text goes out through messages/sms/send, which takes a customer, not a number. Cubby documents no idempotency
key and no way to look a sent text up, and its documentation says nothing enforces a customer's preferences for an
integrator. So the desk decides who may be texted, writes the intent before it calls, calls once, and holds a text whose
answer never arrived for a person."""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, InboundText, NotFound, PermissionDenied, ProviderCustomer
from .cases import current_case
from .db import utcnow
from .models import AgentRun, Facility, InboundMessage, Lease, OutboundMessage

# the words a carrier acts on; a number that sent one of these is not texted until it sends a START word
STOP_WORDS = ("STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT")
START_WORDS = ("START", "UNSTOP", "YES")
# a text the desk starts is not sent from 9 PM to 8 AM at the facility
QUIET_FROM, QUIET_UNTIL = time(21, 0), time(8, 0)
# a tenant who texted in the last day has opened a conversation; an answer to it is not a text out of the blue
REPLY_WINDOW = timedelta(hours=24)
# what messages/sms/send accepts
SMS_LIMIT = 320
# a send still at the intent stage after this long was cut off before its outcome was written; the call itself times out sooner
STRANDED_AFTER = timedelta(minutes=2)


def e164(phone: str | None) -> str:
    """The whole number with its country code. A number written with a plus already has one; ten digits without a plus are
    a US or Canadian number. Two numbers that end the same way in different countries are two numbers."""
    text = (phone or "").strip()
    digits = re.sub(r"\D", "", text)
    return "+" + digits if text.startswith("+") or len(digits) != 10 else "+1" + digits


def last4(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")[-4:]


def keyword(body: str) -> str:
    """The one word a whole message consists of, ignoring case and the spaces and punctuation around it. 'Stop.' is STOP;
    'stop charging me' and 'YES2' are not keywords."""
    found = re.fullmatch(r"\W*([A-Za-z]+)\W*", body)
    return found.group(1).upper() if found else ""


def full_name(customer: ProviderCustomer | None) -> str | None:
    if customer is None:
        return None
    return " ".join(p for p in (customer.first_name, customer.last_name) if p) or None


# ---------------------------------------------------------------- inbound


def receive_text(session: Session, text: InboundText, routes: Mapping[str, str] | None, served: Collection[str]) -> InboundMessage | None:
    """Keep one text from Cubby's webhook. None when it is already kept. The facility is the one the webhook names, or the one
    `routes` gives for the receiving number (Cubby leaves it out when a number serves several facilities). A text is filed only
    under a facility in `served`. With no such facility, or one the desk has not read yet, the text is still kept, as unrouted,
    and filed later by file_unrouted. The caller has already checked the text belongs to this operator."""
    if session.scalar(select(InboundMessage.id).where(InboundMessage.provider_message_id == text.message_id)) is not None:
        return None
    named = text.facility_provider_id or {e164(k): v for k, v in (routes or {}).items()}.get(e164(text.to))
    facility = session.scalar(select(Facility).where(Facility.provider_id == named)) if named and named in served else None
    if facility is not None:
        note = None
    elif named and named in served:
        note = f"Facility {named} has not been read yet. The text is filed when it is."
    elif named:
        note = f"Facility {named} is not one this desk serves."
    else:
        note = f"No facility is set for the receiving number {text.to}; see DESK_TEXT_NUMBERS."
    # a time in the future is not believable, and the order of texts decides who has asked to stop: the desk's own clock is
    # the limit, so a text dated tomorrow can never outrank a STOP that really came after it
    received_at = min(text.received_at, utcnow())
    # only a facility the webhook itself names is kept for later. One that came from DESK_TEXT_NUMBERS is worked out again
    # each time, so correcting the setting can still file the text.
    row = InboundMessage(
        facility_id=facility.id if facility else None, facility_provider_id=text.facility_provider_id, provider_message_id=text.message_id,
        phone=text.sender, phone_key=e164(text.sender), to_number=text.to, body=text.body, received_at=received_at,
        match_state="pending" if facility else "unrouted", match_note=note,
        customer_provider_id=text.customer.provider_id if text.customer else None, customer_name=full_name(text.customer))
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()          # the same text was delivered a second time at the same moment
        return None
    return row


def file_unrouted(session: Session, routes: Mapping[str, str] | None, served: Collection[str]) -> int:
    """File the kept texts that have no facility yet: the desk has read the facility since, or the operator has since set
    a facility for the receiving number. Only a facility in `served` takes a text. A filed text is then matched like any other."""
    mapped = {e164(k): v for k, v in (routes or {}).items()}
    filed = 0
    for m in session.scalars(select(InboundMessage).where(InboundMessage.facility_id.is_(None), InboundMessage.match_state == "unrouted")):
        named = m.facility_provider_id or mapped.get(e164(m.to_number))
        facility = session.scalar(select(Facility).where(Facility.provider_id == named)) if named and named in served else None
        if facility is not None:
            m.facility_id, m.facility_provider_id, m.match_state, m.match_note = facility.id, facility.provider_id, "pending", None
            filed += 1
    session.commit()
    return filed


@dataclass(frozen=True)
class Match:
    state: str                      # matched, unmatched or ambiguous
    customer_id: str | None = None
    customer_name: str | None = None
    note: str | None = None


def decide(hint_id: str | None, hint_name: str | None, found: list[ProviderCustomer] | None, sender: str,
           hint_at_facility: bool = True) -> Match:
    """Who sent a text. Cubby's webhook names a tenant only when the number belongs to a customer with a lease, not
    necessarily at this facility, so the tenant must be a customer here (`hint_at_facility`), and a search of the facility by
    phone is the check on the number. `found` is None when the search was refused; customers that come back without a phone
    number mean the key cannot read them. The phone filter is a partial match, so only a customer whose whole number is the
    sender's counts."""
    if hint_id and not hint_at_facility:
        return Match("ambiguous", note="Cubby's webhook names a tenant who is not a customer at this facility. A person decides who it is.")
    if found is None or (found and all(c.phone is None for c in found)):
        if hint_id:
            return Match("matched", hint_id, hint_name, "Named by Cubby's webhook; this API key cannot search by phone to check it.")
        return Match("unmatched", note="This API key cannot read customers' phone numbers, so the sender cannot be matched.")
    same = {c.provider_id: c for c in found if c.phone and e164(c.phone) == e164(sender)}
    if len(same) > 1:
        return Match("ambiguous", note=f"{len(same)} tenants at this facility have this number on file. A person decides who it is.")
    if len(same) == 1:
        (found_id, person), = same.items()
        if hint_id and hint_id != found_id:
            return Match("ambiguous", note="Cubby's webhook names one tenant and a phone search finds another. A person decides who it is.")
        return Match("matched", found_id, full_name(person), None if hint_id else "Found by phone search; Cubby's webhook named nobody.")
    if hint_id:
        return Match("ambiguous", note="Cubby's webhook names a tenant, but no number on file at this facility is the sender's whole number. "
                                      "A person decides who it is.")
    return Match("unmatched", note="No tenant at this facility has this number on file.")


def match_pending(session: Session, adapter: FMSAdapter, facility: Facility) -> int:
    """Check every kept text that has not been checked. An FMS read that fails leaves the rest pending for the next pass."""
    pending = session.scalars(select(InboundMessage).where(InboundMessage.facility_id == facility.id,
                                                           InboundMessage.match_state == "pending").order_by(InboundMessage.id)).all()
    searched: dict[str, list[ProviderCustomer] | None] = {}
    here: dict[str, bool] = {}
    done = 0
    try:
        for m in pending:
            if m.phone_key not in searched:
                try:
                    searched[m.phone_key] = adapter.customers_by_phone(facility.provider_id, m.phone)
                except PermissionDenied:
                    searched[m.phone_key] = None
            if m.customer_provider_id and m.customer_provider_id not in here:
                here[m.customer_provider_id] = adapter.has_customer(facility.provider_id, m.customer_provider_id)
            found = decide(m.customer_provider_id, m.customer_name, searched[m.phone_key], m.phone,
                           here.get(m.customer_provider_id, True) if m.customer_provider_id else True)
            m.match_state, m.match_note = found.state, found.note
            m.customer_provider_id, m.customer_name = found.customer_id, found.customer_name
            done += 1
    finally:
        session.commit()   # what was checked before a failed read is kept
    return done


# ---------------------------------------------------------------- who may be texted


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


def stop_text(session: Session, phone: str) -> InboundMessage | None:
    """The text in which this number asked to stop, unless a later one asked to start again. It covers every facility
    this desk serves: a tenant who told one office to stop has told the operator. A STOP counts from any kept text, because
    refusing to text is the safe mistake. A START counts only from a text filed under a facility, so one that is waiting
    unrouted cannot lift a STOP."""
    newest_first = select(InboundMessage).where(InboundMessage.phone_key == e164(phone)).order_by(
        InboundMessage.received_at.desc(), InboundMessage.id.desc())
    for m in session.scalars(newest_first):
        word = keyword(m.body)
        if word in START_WORDS and m.facility_id is not None:
            return None
        if word in STOP_WORDS:
            return m
    return None


def can_text(session: Session, facility: Facility, customer: ProviderCustomer | None, now: datetime, *,
             reply_to: InboundMessage | None = None) -> Verdict:
    """May the desk text this customer now? Cubby texts the number it holds for the customer and the desk has to look at
    every preference, so the checks are here. `customer` is the tenant as the FMS shows them at this moment, or None when
    nobody is named. Gatehouse sends account messages and never promotions, so the marketing flag is not consulted.
    The carrier refuses a number that texted STOP; the desk's own record is a second layer, not the first."""
    if customer is None:
        return Verdict(False, "Cubby texts only a customer on file, so a person has to answer this number.")
    if not customer.phone:
        return Verdict(False, "This API key cannot read the tenant's phone number, so Gatehouse cannot tell where Cubby would send a text.")
    if reply_to is not None and reply_to.phone_key != e164(customer.phone):
        return Verdict(False, f"Cubby would text the number on file (ending {last4(customer.phone)}), "
                              f"not the number that texted (ending {last4(reply_to.phone)}).")
    stop = stop_text(session, customer.phone)
    if stop is not None:
        return Verdict(False, f"This number texted {keyword(stop.body)} on {stop.received_at:%b} {stop.received_at.day}. "
                              "It stays off until they text START.")
    sms = customer.sms
    if sms is None:
        return Verdict(False, "The FMS shows no text preferences for this tenant, so Gatehouse cannot tell whether texts are allowed.")
    if sms.suppressed:
        if sms.reason == "UNDELIVERABLE":
            return Verdict(False, "The FMS marks this number undeliverable. Someone should check it before it is texted again.")
        if sms.reason == "OPTED_OUT":
            return Verdict(False, "The FMS records this tenant as opted out of texts.")
        return Verdict(False, "The FMS has this tenant's texts switched off.")
    if reply_to is not None and now - reply_to.received_at <= REPLY_WINDOW:
        return Verdict(True)
    # a text the tenant did not just ask for
    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(facility.time_zone)).time()
    if local >= QUIET_FROM or local < QUIET_UNTIL:
        return Verdict(False, "Quiet hours at this facility: no text it starts goes out between 9 PM and 8 AM local time.")
    return Verdict(True)


# ---------------------------------------------------------------- outbound


def _same_request(earlier: OutboundMessage, facility: Facility, reply_to: InboundMessage | None, customer_provider_id: str | None,
                  body: str) -> OutboundMessage:
    """A request id names one text to one tenant about one message. The same id for anything else is a mistake, not a retry."""
    asked = (facility.id, reply_to.id if reply_to is not None else None, customer_provider_id, body)
    if (earlier.facility_id, earlier.inbound_id, earlier.customer_provider_id, earlier.body) != asked:
        raise ValueError("this request id was already used for a different text")
    return earlier


def send_text(session: Session, adapter: FMSAdapter, facility: Facility, customer_provider_id: str | None, body: str, author: str,
              *, reply_to: InboundMessage | None = None, request_id: str | None = None, now: datetime | None = None) -> OutboundMessage:
    """Decide, write down what is meant to go out, then call Cubby once. The row says what happened: sent, blocked by the
    rule (Cubby was never called), rejected by Cubby, or not known because no usable answer came back, in which case the
    desk never sends it again. A repeated request id returns the first row. Reading the tenant can raise FMSError, and then
    nothing has been written."""
    if not body.strip() or len(body) > SMS_LIMIT:
        raise ValueError(f"A text is 1 to {SMS_LIMIT} characters; this one is {len(body)}.")
    if request_id:
        earlier = session.scalar(select(OutboundMessage).where(OutboundMessage.request_id == request_id))
        if earlier is not None:
            return _same_request(earlier, facility, reply_to, customer_provider_id, body)
    customer = None
    if customer_provider_id:
        try:
            customer = adapter.customer(customer_provider_id)
        except (NotFound, PermissionDenied):
            customer = None
    verdict = can_text(session, facility, customer, now or utcnow(), reply_to=reply_to)
    row = OutboundMessage(
        facility_id=facility.id, inbound_id=reply_to.id if reply_to is not None else None, customer_provider_id=customer_provider_id,
        phone=customer.phone if customer is not None and customer.phone else (reply_to.phone if reply_to is not None else ""),
        body=body, author=author, request_id=request_id, state="intent" if verdict.ok else "blocked",
        reason=None if verdict.ok else verdict.reason)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()          # the same request id arrived twice at once
        return _same_request(session.scalars(select(OutboundMessage).where(OutboundMessage.request_id == request_id)).one(),
                             facility, reply_to, customer_provider_id, body)
    if not verdict.ok or customer is None:
        return row
    # a STOP can be stored while the intent is being written: look again, right before the call. The carrier refuses a
    # number that texted STOP in any case; this narrows the window on the desk's side to the moment before the call.
    again = can_text(session, facility, customer, now or utcnow(), reply_to=reply_to)
    if not again.ok:
        row.state, row.reason = "blocked", again.reason
        session.commit()
        return row
    outcome = adapter.send_sms(facility.provider_id, customer.provider_id, body)
    if outcome.kind == "sent":
        row.state, row.sent_at = "sent", utcnow()
    elif outcome.kind == "rejected":
        row.state, row.reason = "rejected", outcome.detail
    else:
        row.state = "outcome_unknown"
        row.reason = (f"No usable answer came back ({outcome.detail}). The text may have gone out, so Gatehouse does not send "
                      "it again. Check the conversation with this tenant in Cubby.")
    session.commit()
    return row


def hold_stranded(session: Session, now: datetime | None = None) -> int:
    """A send cut off between its intent and its outcome (the process stopped) may or may not have gone out. It is held as
    unknown, like a call that got no answer, so a person checks it instead of it staying an intent for good."""
    cutoff = (now or utcnow()) - STRANDED_AFTER
    rows = session.scalars(select(OutboundMessage).where(OutboundMessage.state == "intent", OutboundMessage.created_at < cutoff)).all()
    for row in rows:
        row.state = "outcome_unknown"
        row.reason = ("The desk stopped before it recorded what Cubby answered. The text may have gone out, so Gatehouse does not "
                      "send it again. Check the conversation with this tenant in Cubby.")
    session.commit()
    return len(rows)


# ---------------------------------------------------------------- what a screen or an API shows


def outbound_view(m: OutboundMessage) -> dict:
    return {"id": m.id, "body": m.body, "author": m.author, "state": m.state, "reason": m.reason,
            "created_at": m.created_at, "sent_at": m.sent_at}


def inbox(session: Session, facility: Facility, limit: int = 50, offset: int = 0) -> list[dict]:
    """The facility's texts, newest first, each with the tenant it was matched to, that tenant's leases and open cases,
    and what the desk sent back."""
    rows = session.scalars(select(InboundMessage).where(InboundMessage.facility_id == facility.id).order_by(
        InboundMessage.received_at.desc(), InboundMessage.id.desc()).limit(limit).offset(offset)).all()
    out = []
    for m in rows:
        leases = []
        if m.customer_provider_id:
            for lease in session.scalars(select(Lease).where(Lease.facility_id == facility.id,
                                                             Lease.provider_customer_id == m.customer_provider_id).order_by(Lease.id)):
                case = current_case(session, lease)
                leases.append({"lease_id": lease.provider_id, "unit": lease.unit_name,
                               "case_id": case.id if case else None, "case_state": case.state if case else None})
        replies = session.scalars(select(OutboundMessage).where(OutboundMessage.inbound_id == m.id).order_by(OutboundMessage.id)).all()
        run = session.scalar(select(AgentRun).where(AgentRun.inbound_id == m.id).order_by(AgentRun.id.desc()))
        out.append({"id": m.id, "from": m.phone, "body": m.body, "received_at": m.received_at,
                    "investigation": ({"id": run.id, "status": run.status, "intent": (run.packet or {}).get("intent"),
                                       "summary": (run.packet or {}).get("summary"), "needs_person": (run.packet or {}).get("needs_person"),
                                       "supported": (run.verified or {}).get("supported"), "cost_usd": round(run.cost_usd, 6)}
                                      if run is not None else None),
                    "match": {"state": m.match_state, "note": m.match_note, "customer_id": m.customer_provider_id,
                              "customer_name": m.customer_name, "leases": leases},
                    "stopped": stop_text(session, m.phone) is not None,
                    "replies": [outbound_view(r) for r in replies]})
    return out


def unrouted(session: Session, served: Collection[str], limit: int = 50, offset: int = 0) -> list[dict]:
    """Texts kept without a facility, so they are not lost while no facility is set for the number that received them. A text
    that named a facility the desk no longer serves stays in the database but is not listed."""
    rows = session.scalars(select(InboundMessage).where(
        InboundMessage.facility_id.is_(None),
        or_(InboundMessage.facility_provider_id.is_(None), InboundMessage.facility_provider_id.in_(list(served)))).order_by(
        InboundMessage.received_at.desc(), InboundMessage.id.desc()).limit(limit).offset(offset)).all()
    return [{"id": m.id, "from": m.phone, "to": m.to_number, "body": m.body, "received_at": m.received_at, "note": m.match_note}
            for m in rows]
