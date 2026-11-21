"""The desk runs the collections sweep over every visitor copy (at boot it catches up, later passes write only what is new),
keeps the simulated processor's records beside the core's database, and closes a copy's payments still processing when the
copy is reset."""
import json
import os
import threading
from datetime import date, timedelta

import pytest

from core import delinquency, renting
from core.billing import Ledger
from core.books import at_local
from desk.front_desk import FrontDesk

from .test_core_renting_pay import desk_rental

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


def access_of(store, stream, rental):
    r = renting.load(store, stream)[1]
    return r.access[r.rentals[rental].access], [t for t in r.tasks.values() if t.open]


def test_a_rental_started_through_the_desk_gets_its_code_at_once_from_the_stand_in(tmp_path):
    path = str(tmp_path / "core.db")
    desk, store, stream, _, _, rn, _ = desk_rental("card_ok", True, desk=FrontDesk(path, clock=clock))
    a, tasks = access_of(store, stream, rn)
    assert a.state == "verified" and a.simulated and tasks == []                    # the record says the code is the stand-in's
    calls = list(desk.access.calls)
    assert [k.rsplit(":", 1)[1] for k in calls] == ["user", "unit_group", "level_group", "pin"]
    again = FrontDesk(path, clock=clock)                                           # the app restarts the same day
    assert again.sweep() == [stream] and again.access.calls == calls                # it found the codes it made and issued nothing
    assert again.access.read_back(a.user)["code"] and access_of(again.store, stream, rn)[1] == []


def test_the_pass_issues_a_missing_code_before_it_checks_and_reset_forgets_the_copys_codes():
    desk, store, stream, _, _, rn, _ = desk_rental("card_ok", True, through_desk=False)     # active, no code yet
    assert access_of(store, stream, rn)[0].state == "requested"
    assert desk.sweep() == [stream]
    a, tasks = access_of(store, stream, rn)
    assert a.state == "verified" and tasks == []                                    # advance ran first: no task for a code about to exist
    assert any(k.startswith(f"{stream}@") for k in desk.access.done) and desk.access.users
    desk.reset("v1")
    assert not any(k.startswith(f"{stream}@") for k in desk.access.done) and not desk.access.users


def test_a_fault_in_one_copys_renting_pass_stops_neither_the_check_nor_the_other_copies(monkeypatch):
    desk, store, a, _, _, rn_a, _ = desk_rental("card_ok", True, stream="visitor:a", through_desk=False)
    desk_, _, b, _, _, rn_b, _ = desk_rental("card_ok", True, desk=desk, stream="visitor:b", through_desk=False)
    real = renting.advance

    def broken(st, stream, system, now):
        if stream == "visitor:a":
            raise RuntimeError("the access system is down for this copy")
        return real(st, stream, system, now)
    monkeypatch.setattr(renting, "advance", broken)
    assert desk.sweep() == ["visitor:a", "visitor:b"]                               # the collections pass of both still ran
    assert access_of(store, b, rn_b)[0].state == "verified" and access_of(store, b, rn_b)[1] == []
    xa, tasks = access_of(store, a, rn_a)
    assert xa.state == "requested" and [(t.about, t.kind) for t in tasks] == [(xa.id, "access")]     # the check still ran for a
    monkeypatch.setattr(renting, "advance", real)
    desk.sweep()
    assert access_of(store, a, rn_a)[0].state == "verified" and access_of(store, a, rn_a)[1] == []
    counts = {s: len(store.stream_events(s)) for s in (a, b)}
    desk.sweep()
    assert {s: len(store.stream_events(s)) for s in counts} == counts                # a pass with nothing new writes nothing


def test_a_fault_in_the_stand_in_after_a_charge_never_undoes_the_start_and_the_next_pass_finishes_the_code(monkeypatch):
    def down(*args):
        raise RuntimeError("the access system is down")
    monkeypatch.setattr(renting, "advance", down)
    desk, store, stream, _, _, rn, intent = desk_rental("card_ok", True)           # the card was charged and the rental started
    assert intent.state == "succeeded" and renting.load(store, stream)[1].rentals[rn].state == "active"
    assert access_of(store, stream, rn)[0].state == "requested"
    monkeypatch.undo()
    desk.sweep()
    assert access_of(store, stream, rn)[0].state == "verified"


