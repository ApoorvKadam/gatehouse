"""A staff review: the FMS task that asks a person to act on a sale that is still listed.

The model may ask for one. The desk decides, acts and reads the task back from the FMS, and nothing here says an action happened
until that read-back found the task. A task the desk already opened is linked and read back. One the desk has not asked for yet is
asked for only when the desk's own check of the packet found the sale listed with nothing owed, and never twice: `ensure_task`
claims the item before it calls the FMS, and when an answer is lost it only looks the task up by its marker."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .access import ensure_task
from .adapter.base import FMSAdapter
from .db import utcnow
from .models import Facility, WorkItem
from .sales import LISTED_PAID


def staff_review(session: Session, adapter: FMSAdapter, facility: Facility, item: WorkItem, confirmed: bool) -> dict:
    """Link or ask for the FMS task of an open sale item, read it back, and say what happened. `confirmed` is the desk's own check that
    the sale is listed with nothing owed. The outcome is one of: linked (the task was already there), requested (the desk asked the
    FMS for it just now), not_confirmed (no task could be found in the FMS), refused (the FMS refused to make it) and not_requested
    (the desk had no confirmed sale to ask a task for)."""
    before = item.task_state
    task = (item.observed or {}).get("task")
    failures: list[str] = []
    if before == "none":
        if not (confirmed and item.kind == LISTED_PAID and task):
            return {"outcome": "not_requested", "task_status": None, "read_at": None}
        ensure_task(session, adapter, facility, item, task, failures, keep_note=True)
    elif before == "created":
        ensure_task(session, adapter, facility, item, None, failures, keep_note=True)
    else:
        # sending or outcome_unknown: an answer that never arrived is looked up by its marker, never asked for again. rejected stays rejected.
        ensure_task(session, adapter, facility, item, task, failures, keep_note=True)
    session.refresh(item)
    read_at = utcnow().isoformat(timespec="seconds")
    if failures:
        outcome = "not_confirmed"
    elif item.task_state == "rejected":
        outcome = "refused"
    elif item.task_state == "created" and item.task_status and item.task_status != "not found":
        outcome = "requested" if before == "none" else "linked"
    else:
        outcome = "not_confirmed"
    return {"outcome": outcome, "task_status": item.task_status if outcome in ("linked", "requested") else None, "read_at": read_at}
