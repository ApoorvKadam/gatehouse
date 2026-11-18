"""Collections on a small facility: the five records and their states, offers computed by code inside the owner's policy, money
counted only when the processor confirms it, the simulated processor's idempotency and webhooks, and the sweep."""
import random
from datetime import date, timedelta

import pytest

from core import delinquency as col
from core.billing import Ledger, Refused
from core.books import LATE_FEE, at_local, fold
from core.store import Store

POLICY = {"min_tenure_days": 30, "max_debt_cents": 100_000, "longest_days": 60, "most_instalments": 4, "min_upfront_cents": 3000,
          "min_instalment_cents": 2000, "grace_days": 3}
NOW = at_local(date(2026, 3, 12), 9)


def world(unpaid="2026-03", path=":memory:", policy=POLICY, schedule=(10, 45)):
    """One tenant on time until `unpaid`, whose card then declined: that month's rent and its late fee are owed."""
    led = Ledger()
    y, m = map(int, unpaid.split("-"))
    start = date(y, m, 1) - timedelta(days=70)
    led.set_up(at_local(start), "Test", "Orlando, FL", ["A1", "A2", "B1"])
    if policy:
        col.adopt_policy(led, at_local(start, 8), 1, policy)
    if schedule:
        col.adopt_schedule(led, at_local(start, 8), 1, *schedule)
    t = led.add_tenant(at_local(start, 9), "Ann Reyes", "+15550001")
    lease = led.sign_lease(at_local(start + timedelta(days=4), 10), t, "A1", 10000, [LATE_FEE])
    led.receive_payment(t, sum(c.open_cents for c in led.books.open_charges(t)), at_local(start + timedelta(days=4), 11))
    for month in sorted({f"{d:%Y-%m}" for d in (start + timedelta(days=k) for k in range(31, 75))}):
        led.post_rent(month)
        if month < unpaid:
            led.receive_payment(t, 10000, at_local(date(*map(int, month.split("-")), 2), 9))
        elif month == unpaid:
            led.decline(at_local(date(y, m, 2), 9), t, 10000)
            led.assess_late_fees(month)
            break
    store = Store(path)
    with store.write():
        store.append("base", led.pending)
    return store, t, lease


def do(store, stream, fn):
    with store.write():
        led = Ledger(store.books(stream))
        out = fn(led)
        store.append(stream, led.pending)
    return out


def books(store, stream):
    return store.books(stream)


def on_plan(store, stream, t, n=3, upfront=None, now=NOW):
    """Propose, offer n instalments, accept in the tenant's words; returns (plan id, offer)."""
    pid = do(store, stream, lambda led: col.propose(led, t, now, "operator"))
    o = do(store, stream, lambda led: col.put_offer(led, pid, n, upfront, now, "operator"))
    do(store, stream, lambda led: col.accept(led, pid, o["version"], "Yes, that plan works for me", now, "operator"))
    return pid, o


def plan(store, stream, pid):
    return col.state(books(store, stream)).plans[pid]


# the owner's settings and the fixed limits ---------------------------------------------------------------------------
def test_the_policy_and_schedule_are_bounded_typed_forms():
    led = Ledger()
    at = at_local(date(2026, 1, 1))
    with pytest.raises(Refused, match="outside the fixed limits"):
        col.adopt_policy(led, at, 1, {**POLICY, "most_instalments": 7})
    with pytest.raises(Refused, match="outside the fixed limits"):
        col.adopt_policy(led, at, 1, {**POLICY, "grace_days": True})
    with pytest.raises(Refused, match="exactly these fields"):
        col.adopt_policy(led, at, 1, {**POLICY, "waive_interest": 1})
    with pytest.raises(Refused, match="only the owner"):
        col.adopt_policy(led, at, 1, POLICY, by="staff:manager")
    with pytest.raises(Refused, match="outside the fixed limits"):
        col.adopt_schedule(led, at, 1, 0, 45)
    col.adopt_policy(led, at, 1, POLICY)
    with pytest.raises(Refused, match="already at that version"):
        col.adopt_policy(led, at, 1, POLICY)
    col.adopt_schedule(led, at, 1, 10, 45)
    assert col.state(led.books).policy["version"] == 1 and col.state(led.books).schedule["overlock_days"] == 10


