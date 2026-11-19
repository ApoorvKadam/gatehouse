"""Renting: one holder per unit even under racing writes, holds that run out, rentals that start only with a confirmed payment
and an accepted agreement, transfers and move-outs through billing, the waitlist, and gate codes that count only once read back,
with every outside call made once however often the work is retried."""
import hashlib
import itertools
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from core.billing import Ledger
from core.books import ADMIN_FEE, RENT, at_local
from core.gate import RENT_SCOPE
from core.renting import (ACCESS_LEVEL, AGREEMENT_VERSION, CHECKOUT, DO, ON, STEPS, Act, Crash, RentLedger, StandIn, advance,
                          check_codes, checkout_intent, copy_mark, load, run)
from core.store import BASE, Store

NOW = at_local(date(2026, 11, 10), 10)
N = itertools.count(1)


def facility(path=":memory:"):
    """Northstar Storage with three priced units and two tenants who rent nothing yet."""
    store = Store(path)
    led = Ledger()
    led.set_up(at_local(date(2026, 8, 1)), "Northstar Storage", "Orlando, FL", ["A101", "A102", "B201"])
    dana = led.add_tenant(at_local(date(2026, 9, 1), 9), "Dana Reyes", "+14075550101")
    eli = led.add_tenant(at_local(date(2026, 9, 1), 9), "Eli Moss", "+14075550102")
    with store.write():
        store.append(BASE, led.pending)
    for unit, rent, size in (("A101", 10000, "5x10"), ("A102", 9000, "5x10"), ("B201", 15000, "10x10")):
        ok(store, Act(f"price-{unit}", "price_unit", "staff:manager", unit, args={"rent_cents": rent, "size": size}), NOW - timedelta(days=60))
    return store, dana, eli


def act(action, source, about, tenant=None, **args):
    return Act(f"c{next(N)}", action, source, about, tenant, args)


def go(store, a, now=NOW):
    return run(store, BASE, a, now)


def ok(store, a, now=NOW):
    d = go(store, a, now)
    assert d.allowed, d.check
    return d.made


def rent(store):
    return load(store, BASE)[1]


def books(store):
    return store.books(BASE)


def billing(store, fn):
    """Run billing commands straight on the books, as the processor and the monthly run do."""
    with store.write():
        led = Ledger(store.books(BASE))
        out = fn(led)
        store.append(BASE, led.pending)
    return out


def pay(store, tenant, rental, cents, now=NOW):
    checkout_intent(store, BASE, rental, now)
    pid = billing(store, lambda led: led.receive_payment(tenant, cents, now))
    return go(store, act("confirm_payment", "code", rental, payment=pid), now)


def accept(store, tenant, rental, now=NOW, sha=None):
    r = rent(store)
    sha = sha or r.agreements[r.rentals[rental].agreement].sha256
    new = r.rentals[rental]
    extra = {"amount_cents": r.short(new, now, r.rentals[r.transfers[new.transfer].rental])} if new.transfer else {}
    return go(store, act("accept_agreement", "renter", rental, tenant, sha256=sha, **extra), now)


def checkout(store, tenant, unit, now=NOW):
    rs = ok(store, act("reserve", "renter", unit, tenant), now)
    return ok(store, act("start_checkout", "renter", rs, tenant), now)


def rented(store, tenant, unit, now=NOW):
    rn = checkout(store, tenant, unit, now)
    assert pay(store, tenant, rn, rent(store).rentals[rn].quote["total_cents"], now).allowed
    assert accept(store, tenant, rn, now).allowed
    assert rent(store).rentals[rn].state == "active"
    return rn


def move_out(store, rental, now, on=None):
    mo = ok(store, act("request_move_out", "staff:manager", rental, **({"on": on} if on else {})), now)
    ok(store, act("review_move_out", "staff:manager", mo), now)
    ok(store, act("final_bill", "staff:manager", mo), now)
    return mo


def test_every_renting_command_has_a_gate_scope_a_record_and_a_body():
    assert set(RENT_SCOPE) == set(ON) == set(DO)


