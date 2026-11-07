"""Lien evidence review for a delinquent account: what has been documented, what is missing, and what can happen
when, under one reviewed rule version.

This desk never says a file is legally compliant or that a sale may proceed. It checks the evidence an operator
records against a written rule version and reports every open item, each tagged as a statutory requirement or as
an operator policy this desk applies on top of the statute. A complete file is "Review packet assembled": listed
evidence checks completed; sale authorization is outside this desk.

Florida (Fla. Stat. 83.801-83.809) is the one executable rule version. New York is reference material only, so a
New York case always goes to counsel. Every other state has no reviewed rule version here.

Dates: every deadline is a calendar date at the facility, in the facility's own time zone, which must come from the
FMS (there is no default). A stated deadline stays open through its whole local day; the next step can start the
day after."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, model_validator

# ------------------------------------------------------------------ the rule version

@dataclass(frozen=True)
class RuleVersion:
    label: str
    jurisdiction: str
    citations: tuple[str, ...]
    statute_fetched_on: str
    review_note: str
    policies: tuple[str, ...]


FL_R1 = RuleVersion(
    label="FL-r1",
    jurisdiction="FL",
    citations=("Fla. Stat. 83.803", "Fla. Stat. 83.805", "Fla. Stat. 83.806", "Fla. Stat. 83.808"),
    statute_fetched_on="2026-11-06",
    review_note=("Checked against the statute text published by the Florida Legislature, fetched 2026-11-06 "
                 "(83.806 last amended by ch. 2017-82). Not legal review."),
    policies=(
        "Posting is documented on the day the notice is delivered; any other sequence goes to review.",
        "When e-mail is confirmed only by a later reply or return receipt, delivery is dated by that confirmation.",
        "When e-mail is unconfirmed, the demand period is counted from the fallback mailing.",
        "The two publications are exactly seven days apart; any other spacing goes to review.",
        "Every legal check (military disclosure and service, bankruptcy, vehicle or watercraft, dispute, secured "
        "party) is recorded as yes, no or unknown with its source; yes or unknown goes to counsel.",
        "Any payment the FMS shows after the notice was delivered reopens review of the notice.",
        "The notice names the FMS ledger snapshot its claim came from, taken before the delivery day, and each itemized "
        "charge is an open charge on it.",
        "The no-newspaper posting route of 83.806(4)(c) is not evaluated here; such files go to counsel.",
        "Both publications are in the same newspaper, and every advertisement recorded for the notice must agree.",
        "The fallback mailing for an unconfirmed e-mail is deposited on or after the e-mail was sent.",
        "A document filed for two notices, or on another case, goes to review.",
    ),
)
# a rule version is its label and the exact code that evaluates it: change this file and old evaluations no longer
# claim to replay under it
RULES = f"{FL_R1.label}+" + __import__("hashlib").sha256(open(__file__, "rb").read()).hexdigest()[:12]

REFERENCE_ONLY = {"NY": "N.Y. Lien Law 182 is imported as reference material only; New York cases are not evaluated here."}

# ------------------------------------------------------------------ dates at the facility

class DateProblem(ValueError):
    """A date or time that cannot be placed on the facility's calendar without guessing."""


def facility_zone(name: str | None) -> ZoneInfo:
    if not name:
        raise DateProblem("the FMS gave no time zone for this facility")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DateProblem(f"unknown time zone {name!r}") from exc


