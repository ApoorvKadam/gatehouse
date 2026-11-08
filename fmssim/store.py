"""Simulator state. Kept in its own database so the desk can never read it except through the API."""
from __future__ import annotations

import os
import random
import secrets
import string
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


_ALPHABET = string.ascii_letters + string.digits
_ids: random.Random = secrets.SystemRandom()


def restart_ids(seed: str | None = None) -> None:
    """With a seed, every reset hands out the same ids in the same order, so a seeded demo, a scenario and a
    screenshot all name the same payment. Without one, ids are random, like a real FMS."""
    global _ids
    _ids = random.Random(seed) if seed else secrets.SystemRandom()


def new_id(prefix: str) -> str:
    return f"{prefix}_" + "".join(_ids.choice(_ALPHABET) for _ in range(11))


def carrier_id() -> str:
    """A text's id as a carrier writes it: SM and 32 hex digits."""
    return "SM" + "".join(_ids.choice("0123456789abcdef") for _ in range(32))


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
    # the Cubby number tenants text, E.164
    sms_number: Mapped[str | None] = mapped_column(String, nullable=True)


class Customer(Base):
    __tablename__ = "customers"
    customer_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"))
    first_name: Mapped[str] = mapped_column(String)
    last_name: Mapped[str] = mapped_column(String)
    phone: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String)
    # what the customer told the FMS about texts; one who never said anything accepts everything
    sms_marketing: Mapped[bool] = mapped_column(Boolean, default=True)
    sms_suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    sms_suppression_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # the carrier's record that this number texted STOP; never returned by the API. The flag above only catches up
    # when a send to the customer is rejected, as Cubby documents ("sms.suppressed can lag a customer's STOP")
    sms_carrier_stop: Mapped[bool] = mapped_column(Boolean, default=False)


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


class AccessRule(Base):
    """The simulated FMS's own delinquency automation for one facility: after this many days overdue it disables the
    customer's gate code and marks the lease as needing an overlock. Cubby documents no such setting; real systems
    have one, and Gatehouse never reads this table (it holds its own, operator-attested rule)."""

    __tablename__ = "access_rules"
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"), primary_key=True)
    lockout_days: Mapped[int] = mapped_column(Integer)


class AccessCode(Base):
    """One gate code per customer per facility, as Cubby's access codes are."""

    __tablename__ = "access_codes"
    code_id: Mapped[str] = mapped_column(String, primary_key=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    code: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Lock(Base):
    __tablename__ = "locks"
    lock_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"))
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    serial: Mapped[str] = mapped_column(String, unique=True)
    combo: Mapped[str] = mapped_column(String)
    # AVAILABLE, OPERATOR_USE (restricting a unit), TENANT_USE, DISABLED
    status: Mapped[str] = mapped_column(String, default="AVAILABLE")
    attached_to: Mapped[str | None] = mapped_column(String, nullable=True)   # unit id
    disabled_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String, default="OPEN")
    customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Exemption(Base):
    """A delinquency exemption on one lease. One active exemption per lease."""

    __tablename__ = "delinquency_exemptions"
    exemption_id: Mapped[str] = mapped_column(String, primary_key=True)
    lease_id: Mapped[str] = mapped_column(ForeignKey("leases.lease_id"))
    reason: Mapped[str] = mapped_column(String)
    other_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    end_date: Mapped[date] = mapped_column(Date)
    pause_delinquency_fees: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_overlock: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_lien_eligibility: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_collections_messages: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class AccessFault(Base):
    """A named fault the simulator is told to have, kept inspectable in its truth reader:
    gate_sync_stale (customer at a facility: the automation stops updating that code),
    lose_task_response (facility: tasks/create makes the task, then the connection breaks),
    lose_send_response (facility: messages/sms/send sends the text, then the connection breaks)."""

    __tablename__ = "access_faults"
    fault_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)
    facility_id: Mapped[str] = mapped_column(String)
    customer_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Auction(Base):
    """A facility's lien sale. auctionTime is the facility's local time, as Cubby gives it."""

    __tablename__ = "auctions"
    auction_id: Mapped[str] = mapped_column(String, primary_key=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    auction_time: Mapped[datetime] = mapped_column(DateTime)
    advertisement_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_configuration_id: Mapped[str] = mapped_column(String)


class AuctionedLease(Base):
    """One lease in an auction. LISTED, NO_BIDS, SOLD, PAYMENT_RECEIVED, COMPLETE or CANCELLED. Who changed it and when
    is kept for the truth reader; Cubby's own response does not carry it."""

    __tablename__ = "auctioned_leases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    auction_id: Mapped[str] = mapped_column(ForeignKey("auctions.auction_id"))
    lease_id: Mapped[str] = mapped_column(ForeignKey("leases.lease_id"))
    unit_id: Mapped[str] = mapped_column(String)
    lien_start_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default="LISTED")
    winning_bidder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    bid_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changed_by: Mapped[str | None] = mapped_column(String, nullable=True)   # "api" or the staff member, for the truth reader
    changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuctionApiCall(Base):
    """Every request to an auctions endpoint that can change a sale, written before it is checked, so a refused call
    is on record too. The evaluation counts Gatehouse's red line from this, not from what Gatehouse says it sent."""

    __tablename__ = "auction_api_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String)
    lease_id: Mapped[str | None] = mapped_column(String, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExternalListing(Base):
    """A listing on an outside auction site, as that site's own feed reports it. Not part of the FMS: cancelling the
    lien in the FMS leaves it as it is."""

    __tablename__ = "external_listings"
    listing_id: Mapped[str] = mapped_column(String, primary_key=True)
    marketplace: Mapped[str] = mapped_column(String)
    facility_id: Mapped[str] = mapped_column(String)
    auction_id: Mapped[str] = mapped_column(String)
    lease_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="LISTED")   # LISTED or WITHDRAWN
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TextMessage(Base):
    """One text on a facility's Cubby number, in or out, as the simulator's own record of what the carrier carried.
    Not something a real FMS shows: the desk never reads it, and the preview shows it only next to what Gatehouse
    concluded. An inbound text is also what Cubby turns into its SMS Received webhook."""

    __tablename__ = "text_messages"
    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    facility_id: Mapped[str] = mapped_column(String)
    customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    direction: Mapped[str] = mapped_column(String)               # INBOUND or OUTBOUND
    phone: Mapped[str] = mapped_column(String)                   # the other party's number, E.164
    body: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)                  # RECEIVED, SENT or REJECTED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def unit_id_of(lease: "Lease") -> str:
    return "unit_" + lease.lease_id.removeprefix("lease_")


