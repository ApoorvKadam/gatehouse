"""The work record: one item for each thing staff have to do, drawn from the records that already say so.

Staff work lives in several places (a collections task, a renting task, an overlock that is due, a rental not finished, a gate code
not confirmed, a unit waiting for its old code to go). `conditions` reads each of them off the books as it stands and gives every
one a stable key, so a condition is one item however often it is read. Nothing old is rewritten and nothing is invented: an item
is a reading of a record that exists.

An item closes only when its completion check reads true on the record it is about (the overlock is lifted, the plan is decided, the
code is read back). Acknowledging is not finishing. Where no record can show the work (a call made, a promise judged), staff finish
the item by saying what they checked, and the item says it was staff attested. If the problem comes back the same item reopens.

Events (all in the copy's own stream): work.opened, work.closed, work.reopened, work.changed written by `sweep`, and work.assigned, work.acknowledged,
work.started, work.noted, work.attested written by a staff command through the gate (`run`). The books and renting ignore them."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime

from . import delinquency as col
from . import renting
from .billing import Ledger
from .books import Books, local
from .evidence import clock, day
from .gate import STAFF_ROLES, WHO, WORK_SCOPE, Decision
from .store import Store

CLOSED = ("done", "cancelled")                  # the other states: open, assigned, in progress, waiting for evidence, blocked, reopened
FRONT, OFFICE = "front_desk", col.OWNER_ROLE
NOTE_MAX = 300
# collections: what each kind of task is called on the page, how it ends, and whether a record can show that it ended. The kinds
# that name a plan, a payment in flight or a promise read the record; the rest (a call back, a lien file, the sale money) end when staff say.
WORDS = {"plan breached": "Payment plan breached", "plan needs staff": "Payment plan needs a decision",
         "returned payment needs staff": "Returned payment not applied", "payment uncertain": "Payment waiting for the card processor",
         "call back": "Promise to pay missed: call back", "exemption ended": "Exemption ended"}
CLOSES = {"breached": "Staff reinstate or end the plan.", "review": "Staff decide the plan.",
          "short": "The held or closed charge is settled, or staff say they decided.", "uncertain": "The card processor answers.",
          "callback": "The promised payment arrives, or staff say they called."}
CHECKED = ("breached", "review", "short", "uncertain", "callback")     # a record shows these end
ATTESTED = ("short", "callback")                                       # and staff may also say so where the record cannot


@dataclass
class Cond:
    """One thing that needs doing, read off the books now."""
    key: str                        # the source condition, stable for as long as it is the same condition
    source: str                     # collections, overlock, reservation, rental, refund, access, turnover
    ref: str                        # the record it is about
    kind: str
    what: str                       # the headline in words
    why: str
    closes: str                     # what closes it, in words
    role: str
    due: date
    tone: str = "plain"
    unit: str | None = None
    tenant: str | None = None
    lease: str | None = None
    waits: bool = False             # the work is out of staff's hands for now (a processor, a move-in day, a renter's hold)
    blocked: bool = False           # a hold stops the work until it is settled
    attest: bool = False            # staff may finish it by saying what they checked
    closed_by: str = "The record no longer shows it."


def _dated(d: date, year: int) -> str:
    return day(d) if d.year == year else f"{day(d)}, {d.year}"


# a rental's payment, as the page and the queue both read it ------------------------------------------------------------------
def pay_state(rent: renting.Rent, r) -> str:
    """Where a rental's payment stands: paid, waiting for the card processor, returned by the bank (every payment it had came back),
    declined (the last attempt), or not paid yet."""
    if rent.paid(r):
        return "paid"
    if rent.uncertain(r):
        return "waiting"
    if r.payments:
        return "returned"
    return "declined" if r.pay_ops and "declined" in (rent.ops[r.pay_ops[-1]].result or {}) else "unpaid"


def move_in(rent: renting.Rent, r, now: datetime) -> date | None:
    """The day a paid and signed rental that has not started is waiting for, when that day has not come; else None."""
    if not r.quote:
        return None
    start = date.fromisoformat(r.quote["start"])
    return start if r.state in renting.WAITING and r.accepted and pay_state(rent, r) == "paid" and start > local(now).date() else None


def stage(rent: renting.Rent, r, now: datetime) -> str:
    """Where a rental that has not started stands, in one sentence: its payment and its agreement."""
    pay = pay_state(rent, r)
    if move_in(rent, r, now):
        return "Waiting for the move-in day"
    if pay == "paid" and r.accepted:
        return "Paid and accepted, but the rental has not started"
    words = {"paid": "paid", "waiting": "payment waiting for the card processor", "returned": "payment returned by the bank",
             "declined": "card declined", "unpaid": "not paid yet"}[pay]
    text = f"{words}, agreement {'accepted' if r.accepted else 'not accepted'}"
    return text[:1].upper() + text[1:]


# the conditions ------------------------------------------------------------------------------------------------------------
def _collections(b: Books, today: date) -> list[Cond]:
    st, out = col.state(b), []
    for t in st.tasks.values():
        head, _, rest = t.ref.partition(":")
        sub = rest.split(":")[0]
        plan = st.plans.get(sub) if head in ("breached", "review", "short", "uncertain") else None
        blocked = waits = False
        if head == "breached":
            live = plan is not None and plan.state == "breached"
        elif head == "review":
            live = plan is not None and plan.state == "review"
        elif head == "short":
            live = plan is not None and plan.state in col.OPEN_PLAN and any(b.held(c["charge"]) or b.charges[c["charge"]].closed for c in plan.covered)
            blocked = True
        elif head == "uncertain":
            live, waits = col.unsettled(b, t.tenant), True
        elif head == "callback":
            promise = st.promises.get(sub)
            live = promise is None or col.promise_paid(b, promise) < promise.cents
        else:
            live = True                     # no record shows it: it ends when staff say, as an acknowledgement always did
        attest = head not in CHECKED or head in ATTESTED
        if (t.acknowledged and attest) or not live:         # an old acknowledgement is staff saying so, where no record can
            continue
        lease = plan.covered[0]["lease"] if plan and plan.covered else None
        out.append(Cond(f"collections:{t.ref}", "collections", t.id, t.kind, WORDS.get(t.kind, t.kind[:1].upper() + t.kind[1:]),
                        t.why[:1].upper() + t.why[1:], CLOSES.get(head, "Staff say what they checked."), t.owner, t.due, "act",
                        b.leases[lease].unit if lease else None, t.tenant, lease, waits, blocked, attest,
                        "Staff decided the plan." if head in ("breached", "review") else "The record shows it settled."))
    return out


def _overlocks(b: Books, now: datetime, today: date) -> list[Cond]:
    st, out = col.state(b), []
    days = (st.schedule or {}).get("overlock_days")
    for x in b.leases.values():
        if x.ended is not None:
            continue
        late = col.days_past_due(b, x.id, now)
        if st.overlocks.get(x.id):
            if late is None and col.enforcement_hold(b, x.id, "overlock", now) is None:
                out.append(Cond(f"overlock:{x.id}", "overlock", x.id, "lift overlock", "Overlock can come off",
                                f"{b.tenants[x.tenant].name} has paid what was past due, so the overlock on unit {x.unit} can come off. "
                                "Staff take the lock off, then record it.", "The overlock is lifted on the unit.", OFFICE, today, "act",
                                x.unit, x.tenant, x.id))
        elif (step := col.step_state(b, x.id, "overlock", now))[0] == "eligible":
            out.append(Cond(f"overlock:{x.id}", "overlock", x.id, "overlock", "Overlock due", step[1][:1].upper() + step[1][1:],
                            "The overlock is recorded on the unit.", OFFICE, date.fromordinal(today.toordinal() - (late - days)), "act",
                            x.unit, x.tenant, x.id))
    return out


def _renting(b: Books, rent: renting.Rent, now: datetime, today: date) -> list[Cond]:
    out, year = [], today.year
    why: dict[str, list[str]] = {}
    for t in rent.tasks.values():
        if t.open and t.kind in ("payment", "hold", "activation", "refund"):
            why.setdefault(t.about, []).append(t.reason)
    for r in rent.reservations.values():
        if rent.holder(r.unit, now) == ("reservation", r.id):
            needs = local(r.expires_at).date() <= today
            out.append(Cond(f"reservation:{r.id}", "reservation", r.id, "reservation held", "Reservation held",
                            f"Held until {clock(r.expires_at)}; the unit is released if no rental starts by then.",
                            "A rental starts from the hold, or the hold ends.", FRONT, local(r.expires_at).date(), "plain",
                            r.unit, r.tenant, None, not needs))
    for r in rent.rentals.values():
        if r.state in renting.WAITING and rent.holder(r.unit, now) == ("rental", r.id):
            where, day_in = stage(rent, r, now), move_in(rent, r, now)
            ends = (f"The hold on the unit ends {clock(r.expires_at)}." if now < r.expires_at else
                    f"The hold time ended {clock(r.expires_at)}; the unit stays held while a payment is waiting or paid for.")
            needs = not day_in and not (now < r.expires_at <= now + renting.CHECKOUT) and (
                pay_state(rent, r) in ("paid", "returned", "declined") or local(r.expires_at).date() <= today)
            out.append(Cond(f"rental:{r.id}", "rental", r.id, "rental not finished", where,
                            f"Paid and signed; the rental starts on its move-in day, {_dated(day_in, year)}." if day_in else " ".join(why.get(r.id, [])) or ends,
                            "The rental starts, or it is cancelled and any payment refunded.", FRONT, local(r.expires_at).date(),
                            "act" if where.startswith(("Card declined", "Payment returned", "Paid and accepted")) else "wait",
                            r.unit, r.tenant, r.lease, not needs))
    for t in rent.tasks.values():
        r = rent.rentals.get(t.about)
        if t.open and t.kind == "refund" and r is not None:
            out.append(Cond(f"refund:{t.about}:{t.key or ''}", "refund", r.id, "refund to make",
                            "Cancelled after payment, refund to make" if r.state == "cancelled" else "Moved out, refund to make", t.reason,
                            "The refund is confirmed paid.", t.role, t.due, "act", r.unit, r.tenant, r.lease,
                            closed_by="The refund was confirmed."))
        a = rent.access.get(t.about)
        r = rent.rentals.get(a.rental if a else t.about)
        if t.open and t.kind == "access" and r is not None and r.state in ("active", "ending"):
            out.append(Cond(f"access:{t.about}", "access", r.id, "gate code not confirmed", "Gate code not confirmed", t.reason,
                            "The code is read back from the access system.", t.role, t.due, "act", r.unit, r.tenant, r.lease))
    for r in rent.rentals.values():
        if r.state == "ended" and rent.holder(r.unit, now) == ("rental", r.id):
            m = rent.moveouts.get(r.moveout or "")
            what = f"Moved out {_dated(m.on, year)}" if m else "Moved to another unit"
            if r.access is None:            # from before Staddle: its old code is not on record, so staff check the gate system
                task = next((t for t in rent.tasks.values() if t.open and t.kind == "clearance" and t.about == r.id), None)
                text, tone = (task.reason if task else "The gate code from before Staddle is not on record; staff check the gate system before the unit is rented again."), "act"
                closes = "Staff confirm the old gate code is gone."
            else:
                a = rent.access[r.access]
                task = next((t for t in rent.tasks.values() if t.open and t.kind == "revoke" and t.about == a.id), None)
                text = task.reason if task else "The old code is not revoked on the gate system yet, so the unit cannot be rented again."
                tone = "act" if task or a.state == "revocation_failed" else "wait"
                closes = "The old code is revoked on the gate system."
            out.append(Cond(f"turnover:{r.id}", "turnover", r.id, "waiting for old gate code", what, text, closes, task.role if task else FRONT,
                            m.on if m else today, tone, r.unit, r.tenant, r.lease, tone == "wait", closed_by="The old gate code is gone."))
    return out


def conditions(b: Books, rent: renting.Rent, now: datetime) -> dict[str, Cond]:
    """Everything staff have to do right now, by key, read off the books and the renting records."""
    today = local(now).date()
    return {c.key: c for c in _collections(b, today) + _overlocks(b, now, today) + _renting(b, rent, now, today)}


# the folded record -----------------------------------------------------------------------------------------------------------
@dataclass
class Rec:
    key: str
    snap: dict                                  # the condition as it was when this cycle opened
    open: bool = True
    cycle: int = 1                              # how many times it has been opened
    phase: str = "open"                         # open, reopened, assigned, in progress: where staff have taken it
    assignee: str | None = None
    acknowledged: tuple | None = None           # (who, when) in this cycle
    started: tuple | None = None
    notes: list[tuple] = field(default_factory=list)
    attested: dict | None = None
    closed: dict | None = None                  # how, evidence, at
    moves: int = 0                              # opened, closed and reopened so far: each move has its own event key


class State:
    def __init__(self):
        self.items: dict[str, Rec] = {}

    def apply(self, kind: str, at: datetime, p: dict) -> None:
        if not kind.startswith("work."):
            return
        rec = self.items.get(p["key"])
        if kind == "work.opened":
            self.items[p["key"]] = Rec(p["key"], p["cond"], moves=1)
            return
        if kind == "work.reopened":
            rec.snap, rec.open, rec.cycle, rec.phase, rec.closed, rec.acknowledged, rec.started, rec.attested = p["cond"], True, rec.cycle + 1, "reopened", None, None, None, None
            rec.moves += 1
        elif kind == "work.changed":
            rec.snap, rec.moves = p["cond"], rec.moves + 1
        elif kind == "work.closed":
            rec.open, rec.closed, rec.moves = False, {"how": p["how"], "evidence": p["evidence"], "at": at}, rec.moves + 1
        elif kind == "work.assigned":
            rec.assignee = p["to"]
            rec.phase = "assigned" if rec.phase in ("open", "reopened") else rec.phase
        elif kind == "work.acknowledged":
            rec.acknowledged = (p["by"], at)
        elif kind == "work.started":
            rec.started, rec.phase = (p["by"], at), "in progress"
        elif kind == "work.noted":
            rec.notes.append((at, p["by"], p["text"]))
        elif kind == "work.attested":
            rec.attested = {"by": p["by"], "text": p["text"], "at": at}


def load(store: Store, stream: str) -> tuple[Books, renting.Rent, State]:
    """The stream's books, its renting state and its work record, folded as they stand."""
    books, rent = renting.load(store, stream)
    wk = State()
    for e in store.stream_events(stream):
        wk.apply(e.kind, e.at, e.payload)
    return books, rent, wk


