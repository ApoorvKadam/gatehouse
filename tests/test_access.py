"""Access and overlocks against the simulator: the four red lines from the plan, plus the cases a gate vendor or an
FMS integrations team would try first (mixed leases, exemptions, lost task answers, failed reads, a task marked done)."""
from __future__ import annotations

from datetime import date

import httpx
import pytest
from sqlalchemy import select

from desk import access
from desk.access import MISSING, NOT_RESTORED, REVIEW, reconcile_access
from desk.adapter.base import FMSError, TaskCreateOutcome
from desk.adapter.cubby import CubbyAdapter
from desk.cases import sync_facility
from desk.models import AccessPolicy, Facility, WorkItem

from .conftest import FULL_KEY, ORLANDO

RUN = "test"


def orlando(session, adapter, gate_rule="deny_if_any_unit_restricted", days=30, policy=True) -> Facility:
    sync_facility(session, adapter, ORLANDO)
    facility = session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
    if policy:
        session.add(AccessPolicy(facility_id=facility.id, lockout_days=days, gate_rule=gate_rule, source="test manual",
                                 approved_by="test", effective_on=date(2026, 1, 1), version="t1"))
        session.commit()
    return facility


def open_items(session, kind=None):
    q = select(WorkItem).where(WorkItem.state == "open")
    return session.scalars(q.where(WorkItem.kind == kind) if kind else q).all()


def access_truth(fms_url):
    return httpx.get(f"{fms_url}/sim/access/truth", params={"facilityId": ORLANDO}).json()["data"]


def locks_on(fms_url, lease_id):
    unit = "unit_" + lease_id.removeprefix("lease_")
    return [k for l in access_truth(fms_url)["leases"] if l["unitId"] == unit for k in l["locks"]]


def pay_in_full(sim, lease_id, cents):
    assert sim.post("/sim/payments/offline", {"allocations": {lease_id: cents}}).status_code == 200


def tasks_for_unit(fms_url, lease_id):
    unit = "unit_" + lease_id.removeprefix("lease_")
    return [t for t in access_truth(fms_url)["tasks"] if t["unitId"] == unit]


def assert_no_red_lines(session, adapter):
    """The plan's access red lines, checked against the FMS itself after a pass:
    a closed lock item while the lock is still attached; a physical outcome claimed; a mismatch with no open item."""
    for w in session.scalars(select(WorkItem).where(WorkItem.state == "closed", WorkItem.kind == NOT_RESTORED,
                                                    WorkItem.subject_type == "lease")):
        assert not [k for k in adapter.locks_for_lease(w.subject_id) if k.status == "OPERATOR_USE"], \
            f"item {w.id} closed while the FMS still records an operator lock"
    for w in session.scalars(select(WorkItem)):
        assert "verified" not in (w.verification or "").lower() or "not verified" in (w.verification or "").lower()
    # every unit that is current but still locked has an open item
    for pl in adapter.leases(ORLANDO):
        locked = [k for k in adapter.locks_for_lease(pl.provider_id) if k.status == "OPERATOR_USE"]
        if locked and pl.delinquent_since is None:
            assert any(w.subject_id == pl.provider_id and w.kind == NOT_RESTORED for w in open_items(session)), pl.unit_name


def test_the_seeded_facility_matches_its_rule_and_opens_nothing(session, adapter, sim):
    facility = orlando(session, adapter)
    result = reconcile_access(session, adapter, facility, RUN)
    assert result.failures == [] and open_items(session) == []


def test_a_paid_tenant_still_locked_gets_one_item_and_one_task_however_often_it_runs(session, adapter, sim, fms_url):
    pay_in_full(sim, "lease_WhitB1042", 24000)
    facility = orlando(session, adapter)
    for _ in range(3):
        reconcile_access(session, adapter, facility, RUN)
    items = open_items(session)
    assert [(w.kind, w.subject_id) for w in items] == [(NOT_RESTORED, "lease_WhitB1042")]
    assert items[0].task_state == "created" and len(tasks_for_unit(fms_url, "lease_WhitB1042")) == 1
    task = tasks_for_unit(fms_url, "lease_WhitB1042")[0]
    assert items[0].task_marker in task["description"] and "If it was put on for non-payment" in task["description"]
    assert "not why" in items[0].problem and "Physical outcome not verified" in access.PHYSICAL
    assert_no_red_lines(session, adapter)


