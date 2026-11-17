"""The agentic FMS on the public site: a real sign-in in front of every FMS page and write, each session on its own copy of the
books, the seven screens drawn from the generated year, a staff decision through the gate, and the old screens gone. A scripted
client stands in for the model."""
import glob
import html
import json
import os
import re
from datetime import date, datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from core.books import at_local
from core.seed import NOW
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
    app = create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False, chat_client=client)
    app.state.front_desk.clock = lambda: NOW       # the fixed day the graded messages and these checks were written for
    return app


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
        home = c.get("/home")
        assert home.status_code == 200 and "Dana Reyes" in home.text
        c.post("/sign-out")
        assert c.get("/home", follow_redirects=False).headers["location"] == "/sign-in"


def test_the_screens_show_the_generated_year_in_words(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        pages = {p: c.get(p) for p in PAGES}
        assert all(r.status_code == 200 for r in pages.values()), {p: r.status_code for p, r in pages.items() if r.status_code != 200}
        text = {p: seen(r.text) for p, r in pages.items()}
        assert "$2,556.00" in text["/home"] and "21 tenants" in text["/home"] and "21 tenants owe $2,556.00" in text["/billing"]
        assert "All 120 units" in text["/home"] and len(re.findall(r"[A-Z][a-z]{2} \d{1,2}, 20\d\d", text["/units"].split("units occupied.", 1)[1])) == 111
        assert "122 tenants" in text["/tenants"]
        assert "Building A" in text["/units"] and "Vacant 9" in text["/units"] and "Row A" not in text["/units"]
        assert "38 of 40 right" in text["/messages/tests"] and "23 of 40 right" in text["/messages/tests"]
        assert "We waive one late fee per tenant" in text["/rules"] and "$25.00" in text["/rules"]
        for name in ("Maria Lopez", "James Carter", "Priya Shah", "Faye Turner"):
            assert name in text["/messages"] and name not in text["/today"]       # read in Messages, decide in Today
        record = seen(c.get(record_of(c, "Maria Lopez")).text)
        assert "Ledger" in record and "Card declined" in record and "November rent" in record
        for p, r in pages.items():
            # the menu down the left with an icon for each item, no tagline under a title, no GitHub inside the FMS
            assert 'class="fms-rail"' in r.text and r.text.count('<svg class="icon"') >= 7 and 'class="tagline"' not in r.text, p
            assert "github" not in r.text.lower(), p
        for p, words in text.items():
            assert not re.search(r"\b[CTLPDH]\d{4}\b", words), p          # no record ids
            assert not re.search(r"\b[a-z]+_[a-z_]+\b", words), (p, re.findall(r"\b[a-z]+_[a-z_]+\b", words)[:3])   # no codes
            assert not re.search(r"(?i)claude|haiku|sonnet|anthropic|openai|gpt|our agent", words), p
        assert c.get("/tenants?q=A108").text.count('href="/tenants/') == 1
        # every month's charges add up: billed is paid, removed or still open
        for key in re.findall(r'<option value="(\d{4}-\d{2})"', pages["/billing"].text):
            box = seen(c.get("/billing", params={"month": key}).text)
            got = {k: float(v.replace(",", "")) for k, v in re.findall(r"(Rent billed|Fees billed|Paid|Removed or waived|Still open) \$([\d,]+\.\d\d)", box)}
            assert len(got) == 5 and round(got["Rent billed"] + got["Fees billed"], 2) == round(got["Paid"] + got["Removed or waived"] + got["Still open"], 2), (key, got)


def test_our_agent_off_sends_every_message_to_staff_and_the_books_stay(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        r = run(c, "james", shift="s2", run_id="r1")
        assert r.status_code == 200 and r.json()["off"] and r.json()["handed"]
        today = seen(c.get("/today").text)
        assert "Handed to staff" in today and "Tilden is off on this copy" in today
        assert "Reversed the second copy" not in seen(c.get("/messages").text)


def test_a_reversal_lands_on_this_copy_only_is_shown_after_a_reload_and_start_over_undoes_it(fms_url, sim):
    app = site(fms_url, Script(reader))
    with sign_in(app) as a, sign_in(app) as b:
        assert run(a, "james", shift="s2", run_id="r1").status_code == 200
        shown = seen(a.get("/messages").text)
        assert "Reversed the second copy" in shown and "Fee income" in shown and "You are right" in shown
        assert "Reading cost" in shown and "1,200 tokens read, 150 written" in shown          # the cost shows in the FMS too, as on the Overview
        james = record_of(a, "James Carter")
        assert "Late fee removed: duplicate" in seen(a.get(james).text)
        assert "removed" not in seen(b.get(james).text)
        assert "Tilden closed 1 message on this copy" in seen(a.get("/today").text)
        a.post("/start-over")
        assert "removed" not in seen(a.get(james).text)


def test_staff_remove_a_fee_she_never_signed_for_through_the_gate_with_a_note(fms_url, sim):
    app = site(fms_url, Script(reader))
    with sign_in(app) as c:
        assert run(c, "priya", shift="s3", run_id="r1").status_code == 200
        today = c.get("/today").text
        card = next(a for a in today.split("<article")[1:] if "Priya Shah" in a)
        assert "Collection paused until the review by Nov 24" in seen(card) and "Today by Tilden" in seen(card)
        dispute = re.search(r'name="dispute" value="([^"]+)"', card).group(1)
        assert c.post("/disputes/decide", data={"dispute": dispute, "action": "remove_disputed_fee", "reason": "x"}).status_code == 422
        r = c.post("/disputes/decide", data={"dispute": dispute, "action": "remove_disputed_fee", "reason": "her signed lease has no admin fee"},
                   follow_redirects=False)
        assert r.status_code == 303
        c.post("/disputes/decide", data={"dispute": dispute, "action": "remove_disputed_fee", "reason": "pressed again"})
        after = seen(c.get("/today").text)
        waiting = after.split("Disputes waiting for a decision", 1)[1].split("Handed to staff", 1)[0]
        assert "Priya Shah" not in waiting and "Nora Feld" in waiting                    # last week's dispute still waits
        assert after.count("Removed the fee") == 1 and "her signed lease has no admin fee" in after
        priya = record_of(c, "Priya Shah")
        assert "Admin fee removed: not in the signed schedule" in seen(c.get(priya).text)


def test_the_day_opens_with_last_weeks_dispute_and_staff_can_settle_it_on_their_copy(fms_url, sim):
    """A first visit finds one dispute from last week waiting, on Home, Today, Units and the tenant's record, all from the same
    records. Removing the fee is one reversal on this copy only; Start over brings the dispute back."""
    app = site(fms_url)
    with sign_in(app) as c, sign_in(app) as other:
        assert "1 dispute waiting for a decision" in seen(c.get("/home").text)
        today = c.get("/today").text
        card = next(a for a in today.split("<article")[1:] if "Nora Feld" in a)
        words = seen(card)
        assert "Recorded Nov 8 by staff at the desk" in words and "Collection paused until the review by Nov 18" in words
        assert "$25.00 admin fee, posted Nov 8 by hand" in words and "Late fee" in words and "Remove the fee" in words
        nora = record_of(c, "Nora Feld")
        unit = re.search(r"unit (C3\d\d)", words).group(1)
        units = c.get("/units").text
        assert f'title="{unit}: Nora Feld, Fee in dispute"' in units and "Fee in dispute 1" in seen(units)
        dispute = re.search(r'name="dispute" value="([^"]+)"', card).group(1)
        for reason in ("her move-in special has no admin fee", "pressed again"):
            assert c.post("/disputes/decide", data={"dispute": dispute, "action": "remove_disputed_fee", "reason": reason},
                          follow_redirects=False).status_code == 303
        record = seen(c.get(nora).text)
        assert record.count("Admin fee removed: not in the signed schedule") == 1
        assert "Nora Feld" not in seen(c.get("/today").text).split("Handed to staff", 1)[0] and "0 disputes" in seen(c.get("/home").text)
        assert "Nora Feld" in seen(other.get("/today").text)                           # another visitor's copy still has it
        c.post("/start-over")
        assert "1 dispute waiting for a decision" in seen(c.get("/home").text) and "removed" not in seen(c.get(nora).text)


class RuleScript(Script):
    """Answers the rule compiler with a fixed proposal and every reading call through `answer`."""
    def __init__(self, answer, rule):
        super().__init__(answer)
        self.rule = rule

    def create(self, **kw):
        if "You turn one sentence" in kw["system"]:
            self.calls += 1
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.rule))],
                                   usage=SimpleNamespace(input_tokens=900, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0))
        return super().create(**kw)


