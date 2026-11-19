"""Renting on the FMS pages: Units shows each unit's renting state and takes staff holds, blocks and prices through the gate, Today lists
the rentals not finished yet, the gate codes that are not confirmed and the units waiting for an old code to be revoked, and a
tenant's page shows each rental's steps in words. Every write is a refusal said once or a decision the books keep; each copy is its own."""
import itertools
import re
from datetime import timedelta

from core import renting
from core.billing import Ledger
from core.books import at_local
from core.evidence import clock
from core.seed import NOW
from desk.fms import SESSION, Facility

from tests.test_fms import record_of, seen, sign_in, site

N = itertools.count(1)


def desk_of(app, c):
    """The desk and this signed-in session's id: the way a test reaches the copy's own books and steps."""
    return app.state.front_desk, app.state.guard.valid(c.cookies.get(SESSION), "session")


def rent_of(fd, ident) -> renting.Rent:
    return renting.load(fd.ready(), fd.stream(ident))[1]


def vacant(fd, ident, n=1) -> list[str]:
    r = rent_of(fd, ident)
    return [u for u in sorted(fd.ready().books(fd.stream(ident)).units) if r.status(u, fd.clock()) == "vacant"][:n]


def token(page: str) -> str:
    return re.search(r'name="token" value="([0-9a-f]{12})"', page).group(1)


def unit_step(c, unit: str, page: str, **data):
    return c.post(f"/units/{unit}/renting", data={"token": token(page), **data}, follow_redirects=False)


def new_tenant(fd, ident, name="Rae Quinn", phone="+14075550199") -> str:
    """A tenant who owes nothing, so a paid rental can start (money on an account pays older charges first)."""
    store, stream = fd.ready(), fd.stream(ident)
    with store.write():
        led = Ledger(store.books(stream))
        tenant = led.add_tenant(fd.clock(), name, phone)
        store.append(stream, led.pending)
    return tenant


def step(fd, ident, action, about, tenant=None, source="renter", **args):
    d = fd.rent_step(ident, renting.Act(f"t{next(N)}", action, source, about, tenant, args))
    assert d.allowed, d.check
    return d.made


def start_rental(fd, ident, tenant, unit, accept=True, card=None, days=None, start_days=None) -> tuple[str, str]:
    """A rental in checkout (its reservation and itself); optionally its agreement accepted and a card payment sent (card_ok,
    card_declined, card_pending). With days, staff held the unit that long first, so the checkout hold lasts as long; with start_days
    the rental is quoted to start that many days from now."""
    rs = (step(fd, ident, "reserve", unit, tenant, source="staff:office_manager", until=(NOW + timedelta(days=days)).isoformat(),
               **({"start": (NOW + timedelta(days=start_days)).date().isoformat()} if start_days else {})) if days
          else step(fd, ident, "reserve", unit, tenant))
    rn = step(fd, ident, "start_checkout", rs, tenant)
    if accept:
        r = rent_of(fd, ident)
        step(fd, ident, "accept_agreement", rn, tenant, sha256=r.agreements[r.rentals[rn].agreement].sha256)
    if card:
        fd.pay_rental(ident, rn, card)
    return rs, rn


