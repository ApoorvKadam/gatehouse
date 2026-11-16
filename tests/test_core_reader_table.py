"""The core's own reader, and the fixed workflow's fixed outputs on the shift messages."""
from datetime import date

import pytest

from core.cases import load
from core.evidence import check
from core.reader import amounts, dates, found_in, read_amount, read_date
from core.seed import NOW, build
from core.table import decide, keyword_read

TODAY = date(2026, 11, 14)


@pytest.mark.parametrize("text, want", [("I paid on the 3rd", date(2026, 11, 3)), ("pagué el día 3", date(2026, 11, 3)),
                                        ("i paid on nov 2", date(2026, 11, 2)), ("cleared on 11/5", date(2026, 11, 5)),
                                        ("on November 5th", date(2026, 11, 5)), ("back on the 20th", date(2026, 10, 20)),
                                        ("The 3rd of November", date(2026, 11, 3))])
def test_dates(text, want):
    assert dates(text, TODAY)[0][0] == want


def test_amounts_and_quotes():
    assert [c for c, _ in amounts("a $20 fee and $1,200.50 and 30 dollars")] == [2000, 120050, 3000]
    assert read_amount("110") == 11000 and read_amount("$25") == 2500 and read_amount("soon") is None
    assert read_date("the 3rd", TODAY) == date(2026, 11, 3)
    assert found_in("I paid on the 3rd", "Hey!  I PAID on the 3rd, really") and not found_in("I paid on the 4th", "I paid on the 3rd")


def fixed(year, item):
    b = year.led.books
    t = year.t(item["role"])
    claims, flags = keyword_read(b, t, item["text"], NOW)
    out, spent = [], 0
    for c in sorted(claims, key=lambda c: b.charges[c.charge].seq if c.charge else 0):
        a = decide(b, t, check(b, t, c, NOW), flags, NOW, spent)
        spent += a == "waive_fee"
        out.append((c.kind, a))
    return out


def test_the_fixed_workflow_on_the_shift():
    year = build()
    got = {i["id"]: fixed(year, i) for i in load("shift")["items"]}
    assert got == {
        "s1": [("paid_on_time", "explain_charge")],
        "s2": [("charged_twice", "reverse_duplicate")],
        "s3": [("never_agreed", "open_dispute")],
        "s4": [("wants_waiver", "waive_fee"), ("wants_waiver", "explain_charge")],
        "s5": [("other", "hand_to_staff")],          # Wes asks for help in words the keyword rules do not know
        "s6": [("other", "hand_to_staff")],          # so does Hugo's bank-date question
        "s7": [("charged_twice", "explain_charge")],
        "s8": [("payment_issue", "hand_to_staff")],
    }
