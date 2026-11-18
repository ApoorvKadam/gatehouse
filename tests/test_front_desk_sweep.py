"""The desk runs the collections sweep over every visitor copy (at boot it catches up, later passes write only what is new),
keeps the simulated processor's records beside the core's database, and closes a copy's payments still processing when the
copy is reset."""
import os
from datetime import date, timedelta

from core import delinquency
from core.billing import Ledger
from core.books import at_local
from desk.front_desk import FrontDesk

DAY = date(2026, 12, 3)


def clock():
    return at_local(DAY, 10)


def test_a_restarted_desk_catches_up_every_copy_and_a_second_pass_writes_nothing(tmp_path):
    path = str(tmp_path / "core.db")
    desk = FrontDesk(path, clock=clock)
    store = desk.ready()
    t = desk.cast["maria"]["tenant"]
    with store.write():                 # a promise whose call-back day passed while nothing ran
        led = Ledger(store.books("visitor:v1"))
        delinquency.promise(led, t, 5000, DAY - timedelta(days=3), "I'll pay Friday", at_local(DAY - timedelta(days=5)), "operator")
        store.append("visitor:v1", led.pending)
    again = FrontDesk(path, clock=clock)                       # the app restarts the same day
    assert again.sweep() == ["visitor:v1"]
    b = again.store.books("visitor:v1")
    assert [k.kind for k in delinquency.open_tasks(b) if k.tenant == t] == ["call back"] and DAY in delinquency.state(b).sweeps
    count = len(again.store.stream_events("visitor:v1"))
    again.sweep()
    assert len(again.store.stream_events("visitor:v1")) == count


def test_the_processor_keeps_its_records_beside_the_cores_database(tmp_path):
    desk = FrontDesk(str(tmp_path / "core.db"), clock=clock)
    assert desk.processor.path == os.path.join(str(tmp_path), "processor.json")
    store = desk.ready()
    delinquency.pay(store, "visitor:v3", desk.processor, desk.cast["maria"]["tenant"], 1000, "card_ok", clock())
    assert os.path.exists(desk.processor.path)
    assert FrontDesk(":memory:", clock=clock).processor.path is None


def test_a_reset_closes_the_copys_payments_still_processing():
    desk = FrontDesk(":memory:", clock=clock)
    store = desk.ready()
    t = desk.cast["maria"]["tenant"]
    mine = delinquency.pay(store, "visitor:v2", desk.processor, t, 1000, "card_pending", clock())
    other = delinquency.pay(store, "visitor:v4", desk.processor, t, 1000, "card_pending", clock())
    desk.reset("v2")
    assert desk.processor.payments[mine.outside]["status"] == "canceled"
    assert desk.processor.payments[other.outside]["status"] == "processing"
    assert not store.stream_events("visitor:v2")


def test_the_pass_recovers_lost_answers_delivers_processor_events_and_reconciles_once_a_day():
    desk = FrontDesk(":memory:", clock=clock)
    store = desk.ready()
    t = desk.cast["maria"]["tenant"]
    desk.processor.lose_next_reply = True
    lost = delinquency.pay(store, "visitor:v5", desk.processor, t, 1000, "card_ok", clock())        # charged; the answer never came
    pending = delinquency.pay(store, "visitor:v6", desk.processor, t, 2000, "card_pending", clock())
    desk.processor.settle(pending.outside, "succeeded", clock())                                    # its event waits at the processor
    assert desk.sweep() == ["visitor:v5", "visitor:v6"]
    for stream, i in (("visitor:v5", lost), ("visitor:v6", pending)):
        st = delinquency.state(store.books(stream))
        assert st.intents[i.id].state == "succeeded" and len(st.reconciled) == 1, stream
    assert delinquency.state(store.books("visitor:v6")).webhooks and desk.processor.outbox == []
    counts = {s: len(store.stream_events(s)) for s in ("visitor:v5", "visitor:v6")}
    desk.sweep()
    assert {s: len(store.stream_events(s)) for s in counts} == counts


def test_a_fault_in_one_copy_does_not_stop_the_copies_after_it(monkeypatch):
    desk = FrontDesk(":memory:", clock=clock)
    store = desk.ready()
    t = desk.cast["maria"]["tenant"]
    for s in ("visitor:a", "visitor:b"):
        with store.write():
            led = Ledger(store.books(s))
            delinquency.promise(led, t, 5000, DAY - timedelta(days=3), "I'll pay Friday", at_local(DAY - timedelta(days=5)), "operator")
            store.append(s, led.pending)
    real = delinquency.sweep

    def broken(st, stream, now):
        if stream == "visitor:a":
            raise RuntimeError("a fault in this copy")
        return real(st, stream, now)
    monkeypatch.setattr(delinquency, "sweep", broken)
    assert desk.sweep() == ["visitor:b"]
    assert [k.kind for k in delinquency.open_tasks(store.books("visitor:b")) if k.tenant == t] == ["call back"]
