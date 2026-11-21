"""The work record: one item for each thing staff have to do, drawn from the records that already say so. Acknowledging is not
finishing: an item closes only when the record it is about changes, or, where nothing else can show it, when staff say they checked.
One condition has one item however often the daily check runs, and after a restart; a problem that comes back reopens its item."""
from datetime import timedelta

from core import delinquency as col
from core import work
from core.billing import Ledger
from core.books import at_local
from core.gate import Command, execute
from core.seed import NOW, build
from core.store import Store

AT = at_local(NOW.date(), 10)
STREAM = "visitor:a"
MANAGER = "staff:office_manager"


def fresh(path=":memory:"):
    y = build(today=NOW.date(), cutoff=at_local(NOW.date()), standing=True)
    store = Store(path)
    with store.write():
        store.append("base", y.led.pending)
    return store


def items(store, now=AT, stream=STREAM):
    books, rent, wk = work.load(store, stream)
    return {i.key: i for i in work.queue(books, rent, wk, now)}


def ask(store, n, action, key, now=AT, source=MANAGER, **args):
    return work.run(store, STREAM, work.Act(f"t{n}", action, source, key, **args), now)


def one_due_overlock(store):
    key = next(k for k, i in sorted(items(store).items()) if i.kind == "overlock")
    books = store.books(STREAM)
    return key, books.leases[key.split(":")[1]]


def step(store, action, lease, now=AT):
    d = execute(store, STREAM, Command(f"{action}:{lease.id}:{now.isoformat()}", action, lease.tenant, MANAGER, lease=lease.id), now)
    assert d.allowed, d.check


def pay_all(store, tenant, now=AT):
    with store.write():
        led = Ledger(store.books(STREAM))
        pid = led.receive_payment(tenant, sum(c.open_cents for c in led.books.open_charges(tenant, skip_held=False)), now)
        store.append(STREAM, led.pending)
    return pid


def give_back(store, pid, now=AT):
    with store.write():
        led = Ledger(store.books(STREAM))
        led.return_payment(now, pid)
        store.append(STREAM, led.pending)


def test_a_fresh_copy_lists_the_years_own_work_and_changes_no_money():
    store = fresh()
    before = store.books(STREAM)
    owed = {t: before.past_due(t, AT) for t in before.tenants}
    got = items(store)
    breached = [i for i in got.values() if i.source == "collections"]
    assert [(i.kind, i.role, i.state) for i in breached] == [("plan breached", "office_manager", "open")]
    due = [i for i in got.values() if i.kind == "overlock"]
    assert len(due) == 13 and all(i.role == "office_manager" and i.closes and i.why for i in due)
    assert sum(owed.values()) == 299600 and sum(1 for v in owed.values() if v) == 23          # the year's figures: 23 tenants owe $2,996.00
    assert work.sweep(store, STREAM, AT) == len(got)                                          # one record opened for each
    after = store.books(STREAM)
    assert {t: after.past_due(t, AT) for t in after.tenants} == owed
    assert store.db.execute("SELECT COUNT(*) FROM journal_lines WHERE stream = ?", (STREAM,)).fetchone()[0] == 0


def test_acknowledging_an_overlock_does_not_close_it_and_a_recorded_overlock_does():
    store = fresh()
    key, lease = one_due_overlock(store)
    d = ask(store, 1, "acknowledge_work", key)
    assert d.allowed and items(store)[key].state == "open" and items(store)[key].acknowledged
    assert key in items(store, AT + timedelta(hours=3))
    step(store, "overlock", lease)
    done = items(store)[key]
    assert (done.state, done.evidence) == ("done", "The overlock is recorded on the unit.")
    assert key not in {i.key for i in work.open_items(list(items(store).values()))}


