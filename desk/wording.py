"""Words for a screen. The FMS and the desk keep codes and provider ids; a person reads neither, so anything that reaches a page
goes through `plain` first."""
from __future__ import annotations

import re
from datetime import datetime

# the codes the FMS and the desk use for a payment or a sale, as words a person reads; the model reads the same words
STATE_WORDS = {"NEEDS_STAFF": "needs staff", "CONFIRMED": "confirmed", "AWAITING_APPLICATION": "accepted but not yet on the ledger",
               "PENDING_AT_FMS": "pending at the FMS", "OUTCOME_UNKNOWN": "outcome unknown", "DECLINED": "declined", "REJECTED": "rejected",
               "NOT_SUBMITTED": "not sent", "SUBMITTING": "being sent", "NEEDS_REAUTHORIZATION": "needs a new authorization",
               "RETURNED": "returned by the bank", "REFUNDED": "refunded", "REFUND_PENDING": "refund pending", "DISPUTED": "disputed",
               "CLEARED_BY_STAFF": "cleared by staff", "SUCCEEDED": "succeeded", "PENDING": "pending", "FAILED": "failed",
               "DIRECT_DEBIT_RETURN": "returned by the bank", "REFUND_INITIATED": "refund started",
               # the statuses of a lien sale at the FMS and of a listing on an outside site
               "LISTED": "listed", "CANCELLED": "cancelled", "WITHDRAWN": "withdrawn", "SOLD": "sold", "NO_BIDS": "settled with no bids",
               "PAYMENT_RECEIVED": "sold, and the bidder paid", "COMPLETE": "complete"}

# an id the FMS or the simulator made up: payment_ofK0Ux1ENYC, listing_x7Hq2..., lease_WhitB1042
_PROVIDER_ID = r"(?:payment|auction|listing|task|lease|be|dex|cust|customer|fac|facility)_[A-Za-z0-9]{4,}"
_IN_BRACKETS = re.compile(rf"\s*\({_PROVIDER_ID}\)")
_BARE_ID = re.compile(rf"\b{_PROVIDER_ID}\b")
# a record id the desk gave a record: workitem:4, payment:12, text:in:3
_RECORD_ID = re.compile(r"\b(?:workitem|payment|observed|lease|lien|text|texting)(?::[A-Za-z0-9]+)+")
# a time as the FMS or a feed wrote it, 2026-11-08T17:53:15.573390, which the desk keeps in UTC
_STAMP = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|\+00:00)?")
_CODE = re.compile(r"\b(?:" + "|".join(sorted(STATE_WORDS, key=len, reverse=True)) + r")\b")


def when(t: datetime | None) -> str:
    """A time the desk keeps in UTC, as a person reads it."""
    return f"{t:%b} {t.day}, {t.year}, {t:%H:%M} UTC" if t else "an unknown time"


def words(code: str | None) -> str:
    """A state code as words: a known one by its wording, any other with its underscores taken out."""
    return STATE_WORDS.get(code or "", (code or "").replace("_", " ").lower())


def _when(found: re.Match) -> str:
    try:
        at = datetime(*(int(part) for part in found.groups()))
    except ValueError:
        return found.group(0)
    return f"{at:%b} {at.day}, {at:%Y}, {at:%H:%M} UTC"


def plain(text: str | None, ids: bool = True) -> str | None:
    """Text for a screen: provider ids and record ids taken out, payment state codes written as words, times written as a person
    reads them. An evidence page keeps its ids (`ids=False`), because there a person compares them with the FMS character by character."""
    if text is None:
        return None
    if ids:
        text = _RECORD_ID.sub("", _BARE_ID.sub("", _IN_BRACKETS.sub("", text)))
    text = _STAMP.sub(_when, text)
    text = _CODE.sub(lambda m: STATE_WORDS[m.group(0)], text)
    text = re.sub(r"\s*\(\s*\)", "", text)                  # the brackets an id leaves behind
    return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()


# what the desk did when the model asked for a staff review: the label on the chip, and which chip colour it takes
REVIEW_LABELS = {"linked": ("Existing FMS task linked", "done"), "requested": ("FMS task requested and confirmed", "done"),
                 "not_confirmed": ("FMS task not confirmed", "act"), "refused": ("FMS refused the task", "act"),
                 "not_requested": ("No new task requested", "plain")}


