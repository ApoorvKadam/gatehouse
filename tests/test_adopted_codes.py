"""The existing tenants' rentals hold a gate code on the simulated gate, verified from the first day. The gate is one file shared by every
copy, so a move-out or transfer in one copy revokes the code for that copy only, and a reset gives the copy its baseline back."""
import os
import types
from datetime import date

import pytest

from core import renting
from core.billing import Ledger
from core.books import at_local, lines
from core.store import BASE
from desk.front_desk import FrontDesk

DAY = date(2026, 12, 3)


def clock():
    return at_local(DAY, 10)


def desk_at(tmp_path):
    return FrontDesk(str(tmp_path / "core.db"), clock=clock)


def start(desk, visitor):
    """Give a copy its first event, so it has the mark its codes are kept under, and return its stream."""
    desk.add_renter(visitor, "Rae Quinn", "+14075550199")
    return desk.stream(visitor)


def existing(desk, visitor, skip=()):
    """An existing tenant's rental on this copy: one whose tenant owes nothing, so the final bill is plain."""
    rent = desk.rent_view(visitor)
    books = desk.view(visitor)[0]
    return next(r for r in rent.rentals.values()
                if r.provenance and r.state == "active" and r.id not in skip and not books.open_charges(r.tenant))


def gate(desk, visitor):
    store, stream = desk.ready(), desk.stream(visitor)
    return desk.access.for_copy(renting.copy_mark(store, stream))


def code_of(desk, visitor, rental):
    return gate(desk, visitor).read_back(renting.baseline_user(rental.unit))


def step(desk, visitor, name, action, about, source="staff:manager", tenant=None, **args):
    d = desk.rent_step(visitor, renting.Act(name, action, source, about, tenant, args))
    assert d.allowed, d.check
    return d.made


def move_out(desk, visitor, rental):
    mo = step(desk, visitor, f"mo-{rental.id}", "request_move_out", rental.id, on=DAY.isoformat())
    step(desk, visitor, f"rv-{mo}", "review_move_out", mo)
    step(desk, visitor, f"fb-{mo}", "final_bill", mo)
    return mo


def test_a_fresh_copy_has_a_verified_code_for_every_existing_tenant_and_the_books_do_not_move(tmp_path):
    desk = desk_at(tmp_path)
    store = desk.ready()
    stream = start(desk, "a")
    rent, books = desk.rent_view("a"), desk.view("a")[0]
    adopted = [r for r in rent.rentals.values() if r.provenance]
    active = [x for x in books.leases.values() if x.ended is None]
    assert len(adopted) == len(active) == 112 and len({r.access for r in adopted}) == 112      # 112 on this day; the default seed day has 113
    for r in adopted:
        a = rent.access[r.access]
        seen = gate(desk, "a").read_back(a.user)
        assert a.state == "verified" and a.simulated and a.user == renting.baseline_user(r.unit)
        assert renting.sha(seen["code"]) == a.code_sha256 and set(a.groups) <= set(seen["groups"])
        assert not renting.needs_clearance(r)
    assert renting.check_codes(store, stream, clock()) == 0
    assert not [t for t in desk.rent_view("a").tasks.values() if t.open]
    assert not [e for e in store.events(BASE) if e.kind == "access.baselined" and lines(e.kind, e.payload)]       # no money moved


def test_building_the_year_does_not_save_the_gate_file_once_per_code(tmp_path):
    saves = []

    class Counting(renting.StandIn):
        def _save(self):
            saves.append(1)
            super()._save()

    desk = desk_at(tmp_path)
    desk.access = Counting(path=desk.access.path)
    desk.ready()
    assert len(desk.access.baseline) == 112
    assert not saves and not os.path.exists(desk.access.path)


