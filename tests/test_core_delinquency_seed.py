"""The live copy's tenant on a payment plan: on every day a fresh copy can open, the overlock step is due for her and the plan
holds it, lien eligibility is not yet due, and the fixed books the graded sets read stay as they were."""
from datetime import date, timedelta

from core import delinquency as col
from core.billing import Ledger
from core.books import at_local
from core.gate import Command, evaluate, execute
from core.seed import LIEN_DAYS, NOW, OVERLOCK_DAYS, PLAN_CONSENT, Year, build
from core.store import Store


def test_the_live_copy_opens_with_an_overlock_held_by_a_plan_on_every_day():
    start = date(2026, 11, 25)
    for n in range(130):
        day = start + timedelta(days=n) if n < 70 else date(2028, 1, 20) + timedelta(days=n - 70)
        y = build(today=day, cutoff=at_local(day), standing=True)
        b, now = y.led.books, at_local(day)
        iris = y.plan_tenant
        lease = iris["leases"][0]
        plan = col.state(b).plans[iris["plan"]]
        assert "iris" not in y.cast and plan.state == "active" and plan.consent["quote"] == PLAN_CONSENT, day
        assert [d["at"].date() for d in b.declined if d["tenant"] == iris["tenant"]] == [y.m(0, 2).date()], day
        assert OVERLOCK_DAYS <= col.days_past_due(b, lease, now) <= 40, day
        assert col.step_state(b, lease, "overlock", now) == ("blocked", "the tenant's payment plan is in good standing"), day
        assert col.step_state(b, lease, "lien", now)[0] == "clear", day
        assert [x["state"] for x in col.instalments(b, plan, now)][0] == "paid", day
        assert all(x["state"] in ("paid", "scheduled", "due") for x in col.instalments(b, plan, now)), day
        # Rhea's plan breached before the live window: an open staff task, her overlock waiting for staff, a part still ahead
        rhea = y.breached_tenant
        her_lease, her_plan = rhea["leases"][0], col.state(b).plans[rhea["plan"]]
        assert her_plan.state == "breached" and "rhea" not in y.cast, day
        assert [(k.kind, k.tenant) for k in col.open_tasks(b)] == [("plan breached", rhea["tenant"])], day
        assert col.step_state(b, her_lease, "overlock", now) == ("waiting for staff", "the payment plan was breached; staff review it before any step"), day
        late = col.days_past_due(b, her_lease, now)
        assert col.step_state(b, her_lease, "lien", now)[0] == ("waiting for staff" if late >= LIEN_DAYS else "clear"), day
        reinstate = Command("probe", "review_plan", rhea["tenant"], "staff:office_manager", plan=rhea["plan"], form={"outcome": "reinstate"})
        assert evaluate(b, reinstate, now).allowed, day
        led = Ledger(b)
        col.run_sweep(led, at_local(day, 9))                    # the copy's first sweep that morning breaches nothing new
        assert not [e for e in led.pending if e.kind in ("plan.breached", "plan.review", "task.opened")], day


def test_the_gate_refuses_the_seeded_overlock_and_names_the_plan():
    day = date(2026, 12, 3)
    y = build(today=day, cutoff=at_local(day), standing=True)
    store = Store()
    with store.write():
        store.append("base", y.led.pending)
    t, lease = y.plan_tenant["tenant"], y.plan_tenant["leases"][0]
    d = execute(store, "visitor:fresh", Command("seed-overlock", "overlock", t, "schedule", lease=lease), at_local(day, 10))
    assert not d.allowed and d.check == "the tenant's payment plan is in good standing"


def test_the_two_seeded_tenants_change_no_other_tenant(monkeypatch):
    with_them = build(today=NOW.date(), cutoff=at_local(NOW.date()), standing=True)
    monkeypatch.setattr(Year, "plan_breached", lambda self: None)
    monkeypatch.setattr(Year, "on_a_plan", lambda self: None)
    without = build(today=NOW.date(), cutoff=at_local(NOW.date()), standing=True)
    ours = {with_them.plan_tenant["tenant"], with_them.breached_tenant["tenant"]}
    charges = lambda y: {t: [(c.id, c.cents, c.paid, c.closed) for c in y.led.books.tenant_charges(t)] for t in y.led.books.tenants if t not in ours}
    assert charges(with_them) == charges(without) and len(with_them.led.books.tenants) == len(without.led.books.tenants) + 2


def test_the_fixed_books_carry_no_collections_records():
    y = build()
    assert y.plan_tenant is None and y.breached_tenant is None and y.led.books.collections is None
    assert not [e for e in y.led.pending if e.kind.split(".")[0] in ("plan", "plan_policy", "schedule")]