# one holder per unit ------------------------------------------------------------------------------------------------------
def test_two_writers_racing_for_one_unit_get_one_hold(tmp_path):
    path = str(tmp_path / "facility.db")
    store, dana, eli = facility(path)
    start, out = threading.Barrier(2), {}

    def reserve(tenant):
        mine = Store(path)               # each writer has its own connection, as two web workers would
        start.wait()
        out[tenant] = run(mine, BASE, Act(f"race-{tenant}", "reserve", "renter", "A101", tenant), NOW)

    threads = [threading.Thread(target=reserve, args=(t,)) for t in (dana, eli)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(d.allowed for d in out.values()) == [False, True]
    assert next(d for d in out.values() if not d.allowed).check == "the unit is reserved"
    assert len([r for r in rent(store).reservations.values() if r.unit == "A101"]) == 1


def test_a_writer_working_from_an_old_read_cannot_take_a_held_unit():
    store, dana, eli = facility()
    pending = []
    for tenant in (dana, eli):           # both read the unit as free before either wrote
        b, r = load(store, BASE)
        led = RentLedger(b, r)
        DO["reserve"](r, led, Act(f"stale-{tenant}", "reserve", "renter", "A101", tenant), NOW)
        pending.append(led.pending)
    with store.write():
        store.append(BASE, pending[0])
    with pytest.raises(sqlite3.IntegrityError):
        with store.write():
            store.append(BASE, pending[1])
    assert [r.tenant for r in rent(store).reservations.values()] == [dana]


def test_a_unit_with_a_lease_from_outside_renting_is_occupied():
    store, dana, eli = facility()
    billing(store, lambda led: led.sign_lease(at_local(date(2026, 11, 1), 9), eli, "A102", 9000, [ADMIN_FEE]))
    assert rent(store).status("A102", NOW) == "occupied"
    assert go(store, act("reserve", "renter", "A102", dana), NOW).check == "the unit is occupied"


def test_a_blocked_unit_cannot_be_held_until_unblocked():
    store, dana, _ = facility()
    ok(store, act("block_unit", "staff:manager", "A101", reason="door repair"))
    assert rent(store).status("A101", NOW) == "blocked"
    assert not go(store, act("reserve", "renter", "A101", dana)).allowed
    ok(store, act("unblock_unit", "staff:manager", "A101"))
    assert ok(store, act("reserve", "renter", "A101", dana))


# holds and their deadlines ------------------------------------------------------------------------------------------------
def test_a_checkout_hold_lasts_twenty_minutes():
    store, dana, eli = facility()
    rs = ok(store, act("reserve", "renter", "A101", dana))
    assert rent(store).status("A101", NOW + CHECKOUT - timedelta(seconds=1)) == "reserved"
    assert rent(store).status("A101", NOW + CHECKOUT) == "vacant"
    assert not go(store, act("reserve", "renter", "A101", eli), NOW + timedelta(minutes=5)).allowed
    assert ok(store, act("reserve", "renter", "A101", eli), NOW + CHECKOUT)
    assert rent(store).reservations[rs].state == "expired"


def test_only_staff_set_a_longer_hold():
    store, dana, eli = facility()
    until = (NOW + timedelta(days=2)).isoformat()
    assert "only staff" in go(store, act("reserve", "operator", "A101", dana, until=until)).check
    assert ok(store, act("reserve", "operator", "A101", dana)) is not None       # the agent may place the short hold
    ok(store, act("cancel_reservation", "staff:manager", list(rent(store).reservations)[0]))
    ok(store, act("reserve", "staff:front_desk", "A101", dana, until=until))
    assert rent(store).status("A101", NOW + timedelta(days=1)) == "reserved"
    assert rent(store).status("A101", NOW + timedelta(days=2)) == "vacant"


def test_the_unit_stays_held_while_a_payment_is_unconfirmed():
    store, dana, eli = facility()
    rn = checkout(store, dana, "A101")
    key = checkout_intent(store, BASE, rn, NOW)
    assert checkout_intent(store, BASE, rn, NOW) == key                 # asked again: the same intent, the same key
    later = NOW + timedelta(minutes=45)
    assert rent(store).status("A101", later) == "reserved"
    advance(store, BASE, StandIn(), later)
    r = rent(store)
    assert r.rentals[rn].state == "pending"
    task = next(t for t in r.tasks.values() if t.about == rn)
    assert task.kind == "payment" and task.due == date(2026, 11, 10) and task.open
    ok(store, act("payment_failed", "code", rn, reason="card declined"), later)
    assert rent(store).status("A101", later) == "vacant"
    advance(store, BASE, StandIn(), later)
    assert rent(store).rentals[rn].state == "cancelled"
    assert books(store).declined[-1]["tenant"] == dana


def test_a_payment_after_the_hold_ran_out_stays_as_credit_with_a_refund_task():
    store, dana, _ = facility()
    rn = checkout(store, dana, "A101")
    later = NOW + timedelta(hours=1)
    advance(store, BASE, StandIn(), later)
    pid = billing(store, lambda led: led.receive_payment(dana, 9500, later))
    d = go(store, act("confirm_payment", "code", rn, payment=pid), later)
    assert not d.allowed and "set aside for staff to refund" in d.check
    assert any(t.kind == "refund" and t.about == rn and "$95.00" in t.reason for t in rent(store).tasks.values())
    assert books(store).credit.get(dana, 0) == 0 and books(store).refunds[dana] == 9500


# rentals start with both confirmations, and only code starts them ----------------------------------------------------------
def test_a_rental_is_active_only_with_payment_and_an_accepted_agreement():
    store, dana, eli = facility()
    first = checkout(store, dana, "A101")
    assert accept(store, dana, first).allowed
    assert rent(store).rentals[first].state == "awaiting_payment" and not books(store).leases
    assert pay(store, dana, first, 9500).allowed       # $70.00 for Nov 10 to 30 and the $25.00 admin fee
    r, b = rent(store), books(store)
    lease = r.rentals[first].lease
    assert r.rentals[first].state == "active" and b.receivable(lease) == 0 and r.status("A101", NOW) == "occupied"
    assert [c.cents for c in b.tenant_charges(dana)] == [7000, 2500]

    second = checkout(store, eli, "A102")
    assert pay(store, eli, second, rent(store).rentals[second].quote["total_cents"]).allowed
    assert rent(store).rentals[second].state == "awaiting_signature"
    assert "not the agreement shown" in accept(store, eli, second, sha="0" * 64).check
    assert accept(store, eli, second).allowed and rent(store).rentals[second].state == "active"


def test_only_code_activates_a_rental():
    store, dana, _ = facility()
    rn = checkout(store, dana, "A101")
    for source, tenant in (("renter", dana), ("staff:manager", None), ("operator", dana)):
        assert "only code may" in go(store, act("activate", source, rn, tenant)).check
    # the agent may hold a unit but not take a renter through checkout or accept for them
    rs = ok(store, act("reserve", "operator", "A102", dana))
    assert not go(store, act("start_checkout", "operator", rs, dana)).allowed
    assert not go(store, act("accept_agreement", "operator", rn, dana, sha256="x")).allowed


def test_the_agreement_keeps_its_version_and_the_exact_text_fingerprint():
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    r = rent(store)
    ag = r.agreements[r.rentals[rn].agreement]
    assert ag.version == AGREEMENT_VERSION and ag.sha256 == hashlib.sha256(ag.text.encode()).hexdigest()
    assert ag.state == "accepted" and ag.by == f"renter:{dana}" and ag.at == NOW
    assert "unit A101 to Dana Reyes, starting November 10, 2026" in ag.text and "$95.00 in all" in ag.text
    assert "signature" not in ag.text.lower()


def test_money_that_older_charges_take_leaves_the_rental_waiting_with_a_task():
    store, dana, eli = facility()
    billing(store, lambda led: led.sign_lease(at_local(date(2026, 11, 1), 9), eli, "A102", 9000, [ADMIN_FEE]))     # $115.00 open
    rn = checkout(store, eli, "B201")
    total = rent(store).rentals[rn].quote["total_cents"]                 # $105.00 rent for Nov 10 to 30 and the admin fee
    assert total == 13000
    pay(store, eli, rn, total)
    accept(store, eli, rn)
    r = rent(store)
    assert r.rentals[rn].state == "awaiting_payment" and r.rentals[rn].lease is None
    task = next(t for t in r.tasks.values() if t.about == rn and t.open)
    assert task.kind == "activation" and "$115.00 short" in task.reason
    pay(store, eli, rn, 11500, NOW + timedelta(minutes=5))
    r = rent(store)
    assert r.rentals[rn].state == "active" and not r.tasks[task.id].open
    assert books(store).receivable(r.rentals[rn].lease) == 0


def test_activation_on_a_later_day_owes_every_month_begun():
    store, dana, _ = facility()
    late = at_local(date(2026, 11, 30), 23, 50)
    rn = checkout(store, dana, "A101", late)
    assert rent(store).rentals[rn].quote["total_cents"] == 333 + 2500     # one day of November and the admin fee
    pay(store, dana, rn, 2833, late)
    after_midnight = at_local(date(2026, 12, 1), 0, 5)
    accept(store, dana, rn, after_midnight)
    r = rent(store)
    assert r.rentals[rn].state == "awaiting_payment" and "$100.00 short" in next(t for t in r.tasks.values() if t.about == rn).reason
    pay(store, dana, rn, 10000, at_local(date(2026, 12, 1), 0, 7))
    r, b = rent(store), books(store)
    lease = r.rentals[rn].lease
    assert r.rentals[rn].state == "active" and b.leases[lease].start == date(2026, 11, 30)
    assert sorted((c.obligation, c.cents) for c in b.tenant_charges(dana)) == [
        (f"admin_fee:{lease}", 2500), (f"rent:{lease}:2026-11", 333), (f"rent:{lease}:2026-12", 10000)]
    assert billing(store, lambda led: led.post_rent("2026-12")) == []      # the 1st's run does not bill December again


# move-outs ----------------------------------------------------------------------------------------------------------------
def test_the_final_bill_is_exact_to_the_cent_on_eastern_days():
    store, dana, eli = facility()
    oct10 = at_local(date(2026, 10, 10), 10)
    paid, unpaid = rented(store, dana, "A101", oct10), rented(store, eli, "A102", oct10)
    billing(store, lambda led: led.post_rent("2026-11"))
    billing(store, lambda led: led.receive_payment(dana, 10000, at_local(date(2026, 11, 2), 9)))
    billing(store, lambda led: led.assess_late_fees("2026-11"))                  # Eli's November is late: $20.00
    late_night = datetime(2026, 11, 21, 4, 30, tzinfo=timezone.utc)              # 11:30 PM on Nov 20 in Orlando
    r = rent(store)
    mo_paid, mo_unpaid = move_out(store, paid, late_night), move_out(store, unpaid, late_night)
    r, b = rent(store), books(store)
    bill = r.moveouts[mo_paid].bill
    assert bill["last_day"] == "2026-11-20"
    assert bill["rent"] == [{"month": "2026-11", "billed_cents": 10000, "kept_cents": 6667}]   # 20 of 30 days
    assert (bill["owed_cents"], bill["credit_cents"], bill["refund_cents"], bill["refund"]) == (0, 0, 3333, "pending")
    assert b.refunds[dana] == 3333 and b.credit.get(dana, 0) == 0     # owed back on the books, not paid out, not spendable
    other = r.moveouts[mo_unpaid].bill
    assert other["rent"] == [{"month": "2026-11", "billed_cents": 9000, "kept_cents": 6000}]
    assert (other["owed_cents"], other["refund_cents"], other["refund"]) == (6000 + 2000, 0, "none")
    assert b.leases[r.rentals[paid].lease].ended == date(2026, 11, 20)
    assert billing(store, lambda led: led.post_rent("2026-12")) == []


def test_a_unit_is_not_rentable_until_its_old_code_is_revoked():
    store, dana, eli = facility()
    system = StandIn()
    rn = rented(store, dana, "A101")
    advance(store, BASE, system, NOW)
    access = rent(store).rentals[rn].access
    assert rent(store).access[access].state == "verified"
    later = NOW + timedelta(days=3)
    mo = move_out(store, rn, later)
    assert rent(store).status("A101", later) == "blocked"
    assert go(store, act("reserve", "renter", "A101", eli), later).check == "the unit is blocked"
    system.crash_before.add("revoke")
    with pytest.raises(Crash):
        advance(store, BASE, system, later)
    assert rent(store).status("A101", later) == "blocked"
    advance(store, BASE, system, later)
    r = rent(store)
    assert r.access[access].state == "revoked" and r.moveouts[mo].state == "completed" and r.status("A101", later) == "vacant"
    assert ok(store, act("reserve", "renter", "A101", eli), later)


def test_a_final_bill_waits_while_a_rent_charge_it_would_change_is_held():
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    charge = books(store).tenant_charges(dana)[0].id
    billing(store, lambda led: led.open_dispute(NOW, charge, "I was told November was free", True, "staff:manager"))
    mo = ok(store, act("request_move_out", "staff:manager", rn), NOW + timedelta(days=1))
    ok(store, act("review_move_out", "staff:manager", mo), NOW + timedelta(days=1))
    assert "held under a dispute" in go(store, act("final_bill", "staff:manager", mo), NOW + timedelta(days=1)).check
    billing(store, lambda led: led.decide_dispute(NOW + timedelta(days=1), list(led.books.disputes)[0], "charge stands"))
    assert ok(store, act("final_bill", "staff:manager", mo), NOW + timedelta(days=1)) is None


# transfers ----------------------------------------------------------------------------------------------------------------
NOV11 = NOW + timedelta(days=1)


def half_done_transfer(store, dana, system):
    """Dana moves from A101 to B201 on Nov 11; the move is billed and B201's lease starts, but B201's new code fails its
    read-back, so the transfer waits for review with the old unit and code still Dana's."""
    old = rented(store, dana, "A101")
    advance(store, BASE, system, NOW)
    tr = ok(store, act("request_transfer", "renter", old, dana, unit="B201"), NOV11)
    ok(store, act("approve_transfer", "staff:manager", tr, until=(NOV11 + timedelta(days=2)).isoformat()), NOV11)
    new = rent(store).transfers[tr].target
    billing(store, lambda led: led.receive_payment(dana, 3333, NOV11))
    assert accept(store, dana, new, NOV11).allowed
    ok(store, act("complete_transfer", "staff:manager", tr), NOV11)
    system.hide.add("unit B201")
    advance(store, BASE, system, NOV11)
    system.hide.clear()
    return old, new, tr


def test_the_new_unit_of_an_unfinished_transfer_cannot_move_out_or_move_again():
    store, dana, _ = facility()
    old, new, tr = half_done_transfer(store, dana, StandIn())
    assert rent(store).transfers[tr].state == "review" and rent(store).rentals[new].state == "active"
    for a in (act("request_move_out", "staff:manager", new), act("request_transfer", "staff:manager", new, unit="A102"),
              act("request_move_out", "renter", new, dana)):
        assert "transfer or a move-out under way" in go(store, a, NOV11).check
    assert rent(store).access[rent(store).rentals[old].access].state == "verified"     # the old code stays until the move ends


def test_a_transfer_keeps_the_old_unit_and_code_until_the_new_code_works():
    store, dana, _ = facility()
    system = StandIn()
    old = rented(store, dana, "A101")                       # $70.00 for Nov 10 to 30 at $100.00 a month
    advance(store, BASE, system, NOW)
    nov11 = NOV11
    tr = ok(store, act("request_transfer", "renter", old, dana, unit="B201"), nov11)
    ok(store, act("approve_transfer", "staff:manager", tr, until=(nov11 + timedelta(days=2)).isoformat()), nov11)
    new = rent(store).transfers[tr].target
    assert accept(store, dana, new, nov11).allowed
    # the old unit's unused days (Nov 11 to 30) come back as credit, $66.67, and B201 from Nov 11 is $100.00
    assert "$33.33 short" in go(store, act("complete_transfer", "staff:manager", tr), nov11).check
    billing(store, lambda led: led.receive_payment(dana, 3333, nov11))
    assert "amount to pay changed" in go(store, act("complete_transfer", "staff:manager", tr), nov11).check
    assert accept(store, dana, new, nov11).allowed
    ok(store, act("complete_transfer", "staff:manager", tr), nov11)
    r, b = rent(store), books(store)
    assert b.receivable(r.rentals[new].lease) == 0 and b.credit.get(dana, 0) == 0
    assert b.leases[r.rentals[old].lease].ended == date(2026, 11, 10)
    started = b.decisions[f"{new}:activate"]                # the staff completed the transfer; code started the new rental
    assert started["source"] == "code" and started["allowed"]

    system.hide.add("unit B201")                            # the new code's unit group does not show on read-back
    advance(store, BASE, system, nov11)
    r = rent(store)
    old_access, new_access = r.rentals[old].access, r.rentals[new].access
    assert r.access[new_access].state == "failed" and r.transfers[tr].state == "review"
    assert r.access[old_access].state == "verified" and r.status("A101", nov11) == "occupied"   # the old code still works
    assert r.rentals[old].state == "ending" and r.rentals[new].state == "active"

    system.hide.clear()
    ok(store, act("retry_access", "staff:manager", new_access), nov11)
    advance(store, BASE, system, nov11)
    r = rent(store)
    assert r.access[new_access].state == "verified" and r.transfers[tr].state == "completed"
    assert r.access[old_access].state == "revoked" and r.rentals[old].state == "ended"
    assert r.status("A101", nov11) == "vacant" and r.status("B201", nov11) == "occupied"
    kinds = [(e.kind, e.payload.get("access")) for e in store.events()]
    assert kinds.index(("access.read", new_access), kinds.index(("access.retry", new_access))) < kinds.index(("access.revoke_requested", old_access))


# gate codes ---------------------------------------------------------------------------------------------------------------
def test_a_failed_read_back_leaves_the_rental_active_with_a_dated_task():
    store, dana, _ = facility()
    system = StandIn(hide={ACCESS_LEVEL})
    rn = rented(store, dana, "A101")
    advance(store, BASE, system, NOW)
    r = rent(store)
    a = r.access[r.rentals[rn].access]
    assert r.rentals[rn].state == "active" and a.state == "failed" and r.status("A101", NOW) == "occupied"
    task = next(t for t in r.tasks.values() if t.about == a.id)
    assert task.kind == "access" and task.due == date(2026, 11, 10) and task.open
    system.hide.clear()
    ok(store, act("retry_access", "staff:front_desk", a.id))
    advance(store, BASE, system, NOW)
    r = rent(store)
    assert r.access[a.id].state == "verified" and not r.tasks[task.id].open
    assert system.calls.count(f"{copy_mark(store, BASE)}:{a.id}:pin") == 1


def test_the_code_itself_is_never_written_down():
    store, dana, _ = facility()
    system = StandIn()
    rented(store, dana, "A101")
    advance(store, BASE, system, NOW)
    code = next(u["code"] for u in system.users.values())
    assert all(code not in str(e.payload) for e in store.events())


@pytest.mark.parametrize("where", ["crash_before", "crash_after"])
@pytest.mark.parametrize("step", [*STEPS, "revoke"])
def test_a_restart_at_any_step_finishes_once_and_never_calls_twice(step, where, tmp_path):
    path = str(tmp_path / "facility.db")
    store, dana, _ = facility(path)
    system = StandIn(**({where: {step}} if step != "revoke" else {}))
    rn = rented(store, dana, "A101")
    if step == "revoke":
        advance(store, BASE, system, NOW)
        move_out(store, rn, NOW)
        getattr(system, where).add(step)
    with pytest.raises(Crash):
        advance(store, BASE, system, NOW)
    store = Store(path)                                     # the restart: a new process reads everything from the database
    advance(store, BASE, system, NOW)
    r = rent(store)
    a = r.access[r.rentals[rn].access]
    assert a.state == ("revoked" if step == "revoke" else "verified")
    assert len(system.calls) == len(set(system.calls))      # no outside call was made twice
    op = r.ops[f"{a.id}:{step}"]
    # taken up twice; after a lost answer the second take found the first call's result instead of calling again
    assert (op.how, op.attempts) == ("found" if where == "crash_after" else "response", 2)
    events, calls = len(store.events()), list(system.calls)
    advance(store, BASE, system, NOW)                       # and running it again does nothing more
    assert len(store.events()) == events and system.calls == calls


class MoveOutMidCall(StandIn):
    """Staff finish a move-out in another process while an outside call for the code is still out."""

    def __init__(self, store, rental, step):
        super().__init__()
        self.store, self.rental, self.step = store, rental, step

    def _move_out_once(self, step):
        if step == self.step:
            self.step = None
            move_out(self.store, self.rental, NOW)

    def issue_pin(self, key, user):
        self._move_out_once("pin")
        return super().issue_pin(key, user)

    def read_back(self, user):
        self._move_out_once("read")
        return super().read_back(user)


@pytest.mark.parametrize("step", ["pin", "read"])
def test_a_move_out_during_a_code_call_still_revokes_the_code(step):
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    system = MoveOutMidCall(store, rn, step)
    advance(store, BASE, system, NOW)
    r = rent(store)
    a = r.access[r.rentals[rn].access]
    assert a.state == "revoked" and system.users[a.user]["code"] is None and not system.users[a.user]["groups"]
    assert r.status("A101", NOW) == "vacant"


def test_revoking_a_code_whose_user_answer_was_lost_looks_it_up_first():
    store, dana, _ = facility()
    system = StandIn(crash_after={"user"})
    rn = rented(store, dana, "A101")
    with pytest.raises(Crash):
        advance(store, BASE, system, NOW)
    move_out(store, rn, NOW)                                # the move-out comes before the code was finished
    advance(store, BASE, system, NOW)
    r = rent(store)
    a = r.access[r.rentals[rn].access]
    assert a.state == "revoked" and r.ops[f"{a.id}:user"].how == "found"
    assert [k.rsplit(":", 1)[1] for k in system.calls] == ["user", "revoke"]


# the waitlist ---------------------------------------------------------------------------------------------------------------
def test_the_waitlist_offers_a_unit_for_a_bounded_time_and_never_charges():
    store, dana, eli = facility()
    before = len(store.events())
    first = ok(store, act("join_waitlist", "operator", "5x10", eli))
    ok(store, act("offer_unit", "staff:manager", first, unit="A101", until=(NOW + timedelta(hours=1)).isoformat()))
    assert rent(store).status("A101", NOW) == "reserved"
    assert not go(store, act("reserve", "renter", "A101", dana)).allowed
    later = NOW + timedelta(hours=1)
    assert rent(store).status("A101", later) == "vacant"
    second = ok(store, act("join_waitlist", "renter", "5x10", dana), later)
    assert rent(store).entries[first].state == "expired"
    ok(store, act("offer_unit", "staff:manager", second, unit="A101", until=(later + timedelta(hours=2)).isoformat()), later)
    rs = ok(store, act("accept_offer", "renter", second, dana), later)
    r = rent(store)
    assert r.reservations[rs].expires_at == later + timedelta(hours=2) and r.status("A101", later) == "reserved"
    assert not [e for e in store.events()[before:] if e.kind.startswith(("charge.", "payment.", "credit."))]


def test_a_paid_hold_stays_until_the_renter_accepts_or_staff_refund_and_release():
    store, dana, eli = facility()
    rn = checkout(store, dana, "A101")
    pay(store, dana, rn, 9500)                              # paid, agreement not accepted
    later = NOW + timedelta(hours=2)
    assert rent(store).status("A101", later) == "reserved"
    assert not go(store, act("reserve", "renter", "A101", eli), later).allowed
    task = next(t for t in rent(store).tasks.values() if t.about == rn)
    assert task.kind == "hold" and task.due == date(2026, 11, 10)
    assert "the renter accepts the agreement" in task.reason and "staff refund the payment and release" in task.reason
    assert accept(store, dana, rn, later).allowed and rent(store).rentals[rn].state == "active"
    assert not rent(store).tasks[task.id].open

    other = checkout(store, eli, "A102")
    rs = rent(store).rentals[other].reservation
    pay(store, eli, other, 8800)
    assert "staff refund" in go(store, act("cancel_reservation", "renter", rs, eli), later).check
    ok(store, act("cancel_reservation", "staff:manager", rs), later)
    r = rent(store)
    assert r.rentals[other].refund == {"cents": 8800, "state": "pending"} and r.status("A102", later) == "vacant"
    assert any(t.kind == "refund" and "$88.00" in t.reason for t in r.tasks.values())
    assert books(store).refunds[eli] == 8800 and books(store).credit.get(eli, 0) == 0     # owed back, not paid out


def test_a_pending_refund_is_set_aside_and_never_spent_twice():
    store, dana, _ = facility()
    first = checkout(store, dana, "A101")
    pay(store, dana, first, 9500)
    ok(store, act("cancel_reservation", "staff:manager", rent(store).rentals[first].reservation))
    assert rent(store).rentals[first].refund == {"cents": 9500, "state": "pending"}
    second = checkout(store, dana, "A102")                  # $88.00 due; only $1.00 more is paid
    pay(store, dana, second, 100)
    accept(store, dana, second)
    assert rent(store).rentals[second].state == "awaiting_payment"      # the money promised back does not start it
    ok(store, act("cancel_reservation", "staff:manager", rent(store).rentals[second].reservation))
    refunds = [r.refund["cents"] for r in rent(store).rentals.values() if r.refund]
    assert refunds == [9500, 100]
    b = books(store)
    assert b.credit.get(dana, 0) == 0 and b.refunds[dana] == sum(refunds)
    assert sum(c for _, a, c, _ in store.journal() if a == f"refund:{dana}") == -sum(refunds)   # owed back, not paid out


def test_a_payment_whose_credit_is_set_aside_for_a_refund_is_not_returned_silently():
    from core.billing import Refused
    store, dana, _ = facility()
    rn = checkout(store, dana, "A101")
    pay(store, dana, rn, 9500)
    ok(store, act("cancel_reservation", "staff:manager", rent(store).rentals[rn].reservation))
    pid = rent(store).rentals[rn].payments[0]
    with pytest.raises(Refused, match="set aside for a refund"):
        billing(store, lambda led: led.return_payment(NOW + timedelta(days=1), pid))
    assert books(store).refunds[dana] == 9500 and not books(store).payments[pid].returned


def test_a_refund_draws_only_on_the_payments_given_back_and_once_per_key():
    store, dana, _ = facility()
    rn = checkout(store, dana, "A101")
    pay(store, dana, rn, 9500)
    billing(store, lambda led: led.sign_lease(NOW, dana, "A102", 9000, [ADMIN_FEE]))   # its $88.00 is paid from the rental's money
    billing(store, lambda led: led.receive_payment(dana, 5000, NOW))                    # money that has nothing to do with the rental
    ok(store, act("cancel_reservation", "staff:manager", rent(store).rentals[rn].reservation))
    assert rent(store).rentals[rn].refund == {"cents": 700, "state": "pending"}
    assert books(store).credit[dana] == 5000
    assert billing(store, lambda led: led.reserve_refund(NOW, dana, 5000, f"refund:{rn}")) == 0     # one refund per key
    assert books(store).refunds[dana] == 700


def test_a_late_payment_is_set_aside_only_when_it_is_this_tenants_and_unused():
    store, dana, eli = facility()
    rn = checkout(store, dana, "A101")
    later = NOW + timedelta(hours=1)
    advance(store, BASE, StandIn(), later)                  # the hold runs out unpaid
    elis = billing(store, lambda led: led.receive_payment(eli, 9500, later))
    other = rented(store, dana, "A102", later)
    linked = rent(store).rentals[other].payments[0]         # already recorded for Dana's other rental
    for pid in (elis, linked):
        assert not go(store, act("confirm_payment", "code", rn, payment=pid), later).allowed
    assert not books(store).refunds and not any(t.kind == "refund" for t in rent(store).tasks.values())
    assert books(store).credit[eli] == 9500


def test_staff_can_cancel_a_hold_after_checkout_began_unless_a_payment_is_out():
    store, dana, _ = facility()
    until = (NOW + timedelta(days=3)).isoformat()
    rs = ok(store, act("reserve", "staff:front_desk", "A101", dana, until=until))
    rn = ok(store, act("start_checkout", "staff:front_desk", rs))
    checkout_intent(store, BASE, rn, NOW)
    assert "still unconfirmed" in go(store, act("cancel_reservation", "staff:manager", rs)).check
    ok(store, act("payment_failed", "code", rn))
    ok(store, act("cancel_reservation", "staff:manager", rs))
    r = rent(store)
    assert r.rentals[rn].state == "cancelled" and r.reservations[rs].state == "cancelled" and r.status("A101", NOW) == "vacant"


def test_a_repeated_command_returns_its_first_decision_and_writes_nothing():
    store, dana, _ = facility()
    first = [act("reserve", "renter", "A101", dana)]
    rs = ok(store, first[0])
    first.append(act("start_checkout", "renter", rs, dana))
    rn = ok(store, first[1])
    pay(store, dana, rn, 9500)
    accept(store, dana, rn)
    tr = act("request_transfer", "renter", rn, dana, unit="B201")
    first.append(tr)
    mo = act("request_move_out", "staff:manager", rn)
    first.append(mo)
    made = [rs, rn, ok(store, tr), None]
    assert not go(store, mo).allowed                       # a transfer is under way
    events = len(store.events())
    for a, m in zip(first, made):
        again = go(store, a, NOW + timedelta(minutes=1))
        assert again.repeated and again.made == m
    assert len(store.events()) == events


def test_only_staff_record_a_move_out_day_that_has_passed():
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    later = NOW + timedelta(days=5)
    assert "only staff" in go(store, act("request_move_out", "renter", rn, dana, on="2026-11-12"), later).check
    assert ok(store, act("request_move_out", "staff:manager", rn, on="2026-11-12"), later)


def test_a_visitor_rents_a_vacant_unit_over_the_seeded_year():
    from core.seed import NOW as SEEDED, build
    store, stream = Store(), "visitor:renting"
    with store.write():
        store.append(BASE, build().led.pending)
    b, r = load(store, stream)
    status = {u: r.status(u, SEEDED) for u in b.units}
    occupied, vacant = [u for u, s in status.items() if s == "occupied"], [u for u, s in status.items() if s == "vacant"]
    assert occupied and vacant                              # the seeded leases hold their units without any renting record
    for u in (occupied[0], vacant[0]):
        assert run(store, stream, Act(f"v-price-{u}", "price_unit", "staff:manager", u, args={"rent_cents": 12000, "size": "5x10"}), SEEDED).allowed
    with store.write():
        led = Ledger(store.books(stream))
        newcomer = led.add_tenant(SEEDED, "Rae Quinn", "+14075550199")
        store.append(stream, led.pending)
    assert run(store, stream, Act("v-occ", "reserve", "renter", occupied[0], newcomer), SEEDED).check == "the unit is occupied"
    rs = run(store, stream, Act("v-res", "reserve", "renter", vacant[0], newcomer), SEEDED).made
    rn = run(store, stream, Act("v-co", "start_checkout", "renter", rs, newcomer), SEEDED).made
    checkout_intent(store, stream, rn, SEEDED)
    with store.write():
        led = Ledger(store.books(stream))
        pid = led.receive_payment(newcomer, load(store, stream)[1].rentals[rn].quote["total_cents"], SEEDED)
        store.append(stream, led.pending)
    assert run(store, stream, Act("v-pay", "confirm_payment", "code", rn, args={"payment": pid}), SEEDED).allowed
    sha = load(store, stream)[1].agreements[load(store, stream)[1].rentals[rn].agreement].sha256
    assert run(store, stream, Act("v-acc", "accept_agreement", "renter", rn, newcomer, {"sha256": sha}), SEEDED).allowed
    assert load(store, stream)[1].status(vacant[0], SEEDED) == "occupied"
    assert load(store, BASE)[1].status(vacant[0], SEEDED) == "vacant"      # the facility's own stream is untouched


def test_renting_tasks_and_collections_tasks_live_side_by_side_in_one_copy():
    """Both record a task as a dated staff item; each is folded by its own records only."""
    from core import delinquency
    from core.seed import build
    day = date(2026, 12, 3)
    store, stream = Store(), "visitor:both"
    with store.write():
        store.append(BASE, build(today=day, cutoff=at_local(day), standing=True).led.pending)
    b, r = load(store, stream)
    assert delinquency.open_tasks(b) and not r.tasks               # the breached plan's task is collections' alone
    with store.write():
        books_, rent_ = load(store, stream)
        led = RentLedger(books_, rent_)
        rent_.task(led, at_local(day, 10), "RN0001", "payment", "A payment is unconfirmed.")
        store.append(stream, led.pending)
    b, r = load(store, stream)
    assert [(t.id, t.kind) for t in r.tasks.values()] == [("RT0001", "payment")]
    assert delinquency.open_tasks(b) and all(t.kind != "payment" for t in delinquency.open_tasks(b))


def test_a_late_payment_that_older_charges_took_in_full_opens_no_refund_task():
    store, dana, eli = facility()
    billing(store, lambda led: led.sign_lease(at_local(date(2026, 11, 1), 9), eli, "A102", 9000, [ADMIN_FEE]))     # $115.00 open
    rn = checkout(store, eli, "B201")
    later = NOW + timedelta(hours=1)
    advance(store, BASE, StandIn(), later)                  # the hold runs out unpaid
    pid = billing(store, lambda led: led.receive_payment(eli, 11500, later))         # all of it pays the older charges
    d = go(store, act("confirm_payment", "code", rn, payment=pid), later)
    assert not d.allowed and "nothing to refund" in d.check and "set aside" not in d.check          # the record says what happened
    assert books(store).credit.get(eli, 0) == 0 and not books(store).refunds
    assert not any(t.kind == "refund" for t in rent(store).tasks.values())


def test_a_visitor_rents_a_vacant_unit_on_the_live_copy_without_anyone_pricing_it():
    from core.seed import build
    day = date(2026, 12, 3)
    store, stream = Store(), "visitor:live"
    with store.write():
        store.append(BASE, build(today=day, cutoff=at_local(day), standing=True).led.pending)
    now = at_local(day, 10)
    b, r = load(store, stream)
    assert all(r.units[u].rent_cents and r.units[u].size for u in b.units)
    vacant = next(u for u in b.units if r.status(u, now) == "vacant")
    with store.write():
        led = Ledger(store.books(stream))
        newcomer = led.add_tenant(now, "Rae Quinn", "+14075550199")
        store.append(stream, led.pending)
    rs = run(store, stream, Act("v-res", "reserve", "renter", vacant, newcomer), now)
    assert rs.allowed, rs.check
    rn = run(store, stream, Act("v-co", "start_checkout", "renter", rs.made, newcomer), now).made
    assert load(store, stream)[1].rentals[rn].quote["rent_cents"] == r.units[vacant].rent_cents


# the daily check of gate codes against rentals ----------------------------------------------------------------------------------
def test_the_daily_check_flags_an_active_rental_without_a_verified_code_once():
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")                         # active, its code asked for and not yet issued
    access = rent(store).rentals[rn].access
    assert check_codes(store, BASE, NOW) == 1
    (task,) = [t for t in rent(store).tasks.values() if t.open]
    assert (task.about, task.kind, task.due, task.role) == (access, "access", date(2026, 11, 10), "front_desk")
    events = len(store.events())
    assert check_codes(store, BASE, NOW + timedelta(hours=1)) == 0 and len(store.events()) == events      # nothing new, nothing written
    advance(store, BASE, StandIn(), NOW)                     # the code is issued and read back: the task closes with it
    assert not any(t.open for t in rent(store).tasks.values())
    events = len(store.events())
    assert check_codes(store, BASE, NOW + timedelta(hours=2)) == 0 and len(store.events()) == events


def test_the_daily_check_adds_nothing_where_a_failed_read_back_already_has_its_task():
    store, dana, _ = facility()
    system = StandIn(hide=("unit A101",))
    rn = rented(store, dana, "A101")
    advance(store, BASE, system, NOW)
    assert rent(store).access[rent(store).rentals[rn].access].state == "failed"
    events = len(store.events())
    assert check_codes(store, BASE, NOW) == 0 and len(store.events()) == events
    assert len([t for t in rent(store).tasks.values() if t.open]) == 1


def test_the_daily_check_flags_a_code_still_standing_for_a_rental_that_ended():
    store, dana, _ = facility()
    system = StandIn()
    rn = rented(store, dana, "A101")
    advance(store, BASE, system, NOW)
    assert check_codes(store, BASE, NOW) == 0                # a verified code on an active rental is fine
    later = NOW + timedelta(days=3)
    move_out(store, rn, later)                               # ended; its revocation has not run
    access = rent(store).rentals[rn].access
    assert rent(store).access[access].state == "revocation_pending"
    assert check_codes(store, BASE, later) == 1
    (task,) = [t for t in rent(store).tasks.values() if t.open and t.kind != "refund"]     # (the final bill's credit has its own refund task)
    assert (task.about, task.kind, task.due) == (access, "revoke", date(2026, 11, 13))
    advance(store, BASE, system, later)                      # revoked: the task closes with it
    assert not any(t.open and t.kind != "refund" for t in rent(store).tasks.values()) and check_codes(store, BASE, later) == 0


# the stand-in gate, shared by every copy and kept across restarts -------------------------------------------------------------
def visitor_rental(store, stream, unit, name):
    """An active rental on a visitor copy of the facility, its code not issued yet."""
    with store.write():
        led = Ledger(store.books(stream))
        tenant = led.add_tenant(NOW, name, "+14075550199")
        store.append(stream, led.pending)
    rs = run(store, stream, Act(f"{stream}-res", "reserve", "renter", unit, tenant), NOW).made
    rn = run(store, stream, Act(f"{stream}-co", "start_checkout", "renter", rs, tenant), NOW).made
    checkout_intent(store, stream, rn, NOW)
    with store.write():
        led = Ledger(store.books(stream))
        pid = led.receive_payment(tenant, load(store, stream)[1].rentals[rn].quote["total_cents"], NOW)
        store.append(stream, led.pending)
    assert run(store, stream, Act(f"{stream}-pay", "confirm_payment", "code", rn, args={"payment": pid}), NOW).allowed
    sha = load(store, stream)[1].agreements[load(store, stream)[1].rentals[rn].agreement].sha256
    assert run(store, stream, Act(f"{stream}-acc", "accept_agreement", "renter", rn, tenant, {"sha256": sha}), NOW).allowed
    return rn


def test_a_stand_in_that_keeps_its_records_survives_a_restart_without_issuing_twice(tmp_path):
    path = str(tmp_path / "access.json")
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    with pytest.raises(Crash):
        advance(store, BASE, StandIn(crash_after={"pin"}, path=path), NOW)      # the PIN was issued, the answer was lost
    again = StandIn(path=path)                                                  # the process restarted: it reads the file
    advance(store, BASE, again, NOW)
    r = rent(store)
    assert r.access[r.rentals[rn].access].state == "verified"
    assert [k.rsplit(":", 1)[1] for k in again.calls] == list(STEPS)           # each step made once, across the restart


def test_one_stand_in_serves_two_copies_without_mixing_their_codes_and_forgets_a_reset_copy(tmp_path):
    store, _, _ = facility()
    system = StandIn(path=str(tmp_path / "access.json"))
    a, b = visitor_rental(store, "visitor:a", "A101", "Rae Quinn"), visitor_rental(store, "visitor:b", "A102", "Sam Ito")
    advance(store, "visitor:a", system, NOW)
    advance(store, "visitor:b", system, NOW)
    ra, rb = load(store, "visitor:a")[1], load(store, "visitor:b")[1]
    xa, xb = ra.access[ra.rentals[a].access], rb.access[rb.rentals[b].access]
    assert xa.id == xb.id and xa.state == xb.state == "verified" and xa.user != xb.user and len(system.users) == 2
    assert xa.simulated and xb.simulated                                       # the record says the code is the stand-in's
    mark = copy_mark(store, "visitor:a") + ":"
    system.forget(mark)
    assert system.read_back(xa.user) == {"code": None, "groups": []} and system.read_back(xb.user)["code"]
    assert not any(k.startswith(mark) for k in system.done) and any(k.startswith(copy_mark(store, "visitor:b")) for k in system.done)
    assert StandIn(path=system.path).users.keys() == system.users.keys()       # the forgetting was kept


def test_a_copy_that_comes_back_under_the_same_name_never_finds_the_earlier_copys_answers():
    store, system = facility()[0], StandIn()
    visitor_rental(store, "visitor:a", "A101", "Rae Quinn")
    advance(store, "visitor:a", system, NOW)                    # the first copy's code is made
    store.drop_stream("visitor:a")                              # a reset, or a new day: the name comes back, access ids start again
    rn = visitor_rental(store, "visitor:a", "A101", "Sam Ito")
    system.crash_before.add("user")
    with pytest.raises(Crash):
        advance(store, "visitor:a", system, NOW)                # stopped before this copy's own first call was made
    advance(store, "visitor:a", system, NOW)                    # the lookup must find nothing (not the first copy's user) and make the call
    r = load(store, "visitor:a")[1]
    a = r.access[r.rentals[rn].access]
    assert a.state == "verified" and a.user == "user-2" and system.users["user-2"]["name"].startswith("Sam Ito")


# review round: refusals instead of errors ---------------------------------------------------------------------------------
def test_a_negative_refund_is_refused_before_any_credit_is_drawn():
    from core.billing import Refused
    store, dana, _ = facility()
    billing(store, lambda led: led.receive_payment(dana, 5000, NOW))
    with pytest.raises(Refused, match="cannot be negative"):
        billing(store, lambda led: led.reserve_refund(NOW, dana, -100, "refund:x"))
    assert books(store).credit[dana] == 5000 and not books(store).refunds
    assert billing(store, lambda led: led.reserve_refund(NOW, dana, 0, "refund:x")) == 0         # zero stays a no-op
    assert billing(store, lambda led: led.reserve_refund(NOW, dana, 9000, "refund:x")) == 5000   # positive stays capped by the credit


def test_a_repeat_of_an_accepted_command_under_a_new_id_is_a_refusal_never_an_error():
    store, dana, eli = facility()
    system, seen = StandIn(), []

    def step(action, source, about, tenant=None, now=NOW, **args):
        a = act(action, source, about, tenant, **args)
        seen.append(a)
        return ok(store, a, now)
    rs = step("reserve", "renter", "A101", dana)
    rn = step("start_checkout", "renter", rs, dana)
    sha = rent(store).agreements[rent(store).rentals[rn].agreement].sha256
    step("accept_agreement", "renter", rn, dana, sha256=sha)
    twice = go(store, act("accept_agreement", "renter", rn, dana, sha256=sha))          # still waiting for the payment: the same acceptance again
    assert not twice.allowed and "already accepted" in twice.check
    checkout_intent(store, BASE, rn, NOW)
    pid = billing(store, lambda led: led.receive_payment(dana, rent(store).rentals[rn].quote["total_cents"], NOW))
    step("confirm_payment", "code", rn, payment=pid)
    assert rent(store).rentals[rn].state == "active"
    advance(store, BASE, system, NOW)
    nov11 = NOV11
    tr = step("request_transfer", "renter", rn, dana, now=nov11, unit="B201")
    step("approve_transfer", "staff:manager", tr, now=nov11, until=(nov11 + timedelta(days=2)).isoformat())
    new = rent(store).transfers[tr].target
    billing(store, lambda led: led.receive_payment(dana, 3333, nov11))
    step("accept_agreement", "renter", new, dana, now=nov11, sha256=rent(store).agreements[rent(store).rentals[new].agreement].sha256, amount_cents=0)
    step("complete_transfer", "staff:manager", tr, now=nov11)
    advance(store, BASE, system, nov11)                 # the new code is read back: the transfer is complete and the old code goes
    mo = step("request_move_out", "renter", new, dana, now=nov11)
    step("review_move_out", "staff:manager", mo, now=nov11)
    step("final_bill", "staff:manager", mo, now=nov11)
    entry = step("join_waitlist", "renter", "5x10", eli, now=nov11)
    step("offer_unit", "staff:manager", entry, now=nov11, unit="A102", until=(nov11 + timedelta(hours=1)).isoformat())
    step("accept_offer", "renter", entry, eli, now=nov11)
    for n, a in enumerate(seen):
        if a.action in ("reserve", "join_waitlist", "request_transfer", "request_move_out"):
            continue                                    # these start something new and are allowed to be asked again
        again = Act(f"again-{n}", a.action, a.source, a.about, a.tenant, a.args)
        d = go(store, again, nov11)
        assert not d.allowed and d.check, (a.action, d.check)


def test_dates_that_are_not_dates_are_gate_refusals_never_errors():
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    advance(store, BASE, StandIn(), NOW)
    naive = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    for action, source, about, tenant, args in [
            ("reserve", "staff:manager", "A102", dana, {"start": "invalid"}),
            ("reserve", "staff:manager", "A102", dana, {"until": "invalid"}),
            ("reserve", "staff:manager", "A102", dana, {"until": naive}),
            ("reserve", "staff:manager", "A102", dana, {"until": 5}),
            ("reserve", "staff:manager", "A102", dana, {"start": None}),
            ("request_transfer", "staff:manager", rn, None, {"unit": "B201", "start": "2026-13-45"}),
            ("request_transfer", "staff:manager", rn, None, {"unit": "B201", "until": naive}),
            ("request_move_out", "staff:manager", rn, None, {"on": "someday"}),
            ("request_move_out", "renter", rn, dana, {"on": 20261111})]:
        d = go(store, act(action, source, about, tenant, **args))
        assert not d.allowed and ("date" in d.check or "time" in d.check), (action, args, d.check)
    tr = ok(store, act("request_transfer", "renter", rn, dana, unit="B201"))
    assert not go(store, act("approve_transfer", "staff:manager", tr, until="soon")).allowed
    assert not go(store, act("approve_transfer", "staff:manager", tr, until=naive)).allowed
    entry = ok(store, act("join_waitlist", "renter", "5x10", rent(store).rentals[rn].tenant))
    assert not go(store, act("offer_unit", "staff:manager", entry, unit="A102", until="later")).allowed
    assert not go(store, act("offer_unit", "staff:manager", entry, unit="A102", until=naive)).allowed


def test_a_rent_charge_posted_by_hand_with_no_month_is_left_as_billed_and_listed_in_the_final_bill():
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    lease = rent(store).rentals[rn].lease
    extra = billing(store, lambda led: led.charge_by_hand(NOW, lease, RENT, 5000, "manual-rent"))
    move_out(store, rn, NOV11)                                  # the final bill must not stop on a charge that names no month
    b = books(store)
    assert not b.charges[extra].closed and b.charges[extra].cents == 5000 and b.leases[lease].ended == date(2026, 11, 11)
    (billed,) = [e for e in store.events() if e.kind == "moveout.billed"]
    assert billed.payload["bill"]["untouched_rent"] == [{"charge": extra, "cents": 5000}]


def test_a_transfer_leaves_a_rent_charge_with_no_month_on_the_old_lease():
    store, dana, _ = facility()
    system = StandIn()
    old = rented(store, dana, "A101")
    lease = rent(store).rentals[old].lease
    extra = billing(store, lambda led: led.charge_by_hand(NOW, lease, RENT, 5000, "manual-rent"))
    advance(store, BASE, system, NOW)
    tr = ok(store, act("request_transfer", "renter", old, dana, unit="B201"), NOV11)
    ok(store, act("approve_transfer", "staff:manager", tr, until=(NOV11 + timedelta(days=2)).isoformat()), NOV11)
    new = rent(store).transfers[tr].target
    billing(store, lambda led: led.receive_payment(dana, 3333 + 5000, NOV11))
    assert accept(store, dana, new, NOV11).allowed
    ok(store, act("complete_transfer", "staff:manager", tr), NOV11)
    assert not books(store).charges[extra].closed and rent(store).rentals[new].state == "active"


def test_a_held_rent_charge_does_not_block_a_final_bill_that_leaves_it_as_billed():
    """The tenant leaves on the last day of the month the held charge covers, so the final bill changes none of it."""
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    charge = books(store).tenant_charges(dana)[0].id
    billing(store, lambda led: led.open_dispute(NOW, charge, "I was told November was free", True, "staff:manager"))
    last = NOW + timedelta(days=20)                             # November 30
    mo = ok(store, act("request_move_out", "staff:manager", rn, on="2026-11-30"), NOW)
    ok(store, act("review_move_out", "staff:manager", mo), NOW)
    assert ok(store, act("final_bill", "staff:manager", mo), last) is None
    b = books(store)
    assert not b.charges[charge].closed and b.charges[charge].cents == 7000 and b.leases[rent(store).rentals[rn].lease].ended == date(2026, 11, 30)


# review round: refunds, one task per refund, and the way one is completed -----------------------------------------------------
def refund_tasks(store):
    return [(t.about, t.kind, t.key, t.reason) for t in rent(store).tasks.values() if t.open and t.kind == "refund"]


def test_each_late_payment_after_a_cancelled_rental_gets_its_own_refund_task():
    store, dana, _ = facility()
    rn = checkout(store, dana, "A101")
    later = NOW + timedelta(hours=1)
    advance(store, BASE, StandIn(), later)                            # the hold ran out: the rental is cancelled
    for n in (1, 2):
        pid = billing(store, lambda led: led.receive_payment(dana, 5000, later))
        assert not go(store, act("confirm_payment", "code", rn, payment=pid), later).allowed
    tasks = refund_tasks(store)
    assert len(tasks) == 2 and len({k for _, _, k, _ in tasks}) == 2 and all("$50.00" in why for *_, why in tasks)
    assert books(store).refunds[dana] == 10000
    # a task opened before tasks carried a key still folds, with no key
    from core.renting import Rent
    old = Rent(books(store))
    old.apply("rent_task.opened", later, {"task": "RT9", "about": rn, "kind": "refund", "reason": "old", "due": "2026-11-10", "role": "front_desk"})
    assert old.tasks["RT9"].key is None


def test_a_final_bill_that_sets_credit_aside_opens_a_refund_task_and_staff_complete_it():
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")                                    # $70.00 paid for Nov 10 to 30
    mo = move_out(store, rn, NOV11)                                     # two days used: the rest is credit, set aside as a refund
    b = books(store)
    owed = b.refunds[dana]
    assert owed > 0 and b.credit.get(dana, 0) == 0
    ((about, kind, key, why),) = refund_tasks(store)
    assert about == rn and key == f"refund:{mo}" and f"${owed // 100}.{owed % 100:02d}" in why
    assert not go(store, act("confirm_refund", "renter", key, dana), NOV11).allowed          # only staff say a refund went out
    ok(store, act("confirm_refund", "staff:manager", key), NOV11)
    b = books(store)
    assert b.refunds[dana] == 0 and refund_tasks(store) == [] and b.reserved[key]           # the reserved record stays
    again = go(store, act("confirm_refund", "staff:manager", key), NOV11)
    assert not again.allowed and "already" in again.check
    assert not go(store, act("confirm_refund", "staff:manager", "refund:nothing"), NOV11).allowed


def test_a_final_bill_with_nothing_to_give_back_opens_no_refund_task():
    store, dana, _ = facility()
    rn = rented(store, dana, "A101")
    mo = ok(store, act("request_move_out", "staff:manager", rn, on="2026-11-30"), NOW)
    ok(store, act("review_move_out", "staff:manager", mo), NOW)
    ok(store, act("final_bill", "staff:manager", mo), NOW + timedelta(days=20))
    assert refund_tasks(store) == [] and not books(store).refunds


# review round: gate codes -------------------------------------------------------------------------------------------------------
class FailsFor(StandIn):
    """A stand-in whose user creation fails for one unit's rental, as a persistent fault on one record would."""

    def create_user(self, key, name):
        if "unit A101" in name:
            raise RuntimeError("the access system refuses this one")
        return super().create_user(key, name)


def test_one_failing_gate_code_does_not_stop_the_others_and_is_tried_once_per_pass():
    store, dana, eli = facility()
    first = rented(store, dana, "A101")                                  # its code fails every time
    second = rented(store, eli, "A102")
    system = FailsFor()
    with pytest.raises(RuntimeError, match="refuses this one"):          # the pass ends by reporting the failure
        advance(store, BASE, system, NOW)
    r = rent(store)
    assert r.access[r.rentals[second].access].state == "verified"       # the other rental's code was carried through
    bad = r.access[r.rentals[first].access]
    assert bad.state == "issuing" and r.ops[f"{bad.id}:user"].attempts == 1       # and the failing one was tried once, not again
    with pytest.raises(RuntimeError):
        advance(store, BASE, system, NOW)
    assert rent(store).ops[f"{bad.id}:user"].attempts == 2               # the next pass tries it again


class Stubborn(StandIn):
    """A stand-in whose first revoke answers but leaves the code standing."""

    def revoke(self, key, user):
        if not any(k.endswith(":revoke") for k in self.calls):
            return self._do(key, lambda: {"revoked": user})
        return super().revoke(key, user)


def test_a_staff_retry_after_a_failed_revocation_revokes_again_under_a_new_key_and_reads_back():
    store, dana, _ = facility()
    system = Stubborn()
    rn = rented(store, dana, "A101")
    advance(store, BASE, system, NOW)
    access = rent(store).rentals[rn].access
    later = NOW + timedelta(days=3)
    move_out(store, rn, later)
    advance(store, BASE, system, later)
    r = rent(store)
    user = r.access[access].user
    assert r.access[access].state == "revocation_failed" and system.users[user]["code"]       # revoked on paper, still standing
    assert r.status("A101", later) == "blocked" and any(t.open and t.kind == "revoke" for t in r.tasks.values())     # the unit is not rentable yet
    ok(store, act("retry_access", "staff:manager", access), later)
    system.crash_after.add("revoke")                                     # the retry's call is made and the process stops before the answer
    with pytest.raises(Crash):
        advance(store, BASE, system, later)
    advance(store, BASE, system, later)                                  # after the restart the answer is looked up, not asked again
    r = rent(store)
    revokes = [k for k in system.calls if k.endswith(":revoke")]
    assert r.access[access].state == "revoked" and system.users[user]["code"] is None
    assert len(revokes) == 2 and len(set(revokes)) == 2                  # one stubborn call, one real call under another key
    assert r.status("A101", later) == "vacant" and not any(t.open and t.kind == "revoke" for t in r.tasks.values())


def test_a_rental_reserved_for_a_later_day_gets_no_code_until_that_day_and_a_pass_starts_it():
    store, dana, _ = facility()
    system = StandIn()
    day = NOW + timedelta(days=5)                                         # November 15
    rs = ok(store, act("reserve", "staff:manager", "A101", dana, start="2026-11-15", until=(day + timedelta(days=1)).isoformat()))
    rn = ok(store, act("start_checkout", "staff:manager", rs, dana))
    sha = rent(store).agreements[rent(store).rentals[rn].agreement].sha256
    assert go(store, act("accept_agreement", "renter", rn, dana, sha256=sha)).allowed
    assert pay(store, dana, rn, rent(store).rentals[rn].quote["total_cents"]).allowed
    advance(store, BASE, system, NOW)
    r = rent(store)
    assert r.rentals[rn].state != "active" and r.rentals[rn].lease is None and not r.rentals[rn].access and not system.calls
    assert not [t for t in r.tasks.values() if t.open and t.kind == "activation"]    # waiting for a day is not a problem to hand to staff
    assert not go(store, act("activate", "code", rn)).allowed
    count = len(store.events())
    advance(store, BASE, system, NOW + timedelta(days=2))
    assert len(store.events()) == count                                   # a pass with nothing due writes nothing
    advance(store, BASE, system, day)
    r = rent(store)
    assert r.rentals[rn].state == "active" and r.access[r.rentals[rn].access].state == "verified"
    assert books(store).leases[r.rentals[rn].lease].start == date(2026, 11, 15)


def test_a_copys_mark_stays_the_same_when_a_later_event_is_stamped_earlier_by_a_clock_that_stepped_back():
    """The mark names the copy's codes on the access system, so it must not move once codes are made: it is read from the copy's first
    event, not from the earliest time any event carries."""
    store, dana, _ = facility()
    before = copy_mark(store, BASE)
    with store.write():
        led = Ledger(store.books(BASE))
        led.add_tenant(NOW, "Zed Early", "+14075550103")
        store.append(BASE, led.pending, now=datetime(2000, 1, 1, tzinfo=timezone.utc))      # stamped before the copy's first event
    assert copy_mark(store, BASE) == before


def test_a_rental_paid_and_signed_for_a_later_day_with_the_default_hold_gets_no_hold_task_and_still_starts_that_day():
    store, dana, _ = facility()
    system = StandIn()
    rs = ok(store, act("reserve", "staff:manager", "A101", dana, start="2026-11-15"))      # a later day, the usual twenty-minute hold
    rn = ok(store, act("start_checkout", "staff:manager", rs, dana))
    assert go(store, act("accept_agreement", "renter", rn, dana, sha256=rent(store).agreements[rent(store).rentals[rn].agreement].sha256)).allowed
    assert pay(store, dana, rn, rent(store).rentals[rn].quote["total_cents"]).allowed
    later = NOW + timedelta(hours=1)                                        # the hold time is long up
    advance(store, BASE, system, later)
    r = rent(store)
    assert [t for t in r.tasks.values() if t.open] == [] and r.holder("A101", later) == ("rental", rn)     # waiting for its day, still held
    advance(store, BASE, system, NOW + timedelta(days=5))
    r = rent(store)
    assert r.rentals[rn].state == "active" and r.access[r.rentals[rn].access].state == "verified"


def test_a_confirmed_refund_reads_paid_where_pages_look_for_its_state():
    store, dana, _ = facility()
    rn = checkout(store, dana, "A101")
    pay(store, dana, rn, 9500)
    ok(store, act("cancel_reservation", "staff:manager", rent(store).rentals[rn].reservation))
    assert rent(store).rentals[rn].refund == {"cents": 9500, "state": "pending"}
    ok(store, act("confirm_refund", "staff:manager", f"refund:{rn}"))                        # a cancelled rental's refund
    assert rent(store).rentals[rn].refund == {"cents": 9500, "state": "paid"} and refund_tasks(store) == []
    other = rented(store, dana, "A102")
    mo = move_out(store, other, NOV11)                                                       # a final bill's refund
    assert rent(store).moveouts[mo].bill["refund"] == "pending"
    ok(store, act("confirm_refund", "staff:manager", f"refund:{mo}"), NOV11)
    assert rent(store).moveouts[mo].bill["refund"] == "paid" and rent(store).moveouts[mo].bill["refund_cents"] > 0
    (billed,) = [e for e in store.events() if e.kind == "moveout.billed"]
    assert billed.payload["bill"]["refund"] == "pending"                                     # the bill as it was written is never changed
