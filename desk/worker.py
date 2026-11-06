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


def run_once(factory: sessionmaker[Session], adapter: FMSAdapter) -> tuple[int, int]:
    """One pass: how many payments were checked, and how many FMS reads failed along the way."""
    with factory() as session:
        service = PaymentService(session, adapter)
        checked = service.reconcile_all()
        return checked, service.read_failures


class Worker:
    def __init__(self, factory: sessionmaker[Session], adapter: FMSAdapter, every_seconds: float, stale_after: float | None = None):
        self.factory, self.adapter, self.every = factory, adapter, every_seconds
        # how long without a successful pass before readiness says the worker is not doing its job
        self.stale_after = stale_after if stale_after is not None else every_seconds * 3 + 30
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="reconcile", daemon=True)
        # monotonic time of the last pass that succeeded; readiness reads it
        self.last_ok: float | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def alive(self) -> bool:
        """The loop is running and a whole pass succeeded recently. A stuck FMS call, or passes that fail every
        time or fail on some reads every time, show up here; so does a worker that has not yet finished one."""
        if not self._thread.is_alive() or self.last_ok is None:
            return False
        return time.monotonic() - self.last_ok < self.stale_after

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.every + 5)

    def _loop(self) -> None:
        # the first pass runs at once, so readiness can be earned right after startup
        while True:
            try:
                checked, failed = run_once(self.factory, self.adapter)
                if failed:
                    # the pass went on with the other cases, but it did not do its whole job
                    self.last_error = f"{failed} FMS read(s) failed"
                    log.warning("reconcile pass: %s", self.last_error)
                else:
                    self.last_error, self.last_ok = None, time.monotonic()
                if checked:
                    log.info("reconciled %d payment(s)", checked)
            except Exception as exc:
                # a bad FMS read must not kill the loop; the next pass tries again
                self.last_error = type(exc).__name__
                log.exception("reconcile pass failed")
            if self._stop.wait(self.every):
                return


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
