"""The generated year: built only through billing commands, identical every time, balanced, and carrying each planted situation."""
import hashlib
import random
import re
from datetime import date, timedelta

import pytest

from core.billing import Ledger, Refused
from core.books import ADMIN_FEE, LATE_FEE, at_local, fold, lines, local
from core.case import FIXED, handle
from core.cases import load
from core.reader import dates
from core.seed import NOW, Year, build
from core.store import Store


FIXED_YEAR = "be4c9da7613b0a719e94359b729532461629ad2ca098e5c3fe838b302911d527"


@pytest.fixture(scope="module")
def year():
    return build()


def snapshot(events):
    return [(e.kind, e.at.isoformat(), e.key, sorted(e.payload.items(), key=str)) for e in events]


def test_the_year_is_identical_every_time(year):
    assert snapshot(year.led.pending) == snapshot(build().led.pending)


def test_the_fixed_year_stays_byte_identical(year):
    """The graded sets and their labels rest on these books: the live copy's extras (the dispute from last week) never reach them."""
    events = year.led.pending
    assert len(events) == 3241 and hashlib.sha256(repr(snapshot(events)).encode()).hexdigest() == FIXED_YEAR


def test_the_live_copy_opens_with_one_dispute_from_last_week_on_every_day(year):
    """The live day starts with one fee dispute from last week already open. On every day of two month turns (and a leap
    February) it is open, held, four days from review, paid around, never past due, and it changes nothing the cast does."""
    units = {r: [year.led.books.leases[x].unit for x in c["leases"]] for r, c in year.cast.items()}
    fixed = morning(year, NOW.date())
    start = date(2026, 11, 25)
    for n in range(130):
        day = start + timedelta(days=n) if n < 70 else date(2028, 1, 20) + timedelta(days=n - 70)
        y = build(today=day, cutoff=at_local(day), standing=True)
        b, now = y.led.books, at_local(day)
        nora = y.cast["nora"]
        lease = nora["leases"][0]
        open_ = [d for d in b.disputes.values() if d.outcome is None]
        assert len(open_) == 1 and open_[0].tenant == nora["tenant"], day
        fee = b.charges[open_[0].charge]
        assert fee.kind == ADMIN_FEE and fee.open_cents == 2500 and b.held(fee.id) and ADMIN_FEE not in b.leases[lease].schedule, day
        assert open_[0].review_by == day + timedelta(days=4) and local(fee.posted_at).date() == day - timedelta(days=6), day
        assert b.past_due(nora["tenant"], now) == 0 and not b.credit.get(nora["tenant"]), day
        assert not [c for c in b.tenant_charges(nora["tenant"]) if c.kind == LATE_FEE], day
        assert {r: [b.leases[x].unit for x in c["leases"]] for r, c in y.cast.items() if r != "nora"} == units, day
        assert max(e.at for e in y.led.pending) <= now, day
        if n % 13 == 0:
            assert morning(y, day) == fixed, day


def test_the_year_is_big_enough_and_every_event_balances(year):
    events = year.led.pending
    assert len(events) > 3000
    assert len(year.led.books.leases) >= 100
    for e in events:
        assert sum(c for _, c, _ in lines(e.kind, e.payload)) == 0, e.key


def test_stored_and_replayed_books_agree(year):
    store = Store()
    with store.write():
        store.append("base", year.led.pending)
    books = fold(store.events())
    assert {k: c.open_cents for k, c in books.charges.items()} == {k: c.open_cents for k, c in year.led.books.charges.items()}
    assert store.journal() == [(e.id, a, c, ch) for e in store.events() for a, c, ch in lines(e.kind, e.payload)]
    totals = {}
    for _, account, cents, _ in store.journal():
        totals[account] = totals.get(account, 0) + cents
    for lease in books.leases:
        assert totals.get(f"receivable:{lease}", 0) == books.receivable(lease)
    for t in books.tenants:
        assert -totals.get(f"credit:{t}", 0) == books.credit.get(t, 0)


def charges(year, role, kind=None):
    t = year.t(role)
    return [c for c in year.led.books.tenant_charges(t) if kind is None or c.kind == kind]