# the queue ----------------------------------------------------------------------------------------------------------------------
@dataclass
class Item:
    key: str
    source: str
    ref: str
    kind: str
    what: str
    why: str
    closes: str
    tone: str
    role: str                                   # whose work it is by default; the assignee, when there is one, holds it instead
    assignee: str | None
    unit: str | None
    tenant: str | None
    lease: str | None
    due: date
    state: str
    cycle: int
    waits: bool
    attest: bool
    overdue: bool
    acknowledged: tuple | None = None
    started: tuple | None = None
    notes: list[tuple] = field(default_factory=list)
    evidence: str | None = None
    closed_at: datetime | None = None


def _closing(b: Books, rent: renting.Rent, snap: dict) -> tuple[str, str]:
    """How an item whose condition is gone ended, and what the record shows."""
    key, kind = snap["key"], snap["kind"]
    if key.startswith("overlock:"):
        held = bool(col.state(b).overlocks.get(snap["lease"]))
        if kind == "lift overlock":
            return "done", "The tenant owes again, so the overlock stays on the unit." if held else "The overlock is lifted on the unit."
        return "done", "The overlock is recorded on the unit." if held else "The overlock is no longer due."
    if key.startswith("rental:"):
        r = rent.rentals.get(snap["ref"])
        return ("done", "The rental started.") if r and r.state in ("active", "ending", "ended") else ("cancelled", "The hold ended or the rental was cancelled.")
    if key.startswith("reservation:"):
        started = any(r.reservation == snap["ref"] and r.state not in ("cancelled",) for r in rent.rentals.values())
        return ("done", "A rental started from this hold.") if started else ("cancelled", "The hold ended.")
    if key.startswith("access:"):
        r = rent.rentals.get(snap["ref"])
        return ("done", "The code was read back from the access system.") if r and r.state in ("active", "ending") else (
            "cancelled", "The rental ended; the unit is listed for the old code to be revoked instead.")
    if key.startswith("collections:"):
        t = next((t for t in col.state(b).tasks.values() if t.id == snap["ref"]), None)
        if t and t.acknowledged and (t.ref.split(":")[0] not in CHECKED or t.ref.split(":")[0] in ATTESTED):
            return "done", "Staff acknowledged it."
    return "done", snap["closed_by"]