def local_date(instant: datetime, zone: ZoneInfo) -> date:
    """The facility's calendar date at an instant. A naive instant is read as UTC, the desk's storage convention."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(zone).date()


def local_instant(wall_clock: datetime, zone: ZoneInfo) -> datetime:
    """A facility wall-clock time as an exact instant. A time that occurs twice (clocks going back) or not at all
    (clocks going forward) is refused rather than guessed."""
    if wall_clock.tzinfo is not None:
        return wall_clock
    early, late = wall_clock.replace(tzinfo=zone, fold=0), wall_clock.replace(tzinfo=zone, fold=1)
    if early.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != wall_clock:
        raise DateProblem(f"{wall_clock:%Y-%m-%d %H:%M} does not exist at this facility (clocks moved forward)")
    if early.utcoffset() != late.utcoffset():
        raise DateProblem(f"{wall_clock:%Y-%m-%d %H:%M} happens twice at this facility; give the UTC offset")
    return early


def clock_time(text: str):
    """A stated time of day in any common clear form: 10:00 AM, 10 AM, 14:00."""
    for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(text.strip().upper(), fmt).time()
        except ValueError:
            continue
    raise ValueError(f"{text!r} is not a clear time of day")


def plus_days(d: date, days: int) -> date:
    """Calendar days, never hours: 14 days after March 1 is March 15 whatever the clocks do in between."""
    return d + timedelta(days=days)

# ------------------------------------------------------------------ evidence the operator records

YesNoUnknown = Literal["yes", "no", "unknown"]


class ItemizedCharge(BaseModel):
    description: str = Field(min_length=2)
    category: Literal["rent", "fee", "tax", "other"] = Field(description="anything but rent needs the agreement's authorization")
    amount_cents: int = Field(gt=0)
    due_on: date


class Notice(BaseModel):
    itemized_charges: list[ItemizedCharge] = Field(min_length=1, description="the itemized statement of the claim, as printed")
    itemized_total_cents: int = Field(gt=0)
    sum_due_since: date
    ledger_snapshot_id: int | None = Field(default=None, description="the desk's FMS ledger snapshot the claim was taken from")
    property_description: str = Field(min_length=3)
    description_matches_rental_agreement: YesNoUnknown
    demand_deadline: date
    sale_statement: bool = Field(description="the conspicuous statement that unpaid property will be advertised and sold")
    sale_date: date
    sale_time: str = Field(min_length=3, description="the specified time of the sale, as printed")
    sale_place: str = Field(min_length=3)
    owner_name: str = Field(min_length=2)
    owner_street_address: str = Field(min_length=5)
    owner_phone: str = Field(min_length=7)


class MailDeposit(BaseModel):
    notice_id: int = Field(description="the notice record this mailing carried")
    deposited_on: date
    first_class: bool = Field(description="sent by USPS first-class mail")
    certificate_of_mailing: bool
    postage_prepaid: bool
    address: str = Field(min_length=5)
    address_basis: Literal["rental agreement", "written change of address"]


class InPersonDelivery(BaseModel):
    notice_id: int
    delivered_on: date
    delivered_by: str = Field(min_length=2)
    delivered_to: str = Field(min_length=2)


class EmailDelivery(BaseModel):
    notice_id: int
    sent_on: date
    sent_to: str = Field(min_length=3)
    address_basis: Literal["rental agreement", "written change of address"] = Field(
        description="why this is the tenant's last known e-mail address")
    confirmation: Literal["response", "return receipt", "delivery confirmation"] | None = None
    confirmation_from: str | None = None
    confirmed_on: date | None = None
    delivered_on: date | None = Field(default=None, description="only when the confirmation itself says when it was delivered")

    @model_validator(mode="after")
    def _chronology(self):
        if self.confirmation and self.confirmed_on is None:
            raise ValueError("a confirmation needs the date it was received")
        if self.delivered_on and not (self.sent_on <= self.delivered_on <= (self.confirmed_on or self.delivered_on)):
            raise ValueError("delivery must fall between sending and the confirmation")
        if self.confirmed_on and self.confirmed_on < self.sent_on:
            raise ValueError("a confirmation cannot come before the e-mail was sent")
        return self


class Posting(BaseModel):
    notice_id: int
    posted_on: date
    location: Literal["facility", "unit"]
    location_detail: str = Field(min_length=3, description="where exactly, e.g. the office door or the unit's door")
    conspicuous: YesNoUnknown


class Publication(BaseModel):
    notice_id: int = Field(description="the notice whose property and sale this advertisement describes")
    newspaper: str = Field(min_length=2)
    general_circulation_in_facility_area: YesNoUnknown
    circulation_source: str = Field(min_length=3, description="how that was established")
    published_on: date
    sale_date: date
    sale_time: str = Field(min_length=3)
    sale_place: str = Field(min_length=3)
    includes_property_description: bool
    includes_facility_address: bool
    includes_tenant_name: bool
    includes_sale_time_place_manner: bool


class LegalCheck(BaseModel):
    check: Literal["military_disclosure_provision", "military_service", "bankruptcy", "vehicle_or_watercraft", "dispute", "secured_party",
                   "document_of_title_issued", "fees_authorized_by_rental_agreement", "no_newspaper_in_area", "fee_reasonable"]
    answer: YesNoUnknown
    source: str = Field(min_length=3)
    observed_on: date


# fields that record something that happened; stated future dates (the payment deadline, the sale date) are not events
EVENT_DATES = ("deposited_on", "delivered_on", "sent_on", "confirmed_on", "posted_on", "published_on", "observed_on")

# every kind but a legal check rests on a document the desk keeps (the notice, the certificate, the confirmation, the
# acknowledgment, the posting photo, the publisher's affidavit); a legal check names its source instead
NEEDS_DOCUMENT = ("notice", "mail_deposit", "in_person_delivery", "email_delivery", "posting", "publication")

EVIDENCE_KINDS: dict[str, type[BaseModel]] = {
    "notice": Notice, "mail_deposit": MailDeposit, "in_person_delivery": InPersonDelivery, "email_delivery": EmailDelivery,
    "posting": Posting, "publication": Publication, "legal_check": LegalCheck,
}


def parse_evidence(kind: str, facts: dict) -> BaseModel:
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unsupported evidence kind {kind!r}; supported: {', '.join(EVIDENCE_KINDS)}")
    return EVIDENCE_KINDS[kind].model_validate(facts)

def dollars(cents) -> str:
    """An amount as an operator reads it; findings are written this way, never converted afterwards."""
    return f"${int(cents) / 100:,.2f}"


def on(value) -> str:
    """A date as an operator reads it. Anything that is not a date is kept as it is."""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        try:
            value = date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value)
    return value.strftime("%b %-d, %Y")


# ------------------------------------------------------------------ evaluation

@dataclass
class Finding:
    requirement: str
    basis: str           # "statute 83.806(1)" or "policy"
    status: Literal["met", "missing", "review", "waiting", "blocked"]
    detail: str
    action: str = ""


@dataclass
class Evaluation:
    rule_version: str | None
    state: str
    summary: str
    findings: list[Finding] = field(default_factory=list)
    timing: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseFacts:
    """Everything the evaluation reads, frozen so the same facts always give the same answer."""
    jurisdiction: str
    facility_time_zone: str | None
    total_due_cents: int | None          # None: the FMS could not be read
    payments_after: list[str] | None     # local dates of FMS payments on the lease, ISO format; None if unreadable
    today: str                           # the facility's local date when evaluated, ISO format
    evidence: list[dict]                 # {"id", "kind", "facts", "document_sha256"} for every current record
    notice_snapshots: dict = field(default_factory=dict)   # snapshot id -> {"total_due", "observed_on"} for snapshots notices name
    ledger_changes: list[str] | None = None   # changes since the notice's snapshot to the charges it claimed
    rules: str = ""                      # the rule version and the fingerprint of the code that evaluates it
    reused_elsewhere: list[str] = field(default_factory=list)   # document hashes this case shares with another case

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()


STATES = ("FMS data unavailable", "FMS reports paid", "Counsel review required", "Account changed: re-review",
          "Evidence incomplete", "Waiting for a timing threshold", "Review packet assembled")
SUMMARIES = {
    "Review packet assembled": ("Every listed requirement has a record, with its document or transcription kept where one is "
                                "needed. Nothing here authenticates those documents: a person still has to check them, and sale "
                                "authorization is outside this desk."),
    "FMS reports paid": "The FMS shows nothing due. This case leaves active lien work; releasing any lien is a separate review.",
}


def match_charges(items, open_charges) -> list[tuple]:
    """Pair each itemized charge with an open ledger charge of the same amount and a matching, non-empty name."""
    left = [dict(c) for c in open_charges]
    pairs = []
    for item in items:
        m = next((c for c in left if c["open_cents"] == item.amount_cents and c["name"]
                  and (item.description.lower() in c["name"] or c["name"] in item.description.lower())), None)
        if m is not None:
            left.remove(m)
        pairs.append((item, m))
    return pairs


def evaluate(facts: CaseFacts) -> Evaluation:
    if facts.jurisdiction in REFERENCE_ONLY:
        return Evaluation(None, "Counsel review required", REFERENCE_ONLY[facts.jurisdiction],
                          [Finding("reviewed rule version", "policy", "review", REFERENCE_ONLY[facts.jurisdiction], "send to counsel")])
    if facts.jurisdiction != FL_R1.jurisdiction:
        note = f"There is no reviewed rule version for {facts.jurisdiction or 'this state'}."
        return Evaluation(None, "Counsel review required", note, [Finding("reviewed rule version", "policy", "review", note, "send to counsel")])
    return _evaluate_fl(facts)


def _evaluate_fl(facts: CaseFacts) -> Evaluation:
    out: list[Finding] = []
    timing: dict[str, str] = {}
    try:
        facility_zone(facts.facility_time_zone)
    except DateProblem as exc:
        out.append(Finding("facility time zone", "policy", "blocked", str(exc), "fix the facility's time zone in the FMS"))
        return _finish(out, timing)
    if facts.total_due_cents is None:
        out.append(Finding("current balance", "policy", "blocked", "the FMS ledger could not be read", "retry when the FMS answers"))
        return _finish(out, timing)
    if facts.payments_after is None:
        out.append(Finding("payments on the lease", "policy", "blocked",
                           "the FMS payments could not be read or dated, so a payment after the notice cannot be ruled out",
                           "retry when the FMS answers"))
        return _finish(out, timing)
    if facts.total_due_cents <= 0:
        out.append(Finding("balance due", "statute 83.806(6)", "blocked", "the FMS shows nothing due (or a credit)", "take the case out of lien work"))
        return _finish(out, timing)
    today = date.fromisoformat(facts.today)
    ev = {k: [e for e in facts.evidence if e["kind"] == k] for k in EVIDENCE_KINDS}
    for e in facts.evidence:
        for name in EVENT_DATES:
            when = e["facts"].get(name)
            if when and date.fromisoformat(str(when)) > today:
                out.append(Finding("evidence dates", "policy", "review", f"{e['kind'].replace('_', ' ')} {e['id']} records {name} {on(when)}, after today",
                                   "correct the record; evidence cannot describe something that has not happened"))

    # legal checks: every one recorded, and only a clean answer lets the file continue without counsel. Several current
    # records for one check must agree; a later "no" never erases an earlier "yes" unless it supersedes that record.
    # fail closed: any claimed charge that is not rent, by the notice's category or the FMS ledger's, needs the rental
    # agreement's authorization; the statute speaks of late fees, so other charges are policy
    latest = [Notice.model_validate(e["facts"]) for e in ev["notice"][-1:]]   # only the notice being evaluated counts
    latest_items = [c for n in latest for c in n.itemized_charges]
    latest_snaps = [facts.notice_snapshots.get(str(n.ledger_snapshot_id)) for n in latest if n.ledger_snapshot_id is not None]
    matched = [m for _, m in match_charges(latest_items, latest_snaps[0].get("open_charges", []) if latest_snaps and latest_snaps[0] else [])
               if m is not None]
    ledger_categories = {m.get("category", "") for m in matched}
    claims_fee = any(c.category != "rent" for c in latest_items) or any(cat != "RENT" for cat in ledger_categories)
    fee_basis = "statute 83.808(3)" if any(c.category == "fee" for c in latest_items) or "FEE" in ledger_categories else "policy"
    # 83.808(3): a late fee of $20 or 20 percent of the monthly rent, whichever is greater, is reasonable; above that,
    # reasonableness needs its own sourced check
    # the monthly rent is taken as the largest single rent line (several months claimed must not raise the threshold)
    monthly_rent = max((c.amount_cents for c in latest_items if c.category == "rent"), default=0)
    fees = [c for c in latest_items if c.category == "fee"]
    above_safe_harbor = [c for c in fees if c.amount_cents > max(2000, monthly_rent // 5)]
    answers: dict[str, list[LegalCheck]] = {}
    for e in ev["legal_check"]:
        c = LegalCheck.model_validate(e["facts"])
        answers.setdefault(c.check, []).append(c)
    for check, basis, clean in (("military_disclosure_provision", "statute 83.808(2)", "yes"), ("military_service", "policy", "no"),
                                ("bankruptcy", "policy", "no"), ("vehicle_or_watercraft", "statute 83.806(10)", "no"),
                                ("dispute", "policy", "no"), ("secured_party", "statute 83.805", "no"),
                                ("document_of_title_issued", "statute 83.803(5)", "no"),
                                ("fees_authorized_by_rental_agreement", fee_basis, "yes"),
                                ("no_newspaper_in_area", "statute 83.806(4)(c)", "no"),
                                ("fee_reasonable", "statute 83.808(3)", "yes")):
        if check == "fees_authorized_by_rental_agreement" and not claims_fee:
            continue   # the agreement's fee terms matter only when the notice claims a fee
        if check == "fee_reasonable" and not above_safe_harbor:
            continue   # within the statute's safe harbor, or no fee claimed
        records = answers.get(check, [])
        label = check.replace("_", " ")
        said = "; ".join(f"{c.answer} ({c.source}, {on(c.observed_on)})" for c in records)
        if not records:
            out.append(Finding(label, basis, "missing", f"no {label} check is recorded", "record yes, no or unknown with its source"))
        elif len({c.answer for c in records}) > 1:
            out.append(Finding(label, basis, "review", f"conflicting {label} checks: {said}", "resolve by superseding the wrong record; send to counsel"))
        elif records[0].answer != clean:
            out.append(Finding(label, basis, "review", f"{label}: {said}", "send to counsel"))
        else:
            out.append(Finding(label, basis, "met", f"{label}: {said}"))

    for digest in facts.reused_elsewhere:
        out.append(Finding("document reuse", "policy", "review", "a document in this file is also filed on another case",
                           "attach the document that belongs to this tenant and notice"))
    # one document behind records for two different notices of this case cannot be both
    by_hash: dict[str, set] = {}
    for e in facts.evidence:
        if e.get("document_sha256"):
            by_hash.setdefault(e["document_sha256"], set()).add(e["id"] if e["kind"] == "notice" else e["facts"].get("notice_id"))
    for digest, ids in by_hash.items():
        if len(ids) > 1:
            out.append(Finding("document reuse", "policy", "review", f"one document is filed as evidence for notices {sorted(ids)}",
                               "attach the document that belongs to each notice"))

    # every piece of evidence but a legal check needs its document kept
    for e in facts.evidence:
        if e["kind"] in NEEDS_DOCUMENT and not e.get("document_sha256"):
            out.append(Finding(f"{e['kind'].replace('_', ' ')} document", "policy", "missing",
                               f"no document is kept for {e['kind'].replace('_', ' ')} {e['id']}", "attach the document or its transcription"))

    # the notice itself: the latest notice record; evidence counts only if it names that record
    notices = ev["notice"]
    if not notices:
        out.append(Finding("written notice", "statute 83.806(2)", "missing", "no notice is recorded", "record the notice as sent"))
        return _finish(out, timing)
    notice_id = notices[-1]["id"]
    notice = Notice.model_validate(notices[-1]["facts"])
    if not notice.sale_statement:
        out.append(Finding("sale statement", "statute 83.806(2)(d)", "missing", "the notice lacks the conspicuous advertisement and sale statement",
                           "correct the notice; send to review"))
    if sum(c.amount_cents for c in notice.itemized_charges) != notice.itemized_total_cents:
        out.append(Finding("itemized statement", "statute 83.806(2)(a)", "missing", "the itemized charges do not add up to the stated total",
                           "correct the notice"))
    if notice.description_matches_rental_agreement != "yes":
        out.append(Finding("property description", "statute 83.806(2)(b)", "review",
                           f"match with the rental agreement's description: {notice.description_matches_rental_agreement}", "send to review"))
    snap = facts.notice_snapshots.get(str(notice.ledger_snapshot_id)) if notice.ledger_snapshot_id is not None else None
    if snap is None:
        out.append(Finding("claim at notice time", "policy", "missing",
                           "the notice does not name an FMS ledger snapshot of this case that its claim was taken from", "record the snapshot"))
    else:
        if snap["total_due"] != notice.itemized_total_cents:
            out.append(Finding("claim at notice time", "statute 83.806(2)(a)", "review",
                               f"the notice claims {dollars(notice.itemized_total_cents)}; the FMS ledger then showed {dollars(snap['total_due'])}", "send to review"))
        for item, match in match_charges(notice.itemized_charges, snap.get("open_charges", [])):
            ledger_category = {"RENT": "rent", "FEE": "fee"}.get((match or {}).get("category", ""),
                                                                 "tax" if (match or {}).get("category", "").startswith("TAX") else "other")
            if match is not None and (not match.get("category") or ledger_category != item.category):
                out.append(Finding("charge category", "policy", "review",
                                   f"\"{item.description}\" is {item.category} in the notice and {match.get('category') or 'unclassified'} on the ledger",
                                   "correct the notice; send to review"))
            if match is not None and match.get("due_on") and match["due_on"] != item.due_on.isoformat():
                out.append(Finding("date the charge became due", "statute 83.806(2)(a)", "review",
                                   f"\"{item.description}\" is dated {on(item.due_on)} in the notice and {on(match['due_on'])} on the ledger",
                                   "correct the notice; send to review"))
            if match is None:
                out.append(Finding("itemized charge on the ledger", "statute 83.806(2)(a)", "review",
                                   f"\"{item.description}\" for {dollars(item.amount_cents)} is not an open charge on the FMS ledger snapshot",
                                   "correct the notice; send to review"))
    try:
        zone = facility_zone(facts.facility_time_zone)
        local_instant(datetime.combine(notice.sale_date, clock_time(notice.sale_time)), zone)
    except ValueError as exc:
        out.append(Finding("sale time", "statute 83.806(2)(d)", "review",
                           f"the stated sale time {notice.sale_time!r} on {on(notice.sale_date)} is not a clear local time: {exc}",
                           "state the sale time as, for example, 10:00 AM; send to review"))
    if sum(ch.isdigit() for ch in notice.owner_phone) < 10:
        out.append(Finding("owner contact", "statute 83.806(2)(e)", "missing", "the owner's telephone number is incomplete", "correct the notice"))

    def for_notice(kind):
        return [e for e in ev[kind] if e["facts"].get("notice_id") == notice_id]

    # delivery
    deliveries: list[tuple[date, str]] = []
    for e in for_notice("mail_deposit"):
        m = MailDeposit.model_validate(e["facts"])
        if m.first_class and m.certificate_of_mailing and m.postage_prepaid:
            deliveries.append((m.deposited_on, f"first-class mail deposited {on(m.deposited_on)} with a certificate of mailing (presumed delivered)"))
        else:
            out.append(Finding("mail delivery", "statute 83.806(1), (3)", "missing",
                               "the mailing is not recorded as first-class with a certificate of mailing and prepaid postage",
                               "record the first-class mailing and its certificate"))
    for e in for_notice("in_person_delivery"):
        p = InPersonDelivery.model_validate(e["facts"])
        deliveries.append((p.delivered_on, f"delivered in person on {on(p.delivered_on)} by {p.delivered_by}"))
    unconfirmed_email, unconfirmed_sent = False, None
    for e in for_notice("email_delivery"):
        m = EmailDelivery.model_validate(e["facts"])
        same_address = m.confirmation_from is not None and m.confirmation_from.strip().lower() == m.sent_to.strip().lower()
        if m.confirmation and same_address and (m.delivered_on or m.confirmed_on):
            when = m.delivered_on or m.confirmed_on
            assert when is not None
            basis = "statute 83.806(1)" if m.delivered_on else "policy"
            deliveries.append((when, f"e-mail to {m.sent_to}, {m.confirmation} from the same address, delivery dated {on(when)}"
                                     + ("" if m.delivered_on else " by the confirmation (policy)")))
            out.append(Finding("e-mail confirmation", basis, "met", f"{m.confirmation} from {m.confirmation_from}"))
        else:
            unconfirmed_email = True
            unconfirmed_sent = max(unconfirmed_sent or m.sent_on, m.sent_on)
    fallback = [d for d in deliveries if "mail deposited" in d[1] and unconfirmed_sent and d[0] >= unconfirmed_sent]
    if unconfirmed_email and not fallback:
        out.append(Finding("e-mail fallback mailing", "statute 83.806(1)", "missing",
                           "the e-mail has no response, return receipt or delivery confirmation from the same address, and no "
                           "first-class mailing on or after it is recorded (policy: the fallback follows the e-mail)",
                           "send the notice by first-class mail with a certificate of mailing and record it"))
    if not deliveries:
        out.append(Finding("notice delivery", "statute 83.806(1)", "missing", f"no qualifying delivery of notice {notice_id} is recorded",
                           "record the certificate of mailing, the in-person delivery, or the e-mail confirmation"))
        return _finish(out, timing)
    delivered_on, how = min(deliveries)
    if unconfirmed_email and fallback:
        delivered_on, how = min(fallback)   # policy: count from the fallback mailing
    out.append(Finding("notice delivery", "statute 83.806(1)", "met", how))
    timing["notice delivered"] = delivered_on.isoformat()
    if notice.sum_due_since > delivered_on:
        out.append(Finding("date the sum became due", "statute 83.806(2)(a)", "review",
                           f"the notice says the sum became due {on(notice.sum_due_since)}, after it was delivered", "correct the notice"))
    not_yet_due = [c for c in notice.itemized_charges if c.due_on > delivered_on]
    if not_yet_due:
        out.append(Finding("charges due at notice time", "statute 83.806(2)(a)", "review",
                           f"\"{not_yet_due[0].description}\" fell due {on(not_yet_due[0].due_on)}, after the notice was delivered on {on(delivered_on)}",
                           "claim only what was due when the notice was delivered"))
    earliest_due = min(c.due_on for c in notice.itemized_charges)
    if notice.sum_due_since != earliest_due:
        out.append(Finding("date the sum became due", "statute 83.806(2)(a)", "review",
                           f"the notice says the sum became due {on(notice.sum_due_since)}; its earliest itemized charge fell due {on(earliest_due)}",
                           "correct the notice"))
    if snap is not None and date.fromisoformat(snap["observed_on"]) >= delivered_on:
        out.append(Finding("claim at notice time", "policy", "review",
                           f"the ledger snapshot is from {on(snap['observed_on'])}, not before the notice was delivered on {on(delivered_on)}",
                           "name a snapshot from before the delivery day"))

    # posting at the facility or on the unit
    postings = [Posting.model_validate(e["facts"]) for e in for_notice("posting")]
    if not postings:
        out.append(Finding("conspicuous posting", "statute 83.806(1)", "missing", "no posting is recorded", "post the notice and record it"))
    elif any(p.conspicuous != "yes" for p in postings):
        out.append(Finding("conspicuous posting", "statute 83.806(1)", "review",
                           "the posting is not recorded as conspicuous: " + "; ".join(f"{p.location_detail}: {p.conspicuous}" for p in postings),
                           "send to review"))
    elif any(p.posted_on == delivered_on for p in postings):
        out.append(Finding("conspicuous posting", "statute 83.806(1)", "met", f"posted on {on(delivered_on)} ({postings[0].location_detail})"))
    else:
        out.append(Finding("posting date", "policy", "review", f"posted on {on(postings[-1].posted_on)}, notice delivered {on(delivered_on)}", "send to review"))

    # the demand period: at least 14 days after delivery, deadline open through its local day
    earliest_deadline = plus_days(delivered_on, 14)
    timing["minimum payment deadline under the statute"] = earliest_deadline.isoformat()
    if notice.demand_deadline < earliest_deadline:
        out.append(Finding("demand period", "statute 83.806(2)(c)", "review",
                           f"the notice gives until {on(notice.demand_deadline)}; delivered {on(delivered_on)}, it must give until {on(earliest_deadline)} or later",
                           "correct the notice; the printed deadline is not extended here"))
        return _finish(out, timing)
    out.append(Finding("demand period", "statute 83.806(2)(c)", "met", f"payment deadline {on(notice.demand_deadline)}"))
    timing["payment deadline in the notice"] = notice.demand_deadline.isoformat()

    # anything paid, waived, reversed or changed on the claimed charges after delivery reopens the notice
    after = sorted(p for p in facts.payments_after if p >= delivered_on.isoformat())
    if after:
        when = ("on the delivery day; the order is unknown, so it is treated as after (policy)" if after[0] == delivered_on.isoformat()
                else f"on {on(after[0])}, after the notice was delivered")
        out.append(Finding("account unchanged since notice", "policy", "review", f"the FMS shows a payment {when}",
                           "re-review the notice against the current account"))
    for change in facts.ledger_changes or []:
        out.append(Finding("account unchanged since notice", "policy", "review", change, "re-review the notice against the current account"))

    first_publication_allowed = plus_days(notice.demand_deadline, 1)
    timing["calculated earliest first publication (policy)"] = first_publication_allowed.isoformat()
    pubs = sorted((Publication.model_validate(e["facts"]) for e in for_notice("publication")), key=lambda p: p.published_on)
    if not pubs:
        if today < first_publication_allowed:
            out.append(Finding("advertisement", "statute 83.806(4)", "waiting", f"publication cannot start before {on(first_publication_allowed)}"))
        else:
            out.append(Finding("advertisement", "statute 83.806(4)", "missing", f"no publication for notice {notice_id} is recorded",
                               "publish once a week for two consecutive weeks and record each"))
        return _finish(out, timing)
    first = pubs[0]
    if first.published_on < first_publication_allowed:
        out.append(Finding("first publication", "statute 83.806(4)", "review",
                           f"published {on(first.published_on)}, before the payment deadline {on(notice.demand_deadline)} had passed", "send to review"))
    second_due = plus_days(first.published_on, 7)
    timing["calculated second publication (policy)"] = second_due.isoformat()
    if len(pubs) < 2:
        if today < second_due:
            out.append(Finding("second publication", "statute 83.806(4)", "waiting", f"second publication due {on(second_due)}"))
        else:
            out.append(Finding("second publication", "statute 83.806(4)", "missing", f"no second publication is recorded (due {on(second_due)})",
                               "record the second publication"))
    elif pubs[1].published_on != second_due:
        out.append(Finding("publication spacing", "policy", "review",
                           f"publications on {on(first.published_on)} and {on(pubs[1].published_on)}, not seven days apart", "send to review"))
    elif pubs[1].newspaper.strip().lower() != first.newspaper.strip().lower():
        out.append(Finding("same newspaper both weeks", "policy", "review",
                           f"published in {first.newspaper} and then in {pubs[1].newspaper}", "send to review"))
    else:
        out.append(Finding("advertisement", "statute 83.806(4)", "met", f"published {on(first.published_on)} and {on(pubs[1].published_on)} in {first.newspaper}"))
    for p in pubs:
        if p.general_circulation_in_facility_area != "yes":
            out.append(Finding("newspaper of general circulation in the area", "statute 83.806(4)", "review",
                               f"{p.newspaper}: {p.general_circulation_in_facility_area} ({p.circulation_source})", "send to review"))
        lacking = [name for name, ok in (("property description", p.includes_property_description), ("facility address", p.includes_facility_address),
                                         ("tenant name", p.includes_tenant_name), ("time, place and manner of sale", p.includes_sale_time_place_manner)) if not ok]
        if lacking:
            out.append(Finding("advertisement content", "statute 83.806(4)(b)", "missing",
                               f"the {on(p.published_on)} advertisement lacks: {', '.join(lacking)}", "correct and republish; send to review"))

    sale_threshold = plus_days(first.published_on, 15)
    timing["earliest sale under the statute (15 days after first publication)"] = sale_threshold.isoformat()
    stated = {(notice.sale_date, notice.sale_time.strip().lower(), notice.sale_place.strip().lower())} | {
        (p.sale_date, p.sale_time.strip().lower(), p.sale_place.strip().lower()) for p in pubs}
    if len(stated) > 1:
        out.append(Finding("sale terms agree", "statute 83.806(5)", "review", "the notice and the advertisements state different sale dates, times or places",
                           "send to review"))
    if notice.sale_date < sale_threshold:
        out.append(Finding("sale date", "statute 83.806(4)(b)3", "review",
                           f"the stated sale date {on(notice.sale_date)} is earlier than {on(sale_threshold)}, fifteen days after the first publication",
                           "send to review"))
    return _finish(out, timing)


def _finish(findings: list[Finding], timing: dict[str, str]) -> Evaluation:
    statuses = {f.status for f in findings}
    if any(f.status == "blocked" and f.requirement in ("facility time zone", "current balance", "payments on the lease") for f in findings):
        state = "FMS data unavailable"
    elif any(f.status == "blocked" and f.requirement == "balance due" for f in findings):
        state = "FMS reports paid"
    elif any(f.status == "review" and f.requirement != "account unchanged since notice" for f in findings):
        state = "Counsel review required"
    elif any(f.requirement == "account unchanged since notice" for f in findings):
        state = "Account changed: re-review"
    elif "missing" in statuses:
        state = "Evidence incomplete"
    elif "waiting" in statuses:
        state = "Waiting for a timing threshold"
    else:
        state = "Review packet assembled"
    open_items = sum(f.status != "met" for f in findings)
    summary = SUMMARIES.get(state, f"{open_items} open item{'s' if open_items != 1 else ''}.")
    return Evaluation(RULES, state, summary, findings, timing)


__all__ = ["FL_R1", "CaseFacts", "DateProblem", "EVIDENCE_KINDS", "Evaluation", "Finding", "STATES", "ValidationError",
           "evaluate", "facility_zone", "local_date", "local_instant", "parse_evidence", "plus_days"]
