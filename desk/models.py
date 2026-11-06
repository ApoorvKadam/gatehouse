"""The desk's own records. Three kinds of fact are kept apart on purpose:

- what the FMS reports (ledger snapshots, provider payment status), copied with the time it was observed;
- what the desk itself did or saw (payment commands, case events);
- what the desk concludes from those (a case's state and hold reasons).

The desk never keeps a balance of its own. A balance always comes from the latest FMS snapshot."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class Facility(Base):
    __tablename__ = "facilities"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    provider_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    jurisdiction: Mapped[str] = mapped_column(String)
    time_zone: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("provider", "provider_id"),)


class Lease(Base):
    __tablename__ = "leases"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    provider: Mapped[str] = mapped_column(String)
    provider_id: Mapped[str] = mapped_column(String)
    provider_customer_id: Mapped[str] = mapped_column(String)
    unit_name: Mapped[str] = mapped_column(String)
    billing_cycle: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("provider", "provider_id"),)


class LedgerSnapshot(Base):
    """One read of a lease's ledger, exactly as the FMS answered."""

    __tablename__ = "ledger_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    total_due: Mapped[int] = mapped_column(Integer)
    prepaid: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw: Mapped[dict] = mapped_column(JSON)


class DelinquencyCase(Base):
    """One delinquency episode on one lease, from the first missed charge until it is paid off."""

    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    state: Mapped[str] = mapped_column(String, default="open")
    hold_reasons: Mapped[list] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String, default="front desk")
    last_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_snapshots.id"), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaymentCommand(Base):
    """An authorized intent to take one payment, and everything learned about it afterwards.

    The row is written before the FMS is called, so a crash or a lost response leaves a record of what
    was attempted instead of nothing."""

    __tablename__ = "payment_commands"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    payment_method_id: Mapped[str] = mapped_column(String)
    authorization: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String)
    balance_before: Mapped[int] = mapped_column(Integer)
    prepaid_before: Mapped[int] = mapped_column(Integer)
    provider_payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    checks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ObservedPayment(Base):
    """A payment as the FMS reports it, once per provider payment, however many leases it touched."""

    __tablename__ = "observed_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    provider_payment_id: Mapped[str] = mapped_column(String)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    lease_provider_ids: Mapped[list] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("provider", "provider_payment_id"),)


class CaseEvent(Base):
    """An append-only history: who did what, where it came from, when it happened and when we saw it."""

    __tablename__ = "case_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    kind: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdentityCheck(Base):
    """A caller proved who they are for one case. Payments cite it; a check expires after a short while."""

    __tablename__ = "identity_checks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    verified: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
