"""Staddle's own simulated auction platform, and the steps a lien case takes on it.

The platform is an outside system with its own stored state, kept in a file apart from the books (like the access stand-in), reached
through the small methods of AuctionSite. The books keep one record for each request before it is sent and one for each answer after
it comes, so a lost answer is settled by reading the platform, never by asking again.

Each step is its own gate command: list, open bidding, bid, close bidding, name the winner, the buyer's payment, and the final sale.
A winning bid is not a sale and the buyer's money is not a tenant payment: it is kept here and never reaches the tenant's ledger.
Every forward step meets the same checks inside the gate write, and again just before it is sent. A cancellation is recorded first,
then sent, then read back; the case shows "cancellation waiting" until the platform shows the listing cancelled. Once it is
recorded, nothing revives that auction: a later answer is kept as a staff item and changes nothing, and a new lien needs a new
episode."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from . import delinquency as col
from . import liens
from .billing import Ledger
from .books import Books, local
from .evidence import money
from .renting import OutsideSystem, copy_mark
from .store import Store

STEP_OF = dict(zip(liens.AUCTION_ACTIONS, ("list", "open", "bid", "close", "winner", "payment", "sale", "cancel")))
BIDDER_STEPS = ("bid", "payment")          # taken by the buyer on the platform, not by staff
REQUIRES = {"open": "listed", "bid": "bidding", "close": "bidding", "winner": "bidding_closed", "payment": "won", "sale": "buyer_paid"}
ORDER_WHY = {"open": "bidding opens only on a listed auction", "bid": "a bid is taken only while bidding is open",
             "close": "bidding closes only while it is open", "winner": "a winner is named only after bidding has closed",
             "payment": "the buyer pays only after a winner is named", "sale": "the sale is final only after the buyer has paid"}
STATUS_WORDS = {"new": "not listed", "listed": "listed with bidding not open", "bidding": "open for bids", "bidding_closed": "closed to bids",
                "won": "won, with the buyer not yet paid", "no_bids": "closed with no bids", "buyer_paid": "paid for by the buyer, with the sale not yet final",
                "sold": "sold", "cancelled": "cancelled"}
TASK = "auction needs staff"
LATE_WORDS = {"list": "listed", "open": "opened for bids", "bid": "took a bid", "close": "closed bidding", "winner": "named a winner",
              "payment": "took the buyer's payment", "sale": "made the sale final"}


@dataclass
class Op:
    """One request to the platform: written before it is sent, answered after."""
    id: str
    n: int
    step: str
    args: dict
    by: str
    at: datetime
    attempts: int = 0
    state: str = "requested"            # requested, answered or refused (never sent, or the platform said no)
    result: dict | None = None
    how: str | None = None              # response, or found (an earlier call's answer looked up), or refused
    late: bool = False


@dataclass
class Auction:
    case: str
    ops: dict[str, Op] = field(default_factory=dict)
    listing: str | None = None
    status: str = "new"
    bids: list[dict] = field(default_factory=list)
    winner: dict | None = None
    buyer_paid_cents: int | None = None     # the buyer's money: kept here, never a payment on the tenant's account
    cancel: dict | None = None              # the tombstone: once recorded it is never removed
    late: list[dict] = field(default_factory=list)      # answers that came after the cancellation was recorded: kept, and they change nothing


def fold_event(b: Books, kind: str, at: datetime, p: dict) -> None:
    """Fold one `lien_case.auction_*` event. Called by liens.fold_event."""
    a = b.liens.auctions.setdefault(p["case"], Auction(p["case"]))
    if kind == "lien_case.auction_requested":
        a.ops[p["op"]] = Op(p["op"], p["n"], p["step"], p["args"], p["by"], at)
        if p["step"] == "cancel":
            a.cancel = {"op": p["op"], "by": p["by"], "at": at, "closes": p["args"]["closes"], "confirmed": None}
    elif kind == "lien_case.auction_attempted":
        a.ops[p["op"]].attempts = p["n"]
    elif kind == "lien_case.auction_refused":
        a.ops[p["op"]].state, a.ops[p["op"]].result = "refused", {"why": p["why"]}
    elif kind == "lien_case.auction_answered":
        op = a.ops[p["op"]]
        op.state, op.result, op.how, op.late = "answered", p["result"], p["how"], p["late"]
        if p["late"]:
            a.late.append({"op": op.id, "step": op.step, "result": p["result"], "at": at})
        elif op.step == "list":
            a.listing, a.status = p["result"]["listing"], "listed"
        elif op.step == "open":
            a.status = "bidding"
        elif op.step == "bid":
            a.bids.append({"bidder": op.args["bidder"], "cents": op.args["cents"], "at": at})
        elif op.step == "close":
            a.status = "bidding_closed"
        elif op.step == "winner":
            a.winner = p["result"]["winner"]
            a.status = "won" if a.winner else "no_bids"
        elif op.step == "payment":
            a.buyer_paid_cents, a.status = op.args["cents"], "buyer_paid"
        elif op.step == "sale":
            a.status = "sold"
    elif kind == "lien_case.auction_cancel_confirmed":
        a.cancel["confirmed"], a.status = at, "cancelled"


def auction_of(books: Books, case: liens.Case) -> Auction | None:
    return liens.state(books).auctions.get(case.id)


def listed_ever(a: Auction | None) -> bool:
    """Whether a listing was asked for: a request the platform never saw and the gate dropped does not count."""
    return a is not None and any(o.step == "list" and o.state != "refused" for o in a.ops.values())


def words(a: Auction) -> str:
    return STATUS_WORDS[a.status]


# the gate's checks -----------------------------------------------------------------------------------------------------
def forward_refusal(books: Books, case: liens.Case, now: datetime) -> str | None:
    """Why no forward step may be taken now, or None. The one test for every step, run inside the gate write and again just before a
    step is sent: a cancellation on record, a legal stop, an ended lease, a change since the notice copy (a payment among them), no
    approval standing for the file as it is, and the holds and balance on the owner's schedule."""
    a = auction_of(books, case)
    if a is not None and a.cancel is not None:
        return ("this auction was cancelled and can never come back; a new lien needs a new episode" if a.cancel["confirmed"]
                else "this auction is being cancelled" + (" because the claim was paid in full" if a.cancel["closes"]["kind"] == "cured" else ""))
    if why := liens.stop_reason(books, case):
        return why
    if books.leases[case.lease].ended:
        return "the lease has ended, and an ended lease never means permission to sell; staff handle it"
    if changed := liens.changes(books, case):
        return f"the file is waiting for staff to decide: {changed[0]['why']}"
    if not liens.approval(books, case):
        return "no manager approval stands for the file as it is now; a manager approves it again"
    where, why = col.step_state(books, case.lease, "lien", now, ignore_recorded=True)
    return None if where == "eligible" else why


