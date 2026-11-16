"""The agentic FMS on the public site: a real sign-in in front of every FMS page and write, each session on its own copy of the
books, the seven screens drawn from the generated year, a staff decision through the gate, and the old screens gone. A scripted
client stands in for the model."""
import html
import json
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from desk import spend
from desk.adapter.cubby import CubbyAdapter
from desk.api import create_app
from desk.config import Settings
from desk.fms import DEMO_NAME, DEMO_PASSWORD, PUBLIC, SESSION
from desk.front_desk import SHIFT

from .conftest import FULL_KEY

PAGES = ["/home", "/today", "/messages", "/messages/try", "/messages/tests", "/units", "/tenants", "/billing", "/rules"]


class Script:
    """Answers every reading call with claims built by `answer(text, charges)`."""
    def __init__(self, answer):
        self.answer, self.messages, self.calls = answer, self, 0

    def create(self, **kw):
        self.calls += 1
        content = kw["messages"][0]["content"]
        charges = json.loads(content.split("newest first:\n", 1)[1].split("\n\n<tenant_message>", 1)[0])
        text = content.split("<tenant_message>\n", 1)[1].rsplit("\n</tenant_message>", 1)[0]
        body = json.dumps(self.answer(text, charges))
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=body)],
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=150, cache_read_input_tokens=0, cache_creation_input_tokens=0))


def reader(text, charges):
    def claim(kind, about):
        found = next((c["charge"] for c in charges if c["about"].startswith(about)), None)
        return {"claims": [{"kind": kind, "charge": found, "quote": text, "date": None, "date_quote": None, "amount_quote": None}] if found else [],
                "flags": []}
    return claim("never_agreed", "admin fee") if "admin fee" in text else claim("charged_twice", "late fee, due Nov 10")


def site(fms_url, client=None, **kw):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=12.0,
                        reconcile_every_seconds=60, operator_token="op-token", staff_token="staff-token", public_view=True, demo_seed=True,
                        public_investigate=client is not None, **kw)
    return create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False, chat_client=client)


