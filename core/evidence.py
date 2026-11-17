"""What the records say about one claim a tenant makes, in code.

The reading (a model, or the fixed workflow's keyword rules) turns a message into claims: what the tenant says happened, about
which charge, in their words. This module checks each claim against the folded books and returns a finding: a verdict, what
the records show in plain words, the records it rests on, and the actions the case may take for that claim. It never decides
which action; it lists the ones whose safety checks pass (the owner's rule is left to the gate, so a proposal the rule forbids
is seen and counted)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .books import ADMIN_FEE, CUTOFF_DAY, FEE_KINDS, LATE_FEE, RENT, Books, Charge, cutoff, local

CLAIM_KINDS = ("paid_on_time", "charged_twice", "never_agreed", "challenges_record", "wants_waiver", "wrong_amount", "what_is_this",
               "payment_issue", "other")
VERDICTS = ("supports", "contradicts", "no_record", "unresolved", "payment", "unclear")
KIND_WORDS = {RENT: "rent", LATE_FEE: "late fee", ADMIN_FEE: "admin fee"}


@dataclass
class Claim:
    kind: str
    charge: str | None
    quote: str                          # the tenant's own words for this claim
    date: str | None = None             # a date the tenant gives (ISO), read from date_quote
    date_quote: str | None = None
    cents: int | None = None            # an amount the tenant gives, read from amount_quote
    amount_quote: str | None = None


@dataclass
class Option:
    action: str
    charge: str | None
    label: str                          # what taking it means, in words written by code


@dataclass
class Finding:
    claim: Claim
    verdict: str
    says: str                           # what the records show, in plain words
    records: list[str] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)

    @property
    def choices(self) -> list[Option]:
        """The options other than handing to staff: two or more means a real choice."""
        return [o for o in self.options if o.action != "hand_to_staff"]


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def day(d: date | datetime) -> str:
    d = local(d).date() if isinstance(d, datetime) else d
    return f"{d:%b} {d.day}"


def clock(t: datetime) -> str:
    t = local(t)
    return f"{day(t)} at {t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def month_words(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{date(y, m, 1):%B}"


def describe(c: Charge) -> str:
    state = "open" if c.open_cents == c.cents else ("paid" if c.open_cents == 0 and not c.closed else (c.closed or f"{money(c.open_cents)} open"))
    return f"{KIND_WORDS.get(c.kind, c.kind)}, due {day(c.due)}, {money(c.cents)}, {state}"


def charge_list(books: Books, tenant: str, now: datetime, days: int = 365) -> list[dict]:
    """The tenant's charges of the last year, newest first, as the reading sees them: an id and words."""
    since = local(now).date() - timedelta(days=days)
    out = [c for c in books.tenant_charges(tenant) if c.due >= since]
    return [{"charge": c.id, "about": describe(c), "unit": books.leases[c.lease].unit} for c in sorted(out, key=lambda c: (c.due, c.seq), reverse=True)]


def _month(c: Charge) -> str | None:
    return c.obligation.split(":")[2] if c.obligation and c.obligation.count(":") == 2 else None


def _hand(reason: str) -> Option:
    return Option("hand_to_staff", None, reason)


def paid_twice(books: Books, tenant: str, now: datetime, claim: Claim, charge: str | None = None) -> list:
    """Two payments of the same amount on the same day in the last two months, the later left as credit: how a card charged
    twice shows in the books. With a charge, the earlier payment paid it; without one, the day and amount the tenant names must match."""
    since = local(now).date() - timedelta(days=62)
    pays = sorted((p for p in books.payments.values() if p.tenant == tenant and not p.returned and local(p.effective_at).date() >= since),
                  key=lambda p: p.effective_at)
    for i, a in enumerate(pays):
        on = local(a.effective_at).date()
        if charge is None and (claim.cents not in (None, a.cents) or claim.date not in (None, on.isoformat())):
            continue
        if charge is not None and not any(x["charge"] == charge for x in a.applications):
            continue
        for b in pays[i + 1:]:
            if b.cents == a.cents and b.credit_cents == b.cents and local(b.effective_at).date() == on:
                return [a, b]
    return []