def test_cleaning_old_copies_removes_their_codes_from_the_stand_in(monkeypatch):
    from desk import front_desk
    desk, store, stream, _, _, rn, _ = desk_rental("card_ok", True)
    assert desk.access.users
    monkeypatch.setattr(front_desk, "KEEP_FOR", timedelta(days=-1))          # every copy counts as old
    desk.clean()
    assert not store.stream_events(stream) and not desk.access.users and not desk.access.done


def test_a_staff_retry_of_a_code_stays_closed_unless_the_read_back_fails_again():
    desk = FrontDesk(":memory:", clock=clock)
    desk.access.hide.add(renting.ACCESS_LEVEL)                                    # the access system will not show the second group
    desk, store, stream, _, _, rn, _ = desk_rental("card_ok", True, desk=desk)
    a, tasks = access_of(store, stream, rn)
    assert a.state == "failed" and [(t.about, t.kind) for t in tasks] == [(a.id, "access")]
    desk.sweep()
    assert len(access_of(store, stream, rn)[1]) == 1                                # the pass adds no second task for the same code
    retry = lambda n: renting.Act(f"retry-{n}", "retry_access", "staff:front_desk", a.id)
    assert desk.rent_step("v1", retry(1)).allowed                                  # still hidden: the read-back fails again
    x, tasks = access_of(store, stream, rn)
    assert x.state == "failed" and len(tasks) == 1
    desk.access.hide.clear()
    assert desk.rent_step("v1", retry(2)).allowed                                  # now it reads back: the task closes and stays closed
    x, tasks = access_of(store, stream, rn)
    assert x.state == "verified" and tasks == []
    desk.sweep()                                                                  # the daily check closes the work item that code had, once
    count = len(store.stream_events(stream))
    desk.sweep()
    desk.sweep()
    assert access_of(store, stream, rn)[1] == [] and len(store.stream_events(stream)) == count


def test_a_webhook_that_settles_a_rental_payment_starts_the_rental_and_its_code_without_waiting_for_the_pass():
    desk, store, stream, _, _, rn, intent = desk_rental("card_pending", accepted=True)
    assert renting.load(store, stream)[1].rentals[rn].state == "awaiting_payment"
    desk.processor.settle(intent.outside, "succeeded", clock())
    event = desk.processor.take_events()[-1]
    assert desk.processor_event(event) == "applied"                                # no sweep: the hook alone carries the rental forward
    a, tasks = access_of(store, stream, rn)
    assert renting.load(store, stream)[1].rentals[rn].state == "active" and a.state == "verified" and tasks == []
    count = len(store.stream_events(stream))
    assert desk.processor_event(event) == "duplicate" and len(store.stream_events(stream)) == count


def test_a_fault_after_a_webhook_was_recorded_does_not_undo_it_and_the_next_pass_finishes_the_rental(monkeypatch):
    desk, store, stream, _, _, rn, intent = desk_rental("card_pending", accepted=True)
    desk.processor.settle(intent.outside, "succeeded", clock())
    event = desk.processor.take_events()[-1]
    real = renting.settle_payments

    def down(*args):
        raise RuntimeError("the rental step failed")
    monkeypatch.setattr(renting, "settle_payments", down)
    assert desk.processor_event(event) == "applied"                                # the payment is recorded and the card processor is answered
    assert delinquency.state(store.books(stream)).intents[intent.id].state == "succeeded"
    assert renting.load(store, stream)[1].rentals[rn].state == "awaiting_payment"
    monkeypatch.setattr(renting, "settle_payments", real)
    desk.sweep()
    assert renting.load(store, stream)[1].rentals[rn].state == "active"


def test_a_copy_start_over_keeps_for_a_waiting_payment_keeps_its_codes_and_the_next_start_over_forgets_them(monkeypatch):
    desk, store, stream, tenant, _, rn, _ = desk_rental("card_ok", True)
    delinquency.pay(store, stream, desk.processor, tenant, 1000, "card_pending", clock())       # a payment still processing
    assert desk.access.users and any(k.startswith(f"{stream}@") for k in desk.access.done)
    real = desk.processor.retrieve

    def silent(outside):
        raise delinquency.ProcessorTimeout(outside)
    monkeypatch.setattr(desk.processor, "retrieve", silent)
    users, done = dict(desk.access.users), dict(desk.access.done)
    assert desk.reset("v1") is False and store.stream_events(stream)                # the card processor could not say: nothing is thrown away
    assert desk.access.users == users and desk.access.done == done                  # nor any code of the copy on the stand-in gate
    monkeypatch.setattr(desk.processor, "retrieve", real)
    assert desk.reset("v1") is True and not store.stream_events(stream)
    assert not desk.access.users and not any(k.startswith(f"{stream}@") for k in desk.access.done)


