"""The evidence a lien file keeps, and what each kind must say.

These are the evidence models the core uses. They began as a copy of the earlier desk's (desk/liens.py), which retires with the old
simulator, and the Florida evaluator moves onto them. A record is {id, kind, facts, document_sha256}. `notice_id` in a record's
facts is the id of the notice record it belongs to, and a notice's `copy_version` is the notice copy it was drafted from. Every kind
but a legal check rests on a document the facility keeps, and the file keeps that document's fingerprint; a legal check names its
source instead."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, StrictInt, model_validator

YesNoUnknown = Literal["yes", "no", "unknown"]


class ItemizedCharge(BaseModel):
    description: str = Field(min_length=2)
    category: Literal["rent", "fee", "tax", "other"] = Field(description="anything but rent needs the agreement's authorization")
    amount_cents: int = Field(gt=0)
    due_on: date


class Notice(BaseModel):
    itemized_charges: list[ItemizedCharge] = Field(min_length=1, description="the itemized statement of the claim, as printed")
    itemized_total_cents: int = Field(gt=0)
    sum_due_since: date
    ledger_snapshot_id: int | None = Field(default=None, description="the desk's FMS ledger snapshot the claim was taken from")
    copy_version: int = Field(ge=1, description="the notice copy of the file (the frozen ledger copy) this notice was drafted from")
    property_description: str = Field(min_length=3)
    description_matches_rental_agreement: YesNoUnknown
    demand_deadline: date
    sale_statement: bool = Field(description="the conspicuous statement that unpaid property will be advertised and sold")
    sale_date: date
    sale_time: str = Field(min_length=3, description="the specified time of the sale, as printed")
    sale_place: str = Field(min_length=3)
    owner_name: str = Field(min_length=2)
    owner_street_address: str = Field(min_length=5)
    owner_phone: str = Field(min_length=7)


class MailDeposit(BaseModel):
    notice_id: StrictInt = Field(gt=0, description="the notice record this mailing carried")
    deposited_on: date
    first_class: bool = Field(description="sent by USPS first-class mail")
    certificate_of_mailing: bool
    postage_prepaid: bool
    address: str = Field(min_length=5)
    address_basis: Literal["rental agreement", "written change of address"]


class InPersonDelivery(BaseModel):
    notice_id: StrictInt = Field(gt=0)
    delivered_on: date
    delivered_by: str = Field(min_length=2)
    delivered_to: str = Field(min_length=2)


class EmailDelivery(BaseModel):
    notice_id: StrictInt = Field(gt=0)
    sent_on: date
    sent_to: str = Field(min_length=3)
    address_basis: Literal["rental agreement", "written change of address"] = Field(
        description="why this is the tenant's last known e-mail address")
    agreement_allows_email: YesNoUnknown = "unknown"
    confirmation: Literal["response", "return receipt", "delivery confirmation"] | None = None
    confirmation_from: str | None = None
    confirmed_on: date | None = None
    delivered_on: date | None = Field(default=None, description="only when the confirmation itself says when it was delivered")

    @model_validator(mode="after")
    def _chronology(self):
        if self.confirmation and self.confirmed_on is None:
            raise ValueError("a confirmation needs the date it was received")
        if self.delivered_on and not self.confirmation:
            raise ValueError("a delivery date comes from a confirmation")
        if self.delivered_on and not (self.sent_on <= self.delivered_on <= (self.confirmed_on or self.delivered_on)):
            raise ValueError("delivery must fall between sending and the confirmation")
        if self.confirmed_on and self.confirmed_on < self.sent_on:
            raise ValueError("a confirmation cannot come before the e-mail was sent")
        return self


class Posting(BaseModel):
    notice_id: StrictInt = Field(gt=0)
    posted_on: date
    location: Literal["facility", "unit"]
    location_detail: str = Field(min_length=3, description="where exactly, e.g. the office door or the unit's door")
    conspicuous: YesNoUnknown


class Publication(BaseModel):
    notice_id: StrictInt = Field(gt=0, description="the notice whose property and sale this advertisement describes")
    newspaper: str = Field(min_length=2)
    general_circulation_in_facility_area: YesNoUnknown
    circulation_source: str = Field(min_length=3, description="how that was established")
    published_on: date
    sale_date: date
    sale_time: str = Field(min_length=3)
    sale_place: str = Field(min_length=3)
    includes_property_description: bool
    includes_facility_address: bool
    includes_tenant_name: bool
    includes_sale_time_place_manner: bool
    proof: Literal["publisher_affidavit", "clipping", "staff_entry"] = Field(
        description="what the document behind this record is; the file's evidence rule counts only the publisher's own proof")
    proof_tenant_name: str | None = Field(default=None, description="the tenant the publisher's proof names, when it names one")
    proof_facility_address: str | None = Field(default=None, description="the facility address the publisher's proof names, when it names one")


class LegalCheck(BaseModel):
    check: Literal["military_disclosure_provision", "military_service", "bankruptcy", "vehicle_or_watercraft", "dispute", "secured_party",
                   "document_of_title_issued", "fees_authorized_by_rental_agreement", "no_newspaper_in_area", "fee_reasonable"]
    answer: YesNoUnknown
    source: str = Field(min_length=3)
    observed_on: date


# every kind but a legal check rests on a document the facility keeps (the notice, the certificate, the confirmation, the
# acknowledgment, the posting photo, the publisher's affidavit); a legal check names its source instead
NEEDS_DOCUMENT = ("notice", "mail_deposit", "in_person_delivery", "email_delivery", "posting", "publication")

EVIDENCE_KINDS: dict[str, type[BaseModel]] = {
    "notice": Notice, "mail_deposit": MailDeposit, "in_person_delivery": InPersonDelivery, "email_delivery": EmailDelivery,
    "posting": Posting, "publication": Publication, "legal_check": LegalCheck,
}


def parse_evidence(kind: str, facts: dict) -> BaseModel:
    """The validated model for a record of this kind, built from its facts.

    Raises ValueError for a kind the file does not keep (callers check the kind first and say which kinds it keeps), and
    pydantic's ValidationError, itself a ValueError, for facts that do not fit the kind, with every field's problem in `errors()`.
    """
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unsupported evidence kind {kind!r}; supported: {', '.join(EVIDENCE_KINDS)}")
    return EVIDENCE_KINDS[kind].model_validate(facts)


def clock_time(text: str):
    """A stated time of day in any common clear form (10:00 AM, 10 am, 14:00), or None when it is not one."""
    for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(" ".join(text.upper().split()), fmt).time()
        except ValueError:
            continue
    return None


def same_time(a: str, b: str) -> bool:
    """Whether two stated times of day are the same time, however each is written; two that cannot be read must match as written."""
    x, y = clock_time(a), clock_time(b)
    return x == y if x and y else " ".join(a.lower().split()) == " ".join(b.lower().split())