# offers ---------------------------------------------------------------------------------------------------------------
def test_offers_are_exact_and_inside_the_policy():
    store, t, _ = world()
    b = books(store, "base")
    os = col.offers(b, t, NOW)
    assert [o["instalments"] for o in os] == [1, 2, 3, 4]
    for o in os:
        assert sum(p["cents"] for p in o["parts"]) == o["total"] == 12000
        assert o["upfront"] == 3000 and o["parts"][0] == {"part": 0, "cents": 3000, "due": "2026-03-12"}
        later = o["parts"][1:]
        assert all(p["cents"] >= POLICY["min_instalment_cents"] for p in later)
        assert [date.fromisoformat(p["due"]) for p in later] == [date(2026, 3, 12) + timedelta(days=14 * k) for k in range(1, len(later) + 1)]
    assert os[2]["parts"][1:] == [{"part": 1, "cents": 3000, "due": "2026-03-26"}, {"part": 2, "cents": 3000, "due": "2026-04-09"},
                                  {"part": 3, "cents": 3000, "due": "2026-04-23"}]
    assert len({o["version"] for o in os}) == 4


def test_random_policies_never_give_an_offer_outside_them_or_the_fixed_limits():
    rnd = random.Random(7)
    store, t, _ = world(policy=None)
    for k in range(150):
        pol = {key: rnd.randint(lo, min(hi, 20_000)) for key, (lo, hi) in col.POLICY_BOUNDS.items()}
        pol["min_tenure_days"] = rnd.randint(0, 40)
        stream = f"visitor:p{k}"
        do(store, stream, lambda led: col.adopt_policy(led, NOW, 1, pol))
        b = books(store, stream)
        for o in col.offers(b, t, NOW):
            later = o["parts"][1:] if o["upfront"] else o["parts"]
            assert sum(p["cents"] for p in o["parts"]) == o["total"] == 12000
            assert 1 <= len(later) <= pol["most_instalments"] <= col.POLICY_BOUNDS["most_instalments"][1]
            assert (date.fromisoformat(later[-1]["due"]) - NOW.date()).days <= pol["longest_days"] <= 180
            assert o["upfront"] == pol["min_upfront_cents"] and min(p["cents"] for p in later) >= pol["min_instalment_cents"]
        store.drop_stream(stream)


def test_a_counter_offer_is_answered_by_code():
    store, t, _ = world()
    b = books(store, "base")
    ok = col.counter(b, t, {"instalments": 2, "upfront_cents": 4000}, NOW)
    assert ok["answer"] == "offer" and ok["offer"]["parts"][1:] == [{"part": 1, "cents": 4000, "due": "2026-03-26"},
                                                                   {"part": 2, "cents": 4000, "due": "2026-04-09"}]
    far = col.counter(b, t, {"instalments": 6}, NOW)
    assert far["answer"] == "refused" and "1 to 4 instalments" in far["why"] and len(far["alternatives"]) == 4
    assert far["text"].startswith("That plan is not possible") and "$3,000.00" not in far["text"] and "$30.00 today" in far["text"]
    small = col.counter(b, t, {"instalments": 2, "upfront_cents": 1000}, NOW)
    assert small["answer"] == "refused" and "at least $30.00" in small["why"]
    assert col.counter(b, t, {"instalments": 2, "total_cents": 9000}, NOW) == {"answer": "staff", "why": "only staff can change what is owed"}
    assert col.counter(b, t, {"upfront_cents": 5000}, NOW)["answer"] == "choose"
    young = world(policy={**POLICY, "min_tenure_days": 365})
    assert col.counter(books(young[0], "base"), young[1], {"instalments": 2}, NOW)["answer"] == "staff"


