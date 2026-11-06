"""Simulator state. Kept in its own database so the desk can never read it except through the API."""
from __future__ import annotations

import os
import secrets
import string
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def new_id(prefix: str) -> str:
    alphabet = string.ascii_letters + string.digits
    return f"{prefix}_" + "".join(secrets.choice(alphabet) for _ in range(11))


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"
    org_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)


class ApiKey(Base):
    __tablename__ = "api_keys"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"))
    # "OPERATOR" may take payments; "READ_ONLY" may only search
    role: Mapped[str] = mapped_column(String)
    pii: Mapped[bool] = mapped_column(Boolean, default=True)


class Facility(Base):
    __tablename__ = "facilities"
    facility_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"))
    name: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    time_zone: Mapped[str] = mapped_column(String)
    billing_cycle: Mapped[str] = mapped_column(String, default="MONTHLY")


class Customer(Base):
    __tablename__ = "customers"
    customer_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"))
    first_name: Mapped[str] = mapped_column(String)
    last_name: Mapped[str] = mapped_column(String)
    phone: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String)


class Lease(Base):
    __tablename__ = "leases"
    lease_id: Mapped[str] = mapped_column(String, primary_key=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    unit_name: Mapped[str] = mapped_column(String)
    move_in_date: Mapped[date] = mapped_column(Date)
    monthly_rental_amount: Mapped[int] = mapped_column(Integer)
    billing_cycle: Mapped[str] = mapped_column(String, default="MONTHLY")
    paid_through_date: Mapped[date] = mapped_column(Date)
    prepaid_balance: Mapped[int] = mapped_column(Integer, default=0)
    delinquency_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    overlock_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    payment_method_id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    method_type: Mapped[str] = mapped_column(String, default="CREDIT_CARD")
    last4: Mapped[str] = mapped_column(String)


class BookEntry(Base):
    __tablename__ = "book_entries"
    book_entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    lease_id: Mapped[str] = mapped_column(ForeignKey("leases.lease_id"))
    type: Mapped[str] = mapped_column(String, default="CHARGE")
    category: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[int] = mapped_column(Integer)
    amount_paid: Mapped[int] = mapped_column(Integer, default=0)
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    payment_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accrues_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    accrues_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    waive: Mapped[bool] = mapped_column(Boolean, default=False)


class Payment(Base):
    __tablename__ = "payments"
    payment_id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    amount: Mapped[int] = mapped_column(Integer)
    method_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    explanation: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # what the payment will become once the gateway answers (for PENDING payments)
    resolves_to: Mapped[str | None] = mapped_column(String, nullable=True)
    # payment search calls left before a pending payment resolves
    resolve_after: Mapped[int] = mapped_column(Integer, default=0)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    # payment search calls left before a succeeded payment shows on the lease ledger
    apply_after: Mapped[int] = mapped_column(Integer, default=0)
    # {lease_id: cents} for payments that cover more than one lease
    allocations: Mapped[dict] = mapped_column(JSON, default=dict)


class LeaseBehavior(Base):
    """How the simulator answers payments on one lease. Set through /sim/behaviors."""

    __tablename__ = "lease_behaviors"
    lease_id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String)
    # number of payment searches before a PENDING payment or a late ledger application settles
    lag: Mapped[int] = mapped_column(Integer, default=2)
    decline_reason: Mapped[str] = mapped_column(String, default="Insufficient funds")


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    url = url or os.environ.get("SIM_DATABASE_URL", "sqlite:///./data/fmssim.db")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        os.makedirs(os.path.dirname(url.removeprefix("sqlite:///")) or ".", exist_ok=True)
    kwargs: dict = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    if url == "sqlite:///:memory:":
        kwargs["poolclass"] = StaticPool   # one shared connection, or every thread sees an empty database
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def lease_total_due(session: Session, lease_id: str) -> int:
    entries = session.scalars(select(BookEntry).where(BookEntry.lease_id == lease_id, BookEntry.waive.is_(False))).all()
    return sum(e.amount - e.amount_paid for e in entries)


def apply_to_lease(session: Session, lease: Lease, amount: int, when: datetime) -> None:
    """Pay the oldest open charges first; anything left over becomes prepaid balance."""
    entries = session.scalars(
        select(BookEntry)
        .where(BookEntry.lease_id == lease.lease_id, BookEntry.waive.is_(False))
        .order_by(BookEntry.entry_time, BookEntry.book_entry_id)
    ).all()
    left = amount
    for e in entries:
        open_amount = e.amount - e.amount_paid
        if open_amount <= 0 or left <= 0:
            continue
        take = min(open_amount, left)
        e.amount_paid += take
        e.payment_time = when
        left -= take
    lease.prepaid_balance += left
    if lease_total_due(session, lease.lease_id) == 0:
        lease.delinquency_start_date = None
        lease.overlock_needed = False
    lease.updated_at = when


def reverse_on_lease(session: Session, lease: Lease, amount: int, when: datetime) -> None:
    """Undo an applied amount, newest payments first, as a returned payment does."""
    from_prepaid = min(lease.prepaid_balance, amount)
    lease.prepaid_balance -= from_prepaid
    left = amount - from_prepaid
    entries = session.scalars(
        select(BookEntry)
        .where(BookEntry.lease_id == lease.lease_id, BookEntry.waive.is_(False))
        .order_by(BookEntry.entry_time.desc(), BookEntry.book_entry_id.desc())
    ).all()
    for e in entries:
        if left <= 0:
            break
        take = min(e.amount_paid, left)
        e.amount_paid -= take
        left -= take
    if lease_total_due(session, lease.lease_id) > 0 and lease.delinquency_start_date is None:
        lease.delinquency_start_date = when.date()
    lease.updated_at = when
