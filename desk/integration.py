"""Answers from the FMS that its documentation does not describe. The adapter notes them as they arrive; the worker saves the notes here, and
Today shows each one until a person has reviewed it. Nothing here changes how a payment is read: the adapter already refused the answer."""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .adapter import FMSAdapter
from .db import utcnow
from .models import FmsObservation


def save_notes(session: Session, adapter: FMSAdapter) -> int:
    """Write down what the adapter noted since the last pass; a repeat adds to the count of the record it belongs to. Returns how many notes."""
    notes = adapter.take_anomalies()
    now = utcnow()
    for (endpoint, field, kind, value), n in Counter((a.endpoint, a.field, a.kind, a.value) for a in notes).items():
        row = session.scalar(select(FmsObservation).where(FmsObservation.provider == adapter.provider, FmsObservation.endpoint == endpoint,
                                                          FmsObservation.field == field, FmsObservation.kind == kind, FmsObservation.value == value))
        if row is None:
            session.add(FmsObservation(provider=adapter.provider, endpoint=endpoint, field=field, kind=kind, value=value, count=n,
                                       first_seen=now, last_seen=now))
        else:
            row.count, row.last_seen = row.count + n, now
    session.commit()
    return len(notes)


def is_open(row: FmsObservation) -> bool:
    """Not reviewed yet, or seen again after the review."""
    return row.reviewed_at is None or row.last_seen > row.reviewed_at


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
# what the desk does about each kind of departure, in the words Today uses
CONSEQUENCE = {
    ("payments search", "unlisted value"): "Gatehouse keeps the status as written and does not treat it as paid or failed; a payment it concerns is held for a person.",
    ("payments search", "not whole cents"): "Gatehouse never rounds an amount, so it does not read that lease's payments until the FMS sends whole cents.",
    ("make payment", "unlisted value"): "A payment sent this way is held for a person and is never retried.",
    ("make payment", "not whole cents"): "Gatehouse never rounds an amount: a payment sent this way is held for a person and is never retried.",
}
KEPT_WITHOUT_TIME = "The payment is kept without a time of its own, so it cannot end a reminder hold."


def say(row: FmsObservation) -> str:
    """The record in operator words, for Today."""
    what = {"unlisted value": f"carried a {row.field} of {row.value!r}, which the FMS documentation does not list for that call",
            "not whole cents": f"carried a {row.field} of {row.value!r}, which is not a whole number of cents",
            }.get(row.kind, f"left out {row.field}, which the documentation says that call returns")
    times = "once" if row.count == 1 else f"{row.count} times"
    return f"A {row.endpoint} answer {what}. It has come {times}. " + (KEPT_WITHOUT_TIME if row.kind == "missing" else CONSEQUENCE.get((row.endpoint, row.kind), ""))