def check(books: Books, case: liens.Case, cmd, now: datetime) -> tuple[bool, str]:
    """The gate's decision on an auction step, as (allowed, the reason in plain words). Writes nothing."""
    step, form, a = STEP_OF[cmd.action], cmd.form or {}, auction_of(books, case)
    if step == "cancel":
        return _cancel_check(a, cmd)
    if step == "sale" and cmd.source != liens.APPROVER:
        return False, "only a manager makes a sale final"
    if why := forward_refusal(books, case, now):
        return False, why
    if step == "list":
        if listed_ever(a):
            return False, "this episode already has an auction; there is one listing for each lien episode"
    else:
        if not listed_ever(a):
            return False, "no auction is listed for this case yet"
        if pending := next((o for o in a.ops.values() if o.state == "requested"), None):
            return False, (f"the auction site has not answered the last request ({LATE_WORDS.get(pending.step, pending.step)}) yet; "
                           "it is settled by reading the site, never by asking again")
        if a.status != REQUIRES[step]:
            return False, f"{ORDER_WHY[step]}; this auction is {words(a)}"
    if step in BIDDER_STEPS:
        cents = form.get("cents")
        if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
            return False, "an amount in cents is needed"
        if step == "bid":
            top = max((x["cents"] for x in a.bids), default=0)
            if cents <= top:
                return False, f"a bid must be higher than the top bid, {money(top)}"
        elif cmd.source != f"bidder:{a.winner['bidder']}" or cents != a.winner["cents"]:
            return False, f"only the winning bidder pays, and the amount is the winning bid, {money(a.winner['cents'])}"
    return True, {"list": "the auction is listed on the platform; nothing is sold by listing it",
                  "open": "bidding opens", "bid": "the bid is taken on the platform", "close": "bidding closes",
                  "winner": "the winner is named from the platform's bids; a winning bid is not a sale",
                  "payment": "the buyer's payment is kept on the platform; it is not a payment on the tenant's account",
                  "sale": "the sale is made final on the platform"}[step]


