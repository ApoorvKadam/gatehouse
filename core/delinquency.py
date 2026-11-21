"""Collections: payment plans, promises to pay, exemptions, the overlock and lien-eligibility steps, and payments through a
processor, on top of the books.

Code computes every term: the debt a plan covers, each amount to the cent, the dates (Eastern calendar days) and the finite set
of offers the owner's plan policy and the fixed limits below allow. A tenant accepts one offer by its version, in their own
words; the plan is active once that consent is recorded and any upfront part is confirmed. Money counts toward a plan only when
a payment the processor confirmed, allocated oldest first by billing, reaches a charge the plan covers.

While a plan is in good standing, an exemption is active or a dispute on the tenancy is open, the overlock and the
lien-eligibility step are held; a breached plan, or one sent to staff, holds them until staff decide. Every record here is an
event folded into Books.collections. The gate (core/gate.py) checks and records each command; the sweep makes sure nothing that
falls due lapses unseen."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from .billing import Ledger, Refused
from .books import Books, Charge, at_local, local
from .evidence import day, money
from .store import Store

STEP_DAYS = 14                  # instalments fall every two weeks from the day of the offer
PROMISE_DAYS = 30               # a promise to pay names a day at most this far ahead
EXEMPTION_DAYS = 90             # the longest exemption staff may grant at once
OWNER_ROLE = "office_manager"   # who collections notices and tasks go to
SCHEDULE_RUN = "schedule"       # the source of an enforcement step taken by the owner's schedule
SYSTEM = "collections"          # the source of a move code makes on its own (activation, breach, notices)
STEPS = ("overlock", "lien")
# the fixed limits: an owner policy or schedule outside them is refused, so no offer can ever exceed them
POLICY_BOUNDS = {"min_tenure_days": (0, 3660), "max_debt_cents": (100, 1_000_000), "longest_days": (STEP_DAYS, 180),
                 "most_instalments": (1, 6), "min_upfront_cents": (0, 100_000), "min_instalment_cents": (500, 100_000),
                 "grace_days": (0, 10)}
SCHEDULE_BOUNDS = {"overlock_days": (1, 90), "lien_days": (1, 365)}

PLAN_MOVES = {"proposed": ("offered", "cancelled"), "offered": ("offered", "accepted", "cancelled"), "accepted": ("active", "cancelled"),
              "active": ("completed", "breached", "review"), "breached": ("active", "cancelled"), "review": ("active", "cancelled"),
              "completed": ("review",), "cancelled": ()}
GOOD, WITH_STAFF, OPEN_PLAN = ("active",), ("breached", "review"), ("accepted", "active", "breached", "review")
# a return can arrive before its success was ever seen: the payment came in and went back, so nothing is posted for it
INTENT_MOVES = {"open": ("pending", "succeeded", "failed", "closed", "returned"), "pending": ("succeeded", "failed", "closed", "returned"),
                "failed": ("succeeded",), "succeeded": ("returned",), "returned": (),
                "closed": ("pending", "succeeded", "failed", "returned")}      # a payment that turns up after all is resolved, not dropped
UNSETTLED = ("open", "pending")
IN_FLIGHT = timedelta(minutes=15)   # a request still on its way: only an intent older than this is closed for want of a payment
PROCESSOR_STATES = {"processing": "pending", "succeeded": "succeeded", "failed": "failed", "canceled": "closed", "returned": "returned"}

ENFORCE = ("overlock", "flag_lien_eligible")
STAFF_ONLY = ("lift_overlock", "review_plan", "grant_exemption", "revoke_exemption", "take_payment")
MIN_CARD_CENTS = 50             # the card processor takes no payment smaller than this
ACTIONS = ("propose_plan", "offer_plan", "accept_plan", "record_promise") + ENFORCE + STAFF_ONLY


@dataclass
class Plan:
    id: str
    tenant: str
    state: str
    offers: list[dict]                  # what code computed when the plan was proposed
    policy_version: int
    offered: list[dict] = field(default_factory=list)   # the offers put to the tenant, each with its version
    terms: dict | None = None           # the accepted offer; never changed afterwards
    covered: list[dict] = field(default_factory=list)   # charge, lease, cents, and how many payments it had seen at acceptance
    consent: dict | None = None
    known: list[str] = field(default_factory=list)      # returns and disputes already put before staff
    history: list[tuple[str, datetime]] = field(default_factory=list)


@dataclass
class Promise:
    id: str
    tenant: str
    cents: int
    by: date
    callback: date
    made: datetime
    quote: str | None
    state: str = "open"                 # open, kept, missed, replaced


@dataclass
class Exemption:
    id: str
    lease: str
    tenant: str
    scope: list[str]
    reason: str
    owner: str
    until: date                         # the last day it holds
    revoked: bool = False
    expiry_noted: bool = False


@dataclass
class Intent:
    id: str
    tenant: str
    cents: int
    key: str                            # the idempotency key; it never changes, so a retry can never make a second payment
    copy: str                           # the visitor copy (stream) the payment belongs to
    simulated: bool
    state: str = "open"                 # open (outcome not known), pending, succeeded, failed, returned, closed
    outside: str | None = None          # the processor's payment id
    payment: str | None = None          # the payment posted in the books
    created: datetime | None = None


@dataclass
class Task:
    id: str
    ref: str                            # what it is about, so the same thing never opens two tasks
    kind: str
    tenant: str
    why: str
    due: date
    owner: str
    acknowledged: dict | None = None


@dataclass
class State:
    policy: dict | None = None
    schedule: dict | None = None
    plans: dict[str, Plan] = field(default_factory=dict)
    promises: dict[str, Promise] = field(default_factory=dict)
    exemptions: dict[str, Exemption] = field(default_factory=dict)
    overlocks: dict[str, dict] = field(default_factory=dict)    # lease -> the overlock record while it stands
    liens: dict[str, dict] = field(default_factory=dict)        # lease -> the lien-eligibility flag
    intents: dict[str, Intent] = field(default_factory=dict)
    webhooks: set[str] = field(default_factory=set)
    notices: dict[str, dict] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    sweeps: list[date] = field(default_factory=list)
    reconciled: list[dict] = field(default_factory=list)
    cards: dict[str, list[dict]] = field(default_factory=dict)   # tenant -> the test cards on file: our id, brand, last four, the processor-neutral token
    nonce: str | None = None            # this copy's mark at the processor; a reset copy keeps its name and gets a new mark


def state(books: Books) -> State:
    return books.collections or State()


def fold_event(b: Books, kind: str, at: datetime, p: dict) -> None:
    if b.collections is None:
        b.collections = State()
    st = b.collections
    if kind in ("plan_policy.adopted", "schedule.adopted"):
        attr = "policy" if kind == "plan_policy.adopted" else "schedule"
        if p["version"] > (getattr(st, attr) or {}).get("version", 0):      # a newer version only, as with owner rules
            setattr(st, attr, dict(p))
    elif kind == "plan.proposed":
        st.plans[p["plan"]] = Plan(p["plan"], p["tenant"], "proposed", p["offers"], p["policy_version"], history=[("proposed", at)])
        b._seen("PL", p["plan"])
    elif kind.startswith("plan."):
        plan = st.plans[p["plan"]]
        plan.state = p["to"]
        plan.history.append((p["to"], at))
        plan.known += p.get("known", [])
        if kind == "plan.offered":
            plan.offered.append(p["offer"])
        elif kind == "plan.accepted":
            plan.terms, plan.covered, plan.consent = p["terms"], p["covered"], p["consent"]
    elif kind == "promise.recorded":
        st.promises[p["promise"]] = Promise(p["promise"], p["tenant"], p["cents"], date.fromisoformat(p["by"]),
                                            date.fromisoformat(p["callback"]), at, p.get("quote"))
        b._seen("PR", p["promise"])
    elif kind.startswith("promise."):
        st.promises[p["promise"]].state = kind.split(".")[1]
    elif kind == "exemption.granted":
        st.exemptions[p["exemption"]] = Exemption(p["exemption"], p["lease"], p["tenant"], list(p["scope"]), p["reason"], p["owner"],
                                                  date.fromisoformat(p["until"]))
        b._seen("EX", p["exemption"])
    elif kind == "exemption.revoked":
        st.exemptions[p["exemption"]].revoked = True
    elif kind == "exemption.expired":
        st.exemptions[p["exemption"]].expiry_noted = True
    elif kind == "overlock.recorded":
        st.overlocks[p["lease"]] = {**p, "at": at}
    elif kind == "overlock.lifted":
        st.overlocks.pop(p["lease"], None)
    elif kind == "lien.flagged":
        st.liens[p["lease"]] = {**p, "at": at}
    elif kind == "intent.created":
        st.intents[p["intent"]] = Intent(p["intent"], p["tenant"], p["cents"], p["key"], p["copy"], p["simulated"], created=at)
        st.nonce = st.nonce or p["nonce"]
        b._seen("PI", p["intent"])
    elif kind == "intent.updated":
        i = st.intents[p["intent"]]
        i.state, i.outside, i.payment = p["state"], p.get("outside") or i.outside, p.get("payment") or i.payment
    elif kind == "webhook.received":
        st.webhooks.add(p["event"])
    elif kind == "notice.recorded":
        st.notices[p["notice"]] = {**p, "at": at}
    elif kind == "task.opened":
        st.tasks[p["task"]] = Task(p["task"], p["ref"], p["kind"], p["tenant"], p["why"], date.fromisoformat(p["due"]), p["owner"])
        b._seen("TK", p["task"])
    elif kind == "task.acknowledged":
        st.tasks[p["task"]].acknowledged = {"by": p["by"], "at": at}
    elif kind == "sweep.ran":
        st.sweeps.append(date.fromisoformat(p["day"]))
    elif kind == "reconcile.ran":
        st.reconciled.append({**p, "at": at})
    elif kind == "card.saved":
        st.cards.setdefault(p["tenant"], []).append({k: p[k] for k in ("card", "brand", "last4", "token")})
        b._seen("CD", p["card"])


# the owner's settings ------------------------------------------------------------------------------------------------
def _bounded(form: dict, bounds: dict, what: str) -> None:
    if set(form) != set(bounds):
        raise Refused(f"the {what} has exactly these fields: {', '.join(bounds)}")
    for k, (lo, hi) in bounds.items():
        v = form[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise Refused(f"the {what}'s {k.replace('_', ' ')} is outside the fixed limits ({lo} to {hi})")


def adopt_policy(led: Ledger, at: datetime, version: int, policy: dict, by: str = "owner") -> None:
    """The owner's plan policy, a bounded typed form: who may have a plan (tenure, largest balance), the longest plan, the most
    instalments, the smallest upfront and instalment amounts, and the grace days after each due date."""
    if by != "owner":
        raise Refused("only the owner sets the plan policy")
    if version <= (state(led.books).policy or {}).get("version", 0):
        raise Refused("the plan policy is already at that version or a later one")
    _bounded(policy, POLICY_BOUNDS, "plan policy")
    led.emit("plan_policy.adopted", at, {"version": version, **policy, "by": by}, f"plan_policy:{version}", source=by)


def adopt_schedule(led: Ledger, at: datetime, version: int, overlock_days: int, lien_days: int, by: str = "owner") -> None:
    """The owner's schedule: how many days after the due date the overlock and lien eligibility come due. His setting, never law."""
    if by != "owner":
        raise Refused("only the owner sets the schedule")
    if version <= (state(led.books).schedule or {}).get("version", 0):
        raise Refused("the schedule is already at that version or a later one")
    form = {"overlock_days": overlock_days, "lien_days": lien_days}
    _bounded(form, SCHEDULE_BOUNDS, "schedule")
    led.emit("schedule.adopted", at, {"version": version, **form, "by": by}, f"schedule:{version}", source=by)


