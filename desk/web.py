"""The operator screens of the hosted preview: Today, an account's record, a payment under review and a lien file.

Every page reads Gatehouse's own records and the simulator trace saved when the world was built. No page calls the
FMS or writes anything, so a visitor can read freely without moving the world."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    CaseEvent, DelinquencyCase, DeskBinding, Facility, IdentityCheck, Lease, LedgerSnapshot, LienEvaluation, LienEvidence,
    PaymentCommand, PaymentProposal,
)
from .payments import tenant_message

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

# The four kinds of entry an account's record keeps apart. Every event kind lands in exactly one of them.
FMS, OBSERVED, CONCLUDED, DECIDED = "fms", "observed", "concluded", "decided"
LANES = [(FMS, "The FMS said"), (OBSERVED, "Gatehouse observed"), (CONCLUDED, "Gatehouse concluded"), (DECIDED, "A person decided")]
LANE_OF = {
    "case opened": FMS, "payment taken outside the desk": FMS, "payment declined": FMS,
    "payment status changed after success": FMS,
    "payment submitted": OBSERVED, "fms read failed": OBSERVED, "possible match for staff": OBSERVED,
    "identity checked": OBSERVED, "tenant said": OBSERVED,
    "state changed": CONCLUDED, "payment confirmed": CONCLUDED, "payment needs staff": CONCLUDED,
    "payment needs a new authorization": CONCLUDED, "lien file evaluated": CONCLUDED, "verification expired": CONCLUDED,
    "handed to staff": CONCLUDED, "desk replied": CONCLUDED,
    "payment resolved by staff": DECIDED, "lien evidence recorded": DECIDED,
}
# payments that still hold the lease, and what each is waiting for
IN_FLIGHT = {"SUBMITTING": "being sent to the FMS", "PENDING_AT_FMS": "pending at the FMS",
             "AWAITING_APPLICATION": "accepted by the FMS but not yet on the ledger",
             "OUTCOME_UNKNOWN": "not confirmed yet, and Gatehouse is checking the FMS"}
# what a lien file's latest state means on Today: rank, head, what proves it done, who acts. A file stays on the list
# until a person's decision is recorded, which Gatehouse cannot record yet, so these items do not close on their own.
LIEN_ITEMS = {
    "Account changed: re-review": (1, "Lien file out of date", "A person reviews the file against the account as it is now and decides what happens to the sale.", "Collections lead"),
    "FMS reports paid": (1, "Lien file: the FMS reported the account paid", "A person confirms the sale is cancelled everywhere it was listed.", "Collections lead"),
    "FMS data unavailable": (2, "Lien file cannot be checked", "Once the FMS answers, a person has the file evaluated again.", "Collections lead"),
    "Evidence incomplete": (2, "Lien file incomplete", "Every requirement has a record, with its document where one is needed.", "Front desk"),
    "Waiting for a timing threshold": (3, "Lien file waiting on a date", "After the date, a person has the file evaluated again.", "Collections lead"),
    "Counsel review required": (3, "Counsel review required", "Counsel's decision is recorded on the file. Gatehouse cannot record one yet.", "Counsel"),
    "Review packet assembled": (3, "Lien packet ready for review", "A person reviews the packet. Sale authorization is outside Gatehouse.", "Collections lead"),
}
# a payment the FMS later reversed or put in dispute, and what closes it
REVERSED = {
    "RETURNED": ("Payment returned", "The FMS ledger shows the balance paid."),
    "REFUNDED": ("Payment refunded", "The FMS ledger shows the balance paid."),
    "REFUND_PENDING": ("Refund in progress", "The FMS reports the refund finished, or that it was cancelled."),
    "DISPUTED": ("Payment disputed", "The FMS reports the dispute settled, and the ledger shows what is owed."),
}
EVIDENCE_NAMES = {"notice": "Lien notice", "mail_deposit": "Certificate of mailing", "posting": "Notice posted",
                  "publication": "Newspaper publication", "legal_check": "Legal check", "email_delivery": "Email delivery"}


def money(cents: int | None) -> str:
    return "" if cents is None else f"${cents / 100:,.2f}"


def local(when: datetime | None, zone: str = "America/New_York") -> str:
    if when is None:
        return ""
    t = when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))
    return t.strftime("%b %-d, %-I:%M %p")


def readable(text: str) -> str:
    """Finding details come from the rules engine in cents and ISO dates; operators read dollars and dates.
    Only the engine's own amount phrasings are converted, and a date-shaped string that is not a real date is left
    as it is, so free text inside a finding (a charge description) cannot be misread or break the page."""
    text = re.sub(r"claims (\d+) cents; the FMS ledger then showed (\d+)",
                  lambda m: f"claims {money(int(m.group(1)))}; the FMS ledger then showed {money(int(m.group(2)))}", text)
    text = re.sub(r"from (\d+) to (\d+) cents", lambda m: f"from {money(int(m.group(1)))} to {money(int(m.group(2)))}", text)
    text = re.sub(r"(\d+) cents", lambda m: money(int(m.group(1))), text)

    def as_day(m: re.Match) -> str:
        try:
            return day(m.group(1))
        except ValueError:
            return m.group(1)
    return re.sub(r"\b(20\d\d-\d\d-\d\d)\b", as_day, text)


def sentence(text: str) -> str:
    text = text.strip()
    return (text[0].upper() + text[1:] + ("" if text.endswith(".") else ".")) if text else ""


templates.env.globals.update(sentence=sentence, readable=readable)


def day(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%b %-d, %Y") if iso else ""


@dataclass
class Entry:
    """One line of an account's record, in its lane."""
    at: datetime
    lane: str
    title: str
    text: str = ""
    who: str = ""
    link: str | None = None
    count: int = 1


