"""The bot check in front of the preview's two public write paths. Turnstile is played by a stand-in that records what it was asked and
answers as a test says; the desk, the cookie, the limits and the pages are the real ones."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from desk.config import Settings
from desk.guard import ACTION, ASK, LIFETIME, TRIES_PER_HOUR, UNSET, Guard, Unreachable
from desk.models import InboundMessage

from .test_investigator import TOKEN, Scripted
from .test_live import live_app
from .test_sandbox import explore, type_as

KEYS = {"turnstile_sitekey": "site-key-for-tests", "turnstile_secret": "secret-for-tests"}
HOST = "testserver"


class Turnstile:
    """What Turnstile would say, and what it was asked."""

    def __init__(self):
        self.asked: list[tuple[str, str]] = []
        self.answer = {"success": True, "hostname": HOST, "action": ACTION}

    def __call__(self, token: str, address: str) -> dict:
        self.asked.append((token, address))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def turnstile(monkeypatch) -> Turnstile:
    fake = Turnstile()
    monkeypatch.setattr(Guard, "_siteverify", fake)
    return fake


def pass_the_check(client: TestClient, token: str = "good-token"):
    return client.post("/api/visitor", json={"token": token})


def seeded_text(app) -> int:
    with app.state.session_factory() as s:
        return s.scalar(select(InboundMessage.id).where(InboundMessage.visitor.is_(None), InboundMessage.match_state == "matched").order_by(InboundMessage.id))


def test_without_the_check_a_visitor_can_neither_type_nor_explore(fms_url, sim, turnstile):
    app = live_app(fms_url, Scripted(), **KEYS)
    with TestClient(app) as visitor:
        page = visitor.get("/inbox")
        assert "set-cookie" not in page.headers                                  # a cookie the desk would trust is given only for a pass
        assert 'class="cf-turnstile"' in page.text and "site-key-for-tests" in page.text and 'data-action="visit"' in page.text
        typed = type_as(visitor, "dana", "Why is my unit on the sale list?", "press-0001")
        assert typed.status_code == 403 and typed.json()["detail"] == ASK
        assert explore(visitor, {"id": seeded_text(app)}, "press-0002").status_code == 403
        assert turnstile.asked == []                                             # the routes do not ask Turnstile; only /api/visitor does


def test_a_genuine_answer_gives_a_signed_cookie_and_then_the_visitor_can_type_and_explore(fms_url, sim, turnstile):
    app = live_app(fms_url, Scripted(), **KEYS)
    with TestClient(app) as visitor:
        passed = pass_the_check(visitor)
        assert passed.status_code == 200 and passed.json() == {"verified": True}
        cookie = passed.headers["set-cookie"]
        assert "gh_visitor=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
        assert app.state.guard.valid(visitor.cookies.get("gh_visitor")) is not None
        assert turnstile.asked == [("good-token", "testclient")]                 # the token and the address, nothing else
        typed = type_as(visitor, "dana", "Why is my unit on the sale list?", "press-0001")
        assert typed.status_code == 200
        page = visitor.get(f"/inbox?facility={typed.json()['facility']}&text={typed.json()['id']}").text
        assert "Why is my unit on the sale list?" in page and 'class="cf-turnstile"' not in page      # nothing left to ask this browser


def test_an_answer_that_is_not_genuine_for_this_address_and_this_check_gives_nothing(fms_url, sim, turnstile):
    app = live_app(fms_url, Scripted(), **KEYS)
    for answer in ({"success": False, "error-codes": ["invalid-input-response"]},
                   {"success": True, "hostname": "elsewhere.example", "action": ACTION},
                   {"success": True, "hostname": HOST, "action": "something-else"},
                   {"success": True, "hostname": HOST}):
        turnstile.answer = answer
        with TestClient(app) as visitor:
            refused = pass_the_check(visitor)
            assert refused.status_code == 403 and "did not pass" in refused.json()["detail"], answer
            assert "set-cookie" not in refused.headers
            assert type_as(visitor, "dana", "Hello", "press-0001").status_code == 403
    turnstile.answer = Unreachable("ConnectError")
    with TestClient(app) as visitor:
        down = pass_the_check(visitor)
        assert down.status_code == 503 and "not answering" in down.json()["detail"] and "set-cookie" not in down.headers      # closed, and not called a failure
        assert visitor.get("/inbox").status_code == 200                                                                    # reading still works


def test_a_cookie_the_desk_did_not_sign_is_not_a_pass(fms_url, sim, turnstile):
    app = live_app(fms_url, Scripted(), **KEYS)
    other = Guard("site-key-for-tests", "secret-for-tests")                       # another process, with another key
    for value in ("0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef.9999999999.deadbeef", "0123456789abcdef0123456789abcdef.deadbeef",
                  other.cookie(), "x.y.z", ""):
        with TestClient(app, cookies={"gh_visitor": value}) as visitor:
            assert type_as(visitor, "dana", "Hello", "press-0001").status_code == 403, value


def test_a_browser_that_has_passed_keeps_its_cookie_and_is_not_asked_again(fms_url, sim, turnstile):
    app = live_app(fms_url, Scripted(), **KEYS)
    with TestClient(app) as visitor:
        pass_the_check(visitor)
        again = pass_the_check(visitor, "another-token")
        assert again.status_code == 200 and "set-cookie" not in again.headers      # a new cookie would be a new count to start from
        assert len(turnstile.asked) == 1


def test_one_address_may_ask_for_the_check_only_so_often_in_an_hour(fms_url, sim, turnstile):
    turnstile.answer = {"success": False}
    app = live_app(fms_url, Scripted(), **KEYS)
    with TestClient(app) as visitor:
        codes = [visitor.post("/api/visitor", json={"token": f"t{n}"}).status_code for n in range(TRIES_PER_HOUR + 2)]
        assert codes == [403] * TRIES_PER_HOUR + [429] * 2
        elsewhere = visitor.post("/api/visitor", json={"token": "t"}, headers={"cf-connecting-ip": "203.0.113.9"})
        assert elsewhere.status_code == 403                                      # another address has tries of its own


def test_the_operator_is_not_asked_and_each_passed_browser_has_a_count_of_its_own(fms_url, sim, turnstile):
    app = live_app(fms_url, Scripted(), **KEYS)
    with TestClient(app) as first, TestClient(app) as second, TestClient(app) as operator:
        assert type_as(operator, "dana", "From the desk", "press-0001", headers={"X-Desk-Token": TOKEN}).status_code == 200
        pass_the_check(first)
        pass_the_check(second)
        type_as(first, "dana", "One", "press-0001")
        type_as(second, "dana", "Two", "press-0001")
        with app.state.session_factory() as s:
            owners = {m.body: m.visitor for m in s.scalars(select(InboundMessage).where(InboundMessage.visitor.is_not(None)))}
        assert owners["One"] != owners["Two"] and None not in owners.values()


def test_with_no_keys_the_check_is_off_and_the_preview_works_as_before(fms_url, sim, turnstile, monkeypatch):
    app = live_app(fms_url, Scripted())
    with TestClient(app) as visitor:
        page = visitor.get("/inbox")
        assert 'class="cf-turnstile"' not in page.text and "gh_visitor" in page.headers["set-cookie"]       # a plain cookie, as before
        assert pass_the_check(visitor).status_code == 404
        assert type_as(visitor, "dana", "Hello", "press-0001").status_code == 200
    monkeypatch.setenv("DESK_TURNSTILE_SITEKEY", "a-site-key")
    monkeypatch.setenv("DESK_TURNSTILE_SECRET", "a-secret")
    monkeypatch.setenv("DESK_TURNSTILE_HOST", "Gatehouse-Peug.onrender.com")
    settings = Settings.from_env()
    assert (settings.turnstile_sitekey, settings.turnstile_secret, settings.turnstile_host) == ("a-site-key", "a-secret", "Gatehouse-Peug.onrender.com")
    assert Guard(settings.turnstile_sitekey, settings.turnstile_secret, settings.turnstile_host).host == "gatehouse-peug.onrender.com"


def test_one_key_without_the_other_closes_both_paths_and_the_operator_still_works(fms_url, sim, turnstile):
    app = live_app(fms_url, Scripted(), turnstile_sitekey="site-key-for-tests")
    with TestClient(app) as visitor:
        page = visitor.get("/inbox")
        assert 'class="cf-turnstile"' not in page.text
        closed = type_as(visitor, "dana", "Hello", "press-0001")
        assert closed.status_code == 503 and closed.json()["detail"] == UNSET
        assert explore(visitor, {"id": seeded_text(app)}, "press-0002").status_code == 503
        assert pass_the_check(visitor).status_code == 503
        assert type_as(visitor, "dana", "From the desk", "press-0003", headers={"X-Desk-Token": TOKEN}).status_code == 200


def test_the_answer_must_come_from_the_address_the_desk_was_told_to_expect(fms_url, sim, turnstile):
    app = live_app(fms_url, Scripted(), turnstile_host="gatehouse-peug.onrender.com", **KEYS)
    with TestClient(app) as visitor:
        assert pass_the_check(visitor).status_code == 403                            # given on "testserver", the address the request came to
        turnstile.answer = {"success": True, "hostname": "Gatehouse-Peug.onrender.com", "action": ACTION}
        assert pass_the_check(visitor).status_code == 200                            # given where the desk expects it, whatever the Host header says
    assert app.state.guard.host == "gatehouse-peug.onrender.com"


def test_a_cookie_stops_counting_when_its_day_is_over_and_cannot_be_extended():
    clock = {"now": 1_800_000_000}
    guard = Guard("site", "secret", now=lambda: clock["now"])
    cookie = guard.cookie()
    ident, expires, sig = cookie.split(".")
    assert guard.valid(cookie) == ident and int(expires) == clock["now"] + LIFETIME
    assert guard.valid(f"{ident}.{int(expires) + 86400}.{sig}") is None              # a later time on the same signature is a forgery
    clock["now"] += LIFETIME - 1
    assert guard.valid(cookie) == ident
    clock["now"] += 1
    assert guard.valid(cookie) is None                                               # the day is over: the check has to be passed again


def test_the_real_call_sends_the_secret_the_token_and_a_valid_address_and_a_failure_is_unreachable(monkeypatch):
    seen = {}

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"success": True}

    def post(url, data, timeout):
        seen.update(url=url, data=data, timeout=timeout)
        return Reply()
    monkeypatch.setattr(httpx, "post", post)
    guard = Guard("site", "secret")
    assert guard._siteverify("tok", "203.0.113.9") == {"success": True}
    assert seen["url"].endswith("/turnstile/v0/siteverify") and seen["timeout"] == 5.0
    assert seen["data"] == {"secret": "secret", "response": "tok", "remoteip": "203.0.113.9"}
    guard._siteverify("tok", "unknown")
    assert "remoteip" not in seen["data"]                                        # an address that is not one is not sent

    def down(url, data, timeout):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(Unreachable):
        guard._siteverify("tok", "203.0.113.9")
