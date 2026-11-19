"""The agentic FMS: Northstar Storage's own books (core/) behind a sign-in, the work Tilden (the agent inside it) did on them, and
what waits for a person. The Overview at / is the only public page. Every FMS page and every write needs a signed-in session, and each session
works on its own copy of the books (its own stream), so one visitor's changes never reach the next. The demo name and password
are printed on the sign-in page: the sign-in is real (a signed session cookie, refused when missing or forged), the person is not."""
from __future__ import annotations

import hmac
import json
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from core import delinquency as col
from core import renting
from core.case import OFF
from core.books import ADMIN_FEE_CENTS, LATE_FEE_CENTS, RENT, REVIEW_DAYS, at_local, lines, local, month_key
from core.billing import Refused
from core.evidence import KIND_WORDS, clock, day, money, month_words
from core.gate import ACTIONS, RENT_SCOPE, STAFF_ACTIONS, STAFF_RULE, Command, execute
from core.rules import staff_words, waiver_words
from core.seed import story_month

from . import spend
from .web import AGENT
from .front_desk import ACTION_WORDS, CLAIM_WORDS, SHIFT, CaseIn, FrontDesk, RuleIn, account_words
from .guard import LIFETIME, SLOW_DOWN, UNSET, Unreachable
from .wording import plain

DEMO_NAME, DEMO_PASSWORD = "dana.reyes", "northstar"
SESSION = "staddle_session"
STAFF = "staff:office_manager"
# the menu in the groups an FMS is organised by; a module joins its group when it is built, never as a greyed-out item
MENU = (("Run the facility", (("home", "Home", "house"), ("today", "Today", "list-checks"), ("messages", "Messages", "message-square"))),
        ("Facility and tenants", (("units", "Units", "warehouse"), ("tenants", "Tenants", "users"))),
        ("Money", (("billing", "Billing and payments", "credit-card"),)),
        ("Setup", (("rules", "Rules", "scale"),)))
# the only addresses that answer without a session; tests/test_fms.py walks every route against this list. The card processor's
# webhook is the one write among them: the processor calls it, no session exists, its signature is the check, and without a signing
# secret it answers 503 (desk/stripe_hook.py)
PUBLIC = {"/", "/film", "/sign-in", "/sign-out", "/static", "/health/live", "/health/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
          "/webhooks/stripe"}
SCORE = Path(__file__).parent / "static" / "first_shift.json"
STAFF_WORDS = {"remove_disputed_fee": "Removed the fee", "keep_under_review": "Kept under review"}
# renting's staff commands as the Today list words them; an attempt the gate refused reads "Asked to ..."
RENT_WORDS = {"price_unit": ("Priced a unit", "Asked to price a unit"), "block_unit": ("Blocked a unit", "Asked to block a unit"),
              "unblock_unit": ("Unblocked a unit", "Asked to unblock a unit"), "reserve": ("Held a unit", "Asked to hold a unit"),
              "cancel_reservation": ("Cancelled a reservation", "Asked to cancel a reservation"),
              "start_checkout": ("Started a checkout", "Asked to start a checkout"),
              "request_transfer": ("Asked for a transfer", "Asked for a transfer"), "approve_transfer": ("Approved a transfer", "Asked to approve a transfer"),
              "complete_transfer": ("Completed a transfer", "Asked to complete a transfer"),
              "request_move_out": ("Recorded a move-out", "Asked to record a move-out"), "review_move_out": ("Reviewed a move-out", "Asked to review a move-out"),
              "final_bill": ("Made the final bill", "Asked to make the final bill"), "offer_unit": ("Offered a unit", "Asked to offer a unit"),
              "decline_offer": ("Declined an offer", "Asked to decline an offer"), "retry_access": ("Retried a gate code", "Asked to retry a gate code"),
              # commands that are not staff's to give still leave a refused record when staff try them
              "join_waitlist": ("Joined a waitlist", "Asked to join a waitlist"), "accept_offer": ("Accepted an offer", "Asked to accept an offer"),
              "accept_agreement": ("Accepted an agreement", "Asked to accept an agreement"),
              "confirm_payment": ("Recorded a payment", "Asked to record a payment"),
              "payment_failed": ("Recorded a failed payment", "Asked to record a failed payment"),
              "activate": ("Started a rental", "Asked to start a rental"),
              "confirm_refund": ("Confirmed a refund", "Asked to confirm a refund")}
MAY_DO = {"explain_charge": "Explain a charge from the records", "reverse_duplicate": "Reverse the second copy of a charge posted twice",
          "correct_fee_posted_in_error": "Remove a late fee posted in error", "open_dispute": "Record a dispute for the office manager",
          "waive_fee": "Waive a late fee under the owner's rule", "hand_to_staff": "Hand a message to staff, with the reason"}
STAFF_DO = {"remove_disputed_fee": "Remove a fee the tenant never signed for", "keep_under_review": "Keep a dispute under review until its date"}
BY = {"billing": "Billing", "operator": AGENT, "fixed_workflow": "Fixed workflow"}
JOB_WORDS = {"understand": "Reading", "operate": "Choosing", "plan reply": "Reading the reply about paying in parts"}
WRONG = "That name and password do not match the demo login shown on this page."
# collections, as an FMS shows it: each state in words with its chip colour
PLAN_WORDS = {"proposed": ("Terms worked out, not yet offered", "plain"), "offered": ("Offered, waiting for the tenant", "plain"),
              "accepted": ("Accepted, waiting for the first payment", "wait"), "active": ("In good standing", "done"),
              "breached": ("Breached, waiting for staff", "act"), "review": ("With staff for a decision", "wait"),
              "completed": ("Completed", "plain"), "cancelled": ("Ended", "plain")}
PART_WORDS = {"scheduled": ("Scheduled", "plain"), "due": ("Due", "wait"), "paid": ("Paid", "done"), "partly paid": ("Partly paid", "wait"),
              "missed": ("Missed", "act"), "uncertain": ("Waiting for the card processor", "wait")}
STEP_WORDS = {"clear": ("Clear", "plain"), "eligible": ("Due", "act"), "blocked": ("Held", "wait"), "waiting for staff": ("Waiting for staff", "act"),
              "recorded": ("Recorded", "plain")}