@dataclass
class Item:
    """One piece of open work on Today."""
    rank: int
    head: str
    unit: str
    case_id: int
    since: datetime
    what: str
    proof: str
    who: str
    link: str
    amount: str = ""
    extra: list[str] = field(default_factory=list)


def describe(e: CaseEvent, commands: dict[int, PaymentCommand], evidence: dict[int, dict]) -> Entry:
    """An event in operator words. The detail keys are the ones each `record(...)` call writes."""
    d, kind = e.detail or {}, e.kind
    cmd = commands.get(d.get("command"))
    pay_link = f"/payments/{cmd.id}" if cmd else None
    text, title = "", kind[0].upper() + kind[1:]
    if kind == "case opened":
        title, text = "Past due", f"{money(d.get('total_due'))} owed since {day(d.get('delinquent_since'))}."
    elif kind == "payment submitted":
        said = {"lost": f"The outcome was not confirmed ({d.get('detail') or 'no usable answer'}).", "accepted": f"The FMS answered {d.get('status')}."}
        title = f"Sent a {money(d.get('amount'))} card payment"
        text = said.get(d.get("fms_said"), f"The FMS answered: {d.get('detail') or d.get('fms_said')}.")
    elif kind == "possible match for staff":
        title, text = "Found a possible match", f"{d.get('payment')} ({d.get('status')}) at the FMS may be this attempt. Not matched automatically."
    elif kind == "state changed":
        title, text = f"Account {d.get('to')}", f"From {d.get('from')}. The FMS balance read {money(d.get('total_due'))}."
    elif kind == "payment confirmed":
        title, text = "Payment confirmed", f"{money(d.get('amount'))} as {d.get('payment')}, applied on the FMS ledger."
    elif kind == "payment status changed after success":
        title, text = "Payment came back", f"The FMS now reports {d.get('fms_status')} for a payment Gatehouse had confirmed."
    elif kind == "payment taken outside the desk":
        title, text = "Payment made outside Gatehouse", f"{money(d.get('amount'))}, recorded as {d.get('payment')}."
    elif kind == "payment needs staff":
        title, text = "Held for a person", d.get("reason") or (cmd.provider_reason if cmd else "")
    elif kind == "lien file evaluated":
        title, text = d.get("state", "Lien file evaluated"), f"Rule version {d.get('rule_version') or 'none'}."
        pay_link = f"/accounts/{e.case_id}/lien"
    elif kind == "lien evidence recorded":
        title = EVIDENCE_NAMES.get(d.get("kind"), d.get("kind", "Evidence")) + " recorded"
        facts = evidence.get(d.get("evidence"), {})
        when = next((facts[k] for k in ("deposited_on", "posted_on", "published_on", "observed_on", "delivered_on") if facts.get(k)), None)
        if d.get("kind") == "legal_check":
            text = f"{facts.get('check', '').replace('_', ' ').capitalize()}: {facts.get('answer', '')}."
        elif when:
            text = f"Dated {day(when)}."
        pay_link = f"/accounts/{e.case_id}/lien"
    elif kind == "payment resolved by staff":
        title, text = "Resolved by staff", d.get("note", "")
    elif kind == "fms read failed":
        title, text = "The FMS did not answer", d.get("error", "")
    elif kind in ("tenant said", "desk replied"):
        text = d.get("text", "")
    return Entry(e.occurred_at, LANE_OF.get(kind, OBSERVED), title, text, e.actor if e.actor not in ("desk",) else "", pay_link)