def test_a_task_marked_done_does_not_close_the_item_the_fms_reading_does(session, adapter, sim, fms_url):
    pay_in_full(sim, "lease_WhitB1042", 24000)
    facility = orlando(session, adapter)
    reconcile_access(session, adapter, facility, RUN)
    task = tasks_for_unit(fms_url, "lease_WhitB1042")[0]
    sim.post("/sim/tasks/complete", {"taskId": task["taskId"]})
    reconcile_access(session, adapter, facility, RUN)
    (item,) = open_items(session)
    assert item.task_status == "COMPLETED" and "does not close this item" in item.note
    assert_no_red_lines(session, adapter)
    # staff take the lock off and record it: the next complete reading closes the item
    lock = locks_on(fms_url, "lease_WhitB1042")[0]
    assert sim.post("/sim/locks/return", {"lockId": lock["lockId"]}).status_code == 200
    reconcile_access(session, adapter, facility, RUN)
    assert open_items(session) == []
    closed = session.scalar(select(WorkItem).where(WorkItem.state == "closed"))
    assert closed.verification == access.CLOSED_BY and "Physical outcome not verified" in closed.note
    assert_no_red_lines(session, adapter)


def test_negative_control_a_build_that_trusts_task_completion_breaks_a_red_line(session, adapter, sim, fms_url, monkeypatch):
    """If closing followed the task instead of the FMS reading, the red-line check must catch it."""
    pay_in_full(sim, "lease_WhitB1042", 24000)
    facility = orlando(session, adapter)
    reconcile_access(session, adapter, facility, RUN)
    sim.post("/sim/tasks/complete", {"taskId": tasks_for_unit(fms_url, "lease_WhitB1042")[0]["taskId"]})
    reconcile_access(session, adapter, facility, RUN)
    item = open_items(session)[0]
    item.state, item.verification = "closed", access.CLOSED_BY   # what the wrong build would do
    session.commit()
    with pytest.raises(AssertionError, match="closed while the FMS still records an operator lock"):
        assert_no_red_lines(session, adapter)