TASK_WORDS = {"plan breached": "Payment plan breached", "plan needs staff": "Payment plan needs a decision",
              "returned payment needs staff": "Returned payment not applied", "payment uncertain": "Payment waiting for the card processor",
              "call back": "Promise to pay missed: call back", "exemption ended": "Exemption ended"}
SCOPE_WORDS = {"overlock": "overlock", "lien": "lien eligibility"}
STEP_FORMS = ("overlock", "lift_overlock", "review_plan", "grant_exemption", "revoke_exemption", "take_payment")
UNIT_FORMS = ("price_unit", "reserve", "block_unit", "unblock_unit")       # what staff do to a unit on the Units page
PAYMENT_WORDS = {"open": ("Waiting for the card processor", "wait"), "pending": ("Waiting for the card processor", "wait"), "succeeded": ("Paid", "done"),
                 "failed": ("Declined", "act"), "returned": ("Returned by the bank", "act"), "closed": ("Not taken", "plain")}
# a rental's payment and its gate code on the tenant's page: (words, chip tone)
RENTAL_PAY = {"paid": ("Paid", "done"), "waiting": ("Waiting for the card processor", "wait"), "returned": ("Returned by the bank", "act"),
              "declined": ("Card declined", "act"), "unpaid": ("Not paid yet", "wait")}
GATE_WORDS = {"verified": ("Code set and read back", "done"), "failed": ("Access pending: the code could not be read back", "act"),
              "requested": ("Access pending", "wait"), "issuing": ("Access pending", "wait"), "issued_unverified": ("Access pending", "wait"),
              "revocation_pending": ("Old code being revoked", "wait"), "revocation_failed": ("Old code still standing after the revoke", "act"),
              "revoked": ("Code revoked", "plain")}
# the owner's settings as a form: field, label, unit; dollars are typed as whole dollars and kept in cents
POLICY_FORM = (("min_tenure_days", "Rented at least", "days"), ("max_debt_cents", "Balance at most", "dollars"),
               ("longest_days", "Longest plan", "days"), ("most_instalments", "Payments after the first", "payments"),
               ("min_upfront_cents", "Smallest first payment", "dollars"), ("min_instalment_cents", "Smallest later payment", "dollars"),
               ("grace_days", "Grace after each due date", "days"))
SCHEDULE_FORM = (("overlock_days", "Overlock", "days"), ("lien_days", "Lien eligibility", "days"))


def upfirst(text: str) -> str:
    return text[:1].upper() + text[1:]


