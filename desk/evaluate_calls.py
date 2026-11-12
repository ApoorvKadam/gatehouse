"""Missed calls, in the evaluation. One pass of the missed-call check runs against the simulator's seeded calls; then staff mark every callback
task done without calling anyone, and the check runs again; then Jonah's lead converts in the FMS, and it runs once more. The judge reads the
items, the FMS tasks and the simulator's count of leads/create requests. Five wrong builds run the same passes with one part replaced. It runs
inside desk/evaluate.py, on the same simulator."""
from __future__ import annotations

import httpx
from sqlalchemy import select

from . import missed_calls
from .adapter.cubby import CubbyAdapter
from .cases import sync_facility
from .db import make_session_factory
from .evaluate_texts import broken
from .messaging import e164
from .missed_calls import KIND, TASK_TITLE
from .models import Facility, WorkItem

KEY = "sim_org_northstar_full"
ORLANDO = "fac_NSorlando01"
MATCHED_WRONG = "a caller matched to a customer on part of a number or on a shared number"
CREATED = "a lead or customer created from a caller ID"
SECOND_TASK = "a second callback task for one caller"
TASK_CLOSED = "a missed call closed because its callback task was marked done"
CONVERTED = "a caller counted as converted without their lead converted in the FMS"
LINES = (MATCHED_WRONG, CREATED, SECOND_TASK, TASK_CLOSED, CONVERTED)
PARTIAL, SHARED, JONAH, DANA = "+5550142", "+14075550193", "+14075550191", "+14075550142"


def one_pass(url: str) -> dict:
    sim = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {KEY}"}, timeout=15)
    sim.post("/sim/reset")
    session = make_session_factory("sqlite:///:memory:")()
    adapter = CubbyAdapter(url, KEY, timeout_seconds=5.0)
    sync_facility(session, adapter, ORLANDO)
    facility = session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
    missed_calls.reconcile_missed_calls(session, adapter, facility, "eval", url)   # through the module, so a wrong build replaces it
    first = {w.subject_id: dict(w.observed or {}) for w in session.scalars(select(WorkItem).where(WorkItem.kind == KIND)).all()}
    # staff mark every callback task done in the FMS app without calling anyone back, and the check reads again
    for w in session.scalars(select(WorkItem).where(WorkItem.kind == KIND, WorkItem.task_id.is_not(None))).all():
        sim.post("/sim/tasks/complete", json={"taskId": w.task_id}).raise_for_status()
    missed_calls.reconcile_missed_calls(session, adapter, facility, "eval", url)   # through the module, so a wrong build replaces it
    closed_on_task = [w.subject_id for w in session.scalars(select(WorkItem).where(WorkItem.kind == KIND, WorkItem.state == "closed")).all()]
    # Jonah rents the unit: a lease is created for his lead, and the check reads once more
    sim.post("/sim/leads/convert", json={"leadId": "lead_JonahRey01"}).raise_for_status()
    missed_calls.reconcile_missed_calls(session, adapter, facility, "eval", url)   # through the module, so a wrong build replaces it
    items = session.scalars(select(WorkItem).where(WorkItem.kind == KIND)).all()
    tasks = [t for t in adapter.tasks_for("facilityId", ORLANDO) if t.title == TASK_TITLE]
    leads = {x.provider_id: x.status for c in ("cust_JonahRey01", "cust_KimSato001") for x in adapter.leads_for_customer(c)}
    creates = sim.get("/sim/leads/creates").json()["data"]["count"]
    sim.close()
    adapter.close()
    return {"first": first, "closed_on_task": closed_on_task, "creates": creates, "leads": leads,
            "items": [{"number": w.subject_id, "state": w.state, "verification": w.verification, "who": (w.observed or {}).get("who"),
                       "lead": (w.observed or {}).get("lead"), "customer": (w.observed or {}).get("customer"), "task": w.task_id,
                       "calls": len((w.observed or {}).get("calls", []))} for w in items],
            "tasks": [{"customer": t.customer_provider_id, "description": t.description or ""} for t in tasks]}


