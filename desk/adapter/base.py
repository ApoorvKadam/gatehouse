from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol


class FMSError(Exception):
    """The FMS answered with an error, or could not be reached for a read."""


class NotFound(FMSError):
    """The record does not exist for this API key. Another operator's record looks the same."""


class PermissionDenied(FMSError):
    """The API key is not allowed to do this."""


@dataclass(frozen=True)
class Anomaly:
    """An answer from the FMS that its documentation does not describe: which call, which field, what kind of departure, and a short, safe
    rendering of the value. It carries no payment, customer or account data."""
    endpoint: str       # the call, in words: "payments search" or "make payment"
    field: str
    kind: str           # "unlisted value", "not whole cents" or "missing"
    value: str


@dataclass(frozen=True)
class ProviderLease:
    provider_id: str
    facility_provider_id: str
    customer_provider_id: str
    unit_name: str
    billing_cycle: str
    total_due: int
    delinquent_since: str | None
    unit_id: str | None = None
    overlock_needed: bool | None = None     # None: the FMS did not say


@dataclass(frozen=True)
class ProviderLedger:
    lease_provider_id: str
    total_due: int
    prepaid: int
    raw: dict


@dataclass(frozen=True)
class SmsPreference:
    """What a customer told the FMS about texts. `reason` is OPTED_OUT or UNDELIVERABLE when the channel is suppressed."""

    marketing: bool
    suppressed: bool
    reason: str | None


@dataclass(frozen=True)
class ProviderCustomer:
    provider_id: str
    # None when the API key cannot see personal data
    first_name: str | None
    last_name: str | None
    phone: str | None
    # None when the key cannot see communication preferences, or the FMS did not send them
    sms: SmsPreference | None = None


@dataclass(frozen=True)
class ProviderPayment:
    provider_id: str
    amount: int
    status: str
    reason: str | None
    created_at: datetime | None
    method_type: str | None = None
    customer_provider_id: str | None = None


@dataclass(frozen=True)
class SubmitOutcome:
    """What submitting a payment told us. 'lost' means no usable answer came back: the payment may or may not exist."""

    kind: Literal["accepted", "declined", "pending", "rejected", "lost"]
    payment: ProviderPayment | None = None
    detail: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderAccessCode:
    code: str
    enabled: bool
    customer_provider_id: str


@dataclass(frozen=True)
class AccessCheck:
    allowed: bool
    message: str


@dataclass(frozen=True)
class ProviderLock:
    lock_id: str
    status: str                 # AVAILABLE, OPERATOR_USE, TENANT_USE, DISABLED
    attached_to: str | None
    serial: str


@dataclass(frozen=True)
class ProviderExemption:
    lease_provider_id: str
    reason: str
    end_date: str
    pause_overlock: bool
    note: str | None
    pause_collections_messages: bool = False      # the exemption pauses the FMS's own collections texts for the lease


@dataclass(frozen=True)
class ProviderLead:
    provider_id: str
    customer_provider_id: str
    facility_provider_id: str
    status: str                 # NEW, ON_HOLD, IN_PROGRESS_HOT, IN_PROGRESS_COLD, RESERVATION, CONVERTED, UNQUALIFIED, DUPLICATE
    updated_at: datetime | None
    converted_at: datetime | None
    unqualified_at: datetime | None


@dataclass(frozen=True)
class ProviderTask:
    task_id: str
    title: str
    description: str | None
    status: str                 # OPEN, COMPLETED, ARCHIVED
    customer_provider_id: str | None
    unit_id: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class ProviderAuctionEntry:
    """One lease in a lien sale, as auctions/search reports it. auction_time is the facility's local time, unparsed
    beyond the ISO form; turning it into an instant needs the facility's time zone."""

    auction_id: str
    auction_time: str
    lease_provider_id: str
    unit_id: str | None
    lien_start_date: str | None
    status: str                 # LISTED, NO_BIDS, SOLD, PAYMENT_RECEIVED, COMPLETE, CANCELLED


