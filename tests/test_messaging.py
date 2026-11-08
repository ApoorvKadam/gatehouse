"""Texts in and out, the way Cubby carries them: the SMS Received webhook in, messages/sms/send out. Cubby takes a customer
rather than a number, has no idempotency key, cannot be asked afterwards, and checks no preference, so the desk decides who may
be texted and never sends a text twice. The simulator runs as a real server and is reached only over HTTP."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from desk.adapter import FMSError
from desk.adapter.base import ProviderCustomer, SmsPreference
from desk.adapter.cubby import CubbyAdapter
from desk.api import create_app
from desk.cases import sync_facility
from desk.config import Settings
from desk.db import bind_to_fms, make_session_factory, refresh_binding, utcnow
from desk.messaging import (SMS_LIMIT, can_text, decide, e164, file_unrouted, hold_stranded, inbox, match_pending, receive_text,
                            send_text, unrouted)
from desk.models import DeskBinding, Facility, InboundMessage, OutboundMessage
from desk.worker import run_once

from .conftest import FULL_KEY, ORLANDO

TOKEN, WEBHOOK = "front-desk-token", "webhook-shared-secret"
ALBANY = "fac_NSalbany001"
SERVED = {ORLANDO, ALBANY}          # the two facilities of the Northstar key
DANA, MARCUS, PRIYA, STRANGER = "+14075550142", "+14075550187", "+14075550119", "+13215550177"
# 12:00 and 22:00 on an ordinary November day in Orlando (Eastern standard time, five hours behind UTC)
NOON, NIGHT = datetime(2026, 11, 9, 17, 0), datetime(2026, 11, 10, 3, 0)


@pytest.fixture
def desk(fms_url, sim, adapter, session):
    """A desk database that has read Orlando's leases; the value is Orlando's facility row."""
    sync_facility(session, adapter, ORLANDO)
    return session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))


def texts_in(sim, sender: str, body: str, minutes_ago: int = 1) -> dict:
    """A tenant texts Orlando's Cubby number. The answer is the SMS Received payload Cubby would send for it."""
    when = (utcnow() - timedelta(minutes=minutes_ago)).isoformat()
    answer = sim.post("/sim/messages/inbound", {"facilityId": ORLANDO, "from": sender, "body": body, "receivedAt": when})
    assert answer.status_code == 200, answer.text
    return answer.json()["data"]["webhook"]


def arrive(session, adapter, payload: dict) -> InboundMessage | None:
    """What the webhook receiver does with a payload."""
    return receive_text(session, adapter.inbound_text(payload), None, SERVED)


def service_truth(fms_url: str) -> dict:
    return httpx.get(f"{fms_url}/sim/messages/truth", params={"facilityId": ORLANDO}, timeout=10).json()["data"]


def went_out(fms_url: str) -> list[dict]:
    return [m for m in service_truth(fms_url)["messages"] if m["direction"] == "OUTBOUND" and m["status"] == "SENT"]


def stored(session, desk, phone: str, body: str, at: datetime) -> InboundMessage:
    """A text the desk already holds, for rules that need no simulator."""
    m = InboundMessage(facility_id=desk.id, provider_message_id=f"SM{phone}{at.isoformat()}{body}", phone=phone, phone_key=e164(phone),
                       body=body, received_at=at, match_state="matched")
    session.add(m)
    session.commit()
    return m


def person(customer_id: str, phone: str | None, sms: SmsPreference | None = SmsPreference(True, False, None)) -> ProviderCustomer:
    return ProviderCustomer(customer_id, "Pat", "Example", phone, sms)


# ---------------------------------------------------------------- what the simulator does, as Cubby documents it


def test_the_webhook_payload_has_the_fields_cubby_documents(fms_url, sim):
    known = texts_in(sim, "(407) 555-0142", "Can I get my gate code resent?")
    assert re.fullmatch(r"SM[0-9a-f]{32}", known["messageId"])
    assert (known["orgId"], known["facilityId"], known["from"], known["to"]) == ("org_Northstar01", ORLANDO, DANA, "+14075550100")
    assert known["body"] == "Can I get my gate code resent?" and known["receivedAt"].endswith("Z") and known["topicId"]
    assert known["customer"]["customerId"] == "cust_DanaWhit01" and known["customer"]["contact"]["phone"] == DANA
    # a number that is not a customer's arrives with no customer
    assert texts_in(sim, STRANGER, "What time does the gate open?")["customer"] is None


def test_cubby_texts_a_customer_and_does_not_look_at_preferences(fms_url, sim, adapter):
    assert sim.post("/sim/customers/sms", {"customerId": "cust_DanaWhit01", "suppressed": True, "reason": "OPTED_OUT"}).status_code == 200
    assert adapter.send_sms(ORLANDO, "cust_DanaWhit01", "Your receipt is ready.").kind == "sent"      # delivered without complaint
    [sent] = went_out(fms_url)
    assert sent["phone"] == DANA and sent["customerId"] == "cust_DanaWhit01"
    assert sim.post("/v1/messages/sms/send", {"facilityId": ORLANDO, "customerId": "cust_DanaWhit01", "body": "x" * (SMS_LIMIT + 1)}).status_code == 400
    assert sim.post("/v1/messages/sms/send", {"facilityId": ORLANDO, "customerId": "cust_nobody", "body": "hi"}).status_code == 404
    # another operator's customer is indistinguishable from a missing one
    assert sim.post("/v1/messages/sms/send", {"facilityId": ORLANDO, "customerId": "cust_HBRuizLu01", "body": "hi"}).status_code == 404
    assert len(went_out(fms_url)) == 1


def test_the_fms_flag_learns_of_a_stop_only_when_a_send_is_rejected(fms_url, sim, adapter):
    texts_in(sim, DANA, "STOP")
    assert adapter.customer("cust_DanaWhit01").sms.suppressed is False          # the flag lags a STOP by one message
    refused = adapter.send_sms(ORLANDO, "cust_DanaWhit01", "One more thing.")
    assert refused.kind == "rejected" and "opted out" in refused.detail
    assert adapter.customer("cust_DanaWhit01").sms == SmsPreference(False, True, "OPTED_OUT")
    assert went_out(fms_url) == [] and service_truth(fms_url)["carrierStops"] == ["cust_DanaWhit01"]
    texts_in(sim, DANA, "Start")
    assert adapter.send_sms(ORLANDO, "cust_DanaWhit01", "One more thing.").kind == "sent"
    assert adapter.customer("cust_DanaWhit01").sms.suppressed is True           # only customers/update clears the FMS flag


# ---------------------------------------------------------------- the receiver