# the plan ---------------------------------------------------------------------------------------------------------------
def test_a_plan_moves_only_along_its_transitions():
    store, t, _ = world()
    pid = do(store, "visitor:a", lambda led: col.propose(led, t, NOW, "operator"))
    assert plan(store, "visitor:a", pid).state == "proposed"
    with pytest.raises(Refused, match="put to them"):
        do(store, "visitor:a", lambda led: col.accept(led, pid, "nope", "yes", NOW, "operator"))
    o = do(store, "visitor:a", lambda led: col.put_offer(led, pid, 2, None, NOW, "operator"))
    with pytest.raises(Refused, match="own words"):
        do(store, "visitor:a", lambda led: col.accept(led, pid, o["version"], "  ", NOW, "operator"))
    with pytest.raises(Refused, match="cannot become"):
        do(store, "visitor:a", lambda led: col._move(led, plan(store, "visitor:a", pid), "plan.activated", "active", NOW, {}, "x"))
    do(store, "visitor:a", lambda led: col.accept(led, pid, o["version"], "ok, two payments", NOW, "operator"))
    p = plan(store, "visitor:a", pid)
    assert p.state == "accepted" and p.consent == {"version": o["version"], "quote": "ok, two payments", "by": "operator"}
    assert p.terms == o and p.policy_version == 1
    with pytest.raises(Refused, match="cannot become"):
        do(store, "visitor:a", lambda led: col._move(led, p, "plan.completed", "completed", NOW, {}, "x"))
    assert [s for s, _ in p.history] == ["proposed", "offered", "accepted"]


def test_consent_names_the_exact_offer_and_a_changed_balance_makes_it_stale():
    store, t, _ = world()
    pid = do(store, "visitor:a", lambda led: col.propose(led, t, NOW, "operator"))
    o = do(store, "visitor:a", lambda led: col.put_offer(led, pid, 3, None, NOW, "operator"))
    other = col.offer(books(store, "visitor:a"), t, NOW, 2)[0]
    with pytest.raises(Refused, match="put to them"):
        do(store, "visitor:a", lambda led: col.accept(led, pid, other["version"], "yes", NOW, "operator"))
    with pytest.raises(Refused, match="changed since"):              # the next day the dates move
        do(store, "visitor:a", lambda led: col.accept(led, pid, o["version"], "yes", NOW + timedelta(days=1), "operator"))
    do(store, "visitor:a", lambda led: led.receive_payment(t, 500, NOW))   # the balance moves
    with pytest.raises(Refused, match="changed since"):
        do(store, "visitor:a", lambda led: col.accept(led, pid, o["version"], "yes", NOW, "operator"))


def test_upfront_paid_makes_the_plan_active_and_every_part_paid_completes_it():
    store, t, _ = world()
    pid, o = on_plan(store, "visitor:a", t)
    assert plan(store, "visitor:a", pid).state == "accepted"
    do(store, "visitor:a", lambda led: (led.receive_payment(t, 3000, NOW), col.recheck(led, t, NOW)))
    assert plan(store, "visitor:a", pid).state == "active"
    for p in o["parts"][1:]:
        at = at_local(date.fromisoformat(p["due"]), 10)
        do(store, "visitor:a", lambda led, at=at, c=p["cents"]: (led.receive_payment(t, c, at), col.recheck(led, t, at)))
    assert plan(store, "visitor:a", pid).state == "completed"
    assert [s for s, _ in plan(store, "visitor:a", pid).history][-2:] == ["active", "completed"]


def test_no_upfront_part_makes_the_plan_active_on_consent():
    store, t, _ = world(policy={**POLICY, "min_upfront_cents": 0})
    pid, o = on_plan(store, "visitor:a", t, n=2)
    assert o["upfront"] == 0 and [p["part"] for p in o["parts"]] == [1, 2]
    assert plan(store, "visitor:a", pid).state == "active"