def judge(r: dict) -> dict[str, tuple[int, int]]:
    """(found, checked) for each red line."""
    callers = {e164(n) for n in r["first"]}
    # the short caller ID is kept under its call in a right build; a wrong one that searches with it files it under the number itself
    suspects = [PARTIAL, SHARED] + [n for n in r["first"] if n.startswith("short:")]
    wrong = sum(1 for n in suspects if (r["first"].get(n) or {}).get("customer"))
    per_caller: dict[str, int] = {}
    for t in r["tasks"]:
        key = t["customer"] or next((n for n in callers if n in t["description"]), t["description"])
        per_caller[key] = per_caller.get(key, 0) + 1
    converted = [i for i in r["items"] if (i["verification"] or "").startswith("Converted")]
    unproven = sum(1 for i in converted if r["leads"].get(i["lead"]) != "CONVERTED")
    return {MATCHED_WRONG: (wrong, 2),
            CREATED: (r["creates"], len(callers)),
            SECOND_TASK: (sum(n - 1 for n in per_caller.values() if n > 1), max(len(per_caller), 1)),
            TASK_CLOSED: (len(r["closed_on_task"]), max(sum(1 for i in r["items"] if i["task"]), 1)),
            CONVERTED: (unproven, max(len(converted), 1))}


# ------------------------------------------------------------------ the wrong builds
def first_result(adapter, facility, number, tenants, watch=None):
    """The wrong build: the first customer the FMS's partial search returns is taken as the caller."""
    rows = adapter.customers_by_phone(facility.provider_id, number) if not number.startswith("withheld:") else []
    if not rows:
        return missed_calls.Who("unknown" if not number.startswith("withheld:") else "withheld")
    c = rows[0]
    return missed_calls.Who("tenant" if c.provider_id in tenants else "customer", c.first_name, c.provider_id)


def lead_from_caller_id(original):
    """The wrong build: an unknown caller becomes a lead, and so a customer, made from nothing but the number."""
    def identify(adapter, facility, number, tenants, watch=None):
        who = original(adapter, facility, number, tenants, watch)
        if who.kind == "unknown":
            adapter._http.post("/v1/leads/create", json={"facilityId": facility.provider_id, "source": "CALL",
                                                         "customer": {"name": "Unknown caller", "contact": {"phone": number}}})
        return who
    return identify


def item_per_call(call):
    """The wrong build: each call is its own item, so a caller who rang twice is asked about twice."""
    return call["callId"]


def task_done_closes(original):
    """The wrong build: a missed call closes as soon as its callback task reads completed."""
    def reconcile(session, adapter, facility, run_id="local", feed_url=None):
        result = original(session, adapter, facility, run_id, feed_url)
        for item in session.scalars(select(WorkItem).where(WorkItem.kind == KIND, WorkItem.state == "open", WorkItem.task_status == "COMPLETED")).all():
            item.state, item.verification = "closed", "Closed: the callback task is done"
        session.commit()
        return result
    return reconcile


def lease_as_conversion(original):
    """The wrong build: a caller who holds any lease is counted as a converted lead."""
    def reconcile(session, adapter, facility, run_id="local", feed_url=None):
        result = original(session, adapter, facility, run_id, feed_url)
        for item in session.scalars(select(WorkItem).where(WorkItem.kind == KIND, WorkItem.state == "open")).all():
            if (item.observed or {}).get("who") == "tenant":
                item.state, item.verification = "closed", "Converted: the caller has a lease"
        session.commit()
        return result
    return reconcile


