"""Missed calls: each caller is one work item with at most one callback task; a caller is matched only on the whole number; a shared number
or a withheld one goes to a person; nothing creates a lead or a customer from a caller ID; an item closes only when the caller's lead ends in
the FMS after the call. The simulator, its phone system and the desk are real."""
from __future__ import annotations

import httpx
from sqlalchemy import select

from desk.missed_calls import KIND, reconcile_missed_calls
from desk.models import Facility, WorkItem

from .conftest import ORLANDO
from .test_investigator import factory  # noqa: F401


def calls(s, state="open"):
    return {w.subject_id: w for w in s.scalars(select(WorkItem).where(WorkItem.kind == KIND, WorkItem.state == state))}


def orlando(s):
    return s.scalar(select(Facility).where(Facility.provider_id == ORLANDO))


def test_each_caller_is_told_apart_and_matched_only_on_the_whole_number(factory, fms_url):  # noqa: F811
    with factory() as s:
        items = calls(s)
        who = {n: (w.observed or {}).get("who") for n, w in items.items()}
        assert who["+14075550191"] == "lead"            # Jonah, an open lead
        assert who["+14075550192"] == "lead"            # Kim, a lead gone quiet
        assert who["+14075550142"] == "tenant"          # Dana, a current tenant: an existing customer, interest unknown
        assert who["+14075550199"] == "unknown"
        assert who["+14075550193"] == "shared"          # the Lees share one number
        # seven digits inside Dana's number: never searched with, so never matched to her; a person decides
        assert [w for n, w in who.items() if n.startswith("short:")] == ["short"] and not items[next(n for n in who if n.startswith("short:"))].task_id
        assert sum(1 for n in who if n.startswith("withheld:")) == 1 and "withheld" in who.values()
        assert "it may be another unit" in items["+14075550142"].problem          # a tenant's call is never written off as not sales
        assert 'Voicemail: "Hi, it\'s Jonah Reyes' in items["+14075550191"].problem


def test_a_caller_who_rang_twice_is_one_item_with_one_task(factory, adapter, fms_url):  # noqa: F811
    with factory() as s:
        jonah = calls(s)["+14075550191"]
        assert len(jonah.observed["calls"]) == 2 and jonah.task_state == "created"
        task = adapter.tasks_for("taskId", jonah.task_id)[0]
        assert task.title == "Return a missed call" and task.customer_provider_id == "cust_JonahRey01" and "and 1 more since" in task.description
        assert "a missed call is not consent" in task.description
        assert len([t for t in adapter.tasks_for("customerId", "cust_JonahRey01") if t.title == "Return a missed call"]) == 1
        reconcile_missed_calls(s, adapter, orlando(s), "local", fms_url)       # the next pass asks for nothing new
        assert len([t for t in adapter.tasks_for("customerId", "cust_JonahRey01") if t.title == "Return a missed call"]) == 1


def test_a_shared_or_withheld_number_gets_no_task_and_waits_for_a_person(factory):  # noqa: F811
    with factory() as s:
        items = calls(s)
        shared = items["+14075550193"]
        withheld = next(w for n, w in items.items() if n.startswith("withheld:"))
        assert shared.task_state == "none" and "a person decides who called" in shared.problem
        assert withheld.task_state == "none" and "no one to look up" in withheld.problem


def test_an_unknown_caller_gets_a_facility_task_and_no_lead_or_customer(factory, adapter, fms_url):  # noqa: F811
    with factory() as s:
        rosa = calls(s)["+14075550199"]
        task = adapter.tasks_for("taskId", rosa.task_id)[0]
        assert task.customer_provider_id is None and task.unit_id is None and "+14075550199" in task.description
        assert httpx.get(f"{fms_url}/sim/leads/creates", timeout=5).json()["data"]["count"] == 0     # no lead, no customer, from a caller ID


