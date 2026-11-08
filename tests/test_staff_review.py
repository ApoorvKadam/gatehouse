"""The model may ask for a staff review of a sale that is still listed. It only asks: the desk decides, acts after it has checked the
packet, and reads the FMS task back before anything is shown as done. The model is scripted here; the desk, the FMS simulator and the
page are the real ones."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from desk.investigator import Lookups, act, investigate, run_view, scope_of

from .conftest import ORLANDO
from .test_investigator import (DANA_LEASE, Scripted, dana_facts, dana_packet, factory, message_ids, packet, text_of, tool)  # noqa: F401
from .test_live import A, click, live_app

UNIT = "unit_WhitB1042"


def asking(s, record: str | None = None, final=None) -> Scripted:
    """The lookups, then a request for a staff review of Dana's sale item, then a packet."""
    sale = f"workitem:{dana_facts(s)['sale'].id}"
    return Scripted([tool("get_account", id_="a1"), tool("get_sale_status", {"lease_id": DANA_LEASE}, id_="a2"), tool("get_payments", {"lease_id": DANA_LEASE}, id_="a3")],
                    [tool("request_staff_review", {"record_id": record or sale}, id_="a4")],
                    *(final if final is not None else [[tool("submit_case_packet", dana_packet(s), id_="a5")]]))


def without_a_task(item) -> None:
    """Dana's sale item as it was before the desk asked the FMS for its task."""
    item.task_state, item.task_id, item.task_marker, item.task_status = "none", None, None, None


def tasks(adapter) -> int:
    return len(adapter.tasks_for("unitId", UNIT))


def test_a_review_asked_for_links_the_task_the_desk_already_opened_and_reads_it_back(factory, adapter):
    with factory() as s:
        before = tasks(adapter)
        run, _ = investigate(s, asking(s), text_of(s, "Dana Whitfield"), "request-review-01", adapter=adapter)
        assert run.status == "done"
        [action] = run.actions
        assert action["kind"] == "staff_review" and action["outcome"] == "linked" and action["work_item_id"] == dana_facts(s)["sale"].id
        assert action["task_status"] == "OPEN" and action["read_at"]
        assert tasks(adapter) == before                                       # nothing new was asked for
        shown = run_view(run)["actions"][0]
        assert shown["label"] == "Existing FMS task linked" and shown["tone"] == "done" and "read it back from the FMS" in shown["text"]
        assert "task_" not in str(shown) and "OPEN" not in str(shown)         # no task id and no state code reaches a screen


def test_the_desk_marks_reading_the_task_back_in_the_run_so_a_waiting_page_shows_the_step(factory, adapter):
    from desk.investigator import progress
    with factory() as s:
        dana = text_of(s, "Dana Whitfield")
        run, _ = investigate(s, asking(s), dana, "request-review-trace", adapter=adapter)
        assert run.status == "done" and run.actions[0]["outcome"] == "linked"
        steps = progress(s, dana.id)["steps"]
        assert steps.index("Writing the case notes") < steps.index("Reading the FMS task back")      # after the packet, as it happened
        marked = [turn for turn in run.trace if turn.get("asked") == ["read_task_back"]]
        assert len(marked) == 1 and marked[0]["calls"] == []                                          # marked once, with no model call behind it


def test_the_message_carries_where_the_sale_and_its_outside_listing_stand_each_from_its_own_item(factory, adapter):
    from desk.db import utcnow
    from desk.messaging import inbox
    from desk.models import Facility
    from desk.sales import CANCELLED_BY
    with factory() as s:
        dana = text_of(s, "Dana Whitfield")
        facility = s.get(Facility, dana.facility_id)
        shown = lambda: next(m for m in inbox(s, facility) if m["id"] == dana.id)["outcome"]        # noqa: E731
        assert shown() is None                                                                       # nobody has explored it, so no sale is cited
        investigate(s, asking(s), dana, "request-outcome-01", adapter=adapter)
        outcome = shown()
        sale = dana_facts(s)["sale"]
        assert outcome["sale"]["id"] == sale.id and outcome["sale"]["state"] == "open" and outcome["sale"]["status"] == "LISTED"
        assert outcome["outside"]["kind"] == "sale_outside_listing" and outcome["outside"]["state"] == "open" and outcome["outside"]["marketplace"]
        # the FMS cancelling the sale closes its own item and leaves the outside listing open
        sale.state, sale.verification, sale.closed_at = "closed", CANCELLED_BY, utcnow()
        s.commit()
        outcome = shown()
        assert outcome["sale"]["state"] == "closed" and outcome["sale"]["verification"] == CANCELLED_BY and outcome["outside"]["state"] == "open"


def test_a_sale_item_with_no_task_gets_one_asked_for_once_the_desk_has_confirmed_the_sale(factory, adapter):
    with factory() as s:
        item = dana_facts(s)["sale"]
        without_a_task(item)
        s.commit()
        before = tasks(adapter)
        run, _ = investigate(s, asking(s), text_of(s, "Dana Whitfield"), "request-review-02", adapter=adapter)
        [action] = run.actions
        assert action["outcome"] == "requested" and action["task_status"] == "OPEN"
        assert tasks(adapter) == before + 1
        s.refresh(item)
        assert item.task_state == "created" and item.task_id
        assert run_view(run)["actions"][0]["label"] == "FMS task requested and confirmed"
        # a second run links the task that now exists and makes no second one
        again, _ = investigate(s, asking(s), text_of(s, "Dana Whitfield"), "request-review-03", again=True, adapter=adapter)
        assert again.actions[0]["outcome"] == "linked" and tasks(adapter) == before + 1