def test_a_text_is_kept_once_even_when_cubby_delivers_it_twice(sim, adapter, session, desk):
    payload = texts_in(sim, DANA, "I paid, so why is B-1042 on the auction list?")
    first, second = arrive(session, adapter, payload), arrive(session, adapter, payload)
    assert first is not None and second is None
    [m] = session.scalars(select(InboundMessage)).all()
    assert (m.provider_message_id, m.phone_key, m.facility_id, m.match_state) == (payload["messageId"], DANA, desk.id, "pending")
    assert (m.customer_provider_id, m.customer_name) == ("cust_DanaWhit01", "Dana Whitfield")      # what Cubby's payload named


def test_a_payload_dated_in_the_future_cannot_outrank_a_later_stop(sim, adapter, session, desk):
    ahead = {**texts_in(sim, DANA, "Start"), "receivedAt": (utcnow() + timedelta(days=1)).isoformat() + "Z"}
    kept = arrive(session, adapter, ahead)
    assert kept is not None and kept.received_at <= utcnow()          # held to the desk's own clock
    arrive(session, adapter, texts_in(sim, DANA, "STOP", minutes_ago=0))
    verdict = can_text(session, desk, person("cust_1", DANA), utcnow(), reply_to=kept)
    assert not verdict.ok and "STOP" in verdict.reason


def test_a_payload_that_is_not_an_sms_received_webhook_is_refused(sim, adapter):
    good = texts_in(sim, DANA, "hello")
    without_facility = {k: v for k, v in good.items() if k != "facilityId"}
    without_org = {k: v for k, v in good.items() if k != "orgId"}
    for broken in ({}, {**good, "body": ""}, {**good, "receivedAt": "yesterday"}, {**good, "messageId": None}, {**good, "facilityId": 7},
                   without_facility, without_org, {**good, "to": "5550100"}, {**good, "from": "hello"}):
        with pytest.raises(ValueError):
            adapter.inbound_text(broken)
    assert adapter.inbound_text({**good, "customer": {"name": "no id"}}).customer is None
    assert adapter.inbound_text({**good, "facilityId": None}).facility_provider_id is None      # null is how Cubby says "several"


def test_a_text_for_a_facility_the_desk_has_not_read_yet_is_kept_and_filed_when_it_is(sim, adapter, session):
    m = arrive(session, adapter, texts_in(sim, DANA, "hello"))
    assert m is not None and m.facility_id is None and m.match_state == "unrouted" and "has not been read yet" in m.match_note
    assert file_unrouted(session, None, SERVED) == 0                      # still not read
    sync_facility(session, adapter, ORLANDO)
    assert file_unrouted(session, None, SERVED) == 1
    desk = session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
    session.refresh(m)
    assert (m.facility_id, m.match_state, m.match_note) == (desk.id, "pending", None)
    assert match_pending(session, adapter, desk) == 1 and m.match_state == "matched"


def test_a_text_from_a_number_shared_by_several_facilities_is_kept_unrouted_until_the_operator_says_which_is_which(sim, adapter, session, desk):
    shared = {**texts_in(sim, DANA, "hello"), "facilityId": None}
    text = adapter.inbound_text(shared)
    m = receive_text(session, text, None, SERVED)
    assert m is not None and m.facility_id is None and m.match_state == "unrouted" and "DESK_TEXT_NUMBERS" in m.match_note
    assert match_pending(session, adapter, desk) == 0 and inbox(session, desk) == []
    [listed] = unrouted(session, SERVED)
    assert listed["from"] == DANA and listed["to"] == "+14075550100" and "DESK_TEXT_NUMBERS" in listed["note"]
    # with the number set to a facility, the next such text is filed under it, and so is the one that was waiting
    filed = receive_text(session, adapter.inbound_text({**shared, "messageId": "SM-other"}), {"(407) 555-0100": ORLANDO}, SERVED)
    assert filed is not None and filed.facility_id == desk.id and filed.match_state == "pending"
    assert file_unrouted(session, {"(407) 555-0100": ORLANDO}, SERVED) == 1 and unrouted(session, SERVED) == []
    session.refresh(m)
    assert (m.facility_id, m.match_state) == (desk.id, "pending")


def test_a_wrong_number_setting_can_be_corrected_and_the_waiting_text_is_filed(sim, adapter, session, desk):
    shared = {**texts_in(sim, DANA, "hello"), "facilityId": None}
    m = receive_text(session, adapter.inbound_text(shared), {"+14075550100": "fac_WRONG"}, SERVED)
    assert m is not None and m.match_state == "unrouted" and "fac_WRONG" in m.match_note
    assert m.facility_provider_id is None                                      # the setting's answer is not kept as the webhook's
    assert file_unrouted(session, {"+14075550100": "fac_WRONG"}, SERVED) == 0
    assert file_unrouted(session, {"+14075550100": ORLANDO}, SERVED) == 1       # corrected: filed
    session.refresh(m)
    assert (m.facility_id, m.match_state) == (desk.id, "pending")


# ---------------------------------------------------------------- whose number it is


def test_cubbys_own_match_stands_and_the_phone_search_confirms_it(sim, adapter, session, desk):
    arrive(session, adapter, texts_in(sim, DANA, "I paid, so why is B-1042 on the auction list?"))
    assert match_pending(session, adapter, desk) == 1
    [m] = session.scalars(select(InboundMessage)).all()
    assert (m.match_state, m.customer_provider_id, m.customer_name, m.match_note) == ("matched", "cust_DanaWhit01", "Dana Whitfield", None)
    shown = inbox(session, desk)[0]
    assert shown["stopped"] is False and shown["match"]["leases"][0]["unit"] == "B-1042" and shown["match"]["leases"][0]["case_id"]


def test_a_number_nobody_has_on_file_stays_unmatched(sim, adapter, session, desk):
    arrive(session, adapter, texts_in(sim, STRANGER, "What time does the gate open on Sundays?"))
    match_pending(session, adapter, desk)
    [m] = session.scalars(select(InboundMessage)).all()
    assert (m.match_state, m.customer_provider_id) == ("unmatched", None) and "No tenant" in m.match_note


def test_two_tenants_on_one_number_are_never_decided_for_the_sender():
    shared = [person("cust_1", "+14075550142"), person("cust_2", "(407) 555-0142")]
    found = decide(None, None, shared, DANA)
    assert found.state == "ambiguous" and found.customer_id is None and "2 tenants" in found.note
    assert decide("cust_1", "Pat Example", shared, DANA).state == "ambiguous"       # Cubby's hint does not settle it
    assert decide("cust_1", "Pat Example", [person("cust_1", DANA), person("cust_1", DANA)], DANA).state == "matched"


def test_a_search_that_finds_someone_else_than_cubby_named_goes_to_a_person():
    found = decide("cust_1", "Pat Example", [person("cust_2", DANA)], DANA)
    assert found.state == "ambiguous" and "names one tenant" in found.note


