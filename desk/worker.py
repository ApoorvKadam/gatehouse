"""The reconciliation worker: re-reads the FMS for every unsettled payment and every open case on a timer.

It only reads. Submitting happens in one place (PaymentService.take_payment) and never from here."""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.orm import Session, sessionmaker

from .adapter import FMSAdapter
from .payments import PaymentService

log = logging.getLogger("desk.worker")


def run_once(factory: sessionmaker[Session], adapter: FMSAdapter) -> int:
    with factory() as session:
        return PaymentService(session, adapter).reconcile_all()


class Worker:
    def __init__(self, factory: sessionmaker[Session], adapter: FMSAdapter, every_seconds: float):
        self.factory, self.adapter, self.every = factory, adapter, every_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="reconcile", daemon=True)
        # monotonic time of the last pass that finished, failed or not; readiness reads it
        self.last_pass: float | None = None
        self.last_error: str | None = None
        self.started_at = time.monotonic()

    def start(self) -> None:
        self.started_at = time.monotonic()
        self._thread.start()

    def alive(self) -> bool:
        """The loop is running and has finished a pass recently (a stuck FMS call shows up here)."""
        if not self._thread.is_alive():
            return False
        since = time.monotonic() - (self.last_pass if self.last_pass is not None else self.started_at)
        return since < self.every * 3 + 30

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.every + 5)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            try:
                checked = run_once(self.factory, self.adapter)
                self.last_error = None
                if checked:
                    log.info("reconciled %d payment(s)", checked)
            except Exception as exc:
                # a bad FMS read must not kill the loop; the next pass tries again
                self.last_error = type(exc).__name__
                log.exception("reconcile pass failed")
            self.last_pass = time.monotonic()


if __name__ == "__main__":
    import time

    from .adapter.cubby import CubbyAdapter
    from .config import Settings
    from .db import bind_to_fms, make_session_factory

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = Settings.from_env()
    factory, fms = make_session_factory(s.database_url), CubbyAdapter(s.fms_base_url, s.fms_api_key, s.fms_timeout_seconds)
    bind_to_fms(factory, fms, s.fms_base_url)   # the same check the API makes: never reconcile another account's records
    w = Worker(factory, fms, s.reconcile_every_seconds)
    w.start()
    while True:
        time.sleep(3600)
