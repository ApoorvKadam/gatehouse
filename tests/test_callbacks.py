"""What staff say came of a call back: a person's statement, kept apart from what the FMS reports. Speaking with a tenant, a customer or an
unknown caller closes the call; an open lead stays until the FMS shows it ended; no answer and a voicemail never close; only a call with no
usable number can be closed as one that cannot be returned. The simulator and the desk are real."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from desk.missed_calls import KIND, STAFF, CallbackRefused, record_callback
from desk.models import CallbackRecord, WorkItem

from .test_investigator import factory  # noqa: F401
from .test_pages import preview


def calls(s):
    return {w.subject_id: w for w in s.scalars(select(WorkItem).where(WorkItem.kind == KIND))}


def test_speaking_with_a_tenant_closes_the_call_as_a_staff_record(factory):  # noqa: F811
    with factory() as s:
        dana = calls(s)["+14075550142"]
        assert dana.state == "closed" and dana.verification.startswith(STAFF)      # the preview's own seeded record
        assert "FMS" not in dana.verification
        rec = s.scalars(select(CallbackRecord).where(CallbackRecord.work_item_id == dana.id)).one()
        assert rec.outcome == "spoke" and rec.recorded_by == "front desk (synthetic)" and rec.note


def test_no_answer_and_a_voicemail_never_close_and_count_as_attempts(factory):  # noqa: F811
    with factory() as s:
        rosa = calls(s)["+14075550199"]
        record_callback(s, rosa.id, "voicemail", None, "front desk staff")
        s.refresh(rosa)
        assert rosa.state == "open" and len(s.scalars(select(CallbackRecord).where(CallbackRecord.work_item_id == rosa.id)).all()) == 2


def test_reaching_an_open_lead_leaves_the_lead_to_end_in_the_fms(factory):  # noqa: F811
    with factory() as s:
        jonah = calls(s)["+14075550191"]
        _, closed = record_callback(s, jonah.id, "spoke", "Wants the ten by ten from Saturday.", "front desk staff")
        s.refresh(jonah)
        assert not closed and jonah.state == "open" and jonah.observed["callback"] == "spoke" and jonah.who_acts == "Whoever works the lead"


def test_the_record_refuses_what_it_cannot_stand_for(factory):  # noqa: F811
    with factory() as s:
        items = calls(s)
        withheld = next(w for n, w in items.items() if n.startswith("withheld:"))
        with pytest.raises(CallbackRefused):
            record_callback(s, items["+14075550199"].id, "cannot_return", None, "front desk staff")     # it has a number
        with pytest.raises(CallbackRefused):
            record_callback(s, withheld.id, "spoke", "Spoke to them.", "front desk staff")              # no number to call
        with pytest.raises(CallbackRefused):
            record_callback(s, items["+14075550192"].id, "spoke", "", "front desk staff")              # say what came of it
        _, closed = record_callback(s, withheld.id, "cannot_return", None, "front desk staff")
        assert closed
        with pytest.raises(CallbackRefused):
            record_callback(s, withheld.id, "cannot_return", None, "front desk staff")                 # already closed
        with pytest.raises(Exception):
            s.execute(CallbackRecord.__table__.update().values(outcome="spoke"))                         # the records never change
        s.rollback()


def test_the_route_needs_the_staff_token_and_names_who_from_it(fms_url, sim):
    with TestClient(preview(fms_url)) as client:
        with client.app.state.session_factory() as s:
            rosa = calls(s)["+14075550199"].id
        body = {"outcome": "no_answer", "calls_seen": 1}
        assert client.post(f"/api/missed-calls/{rosa}/callback", json=body).status_code in (401, 403)
        assert client.post(f"/api/missed-calls/{rosa}/callback", json=body, headers={"X-Desk-Token": "op-token"}).status_code == 403
        ok = client.post(f"/api/missed-calls/{rosa}/callback", json=body, headers={"X-Desk-Token": "op-token", "X-Staff-Token": "staff-token"})
        assert ok.status_code == 200 and ok.json()["closed"] is False
        stale = client.post(f"/api/missed-calls/{rosa}/callback", json={"outcome": "wrong_number", "calls_seen": 3},
                            headers={"X-Desk-Token": "op-token", "X-Staff-Token": "staff-token"})
        assert stale.status_code == 409 and "rung again" in stale.json()["detail"]      # a record made on an older view is refused
        page = client.get("/calls-and-reminders").text
        assert "Recorded by front desk staff" in page and "Calls closed from staff records" in page and "not an FMS fact" in page


def test_a_shared_number_is_not_marked_reached_and_a_later_call_reopens_the_call_back(factory, adapter, fms_url):  # noqa: F811
    import httpx

    from desk.missed_calls import reconcile_missed_calls
    from desk.models import Facility

    from .conftest import ORLANDO
    with factory() as s:
        items = calls(s)
        with pytest.raises(CallbackRefused):
            record_callback(s, items["+14075550193"].id, "spoke", "Spoke to one of the Lees.", "front desk staff")
        jonah = items["+14075550191"]
        record_callback(s, jonah.id, "spoke", "Wants the ten by ten from Saturday.", "front desk staff")
        httpx.post(f"{fms_url}/sim/phone/missed-call", json={"facilityId": ORLANDO, "from": "+14075550191"}, timeout=5)
        reconcile_missed_calls(s, adapter, s.scalar(select(Facility).where(Facility.provider_id == ORLANDO)), "local", fms_url)
        s.refresh(jonah)
        assert jonah.state == "open" and "callback" not in jonah.observed and jonah.who_acts == "Front desk"   # he rang again: call him again


def test_the_records_are_append_only_on_a_new_database():
    from desk.db import make_session_factory
    from desk.lien_files import ensure_append_only  # noqa: F401  (importing it registers the triggers)
    factory = make_session_factory("sqlite:///:memory:")
    with factory() as s:
        s.execute(CallbackRecord.__table__.insert().values(work_item_id=1, episode_key="e", outcome="spoke", recorded_by="x"))   # writing is allowed
        for change in (CallbackRecord.__table__.update().values(outcome="no_answer"), CallbackRecord.__table__.delete()):
            with pytest.raises(Exception, match="append-only"):
                s.execute(change)
