"""A visitor can explore a message with our agent live on the preview, inside the limits, and the Messages page shows the result.
The model is scripted here; the limits, the refusals and the page are the real ones."""
from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from desk import spend
from desk.adapter.cubby import CubbyAdapter
from desk.api import create_app
from desk.config import Settings
from desk.models import AgentRun

from .conftest import FULL_KEY
from .test_investigator import TOKEN, Scripted, factory, lookups_then_packet, message_ids, preview  # noqa: F401

CALL = "/api/inbox/{}/investigate"
A = {"cf-connecting-ip": "203.0.113.7"}


def no_vendor_name(page: str) -> bool:
    """No page names the model or its maker. The agent is called our agent."""
    return not any(word in page.lower() for word in ("claude", "anthropic", "haiku"))


def live_app(fms_url, model, **limits):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token=TOKEN, public_view=True, demo_seed=True, public_investigate=True, **limits)
    return create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False, chat_client=model)


def fill(client, model, runs: int) -> None:
    """Script the model for this many runs on Dana's text: the lookups, then the packet, each time."""
    model.turns = []
    with client.app.state.session_factory() as s:
        for _ in range(runs):
            model.turns += lookups_then_packet(s).turns


def click(client, message_id: int, request_id: str, headers=None):
    return client.post(CALL.format(message_id), json={"request_id": request_id}, headers=headers or {})


# ---------------------------------------------------------------- the day and the visitor, on their own


def test_a_day_starts_at_midnight_in_new_york():
    assert spend.day_start(datetime(2026, 11, 8, 3, 30)) == datetime(2026, 11, 7, 5, 0)       # 10:30 PM on Nov 7, standard time
    assert spend.day_start(datetime(2026, 11, 8, 6, 0)) == datetime(2026, 11, 8, 5, 0)        # 1 AM on Nov 8
    assert spend.day_start(datetime(2026, 7, 4, 3, 0)) == datetime(2026, 7, 3, 4, 0)          # daylight time in July


def test_a_visitor_is_a_hash_of_where_the_request_came_from_and_never_the_address():
    class Request:
        def __init__(self, headers, host="10.0.0.9", cookies=None):
            self.headers, self.client, self.cookies = headers, type("C", (), {"host": host})(), cookies or {}

    behind_cloudflare = spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "198.51.100.1, 10.1.1.1"}))
    assert behind_cloudflare == spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.7"}))
    assert behind_cloudflare != spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.8"}))
    assert spend.visitor_of(Request({"x-forwarded-for": "198.51.100.1, 10.1.1.1"})) == spend.visitor_of(Request({"x-forwarded-for": "198.51.100.1"}))
    assert spend.visitor_of(Request({}, host="192.0.2.5")) != spend.visitor_of(Request({}, host="192.0.2.6"))
    assert "203.0.113.7" not in behind_cloudflare and len(behind_cloudflare) == 16
    assert behind_cloudflare != hashlib.sha256(b"gatehouse-visitor:203.0.113.7").hexdigest()[:16]      # not a hash anyone can compute from an address
    # a browser that holds the desk's cookie is told apart by it, not by its address, and a cookie that is not the desk's own shape is ignored
    token = spend.new_cookie()
    held = spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.7"}, cookies={spend.COOKIE: token}))
    assert held == spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.99"}, cookies={spend.COOKIE: token})) and held != behind_cloudflare
    assert spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.7"}, cookies={spend.COOKIE: "not-a-token"})) == behind_cloudflare


# ---------------------------------------------------------------- a visitor starts a run