# offers ---------------------------------------------------------------------------------------------------------------
def owed(books: Books, tenant: str, now: datetime) -> list[Charge]:
    """What a plan may cover: the tenant's past-due open charges, oldest first, leaving out any under a hold or an open dispute."""
    today = local(now).date()
    disputed = {d.charge for d in books.disputes.values() if d.outcome is None}
    return [c for c in books.open_charges(tenant) if c.due < today and c.id not in disputed]


def ineligible(books: Books, tenant: str, now: datetime) -> str | None:
    """Why the tenant cannot be offered a plan, or None."""
    st = state(books)
    pol = st.policy
    if pol is None:
        return "the owner has not set a plan policy"
    if any(p.tenant == tenant and p.state in OPEN_PLAN for p in st.plans.values()):
        return "the tenant already has a plan"
    charges = owed(books, tenant, now)
    if any(books.leases[c.lease].ended for c in charges):
        return "part of the balance is on a lease that has ended; staff handle it"
    total = sum(c.open_cents for c in charges)
    if total == 0:
        return "nothing is past due"
    if total > pol["max_debt_cents"]:
        return f"the balance is over the {money(pol['max_debt_cents'])} the owner's policy allows on a plan"
    starts = [x.start for x in books.leases.values() if x.tenant == tenant]
    if (local(now).date() - min(starts)).days < pol["min_tenure_days"]:
        return f"the tenant has rented for less than the {pol['min_tenure_days']} days the owner's policy asks"
    return None


def offer(books: Books, tenant: str, now: datetime, instalments, upfront=None) -> tuple[dict | None, str]:
    """One plan, built by code: the upfront part today (the policy's smallest when none is named), then the rest in equal
    instalments to the cent, the last taking the remainder, every STEP_DAYS. Returns (offer, "") or (None, why not)."""
    why = ineligible(books, tenant, now)
    if why:
        return None, why
    pol = state(books).policy
    charges = owed(books, tenant, now)
    total = sum(c.open_cents for c in charges)
    up = pol["min_upfront_cents"] if upfront is None else upfront
    parts, why = split(total, up, instalments, pol, local(now).date())
    if parts is None:
        return None, why
    terms = {"tenant": tenant, "total": total, "upfront": up, "instalments": instalments, "parts": parts, "grace_days": pol["grace_days"],
             "charges": [{"charge": c.id, "lease": c.lease, "cents": c.open_cents} for c in charges], "policy_version": pol["version"]}
    return {**terms, "version": hashlib.sha256(json.dumps(terms, sort_keys=True).encode()).hexdigest()[:12]}, ""