def test_units_show_the_four_states_with_counts_and_a_price_and_staff_hold_block_and_unblock_through_the_gate(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c, sign_in(app) as other:
        fd, ident = desk_of(app, c)
        held, blocked = vacant(fd, ident, 2)
        page = c.get("/units").text
        text = seen(page)
        assert "Vacant 7" in text and "Reserved 0" in text and "Blocked 0" in text and "Street rate" in text and "Asking price" not in text
        assert (page.count('value="reserve"'), page.count('value="block_unit"'), page.count('value="unblock_unit"')) == (7, 7, 0)
        assert page.count('value="price_unit"') == 120                                     # every unit's street rate can be changed
        # a unit's change controls are a row under it, opened by its Change link, with the forms side by side in it
        assert page.count('<tr class="fms-act"') == 120 and f'href="#act-{held}"' in page and f'<tr class="fms-act" id="act-{held}">' in page
        row = page.split(f'<tr class="fms-act" id="act-{held}">', 1)[1].split("</tr>", 1)[0]
        assert re.findall(r'name="action" value="(\w+)"', row) == ["price_unit", "reserve", "block_unit"] and "<details" not in page and "<select" not in page
        # the tenant is typed against one list of every name, shared by every row
        assert page.count("<datalist") == 1 and page.count('<option value="') == 124 and 'value="Priya Shah"' in page and page.count('list="tenant-names"') == 7
        last = (NOW + timedelta(days=3)).date()
        r = unit_step(c, held, page, action="reserve", tenant=" priya  shah ", until=last.isoformat())
        assert (r.status_code, r.headers["location"]) == (303, f"/units#u-{held}")
        unit_step(c, held, page, action="reserve", tenant="Priya Shah", until=last.isoformat())       # the same page pressed again: the first decision
        after = c.get("/units").text
        assert len(rent_of(fd, ident).reservations) == 1 and rent_of(fd, ident).status(held, NOW) == "reserved"
        assert "Reserved 1" in seen(after) and "Vacant 6" in seen(after) and "Nothing changed" not in seen(after)
        assert f"Held for Priya Shah until {clock(at_local(last, 23, 59))}" in seen(after)
        assert f'title="{held}: reserved for Priya Shah"' in after and after.count('value="reserve"') == 6
        unit_step(c, blocked, page, action="block_unit", reason="door being repaired")
        after = c.get("/units").text
        assert "Blocked 1" in seen(after) and "Blocked: door being repaired" in seen(after) and after.count('value="unblock_unit"') == 1
        assert rent_of(fd, ident).status(blocked, NOW) == "blocked"
        unit_step(c, blocked, page, action="unblock_unit")
        after = seen(c.get("/units").text)
        assert "Blocked 0" in after and "Vacant 6" in after and rent_of(fd, ident).status(blocked, NOW) == "vacant"
        assert "Reserved 0" in seen(other.get("/units").text)                                  # another copy never sees this one's hold
        assert [d["words"] for d in Facility(fd, ident).decisions()][:3] == ["Unblocked a unit", "Blocked a unit", "Held a unit"]
        # Today says what was done, in the second column of every row
        decided = c.get("/today").text.split('id="decided"', 1)[1].split("</section>", 1)[0]
        assert all(re.match(r"<li>(?:<a [^>]*>[^<]*</a>|<span></span>)<span>", li) for li in re.findall(r"<li>.*?</li>", decided, re.S))
        said = seen(decided)
        assert f"Unblocked unit {blocked}." in said and f"Blocked unit {blocked}: door being repaired." in said
        assert f"Held unit {held} for Priya Shah until {clock(at_local(last, 23, 59)).split(' at ')[0]}." in said
        assert "The records support it" not in said and "a unit, unit" not in said and "Held a unit" not in said


def test_a_refused_step_is_said_once_and_a_price_is_a_whole_number_of_cents(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        page = c.get("/units").text
        priya = "Priya Shah"
        until = (NOW + timedelta(days=2)).date().isoformat()
        unit_step(c, unit, page, action="reserve", tenant=priya, until=until)
        fresh = c.get("/units").text
        refused = unit_step(c, unit, fresh, action="block_unit", reason="repairs")            # a stale page: the unit was held meanwhile
        assert refused.headers["location"] == "/units#note"                         # a refusal lands where its note is, not on the row
        page_with_note = c.get("/units").text
        assert 'id="note"' in page_with_note and "Nothing changed. The unit is reserved." in seen(page_with_note)
        refused = unit_step(c, unit, fresh, action="block_unit", reason="repairs")
        said = seen(c.get("/units").text)
        assert said.count("Nothing changed. The unit is reserved.") == 1
        assert "Nothing changed" not in seen(c.get("/units").text)                  # said once
        assert "Blocked 0" in said and rent_of(fd, ident).status(unit, NOW) == "reserved"
        today = seen(c.get("/today").text.split('id="decided"', 1)[1])
        assert f"Asked to block unit {unit}: repairs. Not done: the unit is reserved. Nothing in the books changed." in today
        for name in ("Nobody Here", ""):                                            # a typed name can miss: said on the page, not an error
            missed = unit_step(c, unit, fresh, action="reserve", tenant=name, until=until)
            assert (missed.status_code, missed.headers["location"]) == (303, "/units#note")
            assert "Nothing changed. No tenant has that name, or two do; pick the name from the list" in seen(c.get("/units").text)
            assert "Nothing changed" not in seen(c.get("/units").text)
        for bad in (dict(action="reserve", tenant=priya, until="soon"),
                    dict(action="reserve", tenant=priya, until=(NOW - timedelta(days=1)).date().isoformat()),
                    dict(action="block_unit", reason=" "), dict(action="price_unit", amount="cheap"), dict(action="sell")):
            assert unit_step(c, unit, fresh, **bad).status_code == 422, bad
        assert c.post("/units/Z999/renting", data={"token": token(fresh), "action": "unblock_unit"}, follow_redirects=False).status_code == 404
        assert c.post(f"/units/{unit}/renting", data={"token": "not-a-token", "action": "unblock_unit"}, follow_redirects=False).status_code == 422
        other, = vacant(fd, ident)
        page = c.get("/units").text
        unit_step(c, other, page, action="price_unit", amount="125.50")
        after = c.get("/units").text
        assert rent_of(fd, ident).units[other].rent_cents == 12550 and "$125.50" in seen(after) and 'value="125.50"' in after
        unit_step(c, other, after, action="price_unit", amount="0.00")
        assert "Nothing changed. The rent must be a whole number of cents above zero." in seen(c.get("/units").text)
        assert rent_of(fd, ident).units[other].rent_cents == 12550
        today = seen(c.get("/today").text.split('id="decided"', 1)[1])
        assert f"Set the street rate of unit {other} to $125.50." in today and f"Asked to set the street rate of unit {other} to $0.00." in today


def test_two_tenants_with_one_name_are_told_apart_by_their_phones(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        new_tenant(fd, ident, "Sam Lee", "+14075550111")
        new_tenant(fd, ident, "Sam Lee", "+14075550122")
        page = c.get("/units").text
        assert page.count('<option value="Sam Lee, (407) 555-01') == 2 and '<option value="Sam Lee">' not in page
        until = (NOW + timedelta(days=1)).date().isoformat()
        which = unit_step(c, unit, page, action="reserve", tenant="Sam Lee", until=until)        # which one? said on the page
        assert which.headers["location"] == "/units#note" and "two do" in seen(c.get("/units").text)
        assert unit_step(c, unit, page, action="reserve", tenant="sam lee, (407) 555-0122", until=until).status_code == 303
        assert "Held for Sam Lee until" in seen(c.get("/units").text)


def test_every_home_bar_is_as_long_as_its_own_count(fms_url, sim):
    """Each Occupancy by building bar is handed the width of its own "N of M". (A bar looks short for the 900 ms it takes to grow.)"""
    with sign_in(site(fms_url)) as c:
        rows = re.findall(r'<i style="width: ([\d.]+)%"></i></span><span>(\d+) of (\d+)</span>', c.get("/home").text)
        assert rows and all(abs(float(w) - 100 * int(a) / int(b)) < 0.06 for w, a, b in rows), rows


def test_a_moved_out_unit_is_blocked_until_its_code_is_revoked_and_staff_cannot_unblock_it(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        tenant = new_tenant(fd, ident)
        _, rn = start_rental(fd, ident, tenant, unit, card="card_ok")
        assert rent_of(fd, ident).rentals[rn].state == "active"
        fd.access = renting.StandIn(crash_before={"revoke"})                       # the gate does not answer when the code is revoked
        mo = step(fd, ident, "request_move_out", rn, source="staff:office_manager", on=fd.today().isoformat())
        step(fd, ident, "review_move_out", mo, source="staff:office_manager")
        step(fd, ident, "final_bill", mo, source="staff:office_manager")
        assert rent_of(fd, ident).status(unit, NOW) == "blocked"
        page = c.get("/units").text
        text = seen(page)
        assert "Blocked 1" in text and "Moved out; the old gate code is not revoked yet" in text
        assert page.count('value="unblock_unit"') == 0 and page.count('value="reserve"') == 6          # only a staff block has an Unblock
        assert f'title="{unit}: blocked"' in page


def work(c) -> str:
    """What Today lists for renting, as a person reads it."""
    text = seen(c.get("/today").text)
    return text.split("Decided by staff")[0]


def test_a_fresh_copy_lists_no_renting_work_on_today_or_home(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        today, home = seen(c.get("/today").text), seen(c.get("/home").text)
        for heading in ("Rentals not finished yet", "Gate codes not confirmed", "Units waiting for an old gate code"):
            assert heading not in today
        assert not re.search(r"\d+ rentals? waiting for staff", home) and "not confirmed" not in home and "to be revoked" not in home


def test_today_lists_each_rental_not_finished_yet_and_home_counts_only_those_that_need_a_person(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c, sign_in(app) as other:
        fd, ident = desk_of(app, c)
        units = vacant(fd, ident, 6)
        people = [new_tenant(fd, ident, n) for n in ("Rae Quinn", "Ben Ortiz", "Cara Dunn", "Dev Patel", "Eli Park", "Hal Moss")]
        start_rental(fd, ident, people[0], units[0], card="card_pending")                    # the card processor has not answered
        start_rental(fd, ident, people[1], units[1], card="card_declined")
        start_rental(fd, ident, people[2], units[2], accept=False, card="card_ok", days=3)    # paid, agreement not accepted, hold ends in three days
        step(fd, ident, "reserve", units[3], people[3], source="staff:office_manager", until=(NOW + timedelta(hours=5)).isoformat())
        rs, _ = start_rental(fd, ident, people[4], units[4], accept=False, card="card_ok")
        step(fd, ident, "cancel_reservation", rs, source="staff:office_manager")             # paid for, then cancelled: money to give back
        start_rental(fd, ident, people[5], units[5], accept=False, days=3)                   # nothing paid, hold ends in three days
        text = work(c)
        section = text.split("Rentals not finished yet", 1)[1].split("Handed to staff")[0]
        for row in ("Rae Quinn · unit " + units[0] + " Payment waiting for the card processor, agreement accepted",
                    "Ben Ortiz · unit " + units[1] + " Card declined, agreement accepted",
                    "Cara Dunn · unit " + units[2] + " Paid, agreement not accepted",
                    "Dev Patel · unit " + units[3] + " Reservation held",
                    "Eli Park · unit " + units[4] + " Cancelled after payment, refund to make",
                    "Hal Moss · unit " + units[5] + " Not paid yet, agreement not accepted"):
            assert row in section, row
        assert section.index("Eli Park") < section.index("Ben Ortiz") < section.index("Rae Quinn")     # the ones to act on come first
        assert "Held until" in section and "The hold on the unit ends" in section and "refund" in section.lower()
        page = c.get("/today").text
        assert 'href="/tenants/rae-quinn-none#renting"' in page and f'href="/units#u-{units[0]}"' in page
        home = seen(c.get("/home").text)
        # Home counts what needs a person: money stuck (paid, declined, returned), a refund to make, a hold ending today. Not the checkouts
        # under way (Rae's and Ben's 20-minute holds settle by themselves), nor Hal's hold that ends on a later day.
        assert "3 rentals waiting for staff" in home and "not confirmed" not in home and "to be revoked" not in home
        assert "Rentals not finished yet" not in seen(other.get("/today").text)                # another copy has none of it
        # the hold runs out with no sweep in between: the unpaid ones leave the list as Units shows their units vacant, the paid or
        # unconfirmed ones keep holding their units
        fd.clock = lambda: NOW + timedelta(minutes=45)
        later = work(c).split("Rentals not finished yet", 1)[1].split("Handed to staff")[0]
        assert "Ben Ortiz" not in later and "Dev Patel" in later and "Rae Quinn" in later and "Cara Dunn" in later and "Hal Moss" in later
        assert "The hold time ended" in later and "4 rentals waiting for staff" in seen(c.get("/home").text)
        assert rent_of(fd, ident).status(units[1], NOW + timedelta(minutes=45)) == "vacant"


def test_a_code_that_was_not_read_back_is_listed_until_the_rental_ends_and_then_only_as_turnover(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c, sign_in(app) as other:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        fd.access = renting.StandIn(hide={renting.ACCESS_LEVEL})                          # the code is issued but never shows with both groups
        tenant = new_tenant(fd, ident)
        _, rn = start_rental(fd, ident, tenant, unit, card="card_ok")
        assert rent_of(fd, ident).access[rent_of(fd, ident).rentals[rn].access].state == "failed"
        text = work(c)
        section = text.split("Gate codes not confirmed", 1)[1].split("Handed to staff")[0]
        assert f"Rae Quinn · unit {unit} Gate code not confirmed since Nov 14" in section and "does not show it with both groups" in section
        assert "Rentals not finished yet" not in text and "to be revoked" not in text
        assert "1 gate code not confirmed" in seen(c.get("/home").text)
        assert "Gate codes not confirmed" not in seen(other.get("/today").text)
        fd.access = renting.StandIn(hide={renting.ACCESS_LEVEL}, crash_before={"revoke"})   # moving out: the gate does not take the revoke
        mo = step(fd, ident, "request_move_out", rn, source="staff:office_manager", on=fd.today().isoformat())
        step(fd, ident, "review_move_out", mo, source="staff:office_manager")
        step(fd, ident, "final_bill", mo, source="staff:office_manager")
        after = work(c)
        assert "Gate codes not confirmed" not in after                                      # the task is still open, but the rental is over
        section = after.split("Units waiting for an old gate code to be revoked", 1)[1].split("Handed to staff")[0]
        assert section.count(f"unit {unit}") == 1                                           # once under turnover (its refund is another job, listed above)
        assert f"Rae Quinn · unit {unit} Moved out Nov 14" in section and "cannot be rented again" in section
        home = seen(c.get("/home").text)
        assert "1 unit waiting for an old gate code to be revoked" in home and "not confirmed" not in home


def renting_section(page: str) -> str:
    return re.search(r'<section[^>]*id="renting".*?</section>', page, re.S).group(0)


def rows_of(section: str) -> dict[str, str]:
    """Each row of the Renting table as a person reads it, by its unit."""
    return {re.search(r"#u-(\w+)", r).group(1): seen(r) for r in re.findall(r"<tr>.*?</tr>", section, re.S) if "#u-" in r}


def test_a_tenants_page_shows_each_rental_step_in_words_and_offers_no_button(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c, sign_in(app) as other:
        fd, ident = desk_of(app, c)
        assert 'id="renting"' not in c.get(record_of(c, "Priya Shah")).text                    # nothing in renting: no section
        u1, u2, u3, u4, u5 = vacant(fd, ident, 5)
        tenant = new_tenant(fd, ident)
        start_rental(fd, ident, tenant, u1, card="card_ok")                                    # active, its code read back
        rs, _ = start_rental(fd, ident, tenant, u4, accept=False, card="card_ok")
        step(fd, ident, "cancel_reservation", rs, source="staff:office_manager")                # paid for, then cancelled
        start_rental(fd, ident, tenant, u2, card="card_pending")                               # the card processor has not answered (no later payment starts)
        step(fd, ident, "reserve", u3, tenant, source="staff:office_manager", until=(NOW + timedelta(hours=5)).isoformat())
        start_rental(fd, ident, tenant, u5, accept=False)                                      # nothing paid, nothing accepted
        section = renting_section(c.get(record_of(c, "Rae Quinn")).text)
        rows = rows_of(section)
        assert set(rows) == {u1, u2, u3, u4, u5}
        for part in ("Active", "Paid", "Move-in total", "Accepted by Rae Quinn, Nov 14 at 9:00 am", "Agreement version 1", "Started Nov 14",
                     "Code set and read back"):
            assert part in rows[u1], part
        assert "Waiting to start Hold ends Nov 14 at 9:20 am." in rows[u2] and "Waiting for the card processor" in rows[u2]
        assert "Accepted by" in rows[u2] and "Code" not in rows[u2] and "Started" not in rows[u2]
        assert "Reserved until Nov 14 at 2:00 pm" in rows[u3] and "Paid" not in rows[u3]
        assert "Cancelled" in rows[u4] and "Refund of" in rows[u4] and "waiting for staff." in rows[u4]
        assert "Refund waiting" in rows[u4] and "Paid" not in rows[u4]                          # money that is to go back is not shown as paid
        assert "\N{EN DASH}" not in rows[u1] and "\N{EN DASH}" in rows[u3] and "\N{EN DASH}" in rows[u5]   # a step not started shows a dash
        assert "Not accepted" in rows[u4] and "yet" not in rows[u4]                             # over: no step is still to do
        assert "Waiting to start" in rows[u5] and "Not paid yet" in rows[u5] and "Not accepted yet" in rows[u5]
        low = section.lower()
        for bad in ("<form", "<button", "<input", "pending", "awaiting", "unverified", "gate opened", "legally", "e-signature", "rn0", "rs0", "ac0", "ag0"):
            assert bad not in low, bad
        priya = fd.cast["priya"]["tenant"]                                                   # an existing tenant gets the section once she has a record
        held, = vacant(fd, ident)
        step(fd, ident, "reserve", held, priya, source="staff:office_manager", until=(NOW + timedelta(hours=6)).isoformat())
        hers = renting_section(c.get(record_of(c, "Priya Shah")).text)
        assert "Reserved until Nov 14 at 3:00 pm" in rows_of(hers)[held] and "<form" not in hers
        assert 'id="renting"' not in other.get(record_of(other, "Priya Shah")).text            # another copy never sees her reservation
        fd.clock = lambda: NOW + timedelta(minutes=45)                                        # the hold runs out; no sweep has run
        later = rows_of(renting_section(c.get(record_of(c, "Rae Quinn")).text))
        assert "Hold ended The hold ended" in later[u5] and "Not paid" in later[u5] and "yet" not in later[u5]
        assert "The hold time ended" in later[u2]                                              # a payment is still waiting: the unit stays held


def test_a_code_not_read_back_and_a_returned_payment_are_said_plainly(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        u1, u2 = vacant(fd, ident, 2)
        fd.access = renting.StandIn(hide={renting.ACCESS_LEVEL})
        tenant = new_tenant(fd, ident)
        start_rental(fd, ident, tenant, u1, card="card_ok")
        _, rn = start_rental(fd, ident, tenant, u2, accept=False)
        intent = fd.pay_rental(ident, rn, "card_ok")
        fd.processor.settle(intent.outside, "returned", fd.clock())
        fd.sweep()
        rows = rows_of(renting_section(c.get(record_of(c, "Rae Quinn")).text))
        assert "Active" in rows[u1] and "Access pending: the code could not be read back" in rows[u1] and "Code set and read back" not in rows[u1]
        assert "Returned by the bank" in rows[u2] and "Paid" not in rows[u2].replace("Move-in total", "")
        assert "Payment returned by the bank, agreement not accepted" in work(c)


def test_a_payment_that_came_after_the_hold_ran_out_is_a_refund_waiting_not_a_paid_rental(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        tenant = new_tenant(fd, ident)
        _, rn = start_rental(fd, ident, tenant, unit, accept=False)
        later = NOW + timedelta(hours=1)
        fd.clock = lambda: later
        store, stream = fd.ready(), fd.stream(ident)
        renting.advance(store, stream, fd.access, later)                                      # the hold ran out and the rental was dropped
        with store.write():
            led = Ledger(store.books(stream))
            pid = led.receive_payment(tenant, rent_of(fd, ident).rentals[rn].quote["total_cents"], later)
            store.append(stream, led.pending)
        d = fd.rent_step(ident, renting.Act("late-1", "confirm_payment", "code", rn, args={"payment": pid}))
        assert not d.allowed and "set aside for staff to refund" in d.check
        row = rows_of(renting_section(c.get(record_of(c, "Rae Quinn")).text))[unit]
        assert "Cancelled" in row and "Refund waiting" in row and "A refund is waiting for staff." in row and "Paid" not in row
        assert f"Rae Quinn · unit {unit} Cancelled after payment, refund to make" in work(c)


def test_a_paid_and_signed_rental_waits_for_its_move_in_day_and_is_not_work(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        tenant = new_tenant(fd, ident)
        _, rn = start_rental(fd, ident, tenant, unit, card="card_ok", days=4, start_days=3)
        assert rent_of(fd, ident).rentals[rn].state == "awaiting_payment"                    # the core's own word for it, paid or not
        today = work(c)
        assert f"Rae Quinn · unit {unit} Waiting for the move-in day" in today and "starts on its move-in day, Nov 17" in today
        assert not re.search(r"\d+ rentals? waiting for staff", seen(c.get("/home").text))     # nothing for a person to do
        row = rows_of(renting_section(c.get(record_of(c, "Rae Quinn")).text))[unit]
        assert "Waiting for the move-in day Starts Nov 17." in row and "Paid" in row and "Accepted by" in row and "Code" not in row


def test_a_final_bills_refund_is_work_until_it_is_confirmed_and_then_reads_made(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        tenant = new_tenant(fd, ident)
        _, rn = start_rental(fd, ident, tenant, unit, card="card_ok")
        mo = step(fd, ident, "request_move_out", rn, source="staff:office_manager", on=fd.today().isoformat())
        step(fd, ident, "review_move_out", mo, source="staff:office_manager")
        step(fd, ident, "final_bill", mo, source="staff:office_manager")
        cents = rent_of(fd, ident).moveouts[mo].bill["refund_cents"]
        assert cents > 0
        assert f"Rae Quinn · unit {unit} Moved out, refund to make" in work(c) and "Cancelled after payment" not in work(c)
        row = rows_of(renting_section(c.get(record_of(c, "Rae Quinn")).text))[unit]
        assert f"Refund of ${cents // 100}.{cents % 100:02d} waiting for staff." in row and "Ended" in row and "Refund waiting" not in row
        step(fd, ident, "confirm_refund", f"refund:{mo}", source="staff:office_manager")
        assert "refund to make" not in work(c)
        row = rows_of(renting_section(c.get(record_of(c, "Rae Quinn")).text))[unit]
        assert f"Refund of ${cents // 100}.{cents % 100:02d} was made." in row and "waiting for staff" not in row
