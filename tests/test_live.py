"""A visitor can start a live Claude run on the preview, inside the owner's limits, and the Texts page shows the result.
The model is scripted here; the limits, the refusals and the page are the real ones."""
from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from desk import spend
from desk.adapter.cubby import CubbyAdapter
from desk.api import create_app
from desk.config import Settings
from desk.models import AgentRun

from .conftest import FULL_KEY
from .test_investigator import TOKEN, Scripted, lookups_then_packet, message_ids, preview

CALL = "/api/inbox/{}/investigate"
A = {"cf-connecting-ip": "203.0.113.7"}


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
        def __init__(self, headers, host="10.0.0.9"):
            self.headers, self.client = headers, type("C", (), {"host": host})()

    behind_cloudflare = spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "198.51.100.1, 10.1.1.1"}))
    assert behind_cloudflare == spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.7"}))
    assert behind_cloudflare != spend.visitor_of(Request({"cf-connecting-ip": "203.0.113.8"}))
    assert spend.visitor_of(Request({"x-forwarded-for": "198.51.100.1, 10.1.1.1"})) == spend.visitor_of(Request({"x-forwarded-for": "198.51.100.1"}))
    assert spend.visitor_of(Request({}, host="192.0.2.5")) != spend.visitor_of(Request({}, host="192.0.2.6"))
    assert "203.0.113.7" not in behind_cloudflare and len(behind_cloudflare) == 16


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
        assert third.status_code == 429 and "2 cases for today" in third.json()["detail"]
        again = click(client, dana, "visitor-click-002", A)                                          # a retry of an answered click is not a new run
        assert again.status_code == 200 and again.json()["id"] == second.json()["id"]
        other = click(client, dana, "visitor-click-004", {"cf-connecting-ip": "203.0.113.99"})       # another visitor has their own count
        assert other.status_code == 200 and other.json()["limits"]["cases_left"] == 1


def test_the_daily_budget_stops_everyone_the_operator_too(fms_url, sim):
    with TestClient(live_app(fms_url, Scripted(), daily_budget_usd=0.02)) as client:
        dana = message_ids(client)["Dana Whitfield"]
        visitor = click(client, dana, "visitor-click-010", A)
        assert visitor.status_code == 429 and "budget" in visitor.json()["detail"]
        operator = click(client, dana, "operator-click-01", {"X-Desk-Token": TOKEN})
        assert operator.status_code == 429


def test_a_token_that_is_sent_is_checked_even_when_visitors_may_run(fms_url, sim):
    with TestClient(live_app(fms_url, Scripted())) as client:
        dana = message_ids(client)["Dana Whitfield"]
        assert click(client, dana, "visitor-click-020", {"X-Desk-Token": "not-the-token"}).status_code == 401


def test_without_the_switch_a_visitor_still_cannot_start_a_run(fms_url, sim):
    with TestClient(preview(fms_url, Scripted())) as client:
        dana = message_ids(client)["Dana Whitfield"]
        assert click(client, dana, "visitor-click-030").status_code == 401


# ---------------------------------------------------------------- the Texts page


def test_the_texts_page_shows_each_text_who_it_matches_and_the_switch(fms_url, sim):
    with TestClient(live_app(fms_url, Scripted(), visitor_cases_per_day=10)) as client:
        page = client.get("/inbox")
        html = page.text
        assert page.status_code == 200 and 'href="/inbox"' in html and "Texts at " in html
        assert all(name in html for name in ("Dana Whitfield", "Priya Raman", "Marcus Bell", "Unknown number"))
        assert "Texting stopped" in html and "Reply blocked" in html and "Reply sent" in html
        assert "You have 10 of 10 cases left today." in html
        assert html.count("Investigate with Claude") == 3                          # the three matched texts, not the stranger


def test_the_texts_page_says_so_when_live_runs_are_off(fms_url, sim):
    with TestClient(preview(fms_url, None)) as client:
        html = client.get("/inbox").text
        assert "Live Claude runs are off on this link." in html and "Investigate with Claude" not in html
        assert "Claude has not looked at this text." in html


def test_after_a_run_the_page_shows_claudes_words_under_their_label_the_records_and_the_desks_check(fms_url, sim):
    model = Scripted()
    with TestClient(live_app(fms_url, model)) as client:
        fill(client, model, 1)
        assert click(client, message_ids(client)["Dana Whitfield"], "visitor-click-040", A).status_code == 200
        html = client.get("/inbox").text
        assert "Claude&#39;s reading" in html or "Claude's reading" in html
        assert "Dana says she paid $240.00, and her unit is still on the sale list." in html
        assert "a person has to ask for the sale to be cancelled" in html
        assert "word for word" in html and "Sale still listed, nothing owed (open)." in html
        assert "The desk confirms the sale" in html and "Investigate again" in html