def test_one_copys_move_out_revokes_the_code_for_that_copy_only_and_a_reset_brings_it_back(tmp_path):
    desk = desk_at(tmp_path)
    desk.ready()
    start(desk, "a")
    start(desk, "b")
    rental = existing(desk, "a")
    before = code_of(desk, "a", rental)
    assert before["code"] and code_of(desk, "b", rental) == before
    move_out(desk, "a", rental)
    a, b = desk.rent_view("a"), desk.rent_view("b")
    assert a.access[rental.access].state == "revoked" and a.moveouts[a.rentals[rental.id].moveout].state == "completed"
    assert a.status(rental.unit, clock()) == "vacant" and not [t for t in a.tasks.values() if t.open and t.kind != "refund"]
    assert code_of(desk, "a", rental) == {"code": None, "groups": []}                        # gone, as read back for this copy
    assert code_of(desk, "b", rental) == before and b.access[rental.access].state == "verified"
    assert b.status(rental.unit, clock()) == "occupied"
    assert list(desk.access.masks.values()) == [{renting.baseline_user(rental.unit)}]       # the baseline itself was never changed
    assert desk.access.baseline[renting.baseline_user(rental.unit)]["code"] == before["code"]
    assert desk.reset("a")
    assert desk.access.masks == {}
    start(desk, "a")
    assert code_of(desk, "a", rental) == before
    assert desk.rent_view("a").access[rental.access].state == "verified"


def test_a_transfer_issues_and_reads_back_the_new_code_then_revokes_the_old_one_for_that_copy_only(tmp_path):
    desk = desk_at(tmp_path)
    store = desk.ready()
    start(desk, "a")
    start(desk, "b")
    old = existing(desk, "a")
    rent = desk.rent_view("a")
    target = next(u for u in desk.view("a")[0].units if rent.status(u, clock()) == "vacant")
    tr = step(desk, "a", "tr", "request_transfer", old.id, unit=target)
    new = desk.rent_view("a").transfers[tr].target
    step(desk, "a", "ap", "approve_transfer", tr)
    rent = desk.rent_view("a")
    cents = rent.short(rent.rentals[new], clock(), rent.rentals[old.id])
    with store.write():
        led = Ledger(store.books("visitor:a"))
        if cents:
            led.receive_payment(old.tenant, cents, clock())
        store.append("visitor:a", led.pending)
    sha = rent.agreements[rent.rentals[new].agreement].sha256
    step(desk, "a", "ac", "accept_agreement", new, "renter", old.tenant, sha256=sha, amount_cents=cents)
    step(desk, "a", "co", "complete_transfer", tr)
    a = desk.rent_view("a")
    fresh = a.access[a.rentals[new].access]
    seen = gate(desk, "a").read_back(fresh.user)
    assert a.transfers[tr].state == "completed" and a.rentals[new].state == "active" and fresh.state == "verified"
    assert renting.sha(seen["code"]) == fresh.code_sha256 and fresh.user != renting.baseline_user(old.unit)
    assert a.rentals[old.id].state == "ended" and a.access[old.access].state == "revoked"
    assert code_of(desk, "a", old) == {"code": None, "groups": []}
    assert code_of(desk, "b", old)["code"] and desk.rent_view("b").access[old.access].state == "verified"
    assert list(desk.access.masks.values()) == [{renting.baseline_user(old.unit)}]


def test_a_restarted_desk_keeps_one_copys_mask_and_the_others_baseline(tmp_path):
    desk = desk_at(tmp_path)
    desk.ready()
    start(desk, "a")
    start(desk, "b")
    rental = existing(desk, "a")
    before = code_of(desk, "b", rental)
    move_out(desk, "a", rental)
    again = desk_at(tmp_path)                                  # the app restarts on the same files
    again.ready()
    assert code_of(again, "a", rental) == {"code": None, "groups": []}
    assert code_of(again, "b", rental) == before
    assert again.rent_view("a").access[rental.access].state == "revoked"
    assert renting.check_codes(again.ready(), "visitor:a", clock()) == 0 and renting.check_codes(again.ready(), "visitor:b", clock()) == 0