def test_cleaning_skips_a_copy_with_a_waiting_payment_and_its_codes_stay_until_it_is_cleaned(monkeypatch):
    from desk import front_desk
    desk, store, stream, tenant, _, rn, _ = desk_rental("card_ok", True)
    delinquency.pay(store, stream, desk.processor, tenant, 1000, "card_pending", clock())
    monkeypatch.setattr(front_desk, "KEEP_FOR", timedelta(days=-1))              # every copy counts as old
    users = dict(desk.access.users)
    desk.clean()
    assert store.stream_events(stream) and desk.access.users == users               # a payment waits at the processor: the copy and its codes stay
    waiting = next(p["id"] for p in desk.processor.payments.values() if p["status"] == "processing")
    desk.processor.settle(waiting, "succeeded", clock())
    desk.sweep()                                                                    # the pass records the payment: nothing waits now
    desk.clean()
    assert not store.stream_events(stream) and not desk.access.users and not desk.access.done


def lost_rental(monkeypatch):
    """The desk's copy with a rental whose checkout intent was written and whose request never went out. Returns the desk, its store,
    the copy, the clock the test moves, the rental and the intent."""
    now = [clock()]
    desk = FrontDesk(":memory:", clock=lambda: now[0])
    store, stream = desk.ready(), "visitor:v1"
    real = delinquency.send

    def stopped(*args, **kwargs):
        raise RuntimeError("the process stopped after the intent was written")
    monkeypatch.setattr(delinquency, "send", stopped)
    with pytest.raises(RuntimeError):
        desk_rental("card_ok", True, desk=desk)
    monkeypatch.setattr(delinquency, "send", real)
    r = renting.load(store, stream)[1]
    rn = next(k for k, x in r.rentals.items() if not x.provenance)          # the copy's own rental, not one the year starts with
    return desk, store, stream, now, rn, delinquency.state(store.books(stream)).intents[r.ops[r.rentals[rn].pay_ops[0]].intent]


def turns_up_after_the_hold(desk, store, stream, now, rn, intent):
    """The payment the processor had after all arrives half an hour on (the hold lasts twenty minutes), and the next pass finds it.
    No rental starts: it is cancelled, no lease or code is made, and the money is set aside for staff to refund, once."""
    leases = len(store.books(stream).leases)
    now[0] += timedelta(minutes=30)
    nonce = delinquency.state(store.books(stream)).nonce
    desk.processor.create(intent.key, intent.cents, "card_ok", {"copy": stream, "intent": intent.id, "nonce": nonce}, now[0])
    desk.sweep()
    r, b = renting.load(store, stream)[1], store.books(stream)
    assert r.rentals[rn].state == "cancelled" and r.rentals[rn].lease is None and not r.rentals[rn].access and len(b.leases) == leases
    assert b.refunds[intent.tenant] == intent.cents and [t.kind for t in r.tasks.values() if t.open and t.about == rn] == ["refund"]
    count = len(store.stream_events(stream))
    desk.sweep()
    assert len(store.stream_events(stream)) == count


def test_a_checkout_payment_closed_as_absent_by_a_settling_attempt_never_starts_a_rental_when_it_turns_up_later(monkeypatch):
    """Settling a copy's waiting payments outside the hourly pass (the day's roll over, Start over) closes an intent the processor has
    nothing for. The rental's wait is answered then, so a payment that turns up after the hold ran out is set aside for staff to
    refund and never starts a rental."""
    desk, store, stream, now, rn, intent = lost_rental(monkeypatch)
    now[0] += timedelta(minutes=16)
    assert desk._resolve(store, [stream])                                           # the processor has no payment for the key: closed as absent
    r = renting.load(store, stream)[1]
    assert delinquency.state(store.books(stream)).intents[intent.id].state == "closed" and not r.uncertain(r.rentals[rn])
    turns_up_after_the_hold(desk, store, stream, now, rn, intent)


