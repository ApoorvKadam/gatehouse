"""Renting a unit, start to end: holds, reservations, rentals and their agreements, transfers, move-outs, the waitlist, and the
gate code that goes with a rental.

A unit's status (vacant, reserved, occupied, blocked) is worked out from its records at the moment asked, never stored. A unit
has one holder at a time and the database keeps it so: each new hold takes the next number for its unit as its event key
(claim:A101:3), keys are unique in a stream, and two writers that both saw the unit free cannot both write. Money moves only
through billing commands. Each outside call (the access system and the payment processor) has one operation record,
written before the call; a retry first asks the outside system what the earlier call did, so nothing is issued twice."""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import pickle
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from . import delinquency
from .billing import Ledger, Refused, first_month
from .books import ADMIN_FEE, ADMIN_FEE_CENTS, CUTOFF_DAY, LATE_FEE, LATE_FEE_CENTS, RENT, Books, local, month_key
from .evidence import money
from .gate import Decision, evaluate_rent
from .store import BASE, Store

log = logging.getLogger("core.renting")
CHECKOUT = timedelta(minutes=20)        # how long a hold lasts while the renter pays
SCHEDULE = [ADMIN_FEE, LATE_FEE]        # the fees a new rental signs for
ACCESS_LEVEL = "tenant hours"           # the access-level group every renter's code joins, beside the unit's own group
STEPS = ("user", "unit_group", "level_group", "pin")
WAITING = ("pending", "awaiting_payment", "awaiting_signature")
AGREEMENT_VERSION = "rental-agreement-1"
AGREEMENT = ("Storage rental agreement ({version})\n"
             "{facility} rents storage unit {unit} to {name}, starting {start}.\n"
             "Monthly rent is {rent}, due on the 1st of each month.\n"
             "Due before move-in: {first} rent for the rest of the first month{fees}, {total} in all.\n"
             "{late}"
             "The tenant stores nothing unlawful or dangerous in the unit and tells the facility before moving out.\n"
             "Accepting this in checkout records this exact text, who accepted it and when.")
# a renting task is its own record kind: collections folds every "task.*" event into its own tasks, with other fields. A payment
# intent's moves are collections' to fold; renting reads them only to remember which intents were ever reported failed or closed
KINDS = ("unit.", "reservation.", "rental.", "agreement.", "transfer.", "moveout.", "waitlist.", "access.", "op.", "rent_task.",
         "intent.updated", "refund.paid")
# the refusal that means a payment checked out as this tenant's, unreturned and unrecorded, but its rental was already cancelled:
# only this refusal sets the money aside for a refund
LATE = "the rental was cancelled before the payment came; the money is set aside for staff to refund"
NO_HOLD = "the hold on the unit ran out, so no new payment can start on it; reserve the unit again"
LATE_SPENT = "the rental was cancelled before the payment came; older charges on the account took all of it, so there is nothing to refund"

# events that only move a record to a new state: kind -> (collection, payload field naming the record, the new state)
MOVES = {
    "reservation.checkout": ("reservations", "reservation", "awaiting_checkout"),
    "reservation.confirmed": ("reservations", "reservation", "confirmed"),
    "reservation.expired": ("reservations", "reservation", "expired"),
    "reservation.cancelled": ("reservations", "reservation", "cancelled"),
    "rental.activated": ("rentals", "rental", "active"),
    "rental.ending": ("rentals", "rental", "ending"),
    "rental.ended": ("rentals", "rental", "ended"),
    "rental.cancelled": ("rentals", "rental", "cancelled"),
    "transfer.approved": ("transfers", "transfer", "approved"),
    "transfer.review": ("transfers", "transfer", "review"),
    "transfer.completed": ("transfers", "transfer", "completed"),
    "transfer.cancelled": ("transfers", "transfer", "cancelled"),
    "moveout.reviewed": ("moveouts", "moveout", "reviewed"),
    "moveout.billed": ("moveouts", "moveout", "final_billed"),
    "moveout.revoking": ("moveouts", "moveout", "access_revocation_pending"),
    "moveout.clearing": ("moveouts", "moveout", "access_clearance_pending"),
    "moveout.completed": ("moveouts", "moveout", "completed"),
    "waitlist.accepted": ("entries", "entry", "accepted"),
    "waitlist.declined": ("entries", "entry", "declined"),
    "waitlist.expired": ("entries", "entry", "expired"),
    "access.revoke_requested": ("access", "access", "revocation_pending"),
    "access.revoked": ("access", "access", "revoked"),
}
# what each command acts on: a unit of the facility, a size (for the waitlist), or a record collection
ON = {
    "price_unit": "unit", "block_unit": "unit", "unblock_unit": "unit", "reserve": "unit",
    "cancel_reservation": "reservations", "start_checkout": "reservations",
    "accept_agreement": "rentals", "confirm_payment": "rentals", "payment_failed": "rentals", "activate": "rentals",
    "request_transfer": "rentals", "approve_transfer": "transfers", "complete_transfer": "transfers",
    "request_move_out": "rentals", "review_move_out": "moveouts", "final_bill": "moveouts",
    "join_waitlist": "size", "offer_unit": "entries", "accept_offer": "entries", "decline_offer": "entries",
    "retry_access": "access", "confirm_refund": "refunds", "confirm_access_cleared": "rentals",
}
NOUN = {"unit": "unit at this facility", "size": "unit size at this facility", "reservations": "reservation", "rentals": "rental",
        "transfers": "transfer", "moveouts": "move-out", "entries": "waitlist entry", "access": "gate code record", "refunds": "refund"}


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _d(s: str) -> date:
    return date.fromisoformat(s)


# the dates and times a command may carry: a day for `start` and `on`, a time with its zone for `until`
DATE_ARGS = {"reserve": ("until", "start"), "request_transfer": ("until", "start"), "approve_transfer": ("until",),
             "request_move_out": ("on",), "offer_unit": ("until",)}


def _arg_problem(action: str, args: dict) -> str | None:
    """Why a date or time in the command's arguments cannot be read, or None. Checked before any of them is compared, so a bad one
    is a refusal and never an error."""
    for name in DATE_ARGS.get(action, ()):
        if name not in args:
            continue
        value = args[name]
        try:
            if name == "until":
                if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError
            elif not isinstance(value, str):
                raise ValueError
            else:
                date.fromisoformat(value)
        except ValueError:
            return (f"the {name} time must be a date and time with its time zone" if name == "until"
                    else f"the {name} day must be a real date, year-month-day")
    return None


def sha(text: str) -> str:
    # for a PIN this is a comparison value, not secrecy: six digits are quick to enumerate
    return hashlib.sha256(text.encode()).hexdigest()


