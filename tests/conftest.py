"""The simulator runs as a real HTTP server for the whole test session; the desk talks to it only over the wire."""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from desk.adapter.cubby import CubbyAdapter
from desk.db import make_session_factory
from fmssim.app import create_app

FULL_KEY = "sim_org_northstar_full"
ORLANDO = "fac_NSorlando01"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def fms_url(tmp_path_factory):
    db = tmp_path_factory.mktemp("fms") / "fmssim.db"
    app = create_app(f"sqlite:///{db}", lose_response_seconds=3.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.post(f"{url}/sim/reset", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def sim(fms_url):
    """Test controls for the simulator. The FMS is reset before every test."""

    class Sim:
        def post(self, path, body=None, key=FULL_KEY):
            return httpx.post(f"{fms_url}{path}", json=body or {}, headers={"Authorization": f"Bearer {key}"}, timeout=10)

        def behave(self, lease_id, mode, lag=2):
            assert self.post("/sim/behaviors", {"leaseId": lease_id, "mode": mode, "lag": lag}).status_code == 200

        def payments(self, lease_id):
            return self.post("/v1/payments/search", {"where": {"leaseId": lease_id}}).json()["data"]["payments"]

        def total_due(self, lease_id):
            return self.post("/v1/ledgers/search", {"where": {"leaseId": lease_id}}).json()["data"]["ledgers"][0]["balances"]["totalDue"]

    s = Sim()
    s.post("/sim/reset")
    return s


@pytest.fixture
def adapter(fms_url):
    # below the simulator's 3 s lost-response delay, with room for a busy machine on ordinary reads
    a = CubbyAdapter(fms_url, FULL_KEY, timeout_seconds=2.0)
    yield a
    a.close()


@pytest.fixture
def session():
    factory = make_session_factory("sqlite:///:memory:")
    with factory() as s:
        yield s