def bounded(data: dict, fields, bounds: dict) -> tuple[dict | None, str]:
    """A settings form read field by field into whole numbers inside the fixed limits; dollars become cents. Returns (values, "")
    or (None, why not, in the form's own words)."""
    out = {}
    for key, label, unit in fields:
        lo, hi = bounds[key]
        if unit == "dollars":
            lo, hi = -(-lo // 100), hi // 100
        raw = (data.get(key) or "").strip()
        if not raw.isdecimal() or not lo <= int(raw) <= hi:
            return None, f"{label} must be a whole number from {lo:,} to {hi:,} {unit}"
        out[key] = int(raw) * (100 if unit == "dollars" else 1)
    return out, ""


def dollars(text: str) -> int | None:
    """An amount typed in dollars and cents, to whole cents without floats; None when it is not one."""
    m = re.fullmatch(r"\$?(\d{1,7})(?:\.(\d{1,2}))?", text.strip().replace(",", ""))
    return int(m[1]) * 100 + int((m[2] or "0").ljust(2, "0")) if m else None


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
        self.fd, self.today, self.now, self.ident = fd, fd.day, fd.clock(), ident
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
                if p.get("plan_read") and c["kind"] == "other":       # the plan reading says it better
                    continue
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
            if any(o.get("by") in ("owner rule", "staff rule") for o in outcomes):      # a rule sent it: say which, not the records
                why = p["handed"]
            out.append({"n": e.id, "who": who, "text": p["text"], "handed": p["handed"], "why": why or p["handed"], "reply": p["reply"], "claims": claims, "plan_read": p.get("plan_read"),
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

    def rent_sentence(self, p: dict) -> str:
        """What a staff renting step did, or was refused, from what the gate recorded: a unit is named only when the step is about one."""
        action, args, who = p["action"], p.get("args") or {}, self.people.get(p.get("tenant") or "")
        unit, asked = (p["about"] if p["about"] in self.b.units else None), not p["allowed"]
        if action == "price_unit" and unit and isinstance(args.get("rent_cents"), int):
            return f"{'Asked to set' if asked else 'Set'} the street rate of unit {unit} to {money(args['rent_cents'])}"
        text = RENT_WORDS[action][1 if asked else 0]
        if unit:
            text = text.replace("a unit", f"unit {unit}")
        if action == "block_unit" and args.get("reason"):
            text += f": {args['reason']}"
        if who:
            text += f" for {who['name']}"
        if action == "reserve" and args.get("until"):
            try:
                text += f" until {day(datetime.fromisoformat(args['until']))}"
            except ValueError:
                pass
        return plain(text)

    def decisions(self) -> list[dict]:
        out = []
        for e in self.own:
            if e.kind == "gate.decided" and e.source.startswith("staff:"):
                p = e.payload
                if p["action"] in RENT_SCOPE:       # a renting step: no fee, no note, and often no tenant (pricing a unit); what was done is said from what was recorded
                    out.append({"who": self.people.get(p.get("tenant") or ""), "words": RENT_WORDS[p["action"]][0 if p["allowed"] else 1], "allowed": p["allowed"],
                                "removed": False, "check": p["check"], "reason": None, "fee": "", "renting": True, "text": self.rent_sentence(p)})
                    continue
                ch = self.b.charges.get(p.get("charge") or "")
                out.append({"who": self.people.get(p.get("tenant") or ""), "words": STAFF_WORDS.get(p["action"], p["action"]), "allowed": p["allowed"],
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
                rows.append({**p, "oldest": day(oldest), "days": (self.today - oldest).days, **self.standing(p["id"])})
        return sorted(rows, key=lambda r: (-r["days"], -r["past"]))

    # renting ----------------------------------------------------------------------------------------------------------------
    @cached_property
    def rent(self) -> renting.Rent:
        """This copy's renting state as of this page load; read only (rent.units.get, never rent.unit, which would add a record)."""
        return self.fd.rent_view(self.ident)

    def pay_state(self, r) -> str:
        """Where a rental's payment stands: paid, waiting for the card processor, returned by the bank (every payment it had came back),
        declined (the last attempt), or not paid yet."""
        rent = self.rent
        if rent.paid(r):
            return "paid"
        if rent.uncertain(r):
            return "waiting"
        if r.payments:
            return "returned"
        return "declined" if r.pay_ops and "declined" in (rent.ops[r.pay_ops[-1]].result or {}) else "unpaid"

    def move_in(self, r) -> date | None:
        """The day a paid and signed rental that has not started is waiting for, when that day has not come; else None."""
        start = date.fromisoformat(r.quote["start"])
        return start if r.state in renting.WAITING and r.accepted and self.pay_state(r) == "paid" and start > local(self.now).date() else None

    def stage(self, r) -> str:
        """Where a rental that has not started stands, in one sentence: its payment and its agreement."""
        pay = self.pay_state(r)
        if self.move_in(r):
            return "Waiting for the move-in day"
        if pay == "paid" and r.accepted:
            return "Paid and accepted, but the rental has not started"
        words = {"paid": "paid", "waiting": "payment waiting for the card processor", "returned": "payment returned by the bank",
                 "declined": "card declined", "unpaid": "not paid yet"}[pay]
        return upfirst(f"{words}, agreement {'accepted' if r.accepted else 'not accepted'}")

    def held(self, unit: str) -> tuple[str, dict | None]:
        """What holds a unit now, in a sentence, and the person it is held for (None for a block or when nothing holds it)."""
        rent = self.rent
        h = rent.holder(unit, self.now)
        if h is None:
            return "", None
        kind, rid = h
        if kind == "block":
            return f"Blocked: {rent.blocks[rid].reason}", None
        if kind == "reservation":
            r = rent.reservations[rid]
            return f"Held for {self.people[r.tenant]['name']} until {clock(r.expires_at)}", self.people[r.tenant]
        if kind == "offer":
            e = rent.entries[rid]
            return f"Offered to {self.people[e.tenant]['name']} from the waitlist until {clock(e.expires_at)}", self.people[e.tenant]
        r = rent.rentals[rid]
        who = self.people[r.tenant]
        if r.state == "ended":
            return "Moved out; the old gate code is not revoked yet", who
        if r.state in renting.WAITING:
            stage = self.stage(r)
            return f"{who['name']} is renting it: {stage[:1].lower()}{stage[1:]}", who
        return "", who

    def renting_of(self, tenant: str) -> list[dict]:
        """A tenant's reservations and rentals, newest first, each step in words: where it stands, the payment, the agreement with its
        template version, the lease and the gate code, which counts as working only once it was read back. A rental that is over or whose
        hold ran out says what happened, not what is still to do."""
        rent, b, now, year, rows = self.rent, self.b, self.now, self.today.year, []
        rented = {r.reservation for r in rent.rentals.values() if r.reservation}
        for r in rent.reservations.values():
            if r.tenant == tenant and r.id not in rented:
                live = rent.holder(r.unit, now) == ("reservation", r.id)
                where = ((f"Reserved until {clock(r.expires_at)}", "wait") if live else ("Hold ended", "plain") if r.state in ("held", "expired") else ("Cancelled", "plain"))
                rows.append({"unit": r.unit, "where": (*where, ""), "pay": None, "agreement": None, "lease": None, "gate": None})
        for r in rent.rentals.values():
            if r.tenant != tenant:
                continue
            holds = r.state in renting.WAITING and rent.holder(r.unit, now) == ("rental", r.id)
            live = holds or r.state in ("active", "ending")
            m = rent.moveouts.get(r.moveout or "")
            if holds and self.move_in(r):
                where = ("Waiting for the move-in day", "wait", f"Starts {dated(self.move_in(r), year)}.")
            elif holds:
                where = ("Waiting to start", "wait", f"Hold ends {clock(r.expires_at)}." if now < r.expires_at else
                         f"The hold time ended {clock(r.expires_at)}; the unit stays held while a payment is waiting or paid for.")
            else:
                where = {"active": ("Active", "done", ""), "ended": ("Ended", "plain", ""), "cancelled": ("Cancelled", "plain", f"Cancelled because {r.cancelled_why}." if r.cancelled_why else ""),
                         "ending": ("Moving out" if m else "Moving to another unit", "wait", f"Last day {dated(m.on, year)}." if m else "")
                         }.get(r.state) or ("Hold ended", "plain", f"The hold ended {clock(r.expires_at)}.")
            note = [where[2]] if where[2] else []
            if r.kind == "transfer" and r.state in renting.WAITING:
                note.append("This is the new unit of a transfer.")
            # a refund keeps its own task (keyed), so one rental can owe several: the one set aside when it was cancelled, the credit left
            # by its final bill, and a payment that came late. Each reads waiting until the processor confirms it, then made.
            asked = [t for t in rent.tasks.values() if t.open and t.kind == "refund" and t.about == r.id]
            sums = [(r.refund["cents"], r.refund["state"], f"refund:{r.id}")] if r.refund else []
            if m and m.bill and m.bill.get("refund_cents"):
                sums.append((m.bill["refund_cents"], m.bill["refund"], f"refund:{m.id}"))
            for cents, state_of, key in sums:
                note.append(f"Refund of {money(cents)} {'was made' if state_of == 'paid' else 'waiting for staff'}.")
            if any(t.key not in {k for *_, k in sums} for t in asked):
                note.append("A refund is waiting for staff.")
            state = self.pay_state(r)
            refund = r.state == "cancelled" and ((r.refund or {}).get("state") == "pending" or asked)
            made = r.state == "cancelled" and (r.refund or {}).get("state") == "paid" and not asked
            pay = (("Refund waiting", "act") if refund else ("Refunded", "plain") if made else RENTAL_PAY[state] if live or state not in ("unpaid", "declined") else
                   ("Not paid" if state == "unpaid" else "Card declined", "plain"))
            ag = rent.agreements.get(r.agreement or "")
            version = f"Agreement version {ag.version.rsplit('-', 1)[-1]}" if ag else ""
            agreement = ((f"Accepted by {self.people[r.tenant]['name']}, {clock(ag.at)}", "done", version) if r.accepted and ag and ag.at else
                         ("Not accepted yet", "wait", version) if live else ("Not accepted", "plain", version))
            lease = b.leases.get(r.lease or "")
            a = rent.access.get(r.access or "")
            gate = GATE_WORDS[a.state] if a else ("Access pending", "wait") if r.state in ("active", "ending") else None
            rows.append({"id": r.id, "unit": r.unit, "where": (where[0], where[1], " ".join(note)), "pay": (*pay, f"Move-in total {money(r.quote['total_cents'])}"),
                         "agreement": agreement, "gate": (*gate, "") if gate else None,
                         "lease": (f"Ended {dated(lease.ended, year)}" if lease.ended else f"Started {dated(lease.start, year)}") if lease else None})
        return rows[::-1]

    def unfinished(self) -> list[dict]:
        """Rentals not finished yet, a declined or stuck one first: a reservation still holding its unit, a rental waiting on its
        payment or its agreement while it still holds the unit (a hold that ran out is not listed, as Units shows that unit vacant),
        and a rental paid for but cancelled with its refund still to make. An open task of the rental says why it is stuck."""
        rent, now, rows = self.rent, self.now, []
        today = local(now).date()
        why: dict[str, list[str]] = {}
        for t in rent.tasks.values():
            if t.open and t.kind in ("payment", "hold", "activation", "refund"):
                why.setdefault(t.about, []).append(plain(t.reason))
        for r in rent.reservations.values():
            if rent.holder(r.unit, now) == ("reservation", r.id):
                rows.append({"who": self.people[r.tenant], "unit": r.unit, "what": "Reservation held", "tone": "plain", "by": r.expires_at,
                             "needs": local(r.expires_at).date() <= today,
                             "why": f"Held until {clock(r.expires_at)}; the unit is released if no rental starts by then."})
        for r in rent.rentals.values():
            if r.state in renting.WAITING and rent.holder(r.unit, now) == ("rental", r.id):
                stage = self.stage(r)
                ends = (f"The hold on the unit ends {clock(r.expires_at)}." if now < r.expires_at else
                        f"The hold time ended {clock(r.expires_at)}; the unit stays held while a payment is waiting or paid for.")
                day_in = self.move_in(r)
                rows.append({"who": self.people[r.tenant], "unit": r.unit, "what": stage, "by": r.expires_at,
                             "why": f"Paid and signed; the rental starts on its move-in day, {dated(day_in, self.today.year)}." if day_in else " ".join(why.get(r.id, [])) or ends,
                             "tone": "act" if stage.startswith(("Card declined", "Payment returned", "Paid and accepted")) else "wait",
                             # a person is needed when money is stuck (paid, returned or declined) or the hold ends today, but not while a checkout is
                             # running (its hold has at most the checkout time left): that settles by itself. A hold ending on a later day waits.
                             "needs": not day_in and not (now < r.expires_at <= now + renting.CHECKOUT) and (
                                 self.pay_state(r) in ("paid", "returned", "declined") or local(r.expires_at).date() <= today)})
        for t in rent.tasks.values():
            r = rent.rentals.get(t.about)
            if t.open and t.kind == "refund" and r is not None:
                rows.append({"who": self.people[r.tenant], "unit": r.unit, "tone": "act", "by": now, "needs": True, "why": plain(t.reason),
                             "what": "Cancelled after payment, refund to make" if r.state == "cancelled" else "Moved out, refund to make"})
        return sorted(rows, key=lambda x: (x["tone"] != "act", x["by"]))

    def codes(self) -> list[dict]:
        """Gate codes not confirmed working on a rental that is still going: each open access task (a failed read-back, or the daily
        check finding a code never read back). Once the rental ends, the unit is listed under turnover instead, never twice."""
        rent, rows = self.rent, []
        for t in rent.tasks.values():
            a = rent.access.get(t.about)
            r = rent.rentals.get(a.rental if a else t.about)
            if t.open and t.kind == "access" and r is not None and r.state in ("active", "ending"):
                rows.append({"who": self.people[r.tenant], "unit": r.unit, "what": "Gate code not confirmed", "tone": "act", "why": plain(t.reason),
                             "since": dated(t.due, self.today.year)})
        return sorted(rows, key=lambda x: x["unit"])

    def turnover(self) -> list[dict]:
        """Units whose rental ended while the old gate code still stands: they cannot be rented again until it is revoked. An open
        revoke task only supplies the reason."""
        rent, rows = self.rent, []
        for r in rent.rentals.values():
            if r.state == "ended" and rent.holder(r.unit, self.now) == ("rental", r.id):
                a = rent.access[r.access]
                task = next((t for t in rent.tasks.values() if t.open and t.kind == "revoke" and t.about == a.id), None)
                m = rent.moveouts.get(r.moveout or "")
                rows.append({"who": self.people[r.tenant], "unit": r.unit, "tone": "act" if task or a.state == "revocation_failed" else "wait",
                             "what": f"Moved out {dated(m.on, self.today.year)}" if m else "Moved to another unit",
                             "why": plain(task.reason) if task else "The old code is not revoked on the gate system yet, so the unit cannot be rented again."})
        return sorted(rows, key=lambda x: x["unit"])

    def units(self) -> list[tuple[str, list[dict]]]:
        b, rent = self.b, self.rent
        active = {x.unit: x for x in b.leases.values() if x.ended is None}
        disputed = {b.charges[d.charge].lease for d in b.disputes.values() if d.outcome is None}
        rows: dict[str, list[dict]] = {}
        for u in b.units:
            lease, state = active.get(u), rent.status(u, self.now)
            note, held_for = self.held(u)
            priced = rent.units.get(u)
            cents = priced.rent_cents if priced else None
            item = {"unit": u, "state": state, "chip": None, "who": held_for, "rent": "", "since": "", "note": plain(note),
                    "size": (priced.size if priced else None) or "", "price": money(cents) if cents else "Not priced",
                    "price_value": f"{cents / 100:.2f}" if cents else "",
                    "block": (rent.holder(u, self.now) or ("", ""))[0] == "block"}
            if lease is None:
                last = max((x.ended for x in b.leases.values() if x.unit == u and x.ended), default=None)
                item["since"] = f"Empty since {day(last)}" if last else "Not rented this year"
            else:
                p = self.people[lease.tenant]
                chip = ("Fee in dispute", "wait") if lease.id in disputed else (("Past due", "act") if p["past"] else None)
                item.update(chip=chip, who=p, rent=money(lease.rent_cents), since=f"{day(lease.start)}, {lease.start.year}")
            rows.setdefault(u[0], []).append(item)
        return sorted(rows.items())

    @cached_property
    def tenant_labels(self) -> dict[str, str]:
        """Every tenant as staff type the name, mapped to their record: the name, with the phone added where two tenants share one."""
        names = [p["name"] for p in self.people.values()]
        return {(p["name"] if names.count(p["name"]) == 1 else f"{p['name']}, {p['phone']}"): p["slug"]
                for p in sorted(self.people.values(), key=lambda p: (p["name"], p["phone"]))}

    def tenant_typed(self, text: str) -> dict | None:
        """The tenant staff typed, by the name the list shows (case and spacing do not matter); None when it names no one or two."""
        want = " ".join(text.split()).casefold()
        return next((self.by_slug[slug] for label, slug in self.tenant_labels.items() if label.casefold() == want), None)

    @staticmethod
    def unit_counts(rows) -> dict:
        """The legend's figures: every unit counted once, an occupied one by its worst chip."""
        counts = dict.fromkeys(("occupied", "past", "dispute", "reserved", "blocked", "vacant"), 0)
        for _, items in rows:
            for u in items:
                tone = u["chip"][1] if u["chip"] else None
                counts[{"act": "past", "wait": "dispute"}.get(tone) if u["state"] == "occupied" and tone else u["state"]] += 1
        return counts

    # collections ----------------------------------------------------------------------------------------------------------
    def collection_tasks(self) -> list[dict]:
        """What waits for staff in collections, oldest first, until someone acknowledges it."""
        return [{"id": k.id, "who": self.people[k.tenant], "what": TASK_WORDS.get(k.kind, upfirst(k.kind)), "why": plain(upfirst(k.why)),
                 "since": dated(k.due, self.today.year)} for k in col.open_tasks(self.b)]

    def overlocks_due(self) -> list[dict]:
        """Every rented unit whose overlock is due on the owner's schedule with nothing holding it, longest past due first, so a due
        step reaches staff on Today instead of waiting unseen on the tenant's page."""
        rows = []
        for x in self.b.leases.values():
            if x.ended is None:
                o = self.steps(x)["overlock"]
                if o["state"] == "eligible":
                    rows.append({"who": self.people[x.tenant], "unit": x.unit, "lease": x.id, "days": col.days_past_due(self.b, x.id, self.now),
                                 "why": o["why"]})
        return sorted(rows, key=lambda r: (-r["days"], r["unit"]))

    def steps(self, lease) -> dict:
        """The overlock and lien-eligibility steps on one unit: the state in words and why, from the same check the gate makes."""
        st, out = col.state(self.b), {}
        for step in col.STEPS:
            state, why = col.step_state(self.b, lease.id, step, self.now)
            if state == "recorded":
                why = f"Since {dated(local((st.overlocks if step == 'overlock' else st.liens)[lease.id]['at']).date(), self.today.year)}, by staff"
            elif state == "clear" and col.days_past_due(self.b, lease.id, self.now) is None:
                why = ""                    # nothing past due: nothing to explain
            text, tone = ("Moved out", "plain") if lease.ended else STEP_WORDS[state]
            out[step] = {"state": state, "words": text, "tone": tone, "why": plain(upfirst(why))}
        return out

    def plan_of(self, tenant: str) -> dict | None:
        """The tenant's latest accepted payment plan as an FMS shows it, or None."""
        plans = [p for p in col.state(self.b).plans.values() if p.tenant == tenant and p.terms]
        if not plans:
            return None
        p = max(plans, key=lambda x: x.history[0][1])
        agreed = next(at for s, at in p.history if s == "accepted")
        parts = []
        for x in col.instalments(self.b, p, self.now):
            text, tone = PART_WORDS[x["state"]]
            if p.state == "cancelled" and x["state"] in ("scheduled", "due", "partly paid"):
                text, tone = "", ""             # the plan ended before this part came due: it has no state
            parts.append({"label": "First payment" if x["part"] == 0 else f"Payment {x['part']}", "due": dated(date.fromisoformat(x["due"]), self.today.year),
                          "amount": money(x["cents"]), "words": text + (f", {money(x['covered'])} so far" if x["state"] == "partly paid" else ""), "tone": tone})
        text, tone = PLAN_WORDS[p.state]
        return {"id": p.id, "state": p.state, "words": text, "tone": tone, "total": money(p.terms["total"]), "consent": p.consent["quote"],
                "agreed": clock(agreed), "policy": p.policy_version, "grace": p.terms["grace_days"], "parts": parts,
                "review": p.state in col.WITH_STAFF}

    def collections(self, tenant: str) -> dict:
        """Everything collections holds for one tenant: the plan, each unit's steps, active exemptions, open promises."""
        st = col.state(self.b)
        units = [{"lease": x.id, "unit": x.unit, **self.steps(x)} for x in self.leases_of.get(tenant, []) if x.ended is None]
        exemptions = [{"id": e.id, "lease": e.lease, "unit": self.b.leases[e.lease].unit, "scope": upfirst(" and ".join(SCOPE_WORDS[s] for s in e.scope)),
                       "reason": e.reason, "until": dated(e.until, self.today.year), "owner": e.owner.replace("_", " ")}
                      for e in st.exemptions.values() if e.tenant == tenant and col.exemption_state(e, self.today) == "active"]
        promises = [{"amount": money(p.cents), "by": dated(p.by, self.today.year), "callback": dated(p.callback, self.today.year), "quote": p.quote}
                    for p in st.promises.values() if p.tenant == tenant and p.state == "open"]
        plan = self.plan_of(tenant)
        cards = [{"id": c["card"], "label": f"{c['brand']} ending {c['last4']}" + (", a test card that declines" if c["token"] == "card_declined" else "")}
                 for c in col.cards_of(self.b, tenant)]
        payments = [{"when": clock(i.created), "amount": money(i.cents), "words": PAYMENT_WORDS[i.state][0], "tone": PAYMENT_WORDS[i.state][1]}
                    for i in sorted((i for i in st.intents.values() if i.tenant == tenant), key=lambda i: i.created, reverse=True)[:6]]
        show = bool(plan or exemptions or promises or self.people[tenant]["past"] or any(u["overlock"]["state"] != "clear" for u in units))
        return {"plan": plan, "units": units, "exemptions": exemptions, "promises": promises, "show": show and bool(units),
                "cards": cards, "card_payments": payments, "owing": col.owing(self.b, tenant),
                "until_max": (self.today + timedelta(days=col.EXEMPTION_DAYS)).isoformat(), "until_min": self.today.isoformat()}

    def standing(self, tenant: str) -> dict:
        """For the past-due list: the plan's state and the first unit whose overlock is not clear, with why."""
        plans = [p for p in col.state(self.b).plans.values() if p.tenant == tenant and p.terms]
        plan = PLAN_WORDS[max(plans, key=lambda x: x.history[0][1]).state] if plans else None
        leases = self.leases_of.get(tenant, [])
        # the units still rented; a tenant who moved out with money owed is shown on the unit they left, with why no step applies
        units = [(x.unit, self.steps(x)["overlock"]) for x in ([x for x in leases if x.ended is None] or leases[-1:])]
        unit, step = next(((u, s) for u, s in units if s["state"] != "clear"), units[0] if units else (None, None))
        return {"plan": plan, "overlock": step, "overlock_unit": unit if len(units) > 1 else None}

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

    def versions(self, name: str, say) -> list[dict]:
        """Every version of a rule on this copy, newest first, in the words code writes from each typed form. Nothing to list
        until a second version exists: the rule in force is already shown by its own panel."""
        held = self.b.rules[name]
        rows = [{"rule": name, "version": h["version"], "words": say(h["typed"]), "when": dated(local(h["at"]).date(), self.today.year),
                 "by": h["by"], "restores": h.get("restores"), "now": h["version"] == held["version"], "same": h["typed"] == held["typed"]}
                for h in reversed(self.b.rule_history[name])]
        return rows if len(rows) > 1 else []

    def rules(self) -> dict:
        rule = self.b.rules.get("late_fee_waiver")
        staff_rule = self.b.rules.get(STAFF_RULE)
        return {"waiver": rule, "waiver_versions": self.versions("late_fee_waiver", waiver_words) if rule else [],
                "staff_versions": self.versions(STAFF_RULE, staff_words) if staff_rule else [], "typed": [words(k, v) for k, v in (rule or {}).get("typed", {}).items() if not isinstance(v, dict)],
                "waiver_words": waiver_words(rule["typed"]) if rule else [], "staff_rule": staff_rule,
                "staff_rule_words": staff_words(staff_rule["typed"]) if staff_rule else [],
                "late_fee": money(LATE_FEE_CENTS), "admin_fee": money(ADMIN_FEE_CENTS), "review_days": REVIEW_DAYS,
                "actions": [(MAY_DO[a], a in ("reverse_duplicate", "correct_fee_posted_in_error", "waive_fee")) for a in ACTIONS],
                "staff": [STAFF_DO[a] for a in STAFF_ACTIONS], **self.settings()}

    def settings(self) -> dict:
        """The owner's plan policy and schedule, each as its form fields with the value in force and the fixed limits as bounds."""
        st = col.state(self.b)

        def fields(spec, held, bounds):
            out = []
            for key, label, unit in spec:
                lo, hi = bounds[key]
                value = (held or {}).get(key)
                if unit == "dollars":
                    lo, hi, value = -(-lo // 100), hi // 100, None if value is None else value // 100
                out.append({"key": key, "label": label, "unit": unit, "min": lo, "max": hi, "value": value})
            return out
        return {"policy": st.policy, "policy_fields": fields(POLICY_FORM, st.policy, col.POLICY_BOUNDS),
                "schedule": st.schedule, "schedule_fields": fields(SCHEDULE_FORM, st.schedule, col.SCHEDULE_BOUNDS)}


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
    def site_show(request, template, f, **ctx):
        def site_money(cents):
            return f"${cents // 100:,}" + (f".{cents % 100:02d}" if cents % 100 else "")

        return templates.TemplateResponse(request, template, {"f": f, "money": site_money, "plain": plain, **ctx})

    @app.get("/website", include_in_schema=False)
    def website(request: Request):
        f = Facility(fd, need(request))

        def area(size):
            parts = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*", size)
            return Decimal(parts[1]) * Decimal(parts[2]) if parts else Decimal("Infinity")

        units = []
        for unit, record in f.rent.units.items():
            if record.rent_cents and record.size and f.rent.status(unit, f.now) == "vacant":
                units.append(record)
        units.sort(key=lambda u: (area(u.size), u.rent_cents, u.id))
        sizes = sorted({u.size for u in units}, key=lambda size: (area(size), size))
        return site_show(request, "site_store.html", f, units=units, sizes=sizes)

    @app.get("/website/rent/{unit}", include_in_schema=False)
    def website_rent(request: Request, unit: str):
        f = Facility(fd, need(request))
        record = f.rent.units.get(unit)
        if not record or not record.rent_cents or not record.size or f.rent.status(unit, f.now) != "vacant":
            return RedirectResponse("/website?unavailable=1", status_code=303)
        return site_show(request, "site_renter.html", f, unit=record, token=secrets.token_hex(6))

    @app.post("/website/rent/{unit}", include_in_schema=False)
    async def website_begin(request: Request, unit: str):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token, name, phone = data.get("token", ""), data.get("name", "").strip(), data.get("phone", "").strip()
        if not re.fullmatch(r"[0-9a-f]{12}", token) or not name or not phone or len(name) > 80 or len(phone) > 30:
            raise HTTPException(422, "Enter your name and phone, then try again.")

        def begin():
            with fd.lock:
                rent = fd.rent_view(ident)
                record = rent.units.get(unit)
                if not record or not record.rent_cents or not record.size:
                    return None
                tenant = fd.add_renter(ident, name, phone)
                held = fd.rent_step(ident, renting.Act(f"site:{token}:reserve", "reserve", "renter", unit, tenant))
                if not held.allowed:
                    return None
                checkout = fd.rent_step(ident, renting.Act(f"site:{token}:checkout", "start_checkout", "renter", held.made, tenant))
                return checkout.made if checkout.allowed else None

        rental = await run_in_threadpool(begin)
        return RedirectResponse(f"/website/checkout/{rental}" if rental else "/website?unavailable=1", status_code=303)

    @app.get("/website/checkout/{rental}", include_in_schema=False)
    def website_checkout(request: Request, rental: str):
        f = Facility(fd, need(request))
        r = f.rent.rentals.get(rental)
        if not r:
            raise HTTPException(404, "No such rental.")
        agreement = f.rent.agreements[r.agreement]
        return site_show(request, "site_checkout.html", f, rental=r, agreement=agreement, token=secrets.token_hex(6))

    @app.post("/website/checkout/{rental}", include_in_schema=False)
    async def website_pay(request: Request, rental: str):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token, card = data.get("token", ""), data.get("card", "")
        if not re.fullmatch(r"[0-9a-f]{12}", token) or card not in ("card_ok", "card_declined"):
            raise HTTPException(422, "Choose a card.")

        def pay():
            with fd.lock:
                r = fd.rent_view(ident).rentals.get(rental)
                if not r:
                    raise HTTPException(404, "No such rental.")
                if not r.accepted and data.get("accept") != "yes":
                    raise HTTPException(422, "Accept the agreement and choose a card.")
                if (ident, token) in fd.site_forms:     # a second press of the same page returns the first answer
                    return
                fd.site_forms.add((ident, token))
                if not r.accepted:      # once accepted, a declined card can be tried again without accepting twice
                    decision = fd.rent_step(ident, renting.Act(f"site:{token}:accept", "accept_agreement", "renter", rental, r.tenant,
                                                               {"sha256": data.get("sha256", "")}))
                    if not decision.allowed or decision.repeated:
                        return
                try:
                    fd.pay_rental(ident, rental, card)      # the core refuses a payment that is not due, so a second press pays nothing
                except Refused:
                    pass

        await run_in_threadpool(pay)
        return RedirectResponse(f"/website/result/{rental}", status_code=303)

    @app.get("/website/result/{rental}", include_in_schema=False)
    def website_result(request: Request, rental: str):
        f = Facility(fd, need(request))
        r = f.rent.rentals.get(rental)
        if not r:
            raise HTTPException(404, "No such rental.")
        row = next(row for row in f.renting_of(r.tenant) if row.get("id") == r.id)
        retry = r.state in renting.WAITING and f.rent.holder(r.unit, f.now) == ("rental", r.id) and f.pay_state(r) != "paid"   # a declined card, hold still on
        return site_show(request, "site_result.html", f, rental=r, row=row, person=f.people[r.tenant], retry=retry)

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
        ident = need(request)
        f = Facility(fd, ident)
        cases = f.cases()
        morning = [c for c in cases if c["shift"]]
        return show(request, "fms_home.html", "home", f, rows=f.occupancy(), month=f.month(month_key(f.today)), past=f.past_due(),
                    waiting=len(f.waiting(cases)), handed=sum(1 for c in cases if c["handed"]), disputes=len(f.disputes()), tasks=len(f.collection_tasks()),
                    overlocks=len(f.overlocks_due()), unfinished=sum(1 for x in f.unfinished() if x["needs"]), codes=len(f.codes()), turnover=len(f.turnover()),
                    morning=len(SHIFT), closed=sum(1 for c in morning if not c["handed"]),
                    note=plain(fd.take_reset_note(ident)))

    @app.get("/today", include_in_schema=False)
    def today(request: Request):
        ident = need(request)
        f = Facility(fd, ident)
        cases = f.cases()
        return show(request, "fms_today.html", "today", f, cases=cases, waiting=f.waiting(cases), disputes=f.disputes(), decisions=f.decisions(),
                    tasks=f.collection_tasks(), overlocks=f.overlocks_due(), overlock_days=(col.state(f.b).schedule or {}).get("overlock_days"),
                    unfinished=f.unfinished(), codes=f.codes(), turnover=f.turnover(),
                    token=secrets.token_hex(6), step_note=plain(fd.take_step_note(ident)))

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

    @app.get("/units", include_in_schema=False)
    def units(request: Request):
        ident = need(request)
        f = Facility(fd, ident)
        return show(request, "fms_units.html", "units", f, rows=f.units(), occupancy=f.occupancy(), token=secrets.token_hex(6),
                    until_min=f.today.isoformat(), until_max=(f.today + timedelta(days=366)).isoformat(), step_note=plain(fd.take_step_note(ident)))

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
        ident = need(request)
        f = Facility(fd, ident, everything=True)
        p = f.by_slug.get(key)
        if p is None:
            raise HTTPException(404, "no such tenant")
        return show(request, "fms_tenant.html", "tenants", f, p=p, rentals=f.rentals(p["id"]), ledger=f.ledger(p["id"]),
                    cases=[c for c in f.cases() if c["who"] and c["who"]["id"] == p["id"]],
                    disputes=[d for d in f.disputes() if d["who"]["id"] == p["id"]], col=f.collections(p["id"]),
                    renting=f.renting_of(p["id"]), token=secrets.token_hex(6), step_note=plain(fd.take_step_note(ident)))

    @app.get("/billing", include_in_schema=False)
    def billing(request: Request, month: str = ""):
        f = Facility(fd, need(request))
        months = f.months()
        key = month if month in dict(months) else month_key(f.today)
        return show(request, "fms_billing.html", "billing", f, months=months, month=f.month(key), past=f.past_due())

    @app.get("/rules", include_in_schema=False)
    def rules(request: Request, stale: int = 0):
        ident = need(request)
        f = Facility(fd, ident)
        return show(request, "fms_rules.html", "rules", f, stale=bool(stale), note=plain(fd.take_note(ident)), **f.rules())

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

    @app.post("/rules/restore", include_in_schema=False)
    async def restore_rule(request: Request):
        """The visitor, as the owner, goes back to an earlier version of a rule on this session's copy only."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        version = data.get("version", "")[:4]
        try:
            await run_in_threadpool(fd.restore_rule, ident, data.get("rule", "")[:40], int(version) if version.isdecimal() else 0)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return RedirectResponse("/rules#in-force", status_code=303)     # the page says why nothing changed
        return RedirectResponse("/rules#in-force", status_code=303)

    @app.post("/collections/acknowledge", include_in_schema=False)
    async def acknowledge(request: Request):
        """Staff saw a collections task; it leaves Today. A second press changes nothing."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        await run_in_threadpool(fd.acknowledge, ident, data.get("task", "")[:12], STAFF)
        return RedirectResponse("/today#collections", status_code=303)

    @app.post("/tenants/{key}/collections", include_in_schema=False)
    async def collections_step(request: Request, key: str):
        """A staff step on one tenant's collections, through the gate on this session's copy: record or lift an overlock, reinstate
        or end a breached plan, grant or revoke an exemption. The tenant comes from the address, never the form; the gate checks the
        unit, plan or exemption belongs to them. The page's token makes a second press return the first decision."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        if action not in STEP_FORMS:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        p = Facility(fd, ident).by_slug.get(key)
        if p is None:
            raise HTTPException(404, "no such tenant")
        reason = " ".join(data.get("reason", "").split())[:300]
        cents = dollars(data.get("amount", "")) if action == "take_payment" else None
        if action == "take_payment" and cents is None:
            raise HTTPException(422, "Type the amount in dollars and cents, for example 25.00.")
        typed = {"take_payment": {"cents": cents, "card": data.get("card", "")[:12]},
                 "review_plan": {"outcome": data.get("outcome", "")},
                 "grant_exemption": {"scope": [s for s in col.STEPS if data.get(f"scope_{s}")], "reason": reason, "until": data.get("until", "")[:10]},
                 "revoke_exemption": {"exemption": data.get("exemption", "")[:12]}}.get(action)
        if action == "review_plan" and len(reason) < 3:
            raise HTTPException(422, "Write what you agreed with the tenant, in a few words.")
        target = (data.get("lease") or data.get("plan") or data.get("exemption") or "")[:12]
        if action == "review_plan":
            target += ":" + typed["outcome"][:10]           # Reinstate and End on one page are two different decisions
        cmd = Command(f"form:{token}:{action}:{target}", action, p["id"], STAFF, lease=data.get("lease", "")[:12] or None,
                      plan=data.get("plan", "")[:12] or None, form=typed, reason=reason or None)
        await run_in_threadpool(fd.take_card_payment if action == "take_payment" else fd.staff_step, ident, cmd)
        if data.get("next") == "today" and action == "overlock":
            return RedirectResponse("/today#overlocks", status_code=303)       # pressed on Today's list: back to the list
        return RedirectResponse(f"/tenants/{key}#collections", status_code=303)

    @app.post("/units/{unit}/renting", include_in_schema=False)
    async def unit_step(request: Request, unit: str):
        """A staff renting step on one unit, through the gate on this session's copy: price it, hold it for a tenant, block it or unblock
        it. The unit comes from the address, never the form. The page's token makes a second press return the first decision, and a
        refusal is said once on Units."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        if action not in UNIT_FORMS:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        f = Facility(fd, ident)
        if unit not in f.b.units:
            raise HTTPException(404, "no such unit")
        tenant, args = None, {}
        if action == "price_unit":
            cents = dollars(data.get("amount", ""))
            if cents is None:
                raise HTTPException(422, "Type the price in dollars and cents, for example 125.00.")
            priced = f.rent.units.get(unit)
            args = {"rent_cents": cents, "size": priced.size if priced else None}
        elif action == "reserve":
            person = f.tenant_typed(data.get("tenant", "")[:80])
            if person is None:      # a typed name can miss, so it is said on the page like a refusal, not answered with an error
                fd.keep_step_note(ident, "no tenant has that name, or two do; pick the name from the list as it shows, with the phone where two share a name")
                return RedirectResponse("/units#note", status_code=303)
            try:
                last = date.fromisoformat(data.get("until", "")[:10])
            except ValueError:
                raise HTTPException(422, "Pick the last day of the hold.") from None
            if not f.today <= last <= f.today + timedelta(days=366):
                raise HTTPException(422, "The hold can end from today to a year ahead.")
            tenant, args = person["id"], {"until": at_local(last, 23, 59).isoformat()}
        elif action == "block_unit":
            reason = " ".join(data.get("reason", "").split())[:300]
            if len(reason) < 3:
                raise HTTPException(422, "Write why the unit is blocked, in a few words.")
            args = {"reason": reason}
        done = await run_in_threadpool(fd.staff_rent, ident, renting.Act(f"form:{token}:{action}:{unit}", action, STAFF, unit, tenant, args))
        return RedirectResponse(f"/units#u-{unit}" if done else "/units#note", status_code=303)     # a refusal lands where the note is

    @app.post("/rules/plan-policy", include_in_schema=False)
    async def plan_policy(request: Request):
        """The owner adopts a new version of the plan policy on this session's copy; outside the fixed limits nothing changes."""
        same_site(request)
        ident = need(request)
        values, why = bounded(await form(request), POLICY_FORM, col.POLICY_BOUNDS)
        done = await run_in_threadpool(fd.owner_setting, ident, "policy", values, why)
        return RedirectResponse("/rules#plan-policy" if done else "/rules#in-force", status_code=303)     # a refusal is said above the panels

    @app.post("/rules/schedule", include_in_schema=False)
    async def schedule(request: Request):
        """The owner adopts a new version of the schedule on this session's copy; outside the fixed limits nothing changes."""
        same_site(request)
        ident = need(request)
        values, why = bounded(await form(request), SCHEDULE_FORM, col.SCHEDULE_BOUNDS)
        done = await run_in_threadpool(fd.owner_setting, ident, "schedule", values, why)
        return RedirectResponse("/rules#schedule" if done else "/rules#in-force", status_code=303)

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
