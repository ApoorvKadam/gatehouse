"""Collections commands through the gate: the same order as every other action (the action and its scope, fixed safety, the
owner's settings, then holds), with the hold on the whole tenancy as the last check."""
import itertools
from datetime import date, timedelta

from core import delinquency as col
from core.books import at_local
from core.gate import Command, evaluate, execute
from tests.test_core_delinquency import NOW, do, world

ids = itertools.count()


def run(store, stream, now=NOW, **kw):
    kw.setdefault("source", "operator")
    kw.setdefault("id", f"{stream}:{kw['action']}:{next(ids)}")
    return execute(store, stream, Command(**kw), now)


def plans(store, stream):
    return col.state(store.books(stream)).plans


def active_plan(store, stream, t, now=NOW):
    run(store, stream, now, action="propose_plan", tenant=t)
    pid = max(plans(store, stream))
    run(store, stream, now, action="offer_plan", tenant=t, plan=pid, form={"instalments": 3})
    version = plans(store, stream)[pid].offered[-1]["version"]
    assert run(store, stream, now, action="accept_plan", tenant=t, plan=pid, offer=version, quote="Yes to the three payments").allowed
    do(store, stream, lambda led: (led.receive_payment(t, 3000, now), col.recheck(led, t, now)))
    assert plans(store, stream)[pid].state == "active"
    return pid


def test_the_plan_flow_runs_through_the_gate_and_code_writes_every_term():
    store, t, _ = world()
    s = "visitor:g1"
    assert run(store, s, action="propose_plan", tenant=t).check == "4 plans computed under the owner's policy"
    pid = max(plans(store, s))
    far = run(store, s, action="offer_plan", tenant=t, plan=pid, form={"instalments": 6})
    assert not far.allowed and far.check.startswith("That plan is not possible: a plan has 1 to 4 instalments") and "$30.00 today" in far.check
    ok = run(store, s, action="offer_plan", tenant=t, plan=pid, form={"instalments": 3}, id="offer-3")
    assert ok.allowed and "$30.00 today, then 3 payments: $30.00 on Mar 26" in ok.check
    version = plans(store, s)[pid].offered[-1]["version"]
    assert not run(store, s, action="accept_plan", tenant=t, plan=pid, offer=version).allowed
    assert not run(store, s, action="accept_plan", tenant=t, plan=pid, offer="0" * 12, quote="yes").allowed
    assert run(store, s, action="accept_plan", tenant=t, plan=pid, offer=version, quote="yes please").allowed
    assert plans(store, s)[pid].state == "accepted"
    again = run(store, s, action="offer_plan", tenant=t, plan=pid, form={"instalments": 3}, id="offer-3")
    assert again.repeated and again.allowed and plans(store, s)[pid].state == "accepted"
    assert not run(store, s, action="propose_plan", tenant=t).allowed            # one open plan at a time


def test_an_unaccepted_offer_blocks_nothing():
    store, t, lease = world()
    s = "visitor:g2"
    run(store, s, action="propose_plan", tenant=t)
    run(store, s, action="offer_plan", tenant=t, plan=max(plans(store, s)), form={"instalments": 2})
    assert col.step_state(store.books(s), lease, "overlock", NOW)[0] == "eligible"
    assert run(store, s, action="overlock", tenant=t, lease=lease, source="staff:manager").allowed
    assert col.step_state(store.books(s), lease, "overlock", NOW)[0] == "recorded"


def test_a_good_plan_holds_overlock_and_lien_until_a_breach_and_a_staff_review():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:g3"
    pid = active_plan(store, s, t)
    for action in ("overlock", "flag_lien_eligible"):
        d = run(store, s, action=action, tenant=t, lease=lease, source="schedule")
        assert not d.allowed and d.check == "the tenant's payment plan is in good standing"
    late = at_local(date(2026, 3, 30), 7)
    col.sweep(store, s, late)
    assert plans(store, s)[pid].state == "breached"
    d = run(store, s, late, action="overlock", tenant=t, lease=lease, source="schedule")
    assert not d.allowed and "staff review it" in d.check
    assert col.step_state(store.books(s), lease, "lien", late)[0] == "waiting for staff"
    assert not run(store, s, late, action="review_plan", tenant=t, plan=pid, form={"outcome": "end"}).allowed     # the agent cannot
    assert run(store, s, late, action="review_plan", tenant=t, plan=pid, form={"outcome": "reinstate"}, source="staff:office_manager").allowed
    assert not run(store, s, late, action="overlock", tenant=t, lease=lease, source="schedule").allowed
    col.sweep(store, s, late + timedelta(days=1))
    assert plans(store, s)[pid].state == "active"                             # the same missed part does not breach twice by itself
    store2, t2, lease2 = world(schedule=(10, 11))
    pid2 = active_plan(store2, s, t2)
    col.sweep(store2, s, late)
    assert run(store2, s, late, action="review_plan", tenant=t2, plan=pid2, form={"outcome": "end"}, source="staff:office_manager").allowed
    assert plans(store2, s)[pid2].state == "cancelled"
    assert run(store2, s, late, action="overlock", tenant=t2, lease=lease2, source="schedule").allowed
    assert run(store2, s, late, action="flag_lien_eligible", tenant=t2, lease=lease2, source="staff:manager").allowed
    st = col.state(store2.books(s))
    assert st.overlocks[lease2]["record_only"] and lease2 in st.liens