@dataclass
class Pages:
    factory: sessionmaker[Session]
    trace: dict = field(default_factory=dict)   # simulator truth saved when the world was built, keyed by lease

    def facilities(self, s: Session) -> list[Facility]:
        binding = s.get(DeskBinding, 1)
        rows = s.scalars(select(Facility).order_by(Facility.id)).all()
        return [f for f in rows if binding is None or f.provider_id in binding.facility_ids]

    def today(self, s: Session, facility: Facility) -> list[Item]:
        items: list[Item] = []
        # a paid account can still have a lien file in motion, so every case is read; only lien work shows for those
        cases = s.scalars(select(DelinquencyCase).join(Lease, Lease.id == DelinquencyCase.lease_id)
                          .where(Lease.facility_id == facility.id)).all()
        for case in cases:
            lease = s.get(Lease, case.lease_id)
            snap = s.get(LedgerSnapshot, case.last_snapshot_id) if case.last_snapshot_id else None
            owed = snap.total_due if snap else None
            found = False
            commands = s.scalars(select(PaymentCommand).where(PaymentCommand.case_id == case.id)).all()
            for cmd in commands:
                if case.state == "paid" and cmd.state in ("RETURNED", "REFUNDED"):
                    continue   # their proof is a paid balance, which the FMS now shows
                how = "by phone" if (cmd.authorization or {}).get("channel") == "desk" else "in the tenant chat"
                if cmd.state == "NEEDS_STAFF":
                    found = True
                    if cmd.provider_payment_id is None:
                        what = (f"A {money(cmd.amount)} payment agreed {how}. The FMS did not confirm the outcome, so Gatehouse "
                                "holds the account and has not retried. "
                                + (f"The FMS shows {cmd.suggested_payment_id}, which may be this attempt." if cmd.suggested_payment_id
                                   else "Gatehouse has not matched any FMS payment to this attempt."))
                        head = "Payment outcome unknown"
                    else:
                        what, head = sentence(cmd.provider_reason or "The FMS and the ledger disagree about this payment"), "Payment needs a person"
                    items.append(Item(0, head, lease.unit_name, case.id, cmd.created_at, what,
                                      "A person names the FMS payment that is this attempt and the ledger shows it, or records "
                                      "with evidence that it was not charged.",
                                      "Collections lead", f"/payments/{cmd.id}", money(cmd.amount)))
                elif cmd.state in IN_FLIGHT:
                    found = True
                    items.append(Item(1, "Payment in progress", lease.unit_name, case.id, cmd.created_at,
                                      f"A {money(cmd.amount)} payment agreed {how} is {IN_FLIGHT[cmd.state]}. No other payment can start "
                                      "on this lease until it settles.",
                                      "The FMS reports it succeeded and the ledger shows it, the FMS reports it declined, or "
                                      "Gatehouse hands it to a person.",
                                      "Gatehouse checks on its own", f"/payments/{cmd.id}", money(cmd.amount)))
                elif cmd.state in REVERSED:
                    found = True
                    head, proof = REVERSED[cmd.state]
                    before = (f"{money(cmd.amount)} was confirmed on the ledger {local(cmd.confirmed_at)}. " if cmd.confirmed_at
                              else f"A {money(cmd.amount)} payment. ")
                    items.append(Item(2, head, lease.unit_name, case.id, cmd.reversed_at or cmd.created_at,
                                      before + f"The FMS now reports it {cmd.provider_status}.",
                                      proof, "Front desk", f"/payments/{cmd.id}", money(owed)))
            last = s.scalar(select(LienEvaluation).where(LienEvaluation.case_id == case.id).order_by(LienEvaluation.id.desc()).limit(1))
            if last is not None:
                # the evaluation's own state, labelled with its date; nothing here infers what it would say today
                found = True
                rank, head, proof, who = LIEN_ITEMS.get(last.state, (3, last.state, "A person reviews the file.", "Collections lead"))
                open_findings = [sentence(readable(f["detail"])) for f in last.findings if f["status"] != "met"]
                sale = next((v for k, v in last.timing.items() if k.startswith("earliest sale")), None)
                what = " ".join(open_findings) if last.state != "Counsel review required" and open_findings else sentence(last.summary)
                items.append(Item(rank, head, lease.unit_name, case.id, last.evaluated_at,
                                  f"As of the evaluation on {local(last.evaluated_at)}: " + what
                                  + (f" Earliest sale under the statute: {day(sale)}." if sale and rank <= 2 else ""),
                                  proof, who, f"/accounts/{case.id}/lien", money(owed)))
            if case.state == "paid":
                continue   # past due no longer; any open payment or lien work above still shows
            if not found:
                items.append(Item(4, "Past due", lease.unit_name, case.id, case.opened_at,
                                  f"{money(owed)} owed. No payment or lien step in progress.",
                                  "The FMS ledger shows the balance paid.", "Front desk", f"/accounts/{case.id}", money(owed)))
        return sorted(items, key=lambda i: (i.rank, i.since))

    def record(self, s: Session, case: DelinquencyCase) -> list[Entry]:
        commands = {c.id: c for c in s.scalars(select(PaymentCommand).where(PaymentCommand.case_id == case.id)).all()}
        evidence = {r.id: r.facts for r in s.scalars(select(LienEvidence).where(LienEvidence.case_id == case.id)).all()}
        entries = [describe(e, commands, evidence) for e in s.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id)).all()]
        for p in s.scalars(select(PaymentProposal).where(PaymentProposal.case_id == case.id, PaymentProposal.confirmed_by.is_not(None))).all():
            when = commands[p.command_id].created_at if p.command_id in commands else p.created_at
            entries.append(Entry(when, DECIDED, f"Tenant agreed to {money(p.amount)}",
                                 f"“{p.tenant_answer}”, card ending {p.card_last4}, heard by {p.confirmed_by}.",
                                 "", f"/payments/{p.command_id}" if p.command_id else None))
        entries.sort(key=lambda x: x.at)
        # nine legal checks entered one after another by the same person read as one row with its list
        folded: list[Entry] = []
        for e in entries:
            last = folded[-1] if folded else None
            if last and last.lane == e.lane == DECIDED and last.title == e.title and last.who == e.who and e.title.startswith("Legal check"):
                last.count += 1
                last.text = f"{last.text} {e.text}".strip()
            else:
                folded.append(e)
        for e in folded:
            if e.count > 1:
                e.title = f"{e.count} legal checks recorded"
        return folded


