"""The preview's screens: what a first-time visitor sees, and that reading them changes nothing."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from desk.adapter.cubby import CubbyAdapter
from desk.api import create_app
from desk.config import Settings
from desk.db import Base

from .conftest import FULL_KEY, ORLANDO


def preview(fms_url, **kw):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token="op-token", staff_token="staff-token",
                        public_view=True, demo_seed=True, **kw)
    return create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False)


def counts(app) -> dict:
    with app.state.session_factory() as s:
        return {t.name: s.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def test_today_lists_each_open_item_with_what_proves_it_done(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/today").text
        for head in ("Payment outcome unknown", "Lien file out of date", "Payment returned", "Past due"):
            assert head in page
        assert page.index("Payment outcome unknown") < page.index("Payment returned")
        assert "A person names the FMS payment that is this attempt" in page
        assert "Synthetic technical preview" in page
        albany = visitor.get("/today", params={"facility": "fac_NSalbany001"}).text
        assert "Counsel review required" in albany and "Payment outcome unknown" not in albany
        # a facility id that is not ours is not quietly swapped for another facility
        assert visitor.get("/today", params={"facility": "fac_HBtampa0001"}).status_code == 404
        assert visitor.get("/today?facility=").status_code == 404


def test_the_lost_payment_shows_both_sides_and_offers_no_payment_button(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/payments/1").text
        assert "Outcome unknown: held for a person" in page
        assert "No usable answer" in page and "never sent again" in page
        assert "&ldquo;Yes, go ahead&rdquo;" in page or "Yes, go ahead" in page
        # the simulator's own record, saved when the world was built: one charge, applied
        assert "The simulator&#39;s own record" in page or "The simulator's own record" in page
        assert "SUCCEEDED, applied to the ledger (possible match)" in page and "did not retry it" in page
        returned = visitor.get("/payments/2").text
        assert "DIRECT_DEBIT_RETURN" in returned and "(this attempt)" in returned and "did not retry it" not in returned
        assert "<form" not in page.split("<main>")[1] and "Mark paid" not in page


def test_the_record_keeps_each_source_in_its_own_lane(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        rows = visitor.get(f"/api/facilities/{ORLANDO}/queue").json()["rows"]
        gina = next(r for r in rows if r["lease"] == "lease_OkfrE0505")["case_id"]
        page = visitor.get(f"/accounts/{gina}").text
        for lane in ("The FMS said", "Gatehouse observed", "Gatehouse concluded", "A person decided"):
            assert lane in page
        assert "9 legal checks recorded" in page and "Payment made outside Gatehouse" in page
        lien = visitor.get(f"/accounts/{gina}/lien").text
        assert "Account changed: re-review" in lien and "Changed since the evaluation" in lien
        assert "compliant" not in lien.replace("never says a file is compliant", "")


def test_reading_every_page_writes_nothing(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        before = counts(app)
        pages = ["/", "/today", "/today?facility=fac_NSalbany001", "/limits"]
        for case_id in range(1, 7):
            pages += [f"/accounts/{case_id}", f"/accounts/{case_id}/lien"]
        pages += ["/payments/1", "/payments/2"]
        for path in pages:
            assert visitor.get(path).status_code in (200, 404), path
        assert counts(app) == before
        assert visitor.get("/accounts/999").status_code == 404 and visitor.get("/payments/999").status_code == 404


def test_the_screens_exist_only_in_the_preview(fms_url, sim):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token="op-token")
    with TestClient(create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False)) as c:
        assert c.get("/").status_code == 404 and c.get("/today").status_code == 404 and c.get("/payments/1").status_code == 404


def test_a_payment_still_pending_at_the_fms_is_shown_as_in_progress(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor, TestClient(app, headers={"X-Desk-Token": "op-token"}) as desk:
        sim.behave("lease_RamnA0012", "pending_then_succeed", lag=5)
        rows = desk.get(f"/api/facilities/{ORLANDO}/queue").json()["rows"]
        case_id = next(r for r in rows if r["lease"] == "lease_RamnA0012")["case_id"]
        check = desk.post(f"/api/cases/{case_id}/identity", json={"last_name": "Raman", "phone_last4": "0119", "unit_name": "A-012"}).json()
        proposal = desk.post(f"/api/cases/{case_id}/proposals", json={"identity_check_id": check["identity_check_id"], "amount": 9500}).json()
        paid = desk.post(f"/api/cases/{case_id}/proposals/{proposal['proposal_id']}/confirm",
                         json={"confirmed_by": "J. Ortiz", "tenant_answer": "Yes"}).json()
        assert paid["state"] == "PENDING_AT_FMS"
        page = visitor.get("/today").text
        unit = page[page.index("Unit A-012") - 400: page.index("Unit A-012") + 400]
        assert "Payment in progress" in unit and "No payment or lien step in progress" not in unit


def test_a_lien_file_shows_its_state_as_of_its_evaluation_beside_the_balance_now(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor, TestClient(app, headers={"X-Desk-Token": "op-token"}) as desk:
        # the tenant pays the rest at the counter; Gatehouse sees it on its next read of the FMS
        sim.post("/sim/payments/offline", {"allocations": {"lease_OkfrE0505": 11500}})
        assert desk.post("/api/reconcile").status_code == 200
        lien = visitor.get("/accounts/2/lien").text
        assert "Account changed: re-review</span><span class=\"muted small\">as of" in lien and "Owed on this account, per the FMS: <b>$0.00</b>" in lien
        page = visitor.get("/today").text
        unit = page[page.index("Unit E-505") - 400: page.index("Unit E-505") + 500]
        assert "Lien file out of date" in unit and "As of the evaluation on" in unit


def test_the_seeded_lien_file_is_not_called_stale_by_its_own_payment(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        lien = visitor.get("/accounts/2/lien").text
        assert "Account changed: re-review</span><span class=\"muted small\">as of" in lien and "not reflected here until the file is evaluated again" in lien


def test_the_front_page_shows_every_built_feature_with_a_link_into_it(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/").text
        for heading in ("A payment whose answer was lost is held, not retried.", "Every fact keeps its source.",
                        "A payment that comes back reopens the account.", "A lien file goes back to review when the account changes.",
                        "Where no rules are reviewed, the file goes to counsel."):
            assert heading in page
        for link in ("/today", "/payments/1", "/payments/2", "/accounts/2/lien", "/accounts/6/lien"):
            assert f'href="{link}"' in page and visitor.get(link).status_code == 200
        assert "cents" not in page   # amounts read as dollars


def test_findings_are_written_in_dollars_and_dates_and_quote_records_as_they_are():
    from desk.lien_files import ledger_changes
    from desk.liens import dollars, on
    assert dollars(16500) == "$165.00" and on("2026-11-06") == "Nov 6, 2026" and on("2026-02-30 rent") == "2026-02-30 rent"
    then = {"bookEntries": [{"bookEntryId": "be1", "type": "CHARGE", "amount": 5000, "amountPaid": 0,
                             "details": {"description": 'Fee "5000 cents 2026-10-06" late', "accruesOn": "2026-10-06"}}]}
    now = {"bookEntries": [{"bookEntryId": "be1", "type": "CHARGE", "amount": 5000, "amountPaid": 5000,
                            "details": {"description": 'Fee "5000 cents 2026-10-06" late', "accruesOn": "2026-10-07"}}]}
    changes = ledger_changes(then, now)
    # the description is quoted from the ledger exactly; only Gatehouse's own words carry dollars and dates
    assert 'the Fee "5000 cents 2026-10-06" late charge\'s amount paid changed from $0.00 to $50.00' in changes
    assert 'the Fee "5000 cents 2026-10-06" late charge\'s due date changed from Oct 6, 2026 to Oct 7, 2026' in changes