def _cancel_check(a: Auction | None, cmd) -> tuple[bool, str]:
    """A cancellation is open to staff whatever holds the file."""
    if not (cmd.reason or "").strip():
        return False, "a cancellation is recorded with a reason"
    if not listed_ever(a):
        return False, "there is no auction on this case to cancel"
    if a.cancel is not None:
        return False, "the cancellation of this auction is already recorded; it waits for the platform to show it cancelled"
    if a.status == "sold":
        return False, "the sale is final on the platform; staff and counsel decide what happens next, and no reversal is made here"
    return True, "the cancellation is recorded first, then sent to the platform, and counts once the platform shows the listing cancelled"


# recording a request ---------------------------------------------------------------------------------------------------
def _request(led: Ledger, case: liens.Case, now: datetime, step: str, args: dict, by: str, command: str | None = None) -> None:
    a = auction_of(led.books, case)
    n = (len(a.ops) if a else 0) + 1
    led.emit("lien_case.auction_requested", now, {"case": case.id, "op": f"{case.id}:{n}:{step}", "n": n, "step": step, "args": args, "by": by},
             f"lien_case:{case.id}:auction:{n}:requested", source=by, command=command)


def act(led: Ledger, cmd, case: liens.Case, now: datetime) -> None:
    """Record an allowed auction step as a request; drive() sends it."""
    step, form = STEP_OF[cmd.action], cmd.form or {}
    if step == "cancel":
        request_cancel(led, case, now, {"kind": "cancelled", "why": cmd.reason.strip(), "by": cmd.source}, cmd.source, cmd.id)
        return
    args = {"bidder": cmd.source.removeprefix("bidder:"), "cents": form["cents"]} if step in BIDDER_STEPS else {}
    _request(led, case, now, step, args, cmd.source, cmd.id)


def live(a: Auction | None) -> bool:
    """An auction a cancellation can still reach: begun, not cancelling, and not sold."""
    return a is not None and a.cancel is None and a.status != "sold" and listed_ever(a)


def holds_open(a: Auction | None) -> bool:
    """Whether a case must stay open for the platform: a listing still to take down, or a cancellation not yet read back. An auction
    whose only request was dropped before it was sent holds nothing, so its case closes at once."""
    return live(a) or (a is not None and a.cancel is not None and not a.cancel["confirmed"])


def request_cancel(led: Ledger, case: liens.Case, now: datetime, closes: dict, by: str, command: str | None = None) -> None:
    """Record the cancellation first. `closes` says how the case closes once the platform shows the listing cancelled. Safe to repeat."""
    if live(auction_of(led.books, case)):
        _request(led, case, now, "cancel", {"closes": closes}, by, command)


# the simulated platform ------------------------------------------------------------------------------------------------
class PlatformRefused(Exception):
    """The platform said no: the step is out of order on its side, or the bid is too low."""


