"""Rent rows a person has to act on get one FMS task each, carried by a work item: asked once, never again on the next pass, never from a
reading the pass could not renew, and closed only when a fresh reading no longer shows the problem. The simulator and the desk are real."""
from __future__ import annotations

from sqlalchemy import select

from desk.adapter import FMSError
from desk.models import Facility, WorkItem
from desk.rent_changes import TASK_KINDS, reconcile_rent_changes

from .conftest import ORLANDO
from .test_investigator import factory  # noqa: F401


def rent_items(s, state="open"):
    return {w.subject_id: w for w in s.scalars(select(WorkItem).where(WorkItem.kind.in_(TASK_KINDS.values()), WorkItem.state == state))}


def orlando(s):
    return s.scalar(select(Facility).where(Facility.provider_id == ORLANDO))


def test_each_row_a_person_must_act_on_gets_one_task_and_only_one(factory, adapter):  # noqa: F811
    with factory() as s:
        assert reconcile_rent_changes(s, adapter, orlando(s)).failures == []
        items = rent_items(s)
        kinds = sorted(w.kind for w in items.values())
        assert kinds == ["rent_hold", "rent_not_applied", "rent_wrong_charge"]          # E-505, M-611 and H-204
        assert all(w.task_state == "created" and w.task_id for w in items.values())
        wrong = next(w for w in items.values() if w.kind == "rent_wrong_charge")
        task = adapter.tasks_for("taskId", wrong.task_id)[0]
        assert task.title == "Review rent charge discrepancy" and "unit H-204" in task.description and "$155.00" in task.description
        held = next(w for w in items.values() if w.kind == "rent_hold")
        assert adapter.tasks_for("taskId", held.task_id)[0].title == "Review held rent change"     # the reason is in the description, not assumed
        assert "completing it does not close" in task.description and "—" not in task.title
        assert "the FMS rent is now $155.00 a month" in task.description and "not readable" not in task.description
        ids = {w.id: w.task_id for w in items.values()}
        reconcile_rent_changes(s, adapter, orlando(s))                                    # the next pass asks for nothing new
        assert {w.id: w.task_id for w in rent_items(s).values()} == ids


def test_a_reading_the_pass_could_not_renew_keeps_its_item_and_asks_for_nothing(factory, adapter, monkeypatch):  # noqa: F811
    with factory() as s:
        wrong_lease = None
        real = adapter.ledger

        def failing(lease_id):
            if lease_id == wrong_lease:
                raise FMSError("ledger read failed")
            return real(lease_id)
        reconcile_rent_changes(s, adapter, orlando(s))
        before = rent_items(s)
        wrong_lease = next(k for k, w in before.items() if w.kind == "rent_wrong_charge")
        monkeypatch.setattr(adapter, "ledger", failing)
        result = reconcile_rent_changes(s, adapter, orlando(s))
        assert any(wrong_lease in f for f in result.failures)
        after = rent_items(s)
        assert after[wrong_lease].id == before[wrong_lease].id and after[wrong_lease].state == "open"   # not closed on a guess


def test_an_item_closes_when_a_fresh_reading_no_longer_shows_its_problem(factory, adapter, monkeypatch):  # noqa: F811
    with factory() as s:
        reconcile_rent_changes(s, adapter, orlando(s))
        not_applied = next(k for k, w in rent_items(s).items() if w.kind == "rent_not_applied")
        real = adapter.rent_facts
        monkeypatch.setattr(adapter, "rent_facts", lambda fac, today: [f for f in real(fac, today) if f.lease_provider_id != not_applied])
        reconcile_rent_changes(s, adapter, orlando(s))
        assert not_applied not in rent_items(s)
        closed = rent_items(s, "closed")[not_applied]
        assert closed.verification == "closed when the rent check no longer showed it" and closed.task_id   # the task stays as it was


def test_the_rent_page_says_what_task_the_desk_asked_for(fms_url, sim):
    from fastapi.testclient import TestClient
    from .test_pages import preview
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/rent-changes", params={"facility": ORLANDO}).text
        assert page.count("An FMS task asks staff to review this. It is open.") == 3


def test_a_reading_that_cannot_tell_keeps_the_item_and_asks_for_nothing_new(factory, adapter, monkeypatch):  # noqa: F811
    from desk import rent_changes
    with factory() as s:
        reconcile_rent_changes(s, adapter, orlando(s))
        wrong = next(w for w in rent_items(s).values() if w.kind == "rent_wrong_charge")
        first = (wrong.id, wrong.task_id)
        real = rent_changes.completed

        def unsure(f, since, entries, today):
            row = real(f, since, entries, today)
            return dict(row, state="cannot_tell") if f.lease_provider_id == wrong.subject_id else row
        monkeypatch.setattr(rent_changes, "completed", unsure)
        reconcile_rent_changes(s, adapter, orlando(s))
        assert rent_items(s)[wrong.subject_id].id == first[0]                 # not closed on a reading without an answer
        monkeypatch.setattr(rent_changes, "completed", real)
        reconcile_rent_changes(s, adapter, orlando(s))
        again = rent_items(s)[wrong.subject_id]
        assert (again.id, again.task_id) == first                              # the same item and the same task, no second task


def test_a_lost_answer_on_create_never_makes_a_second_task(factory, adapter, sim):  # noqa: F811
    assert sim.post("/sim/faults", {"kind": "lose_task_response", "facilityId": ORLANDO, "on": True}).status_code == 200
    with factory() as s:
        for _ in range(3):
            reconcile_rent_changes(s, adapter, orlando(s))
        items = rent_items(s)
        assert items
        for w in items.values():
            found = [t for t in adapter.tasks_for("customerId", _customer(s, w)) if w.task_marker in (t.description or "")]
            assert len(found) == 1 and w.task_state in ("created", "outcome_unknown")


def _customer(s, w):
    from desk.models import Lease
    return s.scalar(select(Lease.provider_customer_id).where(Lease.provider_id == w.subject_id))