def review_view(action: dict) -> dict:
    """One staff-review action as a screen shows it: a label, a tone and a sentence, written from what the desk recorded. No task id
    is shown, only what the FMS said about the task and when the desk read it."""
    outcome, status = action.get("outcome"), words(action.get("task_status")) if action.get("task_status") else "not found"
    try:
        read = datetime.fromisoformat(action["read_at"]) if action.get("read_at") else None
    except ValueError:
        read = None
    label, tone = REVIEW_LABELS.get(outcome, ("Staff review", "plain"))
    done = {"linked": "The FMS already had a task asking a person to act on this sale. Gatehouse read it back from the FMS on {at}: it is {status}.",
            "requested": "Gatehouse asked the FMS for a task asking a person to act on this sale, and read it back on {at}: it is {status}.",
            "not_confirmed": "Gatehouse could not confirm an FMS task for this sale. It does not ask again while the outcome is unknown, so a person has to follow up.",
            "refused": "The FMS refused to create the task, so nothing was made. A person has to follow up.",
            "not_requested": "The desk could not confirm that the sale is listed with nothing owed, so it asked for no new task."}.get(outcome, "")
    text = done.format(at=when(read), status=status)
    if outcome in ("linked", "requested") and status == "completed":
        text += " A completed task does not close the sale."
    return {"kind": action.get("kind"), "label": label, "tone": tone, "text": text, "at": read}


# What an acknowledgement may say. Every sentence is written here, in advance, and filled from a verified record. Nothing a model writes
# reaches a tenant, so no id, code, promise or legal claim can: the catalog is the whole vocabulary. A test keeps these free of
# "paid in full", cancel, will, within, hours, legal and compliant.
REPLY_PHRASES = {
    "message_received": "Hi {first}, we got your message from {when}.",
    "payment_applied": "Your payment of {amount} has been applied to your account.",
    "balance_as_of": "Your account shows {balance} owed on unit {unit} as of {when}.",
    "sale_status": "Unit {unit} is listed for a lien sale on {when}.",
    "staff_review": "A staff member has an open task to review this.",
    "acknowledgment": "Call the office if anything changes.",
}
# the kinds a model may propose, and what a screen calls each part
REPLY_KINDS = tuple(REPLY_PHRASES)
REPLY_PART_LABELS = {"message_received": "Message received", "payment_applied": "Payment applied", "balance_as_of": "Balance",
                     "sale_status": "Sale status", "staff_review": "Staff review", "acknowledgment": "Closing line"}
# how the outcome of sending reads, and which chip colour it takes. "sent" is what the messaging system answered, never delivery.
REPLY_OUTCOMES = {"sent": ("Reply accepted by the messaging system", "done"), "blocked": ("Reply blocked", "act"),
                  "rejected": ("Reply refused by the messaging system", "act"), "not_confirmed": ("Reply not confirmed", "wait"),
                  "not_sent": ("No reply sent", "plain")}


def action_view(action: dict) -> dict | None:
    """One thing the desk did for a run, as a screen shows it, or None for a kind that has no view."""
    if action.get("kind") == "staff_review":
        return review_view(action)
    if action.get("kind") == "reply":
        return reply_view(action)
    return None


def reply_view(action: dict) -> dict:
    """The acknowledgement the desk wrote from the parts a model proposed: how sending went, the text, each sentence with the record it
    came from, and what was left out and why."""
    label, tone = REPLY_OUTCOMES.get(action.get("outcome"), ("Reply", "plain"))
    return {"kind": "reply", "label": label, "tone": tone, "text": action.get("reason") or "",
            "body": action.get("body") or "", "parts": list(action.get("parts") or []), "dropped": list(action.get("dropped") or [])}


# What went wrong in an evaluation scenario, in the order the Evaluation page lists the kinds. Each scenario names one of these keys
# (desk/scenarios.py and desk/evaluate.py), and the page gives each kind its own colour.
TROUBLES = {"normal": "Nothing goes wrong", "no": "The FMS says no or fails", "late": "The answer is late or lost",
            "twice": "Two things at once", "money": "Money moves later", "disagree": "The records disagree"}
