"""A generated year of one facility, Northstar Storage in Orlando, built only through the billing commands, so it exercises
the same code a real facility's history would.

Most tenants follow a payment habit drawn from a fixed seed. A cast of named tenants is scripted on top: each one carries a
situation the graded cases ask about (a late fee that stands, a fee posted twice, a fee missing from the signed schedule, a
payment that came back...). Which situation each cast member carries is known only here and in the tests; nothing in the
records marks a charge as planted. Every name, phone number and amount is made up."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from .billing import Ledger
from .books import ADMIN_FEE, LATE_FEE, at_local

SEED = 20261114
OPENED = date(2025, 11, 1)
TODAY = date(2026, 11, 14)          # the facility clock in step 1
NOW = at_local(TODAY, 9, 0)
UNITS = [f"{row}{n}" for row, first in (("A", 101), ("B", 201), ("C", 301)) for n in range(first, first + 40)]
RENTS = {"A": (8500, 12000), "B": (14000, 19000), "C": (21000, 26000)}
FIRST = ["Ana", "Ben", "Carla", "Dev", "Elena", "Femi", "Grace", "Hugo", "Ines", "Jon", "Kemi", "Luis", "Mona", "Nate", "Olga", "Pablo",
         "Rosa", "Sam", "Tara", "Uma", "Vic", "Wes", "Yara", "Zane", "Abby", "Cole", "Dina", "Eli", "Faye", "Gus"]
LAST = ["Alvarez", "Brooks", "Chen", "Diaz", "Evans", "Foster", "Garcia", "Hayes", "Ito", "Jensen", "Khan", "Lopez", "Moreno", "Nguyen",
        "Ortiz", "Patel", "Quinn", "Rivera", "Singh", "Turner", "Usman", "Vargas", "Walsh", "Young", "Zimmer"]
SCHEDULE = [ADMIN_FEE, LATE_FEE]
WAIVER_RULE = ("We waive one late fee per tenant in any rolling 12 months, up to $25, when the tenant paid on time for each of the six "
               "completed billing months before the fee. It may be offered when the tenant raises the fee.")
WAIVER_TYPED = {"action": "waive_fee", "fee_kind": LATE_FEE, "per_tenant": 1, "within_days": 365, "cap_cents": 2500,
                "on_time_months": 6, "only_when_raised": True}


def months_between(a: date, b: date):
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _d(month: str, day: int) -> date:
    y, m = map(int, month.split("-"))
    return date(y, m, day)


class Year:
    """Builds the year. `cast` maps a role name to its tenant, its leases and its unit."""

    def __init__(self, seed: int = SEED):
        self.rnd = random.Random(seed)
        self.led = Ledger()
        self.cast: dict[str, dict] = {}
        self.habit: dict[str, str] = {}          # tenant -> on_time, sometimes_late, chronic, scripted
        self.free = list(UNITS)
        self.used_names: set[str] = set()
        self.phones = iter(range(5552000, 5559999, 7))   # generated numbers stay clear of the cast's 555-01xx and 555-02xx
        self.actions: dict[date, list[tuple[datetime, int, object]]] = {}
        self.order = 0

    # helpers --------------------------------------------------------------------------------------------------------
    def at(self, when: datetime, fn) -> None:
        self.order += 1
        self.actions.setdefault(when.date(), []).append((when, self.order, fn))

    def name(self) -> str:
        while True:
            n = f"{self.rnd.choice(FIRST)} {self.rnd.choice(LAST)}"
            if n not in self.used_names:
                self.used_names.add(n)
                return n

    def unit(self, row: str | None = None) -> str:
        pool = [u for u in self.free if row is None or u.startswith(row)]
        u = pool[self.rnd.randrange(len(pool))]
        self.free.remove(u)
        return u

    def rent_for(self, unit: str) -> int:
        lo, hi = RENTS[unit[0]]
        return self.rnd.randrange(lo, hi + 1, 500)

    def move_in(self, when: datetime, habit: str, name: str | None = None, phone: str | None = None, row: str | None = None,
                schedule=None, rent: int | None = None, tenant: str | None = None) -> tuple[str, str]:
        b = self.led.books
        t = tenant or self.led.add_tenant(when - timedelta(minutes=5), name or self.name(), phone or f"+1407{next(self.phones)}")
        u = self.unit(row)
        lease = self.led.sign_lease(when, t, u, rent or self.rent_for(u), list(SCHEDULE if schedule is None else schedule))
        self.habit[t] = habit
        # the move-in payment covers what was just charged, except for tenants whose script says otherwise
        if habit != "scripted":
            self.led.receive_payment(t, sum(c.open_cents for c in b.open_charges(t)), when + timedelta(minutes=10))
        return t, lease

    def balance(self, t: str) -> int:
        return sum(c.open_cents for c in self.led.books.open_charges(t, skip_held=False))

    def pay_balance(self, t: str, when: datetime, share: float = 1.0) -> None:
        owed = self.balance(t)
        cents = int(owed * share) // 100 * 100 if share < 1 else owed
        if cents > 0:
            self.led.receive_payment(t, cents, when)

    # the year ---------------------------------------------------------------------------------------------------------
    def build(self) -> "Year":
        led = self.led
        led.set_up(at_local(OPENED - timedelta(days=10)), "Northstar Storage", "Orlando, FL", UNITS)
        led.adopt_rule(at_local(OPENED - timedelta(days=10), 9), "late_fee_waiver", 1, WAIVER_RULE, WAIVER_TYPED)
        for i in range(78):        # the first tenants move in over the first two weeks
            day = OPENED + timedelta(days=i % 14)
            habit = self.rnd.choices(["on_time", "sometimes_late", "chronic"], [62, 28, 10])[0]
            self.at(at_local(day, 9 + i % 9, (i * 7) % 60), lambda h=habit, d=day, k=i: self.move_in(at_local(d, 9 + k % 9, (k * 7) % 60), h))
        self.plant()
        for month in months_between(OPENED, TODAY):
            self.plan_month(month)
        day = OPENED - timedelta(days=10)
        while day <= TODAY:
            done: set[int] = set()
            while True:         # an action may plan more actions for the same day, so the day's list is re-read each time
                todo = sorted((a for a in self.actions.get(day, []) if a[1] not in done and a[0] <= NOW), key=lambda a: (a[0], a[1]))
                if not todo:
                    break
                when, n, fn = todo[0]
                done.add(n)
                fn()
            day += timedelta(days=1)
        return self

    def plan_month(self, month: str) -> None:
        first = _d(month, 1)
        if first > OPENED:
            self.at(at_local(first), lambda m=month: self.led.post_rent(m))
            self.at(at_local(first, 0, 1), lambda m=month: self.schedule_payments(m))
        if _d(month, 6) > OPENED:
            self.at(at_local(_d(month, 6)), lambda m=month: self.led.assess_late_fees(m))
        if first > OPENED:
            for k in range(2):               # new tenants
                day = _d(month, 3 + self.rnd.randrange(24))
                habit = self.rnd.choices(["on_time", "sometimes_late", "chronic"], [62, 28, 10])[0]
                when = at_local(day, 10 + k * 3, 15)
                self.at(when, lambda h=habit, w=when: self.free and self.move_in(w, h))
            self.at(at_local(_d(month, 24), 16), lambda m=month: self.move_out_one(m))

    def schedule_payments(self, month: str) -> None:
        """At the start of each month, each habit tenant's payment day is drawn."""
        b = self.led.books
        active = {lease.tenant for lease in b.leases.values() if lease.ended is None}
        for t in sorted(active):
            habit = self.habit.get(t)
            if habit in (None, "scripted"):
                continue
            if habit == "on_time" or (habit == "sometimes_late" and self.rnd.random() < 0.7):
                day, share = 1 + self.rnd.randrange(4), 1.0
            elif habit == "sometimes_late":
                day, share = 7 + self.rnd.randrange(8), 1.0
            else:
                day, share = 7 + self.rnd.randrange(14), (0.6 if self.rnd.random() < 0.3 else 1.0)
            when = at_local(_d(month, day), 8 + self.rnd.randrange(13), self.rnd.randrange(60))
            if self.rnd.random() < 0.04:     # a card that declines first, then goes through a day later
                self.at(when, lambda t=t, w=when: self.balance(t) and self.led.decline(w, t, self.balance(t)))
                when += timedelta(days=1)
            if when <= NOW:
                self.at(when, lambda t=t, w=when, s=share: self.pay_balance(t, w, s))

    def move_out_one(self, month: str) -> None:
        b = self.led.books
        leaving = [lease for lease in b.leases.values() if lease.ended is None and self.habit.get(lease.tenant) == "on_time"
                   and sum(1 for x in b.leases.values() if x.tenant == lease.tenant) == 1]
        if not leaving:
            return
        lease = leaving[self.rnd.randrange(len(leaving))]
        when = at_local(_d(month, 24), 16)
        self.pay_balance(lease.tenant, when)
        y, m = map(int, month.split("-"))
        last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
        self.at(at_local(last, 18), lambda lid=lease.id, d=last, u=lease.unit: (self.led.end_lease(at_local(d, 18), lid), self.free.append(u)))

    # the cast ---------------------------------------------------------------------------------------------------------
    def role(self, role: str, when: datetime, name: str, phone: str, *, row: str | None = None, schedule=None, rent: int | None = None,
             on_time_until: str | None = "2026-11", skip: tuple[str, ...] = ()) -> None:
        """A scripted tenant: moves in at `when` and pays the full balance on the 2nd of every month up to `on_time_until`,
        except the months in `skip`, which its own script handles."""
        def go():
            t, lease = self.move_in(when, "scripted", name=name, phone=phone, row=row, schedule=schedule, rent=rent)
            self.led.receive_payment(t, self.balance(t), when + timedelta(minutes=10))
            self.cast[role] = {"tenant": t, "leases": [lease], "name": name, "phone": phone}
            for month in months_between(when.date() + timedelta(days=1), TODAY):
                if month in skip or (on_time_until and month > on_time_until) or _d(month, 2) <= when.date():
                    continue
                self.at(at_local(_d(month, 2), 10, 30), lambda r=role, m=month: self.pay_balance(self.cast[r]["tenant"], at_local(_d(m, 2), 10, 30)))
        self.at(when, go)

    def then(self, when: datetime, fn) -> None:
        self.at(when, fn)

    def t(self, role: str) -> str:
        return self.cast[role]["tenant"]

    def lease(self, role: str, i: int = 0) -> str:
        return self.cast[role]["leases"][i]

    def plant(self) -> None:
        """Each planted situation sits in November, the facility's current month, so no later payment has paid it off."""
        led, d = self.led, lambda y, m, day, h=10, mi=0: at_local(date(y, m, day), h, mi)
        nov = "2026-11"

        # maria: a declined try on Nov 3, the payment through on Nov 6 after the fee; late in August, so the yearly waiver
        # does not apply and the fee stands
        self.role("maria", d(2025, 12, 3), "Maria Lopez", "+14075550111", row="A", rent=12000, skip=("2026-08", nov))
        self.then(d(2026, 8, 9, 15), lambda: self.pay_balance(self.t("maria"), d(2026, 8, 9, 15)))
        self.then(d(2026, 11, 3, 10, 15), lambda: led.decline(d(2026, 11, 3, 10, 15), self.t("maria"), 12000))
        self.then(d(2026, 11, 6, 14, 20), lambda: led.receive_payment(self.t("maria"), 12000, d(2026, 11, 6, 14, 20)))

        # wes: the same days as maria, but on time for the six months before: the yearly waiver applies, a real choice
        self.role("wes", d(2025, 11, 20), "Wes Young", "+14075550288", row="A", rent=11500, skip=(nov,))
        self.then(d(2026, 11, 3, 19), lambda: led.decline(d(2026, 11, 3, 19), self.t("wes"), 11500))
        self.then(d(2026, 11, 6, 8, 45), lambda: led.receive_payment(self.t("wes"), 11500, d(2026, 11, 6, 8, 45)))

        # james: November's late fee posted by billing, and the same fee posted again by hand on Nov 10
        self.role("james", d(2026, 2, 10), "James Carter", "+14075550122", row="B", rent=16000, skip=(nov,))
        self.then(d(2026, 11, 8, 12), lambda: led.receive_payment(self.t("james"), 16000, d(2026, 11, 8, 12)))
        self.then(d(2026, 11, 10, 9, 40), lambda: led.charge_by_hand(d(2026, 11, 10, 9, 40), self.lease("james"), LATE_FEE, 2000,
                                                                    f"late_fee:{self.lease('james')}:{nov}", note="late fee November"))

        # priya: signed in June on a schedule without the admin fee; the front desk posted one by hand on Nov 10
        self.role("priya", d(2026, 6, 15), "Priya Shah", "+14075550133", row="A", rent=9500, schedule=[LATE_FEE])
        self.then(d(2026, 11, 10, 11), lambda: led.charge_by_hand(d(2026, 11, 10, 11), self.lease("priya"), ADMIN_FEE, 2500,
                                                                 f"admin_fee:{self.lease('priya')}", note="admin fee missed at move-in"))

        # tom: on time March to August, nothing paid since; September's fee is the one the yearly rule allows
        self.role("tom", d(2025, 11, 5), "Tom Becker", "+14075550144", row="C", rent=22000, on_time_until="2026-08")

        # gus: nothing paid since August either, three equal late fees for three months, no waiver history
        self.role("gus", d(2026, 1, 6), "Gus Walsh", "+14075550211", row="A", rent=8500, on_time_until="2026-08")

        # ines: November's rent paid on Nov 3, and a late fee posted by hand on Nov 4 anyway
        self.role("ines", d(2026, 1, 12), "Ines Moreno", "+14075550155", row="A", rent=10000, skip=(nov,))
        self.then(d(2026, 11, 3, 9), lambda: self.pay_balance(self.t("ines"), d(2026, 11, 3, 9)))
        self.then(d(2026, 11, 4, 16), lambda: led.charge_by_hand(d(2026, 11, 4, 16), self.lease("ines"), LATE_FEE, 2000,
                                                                f"late_fee:{self.lease('ines')}:{nov}", note="late fee"))

        # hugo: confirmed by the processor on Nov 5, learned on Nov 7; billing posted the fee on what it knew
        self.role("hugo", d(2026, 3, 20), "Hugo Brooks", "+14075550166", row="B", rent=15000, skip=(nov,))
        self.then(d(2026, 11, 7, 9), lambda: led.receive_payment(self.t("hugo"), 15000, d(2026, 11, 5, 15), d(2026, 11, 7, 9)))

        # cole: paid Nov 2, the payment came back Nov 9, so November's late fee posted then
        self.role("cole", d(2026, 1, 25), "Cole Jensen", "+14075550199", row="B", rent=14500, skip=(nov,))
        self.then(d(2026, 11, 2, 13), lambda: led.receive_payment(self.t("cole"), 14500, d(2026, 11, 2, 13)))
        self.then(d(2026, 11, 9, 10), lambda: led.return_payment(d(2026, 11, 9, 10), max(p for p, x in led.books.payments.items() if x.tenant == self.t("cole"))))

        # tara: paid part of November before the cutoff
        self.role("tara", d(2026, 2, 17), "Tara Singh", "+14075550277", row="B", rent=18000, skip=(nov,))
        self.then(d(2026, 11, 4, 18), lambda: led.receive_payment(self.t("tara"), 10000, d(2026, 11, 4, 18)))

        # rosa: one late fee in November, on time for the six months before: the yearly waiver applies
        self.role("rosa", d(2025, 12, 1), "Rosa Garcia", "+14075550222", row="B", rent=17000, skip=(nov,))
        self.then(d(2026, 11, 8, 17), lambda: led.receive_payment(self.t("rosa"), 17000, d(2026, 11, 8, 17)))

        # ben: a late fee waived in March; November's fee is open, so the yearly waiver is used
        self.role("ben", d(2025, 11, 10), "Ben Hayes", "+14075550233", row="C", rent=21500, skip=("2026-03", nov))
        self.then(d(2026, 3, 8, 10), lambda: led.receive_payment(self.t("ben"), 21500, d(2026, 3, 8, 10)))
        self.then(d(2026, 3, 12, 14), lambda: led.waive(d(2026, 3, 12, 14), led.books.live(f"late_fee:{self.lease('ben')}:2026-03")[0].id,
                                                        "yearly late fee waiver", "staff:office_manager"))
        self.then(d(2026, 11, 8, 10), lambda: led.receive_payment(self.t("ben"), 21500, d(2026, 11, 8, 10)))

        # faye: two card payments on Nov 2; the second became credit
        self.role("faye", d(2026, 2, 3), "Faye Turner", "+14075550200", row="A", rent=11000, skip=(nov,))
        self.then(d(2026, 11, 2, 9, 1), lambda: led.receive_payment(self.t("faye"), 11000, d(2026, 11, 2, 9, 1)))
        self.then(d(2026, 11, 2, 9, 3), lambda: led.receive_payment(self.t("faye"), 11000, d(2026, 11, 2, 9, 3)))

        # dev: two units, two rents on the 1st, both paid
        self.role("dev", d(2025, 11, 8), "Dev Patel", "+14075550177", row="A", rent=9000)
        self.then(d(2026, 4, 2, 11), lambda: self.cast["dev"]["leases"].append(led.sign_lease(d(2026, 4, 2, 11), self.t("dev"), self.unit("B"), 15500, SCHEDULE)))
        self.then(d(2026, 4, 2, 11, 10), lambda: self.pay_balance(self.t("dev"), d(2026, 4, 2, 11, 10)))

        # yara: moved in Oct 20, a prorated October and a full November, both paid
        self.role("yara", d(2026, 10, 20), "Yara Quinn", "+14075550188", row="C", rent=24000)

        # luis: signed for the admin fee at move-in and paid it
        self.role("luis", d(2026, 9, 14), "Luis Ortiz", "+14075550266", row="C", rent=23000)

        # olga: moved out on Sep 30 with September's late fee still owed
        self.role("olga", d(2025, 11, 12), "Olga Rivera", "+14075550255", row="B", rent=14000, on_time_until="2026-08")
        self.then(d(2026, 9, 25, 12), lambda: led.receive_payment(self.t("olga"), 14000, d(2026, 9, 25, 12)))
        self.then(d(2026, 9, 30, 18), lambda: led.end_lease(d(2026, 9, 30, 18), self.lease("olga")))

        # kemi and femi: a family sharing one phone number
        self.role("kemi", d(2026, 5, 4), "Kemi Usman", "+14075550244", row="A", rent=9500)
        self.role("femi", d(2026, 5, 4, 11), "Femi Usman", "+14075550244", row="A", rent=9000)


def build(seed: int = SEED) -> Year:
    return Year(seed).build()