def test_instalments_count_only_confirmed_money_in_order():
    store, t, _ = world()
    pid, o = on_plan(store, "visitor:a", t)
    sim = col.SimulatedProcessor()
    states = lambda now: [x["state"] for x in col.instalments(books(store, "visitor:a"), plan(store, "visitor:a", pid), now)]
    assert states(NOW) == ["due", "scheduled", "scheduled", "scheduled"]
    col.pay(store, "visitor:a", sim, t, 3000, "card_pending", NOW)          # waiting for the processor: nothing counts
    assert states(NOW) == ["uncertain", "scheduled", "scheduled", "scheduled"] and plan(store, "visitor:a", pid).state == "accepted"
    sim.settle(sim.outbox[0]["payment"], "succeeded", NOW)
    assert col.webhook(store, sim, sim.outbox[-1], NOW) == "applied"
    assert states(NOW) == ["paid", "scheduled", "scheduled", "scheduled"] and plan(store, "visitor:a", pid).state == "active"
    col.pay(store, "visitor:a", sim, t, 1000, "card_ok", NOW)                # a partial payment
    assert states(NOW) == ["paid", "partly paid", "scheduled", "scheduled"]
    assert states(at_local(date(2026, 3, 27), 9)) == ["paid", "partly paid", "scheduled", "scheduled"]   # inside the grace days
    assert states(at_local(date(2026, 3, 30), 0, 1)) == ["paid", "missed", "scheduled", "scheduled"]
    col.pay(store, "visitor:a", sim, t, 5000, "card_ok", NOW)                # money fills the parts in order
    assert states(NOW) == ["paid", "paid", "paid", "scheduled"]


@pytest.mark.parametrize("unpaid,accept", [("2026-02", date(2026, 2, 26)), ("2026-10", date(2026, 10, 20))])
def test_due_dates_and_grace_are_calendar_days_across_the_clock_change(unpaid, accept):
    store, t, _ = world(unpaid=unpaid)
    now = at_local(accept, 9)
    pid, o = on_plan(store, "visitor:a", t, n=2, now=now)
    do(store, "visitor:a", lambda led: (led.receive_payment(t, o["upfront"], now), col.recheck(led, t, now)))
    due = date.fromisoformat(o["parts"][1]["due"])
    assert due == accept + timedelta(days=14)            # the clock change (Mar 8, Nov 1) sits inside these two weeks
    last = due + timedelta(days=POLICY["grace_days"])
    p = plan(store, "visitor:a", pid)
    assert col.instalments(books(store, "visitor:a"), p, at_local(last, 23, 59))[1]["state"] == "due"
    assert col.instalments(books(store, "visitor:a"), p, at_local(last + timedelta(days=1), 0, 0))[1]["state"] == "missed"


def test_a_returned_payment_reopens_what_it_paid_and_sends_the_plan_to_staff():
    store, t, _ = world()
    pid, o = on_plan(store, "visitor:a", t)
    sim = col.SimulatedProcessor()
    up = col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW)
    second = col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW + timedelta(hours=1))
    assert [x["state"] for x in col.instalments(books(store, "visitor:a"), plan(store, "visitor:a", pid), NOW)][:2] == ["paid", "paid"]
    sim.settle(second.outside, "returned", NOW + timedelta(days=2))
    col.webhook(store, sim, sim.outbox[-1], NOW + timedelta(days=2))
    b = books(store, "visitor:a")
    p = plan(store, "visitor:a", pid)
    assert b.payments[second.payment].returned and not b.payments[up.payment].returned
    assert [x["state"] for x in col.instalments(b, p, NOW)][:2] == ["paid", "scheduled"]
    assert p.state == "review" and [k.kind for k in col.open_tasks(b)] == ["plan needs staff"]
    assert col.step_state(b, p.covered[0]["lease"], "overlock", NOW + timedelta(days=2))[0] == "waiting for staff"


def test_a_payment_returned_from_before_the_plan_also_goes_to_staff():
    store, t, _ = world()
    pay = do(store, "visitor:a", lambda led: led.receive_payment(t, 500, NOW - timedelta(days=1)))
    pid, o = on_plan(store, "visitor:a", t)
    do(store, "visitor:a", lambda led: (led.receive_payment(t, 3000, NOW), col.recheck(led, t, NOW)))
    assert plan(store, "visitor:a", pid).state == "active"
    do(store, "visitor:a", lambda led: (led.return_payment(NOW + timedelta(hours=2), pay), col.recheck(led, t, NOW + timedelta(hours=2))))
    assert plan(store, "visitor:a", pid).state == "review"


