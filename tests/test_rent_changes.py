"""The rent change check: each change the operator scheduled or completed, and the work around it. The simulator's seed holds one case of each
kind; the desk, the simulator and the pages are the real ones."""
from __future__ import annotations

import html

from fastapi.testclient import TestClient
from sqlalchemy import select

from desk.adapter.base import RentFacts
from desk.models import Facility, Lease, RentChangeCheck
from desk.rent_changes import NOTICE, NOTICE_WHY, Account, completed, in_lease_cycle, overdue_schedule, reconcile_rent_changes, upcoming

from .conftest import ORLANDO
from .test_investigator import factory  # noqa: F401
from .test_pages import preview


def rows(s) -> dict[str, dict]:
    found = s.execute(select(Lease.unit_name, RentChangeCheck).join(RentChangeCheck, RentChangeCheck.lease_id == Lease.id)).all()
    return {unit: c.data for unit, c in found}


def test_each_seeded_change_gets_the_verdict_its_facts_support(factory, adapter):  # noqa: F811
    with factory() as s:
        facility = s.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
        result = reconcile_rent_changes(s, adapter, facility)
        assert result.failures == []
        got = rows(s)
        assert {unit: r["state"] for unit, r in got.items()} == {
            "G-118": "verified", "L-520": "verified",          # the 28-day lease is right once its bill is read in its own cycle
            "H-204": "wrong_charge", "M-611": "not_applied", "P-815": "cannot_tell",
            "J-330": "clear", "K-412": "review", "F-610": "review", "A-012": "review", "E-505": "hold"}
        assert "billed $140.00, but the rent the FMS reports is $155.00" in got["H-204"]["why"]
        assert "discount" in got["P-815"]["why"]
        assert "28 percent" in " ".join([got["K-412"]["why"], *got["K-412"]["also"]]) and "120 days ago" in " ".join(got["K-412"]["also"])
        assert "past due" in got["F-610"]["why"] and "payment arrangement" in got["A-012"]["why"] and "lien file" in got["E-505"]["why"]
        # a notice is never proven, whatever else the row says
        assert all(dict((n, (st, w)) for n, st, w in r["steps"])["Notice"] == ("unproven", NOTICE) for r in got.values())


def test_a_28_day_lease_is_compared_in_its_own_cycle_and_never_against_the_monthly_figure():
    assert in_lease_cycle(30000, "MONTHLY", "DAYS_28") == 27692 and in_lease_cycle(27692, "DAYS_28", "MONTHLY") == 30000
    assert in_lease_cycle(30000, "MONTHLY", "FORTNIGHTLY") is None
    f = RentFacts("lease_x", "cust_x", "L-1", "MONTHLY", "DAYS_28", 30000, None, None)
    from datetime import date
    since = date(2026, 9, 1)
    bill = lambda amount: [{"type": "CHARGE", "amount": amount, "amountPaid": 0, "details": {"category": "RENT", "accruesOn": "2026-09-01", "accruesThrough": "2026-09-28"}}]
    assert completed(f, since, bill(27692), date(2026, 11, 1))["state"] == "verified"
    assert completed(f, since, bill(30000), date(2026, 11, 1))["state"] == "wrong_charge"


def test_the_bill_is_the_first_full_period_never_a_straddling_one_or_the_amount_paid():
    from datetime import date
    f = RentFacts("lease_x", "cust_x", "G-1", "MONTHLY", "MONTHLY", 13500, None, None)
    entries = [
        # starts before the change: it may mix both rents, so it says nothing
        {"type": "CHARGE", "amount": 13000, "amountPaid": 13000, "details": {"category": "RENT", "accruesOn": "2026-09-20", "accruesThrough": "2026-10-19"}},
        # the first full period at the new rent, not yet paid: a charge unpaid is still the bill
        {"type": "CHARGE", "amount": 13500, "amountPaid": 0, "details": {"category": "RENT", "accruesOn": "2026-10-20", "accruesThrough": "2026-11-19"}},
        {"type": "CHARGE", "amount": 2500, "amountPaid": 0, "details": {"category": "FEE", "accruesOn": "2026-10-20"}},
    ]
    assert completed(f, date(2026, 10, 1), entries, date(2026, 11, 1))["state"] == "verified"
    assert completed(f, date(2026, 10, 1), entries[:1], date(2026, 11, 1))["state"] == "cannot_tell"      # no full period billed yet