def test_no_task_is_asked_for_when_the_desks_own_check_does_not_confirm_the_sale(factory, adapter):
    with factory() as s:
        item = dana_facts(s)["sale"]
        without_a_task(item)
        s.commit()
        before = tasks(adapter)
        # the packet is accepted but it is not a sale claim, so the desk has nothing confirmed to ask a task for
        other = packet("payment_status", [dana_facts(s)["payment"], f"workitem:{item.id}"], False, "", "Dana asks about her payment.")
        run, _ = investigate(s, asking(s, final=[[tool("submit_case_packet", other, id_="a5")]]), text_of(s, "Dana Whitfield"), "request-review-04", adapter=adapter)
        assert run.status == "done" and run.actions[0]["outcome"] == "not_requested"
        assert tasks(adapter) == before
        s.refresh(item)
        assert item.task_state == "none"
        assert run_view(run)["actions"][0]["tone"] == "plain"


def test_a_packet_the_desk_does_not_accept_drops_the_request(factory, adapter):
    with factory() as s:
        item = dana_facts(s)["sale"]
        without_a_task(item)
        s.commit()
        before = tasks(adapter)
        bad = packet("sale_after_payment", ["payment:9999"], True, "x", "Dana says she paid.")
        run, _ = investigate(s, asking(s, final=[[tool("submit_case_packet", bad, id_="a5")], [tool("submit_case_packet", bad, id_="a6")]]),
                             text_of(s, "Dana Whitfield"), "request-review-05", adapter=adapter)
        assert run.status == "needs_person" and run.actions == [] and tasks(adapter) == before


def test_a_record_the_lookups_never_returned_is_refused_and_nothing_is_asked_for(factory, adapter):
    with factory() as s:
        item = dana_facts(s)["sale"]
        without_a_task(item)
        s.commit()
        before = tasks(adapter)
        model = asking(s, record="workitem:9999")
        run, _ = investigate(s, model, text_of(s, "Dana Whitfield"), "request-review-06", adapter=adapter)
        answer = model.calls[2]["messages"][-1]["content"][0]["content"]
        assert "not the record_id of a sale item" in answer
        assert run.status == "done" and run.actions == [] and tasks(adapter) == before


def test_a_sale_item_of_another_tenant_is_refused_even_if_its_id_was_somehow_in_the_run(factory):
    with factory() as s:
        sale = dana_facts(s)["sale"]
        marcus = text_of(s, "Marcus Bell")
        lookups = Lookups(s, scope_of(s, marcus), marcus)
        lookups.facts[f"workitem:{sale.id}"] = "pretend this record came from a lookup"
        answer = lookups.request_staff_review(f"workitem:{sale.id}")
        assert "not open for one of this tenant's units" in answer["error"] and lookups.requests == []


def test_an_answer_that_never_arrives_is_looked_up_by_its_marker_and_the_task_is_never_asked_for_twice(factory, adapter, sim):
    with factory() as s:
        item = dana_facts(s)["sale"]
        without_a_task(item)
        s.commit()
        before = tasks(adapter)
        assert sim.post("/sim/faults", {"kind": "lose_task_response", "facilityId": ORLANDO}).status_code == 200
        run, _ = investigate(s, asking(s), text_of(s, "Dana Whitfield"), "request-review-07", adapter=adapter)
        assert run.actions[0]["outcome"] == "requested"                        # the lookup by the marker found the task the FMS did make
        assert tasks(adapter) == before + 1


def test_the_page_shows_the_staff_review_under_the_desks_check(fms_url, sim):
    model = Scripted()
    with TestClient(live_app(fms_url, model)) as client:
        with client.app.state.session_factory() as s:
            model.turns = asking(s).turns
        assert click(client, message_ids(client)["Dana Whitfield"], "visitor-click-review", A).status_code == 200
        html = client.get(f"/inbox?text={message_ids(client)['Dana Whitfield']}").text
        assert "Existing FMS task linked" in html and "read it back from the FMS" in html
        for code in ("task_", "workitem:", "OPEN"):
            assert code not in html, code


def test_an_item_the_worker_closed_before_the_desk_acted_gets_no_task(factory, adapter):
    with factory() as s:
        item = dana_facts(s)["sale"]
        dana = text_of(s, "Dana Whitfield")
        run, _ = investigate(s, asking(s), dana, "request-review-08", adapter=adapter)     # a finished run whose request named the item
        run.actions = []
        without_a_task(item)
        item.state = "closed"                                                              # the worker closed it after the request was made
        s.commit()
        before = tasks(adapter)
        asked = SimpleNamespace(row=run, lookups=SimpleNamespace(requests=[item.id]), scope=scope_of(s, dana), inbound=dana)
        act(s, adapter, asked)
        assert run.actions[0]["outcome"] == "not_requested" and tasks(adapter) == before


def test_nothing_the_desk_does_after_an_accepted_packet_can_fail_the_run(factory, adapter):
    with factory() as s:
        run, _ = investigate(s, asking(s), text_of(s, "Dana Whitfield"), "request-review-09", adapter=adapter)
        assert run.status == "done"
        act(s, adapter, SimpleNamespace(row=run, lookups=None, scope=None))              # an unexpected error in the action is logged, not raised
        assert run.status == "done"