def test_a_revoke_whose_save_failed_is_not_counted_and_is_made_again_on_the_retry(tmp_path):
    saves = []

    class Flaky(renting.StandIn):
        fail = 1

        def _save(self):
            if self.fail:
                self.fail -= 1
                raise OSError("disk full")
            super()._save()
            saves.append(1)

    desk = desk_at(tmp_path)
    desk.access = Flaky(path=desk.access.path)
    desk.ready()
    start(desk, "a")
    start(desk, "b")
    rental = existing(desk, "a")
    before = code_of(desk, "b", rental)
    move_out(desk, "a", rental)                                # the gate step fails to save; the desk logs it and goes on
    assert desk.rent_view("a").access[rental.access].state == "revocation_pending"
    assert desk.access.masks == {} and not desk.access.calls and not desk.access.done      # nothing held in memory that the file lacks
    assert code_of(desk, "a", rental) == before                # the code still stands, and the record does not say otherwise
    desk.sweep()                                               # the next pass makes the call again, and this time it is saved
    assert desk.rent_view("a").access[rental.access].state == "revoked" and saves
    again = desk_at(tmp_path)                                  # the app restarts on the same files
    again.ready()
    assert code_of(again, "a", rental) == {"code": None, "groups": []} and code_of(again, "b", rental) == before
    assert again.rent_view("a").access[rental.access].state == "revoked"


def test_a_revoke_saved_but_never_answered_is_found_on_the_retry_not_made_again(tmp_path):
    desk = desk_at(tmp_path)
    desk.access = renting.StandIn(crash_after={"revoke"}, path=desk.access.path)
    desk.ready()
    start(desk, "a")
    rental = existing(desk, "a")
    move_out(desk, "a", rental)                                # the mask was saved, then the answer was lost
    assert len(desk.access.calls) == 1 and desk.rent_view("a").access[rental.access].state == "revocation_pending"
    desk.sweep()
    assert len(desk.access.calls) == 1                         # found by its key, never called twice
    assert desk.rent_view("a").access[rental.access].state == "revoked"
    again = desk_at(tmp_path)
    again.ready()
    assert code_of(again, "a", rental) == {"code": None, "groups": []}


def test_the_default_seed_gives_all_113_existing_tenants_a_code_and_the_books_do_not_move():
    from core.seed import build
    year = build(standing=True)
    books = year.led.books
    events = [e for e in year.led.pending if e.kind == "access.baselined"]
    assert len(events) == 113 and len({e.payload["user"] for e in events}) == 113
    assert sum(c.open_cents for t in books.tenants for c in books.open_charges(t)) == 299600
    assert sum(bool(books.open_charges(t)) for t in books.tenants) == 23


def test_a_shared_gate_refuses_to_revoke_a_seeded_code_for_every_copy(tmp_path):
    desk = desk_at(tmp_path)
    desk.ready()
    user = next(iter(desk.access.baseline))
    with pytest.raises(KeyError):
        desk.access.revoke("visitor:x@00000000:AC0001:revoke", user)
    assert desk.access.read_back(user)["code"]


def test_a_seeded_record_that_does_not_match_the_gate_is_refused_and_the_baseline_stays(tmp_path):
    desk = desk_at(tmp_path)
    store = desk.ready()
    events = [e for e in store.events(BASE) if e.kind == "access.baselined"]
    bad = types.SimpleNamespace(kind=events[0].kind, payload={**events[0].payload, "code_sha256": "0" * 64})
    with pytest.raises(ValueError):
        desk.access.know([bad])
    assert len(desk.access.baseline) == 112


def test_a_rental_adopted_later_has_no_baseline_code_and_keeps_the_staff_check(tmp_path):
    desk = desk_at(tmp_path)
    store = desk.ready()
    stream = start(desk, "a")
    with store.write():
        books, rent = renting.load(store, stream)
        led = renting.RentLedger(books, rent)
        lease = led.sign_lease(clock(), next(iter(books.tenants)), next(u for u in books.units if rent.status(u, clock()) == "vacant"), 10000, [])
        rid = renting.adopt_existing_lease(led, lease, "imported_existing", clock())
        with pytest.raises(renting.Refused):
            renting.record_baseline_access(led, rid, clock())
        store.append(stream, led.pending)
    assert renting.needs_clearance(desk.rent_view("a").rentals[rid])
