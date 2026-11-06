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


DOC = "a" * 64   # a kept document's hash


def legal(**overrides):
    answers = {"military_disclosure_provision": "yes", "military_service": "no", "bankruptcy": "no",
               "vehicle_or_watercraft": "no", "dispute": "no", "secured_party": "no", "document_of_title_issued": "no",
               "fees_authorized_by_rental_agreement": "yes", "no_newspaper_in_area": "no"} | overrides
    return [{"id": i, "kind": "legal_check", "document_sha256": None,
             "facts": {"check": k, "answer": v, "source": "rental agreement and PACER search", "observed_on": "2026-11-05"}}
            for i, (k, v) in enumerate(answers.items(), 100)]


def notice(deadline=date(2026, 11, 20), sale=date(2026, 12, 6), nid=1, total=24000, **extra):
    return {"id": nid, "kind": "notice", "document_sha256": DOC, "facts": {
        "itemized_charges": [{"description": "October rent", "category": "rent", "amount_cents": 21500, "due_on": "2026-10-01"},
                             {"description": "Late fee", "category": "fee", "amount_cents": 2500, "due_on": "2026-10-06"}],
        "itemized_total_cents": total, "sum_due_since": "2026-10-01", "ledger_snapshot_id": 7,
        "property_description": "household goods and boxes", "description_matches_rental_agreement": "yes",
        "demand_deadline": deadline.isoformat(), "sale_statement": True, "sale_date": sale.isoformat(), "sale_time": "10:00 AM",
        "sale_place": "lockerbid.example online auction", "owner_name": "Northstar Storage",
        "owner_street_address": "1200 E Colonial Dr, Orlando FL", "owner_phone": "407-555-0100"} | extra}


def mailed(on=DELIVERED, nid=1, certificate=True):
    return {"id": 2, "kind": "mail_deposit", "document_sha256": DOC, "facts": {"notice_id": nid, "deposited_on": on.isoformat(),
            "first_class": True, "certificate_of_mailing": certificate, "postage_prepaid": True, "address": "88 Pine St, Orlando FL",
            "address_basis": "rental agreement"}}


def posted(on=DELIVERED, nid=1):
    return {"id": 3, "kind": "posting", "document_sha256": DOC, "facts": {"notice_id": nid, "posted_on": on.isoformat(), "location": "unit",
                                                                "location_detail": "door of unit B-1042", "conspicuous": "yes"}}


def published(on, i, sale=date(2026, 12, 6), nid=1, **extra):
    return {"id": 10 + i, "kind": "publication", "document_sha256": DOC, "facts": {
        "notice_id": nid, "newspaper": "Orlando Sentinel", "general_circulation_in_facility_area": "yes",
        "circulation_source": "Florida Press Association listing for Orange County", "published_on": on.isoformat(),
        "sale_date": sale.isoformat(), "sale_time": "10:00 AM", "sale_place": "lockerbid.example online auction",
        "includes_property_description": True, "includes_facility_address": True, "includes_tenant_name": True,
        "includes_sale_time_place_manner": True} | extra}


def complete():
    return [notice(), mailed(), posted(), published(date(2026, 11, 21), 1), published(date(2026, 11, 28), 2)] + legal()


def facts(evidence, today=date(2026, 12, 1), jurisdiction="FL", zone="America/New_York", due=24000, payments=(), changes=(),
          snapshot_total=24000):
    charges = [{"name": "october rent", "open_cents": 21500}, {"name": "late fee", "open_cents": 2500}]
    return CaseFacts(jurisdiction, zone, due, None if payments is None else list(payments), today.isoformat(), evidence,
                     {"7": {"total_due": snapshot_total, "observed_on": "2026-11-05", "open_charges": charges}}, list(changes))


def by(evaluation, requirement):
    return next(f for f in evaluation.findings if f.requirement == requirement)