def test_a_partial_match_from_the_fms_and_a_number_from_another_country_are_not_the_sender():
    assert decide(None, None, [person("cust_1", "+14075550142")], "555-0142").state == "unmatched"      # the FMS filter is partial
    abroad = [person("cust_1", "+44 4075550142")]                                                       # the same last ten digits
    assert decide(None, None, abroad, DANA).state == "unmatched"
    assert e164("(407) 555-0142") == e164("4075550142") == e164("+1 407 555 0142") == DANA
    assert e164("+44 4075550142") != DANA
    # a number written with a plus keeps its own country code, even when it is ten digits long
    assert e164("+4420712345") != e164("+14420712345") and e164("+4420712345") == "+4420712345"
    assert decide(None, None, [person("cust_1", "+14420712345")], "+4420712345").state == "unmatched"


def test_a_key_that_cannot_read_phone_numbers_can_only_trust_cubbys_webhook(fms_url, sim, session, desk):
    blind = CubbyAdapter(fms_url, "sim_org_northstar_nopii", 5.0)
    arrive(session, blind, texts_in(sim, DANA, "hello"))
    arrive(session, blind, texts_in(sim, STRANGER, "hello"))
    match_pending(session, blind, desk)
    blind.close()
    by_sender = {m.phone_key: m for m in session.scalars(select(InboundMessage))}
    # the search answers with the customer but leaves out the number, as Cubby says it does without access to personal data
    assert by_sender[DANA].match_state == "matched" and "cannot search by phone" in by_sender[DANA].match_note
    assert by_sender[STRANGER].match_state == "unmatched"
    assert decide(None, None, None, DANA).state == "unmatched" and "cannot read" in decide(None, None, None, DANA).note


def test_a_text_the_fms_could_not_be_asked_about_is_kept_and_matched_on_the_next_pass(sim, adapter, session, desk):
    arrive(session, adapter, texts_in(sim, DANA, "hello"))
    nobody_home = CubbyAdapter("http://127.0.0.1:9", FULL_KEY, 0.5)      # nothing listens on this port
    with pytest.raises(FMSError):
        match_pending(session, nobody_home, desk)
    nobody_home.close()
    assert session.scalar(select(InboundMessage.match_state)) == "pending"
    assert match_pending(session, adapter, desk) == 1
    assert session.scalar(select(InboundMessage.match_state)) == "matched"


def test_the_worker_matches_a_text_that_waited(fms_url, sim, adapter):
    factory = make_session_factory("sqlite:///:memory:")
    bind_to_fms(factory, adapter, fms_url)
    with factory() as s:
        sync_facility(s, adapter, ORLANDO)
        arrive(s, adapter, texts_in(sim, DANA, "hello"))
        assert s.scalar(select(InboundMessage.match_state)) == "pending"
    run_once(factory, adapter)
    with factory() as s:
        assert s.scalar(select(InboundMessage.match_state)) == "matched"


# ---------------------------------------------------------------- who may be texted


def test_a_text_to_nobody_on_file_is_never_sent(session, desk):
    asked = stored(session, desk, STRANGER, "What time does the gate open?", NOON - timedelta(minutes=5))
    verdict = can_text(session, desk, None, NOON, reply_to=asked)
    assert not verdict.ok and "only a customer on file" in verdict.reason
    assert "cannot read the tenant's phone" in can_text(session, desk, person("cust_1", None), NOON).reason


def test_a_reply_goes_only_to_the_number_the_customer_texted_from(session, desk):
    elsewhere = stored(session, desk, "+14075550199", "It is Dana, my phone died.", NOON - timedelta(minutes=5))
    verdict = can_text(session, desk, person("cust_1", DANA), NOON, reply_to=elsewhere)
    assert not verdict.ok and "number on file (ending 0142), not the number that texted (ending 0199)" in verdict.reason


def test_a_number_that_texted_stop_gets_nothing_until_it_texts_start(session, desk):
    known = person("cust_1", DANA)
    stop = stored(session, desk, DANA, "Stop.", NOON - timedelta(hours=2))
    verdict = can_text(session, desk, known, NOON, reply_to=stop)
    assert not verdict.ok and "STOP" in verdict.reason
    stored(session, desk, "(407) 555-0142", "start", NOON - timedelta(hours=1))      # written another way, still the same number
    assert can_text(session, desk, known, NOON, reply_to=stop).ok


def test_a_stop_from_a_number_in_another_country_is_not_this_tenants_stop(session, desk):
    stored(session, desk, "+44 4075550142", "STOP", NOON - timedelta(hours=2))
    asked = stored(session, desk, DANA, "Is anyone there?", NOON - timedelta(minutes=5))
    assert can_text(session, desk, person("cust_1", DANA), NOON, reply_to=asked).ok


def test_a_sentence_that_contains_stop_is_not_the_keyword(session, desk):
    asked = stored(session, desk, DANA, "Please stop by the unit and check the door", NOON - timedelta(minutes=5))
    assert can_text(session, desk, person("cust_1", DANA), NOON, reply_to=asked).ok


def test_only_a_whole_start_word_lifts_a_stop(session, desk):
    known = person("cust_1", DANA)
    stop = stored(session, desk, DANA, "STOP", NOON - timedelta(hours=3))
    for not_a_keyword in ("YES2", "start 123", "yes please"):
        stored(session, desk, DANA, not_a_keyword, NOON - timedelta(hours=2))
        assert not can_text(session, desk, known, NOON, reply_to=stop).ok
    stored(session, desk, DANA, "  yes!  ", NOON - timedelta(hours=1))
    assert can_text(session, desk, known, NOON, reply_to=stop).ok


def test_a_tenant_the_fms_has_switched_off_is_not_texted_even_in_reply(session, desk):
    asked = stored(session, desk, DANA, "Why is my gate code off?", NOON - timedelta(minutes=5))
    off = person("cust_1", DANA, SmsPreference(False, True, "OPTED_OUT"))
    assert "opted out" in can_text(session, desk, off, NOON, reply_to=asked).reason
    bounced = person("cust_1", DANA, SmsPreference(False, True, "UNDELIVERABLE"))
    assert "undeliverable" in can_text(session, desk, bounced, NOON, reply_to=asked).reason


def test_a_reply_may_go_out_at_night_but_a_text_the_desk_starts_may_not(session, desk):
    known = person("cust_1", DANA, SmsPreference(False, False, None))      # transactional messages only
    asked = stored(session, desk, DANA, "Is anyone there?", NIGHT - timedelta(minutes=5))
    assert can_text(session, desk, known, NIGHT, reply_to=asked).ok
    assert can_text(session, desk, known, NOON).ok
    night = can_text(session, desk, known, NIGHT)
    assert not night.ok and "Quiet hours" in night.reason