class AuctionSite(OutsideSystem):
    """A deterministic auction platform for tests and for when no real one is connected; every record made through it says it is a
    simulation. It enforces its own order, and a repeated operation key returns the first answer without doing anything again, one
    caller at a time, also after a restart. `hide` names listing states a read-back fails to show (it shows `listed` instead). Given a
    path, its listings, answers and calls are kept in that file, so a restart finds them. The platform knows a listing by the case's
    reference, never by anything in the tenant's books."""

    def __init__(self, crash_before=(), crash_after=(), hide=(), path: str | None = None):
        self.hide = set(hide)
        self._lock = threading.RLock()
        super().__init__(crash_before, crash_after, path)

    def _do(self, key, go):
        with self._lock:
            return self.done[key] if key in self.done else super()._do(key, go)

    def _load(self, saved):
        self.listings: dict[str, dict] = saved.get("listings", {})
        self._n = saved.get("n", 0)             # listings made so far, so a forgotten listing's id is never used again

    def _dump(self):
        return {"listings": self.listings, "n": self._n}

    def _drop(self, prefix):
        self.listings = {k: v for k, v in self.listings.items() if not v["ref"].startswith(prefix)}

    def _in(self, listing: str, states: tuple, why: str) -> dict:
        found = self.listings.get(listing)
        if found is None:
            raise PlatformRefused("there is no such listing")
        if found["status"] not in states:
            raise PlatformRefused(why)
        return found

    def list_item(self, key, ref, description):
        def go():
            self._n += 1
            listing = f"auction-{self._n}"
            self.listings[listing] = {"id": listing, "ref": ref, "description": description, "status": "listed", "bids": [], "winner": None,
                                      "simulated": True}
            return {"listing": listing}
        return self._do(key, go)

    def open_bidding(self, key, listing):
        def go():
            self._in(listing, ("listed",), ORDER_WHY["open"])["status"] = "bidding"
            return {"status": "bidding"}
        return self._do(key, go)

    def place_bid(self, key, listing, bidder, cents):
        def go():
            found = self._in(listing, ("bidding",), ORDER_WHY["bid"])
            if found["bids"] and cents <= max(x["cents"] for x in found["bids"]):
                raise PlatformRefused("a bid must be higher than the top bid")
            found["bids"].append({"bidder": bidder, "cents": cents})
            return {"top_cents": cents}
        return self._do(key, go)

    def close_bidding(self, key, listing):
        def go():
            self._in(listing, ("bidding",), ORDER_WHY["close"])["status"] = "closed"
            return {"status": "closed"}
        return self._do(key, go)

    def name_winner(self, key, listing):
        def go():
            found = self._in(listing, ("closed",), ORDER_WHY["winner"])
            top = max(found["bids"], key=lambda x: x["cents"], default=None)
            found["winner"], found["status"] = top, "won" if top else "no_bids"
            return {"winner": top}
        return self._do(key, go)

    def take_payment(self, key, listing, bidder, cents):
        def go():
            found = self._in(listing, ("won",), ORDER_WHY["payment"])
            if (found["winner"]["bidder"], found["winner"]["cents"]) != (bidder, cents):
                raise PlatformRefused("only the winning bidder pays, and the amount is the winning bid")
            found["status"] = "paid"
            return {"paid_cents": cents}
        return self._do(key, go)

    def finish_sale(self, key, listing):
        def go():
            self._in(listing, ("paid",), ORDER_WHY["sale"])["status"] = "sold"
            return {"status": "sold"}
        return self._do(key, go)

    def cancel(self, key, ref):
        """Cancel every listing under the reference that is not sold. A sold listing stays sold and is named as kept."""
        def go():
            mine = [x for x in self.listings.values() if x["ref"] == ref]
            for x in mine:
                if x["status"] not in ("sold", "cancelled"):
                    x["status"] = "cancelled"
            return {"cancelled": [x["id"] for x in mine if x["status"] == "cancelled"], "kept": [x["id"] for x in mine if x["status"] == "sold"]}
        return self._do(key, go)

    def read_back(self, ref):
        """What the platform shows now for the reference: every listing under it and its state. Changes nothing."""
        return {"listings": [{"id": x["id"], "status": "listed" if x["status"] in self.hide else x["status"]}
                             for x in self.listings.values() if x["ref"] == ref]}


