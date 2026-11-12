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
                ("cust_OmarHadd01", "fac_NSorlando01", "5519"),
                # the paid-up tenants with rent changes
                ("cust_LenaPark01", "fac_NSorlando01", "6604"), ("cust_SamOrtiz01", "fac_NSorlando01", "3871"),
                ("cust_MiaChen001", "fac_NSorlando01", "9142"), ("cust_NoorAli001", "fac_NSorlando01", "2058"),
                ("cust_RaviMenon1", "fac_NSorlando01", "7713"), ("cust_AnaCosta01", "fac_NSorlando01", "4480"),
                ("cust_EliBrooks1", "fac_NSorlando01", "1396")]
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


# Rent changes. Six current tenants at Orlando East, each with a rent change the operator scheduled or completed, plus schedules on three
# accounts the other checks already know. Every date counts from the facility's today, so the world reads the same whichever day it is built.
RENT_CUSTOMERS = [
    ("cust_LenaPark01", NORTHSTAR, "Lena", "Park", "+14075550171", "lena.park@example.com"),
    ("cust_SamOrtiz01", NORTHSTAR, "Sam", "Ortiz", "+14075550172", "sam.ortiz@example.com"),
    ("cust_MiaChen001", NORTHSTAR, "Mia", "Chen", "+14075550173", "mia.chen@example.com"),
    ("cust_NoorAli001", NORTHSTAR, "Noor", "Ali", "+14075550174", "noor.ali@example.com"),
    ("cust_RaviMenon1", NORTHSTAR, "Ravi", "Menon", "+14075550175", "ravi.menon@example.com"),
    ("cust_AnaCosta01", NORTHSTAR, "Ana", "Costa", "+14075550176", "ana.costa@example.com"),
    ("cust_EliBrooks1", NORTHSTAR, "Eli", "Brooks", "+14075550177", "eli.brooks@example.com"),
]


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return d.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise ValueError(d)


def next_period(start: date, cycle: str) -> date:
    return start + timedelta(days=28) if cycle == "DAYS_28" else add_months(start, 1)


def rent_tenant(session: Session, lease_id: str, customer_id: str, unit: str, rent: int, periods: list[tuple[date, int]], *, cycle: str = "MONTHLY",
                scheduled: tuple[date, int] | None = None, last_change: date | None = None, discount: int = 0) -> None:
    """A tenant who is paid up: each period's RENT charge billed and paid, a discount billed as its own credit."""
    end = next_period(periods[-1][0], cycle) - timedelta(days=1)
    session.add(Lease(lease_id=lease_id, facility_id="fac_NSorlando01", customer_id=customer_id, unit_name=unit, move_in_date=periods[0][0] - timedelta(days=300),
                      monthly_rental_amount=rent, billing_cycle=cycle, paid_through_date=end, delinquency_start_date=None,
                      rent_change_scheduled_date=scheduled[0] if scheduled else None, rent_change_new_rent_amount=scheduled[1] if scheduled else None,
                      last_rent_change_date=last_change, rent_discount=discount))
    for i, (start, cents) in enumerate(periods):
        when = datetime.combine(start, datetime.min.time()).replace(hour=9)
        through = next_period(start, cycle) - timedelta(days=1)
        session.add(BookEntry(book_entry_id=f"be_r{lease_id[6:]}{i:02d}", lease_id=lease_id, category="RENT", amount=cents, amount_paid=cents,
                              entry_time=when, payment_time=when, accrues_on=start, accrues_through=through))
        if discount:
            session.add(BookEntry(book_entry_id=f"be_d{lease_id[6:]}{i:02d}", lease_id=lease_id, category="DISCOUNTS", description="Loyalty discount",
                                  amount=-discount, amount_paid=-discount, entry_time=when, payment_time=when, accrues_on=start, accrues_through=through))


def seed_rent_changes(session: Session) -> None:
    t = facility_today(session, "fac_NSorlando01")
    session.add_all(Customer(customer_id=c, org_id=o, first_name=fn, last_name=ln, phone=ph, email=em) for c, o, fn, ln, ph, em in RENT_CUSTOMERS)
    session.flush()
    # completed 40 days ago, the first full period at the new rent: verified
    d = t - timedelta(days=40)
    rent_tenant(session, "lease_ParkG0118", "cust_LenaPark01", "G-118", 13500, [(add_months(d, -1), 12500), (d, 13500), (add_months(d, 1), 13500)], last_change=d)
    # completed 35 days ago, but the first full period was billed at the old rent
    d = t - timedelta(days=35)
    rent_tenant(session, "lease_OrtzH0204", "cust_SamOrtiz01", "H-204", 15500, [(add_months(d, -1), 14000), (d, 14000), (add_months(d, 1), 15500)], last_change=d)
    # a 28-day lease at a monthly facility: $300.00 a month is billed as $276.92 every 28 days, which is right
    d = t - timedelta(days=45)
    rent_tenant(session, "lease_ChenL0520", "cust_MiaChen001", "L-520", 30000, [(d - timedelta(days=28), 24923), (d, 27692), (d + timedelta(days=28), 27692)],
                cycle="DAYS_28", last_change=d)
    # scheduled for five days ago and still scheduled; the rent and the bill are unchanged
    d = t - timedelta(days=5)
    rent_tenant(session, "lease_AliM00611", "cust_NoorAli001", "M-611", 11000, [(add_months(d, -1), 11000), (d, 11000)], scheduled=(d, 11800))
    # upcoming: a 7 percent increase on a tenant in good standing
    rent_tenant(session, "lease_MennJ0330", "cust_RaviMenon1", "J-330", 10000, [(add_months(t, -1), 10000), (t, 10000)], scheduled=(t + timedelta(days=21), 10700),
                last_change=t - timedelta(days=400))
    # upcoming: 28 percent, four months after the last increase
    rent_tenant(session, "lease_CostK0412", "cust_AnaCosta01", "K-412", 12500, [(add_months(t, -1), 12500), (t, 12500)], scheduled=(t + timedelta(days=30), 16000),
                last_change=t - timedelta(days=120))
    # completed 50 days ago on a discounted lease: what the bill should be cannot be told from the rent alone
    d = t - timedelta(days=50)
    rent_tenant(session, "lease_BrksP0815", "cust_EliBrooks1", "P-815", 12000, [(add_months(d, -1), 11000), (d, 12000), (add_months(d, 1), 12000)],
                last_change=d, discount=1200)
    # schedules on accounts the other checks already hold: a lien file, a past-due balance, a payment arrangement
    for lease_id, days, amount in (("lease_OkfrE0505", 25, 15400), ("lease_HaddF0610", 18, 15900), ("lease_RamnA0012", 28, 10200)):
        lease = session.get(Lease, lease_id)
        lease.rent_change_scheduled_date, lease.rent_change_new_rent_amount = t + timedelta(days=days), amount
    session.flush()


