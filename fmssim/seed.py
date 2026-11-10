"""Synthetic operators, facilities, tenants and balances. Every name and number here is made up.

IDs are fixed so a scenario, a test and a screenshot all point at the same account."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from datetime import timedelta

from .store import (
    AccessCode, AccessRule, ApiKey, Base, BookEntry, Customer, Exemption, Facility, Lease, LeaseBehavior, Lock, Organization,
    Payment, PaymentMethod, facility_today, restart_ids, run_access_automation, unit_id_of,
)

NORTHSTAR = "org_Northstar01"
HARBOR = "org_HarborSS001"

KEYS = [
    # key, organization, role, sees personal data
    ("sim_org_northstar_full", NORTHSTAR, "OPERATOR", True),
    ("sim_org_northstar_readonly", NORTHSTAR, "READ_ONLY", True),
    ("sim_org_northstar_nopii", NORTHSTAR, "OPERATOR", False),
    ("sim_org_harbor_full", HARBOR, "OPERATOR", True),
]

# facility id, organization, name, state, time zone, the number tenants text
FACILITIES = [
    ("fac_NSorlando01", NORTHSTAR, "Northstar Storage, Orlando East", "FL", "America/New_York", "+14075550100"),
    ("fac_NSalbany001", NORTHSTAR, "Northstar Storage, Albany Central", "NY", "America/New_York", "+15185550100"),
    ("fac_HBtampa0001", HARBOR, "Harbor Self Storage, Tampa", "FL", "America/New_York", "+18135550100"),
]

# customer id, organization, first, last, phone, email
CUSTOMERS = [
    ("cust_DanaWhit01", NORTHSTAR, "Dana", "Whitfield", "+14075550142", "dana.whitfield@example.com"),
    ("cust_MarcBell01", NORTHSTAR, "Marcus", "Bell", "+14075550187", "mbell@example.com"),
    ("cust_PriyaRmn01", NORTHSTAR, "Priya", "Raman", "+14075550119", "priya.raman@example.com"),
    ("cust_TeoVarga01", NORTHSTAR, "Teodor", "Varga", "+15185550164", "teo.varga@example.com"),
    ("cust_GinaOkfr01", NORTHSTAR, "Gina", "Okafor", "+14075550133", "gokafor@example.com"),
    ("cust_OmarHadd01", NORTHSTAR, "Omar", "Haddad", "+14075550158", "omar.haddad@example.com"),
    ("cust_HBRuizLu01", HARBOR, "Lucia", "Ruiz", "+18135550101", "lucia.ruiz@example.com"),
]

# Omar's rent went unpaid twelve days before the world is built, whatever day that is: nothing is wrong with his account but the balance
RECENT = date.today() - timedelta(days=12)

# lease id, facility, customer, unit, monthly rent (cents), open charges [(category, description, cents, accrues_on)]
LEASES = [
    ("lease_WhitB1042", "fac_NSorlando01", "cust_DanaWhit01", "B-1042", 21500,
     [("RENT", None, 21500, date(2026, 10, 1)), ("FEE", "Late fee", 2500, None)]),
    ("lease_BellC0207", "fac_NSorlando01", "cust_MarcBell01", "C-207", 16000,
     [("RENT", None, 16000, date(2026, 10, 1)), ("FEE", "Late fee", 2500, None)]),
    ("lease_RamnA0012", "fac_NSorlando01", "cust_PriyaRmn01", "A-012", 9500,
     [("RENT", None, 9500, date(2026, 10, 1))]),
    ("lease_RamnA0013", "fac_NSorlando01", "cust_PriyaRmn01", "A-013", 12500,
     [("RENT", None, 12500, date(2026, 10, 1))]),
    ("lease_VargD0310", "fac_NSalbany001", "cust_TeoVarga01", "D-310", 19800,
     [("RENT", None, 19800, date(2026, 9, 1)), ("RENT", None, 19800, date(2026, 10, 1)), ("FEE", "Late fee", 2000, None)]),
    ("lease_OkfrE0505", "fac_NSorlando01", "cust_GinaOkfr01", "E-505", 14000,
     [("RENT", None, 14000, date(2026, 10, 1)), ("FEE", "Late fee", 2500, None)]),
    ("lease_HBRuiz101", "fac_HBtampa0001", "cust_HBRuizLu01", "101", 11000,
     [("RENT", None, 11000, date(2026, 10, 1))]),
    ("lease_HaddF0610", "fac_NSorlando01", "cust_OmarHadd01", "F-610", 15000,
     [("RENT", None, 15000, RECENT)]),
]

PAYMENT_METHODS = [
    ("pm_DanaVisa4242", "cust_DanaWhit01", "4242"),
    ("pm_MarcVisa1881", "cust_MarcBell01", "1881"),
    ("pm_PriyMc5100", "cust_PriyaRmn01", "5100"),
    ("pm_TeoVisa0077", "cust_TeoVarga01", "0077"),
    ("pm_GinaVisa3030", "cust_GinaOkfr01", "3030"),
    ("pm_LuciVisa9090", "cust_HBRuizLu01", "9090"),
    ("pm_OmarVisa7777", "cust_OmarHadd01", "7777"),
]


# the simulated FMS's own lockout automation: days overdue before it disables the gate code and calls for an overlock
ACCESS_RULES = [("fac_NSorlando01", 30), ("fac_NSalbany001", 30), ("fac_HBtampa0001", 30)]
# one gate code per customer at their facility
ACCESS_CODES = [("cust_DanaWhit01", "fac_NSorlando01", "4127"), ("cust_MarcBell01", "fac_NSorlando01", "8830"),
                ("cust_PriyaRmn01", "fac_NSorlando01", "2265"), ("cust_TeoVarga01", "fac_NSalbany001", "7741"),
                ("cust_GinaOkfr01", "fac_NSorlando01", "5196"), ("cust_HBRuizLu01", "fac_HBtampa0001", "3302"),
                ("cust_OmarHadd01", "fac_NSorlando01", "5519")]
# the office's lock stock: serial, combination
LOCKS = {"fac_NSorlando01": [("NSO-OL-101", "4821"), ("NSO-OL-102", "7310"), ("NSO-OL-103", "0954"), ("NSO-OL-104", "6628"),
                             ("NSO-OL-105", "3197"), ("NSO-OL-106", "8402")],
         "fac_NSalbany001": [("NSA-OL-201", "5530"), ("NSA-OL-202", "2716")],
         "fac_HBtampa0001": [("HBT-OL-301", "9041")]}
# Priya agreed to pay A-012's October balance in two parts; staff paused its overlock while she does
EXEMPTIONS = [("lease_RamnA0012", "PAYMENT_ARRANGEMENT", "Paying the October balance in two parts (synthetic)")]


def seed_access(session: Session) -> None:
    """Codes, locks and the overlocks staff already put on, as an office would have them on a normal morning."""
    session.add_all(AccessRule(facility_id=f, lockout_days=d) for f, d in ACCESS_RULES)
    session.add_all(AccessCode(code_id=f"code_{c[5:]}_{f[4:]}", facility_id=f, customer_id=c, code=code)
                    for c, f, code in ACCESS_CODES)
    for facility_id, stock in LOCKS.items():
        org = session.get(Facility, facility_id).org_id
        session.add_all(Lock(lock_id=f"lock_{serial.replace('-', '')}", org_id=org, facility_id=facility_id, serial=serial,
                             combo=combo) for serial, combo in stock)
    session.flush()
    for lease_id, reason, note in EXEMPTIONS:
        lease = session.get(Lease, lease_id)
        session.add(Exemption(exemption_id=f"dex_{lease_id[6:]}", lease_id=lease_id, reason=reason, note=note,
                              end_date=facility_today(session, lease.facility_id) + timedelta(days=30),
                              pause_delinquency_fees=True, pause_overlock=True))
    session.flush()
    for customer_id, facility_id, _ in ACCESS_CODES:
        run_access_automation(session, facility_id, customer_id)
    session.flush()
    # staff have done their rounds: every unit the FMS says needs an overlock has one
    for lease in session.query(Lease).order_by(Lease.lease_id):
        if lease.overlock_needed:
            lock = session.query(Lock).filter(Lock.facility_id == lease.facility_id, Lock.status == "AVAILABLE").order_by(Lock.lock_id).first()
            lock.status, lock.attached_to = "OPERATOR_USE", unit_id_of(lease)


def reset(session: Session, id_seed: str | None = None) -> None:
    restart_ids(id_seed)
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(delete(table))
    session.add_all(Organization(org_id=o, name=n) for o, n in [(NORTHSTAR, "Northstar Storage"), (HARBOR, "Harbor Self Storage")])
    session.add_all(ApiKey(key=k, org_id=o, role=r, pii=p) for k, o, r, p in KEYS)
    session.add_all(Facility(facility_id=f, org_id=o, name=n, state=s, time_zone=tz, sms_number=num) for f, o, n, s, tz, num in FACILITIES)
    session.add_all(Customer(customer_id=c, org_id=o, first_name=fn, last_name=ln, phone=ph, email=em)
                    for c, o, fn, ln, ph, em in CUSTOMERS)
    session.flush()
    for lease_id, facility_id, customer_id, unit, rent, charges in LEASES:
        first_due = min((d for _, _, _, d in charges if d), default=date(2026, 10, 1))
        session.add(Lease(lease_id=lease_id, facility_id=facility_id, customer_id=customer_id, unit_name=unit,
                          move_in_date=date(2025, 4, 6), monthly_rental_amount=rent, paid_through_date=first_due,
                          delinquency_start_date=first_due))
        for i, (category, description, cents, accrues_on) in enumerate(charges):
            session.add(BookEntry(
                book_entry_id=f"be_{lease_id[6:]}{i:02d}", lease_id=lease_id, category=category,
                description=description, amount=cents, entry_time=datetime(2026, 10, 1, 9, 0, i),
                accrues_on=accrues_on, accrues_through=(accrues_on.replace(day=28) if accrues_on else None)))
    session.add_all(PaymentMethod(payment_method_id=pm, customer_id=c, last4=l4) for pm, c, l4 in PAYMENT_METHODS)
    session.flush()
    seed_access(session)
    session.commit()


__all__ = ["reset", "NORTHSTAR", "HARBOR", "Payment", "LeaseBehavior"]
