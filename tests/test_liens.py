"""Florida lien evidence review. The dates are the risky part, so most of these pin calendar arithmetic at the
facility, across clock changes and at every boundary the statute or the policy draws."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from desk.liens import CaseFacts, DateProblem, evaluate, facility_zone, local_date, local_instant, plus_days

NY, CHI = ZoneInfo("America/New_York"), ZoneInfo("America/Chicago")


# ------------------------------------------------------------------ calendar days at the facility

@pytest.mark.parametrize("start, end, hours", [(date(2026, 3, 1), date(2026, 3, 15), 335),
                                               (date(2026, 10, 25), date(2026, 11, 8), 337)])
def test_fourteen_days_are_calendar_days_across_a_clock_change(start, end, hours):
    assert plus_days(start, 14) == end
    for zone in (NY, CHI):
        midnight = lambda d: datetime(d.year, d.month, d.day, tzinfo=zone).astimezone(timezone.utc)  # noqa: E731
        elapsed = midnight(end) - midnight(start)
        assert elapsed.total_seconds() / 3600 == hours   # the hours differ, the calendar answer does not


def test_the_same_instant_is_a_different_day_in_orlando_and_pensacola():
    instant = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)
    assert local_date(instant, NY) == date(2026, 11, 2)
    assert local_date(instant, CHI) == date(2026, 11, 1)


def test_a_time_that_happens_twice_or_never_is_refused():
    with pytest.raises(DateProblem, match="twice"):
        local_instant(datetime(2026, 11, 1, 1, 30), NY)
    with pytest.raises(DateProblem, match="does not exist"):
        local_instant(datetime(2026, 3, 8, 2, 30), NY)
    first = datetime(2026, 11, 1, 1, 30, tzinfo=NY)
    second = first.replace(fold=1)
    # given with their offsets, the two 1:30s are different instants an hour apart
    assert local_instant(second, NY).astimezone(timezone.utc) - local_instant(first, NY).astimezone(timezone.utc) == timedelta(hours=1)


def test_month_year_and_leap_day_boundaries():
    assert plus_days(date(2028, 2, 15), 14) == date(2028, 2, 29)
    assert plus_days(date(2026, 12, 25), 14) == date(2027, 1, 8)


def test_a_missing_or_unknown_zone_blocks_instead_of_defaulting():
    for bad in (None, "", "Mars/Olympus"):
        with pytest.raises(DateProblem):
            facility_zone(bad)


# ------------------------------------------------------------------ a Florida file

DELIVERED = date(2026, 11, 6)


def legal(**overrides):
    answers = {"military_disclosure_provision": "yes", "military_service": "no", "bankruptcy": "no",
               "vehicle_or_watercraft": "no", "dispute": "no", "secured_party": "no"} | overrides
    return [{"id": i, "kind": "legal_check", "facts": {"check": k, "answer": v, "source": "rental agreement and PACER search",
                                                        "observed_on": "2026-11-05"}} for i, (k, v) in enumerate(answers.items(), 100)]


def notice(deadline=date(2026, 11, 20), sale=date(2026, 12, 6), version="N1", **extra):
    return {"id": 1, "kind": "notice", "facts": {
        "notice_version": version, "itemized_total_cents": 24000, "sum_due_since": "2026-10-01",
        "property_description": "household goods and boxes", "demand_deadline": deadline.isoformat(), "sale_statement": True,
        "sale_date": sale.isoformat(), "sale_place": "lockerbid.example online auction", "owner_name": "Northstar Storage",
        "owner_street_address": "1200 E Colonial Dr, Orlando FL", "owner_phone": "407-555-0100"} | extra}


def mailed(on=DELIVERED, version="N1", certificate=True):
    return {"id": 2, "kind": "mail_deposit", "facts": {"notice_version": version, "deposited_on": on.isoformat(),
            "certificate_of_mailing": certificate, "postage_prepaid": True, "address": "88 Pine St, Orlando FL",
            "address_basis": "rental agreement"}}


def posted(on=DELIVERED):
    return {"id": 3, "kind": "posting", "facts": {"notice_version": "N1", "posted_on": on.isoformat(), "location": "unit"}}


def published(on, i, sale=date(2026, 12, 6), **extra):
    return {"id": 10 + i, "kind": "publication", "facts": {"newspaper": "Orlando Sentinel", "published_on": on.isoformat(),
            "sale_date": sale.isoformat(), "sale_place": "lockerbid.example online auction", "includes_property_description": True,
            "includes_facility_address": True, "includes_tenant_name": True, "includes_sale_time_place_manner": True} | extra}


def complete():
    return [notice(), mailed(), posted(), published(date(2026, 11, 21), 1), published(date(2026, 11, 28), 2)] + legal()


def facts(evidence, today=date(2026, 12, 1), jurisdiction="FL", zone="America/New_York", due=24000, payments=()):
    return CaseFacts(jurisdiction, zone, due, None if payments is None else list(payments), today.isoformat(), evidence)


def by(evaluation, requirement):
    return next(f for f in evaluation.findings if f.requirement == requirement)


def test_a_complete_file_is_a_review_packet_and_never_a_sale_approval():
    e = evaluate(facts(complete()))
    assert e.state == "Review packet assembled"
    assert e.summary == "Listed evidence checks completed; sale authorization is outside this desk."
    assert e.timing["payment deadline"] == "2026-11-20" and e.timing["calculated sale threshold under this policy"] == "2026-12-06"
    assert all(f.basis.startswith(("statute", "policy")) for f in e.findings)


def test_mail_needs_a_certificate_but_never_proof_of_receipt():
    without = [x for x in complete() if x["kind"] != "mail_deposit"] + [mailed(certificate=False)]
    assert evaluate(facts(without)).state == "Evidence incomplete"
    assert by(evaluate(facts(complete())), "notice delivery").status == "met"   # deposit alone is presumed delivery


def test_an_unconfirmed_email_needs_the_fallback_mailing_and_counts_from_it():
    email = {"id": 4, "kind": "email_delivery", "facts": {"notice_version": "N1", "sent_on": "2026-11-02", "sent_to": "dana@example.com"}}
    only_email = [x for x in complete() if x["kind"] != "mail_deposit"] + [email]
    assert by(evaluate(facts(only_email)), "e-mail fallback mailing").status == "missing"
    with_fallback = only_email + [mailed(on=DELIVERED)]
    assert evaluate(facts(with_fallback)).timing["notice delivered"] == "2026-11-06"


def test_an_email_reply_dates_delivery_by_the_reply():
    email = {"id": 4, "kind": "email_delivery", "facts": {"notice_version": "N1", "sent_on": "2026-11-02", "sent_to": "dana@example.com",
                                                           "confirmation": "response", "confirmation_from": "Dana@example.com",
                                                           "confirmed_on": "2026-11-06"}}
    evidence = [x for x in complete() if x["kind"] != "mail_deposit"] + [email]
    e = evaluate(facts(evidence))
    assert e.timing["notice delivered"] == "2026-11-06" and by(e, "e-mail confirmation").basis == "policy"


@pytest.mark.parametrize("days, ok", [(13, False), (14, True)])
def test_the_demand_period_boundary(days, ok):
    evidence = [notice(deadline=plus_days(DELIVERED, days))] + complete()[1:]
    assert (by(evaluate(facts(evidence)), "demand period").status == "met") is ok


@pytest.mark.parametrize("first, ok", [(date(2026, 11, 20), False), (date(2026, 11, 21), True)])
def test_publication_starts_the_day_after_the_deadline(first, ok):
    evidence = complete()[:3] + [published(first, 1, sale=plus_days(first, 15)), published(plus_days(first, 7), 2, sale=plus_days(first, 15))] + legal()
    evidence[0] = notice(sale=plus_days(first, 15))
    assert (evaluate(facts(evidence)).state == "Review packet assembled") is ok


@pytest.mark.parametrize("gap, ok", [(14, False), (15, True)])
def test_the_sale_threshold_boundary(gap, ok):
    sale = plus_days(date(2026, 11, 21), gap)
    evidence = [notice(sale=sale), mailed(), posted(), published(date(2026, 11, 21), 1, sale=sale), published(date(2026, 11, 28), 2, sale=sale)] + legal()
    assert (evaluate(facts(evidence)).state == "Review packet assembled") is ok


def test_publications_not_seven_days_apart_go_to_review():
    evidence = complete()[:3] + [published(date(2026, 11, 21), 1), published(date(2026, 11, 27), 2)] + legal()
    assert by(evaluate(facts(evidence)), "publication spacing").basis == "policy"
    assert evaluate(facts(evidence)).state == "Counsel review required"


def test_posting_on_another_day_goes_to_review_as_policy_not_statute():
    evidence = complete()
    evidence[2] = posted(on=date(2026, 11, 9))
    f = by(evaluate(facts(evidence)), "posting date")
    assert f.status == "review" and f.basis == "policy"


@pytest.mark.parametrize("check, answer", [("military_service", "unknown"), ("bankruptcy", "yes"), ("vehicle_or_watercraft", "yes"),
                                           ("dispute", "yes"), ("secured_party", "yes"), ("military_disclosure_provision", "no")])
def test_legal_flags_and_unknowns_go_to_counsel(check, answer):
    evidence = complete()[:5] + legal(**{check: answer})
    assert evaluate(facts(evidence)).state == "Counsel review required"


def test_a_missing_legal_check_is_missing_evidence_not_a_clean_answer():
    evidence = complete()[:5] + [x for x in legal() if x["facts"]["check"] != "bankruptcy"]
    assert evaluate(facts(evidence)).state == "Evidence incomplete"


def test_a_payment_after_the_notice_reopens_review():
    e = evaluate(facts(complete(), payments=["2026-11-10"]))
    assert e.state == "Account changed: re-review"
    assert evaluate(facts(complete(), payments=["2026-11-01"])).state == "Review packet assembled"


def test_paid_unavailable_and_waiting():
    assert evaluate(facts(complete(), due=0)).state == "FMS reports paid"
    assert evaluate(facts(complete(), due=None)).state == "FMS data unavailable"
    assert evaluate(facts(complete(), zone=None)).state == "FMS data unavailable"
    assert evaluate(facts(complete(), payments=None)).state == "FMS data unavailable"   # unreadable payments never look like none
    assert evaluate(facts(complete()[:3] + legal(), today=date(2026, 11, 15))).state == "Waiting for a timing threshold"


def test_evidence_for_another_notice_version_does_not_count():
    evidence = [notice(version="N2")] + complete()[1:]
    assert evaluate(facts(evidence)).state == "Evidence incomplete"


def test_new_york_and_other_states_go_to_counsel():
    assert evaluate(facts(complete(), jurisdiction="NY")).state == "Counsel review required"
    assert "no reviewed rule version" in evaluate(facts(complete(), jurisdiction="TX")).summary


def test_the_same_facts_give_the_same_answer():
    f = facts(complete())
    assert evaluate(f) == evaluate(CaseFacts(**{k: getattr(f, k) for k in f.__dataclass_fields__}))
    assert f.digest() == facts(complete()).digest()