def test_a_complete_file_is_a_review_packet_and_never_a_sale_approval():
    e = evaluate(facts(complete()))
    assert e.state == "Review packet assembled"
    assert "Nothing here authenticates those documents" in e.summary and "sale authorization is outside this desk" in e.summary
    assert e.timing["payment deadline in the notice"] == "2026-11-20"
    assert e.timing["earliest sale under the statute (15 days after first publication)"] == "2026-12-06"
    assert all(f.basis.startswith(("statute", "policy")) for f in e.findings)


def test_mail_needs_a_certificate_but_never_proof_of_receipt():
    without = [x for x in complete() if x["kind"] != "mail_deposit"] + [mailed(certificate=False)]
    assert evaluate(facts(without)).state == "Evidence incomplete"
    assert by(evaluate(facts(complete())), "notice delivery").status == "met"   # deposit alone is presumed delivery


def test_an_unconfirmed_email_needs_the_fallback_mailing_and_counts_from_it():
    email = {"id": 4, "kind": "email_delivery", "document_sha256": DOC,
             "facts": {"notice_id": 1, "sent_on": "2026-11-02", "sent_to": "dana@example.com", "address_basis": "rental agreement"}}
    only_email = [x for x in complete() if x["kind"] != "mail_deposit"] + [email]
    assert by(evaluate(facts(only_email)), "e-mail fallback mailing").status == "missing"
    with_fallback = only_email + [mailed(on=DELIVERED)]
    assert evaluate(facts(with_fallback)).timing["notice delivered"] == "2026-11-06"


