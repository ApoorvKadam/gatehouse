"""Words for a screen. The FMS and the desk keep codes and provider ids; a person reads neither, so anything that reaches a page
goes through `plain` first."""
from __future__ import annotations

import re
from datetime import datetime

# the codes the FMS and the desk use for a payment, as words a person reads; the model reads the same words
STATE_WORDS = {"NEEDS_STAFF": "needs staff", "CONFIRMED": "confirmed", "AWAITING_APPLICATION": "accepted but not yet on the ledger",
               "PENDING_AT_FMS": "pending at the FMS", "OUTCOME_UNKNOWN": "outcome unknown", "DECLINED": "declined", "REJECTED": "rejected",
               "NOT_SUBMITTED": "not sent", "SUBMITTING": "being sent", "NEEDS_REAUTHORIZATION": "needs a new authorization",
               "RETURNED": "returned by the bank", "REFUNDED": "refunded", "REFUND_PENDING": "refund pending", "DISPUTED": "disputed",
               "CLEARED_BY_STAFF": "cleared by staff", "SUCCEEDED": "succeeded", "PENDING": "pending", "FAILED": "failed",
               "DIRECT_DEBIT_RETURN": "returned by the bank", "REFUND_INITIATED": "refund started"}

# an id the FMS or the simulator made up: payment_ofK0Ux1ENYC, listing_x7Hq2..., lease_WhitB1042
_PROVIDER_ID = r"(?:payment|auction|listing|task|lease|be|dex|cust|customer|fac|facility)_[A-Za-z0-9]{4,}"
_IN_BRACKETS = re.compile(rf"\s*\({_PROVIDER_ID}\)")
_BARE_ID = re.compile(rf"\b{_PROVIDER_ID}\b")
# a record id the desk gave a record: workitem:4, payment:12, text:in:3
_RECORD_ID = re.compile(r"\b(?:workitem|payment|observed|lease|lien|text|texting)(?::[A-Za-z0-9]+)+")
# a time as the FMS or a feed wrote it, 2026-11-08T17:53:15.573390, which the desk keeps in UTC
_STAMP = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|\+00:00)?")
_CODE = re.compile(r"\b(?:" + "|".join(sorted(STATE_WORDS, key=len, reverse=True)) + r")\b")


def words(code: str | None) -> str:
    """A state code as words: a known one by its wording, any other with its underscores taken out."""
    return STATE_WORDS.get(code or "", (code or "").replace("_", " ").lower())


def _when(found: re.Match) -> str:
    try:
        at = datetime(*(int(part) for part in found.groups()))
    except ValueError:
        return found.group(0)
    return f"{at:%b} {at.day}, {at:%Y}, {at:%H:%M} UTC"


def plain(text: str | None) -> str | None:
    """Text for a screen: provider ids and record ids taken out, payment state codes written as words, times written as a person
    reads them."""
    if text is None:
        return None
    text = _STAMP.sub(_when, _BARE_ID.sub("", _IN_BRACKETS.sub("", text)))
    text = _RECORD_ID.sub("", text)
    text = _CODE.sub(lambda m: STATE_WORDS[m.group(0)], text)
    return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()
