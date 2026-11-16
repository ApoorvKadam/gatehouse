"""Our agent working tenant messages on the facility's own books (the core in core/), for the FMS pages in desk/fms.py.

Each signed-in visitor works on a scratch copy of the generated year (their own stream), so one visitor's reversal never changes
what the next one sees. A message is read by our agent; code checks every claim against the records and decides; the gate
records it. Runs count against the visitor's tries and the day's budget."""
from __future__ import annotations

import glob
import os
import secrets
import threading
from collections import OrderedDict
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import BaseModel, Field

from core.books import ZONE, at_local, local
from core.case import READ_ONLY, handle
from core.cases import load
from core.seed import build
from core.store import BASE, Store

from . import spend

SHIFT = load("shift")["items"]
WORST_CASE_CENTS = 0.1          # the most one message can cost on the live link (one reading call on Haiku, capped)
KEEP_FOR = timedelta(hours=24)
ACTION_WORDS = {"explain_charge": "Explained from the records", "reverse_duplicate": "Reversed the second copy",
                "correct_fee_posted_in_error": "Removed as posted in error", "open_dispute": "Dispute recorded for the office manager",
                "waive_fee": "Waived under the owner's rule", "hand_to_staff": "Handed to staff"}
CLAIM_WORDS = {"paid_on_time": "says they paid in time", "charged_twice": "says a charge appears twice", "never_agreed": "says they never agreed to it",
               "challenges_record": "challenges a record", "wants_waiver": "asks for it to be waived", "wrong_amount": "says the amount is wrong",
               "what_is_this": "asks what it is", "payment_issue": "asks about their card payments", "other": "says something else"}


def account_words(account: str, books) -> str:
    kind, _, ident = account.partition(":")
    if kind == "receivable":
        return f"Owed by the tenant, unit {books.leases[ident].unit}"
    if kind == "credit":
        return "Held for the tenant as credit"
    return {"income:rent": "Rent income", "income:fees": "Fee income", "contra:waivers": "Waivers given",
            "clearing:card": "Card payments on the way"}.get(account, account)


class CaseIn(BaseModel):
    tenant: str = Field(max_length=20)
    text: str = Field(min_length=1, max_length=600)
    shift: str | None = Field(default=None, max_length=8)      # a shift message's id, when the page runs the shift
    run: str | None = Field(default=None, max_length=40)       # one shift run is one try


def as_utc(at: datetime) -> datetime:
    """A naive time here is UTC, as spend.day_start() gives it; astimezone() would read it as the server's local time,
    which on New York time put the day's start at 5 AM and let a repeated press through between midnight and then."""
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


