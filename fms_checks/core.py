"""The vendor-neutral core: what a check reads from an FMS, in words that belong to no vendor.

A unit's access is `open` (the tenant can get in) or `restricted` (anything else, including a state the adapter does not know). A rent
increase is described by when it starts, when its notice is dated, and the rent before and after, in cents. Every reply an adapter
gets is kept as a `Read`, with the vendor's own words in `raw` for the private record only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

OPEN, RESTRICTED = "open", "restricted"


@dataclass(frozen=True)
class Read:
    """One reply from the FMS, as evidence: what was asked, the HTTP status, the FMS's request id, the time, the neutral value, any
    error code, and the vendor's own value for the private record."""

    what: str
    http_status: int
    request_id: str | None
    at: str
    value: object = None
    error: str | None = None
    raw: object = None


class FMS(Protocol):
    """What the checks need from an FMS adapter. Writes carry an idempotency key; a repeated key must not act twice."""

    def unit_access(self, unit_id: str) -> Read: ...                       # value: OPEN or RESTRICTED
    def held_units(self) -> Read: ...                                      # value: ids of units an unlock could lift, or None
    def restrict(self, contact_id: str, key: str) -> Read: ...             # lock every occupied unit of the contact
    def lift(self, contact_id: str, key: str) -> Read: ...                 # lift every lock of the contact
    def open_increases(self, subscription_id: str) -> Read: ...            # value: ids of draft or pending increases, or None
    def draft_increase(self, subscription_id: str, percent: float, key: str) -> Read: ...   # on the operator's default notice
    def schedule_increase(self, increase_id: str, key: str) -> Read: ...   # sends the tenant the notice
    def increase(self, increase_id: str) -> Read: ...                      # value: id, status, starts_on, notice_on, rent_before, rent_after
    def delete_increase(self, increase_id: str) -> Read: ...
