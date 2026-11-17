"""The agentic FMS: Northstar Storage's own books (core/) behind a sign-in, the work Tilden (the agent inside it) did on them, and
what waits for a person. The Overview at / is the only public page. Every FMS page and every write needs a signed-in session, and each session
works on its own copy of the books (its own stream), so one visitor's changes never reach the next. The demo name and password
are printed on the sign-in page: the sign-in is real (a signed session cookie, refused when missing or forged), the person is not."""
from __future__ import annotations

import hmac
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from core.case import OFF
from core.books import ADMIN_FEE_CENTS, LATE_FEE_CENTS, RENT, REVIEW_DAYS, lines, local, month_key
from core.evidence import KIND_WORDS, day, money, month_words
from core.gate import ACTIONS, STAFF_ACTIONS, STAFF_RULE, Command, execute
from core.rules import staff_words, waiver_words
from core.seed import story_month

from . import spend
from .web import AGENT
from .front_desk import ACTION_WORDS, CLAIM_WORDS, SHIFT, CaseIn, FrontDesk, RuleIn, account_words
from .guard import LIFETIME, SLOW_DOWN, UNSET, Unreachable

DEMO_NAME, DEMO_PASSWORD = "dana.reyes", "northstar"
SESSION = "staddle_session"
STAFF = "staff:office_manager"
# the menu in the groups an FMS is organised by; a module joins its group when it is built, never as a greyed-out item
MENU = (("Run the facility", (("home", "Home", "house"), ("today", "Today", "list-checks"), ("messages", "Messages", "message-square"))),
        ("Facility and tenants", (("units", "Units", "warehouse"), ("tenants", "Tenants", "users"))),
        ("Money", (("billing", "Billing and payments", "credit-card"),)),
        ("Setup", (("rules", "Rules", "scale"),)))