def test_preferences_the_fms_does_not_show_block_a_reply_as_well(session, desk):
    asked = stored(session, desk, DANA, "Hello", NOON - timedelta(minutes=5))
    unknown = person("cust_1", DANA, None)
    verdict = can_text(session, desk, unknown, NOON, reply_to=asked)
    assert not verdict.ok and "shows no text preferences" in verdict.reason
    assert can_text(session, desk, person("cust_1", DANA), NOON, reply_to=asked).ok


def test_an_old_text_opens_no_conversation(session, desk):
    old = stored(session, desk, DANA, "Hello", NOON - timedelta(days=3))
    known = person("cust_1", DANA, SmsPreference(True, False, None))
    assert can_text(session, desk, known, NOON, reply_to=old).ok                        # a daytime text to a tenant who may be texted
    assert not can_text(session, desk, known, NIGHT, reply_to=old).ok                   # but not at night: the old text is no reply


# ---------------------------------------------------------------- sending, through Cubby


def matched(session, adapter, desk, sim, sender: str, body: str) -> InboundMessage:
    arrive(session, adapter, texts_in(sim, sender, body))
    match_pending(session, adapter, desk)
    return session.scalars(select(InboundMessage).where(InboundMessage.phone_key == sender)).one()


def test_a_reply_goes_through_cubby_once(fms_url, sim, adapter, session, desk):
    asked = matched(session, adapter, desk, sim, DANA, "I paid, so why is B-1042 on the auction list?")
    row = send_text(session, adapter, desk, "cust_DanaWhit01", "We are checking it now.", "front desk", reply_to=asked)
    assert row.state == "sent" and row.sent_at and row.inbound_id == asked.id and row.phone == DANA
    [delivered] = went_out(fms_url)
    assert delivered["body"] == "We are checking it now." and delivered["phone"] == DANA


def test_a_lost_answer_is_held_for_a_person_and_the_text_is_never_sent_again(fms_url, sim, adapter, session, desk):
    asked = matched(session, adapter, desk, sim, DANA, "Is anyone there?")
    assert sim.post("/sim/faults", {"kind": "lose_send_response", "facilityId": ORLANDO}).status_code == 200
    row = send_text(session, adapter, desk, "cust_DanaWhit01", "Yes, until 6.", "front desk", reply_to=asked, request_id="reply-0001")
    assert row.state == "outcome_unknown" and "does not send it again" in row.reason and "Check the conversation" in row.reason
    again = send_text(session, adapter, desk, "cust_DanaWhit01", "Yes, until 6.", "front desk", reply_to=asked, request_id="reply-0001")
    assert again.id == row.id and again.state == "outcome_unknown"
    assert len(went_out(fms_url)) == 1                         # the text did go out, once, and nothing tried again
    with pytest.raises(ValueError):
        send_text(session, adapter, desk, "cust_DanaWhit01", "Something else.", "front desk", reply_to=asked, request_id="reply-0001")


def test_a_stop_blocks_the_text_before_cubby_is_called_and_before_its_flag_turns(fms_url, sim, adapter, session, desk):
    stop = matched(session, adapter, desk, sim, MARCUS, "STOP")
    row = send_text(session, adapter, desk, "cust_MarcBell01", "Your payment came back.", "front desk", reply_to=stop)
    assert row.state == "blocked" and "STOP" in row.reason
    assert went_out(fms_url) == []
    assert adapter.customer("cust_MarcBell01").sms.suppressed is False      # Cubby was never asked, so its flag has not caught up
    assert inbox(session, desk)[0]["stopped"] is True


def test_a_tenant_the_fms_switched_off_is_blocked_before_cubby_is_called(fms_url, sim, adapter, session, desk):
    assert sim.post("/sim/customers/sms", {"customerId": "cust_DanaWhit01", "suppressed": True, "reason": "OPTED_OUT"}).status_code == 200
    asked = matched(session, adapter, desk, sim, DANA, "Why is my gate code off?")
    row = send_text(session, adapter, desk, "cust_DanaWhit01", "It is back on.", "front desk", reply_to=asked)
    assert row.state == "blocked" and "opted out" in row.reason and went_out(fms_url) == []


def test_a_number_that_is_not_a_customers_gets_a_blocked_row_not_a_text(fms_url, sim, adapter, session, desk):
    asked = matched(session, adapter, desk, sim, STRANGER, "What time does the gate open on Sundays?")
    nobody = send_text(session, adapter, desk, None, "The gate opens at 6.", "front desk", reply_to=asked)
    assert nobody.state == "blocked" and "only a customer on file" in nobody.reason
    # naming a customer for a number that is not hers does not get round it: Cubby would text her number on file
    wrong = send_text(session, adapter, desk, "cust_DanaWhit01", "The gate opens at 6.", "front desk", reply_to=asked)
    assert wrong.state == "blocked" and "not the number that texted" in wrong.reason
    assert went_out(fms_url) == []


def test_cubby_refusing_a_text_is_recorded_and_nothing_is_assumed(fms_url, sim, adapter, session, desk):
    asked = stored(session, desk, DANA, "Is anyone there?", utcnow() - timedelta(minutes=2))
    texts_in(sim, DANA, "STOP")                      # the carrier heard it; the desk never got that webhook
    row = send_text(session, adapter, desk, "cust_DanaWhit01", "Yes, until 6.", "front desk", reply_to=asked)
    assert row.state == "rejected" and "opted out" in row.reason and went_out(fms_url) == []


def test_a_text_the_carrier_cannot_take_is_refused_before_anything_is_written(adapter, session, desk):
    asked = stored(session, desk, DANA, "Is anyone there?", utcnow() - timedelta(minutes=2))
    for body in ("", "   ", "x" * (SMS_LIMIT + 1)):
        with pytest.raises(ValueError):
            send_text(session, adapter, desk, "cust_DanaWhit01", body, "front desk", reply_to=asked)
    assert session.scalars(select(OutboundMessage)).all() == []


# ---------------------------------------------------------------- the API


@pytest.fixture
def api(fms_url, sim):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token=TOKEN, webhook_token=WEBHOOK)
    with TestClient(create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False), headers={"X-Desk-Token": TOKEN}) as c:
        assert c.post(f"/api/facilities/{ORLANDO}/sync").status_code == 200
        yield c


def webhook(api, payload: dict, token: str | None = WEBHOOK):
    return api.post("/webhooks/cubby/sms-received", json=payload, headers={"X-Webhook-Token": token} if token else {})


def test_the_webhook_receiver_needs_the_shared_secret_and_a_real_payload(fms_url, sim, api):
    payload = texts_in(sim, DANA, "hello")
    assert webhook(api, payload, token=None).status_code == 401
    assert webhook(api, payload, token="wrong").status_code == 401
    assert webhook(api, {"messageId": "SM1"}).status_code == 422
    assert api.get(f"/api/facilities/{ORLANDO}/inbox").json() == {"messages": []}

    assert webhook(api, payload).json() == {"stored": True}
    assert webhook(api, payload).json() == {"stored": False}                  # Cubby delivered it twice
    assert len(api.get(f"/api/facilities/{ORLANDO}/inbox").json()["messages"]) == 1