def split(total: int, up, instalments, pol: dict, today: date) -> tuple[list[dict] | None, str]:
    """The parts of one plan for a balance, by the owner's policy: the upfront part today, then equal instalments to the cent every
    STEP_DAYS, the last taking the remainder. Returns (parts, "") or (None, why the policy does not allow it)."""
    if not isinstance(instalments, int) or isinstance(instalments, bool) or not 1 <= instalments <= pol["most_instalments"]:
        return None, f"a plan has 1 to {pol['most_instalments']} instalments after the upfront part"
    if instalments * STEP_DAYS > pol["longest_days"]:
        return None, f"the longest plan is {pol['longest_days']} days, which fits {pol['longest_days'] // STEP_DAYS} instalments two weeks apart"
    if not isinstance(up, int) or isinstance(up, bool) or up < pol["min_upfront_cents"]:
        return None, f"the upfront part is at least {money(pol['min_upfront_cents'])}"
    rest = total - up
    if rest <= 0:
        return None, "that pays the whole balance now, so no plan is needed"
    each = rest // instalments
    if each < pol["min_instalment_cents"]:
        return None, f"each instalment is at least {money(pol['min_instalment_cents'])}"
    amounts = [each] * (instalments - 1) + [rest - each * (instalments - 1)]
    return ([{"part": 0, "cents": up, "due": today.isoformat()}] if up else []) + [
        {"part": k, "cents": a, "due": (today + timedelta(days=STEP_DAYS * k)).isoformat()} for k, a in enumerate(amounts, 1)], ""


def offers(books: Books, tenant: str, now: datetime) -> list[dict]:
    """The finite set of plans the owner's policy allows for this balance today, at the smallest upfront part."""
    pol = state(books).policy
    return [o for n in range(1, (pol or {}).get("most_instalments", 0) + 1) if (o := offer(books, tenant, now, n)[0])]


def words(o: dict) -> str:
    later = [p for p in o["parts"] if p["part"]]
    each = ", ".join(f"{money(p['cents'])} on {day(date.fromisoformat(p['due']))}" for p in later)
    head = f"{money(o['upfront'])} today, then " if o["upfront"] else ""
    return f"{head}{len(later)} payment{'s' if len(later) > 1 else ''}: {each}"


def refusal(why: str, allowed: list[dict]) -> str:
    if not allowed:
        return f"That plan is not possible: {why}."
    return f"That plan is not possible: {why}. The plans on offer are: " + "; ".join(words(o) for o in allowed) + "."


def counter(books: Books, tenant: str, ask: dict, now: datetime) -> dict:
    """A tenant's counter-offer, read into a typed form (instalments, upfront_cents, total_cents) and answered by code: the plan
    it names when the policy allows it, a refusal written here with the plans that are allowed, or staff when no plan can be
    offered or the ask is to owe less. Nothing in the ask becomes a term unless code builds the same plan."""
    why = ineligible(books, tenant, now)
    if why:
        return {"answer": "staff", "why": why}
    total = sum(c.open_cents for c in owed(books, tenant, now))
    if ask.get("total_cents") not in (None, total):
        return {"answer": "staff", "why": "only staff can change what is owed"}
    allowed = offers(books, tenant, now)
    if not allowed:
        return {"answer": "staff", "why": "no plan fits the owner's policy for this balance"}
    up = ask.get("upfront_cents")
    if ask.get("instalments") is None:
        fit = [o for n in range(1, state(books).policy["most_instalments"] + 1) if (o := offer(books, tenant, now, n, up)[0])]
        if fit:
            return {"answer": "choose", "alternatives": fit}
        why = offer(books, tenant, now, 1, up)[1]
    else:
        o, why = offer(books, tenant, now, ask["instalments"], up)
        if o:
            return {"answer": "offer", "offer": o}
    return {"answer": "refused", "why": why, "text": refusal(why, allowed), "alternatives": allowed}


# plans ----------------------------------------------------------------------------------------------------------------
def _move(led: Ledger, plan: Plan, kind: str, to: str, at: datetime, payload: dict, source: str, command: str | None = None) -> None:
    if to not in PLAN_MOVES[plan.state]:
        raise Refused(f"a plan that is {plan.state} cannot become {to}")
    led.emit(kind, at, {"plan": plan.id, "to": to, **payload}, f"plan:{plan.id}:{len(plan.history)}", source=source, command=command)


def propose(led: Ledger, tenant: str, at: datetime, source: str, command: str | None = None) -> str:
    """Code computes the offers for the tenant's balance; an earlier proposal nobody accepted is replaced."""
    b = led.books
    why = ineligible(b, tenant, at)
    if why:
        raise Refused(why)
    for p in list(state(b).plans.values()):
        if p.tenant == tenant and p.state in ("proposed", "offered"):
            _move(led, p, "plan.cancelled", "cancelled", at, {"why": "replaced by a new proposal"}, source, command)
    pid = b.next_id("PL")
    led.emit("plan.proposed", at, {"plan": pid, "tenant": tenant, "offers": offers(b, tenant, at), "policy_version": state(b).policy["version"]},
             f"plan:{pid}:0", source=source, command=command)
    return pid


def put_offer(led: Ledger, plan_id: str, instalments, upfront, at: datetime, source: str, command: str | None = None) -> dict:
    """Put one plan to the tenant; code builds it, so its terms are always inside the policy."""
    plan = state(led.books).plans[plan_id]
    o, why = offer(led.books, plan.tenant, at, instalments, upfront)
    if o is None:
        raise Refused(why)
    _move(led, plan, "plan.offered", "offered", at, {"offer": o}, source, command)
    return o


def stale(books: Books, plan: Plan, version: str, now: datetime) -> str | None:
    """Why the tenant cannot accept this version now, or None: it must be an offer put to them, and code must build exactly
    the same offer from today's books."""
    put = next((o for o in plan.offered if o["version"] == version), None)
    if put is None:
        return "the tenant can accept only an offer that was put to them, named by its version"
    again, _ = offer(books, plan.tenant, now, put["instalments"], put["upfront"])
    if again is None or again["version"] != version:
        return "the offer has changed since it was made (the balance or the day moved), so it is offered again before anyone accepts"
    return None


def accept(led: Ledger, plan_id: str, version: str, quote: str, at: datetime, source: str, command: str | None = None) -> None:
    """The tenant's consent to one offer version. The accepted terms and each covered charge's payments so far are recorded,
    so later money is counted from this moment."""
    b = led.books
    plan = state(b).plans[plan_id]
    why = stale(b, plan, version, at) or (None if (quote or "").strip() else "acceptance needs the tenant's own words")
    if why:
        raise Refused(why)
    terms = next(o for o in plan.offered if o["version"] == version)
    covered = [{**c, "seen": len(b.charges[c["charge"]].paid_effective)} for c in terms["charges"]]
    gone = [f"returned:{p.id}" for p in b.payments.values() if p.tenant == plan.tenant and p.returned]
    _move(led, plan, "plan.accepted", "accepted", at, {"terms": terms, "covered": covered, "known": gone,
          "consent": {"version": version, "quote": quote, "by": source}}, source, command)
    recheck(led, plan.tenant, at)


def paid_toward(books: Books, plan: Plan, by: datetime | None = None) -> int:
    """Confirmed money that reached the plan's charges after acceptance, by billing's own oldest-first allocation, counted by
    when the processor confirmed it; with `by`, only money in before then. A payment that came back never counts."""
    gone = {p.id for p in books.payments.values() if p.returned}
    return sum(c for cov in plan.covered for t, c, src in books.charges[cov["charge"]].paid_effective[cov["seen"]:]
               if src not in gone and (by is None or t < by))


def cards_of(books: Books, tenant: str) -> list[dict]:
    """The test cards on file for the tenant. Only a brand, the last four digits and a token the processor adapter knows are kept."""
    return list(state(books).cards.get(tenant, []))


def save_card(led: Ledger, tenant: str, at: datetime, brand: str, last4: str, token: str) -> str:
    cid = led.books.next_id("CD")
    led.emit("card.saved", at, {"card": cid, "tenant": tenant, "brand": brand, "last4": last4, "token": token}, f"card:{cid}", source=SYSTEM)
    return cid