def test_enforcement_checks_come_in_the_gate_order():
    store, t, lease = world()
    s = "visitor:g4"
    assert run(store, s, action="overlock", tenant=t, lease=lease).check == "only staff or the owner's schedule take an enforcement step"
    assert run(store, s, action="overlock", tenant=t, lease="L0999", source="schedule").check == "no such unit on this tenant's account"
    early = run(store, s, NOW - timedelta(days=3), action="overlock", tenant=t, lease=lease, source="schedule")
    assert not early.allowed and early.check.startswith("not due on the owner's schedule: 10 days")
    lien = run(store, s, action="flag_lien_eligible", tenant=t, lease=lease, source="schedule")
    assert not lien.allowed and "45 days" in lien.check
    bare, t0, lease0 = world(schedule=None)
    assert run(bare, s, action="overlock", tenant=t0, lease=lease0, source="schedule").check == "the owner has set no schedule for this step"
    sim = col.SimulatedProcessor()
    col.pay(store, s, sim, t, 1000, "card_pending", NOW)
    assert run(store, s, action="overlock", tenant=t, lease=lease, source="schedule").check == "a payment from this tenant is waiting for the processor's answer"


def test_an_open_dispute_on_the_tenancy_blocks_overlock_without_a_charge_hold():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:g5"
    rent = next(c for c in store.books("base").open_charges(t) if c.kind == "rent")
    d = run(store, s, action="open_dispute", tenant=t, charge=rent.id, quote="I paid March in cash at the desk")
    assert d.allowed and not d.hold and not store.books(s).held(rent.id)
    for action in ("overlock", "flag_lien_eligible"):
        assert run(store, s, action=action, tenant=t, lease=lease, source="schedule").check == "a dispute on this tenancy is open"


def test_a_dispute_mid_plan_holds_its_charge_and_sends_the_plan_to_staff():
    store, t, lease = world()
    s = "visitor:g6"
    pid = active_plan(store, s, t)
    fee = next(c for c in store.books(s).open_charges(t) if c.kind == "late_fee")
    d = run(store, s, action="open_dispute", tenant=t, charge=fee.id, quote="The card machine was down on the 2nd")
    assert d.allowed and d.hold and "payment plan" in d.check
    b = store.books(s)
    p = col.state(b).plans[pid]
    assert b.held(fee.id) and p.state == "review" and [k.kind for k in col.open_tasks(b)] == ["plan needs staff"]
    assert [x["state"] for x in col.instalments(b, p, NOW)] == ["paid", "scheduled", "scheduled", "scheduled"]
    assert col.step_state(b, lease, "overlock", NOW) == ("blocked", "a dispute on this tenancy is open")
    col.sweep(store, s, at_local(date(2026, 4, 1), 7))                          # with staff: never breached by the sweep
    assert col.state(store.books(s)).plans[pid].state == "review"


def test_exemptions_are_granted_and_revoked_by_staff_for_named_steps():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:g7"
    form = {"scope": ["overlock"], "reason": "tenant in hospital", "until": "2026-03-20"}
    assert run(store, s, action="grant_exemption", tenant=t, lease=lease, form=form).check == "only staff take this step"
    assert not run(store, s, action="grant_exemption", tenant=t, lease=lease, form={**form, "until": "2026-09-01"}, source="staff:manager").allowed
    assert not run(store, s, action="grant_exemption", tenant=t, lease=lease, form={**form, "scope": ["sale"]}, source="staff:manager").allowed
    assert run(store, s, action="grant_exemption", tenant=t, lease=lease, form=form, source="staff:manager").allowed
    e = next(iter(col.state(store.books(s)).exemptions.values()))
    assert (e.scope, e.reason, e.owner, e.until) == (["overlock"], "tenant in hospital", "manager", date(2026, 3, 20))
    assert "tenant in hospital" in run(store, s, action="overlock", tenant=t, lease=lease, source="schedule").check
    assert run(store, s, action="flag_lien_eligible", tenant=t, lease=lease, source="schedule").allowed      # not in its scope
    assert run(store, s, action="revoke_exemption", tenant=t, lease=lease, form={"exemption": e.id}, source="staff:manager").allowed
    assert run(store, s, action="overlock", tenant=t, lease=lease, source="schedule").allowed


