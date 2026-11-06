"""The reconciliation worker: re-reads the FMS for every unsettled payment and every open case on a timer.

It only reads. Submitting happens in one place (PaymentService.take_payment) and never from here."""
from __future__ import annotations

import logging
import threading

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

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.every + 5)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            try:
                checked = run_once(self.factory, self.adapter)
                if checked:
                    log.info("reconciled %d payment(s)", checked)
            except Exception:
                # a bad FMS read must not kill the loop; the next pass tries again
                log.exception("reconcile pass failed")


if __name__ == "__main__":
    import time

    from .adapter.cubby import CubbyAdapter
    from .config import Settings
    from .db import make_session_factory

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = Settings.from_env()
    w = Worker(make_session_factory(s.database_url), CubbyAdapter(s.fms_base_url, s.fms_api_key, s.fms_timeout_seconds),
               s.reconcile_every_seconds)
    w.start()
    while True:
        time.sleep(3600)
