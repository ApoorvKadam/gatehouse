"""The operations page: the worker's recent passes, the latest readings and the open work, from the database only."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from desk.adapter.cubby import CubbyAdapter
from desk.db import make_session_factory
from desk.worker import Worker

from .conftest import FULL_KEY
from .test_pages import counts, preview


def test_the_page_lists_open_work_and_says_when_no_worker_runs_here(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        before = counts(app)
        page = visitor.get("/operations").text
        assert "Not running in this process" in page
        for label in ("Held for a person", "Returned, refunded or disputed", "Account changed: re-review", "Access not restored"):
            assert label in page
        assert counts(app) == before          # reading the page writes nothing


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
