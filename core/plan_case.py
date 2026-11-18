"""A tenant's reply about paying in parts, on the facility's own books: the context code builds, what is done through the gate,
and the reply code writes. Used only when the charge reader found no claim about a charge and no flag that sends the message to
staff, and the tenant is past due or has plans offered or a plan open."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from . import delinquency as col
from . import plan_reading as pr
from .books import Books, local
from .evidence import day, money
from .gate import Command, execute
from .store import Store


@dataclass
class Plans:
    ctx: pr.Context
    plan: object | None = None        # the open proposal whose plans were put to the tenant, if any
    labels: dict = field(default_factory=dict)        # "Plan 1" -> offer version


def context(books: Books, tenant: str, now: datetime) -> Plans:
    """What code knows: the balance past due, how long they have rented, the owner's policy, the plans put to them (named Plan 1,
    Plan 2 ... in the order they were offered), whether a plan is open and whether it waits for staff. Eligibility is the plan
    core's own check."""
    st = col.state(books)
    mine = sorted((p for p in st.plans.values() if p.tenant == tenant), key=lambda p: (p.history[0][1], int("".join(c for c in p.id if c.isdigit()) or 0)))
    open_plan = next((p for p in reversed(mine) if p.state == "offered"), None)
    # labels never repeat for a tenant: a later proposal's plans carry on the count, so an old "Plan 1" never names a new offer
    first = 1 + sum(len(p.offered) for p in mine[:mine.index(open_plan)]) if open_plan is not None else 1
    active = next((p for p in reversed(mine) if p.state in col.OPEN_PLAN), None)
    offered = list(open_plan.offered) if open_plan else []
    starts = [x.start for x in books.leases.values() if x.tenant == tenant]
    today = local(now).date()
    ctx = pr.Context(today, sum(c.open_cents for c in col.owed(books, tenant, now)), (today - min(starts)).days if starts else 0,
                     st.policy or {}, [(f"Plan {k}", o["upfront"], o["instalments"]) for k, o in enumerate(offered, first)],
                     has_plan=active is not None, plan_with_staff=bool(active and active.state in col.WITH_STAFF),
                     blocked=col.ineligible(books, tenant, now) if st.policy else "the owner has not set a plan policy",
                     offer_words={f"Plan {k}": col.words(o) for k, o in enumerate(offered, first)})
    return Plans(ctx, open_plan, {f"Plan {k}": o["version"] for k, o in enumerate(offered, first)})


def applies(books: Books, tenant: str, now: datetime) -> bool:
    """Past due, or plans offered, or a plan open: only then can a reply be about paying in parts."""
    p = context(books, tenant, now)
    return p.ctx.balance_cents > 0 or bool(p.ctx.offered) or p.ctx.has_plan


def _plans_for(store: Store, stream: str, case: str, tenant: str, now: datetime, terms: list[tuple[int, int]] | None = None) -> list[dict]:
    """Propose (code computes every plan the policy allows), then put plans to the tenant through the gate: the ones named in
    terms as (upfront, instalments), or every plan code computed. Returns what was done as outcome records, one for the plans."""
    d = execute(store, stream, Command(f"{case}:plan:propose", "propose_plan", tenant, "operator", case=case), now)
    if not d.allowed:
        return [{"says": d.check, "action": "propose_plan", "by": "code", "allowed": False, "charge": None, "check": d.check}]
    books = store.books(stream)
    plan = next(p for p in sorted(col.state(books).plans.values(), key=lambda p: p.history[0][1], reverse=True) if p.tenant == tenant and p.state == "proposed")
    refused = []
    for k, (up, n) in enumerate(terms or [(o["upfront"], o["instalments"]) for o in plan.offers], 1):
        d = execute(store, stream, Command(f"{case}:plan:offer:{k}", "offer_plan", tenant, "operator", case=case, plan=plan.id,
                                           form={"instalments": n, "upfront_cents": up}), now)
        if not d.allowed:
            refused.append({"says": d.check, "action": "offer_plan", "by": "code", "allowed": False, "charge": None, "check": d.check})
    shown = context(store.books(stream), tenant, now).ctx.offer_words
    out = [{"says": f"{len(shown)} plan{'s' if len(shown) != 1 else ''} offered ({', '.join(shown)}), each worked out by code under the owner's policy.",
            "action": "offer_plan", "by": "code", "allowed": True,
            "charge": None, "check": None}] if shown else []
    return out + refused