TWO_MONTHS = "Two good months should be enough for one little late fee. Change the six paid-on-time months to two and keep the rest."
TWO_MONTHS_RULE = {"outcome": "adopt", "kind": "waiver", "staff": {"flags": [], "claim_kinds": [], "actions": [], "several_units": False},
                   "waiver": {"per_tenant": None, "window": None, "cap_dollars": None, "on_time_months": 2, "only_when_raised": None,
                              "min_tenure": None, "max_fee_age": None, "no_rent_owed": None, "current_rent_paid": None},
                   "quotes": [{"field": "on_time_months", "words": "Change the six paid-on-time months to two"}], "not_expressed": [],
                   "reason": "Two on-time months before the fee instead of six."}


def paid_in_time(text, charges):
    fee = next(c["charge"] for c in charges if c["about"].startswith("late fee"))
    return {"claims": [{"kind": "paid_on_time", "charge": fee, "quote": text, "date": None, "date_quote": None, "amount_quote": None}], "flags": []}


def test_an_owner_sentence_becomes_an_approved_rule_on_this_copy_and_the_next_case_follows_it(fms_url, sim):
    """Step 2: the owner writes a rule, sees it in words and last year replayed under it, approves it as the owner on their own
    copy, and the next tenant message follows it. Another copy is untouched; a proposal is approved once."""
    app = site(fms_url, RuleScript(paid_in_time, TWO_MONTHS_RULE))
    with sign_in(app) as c, sign_in(app) as other:
        p = c.post("/api/fms/rules", json={"sentence": TWO_MONTHS}).json()
        assert p["outcome"] == "adopt" and any("each of the 2 completed billing months" in w for w in p["words"])
        assert p["replay"]["now_allowed"] >= 1 and p["replay"]["now_refused"] == 0
        assert c.post("/rules/approve", data={"proposal": p["id"]}, follow_redirects=False).headers["location"] == "/rules#in-force"
        assert c.post("/rules/approve", data={"proposal": p["id"]}, follow_redirects=False).headers["location"].startswith("/rules?stale=1")
        rules = seen(c.get("/rules").text)
        assert "Version 2" in rules and TWO_MONTHS in rules and "Version 2" not in seen(other.get("/rules").text)
        assert run(c, "maria", shift="s1", run_id="r1").status_code == 200
        assert "Waived under the owner's rule" in seen(c.get("/messages").text)