def _live(b: Books, rent: renting.Rent, wk: State, now: datetime) -> tuple[dict[str, Cond], dict[str, dict]]:
    """The conditions that still stand, and those staff finished by attestation this cycle (they count as gone)."""
    conds, said = conditions(b, rent, now), {}
    for key, c in list(conds.items()):
        rec = wk.items.get(key)
        if c.attest and rec and rec.attested:               # said done for this condition: it stays done while the books still show it
            said[key] = conds.pop(key).__dict__
    return conds, said


def _changed(rec: Rec, c: Cond) -> bool:
    """Whether the condition under an open item now means something else (an overlock paid before the check ran now asks to be lifted)."""
    return any(rec.snap[k] != getattr(c, k) for k in ("kind", "closes", "attest", "closed_by"))


def _snap(c: Cond) -> dict:
    return {**asdict(c), "due": c.due.isoformat()}


def _item(c: dict, rec: Rec | None, state: str, today: date, **extra) -> Item:
    due = date.fromisoformat(c["due"]) if isinstance(c["due"], str) else c["due"]
    return Item(c["key"], c["source"], c["ref"], c["kind"], c["what"], c["why"], c["closes"], c["tone"], c["role"],
                rec.assignee if rec else None, c["unit"], c["tenant"], c["lease"], due, state, extra.get("cycle") or (rec.cycle if rec else 1), c["waits"], c["attest"],
                state not in CLOSED and due < today, rec.acknowledged if rec else None, rec.started if rec else None,
                list(rec.notes) if rec else [], extra.get("evidence"), extra.get("closed_at"))


