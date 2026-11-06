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

from pydantic import BaseModel, Field, ValidationError

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
    ),
)
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


def plus_days(d: date, days: int) -> date:
    """Calendar days, never hours: 14 days after March 1 is March 15 whatever the clocks do in between."""
    return d + timedelta(days=days)

# ------------------------------------------------------------------ evidence the operator records

YesNoUnknown = Literal["yes", "no", "unknown"]


class Notice(BaseModel):
    notice_version: str
    itemized_total_cents: int = Field(ge=0)
    sum_due_since: date
    property_description: str = Field(min_length=3)
    demand_deadline: date
    sale_statement: bool = Field(description="the conspicuous statement that unpaid property will be advertised and sold")
    sale_date: date
    sale_place: str = Field(min_length=3)
    owner_name: str = Field(min_length=2)
    owner_street_address: str = Field(min_length=5)
    owner_phone: str = Field(min_length=7)


class MailDeposit(BaseModel):
    notice_version: str
    deposited_on: date
    certificate_of_mailing: bool
    postage_prepaid: bool
    address: str = Field(min_length=5)
    address_basis: Literal["rental agreement", "written change of address"]


class InPersonDelivery(BaseModel):
    notice_version: str
    delivered_on: date
    delivered_by: str = Field(min_length=2)
    delivered_to: str = Field(min_length=2)


class EmailDelivery(BaseModel):
    notice_version: str
    sent_on: date
    sent_to: str = Field(min_length=3)
    confirmation: Literal["response", "return receipt", "delivery confirmation"] | None = None
    confirmation_from: str | None = None
    confirmed_on: date | None = None
    delivered_on: date | None = Field(default=None, description="only when the confirmation itself says when it was delivered")


class Posting(BaseModel):
    notice_version: str
    posted_on: date
    location: Literal["facility", "unit"]


class Publication(BaseModel):
    newspaper: str = Field(min_length=2)
    published_on: date
    sale_date: date
    sale_place: str = Field(min_length=3)
    includes_property_description: bool
    includes_facility_address: bool
    includes_tenant_name: bool
    includes_sale_time_place_manner: bool


class LegalCheck(BaseModel):
    check: Literal["military_disclosure_provision", "military_service", "bankruptcy", "vehicle_or_watercraft", "dispute", "secured_party"]
    answer: YesNoUnknown
    source: str = Field(min_length=3)
    observed_on: date


EVIDENCE_KINDS: dict[str, type[BaseModel]] = {
    "notice": Notice, "mail_deposit": MailDeposit, "in_person_delivery": InPersonDelivery, "email_delivery": EmailDelivery,
    "posting": Posting, "publication": Publication, "legal_check": LegalCheck,
}


def parse_evidence(kind: str, facts: dict) -> BaseModel:
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unsupported evidence kind {kind!r}; supported: {', '.join(EVIDENCE_KINDS)}")
    return EVIDENCE_KINDS[kind].model_validate(facts)

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
    payments_after: list[str]            # local dates of FMS payments on the lease, ISO format
    today: str                           # the facility's local date when evaluated, ISO format
    evidence: list[dict]                 # {"id", "kind", "facts"} for every current (not superseded) record

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()


STATES = ("FMS data unavailable", "FMS reports paid", "Counsel review required", "Account changed: re-review",
          "Evidence incomplete", "Waiting for a timing threshold", "Review packet assembled")