STAFF_COPY_RULE = {**TWO_MONTHS_RULE, "kind": "staff", "staff": {"flags": [], "claim_kinds": [], "actions": ["reverse_duplicate"], "several_units": False},
                   "quotes": [{"field": "actions", "words": "a second copy of a charge is reversed only by staff"}], "reason": "Second copies go to staff."}


def test_an_action_the_owner_leaves_to_staff_hands_the_message_over_and_today_names_the_rule(fms_url, sim):
    """The design file's own example: after "a second copy of a charge is reversed only by staff", James's message about a fee
    posted twice goes to staff, nothing is reversed, and Today says the owner's rule sent it; a second Approve lands on Rules."""
    app = site(fms_url, RuleScript(reader, STAFF_COPY_RULE))
    with sign_in(app) as c:
        p = c.post("/api/fms/rules", json={"sentence": "From now on a second copy of a charge is reversed only by staff."}).json()
        assert p["outcome"] == "adopt" and p["words"] == ["Staff, not the agent, do the removing a second copy of a charge."]
        assert c.post("/rules/approve", data={"proposal": p["id"]}, follow_redirects=False).status_code == 303
        again = c.post("/rules/approve", data={"proposal": p["id"]}, follow_redirects=False)
        assert again.status_code == 303 and again.headers["location"].startswith("/rules?stale=1")
        assert "already approved" in seen(c.get("/rules?stale=1").text) and "Sent to staff by the owner's rule" in seen(c.get("/rules").text)
        assert run(c, "james", shift="s2", run_id="r1").status_code == 200
        today = seen(c.get("/today").text)
        handed = today.split("Handed to staff", 1)[1].split("Closed by", 1)[0]
        assert "James Carter" in handed and "owner's rule leaves this action to staff" in handed
        assert "Reversed the second copy" not in seen(c.get("/messages").text)
        assert "Waiting for staff" in c.get("/messages").text


SEVERAL_UNITS_RULE = {**TWO_MONTHS_RULE, "kind": "staff", "staff": {"flags": [], "claim_kinds": [], "actions": [], "several_units": True},
                      "quotes": [{"field": "several_units", "words": "two different storage units"}], "reason": "Several units go to the office."}


def one_rent_claim(text, charges):
    rent = next(c["charge"] for c in charges if c["about"].startswith("rent"))
    return {"claims": [{"kind": "charged_twice", "charge": rent, "quote": text, "date": None, "date_quote": None, "amount_quote": None}], "flags": []}


