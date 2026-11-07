"""The synthetic world the hosted preview opens on.

Every item in it is produced by running Gatehouse's own flows against the simulator (identity check, read-back,
the tenant's yes, the FMS misbehaving, reconciliation, lien evidence), never by writing finished rows, so what a
visitor sees is what the code actually did. It runs once, on a database that holds no leases yet, and before
the worker starts, so the worker never sees a half-built world. When it is done no payment is pending or waiting
for the ledger, so the worker's FMS reads cannot move anything along. All names, amounts and documents are made up."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .access import reconcile_access
from .adapter import FMSAdapter
from .cases import current_case, sync_facility
from .lien_files import evaluate_case, record_evidence
from .models import AccessPolicy, DelinquencyCase, Facility, Lease, LedgerSnapshot
from .payments import IdentityClaim, PaymentService, check_identity, confirm, propose

log = logging.getLogger("desk.demo")

ORLANDO, ALBANY = "fac_NSorlando01", "fac_NSalbany001"
DESK = "front desk (synthetic)"
# the simulator hands out the same ids after every reset with this seed, so every boot names the same payments
ID_SEED = "gatehouse-preview"


def build(factory: sessionmaker[Session], adapter: FMSAdapter, fms_base_url: str, run_id: str = "local") -> bool:
    """Build the world if this database is empty. Returns False when there was already something to show."""
    with factory() as session:
        if session.scalar(select(Lease.id).limit(1)) is not None:
            return False
    sim = httpx.Client(base_url=fms_base_url, timeout=30)
    sim.post("/sim/reset", json={"idSeed": ID_SEED}).raise_for_status()
    with factory() as session:
        sync_facility(session, adapter, ORLANDO)
        sync_facility(session, adapter, ALBANY)
        lost_response(session, adapter, sim)
        returned_payment(session, adapter, sim)
        changed_lien_file(session, adapter, sim)
        new_york_file(session, adapter)
        # one more pass, as the worker would make, so everything the FMS shows has been observed before the
        # world is opened: the worker then finds nothing new to record
        settle(session, adapter, passes=2)
        access_world(session, adapter, sim, run_id)
    sim.close()
    log.info("synthetic world built")
    return True


def trace(fms_base_url: str) -> dict:
    """The simulator's own record of the seeded payments, saved once the world is built. A real FMS has nothing
    like it; the preview shows it next to what Gatehouse concluded, so a reader can check the two agree."""
    recorded_at = datetime.now(ZoneInfo("America/New_York")).strftime("%b %-d, %-I:%M %p")
    out = {}
    with httpx.Client(base_url=fms_base_url, timeout=30) as sim:
        for lease in ("lease_WhitB1042", "lease_BellC0207", "lease_OkfrE0505"):
            data = sim.get("/sim/truth", params={"leaseId": lease}).raise_for_status().json()["data"]
            out[lease] = {**data, "recorded_at": recorded_at}
    return out


def case_of(session: Session, lease_provider_id: str) -> DelinquencyCase:
    lease = session.scalar(select(Lease).where(Lease.provider_id == lease_provider_id))
    return current_case(session, lease)


def pay_by_phone(session: Session, adapter: FMSAdapter, case: DelinquencyCase, claim: IdentityClaim, amount: int):
    """What the front desk does on a call: check who is calling, read the amount back, take the yes."""
    check = check_identity(session, adapter, case, claim)
    proposal = propose(session, adapter, case, check.id, amount)
    return confirm(session, adapter, case.id, proposal.id, DESK, "Yes, go ahead")


def settle(session: Session, adapter: FMSAdapter, passes: int = 4) -> None:
    for _ in range(passes):
        PaymentService(session, adapter).reconcile_all()


def settle_until(session: Session, adapter: FMSAdapter, command, done, passes: int = 10) -> None:
    """Reconcile, as the worker would, until the payment reaches the state the story needs. A slow FMS read only
    costs another pass; a story that never gets there stops the seed instead of opening a different world."""
    for _ in range(passes):
        PaymentService(session, adapter).reconcile_all()
        session.refresh(command)
        if done(command):
            return
    raise RuntimeError(f"seed: payment {command.id} stopped at {command.state}")


def lost_response(session: Session, adapter: FMSAdapter, sim: httpx.Client) -> None:
    """Dana pays her $240 balance by phone. The FMS takes the money and applies it, and the connection breaks before
    its answer arrives:
    the desk does not retry, does not tell her it went through, and holds the account for a person."""
    sim.post("/sim/behaviors", json={"leaseId": "lease_WhitB1042", "mode": "drop_after_charge"}).raise_for_status()
    case = case_of(session, "lease_WhitB1042")
    command = pay_by_phone(session, adapter, case, IdentityClaim("Whitfield", "0142", "B-1042"), 24000)
    settle_until(session, adapter, command, lambda c: c.state == "NEEDS_STAFF" and c.suggested_payment_id is not None)


def returned_payment(session: Session, adapter: FMSAdapter, sim: httpx.Client) -> None:
    """Marcus pays $185 and it posts, so staff take the overlock off C-207. Days later the bank returns the payment;
    the account reopens, and the unit is past the facility's rule again with no lock on it."""
    case = case_of(session, "lease_BellC0207")
    command = pay_by_phone(session, adapter, case, IdentityClaim("Bell", "0187", "C-207"), 18500)
    settle_until(session, adapter, command, lambda c: c.state == "CONFIRMED")     # it posts before anything else happens
    for lock in adapter.locks_for_lease("lease_BellC0207"):
        sim.post("/sim/locks/return", json={"lockId": lock.lock_id}).raise_for_status()
    sim.post("/sim/payments/return", json={"paymentId": command.provider_payment_id}).raise_for_status()
    settle_until(session, adapter, command, lambda c: c.state == "RETURNED")


