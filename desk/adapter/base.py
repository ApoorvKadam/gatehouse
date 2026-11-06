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
class ProviderLease:
    provider_id: str
    facility_provider_id: str
    customer_provider_id: str
    unit_name: str
    billing_cycle: str
    total_due: int
    delinquent_since: str | None


@dataclass(frozen=True)
class ProviderLedger:
    lease_provider_id: str
    total_due: int
    prepaid: int
    raw: dict


@dataclass(frozen=True)
class ProviderCustomer:
    provider_id: str
    # None when the API key cannot see personal data
    first_name: str | None
    last_name: str | None
    phone: str | None


@dataclass(frozen=True)
class ProviderPayment:
    provider_id: str
    amount: int
    status: str
    reason: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class SubmitOutcome:
    """What submitting a payment told us. 'lost' means no usable answer came back: the payment may or may not exist."""

    kind: Literal["accepted", "declined", "pending", "rejected", "lost"]
    payment: ProviderPayment | None = None
    detail: str | None = None
    errors: list[str] = field(default_factory=list)


class FMSAdapter(Protocol):
    provider: str

    def delinquent_leases(self, facility_provider_id: str) -> list[ProviderLease]: ...
    def lease(self, lease_provider_id: str) -> ProviderLease: ...
    def ledger(self, lease_provider_id: str) -> ProviderLedger: ...
    def customer(self, customer_provider_id: str) -> ProviderCustomer: ...
    def facilities(self) -> list[dict]: ...
    def make_payment(self, lease_provider_id: str, payment_method_id: str, amount: int) -> SubmitOutcome: ...
    def payments_for_lease(self, lease_provider_id: str) -> list[ProviderPayment]: ...
    def payment_methods(self, customer_provider_id: str) -> list[dict]: ...