def test_an_overlock_comes_off_only_when_the_record_says_it_was_lifted_after_the_payment():
    store = fresh()
    key, lease = one_due_overlock(store)
    work.sweep(store, STREAM, AT)
    step(store, "overlock", lease)
    work.sweep(store, STREAM, AT)
    assert items(store)[key].state == "done"
    pay_all(store, lease.tenant)
    came_back = items(store)[key]
    assert (came_back.kind, came_back.state) == ("lift overlock", "reopened") and "paid" in came_back.why
    assert ask(store, 2, "acknowledge_work", key).allowed and items(store)[key].state == "reopened"       # seen, not finished
    assert not ask(store, 3, "finish_work", key, text="I took the lock off").allowed                      # the record closes it, not a word
    step(store, "lift_overlock", lease)
    assert (items(store)[key].state, items(store)[key].evidence) == ("done", "The overlock is lifted on the unit.")


def test_a_returned_payment_reopens_the_overlock_item_instead_of_opening_another():
    store = fresh()
    key, lease = one_due_overlock(store)
    work.sweep(store, STREAM, AT)
    step(store, "overlock", lease)
    work.sweep(store, STREAM, AT)
    pid = pay_all(store, lease.tenant)
    work.sweep(store, STREAM, AT)
    step(store, "lift_overlock", lease)
    work.sweep(store, STREAM, AT)
    assert items(store)[key].state == "done"
    give_back(store, pid)                                    # the bank took the payment back: the unit is past due again
    again = items(store)[key]
    assert (again.kind, again.state, again.cycle) == ("overlock", "reopened", 3)
    work.sweep(store, STREAM, AT)
    assert [k for k in items(store) if k.endswith(key.split(":")[1])] == [key]
    assert sum(1 for e in store.stream_events(STREAM) if e.kind == "work.opened" and e.payload["key"] == key) == 1


def test_the_daily_check_and_a_restart_make_no_second_item(tmp_path):
    store = fresh(str(tmp_path / "copy.db"))
    first = work.sweep(store, STREAM, AT)
    assert first >= 14 and work.sweep(store, STREAM, AT) == 0 and work.sweep(store, STREAM, AT + timedelta(minutes=5)) == 0
    keys = sorted(items(store))
    restarted = Store(str(tmp_path / "copy.db"))             # a new process reading the same database
    assert work.sweep(restarted, STREAM, AT + timedelta(hours=1)) == 0 and sorted(items(restarted)) == keys
    opened = [e.payload["key"] for e in store.stream_events(STREAM) if e.kind == "work.opened"]
    assert len(opened) == len(set(opened)) == first


def test_an_item_assigned_to_a_role_shows_under_that_roles_work_and_an_overdue_one_stays():
    store = fresh()
    key = next(k for k, i in items(store).items() if i.source == "collections")
    assert items(store)[key].role == "office_manager"
    assert ask(store, 1, "assign_work", key, to="front_desk").allowed
    ids = lambda role, person=None: {i.key for i in work.mine(list(items(store).values()), role, person)}
    assert key in ids("front_desk") and key not in ids("office_manager")
    assert ask(store, 2, "assign_work", key, to="person:dana.reyes").allowed
    assert key in ids("office_manager", "person:dana.reyes") and key not in ids("front_desk")
    later = items(store, AT + timedelta(days=40))[key]
    assert later.state == "assigned" and later.overdue and later.due < (AT + timedelta(days=40)).date()


def test_an_item_that_only_a_person_can_clear_closes_when_staff_say_they_checked():
    store = fresh()
    with store.write():
        led = Ledger(store.books(STREAM))
        col._task(led, "call back", next(iter(led.books.tenants)), "callback:PR0099", "promised $50.00 by Nov 20", AT.date(), AT)
        store.append(STREAM, led.pending)
    key = "collections:callback:PR0099"
    assert items(store)[key].attest
    assert not ask(store, 1, "finish_work", key, text="").allowed                                       # a word on what was checked is needed
    assert ask(store, 2, "finish_work", key, text="Called her, she pays Friday").allowed
    assert (items(store)[key].state, items(store)[key].evidence) == ("done", "Staff checked: Called her, she pays Friday")
    plan = next(k for k, i in items(store).items() if i.kind == "plan breached")
    assert not ask(store, 3, "finish_work", plan, text="I looked at it").allowed and items(store)[plan].state == "open"