def owing(books: Books, tenant: str) -> int:
    """What a payment can pay: the tenant's open charges, held ones left out as billing leaves them out."""
    return sum(c.open_cents for c in books.open_charges(tenant))


def unsettled(books: Books, tenant: str) -> bool:
    return any(i.tenant == tenant and i.state in UNSETTLED for i in state(books).intents.values())


def instalments(books: Books, plan: Plan, now: datetime) -> list[dict]:
    """Each part of an accepted plan with its state: scheduled, due, paid, partly paid, missed or uncertain. Money fills the
    parts in order; a part is paid only when confirmed money covers all of it. A part past its grace days while a payment is
    still waiting for the processor is uncertain, never missed."""
    if plan.terms is None:
        return []
    paid, before, today = paid_toward(books, plan), 0, local(now).date()
    waiting = unsettled(books, plan.tenant)
    out = []
    for part in plan.terms["parts"]:
        due = date.fromisoformat(part["due"])
        covered = max(0, min(part["cents"], paid - before))
        end = at_local(due + timedelta(days=plan.terms["grace_days"] + 1))       # the first moment after its grace days
        if now >= end:          # judged on the money confirmed by then: what came later cannot undo a miss
            s = "paid" if paid_toward(books, plan, end) - before >= part["cents"] else "uncertain" if waiting else "missed"
        elif covered == part["cents"]:
            s = "paid"
        elif today < due:
            s = "partly paid" if covered else "scheduled"
        elif waiting:
            s = "uncertain"
        else:
            s = "partly paid" if covered else "due"
        before += part["cents"]
        out.append({**part, "covered": covered, "state": s})
    return out


def _task(led: Ledger, kind: str, tenant: str, ref: str, why: str, due: date, at: datetime) -> None:
    if any(t.ref == ref for t in state(led.books).tasks.values()):
        return
    tid = led.books.next_id("TK")
    led.emit("task.opened", at, {"task": tid, "ref": ref, "kind": kind, "tenant": tenant, "why": why, "due": due.isoformat(),
                                 "owner": OWNER_ROLE}, f"task:{ref}", source=SYSTEM)


def _notice(led: Ledger, notice: str, what: str, tenant: str, due: date, at: datetime) -> None:
    """A record that an item came due and who it was put in front of; once per item."""
    if notice in state(led.books).notices:
        return
    led.emit("notice.recorded", at, {"notice": notice, "what": what, "tenant": tenant, "due": due.isoformat(), "to": OWNER_ROLE, "by": SYSTEM},
             f"notice:{notice}", source=SYSTEM)


def recheck(led: Ledger, tenant: str, at: datetime) -> None:
    """Move the tenant's plans to the state their money and records call for now: active once the upfront part is confirmed,
    completed when all of it is, and to staff when a payment came back or a covered charge went under dispute. Safe to repeat."""
    b, today = led.books, local(at).date()
    for plan in [p for p in state(b).plans.values() if p.tenant == tenant and p.state in ("accepted", "active", "completed")]:
        charges = {c["charge"] for c in plan.covered}
        back = [f"returned:{p.id}" for p in b.payments.values() if p.tenant == tenant and p.returned and f"returned:{p.id}" not in plan.known]
        if plan.state == "completed":       # a payment that came back reopened what the plan had settled: staff decide again
            if back and any(b.charges[c].open_cents for c in charges):
                why = "a payment came back after the plan was completed, so what it paid is owed again"
                _move(led, plan, "plan.review", "review", at, {"why": why, "known": back}, SYSTEM)
                _task(led, "plan needs staff", tenant, f"review:{plan.id}:{back[0]}", why, today, at)
            continue
        paid = paid_toward(b, plan)
        if plan.state == "accepted" and paid >= plan.terms["upfront"]:
            _move(led, plan, "plan.activated", "active", at, {"why": "consent recorded and the upfront part confirmed"
                                                              if plan.terms["upfront"] else "consent recorded"}, SYSTEM)
        if plan.state != "active":
            continue
        new = list(back)
        new += [f"dispute:{d.id}" for d in b.disputes.values() if d.charge in charges and d.outcome is None and f"dispute:{d.id}" not in plan.known]
        new += [f"closed:{c}" for c in sorted(charges) if b.charges[c].closed and f"closed:{c}" not in plan.known]
        if new:
            why = {"returned": "a payment came back, so what it paid is owed again",
                   "dispute": "a charge in the plan is disputed; its collection waits and staff decide whether the plan changes",
                   "closed": "a charge in the plan was waived or reversed, so its amounts no longer match what is owed; staff decide"}[new[0].split(":")[0]]
            _move(led, plan, "plan.review", "review", at, {"why": why, "known": new}, SYSTEM)
            _task(led, "plan needs staff", tenant, f"review:{plan.id}:{new[0]}", why, today, at)
        elif paid >= plan.terms["total"] or all(b.charges[c].open_cents == 0 for c in charges):
            # a part missed before the money came is still a miss: the plan goes to staff, and is never completed past it
            missed = next((x for x in instalments(b, plan, at) if x["state"] == "missed" and f"missed:{x['part']}" not in plan.known), None)
            if missed:
                due, grace = date.fromisoformat(missed["due"]), plan.terms["grace_days"]
                why = f"the part due {day(due)} was not paid within {grace} grace days; the rest was paid later"
                _move(led, plan, "plan.breached", "breached", at, {"why": why, "part": missed["part"]}, SYSTEM)
                _task(led, "plan breached", tenant, f"breached:{plan.id}:{missed['part']}", why, due + timedelta(days=grace + 1), at)
            else:
                _move(led, plan, "plan.completed", "completed", at, {"why": "nothing the plan covers is still owed"}, SYSTEM)
    from . import liens             # a lien file reads these books too, and liens imports this module, so it cannot be imported at the top
    liens.recheck(led, tenant, at)  # a payment, waiver or reversal may have changed what an open lien case was approved on


def promise(led: Ledger, tenant: str, cents: int, by: date, quote: str | None, at: datetime, source: str, command: str | None = None) -> str:
    """A promise to pay brings a call-back the day after its date; it holds no enforcement step."""
    b = led.books
    for p in list(state(b).promises.values()):
        if p.tenant == tenant and p.state == "open":
            led.emit("promise.replaced", at, {"promise": p.id}, f"promise:{p.id}:replaced", source=source, command=command)
    pid = b.next_id("PR")
    led.emit("promise.recorded", at, {"promise": pid, "tenant": tenant, "cents": cents, "by": by.isoformat(),
                                      "callback": (by + timedelta(days=1)).isoformat(), "quote": quote}, f"promise:{pid}", source=source, command=command)
    return pid


def promise_paid(books: Books, p: Promise) -> int:
    return sum(x.cents for x in books.payments.values()
               if x.tenant == p.tenant and not x.returned and x.known_at >= p.made and local(x.known_at).date() <= p.by)


def exemption_state(e: Exemption, today: date) -> str:
    return "revoked" if e.revoked else "expired" if e.until < today else "active"


def acknowledge(led: Ledger, task: str, who: str, at: datetime) -> None:
    """Someone saw the task; until then it stays on Today."""
    t = state(led.books).tasks.get(task)
    if t is None or t.acknowledged:
        raise Refused("no open task with that id")
    led.emit("task.acknowledged", at, {"task": task, "by": who}, f"task:{t.ref}:seen", source=who)