class FrontDesk:
    def __init__(self, path: str, client=None, clock=None):
        self.path, self.client = path, client
        self.clock = clock or (lambda: datetime.now(ZONE))      # the real time in Orlando; tests pass a fixed one
        self.day: date | None = None                            # the day the books were made for
        self.lock = threading.RLock()
        self.store: Store | None = None
        self.cast: dict[str, dict] = {}
        self.reserved = 0.0
        self._year = None
        self._views: OrderedDict[str, tuple] = OrderedDict()
        self._facts: dict | None = None

    def today(self) -> date:
        return local(self.clock()).date()

    def ready(self) -> Store:
        """The core's database, with the generated year up to this morning in it; a new day makes new books."""
        with self.lock:
            today = self.today()
            if self.store is None or self.day != today:
                self._seed(today)
        return self.store

    def _seed(self, today: date) -> None:
        """The year up to midnight today, written into a database of the day's own (seconds on a small instance; desk/api.py
        starts it at boot). Yesterday's file keeps yesterday's visitor copies: the footer says a copy lasts a day."""
        root, ext = os.path.splitext(self.path)
        store = Store(self.path if self.path == ":memory:" else f"{root}-{today.isoformat()}{ext}")
        year = build(today=today, cutoff=at_local(today))
        if not store.events(BASE):
            with store.write():
                store.append(BASE, year.led.pending)
        # what each visitor used, kept apart from their copy of the books: Start over removes the copy, never this
        store.db.execute("CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, at TEXT NOT NULL, visitor TEXT NOT NULL, "
                         "run TEXT NOT NULL, shift TEXT, cents REAL NOT NULL)")
        store.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS usage_once ON usage(visitor, run, shift)")
        self._views.clear()             # event ids start again in the new file, so no cached view may match
        self._facts = None
        self.cast, self.store, self._year, self.day = year.cast, store, year, today
        if self.path != ":memory:":     # visitor copies last a day: files older than yesterday's go (a run begun before midnight keeps yesterday's)
            keep = {f"{root}-{d.isoformat()}{ext}" for d in (today, today - timedelta(days=1))}
            for old in glob.glob(f"{glob.escape(root)}-????-??-??{ext}*"):
                if old.removesuffix("-wal").removesuffix("-shm") not in keep:
                    os.remove(old)

    def view(self, visitor: str, everything: bool = False) -> tuple:
        """What the pages read for one copy: its books, its own events, whether every entry balances and the debits, and with
        everything=True all its events. Folded once per change and kept for the last few copies; a page never changes them."""
        with self.lock:
            store = self.ready()
            stream = self.stream(visitor)
            last = store.db.execute("SELECT MAX(id) FROM events WHERE stream IN (?, ?)", (BASE, stream)).fetchone()[0]
            hit = self._views.get(stream)
            if hit is None or hit[0] != last:
                total, debits = store.db.execute("SELECT COALESCE(SUM(cents), 0), COALESCE(SUM(CASE WHEN cents > 0 THEN cents END), 0) "
                                                 "FROM journal_lines WHERE stream IN (?, ?)", (BASE, stream)).fetchone()
                hit = (last, store.books(stream), store.stream_events(stream), total == 0, debits)
                self._views[stream] = hit
                # ponytail: eight copies held in memory; a shared cache keyed by event id if many visitors read at once
                while len(self._views) > 8:
                    self._views.popitem(last=False)
            self._views.move_to_end(stream)
            return hit[1], hit[2], hit[3], hit[4], (store.events(stream) if everything else [])

    def facts(self) -> dict:
        """The facility's own numbers for the Overview, from the base year (which never changes after it is built)."""
        if self._facts is not None and self.day == self.today():     # read without the lock, so the Overview never waits behind a running case
            return self._facts
        with self.lock:
            store = self.ready()
            if self._facts is None:
                b, now = store.books(BASE), self.clock()
                self._facts = {"units": len(b.units), "rented": sum(1 for x in b.leases.values() if x.ended is None), "tenants": len(b.tenants),
                               "leases": len(b.leases), "charges": len(b.charges), "payments": len(b.payments), "declined": len(b.declined),
                               "past_due": sum(1 for t in b.tenants if b.past_due(t, now)), "past_due_cents": sum(b.past_due(t, now) for t in b.tenants)}
            return self._facts

    @staticmethod
    def stream(visitor: str) -> str:
        return f"visitor:{visitor}"

    def spent_dollars(self, since: datetime) -> float:
        """What our agent's front desk calls cost since `since`, plus what a running case has reserved."""
        with self.lock:
            cents = self.ready().db.execute("SELECT COALESCE(SUM(cents), 0) FROM usage WHERE at >= ?",
                                            (as_utc(since).isoformat(),)).fetchone()[0]
        return (cents + self.reserved) / 100

    def runs_today(self, visitor: str) -> dict[str, set]:
        """Today's tries by this visitor, each with the shift messages it ran: a shift is one try, a typed message is one try."""
        since = as_utc(spend.day_start()).isoformat()
        out: dict[str, set] = {}
        for run, shift in self.ready().db.execute("SELECT run, shift FROM usage WHERE visitor = ? AND at >= ?", (visitor, since)):
            out.setdefault(run, set()).add(shift)
        return out

    def clean(self) -> None:
        """Visitor copies older than a day are removed; the facility's own stream never is."""
        cutoff = (datetime.now(timezone.utc) - KEEP_FOR).isoformat()
        old = [s for (s,) in self.store.db.execute("SELECT stream FROM events WHERE stream != 'base' GROUP BY stream HAVING MAX(recorded_at) < ?", (cutoff,))]
        for s in old:
            self.store.drop_stream(s)
            self._views.pop(s, None)        # event ids can be used again once a copy is gone, so its cached view goes with it

    def tenants(self) -> list[dict]:
        self.ready()
        b = self._year.led.books
        return [{"role": i["role"], "name": b.tenants[self.cast[i["role"]]["tenant"]].name, "shift": i["id"], "text": self._year.words(i["text"]),
                 "units": [b.leases[l].unit for l in self.cast[i["role"]]["leases"]]} for i in SHIFT]

    def run(self, visitor: str, body: CaseIn, live: bool, tries_per_day: int | None = None, budget_left=None) -> dict:
        with self.lock:
            store = self.ready()
            self.clean()
            if body.tenant not in self.cast:
                raise HTTPException(404, "no such tenant on the page")
            item = next((i for i in SHIFT if i["id"] == body.shift), None)
            if body.shift is not None and (item is None or self._year.words(item["text"]) != body.text or item["role"] != body.tenant):
                raise HTTPException(422, "that is not one of this morning's messages")
            # a run id is honoured only for this morning's messages, each once; a typed message is always a try of its own
            # the New York day is part of a shift's run, so the same page can run the shift again tomorrow
            run = f"{spend.day_start().date()}:{body.run}" if (item is not None and body.run) else secrets.token_hex(6)
            runs = self.runs_today(visitor)
            # a double press or a retry of the same morning message is refused whether or not tries are counted
            if run in runs and body.shift in runs[run]:
                raise HTTPException(409, "That message already ran in this shift. Start a new shift to run it again.")
            if tries_per_day is not None:
                if run not in runs and len(runs) >= tries_per_day:
                    raise HTTPException(429, f"You have used today's {tries_per_day} tries. They come back at midnight, New York time.")
            if budget_left is not None and budget_left() < WORST_CASE_CENTS / 100:
                raise HTTPException(429, "Today's budget for live runs is used up. It comes back at midnight, New York time.")
            # the try is written in the same locked step as the check, so two requests at once cannot both pass it
            usage = store.db.execute("INSERT INTO usage(at, visitor, run, shift, cents) VALUES (?,?,?,?,0)",
                                     (datetime.now(timezone.utc).isoformat(), visitor, run, body.shift)).lastrowid
            stream = self.stream(visitor)
            phone = self.cast[body.tenant]["phone"]
            case = f"{run}:{body.shift or secrets.token_hex(4)}"
            started = time.monotonic()
            self.reserved += WORST_CASE_CENTS
        # ponytail: one lock serializes the core's database, model call included (a case takes a few seconds);
        # per-visitor locks when visitors start to wait on each other
        with self.lock:
            cost = WORST_CASE_CENTS
            try:
                res = handle(store, stream, case, phone, body.text, READ_ONLY, self.clock(), client=self.client, model_off=not live)
                cost = res.cents                 # a failed case keeps its worst cost: it may still have been billed
            finally:
                # the cost is written and the reservation released in one step, so no request sees neither
                store.db.execute("UPDATE usage SET cents = ? WHERE id = ?", (cost, usage))
                self.reserved -= WORST_CASE_CENTS
        return {"case": case, "handed": res.handed, "cents": round(res.cents, 4), "seconds": round(time.monotonic() - started, 1), "off": not live}

    def reset(self, visitor: str) -> None:
        with self.lock:
            self.ready()
            self.store.drop_stream(self.stream(visitor))
            self._views.pop(self.stream(visitor), None)