def test_the_receiver_keeps_only_texts_that_belong_to_this_operator(fms_url, sim, api):
    # another operator's facility, and a text with no facility from another organization, are refused and nothing is kept
    assert webhook(api, {**texts_in(sim, DANA, "hi"), "facilityId": "fac_HBtampa0001"}).status_code == 404
    assert webhook(api, {**texts_in(sim, DANA, "hi"), "facilityId": None, "orgId": "org_HarborSS001"}).status_code == 404
    assert api.get("/api/inbox/unrouted").json() == {"messages": []}
    # a number this operator's facilities share has no facility in the webhook; its organization is this operator's
    assert webhook(api, {**texts_in(sim, DANA, "shared"), "facilityId": None}).json() == {"stored": True}
    # a facility the desk serves but has not read yet is kept and filed once it is read
    assert webhook(api, {**texts_in(sim, DANA, "hi again"), "facilityId": ALBANY}).json() == {"stored": True}
    assert sorted(m["body"] for m in api.get("/api/inbox/unrouted").json()["messages"]) == ["hi again", "shared"]
    assert api.post(f"/api/facilities/{ALBANY}/sync").status_code == 200
    with api.app.state.session_factory() as s:
        assert file_unrouted(s, None, SERVED) == 1
    assert [m["body"] for m in api.get("/api/inbox/unrouted").json()["messages"]] == ["shared"]
    assert [m["body"] for m in api.get(f"/api/facilities/{ALBANY}/inbox").json()["messages"]] == ["hi again"]


def test_a_facility_added_after_the_desk_started_is_served_once_the_fms_shows_it(fms_url, sim, api):
    with api.app.state.session_factory() as s:
        binding = s.get(DeskBinding, 1)
        assert binding.org_ids == ["org_Northstar01"]
        binding.facility_ids = [ORLANDO]                              # as it was before ALBANY was added to the account
        s.commit()
    assert webhook(api, {**texts_in(sim, DANA, "hello"), "facilityId": ALBANY}).json() == {"stored": True}
    with api.app.state.session_factory() as s:
        assert sorted(s.get(DeskBinding, 1).facility_ids) == sorted(SERVED)          # the binding followed the FMS


def test_when_the_fms_cannot_be_asked_a_text_for_an_unknown_facility_is_not_stored(fms_url, sim):
    class Flaky(CubbyAdapter):
        down = False

        def facilities(self):
            if self.down:
                raise FMSError("the FMS did not answer")
            return super().facilities()

    flaky = Flaky(fms_url, FULL_KEY, 5.0)
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token=TOKEN, webhook_token=WEBHOOK)
    with TestClient(create_app(settings, flaky, start_worker=False), headers={"X-Desk-Token": TOKEN}) as c:
        flaky.down = True
        refused = webhook(c, {**texts_in(sim, DANA, "hello"), "facilityId": "fac_NewlyAdded1"})
        assert refused.status_code == 503 and c.get("/api/inbox/unrouted").json() == {"messages": []}
        assert webhook(c, {**texts_in(sim, DANA, "known"), "facilityId": ORLANDO}).json() == {"stored": True}      # no FMS call needed


def test_a_desk_without_a_webhook_token_refuses_every_webhook(fms_url, sim):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token=TOKEN)
    with TestClient(create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False)) as bare:
        assert bare.post("/webhooks/cubby/sms-received", json=texts_in(sim, DANA, "hello"), headers={"X-Webhook-Token": "x"}).status_code == 503
        assert bare.get(f"/api/facilities/{ORLANDO}/inbox", headers={"X-Desk-Token": TOKEN}).json() == {"messages": []}   # not synced yet


def test_the_inbox_api_matches_lists_and_answers_a_text_once(fms_url, sim, api):
    webhook(api, texts_in(sim, DANA, "I paid, so why is B-1042 on the auction list?"))
    [shown] = api.get(f"/api/facilities/{ORLANDO}/inbox").json()["messages"]
    assert shown["match"]["state"] == "matched" and shown["match"]["customer_name"] == "Dana Whitfield" and shown["replies"] == []
    answer = {"text": "We are looking at it now.", "staff": "J. Ortiz", "request_id": "reply-0001"}
    first = api.post(f"/api/inbox/{shown['id']}/reply", json=answer)
    second = api.post(f"/api/inbox/{shown['id']}/reply", json=answer)      # a double click
    assert first.status_code == second.status_code == 200 and first.json()["id"] == second.json()["id"]
    assert first.json()["state"] == "sent" and first.json()["author"] == "staff: J. Ortiz"
    assert len(went_out(fms_url)) == 1
    [shown] = api.get(f"/api/facilities/{ORLANDO}/inbox").json()["messages"]
    assert [r["body"] for r in shown["replies"]] == ["We are looking at it now."]


def test_the_api_refuses_a_reply_it_must_not_send_and_says_why(fms_url, sim, api):
    webhook(api, texts_in(sim, MARCUS, "STOP"))
    webhook(api, texts_in(sim, STRANGER, "What time does the gate open?"))
    by_sender = {m["from"]: m for m in api.get(f"/api/facilities/{ORLANDO}/inbox").json()["messages"]}
    stop = api.post(f"/api/inbox/{by_sender[MARCUS]['id']}/reply", json={"text": "Your payment came back.", "staff": "J. Ortiz", "request_id": "reply-stop-1"})
    assert stop.status_code == 409 and "STOP" in stop.json()["detail"]
    stranger = api.post(f"/api/inbox/{by_sender[STRANGER]['id']}/reply", json={"text": "At six.", "staff": "J. Ortiz", "request_id": "reply-stranger-1"})
    assert stranger.status_code == 409 and "only a customer on file" in stranger.json()["detail"]
    too_long = api.post(f"/api/inbox/{by_sender[MARCUS]['id']}/reply", json={"text": "x" * (SMS_LIMIT + 1), "staff": "J. Ortiz", "request_id": "reply-long-1"})
    assert too_long.status_code == 422
    assert went_out(fms_url) == []


def test_replying_needs_the_operator_token_and_a_real_text(fms_url, sim, api):
    webhook(api, texts_in(sim, DANA, "hello"))
    [shown] = api.get(f"/api/facilities/{ORLANDO}/inbox").json()["messages"]
    body = {"text": "Hi.", "staff": "J. Ortiz", "request_id": "reply-0002"}
    assert api.post(f"/api/inbox/{shown['id']}/reply", json=body, headers={"X-Desk-Token": "wrong"}).status_code == 401
    assert api.post("/api/inbox/99999/reply", json=body).status_code == 404
    # a reply with no request id is refused: a double click or a retry would otherwise send the text twice
    assert api.post(f"/api/inbox/{shown['id']}/reply", json={"text": "Hi.", "staff": "J. Ortiz"}).status_code == 422
    assert went_out(fms_url) == []


