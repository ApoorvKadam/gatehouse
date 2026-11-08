"""What a live run may cost, and who may start one. The limits are $1 a day in all, and ten full cases per visitor
a day, counted in New York time. A refused request is told why in plain words and costs nothing.

The count lives in the desk's own database, so a restart of the preview starts it again; that is enough while the link is
private. The guard in front of a public link belongs in a service that a restart cannot reset."""
from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import AgentRun

DAY_ZONE = ZoneInfo("America/New_York")
# The key of the visitor hash, made when the process starts. Without it anyone holding the database could hash every address and
# read the visitors back. A restart gives every visitor a fresh value, which is what a restart does to the counts anyway.
_VISITOR_KEY = secrets.token_bytes(16)
# No run is let to cost more than this. The investigator stops before any call that could take it past, and counts a call that
# ended with no answer as if it had been billed in full. A run still going is therefore reserved at this amount.
WORST_RUN_USD = 0.03
# Runs going at the same moment. A burst of clicks cannot start more than this before the first one is counted.
MAX_RUNNING = 3


class Refused(Exception):
    """A run was not started because a limit is used up. The message says which, in plain words."""


@dataclass
class Allowance:
    ok: bool
    reason: str | None
    cases_left: int | None        # for a visitor; none for the operator
    budget_left_usd: float


def day_start(now: datetime | None = None) -> datetime:
    """The start of today in New York, as a naive UTC time like every time in the database."""
    local = (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(DAY_ZONE)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


def running(session: Session, exclude: int | None = None) -> int:
    """Runs going now. `exclude` leaves one out, so a run already written can ask about the others."""
    query = select(func.count()).select_from(AgentRun).where(AgentRun.status == "running")
    return session.scalar(query.where(AgentRun.id != exclude) if exclude is not None else query) or 0


def spent_today(session: Session, now: datetime | None = None, exclude: int | None = None) -> float:
    """What today's runs cost: the finished ones as they were counted, and for each run still going the most it could still cost."""
    done = session.scalar(select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)).where(AgentRun.started_at >= day_start(now), AgentRun.status != "running"))
    return float(done) + running(session, exclude) * WORST_RUN_USD


def cases_today(session: Session, visitor: str, now: datetime | None = None, exclude: int | None = None) -> int:
    query = select(func.count()).select_from(AgentRun).where(AgentRun.visitor == visitor, AgentRun.started_at >= day_start(now))
    return session.scalar(query.where(AgentRun.id != exclude) if exclude is not None else query) or 0


def allowance(session: Session, budget_usd: float, cases_per_day: int, visitor: str | None, now: datetime | None = None,
              exclude: int | None = None) -> Allowance:
    """May one more run start now? A visitor has a number of cases a day, and everyone shares the day's budget. The operator is not
    stopped by the budget, so visitors cannot lock the operator out; the operator's runs still count in it. Asked again after a run is
    written, with that run left out, the answer is the one the database gave when it was alone with the others."""
    left = max(0.0, budget_usd - spent_today(session, now, exclude))
    cases_left = None if visitor is None else max(0, cases_per_day - cases_today(session, visitor, now, exclude))
    if cases_left == 0:
        return Allowance(False, f"You have used your {cases_per_day} cases for today. They come back at midnight, New York time.", 0, left)
    if visitor is not None and left < WORST_RUN_USD:
        return Allowance(False, "Today's budget for live runs is used up. It comes back at midnight, New York time.", cases_left, left)
    if running(session, exclude) >= MAX_RUNNING:
        return Allowance(False, "Cases are being prepared for other visitors. Try again in a minute.", cases_left, left)
    return Allowance(True, None, cases_left, left)


def visitor_of(request) -> str:
    """A short keyed hash of where a request came from. Render's front door puts the visitor's address in a header; the hash keeps the
    address itself out of the database. A header can be forged by someone who reaches the app without the front door, so the count
    per visitor is a courtesy; what bounds the spend is the day's budget and the cap on runs going at once."""
    ip = request.headers.get("cf-connecting-ip") or (request.headers.get("x-forwarded-for", "").split(",")[0].strip()) \
        or (request.client.host if request.client else "unknown")
    return hmac.new(_VISITOR_KEY, ip.encode(), "sha256").hexdigest()[:16]


def refusal(session: Session, budget_usd: float, cases_per_day: int, visitor: str | None, exclude: int | None = None) -> str | None:
    """Why one more run may not start now, in plain words, or None."""
    return allowance(session, budget_usd, cases_per_day, visitor, exclude=exclude).reason