def _day(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


@dataclass
class Act:
    """One renting command. `about` is what it acts on (a unit, a size, or a record's id); `tenant` is who asks, for a renter's
    or the agent's request, and who a staff hold is for."""
    id: str                         # stable: the same id always gets the same decision back
    action: str
    source: str                     # renter, operator, staff:<role>, code
    about: str
    tenant: str | None = None
    args: dict = field(default_factory=dict)


@dataclass
class Unit:
    id: str
    rent_cents: int | None = None
    size: str | None = None
    claims: int = 0                         # holds the unit has had; the next one's event key uses claims + 1
    holder: tuple[str, str] | None = None   # the latest hold: (block, reservation, offer or rental; its id)


@dataclass
class Block:
    id: str
    unit: str
    reason: str
    active: bool = True


@dataclass
class Reservation:
    id: str
    unit: str
    tenant: str
    expires_at: datetime
    start: date
    quote: dict                     # frozen when the unit was held
    state: str = "held"             # held, awaiting_checkout, confirmed, expired, cancelled


@dataclass
class Rental:
    id: str
    tenant: str
    unit: str
    quote: dict | None
    expires_at: datetime | None     # the hold's deadline until the rental is active
    kind: str = "new"               # new, or transfer (the new unit of a transfer)
    state: str = "pending"          # pending, awaiting_payment, awaiting_signature, active, ending, ended, cancelled
    reservation: str | None = None
    transfer: str | None = None
    lease: str | None = None
    agreement: str | None = None
    accepted: bool = False
    payments: list[str] = field(default_factory=list)   # the processor's confirmed payments for it
    pay_ops: list[str] = field(default_factory=list)    # each checkout intent's operation
    access: str | None = None
    moveout: str | None = None
    refund: dict | None = None      # money to give back when a paid rental never started: cents, and pending until confirmed
    cancelled_why: str | None = None    # what cancelled a rental that never started, as the cancel event recorded it
    provenance: dict | None = None    # a rental adopted from a lease made before Staddle: where it came from
    cleared: bool = False           # staff confirmed no gate code from before Staddle is left for the unit


@dataclass
class Agreement:
    id: str
    rental: str
    version: str
    sha256: str                     # of the exact text shown
    text: str
    state: str = "presented"        # presented, accepted
    by: str | None = None
    at: datetime | None = None
    amount_cents: int | None = None
    payments: list[str] = field(default_factory=list)


@dataclass
class Transfer:
    id: str
    rental: str                     # the rental being left
    target: str                     # the new unit's rental
    unit: str
    state: str = "target_reserved"  # target_reserved, approved, review, completed, cancelled


@dataclass
class MoveOut:
    id: str
    rental: str
    on: date                        # the last day the tenant has the unit
    state: str = "requested"        # requested, reviewed, final_billed, access_revocation_pending, completed
    bill: dict | None = None


@dataclass
class Entry:
    id: str
    tenant: str
    size: str
    joined_at: datetime
    state: str = "waiting"          # waiting, offered, accepted, declined, expired
    unit: str | None = None
    expires_at: datetime | None = None


@dataclass
class Access:
    id: str
    rental: str
    unit: str
    groups: list[str]               # the unit's group and the access-level group; a code works only with both
    state: str = "requested"        # requested, issuing, issued_unverified, verified, failed, revocation_pending, revocation_failed, revoked
    user: str | None = None
    credential: str | None = None
    code_sha256: str | None = None
    reads: int = 0                  # read-backs answered, so the next read gets a new operation id
    revokes: int = 0                # staff retries of a failed revocation, so each gets a revoke operation of its own
    simulated: bool = False         # the code came from the stand-in, never from a real gate


@dataclass
class Op:
    """One outside call: recorded before it is made, its attempts counted, its answer kept."""
    id: str                         # stable; the outside system's idempotency key
    step: str
    sha256: str                     # of the request
    access: str | None = None
    rental: str | None = None
    attempts: int = 0
    result: dict | None = None
    how: str | None = None          # response; found (the answer was lost and a lookup found it); not_found
    intent: str | None = None       # a payment's processor intent (core/delinquency.py), written in the same step as the operation


@dataclass
class Task:
    id: str
    about: str
    kind: str
    reason: str
    due: date
    role: str = "front_desk"
    open: bool = True
    key: str | None = None          # what the work is about when a record has several of one kind (a refund's own key); absent on older tasks


class Rent:
    """The renting state of one stream, folded event by event beside the books. Commands read it; only events change it."""

    def __init__(self, books: Books):
        self.books = books
        self.units: dict[str, Unit] = {}
        self.blocks: dict[str, Block] = {}
        self.reservations: dict[str, Reservation] = {}
        self.rentals: dict[str, Rental] = {}
        self.agreements: dict[str, Agreement] = {}
        self.transfers: dict[str, Transfer] = {}
        self.moveouts: dict[str, MoveOut] = {}
        self.entries: dict[str, Entry] = {}
        self.access: dict[str, Access] = {}
        self.ops: dict[str, Op] = {}
        self.tasks: dict[str, Task] = {}
        self.failed_intents: set[str] = set()   # payment intents the processor ever reported failed, or closed for want of a payment

    @property
    def refunds(self) -> dict[str, dict]:
        """The refunds set aside on the books, by key, for the commands that act on one."""
        return self.books.refund_of

    def unit(self, uid: str) -> Unit:
        return self.units.setdefault(uid, Unit(uid))

    @staticmethod
    def new_id(prefix: str, records: dict) -> str:
        return f"{prefix}{len(records) + 1:04d}"

    def claim_key(self, unit: str) -> str:
        return f"claim:{unit}:{self.unit(unit).claims + 1}"

    def _hold(self, unit: str, kind: str, rid: str, new: bool) -> None:
        u = self.unit(unit)
        u.claims += new
        u.holder = (kind, rid)

    # folding ----------------------------------------------------------------------------------------------------------
    def apply(self, kind: str, at: datetime, p: dict) -> None:
        if not kind.startswith(KINDS):
            return
        if kind in MOVES:
            coll, key, state = MOVES[kind]
            getattr(self, coll)[p[key]].state = state
        fn = getattr(self, "_on_" + kind.replace(".", "_"), None)
        if fn:
            fn(at, p)

    def _on_unit_priced(self, at, p):
        u = self.unit(p["unit"])
        u.rent_cents, u.size = p["rent_cents"], p["size"]

    def _on_unit_blocked(self, at, p):
        self.blocks[p["block"]] = Block(p["block"], p["unit"], p["reason"])
        self._hold(p["unit"], "block", p["block"], True)

    def _on_unit_unblocked(self, at, p):
        self.blocks[p["block"]].active = False

    def _on_reservation_made(self, at, p):
        self.reservations[p["reservation"]] = Reservation(p["reservation"], p["unit"], p["tenant"], _t(p["expires_at"]), _d(p["start"]), p["quote"])
        self._hold(p["unit"], "reservation", p["reservation"], True)

    def _on_rental_started(self, at, p):
        r = Rental(p["rental"], p["tenant"], p["unit"], p["quote"], _t(p["expires_at"]), p["kind"], reservation=p.get("reservation"),
                   transfer=p.get("transfer"))
        self.rentals[r.id] = r
        self._hold(r.unit, "rental", r.id, p["claims"])

    def _on_rental_adopted(self, at, p):
        r = Rental(p["rental"], p["tenant"], p["unit"], None, None, state="active", lease=p["lease"],
                   provenance=p["provenance"])
        self.rentals[r.id] = r
        self._hold(r.unit, "rental", r.id, True)

    def _on_rental_access_cleared(self, at, p):
        self.rentals[p["rental"]].cleared = True

    def _on_rental_paid(self, at, p):
        r = self.rentals[p["rental"]]
        r.payments.append(p["payment"])
        r.state = self._waiting(r)

    def _on_rental_cancelled(self, at, p):
        self.rentals[p["rental"]].refund = p.get("refund")
        self.rentals[p["rental"]].cancelled_why = p.get("reason")

    def _on_rental_activated(self, at, p):
        self.rentals[p["rental"]].lease = p["lease"]

    def _on_rental_funded(self, at, p):
        self.rentals[p["rental"]].lease = p["lease"]

    def _on_agreement_presented(self, at, p):
        self.agreements[p["agreement"]] = Agreement(p["agreement"], p["rental"], p["version"], p["sha256"], p["text"])
        self.rentals[p["rental"]].agreement = p["agreement"]

    def _on_agreement_accepted(self, at, p):
        a = self.agreements[p["agreement"]]
        a.state, a.by, a.at = "accepted", p["by"], at
        a.amount_cents = p.get("amount_cents")
        r = self.rentals[p["rental"]]
        a.payments = list(r.payments)
        r.accepted = True
        r.state = self._waiting(r)

    def _on_agreement_requoted(self, at, p):
        r = self.rentals[p["rental"]]
        r.accepted = False
        self.agreements[r.agreement].state = "presented"

    def _on_transfer_requested(self, at, p):
        self.transfers[p["transfer"]] = Transfer(p["transfer"], p["rental"], p["target"], p["unit"])

    def _on_transfer_approved(self, at, p):
        if p.get("expires_at"):
            self.rentals[self.transfers[p["transfer"]].target].expires_at = _t(p["expires_at"])

    def _on_moveout_requested(self, at, p):
        self.moveouts[p["moveout"]] = MoveOut(p["moveout"], p["rental"], _d(p["on"]))
        self.rentals[p["rental"]].moveout = p["moveout"]

    def _on_moveout_billed(self, at, p):
        self.moveouts[p["moveout"]].bill = dict(p["bill"])      # a copy: the bill as written is never changed, its refund state is

    def _on_refund_paid(self, at, p):
        # a confirmed refund reads paid where it is shown: on the cancelled rental it was set aside for, or in the final bill
        owner = p["refund"].removeprefix("refund:")
        if owner in self.rentals and self.rentals[owner].refund:
            self.rentals[owner].refund = {**self.rentals[owner].refund, "state": "paid"}
        elif owner in self.moveouts and self.moveouts[owner].bill:
            self.moveouts[owner].bill["refund"] = "paid"

    def _on_waitlist_joined(self, at, p):
        self.entries[p["entry"]] = Entry(p["entry"], p["tenant"], p["size"], at)

    def _on_waitlist_offered(self, at, p):
        e = self.entries[p["entry"]]
        e.state, e.unit, e.expires_at = "offered", p["unit"], _t(p["expires_at"])
        self._hold(p["unit"], "offer", e.id, True)

    def _on_waitlist_accepted(self, at, p):
        self.reservations[p["reservation"]] = Reservation(p["reservation"], p["unit"], p["tenant"], _t(p["expires_at"]), _d(p["start"]), p["quote"])
        self._hold(p["unit"], "reservation", p["reservation"], False)

    def _on_access_requested(self, at, p):
        self.access[p["access"]] = Access(p["access"], p["rental"], p["unit"], list(p["groups"]))
        self.rentals[p["rental"]].access = p["access"]

    def _on_access_read(self, at, p):
        a = self.access[p["access"]]
        if p.get("revoking"):
            if not p["ok"]:
                a.state = "revocation_failed"
        else:
            a.state = "verified" if p["ok"] else "failed"

    def _on_access_retry(self, at, p):
        a = self.access[p["access"]]
        if a.state == "revocation_failed":
            a.state, a.revokes = "revocation_pending", a.revokes + 1
        else:
            a.state = "issued_unverified"

    def _on_intent_updated(self, at, p):
        # the folded intent keeps only where it stands now, so a close that a later success replaces would be forgotten
        if p["state"] in ("failed", "closed"):
            self.failed_intents.add(p["intent"])

    def _on_op_requested(self, at, p):
        op = Op(p["op"], p["step"], p["sha256"], p.get("access"), p.get("rental"), intent=p.get("intent"))
        self.ops[op.id] = op
        if op.rental:
            self.rentals[op.rental].pay_ops.append(op.id)
        if op.access and op.step in STEPS and self.access[op.access].state == "requested":
            self.access[op.access].state = "issuing"

    def _on_op_attempted(self, at, p):
        self.ops[p["op"]].attempts = p["n"]

    def _on_op_acknowledged(self, at, p):
        op = self.ops[p["op"]]
        op.result, op.how = p["result"], p["how"]
        a = self.access.get(op.access or "")
        if a is not None and p.get("simulated"):
            a.simulated = True
        if a is None or op.result is None:
            return
        if op.step == "user":
            a.user = op.result["user"]
        elif op.step == "read":
            a.reads += 1
        elif op.step == "pin":
            a.credential, a.code_sha256 = op.result["credential"], op.result["code_sha256"]
            # a PIN answer that lands after a move-out asked for revocation keeps the revocation going: the revoke step removes it
            if a.state in ("requested", "issuing"):
                a.state = "issued_unverified"

    def _on_rent_task_opened(self, at, p):
        self.tasks[p["task"]] = Task(p["task"], p["about"], p["kind"], p["reason"], _d(p["due"]), p["role"], key=p.get("key"))

    def _on_rent_task_closed(self, at, p):
        self.tasks[p["task"]].open = False

    def _waiting(self, r: Rental) -> str:
        if r.state not in WAITING:
            return r.state
        if r.accepted and not r.payments:
            return "awaiting_payment"
        if r.payments and not r.accepted:
            return "awaiting_signature"
        return "awaiting_payment" if r.accepted else "pending"

    # reads ------------------------------------------------------------------------------------------------------------
    def paid(self, r: Rental) -> bool:
        """A confirmed payment for the rental that has not come back."""
        return (any(not self.books.payments[p].returned for p in r.payments)
                or bool(r.lease and not self.books.receivable(r.lease)))

    def uncertain(self, r: Rental) -> bool:
        """A checkout payment was started and the processor's answer has not come."""
        return any(self.ops[o].how is None for o in r.pay_ops)

    def holder(self, unit: str, now: datetime) -> tuple[str, str] | None:
        """The record holding the unit at `now`, or None. A hold past its deadline still holds while a payment for it is
        unconfirmed, paid for, or a lease was already signed (until the rental starts or staff release it); a rental that ended
        holds until its old code is revoked."""
        u = self.units.get(unit)
        if u is None or u.holder is None:
            return None
        kind, rid = u.holder
        if kind == "block":
            live = self.blocks[rid].active
        elif kind == "reservation":
            r = self.reservations[rid]
            live = r.state == "held" and now < r.expires_at
        elif kind == "offer":
            e = self.entries[rid]
            live = e.state == "offered" and now < e.expires_at
        else:
            r = self.rentals[rid]
            if r.state in WAITING:
                live = now < r.expires_at or self.uncertain(r) or self.paid(r) or bool(r.lease)
            elif r.state == "ended":
                live = (r.access is not None and self.access[r.access].state != "revoked") or needs_clearance(r)
            else:
                live = r.state in ("active", "ending")
        return u.holder if live else None

    def _outside_lease(self, unit: str) -> bool:
        """A lease on the unit signed outside renting (the seeded year) and not ended."""
        ours = {r.lease for r in self.rentals.values() if r.lease}
        return any(x.unit == unit and x.ended is None and x.id not in ours for x in self.books.leases.values())

    def status(self, unit: str, now: datetime) -> str:
        h = self.holder(unit, now)
        if h is None:
            return "occupied" if self._outside_lease(unit) else "vacant"
        if h[0] == "block":
            return "blocked"
        if h[0] == "rental":
            state = self.rentals[h[1]].state
            if state in ("active", "ending"):
                return "occupied"
            if state == "ended":
                return "blocked"                # moved out; the old code is not revoked yet
        return "reserved"

    def quote(self, unit: str, start: date, schedule: list[str]) -> dict:
        rent = self.units[unit].rent_cents
        fees = {ADMIN_FEE: ADMIN_FEE_CENTS} if ADMIN_FEE in schedule else {}
        first = first_month(rent, start)
        return {"rent_cents": rent, "start": start.isoformat(), "first_cents": first, "fees": fees, "total_cents": first + sum(fees.values()),
                "schedule": list(schedule)}

    def short(self, r: Rental, now: datetime, old: Rental | None = None) -> int:
        """What the rental's lease would still owe if signed now, on a copy of the books: the money on the account pays older
        charges first. For a transfer, the old lease ends the day before the new one starts."""
        if r.lease and r.state in WAITING:
            return self.books.receivable(r.lease)
        sim = Ledger(pickle.loads(pickle.dumps(self.books)))     # a copy of our own folded books, never outside data
        if old is not None:
            end_lease(sim, old.lease, _d(r.quote["start"]) - timedelta(days=1), now, "code")
        return sim.books.receivable(sign(sim, r, now))

    def _record(self, act: Act):
        where = ON[act.action]
        if where == "unit":
            return self.unit(act.about) if act.about in self.books.units else None
        if where == "size":
            return act.about if any(u.size == act.about for u in self.units.values()) else None
        return getattr(self, where).get(act.about)

    def _tenant_of(self, rec) -> str | None:
        if isinstance(rec, (Reservation, Rental, Entry)):
            return rec.tenant
        if isinstance(rec, (Transfer, MoveOut, Access)):
            return self.rentals[rec.rental].tenant
        return None

    def _rental_of(self, reservation: str) -> Rental | None:
        return next((r for r in self.rentals.values() if r.reservation == reservation and r.state in WAITING), None)

    def _open_transfer(self, rental: str) -> bool:
        """The rental is the old or the new unit of a transfer still under way. Its new unit may already be active (the move is
        billed) while its code is unconfirmed; moving it again then would leave the old unit's code never revoked."""
        return any(rental in (t.rental, t.target) and t.state in ("target_reserved", "approved", "review") for t in self.transfers.values())

    def _target_free(self, unit: str | None, now: datetime, size: str | None = None) -> str | None:
        if unit not in self.books.units:
            return "no such unit at this facility"
        u = self.unit(unit)
        if u.rent_cents is None:
            return "the unit has no price yet"
        if size and u.size != size:
            return f"the unit is not {size}"
        status = self.status(unit, now)
        return None if status == "vacant" else f"the unit is {status}"

    def refusal(self, act: Act, now: datetime) -> str | None:
        """Why the records do not allow the command (the gate's fixed safety check), or None."""
        a, args, b = act.action, act.args, self.books
        staff = act.source.startswith("staff:")
        today = local(now).date()
        rec = self._record(act)
        if rec is None:
            return f"no such {NOUN[ON[a]]}"
        if (bad := _arg_problem(a, args)):
            return bad
        if a in ("reserve", "join_waitlist") and act.tenant not in b.tenants:
            return "no identified tenant to hold it for"
        # a renter or the agent acts only on their own records
        if act.source in ("renter", "operator") and a not in ("reserve", "join_waitlist") and act.tenant != self._tenant_of(rec):
            return "that record is not this tenant's"
        if a == "price_unit":
            if not isinstance(args.get("rent_cents"), int) or args["rent_cents"] <= 0:
                return "the rent must be a whole number of cents above zero"
            return None if args.get("size") else "the unit needs a size"
        if a == "block_unit":
            status = self.status(act.about, now)
            return (None if args.get("reason") else "a block needs a reason") if status == "vacant" else f"the unit is {status}"
        if a == "unblock_unit":
            h = self.holder(act.about, now)
            return None if h and h[0] == "block" else "the unit is not blocked"
        if a == "reserve":
            if ("until" in args or "start" in args) and not staff:
                return f"only staff set a hold longer than {CHECKOUT.seconds // 60} minutes or a later move-in day"
            if "until" in args and _t(args["until"]) <= now:
                return "the hold must end after now"
            if "start" in args and _d(args["start"]) < today:
                return "the move-in day has passed"
            return self._target_free(act.about, now)
        if a == "cancel_reservation" and self.holder(rec.unit, now) != ("reservation", rec.id):
            r = self._rental_of(rec.id)
            if r is None or self.holder(rec.unit, now) != ("rental", r.id):
                return "the reservation is no longer held"
            if self.uncertain(r):
                return "a payment for it is still unconfirmed; wait for the processor's answer"
            return "staff refund the payment and release a paid hold" if self.paid(r) and not staff else None
        if a == "start_checkout":
            return None if self.holder(rec.unit, now) == ("reservation", rec.id) else "the reservation is no longer held"
        if a == "accept_agreement":
            if rec.state not in WAITING:
                return f"the rental is {rec.state.replace('_', ' ')}"
            if self.holder(rec.unit, now) != ("rental", rec.id):
                return "the rental no longer holds the unit"
            if rec.accepted:
                return "the agreement is already accepted"
            if args.get("sha256") != self.agreements[rec.agreement].sha256:
                return "the text accepted is not the agreement shown; show it again"
            if rec.kind == "transfer":
                old = self.rentals[self.transfers[rec.transfer].rental]
                amount = self.short(rec, now, old)
                if type(args.get("amount_cents")) is not int or args["amount_cents"] != amount:
                    return f"the amount to pay is now {money(amount)}; show it and accept again"
            return None
        if a == "confirm_payment":
            pay = b.payments.get(args.get("payment", ""))
            if pay is None or pay.tenant != rec.tenant:
                return "no such payment from this tenant"
            if pay.returned:
                return "that payment came back"
            if any(pay.id in r.payments for r in self.rentals.values()):
                return "that payment is already recorded for a rental"
            if rec.state == "cancelled":
                return LATE
            return None if rec.state in WAITING else f"the rental is {rec.state}"
        if a == "payment_failed":
            op = args.get("op")
            waiting = self.uncertain(rec) if op is None else op in rec.pay_ops and self.ops[op].how is None
            return None if waiting else "no payment for this rental is waiting for an answer"
        if a == "activate":
            if rec.state not in WAITING or rec.kind != "new":
                return "the rental is not waiting to start"
            if self.holder(rec.unit, now) != ("rental", rec.id):
                return "the rental no longer holds the unit"
            if not rec.accepted:
                return "the agreement is not accepted"
            if _d(rec.quote["start"]) > today:
                return "the move-in day has not come"
            if rec.lease and any(b.pending_return(src) for cid in b.of_lease.get(rec.lease, [])
                                 if not b.charges[cid].closed and b.charges[cid].due == b.leases[rec.lease].start
                                 for _, _, src in b.charges[cid].paid_effective):
                return "a payment for the move-in charges has a return waiting for staff"
            short = self.short(rec, now)
            return f"the move-in charges are {money(short)} short (money on the account pays older charges first)" if short else None
        if a == "request_transfer":
            if rec.state != "active":
                return "only an active rental can move"
            if self._open_transfer(rec.id) or rec.moveout:
                return "the rental already has a transfer or a move-out under way"
            if "until" in args and not staff:
                return f"only staff hold the new unit longer than {CHECKOUT.seconds // 60} minutes"
            if _d(args.get("start") or today.isoformat()) < today:
                return "the transfer day has passed"
            if args.get("unit") == rec.unit:
                return "the rental is already in that unit"
            return self._target_free(args.get("unit"), now)
        if a == "approve_transfer":
            if rec.state != "target_reserved":
                return f"the transfer is {rec.state.replace('_', ' ')}"
            if "until" in args and _t(args["until"]) <= now:
                return "the hold must end after now"
            return None if self.holder(rec.unit, now) == ("rental", rec.target) else "the new unit is no longer held"
        if a == "complete_transfer":
            new, old = self.rentals[rec.target], self.rentals[rec.rental]
            if rec.state != "approved":
                return f"the transfer is {rec.state.replace('_', ' ')}"
            if self.holder(rec.unit, now) != ("rental", new.id) or new.state not in WAITING:
                return "the new unit is no longer held"
            if not new.accepted:
                return "the agreement for the new unit is not accepted"
            if _d(new.quote["start"]) > today:
                return "the transfer day has not come"
            short = self.short(new, now, old)
            accepted = self.agreements[new.agreement].amount_cents
            if accepted is None:
                return "the transfer amount needs the tenant's acceptance"
            agreement = self.agreements[new.agreement]
            paid = min(accepted, sum(b.payments[p].cents for p in new.payments
                                     if p not in agreement.payments and not b.payments[p].returned))
            obligation = short + paid
            if accepted != obligation:
                return f"the amount to pay changed from {money(accepted)} to {money(obligation)}; the tenant accepts the new amount first"
            return f"the new unit's first charges are {money(short)} short after the old unit's unused days are credited" if short else None
        if a == "request_move_out":
            if rec.state != "active":
                return "only an active rental can move out"
            if self._open_transfer(rec.id) or rec.moveout:
                return "the rental already has a transfer or a move-out under way"
            on = _d(args.get("on") or today.isoformat())
            if on < today and not staff:
                return "only staff record a move-out day that has passed"
            return "the move-out day is before the rental began" if on < b.leases[rec.lease].start else None
        if a == "review_move_out":
            return None if rec.state == "requested" else f"the move-out is {rec.state.replace('_', ' ')}"
        if a == "final_bill":
            if rec.state != "reviewed":
                return "the move-out is not reviewed" if rec.state == "requested" else f"the move-out is {rec.state.replace('_', ' ')}"
            return None if today >= rec.on else "the move-out day has not come"
        if a == "confirm_access_cleared":
            if not staff:
                return "only staff confirm that a gate code is gone"
            return None if rec.state == "ended" and needs_clearance(rec) else "no gate code check is waiting for this rental"
        if a == "join_waitlist":
            waiting = any(e.tenant == act.tenant and e.size == act.about and e.state in ("waiting", "offered") for e in self.entries.values())
            return "the tenant is already on the waitlist for this size" if waiting else None
        if a == "offer_unit":
            if rec.state != "waiting":
                return f"the entry is {rec.state}"
            if "until" not in args or _t(args["until"]) <= now:
                return "an offer needs an end after now"
            return self._target_free(args.get("unit"), now, rec.size)
        if a == "accept_offer":
            return None if rec.state == "offered" and now < rec.expires_at else "the offer is no longer open"
        if a == "decline_offer":
            return None if rec.state == "offered" else "the offer is no longer open"
        if a == "retry_access":
            return None if rec.state in ("failed", "revocation_failed") else "the gate code is not waiting for a retry"
        if a == "confirm_refund":
            return "that refund is already confirmed" if rec["paid"] else None
        return None

    def touches(self, act: Act, now: datetime) -> list:
        """The charges a command would change, for the gate's holds check."""
        if act.action == "final_bill":
            m = self.moveouts[act.about]
            return [c for c, *_ in rent_changes(self.books, self.rentals[m.rental].lease, m.on)]
        if act.action == "complete_transfer":
            t = self.transfers[act.about]
            return [c for c, *_ in rent_changes(self.books, self.rentals[t.rental].lease, _d(self.rentals[t.target].quote["start"]) - timedelta(days=1))]
        return []

    # writes used by several commands ------------------------------------------------------------------------------------
    def task(self, led: Ledger, now: datetime, about: str, kind: str, reason: str, role: str = "front_desk", key: str | None = None) -> None:
        """A dated task on Today for staff; one open task per record, kind and key (a refund's key, where a record can owe several)."""
        if any(t.open and t.about == about and t.kind == kind and t.key == key for t in self.tasks.values()):
            return
        tid = self.new_id("RT", self.tasks)
        led.emit("rent_task.opened", now, {"task": tid, "about": about, "kind": kind, "reason": reason, "due": local(now).date().isoformat(),
                                      "role": role, **({"key": key} if key else {})}, f"task:{tid}", source="code")

    def close(self, led: Ledger, now: datetime, about: str, kind: str) -> None:
        for t in [t for t in self.tasks.values() if t.open and t.about == about and t.kind == kind]:
            led.emit("rent_task.closed", now, {"task": t.id}, f"task-closed:{t.id}", source="code")

    def close_key(self, led: Ledger, now: datetime, key: str) -> None:
        for t in [t for t in self.tasks.values() if t.open and t.key == key]:
            led.emit("rent_task.closed", now, {"task": t.id}, f"task-closed:{t.id}", source="code")

    def present(self, led: Ledger, now: datetime, rental: str) -> None:
        text = render(self.books, self.rentals[rental])
        aid = self.new_id("AG", self.agreements)
        led.emit("agreement.presented", now, {"agreement": aid, "rental": rental, "version": AGREEMENT_VERSION, "sha256": sha(text), "text": text},
                 f"agreement:{aid}", source="code")

    def request_access(self, led: Ledger, now: datetime, r: Rental) -> None:
        aid = self.new_id("AC", self.access)
        led.emit("access.requested", now, {"access": aid, "rental": r.id, "unit": r.unit, "groups": [f"unit {r.unit}", ACCESS_LEVEL]},
                 f"access:{aid}", source="code")

    def revoked(self, led: Ledger, now: datetime, a: Access) -> None:
        led.emit("access.revoked", now, {"access": a.id}, f"revoked:{a.id}", source="code")
        self.close(led, now, a.id, "revoke")
        self.close(led, now, a.id, "access")
        m = self.moveouts.get(self.rentals[a.rental].moveout or "")
        if m and m.state == "access_revocation_pending":
            led.emit("moveout.completed", now, {"moveout": m.id}, f"moved-out:{m.id}", source="code")

    def sweep(self, led: Ledger, now: datetime) -> None:
        """Write down every hold whose time ran out. A rental whose payment is unconfirmed, or that is paid for but not started,
        keeps its unit and gets a staff task instead; a paid hold ends only when the rental starts or staff refund and release it."""
        for r in [x for x in self.reservations.values() if x.state == "held" and now >= x.expires_at]:
            led.emit("reservation.expired", now, {"reservation": r.id}, f"expired:{r.id}", source="code")
        for e in [x for x in self.entries.values() if x.state == "offered" and now >= x.expires_at]:
            led.emit("waitlist.expired", now, {"entry": e.id}, f"expired:{e.id}", source="code")
        for r in [x for x in self.rentals.values() if x.state in WAITING and now >= x.expires_at]:
            if self.uncertain(r):
                self.task(led, now, r.id, "payment", f"The hold on unit {r.unit} ran out while a payment is unconfirmed; the unit stays held "
                                                     "until the processor answers.")
            elif r.lease and self.books.receivable(r.lease):
                self.task(led, now, r.id, "hold", f"Unit {r.unit} has a signed lease but its move-in charges are short. "
                                                  "The unit stays held until the charges are paid or staff release it.")
            elif self.paid(r):
                if r.accepted and _d(r.quote["start"]) > local(now).date():
                    continue        # signed and paid for a later day: it waits for that day, and that is no task
                way = "the rest of the move-in charges is paid" if r.accepted else "the renter accepts the agreement"
                self.task(led, now, r.id, "hold", f"Unit {r.unit} is paid for but the rental has not started, and its hold time is up. The "
                                                  f"unit stays held until {way}, which starts the rental, or staff refund the payment and "
                                                  "release the unit.")
            else:
                self.drop(led, now, r, "the hold ran out")
                if r.reservation:
                    led.emit("reservation.expired", now, {"reservation": r.reservation}, f"expired:{r.reservation}", source="code")

    def drop(self, led: Ledger, now: datetime, r: Rental, reason: str, source: str = "code") -> None:
        """Cancel a rental that never started. What is left of its payments as credit is set aside as a refund, pending until
        the processor confirms it went out, and staff get a task to make it."""
        p = {"rental": r.id, "reason": reason}
        if r.lease:
            for cid in list(self.books.of_lease[r.lease]):
                if not self.books.charges[cid].closed:
                    led.reverse(now, cid, "rental cancelled", source)
            led.end_lease(now, r.lease)
        if self.paid(r):
            left = sum(lot[1] for lot in self.books.lots.get(r.tenant, []) if lot[0] in r.payments)    # its payments' unspent credit
            cents = led.reserve_refund(now, r.tenant, left, f"refund:{r.id}", tuple(r.payments), source)
            if cents:
                p["refund"] = {"cents": cents, "state": "pending"}
        led.emit("rental.cancelled", now, p, f"cancelled:{r.id}", source=source)
        for kind in ("payment", "activation", "hold"):
            self.close(led, now, r.id, kind)
        if r.transfer:
            led.emit("transfer.cancelled", now, {"transfer": r.transfer}, f"cancelled:{r.transfer}", source=source)
        if "refund" in p:
            self.task(led, now, r.id, "refund", f"Unit {r.unit} was paid for but the rental never started ({reason}); refund "
                                                f"{money(p['refund']['cents'])}, which stays on the tenant's account until the refund is confirmed.",
                      key=f"refund:{r.id}")


class RentLedger(Ledger):
    """A ledger whose events also fold into the renting state, so a command sees its own effects, billing's included."""

    def __init__(self, books: Books, rent: Rent):
        super().__init__(books)
        self.rent = rent

    def emit(self, kind, at, payload, key, source="billing", effects=(), command=None):
        e = super().emit(kind, at, payload, key, source, effects, command)
        self.rent.apply(kind, at, payload)
        return e


def adopt_existing_lease(led: Ledger, lease: str, source: str, now: datetime) -> str:
    """Record an active lease as a rental without changing its account."""
    books = led.books
    record = books.leases.get(lease)
    if not record or record.tenant not in books.tenants or record.unit not in books.units or record.ended is not None:
        raise Refused("The active lease, tenant and unit must be on the books.")
    if source not in ("seeded_existing", "imported_existing"):
        raise Refused("The lease source is not known.")
    if not isinstance(led, RentLedger):      # the renting history must be folded in, or a lease's existing rental would be missed
        raise Refused("Adoption needs the copy's renting records.")
    rent = led.rent
    facility = books.facility.get("name", "")
    if not facility:
        raise Refused("The facility must be on the books.")
    key = "adopt:" + hashlib.sha256(json.dumps([facility, source, lease]).encode()).hexdigest()
    rid = "adopted-" + key.removeprefix("adopt:")
    if rid in rent.rentals:
        return rid
    if any(r.lease == lease for r in rent.rentals.values()):
        raise Refused("The lease already has a rental.")
    if rent.holder(record.unit, now):
        raise Refused("The unit already has a rental or hold.")
    if any(x.id != lease and x.unit == record.unit and x.ended is None for x in books.leases.values()):
        raise Refused("The unit has another active lease.")
    provenance = {"kind": source, "source_reference": lease, "snapshot": "current books", "adopted_at": now.isoformat(),
                  "agreement": "recorded_prior_to_staddle", "start": record.start.isoformat(),
                  "rent_cents": record.rent_cents, "schedule": list(record.schedule), "facility": facility}
    led.emit("rental.adopted", now, {"rental": rid, "tenant": record.tenant, "unit": record.unit,
                                    "lease": lease, "provenance": provenance}, key, source=source)
    return rid


def load(store: Store, stream: str) -> tuple[Books, Rent]:
    books = store.books(stream)
    rent = Rent(books)
    # ponytail: folds the stream's events on every call; keep a snapshot as store.books does once the log grows large
    for e in store.events(stream):
        rent.apply(e.kind, e.at, e.payload)
    return books, rent


def render(books: Books, r: Rental) -> str:
    q = r.quote
    fees = "".join(f" and the {k.replace('_', ' ')} of {money(v)}" for k, v in q["fees"].items())
    late = (f"A late fee of {money(LATE_FEE_CENTS)} is added when a month's rent is still unpaid at the start of the {CUTOFF_DAY}th.\n"
            if LATE_FEE in q["schedule"] else "")
    return AGREEMENT.format(version=AGREEMENT_VERSION, facility=books.facility.get("name", "The facility"), unit=r.unit,
                            name=books.tenants[r.tenant].name, start=_day(_d(q["start"])), rent=money(q["rent_cents"]),
                            first=money(q["first_cents"]), fees=fees, total=money(q["total_cents"]), late=late)


MONTHLY_RENT = re.compile(r"rent:[^:]+:(\d{4})-(\d{2})")     # the obligation of a month's rent, as billing posts it


def _rent_month(charge) -> tuple[int, int] | None:
    m = MONTHLY_RENT.fullmatch(charge.obligation or "")
    return (int(m[1]), int(m[2])) if m else None


def rent_changes(books: Books, lease: str, last_day: date) -> list[tuple]:
    """The lease's live monthly rent charges that ending the lease after `last_day` changes, each with its year, month and the days
    used and covered in it: every month after `last_day`, and its own month when part of it goes unused. One list for the gate's
    holds check and for the bill, so the check holds exactly what the bill would change. Rent posted by hand with no month is not
    in it (see untouched_rent): it names no period to prorate."""
    start = books.leases[lease].start
    out = []
    for i in books.of_lease.get(lease, []):
        ch = books.charges[i]
        ym = _rent_month(ch) if ch.kind == RENT and not ch.closed else None
        if ym is None or ym < (last_day.year, last_day.month):
            continue
        y, m = ym
        begin = start if (start.year, start.month) == (y, m) else date(y, m, 1)
        covered = calendar.monthrange(y, m)[1] - begin.day + 1
        used = max(0, min(covered, last_day.day - begin.day + 1)) if (last_day.year, last_day.month) == (y, m) else 0
        if used != covered:
            out.append((ch, y, m, used, covered))
    return out


def untouched_rent(books: Books, lease: str) -> list[dict]:
    """The lease's live rent charges that name no month (posted by hand): ending the lease leaves them as billed, and the final bill
    lists them so staff settle them."""
    return [{"charge": c.id, "cents": c.cents} for i in books.of_lease.get(lease, [])
            if (c := books.charges[i]).kind == RENT and not c.closed and _rent_month(c) is None]


def sign(led: Ledger, r: Rental, now: datetime) -> str:
    """Sign the rental's lease as quoted, then bill each later month that has begun by today, as the 1st's run would have."""
    q = r.quote
    start = _d(q["start"])
    lease = led.sign_lease(now, r.tenant, r.unit, q["rent_cents"], q["schedule"], start=start)
    if [led.books.charges[i].cents for i in led.books.of_lease[lease]] != [q["first_cents"], *q["fees"].values()]:
        raise RuntimeError("the move-in charges posted differ from the quote")
    today, m = local(now).date(), _next_month(start)
    while m <= today:
        led._charge(now, lease, RENT, q["rent_cents"], f"rent:{lease}:{month_key(m)}", m)
        m = _next_month(m)
    return lease


def end_lease(led: Ledger, lease: str, last_day: date, now: datetime, source: str) -> list[dict]:
    """End a lease after `last_day` through billing. Each month's rent from that month on is reversed and, where part of it was
    used, posted again for the Eastern calendar days used, rounded half up; money it released becomes credit."""
    b = led.books
    out = []
    for ch, y, m, used, covered in rent_changes(b, lease, last_day):
        days = calendar.monthrange(y, m)[1]
        keep = min(ch.cents, (b.leases[lease].rent_cents * used * 2 + days) // (2 * days))
        led.reverse(now, ch.id, "move-out", source)
        if keep:
            led._charge(now, lease, RENT, keep, ch.obligation, ch.due, key=f"final:{ch.id}")
        out.append({"month": f"{y:04d}-{m:02d}", "billed_cents": ch.cents, "kept_cents": keep})
    led.emit("lease.ended", now, {"lease": lease, "on": last_day.isoformat()}, f"lease-end:{lease}", source=source)
    return out


# the commands: each runs only after the gate allowed it, and returns the record it made, if any ----------------------------
def _price(rent, led, act, now):
    led.emit("unit.priced", now, {"unit": act.about, "rent_cents": act.args["rent_cents"], "size": act.args["size"]}, f"priced:{act.id}",
             source=act.source)


def _block(rent, led, act, now):
    bid = rent.new_id("BK", rent.blocks)
    led.emit("unit.blocked", now, {"block": bid, "unit": act.about, "reason": act.args["reason"]}, rent.claim_key(act.about), source=act.source)
    return bid


def _unblock(rent, led, act, now):
    led.emit("unit.unblocked", now, {"block": rent.holder(act.about, now)[1]}, f"unblocked:{act.id}", source=act.source)


def _reserve(rent, led, act, now):
    rid = rent.new_id("RS", rent.reservations)
    until = _t(act.args["until"]) if "until" in act.args else now + CHECKOUT
    start = _d(act.args["start"]) if "start" in act.args else local(now).date()
    led.emit("reservation.made", now, {"reservation": rid, "unit": act.about, "tenant": act.tenant, "expires_at": until.isoformat(),
                                       "start": start.isoformat(), "quote": rent.quote(act.about, start, SCHEDULE)},
             rent.claim_key(act.about), source=act.source)
    return rid


def _cancel(rent, led, act, now):
    led.emit("reservation.cancelled", now, {"reservation": act.about}, f"cancelled:{act.about}", source=act.source)
    r = rent._rental_of(act.about)
    if r:
        rent.drop(led, now, r, "the reservation was cancelled", act.source)


def _checkout(rent, led, act, now):
    res = rent.reservations[act.about]
    rid = rent.new_id("RN", rent.rentals)
    led.emit("reservation.checkout", now, {"reservation": res.id}, f"checkout:{res.id}", source=act.source)
    led.emit("rental.started", now, {"rental": rid, "tenant": res.tenant, "unit": res.unit, "quote": res.quote, "kind": "new", "claims": False,
                                     "expires_at": max(res.expires_at, now + CHECKOUT).isoformat(), "reservation": res.id},
             f"rental:{rid}", source=act.source)
    rent.present(led, now, rid)
    return rid


def _accept(rent, led, act, now):
    r = rent.rentals[act.about]
    led.emit("agreement.accepted", now, {"agreement": r.agreement, "rental": r.id, "by": f"renter:{act.tenant}", "sha256": act.args["sha256"],
                                         "amount_cents": act.args.get("amount_cents")},
             f"accepted:{r.agreement}:{act.id}", source=act.source)
    if r.kind == "new" and _d(r.quote["start"]) > local(now).date() and not rent.short(r, now):
        lease = sign(led, r, now)
        led.emit("rental.funded", now, {"rental": r.id, "lease": lease}, f"funded:{r.id}", source="code")


def _answer(rent, led, r, result, now, op=None):
    """Answer a checkout payment's waiting operation: the named one, or (a command that does not say which) the one still open. An
    operation already answered is never answered again, so a late success of an earlier payment cannot close a newer one."""
    op = op or next((o for o in r.pay_ops if rent.ops[o].how is None), None)
    if op is not None and rent.ops[op].how is None:
        led.emit("op.acknowledged", now, {"op": op, "step": "pay", "result": result, "how": "response"}, f"op:{op}:ack", source="code")


def _confirm(rent, led, act, now):
    r = rent.rentals[act.about]
    _answer(rent, led, r, {"payment": act.args["payment"]}, now, act.args.get("op"))
    led.emit("rental.paid", now, {"rental": r.id, "payment": act.args["payment"]}, f"paid:{r.id}:{act.args['payment']}", source=act.source)


def _failed(rent, led, act, now):
    r = rent.rentals[act.about]
    if not act.args.get("recorded"):        # an answer from the processor path was recorded as a decline when its intent settled
        led.decline(now, r.tenant, r.quote["total_cents"])
    _answer(rent, led, r, {"declined": act.args.get("reason", "declined")}, now, act.args.get("op"))


def _activate(rent, led, act, now):
    r = rent.rentals[act.about]
    lease = r.lease or sign(led, r, now)
    led.emit("rental.activated", now, {"rental": r.id, "lease": lease}, f"active:{r.id}", source="code")
    if r.reservation:
        led.emit("reservation.confirmed", now, {"reservation": r.reservation}, f"confirmed:{r.reservation}", source="code")
    rent.request_access(led, now, r)
    for kind in ("payment", "activation", "hold"):
        rent.close(led, now, r.id, kind)


def _transfer(rent, led, act, now):
    old = rent.rentals[act.about]
    unit = act.args["unit"]
    start = _d(act.args.get("start") or local(now).date().isoformat())
    until = _t(act.args["until"]) if "until" in act.args else now + CHECKOUT
    schedule = [k for k in rent.books.leases[old.lease].schedule if k != ADMIN_FEE]     # a transfer carries no new admin fee
    tid, rid = rent.new_id("TR", rent.transfers), rent.new_id("RN", rent.rentals)
    led.emit("rental.started", now, {"rental": rid, "tenant": old.tenant, "unit": unit, "quote": rent.quote(unit, start, schedule), "kind": "transfer",
                                     "claims": True, "expires_at": until.isoformat(), "transfer": tid}, rent.claim_key(unit), source=act.source)
    led.emit("transfer.requested", now, {"transfer": tid, "rental": old.id, "target": rid, "unit": unit}, f"transfer:{tid}", source=act.source)
    rent.present(led, now, rid)
    return tid


def _approve(rent, led, act, now):
    p = {"transfer": act.about, **({"expires_at": act.args["until"]} if "until" in act.args else {})}
    led.emit("transfer.approved", now, p, f"approved:{act.about}", source=act.source)


def _complete(rent, led, act, now):
    t = rent.transfers[act.about]
    old, new = rent.rentals[t.rental], rent.rentals[t.target]
    end_lease(led, old.lease, _d(new.quote["start"]) - timedelta(days=1), now, act.source)
    lease = sign(led, new, now)
    # only code starts a rental: the new unit's start is code's decision, recorded as such, inside the staff's transfer
    led.emit("rental.activated", now, {"rental": new.id, "lease": lease}, f"active:{new.id}", source="code")
    start = Act(f"{new.id}:activate", "activate", "code", new.id)
    _record(led, start, Decision(True, "activate", "the new unit starts as the old one ends, paid from the account", command=start.id), now)
    # the old unit and its code stay with the tenant until the new code is read back
    led.emit("rental.ending", now, {"rental": old.id, "reason": "transfer"}, f"ending:{old.id}", source=act.source)
    rent.request_access(led, now, new)


def _move_out(rent, led, act, now):
    r = rent.rentals[act.about]
    mid = rent.new_id("MO", rent.moveouts)
    on = act.args.get("on") or local(now).date().isoformat()
    led.emit("moveout.requested", now, {"moveout": mid, "rental": r.id, "on": on}, f"moveout:{mid}", source=act.source)
    led.emit("rental.ending", now, {"rental": r.id, "reason": "move-out"}, f"ending:{r.id}", source=act.source)
    return mid


def _review(rent, led, act, now):
    led.emit("moveout.reviewed", now, {"moveout": act.about}, f"reviewed:{act.about}", source=act.source)


def _final_bill(rent, led, act, now):
    m = rent.moveouts[act.about]
    r, b = rent.rentals[m.rental], rent.books
    lines = end_lease(led, r.lease, m.on, now, act.source)
    others = any(x.tenant == r.tenant and x.ended is None for x in b.leases.values())
    # credit stays on the account while the tenant rents another unit; otherwise it is set aside as a refund, pending until the
    # processor confirms it went out, so nothing else can spend it
    refund = 0 if others else led.reserve_refund(now, r.tenant, b.credit.get(r.tenant, 0), f"refund:{m.id}", source=act.source)
    if refund:
        rent.task(led, now, r.id, "refund", f"Unit {r.unit} was moved out of; refund {money(refund)}, the credit left after the final bill, "
                                            "set aside on the tenant's account until the refund is confirmed.", key=f"refund:{m.id}")
    bill = {"last_day": m.on.isoformat(), "rent": lines, "owed_cents": b.receivable(r.lease), "credit_cents": b.credit.get(r.tenant, 0),
            "refund_cents": refund, "refund": "pending" if refund else "none", "untouched_rent": untouched_rent(b, r.lease)}
    led.emit("moveout.billed", now, {"moveout": m.id, "bill": bill}, f"billed:{m.id}", source=act.source)
    led.emit("rental.ended", now, {"rental": r.id}, f"ended:{r.id}", source=act.source)
    if r.access:
        led.emit("moveout.revoking", now, {"moveout": m.id}, f"revoking:{m.id}", source="code")
        led.emit("access.revoke_requested", now, {"access": r.access}, f"revoke:{r.access}", source="code")
    elif needs_clearance(r):        # a code from before Staddle may still work: the unit waits for staff, never assumed clear
        led.emit("moveout.clearing", now, {"moveout": m.id}, f"clearing:{m.id}", source="code")
        clearance_task(rent, led, now, r)
    else:
        led.emit("moveout.completed", now, {"moveout": m.id}, f"moved-out:{m.id}", source="code")


def needs_clearance(r: Rental) -> bool:
    """A rental from before Staddle with no gate code on record here: after it ends, staff check the gate system before the unit is free."""
    return r.provenance is not None and r.access is None and not r.cleared


def clearance_task(rent, led, now, r: Rental) -> None:
    rent.task(led, now, r.id, "clearance", f"Unit {r.unit} was left, and its gate code from before Staddle is not on record here. Check the "
                                           "gate system, remove any code for this unit, then confirm it is gone.")


def _confirm_cleared(rent, led, act, now):
    r = rent.rentals[act.about]
    led.emit("rental.access_cleared", now, {"rental": r.id}, f"cleared:{r.id}", source=act.source)
    rent.close(led, now, r.id, "clearance")
    m = rent.moveouts.get(r.moveout or "")
    if m and m.state == "access_clearance_pending":
        led.emit("moveout.completed", now, {"moveout": m.id}, f"moved-out:{m.id}", source="code")


def _join(rent, led, act, now):
    wid = rent.new_id("WL", rent.entries)
    led.emit("waitlist.joined", now, {"entry": wid, "tenant": act.tenant, "size": act.about}, f"waitlist:{wid}", source=act.source)
    return wid


def _offer(rent, led, act, now):
    unit = act.args["unit"]
    led.emit("waitlist.offered", now, {"entry": act.about, "unit": unit, "expires_at": act.args["until"]}, rent.claim_key(unit), source=act.source)


def _accept_offer(rent, led, act, now):
    e = rent.entries[act.about]
    rid, start = rent.new_id("RS", rent.reservations), local(now).date()
    led.emit("waitlist.accepted", now, {"entry": e.id, "reservation": rid, "unit": e.unit, "tenant": e.tenant, "expires_at": e.expires_at.isoformat(),
                                        "start": start.isoformat(), "quote": rent.quote(e.unit, start, SCHEDULE)}, f"accepted:{e.id}", source=act.source)
    return rid


def _decline(rent, led, act, now):
    led.emit("waitlist.declined", now, {"entry": act.about}, f"declined:{act.about}", source=act.source)


def _confirm_refund(rent, led, act, now):
    led.refund_paid(now, act.about, act.source)
    rent.close_key(led, now, act.about)


def _retry(rent, led, act, now):
    led.emit("access.retry", now, {"access": act.about}, f"retry:{act.id}", source=act.source)
    rent.close(led, now, act.about, "access")
    rent.close(led, now, act.about, "revoke")


DO = {"price_unit": _price, "block_unit": _block, "unblock_unit": _unblock, "reserve": _reserve, "cancel_reservation": _cancel,
      "start_checkout": _checkout, "accept_agreement": _accept, "confirm_payment": _confirm, "payment_failed": _failed, "activate": _activate,
      "request_transfer": _transfer, "approve_transfer": _approve, "complete_transfer": _complete, "request_move_out": _move_out,
      "review_move_out": _review, "final_bill": _final_bill, "join_waitlist": _join, "offer_unit": _offer, "accept_offer": _accept_offer,
      "decline_offer": _decline, "retry_access": _retry, "confirm_refund": _confirm_refund,
      "confirm_access_cleared": _confirm_cleared}


def _record(led: Ledger, act: Act, dec: Decision, now: datetime) -> None:
    led.emit("gate.decided", now, {**asdict(act), "command": act.id, "allowed": dec.allowed, "check": dec.check, "made": dec.made},
             f"gate:{act.id}", source=act.source, command=act.id)


def _try_activate(rent: Rent, led: Ledger, rid: str, now: datetime) -> None:
    """Try to start an accepted rental using the money on the account."""
    r = rent.rentals[rid]
    act = Act(f"{rid}:activate", "activate", "code", rid)
    if act.id in rent.books.decisions or r.kind != "new" or r.state not in WAITING or not r.accepted:
        return
    if _d(r.quote["start"]) > local(now).date():
        return                      # paid and signed for a later day: it starts on that day (advance tries again), and that is no task
    dec = evaluate_rent(rent, rent.books, act, now)
    if dec.allowed:
        _activate(rent, led, act, now)
        _record(led, act, dec, now)
    else:
        if r.payments or rent.books.credit.get(r.tenant, 0):
            reason = f"Unit {r.unit} has an accepted agreement, but the rental did not start: {dec.check}."
            if any(t.open and t.about == rid and t.kind == "activation" and t.reason != reason for t in rent.tasks.values()):
                rent.close(led, now, rid, "activation")
            rent.task(led, now, rid, "activation", reason)


def run(store: Store, stream: str, act: Act, now: datetime) -> Decision:
    """Check and record one renting command in one write transaction. A command id already decided returns that decision and
    writes nothing; holds whose time ran out are written down first."""
    with store.write():
        books, rent = load(store, stream)
        if act.id in books.decisions:
            p = books.decisions[act.id]
            return Decision(p["allowed"], p["action"], p["check"], command=act.id, repeated=True, made=p.get("made"))
        led = RentLedger(books, rent)
        rent.sweep(led, now)
        dec = evaluate_rent(rent, books, act, now)
        if dec.allowed:
            dec.made = DO[act.action](rent, led, act, now)
        elif act.action == "complete_transfer" and (dec.check.startswith("the amount to pay changed from ")
                                                    or dec.check == "the transfer amount needs the tenant's acceptance"):
            transfer = rent.transfers[act.about]
            new, old = rent.rentals[transfer.target], rent.rentals[transfer.rental]
            led.emit("agreement.requoted", now, {"rental": new.id, "amount_cents": rent.short(new, now, old)},
                     f"requote:{act.id}", source="code")
        elif dec.check == LATE:
            pay = books.payments[act.args["payment"]]
            cents = led.reserve_refund(now, pay.tenant, pay.cents, f"refund:{pay.id}", (pay.id,), "code")
            if cents:
                rent.task(led, now, act.about, "refund", f"A payment came in after the hold on unit {rent.rentals[act.about].unit} ran out; "
                                                         f"refund {money(cents)}, set aside on the tenant's account until the refund is confirmed.",
                          key=f"refund:{pay.id}")
            else:                   # older charges took all of it: nothing is set aside, there is no task, and the record says so
                dec.check = LATE_SPENT
        _record(led, act, dec, now)
        if dec.allowed and act.action in ("accept_agreement", "confirm_payment"):
            _try_activate(rent, led, act.about, now)
        dec.events = [e.id for e in store.append(stream, led.pending) if e.id is not None]
        return dec


def checkout_intent(store: Store, stream: str, rental: str, now: datetime) -> str:
    """Record the intent to take a rental's payment, before the processor is called. The id returned is the processor's
    idempotency key; asking again while the answer is out returns the same id, so a retried call cannot charge twice."""
    with store.write():
        books, rent = load(store, stream)
        r = rent.rentals[rental]
        out = next((o for o in r.pay_ops if rent.ops[o].how is None), None)
        if out:
            return out
        if r.state not in WAITING:
            raise Refused(f"the rental is {r.state}")
        if rent.holder(r.unit, now) != ("rental", r.id):
            raise Refused(NO_HOLD)
        op = f"{r.id}:pay:{len(r.pay_ops) + 1}"
        led = RentLedger(books, rent)
        led.emit("op.requested", now, {"op": op, "rental": r.id, "step": "pay", "sha256": sha(f"{r.tenant}:{r.quote['total_cents']}")},
                 f"op:{op}", source="code")
        store.append(stream, led.pending)
        return op


def pay_rental(store: Store, stream: str, processor: delinquency.Processor, rental: str, card: str, now: datetime) -> delinquency.Intent:
    """A renter's move-in payment through the processor, the path every payment takes. The rental's payment record and the
    processor's intent are written in one step before any request goes out, and the request carries the intent's key, so a retry
    after a lost answer or a crash sends the same key and gets the same payment back, never a second. The answer then drives the
    rental (settle_payments), at once or when a webhook or the daily check brings it."""
    with store.write():
        books, rent = load(store, stream)
        r = rent.rentals.get(rental)
        if r is None:
            raise Refused("no such rental")
        op = next((o for o in r.pay_ops if rent.ops[o].how is None), None)
        if op is None:
            if r.state not in WAITING:
                raise Refused(f"the rental is {r.state}")
            if rent.holder(r.unit, now) != ("rental", r.id):
                raise Refused(NO_HOLD)          # a hold whose time is up, not yet written down by a pass, takes no new payment
            if delinquency.unsettled(books, r.tenant):
                raise Refused("an earlier payment's outcome is not known yet; it is checked with the processor, never paid again")
            old = rent.rentals[rent.transfers[r.transfer].rental] if r.transfer else None
            cents = rent.short(r, now, old)
            if not cents:
                raise Refused("the rental is already paid for")
            led = RentLedger(books, rent)
            iid = delinquency.open_intent(led, stream, processor, r.tenant, cents, now)
            op = f"{r.id}:pay:{len(r.pay_ops) + 1}"
            led.emit("op.requested", now, {"op": op, "rental": r.id, "step": "pay", "intent": iid, "sha256": sha(f"{r.tenant}:{cents}")},
                     f"op:{op}", source="code")
            store.append(stream, led.pending)
        elif rent.ops[op].intent is None:
            raise Refused("a payment for this rental is already out, started without the processor")
        else:
            iid = rent.ops[op].intent
    delinquency.send(store, stream, processor, iid, card, now)
    settle_payments(store, stream, now)
    return delinquency.state(store.books(stream)).intents[iid]


def check_codes(store: Store, stream: str, now: datetime) -> int:
    """The daily check of gate codes against rentals, from the copy's own records: an active rental whose code is not verified,
    and a code still standing for a rental that ended, each become a dated staff task. They use the kinds a failed read-back and a
    failed revocation already use, so a problem that has its task gets no second one and the task closes when the code is read
    back or revoked. A pass that finds nothing new writes nothing. Returns the tasks opened."""
    with store.write():
        books, rent = load(store, stream)
        led = RentLedger(books, rent)
        for r in rent.rentals.values():
            a = rent.access.get(r.access or "")
            if r.state in ("active", "ending") and (a is not None or not r.provenance) and (a is None or a.state != "verified"):
                rent.task(led, now, a.id if a else r.id, "access", f"Unit {r.unit} is rented, but its gate code is not confirmed working: "
                                                                  "it has not been read back from the access system.")
            elif r.state == "ended" and a is not None and a.state != "revoked":
                rent.task(led, now, a.id, "revoke", f"The code for unit {r.unit} is still standing after its rental ended; it is not "
                                                    "revoked on the access system.")
        store.append(stream, led.pending)
        return len(led.pending)


def settle_payments(store: Store, stream: str, now: datetime) -> None:
    """Carry each rental's checkout payments forward from what the processor said, as the copy's books record it: a succeeded
    intent confirms the payment, a failed or closed (never received) or returned one answers the waiting operation. Each command
    has an id of its own per payment, so running this after every way an answer can arrive (the reply, a webhook, the daily
    check) does each thing once; an answer the gate refused stays refused and is not asked again. An intent that was ever failed or
    closed answers the wait as declined before a success is confirmed, so a success that replaces the close before this ran is
    judged as a late payment, not taken as the first answer."""
    books, rent = load(store, stream)
    intents = delinquency.state(books).intents
    acts = []
    for r in rent.rentals.values():
        for op in r.pay_ops:
            i = intents.get(rent.ops[op].intent or "")
            if i is None:
                continue
            if rent.ops[op].how is None and (i.state in ("failed", "closed", "returned") or i.id in rent.failed_intents):
                said = i.state if i.state in ("failed", "closed", "returned") else "closed"
                acts.append(Act(f"{op}:failed", "payment_failed", "code", r.id, args={"reason": f"the processor says {said}", "recorded": True, "op": op}))
            if i.state == "succeeded" and i.payment not in r.payments:
                acts.append(Act(f"{op}:paid", "confirm_payment", "code", r.id, args={"payment": i.payment, "op": op}))
    for a in acts:
        if a.id not in books.decisions:
            run(store, stream, a, now)


# the gate's access system --------------------------------------------------------------------------------------------------
class AccessSystem(Protocol):
    """What Staddle needs from a facility's gate access system. A call that changes something takes its operation id, with the copy's
    mark in front of it (copy_mark), as `key`, and find(key) returns what an earlier call with that key did (None if it never
    happened), so a retry after a lost answer never issues twice. A real adapter finds by the key it stored on the user or
    credential, and needs a way to remove a reset copy's users, as StandIn.forget does."""

    def create_user(self, key: str, name: str) -> dict: ...                 # {"user": id}
    def add_to_group(self, key: str, user: str, group: str) -> dict: ...    # {"group": group}
    def issue_pin(self, key: str, user: str) -> dict: ...                   # {"credential": id, "code": "123456"}
    def revoke(self, key: str, user: str) -> dict: ...                      # {"revoked": user}
    def read_back(self, user: str) -> dict: ...                             # {"code": str | None, "groups": [..]}
    def find(self, key: str) -> dict | None: ...


class Crash(Exception):
    """The process stopped between two steps, as on a restart."""


class OutsideSystem:
    """What every simulated outside system shares: answers kept by call key in a file, so a restart finds them, `crash_before` and
    `crash_after` naming steps that stop the process once around a call, and the per-copy cleanup. A copy's records all begin with
    its mark (copy_mark), so a copy that is reset or removed has them noted before it goes (note), forgotten after (forget), or kept
    after all (release); the note stays on file until the forgetting is saved, so a fault halfway is finished by a later pass. A
    subclass keeps its own records: _load reads them from the saved file, _dump gives them back, _drop removes a copy's."""
    simulated = True

    def __init__(self, crash_before=(), crash_after=(), path: str | None = None):
        self.crash_before, self.crash_after = set(crash_before), set(crash_after)
        self.path = path
        saved = {}
        if path and os.path.exists(path):
            with open(path) as f:
                saved = json.load(f)
        self.done: dict[str, dict] = saved.get("done", {})
        self.calls: list[str] = saved.get("calls", [])      # the key of every changing call made, repeats included
        self.pending: list[str] = saved.get("pending", [])  # copies whose records are to be forgotten: noted here before the copy goes
        self._load(saved)

    def _load(self, saved: dict) -> None: ...

    def _dump(self) -> dict:
        return {}

    def _drop(self, prefix: str) -> None: ...

    def _save(self) -> None:
        if self.path:
            with open(self.path + ".tmp", "w") as f:
                json.dump({**self._dump(), "done": self.done, "calls": self.calls, "pending": self.pending}, f)
            os.replace(self.path + ".tmp", self.path)       # whole or not at all

    def note(self, prefix: str) -> None:
        """Write down, before a copy goes, that its records are to be forgotten. If the note cannot be written this raises and the copy
        is not dropped; once it is on file, a restart or a later pass finishes the forgetting even if it failed halfway."""
        if prefix not in self.pending:
            self.pending.append(prefix)
            try:
                self._save()
            except Exception:
                self.pending.remove(prefix)
                raise

    def release(self, prefix: str) -> None:
        """The copy was not dropped after all: its records stay and the note goes."""
        if prefix in self.pending:
            self.pending.remove(prefix)
            self._save()

    def forget(self, prefix: str) -> None:
        """A copy was reset: what it made and the answers kept for its keys go, so nothing of it is left here. Safe to repeat; the
        note stays on file until the forgetting is saved."""
        self._drop(prefix)
        self.done = {k: v for k, v in self.done.items() if not k.startswith(prefix)}
        had = prefix in self.pending
        if had:
            self.pending.remove(prefix)
        try:
            self._save()
        except Exception:
            if had:
                self.pending.append(prefix)
            raise

    def _do(self, key: str, go) -> dict:
        step = key.rsplit(":", 1)[1]
        if step in self.crash_before:
            self.crash_before.discard(step)
            raise Crash(f"before {step}")
        out = go()
        self.calls.append(key)
        self.done[key] = out
        self._save()
        if step in self.crash_after:
            self.crash_after.discard(step)
            raise Crash(f"after {step}")
        return out

    def find(self, key):
        return self.done.get(key)


class StandIn(OutsideSystem):
    """A deterministic access system for tests and for when no real one is connected; every record made through it says it is a
    stand-in. It does not remove repeats by itself: a second call with the same key issues again, so only Staddle's own operation
    records prevent a repeat. `hide` names groups a read-back fails to show. Given a path, its users, answers and calls are kept in
    that file, as the simulated payment processor keeps its payments, so a restart finds them and nothing is issued twice."""

    def __init__(self, crash_before=(), crash_after=(), hide=(), path: str | None = None):
        self.hide = set(hide)
        super().__init__(crash_before, crash_after, path)

    def _load(self, saved):
        self.users: dict[str, dict] = {u: {**v, "groups": set(v["groups"])} for u, v in saved.get("users", {}).items()}
        self._n = saved.get("n", 0)                         # users made so far, so a forgotten user's id is never used again

    def _dump(self):
        return {"users": {u: {**v, "groups": sorted(v["groups"])} for u, v in self.users.items()}, "n": self._n}

    def _drop(self, prefix):
        for user in [u for u, v in self.users.items() if v["key"].startswith(prefix)]:
            del self.users[user]

    def create_user(self, key, name):
        def go():
            self._n += 1
            user = f"user-{self._n}"
            self.users[user] = {"name": name, "groups": set(), "code": None, "credential": None, "key": key}
            return {"user": user}
        return self._do(key, go)

    def add_to_group(self, key, user, group):
        return self._do(key, lambda: self.users[user]["groups"].add(group) or {"group": group})

    def issue_pin(self, key, user):
        def go():
            u = self.users[user]
            u["code"], u["credential"] = f"{int(sha(key), 16) % 10 ** 6:06d}", f"cred-{len(self.calls) + 1}"
            return {"credential": u["credential"], "code": u["code"]}
        return self._do(key, go)

    def revoke(self, key, user):
        return self._do(key, lambda: self.users[user].update(groups=set(), code=None, credential=None) or {"revoked": user})

    def read_back(self, user):
        u = self.users.get(user, {"groups": set(), "code": None})
        return {"code": u["code"], "groups": sorted(u["groups"] - self.hide)}


def _next(rent: Rent, a: Access) -> str | None:
    """The access record's next outside step, or None when it rests (verified, failed or revoked, waiting for staff)."""
    done = lambda step: (op := rent.ops.get(_op_id(a, step))) is not None and op.how is not None
    if a.state == "revocation_pending":
        user = rent.ops.get(f"{a.id}:user")
        if user is not None and user.how is None:
            return "settle"                 # a user may have been made and its answer lost: look it up, never make another
        if a.user is None:
            return "nothing"                # no user was ever made, so there is nothing to revoke
        return "read" if done("revoke") else "revoke"
    if a.state in ("requested", "issuing"):
        return next((s for s in STEPS if not done(s)), "read")
    return "read" if a.state == "issued_unverified" else None


def _op_id(a: Access, step: str) -> str:
    if step == "settle":
        return f"{a.id}:user"
    if step == "revoke" and a.revokes:
        return f"{a.id}:retry{a.revokes}:revoke"        # a staff retry asks the access system again, under a key of its own
    return f"{a.id}:read:{a.reads + 1}" if step == "read" else f"{a.id}:{step}"


def _call(system: AccessSystem, step: str, key: str, a: Access, name: str) -> dict:
    if step == "user":
        return system.create_user(key, name)
    if step in ("unit_group", "level_group"):
        return system.add_to_group(key, a.user, a.groups[0 if step == "unit_group" else 1])
    if step == "pin":
        return system.issue_pin(key, a.user)
    if step == "revoke":
        return system.revoke(key, a.user)
    return system.read_back(a.user)


def _clean(step: str, result: dict | None) -> dict | None:
    """What is kept of an answer: never the code itself, only its fingerprint."""
    if result is None:
        return None
    if step == "pin":
        return {"credential": result["credential"], "code_sha256": sha(result["code"])}
    if step == "read":
        return {"code_sha256": result["code"] and sha(result["code"]), "groups": sorted(result["groups"])}
    return dict(result)


def _after_read(rent: Rent, led: Ledger, a: Access, op: str, result: dict, now: datetime) -> None:
    key = f"read:{op}"
    if a.state == "revocation_pending":
        ok = result["code_sha256"] is None and not result["groups"]
        led.emit("access.read", now, {"access": a.id, "ok": ok, "revoking": True}, key, source="code")
        if ok:
            rent.revoked(led, now, a)
        else:
            rent.task(led, now, a.id, "revoke", f"The old code for unit {a.unit} still shows on the access system after it was revoked.")
        return
    ok = result["code_sha256"] == a.code_sha256 and set(a.groups) <= set(result["groups"])
    led.emit("access.read", now, {"access": a.id, "ok": ok}, key, source="code")
    r = rent.rentals[a.rental]
    t = rent.transfers.get(r.transfer or "")
    if not ok:
        rent.task(led, now, a.id, "access", f"The code for unit {a.unit} was issued but the access system does not show it with both groups; "
                                            "the rental stays active and the code is not counted as working yet.")
        if t and t.state == "approved":
            led.emit("transfer.review", now, {"transfer": t.id}, f"review:{op}", source="code")
        return
    rent.close(led, now, a.id, "access")
    if t and t.state in ("approved", "review"):
        # the new code works: only now does the old unit end and its code go
        old = rent.rentals[t.rental]
        led.emit("transfer.completed", now, {"transfer": t.id}, f"transferred:{t.id}", source="code")
        led.emit("rental.ended", now, {"rental": old.id}, f"ended:{old.id}", source="code")
        if old.access:
            led.emit("access.revoke_requested", now, {"access": old.access}, f"revoke:{old.access}", source="code")
        elif needs_clearance(old):
            clearance_task(rent, led, now, old)


def copy_mark(store: Store, stream: str) -> str:
    """The copy's name with a mark of when it began (its first event's time, the first by id so a clock that steps back cannot move
    it), which is how the access system tells it from an earlier copy that had the same name: a reset copy, or a new day's, comes back
    with its access ids numbered from AC0001 again."""
    first = store.db.execute("SELECT recorded_at FROM events WHERE stream = ? ORDER BY id LIMIT 1", (stream,)).fetchone()
    return mark_of(stream, first[0] if first else "")


def mark_of(stream: str, born: str) -> str:
    return f"{stream}@{hashlib.sha256(born.encode()).hexdigest()[:8]}"


def copy_marks_in(path: str) -> list[str]:
    """The mark of every visitor copy in a database file, read without opening it for writing: the copies that go when the file is
    deleted, so their codes can be forgotten with it."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT stream, (SELECT recorded_at FROM events x WHERE x.stream = e.stream ORDER BY id LIMIT 1) "
                          "FROM events e WHERE stream != ? GROUP BY stream", (BASE,)).fetchall()
    finally:
        db.close()
    return [mark_of(stream, born) for stream, born in rows]


def drive(store: Store, stream: str, access: str, system: AccessSystem, now: datetime) -> str:
    """Carry one gate code record forward, one outside call at a time. Each call sits between two write transactions: the
    operation is recorded before it, and its answer after. A retry after a crash looks the operation up before calling again."""
    while True:
        with store.write():
            books, rent = load(store, stream)
            a = rent.access[access]
            step = _next(rent, a)
            if step is None:
                return a.state
            before = a.state
            led = RentLedger(books, rent)
            if step == "nothing":
                rent.revoked(led, now, a)
                store.append(stream, led.pending)
                continue
            key = _op_id(a, step)
            op = rent.ops.get(key)
            retry = op is not None
            if op is None:
                led.emit("op.requested", now, {"op": key, "access": a.id, "step": step, "sha256": sha(f"{key}:{a.user}:{a.groups}")}, f"op:{key}",
                         source="code")
            if step != "settle":
                n = (op.attempts if op else 0) + 1
                led.emit("op.attempted", now, {"op": key, "n": n}, f"op:{key}:try:{n}", source="code")
            store.append(stream, led.pending)
            name = f"{books.tenants[rent.rentals[a.rental].tenant].name}, unit {a.unit}"
        # the outside call, with no write lock held; its key carries the copy's mark, as a payment's carries the copy's nonce, since
        # every copy numbers its own access records from AC0001
        ext = f"{copy_mark(store, stream)}:{key}"
        # ponytail: two advance() runs at once can both take up an op whose answer is still out, and find() sees nothing until the
        # first call lands, so both call; give each op a short lease (taken by, until) if workers ever overlap
        step_done = "user" if step == "settle" else step
        found = system.find(ext) if retry and step != "read" else None
        if step == "settle" or found is not None:
            result, how = found, "found" if found is not None else "not_found"
        else:
            result, how = _call(system, step, ext, a, name), "response"
        with store.write():
            books, rent = load(store, stream)
            if rent.ops[key].how is not None:
                continue                    # another worker recorded the answer first
            led = RentLedger(books, rent)
            led.emit("op.acknowledged", now, {"op": key, "step": step_done, "result": _clean(step_done, result), "how": how,
                                              "simulated": bool(getattr(system, "simulated", False))}, f"op:{key}:ack", source="code")
            # a read-back answers the question asked when it was sent; if the record moved on meanwhile (a move-out asked for
            # revocation), the answer is kept but decides nothing, and the next step is worked out again from the new state
            if step == "read" and rent.access[access].state == before:
                _after_read(rent, led, rent.access[access], key, rent.ops[key].result, now)
            store.append(stream, led.pending)


def advance(store: Store, stream: str, system: AccessSystem, now: datetime) -> dict[str, str]:
    """Do every piece of unfinished renting work once: write down holds whose time ran out, then carry each gate code that
    needs it forward. After a crash, running it again finishes the work and repeats nothing."""
    states: dict[str, str] = {}
    failed: dict[str, Exception] = {}       # records that failed this pass: not tried again in it
    while True:
        with store.write():
            books, rent = load(store, stream)
            led = RentLedger(books, rent)
            rent.sweep(led, now)
            for rid in [x.id for x in rent.rentals.values() if x.state in WAITING and x.accepted and (x.payments or x.lease)]:
                _try_activate(rent, led, rid, now)      # a rental signed and paid for a later day starts when the day comes
            store.append(stream, led.pending)
            # drive() stops only at a resting state, so any record still in a working state has work left
            todo = [a.id for a in rent.access.values() if a.id not in failed
                    and a.state in ("requested", "issuing", "issued_unverified", "revocation_pending")]
        if not todo:
            break
        for a in todo:
            try:
                states[a] = drive(store, stream, a, system, now)
            except Crash:
                raise                       # the process stopped: nothing after it runs
            except Exception as e:          # one record's fault stops neither the others nor this pass from ending
                log.error("gate code work failed for %s in %s: %s", a, stream, type(e).__name__, exc_info=True)
                failed[a] = e
    if failed:
        raise next(iter(failed.values()))       # reported after the others were carried through, so the caller logs and retries next pass
    return states