# the overlock and lien-eligibility steps ------------------------------------------------------------------------------
def days_past_due(books: Books, lease: str, now: datetime) -> int | None:
    """How many days past due the lease's oldest unpaid charge is (charges under a hold are not past due)."""
    today = local(now).date()
    dues = [c.due for i in books.of_lease.get(lease, ()) if (c := books.charges[i]).open_cents > 0 and not books.held(i) and c.due < today]
    return (today - min(dues)).days if dues else None


def enforcement_hold(books: Books, lease: str, step: str, now: datetime) -> tuple[str, str] | None:
    """What holds an enforcement step on the whole tenancy, as (state, why): an open dispute on any of its charges, an active
    exemption for the step, or the tenant's plan covering it (in good standing: blocked; breached or with staff: waiting for
    staff). Separate from the charge-level dispute hold, which only stops collection of one charge."""
    st, today, tenant = state(books), local(now).date(), books.leases[lease].tenant
    if any(d.outcome is None and books.charges[d.charge].lease == lease for d in books.disputes.values()):
        return "blocked", "a dispute on this tenancy is open"
    for e in st.exemptions.values():
        if e.lease == lease and step in e.scope and exemption_state(e, today) == "active":
            return "blocked", f"an exemption holds it until {day(e.until)}: {e.reason}"
    for p in st.plans.values():
        if p.tenant == tenant and any(c["lease"] == lease for c in p.covered):
            if p.state in GOOD:
                return "blocked", "the tenant's payment plan is in good standing"
            if p.state in WITH_STAFF:
                return "waiting for staff", ("the payment plan was breached; staff review it before any step" if p.state == "breached"
                                             else "the payment plan is with staff")
    return None


def step_state(books: Books, lease: str, step: str, now: datetime, ignore_recorded: bool = False) -> tuple[str, str]:
    """Where the lease stands for the step, and why: clear, eligible, blocked, waiting for staff or recorded. The gate allows the
    step only when this says eligible, so what a page shows and what the gate does cannot differ. A lease that is not due on the
    owner's schedule is clear, whatever else is going on: staff never see it waiting for them. `ignore_recorded` asks the same
    question of a unit whose step is already recorded (a lien case is opened on what is due now, never on the old flag)."""
    st = state(books)
    ended = books.leases[lease].ended
    if ended:                   # the tenant moved out: no unit to hold or overlock, whatever is still owed
        return "clear", f"the lease ended {day(ended)}, so there is no unit to overlock; staff handle what is still owed"
    if not ignore_recorded and (st.overlocks if step == "overlock" else st.liens).get(lease):
        return "recorded", "that step is already recorded on this unit"
    if st.schedule is None:
        return "clear", "the owner has set no schedule for this step"
    late, days = days_past_due(books, lease, now), st.schedule[f"{step}_days"]
    if late is None or late < days:
        return "clear", f"not due on the owner's schedule: {days} days after the due date, and the oldest unpaid charge is {late or 0} days past due"
    if unsettled(books, books.leases[lease].tenant):
        return "waiting for staff", "a payment from this tenant is waiting for the processor's answer"
    # the holds on the whole tenancy
    return enforcement_hold(books, lease, step, now) or ("eligible", f"{late} days past due; the owner's schedule says {days}")


# the gate's checks for these actions, in its order: the action and scope, fixed safety, the owner's settings, then holds --
def _date(s) -> date | None:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def check(books: Books, cmd, now: datetime) -> tuple[bool, str]:
    st, today, form, a = state(books), local(now).date(), cmd.form or {}, cmd.action
    staff = cmd.source.startswith("staff:")
    # 1. the action and its scope
    if a in STAFF_ONLY and not staff:
        return False, "only staff take this step"
    if a in ENFORCE and not (staff or cmd.source == SCHEDULE_RUN):
        return False, "only staff or the owner's schedule take an enforcement step"
    lease = books.leases.get(cmd.lease or "")
    if a in ENFORCE + ("lift_overlock", "grant_exemption", "revoke_exemption"):
        if lease is None or lease.tenant != cmd.tenant:
            return False, "no such unit on this tenant's account"
        if lease.ended:
            return False, "that lease has ended; staff handle it"
    plan = st.plans.get(cmd.plan or "")
    if a in ("offer_plan", "accept_plan", "review_plan") and (plan is None or plan.tenant != cmd.tenant):
        return False, "no such plan on this tenant's account"
    if a == "propose_plan":
        why = ineligible(books, cmd.tenant, now)
        n = len(offers(books, cmd.tenant, now)) if why is None else 0
        return (True, f"{n} plans computed under the owner's policy") if n else (False, why or "no plan fits the owner's policy for this balance")
    if a == "offer_plan":
        if plan.state not in ("proposed", "offered"):
            return False, f"the plan is {plan.state}, so no new offer is made on it"
        o, why = offer(books, cmd.tenant, now, form.get("instalments"), form.get("upfront_cents"))
        return (True, f"offered under the owner's policy: {words(o)}") if o else (False, refusal(why, offers(books, cmd.tenant, now)))
    if a == "accept_plan":
        if plan.state != "offered":
            return False, f"the plan is {plan.state}, so there is nothing to accept"
        if not (cmd.quote or "").strip():
            return False, "acceptance needs the tenant's own words"
        why = stale(books, plan, cmd.offer, now)
        return (False, why) if why else (True, "the tenant accepted the offer in their own words")
    if a == "record_promise":
        cents, by = form.get("cents"), _date(form.get("by"))
        if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
            return False, "a promise needs an amount"
        if by is None or not today <= by <= today + timedelta(days=PROMISE_DAYS):
            return False, f"a promise names a day within the next {PROMISE_DAYS} days"
        return True, f"promise recorded with a call-back on {day(by + timedelta(days=1))}; it holds no step"
    if a in ENFORCE:
        step = "overlock" if a == "overlock" else "lien"
        where, why = step_state(books, lease.id, step, now)
        if where != "eligible":
            return False, why
        return True, ("due on the owner's schedule and nothing holds it; recorded in the FMS only, no lock was touched" if step == "overlock"
                      else "due on the owner's schedule and nothing holds it; lien eligibility recorded, no lien step taken")
    if a == "take_payment":
        cents = form.get("cents")
        if not isinstance(cents, int) or isinstance(cents, bool) or cents < MIN_CARD_CENTS:
            return False, f"a card payment is at least {money(MIN_CARD_CENTS)}"
        if cents > owing(books, cmd.tenant):
            return False, f"the tenant owes {money(owing(books, cmd.tenant))}, and a card payment is never more than that"
        if not any(c["card"] == form.get("card") for c in cards_of(books, cmd.tenant)):
            return False, "that card is not on file for this tenant"
        if unsettled(books, cmd.tenant):
            return False, "an earlier payment's outcome is not known yet; it is checked with the processor, never paid again"
        return True, f"a card payment of {money(cents)} goes to the card processor; it counts when the processor confirms it"
    if a == "lift_overlock":
        rec = st.overlocks.get(lease.id)
        if rec is None:
            return False, "no overlock is recorded on this unit"
        if not any(p.tenant == cmd.tenant and not p.returned and p.known_at >= rec["at"] for p in books.payments.values()):
            return False, "an overlock is lifted only after a payment the processor confirmed"
        return True, "lifted after a confirmed payment"
    if a == "review_plan":
        if plan.state not in WITH_STAFF:
            return False, f"the plan is {plan.state}; staff review a breached plan or one sent to them"
        if form.get("outcome") not in ("reinstate", "end"):
            return False, "staff reinstate the plan or end it"
        # a reinstated plan with nothing still to fall due could never breach again, so it would hold the steps for good
        if form["outcome"] == "reinstate" and not any(now < at_local(date.fromisoformat(p["due"]) + timedelta(days=plan.terms["grace_days"] + 1))
                                                      for p in plan.terms["parts"]):
            return False, "every part of the plan is past its grace days, so reinstating it would leave nothing to pay; end it instead"
        return True, "plan reinstated by staff" if form["outcome"] == "reinstate" else "plan ended by staff; the steps it held may go ahead"
    if a == "grant_exemption":
        scope, until = form.get("scope") or [], _date(form.get("until"))
        if not scope or not set(scope) <= set(STEPS):
            return False, "an exemption covers the overlock, lien eligibility or both"
        if not (form.get("reason") or "").strip():
            return False, "an exemption needs a reason"
        if until is None or not today <= until <= today + timedelta(days=EXEMPTION_DAYS):
            return False, f"an exemption ends within the next {EXEMPTION_DAYS} days"
        return True, f"exemption granted until {day(until)}"
    if a == "revoke_exemption":
        e = st.exemptions.get(form.get("exemption") or "")
        if e is None or e.lease != lease.id or exemption_state(e, today) != "active":
            return False, "no active exemption with that id on this unit"
        return True, "exemption revoked"
    return False, "not an action the system knows"