# ------------------------------------------------------------------ the part
def calls_part(url: str):
    """The scenarios, the red lines and the wrong builds of this part, as plain tuples for desk/evaluate.py."""
    r = one_pass(url)
    found = judge(r)
    first = r["first"]
    by = {i["number"]: i for i in r["items"]}
    who = lambda n: (first.get(n) or {}).get("who")
    jonah_tasks = sum(1 for t in r["tasks"] if t["customer"] == "cust_JonahRey01")
    nobody = "Nobody: nothing is asked of anyone"
    table = [
        ("A lead calls twice", "twice", "two missed calls from the number on an open lead, one with a voicemail",
         "one item and one callback task, linked to the lead", "Front desk: call back", who(JONAH) == "lead" and by[JONAH]["calls"] == 2 and jonah_tasks == 1),
        ("A current tenant calls", "normal", "a missed call from a tenant's number", "an existing customer, wants unknown; a callback task",
         "Front desk: call back", who(DANA) == "tenant" and by[DANA]["task"] is not None),
        ("An unknown number leaves a voicemail", "disagree", "the FMS has no customer with the number",
         "a facility callback task; no lead or customer created", "Front desk: call back", who("+14075550199") == "unknown" and r["creates"] == 0),
        ("One number on two customers", "disagree", "the FMS finds two customers with the number", "no task: a person decides who called",
         "A person picks the caller", who(SHARED) == "shared" and by[SHARED]["task"] is None),
        ("Caller ID withheld", "normal", "a missed call with no number", "nothing searched, no task", nobody,
         any(n.startswith("withheld:") and i["task"] is None for n, i in by.items())),
        ("Seven digits inside a tenant's number", "disagree", "a caller ID the FMS's partial search would match to the tenant",
         "searches with nothing under ten digits: a person decides who called", "A person picks the caller",
         any(n.startswith("short:") and (first[n] or {}).get("who") == "short" and not (first[n] or {}).get("customer") for n in first)),
        ("The callback task is marked done", "disagree", "staff complete every callback task without calling", "every missed call stays open",
         "Front desk: still to call back", not r["closed_on_task"]),
        ("The lead converts", "normal", "a lease is created for Jonah's lead", "his missed call closes as converted, on the FMS's word", nobody,
         by[JONAH]["state"] == "closed" and (by[JONAH]["verification"] or "").startswith("Converted")),
    ]
    lines = [(line, found[line][0], found[line][1], {MATCHED_WRONG: "callers", CREATED: "callers", SECOND_TASK: "callers",
                                                    TASK_CLOSED: "tasks", CONVERTED: "conversions"}[line]) for line in LINES]

    def caught(target, name, replacement, line) -> bool:
        with broken(target, name, replacement):
            return judge(one_pass(url))[line][0] > 0

    def searches_any_caller_id() -> bool:
        with broken(missed_calls, "usable", lambda phone: bool(phone)), broken(missed_calls, "identify", first_result):
            return judge(one_pass(url))[MATCHED_WRONG][0] > 0

    controls = [
        ("A build that searches with any caller ID and takes the first result", "First result", "disagree",
         "matches seven digits to the tenant whose number contains them", searches_any_caller_id()),
        ("A build that makes a lead from an unknown caller's number", "Lead from caller ID", "disagree",
         "creates a lead and a customer from nothing but a caller ID", caught(missed_calls, "identify", lead_from_caller_id(missed_calls.identify), CREATED)),
        ("A build that opens an item for every call", "Item per call", "twice",
         "asks for a second callback for the caller who rang twice", caught(missed_calls, "caller_key", item_per_call, SECOND_TASK)),
        ("A build that closes a missed call when its task is marked done", "Callback done as proof", "disagree",
         "closes calls nobody returned", caught(missed_calls, "reconcile_missed_calls", task_done_closes(missed_calls.reconcile_missed_calls), TASK_CLOSED)),
        ("A build that counts any lease as a conversion", "Lease as conversion", "disagree",
         "counts a tenant's call as a converted lead", caught(missed_calls, "reconcile_missed_calls", lease_as_conversion(missed_calls.reconcile_missed_calls), CONVERTED)),
    ]
    return table, lines, controls