def queue(b: Books, rent: renting.Rent, wk: State, now: datetime) -> list[Item]:
    """Every item of work, open ones first (overdue, then by due date), then those finished. Read as the records stand now, so it
    is right before the daily check has run; the check only keeps what this shows."""
    today, out = local(now).date(), []
    conds, said = _live(b, rent, wk, now)
    for key, c in conds.items():
        rec, snap = wk.items.get(key), _snap(c)
        extra = {}
        if rec and not rec.open:                        # its problem is back and the check has not run: it reads as reopened now
            state, extra = "reopened", {"cycle": rec.cycle + 1}
            rec = replace(rec, acknowledged=None, started=None, notes=list(rec.notes))
        elif rec is None:
            state = "waiting for evidence" if c.waits else "blocked" if c.blocked else "open"
        else:
            state = "waiting for evidence" if c.waits else "blocked" if c.blocked else rec.phase
            if not _changed(rec, c):                    # the same condition keeps its first reading; a changed one is read afresh
                snap = rec.snap | {k: v for k, v in snap.items() if k in ("what", "why", "waits", "tone", "blocked", "role")}
        out.append(_item({**snap, "key": key}, rec, state, today, **extra))
    for key, rec in wk.items.items():
        if key in conds:
            continue
        snap = rec.snap
        if key in said:
            how, evidence, at = "done", f"Staff checked: {rec.attested['text']}", rec.attested["at"]
        elif rec.open:
            (how, evidence), at = _closing(b, rent, {**snap, "key": key}), now
        else:
            how, evidence, at = rec.closed["how"], rec.closed["evidence"], rec.closed["at"]
        out.append(_item({**snap, "key": key}, rec, how, today, evidence=evidence, closed_at=at))
    return sorted(out, key=lambda i: (i.state in CLOSED, i.due, i.tone != "act", i.source, i.key))