def test_the_preview_opens_on_four_texts_and_what_the_desk_did_about_each(fms_url, sim):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token=TOKEN, public_view=True, demo_seed=True)
    with TestClient(create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False)) as visitor:
        messages = visitor.get(f"/api/facilities/{ORLANDO}/inbox").json()["messages"]
        by_name = {m["match"]["customer_name"] or "stranger": m for m in messages}
        assert sorted(by_name) == ["Dana Whitfield", "Marcus Bell", "Priya Raman", "stranger"]
        dana = by_name["Dana Whitfield"]
        assert dana["match"]["leases"][0]["unit"] == "B-1042" and dana["replies"] == []        # waiting for an answer
        assert by_name["Priya Raman"]["replies"][0]["state"] == "sent"
        marcus = by_name["Marcus Bell"]
        assert marcus["stopped"] is True and marcus["replies"][0]["state"] == "blocked" and "STOP" in marcus["replies"][0]["reason"]
        stranger = by_name["stranger"]
        assert stranger["match"]["state"] == "unmatched" and "only a customer on file" in stranger["replies"][0]["reason"]
        assert visitor.post(f"/api/inbox/{dana['id']}/reply", json={"text": "Hi.", "staff": "J. Ortiz", "request_id": "reply-0003"}).status_code == 401   # reading is open
    [only] = went_out(fms_url)
    assert only["phone"] == PRIYA


# ---------------------------------------------------------------- more of the documented behaviour, and what the review found


def test_the_carrier_leaves_the_reason_the_fms_already_has(fms_url, sim, adapter):
    assert sim.post("/sim/customers/sms", {"customerId": "cust_DanaWhit01", "suppressed": True, "reason": "UNDELIVERABLE"}).status_code == 200
    texts_in(sim, DANA, "STOP")
    assert adapter.send_sms(ORLANDO, "cust_DanaWhit01", "One more thing.").kind == "rejected"
    assert adapter.customer("cust_DanaWhit01").sms == SmsPreference(False, True, "UNDELIVERABLE")      # not replaced by OPTED_OUT


def test_a_send_cut_off_before_its_outcome_is_held_as_unknown_and_never_repeated(adapter, session, desk):
    asked = stored(session, desk, DANA, "Is anyone there?", utcnow() - timedelta(minutes=10))
    cut_off = OutboundMessage(facility_id=desk.id, inbound_id=asked.id, customer_provider_id="cust_DanaWhit01", phone=DANA, body="Yes.",
                              author="front desk", state="intent", created_at=utcnow() - timedelta(minutes=5))
    just_now = OutboundMessage(facility_id=desk.id, inbound_id=asked.id, customer_provider_id="cust_DanaWhit01", phone=DANA, body="Yes.",
                               author="front desk", state="intent")
    session.add_all([cut_off, just_now])
    session.commit()
    assert hold_stranded(session) == 1
    assert (cut_off.state, just_now.state) == ("outcome_unknown", "intent")
    assert "does not send it again" in cut_off.reason and hold_stranded(session) == 0


def test_the_settings_read_which_facility_a_receiving_number_belongs_to(monkeypatch):
    monkeypatch.setenv("DESK_TEXT_NUMBERS", '{"+14075550100": "fac_NSorlando01"}')
    assert Settings.from_env().text_numbers == {"+14075550100": ORLANDO}
    monkeypatch.delenv("DESK_TEXT_NUMBERS")
    assert Settings.from_env().text_numbers == {}


def test_a_shared_number_is_filed_by_the_receiving_number_or_listed_as_unrouted(fms_url, sim):
    shared = {**texts_in(sim, DANA, "I paid, so why is B-1042 on the auction list?"), "facilityId": None}

    def desk_with(numbers: dict) -> TestClient:
        settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                            reconcile_every_seconds=60, operator_token=TOKEN, webhook_token=WEBHOOK, text_numbers=numbers)
        return TestClient(create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False), headers={"X-Desk-Token": TOKEN})

    with desk_with({"+14075550100": ORLANDO}) as routed:
        routed.post(f"/api/facilities/{ORLANDO}/sync")
        assert webhook(routed, shared).json() == {"stored": True}
        [shown] = routed.get(f"/api/facilities/{ORLANDO}/inbox").json()["messages"]
        assert shown["match"]["state"] == "matched" and shown["match"]["customer_name"] == "Dana Whitfield"
        assert routed.get("/api/inbox/unrouted").json() == {"messages": []}
    with desk_with({}) as unset:
        unset.post(f"/api/facilities/{ORLANDO}/sync")
        assert webhook(unset, {**shared, "messageId": "SM-second"}).json() == {"stored": True}
        assert unset.get(f"/api/facilities/{ORLANDO}/inbox").json() == {"messages": []}
        [waiting] = unset.get("/api/inbox/unrouted").json()["messages"]
        assert waiting["from"] == DANA and "DESK_TEXT_NUMBERS" in waiting["note"]
        reply = unset.post(f"/api/inbox/{waiting['id']}/reply", json={"text": "Hi.", "staff": "J. Ortiz", "request_id": "reply-0004"})
        assert reply.status_code == 404                                   # no facility to answer from


def test_one_request_id_cannot_stand_for_two_different_texts(fms_url, sim, adapter, session, desk):
    first = matched(session, adapter, desk, sim, DANA, "I paid, so why is B-1042 on the auction list?")
    arrive(session, adapter, texts_in(sim, DANA, "And is my gate code still on?"))
    second = session.scalars(select(InboundMessage).where(InboundMessage.body.like("And is%"))).one()
    reply = send_text(session, adapter, desk, "cust_DanaWhit01", "We are looking at it now.", "front desk", reply_to=first, request_id="reply-0005")
    assert reply.state == "sent"
    # the same words under the same id, for the other message, are a mistake and nothing is sent
    with pytest.raises(ValueError):
        send_text(session, adapter, desk, "cust_DanaWhit01", "We are looking at it now.", "front desk", reply_to=second, request_id="reply-0005")
    assert len(went_out(fms_url)) == 1 and session.scalars(select(OutboundMessage)).all() == [reply]


def test_the_worker_files_a_waiting_text_once_the_operator_has_set_its_number(fms_url, sim, adapter):
    factory = make_session_factory("sqlite:///:memory:")
    bind_to_fms(factory, adapter, fms_url)
    with factory() as s:
        sync_facility(s, adapter, ORLANDO)
        arrive(s, adapter, {**texts_in(sim, DANA, "hello"), "facilityId": None})
        assert s.scalar(select(InboundMessage.match_state)) == "unrouted"
    run_once(factory, adapter)                                                                  # no number set: still waiting
    with factory() as s:
        assert s.scalar(select(InboundMessage.match_state)) == "unrouted"
    run_once(factory, adapter, text_numbers={"+14075550100": ORLANDO})
    with factory() as s:
        assert s.scalar(select(InboundMessage.match_state)) == "matched"