def router(pages: Pages) -> APIRouter:
    r = APIRouter()

    def session():
        return pages.factory()

    def case_or_404(s: Session, case_id: int) -> tuple[DelinquencyCase, Lease, Facility]:
        case = s.get(DelinquencyCase, case_id)
        if case is None:
            raise HTTPException(404, "No such account")
        lease = s.get(Lease, case.lease_id)
        facility = s.get(Facility, lease.facility_id)
        if facility not in pages.facilities(s):
            raise HTTPException(404, "No such account")
        return case, lease, facility

    @r.get("/", response_class=HTMLResponse)
    def front(request: Request):
        """What Gatehouse is, and each built feature shown live on the seeded world, with a link into it."""
        with session() as s:
            facilities = pages.facilities(s)
            if not facilities:
                raise HTTPException(503, "No facility has been read from the FMS yet")
            home = facilities[0]
            items = pages.today(s, home)
            # only accounts at the facilities this deployment is bound to, the same rule every other page applies
            mine = select(DelinquencyCase.id).join(Lease, Lease.id == DelinquencyCase.lease_id).where(
                Lease.facility_id.in_([f.id for f in facilities]))
            commands = s.scalars(select(PaymentCommand).where(PaymentCommand.case_id.in_(mine)).order_by(PaymentCommand.id)).all()
            lost = {e.detail.get("command"): e.detail for e in s.scalars(select(CaseEvent).where(
                CaseEvent.kind == "payment submitted", CaseEvent.case_id.in_(mine))).all() if (e.detail or {}).get("fms_said") == "lost"}
            # the example has to be what the overview says it is: held after a lost answer, with a possible FMS match
            held = next((c for c in commands if c.state == "NEEDS_STAFF" and c.id in lost and c.provider_payment_id is None
                         and c.suggested_payment_id), None)
            returned = next((c for c in commands if c.state == "RETURNED" and c.confirmed_at is not None
                             and s.get(DelinquencyCase, c.case_id).state != "paid"), None)
            latest = {}
            for e in s.scalars(select(LienEvaluation).where(LienEvaluation.case_id.in_(mine)).order_by(LienEvaluation.id)).all():
                latest[e.case_id] = e   # the newest evaluation of each file, never an older one
            changed = next((e for e in latest.values() if e.state == "Account changed: re-review"), None)
            counsel = next((e for e in latest.values() if e.state == "Counsel review required" and not e.rule_version), None)
            counsel_state = (s.get(Facility, s.get(Lease, s.get(DelinquencyCase, counsel.case_id).lease_id).facility_id).jurisdiction
                             if counsel else None)
            record_case = held.case_id if held else (items[0].case_id if items else None)
            record_unit = s.get(Lease, s.get(DelinquencyCase, record_case).lease_id).unit_name if record_case is not None else None
            lanes = {}
            if record_case is not None:
                for e in pages.record(s, s.get(DelinquencyCase, record_case)):
                    lanes.setdefault(e.lane, []).append(e)
            return templates.TemplateResponse(request, "front.html", {
                "facility": home, "facilities": facilities, "items": items[:4], "open_count": len(items),
                "held": held, "held_answer": (lost.get(held.id) or {}).get("detail") if held else None, "held_unit": s.get(Lease, held.lease_id).unit_name if held else None,
                "returned": returned, "returned_unit": s.get(Lease, returned.lease_id).unit_name if returned else None,
                "changed": changed, "counsel": counsel, "counsel_state": counsel_state, "record_case": record_case,
                "record_unit": record_unit, "lanes": lanes, "lane_names": LANES,
                "truth": pages.trace.get(s.get(Lease, held.lease_id).provider_id) if held else None,
                "money": money, "local": local, "nav": "front"})

    @r.get("/today", response_class=HTMLResponse)
    def today(request: Request, facility: str | None = None):
        with session() as s:
            facilities = pages.facilities(s)
            if not facilities:
                raise HTTPException(503, "No facility has been read from the FMS yet")
            current = next((f for f in facilities if f.provider_id == facility), None) if facility is not None else facilities[0]
            if current is None:
                raise HTTPException(404, "No such facility")
            items = pages.today(s, current)
            return templates.TemplateResponse(request, "today.html", {
                "facilities": facilities, "facility": current, "items": items, "local": local, "nav": "today"})

    @r.get("/accounts/{case_id}", response_class=HTMLResponse)
    def account(request: Request, case_id: int):
        with session() as s:
            case, lease, facility = case_or_404(s, case_id)
            snap = s.get(LedgerSnapshot, case.last_snapshot_id) if case.last_snapshot_id else None
            entries = pages.record(s, case)
            return templates.TemplateResponse(request, "account.html", {
                "case": case, "lease": lease, "facility": facility, "snap": snap, "entries": entries, "lanes": LANES,
                "money": money, "local": local, "nav": "today", "facilities": pages.facilities(s)})

    @r.get("/payments/{command_id}", response_class=HTMLResponse)
    def payment(request: Request, command_id: int):
        with session() as s:
            cmd = s.get(PaymentCommand, command_id)
            if cmd is None:
                raise HTTPException(404, "No such payment")
            case, lease, facility = case_or_404(s, cmd.case_id)
            proposal = s.scalar(select(PaymentProposal).where(PaymentProposal.command_id == cmd.id))
            sent = next((e.detail for e in s.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id,
                                                                           CaseEvent.kind == "payment submitted")).all()
                         if (e.detail or {}).get("command") == cmd.id), None)
            check = s.get(IdentityCheck, proposal.identity_check_id) if proposal else None
            snap = s.get(LedgerSnapshot, case.last_snapshot_id) if case.last_snapshot_id else None
            return templates.TemplateResponse(request, "payment.html", {
                "cmd": cmd, "case": case, "lease": lease, "facility": facility, "proposal": proposal, "check": check,
                "snap": snap, "told": tenant_message(cmd), "truth": pages.trace.get(lease.provider_id), "sent": sent,
                "money": money, "local": local, "nav": "today", "facilities": pages.facilities(s)})

    @r.get("/accounts/{case_id}/lien", response_class=HTMLResponse)
    def lien(request: Request, case_id: int):
        with session() as s:
            case, lease, facility = case_or_404(s, case_id)
            evaluations = s.scalars(select(LienEvaluation).where(LienEvaluation.case_id == case.id)
                                    .order_by(LienEvaluation.id.desc())).all()
            if not evaluations:
                raise HTTPException(404, "This account has no lien file")
            current, previous = evaluations[0], (evaluations[1] if len(evaluations) > 1 else None)
            snap = s.get(LedgerSnapshot, case.last_snapshot_id) if case.last_snapshot_id else None
            before = {f["requirement"]: f for f in previous.findings} if previous else {}
            changed = [(before.get(f["requirement"]), f) for f in current.findings
                       if previous and before.get(f["requirement"]) != f]
            order = {"blocked": 0, "review": 1, "missing": 2, "waiting": 3, "met": 9}
            findings = sorted(current.findings, key=lambda f: order.get(f["status"], 5))
            evidence = s.scalars(select(LienEvidence).where(LienEvidence.case_id == case.id).order_by(LienEvidence.id)).all()
            return templates.TemplateResponse(request, "lien.html", {
                "case": case, "lease": lease, "facility": facility, "current": current, "previous": previous, "snap": snap, "money": money,
                "changed": changed, "findings": findings, "evidence": evidence, "names": EVIDENCE_NAMES, "day": day,
                "local": local, "nav": "today", "facilities": pages.facilities(s)})

    @r.get("/limits", response_class=HTMLResponse)
    def limits(request: Request):
        with session() as s:
            return templates.TemplateResponse(request, "limits.html", {"nav": "limits", "facilities": pages.facilities(s)})

    return r