def test_an_amount_in_another_shape_is_noted_and_never_coerced(adapter):
    bad: list[str] = []
    assert adapter._cents("leases search", "rentChangeNewRentAmount", "135.00", bad) is None and bad == ["rentChangeNewRentAmount"]
    assert adapter._cents("leases search", "rentChangeNewRentAmount", True, bad) is None
    assert adapter._cents("leases search", "monthlyRentalAmount", 13500, bad) == 13500
    notes = adapter.take_anomalies()
    assert {(n.endpoint, n.field, n.kind) for n in notes} == {("leases search", "rentChangeNewRentAmount", "not whole cents")}
    from datetime import date
    f = RentFacts("lease_x", "cust_x", "G-1", "MONTHLY", "MONTHLY", None, None, None, unreadable=("monthlyRentalAmount",))
    assert completed(f, date(2026, 10, 1), [], date(2026, 11, 1))["state"] == "cannot_tell"


def test_the_page_today_and_the_api_show_the_work_in_plain_words(fms_url, sim, adapter):
    with TestClient(preview(fms_url, adapter)) as visitor:
        app = visitor.app
        with app.state.session_factory() as s:
            facility = s.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
            reconcile_rent_changes(s, adapter, facility)
        page = html.unescape(visitor.get("/rent-changes").text)
        assert "Rent changes at Northstar Storage, Orlando East" in page and 'href="/rent-changes"' in page
        assert "Billed at another amount" in page and "Not applied" in page and "Billed at the FMS rent" in page and "Cannot tell yet" in page
        assert "Gatehouse sets no price and changes no rent." in page and page.count(NOTICE_WHY) == 1
        assert "lease_" not in page and "not_applied" not in page and "wrong_charge" not in page and "cannot_tell" not in page
        today = html.unescape(visitor.get("/today").text)
        for head in ("Rent billed at another amount", "Rent change not applied", "Rent increase on an account a person must weigh"):
            assert head in today, head
        operations = html.unescape(visitor.get("/operations").text)
        assert "Rent changes" in operations.split("Open work by part", 1)[-1][:2000]      # counted as its own part, not as collections
        api = visitor.get(f"/api/facilities/{ORLANDO}/rent-changes").json()
        assert {r["unit"] for r in api["rent_changes"]} >= {"G-118", "H-204", "M-611"}


def rent(amount, start, through, paid=0):
    return {"type": "CHARGE", "amount": amount, "amountPaid": paid, "details": {"category": "RENT", "accruesOn": start, "accruesThrough": through}}


def test_a_correcting_charge_for_the_same_period_closes_a_wrong_bill():
    from datetime import date
    f = RentFacts("lease_x", "cust_x", "H-1", "MONTHLY", "MONTHLY", 15500, None, None)
    billed = [rent(14000, "2026-10-07", "2026-11-06")]
    assert completed(f, date(2026, 10, 7), billed, date(2026, 11, 11))["state"] == "wrong_charge"
    corrected = billed + [rent(1500, "2026-10-07", "2026-11-06")]
    assert completed(f, date(2026, 10, 7), corrected, date(2026, 11, 11))["state"] == "verified"


def test_a_change_past_its_date_with_the_rent_already_new_is_applied_and_its_bill_checked():
    from datetime import date
    applied = RentFacts("lease_x", "cust_x", "M-1", "MONTHLY", "MONTHLY", 11800, "2026-11-06", 11800)
    assert overdue_schedule(applied, date(2026, 11, 6), [rent(11800, "2026-11-06", "2026-12-05")], date(2026, 11, 11))["state"] == "verified"
    stuck = RentFacts("lease_x", "cust_x", "M-1", "MONTHLY", "MONTHLY", 11000, "2026-11-06", 11800)
    assert overdue_schedule(stuck, date(2026, 11, 6), [rent(11000, "2026-11-06", "2026-12-05")], date(2026, 11, 11))["state"] == "not_applied"


def test_a_change_due_today_is_still_upcoming(factory, adapter):  # noqa: F811
    from datetime import date
    f = RentFacts("lease_x", "cust_x", "J-1", "MONTHLY", "MONTHLY", 10000, "2026-11-11", 10700)
    row = upcoming(f, date(2026, 11, 11), Account(), date(2026, 11, 11))
    assert row["state"] == "clear" and "has passed" not in row["why"]
