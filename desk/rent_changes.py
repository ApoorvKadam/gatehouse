"""The rent change check: the work around each rent change the operator already scheduled in the FMS.

Gatehouse sets no price and schedules, changes or cancels nothing. The operator or its revenue tool decides an increase and the FMS holds it.
What nobody checks afterwards is whether the account can take it (a lien file, a balance, a payment arrangement), whether the FMS applied it
on the day, and whether the first full bill after it is the new rent. The worker reads all of it once a pass and keeps one row per lease;
the page, Today and the API read those rows and never ask the FMS.

Each step closes only on its own fact. A scheduled change is what Leases Search shows. A notice is never proven: no read the FMS documents
says a rent notice was sent. Applied is the FMS rent after the date. Charged is the first full billing period after the change, compared in
the lease's own cycle (Leases Search amounts are per facility cycle; the rent roll's are already in the lease's), and never the amount paid.
When the expected bill cannot be told (a discount, a cycle the desk cannot read, no full period billed yet) the row says so instead of
calling anything wrong. The 20 percent and 180 day lines are review lines an operator can change, not law."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .access import ensure_lease, facility_today
from .adapter import FMSAdapter, FMSError, RentFacts
from .adapter.base import ProviderExemption
from .cases import current_case
from .db import utcnow
from .liens import dollars
from .models import Facility, Lease, LienEvaluation, RentChangeCheck, WorkItem
from .sales import SALE_KINDS

log = logging.getLogger("desk.rent_changes")

# billing periods in a year, to put an amount in the lease's own cycle (Cubby: 27692 at a DAYS_28 facility is 30000 on a MONTHLY lease)
PER_YEAR = {"WEEKLY": 52, "BI_WEEKLY": 26, "DAYS_28": 13, "MONTHLY": 12, "QUARTERLY": 4, "BI_ANNUAL": 2, "ANNUAL": 1}
REVIEW_PERCENT = 20         # an increase above this is shown for review; operators set their own line
REVIEW_DAYS = 180           # so is a second increase within this many days of the last
SHOWN_FOR = 120             # a completed change stays on the page this many days after its date
NOTICE = "Not proven."
# said once on the page, under the table, rather than on every row
NOTICE_WHY = "No record the FMS keeps says a rent notice was sent, so every notice shows as not proven."

# the verdicts, in the order the page lists them, and what the page calls each
ORDER = ("hold", "not_applied", "wrong_charge", "review", "cannot_tell", "clear", "verified")
LABELS = {"hold": "A person decides", "not_applied": "Not applied", "wrong_charge": "Billed at another amount", "review": "Review",
          "cannot_tell": "Cannot tell yet", "clear": "Nothing in the way", "verified": "Applied and billed"}
# the rows that are unfinished work, as Today shows them: (rank, heading, what proves it done, who acts)
TODAY = {"hold": (2, "Rent increase on an account a person must weigh", "A person decides the increase stands, or it is cancelled in the FMS.",
                  "Collections lead"),
         "not_applied": (2, "Rent change not applied", "Leases Search shows the new rent, or no change scheduled for a date that has passed.",
                         "Office manager"),
         "wrong_charge": (2, "Rent billed at another amount", "A later full period billed at the rent the FMS reports, or a recorded reason.",
                          "Office manager")}


def in_lease_cycle(amount: int, facility_cycle: str | None, lease_cycle: str | None) -> int | None:
    """An amount per facility cycle as the lease's own cycle bills it, rounded half up; None when either cycle is not one the desk knows."""
    if facility_cycle not in PER_YEAR or lease_cycle not in PER_YEAR:
        return None
    num, den = amount * PER_YEAR[facility_cycle], PER_YEAR[lease_cycle]
    return (2 * num + den) // (2 * den)


def parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except ValueError:
        return None


def day(d: date | None) -> str:
    return f"{d:%b} {d.day}, {d.year}" if d else "an unreadable date"


@dataclass
class Account:
    """What else the desk knows about the account, read once, each a sentence or None."""

    owed: int = 0
    lien: str | None = None
    arrangement: str | None = None


def _lien_or_sale(session: Session, lease: Lease) -> str | None:
    case = current_case(session, lease)
    if case is not None and session.scalar(select(LienEvaluation.id).where(LienEvaluation.case_id == case.id).limit(1)) is not None:
        return "A lien file has begun for this account, so a new charge must be weighed against the lien notice."
    sale = session.scalar(select(WorkItem.id).where(WorkItem.facility_id == lease.facility_id, WorkItem.kind.in_(SALE_KINDS),
                                                    WorkItem.state == "open", WorkItem.subject_id.like(f"%/{lease.provider_id}")).limit(1))
    return "The unit is in a lien sale, so a new charge must be weighed against the sale." if sale is not None else None


