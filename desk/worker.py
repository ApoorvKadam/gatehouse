"""The reconciliation worker: re-reads the FMS for every unsettled payment and every open case on a timer, then
checks access at every facility this database is bound to.

It only reads, apart from asking for FMS tasks when access needs a person. Submitting a payment happens in one place
(PaymentService.take_payment) and never from here."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from .access import bound_facilities, reconcile_access
from .adapter import FMSAdapter, FMSError
from .messaging import match_pending
from .payments import PaymentService
from .sales import reconcile_sales

log = logging.getLogger("desk.worker")


def run_once(factory: sessionmaker[Session], adapter: FMSAdapter, run_id: str = "local", feed_url: str | None = None) -> tuple[int, int]:
    """One pass: how many payments were checked, and how many FMS reads failed along the way (payments, access, sales, and
    matching texts that arrived while the FMS could not be asked)."""
    with factory() as session:
        service = PaymentService(session, adapter)
        checked = service.reconcile_all()
        failures = service.read_failures
        for facility in bound_facilities(session):
            result = reconcile_access(session, adapter, facility, run_id)
            failures += len(result.failures)
            if result.opened or result.closed:
                log.info("access at %s: %d opened, %d closed", facility.provider_id, result.opened, result.closed)
            sales = reconcile_sales(session, adapter, facility, run_id, feed_url)
            failures += len(sales.failures)
            if sales.opened or sales.closed:
                log.info("sales at %s: %d opened, %d closed", facility.provider_id, sales.opened, sales.closed)
            try:
                matched = match_pending(session, adapter, facility)
            except FMSError as exc:
                failures += 1
                log.warning("texts at %s: %s", facility.provider_id, exc)
            else:
                if matched:
                    log.info("texts at %s: %d matched", facility.provider_id, matched)
        return checked, failures


class Worker:
    def __init__(self, factory: sessionmaker[Session], adapter: FMSAdapter, every_seconds: float, stale_after: float | None = None,
                 run_id: str = "local", feed_url: str | None = None):
        self.factory, self.adapter, self.every, self.run_id, self.feed_url = factory, adapter, every_seconds, run_id, feed_url
        # how long without a successful pass before readiness says the worker is not doing its job
        self.stale_after = stale_after if stale_after is not None else every_seconds * 3 + 30
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="reconcile", daemon=True)
        # monotonic time of the last pass that succeeded; readiness reads it
        self.last_ok: float | None = None
        self.last_error: str | None = None
        # the last passes, newest last: when each started (UTC), how long it took, what it checked, what failed
        self.passes: deque[dict] = deque(maxlen=30)

    def start(self) -> None:
        self._thread.start()

    def alive(self) -> bool:
        """The loop is running and a whole pass succeeded recently. A stuck FMS call, or passes that fail every
        time or fail on some reads every time, show up here; so does a worker that has not yet finished one."""
        if not self._thread.is_alive() or self.last_ok is None:
            return False
        return time.monotonic() - self.last_ok < self.stale_after

    def state(self) -> str:
        """In words, for the operations page. Only a latest pass that did its whole job, recently, is "Passing";
        an earlier good pass never covers a later failure."""
        if not self.passes:
            return "No pass finished yet"
        last = self.passes[-1]
        if last["error"] or last["failures"]:
            return "Last pass failed"
        return "Passing" if self.alive() else "Not passing"

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.every + 5)

    def _loop(self) -> None:
        # the first pass runs at once, so readiness can be earned right after startup
        while True:
            started, clock = datetime.now(timezone.utc).replace(tzinfo=None), time.monotonic()
            record = {"started_at": started, "seconds": None, "checked": None, "failures": None, "error": None}
            try:
                checked, failed = run_once(self.factory, self.adapter, self.run_id, self.feed_url)
                record.update(checked=checked, failures=failed)
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
                record["error"] = type(exc).__name__
                log.exception("reconcile pass failed")
            record["seconds"] = round(time.monotonic() - clock, 2)
            self.passes.append(record)
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
