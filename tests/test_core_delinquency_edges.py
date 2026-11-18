"""Edge cases in collections: money that arrives after a part's deadline, a long gap before the sweep, a return after a plan
completed or before its success was seen, a payment still in flight while reconciliation runs, a return billing cannot apply,
and the simulated processor across a restart."""
from datetime import date, timedelta

from core import delinquency as col
from core.books import at_local
from tests.test_core_delinquency import NOW, books, do, on_plan, plan, world


def paid_upfront(store, stream, t, now=NOW):
    do(store, stream, lambda led: (led.receive_payment(t, 3000, now), col.recheck(led, t, now)))


def test_money_that_arrives_after_the_grace_days_does_not_erase_a_missed_part():
    store, t, _ = world()
    pid, _ = on_plan(store, "visitor:a", t)
    paid_upfront(store, "visitor:a", t)
    late = at_local(date(2026, 4, 1), 10)                 # part 1 was due Mar 26 with three grace days; nothing ran meanwhile
    do(store, "visitor:a", lambda led: (led.receive_payment(t, 3000, late), col.recheck(led, t, late)))
    assert col.instalments(books(store, "visitor:a"), plan(store, "visitor:a", pid), late)[1]["state"] == "missed"
    col.sweep(store, "visitor:a", at_local(date(2026, 4, 2), 6))
    assert plan(store, "visitor:a", pid).state == "breached"
    assert [(k.kind, k.due) for k in col.open_tasks(books(store, "visitor:a"))] == [("plan breached", date(2026, 3, 30))]
    # money the processor confirmed inside the grace days counts, even when the news came later
    store2, t2, _ = world()
    pid2, _ = on_plan(store2, "visitor:a", t2)
    paid_upfront(store2, "visitor:a", t2)
    do(store2, "visitor:a", lambda led: (led.receive_payment(t2, 3000, at_local(date(2026, 3, 29), 15), late), col.recheck(led, t2, late)))
    col.sweep(store2, "visitor:a", at_local(date(2026, 4, 2), 6))
    assert plan(store2, "visitor:a", pid2).state == "active"


def test_a_catch_up_after_a_long_gap_notices_every_part_that_fell_due_and_breaches_once():
    store, t, _ = world()
    pid, _ = on_plan(store, "visitor:a", t)
    paid_upfront(store, "visitor:a", t)
    col.sweep(store, "visitor:a", at_local(date(2026, 4, 28), 6))         # parts due Mar 26, Apr 9 and Apr 23 all passed unseen
    st = col.state(books(store, "visitor:a"))
    assert set(st.notices) == {f"due:{pid}:1", f"due:{pid}:2", f"due:{pid}:3"} and st.plans[pid].state == "breached"
    assert [(k.kind, k.due) for k in col.open_tasks(books(store, "visitor:a"))] == [("plan breached", date(2026, 3, 30))]
    store2, t2, _ = world()
    pid2, _ = on_plan(store2, "visitor:a", t2)
    paid_upfront(store2, "visitor:a", t2)
    col.sweep(store2, "visitor:a", at_local(date(2026, 4, 1), 6))
    col.sweep(store2, "visitor:a", at_local(date(2026, 4, 10), 6))          # a breached plan's next part is still noticed
    assert f"due:{pid2}:2" in col.state(books(store2, "visitor:a")).notices
    assert len(col.open_tasks(books(store2, "visitor:a"))) == 1


def test_a_return_after_the_plan_completed_sends_it_back_to_staff():
    store, t, lease = world()
    pid, _ = on_plan(store, "visitor:a", t, n=1)
    paid_upfront(store, "visitor:a", t)
    at = at_local(date(2026, 3, 20), 10)
    last = do(store, "visitor:a", lambda led: led.receive_payment(t, 9000, at))
    do(store, "visitor:a", lambda led: col.recheck(led, t, at))
    assert plan(store, "visitor:a", pid).state == "completed"
    back = at_local(date(2026, 3, 24), 10)
    do(store, "visitor:a", lambda led: led.return_payment(back, last))
    col.sweep(store, "visitor:a", back)                                     # the sweep finds it even when nothing else rechecked
    b = books(store, "visitor:a")
    assert plan(store, "visitor:a", pid).state == "review" and [k.kind for k in col.open_tasks(b)] == ["plan needs staff"]
    assert col.step_state(b, lease, "overlock", back)[0] == "waiting for staff"


def test_a_return_reported_before_the_success_was_seen_settles_the_payment():
    for via in ("webhook", "reconcile"):
        store, t, _ = world()
        sim = col.SimulatedProcessor()
        sim.lose_next_reply = True
        i = col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW)        # it went through and we never heard
        sim.settle(sim.by_key[i.key], "returned", NOW + timedelta(days=2))     # then it came back
        if via == "webhook":
            assert col.webhook(store, sim, sim.outbox[-1], NOW + timedelta(days=2)) == "applied"
        else:
            col.reconcile(store, "visitor:a", sim, NOW + timedelta(days=2))
        b = books(store, "visitor:a")
        assert col.state(b).intents[i.id].state == "returned" and not col.unsettled(b, t), via
        assert not [e for e in store.stream_events("visitor:a") if e.kind in ("payment.received", "payment.returned")], via
        assert col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW + timedelta(days=2)).state == "succeeded", via