def _arrangement(exemptions: list[ProviderExemption], lease_provider_id: str, today: date) -> str | None:
    for e in exemptions:
        end = parse_day(e.end_date)
        if e.lease_provider_id == lease_provider_id and (end is None or end >= today):
            what = "A payment arrangement" if e.reason == "PAYMENT_ARRANGEMENT" else "A delinquency exemption"
            return f"{what} is on file for this unit" + (f" until {day(end)}." if end else ".")
    return None


def upcoming(f: RentFacts, when: date, acct: Account, today: date) -> dict:
    """A change scheduled for a later date: can the account take it."""
    holds, notes = [], []
    if acct.lien:
        holds.append(acct.lien)
    if f.facility_cycle not in PER_YEAR or f.lease_cycle not in PER_YEAR:
        holds.append("The FMS sent a billing cycle Gatehouse cannot read, so the new rent cannot be checked against the bill.")
    if f.rent is None or f.scheduled_amount is None:
        holds.append("The FMS sent the rent or the scheduled amount in a shape Gatehouse does not read.")
    if acct.arrangement:
        notes.append(acct.arrangement)          # first: it says why a balance is there, and what an increase now does to the plan
    if acct.owed > 0:
        notes.append(f"The account is past due by {dollars(acct.owed)}.")
    pct = round((f.scheduled_amount - f.rent) * 100 / f.rent) if f.rent and f.scheduled_amount is not None else None
    if pct is not None and pct > REVIEW_PERCENT:
        notes.append(f"An increase of {pct} percent, above the {REVIEW_PERCENT} percent review line.")
    last = parse_day(f.last_change)
    if last is not None and (today - last).days < REVIEW_DAYS:
        notes.append(f"The last rent change was {(today - last).days} days ago, inside the {REVIEW_DAYS} day review line.")
    state = "hold" if holds else "review" if notes else "clear"
    why = holds[0] if holds else notes[0] if notes else "Nothing in the records stands in the way of this increase."
    rest = holds[1:] + notes if holds else notes[1:]
    steps = [("Scheduled", "done", f"The FMS shows {dollars(f.scheduled_amount)} from {day(when)}." if f.scheduled_amount is not None else
              f"The FMS shows a change on {day(when)}."),
             ("Notice", "unproven", NOTICE),
             ("Applied", "waiting", f"On {day(when)}."),
             ("Billed", "waiting", "On the first full period after it.")]
    return {"state": state, "why": why, "also": rest, "pct": pct, "steps": steps}


def first_full_period(entries: list[dict], since: date) -> dict | None:
    """The first RENT charge whose period starts on or after the change date: a period that straddles the date may mix both rents."""
    rent = []
    for e in entries:
        d = e.get("details") or {}
        start = parse_day(d.get("accruesOn"))
        if e.get("type") == "CHARGE" and d.get("category") == "RENT" and start is not None and start >= since:
            rent.append((start, e))
    return min(rent, key=lambda pair: pair[0])[1] if rent else None


def completed(f: RentFacts, since: date, entries: list[dict] | None, today: date) -> dict:
    """A change the FMS reports completed: is the first full bill after it the rent the FMS reports."""
    steps = [("Scheduled", "done", f"Completed on {day(since)}, per the FMS."),
             ("Notice", "unproven", NOTICE)]
    expected = f.roll_rent if f.roll_rent is not None else (in_lease_cycle(f.rent, f.facility_cycle, f.lease_cycle) if f.rent is not None else None)
    steps.append(("Applied", "done" if f.rent is not None else "unknown",
                  f"The FMS rent is now {dollars(f.rent)}" + (f", {dollars(expected)} per billing period of this lease." if expected not in (None, f.rent) else ".")
                  if f.rent is not None else "The FMS rent could not be read."))

    def tell(state: str, why: str, billed: str) -> dict:
        return {"state": state, "why": why, "also": [], "pct": None, "steps": steps + [("Billed", {"verified": "done", "wrong_charge": "wrong"}.get(state, "waiting"), billed)]}

    if expected is None or f.unreadable:
        return tell("cannot_tell", "The rent or the billing cycle could not be read, so the expected bill cannot be told.", "Cannot tell.")
    if f.roll_effective is not None and f.roll_effective != expected:
        return tell("cannot_tell", f"A discount applies (net rent {dollars(f.roll_effective)}), so the expected bill cannot be told from the rent alone.",
                    "Cannot tell from the rent alone; a person compares the bill with the discount.")
    if entries is None:
        return tell("cannot_tell", "The ledger could not be read on this pass.", "Cannot tell yet.")
    charge = first_full_period(entries, since)
    if charge is None:
        return tell("cannot_tell", "No full billing period after the change has been billed yet.", "Waiting for the first full period.")
    d = charge.get("details") or {}
    period = f"{day(parse_day(d.get('accruesOn')))} to {day(parse_day(d.get('accruesThrough')))}" if parse_day(d.get("accruesThrough")) else \
        f"from {day(parse_day(d.get('accruesOn')))}"
    amount = charge.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool):
        return tell("cannot_tell", "The ledger sent a charge amount Gatehouse does not read.", "Cannot tell.")
    if amount == expected:
        return tell("verified", f"The first full period after the change, {period}, was billed {dollars(amount)}, the rent the FMS reports.",
                    f"{period}: {dollars(amount)}, as expected.")
    return tell("wrong_charge", f"The first full period after the change, {period}, was billed {dollars(amount)}, but the rent the FMS reports is "
                f"{dollars(expected)} for that period.", f"{period}: {dollars(amount)}, expected {dollars(expected)}.")


