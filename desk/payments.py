"""Taking a payment so that it is never charged twice and never reported before the FMS shows it.

The order is fixed: check who is paying, re-read the balance they agreed to, write the intent, submit once,
then read the FMS's own payment record and the lease ledger until the outcome is certain. Anything the desk
cannot confirm becomes an explicit state with an owner, not a guess."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, FMSError, NotFound, PermissionDenied, ProviderPayment
from .cases import record, refresh_case
from .db import utcnow
from .models import DelinquencyCase, IdentityCheck, Lease, ObservedPayment, PaymentCommand

RETURNED_STATUSES = {"REFUND_INITIATED", "REFUNDED", "DISPUTED", "DIRECT_DEBIT_RETURN"}
UNRESOLVED = ("AWAITING_APPLICATION", "PENDING_AT_FMS", "OUTCOME_UNKNOWN")
# after this many reads with no trace of a lost payment, a person has to look at the FMS
MAX_UNKNOWN_CHECKS = 3
# how far back a lost payment may have been created relative to our submit time
CLOCK_SKEW = timedelta(minutes=2)


class PaymentRefused(Exception):
    """The desk will not submit this payment. Nothing was sent to the FMS."""


@dataclass(frozen=True)
class IdentityClaim:
    last_name: str
    phone_last4: str
    unit_name: str


@dataclass(frozen=True)
class Authorization:
    """The tenant's explicit yes to one amount, given after their identity was checked and the balance was read to them."""

    authorization_id: str
    identity_verified: bool
    balance_seen: int
    amount: int
    channel: str = "chat"

    @staticmethod
    def new(identity_verified: bool, balance_seen: int, amount: int, channel: str = "chat") -> "Authorization":
        return Authorization(uuid.uuid4().hex, identity_verified, balance_seen, amount, channel)


IDENTITY_TRIES = 3
IDENTITY_LOCK = timedelta(minutes=30)


def identity_locked(session: Session, case_id: int) -> bool:
    """Three failed checks on a case since its last success lock it for 30 minutes, whichever channel they came from.
    Counted on the case, not the conversation, so starting a new conversation does not reset it."""
    last_ok = session.scalar(select(func.max(IdentityCheck.created_at))
                             .where(IdentityCheck.case_id == case_id, IdentityCheck.verified.is_(True)))
    since = max(utcnow() - IDENTITY_LOCK, last_ok) if last_ok else utcnow() - IDENTITY_LOCK
    failed = session.scalar(select(func.count()).select_from(IdentityCheck)
                            .where(IdentityCheck.case_id == case_id, IdentityCheck.verified.is_(False), IdentityCheck.created_at > since))
    return failed >= IDENTITY_TRIES


def verify_identity(adapter: FMSAdapter, lease: Lease, claim: IdentityClaim) -> tuple[bool, str]:
    """True only when the caller matches the tenant on file. A key that cannot see personal data never verifies."""
    try:
        customer = adapter.customer(lease.provider_customer_id)
    except (NotFound, PermissionDenied):
        return False, "the tenant record is not available to this desk"
    if customer.last_name is None or customer.phone is None:
        return False, "this desk's FMS access does not include tenant contact details, so identity cannot be checked"
    if not (len(claim.phone_last4.strip()) == 4 and claim.phone_last4.strip().isdigit()):
        return False, "the details do not match the tenant on file"
    matches = (customer.last_name.strip().lower() == claim.last_name.strip().lower()
               and customer.phone[-4:] == claim.phone_last4.strip()
               and lease.unit_name.strip().lower() == claim.unit_name.strip().lower())
    return (True, "matched last name, phone and unit") if matches else (False, "the details do not match the tenant on file")


