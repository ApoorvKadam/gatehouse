"""Rent changes, in the evaluation. One pass of the rent change check runs against the simulator's seeded world, with one charge added: a rent
charge for a period that starts before G-118's change and runs past it, as a prorated bill would. The judge reads the rows the check kept.
Four wrong builds run the same pass with one part of the check replaced. It runs inside desk/evaluate.py, on the same simulator."""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import httpx
from sqlalchemy import select

from . import rent_changes
from .access import facility_today
from .adapter.cubby import CubbyAdapter
from .cases import sync_facility
from .db import make_session_factory
from .evaluate_texts import broken
from .models import Facility, Lease, RentChangeCheck

KEY = "sim_org_northstar_full"
ORLANDO = "fac_NSorlando01"
NOTICE_SENT = "a rent notice called sent"
CYCLE_WRONG = "a right bill in another billing cycle called wrong"
STRADDLE = "a bill judged on a straddling period"
UNPROVEN = "a rent change called applied or billed without the fact that shows it"
LINES = (NOTICE_SENT, CYCLE_WRONG, STRADDLE, UNPROVEN)


def one_pass(url: str) -> dict[str, dict]:
    """A fresh world, the straddling charge, one pass of the check: the rows by unit."""
    httpx.post(f"{url}/sim/reset", headers={"Authorization": f"Bearer {KEY}"}, timeout=15)
    session = make_session_factory("sqlite:///:memory:")()
    adapter = CubbyAdapter(url, KEY, timeout_seconds=5.0)
    sync_facility(session, adapter, ORLANDO)
    facility = session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
    today = facility_today(facility)
    park = next(f for f in adapter.rent_facts(ORLANDO, today.isoformat()) if f.unit_name == "G-118")
    changed = date.fromisoformat(park.last_change)
    httpx.post(f"{url}/sim/charges", headers={"Authorization": f"Bearer {KEY}"}, timeout=15,
               json={"leaseId": park.lease_provider_id, "category": "RENT", "amount": 13200, "amountPaid": 13200,
                     "accruesOn": (changed - timedelta(days=10)).isoformat(), "accruesThrough": (changed + timedelta(days=19)).isoformat()})
    rent_changes.reconcile_rent_changes(session, adapter, facility)
    found = session.execute(select(Lease.unit_name, RentChangeCheck).join(RentChangeCheck, RentChangeCheck.lease_id == Lease.id)).all()
    adapter.close()
    return {unit: c.data for unit, c in found}


def step(row: dict, name: str) -> str | None:
    return next((state for n, state, _ in row.get("steps", []) if n == name), None)


def judge(rows: dict[str, dict]) -> dict[str, tuple[int, int]]:
    """(found, checked) for each red line."""
    state = lambda unit: (rows.get(unit) or {}).get("state")
    unproven = sum(1 for r in rows.values() if r.get("state") == "verified" and step(r, "Billed") != "done")
    unproven += (state("M-611") != "not_applied") + (state("H-204") == "verified")
    return {NOTICE_SENT: (sum(1 for r in rows.values() if step(r, "Notice") != "unproven"), len(rows)),
            CYCLE_WRONG: (int(state("L-520") != "verified"), 1),
            STRADDLE: (int(state("G-118") != "verified"), 1),
            UNPROVEN: (unproven, len(rows) + 2)}


# ------------------------------------------------------------------ the wrong builds
def notice_as_sent(original):
    """The wrong build: the planned notice date is taken as the notice sent."""
    def upcoming(f, when, acct, today):
        row = original(f, when, acct, today)
        row["steps"] = [("Notice", "done", f"Sent on {rent_changes.day(when - timedelta(days=30))}.") if n == "Notice" else (n, s, w)
                        for n, s, w in row["steps"]]
        return row
    return upcoming


