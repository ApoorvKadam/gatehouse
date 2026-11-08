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


def same_account(before_facilities, now_facilities, before_orgs=(), now_orgs=()) -> bool:
    """Is what the key sees now the account the desk was bound to? When the FMS names organizations on both sides they are
    the account's identity: the same organizations are the same account whichever facilities come and go, and a key that
    sees different organizations is another account. Without them, one facility list must contain the other and they must
    share a facility, so a key that sees some old and some new facilities is left to a person to rebind."""
    if before_orgs and now_orgs:
        return set(before_orgs) == set(now_orgs)
    before, now = set(before_facilities), set(now_facilities)
    return bool(before & now) and (before <= now or now <= before)


def bind_to_fms(factory, adapter, fms_base_url: str, attempts: int = 10) -> None:
    """Refuse to run against a database that holds another FMS account's records.

    The account is recognized by the FMS address and the facilities its key can see. A portfolio changes (a facility is added or sold), so the
    database belongs to this account while the two facility lists share at least one facility, and the stored list
    follows the current one. A different address, or no facility in common, is another account. An unbound database
    that already holds leases is refused too: nobody can say which account they came from."""
    import time

    from sqlalchemy import select

    from .models import DeskBinding, Lease

    for attempt in range(attempts):
        try:
            seen = adapter.facilities()
            facilities = sorted(f["facilityId"] for f in seen)
            orgs = sorted({f["organizationId"] for f in seen if f.get("organizationId")})
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
            session.add(DeskBinding(id=1, fms_base_url=fms_base_url, facility_ids=facilities, org_ids=orgs))
        else:
            # one kind of change at a time: facilities added, or facilities removed
            if binding.fms_base_url != fms_base_url or not same_account(binding.facility_ids, facilities, binding.org_ids or [], orgs):
                raise RuntimeError("this database belongs to another FMS account; each operator needs its own database")
            binding.facility_ids, binding.org_ids = facilities, orgs
        session.commit()


def refresh_binding(session, adapter) -> None:
    """Read again which facilities and organizations the key can see, so one added after the desk started is served. The
    same rule as at start (same_account): a list that is not the same account changes nothing. Raises FMSError when the FMS
    cannot be asked."""
    from .models import DeskBinding

    seen = adapter.facilities()
    facilities = sorted(f["facilityId"] for f in seen)
    binding = session.get(DeskBinding, 1)
    if binding is None:
        return
    if not facilities:
        # the same key sees nothing now: no facility is served, and the account's organizations stay as they were
        binding.facility_ids = []
        session.commit()
        return
    orgs = sorted({f["organizationId"] for f in seen if f.get("organizationId")})
    if not same_account(binding.facility_ids, facilities, binding.org_ids or [], orgs):
        return
    binding.facility_ids, binding.org_ids = facilities, orgs
    session.commit()
