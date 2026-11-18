"""The books: posting rules that turn an event into balanced journal lines, and the state folded from the event log.

Every event carries in its payload everything its lines need (amounts, which charges a payment paid, how much became
credit), so `lines()` is a pure function of the event. Replaying the log through `lines()` must give back exactly the
journal that was stored; the tests check that. Amounts are whole cents. A debit is positive, a credit negative.

Accounts: receivable:<lease>, income:rent, income:fees, contra:waivers, clearing:card, credit:<tenant> (money the facility
holds for the tenant, a liability)."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/New_York")
RENT, LATE_FEE, ADMIN_FEE = "rent", "late_fee", "admin_fee"
FEE_KINDS = (LATE_FEE, ADMIN_FEE)
LATE_FEE_CENTS, ADMIN_FEE_CENTS = 2000, 2500
CUTOFF_DAY = 6          # the late fee is posted at 00:00 local on this day when rent due on the 1st is still unpaid
REVIEW_DAYS = 10


def local(t: datetime) -> datetime:
    return t.astimezone(ZONE)


def at_local(d: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=ZONE)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def cutoff(month: str) -> datetime:
    y, m = map(int, month.split("-"))
    return at_local(date(y, m, CUTOFF_DAY))


Line = tuple[str, int, "str | None"]


def _income(kind: str) -> str:
    return "income:rent" if kind == RENT else "income:fees"


def _moves(payload: dict) -> list[Line]:
    """Lines for money moved between a charge and the tenant's credit: an application of credit pays a charge, a release
    gives a charge's paid part back to credit."""
    out: list[Line] = []
    for a in payload.get("released", []):          # a reversed or waived charge that was paid: its paid part becomes credit
        out += [(f"receivable:{a['lease']}", a["cents"], a["charge"]), (f"credit:{payload['tenant']}", -a["cents"], None)]
    return out


def lines(kind: str, p: dict) -> list[Line]:
    """The journal lines an event posts: (account, signed cents, charge id or None)."""
    if kind == "charge.posted":
        return [(f"receivable:{p['lease']}", p["cents"], p["charge"]), (_income(p["kind"]), -p["cents"], p["charge"])]
    if kind == "payment.received":
        out: list[Line] = [("clearing:card", p["cents"], None)]
        out += [(f"receivable:{a['lease']}", -a["cents"], a["charge"]) for a in p["applications"]]
        if p.get("credit_cents"):
            out.append((f"credit:{p['tenant']}", -p["credit_cents"], None))
        return out
    if kind == "payment.returned":
        # the mirror of the payment: every charge it paid (directly, or through credit it created) is reopened
        out: list[Line] = [("clearing:card", -p["cents"], None)]
        out += [(f"receivable:{a['lease']}", a["cents"], a["charge"]) for a in p["reopened"]]
        if p.get("credit_cents"):
            out.append((f"credit:{p['tenant']}", p["credit_cents"], None))
        return out
    if kind == "credit.applied":
        out: list[Line] = [(f"credit:{p['tenant']}", sum(a["cents"] for a in p["applications"]), None)]
        out += [(f"receivable:{a['lease']}", -a["cents"], a["charge"]) for a in p["applications"]]
        return out
    if kind == "charge.reversed":
        return [(_income(p["kind"]), p["cents"], p["charge"]), (f"receivable:{p['lease']}", -p["cents"], p["charge"])] + _moves(p)
    if kind == "charge.waived":
        return [("contra:waivers", p["cents"], p["charge"]), (f"receivable:{p['lease']}", -p["cents"], p["charge"])] + _moves(p)
    return []


@dataclass
class Lease:
    id: str
    tenant: str
    unit: str
    rent_cents: int
    start: date
    schedule: list[str]             # the fee kinds the tenant signed for
    ended: date | None = None


@dataclass
class Tenant:
    id: str
    name: str
    phone: str


@dataclass
class Charge:
    id: str
    lease: str
    tenant: str
    kind: str
    obligation: str | None          # what the charge bills ("rent:L001:2026-10"); a hand charge may name none
    cents: int
    due: date
    posted_at: datetime
    source: str
    seq: int                        # posting order
    paid: int = 0                   # paid through payments and credit, net of returns and releases
    paid_effective: list[tuple[datetime, int, str]] = field(default_factory=list)  # (effective time, cents, payment or credit lot) towards it
    closed: str | None = None       # "reversed" or "waived"

    @property
    def open_cents(self) -> int:
        return 0 if self.closed else self.cents - self.paid


@dataclass
class Payment:
    id: str
    tenant: str
    cents: int
    effective_at: datetime
    known_at: datetime
    applications: list[dict]
    credit_cents: int
    returned: bool = False


@dataclass
class Hold:
    id: str
    charge: str
    dispute: str
    review_by: date
    active: bool = True


@dataclass
class Dispute:
    id: str
    charge: str
    tenant: str
    opened_at: datetime
    review_by: date
    owner: str
    quote: str
    hold: str | None
    outcome: str | None = None


@dataclass
class CreditUse:
    lot: str
    charge: str
    lease: str
    cents: int