def test_a_visitor_with_no_token_starts_live_runs_inside_their_count_and_a_retry_costs_nothing(fms_url, sim):
    model = Scripted()
    with TestClient(live_app(fms_url, model, visitor_cases_per_day=2)) as client:
        fill(client, model, 3)
        dana = message_ids(client)["Dana Whitfield"]
        first = click(client, dana, "visitor-click-001", A)
        assert first.status_code == 200 and first.json()["status"] == "done" and first.json()["limits"]["cases_left"] == 1
        with client.app.state.session_factory() as s:
            assert s.scalar(select(AgentRun.visitor)) not in (None, "203.0.113.7")                 # counted, and not by address
        second = click(client, dana, "visitor-click-002", A)
        assert second.status_code == 200 and second.json()["id"] != first.json()["id"]               # every click is a live run
        assert second.json()["limits"]["cases_left"] == 0
        third = click(client, dana, "visitor-click-003", A)
        assert third.status_code == 429 and "2 runs of our agent" in third.json()["detail"]
        again = click(client, dana, "visitor-click-002", A)                                          # a retry of an answered click is not a new run
        assert again.status_code == 200 and again.json()["id"] == second.json()["id"]
        other = click(client, dana, "visitor-click-004", {"cf-connecting-ip": "203.0.113.99"})       # another visitor has their own count
        assert other.status_code == 200 and other.json()["limits"]["cases_left"] == 1


def test_the_daily_budget_stops_visitors_but_never_locks_the_operator_out(fms_url, sim):
    model = Scripted()
    with TestClient(live_app(fms_url, model, daily_budget_usd=0.02)) as client:
        fill(client, model, 1)
        dana = message_ids(client)["Dana Whitfield"]
        visitor = click(client, dana, "visitor-click-010", A)
        assert visitor.status_code == 429 and "budget" in visitor.json()["detail"]
        operator = click(client, dana, "operator-click-01", {"X-Desk-Token": TOKEN})
        assert operator.status_code == 200 and operator.json()["status"] == "done"


def test_a_token_that_is_sent_is_checked_even_when_visitors_may_run(fms_url, sim):
    with TestClient(live_app(fms_url, Scripted())) as client:
        dana = message_ids(client)["Dana Whitfield"]
        assert click(client, dana, "visitor-click-020", {"X-Desk-Token": "not-the-token"}).status_code == 401


def test_without_the_switch_a_visitor_still_cannot_start_a_run(fms_url, sim):
    with TestClient(preview(fms_url, Scripted())) as client:
        dana = message_ids(client)["Dana Whitfield"]
        assert click(client, dana, "visitor-click-030").status_code == 401


# ---------------------------------------------------------------- the Messages page


def test_the_messages_page_lists_each_message_and_shows_the_chosen_one_as_a_sequence(fms_url, sim):
    with TestClient(live_app(fms_url, Scripted(), visitor_cases_per_day=10)) as client:
        ids = message_ids(client)
        page = client.get("/inbox")
        html = page.text
        assert page.status_code == 200 and 'href="/inbox"' in html and "Messages at " in html and ">Messages</a>" in html
        assert all(name in html for name in ("Dana Whitfield", "Priya Raman", "Marcus Bell", "Unknown number"))      # every message is in the list
        assert "Texting stopped" in html                                                                           # Marcus's chip, in the list
        assert "explorations left" not in html and 'class="meter' not in html            # no allowance counter: the server stops a visitor at the limit and says so then
        assert html.count("data-explore=") == 1 and "Explore with our agent" in html                             # one button, for the chosen matched message
        assert no_vendor_name(html)                                                                              # the page never names the model
        dana = client.get(f"/inbox?text={ids['Dana Whitfield']}").text                                           # nobody has explored her message yet
        assert 'class="seq"' in dana and "Message received" in dana and "Our agent reads the case" in dana       # the steps, the first done and the rest waiting
        assert dana.count('class="step pending after"') == 4 and 'id="progress" hidden' in dana
        priya = client.get(f"/inbox?text={ids['Priya Raman']}").text
        assert "Hi Priya, yes. Your payment plan" in priya and ">Sent<" in priya.replace("</svg>", "")
        marcus = client.get(f"/inbox?text={ids['Marcus Bell']}").text
        assert "Blocked" in marcus and "This number texted STOP" in marcus
        stranger = client.get(f"/inbox?text={ids['stranger']}").text
        assert "No tenant was found for this number" in stranger and "data-explore=" not in stranger and 'class="step pending' not in stranger


