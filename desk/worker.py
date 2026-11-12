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
from .db import refresh_binding
from .integration import save_notes
from .investigator import fail_stranded_runs
from .messaging import file_unrouted, hold_stranded, match_pending
from .missed_calls import reconcile_missed_calls
from .payments import PaymentService
from .reminder_check import reconcile_reminder_checks
from .rent_changes import reconcile_rent_changes
from .sales import reconcile_sales

log = logging.getLogger("desk.worker")


def run_once(factory: sessionmaker[Session], adapter: FMSAdapter, run_id: str = "local", feed_url: str | None = None,
             text_numbers: dict[str, str] | None = None) -> tuple[int, int]:
    """One pass: how many payments were checked, and how many FMS reads failed along the way (the facility list, payments,
    access, sales, and matching texts that arrived while the FMS could not be asked)."""
    with factory() as session:
        failures = 0
        try:
            # first, so a facility that joined or left the account is served or dropped on this pass, not at the next restart
            refresh_binding(session, adapter)
        except FMSError as exc:
            failures += 1
            log.warning("could not read which facilities this key sees: %s", exc)
        service = PaymentService(session, adapter)
        checked = service.reconcile_all()
        failures += service.read_failures
        facilities = bound_facilities(session)
        file_unrouted(session, text_numbers, {f.provider_id for f in facilities})
        for facility in facilities:
            result = reconcile_access(session, adapter, facility, run_id)
            failures += len(result.failures)
            if result.opened or result.closed:
                log.info("access at %s: %d opened, %d closed", facility.provider_id, result.opened, result.closed)
            sales = reconcile_sales(session, adapter, facility, run_id, feed_url)
            failures += len(sales.failures)
            if sales.opened or sales.closed:
                log.info("sales at %s: %d opened, %d closed", facility.provider_id, sales.opened, sales.closed)
            try:
                checked_texts = reconcile_reminder_checks(session, adapter, facility)
                failures += len(checked_texts.failures)
            except Exception:
                # the reminder check is read by a page and an API; a bug in it must not stop the texts from being matched or the sales read
                session.rollback()
                failures += 1
                log.exception("reminder check at %s failed", facility.provider_id)
            try:
                failures += len(reconcile_rent_changes(session, adapter, facility, run_id).failures)
            except Exception:
                # like the reminder check, a page reads it; a bug in it must not stop the texts or the sales
                session.rollback()
                failures += 1
                log.exception("rent change check at %s failed", facility.provider_id)
            try:
                failures += len(reconcile_missed_calls(session, adapter, facility, run_id, feed_url).failures)
            except Exception:
                # a page reads it; a bug in it must not stop the texts or the sales
                session.rollback()
                failures += 1
                log.exception("missed calls at %s failed", facility.provider_id)
            try:
                matched = match_pending(session, adapter, facility)
            except FMSError as exc:
                failures += 1
                log.warning("texts at %s: %s", facility.provider_id, exc)
            else:
                if matched:
                    log.info("texts at %s: %d matched", facility.provider_id, matched)
        stranded = hold_stranded(session)
        if stranded:
            log.warning("%d text(s) were cut off before their outcome was recorded and are held for a person", stranded)
        if fail_stranded_runs(session):
            log.warning("a model run was cut off before it finished and is marked failed")
        try:
            save_notes(session, adapter)
        except Exception:
            # the notes are for a person; losing them must not fail the pass that read the FMS
            session.rollback()
            log.exception("could not save the notes on FMS answers")
        return checked, failures


class Worker:
    def __init__(self, factory: sessionmaker[Session], adapter: FMSAdapter, every_seconds: float, stale_after: float | None = None,
                 run_id: str = "local", feed_url: str | None = None, text_numbers: dict[str, str] | None = None):
        self.factory, self.adapter, self.every, self.run_id, self.feed_url = factory, adapter, every_seconds, run_id, feed_url
        self.text_numbers = text_numbers
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
                checked, failed = run_once(self.factory, self.adapter, self.run_id, self.feed_url, self.text_numbers)
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
    w = Worker(factory, fms, s.reconcile_every_seconds, text_numbers=s.text_numbers)
    w.start()
    while True:
        time.sleep(3600)