def test_a_message_naming_rent_on_two_units_goes_to_staff_under_the_several_units_rule(fms_url, sim):
    """Seen with the real model on 2026-11-17: Dev's "$90 and $155" message was read as one claim on one unit, so the rule never
    fired. The units now also come from the rents the message names."""
    app = site(fms_url, RuleScript(one_rent_claim, SEVERAL_UNITS_RULE))
    with sign_in(app) as c:
        p = c.post("/api/fms/rules", json={"sentence": "If a person is complaining about fees on two different storage units in one message, send it to the office."}).json()
        assert p["outcome"] == "adopt"
        c.post("/rules/approve", data={"proposal": p["id"]})
        assert run(c, "dev", shift="s7", run_id="r1").status_code == 200
        handed = seen(c.get("/today").text).split("Handed to staff", 1)[1].split("Closed by", 1)[0]
        assert "Dev Patel" in handed and "several units" in handed


def test_owner_sentences_are_three_a_day_and_an_unsafe_one_is_never_offered_for_approval(fms_url, sim):
    refuse = {**TWO_MONTHS_RULE, "outcome": "refuse", "kind": "none", "quotes": [], "reason": "Rent is never waived."}
    app = site(fms_url, RuleScript(paid_in_time, refuse))
    with sign_in(app) as c:
        for _ in range(3):
            p = c.post("/api/fms/rules", json={"sentence": "Give everyone a month of rent free."}).json()
            assert p["outcome"] == "refuse" and p["replay"] is None
            assert c.post("/rules/approve", data={"proposal": p["id"]}, follow_redirects=False).headers["location"].startswith("/rules?stale=1")
        r = c.post("/api/fms/rules", json={"sentence": "Give everyone a month of rent free."})
        assert r.status_code == 429 and "3 rule readings" in r.json()["detail"]
        assert run(c, "james", text="charged twice").status_code == 200        # rule readings never use up message tries


def test_a_naive_day_start_is_read_as_utc_on_new_york_time():
    from desk.front_desk import as_utc
    assert as_utc(datetime(2026, 11, 16, 5, 0)) == datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)


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
        assert "Tilden" in words and "could not close" not in words
        assert not re.search(r"(?i)surveyed|earlier screens|later steps|separate model|with code deciding|made up and said", words), "internal notes stay off the public page"
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


def test_his_corrections_of_the_night_stay_corrected(fms_url, sim):
    """2026-11-15: a message opens inside Messages, the sign-in comes filled in, and the walkthrough sits below the product picture."""
    app = site(fms_url)
    with TestClient(app) as c:
        page = c.get("/sign-in").text
        assert f'value="{DEMO_NAME}"' in page and f'value="{DEMO_PASSWORD}"' in page
        assert ">Username</label>" in page and "." in DEMO_NAME, "a sign-in asks for a username, and the demo one looks like a real one"
        overview = c.get("/").text
        hero = overview.split('class="ov-hero"', 1)[1].split("</section>", 1)[0]
        assert "<video" not in hero and 'class="ov-shot"' in hero and overview.index('class="ov-shot"') < overview.index('id="walkthrough"')
    with sign_in(app) as c:
        cold = c.get("/messages").text
        conversations = cold.split('class="fms-convs"', 1)[1].split("</ol>", 1)[0]
        assert "/today" not in conversations and conversations.count('href="/messages?unread=') == len(SHIFT)
        assert 'id="read-one"' in cold                       # the first unread message is open, with its own read button
        assert 'id="read-all"' in cold and "read-all" not in c.get("/today").text        # the only read buttons are in Messages


def test_the_date_is_the_real_day_and_a_new_day_brings_new_books(fms_url, sim):
    """The FMS date is today's date and changes every day, with no second date explained anywhere."""
    app = site(fms_url, Script(reader))
    fd = app.state.front_desk
    with sign_in(app) as c:
        home = c.get("/home").text
        assert "Sat, Nov 14, 2026" in seen(home) and not re.search(r"(?i)facility date|demo date", home)
        assert run(c, "james", shift="s2", run_id="r1").status_code == 200
        fd.clock = lambda: at_local(date(2026, 12, 3), 8)
        assert "Thu, Dec 3, 2026" in seen(c.get("/home").text)
        assert "Reversed the second copy" not in seen(c.get("/messages").text)          # yesterday's copy stayed with yesterday
        root, ext = os.path.splitext(fd.path)
        assert [os.path.basename(p) for p in glob.glob(f"{glob.escape(root)}-*{ext}")] == [os.path.basename(f"{root}-2026-12-03{ext}")]   # Nov 14's copy went
        maria = next(t["text"] for t in fd.tenants() if t["shift"] == "s1")
        assert maria.startswith("I paid on November 3rd")                               # "the 3rd" would now read as Dec 3
        assert run(c, "maria", text=maria, shift="s1", run_id="r2").status_code == 200