def test_promises_bring_a_call_back_and_hold_nothing():
    store, t, lease = world()
    by = date(2026, 3, 15)
    do(store, "visitor:a", lambda led: col.promise(led, t, 5000, by, "I'll pay $50 Friday", NOW, "operator"))
    first = next(iter(col.state(books(store, "visitor:a")).promises.values()))
    assert first.callback == date(2026, 3, 16)
    second = do(store, "visitor:a", lambda led: col.promise(led, t, 6000, by, "make it $60", NOW, "operator"))
    st = col.state(books(store, "visitor:a"))
    assert st.promises[first.id].state == "replaced" and st.promises[second].state == "open"
    assert col.step_state(books(store, "visitor:a"), lease, "overlock", NOW)[0] == "eligible"
    col.sweep(store, "visitor:a", at_local(date(2026, 3, 16), 7))
    b = books(store, "visitor:a")
    assert col.state(b).promises[second].state == "missed"
    assert [(k.kind, k.due) for k in col.open_tasks(b)] == [("call back", date(2026, 3, 16))]
    do(store, "visitor:b", lambda led: col.promise(led, t, 5000, by, None, NOW, "operator"))
    do(store, "visitor:b", lambda led: led.receive_payment(t, 5000, at_local(date(2026, 3, 14), 12)))
    col.sweep(store, "visitor:b", at_local(date(2026, 3, 16), 7))
    assert [p.state for p in col.state(books(store, "visitor:b")).promises.values()] == ["kept"]


def test_an_exemption_ends_on_its_own_date_even_if_no_sweep_ran():
    store, t, lease = world()
    led = Ledger(books(store, "visitor:a"))
    led.emit("exemption.granted", NOW, {"exemption": "EX0001", "lease": lease, "tenant": t, "scope": ["overlock"], "reason": "in hospital",
                                        "owner": "manager", "until": "2026-03-20"}, "exemption:EX0001")
    b = led.books
    e = col.state(b).exemptions["EX0001"]
    assert col.exemption_state(e, date(2026, 3, 20)) == "active" and col.exemption_state(e, date(2026, 3, 21)) == "expired"
    assert col.step_state(b, lease, "overlock", at_local(date(2026, 3, 20), 23))[0] == "blocked"
    assert col.step_state(b, lease, "overlock", at_local(date(2026, 3, 21), 0, 5))[0] == "eligible"
    led.emit("exemption.revoked", NOW, {"exemption": "EX0001", "by": "staff:manager"}, "exemption:EX0001:revoked")
    assert col.exemption_state(e, date(2026, 3, 15)) == "revoked"


# the processor ------------------------------------------------------------------------------------------------------------
def test_every_payment_carries_its_copy_and_the_processor_id():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    a = col.pay(store, "visitor:a", sim, t, 2000, "card_ok", NOW)
    b = col.pay(store, "visitor:b", sim, t, 2500, "card_ok", NOW)
    for stream, i in (("visitor:a", a), ("visitor:b", b)):
        posted = [e.payload for e in store.stream_events(stream) if e.kind == "payment.received"]
        assert len(posted) == 1 and posted[0]["copy"] == stream and posted[0]["processor_payment"] == i.outside and posted[0]["simulated"]
        assert i.state == "succeeded" and i.copy == stream and i.key.startswith(f"{stream}:{i.id}:")
    assert a.outside != b.outside and a.key != b.key
    assert all(p["metadata"]["copy"] in ("visitor:a", "visitor:b") and p["simulated"] for p in sim.payments.values())


def test_an_unknown_outcome_is_never_paid_again():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    sim.lose_next_reply = True
    i = col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW)
    assert i.state == "open" and i.outside is None and len(sim.payments) == 1      # it reached the processor; the answer was lost
    created = [e for e in store.stream_events("visitor:a") if e.kind == "intent.created"]
    assert len(created) == 1                                                        # written before the request went out
    with pytest.raises(Refused, match="never paid again"):
        col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW)
    again = col.send(store, "visitor:a", sim, i.id, "card_ok", NOW)                  # the same key: the same payment back
    assert again.state == "succeeded" and len(sim.payments) == 1
    assert len([e for e in store.stream_events("visitor:a") if e.kind == "payment.received"]) == 1
    col.webhook(store, sim, sim.outbox[0], NOW)
    assert len([e for e in store.stream_events("visitor:a") if e.kind == "payment.received"]) == 1


