"""A tenant texts about a rent increase. Our agent reads the desk's rent change record, and the reply is written by the desk from it; no
sentence says a notice was or was not sent. The model is scripted; the desk, the simulator and the pages are the real ones."""
from __future__ import annotations

from sqlalchemy import select

from desk.investigator import investigate
from desk.models import Lease, RentChangeCheck

from .test_conversation import tool
from .test_investigator import Scripted, factory, packet, text_of  # noqa: F401


def ana(s):
    lease = s.scalar(select(Lease).where(Lease.unit_name == "K-412"))
    return text_of(s, "Ana Costa"), lease


def test_the_seeded_text_is_answered_from_the_rent_change_record(factory, adapter):  # noqa: F811
    with factory() as s:
        text, lease = ana(s)
        assert s.get(RentChangeCheck, lease.id).data["state"] == "review"          # the world opens with the rent check already read
        model = Scripted(
            [tool("get_account", id_="r1")],
            [tool("get_rent_change", {"lease_id": lease.provider_id}, id_="r2")],
            [tool("draft_reply", {"components": [{"kind": "message_received", "record_id": ""},
                                                 {"kind": "rent_change", "record_id": f"rentcheck:{lease.id}"},
                                                 {"kind": "acknowledgment", "record_id": ""}]}, id_="r3")],
            [tool("submit_case_packet", {**packet("rent_question", [f"rentcheck:{lease.id}"], True, "she says she was not told; a person answers",
                                                  "Ana asks how much her rent will be; it goes to $160.00."),
                                         "reminder": {"hold": False, "kind": "none", "text_id": "", "quote": ""}}, id_="r4")])
        run, _ = investigate(s, model, text, "request-rent-1", adapter=adapter)
        assert run.status == "done", run.error
        looked = model.calls[2]["messages"][-1]["content"][0]["content"]
        assert "to $160.00 on Dec" in looked or "to $160.00 on" in looked
        assert "No record shows whether a rent notice was sent." in looked
        reply = next(a for a in run.actions if a["kind"] == "reply")
        assert reply["outcome"] == "sent" and "The rent on unit K-412 is set to change to $160.00 on " in reply["body"]
        assert "notice" not in reply["body"].lower()


def test_a_held_or_past_rent_change_is_left_to_a_person(factory, adapter):  # noqa: F811
    from desk.reply import cited  # noqa: F401  (the part is refused by the desk, not by the model)
    with factory() as s:
        text, lease = ana(s)
        check = s.get(RentChangeCheck, lease.id)
        check.data = {**check.data, "state": "hold"}
        s.commit()
        model = Scripted(
            [tool("get_rent_change", {"lease_id": lease.provider_id}, id_="h1")],
            [tool("draft_reply", {"components": [{"kind": "message_received", "record_id": ""},
                                                 {"kind": "rent_change", "record_id": f"rentcheck:{lease.id}"}]}, id_="h2")],
            [tool("submit_case_packet", {**packet("rent_question", [f"rentcheck:{lease.id}"], True, "a person weighs the increase", "Ana asks about her rent."),
                                         "reminder": {"hold": False, "kind": "none", "text_id": "", "quote": ""}}, id_="h3")])
        run, _ = investigate(s, model, text, "request-rent-2", adapter=adapter)
        reply = next(a for a in run.actions if a["kind"] == "reply")
        assert "set to change" not in reply.get("body", "")
        assert "not one the desk tells a tenant about" in str(reply.get("dropped"))


def run_with(s, adapter, text, lease, intent: str, request_id: str):
    model = Scripted(
        [tool("get_rent_change", {"lease_id": lease.provider_id}, id_="g1")],
        [tool("draft_reply", {"components": [{"kind": "message_received", "record_id": ""},
                                             {"kind": "rent_change", "record_id": f"rentcheck:{lease.id}"}]}, id_="g2")],
        [tool("submit_case_packet", {**packet(intent, [f"rentcheck:{lease.id}"], True, "a person answers", "Ana writes to the facility."),
                                     "reminder": {"hold": False, "kind": "none", "text_id": "", "quote": ""}}, id_="g3")])
    run, _ = investigate(s, model, text, request_id, adapter=adapter)
    return next(a for a in run.actions if a["kind"] == "reply")


def test_the_rent_sentence_goes_only_to_a_tenant_who_asked_about_rent(factory, adapter):  # noqa: F811
    with factory() as s:
        text, lease = ana(s)
        reply = run_with(s, adapter, text, lease, "balance_question", "request-rent-3")
        assert "set to change" not in reply.get("body", "") and "did not ask about rent" in str(reply.get("dropped"))


def test_no_figure_goes_to_a_lease_billed_on_another_cycle(factory, adapter):  # noqa: F811
    with factory() as s:
        text, lease = ana(s)
        check = s.get(RentChangeCheck, lease.id)
        check.data = {**check.data, "lease_cycle": "DAYS_28"}
        s.commit()
        reply = run_with(s, adapter, text, lease, "rent_question", "request-rent-4")
        assert "set to change" not in reply.get("body", "") and "another cycle" in str(reply.get("dropped"))


def test_a_tenant_who_says_they_were_not_told_is_not_answered_with_the_amount(factory, adapter):  # noqa: F811
    with factory() as s:
        text, lease = ana(s)
        reply = run_with(s, adapter, text, lease, "rent_notice_dispute", "request-rent-5")
        assert "set to change" not in reply.get("body", "") and "disputes the notice" in str(reply.get("dropped"))
