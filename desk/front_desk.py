"""Our agent working tenant messages on the facility's own books (the core in core/), for the FMS pages in desk/fms.py.

Each signed-in visitor works on a scratch copy of the generated year (their own stream), so one visitor's reversal never changes
what the next one sees. A message is read by our agent; code checks every claim against the records and decides; the gate
records it. Runs count against the visitor's tries and the day's budget."""
from __future__ import annotations

import glob
import logging
import os
import secrets
import threading
from collections import OrderedDict
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import BaseModel, Field

from core import delinquency
from core.books import ZONE, at_local, local
from core.billing import Ledger, Refused
from core.case import READ_ONLY, handle
from core.gate import STAFF_RULE, Command, execute
from core.rules import Proposal, compile_rule, replay
from core.cases import load
from core.seed import build
from core.store import BASE, Store

from . import spend

SHIFT = load("shift")["items"]
SWEEP_EVERY = 3600              # seconds between collections passes; each writes only what is new, so a copy started today is swept today
log = logging.getLogger("desk.front_desk")
WORST_CASE_CENTS = 0.1          # the most one message can cost on the live link (one reading call on Haiku, capped)
RULE_WORST_CENTS = 0.2          # the most reading one owner sentence can cost (one call on Haiku, output capped)
RULES_PER_DAY = 3               # owner sentences a visitor may have read in a day, apart from the message tries
RULE_MODEL = ("claude-haiku-5-5", "low")
KEEP_FOR = timedelta(hours=24)
HOLD_RETRY = 60                 # seconds before a day that cannot leave its waiting payments asks the processor again
ACTION_WORDS = {"explain_charge": "Explained from the records", "reverse_duplicate": "Reversed the second copy",
                "correct_fee_posted_in_error": "Removed as posted in error", "open_dispute": "Dispute recorded for the office manager",
                "waive_fee": "Waived under the owner's rule", "hand_to_staff": "Handed to staff",
                "propose_plan": "Plans worked out under the owner's policy", "offer_plan": "Plans offered to the tenant",
                "accept_plan": "Plan agreed in the tenant's words", "record_promise": "Promise to pay noted"}
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