def test_an_overlock_is_lifted_only_after_a_confirmed_payment():
    store, t, lease = world()
    s = "visitor:g8"
    assert run(store, s, action="overlock", tenant=t, lease=lease, source="staff:manager").allowed
    assert not run(store, s, action="overlock", tenant=t, lease=lease, source="staff:manager").allowed
    lift = lambda now: run(store, s, now, action="lift_overlock", tenant=t, lease=lease, source="staff:manager")
    assert lift(NOW).check == "an overlock is lifted only after a payment the processor confirmed"
    sim = col.SimulatedProcessor()
    i = col.pay(store, s, sim, t, 12000, "card_pending", NOW + timedelta(minutes=5))
    assert not lift(NOW + timedelta(minutes=6)).allowed
    sim.settle(i.outside, "succeeded", NOW + timedelta(minutes=7))
    col.webhook(store, sim, sim.outbox[-1], NOW + timedelta(minutes=7))
    assert lift(NOW + timedelta(minutes=8)).allowed
    assert col.step_state(store.books(s), lease, "overlock", NOW + timedelta(minutes=8))[0] == "clear"


def test_a_promise_is_recorded_through_the_gate_and_holds_no_step():
    store, t, lease = world()
    s = "visitor:g9"
    assert not run(store, s, action="record_promise", tenant=t, form={"cents": 5000, "by": "2026-05-01"}).allowed
    assert not run(store, s, action="record_promise", tenant=t, form={"cents": 0, "by": "2026-03-15"}).allowed
    d = run(store, s, action="record_promise", tenant=t, form={"cents": 5000, "by": "2026-03-15"}, quote="I get paid Friday")
    assert d.allowed and "call-back on Mar 16" in d.check
    assert run(store, s, action="overlock", tenant=t, lease=lease, source="schedule").allowed


def test_a_second_breach_after_reinstatement_opens_its_own_task():
    store, t, _ = world()
    s = "visitor:g10"
    pid = active_plan(store, s, t)
    late = at_local(date(2026, 3, 30), 7)
    col.sweep(store, s, late)
    first = col.open_tasks(store.books(s))[0]
    run(store, s, late, action="review_plan", tenant=t, plan=pid, form={"outcome": "reinstate"}, source="staff:office_manager")
    assert col.open_tasks(store.books(s)) == [] and col.state(store.books(s)).tasks[first.id].acknowledged     # the decision settled it
    col.sweep(store, s, at_local(date(2026, 4, 13), 7))                      # the next part, due Apr 9, is missed too
    assert plans(store, s)[pid].state == "breached"
    assert [(k.kind, k.due) for k in col.open_tasks(store.books(s))] == [("plan breached", date(2026, 4, 13))]


def test_a_charge_in_the_plan_waived_later_goes_to_staff_and_a_paid_up_tenant_is_never_breached():
    store, t, _ = world()
    s = "visitor:g11"
    pid = active_plan(store, s, t)
    fee = next(c for c in store.books(s).open_charges(t) if c.kind == "late_fee")
    do(store, s, lambda led: led.waive(NOW + timedelta(hours=1), fee.id, "goodwill", "staff:manager"))
    col.sweep(store, s, NOW + timedelta(hours=2))                             # the sweep notices it even when nothing else did
    assert plans(store, s)[pid].state == "review" and [k.kind for k in col.open_tasks(store.books(s))] == ["plan needs staff"]
    run(store, s, NOW + timedelta(hours=3), action="review_plan", tenant=t, plan=pid, form={"outcome": "reinstate"}, source="staff:office_manager")
    for when in (date(2026, 3, 26), date(2026, 4, 9)):
        at = at_local(when, 10)
        do(store, s, lambda led, at=at: (led.receive_payment(t, 3000, at), col.recheck(led, t, at)))
    assert plans(store, s)[pid].state == "active"
    at = at_local(date(2026, 4, 20), 10)
    do(store, s, lambda led: (led.receive_payment(t, 1000, at), col.recheck(led, t, at)))   # the last of the rent: nothing owed now
    assert plans(store, s)[pid].state == "completed"
    col.sweep(store, s, at_local(date(2026, 4, 30), 7))
    assert plans(store, s)[pid].state == "completed"