SUMMARIES = {
    "Review packet assembled": "Listed evidence checks completed; sale authorization is outside this desk.",
    "FMS reports paid": "The FMS shows nothing due. This case leaves active lien work; releasing any lien is a separate review.",
}


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
    if facts.total_due_cents == 0:
        out.append(Finding("balance due", "statute 83.806(6)", "blocked", "the FMS shows nothing due", "take the case out of lien work"))
        return _finish(out, timing)
    today = date.fromisoformat(facts.today)
    ev = {k: [e for e in facts.evidence if e["kind"] == k] for k in EVIDENCE_KINDS}

    # legal checks: every one recorded, and only a "no" lets the file continue without counsel
    answers = {}
    for e in ev["legal_check"]:
        c = LegalCheck.model_validate(e["facts"])
        answers[c.check] = c
    for check, basis, risky in (("military_disclosure_provision", "statute 83.808(2)", "no"), ("military_service", "policy", "yes"),
                                ("bankruptcy", "policy", "yes"), ("vehicle_or_watercraft", "statute 83.806(10)", "yes"),
                                ("dispute", "policy", "yes"), ("secured_party", "statute 83.805", "yes")):
        c = answers.get(check)
        label = check.replace("_", " ")
        if c is None:
            out.append(Finding(label, basis, "missing", f"no {label} check is recorded", "record yes, no or unknown with its source"))
        elif c.answer == "unknown" or c.answer == risky:
            out.append(Finding(label, basis, "review", f"{label}: {c.answer} ({c.source}, {c.observed_on})", "send to counsel"))
        else:
            out.append(Finding(label, basis, "met", f"{label}: {c.answer} ({c.source}, {c.observed_on})"))

    # the notice itself
    notices = [Notice.model_validate(e["facts"]) for e in ev["notice"]]
    if not notices:
        out.append(Finding("written notice", "statute 83.806(2)", "missing", "no notice is recorded", "record the notice as sent"))
        return _finish(out, timing)
    notice = notices[-1]
    version = notice.notice_version
    if not notice.sale_statement:
        out.append(Finding("sale statement", "statute 83.806(2)(d)", "missing", "the notice lacks the conspicuous advertisement and sale statement",
                           "correct the notice; send to review"))

    # delivery
    deliveries: list[tuple[date, str]] = []
    for e in ev["mail_deposit"]:
        m = MailDeposit.model_validate(e["facts"])
        if m.notice_version != version:
            continue
        if m.certificate_of_mailing and m.postage_prepaid:
            deliveries.append((m.deposited_on, f"first-class mail deposited {m.deposited_on} with a certificate of mailing (presumed delivered)"))
        else:
            out.append(Finding("mail delivery", "statute 83.806(1), (3)", "missing",
                               "the mailing lacks a certificate of mailing or prepaid postage", "record the certificate of mailing"))
    for e in ev["in_person_delivery"]:
        p = InPersonDelivery.model_validate(e["facts"])
        if p.notice_version == version:
            deliveries.append((p.delivered_on, f"delivered in person on {p.delivered_on} by {p.delivered_by}"))
    unconfirmed_email = False
    for e in ev["email_delivery"]:
        m = EmailDelivery.model_validate(e["facts"])
        if m.notice_version != version:
            continue
        same_address = m.confirmation_from is not None and m.confirmation_from.strip().lower() == m.sent_to.strip().lower()
        if m.confirmation and same_address and (m.delivered_on or m.confirmed_on):
            when = m.delivered_on or m.confirmed_on
            basis = "statute 83.806(1)" if m.delivered_on else "policy"
            assert when is not None
            deliveries.append((when, f"e-mail to {m.sent_to}, {m.confirmation} from the same address, delivery dated {when}"
                                     + ("" if m.delivered_on else " by the confirmation (policy)")))
            out.append(Finding("e-mail confirmation", basis, "met", f"{m.confirmation} from {m.confirmation_from}"))
        else:
            unconfirmed_email = True
    if unconfirmed_email and not any("mail deposited" in d for _, d in deliveries):
        out.append(Finding("e-mail fallback mailing", "statute 83.806(1)", "missing",
                           "the e-mail has no response, return receipt or delivery confirmation from the same address",
                           "send the notice by first-class mail with a certificate of mailing and record it"))
    if not deliveries:
        out.append(Finding("notice delivery", "statute 83.806(1)", "missing", "no qualifying delivery is recorded",
                           "record the certificate of mailing, the in-person delivery, or the e-mail confirmation"))
        return _finish(out, timing)
    delivered_on, how = min(deliveries)
    if unconfirmed_email:
        mail = [d for d in deliveries if "mail deposited" in d[1]]
        if mail:
            delivered_on, how = mail[0]   # policy: count from the fallback mailing
    out.append(Finding("notice delivery", "statute 83.806(1)", "met", how))
    timing["notice delivered"] = delivered_on.isoformat()

    # posting at the facility or on the unit
    postings = [Posting.model_validate(e["facts"]) for e in ev["posting"] if e["facts"].get("notice_version") == version]
    if not postings:
        out.append(Finding("conspicuous posting", "statute 83.806(1)", "missing", "no posting is recorded", "post the notice and record it"))
    elif any(p.posted_on == delivered_on for p in postings):
        out.append(Finding("conspicuous posting", "statute 83.806(1)", "met", f"posted on {delivered_on}"))
    else:
        out.append(Finding("posting date", "policy", "review", f"posted on {postings[-1].posted_on}, notice delivered {delivered_on}", "send to review"))

    # the demand period: at least 14 days after delivery, deadline open through its local day
    earliest_deadline = plus_days(delivered_on, 14)
    timing["earliest permitted payment deadline"] = earliest_deadline.isoformat()
    if notice.demand_deadline < earliest_deadline:
        out.append(Finding("demand period", "statute 83.806(2)(c)", "review",
                           f"the notice gives until {notice.demand_deadline}; delivered {delivered_on}, it must give until {earliest_deadline} or later",
                           "correct the notice; the printed deadline is not extended here"))
        return _finish(out, timing)
    out.append(Finding("demand period", "statute 83.806(2)(c)", "met", f"payment deadline {notice.demand_deadline}"))
    timing["payment deadline"] = notice.demand_deadline.isoformat()

    # anything paid after delivery reopens the notice
    after = sorted(p for p in facts.payments_after if p >= delivered_on.isoformat())
    if after:
        out.append(Finding("account unchanged since notice", "policy", "review",
                           f"the FMS shows a payment on {after[0]}, after the notice was delivered", "re-review the notice against the current account"))

    first_publication_allowed = plus_days(notice.demand_deadline, 1)
    timing["first publication allowed"] = first_publication_allowed.isoformat()
    pubs = sorted((Publication.model_validate(e["facts"]) for e in ev["publication"]), key=lambda p: p.published_on)
    if not pubs:
        if today < first_publication_allowed:
            out.append(Finding("advertisement", "statute 83.806(4)", "waiting", f"publication may start {first_publication_allowed}"))
        else:
            out.append(Finding("advertisement", "statute 83.806(4)", "missing", "no publication is recorded",
                               "publish once a week for two consecutive weeks and record each"))
        return _finish(out, timing)
    first = pubs[0]
    if first.published_on < first_publication_allowed:
        out.append(Finding("first publication", "statute 83.806(4)", "review",
                           f"published {first.published_on}, before the payment deadline {notice.demand_deadline} had passed", "send to review"))
    second_due = plus_days(first.published_on, 7)
    timing["second publication"] = second_due.isoformat()
    if len(pubs) < 2:
        if today < second_due:
            out.append(Finding("second publication", "statute 83.806(4)", "waiting", f"second publication due {second_due}"))
        else:
            out.append(Finding("second publication", "statute 83.806(4)", "missing", f"no second publication is recorded (due {second_due})",
                               "record the second publication"))
    elif pubs[1].published_on != second_due:
        out.append(Finding("publication spacing", "policy", "review",
                           f"publications on {first.published_on} and {pubs[1].published_on}, not seven days apart", "send to review"))
    else:
        out.append(Finding("advertisement", "statute 83.806(4)", "met", f"published {first.published_on} and {pubs[1].published_on} in {first.newspaper}"))
    for p in pubs[:2]:
        lacking = [name for name, ok in (("property description", p.includes_property_description), ("facility address", p.includes_facility_address),
                                         ("tenant name", p.includes_tenant_name), ("time, place and manner of sale", p.includes_sale_time_place_manner)) if not ok]
        if lacking:
            out.append(Finding("advertisement content", "statute 83.806(4)(b)", "missing",
                               f"the {p.published_on} advertisement lacks: {', '.join(lacking)}", "correct and republish; send to review"))

    sale_threshold = plus_days(first.published_on, 15)
    timing["calculated sale threshold under this policy"] = sale_threshold.isoformat()
    stated = {(notice.sale_date, notice.sale_place.strip().lower())} | {(p.sale_date, p.sale_place.strip().lower()) for p in pubs[:2]}
    if len(stated) > 1:
        out.append(Finding("sale terms agree", "statute 83.806(5)", "review", "the notice and the advertisements state different sale dates or places",
                           "send to review"))
    if notice.sale_date < sale_threshold:
        out.append(Finding("sale date", "statute 83.806(4)(b)3", "review",
                           f"the stated sale date {notice.sale_date} is earlier than {sale_threshold}, fifteen days after the first publication",
                           "send to review"))
    return _finish(out, timing)


def _finish(findings: list[Finding], timing: dict[str, str]) -> Evaluation:
    statuses = {f.status for f in findings}
    if any(f.status == "blocked" and f.requirement in ("facility time zone", "current balance") for f in findings):
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
    return Evaluation(FL_R1.label, state, summary, findings, timing)


__all__ = ["FL_R1", "CaseFacts", "DateProblem", "EVIDENCE_KINDS", "Evaluation", "Finding", "STATES", "ValidationError",
           "evaluate", "facility_zone", "local_date", "local_instant", "parse_evidence", "plus_days"]
