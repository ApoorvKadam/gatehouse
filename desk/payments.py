"""Taking a payment from a past-due account and finding out, from the FMS, what actually happened to it.

The order is fixed: a fresh identity check, a proposal built from the FMS balance and the tenant's saved cards,
the tenant's yes, a claim on the account (one open payment per case, enforced by the database), a re-read of the
balance, the intent written down, one submission. Then the FMS's payment record and the lease ledger are read
until the outcome is known. An outcome the desk cannot establish from that evidence becomes an explicit state
with an owner (staff), and the account stays held until a person or the FMS settles it."""
from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, FMSError, NotFound, PermissionDenied, ProviderPayment
from .cases import record, refresh_case
from .db import utcnow
from .models import (
    HOLDING_STATES, DelinquencyCase, DeskBinding, Facility, IdentityCheck, Lease, LedgerSnapshot, ObservedPayment, PaymentCommand,
    PaymentProposal,
)

# what the FMS can later say about a payment that succeeded, and the state the desk records for each; none of
# them says anything about the balance, which only the ledger does
log = logging.getLogger("desk.payments")

REVERSALS = {"DIRECT_DEBIT_RETURN": "RETURNED", "REFUNDED": "REFUNDED", "REFUND_INITIATED": "REFUND_PENDING", "DISPUTED": "DISPUTED"}
WATCHED_AFTER_SUCCESS = ("CONFIRMED", "REFUND_PENDING", "DISPUTED")
# The finality rule: settled payments and paid cases are re-read for this long. Card networks allow disputes up to
# about 120 days after a transaction and ACH allows unauthorized-debit returns for 60 days; 180 days covers both
# with room. A payment that comes back after that is found by the next delinquency sync, not by this watch.
RETURN_WINDOW = timedelta(days=180)
UNRESOLVED = ("AWAITING_APPLICATION", "PENDING_AT_FMS", "OUTCOME_UNKNOWN")
# after this many reads with no trace of a lost payment, a person has to look at the FMS
MAX_UNKNOWN_CHECKS = 3
# a payment found at the FMS can be this lost submission only if it was created in this window around it
CLOCK_SKEW = timedelta(minutes=2)
LOST_MATCH_WINDOW = timedelta(minutes=10)
# an outside payment this recent when ours was sent may still be on its way to the ledger; older ones are taken to
# be applied already (a payment the desk itself confirmed always is)
RECENT_OUTSIDE_PAYMENT = timedelta(hours=24)

# how long the desk waits on the FMS before a person has to look: a payment the FMS calls SUCCEEDED that the
# ledger does not show, or a payment still PENDING at the gateway
APPLICATION_WAIT = timedelta(minutes=15)
PENDING_WAIT = timedelta(hours=1)


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


# a verified caller can pay for this long; after that they verify again, in any channel
IDENTITY_VALID_FOR = timedelta(minutes=15)
# what counts as the tenant's yes at the desk: it starts with a yes and takes nothing back
CLEAR_YES = re.compile(r"^\s*(yes|yes please|yeah|yep|yup|sure|ok|okay|confirm|confirmed|i confirm|go ahead|yes,? go ahead|"
                       r"please go ahead|please do|do it|that's fine|that is fine)[\s.!]*$", re.I)
# a proposal read back to the tenant has to be confirmed within this time, or read again
PROPOSAL_VALID_FOR = timedelta(minutes=10)
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


# Identity checks for one case run one at a time within this process. Several API processes would need a row
# lock or an atomic counter in the database instead.
_identity_locks: dict[int, threading.Lock] = {}
_identity_locks_guard = threading.Lock()


def check_identity(session: Session, adapter: FMSAdapter, case: DelinquencyCase, claim: IdentityClaim) -> IdentityCheck | None:
    """Check a caller and commit the result at once, one check per case at a time, so parallel conversations or
    requests cannot each slip guesses in before the others are counted. None means the case is locked."""
    with _identity_locks_guard:
        lock = _identity_locks.setdefault(case.id, threading.Lock())
    with lock:
        if identity_locked(session, case.id):
            return None
        ok, why = verify_identity(adapter, session.get(Lease, case.lease_id), claim)
        check = IdentityCheck(id=uuid.uuid4().hex, case_id=case.id, verified=ok, reason=why)
        session.add(check)
        session.commit()
        return check


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


def identity_fresh(check: IdentityCheck | None, case_id: int) -> bool:
    return check is not None and check.case_id == case_id and check.verified and utcnow() - check.created_at <= IDENTITY_VALID_FOR