def test_an_email_reply_dates_delivery_by_the_reply():
    email = {"id": 4, "kind": "email_delivery", "document_sha256": DOC, "facts": {"notice_id": 1, "sent_on": "2026-11-02", "sent_to": "dana@example.com", "address_basis": "rental agreement",
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


def test_evidence_for_an_earlier_notice_record_does_not_count():
    corrected = dict(notice(nid=50, deadline=date(2026, 11, 20)), document_sha256="b" * 64)   # a new notice, never delivered
    assert evaluate(facts(complete() + [corrected])).state == "Evidence incomplete"


def test_an_out_of_area_newspaper_goes_to_review():
    evidence = complete()[:3] + [published(date(2026, 11, 21), 1, general_circulation_in_facility_area="unknown",
                                           circulation_source="not checked"), published(date(2026, 11, 28), 2)] + legal()
    assert by(evaluate(facts(evidence)), "newspaper of general circulation in the area").status == "review"


def test_a_file_without_its_documents_is_incomplete():
    evidence = [dict(e, document_sha256=None) if e["kind"] == "mail_deposit" else e for e in complete()]
    assert evaluate(facts(evidence)).state == "Evidence incomplete"


def test_the_claim_is_checked_against_the_ledger_at_notice_time():
    assert by(evaluate(facts(complete(), snapshot_total=26500)), "claim at notice time").status == "review"
    no_snapshot = [notice(ledger_snapshot_id=None)] + complete()[1:]
    assert by(evaluate(facts(no_snapshot)), "claim at notice time").status == "missing"
    unbalanced = [notice(total=30000)] + complete()[1:]
    assert by(evaluate(facts(unbalanced, snapshot_total=30000)), "itemized statement").status == "missing"


def test_a_waiver_or_return_on_the_claimed_charges_reopens_review():
    e = evaluate(facts(complete(), changes=["the late fee charge was waived"]))
    assert e.state == "Account changed: re-review"


def test_a_payment_on_the_delivery_day_is_held_as_policy():
    f = by(evaluate(facts(complete(), payments=["2026-11-06"])), "account unchanged since notice")
    assert "order is unknown" in f.detail and f.basis == "policy"


def test_impossible_email_dates_are_refused():
    import pytest as _pytest
    from desk.liens import ValidationError, parse_evidence
    with _pytest.raises(ValidationError):
        parse_evidence("email_delivery", {"notice_id": 1, "sent_on": "2026-11-19", "sent_to": "d@example.com", "address_basis": "rental agreement", "confirmation": "response",
                                          "confirmation_from": "d@example.com", "confirmed_on": "2026-11-19", "delivered_on": "2026-11-06"})


def test_new_york_and_other_states_go_to_counsel():
    assert evaluate(facts(complete(), jurisdiction="NY")).state == "Counsel review required"
    assert "no reviewed rule version" in evaluate(facts(complete(), jurisdiction="TX")).summary


def test_the_same_facts_give_the_same_answer():
    f = facts(complete())
    assert evaluate(f) == evaluate(CaseFacts(**{k: getattr(f, k) for k in f.__dataclass_fields__}))
    assert f.digest() == facts(complete()).digest()


def test_ledger_changes_cope_with_missing_lists_and_ids():
    from desk.lien_files import ledger_changes
    assert ledger_changes({"bookEntries": None}, {"bookEntries": None}) == []
    then = {"bookEntries": [{"type": "CHARGE", "amount": 2500, "amountPaid": 0, "details": {"description": "Late fee"}}]}
    assert "no entry id" in ledger_changes(then, {"bookEntries": []})[0]


def test_a_later_no_does_not_erase_an_earlier_yes():
    evidence = complete() + [{"id": 200, "kind": "legal_check", "document_sha256": None,
                              "facts": {"check": "secured_party", "answer": "yes", "source": "UCC filing 2026-123", "observed_on": "2026-11-04"}}]
    f = by(evaluate(facts(evidence)), "secured party")
    assert f.status == "review" and "conflicting" in f.detail


def test_a_credit_balance_is_not_a_debt_to_enforce():
    assert evaluate(facts(complete(), due=-1000)).state == "FMS reports paid"


def test_a_document_of_title_or_unauthorized_fees_go_to_counsel():
    assert evaluate(facts(complete()[:5] + legal(document_of_title_issued="unknown"))).state == "Counsel review required"
    assert evaluate(facts(complete()[:5] + legal(fees_authorized_by_rental_agreement="no"))).state == "Counsel review required"


def test_each_itemized_charge_must_be_open_on_the_snapshot():
    # the same $240 total, but claimed as one late fee: the snapshot has no such charge
    wrong = [notice(itemized_charges=[{"description": "Late fee", "category": "fee", "amount_cents": 24000, "due_on": "2026-10-06"}])] + complete()[1:]
    assert by(evaluate(facts(wrong)), "itemized charge on the ledger").status == "review"


@pytest.mark.parametrize("sale_time", ["TBD", "2:30 AM"])
def test_a_sale_time_must_exist_at_the_facility(sale_time):
    sale = date(2026, 3, 8) if sale_time == "2:30 AM" else date(2026, 12, 6)
    evidence = [notice(sale=sale, sale_time=sale_time)] + complete()[1:]
    assert by(evaluate(facts(evidence)), "sale time").status == "review"


def test_one_document_cannot_serve_two_notices():
    second = notice(nid=60)
    reused = dict(mailed(nid=60), id=61)   # the same certificate (same hash) filed for the second notice
    assert by(evaluate(facts(complete() + [second, reused])), "document reuse").status == "review"


def test_the_no_newspaper_route_goes_to_counsel():
    assert evaluate(facts(complete()[:5] + legal(no_newspaper_in_area="yes"))).state == "Counsel review required"


def test_a_posting_not_recorded_as_conspicuous_goes_to_review():
    evidence = complete()
    evidence[2] = dict(posted(), facts=dict(posted()["facts"], conspicuous="unknown"))
    assert by(evaluate(facts(evidence)), "conspicuous posting").status == "review"


def test_two_different_newspapers_go_to_review():
    evidence = complete()[:3] + [published(date(2026, 11, 21), 1), published(date(2026, 11, 28), 2, newspaper="Another Weekly")] + legal()
    assert by(evaluate(facts(evidence)), "same newspaper both weeks").basis == "policy"


def test_a_third_advertisement_with_other_terms_is_not_ignored():
    evidence = complete() + [published(date(2026, 12, 5), 3, sale=date(2026, 12, 20))]
    assert by(evaluate(facts(evidence)), "sale terms agree").status == "review"


def test_a_mailing_before_the_unconfirmed_email_is_not_its_fallback():
    email = {"id": 4, "kind": "email_delivery", "document_sha256": "e" * 64,
             "facts": {"notice_id": 1, "sent_on": "2026-11-07", "sent_to": "dana@example.com", "address_basis": "rental agreement"}}
    assert by(evaluate(facts(complete() + [email])), "e-mail fallback mailing").status == "missing"


def test_a_document_also_filed_on_another_case_goes_to_review():
    shared = CaseFacts("FL", "America/New_York", 24000, [], "2026-12-01", complete(),
                       {"7": {"total_due": 24000, "observed_on": "2026-11-05",
                              "open_charges": [{"name": "october rent", "open_cents": 21500}, {"name": "late fee", "open_cents": 2500}]}},
                       [], "", [DOC])
    assert by(evaluate(shared), "document reuse").status == "review"


def test_a_ledger_charge_without_a_name_matches_nothing():
    f = CaseFacts("FL", "America/New_York", 24000, [], "2026-12-01", complete(),
                  {"7": {"total_due": 24000, "observed_on": "2026-11-05",
                         "open_charges": [{"name": "", "open_cents": 21500}, {"name": "late fee", "open_cents": 2500}]}}, [])
    assert by(evaluate(f), "itemized charge on the ledger").status == "review"


def test_the_fee_check_applies_only_when_a_fee_is_claimed():
    rent_only = [notice(itemized_charges=[{"description": "October rent", "category": "rent", "amount_cents": 21500, "due_on": "2026-10-01"}], total=21500)]
    checks = [x for x in legal() if x["facts"]["check"] != "fees_authorized_by_rental_agreement"]
    f = CaseFacts("FL", "America/New_York", 21500, [], "2026-12-01", rent_only + complete()[1:5] + checks,
                  {"7": {"total_due": 21500, "observed_on": "2026-11-05", "open_charges": [{"name": "october rent", "open_cents": 21500}]}}, [])
    assert evaluate(f).state == "Review packet assembled"


def test_any_charge_that_is_not_rent_needs_the_agreements_authorization():
    charges = [{"description": "October rent", "category": "rent", "amount_cents": 21500, "due_on": "2026-10-01"},
               {"description": "Late charge", "category": "other", "amount_cents": 2500, "due_on": "2026-10-06"}]
    checks = [x for x in legal() if x["facts"]["check"] != "fees_authorized_by_rental_agreement"]
    evidence = [notice(itemized_charges=charges)] + complete()[1:5] + checks
    f = CaseFacts("FL", "America/New_York", 24000, [], "2026-12-01", evidence,
                  {"7": {"total_due": 24000, "observed_on": "2026-11-05",
                         "open_charges": [{"name": "october rent", "open_cents": 21500}, {"name": "late charge", "open_cents": 2500}]}}, [])
    assert by(evaluate(f), "fees authorized by rental agreement").status == "missing"


def test_a_charge_not_yet_due_at_notice_time_goes_to_review():
    charges = [{"description": "October rent", "category": "rent", "amount_cents": 21500, "due_on": "2026-11-10"},
               {"description": "Late fee", "category": "fee", "amount_cents": 2500, "due_on": "2026-10-06"}]
    assert by(evaluate(facts([notice(itemized_charges=charges)] + complete()[1:])), "charges due at notice time").status == "review"


def test_the_stated_due_date_matches_the_earliest_itemized_charge():
    assert by(evaluate(facts([notice(sum_due_since="2026-09-15")] + complete()[1:])), "date the sum became due").status == "review"