# carrying the work forward ---------------------------------------------------------------------------------------------
def _refuse(led: Ledger, case: liens.Case, op: Op, now: datetime, why: str) -> None:
    led.emit("lien_case.auction_refused", now, {"case": case.id, "op": op.id, "why": why}, f"lien_case:{case.id}:auction:{op.n}:refused", source=col.SYSTEM)


def _staff_item(led: Ledger, case: liens.Case, ref: str, why: str, now: datetime) -> None:
    col._task(led, TASK, case.tenant, f"auction:{case.id}:{ref}", why, local(now).date(), now)


def _settle_item(led: Ledger, case: liens.Case, ref: str, now: datetime) -> None:
    """An item that said the cancellation was not confirmed is settled when it is. A late-answer item is never settled here."""
    for t in [t for t in col.state(led.books).tasks.values() if t.ref == f"auction:{case.id}:{ref}" and not t.acknowledged]:
        led.emit("task.acknowledged", now, {"task": t.id, "by": col.SYSTEM}, f"task:{t.ref}:seen", source=col.SYSTEM)


def _next_job(store: Store, stream: str, led: Ledger, site: AuctionSite, now: datetime, read_done: set):
    """The next outside call to make, or None. Writes down what must be written first: a request dropped because a check now fails,
    the attempt about to be made, the read-back a recorded cancellation waits for. Runs inside the write transaction."""
    b = led.books
    for a in list(liens.state(b).auctions.values()):
        case = liens.state(b).cases[a.case]
        if case.closed:
            continue
        while True:
            op = min((o for o in a.ops.values() if o.state == "requested"), key=lambda o: o.n, default=None)
            if op is None and a.cancel and not a.cancel["confirmed"] and a.case not in read_done:
                read_done.add(a.case)           # one read-back a pass: an answer that is not yet "cancelled" waits for the next
                _request(led, case, now, "read", {}, col.SYSTEM)
                continue
            if op is None:
                break
            key = f"{copy_mark(store, stream)}:{op.id}"
            forward = op.step not in ("cancel", "read")
            found = site.find(key) if op.attempts and op.step != "read" else None
            if found is None and forward and (why := forward_refusal(b, case, now)):
                _refuse(led, case, op, now, why)    # never sent, so nothing was done: the check just before the call
                continue
            if found is None:
                led.emit("lien_case.auction_attempted", now, {"case": case.id, "op": op.id, "n": op.attempts + 1},
                         f"lien_case:{case.id}:auction:{op.n}:try:{op.attempts + 1}", source=col.SYSTEM)
            return case.id, op.id, key, f"{copy_mark(store, stream)}:{case.id}"
    return None


def _send(site: AuctionSite, op: Op, key: str, ref: str, listing: str | None, case: liens.Case) -> dict:
    s, a = op.step, op.args
    if s == "list":
        return site.list_item(key, ref, f"Contents of unit {case.unit}")
    if s == "open":
        return site.open_bidding(key, listing)
    if s == "bid":
        return site.place_bid(key, listing, a["bidder"], a["cents"])
    if s == "close":
        return site.close_bidding(key, listing)
    if s == "winner":
        return site.name_winner(key, listing)
    if s == "payment":
        return site.take_payment(key, listing, a["bidder"], a["cents"])
    if s == "sale":
        return site.finish_sale(key, listing)
    if s == "cancel":
        return site.cancel(key, ref)
    return site.read_back(ref)