def propose(session: Session, adapter: FMSAdapter, case: DelinquencyCase, identity_check_id: str, amount: int,
            payment_method_id: str | None = None, channel: str = "desk") -> PaymentProposal:
    """Build what will be read back to the tenant: an amount up to the FMS balance, on a card the FMS has on file
    for this tenant, after a fresh identity check. Nothing is charged here."""
    check = session.get(IdentityCheck, identity_check_id)
    if not identity_fresh(check, case.id):
        raise PaymentRefused("the caller's identity has not been checked in the last 15 minutes")
    lease = session.get(Lease, case.lease_id)
    balance = adapter.ledger(lease.provider_id).total_due
    if not 0 < amount <= balance:
        raise PaymentRefused(f"amount must be between 1 and the balance of {balance} cents")
    methods = adapter.payment_methods(lease.provider_customer_id)
    method = next((m for m in methods if payment_method_id in (None, m["paymentMethodId"])), None)
    if method is None:
        raise PaymentRefused("that card is not on file for this tenant" if payment_method_id else "no saved card on file")
    proposal = PaymentProposal(id=uuid.uuid4().hex, case_id=case.id, identity_check_id=identity_check_id, channel=channel,
                               amount=amount, balance_seen=balance, payment_method_id=method["paymentMethodId"],
                               card_last4=method.get("last4") or "",
                               # never outlives the identity check it rests on
                               expires_at=min(utcnow() + PROPOSAL_VALID_FOR, check.created_at + IDENTITY_VALID_FOR))
    session.add(proposal)
    session.commit()
    return proposal


def confirm(session: Session, adapter: FMSAdapter, case_id: int, proposal_id: str, confirmed_by: str, tenant_answer: str) -> PaymentCommand:
    """The tenant said yes to this proposal: `confirmed_by` is who reports it (the chat, or a named person at the
    desk) and `tenant_answer` the tenant's words. Confirming it again returns the same payment."""
    proposal = session.get(PaymentProposal, proposal_id)
    if proposal is None or proposal.case_id != case_id:
        raise PaymentRefused("no such proposal on this account")
    if proposal.command_id is not None:
        return session.get(PaymentCommand, proposal.command_id)
    if utcnow() > proposal.expires_at:
        raise PaymentRefused("the proposal expired; read the balance to the tenant again")
    if not confirmed_by.strip() or not tenant_answer.strip():
        raise PaymentRefused("a confirmation needs who heard the tenant's yes and what the tenant said")
    if not CLEAR_YES.match(tenant_answer.replace("\u2019", "'")):
        raise PaymentRefused(f"the tenant's answer {tenant_answer!r} is not a clear yes; nothing was charged")
    proposal.confirmed_by, proposal.tenant_answer = confirmed_by.strip(), tenant_answer.strip()
    verified = identity_fresh(session.get(IdentityCheck, proposal.identity_check_id), case_id)
    auth = Authorization(proposal.id, verified, proposal.balance_seen, proposal.amount, proposal.channel)
    command = PaymentService(session, adapter).take_payment(case_id, proposal.payment_method_id, auth)
    proposal.command_id = command.id
    session.commit()
    return command