def act(led: Ledger, cmd, now: datetime) -> None:
    """Record an allowed command (the gate has checked it)."""
    a, form, src, cid = cmd.action, cmd.form or {}, cmd.source, cmd.id
    st = state(led.books)
    if a == "propose_plan":
        propose(led, cmd.tenant, now, src, cid)
    elif a == "offer_plan":
        put_offer(led, cmd.plan, form.get("instalments"), form.get("upfront_cents"), now, src, cid)
    elif a == "accept_plan":
        accept(led, cmd.plan, cmd.offer, cmd.quote, now, src, cid)
    elif a == "record_promise":
        promise(led, cmd.tenant, form["cents"], date.fromisoformat(form["by"]), cmd.quote, now, src, cid)
    elif a == "overlock":
        led.emit("overlock.recorded", now, {"lease": cmd.lease, "tenant": cmd.tenant, "by": src, "record_only": True},
                 f"overlock:{cmd.lease}:{cid}", source=src, command=cid)
    elif a == "lift_overlock":
        led.emit("overlock.lifted", now, {"lease": cmd.lease, "tenant": cmd.tenant, "by": src}, f"overlock-lifted:{cmd.lease}:{cid}", source=src, command=cid)
    elif a == "flag_lien_eligible":
        led.emit("lien.flagged", now, {"lease": cmd.lease, "tenant": cmd.tenant, "by": src}, f"lien:{cmd.lease}:{cid}", source=src, command=cid)
    elif a == "review_plan":
        plan = st.plans[cmd.plan]
        to = "active" if form["outcome"] == "reinstate" else "cancelled"
        # a reinstated plan is not breached again for the parts staff saw missed; a later missed part breaches it
        seen = [f"missed:{x['part']}" for x in instalments(led.books, plan, now) if x["state"] == "missed"] if to == "active" else []
        _move(led, plan, "plan.reviewed", to, now, {"why": cmd.reason or f"staff chose to {form['outcome']} it", "by": src, "known": seen},
              src, cid)
        # the decision settles what the plan had put on Today
        for t in [t for t in st.tasks.values() if not t.acknowledged and t.ref.split(":")[0] in ("breached", "review", "short")
                  and t.ref.split(":")[1] == plan.id]:
            led.emit("task.acknowledged", now, {"task": t.id, "by": src}, f"task:{t.ref}:seen", source=src, command=cid)
    elif a == "grant_exemption":
        eid = led.books.next_id("EX")
        led.emit("exemption.granted", now, {"exemption": eid, "lease": cmd.lease, "tenant": cmd.tenant, "scope": sorted(set(form["scope"])),
                                            "reason": form["reason"].strip(), "owner": src.removeprefix("staff:"), "until": form["until"]},
                 f"exemption:{eid}", source=src, command=cid)
    elif a == "revoke_exemption":
        led.emit("exemption.revoked", now, {"exemption": form["exemption"], "by": src}, f"exemption:{form['exemption']}:revoked", source=src, command=cid)


def in_plan(books: Books, charge: str) -> bool:
    """Whether an accepted plan covers the charge (a dispute on it then pauses its collection)."""
    return any(p.state in OPEN_PLAN and any(c["charge"] == charge for c in p.covered) for p in state(books).plans.values())


# the sweep: nothing that falls due lapses unseen ------------------------------------------------------------------------
def run_sweep(led: Ledger, now: datetime) -> None:
    """Notice what is due, breach what was missed after its grace days, settle promises and exemptions. Everything is keyed, so
    running it twice, or late, records each thing once; a late run dates each task by the day it fell due."""
    b, today = led.books, local(now).date()
    st = state(b)
    from . import lien_money, liens             # see recheck: liens imports this module
    for tenant in sorted({p.tenant for p in st.plans.values() if p.state in ("accepted", "active", "completed")}
                         | {c.tenant for c in liens.state(b).cases.values() if not c.closed}      # an open lien case is rechecked with or without a plan
                         | lien_money.tenants(b)):                                                # and so is a tenant whose surplus is not yet paid
        recheck(led, tenant, now)
    for plan in [p for p in st.plans.values() if p.state in OPEN_PLAN]:
        grace = timedelta(days=plan.terms["grace_days"])
        held = any(b.held(c["charge"]) or b.charges[c["charge"]].closed for c in plan.covered)
        # every part that fell due is noticed, however long nothing ran; a plan already breached or with staff gets notices only,
        # and one pass breaches a plan once
        acted = plan.state not in ("accepted", "active")
        for part in instalments(b, plan, now):
            due, n = date.fromisoformat(part["due"]), part["part"]
            if part["state"] == "paid" or today < due:
                continue
            _notice(led, f"due:{plan.id}:{n}", "instalment due", plan.tenant, due, now)
            if today <= due + grace or f"missed:{n}" in plan.known:
                continue
            if part["state"] == "uncertain":
                _task(led, "payment uncertain", plan.tenant, f"uncertain:{plan.id}:{n}",
                      "a payment is still waiting for the processor's answer after the grace days; the plan is not breached", due + grace, now)
            elif acted:
                continue
            elif plan.state == "accepted":
                _move(led, plan, "plan.cancelled", "cancelled", now, {"why": "the upfront part was not paid within the grace days"}, SYSTEM)
                _notice(led, f"lapsed:{plan.id}", "plan never started", plan.tenant, due + grace, now)
                break
            elif held:
                _task(led, "plan needs staff", plan.tenant, f"short:{plan.id}:{n}",
                      "a part is short because a charge in the plan is held or was closed; staff decide", due + grace + timedelta(days=1), now)
                acted = True
            else:
                why = f"the part due {day(due)} was not paid within {plan.terms['grace_days']} grace days"
                _move(led, plan, "plan.breached", "breached", now, {"why": why, "part": n}, SYSTEM)
                _task(led, "plan breached", plan.tenant, f"breached:{plan.id}:{n}", why, due + grace + timedelta(days=1), now)
                acted = True
    for p in list(st.promises.values()):
        if p.state == "open" and today >= p.callback:
            if promise_paid(b, p) >= p.cents:
                led.emit("promise.kept", now, {"promise": p.id}, f"promise:{p.id}:kept", source=SYSTEM)
            else:
                led.emit("promise.missed", now, {"promise": p.id}, f"promise:{p.id}:missed", source=SYSTEM)
                _task(led, "call back", p.tenant, f"callback:{p.id}", f"promised {money(p.cents)} by {day(p.by)}", p.callback, now)
    for e in list(st.exemptions.values()):
        if e.revoked:
            continue
        if e.until < today and not e.expiry_noted:
            led.emit("exemption.expired", now, {"exemption": e.id}, f"exemption:{e.id}:expired", source=SYSTEM)
            _task(led, "exemption ended", e.tenant, f"ended:{e.id}", f"the exemption ({e.reason}) ended {day(e.until)}", e.until + timedelta(days=1), now)
        elif 0 <= (e.until - today).days <= 1:
            _notice(led, f"ends:{e.id}", "exemption ends", e.tenant, e.until, now)
    if today not in st.sweeps:
        led.emit("sweep.ran", now, {"day": today.isoformat()}, f"sweep:{today.isoformat()}", source=SYSTEM)