def monthly_against_28_days(original):
    """The wrong build: the rent per facility cycle is compared with the bill, whatever cycle the lease is billed on."""
    def completed(f, since, entries, today):
        with broken(rent_changes, "in_lease_cycle", lambda amount, facility_cycle, lease_cycle: amount):
            return original(dataclasses.replace(f, roll_rent=None), since, entries, today)
    return completed


def straddling_period(entries, since):
    """The wrong build: the first rent charge whose period reaches the change date is taken as the bill, though it began before it."""
    charges = [e for e in entries if e.get("type") == "CHARGE" and (e.get("details") or {}).get("category") == "RENT"
               and rent_changes.parse_day((e.get("details") or {}).get("accruesThrough")) and
               rent_changes.parse_day(e["details"]["accruesThrough"]) >= since]
    if not charges:
        return None
    first = min(charges, key=lambda e: e["details"]["accruesOn"])
    start = rent_changes.parse_day(first["details"]["accruesOn"])
    return start, rent_changes.parse_day(first["details"]["accruesThrough"]), first["amount"]


def passed_date_as_applied(original):
    """The wrong build: a scheduled date that has passed counts as the change applied."""
    def overdue(f, when, entries, today):
        return rent_changes.completed(f, when, entries, today)
    return overdue


# ------------------------------------------------------------------ the part
def rents_part(url: str):
    """The scenarios, the red lines and the wrong builds of this part, as plain tuples for desk/evaluate.py."""
    rows = one_pass(url)
    state = lambda unit: (rows.get(unit) or {}).get("state")
    nobody = "Nobody: nothing is wrong"
    table = [
        ("A 28-day lease after an increase", "disagree", "the FMS reports $300.00 a month; the lease is billed $276.92 every 28 days",
         "reads the bill in the lease's own cycle: billed at the FMS rent", nobody, state("L-520") == "verified"),
        ("A first bill at the old rent", "money", "the rent went to $155.00, but the first full period was billed $140.00",
         "a Today row with both amounts", "Front desk: check the bill", state("H-204") == "wrong_charge"),
        ("A change that never took", "late", "the FMS still shows a change for a date that has passed, at the old rent",
         "a Today row: not applied", "Front desk: check the lease", state("M-611") == "not_applied"),
        ("A charge that straddles the change", "money", "a rent charge from before the change runs past its date",
         "judges only the first full period: billed at the FMS rent", nobody, state("G-118") == "verified"),
        ("A discounted lease", "disagree", "a discount makes the net rent differ from the rent",
         "says it cannot tell instead of calling the bill wrong", "A person compares the bill with the discount", state("P-815") == "cannot_tell"),
        ("An increase with nothing in the way", "normal", "a 7 percent increase on a paid-up tenant", "clear; the notice still shows not proven",
         nobody, state("J-330") == "clear"),
    ]
    found = judge(rows)
    lines = [(line, found[line][0], found[line][1], "rows" if line in (NOTICE_SENT, UNPROVEN) else "leases") for line in LINES]

    def caught(target, name, replacement, line) -> bool:
        with broken(target, name, replacement):
            return judge(one_pass(url))[line][0] > 0

    controls = [
        ("A build that takes the planned notice date as the notice sent", "Notice as sent", "disagree",
         "marks the notice sent on the date the FMS planned", caught(rent_changes, "upcoming", notice_as_sent(rent_changes.upcoming), NOTICE_SENT)),
        ("A build that compares the monthly rent with a 28-day bill", "Monthly vs 28 days", "disagree",
         "calls a right 28-day bill wrong", caught(rent_changes, "completed", monthly_against_28_days(rent_changes.completed), CYCLE_WRONG)),
        ("A build that bills from the period straddling the change", "Straddling period", "money",
         "judges the prorated charge as the first bill", caught(rent_changes, "first_full_period", straddling_period, STRADDLE)),
        ("A build that counts a passed date as applied", "Date as applied", "late",
         "calls a change applied because its date has passed", caught(rent_changes, "overdue_schedule", passed_date_as_applied(rent_changes.overdue_schedule), UNPROVEN)),
    ]
    return table, lines, controls