def open_items(items: list[Item]) -> list[Item]:
    return [i for i in items if i.state not in CLOSED]


def mine(items: list[Item], role: str, person: str | None = None) -> list[Item]:
    """The open items that are this role's or this person's: by assignment when there is one, else by the item's own role."""
    return [i for i in open_items(items) if (i.assignee in (role, person) if i.assignee else i.role == role)]


# the daily check ---------------------------------------------------------------------------------------------------------------
def _emit(led: Ledger, wk: State, kind: str, at: datetime, payload: dict, key: str, source: str, command: str | None = None) -> None:
    led.emit(kind, at, payload, key, source=source, command=command)
    wk.apply(kind, at, payload)


def _sync(led: Ledger, rent: renting.Rent, wk: State, now: datetime) -> None:
    """Open an item for each condition that has none, reopen the one whose problem came back, close the one whose record shows it done.
    Each move is keyed by how many moves the item has had, so a second pass, or a pass after a restart, writes nothing new."""
    conds, said = _live(led.books, rent, wk, now)
    for key, c in conds.items():
        rec = wk.items.get(key)
        if rec is None:
            _emit(led, wk, "work.opened", now, {"key": key, "cond": _snap(c)}, f"work:{key}:1", "code")
        elif not rec.open:
            _emit(led, wk, "work.reopened", now, {"key": key, "cond": _snap(c)}, f"work:{key}:{rec.moves + 1}", "code")
        elif _changed(rec, c):
            _emit(led, wk, "work.changed", now, {"key": key, "cond": _snap(c)}, f"work:{key}:{rec.moves + 1}", "code")
    for key, rec in list(wk.items.items()):
        if rec.open and key not in conds:
            how, evidence = (("done", f"Staff checked: {rec.attested['text']}") if key in said else _closing(led.books, rent, {**rec.snap, "key": key}))
            _emit(led, wk, "work.closed", now, {"key": key, "how": how, "evidence": evidence}, f"work:{key}:{rec.moves + 1}", "code")


