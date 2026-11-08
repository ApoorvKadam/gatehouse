"""What a Claude run may cost, and who may start one. The limits are the owner's: $1 a day in all, and ten full cases per visitor
a day, counted in New York time. A refused request is told why in plain words and costs nothing.

The count lives in the desk's own database, so a restart of the preview starts it again; that is enough while the link is
private. The guard in front of a public link belongs in a service that a restart cannot reset."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import AgentRun

DAY_ZONE = ZoneInfo("America/New_York")
# The most one run can cost: eight model calls, each with the largest prompt, tools and results it can carry, and a full answer.
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


def spent_today(session: Session, now: datetime | None = None) -> float:
    """What today's runs cost: the finished ones as they were counted, and for each run still going the most it could still cost."""
    done = session.scalar(select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)).where(AgentRun.started_at >= day_start(now), AgentRun.status != "running"))
    going = session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.status == "running")) or 0
    return float(done) + going * WORST_RUN_USD


def cases_today(session: Session, visitor: str, now: datetime | None = None) -> int:
    return session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.visitor == visitor, AgentRun.started_at >= day_start(now))) or 0


def allowance(session: Session, budget_usd: float, cases_per_day: int, visitor: str | None, now: datetime | None = None) -> Allowance:
    """May one more run start now? A visitor has a number of cases a day; the operator does not. Everyone shares the day's budget."""
    left = max(0.0, budget_usd - spent_today(session, now))
    going = session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.status == "running")) or 0
    cases_left = None if visitor is None else max(0, cases_per_day - cases_today(session, visitor, now))
    if cases_left == 0:
        return Allowance(False, f"You have used your {cases_per_day} cases for today. They come back at midnight, New York time.", 0, left)
    if left < WORST_RUN_USD:
        return Allowance(False, "Today's budget for live Claude runs is used up. It comes back at midnight, New York time.", cases_left, left)
    if going >= MAX_RUNNING:
        return Allowance(False, "Claude is busy with other cases. Try again in a minute.", cases_left, left)
    return Allowance(True, None, cases_left, left)


def visitor_of(request) -> str:
    """A short hash of where a request came from. Render's front door puts the visitor's address in a header; the hash keeps the
    address itself out of the database."""
    ip = request.headers.get("cf-connecting-ip") or (request.headers.get("x-forwarded-for", "").split(",")[0].strip()) \
        or (request.client.host if request.client else "unknown")
    return hashlib.sha256(f"gatehouse-visitor:{ip}".encode()).hexdigest()[:16]


def refusal(session: Session, budget_usd: float, cases_per_day: int, visitor: str | None) -> str | None:
    """Why one more run may not start now, in plain words, or None."""
    return allowance(session, budget_usd, cases_per_day, visitor).reason