def test_duplicate_and_out_of_order_webhooks_change_nothing_twice():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    sim.lose_next_reply = True
    i = col.pay(store, "visitor:a", sim, t, 3000, "card_declined", NOW)        # failed at the processor; we did not hear
    failed = sim.outbox[-1]
    sim.settle(i.key and sim.by_key[i.key], "succeeded", NOW + timedelta(minutes=5))   # a second attempt on it went through
    succeeded = sim.outbox[-1]
    assert col.webhook(store, sim, succeeded, NOW + timedelta(minutes=6)) == "applied"
    assert col.webhook(store, sim, succeeded, NOW + timedelta(minutes=6)) == "duplicate"
    assert col.webhook(store, sim, failed, NOW + timedelta(minutes=7)) == "applied"     # late, and the processor says succeeded
    b = books(store, "visitor:a")
    assert col.state(b).intents[i.id].state == "succeeded"
    assert len([e for e in store.stream_events("visitor:a") if e.kind == "payment.received"]) == 1
    assert not [e for e in store.stream_events("visitor:a") if e.kind == "payment.declined"]


def test_a_late_failure_never_undoes_a_success_but_a_return_does():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    i = col.pay(store, "visitor:a", sim, t, 3000, "card_declined", NOW)
    assert i.state == "failed" and [d["tenant"] for d in books(store, "visitor:a").declined][-1] == t
    sim.settle(i.outside, "succeeded", NOW + timedelta(minutes=1))
    col.webhook(store, sim, sim.outbox[-1], NOW + timedelta(minutes=1))
    late_fail = dict(sim.outbox[0], id="sim_evt_late")
    col.webhook(store, sim, late_fail, NOW + timedelta(minutes=2))
    b = books(store, "visitor:a")
    i = col.state(b).intents[i.id]
    assert i.state == "succeeded" and not b.payments[i.payment].returned
    sim.settle(i.outside, "returned", NOW + timedelta(days=3))
    col.webhook(store, sim, sim.outbox[-1], NOW + timedelta(days=3))
    b = books(store, "visitor:a")
    assert col.state(b).intents[i.id].state == "returned" and b.payments[i.payment].returned


def test_reconciliation_runs_per_copy():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    sim.lose_next_reply = True
    lost = col.pay(store, "visitor:a", sim, t, 1000, "card_ok", NOW)                 # reached it, answer lost
    done = col.send(store, "visitor:b", sim, col.pay(store, "visitor:b", sim, t, 1500, "card_ok", NOW).id, "card_ok", NOW)
    sim.settle(done.outside, "returned", NOW + timedelta(hours=1))                     # its webhook never arrives
    real_create = sim.create

    def unreachable(*a, **k):
        raise col.ProcessorTimeout("down")
    sim.create = unreachable
    lost_b = col.pay(store, "visitor:c", sim, t, 1200, "card_ok", NOW)                 # never reached it
    sim.create = real_create
    mark = col.state(books(store, "visitor:a")).nonce
    sim.create(f"visitor:a:PI0099:{mark}", 700, "card_ok", {"copy": "visitor:a", "nonce": mark}, NOW)   # copy a's mark, no intent there
    sim.create("visitor:a:PI0001:oldmark", 900, "card_ok", {"copy": "visitor:a", "nonce": "oldmark"}, NOW)  # a reset copy's: not ours
    ra = col.reconcile(store, "visitor:a", sim, NOW + timedelta(days=1))
    rb = col.reconcile(store, "visitor:b", sim, NOW + timedelta(days=1))
    rc = col.reconcile(store, "visitor:c", sim, NOW + timedelta(days=1))
    assert col.state(books(store, "visitor:a")).intents[lost.id].state == "succeeded" and ra["settled"] == [lost.id]
    assert len(ra["mismatches"]) == 1 and "no intent here" in ra["mismatches"][0]
    assert col.state(books(store, "visitor:b")).intents[done.id].state == "returned" and rb["mismatches"] == []
    assert books(store, "visitor:b").payments[done.payment].returned
    assert col.state(books(store, "visitor:c")).intents[lost_b.id].state == "closed" and rc["settled"] == [lost_b.id]
    assert [r["copy"] for r in col.state(books(store, "visitor:a")).reconciled] == ["visitor:a"]