def test_a_facility_the_desk_no_longer_serves_takes_no_text(sim, adapter, session, desk):
    sync_facility(session, adapter, ALBANY)                                           # the desk has a row for it
    payload = {**texts_in(sim, DANA, "hello"), "facilityId": ALBANY}
    m = receive_text(session, adapter.inbound_text(payload), None, {ORLANDO})        # but the key no longer sees it
    assert m is not None and m.facility_id is None and "not one this desk serves" in m.match_note
    assert file_unrouted(session, None, {ORLANDO}) == 0
    assert file_unrouted(session, None, SERVED) == 1                                  # served again: filed


def test_a_start_that_is_waiting_unrouted_cannot_lift_a_stop(session, desk):
    known = person("cust_1", DANA)
    stop = stored(session, desk, DANA, "STOP", NOON - timedelta(hours=3))
    waiting = InboundMessage(facility_id=None, provider_message_id="SM-waiting", phone=DANA, phone_key=DANA, to_number="+14075550100",
                             body="START", received_at=NOON - timedelta(hours=2), match_state="unrouted")
    session.add(waiting)
    session.commit()
    assert not can_text(session, desk, known, NOON, reply_to=stop).ok               # a START nobody has filed does not count
    stored(session, desk, DANA, "START", NOON - timedelta(hours=1))                  # one filed under a facility does
    assert can_text(session, desk, known, NOON, reply_to=stop).ok


def test_the_unrouted_list_can_be_paged(sim, adapter, session):
    for n in range(3):          # sent ten, nine and eight minutes ago, so the order does not depend on the clock between calls
        arrive(session, adapter, {**texts_in(sim, DANA, f"text {n}", minutes_ago=10 - n), "facilityId": None})
    assert [m["body"] for m in unrouted(session, SERVED, 2, 0)] == ["text 2", "text 1"]          # newest first
    assert [m["body"] for m in unrouted(session, SERVED, 2, 2)] == ["text 0"]


def test_a_refresh_follows_the_same_organization_and_never_moves_the_desk_to_another_one(adapter, session):
    session.add(DeskBinding(id=1, fms_base_url="http://fms", facility_ids=["fac_gone", ORLANDO], org_ids=["org_Other"]))
    session.commit()
    refresh_binding(session, adapter)                    # the key sees another organization: nothing changes
    assert sorted(session.get(DeskBinding, 1).facility_ids) == sorted(["fac_gone", ORLANDO])
    session.get(DeskBinding, 1).org_ids = ["org_Northstar01"]
    session.commit()
    refresh_binding(session, adapter)                    # the same organization: its facilities, whichever came and went
    assert sorted(session.get(DeskBinding, 1).facility_ids) == sorted(SERVED)


def test_without_organization_ids_a_mixed_list_is_left_to_a_person(adapter, session):
    session.add(DeskBinding(id=1, fms_base_url="http://fms", facility_ids=["fac_gone", ORLANDO], org_ids=[]))
    session.commit()
    refresh_binding(session, adapter)                    # some old, some new, and no organization to go by
    assert sorted(session.get(DeskBinding, 1).facility_ids) == sorted(["fac_gone", ORLANDO])
    session.get(DeskBinding, 1).facility_ids = [ORLANDO]
    session.commit()
    refresh_binding(session, adapter)                    # one more facility than before: the same account
    assert sorted(session.get(DeskBinding, 1).facility_ids) == sorted(SERVED)


def test_an_unrouted_text_of_a_facility_the_desk_no_longer_serves_is_not_listed(sim, adapter, session):
    arrive(session, adapter, {**texts_in(sim, DANA, "for albany"), "facilityId": ALBANY})
    arrive(session, adapter, {**texts_in(sim, DANA, "for nobody in particular"), "facilityId": None})
    assert sorted(m["body"] for m in unrouted(session, SERVED)) == ["for albany", "for nobody in particular"]
    assert [m["body"] for m in unrouted(session, {ORLANDO})] == ["for nobody in particular"]      # ALBANY left the binding


def test_the_inbox_can_be_paged(sim, adapter, session, desk):
    for n in range(3):
        arrive(session, adapter, texts_in(sim, DANA, f"text {n}", minutes_ago=10 - n))
    assert [m["body"] for m in inbox(session, desk, 2, 0)] == ["text 2", "text 1"]
    assert [m["body"] for m in inbox(session, desk, 2, 2)] == ["text 0"]


def test_the_worker_drops_a_facility_the_key_no_longer_sees(fms_url, sim, adapter):
    factory = make_session_factory("sqlite:///:memory:")
    bind_to_fms(factory, adapter, fms_url)
    with factory() as s:
        binding = s.get(DeskBinding, 1)
        binding.facility_ids = sorted([*binding.facility_ids, "fac_sold0001"])      # a facility the operator has since sold
        s.commit()
    run_once(factory, adapter)
    with factory() as s:
        assert sorted(s.get(DeskBinding, 1).facility_ids) == sorted(SERVED)


class SeesNothing:
    """An FMS that answers the facility list with nothing, as a key does once everything has left its account."""

    def facilities(self):
        return []


def test_a_key_that_sees_no_facility_serves_none_and_keeps_the_organization(session):
    session.add(DeskBinding(id=1, fms_base_url="http://fms", facility_ids=[ORLANDO], org_ids=["org_Northstar01"]))
    session.commit()
    refresh_binding(session, SeesNothing())
    binding = session.get(DeskBinding, 1)
    assert binding.facility_ids == [] and binding.org_ids == ["org_Northstar01"]


def test_an_empty_answer_changes_nothing_when_no_organization_was_stored_to_come_back_to(session):
    session.add(DeskBinding(id=1, fms_base_url="http://fms", facility_ids=[ORLANDO], org_ids=[]))
    session.commit()
    refresh_binding(session, SeesNothing())
    assert session.get(DeskBinding, 1).facility_ids == [ORLANDO]            # unknown, so the old list stays


def test_the_desk_restarts_while_the_key_sees_nothing_and_recovers_when_it_sees_facilities_again(fms_url, adapter):
    factory = make_session_factory("sqlite:///:memory:")
    with factory() as s:
        s.add(DeskBinding(id=1, fms_base_url=fms_url, facility_ids=[ORLANDO], org_ids=["org_Northstar01"]))
        s.commit()
    bind_to_fms(factory, SeesNothing(), fms_url)                            # no refusal to start
    with factory() as s:
        binding = s.get(DeskBinding, 1)
        assert binding.facility_ids == [] and binding.org_ids == ["org_Northstar01"]
    bind_to_fms(factory, adapter, fms_url)                                  # the organization brings the facilities back
    with factory() as s:
        assert sorted(s.get(DeskBinding, 1).facility_ids) == sorted(SERVED)