def test_the_messages_page_says_so_when_live_runs_are_off(fms_url, sim):
    with TestClient(preview(fms_url, None)) as client:
        html = client.get("/inbox").text
        assert "Our agent is switched off on this copy" in html and "Explore with our agent" not in html and "data-explore=" not in html
        assert 'class="step pending' not in html and 'id="progress"' not in html         # nothing waits for an agent that is off
        assert no_vendor_name(html)


def test_there_is_no_limits_page_and_no_page_links_to_one(fms_url, sim):
    with TestClient(preview(fms_url, None)) as client:
        assert client.get("/limits").status_code == 404
        for path in ("/", "/today", "/inbox", "/evaluation", "/operations"):
            html = client.get(path).text
            assert "Limits" not in html and "/limits" not in html and "What this is and is not" not in html, path


def test_after_a_run_the_page_shows_the_case_notes_under_their_label_the_records_and_the_desks_check(fms_url, sim):
    model = Scripted()
    with TestClient(live_app(fms_url, model)) as client:
        fill(client, model, 1)
        dana = message_ids(client)["Dana Whitfield"]
        assert click(client, dana, "visitor-click-040", A).status_code == 200
        html = client.get(f"/inbox?text={dana}").text
        assert "Case notes" in html and no_vendor_name(html)
        assert "Dana says she paid $240.00, and her unit is still on the sale list." in html
        assert "a person has to ask for the sale to be cancelled" in html
        assert "these notes rest on" in html and "Sale still listed, nothing owed (open)." in html
        assert 'class="rec-kind">Sale<' in html and 'class="rec-kind">Payment attempt<' in html      # the two records the packet cites, by kind
        for code in ("workitem:", "payment:", "observed:", "lease:", "lease_", "payment_", "NEEDS_STAFF", "sale_listed"):          # no raw id or code on the page
            assert code not in html, code
        assert "The desk confirms the sale" in html and "Explore again" in html and "Written by our agent from the desk" in html
        assert "Our agent read the case" in html and "The desk checked the notes" in html and 'class="step pending' not in html     # the steps that happened, none waiting
        assert html.index("Our agent read the case") < html.index("The desk checked the notes") < html.index("Case notes")      # the sequence first, the long notes under it
        assert "<details class=\"records\" open" not in html                                                                  # the records stay closed until asked for
        assert "FMS lien cancellation" in html and "Outside listing" in html and "Still listed in the FMS" in html and 'href="/sales/' in html   # the two outcomes, each its own step
        for code in ("LISTED", "CANCELLED", "WITHDRAWN", "listing_", "auction_"):
            assert code not in html, code


# ---------------------------------------------------------------- the check, asked again once the run is written


def test_a_run_already_written_can_ask_about_the_others(fms_url, sim):
    from desk.db import utcnow
    with TestClient(live_app(fms_url, Scripted())) as client:
        with client.app.state.session_factory() as s:
            dana = message_ids(client)["Dana Whitfield"]
            row = AgentRun(inbound_id=dana, visitor="v1", model="m", effort="e", status="running", started_at=utcnow())
            s.add(row)
            s.flush()
            mine = row.id
            assert spend.running(s) == 1 and spend.running(s, exclude=mine) == 0
            assert spend.cases_today(s, "v1") == 1 and spend.cases_today(s, "v1", exclude=mine) == 0
            assert spend.refusal(s, 1.0, 1, "v1") is not None and spend.refusal(s, 1.0, 1, "v1", exclude=mine) is None
            s.rollback()


def test_a_run_that_is_over_a_limit_once_it_is_written_is_not_kept_and_costs_nothing(factory):
    from desk.investigator import investigate
    from desk.spend import Refused
    from .test_investigator import text_of
    with factory() as s:
        asked = []

        def allow(session, exclude):
            asked.append(exclude)
            return None if exclude is None else "Too many at once."

        try:
            investigate(s, Scripted(), text_of(s, "Dana Whitfield"), "request-live-01", allow=allow)
            raise AssertionError("the run should have been refused")
        except Refused as exc:
            assert "Too many at once." in str(exc)
        assert asked[0] is None and asked[1] is not None                        # asked before and after it was written
        assert s.scalar(select(AgentRun)) is None                                # and the run did not stay