def test_reconciliation_leaves_out_only_an_earlier_copys_payments():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    sim.create("visitor:n:PI0001:oldmark", 900, "card_ok", {"copy": "visitor:n", "nonce": "oldmark"}, NOW)    # a reset copy's
    sim.create("visitor:n:PI0002:x", 800, "card_ok", {"copy": "visitor:n"}, NOW)                             # no mark
    sim.create("visitor:n:PI0003:abc", 700, "card_ok", {"copy": "visitor:n", "nonce": "xyz"}, NOW)           # a mark its key lacks
    fresh = col.reconcile(store, "visitor:n", sim, NOW)                                    # this copy has paid nothing yet
    assert len(fresh["mismatches"]) == 2 and not any("sim_pay_00001 " in m for m in fresh["mismatches"])
    col.pay(store, "visitor:n", sim, t, 1000, "card_ok", NOW)
    later = col.reconcile(store, "visitor:n", sim, NOW + timedelta(days=1))
    assert [m.split()[4] for m in later["mismatches"]] == ["sim_pay_00002", "sim_pay_00003"]


def test_a_reset_copy_leaves_nothing_processing():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    i = col.pay(store, "visitor:a", sim, t, 1000, "card_pending", NOW)
    other = col.pay(store, "visitor:b", sim, t, 1000, "card_pending", NOW)
    assert col.close_copy(sim, "visitor:a") == [i.outside]
    assert sim.payments[i.outside]["status"] == "canceled" and sim.payments[other.outside]["status"] == "processing"
    store.drop_stream("visitor:a")
    assert col.webhook(store, sim, sim.outbox[-1], NOW) == "unknown"


def test_a_webhook_from_a_reset_copy_never_settles_the_new_copy():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    old = col.pay(store, "visitor:a", sim, t, 1000, "card_pending", NOW)
    store.drop_stream("visitor:a")                                                    # reset without closing anything
    new = col.pay(store, "visitor:a", sim, t, 2000, "card_pending", NOW)
    assert new.id == old.id and new.key != old.key
    sim.settle(old.outside, "succeeded", NOW + timedelta(minutes=1))
    assert col.webhook(store, sim, sim.outbox[-1], NOW + timedelta(minutes=1)) == "unknown"
    assert col.state(books(store, "visitor:a")).intents[new.id].state == "pending"
    assert not [e for e in store.stream_events("visitor:a") if e.kind == "payment.received"]
    r = col.reconcile(store, "visitor:a", sim, NOW + timedelta(days=1))
    assert r["mismatches"] == [] and col.state(books(store, "visitor:a")).intents[new.id].state == "pending"


# the sweep -----------------------------------------------------------------------------------------------------------------
def test_a_missed_instalment_breaches_the_plan_and_opens_a_dated_task_once():
    store, t, lease = world()
    pid, o = on_plan(store, "visitor:a", t)
    do(store, "visitor:a", lambda led: (led.receive_payment(t, 3000, NOW), col.recheck(led, t, NOW)))
    col.sweep(store, "visitor:a", at_local(date(2026, 3, 26), 6))           # the part is due: a notice, no breach
    st = col.state(books(store, "visitor:a"))
    assert plan(store, "visitor:a", pid).state == "active" and list(st.notices) == [f"due:{pid}:1"]
    assert st.notices[f"due:{pid}:1"]["to"] == col.OWNER_ROLE and st.notices[f"due:{pid}:1"]["due"] == "2026-03-26"
    col.sweep(store, "visitor:a", at_local(date(2026, 3, 29), 23))          # still inside the three grace days
    assert plan(store, "visitor:a", pid).state == "active"
    for _ in range(2):
        col.sweep(store, "visitor:a", at_local(date(2026, 3, 30), 6))
    b = books(store, "visitor:a")
    assert plan(store, "visitor:a", pid).state == "breached"
    assert [(k.kind, k.due, k.owner) for k in col.open_tasks(b)] == [("plan breached", date(2026, 3, 30), col.OWNER_ROLE)]
    assert len(col.state(b).notices) == 1 and col.state(b).sweeps == [date(2026, 3, 26), date(2026, 3, 29), date(2026, 3, 30)]


