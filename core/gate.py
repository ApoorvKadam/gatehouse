"""The gate: the one way anything may change a tenant's account.

`evaluate` answers "would this be allowed?" without writing (used for what a tenant literally asks for). `execute` is the only
writer: inside one write transaction it folds the current state, runs the same checks in the same order, and either records
the action with its money, or records the refusal with the check that refused it. A command id makes a retry return the first
decision. The checks, in order: the action exists and is in scope; safety (fixed in code); the owner's rules; disputes and
holds."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from .billing import Ledger
from .books import FEE_KINDS, LATE_FEE, RENT, Books, cutoff, fold, local
from .store import Store

ACTIONS = ("explain_charge", "reverse_duplicate", "correct_fee_posted_in_error", "open_dispute", "waive_fee", "hand_to_staff")
# what staff decide on an open dispute; never offered to the agent
STAFF_ACTIONS = ("remove_disputed_fee", "keep_under_review")
MONEY = ("reverse_duplicate", "correct_fee_posted_in_error", "waive_fee")
STAFF_ROLES = ("front_desk", "office_manager", "manager")


@dataclass
class Command:
    id: str                         # stable: the same id always gets the same decision back
    action: str
    tenant: str                     # the tenant the case identified
    source: str                     # operator, fixed_workflow, staff:<role>
    charge: str | None = None
    case: str | None = None
    quote: str | None = None        # the tenant's own words, for a dispute
    raised: bool = False            # the tenant raised this charge in their message
    role: str | None = None         # the staff role a hand-over goes to
    reason: str | None = None


@dataclass
class Decision:
    allowed: bool
    action: str
    check: str                      # what decided, in words
    rule: str | None = None         # the owner's sentence when an owner rule decided
    hold: bool = False              # a dispute that holds collection on its charge
    command: str | None = None
    events: list[int] = field(default_factory=list)
    repeated: bool = False
    charge: str | None = None       # the charge a tenant's ask named, for asks checked without acting


def _month_of(charge) -> str | None:
    if charge.obligation and charge.obligation.count(":") == 2:
        return charge.obligation.split(":")[2]
    return None


def _prev_months(month: str, n: int) -> list[str]:
    y, m = map(int, month.split("-"))
    out = []
    for _ in range(n):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        out.append(f"{y:04d}-{m:02d}")
    return out


def waiver_rule(books: Books) -> dict | None:
    return books.rules.get("late_fee_waiver")


def waiver_check(books: Books, tenant: str, charge, now: datetime, already: int | None = None) -> tuple[bool, str, str | None]:
    """The owner's waiver rule, read from its recorded typed form. Returns (allowed, check, the owner's sentence)."""
    rule = waiver_rule(books)
    if rule is None:
        return False, "no owner rule allows waivers", None
    t, sentence = rule["typed"], rule["sentence"]
    if charge.kind != t["fee_kind"]:
        return False, "the owner's rule waives late fees only", sentence
    if charge.cents > t["cap_cents"]:
        return False, "over the owner's cap for one waiver", sentence
    used = books.waived_within(tenant, now, t["within_days"]) if already is None else already
    if used >= t["per_tenant"]:
        return False, "the tenant already had a waiver in the last 12 months", sentence
    month = _month_of(charge) or f"{charge.due.year:04d}-{charge.due.month:02d}"
    if not all(books.on_time(tenant, m) for m in _prev_months(month, t["on_time_months"])):
        return False, "the tenant was not on time for each of the six months before this fee", sentence
    return True, "the owner's waiver rule allows it", sentence


def evaluate(books: Books, cmd: Command, now: datetime) -> Decision:
    """The checks, in order, against `books`. Writes nothing."""
    d = lambda ok, check, rule=None, hold=False: Decision(ok, cmd.action, check, rule, hold, cmd.id)
    staff = cmd.source.startswith("staff:")
    # 1. exists and in scope
    if cmd.action not in ACTIONS + STAFF_ACTIONS:
        return d(False, "not an action the system knows")
    if cmd.action in STAFF_ACTIONS and not staff:
        return d(False, "only staff decide a dispute")
    if cmd.action == "hand_to_staff":
        return d(True, f"handed to the {cmd.role or 'office_manager'}".replace("_", " "))
    if cmd.tenant not in books.tenants:
        return d(False, "no identified tenant, so nothing on any account changes")
    ch = books.charges.get(cmd.charge or "")
    if ch is None:
        return d(False, "no such charge")
    if ch.tenant != cmd.tenant:
        return d(False, "that charge is on another tenant's account")
    if cmd.action == "explain_charge":
        return d(True, "explained from the records")
    if ch.closed:
        return d(False, f"that charge was already {ch.closed}")
    lease = books.leases[ch.lease]
    if cmd.action in STAFF_ACTIONS:
        dispute = open_dispute_on(books, ch.id)
        if dispute is None:
            return d(False, "there is no open dispute on this charge")
        if cmd.action == "keep_under_review":
            if any(p.get("action") == "keep_under_review" and p.get("charge") == ch.id and p.get("allowed") and p.get("command") != cmd.id
                   for p in books.decisions.values()):
                return d(False, "the dispute is already kept under review")
            held = "collection stays paused" if books.held(ch.id) else "the charge stays due"
            return d(True, f"the dispute stays open and {held} until the review by {dispute.review_by:%b} {dispute.review_by.day}")
        # the authority for a correction is the schedule the tenant signed; a fee in it would be a waiver, which this is not
        if ch.kind not in FEE_KINDS or ch.kind in lease.schedule:
            return d(False, "the fee is in the tenant's signed schedule, so removing it would be a waiver, not a correction")
        return d(True, "the fee is not in the schedule the tenant signed, so it is removed as a correction and does not count as a waiver")
    if cmd.action in MONEY and lease.ended and not staff:
        return d(False, "the charge is on a lease that has ended; staff handle it")
    # 2. safety, fixed in code
    if cmd.action == "waive_fee" and ch.kind == RENT:
        return d(False, "rent is never waived")
    if cmd.action == "reverse_duplicate":
        twins = books.billed(ch.obligation) if ch.obligation else []
        if len(twins) < 2:
            return d(False, "there is no second charge for the same thing, so this is not a duplicate")
        if twins[0].id == ch.id:
            return d(False, "the first charge for this obligation stands; only the later copy is reversed")
    if cmd.action == "correct_fee_posted_in_error":
        month = _month_of(ch)
        if ch.kind != LATE_FEE or month is None:
            return d(False, "only a late fee for a named month can be corrected this way")
        due = sum(c.cents for c in books.live(f"rent:{ch.lease}:{month}"))
        if due == 0 or books.rent_paid_before(ch.lease, month, cutoff(month)) < due:
            return d(False, "the month's rent was not fully paid before the cutoff, so the fee was not posted in error")
    # 4 (before 3 for money on held charges): a held charge belongs to staff
    if cmd.action in MONEY and books.held(ch.id) and not staff:
        return d(False, "the charge is held under a dispute; staff decide it")
    # 3. the owner's rules
    if cmd.action == "waive_fee" and not staff:
        rule = waiver_rule(books)
        if rule and rule["typed"].get("only_when_raised") and not cmd.raised:
            return d(False, "the owner's rule offers a waiver only when the tenant raises the fee", rule["sentence"])
        ok, check, sentence = waiver_check(books, cmd.tenant, ch, now)
        if not ok:
            return d(False, check, sentence)
        return d(True, check, sentence)
    # 4. disputes and holds
    if cmd.action == "open_dispute":
        if not cmd.quote:
            return d(False, "a dispute needs the tenant's own words")
        if any(x.charge == ch.id and x.outcome is None for x in books.disputes.values()):
            return d(False, "a dispute on this charge is already open")
        if not staff and any(x.charge == ch.id and x.outcome is not None for x in books.disputes.values()):
            return d(False, "staff already decided a dispute on this charge; only staff reopen it, with new evidence")
        hold = ch.kind in FEE_KINDS and ch.kind not in lease.schedule
        return d(True, "dispute recorded and held: the fee is not in the tenant's signed schedule" if hold
                 else "dispute recorded for staff; the records back the charge, so collection is not held", hold=hold)
    return d(True, "the records support it")


def evaluate_ask(books: Books, tenant: str, charges: list[str], now: datetime) -> list[Decision]:
    """What a tenant literally asked to have waived, checked as a whole: the yearly limit counts across the ask."""
    rule = waiver_rule(books)
    used = books.waived_within(tenant, now, rule["typed"]["within_days"]) if rule else 0
    out = []
    for cid in charges:
        ch = books.charges.get(cid)
        if ch is None or ch.tenant != tenant:
            out.append(Decision(False, "waive_fee", "that charge is not on this account", charge=cid))
            continue
        if ch.kind == RENT:
            out.append(Decision(False, "waive_fee", "rent is never waived", charge=cid))
            continue
        ok, check, sentence = waiver_check(books, tenant, ch, now, already=used)
        out.append(Decision(ok, "waive_fee", check, sentence, charge=cid))
        used += ok
    return out


def execute(store: Store, stream: str, cmd: Command, now: datetime) -> Decision:
    """Check and record, in one write transaction. A command id already decided returns that decision and writes nothing."""
    with store.write():
        books = store.books(stream)
        if cmd.id in books.decisions:
            p = books.decisions[cmd.id]
            return Decision(p["allowed"], p["action"], p["check"], p.get("rule"), p.get("hold", False), cmd.id, list(p.get("events", [])), True)
        dec = evaluate(books, cmd, now)
        led = Ledger(books)
        if dec.allowed:
            if cmd.action == "reverse_duplicate":
                led.reverse(now, cmd.charge, "duplicate", cmd.source, command=cmd.id)
            elif cmd.action == "correct_fee_posted_in_error":
                led.reverse(now, cmd.charge, "posted in error", cmd.source, command=cmd.id)
            elif cmd.action == "waive_fee":
                led.waive(now, cmd.charge, dec.check, cmd.source, command=cmd.id)
            elif cmd.action == "open_dispute":
                led.open_dispute(now, cmd.charge, cmd.quote, dec.hold, cmd.source, command=cmd.id)
            elif cmd.action == "remove_disputed_fee":
                led.reverse(now, cmd.charge, "not in the signed schedule", cmd.source, command=cmd.id)
                led.decide_dispute(now, open_dispute_on(books, cmd.charge).id, "fee removed: not in the signed schedule",
                                   cmd.source.removeprefix("staff:"))
        record = {**{k: v for k, v in asdict(cmd).items() if v is not None}, "command": cmd.id, "allowed": dec.allowed, "check": dec.check,
                  "rule": dec.rule, "hold": dec.hold}
        led.emit("gate.decided", now, record, f"gate:{cmd.id}", source=cmd.source, command=cmd.id)
        # a business effect already taken in this stream raises sqlite3.IntegrityError here, and nothing of the command is kept
        dec.events = [e.id for e in store.append(stream, led.pending) if e.id is not None]
        return dec


def open_dispute_on(books: Books, charge: str):
    return next((x for x in books.disputes.values() if x.charge == charge and x.outcome is None), None)


def overdue(books: Books, now: datetime) -> list[str]:
    """Disputes past their review date with no decision: they go to the manager, and their holds stay."""
    today = local(now).date()
    return [d.id for d in books.disputes.values() if d.outcome is None and d.review_by < today]
