"""Today as the work queue: every open piece of staff work from collections, renting and the overlock schedule is one item with its source,
why it exists and what closes it; staff acknowledge, start, assign and note through the gate; an acknowledgement never closes an
unresolved item; a refusal is said once; each copy is its own."""
import re

from fastapi.testclient import TestClient

from core import renting
from core.seed import NOW
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, new_tenant, rent_of, start_rental, token, vacant


def keys(page: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r'name="key" value="([^"]+)"', page)))


def push(c, action, key, view="all", **data):
    page = c.get("/today").text
    return c.post("/work/step", data={"token": token(page), "view": view, "action": action, "key": key, **data}, follow_redirects=False)


def view(c, name="all") -> str:
    return seen(c.get(f"/today?view={name}").text)


def stuck_code(app, c):
    """A rental whose gate code never reads back with both groups: an item for the front desk."""
    fd, ident = desk_of(app, c)
    unit, = vacant(fd, ident)
    fd.access = renting.StandIn(hide={renting.ACCESS_LEVEL})
    tenant = new_tenant(fd, ident)
    start_rental(fd, ident, tenant, unit, card="card_ok")
    return fd, ident, unit


def test_a_fresh_copy_lists_the_years_work_in_four_views_and_the_money_figures_do_not_move(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        page = c.get("/today").text
        text = seen(page)
        assert "Work for staff" in text and "Facility-wide 14" in text and "My work 14" in text and "Waiting 0" in text and "Done today 0" in text
        assert "Rhea Mills Payment plan breached since Oct 26" in text and text.count("Overlock due") == 13
        assert "Each item stays here until the record it is about shows the work done" in text
        assert "What closes it: Staff reinstate or end the plan." in text and "What closes it: The overlock is recorded on the unit." in text
        assert 'id="collections"' in page and 'id="overlocks"' in page          # Home's links still land on the first of each kind
        home = seen(c.get("/home").text)
        assert "$2,996.00" in home and "23 tenants" in home and "14 collections item" not in home and "1 collections item waiting for staff" in home
        assert "13 overlocks due on the owner's schedule" in home
        assert "23 tenants owe $2,996.00" in seen(c.get("/collections").text)
        assert not re.search(r"\b(?:PL|PR|EX|PI|TK|RT|L|T|C)\d{4}\b", text) and not re.search(r"simulat|sim_pay|test mode", text, re.I)


def test_acknowledging_a_due_overlock_keeps_it_listed_and_recording_it_moves_it_to_done_today(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c, sign_in(app) as other:
        key = next(k for k in keys(c.get("/today").text) if k.startswith("overlock:"))
        r = push(c, "acknowledge", key)
        assert (r.status_code, r.headers["location"]) == (303, "/today?view=all#work")
        after = view(c)
        assert after.count("Overlock due") == 13 and "Seen by Office manager" in after              # seen is not done
        assert view(c, "done").count("Overlock due") == 0 and "Done today 0" in view(c, "done")
        assert "Seen by" not in view(other)                                                          # another copy keeps its own
        page = c.get("/today").text
        lease = re.search(r'name="lease" value="([A-Z0-9]+)"><input type="hidden" name="next" value="today"><button[^>]*>Record an overlock', page).group(1)
        owner = next(k for k in keys(page) if k == f"overlock:{lease}")
        who = re.search(r'href="(/tenants/[^"]+)#collections"', next(a for a in page.split("<article") if f'name="lease" value="{lease}"' in a)).group(1)
        c.post(f"{who}/collections", data={"token": token(page), "action": "overlock", "lease": lease, "next": "today"}, follow_redirects=False)
        done = view(c, "done")
        assert "Done today 1" in done and "The overlock is recorded on the unit." in done and "What closes it" not in done.split("Work for staff")[1].split("Handed to staff")[0]
        assert view(c).count("Overlock due") == 12 and owner not in keys(c.get("/today?view=all").text)


def test_acknowledging_an_unconfirmed_gate_code_does_not_close_it_but_the_code_reading_back_does(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident, unit = stuck_code(app, c)
        code = next(k for k in keys(c.get("/today").text) if k.startswith("access:"))
        assert f"Rae Quinn · unit {unit} Gate code not confirmed since Nov 14" in view(c)
        assert push(c, "acknowledge", code).status_code == 303
        text = view(c)
        assert f"unit {unit} Gate code not confirmed" in text and "Seen by Office manager" in text
        assert "Gate code not confirmed" not in view(c, "done")
        # staff retry and the code still does not read back: the renting record closes its task and opens another for the same code,
        # and the queue keeps its one item for it, still not done
        stuck = next(a for a in rent_of(fd, ident).access.values() if a.state == "failed")
        assert fd.rent_step(ident, renting.Act("retry-1", "retry_access", "staff:office_manager", stuck.id)).allowed
        assert sum(1 for t in rent_of(fd, ident).tasks.values() if t.kind == "access") == 2
        after = view(c)
        assert after.count(f"unit {unit} Gate code not confirmed") == 1 and "Seen by Office manager" in after
        assert "Done today 0" in after
        opened = [e.payload["key"] for e in fd.ready().stream_events(fd.stream(ident)) if e.kind == "work.opened" and e.payload["key"] == code]
        assert opened == [code]


def test_an_item_assigned_to_a_role_is_under_that_roles_my_work_and_overdue_ones_say_so(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident, unit = stuck_code(app, c)
        code = next(k for k in keys(c.get("/today").text) if k.startswith("access:"))
        assert f"unit {unit}" not in view(c, "mine")                                                # the front desk's, by default
        assert push(c, "assign", code, to="office_manager").status_code == 303
        mine = view(c, "mine")
        assert f"unit {unit} Gate code not confirmed" in mine and "Assigned to Office manager" in mine and "My work 15" in mine
        assert push(c, "assign", code, to="front_desk").status_code == 303
        assert f"unit {unit}" not in view(c, "mine") and "Assigned to Front desk" in view(c)
        assert push(c, "assign", code, to="person:dana.reyes").status_code == 303
        assert "Assigned to Dana Reyes" in view(c, "mine")
        assert "Overdue" in view(c) and view(c).count("Overdue") >= 1                                # the breached plan fell due Oct 26


def test_starting_and_noting_an_item_are_kept_and_the_item_stays_until_its_record_changes(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        plan = next(k for k in keys(c.get("/today").text) if k.startswith("collections:breached:"))
        assert push(c, "start", plan).status_code == 303 and push(c, "note", plan, text="Left a message for her").status_code == 303
        text = view(c)
        assert "In progress" in text and "Left a message for her" in text and "Rhea Mills" in text
        assert "Nothing changed" not in text
        refused = push(c, "finish", plan, text="I looked at it")                                      # a plan is closed by deciding it, not by a word
        assert refused.status_code == 303
        again = view(c)
        assert "Nothing changed. The record closes this one, not a word: staff reinstate or end the plan." in again
        assert "Nothing changed" not in view(c)                                                       # said once
        assert "Rhea Mills Payment plan breached" in view(c)


def test_a_refusal_is_said_once_and_a_visitor_not_signed_in_is_refused(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        key = keys(c.get("/today").text)[0]
        push(c, "assign", key, to="janitor")
        first = view(c)
        assert "Nothing changed. Work goes to a staff role or a named person." in first
        assert "Nothing changed" not in view(c)
        assert push(c, "nonsense", key).status_code == 422
        r = c.post("/work/step", data={"token": "not-a-token", "action": "start", "key": key})
        assert r.status_code == 422
    stranger = TestClient(app)
    r = stranger.post("/work/step", data={"token": "a" * 12, "action": "start", "key": key}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/sign-in")
    with sign_in(app) as fresh:
        assert "In progress" not in view(fresh)                                                       # nothing leaked into a new copy


def test_visiting_today_again_makes_no_second_item(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        c.get("/today"), c.get("/today")
        fd.sweep()
        c.get("/today")
        opened = [e.payload["key"] for e in fd.ready().stream_events(fd.stream(ident)) if e.kind == "work.opened"]
        assert len(opened) == len(set(opened)) == 14
        assert NOW.date() == fd.today()
