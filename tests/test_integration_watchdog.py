"""When the FMS stops answering as its documentation says: the adapter keeps a status as written and refuses an amount that is not whole cents,
the desk never retries money, and one row stays on Today until a person has looked. The simulator's /sim/drift changes how its payment calls write a payment;
what happened, the ledger and /sim/truth stay as they were."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from desk.adapter import Anomaly, FMSError
from desk.adapter.cubby import CubbyAdapter
from desk.integration import is_open, open_observations, review, save_notes
from desk.models import FmsObservation
from desk.payments import PaymentRefused, PaymentService
from desk.worker import run_once

from .conftest import FULL_KEY
from .test_pages import preview
from .test_verified_payment import DANA, pay, settle


def drift(sim, kind):
    assert sim.post("/sim/drift", {"kind": kind}).status_code == 200


def test_a_renamed_status_is_kept_as_written_and_noted(session, adapter, sim):
    pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    adapter.take_anomalies()
    drift(sim, "status")
    [payment] = adapter.payments_for_lease(DANA)
    assert payment.status == "SETTLED"             # never read as SUCCEEDED, and never dropped
    assert adapter.take_anomalies() == [Anomaly("payments search", "status", "unlisted value", "SETTLED")]
    assert adapter.take_anomalies() == []          # a note is handed over once


def test_an_amount_that_is_not_whole_cents_is_refused_never_rounded(session, adapter, sim):
    pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    adapter.take_anomalies()
    drift(sim, "amount")
    with pytest.raises(FMSError, match="unreadable payment"):
        adapter.payments_for_lease(DANA)
    assert adapter.take_anomalies() == [Anomaly("payments search", "amount", "not whole cents", "100.00")]


def test_a_payment_with_no_time_is_kept_and_noted(session, adapter, sim):
    pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    adapter.take_anomalies()
    drift(sim, "created_at")
    [payment] = adapter.payments_for_lease(DANA)
    assert payment.created_at is None and payment.amount == 10000 and payment.status == "SUCCEEDED"
    assert adapter.take_anomalies() == [Anomaly("payments search", "createdAt", "missing", "")]


def test_a_payment_sent_while_the_status_is_renamed_is_held_for_a_person_and_sent_once(session, adapter, sim):
    drift(sim, "status")
    command, case, _ = pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    # the money moved and the FMS answered, but not in words the desk knows: the payment id is kept, and it is not retried
    assert command.state == "OUTCOME_UNKNOWN" and command.provider_status == "SETTLED" and command.provider_payment_id
    seen = {(a.endpoint, a.field, a.value) for a in adapter.take_anomalies()}
    assert {("make payment", "status", "SETTLED"), ("payments search", "status", "SETTLED")} <= seen
    command.submitted_at -= timedelta(hours=2)      # the wait for the FMS to settle it is over
    session.commit()
    PaymentService(session, adapter).reconcile(command)
    assert command.state == "NEEDS_STAFF" and "'SETTLED', which the desk does not know" in command.provider_reason
    with pytest.raises(PaymentRefused):             # a tenant who asks again is not charged again
        pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    settle(session, adapter, times=3)
    assert len(sim.payments(DANA)) == 1             # one charge, however many passes and asks
    assert sim.total_due(DANA) == 14000             # and it did apply: the FMS truth is untouched by the drift


def test_notes_are_saved_once_per_kind_with_a_count_and_reopen_after_a_review(session, adapter, sim):
    pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    adapter.take_anomalies()
    drift(sim, "status")
    for _ in range(2):
        adapter.payments_for_lease(DANA)
    assert save_notes(session, adapter) == 2
    [row] = open_observations(session)
    assert (row.endpoint, row.field, row.kind, row.value, row.count) == ("payments search", "status", "unlisted value", "SETTLED", 2)
    assert save_notes(session, adapter) == 0        # nothing new, nothing added
    assert session.scalar(select(FmsObservation).where(FmsObservation.id == row.id)).count == 2

    assert review(session, row.id, "Cubby renamed the status; confirmed with their changelog and updated the adapter.") is not None
    assert open_observations(session) == []
    adapter.payments_for_lease(DANA)
    save_notes(session, adapter)
    [again] = open_observations(session)
    assert again.id == row.id and again.count == 3 and is_open(again)    # the same thing after a review opens it again
    assert review(session, 9999, "no such record, so nothing is changed") is None


def test_a_documented_answer_leaves_no_note(session, adapter, sim):
    pay(session, adapter, DANA, "pm_DanaVisa4242", 10000)
    adapter.payments_for_lease(DANA)
    assert adapter.take_anomalies() == []


def test_today_shows_one_row_for_the_connection_and_the_api_lists_and_closes_it(fms_url, sim):
    adapter = CubbyAdapter(fms_url, FULL_KEY, 5.0)
    app = preview(fms_url, adapter=adapter)
    with TestClient(app) as visitor:
        assert "FMS answer not as documented" not in visitor.get("/today").text
        assert visitor.get("/api/integration").json() == {"open": []}
        # the world is built; then the vendor renames a status, and the desk's next pass reads a lease that has a payment on it
        drift(sim, "status")
        run_once(app.state.session_factory, adapter)
        page = visitor.get("/today").text
        assert page.count("FMS answer not as documented") == 1 and "SETTLED" in page and "The FMS connection" in page
        assert "FMS answer not as documented" in visitor.get("/today", params={"facility": "fac_NSalbany001"}).text      # it is the connection's, not one facility's
        [row] = visitor.get("/api/integration").json()["open"]
        assert (row["endpoint"], row["field"], row["value"]) == ("payments search", "status", "SETTLED") and row["count"] >= 1
        note = {"note": "Cubby renamed the status; confirmed with their changelog and updated the adapter."}
        assert visitor.post(f"/api/integration/{row['id']}/review", json=note).status_code == 401           # a visitor cannot close it
        assert visitor.post(f"/api/integration/{row['id']}/review", json={"note": "ok"}, headers={"x-desk-token": "op-token"}).status_code == 422
        closed = visitor.post(f"/api/integration/{row['id']}/review", json=note, headers={"x-desk-token": "op-token"})
        assert closed.status_code == 200 and closed.json()["open"] is False
        assert visitor.post("/api/integration/9999/review", json=note, headers={"x-desk-token": "op-token"}).status_code == 404
        assert "FMS answer not as documented" not in visitor.get("/today").text
        assert visitor.get("/api/integration").json() == {"open": []}
