from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    url = url or os.environ.get("DESK_DATABASE_URL", "sqlite:///./data/desk.db")
    kwargs: dict = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    if url == "sqlite:///:memory:":
        kwargs["poolclass"] = StaticPool
    elif url.startswith("sqlite:///"):
        os.makedirs(os.path.dirname(url.removeprefix("sqlite:///")) or ".", exist_ok=True)
    engine = create_engine(url, **kwargs)
    from . import models  # noqa: F401  registers the tables

    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