def test_a_completed_task_closes_nothing_and_a_converted_lead_closes_the_call(factory, adapter, sim, fms_url):  # noqa: F811
    with factory() as s:
        jonah = calls(s)["+14075550191"]
        assert sim.post("/sim/tasks/complete", {"taskId": jonah.task_id}).status_code == 200
        reconcile_missed_calls(s, adapter, orlando(s), "local", fms_url)
        s.refresh(jonah)
        assert jonah.state == "open"                                    # a completed task does not prove anyone called back
        assert sim.post("/sim/leads/convert", {"leadId": "lead_JonahRey01"}).status_code == 200
        reconcile_missed_calls(s, adapter, orlando(s), "local", fms_url)
        s.refresh(jonah)
        assert jonah.state == "closed" and jonah.verification.startswith("Converted")


def test_a_lead_ended_before_the_call_does_not_close_it(factory, adapter, sim, fms_url):  # noqa: F811
    with factory() as s:
        # Kim's lead is marked unqualified after her call: the call closes. A new call from her afterwards opens a new item, and her
        # lead's earlier end cannot close that one.
        assert sim.post("/sim/leads/status", {"leadId": "lead_KimSato001", "status": "UNQUALIFIED"}).status_code == 200
        reconcile_missed_calls(s, adapter, orlando(s), "local", fms_url)
        assert calls(s, "closed")["+14075550192"].verification == "Closed in the FMS as unqualified"
        httpx.post(f"{fms_url}/sim/phone/missed-call", json={"facilityId": ORLANDO, "from": "+14075550192"}, timeout=5)
        reconcile_missed_calls(s, adapter, orlando(s), "local", fms_url)
        again = calls(s)["+14075550192"]
        assert again.state == "open" and (again.observed or {}).get("who") == "customer"


def test_another_lead_of_the_same_caller_ending_does_not_close_the_call(factory, adapter, sim, fms_url):  # noqa: F811
    with factory() as s:
        jonah = calls(s)["+14075550191"]
        assert jonah.observed["lead"] == "lead_JonahRey01"
        # a newer open lead for the same customer: the call keeps watching its own lead, and the newer one converting changes nothing
        other = sim.post("/v1/leads/create", {"facilityId": ORLANDO, "customerId": "cust_JonahRey01", "source": "WALK_IN"}).json()["data"]["leadId"]
        reconcile_missed_calls(s, adapter, orlando(s), "local", fms_url)
        s.refresh(jonah)
        assert jonah.observed["lead"] == "lead_JonahRey01"
        assert sim.post("/sim/leads/convert", {"leadId": other}).status_code == 200
        reconcile_missed_calls(s, adapter, orlando(s), "local", fms_url)
        s.refresh(jonah)
        assert jonah.state == "open"                    # only the lead this call was recorded against can end it


def test_the_page_shows_each_caller_and_a_conversion_the_fms_reported(fms_url, sim):
    from fastapi.testclient import TestClient
    from desk.missed_calls import reconcile_missed_calls as again

    from .test_console import visible
    from .test_pages import preview
    with TestClient(preview(fms_url)) as client:
        page = visible(client.get("/calls-and-reminders").text)
        assert "Jonah Reyes" in page and "Unknown caller, number ending 0199" in page and "Caller ID too short to look up" in page
        assert "Done when: A person picks which customer called" in page
        today = visible(client.get("/today").text)
        assert "Jonah Reyes" in today and "Caller ID withheld" not in today and "too short" not in today     # no fact can prove these done
        assert sim.post("/sim/leads/convert", {"leadId": "lead_JonahRey01"}).status_code == 200
        from desk.adapter.cubby import CubbyAdapter

        from .conftest import FULL_KEY
        fresh = CubbyAdapter(fms_url, FULL_KEY, 5.0)
        with client.app.state.session_factory() as s:
            again(s, fresh, s.scalar(select(Facility).where(Facility.provider_id == ORLANDO)), "local", fms_url)
        fresh.close()
        page = visible(client.get("/calls-and-reminders").text)
        assert "Closed on what the FMS reported" in page and "Converted: the FMS shows a lease created for this lead" in page