class RuleIn(BaseModel):
    sentence: str = Field(min_length=3, max_length=400)


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
    def __init__(self, path: str, client=None, clock=None, processor_path: str | None = None, processor=None):
        self.path, self.client = path, client
        # the simulated payment processor keeps its records beside the core's database: one per running app, never in the repository
        if processor_path is None and path != ":memory:":
            processor_path = os.path.join(os.path.dirname(os.path.abspath(path)), "processor.json")
        self.processor = processor or delinquency.SimulatedProcessor(processor_path)
        self.clock = clock or (lambda: datetime.now(ZONE))      # the real time in Orlando; tests pass a fixed one
        self.day: date | None = None                            # the day the books were made for
        self.lock = threading.RLock()
        self.store: Store | None = None
        self.cast: dict[str, dict] = {}
        self.reserved = 0.0
        self._year = None
        self._views: OrderedDict[str, tuple] = OrderedDict()
        # ponytail: the last proposal per visitor, in memory; a restart drops it and the owner asks again
        self._proposals: OrderedDict[str, tuple] = OrderedDict()     # visitor -> (id, proposal, sentence, rule versions, day)
        self._resets: dict[str, int] = {}                            # visitor -> how many times their copy was reset
        self._notes: dict[str, str] = {}                             # visitor -> why a going back was refused, shown once on the Rules page
        self._step_notes: dict[str, str] = {}                        # visitor -> why a staff step on a tenant was refused, shown once there
        self._reset_notes: dict[str, str] = {}                       # visitor -> why Start over was refused, shown once on Home
        self._hold_until = 0.0                                       # monotonic time before which a held day does not ask the processor again
        self._facts: dict | None = None

    def today(self) -> date:
        return local(self.clock()).date()

    def ready(self) -> Store:
        """The core's database, with the generated year up to this morning in it; a new day makes new books."""
        with self.lock:
            today = self.today()
            if self.store is None or self.day != today:
                if self.store is not None and not self._may_leave_the_day():
                    return self.store       # a payment of the old day is still waiting at the processor: that day stays until it is settled
                self._seed(today)
        return self.store

    # a payment the processor has not settled is never dropped with its copy: Start over, the new day and the cleaning of idle copies each
    # ask the processor first and go ahead only when none is left waiting
    @staticmethod
    def _unresolved(store: Store, stream: str) -> list:
        return [i for i in delinquency.state(store.books(stream)).intents.values() if i.state in delinquency.UNSETTLED]

    def _resolve(self, store: Store, streams: list[str]) -> bool:
        """Ask the processor about each copy's waiting payments: settle what it settled, cancel what it still processes, read again so the
        cancellation is recorded. Stops at the first answer that does not come, since every further copy would wait out its own timeout
        with the lock held. True only when no copy has a waiting payment left, judged by the books and not by what the calls returned.
        ponytail: a processor that never answers again (a revoked key, a payment record deleted there) keeps the copy and the day;
        the error is logged on every attempt, and an operator settles it by hand."""
        now = self.clock()
        for s in streams:
            try:
                delinquency.reconcile(store, s, self.processor, now)
                waiting = self._unresolved(store, s)
                if any(i.state == "pending" for i in waiting):
                    delinquency.close_copy(self.processor, s, min(i.created for i in waiting))
                    delinquency.reconcile(store, s, self.processor, now)
            except (delinquency.ProcessorTimeout, delinquency.ProcessorRefused) as exc:
                # the first answer that does not come ends the pass: one wait under the lock per attempt, not one per copy
                log.error("the processor could not settle the payments of %s: %s", s, type(exc).__name__)
                break
            except Exception:           # as sweep() does: a fault in one copy is logged, the copy stays as it is, and no page is taken down
                log.exception("settling the payments of %s failed", s)
        return not any(self._unresolved(store, s) for s in streams)

    def _may_leave_the_day(self) -> bool:
        if time.monotonic() < self._hold_until:         # asked a moment ago and got no answer: pages do not each wait on the processor
            return False
        streams = [s for (s,) in self.store.db.execute("SELECT DISTINCT stream FROM events WHERE stream LIKE 'visitor:%'")]
        waiting = [s for s in streams if self._unresolved(self.store, s)]
        if not waiting or self._resolve(self.store, waiting):
            return True
        self._hold_until = time.monotonic() + HOLD_RETRY
        log.error("the day of %s stays: %d copies still have a payment the processor has not settled", self.day, len(waiting))
        return False

    def _seed(self, today: date) -> None:
        """The year up to midnight today, written into a database of the day's own (seconds on a small instance; desk/api.py
        starts it at boot). Yesterday's file keeps yesterday's visitor copies: the footer says a copy lasts a day."""
        root, ext = os.path.splitext(self.path)
        store = Store(self.path if self.path == ":memory:" else f"{root}-{today.isoformat()}{ext}")
        year = build(today=today, cutoff=at_local(today), standing=True)
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

    def sweep(self) -> list[str]:
        """The collections pass over every visitor copy. The processor's events that no webhook brought are delivered first; then
        each copy that has payments is reconciled with the processor (a payment whose answer was lost is settled from its record),
        and its due parts are noticed, missed ones breached with a staff task, promises and exemptions settled. At boot it catches
        up on whatever fell due while nothing ran. Every record is keyed, so a pass with nothing new writes nothing. Returns the
        copies it swept."""
        with self.lock:
            store = self.ready()
            for event in self.processor.take_events():
                try:
                    delinquency.webhook(store, self.processor, event, self.clock())
                except Exception:       # one bad event never stops the others; reconciliation settles it later
                    log.exception("processor event %s failed", event.get("id"))
            streams = [s for (s,) in store.db.execute("SELECT DISTINCT stream FROM events WHERE stream LIKE 'visitor:%' ORDER BY stream")]
        swept = []
        for s in streams:
            with self.lock:         # one copy at a time, so pages wait for one copy's pass at most
                try:                # a fault in one copy never stops the copies after it
                    now = self.clock()
                    if delinquency.state(store.books(s)).intents:
                        delinquency.reconcile(store, s, self.processor, now)
                    delinquency.sweep(store, s, now)
                    swept.append(s)
                except Exception:
                    log.exception("collections sweep failed for %s", s)
        return swept

    def processor_event(self, event: dict) -> str:
        """A signed processor event on the webhook, applied the way the hourly pass applies one. A payment the processor no longer
        knows is answered as unknown, so it is not sent again."""
        with self.lock:
            try:
                return delinquency.webhook(self.ready(), self.processor, event, self.clock())
            except delinquency.ProcessorRefused:
                return "unknown"

    def serve(self) -> None:
        """Run at boot in a thread of its own: build the day's year, catch up, then sweep every SWEEP_EVERY seconds."""
        while True:
            try:
                self.sweep()
            except Exception:       # a failed pass is logged and the next one still runs
                log.exception("collections sweep failed")
            time.sleep(SWEEP_EVERY)

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
        rows = self.ready().db.execute("SELECT run, shift FROM usage WHERE visitor = ? AND at >= ? AND (shift IS NULL OR shift != 'rule')", (visitor, since))
        for run, shift in rows:
            out.setdefault(run, set()).add(shift)
        return out

    def clean(self) -> None:
        """Visitor copies older than a day are removed; the facility's own stream never is."""
        cutoff = (datetime.now(timezone.utc) - KEEP_FOR).isoformat()
        old = [s for (s,) in self.store.db.execute("SELECT stream FROM events WHERE stream != 'base' GROUP BY stream HAVING MAX(recorded_at) < ?", (cutoff,))]
        for s in old:
            if self._unresolved(self.store, s):
                continue                    # a payment the processor has not settled keeps its copy
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

    def reset(self, visitor: str) -> bool:
        """Throw away this visitor's copy. A copy with a card payment the processor has not settled is kept: the processor is asked first
        (settle it, or cancel it if it is still processing), and if it cannot say, nothing is thrown away and Home says so once, so the
        payment is never lost and never taken a second time on a fresh copy. Returns whether the copy was started over."""
        with self.lock:
            store, stream = self.ready(), self.stream(visitor)
            if self._unresolved(store, stream) and not self._resolve(store, [stream]):
                self._keep(self._reset_notes, visitor, "Start over waits: a card payment on this copy has not been settled by the card processor "
                           "yet, so nothing was thrown away. Try again once the card processor has answered.")
                return False
            self._proposals.pop(visitor, None)
            self._notes.pop(visitor, None)
            self._reset_notes.pop(visitor, None)
            self._resets[visitor] = self._resets.get(visitor, 0) + 1
            store.drop_stream(stream)
            self._views.pop(stream, None)
            return True


    # owner rules ------------------------------------------------------------------------------------------------------
    def rule_proposal(self, visitor: str, sentence: str, live: bool, budget_left=None) -> tuple[str, Proposal, dict]:
        """One owner sentence read into a proposed rule, checked by code, with last year replayed under it. A try of its own:
        three a day per visitor, inside the day's budget."""
        with self.lock:
            store = self.ready()
            books = store.books(self.stream(visitor))
            current = dict(books.rules["late_fee_waiver"]["typed"])
            versions = tuple(books.rules.get(n, {}).get("version", 0) for n in ("late_fee_waiver", STAFF_RULE))
            day = self.day
            copy = self._resets.get(visitor, 0)
            since = as_utc(spend.day_start()).isoformat()
            used = store.db.execute("SELECT COUNT(*) FROM usage WHERE visitor = ? AND shift = 'rule' AND at >= ?", (visitor, since)).fetchone()[0]
            if live and used >= RULES_PER_DAY:
                raise HTTPException(429, f"You have used today's {RULES_PER_DAY} rule readings. They come back at midnight, New York time.")
            if live and budget_left is not None and budget_left() < RULE_WORST_CENTS / 100:
                raise HTTPException(429, "Today's budget for live runs is used up. It comes back at midnight, New York time.")
            usage = None
            if live:
                usage = store.db.execute("INSERT INTO usage(at, visitor, run, shift, cents) VALUES (?,?,?,?,0)",
                                         (datetime.now(timezone.utc).isoformat(), visitor, "rule:" + secrets.token_hex(4), "rule")).lastrowid
                self.reserved += RULE_WORST_CENTS
        p = None
        try:
            if live and self.client is not None:
                p = compile_rule(self.client, RULE_MODEL[0], RULE_MODEL[1], sentence, current)
            else:
                p = Proposal("cannot", reason="Tilden is off on this copy, so no sentence can be read into a rule")
        finally:
            with self.lock:     # a failed call keeps its worst cost: it may still have been billed
                if usage is not None:
                    store.db.execute("UPDATE usage SET cents = ? WHERE id = ?", (p.call.cents if p and p.call else RULE_WORST_CENTS, usage))
                    self.reserved -= RULE_WORST_CENTS
        effect = {}
        if p.outcome == "adopt" and p.kind == "waiver":
            with self.lock:
                events = store.events(self.stream(visitor))
            effect = replay(events, current, p.typed)
        pid = secrets.token_hex(6)
        with self.lock:
            if self._resets.get(visitor, 0) != copy:      # the copy was reset while the sentence was read: this result is for the old one
                raise HTTPException(409, "Your copy was started over while the rule was read. Write the rule again.")
            self._proposals[visitor] = (pid, p, sentence, versions, day)
            while len(self._proposals) > 64:
                self._proposals.popitem(last=False)
        return pid, p, effect

    def approve_rule(self, visitor: str, pid: str) -> int:
        """The visitor, acting as the owner on their own copy, adopts the proposal they were shown. Returns the new version."""
        with self.lock:
            held = self._proposals.get(visitor)
            if held is None or held[0] != pid or held[1].outcome != "adopt":
                raise HTTPException(409, "That proposal is no longer here. Write the rule again.")
            _, p, sentence, versions, day = held
            store = self.ready()
            stream = self.stream(visitor)
            books = store.books(stream)
            now = tuple(books.rules.get(n, {}).get("version", 0) for n in ("late_fee_waiver", STAFF_RULE))
            if now != versions or day != self.day:     # a rule changed, or the books were rebuilt, since it was read
                del self._proposals[visitor]
                raise HTTPException(409, "The rules changed since this proposal was read. Write the rule again.")
            name = "late_fee_waiver" if p.kind == "waiver" else STAFF_RULE
            typed = dict(p.typed)
            before = books.rules.get(name)
            if name == STAFF_RULE and before:          # a staff rule adds to the conditions already in force
                old = before["typed"]
                typed = {"action": "send_to_staff", "several_units": bool(old.get("several_units") or typed["several_units"]),
                         **{k: sorted(set(old.get(k, [])) | set(typed[k])) for k in ("flags", "claim_kinds", "actions")}}
                sentence = before["sentence"] + " " + sentence
            version = (before or {}).get("version", 0) + 1
            led = Ledger(books)
            led.adopt_rule(self.clock(), name, version, sentence, typed, by="owner")
            with store.write():
                store.append(stream, led.pending)
            del self._proposals[visitor]
            return version

    def restore_rule(self, visitor: str, name: str, version: int) -> int:
        """The visitor, acting as the owner on their own copy, goes back to an earlier version of a rule. Returns the new version.
        A proposal read before this is refused at its approval, since the rules changed."""
        with self.lock:
            store = self.ready()
            stream = self.stream(visitor)
            led = Ledger(store.books(stream))
            try:
                led.restore_rule(self.clock(), name, version, by="owner")
            except Refused as exc:
                self._notes[visitor] = f"{str(exc)[:1].upper()}{str(exc)[1:]}."
                while len(self._notes) > 64:
                    self._notes.pop(next(iter(self._notes)))
                raise HTTPException(409, str(exc)) from exc
            with store.write():
                store.append(stream, led.pending)
            self._notes.pop(visitor, None)      # a refusal not yet shown is out of date once a going back succeeds
            return led.books.rules[name]["version"]

    # collections ------------------------------------------------------------------------------------------------------------
    def _keep(self, notes: dict[str, str], visitor: str, text: str) -> None:
        notes[visitor] = f"{text[:1].upper()}{text[1:]}" + ("" if text.endswith(".") else ".")
        while len(notes) > 64:
            notes.pop(next(iter(notes)))

    def staff_step(self, visitor: str, cmd: Command) -> None:
        """A staff step on a tenant's collections, through the gate on this copy. A refusal is kept to be shown once on that page."""
        with self.lock:
            d = execute(self.ready(), self.stream(visitor), cmd, self.clock())
            if d.allowed:
                self._step_notes.pop(visitor, None)
            else:
                self._keep(self._step_notes, visitor, d.check)

    def take_card_payment(self, visitor: str, cmd: Command) -> None:
        """Staff take a card payment on a tenant's page. The gate decides and records; a refusal is kept to be shown once. When it allows
        a command it has not seen before, the payment goes through the processor on the card the tenant has on file. It counts when the
        processor confirms it; until then (or when the answer is lost) it shows as waiting, and the hourly pass settles it.
        ponytail: the decision and the intent are two writes (pay() opens its own transaction); a crash between them leaves a decision
        with no payment, and staff press the button again on a fresh page."""
        with self.lock:
            store, stream = self.ready(), self.stream(visitor)
            d = execute(store, stream, cmd, self.clock())
            if not d.allowed:
                return self._keep(self._step_notes, visitor, d.check)
            self._step_notes.pop(visitor, None)
            if d.repeated:          # the same page pressed twice: the first decision comes back and nothing is paid again
                return
            token = next(c["token"] for c in delinquency.cards_of(store.books(stream), cmd.tenant) if c["card"] == cmd.form["card"])
            try:
                intent = delinquency.pay(store, stream, self.processor, cmd.tenant, cmd.form["cents"], token, self.clock())
            except Refused as exc:
                return self._keep(self._step_notes, visitor, str(exc))
            if intent.state == "closed":
                self._keep(self._step_notes, visitor, "the card processor did not take the payment")

    def take_reset_note(self, visitor: str) -> str:
        """Why the last Start over was refused, once; empty when nothing was."""
        with self.lock:
            return self._reset_notes.pop(visitor, "")

    def take_step_note(self, visitor: str) -> str:
        """Why the last staff step on a tenant was refused, once; empty when nothing was."""
        with self.lock:
            return self._step_notes.pop(visitor, "")

    def acknowledge(self, visitor: str, task: str, who: str) -> None:
        """Staff saw a collections task on this copy; a task already acknowledged, or not here, changes nothing."""
        with self.lock:
            store = self.ready()
            led = Ledger(store.books(self.stream(visitor)))
            try:
                delinquency.acknowledge(led, task, who, self.clock())
            except Refused:
                return
            with store.write():
                store.append(self.stream(visitor), led.pending)

    def owner_setting(self, visitor: str, kind: str, values: dict | None, why: str = "") -> bool:
        """The visitor, as the owner on their own copy, adopts the next version of the plan policy or the schedule. Values outside
        the fixed limits, or the same as in force, change nothing, and the Rules page says why once."""
        with self.lock:
            store = self.ready()
            led = Ledger(store.books(self.stream(visitor)))
            held = getattr(delinquency.state(led.books), "policy" if kind == "policy" else "schedule") or {}
            if values is not None and all(held.get(k) == v for k, v in values.items()):
                why = f"that is already the {'plan policy' if kind == 'policy' else 'schedule'} in force"
            if values is None or why:
                self._keep(self._notes, visitor, why)
                return False
            try:
                if kind == "policy":
                    delinquency.adopt_policy(led, self.clock(), held.get("version", 0) + 1, values)
                else:
                    delinquency.adopt_schedule(led, self.clock(), held.get("version", 0) + 1, values["overlock_days"], values["lien_days"])
            except Refused as exc:
                self._keep(self._notes, visitor, str(exc))
                return False
            with store.write():
                store.append(self.stream(visitor), led.pending)
            self._notes.pop(visitor, None)
            return True

    def take_note(self, visitor: str) -> str:
        """Why the last going back was refused, once; empty when nothing was."""
        with self.lock:
            return self._notes.pop(visitor, "")