# people who called or asked about a unit and rent nothing yet (no lease); the Lees share one number
LEAD_CUSTOMERS = [
    ("cust_JonahRey01", NORTHSTAR, "Jonah", "Reyes", "+14075550191", "jonah.reyes@example.com"),
    ("cust_KimSato001", NORTHSTAR, "Kim", "Sato", "+14075550192", "kim.sato@example.com"),
    ("cust_AdaLee0001", NORTHSTAR, "Ada", "Lee", "+14075550193", "ada.lee@example.com"),
    ("cust_BenLee0001", NORTHSTAR, "Ben", "Lee", "+14075550193", "ben.lee@example.com"),
]


def seed_leads(session: Session) -> None:
    """Leads in the FMS, and the morning's missed calls in the phone system's own log, one for each case the desk must tell apart."""
    from .store import Lead, MissedCall, utcnow
    now = utcnow()
    session.add_all(Customer(customer_id=c, org_id=o, first_name=fn, last_name=ln, phone=ph, email=em) for c, o, fn, ln, ph, em in LEAD_CUSTOMERS)
    session.flush()
    session.add_all([
        Lead(lead_id="lead_JonahRey01", facility_id="fac_NSorlando01", customer_id="cust_JonahRey01", source="WEBSITE", origin="CONTACT_FORM",
             status="IN_PROGRESS_HOT", message="Asked for a price on a 10x10.", created_at=now - timedelta(days=3), updated_at=now - timedelta(days=2)),
        Lead(lead_id="lead_KimSato001", facility_id="fac_NSorlando01", customer_id="cust_KimSato001", source="CALL", origin="MANAGER",
             status="IN_PROGRESS_COLD", message="Wants a 5x5 for a few months.", created_at=now - timedelta(days=9), updated_at=now - timedelta(days=9)),
        # two people at one household number, each with an enquiry
        Lead(lead_id="lead_AdaLee0001", facility_id="fac_NSorlando01", customer_id="cust_AdaLee0001", source="WALK_IN", origin="MANAGER",
             status="NEW", message="Looking at climate-controlled units.", created_at=now - timedelta(days=5), updated_at=now - timedelta(days=5)),
        Lead(lead_id="lead_BenLee0001", facility_id="fac_NSorlando01", customer_id="cust_BenLee0001", source="WEBSITE", origin="CONTACT_FORM",
             status="ON_HOLD", message="Asked about vehicle parking.", created_at=now - timedelta(days=12), updated_at=now - timedelta(days=12)),
    ])
    o = "fac_NSorlando01"
    session.add_all([
        # a lead's customer, with a voicemail, and again forty minutes later: one caller, one item, one task
        MissedCall(call_id="call_Jonah0001", facility_id=o, phone="+14075550191", at=now - timedelta(minutes=170), seconds=24,
                   voicemail="Hi, it's Jonah Reyes, calling back about the ten by ten you quoted me. I'd like to move in this Saturday if it's still free."),
        MissedCall(call_id="call_Jonah0002", facility_id=o, phone="+14075550191", at=now - timedelta(minutes=130), seconds=18),
        # a current tenant: an existing customer, and what they want is not known
        MissedCall(call_id="call_Dana00001", facility_id=o, phone="+14075550142", at=now - timedelta(minutes=150), seconds=31),
        # a lead gone quiet calls back
        MissedCall(call_id="call_KimSato01", facility_id=o, phone="+14075550192", at=now - timedelta(minutes=95), seconds=15),
        # nobody the FMS knows, with a voicemail
        MissedCall(call_id="call_Unknown01", facility_id=o, phone="+14075550199", at=now - timedelta(minutes=80), seconds=27,
                   voicemail="Hi, my name's Rosa. I'm looking for a five by ten for about three months starting next week. Can someone call me back?"),
        # one number on two customers: a person decides who called
        MissedCall(call_id="call_Shared001", facility_id=o, phone="+14075550193", at=now - timedelta(minutes=60), seconds=12),
        # caller ID withheld: there is no number to look up
        MissedCall(call_id="call_Withheld1", facility_id=o, phone="", at=now - timedelta(minutes=45), seconds=9),
        # seven digits that sit inside Dana's number: the FMS's partial match finds her, the whole numbers differ
        MissedCall(call_id="call_Partial01", facility_id=o, phone="5550142", at=now - timedelta(minutes=25), seconds=11),
    ])
    session.flush()


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
    seed_rent_changes(session)
    seed_access(session)
    seed_leads(session)
    session.commit()


__all__ = ["reset", "NORTHSTAR", "HARBOR", "Payment", "LeaseBehavior"]