def _record(led: Ledger, case: liens.Case, op: Op, result: dict, how: str, now: datetime, simulated: bool) -> None:
    b = led.books
    a = auction_of(b, case)
    if how == "refused":
        _refuse(led, case, op, now, result["refused"])
        _staff_item(led, case, f"refused:{op.id}", f"The auction platform refused a request ({op.step}): {result['refused']}. Nothing was changed.", now)
        return
    late = a.cancel is not None and op.step not in ("cancel", "read")
    led.emit("lien_case.auction_answered", now, {"case": case.id, "op": op.id, "result": result, "how": how, "late": late, "simulated": simulated},
             f"lien_case:{case.id}:auction:{op.n}:answered", source=col.SYSTEM)
    if late:
        _staff_item(led, case, f"late:{op.id}", f"The auction platform answered after the cancellation was recorded: it {LATE_WORDS[op.step]}. "
                    "The answer is kept and nothing was changed; the cancelled auction is never brought back. Staff and counsel decide what follows.", now)
    elif op.step == "read":
        shown = result["listings"]
        # every listing the books know of (an answer recovered late counts) must show cancelled; no listing known, none to show
        known = {o.result["listing"] for o in a.ops.values() if o.step == "list" and o.state == "answered"}
        missing = sorted(known - {x["id"] for x in shown})
        if not missing and all(x["status"] == "cancelled" for x in shown):
            closes = a.cancel["closes"]
            _settle_item(led, case, "cancel_unconfirmed", now)
            led.emit("lien_case.auction_cancel_confirmed", now, {"case": case.id, "op": op.id}, f"lien_case:{case.id}:auction:cancelled", source=col.SYSTEM)
            led.emit("lien_case.closed", now, {"case": case.id, "kind": closes["kind"], "why": closes["why"], "by": closes["by"]},
                     f"lien_case:{case.id}:closed", source=col.SYSTEM)
            liens._settle_tasks(led, case, col.SYSTEM, now)
        else:
            left = ", ".join([f"{x['id']} ({x['status']})" for x in shown if x["status"] != "cancelled"] + [f"{x} (not shown at all)" for x in missing])
            _staff_item(led, case, "cancel_unconfirmed", f"The auction platform still shows {left} after the cancellation was sent. The case stays open "
                        "until it shows every listing cancelled; staff and counsel decide.", now)


def drive(store: Store, stream: str, site: AuctionSite, now: datetime) -> dict[str, str]:
    """Carry every auction in the stream forward, one outside call at a time. The attempt is written first, in a transaction of its own;
    then one more transaction looks up what an earlier call did, checks a step not yet sent once more, sends it and records the
    answer. After a crash, running it again finds the earlier call's answer before it calls again. Returns where each auction stands."""
    read_done: set[str] = set()
    while True:
        with store.write():                 # the attempt is on record before anything is sent
            led = Ledger(store.books(stream))
            job = _next_job(store, stream, led, site, now, read_done)
            store.append(stream, led.pending)
        if job is None:
            break
        case_id, op_id, key, ref = job
        # ponytail: the send and its answer share one write transaction, so a legal stop or a payment cannot be recorded between the
        # last check and the send, and two drivers cannot both send. A real platform would be called under a short lease instead of
        # under the write lock; the simulated one is a local file.
        with store.write():
            books = store.books(stream)
            led = Ledger(books)
            case, a = liens.state(books).cases[case_id], liens.state(books).auctions[case_id]
            op = a.ops[op_id]
            if op.state != "requested":
                continue                    # another driver answered it first
            found = site.find(key) if op.step != "read" else None
            if found is None and op.step not in ("cancel", "read") and (why := forward_refusal(books, case, now)):
                _refuse(led, case, op, now, why)        # a stop or a payment landed since the attempt: nothing was sent
                store.append(stream, led.pending)
                continue
            if found is not None:
                result, how = found, "found"
            else:
                try:
                    result, how = _send(site, op, key, ref, a.listing, case), "response"
                except PlatformRefused as e:
                    result, how = {"refused": str(e)}, "refused"
            _record(led, case, op, result, how, now, bool(site.simulated))
            store.append(stream, led.pending)
    books = store.books(stream)
    return {c: a.status + (" (cancelling)" if a.cancel and not a.cancel["confirmed"] else "") for c, a in liens.state(books).auctions.items()}
