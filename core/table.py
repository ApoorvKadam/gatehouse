"""The fixed-rule workflow: what a capable front desk system does without a model.

It reads a message with keyword and phrase rules and the same date and money readers, points each claim at the charge the
words most likely mean, checks it with the same evidence code and the same owner rule, and decides with a fixed table. The
table is also the second arm of the comparison: the model reads, the table decides. Written after the labels; its phrase
list was written with the first graded set in view, so the comparison is scored on a second set (graded/outside.json) written
by a separate model afterwards. One change since that set was written: a month name now means the most recent such month."""
from __future__ import annotations

import re
from datetime import date, datetime

from .books import ADMIN_FEE, LATE_FEE, RENT, Books, local
from .evidence import Claim, Finding
from .gate import Command, evaluate
from .reader import amounts, dates

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                                      "october", "november", "december"], 1)}

RULES = [  # (claim kind, phrases) in the order they are tried; the first match wins
    ("payment_issue", r"\bcard\b.*\b(twice|2x|two times|double)|\b(paid|charged) (me )?twice by accident|\btwo charges of\b|\bpaid twice\b"),
    ("challenges_record", r"not my signature|(told|said) (me )?(that )?(there'?s|there is|it would|the .* would|no)|was told|promised"),
    ("charged_twice", r"\btwice\b|\bdouble\b|\btwo (late )?(fees|rent|charges)|\b2x\b|\bsecond \$?\d*\s*(late )?fee|over and over|two times"),
    ("never_agreed", r"never (agreed|signed|mentioned)|did ?n[o']?t (agree|sign)|don'?t remember agreeing|nobody (ever )?(told|mentioned)|disput"),
    ("wants_waiver", r"\bwaive|\bremove\b|take (it|this|the .*) off|take off|taken off|\bdrop\b|\brefund\b|do about the|anything you can do"),
    ("paid_on_time", r"\bi paid\b|\bpaid (on|my|rent|it)|\bpagu[eé]|payment (went|cleared|left)|\btried (to )?pay|\bwent through\b"),
    ("what_is_this", r"\bwhat'?s\b|\bwhat is\b|\bwhy\b|\bowe\b|is that right"),
]
FLAGS = {
    "legal_or_hostile": r"lawyer|attorney|\bsue\b|court|ridiculous",
    "asks_for_person": r"speak to|talk to (someone|a person|a manager)|\bmanager about\b|call me",
    "hardship": r"lost my job|laid off|hospital|sick|passed away|funeral",
    "instructions_to_system": r"ignore (all |your )?(previous )?instructions|\bsystem\b ?(note|:)|you are now",
}


def _months_in(text: str) -> list[int]:
    low = text.lower()
    return [n for name, n in MONTHS.items() if name != "may" and re.search(rf"\b{name[:3]}\w*\b", low)]


def _pick(books: Books, tenant: str, text: str, now: datetime) -> list[str]:
    """The charge(s) the words most likely mean: the kind named, the months named, one charge unless the words are plural."""
    low = text.lower()
    kind = ADMIN_FEE if "admin" in low else (LATE_FEE if "late fee" in low or "fee" in low else (RENT if "rent" in low else None))
    if kind is None:
        kind = LATE_FEE if "$20" in low else None
    if kind is None:
        return []
    mine = sorted((c for c in books.tenant_charges(tenant) if c.kind == kind and not c.closed), key=lambda c: (c.due, c.seq))
    plural = bool(re.search(r"\b(late )?fees\b|\bcharges\b", low))
    months = _months_in(text)
    if months:
        picked = []
        for m in months:
            named = [c for c in mine if c.due.month == m]
            if named:       # a month without a year is the most recent one: "November" is this November, not last year's
                latest = max(c.due.year for c in named)
                named = [c for c in named if c.due.year == latest]
            picked += [c.id for c in (named if plural and len(months) == 1 else named[:1])]
        if picked:
            return picked
    open_ = [c for c in mine if c.open_cents > 0] or mine
    return [c.id for c in open_] if plural and kind == LATE_FEE else [c.id for c in open_[-1:]]


def keyword_read(books: Books, tenant: str | None, text: str, now: datetime) -> tuple[list[Claim], list[str]]:
    low = text.lower()
    flags = [f for f, rx in FLAGS.items() if re.search(rx, low)]
    kind = next((k for k, rx in RULES if re.search(rx, low)), "other")
    if tenant is None:
        return [Claim(kind, None, text)], flags
    charges = [] if kind in ("payment_issue", "other") else _pick(books, tenant, text, now)
    ds = dates(text, local(now).date())
    ms = amounts(text)
    when = ds[0] if ds else None
    cents = ms[0] if ms else None
    if not charges:
        return [Claim(kind, None, text, when[0].isoformat() if when else None, when[1] if when else None,
                      cents[0] if cents else None, cents[1] if cents else None)], flags
    return [Claim(kind, c, text, when[0].isoformat() if when else None, when[1] if when else None, cents[0] if cents else None,
                  cents[1] if cents else None) for c in charges], flags


def _tried_before_cutoff(books: Books, tenant: str, charge) -> bool:
    month = charge.obligation.split(":")[2] if charge.obligation and charge.obligation.count(":") == 2 else None
    if not month:
        return False
    y, m = map(int, month.split("-"))
    return any(x["tenant"] == tenant and local(x["at"]).date() < date(y, m, 6) and local(x["at"]).date() >= date(y, m, 1) for x in books.declined)


def decide(books: Books, tenant: str, finding: Finding, flags: list[str], now: datetime, spent: int = 0) -> str:
    """The fixed table: one action for one claim. `spent` counts waivers already decided earlier in the same message."""
    if "legal_or_hostile" in flags or "asks_for_person" in flags:
        return "hand_to_staff"
    choices = {o.action: o for o in finding.choices}
    if not choices:
        return "hand_to_staff"
    for money in ("correct_fee_posted_in_error", "reverse_duplicate"):
        if money in choices and finding.verdict == "supports":
            return money
    if finding.verdict in ("no_record", "unresolved") and "open_dispute" in choices:
        return "open_dispute"
    c = books.charges.get(finding.claim.charge or "")
    if c is not None and books.leases[c.lease].ended:
        return "hand_to_staff"
    if "waive_fee" in choices and c is not None:
        rule_ok = evaluate(books, Command("table", "waive_fee", tenant, "fixed_workflow", c.id, raised=True), now).allowed and spent == 0
        if "hardship" in flags:
            return "waive_fee" if rule_ok else "hand_to_staff"
        asked = finding.claim.kind == "wants_waiver"
        if rule_ok and (asked or _tried_before_cutoff(books, tenant, c)):
            return "waive_fee"
    if "hardship" in flags:
        return "hand_to_staff"
    return "explain_charge" if "explain_charge" in choices else "hand_to_staff"