def overdue_schedule(f: RentFacts, when: date, entries: list[dict] | None) -> dict:
    """A change still scheduled for a date that has passed."""
    if f.rent is not None and f.rent == f.scheduled_amount:
        why = f"The rent is already {dollars(f.rent)}, but the FMS still shows the change as scheduled for {day(when)}."
    else:
        why = (f"The FMS still shows this change as scheduled for {day(when)}, which has passed, and the rent is still "
               f"{dollars(f.rent) if f.rent is not None else 'unreadable'}.")
    charge = first_full_period(entries or [], when)
    also = []
    if charge is not None and isinstance(charge.get("amount"), int) and f.scheduled_amount is not None:
        also.append(f"The period from {day(parse_day((charge.get('details') or {}).get('accruesOn')))} was billed {dollars(charge['amount'])}.")
    steps = [("Scheduled", "done", f"The FMS shows {dollars(f.scheduled_amount)} from {day(when)}." if f.scheduled_amount is not None else
              f"The FMS shows a change on {day(when)}."),
             ("Notice", "unproven", NOTICE),
             ("Applied", "wrong", "Not applied on the date."),
             ("Billed", "waiting", "Once the change is applied.")]
    return {"state": "not_applied", "why": why, "also": also, "pct": None, "steps": steps}


@dataclass
class RentPass:
    read: int = 0
    failures: list[str] = field(default_factory=list)


def reconcile_rent_changes(session: Session, adapter: FMSAdapter, facility: Facility) -> RentPass:
    """One reading of every lease with a scheduled change, or a change completed in the last SHOWN_FOR days, and a row each. A failed reading keeps
    the rows of the last good one and is named in `failures`; nothing is judged on half a picture."""
    result = RentPass()
    today = facility_today(facility)
    try:
        facts = adapter.rent_facts(facility.provider_id, today.isoformat())
        leases = {pl.provider_id: pl for pl in adapter.leases(facility.provider_id)}
    except FMSError as exc:
        result.failures.append(f"rent facts: {exc}")
        return result
    kept: set[int] = set()
    exemptions: dict[str, list[ProviderExemption]] = {}
    for f in facts:
        when, last = parse_day(f.scheduled_date), parse_day(f.last_change)
        if when is None and (last is None or (today - last).days > SHOWN_FOR):
            continue
        pl = leases.get(f.lease_provider_id)
        if pl is None:
            continue
        lease = ensure_lease(session, facility, pl, adapter.provider)
        try:
            customer = adapter.customer(f.customer_provider_id) if f.customer_provider_id else None
            if when is not None and when > today:
                if f.customer_provider_id not in exemptions:
                    exemptions[f.customer_provider_id] = adapter.exemptions_for_customer(f.customer_provider_id)
                acct = Account(owed=pl.total_due, lien=_lien_or_sale(session, lease),
                               arrangement=_arrangement(exemptions[f.customer_provider_id], f.lease_provider_id, today))
                row = upcoming(f, when, acct, today)
            else:
                entries = (adapter.ledger(f.lease_provider_id).raw.get("bookEntries") or [])
                row = overdue_schedule(f, when, entries) if when is not None else completed(f, last, entries, today)
        except FMSError as exc:
            result.failures.append(f"{f.lease_provider_id}: {exc}")
            kept.add(lease.id)          # the last good row stands until a reading succeeds
            continue
        check = session.get(RentChangeCheck, lease.id) or RentChangeCheck(lease_id=lease.id)
        old = check.data or {}
        since = old.get("since") if old.get("state") == row["state"] and old.get("since") else utcnow().isoformat()
        check.observed_at = utcnow()
        check.data = {**row, "label": LABELS[row["state"]], "unit": f.unit_name, "rent": f.rent, "new": f.scheduled_amount,
                      "date": (when or last).isoformat(), "since": since, "tenant": customer.first_name if customer else None}
        session.merge(check)
        kept.add(lease.id)
        result.read += 1
    for stale in session.scalars(select(RentChangeCheck).join(Lease, Lease.id == RentChangeCheck.lease_id).where(Lease.facility_id == facility.id)).all():
        if stale.lease_id not in kept:
            session.delete(stale)
    session.commit()
    return result