def sweep(store: Store, stream: str, now: datetime) -> int:
    """The daily check of the work queue against its sources, from the copy's own records. Returns the events written."""
    with store.write():
        books, rent, wk = load(store, stream)
        led = Ledger(books)
        _sync(led, rent, wk, now)
        store.append(stream, led.pending)
        return len(led.pending)


# staff commands through the gate ---------------------------------------------------------------------------------------------------
@dataclass
class Act:
    """One staff command on one item. `to` is a staff role or `person:<name>`; `text` is a note, or what staff checked."""
    id: str
    action: str
    source: str
    key: str
    to: str | None = None
    text: str | None = None


def _person(to: str) -> bool:
    name = to.removeprefix("person:")
    return to.startswith("person:") and 2 <= len(name) <= 40 and all(ch.isalpha() or ch in ".-" for ch in name)


def evaluate(item: Item | None, act: Act) -> tuple[bool, str]:
    """The checks, in the gate's order. 1. the action exists and is staff's. 2. safety, fixed in code: the item is there and open, the
    argument is well formed, only what no record can show is finished by a word. 3 and 4. no owner rule or hold applies: the commands
    move no money, unit or code."""
    if act.action not in WORK_SCOPE:
        return False, "not an action the system knows"
    who = "staff" if act.source.startswith("staff:") else act.source
    if who not in WORK_SCOPE[act.action]:
        return False, f"{WHO.get(who, who)} may not {act.action.removesuffix('_work')} work; only staff may"
    if item is None:
        return False, "there is no such item of work"
    if item.state in CLOSED:
        return False, "that item is already closed; if the problem comes back it reopens"
    text = " ".join((act.text or "").split())
    if act.action == "assign_work" and not (act.to in STAFF_ROLES or act.to and _person(act.to)):
        return False, "work goes to a staff role or a named person"
    if act.action == "assign_work" and act.to == item.assignee:
        return False, "it is already with them"
    if act.action == "acknowledge_work" and item.acknowledged:
        return False, "it was already seen"
    if act.action == "start_work" and item.started:
        return False, "it is already started"
    if act.action in ("note_work", "finish_work") and len(text) > NOTE_MAX:
        return False, f"keep it under {NOTE_MAX} characters"
    if act.action == "note_work" and len(text) < 2:
        return False, "a note needs words"
    if act.action == "finish_work":
        if not item.attest:
            return False, f"the record closes this one, not a word: {item.closes[:1].lower()}{item.closes[1:]}"
        if len(text) < 3:
            return False, "say in a few words what you checked"
    return True, "recorded"


