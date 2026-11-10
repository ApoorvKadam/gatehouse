"""The check in front of a reminder: which past-due tenants a collections text must not go to, and the reason for each."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from desk.adapter.base import FMSError, ProviderExemption
from desk.cases import current_case, sync_facility
from desk.db import bind_to_fms, make_session_factory, utcnow
from desk.messaging import Verdict, e164
from desk.models import Facility, InboundMessage, Lease, LienEvaluation, PaymentCommand, ReminderCheck
from desk.payments import PaymentService
from desk.reminder_check import CLEAR, HOLD, Reading, _exemption_words, _payment_words, decide, reconcile_reminder_checks
from desk.worker import run_once

from .conftest import ORLANDO
from .test_messaging import DANA as DANA_PHONE, arrive, texts_in
from .test_pages import preview

OK = Verdict(True)
DANA = "+14075550142"


def read(**changes) -> Reading:
    return Reading(**{"overdue": 10, "consent": OK, **changes})


# ---------------------------------------------------------------- the rule, with no FMS

def test_an_account_with_nothing_in_the_way_is_clear():
    verdict = decide(read())
    assert (verdict["state"], verdict["code"]) == (CLEAR, "clear")


def test_a_tenant_who_must_not_be_texted_outranks_every_other_reason():
    verdict = decide(read(consent=Verdict(False, "This number texted STOP on Nov 3. It stays off until they text START."),
                          lien="A lien file has begun.", exemption=("paused", "An arrangement runs."), payment="A payment is being checked."))
    assert (verdict["state"], verdict["code"]) == (HOLD, "consent") and "STOP" in verdict["why"]


def test_a_lien_then_an_exemption_then_a_payment_hold_the_text_in_that_order():
    everything = dict(lien="lien words", exemption=("paused", "exemption words"), payment="payment words")
    assert decide(read(**everything))["code"] == "lien"
    assert decide(read(exemption=("paused", "exemption words"), payment="payment words"))["code"] == "paused"
    assert decide(read(payment="payment words")) == {"state": HOLD, "code": "payment", "label": "Payment in progress", "why": "payment words", "also": []}


def test_only_a_tenant_who_must_not_be_texted_is_labelled_so():
    assert decide(read(consent=Verdict(False, "STOP.")))["label"] == "Must not be texted"
    assert decide(read(lien="lien words"))["label"] == "A person decides"
    assert decide(read(exemption=("unpaused", "e")))["label"] == "Not paused in the FMS"
    assert decide(read(exemption=("paused", "e")))["label"] == "Texts paused in the FMS"
    assert decide(read(exemption=("tenant_exemption", "e")))["label"] == "A person decides"
    assert decide(read())["label"] == "Clear to text"


def test_every_reason_is_kept_and_only_the_first_is_the_headline():
    verdict = decide(read(overdue=None, consent=Verdict(False, "STOP."), lien="lien words", exemption=("unpaused", "exemption words"), payment="payment words"))
    assert verdict["code"] == "date" and verdict["also"] == ["STOP.", "lien words", "exemption words", "payment words"]


def test_a_delinquency_date_nobody_can_read_holds_the_text_instead_of_guessing():
    verdict = decide(read(overdue=None))
    assert (verdict["state"], verdict["code"]) == (HOLD, "date")


# ---------------------------------------------------------------- the reading, against the simulator

def orlando(session, adapter) -> Facility:
    sync_facility(session, adapter, ORLANDO)
    return session.scalar(select(Facility).where(Facility.provider_id == ORLANDO))


def verdicts(session) -> dict[str, dict]:
    return {session.get(Lease, c.lease_id).provider_id: c.data for c in session.scalars(select(ReminderCheck))}


def lease_of(session, provider_id: str) -> Lease:
    return session.scalar(select(Lease).where(Lease.provider_id == provider_id))


def test_every_past_due_account_gets_a_verdict_and_an_arrangement_that_still_lets_the_fms_text_is_caught(session, adapter, sim):
    facility = orlando(session, adapter)
    result = reconcile_reminder_checks(session, adapter, facility)
    seen = verdicts(session)
    assert result.failures == [] and set(seen) == {"lease_WhitB1042", "lease_BellC0207", "lease_RamnA0012", "lease_RamnA0013", "lease_OkfrE0505"}
    own = seen["lease_RamnA0012"]
    assert (own["state"], own["code"], own["label"]) == (HOLD, "unpaused", "Not paused in the FMS")
    assert "payment arrangement on this unit until" in own["why"] and "has not paused collections texts for it" in own["why"]
    sibling = seen["lease_RamnA0013"]
    assert (sibling["state"], sibling["code"]) == (HOLD, "tenant_exemption") and "arrangement on unit A-012" in sibling["why"]
    assert "A person decides" in sibling["why"]
    assert all(seen[k]["state"] == CLEAR for k in ("lease_WhitB1042", "lease_BellC0207", "lease_OkfrE0505"))
    assert seen["lease_BellC0207"]["owed"] == 18500 and seen["lease_BellC0207"]["tenant"] == "Marcus" and seen["lease_BellC0207"]["overdue_days"] > 0


def test_an_arrangement_that_pauses_the_fms_texts_says_so(session, adapter, sim):
    facility = orlando(session, adapter)
    assert sim.post("/sim/exemptions/cancel", {"leaseId": "lease_RamnA0012"}).status_code == 200
    ends = (date.today() + timedelta(days=20)).isoformat()
    assert sim.post("/sim/exemptions", {"leaseId": "lease_RamnA0012", "reason": "PAYMENT_ARRANGEMENT", "endDate": ends,
                                        "pauseCollectionsMessages": True}).status_code == 200
    reconcile_reminder_checks(session, adapter, facility)
    paused = verdicts(session)["lease_RamnA0012"]
    assert paused["code"] == "paused" and "and has paused collections texts for it" in paused["why"]
    assert sim.post("/sim/exemptions/cancel", {"leaseId": "lease_RamnA0012"}).status_code == 200
    reconcile_reminder_checks(session, adapter, facility)
    assert verdicts(session)["lease_RamnA0012"]["state"] == CLEAR and verdicts(session)["lease_RamnA0013"]["state"] == CLEAR


def test_a_tenant_the_fms_marks_opted_out_is_held(session, adapter, sim):
    facility = orlando(session, adapter)
    assert sim.post("/sim/customers/sms", {"customerId": "cust_DanaWhit01", "suppressed": True, "reason": "OPTED_OUT"}).status_code == 200
    reconcile_reminder_checks(session, adapter, facility)
    dana = verdicts(session)["lease_WhitB1042"]
    assert (dana["state"], dana["code"]) == (HOLD, "consent") and "opted out" in dana["why"]


def test_a_stop_the_desk_holds_counts_before_the_fms_flag_catches_up(session, adapter, sim):
    facility = orlando(session, adapter)
    session.add(InboundMessage(facility_id=facility.id, provider_message_id="SMstop1", phone=DANA, phone_key=e164(DANA), body="Stop.",
                               received_at=utcnow(), match_state="matched"))
    session.commit()
    reconcile_reminder_checks(session, adapter, facility)
    dana = verdicts(session)["lease_WhitB1042"]
    assert (dana["state"], dana["code"]) == (HOLD, "consent") and "texted STOP" in dana["why"]
    assert verdicts(session)["lease_BellC0207"]["state"] == CLEAR                    # nobody else is held for it


def test_a_payment_pending_at_the_fms_holds_the_text(session, adapter, sim):
    facility = orlando(session, adapter)
    sim.behave("lease_BellC0207", "pending_then_succeed", lag=50)
    assert adapter.make_payment("lease_BellC0207", "pm_MarcVisa1881", 5000).kind == "pending"
    PaymentService(session, adapter).reconcile_all()                                 # the desk observes the payment, as the worker does each pass
    reconcile_reminder_checks(session, adapter, facility)
    marcus = verdicts(session)["lease_BellC0207"]
    assert (marcus["state"], marcus["code"]) == (HOLD, "payment") and "pending payment of $50.00" in marcus["why"]


def test_a_payment_the_desk_is_still_checking_holds_every_unit_of_the_tenant(session, adapter, sim):
    facility = orlando(session, adapter)
    first, second = lease_of(session, "lease_RamnA0012"), lease_of(session, "lease_RamnA0013")
    case = current_case(session, first)
    assert _payment_words(session, [first, second]) is None
    session.add(PaymentCommand(case_id=case.id, lease_id=first.id, idempotency_key="k-test", amount=9500, payment_method_id="pm_PriyMc5100",
                               authorization={}, state="NEEDS_STAFF", balance_before=9500, prepaid_before=0))
    session.commit()
    assert "payment of $95.00 is still being checked" in _payment_words(session, [first, second])      # the worker passes all of her units
    assert _payment_words(session, [lease_of(session, "lease_BellC0207")]) is None


def test_a_lien_file_holds_the_text_even_while_the_account_shows_a_balance(session, adapter, sim):
    facility = orlando(session, adapter)
    case = current_case(session, lease_of(session, "lease_OkfrE0505"))
    session.add(LienEvaluation(case_id=case.id, state="Evidence incomplete", facts={}, facts_digest="d", findings=[], timing={}))
    session.commit()
    reconcile_reminder_checks(session, adapter, facility)
    gina = verdicts(session)["lease_OkfrE0505"]
    assert (gina["state"], gina["code"]) == (HOLD, "lien") and "lien file has begun" in gina["why"]


def test_a_paid_account_loses_its_verdict_and_the_rest_stay(session, adapter, sim):
    facility = orlando(session, adapter)
    reconcile_reminder_checks(session, adapter, facility)
    assert "lease_BellC0207" in verdicts(session)
    assert sim.post("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}}).status_code == 200
    reconcile_reminder_checks(session, adapter, facility)
    assert "lease_BellC0207" not in verdicts(session) and "lease_RamnA0013" in verdicts(session)


# ---------------------------------------------------------------- the page and the API

def test_the_page_and_the_api_show_who_must_not_be_texted_and_why(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/reminder-check").text
        assert "Reminder check at Northstar Storage, Orlando East" in page and "Gatehouse sends nothing" in page
        found = re.search(r"(\d+) of (\d+) past-due accounts should not get a text today", page)
        assert found and int(found.group(1)) >= 4
        # only the tenant who texted STOP is labelled a must-not; the rest are held for a person or for a fix
        assert page.count("Must not be texted") == 1 and "Not paused in the FMS" in page and page.count('class="chip act"') == 1
        for reason in ("has not paused collections texts for it", "This number texted STOP", "A lien file has begun for this account",
                       "arrangement on unit A-012"):
            assert reason in page, reason
        albany = visitor.get("/reminder-check", params={"facility": "fac_NSalbany001"}).text
        assert "Reminder check at Northstar Storage, Albany Central" in albany and "A lien file has begun for this account" in albany
        assert visitor.get("/reminder-check", params={"facility": "fac_HBtampa0001"}).status_code == 404
        rows = visitor.get(f"/api/facilities/{ORLANDO}/reminder-check").json()["rows"]
        marcus = next(r for r in rows if r["lease"] == "lease_BellC0207")
        assert (marcus["state"], marcus["code"], marcus["label"], marcus["unit"], marcus["owed"]) == ("hold", "consent", "Must not be texted", "C-207", 18500)
        assert "texted STOP" in marcus["why"] and marcus["customer"] == "cust_MarcBell01" and marcus["read_at"]
        assert {r["state"] for r in rows} <= {"hold", "clear"} and len(rows) >= 4 and all("also" in r for r in rows)


# ---------------------------------------------------------------- a reading that fails, and rows that are not whole

class Flaky:
    """The real adapter, except for the calls named, which fail the way an FMS that is down does."""

    def __init__(self, inner, down: set[str]):
        self._inner, self._down = inner, down

    def __getattr__(self, name):
        if name in self._down:
            def down(*_args, **_kwargs):
                raise FMSError(f"{name}: the FMS did not answer")
            return down
        return getattr(self._inner, name)


def test_a_failed_reading_holds_the_text_instead_of_keeping_an_old_clear(session, adapter, sim):
    facility = orlando(session, adapter)
    reconcile_reminder_checks(session, adapter, facility)
    assert verdicts(session)["lease_BellC0207"]["state"] == CLEAR
    result = reconcile_reminder_checks(session, Flaky(adapter, {"exemptions_for_customer"}), facility)
    marcus = verdicts(session)["lease_BellC0207"]
    assert result.failures and (marcus["state"], marcus["code"], marcus["owed"]) == (HOLD, "unread", 18500)
    assert "cannot say a text is safe" in marcus["why"] and marcus["tenant"] == "Marcus"
    reconcile_reminder_checks(session, adapter, facility)
    assert verdicts(session)["lease_BellC0207"]["state"] == CLEAR                       # the next good reading restores it


def test_when_the_past_due_list_cannot_be_read_no_clear_survives(session, adapter, sim):
    facility = orlando(session, adapter)
    reconcile_reminder_checks(session, adapter, facility)
    result = reconcile_reminder_checks(session, Flaky(adapter, {"delinquent_leases"}), facility)
    seen = verdicts(session)
    assert result.failures and {v["state"] for v in seen.values()} == {HOLD}
    assert seen["lease_BellC0207"]["code"] == "unread" and seen["lease_RamnA0012"]["code"] == "unpaused"       # a real hold keeps its own reason


def test_an_exemption_whose_end_date_cannot_be_read_still_holds(session):
    unreadable = ProviderExemption("lease_1", "PAYMENT_ARRANGEMENT", "next week", True, None)
    code, words = _exemption_words(session, "lease_1", [unreadable], date(2026, 11, 9))
    assert code == "unpaused" and "payment arrangement on this unit" in words and "until" not in words
    ended = ProviderExemption("lease_1", "PAYMENT_ARRANGEMENT", "2026-11-01", True, None)
    assert _exemption_words(session, "lease_1", [ended], date(2026, 11, 9)) is None


def test_a_stored_verdict_without_its_fields_does_not_break_the_page_or_the_api(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        with app.state.session_factory() as s:
            lease = s.scalar(select(Lease).where(Lease.provider_id == "lease_BellC0207"))
            s.merge(ReminderCheck(lease_id=lease.id, data={}))
            s.commit()
        assert visitor.get("/reminder-check").status_code == 200
        rows = visitor.get(f"/api/facilities/{ORLANDO}/reminder-check").json()["rows"]
        assert rows and all(r["lease"] != "lease_BellC0207" for r in rows)


def test_a_failing_reminder_step_does_not_stop_the_rest_of_the_pass(fms_url, sim, adapter, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the check broke")
    monkeypatch.setattr("desk.worker.reconcile_reminder_checks", broken)
    factory = make_session_factory("sqlite:///:memory:")
    bind_to_fms(factory, adapter, fms_url)
    with factory() as s:
        sync_facility(s, adapter, ORLANDO)
        arrive(s, adapter, texts_in(sim, DANA_PHONE, "hello"))
    _, failures = run_once(factory, adapter)
    assert failures >= 1
    with factory() as s:
        assert s.scalar(select(InboundMessage.match_state)) == "matched"            # the text that waited was still matched


def test_the_stop_date_is_the_facilitys_date_not_the_utc_date(session, adapter, sim):
    facility = orlando(session, adapter)
    session.add(InboundMessage(facility_id=facility.id, provider_message_id="SMstop2", phone=DANA, phone_key=e164(DANA), body="STOP",
                               received_at=datetime(2026, 11, 10, 0, 30), match_state="matched"))           # 7:30 PM on Nov 9 in New York
    session.commit()
    reconcile_reminder_checks(session, adapter, facility)
    assert "This number texted STOP on Nov 9." in verdicts(session)["lease_WhitB1042"]["why"]


def test_the_page_says_when_it_is_quiet_hours_at_the_facility(fms_url, sim, monkeypatch):
    with TestClient(preview(fms_url)) as visitor:
        monkeypatch.setattr("desk.web.utcnow", lambda: datetime(2026, 11, 10, 2, 30))                       # 9:30 PM in New York
        assert "It is quiet hours at this facility" in visitor.get("/reminder-check").text
        monkeypatch.setattr("desk.web.utcnow", lambda: datetime(2026, 11, 10, 17, 0))                       # noon in New York
        assert "It is quiet hours" not in visitor.get("/reminder-check").text
