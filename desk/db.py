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


def bind_to_fms(factory, adapter, fms_base_url: str, attempts: int = 10) -> None:
    """Refuse to run against a database that holds another FMS account's records.

    Cubby keys belong to an organization but the API exposes no organization id, so the account is recognized by
    the FMS address and the facilities its key can see. A portfolio changes (a facility is added or sold), so the
    database belongs to this account while the two facility lists share at least one facility, and the stored list
    follows the current one. A different address, or no facility in common, is another account. An unbound database
    that already holds leases is refused too: nobody can say which account they came from."""
    import time

    from sqlalchemy import select

    from .models import DeskBinding, Lease

    for attempt in range(attempts):
        try:
            facilities = sorted(f["facilityId"] for f in adapter.facilities())
            break
        except Exception:
            if attempt == attempts - 1:
                raise RuntimeError("the FMS did not answer, so this database's account cannot be checked; not starting")
            time.sleep(1)
    with factory() as session:
        binding = session.get(DeskBinding, 1)
        if binding is None:
            if session.scalar(select(Lease.id).limit(1)) is not None:
                raise RuntimeError("this database already holds leases but records no FMS account; not starting")
            session.add(DeskBinding(id=1, fms_base_url=fms_base_url, facility_ids=facilities))
        else:
            before, now = set(binding.facility_ids), set(facilities)
            # one kind of change at a time: facilities added, or facilities removed. A key that sees some of the old
            # facilities and some new ones may be another operator sharing one site, so a person rebinds explicitly.
            if binding.fms_base_url != fms_base_url or not (before <= now or now <= before) or not (before & now):
                raise RuntimeError("this database belongs to another FMS account; each operator needs its own database")
            binding.facility_ids = facilities
        session.commit()
