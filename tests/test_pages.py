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
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=2.0,
                        reconcile_every_seconds=60, operator_token="op-token", staff_token="staff-token",
                        public_view=True, demo_seed=True, **kw)
    return create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 2.0), start_worker=False)


def counts(app) -> dict:
    with app.state.session_factory() as s:
        return {t.name: s.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def test_today_lists_each_open_item_with_what_proves_it_done(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/").text
        for head in ("Payment outcome unknown", "Lien file out of date", "Payment returned", "Past due"):
            assert head in page
        assert page.index("Payment outcome unknown") < page.index("Payment returned")
        assert "A person names the FMS payment that is this attempt" in page
        assert "Synthetic technical preview" in page
        albany = visitor.get("/", params={"facility": "fac_NSalbany001"}).text
        assert "Sent to counsel" in albany and "Payment outcome unknown" not in albany


def test_the_lost_payment_shows_both_sides_and_offers_no_payment_button(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/payments/1").text
        assert "Outcome unknown: held for a person" in page
        assert "None before the timeout" in page and "never sent again" in page
        assert "&ldquo;Yes, go ahead&rdquo;" in page or "Yes, go ahead" in page
        # the simulator's own record, saved when the world was built: one charge, applied
        assert "What actually happened, from the simulator" in page and "SUCCEEDED, applied to the ledger" in page
        assert "<form" not in page.split("<main>")[1] and "Mark paid" not in page


def test_the_record_keeps_each_source_in_its_own_lane(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        rows = visitor.get(f"/api/facilities/{ORLANDO}/queue").json()["rows"]
        gina = next(r for r in rows if r["lease"] == "lease_OkfrE0505")["case_id"]
        page = visitor.get(f"/accounts/{gina}").text
        for lane in ("The FMS said", "Gatehouse observed", "Gatehouse concluded", "A person decided"):
            assert lane in page
        assert "9 legal checks recorded" in page and "Payment at the counter" in page
        lien = visitor.get(f"/accounts/{gina}/lien").text
        assert "Account changed: re-review" in lien and "Changed since the evaluation" in lien
        assert "compliant" not in lien.replace("never says a file is compliant", "")


def test_reading_every_page_writes_nothing(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        before = counts(app)
        pages = ["/", "/?facility=fac_NSalbany001", "/limits"]
        for case_id in range(1, 7):
            pages += [f"/accounts/{case_id}", f"/accounts/{case_id}/lien"]
        pages += ["/payments/1", "/payments/2"]
        for path in pages:
            assert visitor.get(path).status_code in (200, 404), path
        assert counts(app) == before
        assert visitor.get("/accounts/999").status_code == 404 and visitor.get("/payments/999").status_code == 404


def test_the_screens_exist_only_in_the_preview(fms_url, sim):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=2.0,
                        reconcile_every_seconds=60, operator_token="op-token")
    with TestClient(create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 2.0), start_worker=False)) as c:
        assert c.get("/").status_code == 404 and c.get("/payments/1").status_code == 404