def changed_lien_file(session: Session, adapter: FMSAdapter, sim: httpx.Client) -> None:
    """Gina's Florida file is complete and her sale is two days out. Then she pays $50 at the counter, so the
    file built on the old balance goes back to review instead of on to the sale."""
    case = case_of(session, "lease_OkfrE0505")
    today = datetime.now(ZoneInfo("America/New_York")).date()
    delivered = today - timedelta(days=28)
    deadline = delivered + timedelta(days=14)
    first_ad = deadline + timedelta(days=1)
    sale = first_ad + timedelta(days=15)
    notice = record_evidence(session, case, "notice", {
        "itemized_charges": [{"description": "Rent", "category": "rent", "amount_cents": 14000, "due_on": "2026-10-01"},
                             {"description": "Late fee", "category": "fee", "amount_cents": 2500, "due_on": "2026-10-06"}],
        "itemized_total_cents": 16500, "sum_due_since": "2026-10-01",
        "ledger_snapshot_id": snapshot_before(session, case, delivered),
        "property_description": "furniture, boxes and a bicycle", "description_matches_rental_agreement": "yes",
        "demand_deadline": deadline.isoformat(), "sale_statement": True, "sale_date": sale.isoformat(), "sale_time": "10:00 AM",
        "sale_place": "online auction", "owner_name": "Northstar Storage",
        "owner_street_address": "1200 E Colonial Dr, Orlando FL", "owner_phone": "407-555-0100",
    }, DESK, "notice-E-505.txt", "Notice of lien sale for unit E-505, as mailed and posted.")
    record_evidence(session, case, "mail_deposit", {
        "notice_id": notice.id, "deposited_on": delivered.isoformat(), "first_class": True, "certificate_of_mailing": True,
        "postage_prepaid": True, "address": "41 Lake Ave, Orlando FL", "address_basis": "rental agreement",
    }, DESK, "certificate-of-mailing-E-505.txt", "USPS certificate of mailing, first class, postage prepaid.")
    record_evidence(session, case, "posting", {
        "notice_id": notice.id, "posted_on": delivered.isoformat(), "location": "unit", "location_detail": "unit door",
        "conspicuous": "yes",
    }, DESK, "posting-photo-E-505.txt", "Photo of the notice taped at eye level on the door of E-505.")
    for on in (first_ad, first_ad + timedelta(days=7)):
        record_evidence(session, case, "publication", {
            "notice_id": notice.id, "newspaper": "Orlando Sentinel", "general_circulation_in_facility_area": "yes",
            "circulation_source": "Florida Press Association listing", "published_on": on.isoformat(),
            "sale_date": sale.isoformat(), "sale_time": "10:00 AM", "sale_place": "online auction",
            "includes_property_description": True, "includes_facility_address": True,
            "includes_tenant_name": True, "includes_sale_time_place_manner": True,
        }, DESK, f"affidavit-of-publication-{on.isoformat()}.txt", "Affidavit of publication from the Orlando Sentinel.")
    for check, answer in (("military_disclosure_provision", "yes"), ("military_service", "no"), ("bankruptcy", "no"),
                          ("vehicle_or_watercraft", "no"), ("dispute", "no"), ("secured_party", "no"), ("document_of_title_issued", "no"),
                          ("fees_authorized_by_rental_agreement", "yes"), ("no_newspaper_in_area", "no")):
        record_evidence(session, case, "legal_check", {"check": check, "answer": answer, "observed_on": delivered.isoformat(),
                                                      "source": "rental agreement, PACER, UCC search"}, DESK)
    session.commit()
    evaluate_case(session, adapter, case)          # the file as it stood: complete, for a person to review
    sim.post("/sim/payments/offline", json={"allocations": {"lease_OkfrE0505": 5000}}).raise_for_status()
    sync_facility(session, adapter, ORLANDO)
    evaluate_case(session, adapter, case)          # the same file after the payment: back to review
    session.commit()