def test_a_rental_step_that_faulted_on_the_pass_that_closed_the_intent_still_never_starts_a_rental_when_the_payment_turns_up(monkeypatch):
    """The pass closes the intent as absent, but the rental step fails that once (it is logged and left to the next pass) and the
    payment turns up before the next pass: the wait is answered as declined first, so the late success is set aside, not taken as
    the first answer."""
    desk, store, stream, now, rn, intent = lost_rental(monkeypatch)
    real = renting.settle_payments

    def down(*args):
        raise RuntimeError("the rental step failed once")
    monkeypatch.setattr(renting, "settle_payments", down)
    now[0] += timedelta(minutes=16)
    desk.sweep()                                                                    # the intent is closed; renting is not told
    assert delinquency.state(store.books(stream)).intents[intent.id].state == "closed"
    assert renting.load(store, stream)[1].uncertain(renting.load(store, stream)[1].rentals[rn])
    monkeypatch.setattr(renting, "settle_payments", real)
    turns_up_after_the_hold(desk, store, stream, now, rn, intent)


# the gate codes of a copy that goes are forgotten durably: a note is written before the copy is dropped and cleared after -------------
def access_file(tmp_path):
    return json.loads((tmp_path / "access.json").read_text())


def failing_after(first_saves, desk, monkeypatch):
    """The stand-in's file saves fail from the save after the first `first_saves` ones, as a full disk would."""
    real, saves = desk.access._save, []

    def save():
        saves.append(1)
        if len(saves) > first_saves:
            raise OSError("no space left on the device")
        real()
    monkeypatch.setattr(desk.access, "_save", save)


def test_a_copy_dropped_while_the_gate_cleanup_fails_leaves_a_note_and_the_next_start_finishes_it(tmp_path, monkeypatch):
    path = str(tmp_path / "core.db")
    desk, store, stream, *_ = desk_rental("card_ok", True, desk=FrontDesk(path, clock=clock))
    failing_after(1, desk, monkeypatch)                                         # the note is written, the forgetting is not
    assert desk.reset("v1") is True and not store.stream_events(stream)         # the copy is gone and the page is not taken down
    left = access_file(tmp_path)
    assert left["users"] and left["pending"]                                    # its codes and the note are still on file
    monkeypatch.undo()
    again = FrontDesk(path, clock=clock)
    again.ready()
    assert not again.access.users and not again.access.pending and not access_file(tmp_path)["users"] and not access_file(tmp_path)["pending"]


def test_a_copy_is_not_dropped_when_the_note_for_its_codes_cannot_be_written(tmp_path, monkeypatch):
    desk, store, stream, *_ = desk_rental("card_ok", True, desk=FrontDesk(str(tmp_path / "core.db"), clock=clock))
    desk._proposals["v1"] = ("pid",)                                            # a rule proposal in flight for this visitor
    failing_after(0, desk, monkeypatch)
    with pytest.raises(OSError):
        desk.reset("v1")
    assert store.stream_events(stream) and desk.access.users                    # nothing was thrown away
    assert desk._proposals["v1"] == ("pid",) and desk._resets.get("v1", 0) == 0     # and nothing said the copy was started over


def test_a_copy_whose_drop_failed_keeps_its_codes_and_the_note_is_cleared_at_the_next_start(tmp_path, monkeypatch):
    path = str(tmp_path / "core.db")
    desk, store, stream, *_ = desk_rental("card_ok", True, desk=FrontDesk(path, clock=clock))

    def broken(*args):
        raise RuntimeError("the database could not drop the copy")
    monkeypatch.setattr(store, "drop_stream", broken)
    with pytest.raises(RuntimeError):
        desk.reset("v1")
    assert desk.access.pending and desk.access.users                           # the note was written before the drop
    monkeypatch.undo()
    again = FrontDesk(path, clock=clock)
    again.ready()
    assert again.store.stream_events(stream) and again.access.users and not again.access.pending      # the copy is alive: its codes stay


def test_cleaning_that_cannot_forget_a_dropped_copys_codes_finishes_in_the_next_pass(tmp_path, monkeypatch):
    from desk import front_desk
    desk, store, stream, *_ = desk_rental("card_ok", True, desk=FrontDesk(str(tmp_path / "core.db"), clock=clock))
    monkeypatch.setattr(front_desk, "KEEP_FOR", timedelta(days=-1))
    failing_after(1, desk, monkeypatch)
    desk.clean()
    assert not store.stream_events(stream) and access_file(tmp_path)["users"]
    monkeypatch.undo()
    desk.sweep()
    assert not access_file(tmp_path)["users"] and not access_file(tmp_path)["pending"]


