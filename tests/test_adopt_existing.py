from copy import deepcopy

import pytest

from core.billing import Refused
from core.books import lines
from core.renting import Rent, RentLedger, adopt_existing_lease
from core.seed import NOW, build


def test_seeded_leases_are_rentals_without_changing_books():
    year = build(standing=True)
    books = year.led.books
    rent = Rent(books)
    for event in year.led.pending:
        rent.apply(event.kind, event.at, event.payload)
    active = [r for r in rent.rentals.values() if r.state == "active"]
    leases = [lease for lease in books.leases.values() if lease.ended is None]
    assert len(active) == len(leases) == 113
    assert len({r.unit for r in active}) == 113
    assert len(books.units) - len({r.unit for r in active}) == 7
    assert {r.lease for r in active} == {lease.id for lease in leases}
    assert sum(bool(books.open_charges(t)) for t in books.tenants) == 23
    assert sum(c.open_cents for t in books.tenants for c in books.open_charges(t)) == 299600
    assert all(not lines(e.kind, e.payload) for e in year.led.pending if e.kind == "rental.adopted")
    led = RentLedger(books, rent)
    for lease in leases:
        adopt_existing_lease(led, lease.id, "seeded_existing", NOW)
    assert not led.pending


def test_adoption_refuses_conflicts_without_effects():
    year = build(standing=True)
    books = deepcopy(year.led.books)
    rent = Rent(books)
    for event in year.led.pending:
        rent.apply(event.kind, event.at, event.payload)
    led = RentLedger(books, rent)
    lease = next(x for x in books.leases.values() if x.ended is None)
    before = deepcopy(books)
    with pytest.raises(Refused):
        adopt_existing_lease(led, lease.id, "imported_existing", NOW)
    other = next(x for x in books.leases.values() if x.ended is None and x.unit != lease.unit)
    original = other.unit
    other.unit = lease.unit
    with pytest.raises(Refused):
        adopt_existing_lease(led, other.id, "imported_existing", NOW)
    other.unit = original
    tenant = other.tenant
    other.tenant = "missing"
    with pytest.raises(Refused):
        adopt_existing_lease(led, other.id, "imported_existing", NOW)
    other.tenant = tenant
    assert not led.pending
    assert books.credit == before.credit
    assert books.charges == before.charges


def test_adoption_needs_the_renting_records():
    year = build(standing=True)
    lease = next(x for x in year.led.books.leases.values() if x.ended is None)
    with pytest.raises(Refused):                    # a plain ledger cannot see a lease's existing rental, so it is refused
        adopt_existing_lease(year.led, lease.id, "imported_existing", NOW)