@dataclass(frozen=True)
class TaskCreateOutcome:
    """'created' carries the task id; 'lost' means the task may exist but its id never arrived; 'rejected' means the
    FMS refused the request and made nothing."""

    kind: Literal["created", "lost", "rejected"]
    task_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class InboundText:
    """A text sent to a facility's number, as the FMS reported it. `customer` is the FMS's own match, present only when
    the number belongs to a customer with a lease; `facility_provider_id` is None when the receiving number serves
    several facilities."""

    message_id: str
    org_id: str
    facility_provider_id: str | None
    to: str
    sender: str
    body: str
    received_at: datetime
    customer: ProviderCustomer | None


@dataclass(frozen=True)
class SmsOutcome:
    """What sending a text told us. 'lost' means no usable answer came back: the text may or may not have gone out, and
    Cubby documents no way to look it up afterwards."""

    kind: Literal["sent", "rejected", "lost"]
    detail: str | None = None


@dataclass(frozen=True)
class RentFacts:
    """What the FMS says about one lease's rent, read for the rent change check. Amounts are as the FMS sent them: `rent` and
    `scheduled_amount` per one billing cycle of the facility (Leases Search), `roll_rent` and `roll_effective` per the lease's own cycle (the
    rent roll, joined on the unit). A field the FMS sent in a shape the documentation does not describe is None and named in `unreadable`."""

    lease_provider_id: str
    customer_provider_id: str
    unit_name: str
    facility_cycle: str | None
    lease_cycle: str | None
    rent: int | None
    scheduled_date: str | None
    scheduled_amount: int | None
    in_roll: bool = False
    roll_rent: int | None = None
    roll_effective: int | None = None
    last_change: str | None = None
    unreadable: tuple[str, ...] = ()


class FMSAdapter(Protocol):
    provider: str

    def delinquent_leases(self, facility_provider_id: str) -> list[ProviderLease]: ...
    def lease(self, lease_provider_id: str) -> ProviderLease: ...
    def ledger(self, lease_provider_id: str) -> ProviderLedger: ...
    def customer(self, customer_provider_id: str) -> ProviderCustomer: ...
    def customers_by_phone(self, facility_provider_id: str, phone: str) -> list[ProviderCustomer]: ...
    def has_customer(self, facility_provider_id: str, customer_provider_id: str) -> bool: ...
    def inbound_text(self, payload: dict) -> InboundText: ...
    def send_sms(self, facility_provider_id: str, customer_provider_id: str, body: str) -> SmsOutcome: ...
    def facilities(self) -> list[dict]: ...
    def make_payment(self, lease_provider_id: str, payment_method_id: str, amount: int) -> SubmitOutcome: ...
    def payments_for_lease(self, lease_provider_id: str) -> list[ProviderPayment]: ...
    def take_anomalies(self) -> list[Anomaly]: ...
    def restore_anomalies(self, notes: list[Anomaly]) -> None: ...
    def payment_methods(self, customer_provider_id: str) -> list[dict]: ...
    def leases(self, facility_provider_id: str) -> list[ProviderLease]: ...
    def rent_facts(self, facility_provider_id: str, today: str) -> list[RentFacts]: ...
    def access_codes(self, facility_provider_id: str, customer_provider_id: str) -> list[ProviderAccessCode]: ...
    def check_code(self, facility_provider_id: str, code: str) -> AccessCheck: ...
    def locks_for_lease(self, lease_provider_id: str) -> list[ProviderLock]: ...
    def exemptions_for_customer(self, customer_provider_id: str) -> list[ProviderExemption]: ...
    def tasks_for(self, scope: str, value: str) -> list[ProviderTask]: ...
    def leads_for_customer(self, customer_provider_id: str) -> list[ProviderLead]: ...
    def auction_entries(self, facility_provider_id: str) -> list[ProviderAuctionEntry]: ...
    def create_task(self, facility_provider_id: str, title: str, description: str, *, unit_id: str | None = None,
                    customer_provider_id: str | None = None) -> TaskCreateOutcome: ...