def sweep(store: Store, stream: str, now: datetime) -> list:
    """The daily pass, and the catch-up when the service starts: whatever fell due while nothing ran is noticed now."""
    with store.write():
        led = Ledger(store.books(stream))
        run_sweep(led, now)
        return store.append(stream, led.pending)


def open_tasks(books: Books) -> list[Task]:
    """What stays on Today until someone acknowledges it."""
    return sorted((t for t in state(books).tasks.values() if not t.acknowledged), key=lambda t: (t.due, t.id))


# payments through a processor -----------------------------------------------------------------------------------------
class ProcessorTimeout(Exception):
    """The request went out and no answer came back: the payment may or may not exist."""


class ProcessorRefused(Exception):
    """The processor refused the request outright (a bad amount, an unknown card or key): no payment exists, so the intent is closed."""


class Processor(Protocol):
    """A payment processor adapter. One payment per idempotency key; the processor's own record is the truth."""
    simulated: bool

    def create(self, key: str, cents: int, card: str, metadata: dict, now: datetime) -> dict: ...
    def retrieve(self, outside: str) -> dict: ...
    # `since` is how far back a lookup that has to list payments must read: the creation time of the oldest intent still waiting, so a
    # payment made while a hold kept its intent open is never missed because the adapter's default window has passed
    def find(self, key: str, since: datetime | None = None) -> dict | None: ...
    def for_copy(self, copy: str, since: datetime | None = None) -> list[dict]: ...
    def cancel(self, outside: str) -> dict: ...
    def take_events(self) -> list[dict]: ...      # events no webhook has brought yet, each handed over once


class SimulatedProcessor:
    """A stand-in processor shared by every copy, as the real account would be. Every result says simulated. The card decides
    the first outcome; settle() moves a payment later (a processing payment finishing, a second attempt on a failed one, a
    return); lose_next_reply makes the next request reach it and its answer get lost. Each change queues a webhook in outbox.
    Given a path, its payments, keys and counts are kept in that file, so a restart finds them as a real processor would."""
    simulated = True
    CARDS = {"card_ok": "succeeded", "card_declined": "failed", "card_pending": "processing"}

    def __init__(self, path: str | None = None):
        self.path = path
        saved = {}
        if path and os.path.exists(path):
            with open(path) as f:
                saved = json.load(f)
        self.payments: dict[str, dict] = saved.get("payments", {})
        self.by_key: dict[str, str] = {p["key"]: pid for pid, p in self.payments.items()}
        self.outbox: list[dict] = []
        self.lose_next_reply = False
        self._n, self._e = saved.get("n", 0), saved.get("e", 0)

    def _save(self) -> None:
        if self.path:
            with open(self.path + ".tmp", "w") as f:
                json.dump({"payments": self.payments, "n": self._n, "e": self._e}, f)
            os.replace(self.path + ".tmp", self.path)           # whole or not at all

    def create(self, key: str, cents: int, card: str, metadata: dict, now: datetime) -> dict:
        if key not in self.by_key:              # the same key always gets the same payment back, never a second one
            self._n += 1
            pid = f"sim_pay_{self._n:05d}"
            self.payments[pid] = {"id": pid, "key": key, "cents": cents, "status": self.CARDS.get(card, "failed"), "metadata": dict(metadata),
                                  "at": now.isoformat(), "simulated": True}
            self.by_key[key] = pid
            self._event(pid)
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise ProcessorTimeout(key)
        return dict(self.payments[self.by_key[key]])

    def settle(self, outside: str, status: str, now: datetime) -> None:
        self.payments[outside].update(status=status, at=now.isoformat())
        self._event(outside)

    def _event(self, outside: str) -> None:
        self._e += 1
        p = self.payments[outside]
        self.outbox.append({"id": f"sim_evt_{self._e:05d}", "type": f"payment.{p['status']}", "payment": outside,
                            "metadata": dict(p["metadata"]), "simulated": True})
        self._save()

    def retrieve(self, outside: str) -> dict:
        return dict(self.payments[outside])

    def take_events(self) -> list[dict]:
        out, self.outbox = self.outbox, []
        return out

    def find(self, key: str, since: datetime | None = None) -> dict | None:
        return dict(self.payments[self.by_key[key]]) if key in self.by_key else None

    def for_copy(self, copy: str, since: datetime | None = None) -> list[dict]:
        return [dict(p) for p in self.payments.values() if p["metadata"].get("copy") == copy]

    def cancel(self, outside: str) -> dict:
        if self.payments[outside]["status"] == "processing":
            self.settle(outside, "canceled", datetime.fromisoformat(self.payments[outside]["at"]))
        return dict(self.payments[outside])


def _settle(led: Ledger, intent: Intent, p: dict, now: datetime) -> None:
    """Apply the processor's own record of the payment to the intent, once, and only forwards: a late failure never undoes a
    success, a repeat changes nothing, a return undoes what the payment paid."""
    to = PROCESSOR_STATES.get(p["status"])
    if to is None or to == intent.state or to not in INTENT_MOVES[intent.state]:
        return
    payload = {"intent": intent.id, "state": to, "outside": p["id"], "simulated": bool(p.get("simulated"))}
    if to == "succeeded":
        payload["payment"] = led.receive_payment(intent.tenant, intent.cents, datetime.fromisoformat(p["at"]), now,
                                                 ref={"processor_payment": p["id"], "copy": intent.copy, "intent": intent.id,
                                                      "simulated": bool(p.get("simulated"))})
    elif to == "failed":
        led.decline(now, intent.tenant, intent.cents)
    elif to == "returned" and intent.payment:
        try:
            led.return_payment(now, intent.payment)
        except Refused as e:
            # billing cannot apply it: the intent stays as it was, so every reconciliation reports it until the books are corrected
            if intent.payment not in led.books.pending_returns:
                led.emit("payment.return_pending", now, {"payment": intent.payment}, f"return-pending:{intent.payment}", source="processor")
            _task(led, "returned payment needs staff", intent.tenant, f"return:{intent.id}", str(e), local(now).date(), now)
            return
    # keyed by the move, not just where it ends: a payment that turns up after its intent was closed and is then cancelled closes it twice
    led.emit("intent.updated", now, payload, f"intent:{intent.id}:{intent.state}:{to}", source="processor")
    recheck(led, intent.tenant, now)
    if to == "returned" and intent.payment and (led.books.payments[intent.payment].applications or any(
            u.lot == intent.payment for u in led.books.credit_uses.get(intent.tenant, []))):
        covered = any(not t.acknowledged and t.tenant == intent.tenant and
                      f"returned:{intent.payment}" in t.ref for t in state(led.books).tasks.values())
        if not covered:
            _task(led, "returned payment needs staff", intent.tenant, f"return:{intent.id}",
                  "A payment came back after paying charges; those charges are owed again.", local(now).date(), now)