def run(store: Store, stream: str, act: Act, now: datetime) -> Decision:
    """Check and record one staff command in one write transaction. A command id already decided returns that decision and writes
    nothing. An item first seen here is opened first, so its record starts at the first thing staff did with it."""
    with store.write():
        books, rent, wk = load(store, stream)
        if act.id in books.decisions:
            p = books.decisions[act.id]
            return Decision(p["allowed"], p["action"], p["check"], command=act.id, repeated=True)
        led = Ledger(books)
        item = next((i for i in queue(books, rent, wk, now) if i.key == act.key), None)
        ok, check = evaluate(item, act)
        if ok:
            if act.key not in wk.items:
                conds = conditions(books, rent, now)
                _emit(led, wk, "work.opened", now, {"key": act.key, "cond": _snap(conds[act.key])}, f"work:{act.key}:1", "code")
            elif not wk.items[act.key].open:
                _emit(led, wk, "work.reopened", now, {"key": act.key, "cond": _snap(conditions(books, rent, now)[act.key])},
                      f"work:{act.key}:{wk.items[act.key].moves + 1}", "code")
            who, text = act.source, " ".join((act.text or "").split())
            kind, payload = {"assign_work": ("work.assigned", {"to": act.to}), "acknowledge_work": ("work.acknowledged", {}),
                             "start_work": ("work.started", {}), "note_work": ("work.noted", {"text": text}),
                             "finish_work": ("work.attested", {"text": text})}[act.action]
            _emit(led, wk, kind, now, {"key": act.key, "by": who, **payload}, f"work:{act.id}", who, act.id)
        led.emit("gate.decided", now, {"id": act.id, "action": act.action, "source": act.source, "key": act.key, "to": act.to, "text": act.text,
                                       "command": act.id, "allowed": ok, "check": check}, f"gate:{act.id}", source=act.source, command=act.id)
        d = Decision(ok, act.action, check, command=act.id)
        d.events = [e.id for e in store.append(stream, led.pending) if e.id is not None]
        return d