def snapshot_before(session: Session, case: DelinquencyCase, delivered: date) -> int:
    """The ledger as the worker would have recorded it the afternoon before the notice went out. The preview
    starts after that day, so this one reading is written with the date it stands for."""
    current = session.get(LedgerSnapshot, case.last_snapshot_id)
    old = LedgerSnapshot(lease_id=current.lease_id, total_due=current.total_due, prepaid=current.prepaid, raw=current.raw,
                         observed_at=datetime.combine(delivered - timedelta(days=1), datetime.min.time()) + timedelta(hours=19))
    session.add(old)
    session.flush()
    return old.id


def new_york_file(session: Session, adapter: FMSAdapter) -> None:
    """Teo's unit is in Albany. New York has no reviewed rule version here, so the file goes to counsel."""
    evaluate_case(session, adapter, case_of(session, "lease_VargD0310"))
    session.commit()


# Northstar's lockout rule as its operations manual states it (synthetic). Albany has none recorded, on purpose: a
# facility without a stated rule gets a review item, not a verdict.
ORLANDO_RULE = dict(lockout_days=30, gate_rule="deny_if_any_unit_restricted", version="ORL-access-1",
                    source="Northstar operations manual, section 4.2 (synthetic)", approved_by="regional manager (synthetic)")


def access_world(session: Session, adapter: FMSAdapter, sim: httpx.Client, run_id: str) -> None:
    """Access after the stories above, read the way the worker reads it:
    - B-1042: Dana's money reached the ledger, so the FMS turned her gate code back on, but the overlock staff put on
      in October is still recorded on her unit. Gatehouse opens an item and one FMS task; staff mark the task done
      without taking the lock off, and the item stays open, because the FMS still records the lock.
    - C-207: staff took the overlock off when Marcus paid; the payment came back, and the unit is past the rule again
      with no lock. Gatehouse flags it for a person and does not lock anyone out.
    - A-012 and A-013: Priya's payment arrangement pauses A-012's overlock; A-013 is past the rule and locked, and her
      shared gate code is off. The FMS matches the rule, so there is nothing to do.
    - Albany: no rule recorded, so one review item."""
    orlando = session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
    session.add(AccessPolicy(facility_id=orlando.id, effective_on=date(2026, 1, 1), **ORLANDO_RULE))
    session.commit()
    facilities = session.scalars(select(Facility).where(Facility.provider_id.in_((ORLANDO, ALBANY))).order_by(Facility.id)).all()
    for facility in facilities:
        reconcile_access(session, adapter, facility, run_id)
    for task in adapter.tasks_for("unitId", "unit_WhitB1042"):
        sim.post("/sim/tasks/complete", json={"taskId": task.task_id}).raise_for_status()
    for facility in facilities:
        reconcile_access(session, adapter, facility, run_id)