def pay(store: Store, stream: str, processor: Processor, tenant: str, cents: int, card: str, now: datetime) -> Intent:
    """A payment through the processor. The intent is written before the request goes out, its key is fixed then, and while an
    earlier payment's outcome is unknown no new one starts: that one is checked with the processor, never paid again."""
    if not isinstance(cents, int) or cents <= 0:
        raise Refused("a payment must be more than zero")
    with store.write():
        books = store.books(stream)
        if tenant not in books.tenants:
            raise Refused("no such tenant")
        if unsettled(books, tenant):
            raise Refused("an earlier payment's outcome is not known yet; it is checked with the processor, never paid again")
        led = Ledger(books)
        iid = open_intent(led, stream, processor, tenant, cents, now)
        store.append(stream, led.pending)
    return send(store, stream, processor, iid, card, now)


def open_intent(led: Ledger, stream: str, processor: Processor, tenant: str, cents: int, now: datetime) -> str:
    """Write a payment's intent, before any request goes out; its key is fixed here. Call it inside a write transaction (renting
    writes it together with the rental's own payment record). Returns the intent's id."""
    books = led.books
    iid = books.next_id("PI")
    # the copy's mark keeps keys apart when a reset copy, under the same name, starts its counts again
    nonce = state(books).nonce or uuid.uuid4().hex[:12]
    led.emit("intent.created", now, {"intent": iid, "tenant": tenant, "cents": cents, "key": f"{stream}:{iid}:{nonce}", "nonce": nonce,
                                     "copy": stream, "simulated": processor.simulated}, f"intent:{iid}", source="processor")
    return iid


def send(store: Store, stream: str, processor: Processor, intent_id: str, card: str, now: datetime) -> Intent:
    """Ask the processor for the intent's payment. A retry sends the same key, so the processor hands back the same payment."""
    intent = state(store.books(stream)).intents[intent_id]
    if intent.state not in UNSETTLED:
        return intent
    try:
        p = processor.create(intent.key, intent.cents, card, {"copy": stream, "intent": intent_id, "nonce": state(store.books(stream)).nonce}, now)
    except ProcessorTimeout:
        return intent           # unknown: a webhook or the reconciliation settles it
    except ProcessorRefused as e:
        with store.write():
            books = store.books(stream)
            led = Ledger(books)
            led.emit("intent.updated", now, {"intent": intent_id, "state": "closed", "why": f"the processor refused the request: {e}"},
                     f"intent:{intent_id}:closed", source="processor")
            store.append(stream, led.pending)
            return state(books).intents[intent_id]
    with store.write():
        books = store.books(stream)
        led = Ledger(books)
        _settle(led, state(books).intents[intent_id], p, now)
        store.append(stream, led.pending)
        return state(books).intents[intent_id]


def webhook(store: Store, processor: Processor, event: dict, now: datetime) -> str:
    """A processor event: routed to its copy, kept once per event id, and applied from the processor's own record of the
    payment, never from the event's type, so duplicates and events out of order change nothing."""
    meta = event.get("metadata") or {}
    stream, iid = meta.get("copy"), meta.get("intent")
    p = processor.retrieve(event["payment"])
    with store.write():
        books = store.books(stream)
        st = state(books)
        if event["id"] in st.webhooks:
            return "duplicate"
        if iid not in st.intents or st.intents[iid].key != p.get("key"):
            return "unknown"            # a copy that was reset (its name and intent ids come back; its keys do not)
        led = Ledger(books)
        led.emit("webhook.received", now, {"event": event["id"], "type": event["type"], "payment": event["payment"], "intent": iid,
                                           "status": p["status"]}, f"webhook:{event['id']}", source="processor")
        _settle(led, st.intents[iid], p, now)
        store.append(stream, led.pending)
    return "applied"


def reconcile(store: Store, stream: str, processor: Processor, now: datetime) -> dict:
    """The copy's daily check against the processor: each open intent is looked up by its key and settled (one the processor
    never received is closed), each posted payment is compared with the processor's record (a missed return is applied), and
    any processor payment for this copy with no intent here is listed."""
    st = state(store.books(stream))
    # open intents, posted payments, and intents closed for want of a payment (one may still have turned up)
    answers = {i.id: (processor.find(i.key, since=i.created) if i.outside is None else processor.retrieve(i.outside))
               for i in st.intents.values() if i.state in UNSETTLED + ("succeeded",) or (i.state == "closed" and i.outside is None)}
    theirs = [p for p in processor.for_copy(stream, since=oldest_waiting(st)) if not _earlier_copy(p, stream, st.nonce)]
    with store.write():
        books = store.books(stream)
        st = state(books)
        led = Ledger(books)
        settled = []
        for iid, p in answers.items():
            i = st.intents[iid]
            before = i.state
            if p is None:
                if i.state in UNSETTLED and now - i.created >= IN_FLIGHT:
                    led.emit("intent.updated", now, {"intent": iid, "state": "closed", "why": "the processor has no payment for it"},
                             f"intent:{iid}:closed", source="processor")
            else:
                _settle(led, i, p, now)
            if i.state != before:
                settled.append(iid)
        ours = {i.outside for i in st.intents.values() if i.outside}
        mismatches = [f"the processor has payment {p['id']} for this copy and no intent here" for p in theirs if p["id"] not in ours]
        mismatches += [f"payment {i.payment} is posted and the processor says {answers[i.id]['status']}" for i in st.intents.values()
                       if i.state == "succeeded" and i.id in answers and answers[i.id] and answers[i.id]["status"] != "succeeded"]
        result = {"day": local(now).date().isoformat(), "copy": stream, "checked": len(answers), "settled": settled, "mismatches": mismatches}
        last = st.reconciled[-1] if st.reconciled else None
        # recorded once a day, and again whenever something settled or the differences changed; a pass that finds the same writes nothing
        if settled or last is None or last["day"] != result["day"] or last["mismatches"] != mismatches:
            led.emit("reconcile.ran", now, result, f"reconcile:{now.isoformat()}:{len(st.reconciled)}", source="processor")
        store.append(stream, led.pending)
    return result


def _earlier_copy(p: dict, stream: str, nonce: str | None) -> bool:
    """A payment made by an earlier copy under this name, before a reset: it carries a mark other than this copy's, and its key is
    the one pay() built with that mark. Only those are left out of the check; a payment with no mark, or a mark its key does not
    carry, is listed as a difference."""
    mark = p["metadata"].get("nonce")
    key = p.get("key") or ""
    return bool(mark) and mark != nonce and key.startswith(f"{stream}:") and key.endswith(f":{mark}")


def oldest_waiting(st: State) -> datetime | None:
    """When the oldest intent still waiting for the processor was made, or None: how far back a listing has to read."""
    return min((i.created for i in st.intents.values() if i.state in UNSETTLED), default=None)


def close_copy(processor: Processor, stream: str, since: datetime | None = None) -> list[str]:
    """Before a visitor copy is reset: its payments still processing are cancelled, so none is left behind at the processor."""
    return [processor.cancel(p["id"])["id"] for p in processor.for_copy(stream, since=since) if p["status"] == "processing"]