# the only addresses that answer without a session; tests/test_fms.py walks every route against this list
PUBLIC = {"/", "/film", "/sign-in", "/sign-out", "/static", "/health/live", "/health/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
SCORE = Path(__file__).parent / "static" / "first_shift.json"
STAFF_WORDS = {"remove_disputed_fee": "Removed the fee", "keep_under_review": "Kept under review"}
MAY_DO = {"explain_charge": "Explain a charge from the records", "reverse_duplicate": "Reverse the second copy of a charge posted twice",
          "correct_fee_posted_in_error": "Remove a late fee posted in error", "open_dispute": "Record a dispute for the office manager",
          "waive_fee": "Waive a late fee under the owner's rule", "hand_to_staff": "Hand a message to staff, with the reason"}
STAFF_DO = {"remove_disputed_fee": "Remove a fee the tenant never signed for", "keep_under_review": "Keep a dispute under review until its date"}
BY = {"billing": "Billing", "operator": AGENT, "fixed_workflow": "Fixed workflow"}
JOB_WORDS = {"understand": "Reading", "operate": "Choosing"}
WRONG = "That name and password do not match the demo login shown on this page."


def slug(name: str, unit: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{name} {unit}".lower()).strip("-")


def phone(p: str) -> str:
    d = re.sub(r"\D", "", p)[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else p


def date_words(d: date) -> str:
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


def dated(d: date, year: int) -> str:
    """A day, with its year when it is not this year: a ledger runs back past January."""
    return day(d) if d.year == year else f"{day(d)}, {d.year}"


def words(key: str, value) -> tuple[str, str]:
    """One field of a typed rule as words: cents as dollars, flags as yes or no, codes without underscores."""
    if key.endswith("_cents"):
        return key.removesuffix("_cents").replace("_", " "), money(value)
    if isinstance(value, bool):
        return key.replace("_", " "), "yes" if value else "no"
    return key.replace("_", " "), str(value).replace("_", " ")


class Facility:
    """One session's view of the books: the folded state, the session's own events, and every tenant with their units and money."""

    def __init__(self, fd: FrontDesk, ident: str, everything: bool = False):
        self.b, self.own, self.balanced, self.debits, self.events = fd.view(ident, everything)
        self.fd, self.today, self.now = fd, fd.day, fd.clock()
        b = self.b
        self.leases_of: dict[str, list] = {}
        for lease in sorted(b.leases.values(), key=lambda x: (x.ended is not None, x.unit)):
            self.leases_of.setdefault(lease.tenant, []).append(lease)
        last_paid: dict[str, object] = {}
        for p in b.payments.values():
            if not p.returned and (p.tenant not in last_paid or p.effective_at > last_paid[p.tenant].effective_at):
                last_paid[p.tenant] = p
        self.people: dict[str, dict] = {}
        for t in b.tenants.values():
            ls = self.leases_of.get(t.id, [])
            active = [x for x in ls if x.ended is None]
            self.people[t.id] = {"id": t.id, "name": t.name, "phone": phone(t.phone), "slug": slug(t.name, ls[0].unit if ls else "none"),
                                 "units": [x.unit for x in (active or ls)], "active": bool(active), "past": b.past_due(t.id, self.now),
                                 "owed": sum(b.receivable(x.id) for x in ls) - b.credit.get(t.id, 0), "credit": b.credit.get(t.id, 0),
                                 "dispute": any(d.tenant == t.id and d.outcome is None for d in b.disputes.values()),
                                 "since": f"{day(min(x.start for x in (active or ls)))}, {min(x.start for x in (active or ls)).year}" if ls else "",
                                 "rent": sum(x.rent_cents for x in active),
                                 "paid": (f"{money(last_paid[t.id].cents)} on {day(last_paid[t.id].effective_at)}" if t.id in last_paid else "")}
        self.by_slug = {p["slug"]: p for p in self.people.values()}
        self.role_of = {c["tenant"]: role for role, c in fd.cast.items()}

    @property
    def name(self) -> str:
        return self.b.facility.get("name", "")

    @staticmethod
    def chips(p: dict) -> list[tuple[str, str]]:
        out = []
        if not p["active"]:
            out.append(("Moved out", "plain"))
        if p["past"]:
            out.append((f"Past due {money(p['past'])}", "act"))
        if p["dispute"]:
            out.append(("Fee in dispute", "wait"))
        if p["credit"]:
            out.append((f"Credit {money(p['credit'])}", "plain"))
        return out

    # the work ---------------------------------------------------------------------------------------------------------
    def cases(self) -> list[dict]:
        """Every message worked on this copy, newest first, redrawn from what was recorded: the words, what was read, what the
        records showed, what was done, the books entries and the reply."""
        b, out = self.b, []
        for e in self.own:
            if e.kind not in ("case.closed", "case.handed_over"):
                continue
            p, case = e.payload, e.payload["case"]
            who = self.people.get(p["tenant"] or "")
            claims = []
            for c in p["claims"]:
                ch = b.charges.get(c.get("charge") or "")
                about = None
                if ch is not None:
                    about = f"{KIND_WORDS.get(ch.kind, ch.kind)} due {day(ch.due)}" + (f", unit {b.leases[ch.lease].unit}" if who and len(who["units"]) > 1 else "")
                claims.append({"what": CLAIM_WORDS.get(c["kind"], c["kind"]), "quote": c["quote"], "about": about})
            outcomes = [{**o, "words": ACTION_WORDS.get(o["action"], o["action"])} for o in p.get("outcomes", [])]
            entries = [{"account": account_words(a, b), "debit": money(c) if c > 0 else "", "credit": money(-c) if c < 0 else ""}
                       for x in self.own if x.command and x.command.startswith(case + ":") for a, c, _ in lines(x.kind, x.payload)]
            shift = case.rsplit(":", 1)[-1]
            def named(o: dict) -> str:
                ch = b.charges.get(o.get("charge") or "")
                return f"{KIND_WORDS.get(ch.kind, ch.kind).capitalize()} due {day(ch.due)}: {o['words'][:1].lower()}{o['words'][1:]}" if ch else o["words"]
            done = list(dict.fromkeys(named(o) for o in outcomes if o["allowed"] and o["action"] != "hand_to_staff"))
            # why it went to staff in the records' own words when a claim says so, else the case's reason
            why = next((o["says"] for o in outcomes if o["action"] == "hand_to_staff" and o["says"]), None)
            if any(o.get("by") == "owner rule" for o in outcomes):      # the owner's own rule sent it: say so, not the records
                why = p["handed"]
            out.append({"n": e.id, "who": who, "text": p["text"], "handed": p["handed"], "why": why or p["handed"], "reply": p["reply"], "claims": claims,
                        "outcomes": outcomes, "asked": [a for a in p.get("asked", []) if not a["allowed"]], "entries": entries,
                        "summary": ". ".join(done) + "." if done else "Answered from the records.",
                        # a message can be answered while the work it started still waits: a dispute stays open for staff
                        "waits": bool(who and who["dispute"] and any(o["action"] == "open_dispute" and o["allowed"] for o in outcomes)),
                        "cents": round(sum(c.get("cents", 0) for c in p.get("calls", [])), 4),
                        # what each model call cost, as the Overview shows it
                        "calls": [{"job": JOB_WORDS.get(c["job"], "Model call"), "tokens_in": c["tokens_in"], "tokens_out": c["tokens_out"],
                                   "cents": c["cents"], "seconds": c["ms"] / 1000, "ok": c["outcome"] == "ok"} for c in p.get("calls", [])],
                        "off": p["handed"] == OFF,
                        "shift": shift if any(i["id"] == shift for i in SHIFT) else None})
        return out[::-1]

    def waiting(self, cases: list[dict]) -> list[dict]:
        """This morning's messages that this copy has not worked yet."""
        done = {c["shift"] for c in cases if c["shift"]}
        out = []
        for t in self.fd.tenants():
            if t["shift"] not in done:
                out.append({**t, "who": self.people[self.fd.cast[t["role"]]["tenant"]]})
        return out

    def disputes(self) -> list[dict]:
        """Open disputes, with what staff need to decide: the fee, who posted it, the tenant's words, the schedule they signed,
        whether collection is paused, and the account before and after removing it."""
        b, out = self.b, []
        mine = {e.payload["dispute"] for e in self.own if e.kind == "dispute.opened"}      # recorded on this copy, by the agent
        for d in sorted(b.disputes.values(), key=lambda x: (x.review_by, x.id)):
            if d.outcome is not None:
                continue
            ch = b.charges[d.charge]
            lease = b.leases[ch.lease]
            who = self.people[d.tenant]
            out.append({"id": d.id, "who": who, "fee": KIND_WORDS.get(ch.kind, ch.kind), "amount": money(ch.cents), "unit": lease.unit,
                        "posted": day(ch.posted_at), "by_hand": ch.source.startswith("staff"), "quote": d.quote, "review_by": day(d.review_by),
                        "recorded": f"{'today' if local(d.opened_at).date() == self.today else day(d.opened_at)} by {AGENT if d.id in mine else 'staff at the desk'}",
                        "held": b.held(ch.id), "in_schedule": ch.kind in lease.schedule, "signed": f"{day(lease.start)}, {lease.start.year}",
                        "schedule": ", ".join(KIND_WORDS.get(k, k) for k in lease.schedule) or "no fees",
                        "owed_now": money(who["owed"]), "owed_after": money(who["owed"] - ch.open_cents)})
        return out

    def decisions(self) -> list[dict]:
        out = []
        for e in self.own:
            if e.kind == "gate.decided" and e.source.startswith("staff:"):
                p = e.payload
                ch = self.b.charges.get(p.get("charge") or "")
                out.append({"who": self.people[p["tenant"]], "words": STAFF_WORDS.get(p["action"], p["action"]), "allowed": p["allowed"],
                            "removed": p["allowed"] and p["action"] == "remove_disputed_fee",
                            "check": p["check"], "reason": p.get("reason"), "fee": f"{money(ch.cents)} {KIND_WORDS.get(ch.kind, ch.kind)}" if ch else ""})
        return out[::-1]

    # the facility -----------------------------------------------------------------------------------------------------
    def occupancy(self) -> list[dict]:
        rented = {x.unit for x in self.b.leases.values() if x.ended is None}
        rows = sorted({u[0] for u in self.b.units})
        return [{"row": r, "units": sum(1 for u in self.b.units if u[0] == r), "rented": sum(1 for u in self.b.units if u[0] == r and u in rented)}
                for r in rows]

    def month(self, key: str) -> dict:
        b = self.b
        charges = [c for c in b.charges.values() if month_key(c.due) == key]
        pays = sorted((p for p in b.payments.values() if month_key(local(p.effective_at).date()) == key), key=lambda p: p.effective_at, reverse=True)
        declined = [x for x in b.declined if month_key(local(x["at"]).date()) == key]
        rows = []
        for p in pays:
            kinds = sorted({KIND_WORDS.get(b.charges[a["charge"]].kind, "") for a in p.applications})
            rows.append({"who": self.people[p.tenant], "amount": money(p.cents), "on": day(p.effective_at), "returned": p.returned,
                         "learned": day(p.known_at) if local(p.known_at).date() != local(p.effective_at).date() else None,
                         "to": " and ".join(kinds) + (" and credit" if p.credit_cents else "") if kinds else ("credit" if p.credit_cents else "")})
        # the month's charges add up: every cent billed is paid, removed (reversed or waived) or still open
        return {"key": key, "name": f"{month_words(key)} {key[:4]}",
                "rent": sum(c.cents for c in charges if c.kind == RENT),
                "fees": sum(c.cents for c in charges if c.kind != RENT),
                "paid": sum(c.paid for c in charges),
                "removed": sum(c.cents - c.paid for c in charges if c.closed),
                "collected": sum(p.cents for p in pays if not p.returned),
                "returned": sum(1 for p in pays if p.returned),
                "declined": [{"who": self.people[x["tenant"]], "on": day(x["at"]), "amount": money(x["cents"])} for x in declined],
                "open": sum(c.open_cents for c in charges), "payments": rows}

    def months(self) -> list[tuple[str, str]]:
        keys = sorted({month_key(c.due) for c in self.b.charges.values() if c.due <= self.today}, reverse=True)
        return [(k, f"{month_words(k)} {k[:4]}") for k in keys]

    def past_due(self) -> list[dict]:
        rows = []
        for p in self.people.values():
            if p["past"]:
                oldest = min((c.due for c in self.b.open_charges(p["id"]) if c.due < self.today), default=self.today)
                rows.append({**p, "oldest": day(oldest), "days": (self.today - oldest).days})
        return sorted(rows, key=lambda r: (-r["days"], -r["past"]))

    def units(self) -> list[tuple[str, list[dict]]]:
        b = self.b
        active = {x.unit: x for x in b.leases.values() if x.ended is None}
        disputed = {b.charges[d.charge].lease for d in b.disputes.values() if d.outcome is None}
        rows: dict[str, list[dict]] = {}
        for u in b.units:
            lease = active.get(u)
            if lease is None:
                last = max((x.ended for x in b.leases.values() if x.unit == u and x.ended), default=None)
                item = {"unit": u, "chip": ("Vacant", "plain"), "who": None, "rent": "", "since": f"Empty since {day(last)}" if last else "Not rented this year"}
            else:
                p = self.people[lease.tenant]
                chip = ("Fee in dispute", "wait") if lease.id in disputed else (("Past due", "act") if p["past"] else None)
                item = {"unit": u, "chip": chip, "who": p, "rent": money(lease.rent_cents), "since": f"{day(lease.start)}, {lease.start.year}"}
            rows.setdefault(u[0], []).append(item)
        return sorted(rows.items())

    def ledger(self, tenant: str) -> list[dict]:
        """The tenant's account line by line, newest first, with the balance after each line. Needs everything=True."""
        b = self.b
        mine = self.leases_of.get(tenant, [])
        accounts = {f"receivable:{x.id}" for x in mine} | {f"credit:{tenant}"}
        several = len(mine) > 1
        rows, balance = [], 0
        for e in self.events:
            p = e.payload
            if e.kind == "payment.declined" and p.get("tenant") == tenant:
                rows.append({"on": dated(local(e.at).date(), self.today.year), "what": f"Card declined, {money(p['cents'])}", "by": "Card", "amount": "", "balance": money(balance)})
                continue
            delta = sum(c for a, c, _ in lines(e.kind, p) if a in accounts)
            if delta == 0:
                continue
            balance += delta
            unit = f", unit {b.leases[p['lease']].unit}" if several and p.get("lease") in b.leases else ""
            when = dated(local(e.at).date(), self.today.year)
            if e.kind == "charge.posted":
                obligation = p.get("obligation") or ""
                what = (f"{month_words(obligation.split(':')[2])} rent" if p["kind"] == RENT and obligation.count(":") == 2
                        else KIND_WORDS.get(p["kind"], p["kind"]).capitalize()) + unit + (", posted by hand" if (p.get("source") or "").startswith("staff") else "")
                by = "Staff" if (p.get("source") or "").startswith("staff") else "Billing"
            elif e.kind == "payment.received":
                eff = p["effective_at"]
                when = dated(date.fromisoformat(eff[:10]), self.today.year)
                what = "Card payment" + (f", learned {day(e.at)}" if eff[:10] != local(e.at).date().isoformat() else "")
                by = "Card"
            elif e.kind == "payment.returned":
                what, by = "Payment returned by the bank", "Card"
            elif e.kind == "charge.reversed":
                what = f"{KIND_WORDS.get(p['kind'], p['kind']).capitalize()} removed: {p['reason']}"
                by = "Staff" if e.source.startswith("staff") else BY.get(e.source, "Billing")
            elif e.kind == "charge.waived":
                what = f"{KIND_WORDS.get(p['kind'], p['kind']).capitalize()} waived under the owner's rule"
                by = "Staff" if e.source.startswith("staff") else BY.get(e.source, "Billing")
            else:
                what, by = e.kind.replace(".", " ").replace("_", " ").capitalize(), BY.get(e.source, "Billing")
            rows.append({"on": when, "what": what, "by": by, "amount": money(delta) if delta > 0 else f"-{money(-delta)}", "balance": money(balance)})
        return rows[::-1]

    def rentals(self, tenant: str) -> list[dict]:
        return [{"unit": x.unit, "rent": money(x.rent_cents), "start": f"{day(x.start)}, {x.start.year}",
                 "ended": f"{day(x.ended)}, {x.ended.year}" if x.ended else None,
                 "schedule": ", ".join(KIND_WORDS.get(k, k) for k in x.schedule) or "no fees", "owed": money(self.b.receivable(x.id))}
                for x in self.leases_of.get(tenant, [])]

    def rules(self) -> dict:
        rule = self.b.rules.get("late_fee_waiver")
        staff_rule = self.b.rules.get(STAFF_RULE)
        return {"waiver": rule, "typed": [words(k, v) for k, v in (rule or {}).get("typed", {}).items() if not isinstance(v, dict)],
                "waiver_words": waiver_words(rule["typed"]) if rule else [], "staff_rule": staff_rule,
                "staff_rule_words": staff_words(staff_rule["typed"]) if staff_rule else [],
                "late_fee": money(LATE_FEE_CENTS), "admin_fee": money(ADMIN_FEE_CENTS), "review_days": REVIEW_DAYS,
                "actions": [(MAY_DO[a], a in ("reverse_duplicate", "correct_fee_posted_in_error", "waive_fee")) for a in ACTIONS],
                "staff": [STAFF_DO[a] for a in STAFF_ACTIONS]}


async def form(request: Request, limit: int = 8192) -> dict[str, str]:
    """A small form post, read up to its limit: a bigger body is refused before it is held in memory."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, "That form is too large.")
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}


def mount(app, fd: FrontDesk, settings, guard, from_elsewhere, session_factory, live: bool) -> None:
    spend.SOURCES["front_desk"] = fd.spent_dollars
    from .web import templates

    def session(request: Request) -> str | None:
        return guard.valid(request.cookies.get(SESSION), "session")

    def need(request: Request) -> str:
        ident = session(request)
        if ident is None:
            if request.url.path.startswith("/api/"):
                raise HTTPException(401, "Sign in first.")
            raise HTTPException(303, "Sign in first.", headers={"Location": "/sign-in"})
        return ident

    def same_site(request: Request) -> None:
        if from_elsewhere(request):
            raise HTTPException(403, "this request came from another site")

    def show(request: Request, name: str, nav: str, f: Facility, **ctx):
        return templates.TemplateResponse(request, name, {"nav": nav, "menu": MENU, "agent": AGENT, "f": f, "today": date_words(f.today), "live": live,
                                                          "money": money, "day": day, **ctx})

    def check() -> dict | None:
        return {"sitekey": guard.sitekey} if guard.on else None

    # public ------------------------------------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def overview(request: Request):
        # the worked example names the month the FMS's own story sits in, so the two never disagree
        return templates.TemplateResponse(request, "overview.html", {"nav": "front", "facts": fd.facts(), "score": json.loads(SCORE.read_text()),
                                                                     "month": f"{story_month(fd.today()):%B}",
                                                                     "money": money, "agent": AGENT, "signed_in": session(request) is not None})

    @app.get("/film", include_in_schema=False)
    def film_moved():
        return RedirectResponse("/", status_code=301)

    @app.get("/sign-in", include_in_schema=False)
    def sign_in_page(request: Request):
        if session(request):
            return RedirectResponse("/home", status_code=303)
        return templates.TemplateResponse(request, "sign_in.html", {"nav": "sign-in", "check": check(), "demo": (DEMO_NAME, DEMO_PASSWORD)})

    @app.post("/sign-in", include_in_schema=False)
    async def sign_in(request: Request):
        same_site(request)

        def refuse(text: str, status: int):
            return templates.TemplateResponse(request, "sign_in.html", {"nav": "sign-in", "check": check(), "demo": (DEMO_NAME, DEMO_PASSWORD),
                                                                       "error": text, "name": name[:60]}, status_code=status)
        data = await form(request)
        name, password = data.get("name", "").strip().lower(), data.get("password", "")
        if guard.broken:
            return refuse(UNSET.replace("texts and runs are closed", "signing in is closed"), 503)
        # both compared in full whatever the first says, so the time taken does not tell which one was wrong
        right = hmac.compare_digest(name.encode(), DEMO_NAME.encode()) & hmac.compare_digest(password.encode(), DEMO_PASSWORD.encode())
        if not right:
            return refuse(WRONG, 401)
        if guard.on:
            if not guard.allow(spend.address_of(request)):
                return refuse(SLOW_DOWN, 429)
            try:
                passed = await run_in_threadpool(guard.passed, data.get("cf-turnstile-response", ""), spend.address_of(request))
            except Unreachable:
                return refuse("The quick check is not answering. Wait a minute, then reload the page.", 503)
            if not passed:
                return refuse("The quick check did not pass. Reload the page and try again.", 403)
        response = RedirectResponse("/home", status_code=303)
        response.set_cookie(SESSION, guard.cookie("session"), max_age=LIFETIME, httponly=True, samesite="lax", secure=spend.is_https(request))
        return response

    # signed in ---------------------------------------------------------------------------------------------------------
    @app.post("/sign-out", include_in_schema=False)
    def sign_out(request: Request):
        same_site(request)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION)
        return response

    @app.post("/start-over", include_in_schema=False)
    async def start_over(request: Request):
        same_site(request)
        ident = need(request)
        await run_in_threadpool(fd.reset, ident)
        return RedirectResponse("/home", status_code=303)

    @app.get("/home", include_in_schema=False)
    def home(request: Request):
        f = Facility(fd, need(request))
        cases = f.cases()
        morning = [c for c in cases if c["shift"]]
        return show(request, "fms_home.html", "home", f, rows=f.occupancy(), month=f.month(month_key(f.today)), past=f.past_due(),
                    waiting=len(f.waiting(cases)), handed=sum(1 for c in cases if c["handed"]), disputes=len(f.disputes()),
                    morning=len(SHIFT), closed=sum(1 for c in morning if not c["handed"]))

    @app.get("/today", include_in_schema=False)
    def today(request: Request):
        f = Facility(fd, need(request))
        cases = f.cases()
        return show(request, "fms_today.html", "today", f, cases=cases, waiting=f.waiting(cases), disputes=f.disputes(), decisions=f.decisions())

    @app.get("/messages", include_in_schema=False)
    def messages(request: Request, case: int | None = None, unread: str | None = None):
        f = Facility(fd, need(request))
        cases = f.cases()
        waiting = f.waiting(cases)
        opened = next((t for t in waiting if t["shift"] == unread), None)
        picked = None if opened else next((c for c in cases if c["n"] == case), cases[0] if cases else None)
        if picked is None and opened is None and waiting:
            opened = waiting[0]          # nothing read yet: open the first message, not an empty pane
        return show(request, "fms_messages.html", "messages", f, cases=cases, picked=picked, opened=opened, waiting=waiting, part="list")

    @app.get("/messages/try", include_in_schema=False)
    def try_message(request: Request):
        f = Facility(fd, need(request))
        return show(request, "fms_messages.html", "messages", f, tenants=fd.tenants(), part="try")

    @app.get("/messages/tests", include_in_schema=False)
    def tests(request: Request):
        f = Facility(fd, need(request))
        score = json.loads(SCORE.read_text())
        return show(request, "fms_messages.html", "messages", f, score=score, ran=date.fromisoformat(score["ran_at"]), part="tests")

    @app.get("/units", include_in_schema=False)
    def units(request: Request):
        f = Facility(fd, need(request))
        return show(request, "fms_units.html", "units", f, rows=f.units(), occupancy=f.occupancy())

    @app.get("/tenants", include_in_schema=False)
    def tenants(request: Request, q: str = "", status: str = ""):
        f = Facility(fd, need(request))
        q = q.strip()[:60]
        digits = re.sub(r"\D", "", q)
        everyone = sorted(f.people.values(), key=lambda p: p["name"])
        kinds = {"renting": lambda p: p["active"], "past": lambda p: p["past"] > 0, "dispute": lambda p: p["dispute"],
                 "credit": lambda p: p["credit"] > 0, "out": lambda p: not p["active"]}
        counts = {k: sum(1 for p in everyone if test(p)) for k, test in kinds.items()}
        people = [p for p in everyone if kinds[status](p)] if status in kinds else everyone
        if q:
            people = [p for p in people if q.lower() in p["name"].lower() or q.upper() in p["units"] or (len(digits) >= 3 and digits in re.sub(r"\D", "", p["phone"]))]
        return show(request, "fms_tenants.html", "tenants", f, people=people, q=q, status=status if status in kinds else "", counts=counts,
                    total=len(everyone))

    @app.get("/tenants/{key}", include_in_schema=False)
    def tenant(request: Request, key: str):
        f = Facility(fd, need(request), everything=True)
        p = f.by_slug.get(key)
        if p is None:
            raise HTTPException(404, "no such tenant")
        return show(request, "fms_tenant.html", "tenants", f, p=p, rentals=f.rentals(p["id"]), ledger=f.ledger(p["id"]),
                    cases=[c for c in f.cases() if c["who"] and c["who"]["id"] == p["id"]],
                    disputes=[d for d in f.disputes() if d["who"]["id"] == p["id"]])

    @app.get("/billing", include_in_schema=False)
    def billing(request: Request, month: str = ""):
        f = Facility(fd, need(request))
        months = f.months()
        key = month if month in dict(months) else month_key(f.today)
        return show(request, "fms_billing.html", "billing", f, months=months, month=f.month(key), past=f.past_due())

    @app.get("/rules", include_in_schema=False)
    def rules(request: Request, stale: int = 0):
        f = Facility(fd, need(request))
        return show(request, "fms_rules.html", "rules", f, stale=bool(stale), **f.rules())

    # writes ------------------------------------------------------------------------------------------------------------
    @app.post("/api/fms/messages")
    def run_message(body: CaseIn, request: Request, ident: str = Depends(need)):
        """A tenant's message through the agent on this session's copy: this morning's messages, or one typed in Try a tenant message."""
        same_site(request)
        if not live:
            return fd.run(ident, body, live)

        def budget_left() -> float:
            with session_factory() as s:
                return settings.daily_budget_usd - spend.spent_today(s)
        return fd.run(ident, body, live, settings.visitor_cases_per_day, budget_left)

    @app.post("/api/fms/rules")
    def propose_rule(body: RuleIn, request: Request, ident: str = Depends(need)):
        """An owner sentence read into a proposed rule on this session's copy: what it says, what changes, last year replayed."""
        same_site(request)

        def budget_left() -> float:
            with session_factory() as s:
                return settings.daily_budget_usd - spend.spent_today(s)
        pid, p, effect = fd.rule_proposal(ident, body.sentence, live, budget_left if live else None)
        f = Facility(fd, ident)
        names = {t: f.people[t]["name"] for t in f.people}
        changed = lambda rows: [{"who": names.get(r["tenant"], ""), "due": day(date.fromisoformat(r["due"])), "asked": day(date.fromisoformat(r["asked"])),
                                 "before": r["old"], "after": r["new"]} for r in rows[:8]]
        return {"id": pid, "outcome": p.outcome, "kind": p.kind, "words": p.words, "reason": p.reason, "not_expressed": p.not_expressed,
                "quotes": [q["words"] for q in p.quotes], "cents": round(p.call.cents, 4) if p.call else 0,
                "replay": {"fees": effect["fees"], "before": effect["old"], "after": effect["new"], "now_allowed": len(effect["to_allowed"]),
                           "now_refused": len(effect["to_refused"]), "allowed": changed(effect["to_allowed"]), "refused": changed(effect["to_refused"])}
                if effect else None}

    @app.post("/rules/approve", include_in_schema=False)
    async def approve_rule(request: Request):
        """The visitor approves the proposal they were shown, as the owner, on this session's copy only."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        try:
            await run_in_threadpool(fd.approve_rule, ident, data.get("proposal", "")[:20])
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return RedirectResponse("/rules?stale=1#h-write", status_code=303)
        return RedirectResponse("/rules#in-force", status_code=303)

    @app.post("/disputes/decide", include_in_schema=False)
    async def decide(request: Request):
        """A staff decision on an open dispute, through the gate, on this session's copy. One decision per dispute and kind: a
        second press gets the first decision back. The facts are checked again inside the write."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, dispute, reason = data.get("action", ""), data.get("dispute", ""), " ".join(data.get("reason", "").split())
        if action not in STAFF_ACTIONS:
            raise HTTPException(422, "That is not a decision staff can make here.")
        if not 3 <= len(reason) <= 300:
            raise HTTPException(422, "Write what you checked, in a few words.")

        def act() -> None:
            with fd.lock:
                store = fd.ready()
                stream = fd.stream(ident)
                d = store.books(stream).disputes.get(dispute)
                if d is None:
                    raise HTTPException(404, "There is no such dispute on this copy.")
                execute(store, stream, Command(f"staff:{dispute}:{action}", action, d.tenant, STAFF, d.charge, reason=reason), fd.clock())
        await run_in_threadpool(act)
        return RedirectResponse("/today#decided", status_code=303)
