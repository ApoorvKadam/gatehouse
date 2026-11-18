"""Reading dates, amounts and quotes out of a tenant's own words, in code.

The patterns come from the desk's reader (desk/investigator.py), copied rather than shared so the core stands alone, and
extended for what tenants write about charges: "the 3rd", "el día 3", a bare "110" inside an amount the model quoted."""
from __future__ import annotations

import re
from datetime import date, timedelta

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
MONEY = re.compile(r"\$\s?(\d[\d,]*)(?:\.(\d{1,2}))?|\b(\d[\d,]*)(?:\.(\d{1,2}))?\s*(?:dollars?|USD)\b", re.I)
NAMED = re.compile(rf"\b{_MONTH}\.? (\d{{1,2}})(?:st|nd|rd|th)?\b(?:,? (\d{{4}})\b)?", re.I)
DAYFIRST = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\b\.?(?:,? (\d{{4}})\b)?", re.I)
NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")
ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
ORDINAL = re.compile(r"\b(?:the|on|el d[ií]a|el)\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)


def _cents(whole: str, fraction: str | None) -> int:
    return int(whole.replace(",", "")) * 100 + int((fraction or "0").ljust(2, "0"))


def amounts(text: str) -> list[tuple[int, str]]:
    """Every dollar amount written with a $ sign or the word dollars, in cents, with the words it was read from."""
    out = []
    for m in MONEY.finditer(text):
        whole, frac = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        out.append((_cents(whole, frac), m.group(0)))
    return out


def read_amount(quote: str) -> int | None:
    """The amount in a short quote the model gave for an amount; a bare number counts as dollars."""
    found = amounts(quote)
    if found:
        return found[0][0]
    m = re.fullmatch(r"\s*(\d[\d,]*)(?:\.(\d{1,2}))?\s*", quote)
    return _cents(m.group(1), m.group(2)) if m else None


BARE = re.compile(r"(?<![\d.,/:$#])\b(\d[\d,]*)(?:\.(\d{1,2}))?\b(?![\d/:]|\.\d|\s*(?:%|st\b|nd\b|rd\b|th\b|[ap]\.?m\b|days?\b|weeks?\b|"
                  r"months?\b|years?\b|hours?\b|minutes?\b|payments?\b|instal+ments?\b|parts?\b|times?\b|x\b|(?:of\s+)?" + _MONTH + r"\b))", re.I)
NOT_MONEY_BEFORE = re.compile(rf"\b(?:plan|option|unit|units|number|no|door|space|apt|apartment|room|locker|suite|{_MONTH})\.?\s*$", re.I)
DAY_BEFORE = re.compile(r"\b(?:the|on|by|until|till|til|before|after|of)\s*$", re.I)


def bare_amount(quote: str) -> tuple[int, str] | None:
    """The one bare number in a short amount quote ("the 75", "100 today"), in cents, with the digits it was read from. A number
    that names a plan, a unit, a date ("Dec 2", "19 November", "on the 19", "in 2026"), or counts days, payments or a time is not
    money; with no number or two left, there is none."""
    hits = []
    for m in BARE.finditer(quote):
        before, whole = quote[:m.start()], int(m.group(1).replace(",", ""))
        if NOT_MONEY_BEFORE.search(before) or (not m.group(2) and (1900 <= whole <= 2100 or (whole <= 31 and DAY_BEFORE.search(before)))):
            continue
        hits.append(m)
    return (_cents(hits[0].group(1), hits[0].group(2)), hits[0].group(0)) if len(hits) == 1 else None


def amount_in(quote: str) -> int | None:
    """read_amount, and also the one bare number in a short amount quote; otherwise nothing, and the reply is asked again."""
    cents = read_amount(quote)
    if cents is not None:
        return cents
    bare = bare_amount(quote)
    return bare[0] if bare else None


def _back(today: date, month: int, day: int, year: int | None, ahead: bool = False) -> date | None:
    """A date without a year is the most recent one not after today; ahead (a promise to pay), the next one not before today."""
    try:
        if year:
            return date(year, month, day)
        d = date(today.year, month, day)
        if ahead:
            return d if d >= today else date(today.year + 1, month, day)
        return d if d <= today else date(today.year - 1, month, day)
    except ValueError:
        return None


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
RELATIVE = re.compile(r"\b(today|tonight|tomorrow|(?:this |next |on )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.I)


def _relative(word: str, today: date) -> date | None:
    """Today, tomorrow, or a weekday ahead. "This Friday" is this week's, today included; a bare "Friday" said on a Friday and
    any "next Friday" could mean two days, so they are not read and the tenant is asked."""
    w = word.lower()
    if w in ("today", "tonight"):
        return today
    if w == "tomorrow":
        return today + timedelta(days=1)
    if w.startswith("next "):
        return None
    ahead = (WEEKDAYS.index(w.split()[-1]) - today.weekday()) % 7
    if ahead == 0 and not w.startswith("this "):
        return None
    return today + timedelta(days=ahead)


def dates(text: str, today: date, ahead: bool = False) -> list[tuple[date, str]]:
    """Every calendar date the words name, with the words it was read from. "The 3rd" is this month's 3rd, or last month's
    if that is still ahead. Ahead (a promise to pay): dates without a year look forward, "the 19th" is the next 19th, and
    "tomorrow" or "Friday" count too."""
    out = []
    for m in NAMED.finditer(text):
        out.append((_back(today, MONTHS.index(m.group(1)[:3].lower()) + 1, int(m.group(2)), int(m.group(3)) if m.group(3) else None, ahead), m.group(0)))
    for m in DAYFIRST.finditer(text):
        out.append((_back(today, MONTHS.index(m.group(2)[:3].lower()) + 1, int(m.group(1)), int(m.group(3)) if m.group(3) else None, ahead), m.group(0)))
    for m in NUMERIC.finditer(text):
        mo, dy = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= dy <= 31:
            y = int(m.group(3)) if m.group(3) else None
            out.append((_back(today, mo, dy, y + 2000 if y is not None and y < 100 else y, ahead), m.group(0)))
    for m in ISO.finditer(text):
        out.append((_back(today, int(m.group(2)), int(m.group(3)), int(m.group(1))), m.group(0)))
    taken = " ".join(q for _, q in out)
    for m in ORDINAL.finditer(text):
        if m.group(0) in taken:
            continue
        dy = int(m.group(1))
        if 1 <= dy <= 31:
            if ahead:
                nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
                d = _back(today, today.month, dy, today.year) if dy >= today.day else _back(today, nxt.month, dy, nxt.year)
            else:
                d = _back(today, today.month, dy, today.year) if dy <= today.day else None
                if d is None:
                    prev = today.replace(day=1) - timedelta(days=1)
                    d = _back(today, prev.month, dy, prev.year)
            out.append((d, m.group(0)))
    if ahead:
        taken = " ".join(q for _, q in out)
        out += [(_relative(m.group(1), today), m.group(0)) for m in RELATIVE.finditer(text) if m.group(0) not in taken]
    return [(d, q) for d, q in out if d is not None]


def read_date(quote: str, today: date, ahead: bool = False) -> date | None:
    found = dates(quote, today, ahead)
    if found:
        return found[0][0]
    m = re.fullmatch(r"\s*(\d{1,2})(?:st|nd|rd|th)?\s*", quote)
    return dates(f"the {m.group(1)}", today, ahead)[0][0] if m and dates(f"the {m.group(1)}", today, ahead) else None


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def found_in(quote: str, text: str) -> bool:
    """The quote is the tenant's own words: found in the message, ignoring case, spacing and curly quotes."""
    q = _norm(quote)
    return bool(q) and q in _norm(text)