def test_a_restriction_that_is_missing_is_flagged_and_nobody_is_locked_out(session, adapter, sim, fms_url):
    # Marcus pays, staff take the overlock off, then the payment comes back: past the rule again with no lock
    paid = sim.post("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}}).json()["data"]["payment"]
    sim.post("/sim/locks/return", {"lockId": locks_on(fms_url, "lease_BellC0207")[0]["lockId"]})
    sim.post("/sim/payments/return", {"paymentId": paid["paymentId"]})
    facility = orlando(session, adapter)
    before = access_truth(fms_url)
    reconcile_access(session, adapter, facility, RUN)
    after = access_truth(fms_url)
    (item,) = open_items(session)
    assert (item.kind, item.subject_id) == (MISSING, "lease_BellC0207") and "does not lock anyone out" in item.problem
    # Gatehouse asked for a person and changed nothing about access
    assert after["codes"] == before["codes"] and [l["locks"] for l in after["leases"]] == [l["locks"] for l in before["leases"]]
    assert len(tasks_for_unit(fms_url, "lease_BellC0207")) == 1


def test_an_exemption_covers_its_own_lease_only(session, adapter, sim):
    facility = orlando(session, adapter)
    reconcile_access(session, adapter, facility, RUN)
    assert open_items(session) == []          # A-012 is paused by its arrangement, A-013 is locked, the code is off
    sim.post("/sim/exemptions/cancel", {"leaseId": "lease_RamnA0012"})
    reconcile_access(session, adapter, facility, RUN)
    assert [(w.kind, w.subject_id) for w in open_items(session)] == [(MISSING, "lease_RamnA0012")]


@pytest.mark.parametrize("rule,expected", [("deny_if_any_unit_restricted", []),
                                           ("allow_if_any_unit_in_good_standing", [(NOT_RESTORED, "customer")])])
def test_one_gate_code_for_two_units_follows_the_recorded_rule(session, adapter, sim, rule, expected):
    # Priya pays A-012 in full; A-013 is still past the rule, so the FMS (which denies if any unit is past it) keeps
    # her shared code off. Whether that is right depends on the facility's recorded rule.
    pay_in_full(sim, "lease_RamnA0012", 9500)
    facility = orlando(session, adapter, gate_rule=rule)
    reconcile_access(session, adapter, facility, RUN)
    assert [(w.kind, w.subject_type) for w in open_items(session)] == expected


def test_a_facility_without_a_recorded_rule_gets_a_review_item_and_no_verdicts(session, adapter, sim, fms_url):
    pay_in_full(sim, "lease_WhitB1042", 24000)
    facility = orlando(session, adapter, policy=False)
    reconcile_access(session, adapter, facility, RUN)
    assert [(w.kind, w.subject_type) for w in open_items(session)] == [(REVIEW, "facility")]
    assert tasks_for_unit(fms_url, "lease_WhitB1042") == []


class FlakyLocks(CubbyAdapter):
    def locks_for_lease(self, lease_provider_id):
        raise FMSError("/v1/locks/search: ReadTimeout")


def test_a_failed_read_changes_nothing(session, adapter, sim, fms_url):
    pay_in_full(sim, "lease_WhitB1042", 24000)
    facility = orlando(session, adapter)
    reconcile_access(session, adapter, facility, RUN)
    lock = locks_on(fms_url, "lease_WhitB1042")[0]
    sim.post("/sim/locks/return", {"lockId": lock["lockId"]})
    flaky = FlakyLocks(fms_url, FULL_KEY, 5.0)
    result = reconcile_access(session, flaky, facility, RUN)
    flaky.close()
    # the lock is gone at the FMS, but a pass that could not read it neither closes nor opens anything
    assert result.failures and [w.subject_id for w in open_items(session)] == ["lease_WhitB1042"]


def test_a_lost_task_answer_is_looked_up_and_never_sent_twice(session, adapter, sim, fms_url):
    pay_in_full(sim, "lease_WhitB1042", 24000)
    sim.post("/sim/faults", {"kind": "lose_task_response", "facilityId": ORLANDO})
    facility = orlando(session, adapter)
    for _ in range(3):
        reconcile_access(session, adapter, facility, RUN)
    (item,) = open_items(session)
    # the task was made, its id never arrived, and Gatehouse found it by its marker
    assert item.task_state == "created" and len(tasks_for_unit(fms_url, "lease_WhitB1042")) == 1


def test_an_unknown_task_outcome_is_never_retried(session, adapter, sim, fms_url, monkeypatch):
    pay_in_full(sim, "lease_WhitB1042", 24000)
    facility = orlando(session, adapter)
    calls = []

    def lost(*a, **k):
        calls.append(1)
        return TaskCreateOutcome(kind="lost", detail="the connection broke before the answer arrived")
    monkeypatch.setattr(adapter, "create_task", lost)
    for _ in range(3):
        reconcile_access(session, adapter, facility, RUN)
    (item,) = open_items(session)
    assert item.task_state == "outcome_unknown" and len(calls) == 1 and "does not create another" in item.note


def test_a_condition_that_comes_back_opens_a_new_episode(session, adapter, sim, fms_url):
    pay_in_full(sim, "lease_WhitB1042", 24000)
    facility = orlando(session, adapter)
    reconcile_access(session, adapter, facility, RUN)
    lock = locks_on(fms_url, "lease_WhitB1042")[0]
    sim.post("/sim/locks/return", {"lockId": lock["lockId"]})
    reconcile_access(session, adapter, facility, RUN)
    sim.post("/sim/locks/attach", {"lockId": lock["lockId"], "leaseId": "lease_WhitB1042"})
    reconcile_access(session, adapter, facility, RUN)
    rows = session.scalars(select(WorkItem).where(WorkItem.subject_id == "lease_WhitB1042").order_by(WorkItem.id)).all()
    assert [(w.episode_key, w.state) for w in rows] == [("1", "closed"), ("2", "open")]


def test_simulator_scopes_and_task_rules_follow_cubby(sim):
    assert sim.post("/v1/locks/search", {"where": {}}).status_code == 403
    assert sim.post("/v1/locks/search", {"where": {"organizationId": "org_Northstar01", "leaseId": "lease_WhitB1042"}}).status_code == 403
    assert sim.post("/v1/tasks/search", {"where": {}}).status_code == 400
    assert sim.post("/v1/tasks/create", {"facilityId": ORLANDO, "title": "t", "unitId": "unit_x", "customerId": "c"}).status_code == 400
    task = sim.post("/v1/tasks/create", {"facilityId": ORLANDO, "title": "t", "unitId": "unit_WhitB1042"}).json()["data"]["taskId"]
    assert sim.post("/v1/tasks/update", {"taskId": task, "status": "COMPLETED"}).status_code == 200
    assert sim.post("/v1/tasks/update", {"taskId": task, "status": "ARCHIVED"}).status_code == 400   # terminal to terminal
    assert sim.post("/v1/tasks/update", {"taskId": task, "title": "edited"}).status_code == 400       # only OPEN is edited
    assert sim.post("/v1/access-codes/check", {"facilityId": ORLANDO, "code": "0000"}).json()["data"]["message"] == "Code not found"