def nov_fees(year, role):
    return [c for c in charges(year, role, LATE_FEE) if c.due.month == 11 and c.due.year == 2026]


def test_the_planted_situations_are_there(year):
    b = year.led.books
    # maria: declined on Nov 3, paid on Nov 6 after the fee; the fee is open
    m = year.t("maria")
    assert [x["at"].date() for x in b.declined if x["tenant"] == m] == [date(2026, 11, 3)]
    assert [f.open_cents for f in nov_fees(year, "maria")] == [2000]
    assert any(p.effective_at.date() == date(2026, 11, 6) for p in b.payments.values() if p.tenant == m)
    # james: two live late fees for the same November obligation, the second posted by hand
    j = b.live(f"late_fee:{year.lease('james')}:2026-11")
    assert [c.source for c in j] == ["billing", "staff:front_desk"]
    # priya: an admin fee posted by hand although her signed schedule has none
    assert ADMIN_FEE not in b.leases[year.lease("priya")].schedule
    assert [c.source for c in charges(year, "priya", ADMIN_FEE)] == ["staff:front_desk"]
    # tom and gus: three open late fees each
    assert len([c for c in charges(year, "tom", LATE_FEE) if c.open_cents]) == 3
    assert len([c for c in charges(year, "gus", LATE_FEE) if c.open_cents]) == 3
    # ines: a hand late fee although November's rent was fully paid before the cutoff
    assert b.rent_paid_before(year.lease("ines"), "2026-11", at_local(date(2026, 11, 6))) == 10000
    assert [c.source for c in nov_fees(year, "ines")] == ["staff:front_desk"]
    # hugo: paid before the cutoff by effective time, learned after it, so billing posted the fee
    assert b.rent_paid_before(year.lease("hugo"), "2026-11", at_local(date(2026, 11, 6))) == 15000
    assert [c.source for c in nov_fees(year, "hugo")] == ["billing"]
    # cole: the payment came back on Nov 9 and the late fee posted then
    assert [f.posted_at for f in nov_fees(year, "cole")] == [at_local(date(2026, 11, 9), 10)]
    # tara: a partial payment before the cutoff, so the fee
    assert len(nov_fees(year, "tara")) == 1
    # ben: one waiver in the last 12 months; rosa and wes: none
    assert b.waived_within(year.t("ben"), NOW) == 1 and b.waived_within(year.t("rosa"), NOW) == 0
    # faye: two payments on Nov 2, the second held as credit
    assert b.credit.get(year.t("faye")) == 11000
    # dev: two leases, both November rents paid; kemi and femi share a phone
    assert len(year.cast["dev"]["leases"]) == 2 and all(c.open_cents == 0 for c in charges(year, "dev") if c.due.month == 11)
    assert b.tenants[year.t("kemi")].phone == b.tenants[year.t("femi")].phone
    # no generated tenant shares a number with the cast (only kemi and femi share one, on purpose)
    cast_phones = {v["phone"] for v in year.cast.values()}
    assert sum(1 for t in b.tenants.values() if t.phone in cast_phones) == len(year.cast)
    # olga: lease ended Sep 30 with September's fee open
    assert b.leases[year.lease("olga")].ended == date(2026, 9, 30)
    assert [c.open_cents for c in charges(year, "olga", LATE_FEE)] == [2000]