def test_a_database_kept_from_before_the_inbox_is_upgraded(tmp_path, fms_url, adapter):
    from fmssim.app import create_app as sim_create_app

    old = sa.create_engine(f"sqlite:///{tmp_path / 'old-desk.db'}")
    with old.begin() as c:
        c.execute(sa.text("CREATE TABLE desk_binding (id INTEGER PRIMARY KEY, fms_base_url VARCHAR, facility_ids JSON, "
                          "fms_fingerprint VARCHAR, bound_at DATETIME)"))
        c.execute(sa.text("INSERT INTO desk_binding (id, fms_base_url, facility_ids, fms_fingerprint) VALUES (1, :u, :f, '')"),
                  {"u": fms_url, "f": json.dumps(sorted(SERVED))})
    factory = make_session_factory(f"sqlite:///{tmp_path / 'old-desk.db'}")      # adds the missing column, empty
    bind_to_fms(factory, adapter, fms_url)                                       # reads the old row and fills it
    with factory() as s:
        assert s.get(DeskBinding, 1).org_ids == ["org_Northstar01"]

    older = sa.create_engine(f"sqlite:///{tmp_path / 'old-sim.db'}")
    with older.begin() as c:
        c.execute(sa.text("CREATE TABLE facilities (facility_id VARCHAR PRIMARY KEY, org_id VARCHAR, name VARCHAR, state VARCHAR, "
                          "time_zone VARCHAR, billing_cycle VARCHAR)"))
        c.execute(sa.text("INSERT INTO facilities VALUES ('fac_NSorlando01', 'org_Northstar01', 'Old name', 'FL', 'America/New_York', 'MONTHLY')"))
        c.execute(sa.text("CREATE TABLE customers (customer_id VARCHAR PRIMARY KEY, org_id VARCHAR, first_name VARCHAR, "
                          "last_name VARCHAR, phone VARCHAR, email VARCHAR)"))
        c.execute(sa.text("INSERT INTO customers VALUES ('cust_DanaWhit01', 'org_Northstar01', 'Dana', 'Whitfield', '+14075550142', 'd@example.com')"))
    with TestClient(sim_create_app(f"sqlite:///{tmp_path / 'old-sim.db'}")) as sim_client:      # starting it rebuilds and reseeds
        ask = {"where": {"customerId": "cust_DanaWhit01"}}
        found = sim_client.post("/v1/customers/search", json=ask, headers={"Authorization": f"Bearer {FULL_KEY}"})
        dana = found.json()["data"]["customers"][0]
        assert dana["communicationPreferences"]["sms"] == {"marketing": True, "suppressed": False, "suppressionReason": None}
        texted = sim_client.post("/sim/messages/inbound", json={"facilityId": ORLANDO, "from": DANA, "body": "hello"})
        assert texted.json()["data"]["webhook"]["to"] == "+14075550100"


def test_a_desk_bound_while_the_key_saw_nothing_adopts_the_first_real_answer(fms_url, adapter):
    first = make_session_factory("sqlite:///:memory:")
    bind_to_fms(first, SeesNothing(), fms_url)
    with first() as s:
        assert s.get(DeskBinding, 1).facility_ids == [] and s.get(DeskBinding, 1).org_ids == []
        refresh_binding(s, adapter)                                              # the facilities arrive
        assert sorted(s.get(DeskBinding, 1).facility_ids) == sorted(SERVED) and s.get(DeskBinding, 1).org_ids == ["org_Northstar01"]
    second = make_session_factory("sqlite:///:memory:")
    bind_to_fms(second, SeesNothing(), fms_url)
    bind_to_fms(second, adapter, fms_url)                                        # a restart with the facilities back
    with second() as s:
        assert sorted(s.get(DeskBinding, 1).facility_ids) == sorted(SERVED)


def test_an_answer_that_is_not_a_facility_list_is_not_an_empty_portfolio():
    def asking(response: httpx.Response) -> CubbyAdapter:
        return CubbyAdapter("http://fms", FULL_KEY, 1.0, transport=httpx.MockTransport(lambda request: response))

    for odd in (httpx.Response(200, text="<html>gateway</html>"), httpx.Response(200, json={"status": 200, "data": {}}),
                httpx.Response(200, json={"status": 200, "data": {"facilities": "none"}})):
        with pytest.raises(FMSError):
            asking(odd).facilities()
    assert asking(httpx.Response(200, json={"status": 200, "data": {"facilities": []}})).facilities() == []


def test_a_tenant_cubby_names_who_is_not_at_this_facility_goes_to_a_person():
    named = decide("cust_9", "Pat Example", [person("cust_1", DANA)], DANA, hint_at_facility=False)
    assert named.state == "ambiguous" and "not a customer at this facility" in named.note
    assert decide("cust_9", "Pat Example", None, DANA, hint_at_facility=False).state == "ambiguous"      # even for a key that cannot read numbers
    # Cubby names someone, but nobody here has the sender's whole number: a person decides
    assert decide("cust_1", "Pat Example", [], DANA).state == "ambiguous"


def test_a_tenant_of_another_facility_is_not_taken_as_the_sender_here(sim, adapter, session, desk):
    arrive(session, adapter, texts_in(sim, "+15185550164", "hello"))        # Teo Varga rents at Albany and texts Orlando's number
    assert match_pending(session, adapter, desk) == 1
    [m] = session.scalars(select(InboundMessage)).all()
    assert m.match_state == "ambiguous" and m.customer_provider_id is None and "not a customer at this facility" in m.match_note


def test_a_stop_that_is_stored_while_the_intent_is_written_stops_the_send(fms_url, sim, adapter, session, desk):
    asked = matched(session, adapter, desk, sim, DANA, "Is anyone there?")
    engine, fired = session.get_bind(), []

    def stop_arrives(_):
        if not fired:
            fired.append(True)
            with Session(engine) as other:                        # the webhook, in its own session, a moment after the check
                other.add(InboundMessage(facility_id=desk.id, provider_message_id="SM-stop-race", phone=DANA, phone_key=DANA,
                                         to_number="+14075550100", body="STOP", received_at=utcnow(), match_state="matched"))
                other.commit()

    sa.event.listen(session, "after_commit", stop_arrives)
    row = send_text(session, adapter, desk, "cust_DanaWhit01", "Yes, until 6.", "front desk", reply_to=asked)
    sa.event.remove(session, "after_commit", stop_arrives)
    assert fired and row.state == "blocked" and "STOP" in row.reason
    assert went_out(fms_url) == []