def test_the_gate_and_the_shown_state_never_disagree():
    """Every state the overlock and lien steps can be in: the gate allows the step only when the shown state is eligible, and a
    refusal gives the same reason the state shows."""
    seen = set()

    def agree(store, stream, t, lease, now):
        b = store.books(stream)
        for action, step in (("overlock", "overlock"), ("flag_lien_eligible", "lien")):
            where, why = col.step_state(b, lease, step, now)
            d = evaluate(b, Command("probe", action, t, "schedule", lease=lease), now)
            assert d.allowed == (where == "eligible") and (d.allowed or d.check == why), (stream, step, where, d.check)
            seen.add(where)
    store, t, lease = world(schedule=(10, 11))
    agree(store, "base", t, lease, NOW)                                       # eligible
    agree(store, "base", t, lease, NOW - timedelta(days=3))                   # clear: not due yet
    early = "visitor:g14"
    col.pay(store, early, col.SimulatedProcessor(), t, 1000, "card_pending", NOW - timedelta(days=3))
    assert col.step_state(store.books(early), lease, "overlock", NOW - timedelta(days=3))[0] == "clear"     # not overdue: never waiting
    agree(store, early, t, lease, NOW - timedelta(days=3))
    bare, t0, lease0 = world(schedule=None)
    agree(bare, "base", t0, lease0, NOW)                                      # clear: no schedule
    s = "visitor:g12"
    pid = active_plan(store, s, t)
    agree(store, s, t, lease, NOW)                                            # blocked by the plan
    col.sweep(store, s, at_local(date(2026, 3, 30), 7))
    agree(store, s, t, lease, at_local(date(2026, 3, 30), 8))                 # waiting for staff: breached
    run(store, s, at_local(date(2026, 3, 30), 8), action="review_plan", tenant=t, plan=pid, form={"outcome": "end"}, source="staff:manager")
    col.pay(store, s, col.SimulatedProcessor(), t, 1000, "card_pending", at_local(date(2026, 3, 30), 9))
    agree(store, s, t, lease, at_local(date(2026, 3, 30), 10))                # waiting for staff: a payment in flight
    s2 = "visitor:g13"
    run(store, s2, action="overlock", tenant=t, lease=lease, source="staff:manager")
    agree(store, s2, t, lease, NOW + timedelta(minutes=1))                    # overlock recorded; lien still eligible
    assert seen == {"eligible", "clear", "blocked", "waiting for staff", "recorded"}


def test_a_plan_with_no_part_still_ahead_cannot_be_reinstated():
    store, t, lease = world()
    s = "visitor:g15"
    pid = active_plan(store, s, t)
    late = at_local(date(2026, 4, 28), 7)                                     # every part (Mar 26, Apr 9, Apr 23) is past its grace days
    col.sweep(store, s, late)
    d = run(store, s, late, action="review_plan", tenant=t, plan=pid, form={"outcome": "reinstate"}, source="staff:office_manager")
    assert not d.allowed and d.check.startswith("every part of the plan is past its grace days")
    assert plans(store, s)[pid].state == "breached"
    assert run(store, s, late, action="review_plan", tenant=t, plan=pid, form={"outcome": "end"}, source="staff:office_manager").allowed


def test_a_staff_decision_on_a_plan_settles_its_open_tasks():
    for outcome in ("reinstate", "end"):
        store, t, _ = world()
        s = f"visitor:g16{outcome}"
        pid = active_plan(store, s, t)
        late = at_local(date(2026, 3, 30), 7)
        col.sweep(store, s, late)
        assert [k.kind for k in col.open_tasks(store.books(s))] == ["plan breached"]
        assert run(store, s, late, action="review_plan", tenant=t, plan=pid, form={"outcome": outcome}, source="staff:office_manager").allowed
        b = store.books(s)
        assert col.open_tasks(b) == [] and [k.acknowledged["by"] for k in col.state(b).tasks.values()] == ["staff:office_manager"], outcome


def test_a_lease_that_ended_has_no_step_to_take_and_says_why():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:g17"
    do(store, s, lambda led: led.end_lease(at_local(date(2026, 3, 10), 18), lease))
    b = store.books(s)
    for action, step in (("overlock", "overlock"), ("flag_lien_eligible", "lien")):
        assert col.step_state(b, lease, step, NOW) == ("clear", "the lease ended Mar 10, so there is no unit to overlock; staff handle what is still owed")
        assert evaluate(b, Command("probe", action, t, "schedule", lease=lease), NOW).check == "that lease has ended; staff handle it"
