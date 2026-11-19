"""Lien cases: the file kept on a unit whose rent has gone unpaid long enough for the owner's schedule, from the day it is opened
until it is closed.

A case belongs to one lease and one enforcement episode. It is folded from "lien_case.*" events into Books.liens, like the
collections records, and everything the gate decides about it is read from those records and the books, never from a flag some
earlier step left behind: the lien.flagged record is a marker, and a case is opened only on what is due now. No notice is sent and
nothing is sold from here."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ValidationError

from . import delinquency as col
from .billing import Ledger
from .books import Books, RENT, local
from .evidence import KIND_WORDS as CHARGE_WORDS, day, money
from .lien_evidence import EVIDENCE_KINDS, NEEDS_DOCUMENT, parse_evidence, same_time

ACTIONS = ("open_lien_case", "record_lien_evidence", "record_lien_address", "prepare_lien_notice",
           "approve_lien_case", "record_lien_stop", "resolve_lien_stop",
           "decide_lien_case")
# the steps on one case: each names the case it is for, so a command prepared for one episode can never land on the next
CASE_ACTIONS = ("record_lien_evidence", "record_lien_address", "prepare_lien_notice", "approve_lien_case", "decide_lien_case")
APPROVER = "staff:manager"
# bankruptcy and military service protect the person and everything they store, so they hold every unit of the tenant; a court order and
# a secured party or ownership conflict name specific property, so they hold only the unit they were recorded on
TENANT_WIDE = ("bankruptcy", "military_service")
# what stops a file until staff record it resolved: no approval, by anyone, overrides one
STOP_WORDS = {"military_service": "reported military service", "bankruptcy": "a bankruptcy", "court_order": "a court order",
              "secured_party": "a secured party or an ownership conflict"}
ADDRESS_BASES = ("rental agreement", "written change of address")
TASK = "lien case needs staff"
DELIVERY = ("mail_deposit", "in_person_delivery", "email_delivery")
KIND_WORDS = {"notice": "notice", "mail_deposit": "mailing", "in_person_delivery": "hand-delivery", "email_delivery": "e-mail",
              "posting": "posting", "publication": "advertisement", "legal_check": "legal check"}


@dataclass
class Evidence:
    """One record in the file. Never edited or deleted: a correction is a new record that names the one it supersedes."""
    id: int                             # counted within the case, from 1; a record's `notice_id` names the notice record it belongs to
    kind: str
    facts: dict
    document_sha256: str | None         # the fingerprint of the document the facility keeps, where the kind rests on one
    by: str
    at: datetime
    supersedes: int | None = None


@dataclass
class Finding:
    requirement: str
    basis: str                          # "policy" for what a packet lists; a statute citation once the Florida rule version is on the core
    status: Literal["met", "missing", "review", "waiting", "blocked"]
    detail: str
    action: str = ""


@dataclass
class Case:
    id: str
    lease: str
    tenant: str
    unit: str
    episode: int                        # the first case on a lease is episode 1; a closed episode never reopens, a new one counts on
    opened_by: str
    opened_at: datetime
    closed: dict | None = None
    evidence: list[Evidence] = field(default_factory=list)
    addresses: list[dict] = field(default_factory=list)     # every written address recorded, oldest first; the last is the one in force
    copies: list[dict] = field(default_factory=list)        # each notice copy: version, when, by, and the frozen snapshot
    reviews: list[dict] = field(default_factory=list)       # every time the file went back to review: when, what changed, for which copy
    decisions: list[dict] = field(default_factory=list)     # what staff decided after the file went back to review, and why
    approvals: list[dict] = field(default_factory=list)     # every approval: who, when, the fingerprint it was tied to, and the void that ended it


@dataclass
class State:
    cases: dict[str, Case] = field(default_factory=dict)
    stops: dict[str, dict] = field(default_factory=dict)    # legal stops by id: they belong to the unit, not to the case they were recorded on


def state(books: Books) -> State:
    """The lien view of the books: every case and stop folded so far. Before any lien record exists it is an empty State, never None, so
    a caller can read it without checking; the empty view is not stored and reading it changes nothing."""
    return books.liens or State()


def fold_event(b: Books, kind: str, at: datetime, p: dict) -> None:
    """Fold one `lien_case.*` event into the books. Books.apply calls it for each such event in the order the log holds them (opened,
    then the records on that case, with a stop's record and its resolution in order), so replaying the log gives the same state; an
    event whose case or stop is not yet folded is a log out of order, and raises."""
    if b.liens is None:
        b.liens = State()
    if kind == "lien_case.opened":
        b.liens.cases[p["case"]] = Case(p["case"], p["lease"], p["tenant"], p["unit"], p["episode"], p["by"], at)
        b._seen("LC", p["case"])
    elif kind == "lien_case.address_recorded":
        b.liens.cases[p["case"]].addresses.append({"id": p["address_id"], "address": p["address"], "basis": p["basis"], "by": p["by"], "at": at})
    elif kind == "lien_case.notice_prepared":
        b.liens.cases[p["case"]].copies.append({"version": p["version"], "by": p["by"], "at": at, "snapshot": p["snapshot"]})
    elif kind == "lien_case.stop_recorded":
        b.liens.stops[p["stop"]] = {"id": p["stop"], "lease": p["lease"], "tenant": p["tenant"], "kind": p["kind"], "detail": p["detail"],
                                    "source": p["source"], "by": p["by"], "at": at, "case": p["case"], "resolved": None}
        b._seen("LS", p["stop"])
    elif kind == "lien_case.stop_resolved":
        b.liens.stops[p["stop"]]["resolved"] = {"reason": p["reason"], "by": p["by"], "at": at, "case": p["case"]}
    elif kind == "lien_case.decided":
        b.liens.cases[p["case"]].decisions.append({"outcome": p["outcome"], "reason": p["reason"], "by": p["by"], "at": at, "version": p["version"]})
    elif kind == "lien_case.closed":
        b.liens.cases[p["case"]].closed = {"kind": p["kind"], "why": p["why"], "by": p["by"], "at": at}
    elif kind == "lien_case.approved":
        b.liens.cases[p["case"]].approvals.append({"id": p["approval"], "by": p["by"], "at": at, "fingerprint": p["fingerprint"], "parts": p["parts"],
                                                   "voided": None})
    elif kind == "lien_case.approval_voided":
        b.liens.cases[p["case"]].approvals[p["approval"] - 1]["voided"] = {"why": p["why"], "at": at}
    elif kind == "lien_case.review_required":
        b.liens.cases[p["case"]].reviews.append({"keys": p["keys"], "why": p["why"], "version": p["version"], "at": at})
    elif kind == "lien_case.evidence_recorded":
        b.liens.cases[p["case"]].evidence.append(Evidence(p["evidence"], p["kind"], p["facts"], p["document_sha256"], p["by"], at, p["supersedes"]))


def active_case(books: Books, lease: str) -> Case | None:
    """The one case on the lease that is not closed."""
    return next((c for c in state(books).cases.values() if c.lease == lease and not c.closed), None)


def current_evidence(case: Case) -> list[Evidence]:
    """Every record no later one supersedes, oldest first."""
    replaced = {e.supersedes for e in case.evidence if e.supersedes is not None}
    return [e for e in case.evidence if e.id not in replaced]


def _notice_gap(books: Books, case: Case, notice: Evidence) -> str | None:
    """How a notice differs from the notice copy it names (the total and the itemized charges), or None when it states that claim."""
    snap = next(c["snapshot"] for c in case.copies if c["version"] == notice.facts["copy_version"])
    if notice.facts["itemized_total_cents"] != snap["claim_cents"]:
        return f"it states {money(notice.facts['itemized_total_cents'])} and the copy's claim is {money(snap['claim_cents'])}"
    stated = sorted((i["due_on"], i["amount_cents"], i["category"]) for i in notice.facts["itemized_charges"])
    claimed = sorted((c["due"], c["cents"], "rent" if c["kind"] == RENT else "fee") for c in snap["claim"])
    return None if stated == claimed else "its itemized charges differ from the copy's"


def _sale_gap(ad: Evidence, notice: Evidence) -> str | None:
    """How an advertisement's sale differs from the sale its notice names (date, time and place), or None when it is the same sale."""
    a, n, out = ad.facts, notice.facts, []
    if a["sale_date"] != n["sale_date"]:
        out.append(f"sale date {day(date.fromisoformat(a['sale_date']))} where the notice says {day(date.fromisoformat(n['sale_date']))}")
    if not same_time(a["sale_time"], n["sale_time"]):
        out.append(f"sale time {a['sale_time']} where the notice says {n['sale_time']}")
    def place(value):
        try:
            url = urlsplit(value)
        except ValueError:
            return " ".join(value.lower().split())
        if url.scheme.lower() in ("http", "https") and url.netloc:
            user, separator, host = url.netloc.rpartition("@")
            return (url.scheme.lower(), user + separator, host.lower(), url.path, url.query, url.fragment)
        return " ".join(value.lower().split())

    if place(a["sale_place"]) != place(n["sale_place"]):
        out.append(f"sale place {a['sale_place']} where the notice says {n['sale_place']}")
    return "; ".join(out) or None


def _delivery_gap(e: Evidence) -> str | None:
    """Why a delivery record does not count, from its own facts: first-class mail needs its certificate and prepaid postage, and an
    e-mail counts only with a response, return receipt or delivery confirmation from the address it went to."""
    f = e.facts
    if e.kind == "mail_deposit":
        lacks = [w for k, w in (("first_class", "first-class mail"), ("certificate_of_mailing", "a certificate of mailing"),
                                ("postage_prepaid", "prepaid postage")) if not f.get(k)]
        return "the mailing has no " + ", no ".join(lacks) if lacks else None
    if e.kind == "email_delivery" and not (f.get("confirmation") and (f.get("confirmation_from") or "").strip().lower() == (f.get("sent_to") or "").strip().lower()):
        return "no response, return receipt or delivery confirmation came from the address it went to, so a first-class mailing with a certificate must follow"
    return None


def _ad_gap(ad: Evidence, notice: Evidence) -> str | None:
    """Why an advertisement does not count: a different sale than the notice names, a newspaper not shown to circulate in the
    facility's area, or a part the advertisement must carry left out."""
    if gap := _sale_gap(ad, notice):
        return "it names a different sale than the notice: " + gap
    if ad.facts.get("general_circulation_in_facility_area") != "yes":
        return "the newspaper is not shown to be of general circulation in the facility's area"
    left = [k.removeprefix("includes_").replace("_", " ") for k, v in ad.facts.items() if k.startswith("includes_") and not v]
    return "it leaves out the " + ", the ".join(left) if left else None


def findings(books: Books, case: Case) -> list[Finding]:
    """What the packet lists and where the file stands against it: the notice, its delivery, posting, and newspaper advertisement.

    A notice counts only if it was made from the latest notice copy and states that copy's claim, and delivery and the
    advertisement only for that notice, so a resumed file, whose copy was taken again, needs its service made again. An
    advertisement counts only if it describes the sale the notice names. A record that does not count is still on the file as
    evidence and reads "review" until a correction supersedes it."""
    cur = current_evidence(case)
    latest = case.copies[-1]["version"] if case.copies else None
    notices = [e for e in cur if e.kind == "notice"]
    named = [e for e in reversed(notices) if e.facts.get("copy_version") == latest]
    notice = next((e for e in named if not _notice_gap(books, case, e)), None)

    def record_for(kinds):
        return next((e for e in cur if notice and e.kind in kinds and e.facts.get("notice_id") == notice.id), None)

    if notice:
        out = [Finding("the notice", "policy", "met", f"record {notice.id}, made from copy {latest}")]
    elif named:
        out = [Finding("the notice", "policy", "review", f"record {named[0].id} does not match copy {latest}: {_notice_gap(books, case, named[0])}",
                       "record a corrected notice that supersedes it")]
    elif notices:
        old = notices[-1]
        out = [Finding("the notice", "policy", "missing", f"the latest notice (record {old.id}) was made from copy {old.facts.get('copy_version')} and the file "
                       f"is now on copy {latest}", "record a notice made from the latest copy")]
    else:
        out = [Finding("the notice", "policy", "missing", "no notice record is on the file", "record the notice with the fingerprint of its document")]
    sent = [e for e in cur if notice and e.kind in DELIVERY and e.facts.get("notice_id") == notice.id]
    found = next((e for e in sent if not _delivery_gap(e)), None)
    words = "the notice being mailed, handed over or e-mailed"
    if found:
        out.append(Finding("delivery of the notice", "policy", "met", f"record {found.id}"))
    elif sent:            # a record that falls short stays on the file as evidence, and the packet says what it lacks
        out.append(Finding("delivery of the notice", "policy", "review", f"record {sent[-1].id}: {_delivery_gap(sent[-1])}",
                           "record a delivery that meets the rule"))
    else:
        out.append(Finding("delivery of the notice", "policy", "missing", f"no record of {words} for the current notice",
                           f"record {words} with the fingerprint of its document"))
    posted = record_for(("posting",))
    if posted:
        out.append(Finding("posting of the notice", "policy", "met", f"record {posted.id}"))
    else:
        out.append(Finding("posting of the notice", "policy", "missing", "no posting record for the current notice",
                           "record where the notice was posted with the fingerprint of its document"))
    ads = [e for e in reversed(cur) if notice and e.kind == "publication" and e.facts.get("notice_id") == notice.id]
    good = next((e for e in ads if not _ad_gap(e, notice)), None)
    if good:
        out.append(Finding("the newspaper advertisement", "policy", "met", f"record {good.id}"))
    elif ads:
        out.append(Finding("the newspaper advertisement", "policy", "review", f"record {ads[0].id}: {_ad_gap(ads[0], notice)}",
                           "record a corrected advertisement that supersedes it"))
    else:
        out.append(Finding("the newspaper advertisement", "policy", "missing", "no record of the advertisement for the current notice",
                           "record the advertisement with the fingerprint of its document"))
    return out


def unit_stops(books: Books, lease: str) -> list[dict]:
    """Every legal stop that applies to the unit, in the order recorded, whichever case it was recorded on: those recorded on the
    unit, and bankruptcy and military service recorded on any unit of the same tenant."""
    tenant = books.leases[lease].tenant
    return sorted((s for s in state(books).stops.values() if s["lease"] == lease or (s["kind"] in TENANT_WIDE and s["tenant"] == tenant)),
                  key=lambda s: s["id"])


def open_stops(books: Books, lease: str) -> list[dict]:
    """The legal stops that apply to the unit and are not yet resolved: those recorded on the unit, whether or not a case is open,
    and the tenant-wide kinds (bankruptcy, military service) recorded on any unit of the same tenant. Empty means nothing holds it."""
    return [s for s in unit_stops(books, lease) if not s["resolved"]]


def _named(books: Books, stop: dict, lease: str) -> str:
    """The stop in words, saying which unit it was recorded on when it applies to this one only because it follows the tenant."""
    words = STOP_WORDS[stop["kind"]]
    return words if stop["lease"] == lease else f"{words} recorded for this tenant on unit {books.leases[stop['lease']].unit}"


def stop_reason(books: Books, case: Case) -> str | None:
    """Why a legal stop holds the file, or None. Safety fixed in code: no role, approval or schedule gets past it, and closing a file
    or opening the next one does not lift it, because the stop belongs to the unit."""
    if not (open_ := open_stops(books, case.lease)):
        return None
    s = open_[0]
    return (f"{_named(books, s, case.lease)} is on the file as a legal stop ({s['detail']}); no approval overrides it, and only staff recording "
            f"it resolved, with the reason, lifts it")


def frozen(case: Case) -> dict | None:
    """The itemized copy the current notice is drawn from, exactly as it was taken."""
    return case.copies[-1]["snapshot"] if case.copies else None


def snapshot(books: Books, case: Case) -> dict:
    """The facts a notice rests on, as the books show them now: the open charges that make the claim, the tenant, the unit, the
    signed agreement and the last written address, plus what existed when it was taken, so a later change is found by comparing."""
    lease, tenant = books.leases[case.lease], books.tenants[case.tenant]
    claim = sorted((books.charges[i] for i in books.of_lease.get(lease.id, ()) if books.charges[i].open_cents > 0),
                   key=lambda c: (c.due, c.kind != RENT, c.seq))
    last = case.addresses[-1] if case.addresses else None
    return {
        "tenant": {"id": tenant.id, "name": tenant.name},
        "unit": lease.unit,
        "agreement": {"lease": lease.id, "start": lease.start.isoformat(), "rent_cents": lease.rent_cents, "schedule": list(lease.schedule),
                      "ended": lease.ended.isoformat() if lease.ended else None},
        "address": {"address": last["address"], "basis": last["basis"]} if last else None,
        "claim": [{"charge": c.id, "kind": c.kind, "obligation": c.obligation, "due": c.due.isoformat(), "cents": c.open_cents} for c in claim],
        "claim_cents": sum(c.open_cents for c in claim),
        "charges_seen": sorted(books.of_lease.get(lease.id, ())),
        "payments_seen": sorted(p.id for p in books.payments.values() if p.tenant == case.tenant),
        "returned_seen": sorted(p.id for p in books.payments.values() if p.tenant == case.tenant and p.returned),
        "addresses_seen": len(case.addresses),
    }


def changes(books: Books, case: Case) -> list[dict]:
    """What the books and the file hold now that the notice copy does not: a payment (to this unit or another), a payment that
    came back, a claimed charge that changed or closed, a new charge, a newer address, a changed tenant, unit or agreement. Read
    from append-only facts since the copy, never from a flag, so a change that is later undone is still a change."""
    snap = frozen(case)
    if snap is None:
        return []
    out = []
    for p in sorted((p for p in books.payments.values() if p.tenant == case.tenant and p.id not in snap["payments_seen"]), key=lambda p: p.id):
        away = ", and part of it was applied to another unit" if any(a["lease"] != case.lease for a in p.applications) else ""
        out.append({"key": f"payment:{p.id}", "why": f"a {money(p.cents)} payment came in on {day(p.known_at)}{away}"})
    for p in sorted((p for p in books.payments.values() if p.tenant == case.tenant and p.returned and p.id not in snap["returned_seen"]), key=lambda p: p.id):
        out.append({"key": f"returned:{p.id}", "why": f"the {money(p.cents)} payment of {day(p.known_at)} came back"})
    for c in snap["claim"]:
        now, word = books.charges[c["charge"]], CHARGE_WORDS.get(c["kind"], c["kind"])
        due = day(date.fromisoformat(c["due"]))
        if now.closed:
            out.append({"key": f"closed:{c['charge']}", "why": f"the {word} charge due {due} was {now.closed}"})
        elif now.open_cents != c["cents"]:
            out.append({"key": f"amount:{c['charge']}", "why": f"the {word} charge due {due} is now {money(now.open_cents)} open, it was {money(c['cents'])}"})
    for i in books.of_lease.get(case.lease, ()):
        if i not in snap["charges_seen"] and not _billed(books.charges[i], case.lease):
            ch = books.charges[i]
            out.append({"key": f"charge:{i}", "why": f"a new {CHARGE_WORDS.get(ch.kind, ch.kind)} charge of {money(ch.cents)} due {day(ch.due)} was posted"})
    for i, ch in _billed_since(books, case):       # billed charges join the claim, but a waiver or reversal of one is still a change
        if ch.closed:
            out.append({"key": f"closed:{i}", "why": f"the {CHARGE_WORDS.get(ch.kind, ch.kind)} charge due {day(ch.due)} was {ch.closed}"})
    for a in case.addresses[snap["addresses_seen"]:]:
        out.append({"key": f"address:{a['id']}", "why": f"a new written address was recorded on {day(a['at'])}"})
    live = snapshot(books, case)
    if live["tenant"] != snap["tenant"]:
        out.append({"key": "tenant", "why": "the tenant's details changed"})
    if live["unit"] != snap["unit"]:
        out.append({"key": "unit", "why": "the tenant's unit changed"})
    if live["agreement"] != snap["agreement"]:
        ended = live["agreement"]["ended"]
        out.append({"key": "agreement", "why": f"the lease ended on {day(date.fromisoformat(ended))}" if ended and not snap["agreement"]["ended"]
                    else "the signed agreement changed"})
    return out


def _billed(ch, lease: str) -> bool:
    """Rent and late fees that billing posts on the signed schedule. The lien covers charges "present or future" (83.805) and the
    notice shows the sum due at its own time (83.806(2)(a)), so these join the claim without a review; a charge posted by hand is
    still a change."""
    return ch.source == "billing" and (ch.obligation or "").startswith((f"rent:{lease}:", f"late_fee:{lease}:"))


def _billed_since(books: Books, case: Case) -> list:
    """The (id, charge) pairs billing posted on the signed schedule after the notice copy: part of the claim from then on."""
    snap = frozen(case)
    if snap is None:
        return []
    return [(i, books.charges[i]) for i in books.of_lease.get(case.lease, ()) if i not in snap["charges_seen"] and _billed(books.charges[i], case.lease)]


def _digest(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


def parts(books: Books, case: Case) -> dict[str, str]:
    """A fingerprint of each kind of fact the file stands on. Nothing here depends on the clock or on what is eligible today, or an
    approval would void itself every day."""
    return {
        "copy": _digest([case.copies[-1]["version"] if case.copies else None, frozen(case)]),
        "changes": _digest(changes(books, case)),
        "billed": _digest([[i, ch.cents] for i, ch in _billed_since(books, case)]),     # a grown claim voids an approval, without a review
        "evidence": _digest([[e.id, e.kind, e.document_sha256, e.facts, e.supersedes] for e in case.evidence]),
        "stops": _digest([[s["id"], s["kind"], s["detail"], bool(s["resolved"])] for s in unit_stops(books, case.lease)]),
        "findings": _digest([[f.requirement, f.status, f.detail] for f in findings(books, case)]),
    }


def fingerprint(books: Books, case: Case) -> str:
    """The file as it stands, in one value: the manager approves this, and any change to the facts or the findings changes it."""
    return _digest({"case": case.id, "lease": case.lease, **parts(books, case)})


def approval(books: Books, case: Case) -> dict | None:
    """The approval in force: the latest one nothing has voided, if the file is still exactly as the manager saw it. Read from the
    facts, so it is right before any void has been written, and a change that is later undone does not bring it back."""
    if case.closed:                                         # the episode is over; the history keeps the approval as it was
        return None
    last = next((a for a in reversed(case.approvals) if not a["voided"]), None)
    return last if last and last["fingerprint"] == fingerprint(books, case) else None


def case_state(books: Books, case: Case, now: datetime) -> tuple[str, str]:
    """Where the file stands and why: review (the notice copy is not prepared yet), review required (something changed since it
    was), evidence incomplete, packet assembled, approved, or held (a legal stop or a hold on the tenancy)."""
    if case.closed:
        return "closed", case.closed["why"]
    if why := stop_reason(books, case):
        return "held", why
    # a dispute, an exemption, a plan, a payment still with the processor: asked of the holds themselves, because a hold can take away
    # every past-due charge and then the schedule would call the unit clear before looking at it (the gate keeps its own order)
    if col.unsettled(books, books.leases[case.lease].tenant):
        return "held", "a payment from this tenant is waiting for the processor's answer"
    if hold := col.enforcement_hold(books, case.lease, "lien", now):
        return "held", hold[1]
    if not case.copies:
        return "review", "the notice copy has not been prepared yet"
    if changed := changes(books, case):
        return "review required", "; ".join(c["why"] for c in changed)
    if gaps := [f.requirement for f in findings(books, case) if f.status != "met"]:
        return "evidence incomplete", "the packet still needs: " + ", ".join(gaps)
    if approval(books, case):
        return "approved", "a manager approved the file as it stands"
    return "packet assembled", "every record the packet lists is on the file"


def _void_why(books: Books, case: Case, approved: dict) -> str:
    now = parts(books, case)
    what = []
    if approved["parts"]["copy"] != now["copy"]:
        what.append("the notice copy was prepared again")
    if approved["parts"]["changes"] != now["changes"]:
        what.append("the account or the file changed since the notice copy: " + "; ".join(c["why"] for c in changes(books, case)))
    if approved["parts"].get("billed") != now["billed"]:
        what.append("billing posted a new charge on the signed schedule, so the claim grew")
    if approved["parts"]["evidence"] != now["evidence"]:
        what.append("evidence was added or corrected")
    if approved["parts"]["stops"] != now["stops"]:
        what.append("a legal stop was recorded or resolved")
    return "; ".join(what) or "the file's findings changed"


def cleared(books: Books, case: Case) -> bool:
    """Nothing the claim named is owed any more (or, before a copy, nothing is owed on the unit): the enforcement episode is over."""
    snap = frozen(case)
    if snap is None:
        return books.receivable(case.lease) == 0
    return all(books.charges[c["charge"]].open_cents == 0 for c in snap["claim"]) and all(ch.open_cents == 0 for _, ch in _billed_since(books, case))


def _settle_tasks(led: Ledger, case: Case, by: str, now: datetime, command: str | None = None) -> None:
    """What the file had put on Today is settled once it is decided or over."""
    for t in [t for t in col.state(led.books).tasks.values() if t.ref.startswith(f"lien:{case.id}:") and not t.acknowledged]:
        led.emit("task.acknowledged", now, {"task": t.id, "by": by}, f"task:{t.ref}:seen", source=by, command=command)


def _void_stale(led: Ledger, case: Case, now: datetime) -> None:
    for a in [a for a in case.approvals if not a["voided"] and a["fingerprint"] != fingerprint(led.books, case)]:
        led.emit("lien_case.approval_voided", now, {"case": case.id, "approval": a["id"], "why": _void_why(led.books, case, a)},
                 f"lien_case:{case.id}:approval:{a['id']}:voided", source=col.SYSTEM)


def recheck(led: Ledger, tenant: str, now: datetime) -> None:
    """Write down what changed on each open case since its notice copy, once per change, with one task for staff on Today per copy,
    and void any approval the file no longer matches. Safe to repeat. What the gate decides never depends on this having run: it
    reads the changes and the fingerprint itself."""
    b = led.books
    for case in [c for c in state(b).cases.values() if c.tenant == tenant and not c.closed]:
        _void_stale(led, case, now)
        if cleared(b, case):
            paid = case.copies and any(p.tenant == tenant and p.id not in frozen(case)["payments_seen"] for p in b.payments.values())
            led.emit("lien_case.closed", now, {"case": case.id, "kind": "cured", "by": col.SYSTEM,
                                               "why": "cured: the claim was paid in full" if paid else "cured: nothing in the claim is owed any more"},
                     f"lien_case:{case.id}:closed", source=col.SYSTEM)
            _settle_tasks(led, case, col.SYSTEM, now)
            continue
        if not case.copies:
            continue
        version = case.copies[-1]["version"]
        known = {k for r in case.reviews if r["version"] == version for k in r["keys"]}
        new = [c for c in changes(b, case) if c["key"] not in known]
        if not new:
            continue
        why = "; ".join(c["why"] for c in new)
        led.emit("lien_case.review_required", now, {"case": case.id, "version": version, "keys": [c["key"] for c in new], "why": why},
                 f"lien_case:{case.id}:review:{len(case.reviews) + 1}", source=col.SYSTEM)
        col._task(led, TASK, tenant, f"lien:{case.id}:{version}", why, local(now).date(), now)


# the gate's checks, in its order: the action and its scope, fixed safety, the owner's schedule, then holds ------------------
def check(books: Books, cmd, now: datetime) -> tuple[bool, str]:
    """The gate's decision on a lien command, as (allowed, the reason in plain words), reading the books and writing nothing. The
    checks run in the gate's order: the action and its scope, fixed safety, the owner's schedule, then holds. When it says allowed,
    act() can record the command: the unit is the tenant's, a case step names the open case, and every safety check has passed."""
    a, form = cmd.action, cmd.form or {}
    # 1. the action and its scope
    if not cmd.source.startswith("staff:"):
        return False, "only staff take a lien step"
    if a == "approve_lien_case" and cmd.source != APPROVER:
        return False, "only a manager approves a lien case"
    lease = books.leases.get(cmd.lease or "")
    if lease is None or lease.tenant != cmd.tenant:
        return False, "no such unit on this tenant's account"
    if a == "open_lien_case":
        return _open_check(books, lease, now)
    if a in ("record_lien_stop", "resolve_lien_stop"):          # a stop belongs to the unit: no case need be open to record or lift it
        return _stop_check(books, lease, cmd, form)
    case = active_case(books, lease.id)
    if case is None:
        return False, "no lien case is open on this unit"
    if a in CASE_ACTIONS and form.get("case") != case.id:
        if not form.get("case"):
            return False, f"name the lien case this step is for; the open case on this unit is {case.id}"
        return False, f"lien case {form['case']} is not the open case on this unit; the open case is {case.id}"
    if a == "record_lien_evidence":
        return _evidence_check(case, form)
    if a == "record_lien_address":
        address, basis = form.get("address"), form.get("basis")
        if not isinstance(address, str) or len(address.strip()) < 5:
            return False, "an address needs the full street address"
        if basis not in ADDRESS_BASES:
            return False, "an address comes from the rental agreement or a written change of address"
        return True, "the address is added to the file"
    if a == "prepare_lien_notice":
        return _prepare_check(books, lease, case, now)
    if a == "approve_lien_case":
        return _approve_check(books, lease, case, form, now)
    if a == "decide_lien_case":
        return _decide_check(books, lease, case, form, cmd.reason, now)
    return False, "not an action the system knows"


def _stop_check(books: Books, lease, cmd, form: dict) -> tuple[bool, str]:
    """Recording and lifting a legal stop, with or without an open case: a stop belongs to the unit, and a cure can close the case
    while the stop stays, so staff must be able to lift it afterwards without a case to open."""
    if cmd.action == "record_lien_stop":
        kind, detail, source = form.get("kind"), form.get("detail"), form.get("source")
        if not isinstance(kind, str) or kind not in STOP_WORDS:
            return False, "a legal stop is one of: " + ", ".join(STOP_WORDS.values())
        if not all(isinstance(x, str) and x.strip() for x in (detail, source)):
            return False, "a legal stop needs what was reported and where it came from"
        if dup := next((s for s in open_stops(books, lease.id) if s["kind"] == kind), None):
            return False, f"{_named(books, dup, lease.id)} is already open as a legal stop" + (" on this unit" if dup["lease"] == lease.id else "")
        return True, f"{STOP_WORDS[kind]} is recorded as a legal stop; no step goes ahead until staff record it resolved, with the reason"
    if cmd.source != APPROVER:        # lifting a stop undoes the one thing no approval may override, so only the manager records it
        return False, "only the manager records a legal stop resolved"
    found = next((s for s in open_stops(books, lease.id) if s["id"] == form.get("stop")), None)
    if found is None:
        return False, f"there is no open stop {form.get('stop')} on this unit"
    if not (cmd.reason or "").strip():
        return False, "a stop is lifted with a reason: say why it no longer applies"
    return True, f"{STOP_WORDS[found['kind']]} is recorded as resolved; the file is checked again from the facts"


def _open_check(books: Books, lease, now: datetime) -> tuple[bool, str]:
    if lease.ended:
        return False, "that lease has ended; staff handle it"
    if active_case(books, lease.id):
        return False, "a lien case is already open on this unit"
    # 2. fixed safety: the marker must be there, but it never says the unit is due
    if lease.id not in col.state(books).liens:
        return False, "the unit has not been flagged lien-eligible; staff flag it first"
    # 3 and 4. the owner's schedule, then unsettled payments and the tenancy hold, asked of what is due now
    where, why = col.step_state(books, lease.id, "lien", now, ignore_recorded=True)
    if where != "eligible":
        return False, why
    n = len(open_stops(books, lease.id))
    also = f"; {'a legal stop' if n == 1 else f'{n} legal stops'} on this unit still {'applies' if n == 1 else 'apply'}" if n else ""
    return True, f"due on the owner's schedule and nothing holds it; the lien case is opened for review, no notice is sent{also}"


def _prepare_check(books: Books, lease, case: Case, now: datetime) -> tuple[bool, str]:
    # 2. fixed safety
    if why := stop_reason(books, case):
        return False, why
    if lease.ended:
        return False, "the lease has ended, and an ended lease never means permission to sell; staff handle it"
    if changed := changes(books, case):
        return False, f"the file is waiting for staff to decide: {changed[0]['why']}"
    if case.copies:
        return False, "the notice copy is already prepared and nothing has changed since"
    if not case.addresses:
        return False, "no written address is on file; record the one from the signed agreement or a written change of address first"
    snap = snapshot(books, case)
    if not snap["claim"]:
        return False, "nothing is owed on this unit, so there is no claim to copy"
    # 3 and 4. the owner's schedule, then unsettled payments and the tenancy hold, read again now
    where, why = col.step_state(books, lease.id, "lien", now, ignore_recorded=True)
    if where != "eligible":
        return False, why
    n = len(snap["claim"])
    return True, f"the notice copy is prepared from the ledger as it stands: {money(snap['claim_cents'])} across {n} charge{'s' if n != 1 else ''}"


def _approve_check(books: Books, lease, case: Case, form: dict, now: datetime) -> tuple[bool, str]:
    seen = form.get("fingerprint")
    if not isinstance(seen, str) or not seen:
        return False, "an approval names the file as the manager saw it: send its fingerprint"
    # 2. fixed safety
    if why := stop_reason(books, case):
        return False, why
    if lease.ended:
        return False, "the lease has ended, and an ended lease never means permission to sell; staff handle it"
    if changed := changes(books, case):
        return False, f"the file is waiting for staff to decide: {changed[0]['why']}"
    if not case.copies:
        return False, "the notice copy has not been prepared yet; prepare it first"
    if gaps := [f for f in findings(books, case) if f.status != "met"]:
        return False, "the packet is not complete; it still needs: " + ", ".join(
            f.requirement if f.status == "missing" else f"{f.requirement} ({f.detail})" for f in gaps)
    if seen != fingerprint(books, case):
        return False, "the file changed since you looked at it; look at it again"
    if approval(books, case):
        return False, "the file is already approved as it stands"
    # 3 and 4. the owner's schedule, then unsettled payments and the tenancy hold, read again now
    where, why = col.step_state(books, lease.id, "lien", now, ignore_recorded=True)
    if where != "eligible":
        return False, why
    return True, "approved as the file stands; any change to its facts voids this approval"


def _decide_check(books: Books, lease, case: Case, form: dict, reason: str | None, now: datetime) -> tuple[bool, str]:
    """What staff decide once the file went back to review: resume it from the books as they stand now, or close it. Closing is always
    open to them, whatever holds the file; resuming is a step forward and meets every check a step does."""
    outcome = form.get("outcome")
    if outcome not in ("resume", "close"):
        return False, "staff decide to resume the file or to close it"
    if not (reason or "").strip():
        return False, "a decision is recorded with a reason"
    if outcome == "close":
        return True, "the file is closed by staff; a new lien needs a new episode"
    seen = form.get("fingerprint")
    if not isinstance(seen, str) or not seen:
        return False, "a decision to resume names the file staff saw: send its fingerprint"
    if not changes(books, case):
        return False, "there is nothing to decide: nothing has changed since the notice copy was prepared"
    if why := stop_reason(books, case):
        return False, why
    if lease.ended:
        return False, "the lease has ended, and an ended lease never means permission to sell; staff handle it"
    if seen != fingerprint(books, case):
        return False, "the file changed since you looked at it; look at it again"
    where, why = col.step_state(books, lease.id, "lien", now, ignore_recorded=True)
    if where != "eligible":
        return False, why
    return True, "the file resumes from the books as they stand now; a manager must approve it again"


def _evidence_check(case: Case, form: dict) -> tuple[bool, str]:
    """Evidence is gathered whatever else holds the file: a hold, a legal stop or a review never stops a record being added."""
    kind, facts, fingerprint, supersedes = form.get("kind"), form.get("facts"), form.get("document_sha256"), form.get("supersedes")
    if not isinstance(kind, str) or kind not in EVIDENCE_KINDS:
        return False, f"the file keeps no evidence of the kind {kind}; it keeps {', '.join(EVIDENCE_KINDS)}"
    word = KIND_WORDS[kind]
    try:
        parsed = parse_evidence(kind, facts if isinstance(facts, dict) else {})
    except ValidationError as exc:
        return False, f"the {word} record is not complete: " + "; ".join((f"{'.'.join(map(str, e['loc']))}: " if e["loc"] else "") + e["msg"].removeprefix("Value error, ") for e in exc.errors()[:3])
    if kind in NEEDS_DOCUMENT and not (isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        return False, f"a {word} record needs the fingerprint of its document"
    if kind == "notice" and not case.copies:
        return False, "a notice is drafted from the notice copy; prepare the copy first"
    if kind == "notice" and not any(c["version"] == parsed.copy_version for c in case.copies):
        return False, f"the file has no notice copy {parsed.copy_version}"
    notice_id = getattr(parsed, "notice_id", None)
    if notice_id is not None:
        if not any(e.id == notice_id and e.kind == "notice" for e in case.evidence):
            return False, f"notice {notice_id} is not a notice on this file"
        newer = next((e.id for e in case.evidence if e.supersedes == notice_id), None)
        if newer is not None and supersedes is None:
            return False, f"notice {notice_id} was corrected by notice {newer}; record this for the current notice"
    if supersedes is not None:
        old = next((e for e in case.evidence if isinstance(supersedes, int) and not isinstance(supersedes, bool) and e.id == supersedes), None)
        if old is None:
            return False, f"there is no record {supersedes} on this file to correct"
        if old.kind != kind:
            return False, "a correction must be of the same kind as the record it corrects"
        if kind == "legal_check" and old.facts.get("check") != facts.get("check"):
            return False, "a legal check is corrected only by a check on the same subject"
        if old.facts.get("notice_id") != notice_id:
            return False, "a correction must belong to the same notice as the record it corrects"
        newer = next((e.id for e in case.evidence if e.supersedes == supersedes), None)
        if newer is not None:
            return False, f"record {supersedes} was already corrected by record {newer}; correct the latest one"
    return True, f"the {word} record is added to the file"


def act(led: Ledger, cmd, now: datetime) -> None:
    """Record an allowed command (the gate has checked it)."""
    b, form = led.books, cmd.form or {}
    lease = b.leases[cmd.lease]
    if cmd.action == "open_lien_case":
        cid = b.next_id("LC")
        episode = 1 + sum(1 for c in state(b).cases.values() if c.lease == lease.id)
        led.emit("lien_case.opened", now, {"case": cid, "lease": lease.id, "tenant": lease.tenant, "unit": lease.unit, "episode": episode,
                                           "by": cmd.source}, f"lien_case:{cid}:opened", source=cmd.source, command=cmd.id)
        return
    case = state(b).cases[form["case"]] if cmd.action in CASE_ACTIONS else active_case(b, lease.id)     # the case the check approved, if any
    if cmd.action == "record_lien_evidence":
        n = len(case.evidence) + 1
        facts = parse_evidence(form["kind"], form["facts"]).model_dump(mode="json")
        led.emit("lien_case.evidence_recorded", now, {"case": case.id, "evidence": n, "kind": form["kind"], "facts": facts,
                                                      "document_sha256": form.get("document_sha256"), "supersedes": form.get("supersedes"),
                                                      "by": cmd.source}, f"lien_case:{case.id}:evidence:{n}", source=cmd.source, command=cmd.id)
    elif cmd.action == "record_lien_address":
        n = len(case.addresses) + 1
        led.emit("lien_case.address_recorded", now, {"case": case.id, "address_id": n, "address": form["address"].strip(), "basis": form["basis"],
                                                     "by": cmd.source}, f"lien_case:{case.id}:address:{n}", source=cmd.source, command=cmd.id)
    elif cmd.action == "record_lien_stop":
        sid = b.next_id("LS")
        led.emit("lien_case.stop_recorded", now, {"case": case.id if case else None, "stop": sid, "lease": lease.id, "tenant": lease.tenant, "kind": form["kind"],
                                                  "detail": form["detail"].strip(), "source": form["source"].strip(), "by": cmd.source},
                 f"lien_stop:{sid}:recorded", source=cmd.source, command=cmd.id)
    elif cmd.action == "resolve_lien_stop":
        led.emit("lien_case.stop_resolved", now, {"case": case.id if case else None, "stop": form["stop"], "reason": cmd.reason.strip(), "by": cmd.source},
                 f"lien_stop:{form['stop']}:resolved", source=cmd.source, command=cmd.id)
    elif cmd.action == "decide_lien_case":
        version = len(case.copies) + 1
        led.emit("lien_case.decided", now, {"case": case.id, "outcome": form["outcome"], "reason": cmd.reason.strip(), "version": version,
                                            "by": cmd.source}, f"lien_case:{case.id}:decided:{len(case.decisions) + 1}", source=cmd.source, command=cmd.id)
        if form["outcome"] == "close":
            led.emit("lien_case.closed", now, {"case": case.id, "kind": "staff", "why": cmd.reason.strip(), "by": cmd.source},
                     f"lien_case:{case.id}:closed", source=cmd.source, command=cmd.id)
        else:
            _void_stale(led, case, now)
            led.emit("lien_case.notice_prepared", now, {"case": case.id, "version": version, "snapshot": snapshot(b, case), "by": cmd.source},
                     f"lien_case:{case.id}:copy:{version}", source=cmd.source, command=cmd.id)
        _settle_tasks(led, case, cmd.source, now, cmd.id)
    elif cmd.action == "approve_lien_case":
        _void_stale(led, case, now)                    # an approval the file no longer matches ends here, before a new one is written
        n = len(case.approvals) + 1
        led.emit("lien_case.approved", now, {"case": case.id, "approval": n, "fingerprint": fingerprint(b, case), "parts": parts(b, case),
                                             "by": cmd.source}, f"lien_case:{case.id}:approval:{n}", source=cmd.source, command=cmd.id)
    elif cmd.action == "prepare_lien_notice":
        version = len(case.copies) + 1
        led.emit("lien_case.notice_prepared", now, {"case": case.id, "version": version, "snapshot": snapshot(b, case), "by": cmd.source},
                 f"lien_case:{case.id}:copy:{version}", source=cmd.source, command=cmd.id)
