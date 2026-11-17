"""Evidence: each claim checked against the generated year gives the verdict, the words and the options the design says."""
import pytest

from core.books import ADMIN_FEE, LATE_FEE, RENT
from core.evidence import Claim, charge_list, check
from core.seed import NOW, build


@pytest.fixture(scope="module")
def year():
    return build()


def nov(year, role, kind=LATE_FEE):
    b = year.led.books
    return [c for c in b.tenant_charges(year.t(role)) if c.kind == kind and (c.due.year, c.due.month) == (2026, 11)]


def finding(year, role, kind, charge):
    return check(year.led.books, year.t(role), Claim(kind, charge.id, "their words"), NOW)


def test_paid_on_the_3rd_but_paid_on_the_6th(year):
    f = finding(year, "maria", "paid_on_time", nov(year, "maria")[0])
    assert f.verdict == "contradicts"
    assert "declined on Nov 3 at 10:15 am" in f.says and "Nov 6 at 2:20 pm" in f.says
    assert [o.action for o in f.choices] == ["explain_charge", "waive_fee"]


@pytest.mark.parametrize("role, words", [("ines", "should not have been posted"), ("hugo", "we learned of it on Nov 7")])
def test_rent_paid_before_the_cutoff_supports_the_tenant(year, role, words):
    f = finding(year, role, "paid_on_time", nov(year, role)[0])
    assert f.verdict == "supports" and words in f.says
    assert [o.action for o in f.choices] == ["correct_fee_posted_in_error"]


def test_a_returned_payment_is_named(year):
    f = finding(year, "cole", "paid_on_time", nov(year, "cole")[0])
    assert f.verdict == "contradicts" and "later returned" in f.says


def test_charged_twice(year):
    first, second = year.led.books.live(f"late_fee:{year.lease('james')}:2026-11")
    f = finding(year, "james", "charged_twice", first)
    assert f.verdict == "supports" and [(o.action, o.charge) for o in f.choices] == [("reverse_duplicate", second.id)]
    g = finding(year, "gus", "charged_twice", nov(year, "gus")[0])
    assert g.verdict == "contradicts" and "late fee for" in g.says
    d = finding(year, "dev", "charged_twice", nov(year, "dev", RENT)[0])
    assert d.verdict == "contradicts" and "unit" in d.says
    y = finding(year, "yara", "charged_twice", nov(year, "yara", RENT)[0])
    assert y.verdict == "contradicts" and "October rent" in y.says


def test_a_card_charged_twice_goes_to_staff_whichever_way_it_is_read(year):
    # Faye's two $110 payments on Nov 2: read as a payment question or as rent charged twice, the books show both payments
    b = year.led.books
    rent = nov(year, "faye", RENT)[0]
    named = dict(cents=11000, amount_quote="$110")
    for f in (check(b, year.t("faye"), Claim("payment_issue", None, "$110 from my card twice", **named), NOW),
              check(b, year.t("faye"), Claim("charged_twice", None, "$110 from my card twice", **named), NOW), finding(year, "faye", "charged_twice", rent)):
        assert f.verdict == "payment" and not f.choices and "Two payments of $110.00 went through on Nov 2" in f.says
    # a payment question that names neither the day nor the amount keeps the general answer
    assert "processor" in check(b, year.t("faye"), Claim("payment_issue", None, "my card was declined"), NOW).says
    # a day the tenant names that is not the day of the two payments keeps the general answer
    other_day = check(b, year.t("faye"), Claim("payment_issue", None, "twice on the 3rd", "2026-11-03", "the 3rd"), NOW)
    assert other_day.verdict == "payment" and "processor" in other_day.says
    # with the charge picked, a day the tenant gets wrong ("on the 1st", when rent was due) does not hide the pair
    assert check(b, year.t("faye"), Claim("charged_twice", rent.id, "twice on the 1st", "2026-11-01", "the 1st"), NOW).verdict == "payment"
    # Dev's two rents on two units are explained, not handed over
    assert finding(year, "dev", "charged_twice", nov(year, "dev", RENT)[0]).verdict == "contradicts"


def test_never_agreed_and_challenges(year):
    p = finding(year, "priya", "never_agreed", nov(year, "priya", ADMIN_FEE)[0])
    assert p.verdict == "no_record" and [o.action for o in p.choices] == ["open_dispute"]
    admin = [c for c in year.led.books.tenant_charges(year.t("luis")) if c.kind == ADMIN_FEE][0]
    assert finding(year, "luis", "never_agreed", admin).verdict == "contradicts"
    u = finding(year, "luis", "challenges_record", admin)
    assert u.verdict == "unresolved" and [o.action for o in u.choices] == ["open_dispute"]


def test_payment_questions_ended_leases_and_unknown_charges_go_to_staff(year):
    b = year.led.books
    assert check(b, year.t("faye"), Claim("payment_issue", None, "charged my card twice"), NOW).verdict == "payment"
    olga = [c for c in b.tenant_charges(year.t("olga")) if c.kind == LATE_FEE][0]
    assert [o.action for o in finding(year, "olga", "wants_waiver", olga).choices] == ["explain_charge"]
    assert check(b, year.t("wes"), Claim("paid_on_time", "C0001", "x"), NOW).verdict == "unclear"


def test_the_charge_list_is_words_and_ids(year):
    rows = charge_list(year.led.books, year.t("james"), NOW)
    assert rows[0]["about"].startswith("late fee, due Nov 10, $20.00")
