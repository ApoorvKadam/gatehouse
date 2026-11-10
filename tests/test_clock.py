"""The desk's clock never reads earlier than a stamp it gave before, even if the system clock is set back."""
from datetime import datetime, timedelta

from desk import db


def test_a_system_clock_set_back_does_not_make_a_later_stamp_read_earlier(monkeypatch):
    real = db._system_now()
    readings = iter([real, real + timedelta(milliseconds=5), real - timedelta(milliseconds=600), real - timedelta(milliseconds=599), real + timedelta(seconds=2)])
    monkeypatch.setattr(db, "_system_now", lambda: next(readings))
    monkeypatch.setattr(db, "_last_stamp", datetime.min)
    stamps = [db.utcnow() for _ in range(5)]
    assert stamps == sorted(stamps) and len(set(stamps)) == 5          # strictly later each time
    assert stamps[3] - stamps[1] == timedelta(microseconds=2)          # held just after the last stamp while the clock was behind
    assert stamps[4] == real + timedelta(seconds=2)                    # and back on the system clock once it passes