class PaymentService:
    def __init__(self, session: Session, adapter: FMSAdapter):
        self.session, self.adapter = session, adapter
        # FMS reads that failed during this service's work; the worker counts a pass with any as a failed pass
        self.read_failures = 0

    def _read_failed(self, case: DelinquencyCase, detail: dict) -> None:
        self.read_failures += 1
        record(self.session, case, "fms read failed", detail)

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
            PaymentCommand.lease_id == lease.id, PaymentCommand.state.in_(HOLDING_STATES)))
        if in_flight is not None:
            raise PaymentRefused(f"payment {in_flight.id} on this unit is not settled yet ({in_flight.state})")
        method = next((m for m in self.adapter.payment_methods(lease.provider_customer_id)
                       if m["paymentMethodId"] == payment_method_id), None)
        if method is None:
            raise PaymentRefused("that card is not on file for this tenant")
        # The account is claimed before its balance is read. A payment that finishes while this one is being prepared
        # holds the claim until the ledger shows it, so this one either waits behind it or reads the new balance.
        command = PaymentCommand(case_id=case_id, lease_id=lease.id, idempotency_key=key, amount=auth.amount,
                                 method_type=method.get("methodType"), payment_method_id=payment_method_id,
                                 authorization=auth.__dict__, state="SUBMITTING", balance_before=0, prepaid_before=0)
        self.session.add(command)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            same = self.session.scalar(select(PaymentCommand).where(PaymentCommand.idempotency_key == key))
            if same is not None:
                return same   # the same authorization got here first
            raise PaymentRefused("another payment on this account is not settled yet")
        try:
            # payments first, then the balance: a payment that settles between the two reads is then either still
            # pending in the first (refused below) or already counted in the second
            known = self.adapter.payments_for_lease(lease.provider_id)
            ledger = self.adapter.ledger(lease.provider_id)
        except FMSError as exc:
            self._not_submitted(command, f"the FMS could not be read before charging: {exc}")
            raise PaymentRefused("the facility system did not answer; nothing was charged") from exc
        command.balance_before, command.prepaid_before = ledger.total_due, ledger.prepaid
        command.entries_paid_before = {e["bookEntryId"]: e.get("amountPaid", 0) for e in ledger.raw.get("bookEntries", [])}
        command.fms_payments_before = {p.provider_id: p.status for p in known}
        pending = [p.provider_id for p in known if p.status == "PENDING"]
        if pending:
            # a payment still pending at the FMS, from anyone, may yet succeed: charging now could charge twice
            self._not_submitted(command, f"payment {pending[0]} on this lease is still pending at the FMS")
            raise PaymentRefused(f"payment {pending[0]} on this lease is still pending at the FMS; wait for it to settle")
        unposted = self._unposted_outside_payment(lease, known, ledger)
        if unposted:
            self._not_submitted(command, unposted)
            raise PaymentRefused(unposted + "; check the ledger shows it before taking another payment")
        if ledger.total_due != auth.balance_seen:
            command.state = "NEEDS_REAUTHORIZATION"
            command.provider_reason = f"balance is now {ledger.total_due}, the tenant agreed while it was {auth.balance_seen}"
            record(self.session, case, "payment needs a new authorization", {"command": command.id, "balance": ledger.total_due})
            self.session.commit()
            return command
        if auth.amount > ledger.total_due:
            self._not_submitted(command, f"amount is more than the balance of {ledger.total_due} cents")
            raise PaymentRefused(f"amount is more than the balance of {ledger.total_due} cents")
        # written before the call: a command left SUBMITTING with this set may have charged; without it, it cannot have
        command.submitted_at = utcnow()
        self.session.commit()

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
        try:
            self.session.commit()
        except StaleDataError:
            # the FMS took longer than the worker's patience and the worker already moved this command on: keep its
            # state, add the payment id the FMS gave us, and let reconciliation decide from the evidence
            self.session.rollback()
            command = self.session.get(PaymentCommand, command.id)
            if command.provider_payment_id is None and outcome.payment is not None and outcome.payment.provider_id:
                command.provider_payment_id = outcome.payment.provider_id
            record(self.session, case, "payment submitted", {"command": command.id, "amount": auth.amount, "fms_said": outcome.kind,
                                                              "late": True, "detail": outcome.detail})
            self.session.commit()
        if command.state in UNRESOLVED:
            self.reconcile(command)
        self._refresh(case)
        self.session.commit()
        return command

    # ------------------------------------------------------------------ reconcile
    def reconcile(self, command: PaymentCommand) -> PaymentCommand:
        """Read the FMS once and move the command as far as the evidence allows. Never resubmits. If another pass
        changed the command while this one worked, this pass's older evidence is dropped."""
        try:
            return self._reconcile(command)
        except StaleDataError:
            self.session.rollback()
            log.info("payment %s was updated by another pass first; kept the newer state", command.id)
            return self.session.get(PaymentCommand, command.id)

    def _reconcile(self, command: PaymentCommand) -> PaymentCommand:
        if command.state not in UNRESOLVED + WATCHED_AFTER_SUCCESS + ("NEEDS_STAFF",):
            return command
        lease = self.session.get(Lease, command.lease_id)
        case = self.session.get(DelinquencyCase, command.case_id)
        command.checks += 1
        command.last_checked_at = utcnow()
        try:
            payments = self.adapter.payments_for_lease(lease.provider_id)
        except FMSError as exc:
            self._read_failed(case, {"command": command.id, "error": str(exc)})
            self._save()
            return command
        for p in payments:
            self._observe(lease, p)
        match = next((p for p in payments if p.provider_id == command.provider_payment_id), None) if command.provider_payment_id else None
        if match is None and command.provider_payment_id is None and command.state in ("OUTCOME_UNKNOWN", "NEEDS_STAFF"):
            found = self._find_lost(command, lease, payments)
            if found is None:
                if command.state == "OUTCOME_UNKNOWN" and command.checks >= MAX_UNKNOWN_CHECKS:
                    command.state = "NEEDS_STAFF"
                    command.provider_reason = (f"no payment of {command.amount} cents appeared at the FMS after {command.checks} checks; "
                                               "someone has to confirm with the FMS before any new attempt")
                    record(self.session, case, "payment needs staff", {"command": command.id})
                self._save()
                return command
            # Amount, customer, card type and time make a lead, not proof: Cubby gives no reference that ties a
            # payment to the request that created it, and the tenant may have paid some other way meanwhile. Staff
            # see the suggestion and decide; the unit stays held until they do.
            if command.suggested_payment_id != found.provider_id:
                command.state, command.suggested_payment_id = "NEEDS_STAFF", found.provider_id
                command.provider_reason = f"payment {found.provider_id} ({found.status}) at the FMS may be this attempt; confirm it before any new one"
                record(self.session, case, "possible match for staff", {"command": command.id, "payment": found.provider_id,
                                                                        "status": found.status})
            self._save()
            return command
        if match is None:
            if command.state in UNRESOLVED:
                self._wait(command, case, command.state, PENDING_WAIT,
                           f"payment {command.provider_payment_id} no longer appears at the FMS; check it before any new attempt")
            self._save()
            return command
        command.provider_status = match.status
        if command.state != "NEEDS_STAFF":
            command.provider_reason = match.reason   # a person's open question stays on the command until it is answered
        if command.state in WATCHED_AFTER_SUCCESS and command.state != "CONFIRMED" and match.status == "SUCCEEDED":
            # a dispute or refund the FMS now reports as succeeded again: the ledger has to show it again, and a person checks
            why = (f"the FMS reports payment {match.provider_id} as SUCCEEDED again after {command.state.lower().replace('_', ' ')}; "
                   "confirm the ledger shows it before calling it applied")
            holder = self.session.scalar(select(PaymentCommand.id).where(PaymentCommand.lease_id == lease.id, PaymentCommand.id != command.id,
                                                                        PaymentCommand.state.in_(HOLDING_STATES)).limit(1))
            if holder is None:
                command.state = "NEEDS_STAFF"
            # with another payment holding the lease, this one keeps its state (new charges are refused while it shows
            # succeeded again) and moves to staff once the lease is free
            if command.provider_reason != why:
                command.provider_reason = why
                record(self.session, case, "payment needs staff", {"command": command.id, "reason": why})
        elif command.state in WATCHED_AFTER_SUCCESS:
            after = REVERSALS.get(match.status) or ("CONFIRMED" if match.status == "SUCCEEDED" else command.state)
            if after != command.state:
                record(self.session, case, "payment status changed after success",
                       {"command": command.id, "from": command.state, "to": after, "fms_status": match.status})
                command.state = after
                if after != "CONFIRMED":
                    command.reversed_at = utcnow()
        elif match.status in ("DECLINED", "FAILED", "REJECTED"):
            command.state = "DECLINED"
            record(self.session, case, "payment declined", {"command": command.id, "status": match.status, "reason": match.reason})
        elif match.status == "PENDING":
            self._wait(command, case, "PENDING_AT_FMS", PENDING_WAIT,
                       f"the FMS still reports payment {match.provider_id} as PENDING; check it with the processor before any new attempt")
        elif match.status == "SUCCEEDED" and match.amount != command.amount:
            if command.state != "NEEDS_STAFF":
                command.state = "NEEDS_STAFF"
                command.provider_reason = f"the FMS reports payment {match.provider_id} for {match.amount} cents; {command.amount} was authorized"
                record(self.session, case, "payment needs staff", {"command": command.id, "reason": command.provider_reason})
        elif match.status == "SUCCEEDED":
            try:
                applied, ambiguous = self._applied_to_lease(command, lease, payments)
            except FMSError as exc:
                self._read_failed(case, {"command": command.id, "error": str(exc)})
                self._save()
                return command
            if applied and command.state == "NEEDS_STAFF":
                pass   # once a person owns it, a ledger inference does not settle it; staff confirm it
            elif applied:
                command.state, command.confirmed_at = "CONFIRMED", utcnow()
                record(self.session, case, "payment confirmed", {"command": command.id, "payment": match.provider_id,
                                                                 "amount": command.amount})
            elif ambiguous:
                self._wait(command, case, "AWAITING_APPLICATION", APPLICATION_WAIT, ambiguous + "; confirm which payment landed")
            else:
                self._wait(command, case, "AWAITING_APPLICATION", APPLICATION_WAIT,
                           f"the FMS reports payment {match.provider_id} as SUCCEEDED, but the lease ledger does not show it")
        elif match.status in REVERSALS:
            command.state, command.reversed_at = REVERSALS[match.status], utcnow()
        else:
            self._wait(command, case, command.state, PENDING_WAIT,
                       f"the FMS reports payment {match.provider_id} as {match.status!r}, which the desk does not know; check it before any new attempt")
        self._save()
        return command

    def resolve_by_staff(self, command: PaymentCommand, charged: bool, note: str, staff: str,
                         fms_payment_id: str | None = None, ledger_checked: bool = False) -> PaymentCommand:
        """A named person checked the FMS or the processor for a payment the desk could not settle on its own.

        "Charged" names the FMS payment, which must be on this lease and belong to no other command. Naming it says which
        payment this attempt is, not that it posted: reconciliation then settles it from the ledger as usual, unless the
        person also states they checked the ledger shows it, which confirms it under their name. "Not charged" releases the unit, so it is refused while the FMS shows this
        payment as anything but declined, and while a payment that may be this one sits at the FMS, unless the note
        names that payment (the person looked at it and says it is something else)."""
        if command.state != "NEEDS_STAFF":
            raise PaymentRefused(f"only a payment waiting on staff can be resolved (this one is {command.state})")
        case = self.session.get(DelinquencyCase, command.case_id)
        lease = self.session.get(Lease, command.lease_id)
        payments = self.adapter.payments_for_lease(lease.provider_id)
        if charged:
            if command.provider_payment_id and fms_payment_id != command.provider_payment_id:
                raise PaymentRefused(f"this attempt is already payment {command.provider_payment_id} at the FMS; another id cannot replace it")
            named = next((p for p in payments if p.provider_id == fms_payment_id), None)
            if named is None:
                raise PaymentRefused(f"payment {fms_payment_id} is not on this lease at the FMS")
            impossible = self._cannot_be(command, lease, named)
            if impossible:
                raise PaymentRefused(f"payment {fms_payment_id} cannot be this attempt: {impossible}")
            owner = self.session.scalar(select(PaymentCommand).where(PaymentCommand.provider_payment_id == fms_payment_id,
                                                                     PaymentCommand.id != command.id))
            if owner is not None:
                raise PaymentRefused(f"payment {fms_payment_id} already belongs to payment {owner.id}")
            command.provider_payment_id, command.identified_by_staff = fms_payment_id, True
            if ledger_checked:
                # the statement settles which payment the ledger money belongs to; it cannot stand in for the money
                if named.status != "SUCCEEDED":
                    self.session.rollback()
                    raise PaymentRefused(f"payment {fms_payment_id} is {named.status} at the FMS; it cannot be confirmed as applied")
                applied, ambiguous = self._applied_to_lease(command, lease, payments)
                if not (applied or ambiguous):
                    self.session.rollback()
                    raise PaymentRefused(f"the ledger does not show money for payment {fms_payment_id}; it cannot be confirmed as applied")
                command.state, command.confirmed_at = "CONFIRMED", utcnow()
            else:
                command.state = "OUTCOME_UNKNOWN"
        else:
            known = next((p for p in payments if p.provider_id == command.provider_payment_id), None) if command.provider_payment_id else None
            if known is not None and known.status not in ("DECLINED", "FAILED", "REJECTED"):
                raise PaymentRefused(f"the FMS shows payment {known.provider_id} as {known.status}; it cannot be recorded as not charged")
            leads = [] if command.provider_payment_id else self._lost_candidates(command, lease, payments)
            if command.suggested_payment_id and command.suggested_payment_id not in {p.provider_id for p in leads}:
                leads += [p for p in payments if p.provider_id == command.suggested_payment_id]
            still_open = [p for p in leads if p.status == "PENDING"]
            if still_open:
                raise PaymentRefused(f"payment {still_open[0].provider_id} at the FMS may be this attempt and is still PENDING; "
                                     "it cannot be called not charged until it settles")
            unnamed = [p for p in leads if p.provider_id not in note]
            if unnamed:
                raise PaymentRefused("payments at the FMS that may be this attempt: "
                                     + ", ".join(f"{p.provider_id} ({p.status})" for p in unnamed)
                                     + ". Resolve as charged, or name each in the note and say why it is not this attempt")
            command.state = "CLEARED_BY_STAFF"
        command.provider_reason = note
        record(self.session, case, "payment resolved by staff", {"command": command.id, "charged": charged, "staff": staff,
                                                                 "fms_payment": fms_payment_id, "ledger_checked": ledger_checked,
                                                                 "note": note}, actor=staff)
        self.session.commit()
        if charged and not ledger_checked:
            self.reconcile(command)
        self._refresh(case)
        self.session.commit()
        return command

    def reconcile_all(self) -> int:
        """One pass of the worker: every unsettled payment, and confirmed ones that could still come back, at the facilities the key sees."""
        recent = utcnow() - RETURN_WINDOW
        # a command left in SUBMITTING by a crash may or may not have reached the FMS: look for it instead of resending
        for stuck in self.session.scalars(select(PaymentCommand).where(
                PaymentCommand.state == "SUBMITTING", PaymentCommand.created_at < utcnow() - timedelta(minutes=2))).all():
            if stuck.submitted_at is None:
                stuck.state, stuck.provider_reason = "NOT_SUBMITTED", "stopped before the FMS was called"
            else:
                stuck.state = "OUTCOME_UNKNOWN"
        self.session.commit()
        served = self._served_facilities()
        if served is not None:
            self._send_unserved_to_staff(served)
        payments = select(PaymentCommand).where(
            (PaymentCommand.state.in_(UNRESOLVED))
            | ((PaymentCommand.state == "NEEDS_STAFF") & (PaymentCommand.created_at >= recent))
            | ((PaymentCommand.state == "CONFIRMED") & (PaymentCommand.confirmed_at >= recent))
            # a refund or dispute still open is watched until the FMS settles it, however old
            | (PaymentCommand.state.in_(("REFUND_PENDING", "DISPUTED"))))
        # payments taken outside the desk (at the counter, by autopay) still change open cases, and a payment that
        # settled a case can still come back within the return window
        cases = select(DelinquencyCase).where((DelinquencyCase.state != "paid") | (DelinquencyCase.resolved_at >= recent))
        if served is not None:
            # a facility the key no longer sees cannot be read: what is there waits for it, and does not fail every pass
            payments = payments.join(Lease, Lease.id == PaymentCommand.lease_id).where(Lease.facility_id.in_(served))
            cases = cases.join(Lease, Lease.id == DelinquencyCase.lease_id).where(Lease.facility_id.in_(served))
        commands = self.session.scalars(payments).all()
        touched = set()
        for command in commands:
            self.reconcile(command)
            touched.add(command.case_id)
        for case in self.session.scalars(cases).all():
            self.observe_case(case)
            touched.add(case.id)
        for case_id in touched:
            self._refresh(self.session.get(DelinquencyCase, case_id))
        self.session.commit()
        return len(commands)

    def _served_facilities(self):
        """The facilities the desk's key sees, as a query on their ids; None when the desk has no list to go by. A key that sees
        no facility at all is a broken key: its reads fail and the worker says so, instead of every open payment going to staff."""
        binding = self.session.get(DeskBinding, 1)
        # ponytail: a desk without a binding (the unit tests) serves everything; the running app always has one from its start
        if binding is None or not binding.facility_ids:
            return None
        return select(Facility.id).where(Facility.provider_id.in_(binding.facility_ids))

    def _send_unserved_to_staff(self, served) -> None:
        """An open payment at a facility the key no longer sees cannot be checked, so it must not sit as pending for good."""
        reason = "this facility is no longer served by the desk's key, so the outcome cannot be checked"
        # each case is loaded with its payment: a query between the change and the save would write the change outside _save(),
        # which is where a pass that lost a race on the row drops its change and goes on
        gone = self.session.execute(select(PaymentCommand, DelinquencyCase).select_from(PaymentCommand)
                                    .join(Lease, Lease.id == PaymentCommand.lease_id)
                                    .join(DelinquencyCase, DelinquencyCase.id == PaymentCommand.case_id)
                                    .where(PaymentCommand.state.in_(UNRESOLVED), Lease.facility_id.not_in(served))).all()
        for command, case in gone:
            command.state, command.provider_reason = "NEEDS_STAFF", reason
            record(self.session, case, "payment needs staff", {"command": command.id, "reason": reason})
            log.warning("payment %s is at a facility the key no longer sees; sent to staff", command.id)
        self._save()

    def observe_case(self, case: DelinquencyCase) -> None:
        """Record every payment the FMS shows on this case's lease, whoever took it."""
        lease = self.session.get(Lease, case.lease_id)
        try:
            payments = self.adapter.payments_for_lease(lease.provider_id)
        except FMSError as exc:
            self._read_failed(case, {"error": str(exc)})
            return
        known = set(self.session.scalars(select(PaymentCommand.provider_payment_id).where(PaymentCommand.case_id == case.id)).all())
        for p in payments:
            if p.status in REVERSALS:
                # a desk payment that comes back after the watch window is still caught when its lease is read again
                settled = self.session.scalar(select(PaymentCommand).where(PaymentCommand.provider_payment_id == p.provider_id,
                                                                           PaymentCommand.state == "CONFIRMED"))
                if settled is not None:
                    self.reconcile(settled)
            new = self._observe(lease, p)
            if new and p.provider_id not in known and p.status == "SUCCEEDED":
                record(self.session, case, "payment taken outside the desk", {"payment": p.provider_id, "amount": p.amount}, source="fms")

    # ------------------------------------------------------------------ helpers
    def _refresh(self, case: DelinquencyCase) -> None:
        """Re-read a case from the FMS and publish it. One lease the FMS cannot answer for never stops the others, and
        a refresh that lost the race to a newer one leaves the newer picture in place."""
        try:
            refresh_case(self.session, self.adapter, case)
            self.session.commit()
        except FMSError as exc:
            self._read_failed(case, {"error": str(exc)})
        except StaleDataError:
            self.session.rollback()
            log.info("case %s was refreshed by another pass first; kept the newer state", case.id)

    def _save(self) -> None:
        """Commit what this pass learned, unless another pass changed the command since it was read, or the change
        would give the lease a second holding payment: then this pass's change is dropped and the next pass retries."""
        try:
            self.session.commit()
        except StaleDataError:
            self.session.rollback()
            log.info("a newer reconciliation already updated this payment; dropped the older one")
        except IntegrityError:
            self.session.rollback()
            log.warning("a reconciliation change would have given a lease two holding payments; dropped it")

    def _unposted_outside_payment(self, lease: Lease, payments: list[ProviderPayment], ledger) -> str | None:
        """Why a new charge must wait for a payment the ledger may not show yet, or None.

        Two cases: a payment the desk recorded as disputed or refund-pending that the FMS now reports as succeeded again
        (its reapplication is unproven), and payments taken outside the desk in the last day. Those are reflected only if
        money paid against charges, or prepaid, has grown by at least their combined amount since the desk's last
        snapshot from before the earliest of them; a waiver lowers the balance without paying anything, so it does not
        count, and two payments cannot both lean on the same increase."""
        by_id = {p.provider_id: p for p in payments}
        revived = self.session.scalars(select(PaymentCommand).where(PaymentCommand.lease_id == lease.id,
                                                                    PaymentCommand.state.in_(("DISPUTED", "REFUND_PENDING", "NEEDS_STAFF")),
                                                                    PaymentCommand.reversed_at.is_not(None))).all()
        for c in revived:
            if c.provider_payment_id in by_id and by_id[c.provider_payment_id].status == "SUCCEEDED":
                return f"payment {c.provider_payment_id} was reversed and now shows as succeeded again; staff must confirm the ledger"
        confirmed = set(self.session.scalars(select(PaymentCommand.provider_payment_id).where(
            PaymentCommand.provider_payment_id.is_not(None), PaymentCommand.state == "CONFIRMED")).all())
        cutoff = utcnow() - RECENT_OUTSIDE_PAYMENT
        undated = [p for p in payments if p.status == "SUCCEEDED" and p.provider_id not in confirmed and p.created_at is None]
        if undated:
            return f"payment {undated[0].provider_id} at the FMS has no date, so whether it is on the ledger cannot be checked"
        recent = [p for p in payments if p.status == "SUCCEEDED" and p.provider_id not in confirmed
                  and p.created_at is not None and p.created_at >= cutoff]
        if not recent:
            return None
        earliest = min(p.created_at for p in recent)
        before = self.session.scalar(select(LedgerSnapshot).where(LedgerSnapshot.lease_id == lease.id, LedgerSnapshot.observed_at < earliest)
                                     .order_by(LedgerSnapshot.observed_at.desc()).limit(1))
        if before is None:
            return f"payment {recent[0].provider_id} taken outside the desk cannot be checked against an earlier ledger"
        then = {e["bookEntryId"]: e.get("amountPaid", 0) for e in before.raw.get("bookEntries", [])}
        now = {e["bookEntryId"]: e.get("amountPaid", 0) for e in ledger.raw.get("bookEntries", [])}
        increase = sum(now.get(i, 0) - was for i, was in then.items()) + sum(v for i, v in now.items() if i not in then)
        increase += ledger.prepaid - before.prepaid
        # desk payments confirmed since that snapshot (whenever they were created) account for part of the increase too. "Since" is when its
        # read began, not when its row was stored: a payment confirmed between the two is in the increase and not in the snapshot
        posted_since = self.session.scalars(select(PaymentCommand.amount).where(
            PaymentCommand.lease_id == lease.id, PaymentCommand.state == "CONFIRMED",
            PaymentCommand.confirmed_at > (before.read_at or before.observed_at))).all()
        required = sum(p.amount for p in recent) + sum(posted_since)
        if increase < required:
            return f"payment {recent[0].provider_id} taken outside the desk is not visibly on the ledger yet"
        return None

    def _not_submitted(self, command: PaymentCommand, why: str) -> None:
        command.state, command.provider_reason = "NOT_SUBMITTED", why
        self.session.commit()

    def _wait(self, command: PaymentCommand, case: DelinquencyCase, state: str, limit: timedelta, why: str) -> None:
        """Keep waiting on the FMS, or hand the payment to a person once the wait is longer than it should be."""
        if command.state == "NEEDS_STAFF":
            return
        if utcnow() - (command.submitted_at or command.created_at) > limit:
            command.state, command.provider_reason = "NEEDS_STAFF", why
            record(self.session, case, "payment needs staff", {"command": command.id, "reason": why})
        else:
            command.state = state

    def _applied_to_lease(self, command: PaymentCommand, lease: Lease, payments: list[ProviderPayment]) -> tuple[bool, str | None]:
        """The FMS ledger, not the payment record, decides whether the money reached this lease. Returns (applied,
        why it cannot be decided).

        Only money paid against the charges that existed when the tenant agreed, or added to prepaid, counts, so a
        fee posted afterwards cannot hide the payment and a waiver cannot look like one. Cubby's ledger does not say
        which payment paid an entry, so the money is attributed to this payment only when no other payment on the
        lease could have put it there: none that appeared since this one was sent, none that was or is pending, and
        none taken outside the desk in the day before. Otherwise the answer is "cannot tell" and a person decides.
        Older payments, and payments the desk itself confirmed, are taken to be on the ledger already."""
        ledger = self.adapter.ledger(lease.provider_id)
        now = {e["bookEntryId"]: e.get("amountPaid", 0) for e in ledger.raw.get("bookEntries", [])}
        paid = sum(now[i] - was for i, was in (command.entries_paid_before or {}).items() if i in now)
        paid += ledger.prepaid - command.prepaid_before
        before = command.fms_payments_before or {}
        sent = command.submitted_at or command.created_at
        desk_confirmed = set(self.session.scalars(select(PaymentCommand.provider_payment_id).where(
            PaymentCommand.provider_payment_id.is_not(None), PaymentCommand.state == "CONFIRMED")).all())
        in_motion = [p.provider_id for p in payments
                     if p.provider_id != command.provider_payment_id and p.provider_id not in desk_confirmed
                     and p.status in ("SUCCEEDED", "PENDING")
                     and (p.status == "PENDING" or p.provider_id not in before or before[p.provider_id] == "PENDING"
                          or (p.created_at is not None and p.created_at >= sent - RECENT_OUTSIDE_PAYMENT))]
        if paid >= command.amount and in_motion:
            return False, f"the ledger shows the money, but payment {in_motion[0]} on this lease could have put it there"
        return paid >= command.amount, None

    def _find_lost(self, command: PaymentCommand, lease: Lease, payments: list[ProviderPayment]) -> ProviderPayment | None:
        """The one FMS payment that could be this submission, as a suggestion for staff. Zero or several find nothing."""
        candidates = self._lost_candidates(command, lease, payments)
        return candidates[0] if len(candidates) == 1 else None

    def _cannot_be(self, command: PaymentCommand, lease: Lease, p: ProviderPayment) -> str | None:
        """Why an FMS payment can be ruled out as this attempt from the record alone, or None."""
        sent = command.submitted_at or command.created_at
        if p.provider_id in (command.fms_payments_before or {}):
            return "it was already on the lease before this attempt was sent"
        if p.amount != command.amount:
            return f"it is for {p.amount} cents and this attempt was for {command.amount}"
        if p.customer_provider_id not in (None, lease.provider_customer_id):
            return "it belongs to another customer"
        if command.method_type and p.method_type and p.method_type != command.method_type:
            return f"it was paid by {p.method_type}, this attempt by {command.method_type}"
        if p.created_at is not None and p.created_at < sent - CLOCK_SKEW:
            return "it was created before this attempt was sent"
        return None

    def _lost_candidates(self, command: PaymentCommand, lease: Lease, payments: list[ProviderPayment]) -> list[ProviderPayment]:
        """FMS payments that could be this submission: same amount, same customer, same kind of card, created within
        minutes of it, new since it was sent, and not already some other command's."""
        claimed = set(self.session.scalars(select(PaymentCommand.provider_payment_id).where(
            PaymentCommand.provider_payment_id.is_not(None), PaymentCommand.id != command.id)).all())
        sent = command.submitted_at or command.created_at
        seen_before = command.fms_payments_before or {}
        candidates = [p for p in payments if p.amount == command.amount and p.provider_id not in claimed
                      and p.provider_id not in seen_before
                      and p.created_at is not None and sent - CLOCK_SKEW <= p.created_at <= sent + LOST_MATCH_WINDOW
                      and (p.customer_provider_id in (None, lease.provider_customer_id))
                      and (command.method_type is None or p.method_type == command.method_type)]
        return candidates

    def _observe(self, lease: Lease, p: ProviderPayment) -> bool:
        """Keep one row per FMS payment. Returns True the first time a payment is seen."""
        row = self.session.scalar(select(ObservedPayment).where(ObservedPayment.provider == self.adapter.provider,
                                                                ObservedPayment.provider_payment_id == p.provider_id))
        if row is None:
            # already reversed when first seen: when that happened is unknown, so no date is recorded
            row = ObservedPayment(provider=self.adapter.provider, provider_payment_id=p.provider_id, amount=p.amount,
                                  status=p.status, lease_provider_ids=[lease.provider_id],
                                  status_changed_at=None if p.status in REVERSALS else utcnow())
            self.session.add(row)
            self.session.flush()
            return True
        if row.status != p.status:
            row.status_changed_at = utcnow()
        row.status, row.last_seen_at = p.status, utcnow()
        if lease.provider_id not in row.lease_provider_ids:
            row.lease_provider_ids = row.lease_provider_ids + [lease.provider_id]
        return False