def _paid_twice_finding(f, pair: list) -> Finding:
    a, b = pair
    at = lambda p: clock(p.effective_at).split(" at ")[1]
    return f("payment", f"Two payments of {money(a.cents)} went through on {day(a.effective_at)}, at {at(a)} and {at(b)}. The second was left "
             f"as credit on the account. Money goes back to a card only through staff.", [a.id, b.id], [_hand("money back to a card is for staff")])


def _rent_story(books: Books, c: Charge, month: str) -> tuple[str, list[str]]:
    """How the month's rent was paid, from the records: declined tries, payments and their times, returns."""
    lease = books.leases[c.lease]
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    window_end = first + timedelta(days=20)
    parts, recs = [], []
    for x in books.declined:
        if x["tenant"] == lease.tenant and first <= local(x["at"]).date() <= window_end:
            parts.append(f"a card payment was declined on {clock(x['at'])}")
    for p in sorted(books.payments.values(), key=lambda p: p.effective_at):
        if p.tenant != lease.tenant or not any(books.charges[a["charge"]].obligation == f"rent:{lease.id}:{month}" for a in p.applications):
            continue
        recs.append(p.id)
        when = clock(p.effective_at)
        if p.known_at.date() != p.effective_at.date() and p.known_at > p.effective_at:
            when += f" (we learned of it on {day(p.known_at)})"
        parts.append(f"a payment of {money(p.cents)} went through on {when}" + (" and was later returned" if p.returned else ""))
    return "; ".join(parts) or "no payment towards it was recorded", recs