def test_a_plan_item_closes_when_staff_decide_the_plan_not_when_the_task_is_acknowledged():
    store = fresh()
    books = store.books(STREAM)
    plan = next(p for p in col.state(books).plans.values() if p.state == "breached")
    key = next(k for k, i in items(store).items() if i.kind == "plan breached")
    task = next(t for t in col.open_tasks(books))
    work.sweep(store, STREAM, AT)
    with store.write():
        led = Ledger(store.books(STREAM))
        col.acknowledge(led, task.id, MANAGER, AT)
        store.append(STREAM, led.pending)
    assert items(store)[key].state == "open"                                                         # the old acknowledgement is not a decision
    d = execute(store, STREAM, Command("review", "review_plan", plan.tenant, MANAGER, plan=plan.id, form={"outcome": "reinstate"}, reason="pays Friday"), AT)
    assert d.allowed and items(store)[key].state == "done" and items(store)[key].evidence == "Staff decided the plan."


def test_the_gate_refuses_what_is_not_staffs_or_not_there_and_records_each_refusal_once():
    store = fresh()
    key = next(iter(items(store)))
    for n, (action, source, k, args, why) in enumerate((
            ("assign_work", "operator", key, {"to": "front_desk"}, "the agent may not assign work; only staff may"),
            ("assign_work", MANAGER, key, {"to": "janitor"}, "work goes to a staff role or a named person"),
            ("start_work", MANAGER, "overlock:L9999", {}, "there is no such item of work"),
            ("note_work", MANAGER, key, {"text": " "}, "a note needs words"),
            ("close_everything", MANAGER, key, {}, "not an action the system knows")), 1):
        d = ask(store, f"r{n}", action, k, source=source, **args)
        assert (d.allowed, d.check) == (False, why)
        assert ask(store, f"r{n}", action, k, source=source, **args).repeated
    assert ask(store, "s", "start_work", key).allowed and not ask(store, "s2", "start_work", key).allowed
    assert items(store)[key].state == "in progress"
    assert ask(store, "n", "note_work", key, text="Waiting on the manager").allowed and items(store)[key].notes[0][2] == "Waiting on the manager"


def test_one_copys_work_never_shows_in_another():
    store = fresh()
    key = next(iter(items(store)))
    assert ask(store, 1, "assign_work", key, to="manager").allowed and work.sweep(store, STREAM, AT)
    assert items(store, stream="visitor:b")[key].assignee is None and not [e for e in store.stream_events("visitor:b")]


def test_an_open_item_whose_condition_changed_before_the_check_ran_offers_the_current_action():
    store = fresh()
    key, lease = one_due_overlock(store)
    work.sweep(store, STREAM, AT)
    step(store, "overlock", lease)                           # recorded, then paid, before the next daily check
    pay_all(store, lease.tenant)
    now = items(store)[key]
    assert (now.kind, now.closes, now.state) == ("lift overlock", "The overlock is lifted on the unit.", "open")
    assert work.sweep(store, STREAM, AT) == 1 and work.sweep(store, STREAM, AT) == 0     # the change is kept once
    kept = items(store)[key]
    assert (kept.kind, kept.closes, kept.cycle) == ("lift overlock", "The overlock is lifted on the unit.", 1)
    step(store, "lift_overlock", lease)
    assert items(store)[key].evidence == "The overlock is lifted on the unit."


def test_a_condition_that_did_not_change_keeps_its_first_reading_and_writes_nothing():
    store = fresh()
    key, _ = one_due_overlock(store)
    work.sweep(store, STREAM, AT)
    first = items(store)[key]
    assert ask(store, 1, "acknowledge_work", key).allowed
    later = AT + timedelta(days=2)
    mine = lambda: [e.kind for e in store.stream_events(STREAM) if e.kind.startswith("work.") and e.payload.get("key") == key]
    before = mine()
    work.sweep(store, STREAM, later)                         # other items may open as days pass; this one is unchanged
    assert mine() == before == ["work.opened", "work.acknowledged"]
    kept = items(store, later)[key]
    assert (kept.kind, kept.closes, kept.due) == (first.kind, first.closes, first.due)