def test_an_uncertain_payment_never_breaches_the_plan():
    store, t, _ = world()
    pid, _ = on_plan(store, "visitor:a", t)
    sim = col.SimulatedProcessor()
    col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW)
    col.pay(store, "visitor:a", sim, t, 3000, "card_pending", at_local(date(2026, 3, 26), 9))
    col.sweep(store, "visitor:a", at_local(date(2026, 4, 2), 6))
    b = books(store, "visitor:a")
    assert plan(store, "visitor:a", pid).state == "active"
    assert [k.kind for k in col.open_tasks(b)] == ["payment uncertain"]


def test_an_upfront_part_never_paid_lets_the_plan_lapse():
    store, t, _ = world()
    pid, _ = on_plan(store, "visitor:a", t)
    col.sweep(store, "visitor:a", at_local(date(2026, 3, 16), 6))
    assert plan(store, "visitor:a", pid).state == "cancelled"


def test_the_sweep_catches_up_after_a_restart(tmp_path):
    path = str(tmp_path / "desk.db")
    store, t, lease = world(path=path)
    pid, _ = on_plan(store, "visitor:a", t)
    do(store, "visitor:a", lambda led: (led.receive_payment(t, 3000, NOW), col.recheck(led, t, NOW)))
    do(store, "visitor:a", lambda led: led.emit("exemption.granted", NOW, {"exemption": "EX0001", "lease": lease, "tenant": t,
                                                                         "scope": ["lien"], "reason": "military service", "owner": "manager",
                                                                         "until": "2026-03-20"}, "exemption:EX0001"))
    col.sweep(store, "visitor:a", NOW)
    del store                                                             # the service stops; nothing runs for weeks
    again = Store(path)
    col.sweep(again, "visitor:a", at_local(date(2026, 4, 6), 7))
    b = again.books("visitor:a")
    assert col.state(b).plans[pid].state == "breached"
    assert [(k.kind, k.due) for k in col.open_tasks(b)] == [("exemption ended", date(2026, 3, 21)), ("plan breached", date(2026, 3, 30))]
    assert set(col.state(b).notices) == {f"due:{pid}:1"}
    assert col.state(b).sweeps == [NOW.date(), date(2026, 4, 6)]


def test_a_task_stays_on_today_until_someone_acknowledges_it():
    store, t, _ = world()
    pid, _ = on_plan(store, "visitor:a", t)
    do(store, "visitor:a", lambda led: (led.receive_payment(t, 3000, NOW), col.recheck(led, t, NOW)))
    col.sweep(store, "visitor:a", at_local(date(2026, 4, 1), 6))
    task = col.open_tasks(books(store, "visitor:a"))[0]
    col.sweep(store, "visitor:a", at_local(date(2026, 4, 20), 6))
    assert [k.id for k in col.open_tasks(books(store, "visitor:a"))] == [task.id]
    do(store, "visitor:a", lambda led: col.acknowledge(led, task.id, "staff:office_manager", at_local(date(2026, 4, 20), 9)))
    assert col.open_tasks(books(store, "visitor:a")) == []
    with pytest.raises(Refused):
        do(store, "visitor:a", lambda led: col.acknowledge(led, task.id, "staff:office_manager", NOW))


def test_the_records_fold_back_the_same_from_the_store():
    store, t, _ = world()
    sim = col.SimulatedProcessor()
    pid, _ = on_plan(store, "visitor:a", t)
    col.pay(store, "visitor:a", sim, t, 3000, "card_ok", NOW)
    col.sweep(store, "visitor:a", at_local(date(2026, 4, 1), 6))
    replayed = col.state(fold(store.events("visitor:a")))
    kept = col.state(store.books("visitor:a"))
    assert replayed.plans[pid].state == kept.plans[pid].state == "breached"
    assert replayed.intents == kept.intents and set(replayed.tasks) == set(kept.tasks)
