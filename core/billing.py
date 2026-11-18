"""The billing contract, as commands that turn the current books into new events.

A command reads the books, decides, and emits events; emitting applies each event to the books at once, so the next command
sees it. Nothing here writes to the database: the caller appends `ledger.pending` inside one transaction. Every amount is
computed here, never taken from a model."""
from __future__ import annotations

import calendar
from copy import deepcopy
from datetime import date, datetime, timedelta

from .books import (ADMIN_FEE, ADMIN_FEE_CENTS, FEE_KINDS, LATE_FEE, LATE_FEE_CENTS, RENT, REVIEW_DAYS, Books, at_local, cutoff,
                    local, month_key)
from .store import Event


class Refused(Exception):
    """A command whose preconditions do not hold; nothing was emitted."""


class Ledger:
    def __init__(self, books: Books | None = None):
        self.books = books or Books()
        self.pending: list[Event] = []

    def emit(self, kind: str, at: datetime, payload: dict, key: str, source: str = "billing", effects: tuple[str, ...] = (),
             command: str | None = None) -> Event:
        e = Event(kind=kind, at=at, payload=payload, key=key, source=source, command=command, effects=effects)
        self.books.apply(kind, at, payload)
        self.pending.append(e)
        return e

    # the facility and its tenants ---------------------------------------------------------------------------------------
    def set_up(self, at: datetime, name: str, city: str, units: list[str]) -> None:
        self.emit("facility.set_up", at, {"name": name, "city": city, "time_zone": "America/New_York", "units": units}, "facility")

    def adopt_rule(self, at: datetime, rule: str, version: int, sentence: str, typed: dict, by: str = "owner",
                   restores: int | None = None) -> None:
        """Record an owner rule. Only the owner adopts rules (step 2 adds the approval path); the typed form is checked here so a
        rule outside the platform's bounds is never stored. `restores` names the earlier version a going back repeats."""
        if by != "owner":
            raise Refused("only the owner adopts rules")
        current = self.books.rules.get(rule, {}).get("version", 0)
        if version <= current:
            raise Refused(f"rule {rule} is already at version {current}")
        if typed.get("action") == "waive_fee":
            bounds = {"per_tenant": (1, 12), "within_days": (1, 3660), "cap_cents": (0, 10000), "on_time_months": (0, 24)}
            optional = {"min_tenure_days": (0, 3660), "max_fee_age_days": (1, 3660)}
            for key, (lo, hi) in bounds.items():
                if not isinstance(typed.get(key), int) or not lo <= typed[key] <= hi:
                    raise Refused(f"the rule's {key} is outside the platform's bounds")
            for key, (lo, hi) in optional.items():
                if key in typed and not (isinstance(typed[key], int) and lo <= typed[key] <= hi):
                    raise Refused(f"the rule's {key} is outside the platform's bounds")
            for key in ("only_when_raised", "no_rent_owed", "current_rent_paid"):
                if key in typed and not isinstance(typed[key], bool):
                    raise Refused(f"the rule's {key} must be yes or no")
            if typed.get("fee_kind") not in FEE_KINDS:
                raise Refused("a waiver rule may only waive a fee, never rent")
        elif typed.get("action") == "send_to_staff":
            from .agent import FLAGS
            from .evidence import CLAIM_KINDS
            allowed = {"flags": set(FLAGS), "claim_kinds": set(CLAIM_KINDS),
                       "actions": {"explain_charge", "reverse_duplicate", "correct_fee_posted_in_error", "open_dispute", "waive_fee"}}
            for key, ok in allowed.items():
                if not set(typed.get(key, [])) <= ok:
                    raise Refused(f"the rule names a {key[:-1].replace('_', ' ')} the system does not know")
            if not (any(typed.get(k) for k in allowed) or typed.get("several_units") is True):
                raise Refused("a staff rule needs at least one condition")
        else:
            raise Refused("not a kind of rule the platform knows")
        payload = {"rule": rule, "version": version, "sentence": sentence, "typed": typed, "by": by}
        if restores is not None:
            payload["restores"] = restores
        self.emit("rule.adopted", at, payload, f"rule:{rule}:{version}", source=by)

    def restore_rule(self, at: datetime, rule: str, version: int, by: str = "owner") -> None:
        """Go back to an earlier version: its typed form is adopted again as the next version, so history is never rewritten.
        Refused for anyone but the owner, for a version that was never adopted, and for one that says what is in force already."""
        if by != "owner":
            raise Refused("only the owner can go back to an earlier version")
        held = self.books.rules.get(rule)
        old = next((h for h in self.books.rule_history.get(rule, []) if h["version"] == version), None)
        if held is None or old is None:
            raise Refused(f"there is no version {version} of that rule")
        if old["typed"] == held["typed"]:
            raise Refused("that version already says what is in force")
        self.adopt_rule(at, rule, held["version"] + 1, old["sentence"], deepcopy(old["typed"]), by, restores=version)

    def add_tenant(self, at: datetime, name: str, phone: str) -> str:
        t = self.books.next_id("T")
        self.emit("tenant.added", at, {"tenant": t, "name": name, "phone": phone}, f"tenant:{t}", source="staff:front_desk")
        return t

    def sign_lease(self, at: datetime, tenant: str, unit: str, rent_cents: int, schedule: list[str]) -> str:
        """A move-in: the lease with the fees the tenant signed for, the prorated first month, and the admin fee if signed for."""
        lease = self.books.next_id("L")
        start = local(at).date()
        self.emit("lease.signed", at, {"lease": lease, "tenant": tenant, "unit": unit, "rent_cents": rent_cents, "start": start.isoformat(),
                                       "schedule": schedule}, f"lease:{lease}", source="staff:front_desk")
        days = calendar.monthrange(start.year, start.month)[1]
        first = rent_cents * (days - start.day + 1) // days
        self._charge(at, lease, RENT, first, f"rent:{lease}:{month_key(start)}", start)
        if ADMIN_FEE in schedule:
            self._charge(at, lease, ADMIN_FEE, ADMIN_FEE_CENTS, f"admin_fee:{lease}", start)
        return lease

    def end_lease(self, at: datetime, lease: str) -> None:
        self.emit("lease.ended", at, {"lease": lease, "on": local(at).date().isoformat()}, f"lease-end:{lease}", source="staff:front_desk")

    # charges ----------------------------------------------------------------------------------------------------------
    def _charge(self, at: datetime, lease: str, kind: str, cents: int, obligation: str | None, due: date, source: str = "billing",
                key: str | None = None, note: str | None = None) -> str:
        c = self.books.next_id("C")
        payload = {"charge": c, "lease": lease, "kind": kind, "cents": cents, "obligation": obligation, "due": due.isoformat(), "source": source}
        if note:
            payload["note"] = note
        self.emit("charge.posted", at, payload, key or (obligation if source == "billing" and obligation else f"charge:{c}"), source=source)
        self.apply_credit(at, self.books.leases[lease].tenant)
        return c

    def post_rent(self, month: str) -> list[str]:
        """Rent for every lease active on the 1st, at 00:00 local. The obligation is the key, so billing cannot post twice."""
        y, m = map(int, month.split("-"))
        first = date(y, m, 1)
        out = []
        for lease in self.books.leases.values():
            if lease.start < first and (lease.ended is None or lease.ended >= first) and not self.books.live(f"rent:{lease.id}:{month}"):
                out.append(self._charge(at_local(first), lease.id, RENT, lease.rent_cents, f"rent:{lease.id}:{month}", first))
        return out

    def assess_late_fees(self, month: str) -> list[str]:
        """At 00:00 local on the 6th: a late fee on every lease whose rent for the month still has an unpaid part, as far as the
        books know at that moment."""
        at = cutoff(month)
        out = []
        for lease in self.books.leases.values():
            rent = self.books.live(f"rent:{lease.id}:{month}")
            # rent under an active hold cannot be paid, so it never brings a late fee (it is not past due either)
            if rent and any(c.open_cents > 0 and not self.books.held(c.id) for c in rent) and not self.books.live(f"late_fee:{lease.id}:{month}"):
                out.append(self._charge(at, lease.id, LATE_FEE, LATE_FEE_CENTS, f"late_fee:{lease.id}:{month}", local(at).date()))
        return out

    def charge_by_hand(self, at: datetime, lease: str, kind: str, cents: int, obligation: str | None, due: date | None = None,
                       note: str | None = None, role: str = "front_desk") -> str:
        """A charge a staff member posts by hand. It must name what it bills to ever be corrected automatically; it may not."""
        return self._charge(at, lease, kind, cents, obligation, due or local(at).date(), source=f"staff:{role}", note=note)

    # payments ---------------------------------------------------------------------------------------------------------
    def _spread(self, tenant: str, cents: int) -> tuple[list[dict], int]:
        """Pay open charges in order (oldest due first, rent before fees, held charges skipped); what is left over is credit."""
        apps, left = [], cents
        for c in self.books.open_charges(tenant):
            if left == 0:
                break
            take = min(left, c.open_cents)
            apps.append({"charge": c.id, "lease": c.lease, "cents": take})
            left -= take
        return apps, left

    def decline(self, at: datetime, tenant: str, cents: int, method: str = "card") -> None:
        n = len(self.books.declined) + 1
        self.emit("payment.declined", at, {"tenant": tenant, "cents": cents, "method": method}, f"declined:{tenant}:{n}", source="processor")

    def receive_payment(self, tenant: str, cents: int, effective_at: datetime, known_at: datetime | None = None, method: str = "card",
                        ref: dict | None = None) -> str:
        """A payment the processor confirmed at `effective_at`, which we learned of at `known_at` (the same moment unless the
        news came late). Its applications are written now and never moved afterwards. `ref` names the processor's own payment
        and the copy it was made on, when it came through one."""
        if cents <= 0:
            raise Refused("a payment must be more than zero")
        p = self.books.next_id("P")
        apps, left = self._spread(tenant, cents)
        self.emit("payment.received", known_at or effective_at, {"payment": p, "tenant": tenant, "cents": cents, "method": method,
                  "effective_at": effective_at.isoformat(), "applications": apps, "credit_cents": left, **(ref or {})}, f"payment:{p}",
                  source="processor")
        return p

    def apply_credit(self, at: datetime, tenant: str) -> None:
        """Credit the tenant holds pays open charges in the usual order, oldest credit first."""
        lots = [lot for lot in self.books.lots.get(tenant, []) if lot[1] > 0]
        if not lots:
            return
        apps = []
        remaining = {lot[0]: lot[1] for lot in lots}
        for c in self.books.open_charges(tenant):
            need = c.open_cents
            for src in list(remaining):
                if need == 0:
                    break
                take = min(need, remaining[src])
                if take:
                    apps.append({"charge": c.id, "lease": c.lease, "cents": take, "from": src})
                    remaining[src] -= take
                    need -= take
        if apps:
            n = len(self.books.credit_uses.get(tenant, [])) + 1
            self.emit("credit.applied", at, {"tenant": tenant, "applications": apps}, f"credit:{tenant}:{n}")

    def return_payment(self, at: datetime, payment: str) -> None:
        """The processor took a payment back. Every charge it paid, directly or through the credit it left, is reopened; credit
        it left that was never used is taken back. If it reopens rent whose cutoff has passed, that month's late fee posts now."""
        pay = self.books.payments[payment]
        if pay.returned:
            raise Refused("that payment was already returned")
        reopened = [dict(a) for a in pay.applications]
        uses = [u for u in self.books.credit_uses.get(pay.tenant, []) if u.lot == payment]
        reopened += [{"charge": u.charge, "lease": u.lease, "cents": u.cents} for u in uses]
        # ponytail: a charge since reversed or waived cannot be reopened; such a return goes to staff, not into these books
        if any(self.books.charges[a["charge"]].closed for a in reopened):
            raise Refused("a charge this payment paid has since been reversed or waived; staff handle this return")
        unused = next((lot[1] for lot in self.books.lots.get(pay.tenant, []) if lot[0] == payment), 0)
        self.emit("payment.returned", at, {"payment": payment, "tenant": pay.tenant, "cents": pay.cents, "reopened": reopened,
                                           "credit_cents": unused}, f"returned:{payment}", source="processor")
        for a in reopened:
            ch = self.books.charges[a["charge"]]
            month = ch.obligation.split(":")[2] if ch.kind == RENT and ch.obligation else None
            # only rent due on the 1st has the late-fee cutoff; a prorated first month is due on its move-in day
            # one late fee per lease per month, ever: a fee already posted for it (even waived or reversed) is not posted again
            if (month and ch.due.day == 1 and at >= cutoff(month) and not self.books.held(ch.id)
                    and not self.books.obligations.get(f"late_fee:{ch.lease}:{month}")):
                self._charge(at, ch.lease, LATE_FEE, LATE_FEE_CENTS, f"late_fee:{ch.lease}:{month}", local(at).date())

    # corrections, waivers and disputes (the gate decides whether they may happen; these only do them) ---------------------
    def _release(self, charge: str) -> list[dict]:
        ch = self.books.charges[charge]
        return [{"charge": ch.id, "lease": ch.lease, "cents": ch.paid}] if ch.paid > 0 else []

    def reverse(self, at: datetime, charge: str, reason: str, source: str, command: str | None = None) -> None:
        ch = self.books.charges[charge]
        self.emit("charge.reversed", at, {"charge": charge, "lease": ch.lease, "tenant": ch.tenant, "kind": ch.kind, "cents": ch.cents,
                                          "reason": reason, "released": self._release(charge)},
                  f"reverse:{charge}", source=source, effects=(f"reverse:{charge}",), command=command)
        self.apply_credit(at, ch.tenant)

    def waive(self, at: datetime, charge: str, rule: str, source: str, command: str | None = None) -> None:
        ch = self.books.charges[charge]
        self.emit("charge.waived", at, {"charge": charge, "lease": ch.lease, "tenant": ch.tenant, "kind": ch.kind, "cents": ch.cents,
                                        "rule": rule, "released": self._release(charge)},
                  f"waive:{charge}", source=source, effects=(f"waive:{charge}",), command=command)
        self.apply_credit(at, ch.tenant)

    def open_dispute(self, at: datetime, charge: str, quote: str, hold: bool, source: str, owner: str = "office_manager",
                     command: str | None = None) -> str:
        ch = self.books.charges[charge]
        d = self.books.next_id("D")
        review_by = local(at).date() + timedelta(days=REVIEW_DAYS)
        h = self.books.next_id("H") if hold else None
        self.emit("dispute.opened", at, {"dispute": d, "charge": charge, "tenant": ch.tenant, "quote": quote, "owner": owner,
                                         "review_by": review_by.isoformat(), "hold": h}, f"dispute:{d}", source=source, command=command)
        return d

    def decide_dispute(self, at: datetime, dispute: str, outcome: str, role: str = "office_manager") -> None:
        d = self.books.disputes[dispute]
        n = sum(1 for e in self.pending if e.kind == "dispute.decided") + 1
        self.emit("dispute.decided", at, {"dispute": dispute, "outcome": outcome}, f"decided:{dispute}:{local(at).isoformat()}:{n}",
                  source=f"staff:{role}")
        self.apply_credit(at, d.tenant)        # a hold that ends lets credit reach the charge

    def reopen_dispute(self, at: datetime, dispute: str, evidence: str, hold: bool, role: str = "office_manager") -> None:
        h = self.books.next_id("H") if hold else None
        review_by = local(at).date() + timedelta(days=REVIEW_DAYS)
        self.emit("dispute.reopened", at, {"dispute": dispute, "evidence": evidence, "hold": h, "review_by": review_by.isoformat()},
                  f"reopened:{dispute}:{local(at).isoformat()}", source=f"staff:{role}")