def test_reconciliation_never_closes_a_payment_being_sent_and_a_late_one_is_resolved():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    real = sim.create

    def slow(*a, **k):
        raise col.ProcessorTimeout("still on its way")
    sim.create = slow
    i = col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW)            # recorded; the request has not landed yet
    j = col.pay(store, "visitor:b", sim, t, 2000, "card_ok", NOW)
    sim.create = real
    col.reconcile(store, "visitor:a", sim, NOW + timedelta(minutes=1))
    assert col.state(books(store, "visitor:a")).intents[i.id].state == "open"
    real(i.key, 3000, "card_ok", {"copy": "visitor:a", "intent": i.id, "nonce": i.key.rsplit(":", 1)[1]}, NOW + timedelta(minutes=2))
    assert col.webhook(store, sim, sim.outbox[-1], NOW + timedelta(minutes=2)) == "applied"
    assert col.state(books(store, "visitor:a")).intents[i.id].state == "succeeded"
    col.reconcile(store, "visitor:b", sim, NOW + timedelta(hours=2))          # long after, nothing at the processor: closed
    assert col.state(books(store, "visitor:b")).intents[j.id].state == "closed"
    real(j.key, 2000, "card_ok", {"copy": "visitor:b", "intent": j.id, "nonce": j.key.rsplit(":", 1)[1]}, NOW + timedelta(hours=3))
    col.reconcile(store, "visitor:b", sim, NOW + timedelta(days=1))           # it turned up after all: resolved, not dropped
    b = books(store, "visitor:b")
    assert col.state(b).intents[j.id].state == "succeeded" and b.payments[col.state(b).intents[j.id].payment].cents == 2000


def test_a_return_billing_cannot_apply_stays_open_and_is_reported_every_day():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    i = col.pay(store, "visitor:a", sim, t, 12000, "card_ok", NOW)           # pays March's rent and its late fee
    fee = next(c for c in books(store, "visitor:a").tenant_charges(t) if c.kind == "late_fee" and c.due == date(2026, 3, 6))
    do(store, "visitor:a", lambda led: led.waive(NOW + timedelta(hours=1), fee.id, "goodwill", "staff:manager"))
    sim.settle(i.outside, "returned", NOW + timedelta(days=1))
    col.webhook(store, sim, sim.outbox[-1], NOW + timedelta(days=1))
    b = books(store, "visitor:a")
    assert col.state(b).intents[i.id].state == "succeeded" and not b.payments[i.payment].returned
    assert [k.kind for k in col.open_tasks(b)] == ["returned payment needs staff"]
    for d in (2, 3):
        r = col.reconcile(store, "visitor:a", sim, NOW + timedelta(days=d))
        assert r["mismatches"] == [f"payment {i.payment} is posted and the processor says returned"]
    assert len(col.open_tasks(books(store, "visitor:a"))) == 1


def test_the_simulated_processor_keeps_its_records_across_a_restart(tmp_path):
    path = str(tmp_path / "processor.json")
    store, t, _ = world()
    sim = col.SimulatedProcessor(path)
    done = col.pay(store, "visitor:a", sim, t, 1000, "card_ok", NOW)
    sim.lose_next_reply = True
    lost = col.pay(store, "visitor:b", sim, t, 2000, "card_ok", NOW)
    again = col.SimulatedProcessor(path)                                       # the service restarts
    assert col.send(store, "visitor:b", again, lost.id, "card_ok", NOW).state == "succeeded" and len(again.payments) == 2
    r = col.reconcile(store, "visitor:a", again, NOW + timedelta(days=1))
    assert r["mismatches"] == [] and col.state(books(store, "visitor:a")).intents[done.id].state == "succeeded"
    assert again.create("k", 100, "card_ok", {"copy": "visitor:c"}, NOW)["id"] == "sim_pay_00003"
    assert again.outbox[-1]["id"] == "sim_evt_00003"


def test_a_late_payment_that_clears_the_balance_still_records_the_missed_part():
    store, t, _ = world()
    pid, _ = on_plan(store, "visitor:a", t, n=1)                  # $30 now, then $90 due Mar 26 with three grace days
    paid_upfront(store, "visitor:a", t)
    late = at_local(date(2026, 4, 1), 10)                          # after the part's grace days, before any sweep ran
    do(store, "visitor:a", lambda led: (led.receive_payment(t, 9000, late), col.recheck(led, t, late)))
    assert plan(store, "visitor:a", pid).state == "breached"
    assert [(k.kind, k.due) for k in col.open_tasks(books(store, "visitor:a"))] == [("plan breached", date(2026, 3, 30))]
    col.sweep(store, "visitor:a", at_local(date(2026, 4, 2), 6))
    assert len(col.open_tasks(books(store, "visitor:a"))) == 1