def act(store: Store, stream: str, case: str, tenant: str, text: str, reading: pr.Reading | None, acts: list[str], plans: Plans,
        now: datetime) -> tuple[list[dict], str | None]:
    """Do what was decided, each step through the gate. Returns the outcome records and why it went to staff, if it did."""
    out, handed = [], None
    for a in acts:
        name, _, rest = a.partition(":")
        if name in ("offer_plans", "refuse_counter"):
            out += _plans_for(store, stream, case, tenant, now)
        elif name == "offer_plan":
            up, n = (int(x) for x in rest.split("x"))
            out += _plans_for(store, stream, case, tenant, now, [(up, n)])
        elif name == "accept_plan":
            d = execute(store, stream, Command(f"{case}:plan:accept", "accept_plan", tenant, "operator", case=case, plan=plans.plan.id,
                                               offer=plans.labels[rest], quote=text), now)
            out.append({"says": f"The tenant agreed to {rest} in their own words." if d.allowed else d.check, "action": "accept_plan", "by": "code",
                        "allowed": d.allowed, "charge": None, "check": None if d.allowed else d.check})
        elif name == "record_promise":
            cents, by = rest.split(":")
            d = execute(store, stream, Command(f"{case}:plan:promise", "record_promise", tenant, "operator", case=case, quote=text,
                                               form={"cents": int(cents), "by": by}), now)
            out.append({"says": f"The tenant promised {money(int(cents))} by {day(date.fromisoformat(by))}; staff are reminded the day after if it has not arrived."
                        if d.allowed else d.check, "action": "record_promise", "by": "code", "allowed": d.allowed, "charge": None,
                        "check": None if d.allowed else d.check})
        elif name == "hand_to_staff":
            flagged = _flag_reason(reading)
            handed = flagged or ("the tenant's plan waits for staff, who review it" if plans.ctx.plan_with_staff else _why_staff(reading, plans))
    if any(not o["allowed"] for o in out) and not handed:
        handed = next(o["check"] for o in out if not o["allowed"])
    return out, handed


def _flag_reason(r: pr.Reading | None) -> str | None:
    """The reason a flag in the reply sends it to staff, if one does."""
    return next((STAFF_WORDS[f] for f in (r.flags if r else []) if f in STAFF_WORDS), None)


def _why_staff(r: pr.Reading | None, plans: Plans) -> str:
    if r is None:
        return "the reply could not be read, so staff answer it"
    return _flag_reason(r) or plans.ctx.why_not_eligible() or "a person is needed for this reply"


STAFF_WORDS = {"someone_else": "it is about someone else's account", "access": "it asks about gate access or a lock",
             "legal": "it mentions legal action", "asks_for_person": "the tenant asked for a person", "waive_rent": "it asks for rent to be waived",
             "mark_paid": "it asks for the balance to be marked paid", "change_terms": "it asks to change an agreed plan",
             "dispute": "it disputes a charge", "claims_permission": "it says someone already approved it",
             "instructions_to_system": "it has words aimed at the system"}


def reply(books: Books, tenant: str, acts: list[str], handed: str | None, now: datetime, outcomes: list[dict] | None = None) -> str:
    """The reply, written by code from what was recorded: the plans put to the tenant, the plan agreed, the promise noted, or a
    plain question when nothing could be taken as agreement."""
    name = books.tenants[tenant].name.split()[0]
    first = acts[0].split(":")[0] if acts else ""
    promised = any(o["action"] == "record_promise" and o["allowed"] for o in outcomes or [])     # this reply's own promise, as the gate recorded it
    if handed and first == "record_promise" and promised:
        _, cents, by = acts[0].split(":")
        return (f"Hi {name}, noted: {money(int(cents))} by {day(date.fromisoformat(by))}. A member of our team will also review your plan "
                "with you.")
    if handed:
        return f"Hi {name}, thanks for your message. A member of our team will get back to you about it."
    p = context(books, tenant, now)
    if first in ("offer_plans", "offer_plan", "refuse_counter") and p.plan is not None:
        total = p.plan.offered[-1]["total"]           # the recorded offers' own total, so the plans and the sum always agree
        lines = [f"Hi {name}, you can pay the {money(total)} past due in parts."]
        if first == "refuse_counter":
            lines = [f"Hi {name}, the terms you suggested are outside what the facility can offer. These plans are possible for the {money(total)} past due:"]
        for label, _, _ in p.ctx.offered:
            lines.append(f"{label}: {p.ctx.offer_words[label]}.")
        lines.append(f"Reply with the plan you choose, for example \"I agree to {p.ctx.offered[0][0]}\".")
        return "\n".join(lines)
    if first == "accept_plan":
        plan = next((x for x in col.state(books).plans.values() if x.tenant == tenant and x.terms and x.state in col.OPEN_PLAN), None)
        if plan:
            return f"Hi {name}, your plan is agreed: {col.words(plan.terms)}. The plan starts once the first payment is confirmed."
    if first == "record_promise":
        _, cents, by = acts[0].split(":")
        return f"Hi {name}, noted: {money(int(cents))} by {day(date.fromisoformat(by))}. We will check in the day after if it has not arrived."
    if p.ctx.offered:
        return f"Hi {name}, to set up a plan, reply with the plan you choose: " + ", ".join(label for label, _, _ in p.ctx.offered) + "."
    return f"Hi {name}, to note a promise to pay, reply with the amount and the day you will pay."