def collected(session: Session, provider: str, facility_provider_id: str) -> int:
    """Money the FMS reports as collected on this facility's tracked leases, counted once per payment however many
    leases it covered."""
    from .models import Facility
    leases = set(session.scalars(select(Lease.provider_id).join(Facility, Facility.id == Lease.facility_id)
                                 .where(Facility.provider == provider, Facility.provider_id == facility_provider_id)).all())
    rows = session.scalars(select(ObservedPayment).where(ObservedPayment.provider == provider, ObservedPayment.status == "SUCCEEDED")).all()
    return sum(p.amount for p in rows if leases & set(p.lease_provider_ids))


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
        "NOT_SUBMITTED": "We could not start that payment. Nothing was charged.",
        "NEEDS_REAUTHORIZATION": "Your balance changed since we spoke. Nothing was charged; please confirm the new amount.",
        "RETURNED": f"Your {dollars} payment was returned by your bank. A team member will contact you about the account.",
        "REFUNDED": f"Your {dollars} payment was refunded.",
        "REFUND_PENDING": f"A refund of your {dollars} payment has been started.",
        "DISPUTED": f"Your card issuer has opened a dispute on your {dollars} payment. A team member will contact you about it.",
        "CLEARED_BY_STAFF": f"We checked your {dollars} payment with our records: it was not charged.",
    }.get(command.state, "We are working on your payment.")