class PaymentService:
    def __init__(self, session: Session, adapter: FMSAdapter):
        self.session, self.adapter = session, adapter

    # ------------------------------------------------------------------ submit
    def take_payment(self, case_id: int, payment_method_id: str, auth: Authorization) -> PaymentCommand:
        case = self.session.get(DelinquencyCase, case_id)
        lease = self.session.get(Lease, case.lease_id)
        key = f"case-{case_id}:auth-{auth.authorization_id}"
        existing = self.session.scalar(select(PaymentCommand).where(PaymentCommand.idempotency_key == key))
        if existing is not None:
            # a double click, a retried request or a replayed message: same authorization, same command, no new charge
            return existing
        if not auth.identity_verified:
            raise PaymentRefused("identity not verified")
        if auth.amount <= 0:
            raise PaymentRefused("amount must be positive")
        in_flight = self.session.scalar(select(PaymentCommand).where(
            PaymentCommand.case_id == case_id, PaymentCommand.state.in_(("SUBMITTING",) + UNRESOLVED + ("NEEDS_STAFF",))))
        if in_flight is not None:
            raise PaymentRefused(f"payment {in_flight.id} on this account is not settled yet ({in_flight.state})")
        ledger = self.adapter.ledger(lease.provider_id)
        command = PaymentCommand(case_id=case_id, lease_id=lease.id, idempotency_key=key, amount=auth.amount,
                                 payment_method_id=payment_method_id, authorization=auth.__dict__, state="SUBMITTING",
                                 balance_before=ledger.total_due, prepaid_before=ledger.prepaid)
        if ledger.total_due != auth.balance_seen:
            command.state = "NEEDS_REAUTHORIZATION"
            command.provider_reason = f"balance is now {ledger.total_due}, the tenant agreed while it was {auth.balance_seen}"
        self.session.add(command)
        try:
            self.session.commit()
        except IntegrityError:
            # another request with the same authorization got here first
            self.session.rollback()
            return self.session.scalar(select(PaymentCommand).where(PaymentCommand.idempotency_key == key))
        if command.state == "NEEDS_REAUTHORIZATION":
            record(self.session, case, "payment needs a new authorization", {"command": command.id, "balance": ledger.total_due})
            self.session.commit()
            return command

        command.submitted_at = utcnow()
        outcome = self.adapter.make_payment(lease.provider_id, payment_method_id, auth.amount)
        if outcome.payment is not None:
            command.provider_payment_id = outcome.payment.provider_id or None
            command.provider_status = outcome.payment.status
            command.provider_reason = outcome.payment.reason
        command.state = {"accepted": "AWAITING_APPLICATION", "pending": "PENDING_AT_FMS", "declined": "DECLINED",
                         "rejected": "REJECTED", "lost": "OUTCOME_UNKNOWN"}[outcome.kind]
        if outcome.kind in ("rejected", "lost"):
            command.provider_reason = outcome.detail + (": " + "; ".join(outcome.errors) if outcome.errors else "")
        record(self.session, case, "payment submitted", {"command": command.id, "amount": auth.amount, "fms_said": outcome.kind,
                                                          "status": command.provider_status, "detail": outcome.detail})
        self.session.commit()
        if command.state in UNRESOLVED:
            self.reconcile(command)
        refresh_case(self.session, self.adapter, case)
        self.session.commit()
        return command

    # ------------------------------------------------------------------ reconcile
    def reconcile(self, command: PaymentCommand) -> PaymentCommand:
        """Read the FMS once and move the command as far as the evidence allows. Never resubmits."""
        if command.state not in UNRESOLVED + ("CONFIRMED",):
            return command
        lease = self.session.get(Lease, command.lease_id)
        case = self.session.get(DelinquencyCase, command.case_id)
        command.checks += 1
        command.last_checked_at = utcnow()
        try:
            payments = self.adapter.payments_for_lease(lease.provider_id)
        except FMSError as exc:
            record(self.session, case, "fms read failed", {"command": command.id, "error": str(exc)})
            self.session.commit()
            return command
        for p in payments:
            self._observe(lease, p)
        match = next((p for p in payments if p.provider_id == command.provider_payment_id), None) if command.provider_payment_id else None
        if command.state == "OUTCOME_UNKNOWN" and match is None:
            match = self._find_lost(command, payments)
            if match is None:
                if command.checks >= MAX_UNKNOWN_CHECKS:
                    command.state = "NEEDS_STAFF"
                    command.provider_reason = (f"no payment of {command.amount} cents appeared at the FMS after {command.checks} checks; "
                                               "someone has to confirm with the FMS before any new attempt")
                    record(self.session, case, "payment needs staff", {"command": command.id})
                self.session.commit()
                return command
            command.provider_payment_id = match.provider_id
            record(self.session, case, "lost payment found at the fms", {"command": command.id, "payment": match.provider_id})
        if match is None:
            self.session.commit()
            return command
        command.provider_status, command.provider_reason = match.status, match.reason
        if command.state == "CONFIRMED":
            if match.status in RETURNED_STATUSES:
                command.state = "RETURNED"
                record(self.session, case, "payment returned", {"command": command.id, "status": match.status})
        elif match.status in ("DECLINED", "FAILED", "REJECTED"):
            command.state = "DECLINED"
            record(self.session, case, "payment declined", {"command": command.id, "status": match.status, "reason": match.reason})
        elif match.status == "PENDING":
            command.state = "PENDING_AT_FMS"
        elif match.status == "SUCCEEDED":
            if self._applied_to_lease(command, lease):
                command.state, command.confirmed_at = "CONFIRMED", utcnow()
                record(self.session, case, "payment confirmed", {"command": command.id, "payment": match.provider_id,
                                                                 "amount": command.amount})
            else:
                command.state = "AWAITING_APPLICATION"
        elif match.status in RETURNED_STATUSES:
            command.state = "RETURNED"
        self.session.commit()
        return command

    def reconcile_all(self) -> int:
        """One pass of the worker: every unsettled payment, and confirmed ones that could still come back."""
        recent = utcnow() - timedelta(days=60)
        # a command left in SUBMITTING by a crash may or may not have reached the FMS: look for it instead of resending
        for stuck in self.session.scalars(select(PaymentCommand).where(
                PaymentCommand.state == "SUBMITTING", PaymentCommand.created_at < utcnow() - timedelta(minutes=2))).all():
            stuck.state = "OUTCOME_UNKNOWN"
            stuck.submitted_at = stuck.submitted_at or stuck.created_at
        self.session.commit()
        commands = self.session.scalars(select(PaymentCommand).where(
            (PaymentCommand.state.in_(UNRESOLVED)) | ((PaymentCommand.state == "CONFIRMED") & (PaymentCommand.confirmed_at >= recent)))).all()
        touched = set()
        for command in commands:
            self.reconcile(command)
            touched.add(command.case_id)
        # payments taken outside the desk (at the counter, by autopay) still change open cases
        for case in self.session.scalars(select(DelinquencyCase).where(DelinquencyCase.state.not_in(("paid",)))).all():
            self.observe_case(case)
            touched.add(case.id)
        for case_id in touched:
            refresh_case(self.session, self.adapter, self.session.get(DelinquencyCase, case_id))
        self.session.commit()
        return len(commands)

    def observe_case(self, case: DelinquencyCase) -> None:
        """Record every payment the FMS shows on this case's lease, whoever took it."""
        lease = self.session.get(Lease, case.lease_id)
        try:
            payments = self.adapter.payments_for_lease(lease.provider_id)
        except FMSError as exc:
            record(self.session, case, "fms read failed", {"error": str(exc)})
            return
        known = set(self.session.scalars(select(PaymentCommand.provider_payment_id).where(PaymentCommand.case_id == case.id)).all())
        for p in payments:
            new = self._observe(lease, p)
            if new and p.provider_id not in known and p.status == "SUCCEEDED":
                record(self.session, case, "payment taken outside the desk", {"payment": p.provider_id, "amount": p.amount}, source="fms")

    # ------------------------------------------------------------------ helpers
    def _applied_to_lease(self, command: PaymentCommand, lease: Lease) -> bool:
        """The FMS ledger, not the payment record, decides whether the money reached this lease."""
        ledger = self.adapter.ledger(lease.provider_id)
        later = self.session.scalars(select(PaymentCommand).where(
            PaymentCommand.lease_id == lease.id, PaymentCommand.id != command.id, PaymentCommand.state == "CONFIRMED",
            PaymentCommand.submitted_at > command.submitted_at)).all()
        applied = (command.balance_before - ledger.total_due) + (ledger.prepaid - command.prepaid_before) - sum(c.amount for c in later)
        return applied >= command.amount

    def _find_lost(self, command: PaymentCommand, payments: list[ProviderPayment]) -> ProviderPayment | None:
        claimed = set(self.session.scalars(select(PaymentCommand.provider_payment_id).where(
            PaymentCommand.provider_payment_id.is_not(None), PaymentCommand.id != command.id)).all())
        earliest = (command.submitted_at or command.created_at) - CLOCK_SKEW
        candidates = [p for p in payments if p.amount == command.amount and p.provider_id not in claimed
                      and (p.created_at is None or p.created_at >= earliest)]
        return candidates[0] if len(candidates) == 1 else None

    def _observe(self, lease: Lease, p: ProviderPayment) -> bool:
        """Keep one row per FMS payment. Returns True the first time a payment is seen."""
        row = self.session.scalar(select(ObservedPayment).where(ObservedPayment.provider == self.adapter.provider,
                                                                ObservedPayment.provider_payment_id == p.provider_id))
        if row is None:
            row = ObservedPayment(provider=self.adapter.provider, provider_payment_id=p.provider_id, amount=p.amount,
                                  status=p.status, lease_provider_ids=[lease.provider_id])
            self.session.add(row)
            self.session.flush()
            return True
        row.status, row.last_seen_at = p.status, utcnow()
        if lease.provider_id not in row.lease_provider_ids:
            row.lease_provider_ids = row.lease_provider_ids + [lease.provider_id]
        return False


