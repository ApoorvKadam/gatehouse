"""Answers from the FMS that its documentation does not describe. The adapter notes them as they arrive; the worker saves the notes here, and
Today shows each one until a person has reviewed it. Nothing here changes how a payment is read; the adapter and the payment service decide that.
Today names each departure in words; the exact value the FMS sent is in /api/integration."""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .adapter import FMSAdapter
from .db import utcnow
from .models import FmsObservation


def save_notes(session: Session, adapter: FMSAdapter) -> int:
    """Write down what the adapter noted since the last pass; a repeat adds to the count of the record it belongs to. Returns how many notes.
    If writing fails, the notes go back to the adapter for the next pass."""
    notes = adapter.take_anomalies()
    try:
        now = utcnow()
        for (endpoint, field, kind, value), n in Counter((a.endpoint, a.field, a.kind, a.value) for a in notes).items():
            key = (FmsObservation.provider == adapter.provider, FmsObservation.endpoint == endpoint, FmsObservation.field == field,
                   FmsObservation.kind == kind, FmsObservation.value == value)
            # one statement, so a review made meanwhile is always cleared by this later occurrence, and the count cannot lose a repeat
            changed = session.execute(update(FmsObservation).where(*key).values(count=FmsObservation.count + n, last_seen=now, reviewed_at=None)).rowcount
            if not changed:
                session.add(FmsObservation(provider=adapter.provider, endpoint=endpoint, field=field, kind=kind, value=value, count=n,
                                           first_seen=now, last_seen=now))
        session.commit()
    except Exception:
        session.rollback()
        adapter.restore_anomalies(notes)
        raise
    return len(notes)


def is_open(row: FmsObservation) -> bool:
    """Not reviewed yet, or seen again after the review (saving a repeat clears the review)."""
    return row.reviewed_at is None


def open_observations(session: Session) -> list[FmsObservation]:
    return [r for r in session.scalars(select(FmsObservation).order_by(FmsObservation.first_seen, FmsObservation.id)) if is_open(r)]


def review(session: Session, observation_id: int, note: str, at: datetime | None = None) -> FmsObservation | None:
    """A person has looked at it: the contract was confirmed, the adapter was changed, or the change is expected. None if there is no such record."""
    row = session.get(FmsObservation, observation_id)
    if row is None:
        return None
    row.reviewed_at, row.review_note = at or utcnow(), note.strip()
    session.commit()
    return row


HEAD = "FMS answer not as documented"
PROOF = "A person checks the FMS documentation against what it now sends, then changes the adapter or records that the change is expected."
WHO = "Engineering"
# the calls and fields in the words a person reads
CALLS = {"payments search": "A payment search", "make payment": "A payment request", "leases search": "A lease search", "rent roll": "The rent roll"}
FIELDS = {"status": "payment status", "amount": "amount", "createdAt": "the time the payment was made", "monthlyRentalAmount": "rent",
          "rentChangeNewRentAmount": "scheduled rent", "effectiveRate": "net rent"}
# what the desk does about each kind of departure, checked against the code that does it
CONSEQUENCE = {
    ("payments search", "unlisted value"): ("Gatehouse keeps it as written and never reads it as paid or failed. No new payment starts on that lease until the FMS "
                                            "reports a status Gatehouse knows or the adapter is changed."),
    ("payments search", "not whole cents"): "Gatehouse never rounds an amount, so it does not read that lease's payments until the FMS sends whole cents.",
    ("make payment", "unlisted value"): "The payment is never sent again. It goes to a person if the FMS does not settle it.",
    ("make payment", "not whole cents"): "Gatehouse never rounds an amount: the payment is held as outcome unknown and never sent again.",
    ("payments search", "missing"): "Gatehouse never guesses an amount, so it does not read that lease's payments until the FMS sends one.",
    ("make payment", "missing"): "Gatehouse never guesses an amount: the payment is held as outcome unknown and never sent again.",
    ("leases search", "not whole cents"): "Gatehouse never rounds an amount, so the rent change check says it cannot tell for that lease.",
    ("rent roll", "not whole cents"): "Gatehouse never rounds an amount, so the rent change check says it cannot tell for that lease.",
}
KEPT_WITHOUT_TIME = "A payment first seen without it has no time of its own, so it cannot end a reminder hold."


def say(row: FmsObservation) -> str:
    """The record in operator words, for Today: no raw value or field name, so plain() has nothing to rewrite."""
    field = FIELDS.get(row.field, "a field")
    what = {"unlisted value": f"carried a {field} the FMS documentation does not list for that call",
            "not whole cents": f"carried an {field} that is not a whole number of cents",
            }.get(row.kind, f"left out {field}, which the documentation says that call returns")
    times = "one answer" if row.count == 1 else f"{row.count} answers"
    return (f"{CALLS.get(row.endpoint, 'An answer')} {what}. Seen in {times} so far. "
            + (KEPT_WITHOUT_TIME if row.field == "createdAt" else CONSEQUENCE.get((row.endpoint, row.kind), "")))