def test_random_histories_always_balance():
    """200 short random histories through every command: every event balances and receivable never goes below zero."""
    for seed in range(200):
        rnd = random.Random(seed)
        led = Ledger()
        start = date(2026, 1, 1)
        led.set_up(at_local(start), "Test", "Orlando, FL", ["A1", "A2", "A3"])
        tenants = [led.add_tenant(at_local(start, 8), f"T{i}", f"+1{i}") for i in range(2)]
        leases = [led.sign_lease(at_local(start + timedelta(days=i), 9), tenants[i % 2], f"A{i + 1}", 5000 + 1000 * i,
                                 [ADMIN_FEE, LATE_FEE] if rnd.random() < 0.7 else [LATE_FEE]) for i in range(3)]
        for k, month in enumerate(["2026-02", "2026-03", "2026-04"]):
            led.post_rent(month)
            for _ in range(rnd.randrange(4)):
                t = rnd.choice(tenants)
                day = date(2026, k + 2, 1 + rnd.randrange(27))
                op = rnd.random()
                try:
                    if op < 0.5:
                        led.receive_payment(t, 500 * (1 + rnd.randrange(30)), at_local(day, 10))
                    elif op < 0.6 and led.books.payments:
                        led.return_payment(at_local(day, 11), rnd.choice(sorted(led.books.payments)))
                    elif op < 0.7:
                        led.charge_by_hand(at_local(day, 12), rnd.choice(leases), LATE_FEE, 2000, None)
                    elif op < 0.85:
                        open_ = led.books.open_charges(t, skip_held=False)
                        if open_:
                            c = rnd.choice(open_)
                            if f"reverse:{c.id}" not in [f for e in led.pending for f in e.effects]:
                                led.reverse(at_local(day, 13), c.id, "test", "staff:office_manager")
                    else:
                        open_ = [c for c in led.books.open_charges(t) if c.kind != "rent"]
                        if open_:
                            led.open_dispute(at_local(day, 14), open_[0].id, "test", rnd.random() < 0.5, "operator")
                except Refused:
                    pass
            led.assess_late_fees(month)
        for e in led.pending:
            assert sum(c for _, c, _ in lines(e.kind, e.payload)) == 0, (seed, e.key)
        for lease in leases:
            assert led.books.receivable(lease) >= 0
        assert all(v >= 0 for v in led.books.credit.values()), seed


def morning(year, day):
    """What the rule-based workflow does with each morning message on this year: the action, whether the gate allowed it, and
    the charge it touched as its kind and its month counted from the story month."""
    store = Store()
    with store.write():
        store.append("base", year.led.pending)
    out = {}
    for item in load("shift")["items"]:
        r = handle(store, f"visitor:{item['id']}", item["id"], year.cast[item["role"]]["phone"], year.words(item["text"]), FIXED, at_local(day, 10))
        books = store.books(f"visitor:{item['id']}")
        out[item["id"]] = [(o.action, o.decision.allowed, *((ch.kind, (ch.due.year - year.story.year) * 12 + ch.due.month - year.story.month)
                            if (ch := books.charges.get(o.claim.charge or "")) else ())) for o in r.outcomes]
    return out


def test_the_year_follows_any_day_and_the_morning_reads_the_same(year):
    """The live FMS builds the year up to midnight of the real day (2026-11-16, his order: the date is today's and changes every
    day). On any day the planted situations sit in the story month, the cast keeps its units, nothing is dated after midnight,
    each date a morning message names falls in the story month, and the rule-based workflow does what it does on the fixed day."""
    items = load("shift")["items"]
    assert all(year.words(i["text"]) == i["text"] for i in items)          # the fixed day reads the messages as written
    early = Year(today=date(2026, 12, 5))                                  # the story is November's; a day that names its month keeps it
    assert early.words("I paid the 8th of November and the 3rd.") == "I paid the 8th of November and November 3rd."
    units = {r: [year.led.books.leases[x].unit for x in c["leases"]] for r, c in year.cast.items()}
    fixed = morning(year, NOW.date())
    for day in (date(2026, 12, 1), date(2026, 12, 6), date(2026, 12, 11), date(2027, 1, 4), date(2027, 3, 1), date(2028, 2, 29)):
        y = build(today=day, cutoff=at_local(day))
        assert max(e.at for e in y.led.pending) <= at_local(day), day
        assert {r: [y.led.books.leases[x].unit for x in c["leases"]] for r, c in y.cast.items()} == units, day
        for i in items:
            assert all((d.year, d.month) == (y.story.year, y.story.month) and d <= day for d, _ in dates(y.words(i["text"]), day)), (day, i["id"])
            if day.day < 11 and "a late fee" in i["text"].replace("$20 ", ""):      # two late fees from the 6th: the message names its month
                assert re.search(r"late fee for [A-Z][a-z]+", y.words(i["text"])), (day, i["id"])
        assert morning(y, day) == fixed, day
