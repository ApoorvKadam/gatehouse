"""A generated year of one facility, Northstar Storage in Orlando, built only through the billing commands, so it exercises
the same code a real facility's history would.

Most tenants follow a payment habit drawn from a fixed seed. A cast of named tenants is scripted on top: each one carries a
situation the graded cases ask about (a late fee that stands, a fee posted twice, a fee missing from the signed schedule, a
payment that came back...). Which situation each cast member carries is known only here and in the tests; nothing in the
records marks a charge as planted. Every name, phone number and amount is made up."""
from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta

from . import delinquency as col
from . import renting
from . import liens
from .gate import Command, evaluate
from .billing import Ledger
from .books import ADMIN_FEE, LATE_FEE, LATE_FEE_CENTS, RENT, at_local, cutoff
from .evidence import month_words

SEED = 20261114
TODAY = date(2026, 11, 14)          # the fixed day the tests and the graded sets are read against
NOW = at_local(TODAY, 9, 0)
STORY_DONE = 11                     # the planted situations run from the 2nd to the 10th of their month
NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
# a month the written messages name: in full (not "may", which is also a verb), or short before a day ("Nov 5")
MONTH_WORD = re.compile(r"\b(" + "|".join(n for n in NAMES if n != "May") + r"|(?:" + "|".join(n[:3] for n in NAMES) + r")(?=\.? \d))\b", re.I)
BARE_DAY = re.compile(r"\bthe (\d{1,2})(st|nd|rd|th)\b(?! of)", re.I)       # "the 8th of November" already names it
A_LATE_FEE = re.compile(r"\ba (\$\d+ )?late fee\b(?! for)", re.I)
UNITS = [f"{row}{n}" for row, first in (("A", 101), ("B", 201), ("C", 301)) for n in range(first, first + 40)]
RENTS = {"A": (8500, 12000), "B": (14000, 19000), "C": (21000, 26000)}
SIZES = {"A": "5x10", "B": "10x10", "C": "10x20"}            # the live copy's unit sizes, by row
FIRST = ["Ana", "Ben", "Carla", "Dev", "Elena", "Femi", "Grace", "Hugo", "Ines", "Jon", "Kemi", "Luis", "Mona", "Nate", "Olga", "Pablo",
         "Rosa", "Sam", "Tara", "Uma", "Vic", "Wes", "Yara", "Zane", "Abby", "Cole", "Dina", "Eli", "Faye", "Gus"]
LAST = ["Alvarez", "Brooks", "Chen", "Diaz", "Evans", "Foster", "Garcia", "Hayes", "Ito", "Jensen", "Khan", "Lopez", "Moreno", "Nguyen",
        "Ortiz", "Patel", "Quinn", "Rivera", "Singh", "Turner", "Usman", "Vargas", "Walsh", "Young", "Zimmer"]
SCHEDULE = [ADMIN_FEE, LATE_FEE]
STANDING_QUOTE = "My move-in special said no admin fee. Please take the $25 off."
WAIVER_RULE = ("We waive one late fee per tenant in any rolling 12 months, up to $25, when the tenant paid on time for each of the six "
               "completed billing months before the fee. It may be offered when the tenant raises the fee.")
WAIVER_TYPED = {"action": "waive_fee", "fee_kind": LATE_FEE, "per_tenant": 1, "within_days": 365, "cap_cents": 2500,
                "on_time_months": 6, "only_when_raised": True}
# the live copy's plan policy and schedule (the owner's settings, never law); the fixed books have neither
PLAN_POLICY = {"min_tenure_days": 14, "max_debt_cents": 150000, "longest_days": 90, "most_instalments": 6, "min_upfront_cents": 5000,
               "min_instalment_cents": 2500, "grace_days": 3}
OVERLOCK_DAYS, LIEN_DAYS = 10, 45
PLAN_CONSENT = "Yes, $50 today and the rest in three payments works for me."
BREACHED_CONSENT = "OK, $50 now and five payments after that. I can do that."


def months_between(a: date, b: date):
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _d(month: str, day: int) -> date:
    y, m = map(int, month.split("-"))
    return date(y, m, day)