def collected(session: Session, provider: str) -> int:
    """Money the FMS reports as collected, counted once per payment however many leases it covered."""
    return sum(p.amount for p in session.scalars(select(ObservedPayment).where(ObservedPayment.provider == provider,
                                                                                ObservedPayment.status == "SUCCEEDED")).all())


def tenant_message(command: PaymentCommand) -> str:
    """What the tenant is told. Only a confirmed payment is described as applied."""
    dollars = f"${command.amount / 100:,.2f}"
    return {
        "CONFIRMED": f"Your payment of {dollars} has been applied to your account.",
        "AWAITING_APPLICATION": f"Your card accepted the {dollars} payment. It has not reached your account yet, and we will confirm once it does.",
        "PENDING_AT_FMS": f"Your {dollars} payment is still processing. Please do not pay again; we will confirm the result.",
        "OUTCOME_UNKNOWN": f"We could not confirm whether your {dollars} payment went through. Please do not pay again; we are checking and will follow up.",
        "NEEDS_STAFF": f"We could not confirm your {dollars} payment. A team member will check it and contact you. Please do not pay again.",
        "DECLINED": f"Your {dollars} payment did not go through" + (f" ({command.provider_reason})." if command.provider_reason else "."),
        "REJECTED": "We could not start that payment. Nothing was charged.",
        "NEEDS_REAUTHORIZATION": "Your balance changed since we spoke. Nothing was charged; please confirm the new amount.",
        "RETURNED": f"Your {dollars} payment was returned by your bank, so the balance is open again.",
    }.get(command.state, "We are working on your payment.")