def facility_today(session: Session, facility_id: str) -> date:
    from zoneinfo import ZoneInfo
    facility = session.get(Facility, facility_id)
    return datetime.now(ZoneInfo(facility.time_zone if facility else "America/New_York")).date()


def active_exemption(session: Session, lease_id: str, today: date) -> "Exemption | None":
    return session.scalar(select(Exemption).where(Exemption.lease_id == lease_id, Exemption.cancelled.is_(False),
                                                  Exemption.end_date >= today))


def run_access_automation(session: Session, facility_id: str, customer_id: str) -> None:
    """What the simulated FMS does on its own when a customer's balance changes or its daily run comes round: set
    each lease's overlockNeeded, and disable the customer's gate code while any of their leases here is past the rule.
    It never attaches or removes a physical lock; that is a person's job."""
    rule = session.get(AccessRule, facility_id)
    if rule is None:
        return
    today = facility_today(session, facility_id)
    leases = session.scalars(select(Lease).where(Lease.facility_id == facility_id, Lease.customer_id == customer_id)).all()
    any_eligible = False
    for lease in leases:
        ex = active_exemption(session, lease.lease_id, today)
        overdue = lease.delinquency_start_date is not None and (today - lease.delinquency_start_date).days >= rule.lockout_days
        lease.overlock_needed = bool(overdue and not (ex and ex.pause_overlock))
        any_eligible = any_eligible or lease.overlock_needed
    stale = session.scalar(select(AccessFault).where(AccessFault.kind == "gate_sync_stale", AccessFault.facility_id == facility_id,
                                                    AccessFault.customer_id == customer_id))
    if stale is not None:
        return
    for code in session.scalars(select(AccessCode).where(AccessCode.facility_id == facility_id, AccessCode.customer_id == customer_id)):
        code.enabled = not any_eligible


def oldest_open_due(session: Session, lease_id: str) -> date | None:
    """When the oldest charge still open fell due: the date delinquency counts from."""
    entries = session.scalars(select(BookEntry).where(BookEntry.lease_id == lease_id, BookEntry.waive.is_(False))
                              .order_by(BookEntry.entry_time, BookEntry.book_entry_id)).all()
    for e in entries:
        if e.amount - e.amount_paid > 0:
            return e.accrues_on or e.entry_time.date()
    return None


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
    session.flush()
    run_access_automation(session, lease.facility_id, lease.customer_id)


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
    if lease_total_due(session, lease.lease_id) > 0:
        # delinquency counts from the oldest charge the reversal left unpaid, not from the day of the return
        session.flush()
        lease.delinquency_start_date = oldest_open_due(session, lease.lease_id) or when.date()
    lease.updated_at = when
    session.flush()
    run_access_automation(session, lease.facility_id, lease.customer_id)