def add_months(d: date, n: int) -> date:
    """The first of the month n months from d's month."""
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def story_month(today: date) -> date:
    """The month the planted situations sit in: this month once its story days have passed, else the month before."""
    return add_months(today, 0 if today.day >= STORY_DONE else -1)


class Year:
    """Builds the year. `cast` maps a role name to its tenant, its leases and its unit."""

    def __init__(self, seed: int = SEED, today: date = TODAY, cutoff: datetime | None = None, standing: bool = False):
        # everything up to the cutoff happens: 09:00 on the fixed day; the live FMS passes midnight, so nothing is ahead of the clock
        self.today, self.now = today, cutoff or at_local(today, 9, 0)
        self.standing = standing                         # the live copy's open dispute from last week; never in the fixed books
        self.plan_tenant: dict | None = None             # the live copy's tenant on a payment plan; kept out of the cast
        self.breached_tenant: dict | None = None         # the live copy's tenant whose plan breached; kept out of the cast
        self.story = story_month(today)                  # every date below is counted in months from this one
        self.opened = add_months(self.story, -12)
        self.rnd = random.Random(seed)
        self.later = random.Random(seed + 1)             # the month after the story, drawn apart so it never moves the year before
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
        opened = self.opened
        led.set_up(at_local(opened - timedelta(days=10)), "Northstar Storage", "Orlando, FL", UNITS)
        led.adopt_rule(at_local(opened - timedelta(days=10), 9), "late_fee_waiver", 1, WAIVER_RULE, WAIVER_TYPED)
        if self.standing:
            col.adopt_policy(led, at_local(opened - timedelta(days=10), 9, 5), 1, PLAN_POLICY)
            col.adopt_schedule(led, at_local(opened - timedelta(days=10), 9, 5), 1, OVERLOCK_DAYS, LIEN_DAYS)
            self.price_units(at_local(opened - timedelta(days=10), 9, 10))
        for i in range(78):        # the first tenants move in over the first two weeks
            day = opened + timedelta(days=i % 14)
            habit = self.rnd.choices(["on_time", "sometimes_late", "chronic"], [62, 28, 10])[0]
            self.at(at_local(day, 9 + i % 9, (i * 7) % 60), lambda h=habit, d=day, k=i: self.move_in(at_local(d, 9 + k % 9, (k * 7) % 60), h))
        self.plant()
        if self.standing:
            self.standing_dispute()
        for month in months_between(opened, self.story):
            self.plan_month(month, self.rnd)
        self.plan_month(self.key(1), self.later)
        day = opened - timedelta(days=10)
        while day <= self.today:
            done: set[int] = set()
            while True:         # an action may plan more actions for the same day, so the day's list is re-read each time
                todo = sorted((a for a in self.actions.get(day, []) if a[1] not in done and a[0] <= self.now), key=lambda a: (a[0], a[1]))
                if not todo:
                    break
                when, n, fn = todo[0]
                done.add(n)
                fn()
            day += timedelta(days=1)
        if self.standing:
            self.plan_breached()
            self.on_a_plan()
            self.cards_on_file()
            self.lien_file()
            rent = renting.Rent(self.led.books)
            for event in self.led.pending:
                rent.apply(event.kind, event.at, event.payload)
            adopted = renting.RentLedger(self.led.books, rent)
            for lease in self.led.books.leases.values():
                if lease.ended is None:
                    rental = renting.adopt_existing_lease(adopted, lease.id, "seeded_existing", self.now)
                    renting.record_baseline_access(adopted, rental, self.now)       # the code each tenant already holds on the simulated gate
            self.led.pending.extend(adopted.pending)
        return self

    def price_units(self, at: datetime) -> None:
        """The live copy gives every unit a size and an asking price, as renting's price_unit records them, so a vacant unit can be
        rented on a fresh copy. Worked out from the unit's name and never drawn, so no other draw of the year moves."""
        for u in UNITS:
            lo, hi = RENTS[u[0]]
            rent = lo + 500 * (int(u[1:]) * 3 % ((hi - lo) // 500 + 1))
            self.led.emit("unit.priced", at, {"unit": u, "rent_cents": rent, "size": SIZES[u[0]]}, f"priced:{u}", source="staff:front_desk")

    def plan_month(self, month: str, rnd: random.Random) -> None:
        first = _d(month, 1)
        if first > self.opened:
            self.at(at_local(first), lambda m=month: self.led.post_rent(m))
            self.at(at_local(first, 0, 1), lambda m=month: self.schedule_payments(m))
        if _d(month, 6) > self.opened:
            self.at(at_local(_d(month, 6)), lambda m=month: self.led.assess_late_fees(m))
        if first > self.opened:
            for k in range(2):               # new tenants
                day = _d(month, 3 + rnd.randrange(24))
                habit = rnd.choices(["on_time", "sometimes_late", "chronic"], [62, 28, 10])[0]
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
            if when <= self.now:
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
             on_time_until: str | None = None, skip: tuple[str, ...] = ()) -> None:
        """A scripted tenant: moves in at `when` and pays the full balance on the 2nd of every month up to `on_time_until`
        (the story month when not given), except the months in `skip`, which its own script handles."""
        on_time_until = on_time_until or self.key(0)

        def go():
            t, lease = self.move_in(when, "scripted", name=name, phone=phone, row=row, schedule=schedule, rent=rent)
            self.led.receive_payment(t, self.balance(t), when + timedelta(minutes=10))
            self.cast[role] = {"tenant": t, "leases": [lease], "name": name, "phone": phone}
            for month in months_between(when.date() + timedelta(days=1), self.today):
                if month in skip or (on_time_until and month > on_time_until) or _d(month, 2) <= when.date():
                    continue
                self.at(at_local(_d(month, 2), 10, 30), lambda r=role, m=month: self.pay_balance(self.cast[r]["tenant"], at_local(_d(m, 2), 10, 30)))
        self.at(when, go)

    def then(self, when: datetime, fn) -> None:
        self.at(when, fn)

    def words(self, text: str) -> str:
        """A message written for the fixed day, moved to this year: each month it names moves with the story month. When the story
        month is not this month (the 1st to the 10th), a day or a late fee named without a month gets it ("the 3rd" would read
        as this month's 3rd, and from the 6th the tenant also has this month's late fee), so the message says what it meant."""
        shift = (self.story.year - TODAY.year) * 12 + self.story.month - TODAY.month

        def month(m: re.Match) -> str:
            word = m.group(0)
            name = NAMES[(NAMES.index(next(n for n in NAMES if n[:3].lower() == word[:3].lower())) + shift) % 12]
            name = name[:3] if len(word) == 3 else name
            return name.lower() if word.islower() else name
        out = MONTH_WORD.sub(month, text)
        if (self.story.year, self.story.month) != (self.today.year, self.today.month):
            name = NAMES[self.story.month - 1]
            out = BARE_DAY.sub(lambda m: f"{name} {m.group(1)}{m.group(2)}", out)
            out = A_LATE_FEE.sub(lambda m: f"a {m.group(1) or ''}late fee for {name}", out)
        return out

    def key(self, n: int) -> str:
        """The month n months from the story month, as "2026-11"."""
        return add_months(self.story, n).strftime("%Y-%m")

    def m(self, n: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
        """A time in the month n months from the story month; day 0 is that month's last day."""
        first = add_months(self.story, n)
        d = add_months(first, 1) - timedelta(days=1) if day == 0 else first.replace(day=day)
        return at_local(d, hour, minute)

    def t(self, role: str) -> str:
        return self.cast[role]["tenant"]

    def lease(self, role: str, i: int = 0) -> str:
        return self.cast[role]["leases"][i]

    def plant(self) -> None:
        """Each planted situation sits in the story month (November on the fixed day), so no later payment has paid it off. Months
        are counted from it: m(-3, 9) is the 9th three months before."""
        led, m = self.led, self.m
        nov = self.key(0)

        # maria: a declined try on Nov 3, the payment through on Nov 6 after the fee; late in August, so the yearly waiver
        # does not apply and the fee stands
        self.role("maria", m(-11, 3), "Maria Lopez", "+14075550111", row="A", rent=12000, skip=(self.key(-3), nov))
        self.then(m(-3, 9, 15), lambda: self.pay_balance(self.t("maria"), m(-3, 9, 15)))
        self.then(m(0, 3, 10, 15), lambda: led.decline(m(0, 3, 10, 15), self.t("maria"), 12000))
        self.then(m(0, 6, 14, 20), lambda: led.receive_payment(self.t("maria"), 12000, m(0, 6, 14, 20)))

        # wes: the same days as maria, but on time for the six months before: the yearly waiver applies, a real choice
        self.role("wes", m(-12, 20), "Wes Young", "+14075550288", row="A", rent=11500, skip=(nov,))
        self.then(m(0, 3, 19), lambda: led.decline(m(0, 3, 19), self.t("wes"), 11500))
        self.then(m(0, 6, 8, 45), lambda: led.receive_payment(self.t("wes"), 11500, m(0, 6, 8, 45)))

        # james: November's late fee posted by billing, and the same fee posted again by hand on Nov 10
        self.role("james", m(-9, 10), "James Carter", "+14075550122", row="B", rent=16000, skip=(nov,))
        self.then(m(0, 8, 12), lambda: led.receive_payment(self.t("james"), 16000, m(0, 8, 12)))
        self.then(m(0, 10, 9, 40), lambda: led.charge_by_hand(m(0, 10, 9, 40), self.lease("james"), LATE_FEE, 2000,
                                                                    f"late_fee:{self.lease('james')}:{nov}", note=f"late fee {month_words(nov)}"))

        # priya: signed in June on a schedule without the admin fee; the front desk posted one by hand on Nov 10
        self.role("priya", m(-5, 15), "Priya Shah", "+14075550133", row="A", rent=9500, schedule=[LATE_FEE])
        self.then(m(0, 10, 11), lambda: led.charge_by_hand(m(0, 10, 11), self.lease("priya"), ADMIN_FEE, 2500,
                                                                 f"admin_fee:{self.lease('priya')}", note="admin fee missed at move-in"))

        # tom: on time March to August, nothing paid since; September's fee is the one the yearly rule allows
        self.role("tom", m(-12, 5), "Tom Becker", "+14075550144", row="C", rent=22000, on_time_until=self.key(-3))

        # gus: nothing paid since August either, three equal late fees for three months, no waiver history
        self.role("gus", m(-10, 6), "Gus Walsh", "+14075550211", row="A", rent=8500, on_time_until=self.key(-3))

        # ines: November's rent paid on Nov 3, and a late fee posted by hand on Nov 4 anyway
        self.role("ines", m(-10, 12), "Ines Moreno", "+14075550155", row="A", rent=10000, skip=(nov,))
        self.then(m(0, 3, 9), lambda: self.pay_balance(self.t("ines"), m(0, 3, 9)))
        self.then(m(0, 4, 16), lambda: led.charge_by_hand(m(0, 4, 16), self.lease("ines"), LATE_FEE, 2000,
                                                                f"late_fee:{self.lease('ines')}:{nov}", note="late fee"))

        # hugo: confirmed by the processor on Nov 5, learned on Nov 7; billing posted the fee on what it knew
        self.role("hugo", m(-8, 20), "Hugo Brooks", "+14075550166", row="B", rent=15000, skip=(nov,))
        self.then(m(0, 7, 9), lambda: led.receive_payment(self.t("hugo"), 15000, m(0, 5, 15), m(0, 7, 9)))

        # cole: paid Nov 2, the payment came back Nov 9, so November's late fee posted then
        self.role("cole", m(-10, 25), "Cole Jensen", "+14075550199", row="B", rent=14500, skip=(nov,))
        self.then(m(0, 2, 13), lambda: led.receive_payment(self.t("cole"), 14500, m(0, 2, 13)))
        self.then(m(0, 9, 10), lambda: led.return_payment(m(0, 9, 10), max(p for p, x in led.books.payments.items() if x.tenant == self.t("cole"))))

        # tara: paid part of November before the cutoff
        self.role("tara", m(-9, 17), "Tara Singh", "+14075550277", row="B", rent=18000, skip=(nov,))
        self.then(m(0, 4, 18), lambda: led.receive_payment(self.t("tara"), 10000, m(0, 4, 18)))

        # rosa: one late fee in November, on time for the six months before: the yearly waiver applies
        self.role("rosa", m(-11, 1), "Rosa Garcia", "+14075550222", row="B", rent=17000, skip=(nov,))
        self.then(m(0, 8, 17), lambda: led.receive_payment(self.t("rosa"), 17000, m(0, 8, 17)))

        # ben: a late fee waived in March; November's fee is open, so the yearly waiver is used
        self.role("ben", m(-12, 10), "Ben Hayes", "+14075550233", row="C", rent=21500, skip=(self.key(-8), nov))
        self.then(m(-8, 8, 10), lambda: led.receive_payment(self.t("ben"), 21500, m(-8, 8, 10)))
        self.then(m(-8, 12, 14), lambda: led.waive(m(-8, 12, 14), led.books.live(f"late_fee:{self.lease('ben')}:{self.key(-8)}")[0].id,
                                                        "yearly late fee waiver", "staff:office_manager"))
        self.then(m(0, 8, 10), lambda: led.receive_payment(self.t("ben"), 21500, m(0, 8, 10)))

        # faye: two card payments on Nov 2; the second became credit
        self.role("faye", m(-9, 3), "Faye Turner", "+14075550200", row="A", rent=11000, skip=(nov,))
        self.then(m(0, 2, 9, 1), lambda: led.receive_payment(self.t("faye"), 11000, m(0, 2, 9, 1)))
        self.then(m(0, 2, 9, 3), lambda: led.receive_payment(self.t("faye"), 11000, m(0, 2, 9, 3)))

        # dev: two units, two rents on the 1st, both paid
        self.role("dev", m(-12, 8), "Dev Patel", "+14075550177", row="A", rent=9000)
        self.then(m(-7, 2, 11), lambda: self.cast["dev"]["leases"].append(led.sign_lease(m(-7, 2, 11), self.t("dev"), self.unit("B"), 15500, SCHEDULE)))
        self.then(m(-7, 2, 11, 10), lambda: self.pay_balance(self.t("dev"), m(-7, 2, 11, 10)))

        # yara: moved in Oct 20, a prorated October and a full November, both paid
        self.role("yara", m(-1, 20), "Yara Quinn", "+14075550188", row="C", rent=24000)

        # luis: signed for the admin fee at move-in and paid it
        self.role("luis", m(-2, 14), "Luis Ortiz", "+14075550266", row="C", rent=23000)

        # olga: moved out on Sep 30 with September's late fee still owed
        self.role("olga", m(-12, 12), "Olga Rivera", "+14075550255", row="B", rent=14000, on_time_until=self.key(-3))
        self.then(m(-2, 25, 12), lambda: led.receive_payment(self.t("olga"), 14000, m(-2, 25, 12)))
        self.then(m(-2, 0, 18), lambda: led.end_lease(m(-2, 0, 18), self.lease("olga")))

        # kemi and femi: a family sharing one phone number
        self.role("kemi", m(-6, 4), "Kemi Usman", "+14075550244", row="A", rent=9500)
        self.role("femi", m(-6, 4, 11), "Femi Usman", "+14075550244", row="A", rent=9000)

    def standing_dispute(self) -> None:
        """The live copy opens with one fee dispute from last week, so a visitor finds a decision waiting.
        Nora moved in on a special without the admin fee; the desk posted one by hand six days ago and she disputed it at the counter
        that afternoon, so the review, ten days on, is always four days ahead. She moves in after the last cast member, so every cast
        unit stays as on the fixed day, and pays her rent on the 1st, never the held fee. Dates count from the day, not the story month."""
        led = self.led
        moved = self.m(-1, 22)
        posted = at_local(self.today - timedelta(days=6), 11, 20)
        disputed = posted.replace(hour=16, minute=40)

        def go():
            t, lease = self.move_in(moved, "scripted", name="Nora Feld", phone="+14075550299", row="C", schedule=[LATE_FEE], rent=22500)
            led.receive_payment(t, self.balance(t), moved + timedelta(minutes=10))
            self.cast["nora"] = {"tenant": t, "leases": [lease], "name": "Nora Feld", "phone": "+14075550299"}
            for month in months_between(moved.date(), self.today):
                first = at_local(_d(month, 1), 9)
                if first > moved:
                    self.at(first, lambda w=first: led.receive_payment(t, 22500, w))

        def post():
            self.cast["nora"]["fee"] = led.charge_by_hand(posted, self.lease("nora"), ADMIN_FEE, 2500, f"admin_fee:{self.lease('nora')}", note="admin fee")
        self.at(moved, go)
        self.at(posted, post)
        self.at(disputed, lambda: led.open_dispute(disputed, self.cast["nora"]["fee"], STANDING_QUOTE, True, "staff:front_desk"))

    # the live copy's tenants in collections -----------------------------------------------------------------------------
    # Each story is written after the rest of the year, every event on its own date, in a unit nobody rents in that time: it takes
    # nothing from the draws the year is made of, so every other tenant is as it would be without it.
    def _newcomer(self, moved: datetime, name: str, phone: str, rent: int) -> tuple[str, str]:
        b = self.led.books
        busy = {x.unit for x in b.leases.values() if x.ended is None or x.ended >= moved.date()}
        unit = next(u for u in sorted(UNITS, key=lambda u: u[0] != "B") if u not in busy)
        t = self.led.add_tenant(moved - timedelta(minutes=5), name, phone)
        lease = self.led.sign_lease(moved, t, unit, rent, list(SCHEDULE))
        self.led.receive_payment(t, self.balance(t), moved + timedelta(minutes=10))
        return t, lease

    def _bills(self, lease: str, rent: int, months: range) -> list:
        """Billing ran before the lease existed, so its rent (on the 1st) and late fees (from the 6th, when rent is still open)
        are posted on their own days here."""
        led, b = self.led, self.led.books

        def post(month: str, first: datetime) -> None:
            led._charge(first, lease, RENT, rent, f"rent:{lease}:{month}", first.date())

        def fee(month: str) -> None:
            if any(c.open_cents for c in b.live(f"rent:{lease}:{month}")):
                led._charge(cutoff(month), lease, LATE_FEE, LATE_FEE_CENTS, f"late_fee:{lease}:{month}", cutoff(month).date())
        steps = []
        for n in months:
            month = self.key(n)
            steps += [(self.m(n, 1, 0), lambda month=month, first=self.m(n, 1, 0): post(month, first)), (cutoff(month), lambda month=month: fee(month))]
        return steps

    def _agree(self, t: str, when: datetime, instalments: int, consent: str) -> dict:
        led = self.led
        pid = col.propose(led, t, when, "operator")
        o = col.put_offer(led, pid, instalments, None, when, "operator")
        col.accept(led, pid, o["version"], consent, when, "operator")
        led.receive_payment(t, o["upfront"], when + timedelta(minutes=20))
        col.recheck(led, t, when + timedelta(minutes=20))
        return {"plan": pid, "offer": o}

    def _play(self, steps: list) -> None:
        for when, step in sorted(steps, key=lambda s: s[0]):
            if when <= self.now:
                step()

    def plan_breached(self) -> None:
        """The live copy opens with one tenant whose plan already breached, so Today has an open staff task and her overlock waits
        for staff. Rhea moves in two months before the story month; her card declines on the 2nd of the next month, the late fee
        posts on the 6th, and on the 8th she accepts five instalments and pays the upfront part. She pays nothing more: the first
        part, due on the 22nd, is missed, and the sweep breaches the plan when its grace days end. On any day the live copy can fall
        on, that breach is behind her and at least one part is still ahead, so staff may reinstate the plan or end it."""
        m = self.m
        t, lease = self._newcomer(m(-2, 15), "Rhea Mills", "+14075550302", 16000)
        self.breached_tenant = {"tenant": t, "leases": [lease], "name": "Rhea Mills", "phone": "+14075550302"}
        when = m(-1, 8, 11)
        first_due = when.date() + timedelta(days=col.STEP_DAYS)
        sweep_at = at_local(first_due + timedelta(days=PLAN_POLICY["grace_days"] + 1), 6)
        steps = self._bills(lease, 16000, range(-1, 2)) + [
            (m(-1, 2, 9, 30), lambda: self.led.decline(m(-1, 2, 9, 30), t, 16000)),
            (when, lambda: self.breached_tenant.update(self._agree(t, when, 5, BREACHED_CONSENT))),
            (sweep_at, lambda: col.run_sweep(self.led, sweep_at))]
        self._play(steps)

    def on_a_plan(self) -> None:
        """The live copy also opens with one tenant on a payment plan, so the overlock step is due and held by the plan on every
        day. Iris moves in on the 23rd of the month before the story month; her card declines on the 2nd of the story month, the late
        fee posts on the 6th, and on the 10th she accepts three instalments and pays the upfront part. She pays each part on its day.
        On any day the live copy can fall on (the 11th of the story month to the 10th of the next) the story month's rent is still
        partly open, 10 to 40 days past due: the overlock (10 days on the owner's schedule) is due and held, and lien eligibility
        (45 days) is not yet due."""
        m, led = self.m, self.led
        t, lease = self._newcomer(m(-1, 23), "Iris Nolan", "+14075550301", 16000)
        self.plan_tenant = {"tenant": t, "leases": [lease], "name": "Iris Nolan", "phone": "+14075550301"}
        when = m(0, 10, 11)

        def pay_part(k: int, due: datetime) -> None:
            led.receive_payment(t, self.plan_tenant["offer"]["parts"][k]["cents"], due)
            col.recheck(led, t, due)
        steps = self._bills(lease, 16000, range(0, 2)) + [
            (m(0, 2, 9, 30), lambda: led.decline(m(0, 2, 9, 30), t, 16000)),
            (when, lambda: self.plan_tenant.update(self._agree(t, when, 3, PLAN_CONSENT)))]
        for k in (1, 2, 3):                 # the parts fall every two weeks from the day she agreed
            due = at_local(when.date() + timedelta(days=col.STEP_DAYS * k), 10, 30)
            steps.append((due, lambda k=k, due=due: pay_part(k, due)))
        self._play(steps)

    def lien_file(self) -> None:
        """Open a file for a unit due on the owner's schedule, without inventing notice evidence."""
        lease = self.lease("tom")
        tenant = self.t("tom")
        for action in ("flag_lien_eligible", "open_lien_case"):
            cmd = Command(f"seed:{action}:{lease}", action, tenant, "staff:office_manager", lease=lease)
            if not evaluate(self.led.books, cmd, self.now).allowed:
                return
            (col.act if action == "flag_lien_eligible" else liens.act)(self.led, cmd, self.now)

    def cards_on_file(self) -> None:
        """The live copy's tenants on a plan, and the first few who are past due, each have two test cards on file: one that pays and
        one the issuer declines for insufficient funds, so staff can show a payment that fails and one that goes through. Only a
        brand, the last four digits and a neutral token are kept, never a card number."""
        b = self.led.books
        plan = [x["tenant"] for x in (self.plan_tenant, self.breached_tenant) if x]
        late = [t for t in sorted(b.tenants) if t not in plan and b.past_due(t, self.now) > 0
                and any(x.tenant == t and x.ended is None for x in b.leases.values())]
        for t in plan + late[:3]:
            col.save_card(self.led, t, self.now, "Visa", "4242", "card_ok")
            col.save_card(self.led, t, self.now, "Visa", "9995", "card_declined")


def build(seed: int = SEED, today: date = TODAY, cutoff: datetime | None = None, standing: bool = False) -> Year:
    return Year(seed, today, cutoff, standing).build()
