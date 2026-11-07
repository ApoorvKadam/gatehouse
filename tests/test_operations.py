"""The operations page: the worker's recent passes, the latest readings and the open work, from the database only."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from desk.adapter.cubby import CubbyAdapter
from desk.db import make_session_factory
from desk.models import DelinquencyCase, PaymentCommand
from desk.worker import Worker

from .conftest import FULL_KEY
from .test_pages import counts, preview


def test_the_page_counts_open_work_with_todays_rules_and_says_when_no_worker_runs_here(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        before = counts(app)
        page = visitor.get("/operations").text
        assert "Not running in this process" in page
        # the same heads Today shows, including a past-due account with no payment or lien step
        for head in ("Payment outcome unknown", "Payment returned", "Lien file out of date", "Access not restored", "Past due"):
            assert head in page
        assert counts(app) == before          # reading the page writes nothing


def test_a_return_on_an_account_paid_since_is_not_open_work(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        assert "Payment returned" in visitor.get("/operations").text
        with app.state.session_factory() as s:
            returned = s.scalars(select(PaymentCommand).where(PaymentCommand.state == "RETURNED")).all()
            assert returned
            for c in returned:
                s.get(DelinquencyCase, c.case_id).state = "paid"
            s.commit()
        assert "Payment returned" not in visitor.get("/today").text
        assert "Payment returned" not in visitor.get("/operations").text


def test_an_earlier_good_pass_never_covers_a_later_failure():
    worker = Worker(make_session_factory("sqlite:///:memory:"), adapter=None, every_seconds=60)
    assert worker.state() == "No pass finished yet"
    worker.last_ok = time.monotonic()
    worker.passes.append({"started_at": None, "seconds": 0.1, "checked": 1, "failures": 0, "error": None})
    worker.passes.append({"started_at": None, "seconds": 0.1, "checked": 1, "failures": 1, "error": None})
    assert worker.state() == "Last pass failed"
    worker.passes.append({"started_at": None, "seconds": 0.1, "checked": None, "failures": None, "error": "ReadTimeout"})
    assert worker.state() == "Last pass failed"
    worker.passes.append({"started_at": None, "seconds": 0.1, "checked": 1, "failures": 0, "error": None})
    assert worker.state() == "Not passing"     # a good latest pass, but the loop is not running


def test_the_worker_keeps_a_record_of_its_recent_passes(fms_url, sim):
    factory = make_session_factory("sqlite:///:memory:")
    adapter = CubbyAdapter(fms_url, FULL_KEY, 5.0)
    worker = Worker(factory, adapter, every_seconds=0.05)
    worker.start()
    for _ in range(100):
        if len(worker.passes) >= 2:
            break
        time.sleep(0.05)
    worker.stop()
    adapter.close()
    assert len(worker.passes) >= 2
    first = worker.passes[0]
    assert first["seconds"] is not None and first["failures"] == 0 and first["error"] is None