class Books:
    """The state of one stream, folded event by event. Commands read it; only events change it."""

    def __init__(self):
        self.facility: dict = {}
        self.units: list[str] = []
        self.tenants: dict[str, Tenant] = {}
        self.leases: dict[str, Lease] = {}
        self.charges: dict[str, Charge] = {}
        self.of_tenant: dict[str, list[str]] = {}        # tenant -> charge ids, in posting order, so a tenant's reads never scan the year
        self.of_lease: dict[str, list[str]] = {}         # lease -> charge ids, in posting order
        self.obligations: dict[str, list[str]] = {}       # obligation -> charge ids, in posting order
        self.payments: dict[str, Payment] = {}
        self.declined: list[dict] = []
        self.credit: dict[str, int] = {}                  # tenant -> credit held
        self.lots: dict[str, list[list]] = {}             # tenant -> [source, cents left] for each amount of credit, oldest first
        self.credit_uses: dict[str, list[CreditUse]] = {}  # tenant -> every application of credit, with the credit it came from
        self.waivers: dict[str, list[datetime]] = {}      # tenant -> when each late fee was waived
        self.disputes: dict[str, Dispute] = {}
        self.holds: dict[str, Hold] = {}
        self.rules: dict[str, dict] = {}                  # rule id -> its latest version (sentence, typed form, version)
        self.rule_history: dict[str, list[dict]] = {}     # rule id -> every version that took effect, oldest first, with when it was adopted
        self.decisions: dict[str, dict] = {}              # command id -> the gate's recorded decision
        self.seq = 0
        self.last_at: datetime | None = None
        self.counters: dict[str, int] = {}

    # ids are counted per stream, so a seeded year gets the same ids every time
    def next_id(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}{self.counters[prefix]:04d}"

    def _seen(self, prefix: str, ident: str) -> None:
        n = int(ident[len(prefix):])
        self.counters[prefix] = max(self.counters.get(prefix, 0), n)

    def apply(self, kind: str, at: datetime, p: dict) -> None:
        self.seq += 1
        self.last_at = at
        if kind == "facility.set_up":
            self.facility, self.units = {k: v for k, v in p.items() if k != "units"}, list(p["units"])
        elif kind == "rule.adopted":
            # a rule only moves forward: an older or equal version never replaces the one in force
            if p["version"] > self.rules.get(p["rule"], {}).get("version", 0):
                self.rules[p["rule"]] = dict(p)
                self.rule_history.setdefault(p["rule"], []).append(deepcopy({**p, "at": at}))     # a copy: the history never shares a dict with the books
        elif kind == "gate.decided":
            self.decisions[p["command"]] = dict(p)
        elif kind == "tenant.added":
            self.tenants[p["tenant"]] = Tenant(p["tenant"], p["name"], p["phone"])
            self._seen("T", p["tenant"])
        elif kind == "lease.signed":
            self.leases[p["lease"]] = Lease(p["lease"], p["tenant"], p["unit"], p["rent_cents"], date.fromisoformat(p["start"]), list(p["schedule"]))
            self._seen("L", p["lease"])
        elif kind == "lease.ended":
            self.leases[p["lease"]].ended = date.fromisoformat(p["on"])
        elif kind == "charge.posted":
            c = Charge(p["charge"], p["lease"], self.leases[p["lease"]].tenant, p["kind"], p.get("obligation"), p["cents"],
                       date.fromisoformat(p["due"]), at, p.get("source", "billing"), self.seq)
            self.charges[c.id] = c
            self.of_tenant.setdefault(c.tenant, []).append(c.id)
            self.of_lease.setdefault(c.lease, []).append(c.id)
            if c.obligation:
                self.obligations.setdefault(c.obligation, []).append(c.id)
            self._seen("C", c.id)
        elif kind == "payment.declined":
            self.declined.append({**p, "at": at})
        elif kind == "payment.received":
            eff = datetime.fromisoformat(p["effective_at"])
            self.payments[p["payment"]] = Payment(p["payment"], p["tenant"], p["cents"], eff, at, p["applications"], p.get("credit_cents", 0))
            for a in p["applications"]:
                ch = self.charges[a["charge"]]
                ch.paid += a["cents"]
                ch.paid_effective.append((eff, a["cents"], p["payment"]))
            if p.get("credit_cents"):
                self.credit[p["tenant"]] = self.credit.get(p["tenant"], 0) + p["credit_cents"]
                self.lots.setdefault(p["tenant"], []).append([p["payment"], p["credit_cents"]])
            self._seen("P", p["payment"])
        elif kind == "payment.returned":
            pay = self.payments[p["payment"]]
            pay.returned = True
            for a in p["reopened"]:
                ch = self.charges[a["charge"]]
                ch.paid -= a["cents"]
            if p.get("credit_cents"):
                self.credit[p["tenant"]] = self.credit.get(p["tenant"], 0) - p["credit_cents"]
            for lot in self.lots.get(p["tenant"], []):
                if lot[0] == p["payment"]:
                    lot[1] = 0
        elif kind == "credit.applied":
            for a in p["applications"]:
                ch = self.charges[a["charge"]]
                ch.paid += a["cents"]
                ch.paid_effective.append((at, a["cents"], a["from"]))
                self.credit[p["tenant"]] -= a["cents"]
                for lot in self.lots[p["tenant"]]:
                    if lot[0] == a["from"]:
                        lot[1] -= a["cents"]
                self.credit_uses.setdefault(p["tenant"], []).append(CreditUse(a["from"], a["charge"], a["lease"], a["cents"]))
        elif kind in ("charge.reversed", "charge.waived"):
            ch = self.charges[p["charge"]]
            ch.closed = "reversed" if kind == "charge.reversed" else "waived"
            for a in p.get("released", []):
                ch.paid -= a["cents"]
                self.credit[p["tenant"]] = self.credit.get(p["tenant"], 0) + a["cents"]
                self.lots.setdefault(p["tenant"], []).append([f"release:{ch.id}", a["cents"]])
            if kind == "charge.waived" and ch.kind == LATE_FEE:     # the owner's yearly limit is on late-fee waivers only
                self.waivers.setdefault(p["tenant"], []).append(at)
        elif kind == "dispute.opened":
            hold = p.get("hold")
            self.disputes[p["dispute"]] = Dispute(p["dispute"], p["charge"], p["tenant"], at, date.fromisoformat(p["review_by"]), p["owner"], p["quote"], hold)
            if hold:
                self.holds[hold] = Hold(hold, p["charge"], p["dispute"], date.fromisoformat(p["review_by"]))
            self._seen("D", p["dispute"])
            if hold:
                self._seen("H", hold)
        elif kind == "dispute.decided":
            d = self.disputes[p["dispute"]]
            d.outcome = p["outcome"]
            if d.hold:
                self.holds[d.hold].active = False
        elif kind == "dispute.reopened":
            d = self.disputes[p["dispute"]]
            d.outcome = None
            d.review_by = date.fromisoformat(p["review_by"])
            if p.get("hold"):
                d.hold = p["hold"]
                self.holds[p["hold"]] = Hold(p["hold"], d.charge, d.id, date.fromisoformat(p["review_by"]))
                self._seen("H", p["hold"])

    # reads ------------------------------------------------------------------------------------------------------------
    def held(self, charge: str) -> bool:
        return any(h.active and h.charge == charge for h in self.holds.values())

    def tenant_charges(self, tenant: str) -> list[Charge]:
        return [self.charges[i] for i in self.of_tenant.get(tenant, ())]

    def open_charges(self, tenant: str, skip_held: bool = True) -> list[Charge]:
        """Open charges in the order money pays them: oldest due date first, rent before fees on the same date."""
        out = [c for c in self.tenant_charges(tenant) if c.open_cents > 0 and not (skip_held and self.held(c.id))]
        return sorted(out, key=lambda c: (c.due, c.kind != RENT, c.seq))

    def receivable(self, lease: str) -> int:
        return sum(self.charges[i].open_cents for i in self.of_lease.get(lease, ()))

    def past_due(self, tenant: str, now: datetime) -> int:
        """Unpaid parts past their due date, leaving out charges under an active hold."""
        today = local(now).date()
        return sum(c.open_cents for c in self.tenant_charges(tenant) if c.due < today and not self.held(c.id))

    def billed(self, obligation: str) -> list[Charge]:
        """Every charge posted for the obligation and not reversed (a waived copy was still billed), in posting order."""
        return [self.charges[i] for i in self.obligations.get(obligation, []) if self.charges[i].closed != "reversed"]

    def live(self, obligation: str) -> list[Charge]:
        return [self.charges[i] for i in self.obligations.get(obligation, []) if not self.charges[i].closed]

    def rent_paid_before(self, lease: str, month: str, when: datetime) -> int:
        """How much of the month's rent was paid by money whose effective time is before `when`, whenever we learned of it.
        Money that came back (a returned payment, or credit that came from one) never counts."""
        gone = {p.id for p in self.payments.values() if p.returned}
        return sum(sum(c for t, c, src in ch.paid_effective if t < when and src not in gone) for ch in self.live(f"rent:{lease}:{month}"))

    def waived_within(self, tenant: str, now: datetime, days: int = 365) -> int:
        return sum(1 for t in self.waivers.get(tenant, []) if now - t < timedelta(days=days))

    def on_time(self, tenant: str, month: str) -> bool:
        """Every lease the tenant held on the 1st had that month's rent fully paid, by effective time, before the cutoff."""
        y, m = map(int, month.split("-"))
        first = date(y, m, 1)
        held = [lease for lease in self.leases.values() if lease.tenant == tenant and lease.start < first
                and (lease.ended is None or lease.ended >= first)]
        if not held:
            return False
        for lease in held:
            due = sum(c.cents for c in self.live(f"rent:{lease.id}:{month}"))
            if due == 0 or self.rent_paid_before(lease.id, month, cutoff(month)) < due:
                return False
        return True


def fold(events) -> Books:
    b = Books()
    for e in events:
        b.apply(e.kind, e.at, e.payload)
    return b