def sign_in(app) -> TestClient:
    c = TestClient(app)
    r = c.post("/sign-in", data={"name": DEMO_NAME, "password": DEMO_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/home", r.text
    return c


def seen(page: str) -> str:
    """What a person reads on a page: the text outside tags, scripts and styles."""
    page = re.sub(r"(?s)<(script|style)\b.*?</\1>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


def record_of(c, name: str) -> str:
    """The address of a tenant's record, found the way staff find it: by name on Tenants."""
    first = name.split()[0].lower()
    return re.search(rf'href="(/tenants/{first}-[a-z0-9-]+)"', c.get("/tenants", params={"q": name}).text).group(1)


def run(c, role, text=None, shift=None, run_id=None):
    item = next((i for i in SHIFT if i["role"] == role), None)
    body = {"tenant": role, "text": text or item["text"], "shift": shift, "run": run_id}
    return c.post("/api/fms/messages", json=body)


def test_every_fms_page_and_write_refuses_anyone_not_signed_in(fms_url, sim):
    app = site(fms_url, Script(reader))
    with TestClient(app) as anyone:
        forged = TestClient(app, cookies={SESSION: app.state.guard.cookie("visitor")})     # a bot-check pass is not a sign-in
        checked = 0
        for route in app.routes:
            path = getattr(route, "path", "")
            if path in PUBLIC or path.startswith("/static"):
                continue
            url = re.sub(r"\{[^}]+\}", "x", path)
            for method in getattr(route, "methods", ()):
                for c in (anyone, forged):
                    r = c.request(method, url, json={"tenant": "james", "text": "hi"}, follow_redirects=False)
                    assert (r.status_code, r.headers.get("location")) in ((303, "/sign-in"), (401, None)), (method, url, r.status_code)
                checked += 1
        assert checked >= len(PAGES) + 4


def test_the_demo_login_signs_in_and_out_and_a_wrong_one_is_refused(fms_url, sim):
    app = site(fms_url)
    with TestClient(app) as c:
        page = c.get("/sign-in").text
        assert DEMO_NAME in page and DEMO_PASSWORD in page
        wrong = c.post("/sign-in", data={"name": DEMO_NAME, "password": "guess"}, follow_redirects=False)
        assert wrong.status_code == 401 and "do not match" in wrong.text and SESSION not in wrong.cookies
        assert c.post("/sign-in", data={"name": DEMO_NAME.upper(), "password": DEMO_PASSWORD}, follow_redirects=False).status_code == 303
        assert c.get("/home").status_code == 200
        c.post("/sign-out")
        assert c.get("/home", follow_redirects=False).headers["location"] == "/sign-in"


def test_the_screens_show_the_generated_year_in_words(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        pages = {p: c.get(p) for p in PAGES}
        assert all(r.status_code == 200 for r in pages.values()), {p: r.status_code for p, r in pages.items() if r.status_code != 200}
        text = {p: seen(r.text) for p, r in pages.items()}
        assert "22 tenants owe $2,808.00" in text["/home"] and "22 tenants owe $2,808.00" in text["/billing"]
        assert "All 120 units" in text["/home"] and len(re.findall(r"[A-Z][a-z]{2} \d{1,2}, 20\d\d", text["/units"].split("Row A:", 1)[1])) == 110
        assert "121 tenants" in text["/tenants"]
        assert "Row A: " in text["/units"] and "Vacant" in text["/units"]
        assert "38 of 40 right" in text["/messages/tests"] and "23 of 40 right" in text["/messages/tests"]
        assert "We waive one late fee per tenant" in text["/rules"] and "$25.00" in text["/rules"]
        for name in ("Maria Lopez", "James Carter", "Priya Shah", "Faye Turner"):
            assert name in text["/today"]
        record = seen(c.get(record_of(c, "Maria Lopez")).text)
        assert "Ledger" in record and "Card declined" in record and "November rent" in record
        for p, r in pages.items():
            # his corrections of 2026-11-15: the menu down the left with an icon for each item, no tagline under a title, no GitHub inside
            assert 'class="fms-rail"' in r.text and r.text.count('<svg class="icon"') >= 7 and 'class="tagline"' not in r.text, p
            assert "github" not in r.text.lower(), p
        for p, words in text.items():
            assert not re.search(r"\b[CTLPDH]\d{4}\b", words), p          # no record ids
            assert not re.search(r"\b[a-z]+_[a-z_]+\b", words), (p, re.findall(r"\b[a-z]+_[a-z_]+\b", words)[:3])   # no codes
            assert not re.search(r"(?i)claude|haiku|sonnet|anthropic|openai|gpt|our agent", words), p
        assert c.get("/tenants?q=A108").text.count('href="/tenants/') == 1


def test_our_agent_off_sends_every_message_to_staff_and_the_books_stay(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        r = run(c, "james", shift="s2", run_id="r1")
        assert r.status_code == 200 and r.json()["off"] and r.json()["handed"]
        today = seen(c.get("/today").text)
        assert "Handed to staff" in today and "the agent is off" in today.lower()
        assert "Reversed the second copy" not in seen(c.get("/messages").text)


def test_a_reversal_lands_on_this_copy_only_is_shown_after_a_reload_and_start_over_undoes_it(fms_url, sim):
    app = site(fms_url, Script(reader))
    with sign_in(app) as a, sign_in(app) as b:
        assert run(a, "james", shift="s2", run_id="r1").status_code == 200
        shown = seen(a.get("/messages").text)
        assert "Reversed the second copy" in shown and "Fee income" in shown and "You are right" in shown
        james = record_of(a, "James Carter")
        assert "Late fee removed: duplicate" in seen(a.get(james).text)
        assert "removed" not in seen(b.get(james).text)
        assert "James Carter" in seen(a.get("/today").text).split("Closed by Porter", 1)[1]
        a.post("/start-over")
        assert "removed" not in seen(a.get(james).text)


def test_staff_remove_a_fee_she_never_signed_for_through_the_gate_with_a_note(fms_url, sim):
    app = site(fms_url, Script(reader))
    with sign_in(app) as c:
        assert run(c, "priya", shift="s3", run_id="r1").status_code == 200
        today = c.get("/today").text
        assert "Collection paused until the review by Nov 24" in seen(today)
        dispute = re.search(r'name="dispute" value="([^"]+)"', today).group(1)
        assert c.post("/disputes/decide", data={"dispute": dispute, "action": "remove_disputed_fee", "reason": "x"}).status_code == 422
        r = c.post("/disputes/decide", data={"dispute": dispute, "action": "remove_disputed_fee", "reason": "her signed lease has no admin fee"},
                   follow_redirects=False)
        assert r.status_code == 303
        c.post("/disputes/decide", data={"dispute": dispute, "action": "remove_disputed_fee", "reason": "pressed again"})
        after = seen(c.get("/today").text)
        assert "No open disputes" in after and after.count("Removed the fee") == 1 and "her signed lease has no admin fee" in after
        priya = record_of(c, "Priya Shah")
        assert "Admin fee removed: not in the signed schedule" in seen(c.get(priya).text)


def test_ten_tries_a_day_a_morning_counts_once_and_start_over_gives_none_back(fms_url, sim):
    with sign_in(site(fms_url, Script(reader))) as c:
        for item in SHIFT:                      # the whole morning under one run id: one try
            assert run(c, item["role"], shift=item["id"], run_id="one").status_code == 200
        assert run(c, "maria", shift="s1", run_id="one").status_code == 409
        for _ in range(9):
            assert run(c, "james", text="charged twice").status_code == 200
        c.post("/start-over")
        refused = run(c, "james", text="charged twice")
        assert refused.status_code == 429 and "10 tries" in refused.json()["detail"]
        assert run(c, "james", text="something else", shift="s2", run_id="x").status_code == 422
        assert spend.SOURCES["front_desk"](spend.day_start()) > 0


def test_the_overview_is_public_and_the_old_screens_are_gone(fms_url, sim):
    app = site(fms_url)
    with TestClient(app) as anyone:
        page = anyone.get("/")
        assert page.status_code == 200
        words = seen(page.text)
        assert "38 right" in words and "handled 23" in words and "120 units" in words and 'href="/sign-in"' in page.text
        assert not re.search(r"(?i)claude|haiku|sonnet|anthropic|openai|gpt|our agent|github", re.sub(r"(?s)<footer.*</footer>", "", page.text)), "GitHub belongs in the footer only"
        assert "Porter" in words and "could not close" not in words
    with sign_in(app) as c:
        for path in ("/evaluation", "/inbox", "/front-desk", "/calls-and-reminders", "/rent-changes", "/operations", "/api/inbox/unrouted",
                     "/api/front-desk/tenants", "/api/live-check"):
            assert c.get(path).status_code == 404, path
        for path in ("/api/visitor", "/webhooks/cubby/sms-received", "/api/front-desk/cases", "/api/reconcile"):
            assert c.post(path, json={}).status_code == 404, path


def test_with_the_bot_check_on_a_sign_in_needs_a_passed_check_and_a_half_set_check_closes_it(fms_url, sim):
    app = site(fms_url, Script(reader), turnstile_sitekey="site", turnstile_secret="secret", turnstile_host="testserver")
    guard = app.state.guard
    assert guard.on
    guard._ask = lambda token, address: {"success": token == "passed", "action": "visit", "hostname": "testserver"}
    with TestClient(app) as c:
        assert 'data-sitekey="site"' in c.get("/sign-in").text
        login = {"name": DEMO_NAME, "password": DEMO_PASSWORD}
        refused = c.post("/sign-in", data={**login, "cf-turnstile-response": "forged"}, follow_redirects=False)
        assert refused.status_code == 403 and SESSION not in refused.cookies
        assert c.get("/home", follow_redirects=False).status_code == 303
        ok = c.post("/sign-in", data={**login, "cf-turnstile-response": "passed"}, follow_redirects=False)
        assert ok.status_code == 303 and c.get("/home").status_code == 200
    half = site(fms_url, Script(reader), turnstile_sitekey="site")
    with TestClient(half) as c:
        r = c.post("/sign-in", data={"name": DEMO_NAME, "password": DEMO_PASSWORD}, follow_redirects=False)
        assert r.status_code == 503 and SESSION not in r.cookies