def check(books: Books, tenant: str, claim: Claim, now: datetime) -> Finding:
    f = lambda verdict, says, records=(), options=(): Finding(claim, verdict, says, list(records), list(options))
    hand = _hand("give the case to staff")
    # a card charged twice, named by its day or amount: as a payment question, or as a charge twice with no charge picked
    if (claim.kind == "payment_issue" or (claim.kind == "charged_twice" and claim.charge is None)) and (claim.cents or claim.date):
        if pair := paid_twice(books, tenant, now, claim):
            return _paid_twice_finding(f, pair)
    if claim.kind == "payment_issue":
        return f("payment", "Questions about card charges need the payment processor's records, which staff check.", options=[_hand("a payment question for staff")])
    if claim.kind == "other" or claim.kind not in CLAIM_KINDS:
        return f("unclear", "The message does not ask about a charge the records can answer.", options=[hand])
    c = books.charges.get(claim.charge or "")
    if c is None or c.tenant != tenant:
        return f("unclear", "The charge the tenant means could not be matched to their account.", options=[hand])
    lease = books.leases[c.lease]
    what = KIND_WORDS.get(c.kind, c.kind)
    explain = Option("explain_charge", c.id, f"explain the {what} from the records and leave it as it is")
    if lease.ended:
        return f("contradicts", f"The {what} is on the lease for unit {lease.unit}, which ended on {day(lease.ended)}.", [c.id],
                 [explain, _hand("a charge on an ended lease is for staff")])
    month = _month(c)

    if claim.kind == "paid_on_time":
        if c.kind == LATE_FEE and month:
            due = sum(x.cents for x in books.live(f"rent:{c.lease}:{month}"))
            story, recs = _rent_story(books, c, month)
            if due and books.rent_paid_before(c.lease, month, cutoff(month)) >= due:
                return f("supports", f"{month_words(month)} rent was fully paid before the late fee time (midnight at the start of "
                         f"{month_words(month)} {CUTOFF_DAY}): {story}. The late fee should not have been posted.", [c.id, *recs],
                         [Option("correct_fee_posted_in_error", c.id, f"remove the {money(c.cents)} late fee as posted in error"), hand])
            return f("contradicts", f"The late fee posts at midnight at the start of {month_words(month)} {CUTOFF_DAY} when rent is still "
                     f"unpaid. For {month_words(month)}: {story}.", [c.id, *recs],
                     [explain, Option("waive_fee", c.id, f"waive the {money(c.cents)} late fee"), hand])
        if c.open_cents == 0:
            return f("supports", f"The {what} due {day(c.due)} is paid.", [c.id], [explain, hand])
        return f("contradicts", f"The {what} due {day(c.due)} still has {money(c.open_cents)} unpaid.", [c.id], [explain, hand])

    if claim.kind == "charged_twice":
        twins = books.billed(c.obligation) if c.obligation else []
        if len(twins) >= 2 and not twins[-1].closed:
            later = twins[-1]
            return f("supports", f"The {what} was posted twice for the same thing: on {day(twins[0].posted_at)} by "
                     f"{'billing' if twins[0].source == 'billing' else 'staff'} and again on {day(later.posted_at)} by "
                     f"{'staff, by hand' if later.source.startswith('staff') else 'billing'}.", [x.id for x in twins],
                     [Option("reverse_duplicate", later.id, f"reverse the second {money(later.cents)} {what}"), hand])
        if pair := paid_twice(books, tenant, now, claim, c.id):
            return _paid_twice_finding(f, pair)
        others = [x for x in books.tenant_charges(tenant) if x.id != c.id and not x.closed]
        # what looks like the same charge twice: first the same day, then the same amount, then the same kind, close by
        alike = ([x for x in others if x.due == c.due and x.kind == c.kind]
                 or [x for x in others if x.cents == c.cents and x.kind == c.kind and abs((x.due - c.due).days) <= 62]
                 or [x for x in others if x.kind == c.kind and abs((x.due - c.due).days) <= 31])
        if alike:
            why = []
            for x in alike:
                if x.lease != c.lease:
                    why.append(f"one is for unit {books.leases[x.lease].unit} and one for unit {lease.unit}")
                elif x.kind == c.kind == RENT and x.due.month != c.due.month:
                    why.append(f"one is {month_words(_month(x) or '')} rent and one is {month_words(month or '')} rent")
                elif x.kind == c.kind == LATE_FEE and _month(x) != month:
                    why.append(f"one is the late fee for {month_words(_month(x) or '')} and one for {month_words(month or '')}")
                else:
                    why.append(f"they bill different things ({describe(x)}; {describe(c)})")
            return f("contradicts", "The two charges are not the same charge twice: " + "; ".join(dict.fromkeys(why)) + ".",
                     [c.id, *[x.id for x in alike]], [explain, hand])
        return f("contradicts", f"There is one {what} due {day(c.due)}; no second charge for it was found.", [c.id], [explain, hand])

    if claim.kind in ("never_agreed", "challenges_record"):
        signed = c.kind == RENT or c.kind in lease.schedule
        dispute = Option("open_dispute", c.id, f"record a dispute on the {money(c.cents)} {what} for staff to review")
        if c.kind in FEE_KINDS and not signed:
            return f("no_record", f"The {what} is not in the fee schedule signed on {day(lease.start)} for unit {lease.unit}.", [c.id],
                     [dispute, hand])
        if claim.kind == "challenges_record":
            return f("unresolved", f"The records show the {what} was agreed in the lease signed on {day(lease.start)}, but the tenant "
                     "challenges that record in a way the records cannot settle.", [c.id], [dispute, hand])
        return f("contradicts", f"The {what} is in the lease signed on {day(lease.start)} for unit {lease.unit}.", [c.id],
                 [explain, dispute, hand])

    if claim.kind == "wants_waiver":
        if c.kind == LATE_FEE:
            return f("contradicts", f"The {money(c.cents)} late fee due {day(c.due)} stands on the records.", [c.id],
                     [explain, Option("waive_fee", c.id, f"waive the {money(c.cents)} late fee"), hand])
        return f("contradicts", f"The owner's rule waives late fees only; this is the {what}.", [c.id], [explain, hand])

    # wrong_amount, what_is_this
    note = f"The {what} due {day(c.due)} is {money(c.cents)}"
    if c.kind == LATE_FEE and month:
        note += f", the late fee for {month_words(month)} rent unpaid at midnight at the start of {month_words(month)} {CUTOFF_DAY}"
    elif c.kind == RENT and month and c.cents != lease.rent_cents:
        note += f", the first month counted from the move-in day ({day(lease.start)})"
    return f("contradicts", note + ".", [c.id], [explain, hand])