def test_the_codes_of_copies_in_a_day_file_that_is_deleted_go_with_it(tmp_path):
    now = [clock()]
    desk = FrontDesk(str(tmp_path / "core.db"), clock=lambda: now[0])
    desk_rental("card_ok", True, desk=desk)
    assert desk.access.users
    now[0] += timedelta(days=1)
    desk.ready()                                                                # yesterday's file is kept, so are its copies' codes
    assert desk.access.users and any(tmp_path.glob("core-2026-12-03.db"))
    now[0] += timedelta(days=2)
    desk.ready()                                                                # now that file is deleted
    assert not any(tmp_path.glob("core-2026-12-03.db*")) and not desk.access.users and not desk.access.done
    assert not access_file(tmp_path)["users"] and not access_file(tmp_path)["pending"]


class SlowPin(renting.StandIn):
    """A stand-in whose PIN issuance waits, inside the call, until the test lets it go."""

    def __init__(self):
        super().__init__()
        self.inside, self.go = threading.Event(), threading.Event()

    def issue_pin(self, key, user):
        self.inside.set()
        assert self.go.wait(10)
        return super().issue_pin(key, user)


def test_a_move_out_cannot_overtake_a_code_issuance_in_flight_so_the_code_is_issued_and_then_revoked():
    """Every pass that drives the gate codes (the hourly one, a staff command, a payment, a webhook) holds the desk's one lock, and the
    app runs one process, so two passes never overlap: a move-out asked for while a PIN is being issued waits, and revokes the code that
    was issued, never one that lands after the revocation."""
    desk = FrontDesk(":memory:", clock=clock)
    desk.access = SlowPin()
    desk, store, stream, _, _, rn, _ = desk_rental("card_ok", True, desk=desk, through_desk=False)       # active, no code yet
    sweeper = threading.Thread(target=desk.sweep)
    sweeper.start()
    assert desk.access.inside.wait(10)                                      # the pass is inside the PIN call
    done = {}

    def move_out():
        asked = desk.rent_step("v1", renting.Act("mo1", "request_move_out", "staff:manager", rn))
        desk.rent_step("v1", renting.Act("mo2", "review_move_out", "staff:manager", asked.made))
        done["bill"] = desk.rent_step("v1", renting.Act("mo3", "final_bill", "staff:manager", asked.made))
    mover = threading.Thread(target=move_out)
    mover.start()
    mover.join(1)
    assert mover.is_alive() and not done                                    # it waits for the pass in flight
    desk.access.go.set()
    sweeper.join(10)
    mover.join(10)
    assert done["bill"].allowed
    r = renting.load(store, stream)[1]
    a = r.access[r.rentals[rn].access]
    assert a.state == "revoked" and desk.access.users[a.user]["code"] is None and not desk.access.users[a.user]["groups"]
    assert r.status(r.rentals[rn].unit, clock()) == "vacant"


def test_an_old_day_file_that_cannot_be_read_is_left_for_a_later_attempt(tmp_path):
    bad = tmp_path / "core-2026-11-20.db"
    bad.write_bytes(b"this is not a database file" * 10)
    desk = FrontDesk(str(tmp_path / "core.db"), clock=clock)
    desk.ready()                                                                # the new day's seed would delete it
    assert bad.exists() and not desk.access.pending                             # its copies cannot be named, so it is not deleted


def test_a_note_for_a_copy_still_alive_in_yesterdays_file_is_cleared_with_its_codes_kept(tmp_path):
    now = [clock()]
    desk = FrontDesk(str(tmp_path / "core.db"), clock=lambda: now[0])
    desk_rental("card_ok", True, desk=desk)
    now[0] += timedelta(days=1)
    desk.ready()                                                                # a new day's file; yesterday's is kept with its copy
    (mark,) = renting.copy_marks_in(str(tmp_path / "core-2026-12-03.db"))
    desk.access.note(mark + ":")
    desk._finish_cleanups()
    assert desk.access.users and not desk.access.pending                        # the copy is alive there: its codes stay


def test_a_note_stays_while_a_retained_day_file_cannot_be_checked(tmp_path):
    (tmp_path / "core-2026-12-03.db").write_bytes(b"this is not a database file" * 10)      # yesterday's file, unreadable
    now = [clock() + timedelta(days=1)]
    desk = FrontDesk(str(tmp_path / "core.db"), clock=lambda: now[0])
    desk.ready()
    desk.access.create_user("visitor:v1@deadbeef:AC0001:user", "Rae Quinn")
    desk.access.note("visitor:v1@deadbeef:")
    desk._finish_cleanups()
    assert desk.access.users and desk.access.pending == ["visitor:v1@deadbeef:"]      # not found, but not everywhere checked: nothing forgotten
