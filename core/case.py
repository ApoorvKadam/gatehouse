"""One tenant message, start to end: who wrote it, what they claim, what the records say, what the facility does, and the reply.

The same flow serves the live page and the graded runs; an Arm says who reads (keyword rules, a model, or the labels) and who
chooses (the fixed table or a model). Every action goes through the gate, claim by claim in a fixed order (oldest charge
first), and the reply is written by code from what the gate recorded. Model off: every message goes to staff."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import agent, plan_case, plan_reading, table
from .billing import Ledger
from .books import Books, LATE_FEE, fold, local
from .evidence import Claim, Finding, charge_list, check
from .gate import Command, Decision, evaluate, evaluate_ask, execute, staff_rule, staff_rule_for_message, waiver_rule
from .reader import amounts as read_amounts
from .reply import write
from .store import Store

# flags that send the whole message to staff, with the reason Today shows; a message with words aimed at the system goes too,
# because a waiver it led to could be the instruction obeyed
PERSON_FLAGS = {"asks_for_person": "the tenant asked for a person", "legal_or_hostile": "the message threatens legal action or is abusive",
                "instructions_to_system": "the message has words aimed at the system, so nothing changes before staff see it"}
OFF = "Tilden is off on this copy, so the message waits for staff"


@dataclass
class Arm:
    name: str
    reader: str = "model"                 # model, keywords, labels
    chooser: str = "model"                # model, table
    read_model: str = "claude-haiku-5-5"
    read_effort: str = "low"
    op_model: str = "claude-haiku-5-5"
    op_effort: str = "low"


FIXED = Arm("fixed workflow", reader="keywords", chooser="table")
READ_ONLY = Arm("model reads, table decides", chooser="table")
OPERATOR = Arm("operator")


@dataclass
class Outcome:
    claim: Claim
    finding: Finding
    action: str
    chosen_by: str                        # code, table, model, staff rule
    decision: Decision | None = None


@dataclass
class Result:
    case: str
    tenant: str | None
    claims: list[Claim] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    asked: list[Decision] = field(default_factory=list)
    handed: str | None = None             # why the case went to staff, if it did
    reply: str = ""
    calls: list[agent.Call] = field(default_factory=list)
    repeated: bool = False
    plan: list[dict] = field(default_factory=list)     # steps taken on a reply about paying in parts, through the gate
    plan_read: dict | None = None       # what that reply was read as, in plain words

    @property
    def cents(self) -> float:
        return sum(c.cents for c in self.calls)


def identify(books: Books, phone: str | None) -> str | None:
    """Exactly one tenant with an active lease on this number, or nobody (a demo identity, not authentication)."""
    hits = {t.id for t in books.tenants.values() if t.phone == phone and any(l.tenant == t.id and not l.ended for l in books.leases.values())}
    return hits.pop() if len(hits) == 1 else None


def history(books: Books, tenant: str, now: datetime) -> dict:
    fees = [c for c in books.tenant_charges(tenant) if c.kind == LATE_FEE]
    return {"late_fees_last_12_months": sum(1 for c in fees if (now.date() - c.due).days < 365),
            "waivers_last_12_months": books.waived_within(tenant, now),
            "months_on_time_of_last_6": sum(books.on_time(tenant, m) for m in _last_months(now, 6))}


def _last_months(now: datetime, n: int) -> list[str]:
    y, m = local(now).year, local(now).month
    out = []
    for _ in range(n):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        out.append(f"{y:04d}-{m:02d}")
    return out


def handle(store: Store, stream: str, case: str, phone: str | None, text: str, arm: Arm, now: datetime, *, client=None,
           model_off: bool = False, labelled: tuple[list[Claim], list[str]] | None = None) -> Result:
    done = next((e for e in store.stream_events(stream) if e.key == f"case:{case}"), None) if stream != "base" else None
    if done is not None:        # a retried case gets its recorded result back: no new model call, nothing written twice
        p = done.payload
        return Result(case, p["tenant"], flags=p["flags"], dropped=p["dropped"], handed=p["handed"], reply=p["reply"], repeated=True)
    books = store.books(stream)
    tenant = identify(books, phone)
    res = Result(case, tenant)
    budget = agent.Budget()
    needs_model = arm.reader == "model" or arm.chooser == "model"
    if tenant is None:
        res.handed = "the number does not match exactly one tenant, so nothing on any account changes"
    elif needs_model and (model_off or client is None):
        res.handed = OFF
    if res.handed:
        res.reply = write(books, None, res, now)
        _record(store, stream, case, phone, text, res, now)
        return res
    # read
    if arm.reader == "labels" and labelled is not None:
        res.claims, res.flags = labelled
    elif arm.reader == "keywords":
        res.claims, res.flags = table.keyword_read(books, tenant, text, now)
    else:
        claims, flags, dropped, call = agent.understand(client, arm.read_model, arm.read_effort, text, charge_list(books, tenant, now),
                                                        local(now).date(), budget)
        res.calls.append(call)
        if claims is None:
            res.handed = f"the message could not be read ({call.outcome})"
            res.reply = write(books, tenant, res, now)
            _record(store, stream, case, phone, text, res, now)
            return res
        res.claims, res.flags, res.dropped = claims, flags, dropped
    # a reply about paying in parts: no claim about a charge, nothing that sends it to staff, and the tenant past due or with plans
    # (a claim of kind "other" naming no charge, such as "I agree to Plan 2", is not about a charge either)
    if (arm.reader == "model" and all(c.kind == "other" and not c.charge for c in res.claims) and not res.dropped
            and not any(fl in PERSON_FLAGS for fl in res.flags)
            and "other_account" not in res.flags and not staff_rule_for_message(books, [], tuple(res.flags), 1)
            and plan_case.applies(books, tenant, now)):
        p = plan_case.context(books, tenant, now)
        r, call = plan_reading.read(client, arm.read_model, arm.read_effort, text, p.ctx, budget)
        res.calls.append(call)
        res.plan_read = plan_reading.said(r)
        acts = plan_reading.decide(r, p.ctx)
        res.plan, res.handed = plan_case.act(store, stream, case, tenant, text, r, acts, p, now)
        res.reply = plan_case.reply(store.books(stream), tenant, acts, res.handed, now, res.plan, r)
        _record(store, stream, case, phone, text, res, now)
        return res
    if not res.claims or res.dropped:
        res.handed = "a claim could not be confirmed in the tenant's words" if res.dropped else "no claim about a charge was found"
    # what the records say, and what the tenant literally asked to have waived (checked, never executed)
    findings = [check(books, tenant, c, now) for c in res.claims]
    asks = [c.charge for c in res.claims if c.kind == "wants_waiver" and c.charge and books.charges[c.charge].kind == LATE_FEE]
    if len(asks) > 1:
        res.asked = evaluate_ask(books, tenant, asks, now)
    order = sorted(range(len(findings)), key=lambda i: (books.charges[findings[i].claim.charge].seq if findings[i].claim.charge in books.charges else 10 ** 9, i))
    # choose
    actions: dict[int, tuple[str, str]] = {}
    pending = []
    person = next((why for fl, why in PERSON_FLAGS.items() if fl in res.flags), None)
    if person:
        res.handed = person
    for i in order:
        f = findings[i]
        if person:
            actions[i] = ("hand_to_staff", "staff rule")
        elif len(f.choices) == 1 and not res.flags:
            actions[i] = (f.choices[0].action, "code")
        elif not f.choices:
            actions[i] = ("hand_to_staff", "code")
        else:
            pending.append(i)
    if pending and arm.chooser == "table":
        spent = 0
        for i in pending:
            a = table.decide(books, tenant, findings[i], res.flags, now, spent)
            spent += a == "waive_fee"
            actions[i] = (a, "table")
    elif pending:
        # the owner's rule is shown, never used to filter: each waiver option says what the gate would answer
        for f in findings:
            for o in f.options:
                if o.action == "waive_fee" and "rule" not in o.label:
                    d = evaluate(books, Command("preview", "waive_fee", tenant, "operator", o.charge, raised=True), now)
                    o.label += " (the owner's rule allows it)" if d.allowed else f" (the owner's rule does not allow it: {d.check})"
        cf = agent.case_file(findings, res.flags, (waiver_rule(books) or {}).get("sentence"), history(books, tenant, now))
        picked, call = agent.operate_claude(client, arm.op_model, arm.op_effort, cf, budget)
        res.calls.append(call)
        for i in pending:
            a = (picked or {}).get(i)
            valid = {o.action for o in findings[i].options}
            actions[i] = (a, "model") if a in valid else ("hand_to_staff", "code")
    # act, through the gate: corrections of our own records first, then waivers, then the rest; oldest charge first within each
    source = "fixed_workflow" if arm.chooser == "table" and arm.reader == "keywords" else "operator"
    # the units the message is about: those its claims point at, and those whose rent it names in dollars ("$90 and $155")
    said = {cents for cents, _ in read_amounts(text)}
    leases = {books.charges[c.charge].lease for c in res.claims if c.charge in books.charges}
    leases |= {ch.lease for ch in books.tenant_charges(tenant) if ch.kind == "rent" and ch.cents in said}
    units = len(leases) or 1
    # the owner's staff rule covers the whole message: if any claim, flag or the units match, nothing is done before staff see it
    hit = staff_rule_for_message(books, [f.claim.kind for f in findings], tuple(res.flags), units)
    if hit:
        actions = {i: ("hand_to_staff", "owner rule") for i in order}
        res.handed = res.handed or hit[0]
    rank = {"reverse_duplicate": 0, "correct_fee_posted_in_error": 0, "waive_fee": 1}
    done = set()
    for i in sorted(order, key=lambda i: (rank.get(actions[i][0], 2), order.index(i))):
        action, by = actions[i]
        f = findings[i]
        target = next((o.charge for o in f.options if o.action == action), f.claim.charge)
        # two claims about one pair of copies ("$20 on the 6th and another on the 10th") are one reversal, not a second refused one
        if action != "hand_to_staff" and (action, target) in done:
            continue
        done.add((action, target))
        cmd = Command(f"{case}:{i}:{action}", action, tenant, source, target, case, quote=f.claim.quote,
                      raised=f.claim.kind in ("wants_waiver", "paid_on_time", "charged_twice", "never_agreed", "challenges_record"),
                      role="office_manager" if action == "hand_to_staff" else None, reason=hit[0] if hit else None,
                      claim_kind=f.claim.kind, flags=tuple(res.flags), units=units)
        left = None if staff_rule is None or action == "hand_to_staff" else staff_rule(store.books(stream), cmd)
        if left:        # the owner's rule leaves this action to staff: it becomes a hand-over, with the rule as its reason
            action, by = "hand_to_staff", "owner rule"
            cmd = Command(f"{case}:{i}:owner-rule-to-staff", action, tenant, source, target, case, role="office_manager", reason=left[0],
                          claim_kind=f.claim.kind, flags=tuple(res.flags), units=units)
            res.handed = res.handed or left[0]
        dec = execute(store, stream, cmd, now)
        if not dec.allowed and action != "hand_to_staff":
            execute(store, stream, Command(f"{case}:{i}:refused-to-staff", "hand_to_staff", tenant, source, target, case,
                                           role="office_manager", reason=dec.check), now)
        res.outcomes.append(Outcome(f.claim, f, action, by, dec))
    if any(o.action == "hand_to_staff" for o in res.outcomes) and not res.handed:
        res.handed = "a person is needed for part of this message"
    res.reply = write(store.books(stream), tenant, res, now)
    _record(store, stream, case, phone, text, res, now)
    return res


def _record(store: Store, stream: str, case: str, phone: str | None, text: str, res: Result, now: datetime) -> None:
    """The case's own record: the message, what was read, who it went to, the reply, and every model call with its cost."""
    led = Ledger(store.books(stream))
    led.emit("case.closed" if not res.handed else "case.handed_over", now,
             {"case": case, "phone": phone, "text": text, "tenant": res.tenant, "flags": res.flags, "dropped": res.dropped,
              "claims": [c.__dict__ for c in res.claims], "plan_read": res.plan_read, "handed": res.handed, "reply": res.reply,
              # what the records showed for each claim and what was done, so the case can be shown again after a reload
              "outcomes": [{"says": o.finding.says, "action": o.action, "by": o.chosen_by, "allowed": bool(o.decision and o.decision.allowed), "charge": o.claim.charge,
                            "rule": o.decision.rule if o.decision and o.chosen_by == "owner rule" else None,
                            "check": o.decision.check if o.decision else None} for o in res.outcomes] + res.plan,
              "asked": [{"allowed": d.allowed, "check": d.check} for d in res.asked],
              "calls": [c.__dict__ for c in res.calls]}, f"case:{case}", source="operator")
    with store.write():
        if f"case:{case}" not in {e.key for e in store.stream_events(stream)}:
            store.append(stream, led.pending, now=None)
