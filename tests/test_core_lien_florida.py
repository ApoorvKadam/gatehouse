"""Florida notice rules and their records."""
import hashlib

from core import liens
from tests.test_core_liens import AD, MAIL, NOTICE, POSTING, prepared, record, staff, the_case


def test_facility_state_uses_the_last_comma_and_two_capital_letters():
    assert liens.facility_state("St. Petersburg, FL") == "FL"
    assert liens.facility_state("City, County, NY") == "NY"
    assert liens.facility_state("Orlando, fl") is None
    assert liens.facility_state("Orlando") is None


def test_other_state_requires_counsel():
    store, _, _ = prepared("visitor:fl-state")
    books = store.books("visitor:fl-state")
    books.facility["city"] = "Albany, NY"
    found = liens.findings(books, next(iter(liens.state(books).cases.values())))
    assert len(found) == 1 and found[0].requirement == "counsel review" and found[0].status == "review"


def test_notice_draft_uses_frozen_charges_and_has_a_fingerprint():
    s = "visitor:fl-draft"
    store, t, lease = prepared(s)
    case = the_case(store, s)
    draft = liens.draft_notice(case, {**NOTICE})
    assert "$100.00" in draft["text"] and "$20.00" in draft["text"] and "$120.00" in draft["text"]
    assert "2026-03-01" in draft["text"] and "2026-03-06" in draft["text"]
    assert draft["sha256"] == hashlib.sha256(draft["text"].encode()).hexdigest()
    assert draft["facts"]["copy_version"] == 1


def test_deadline_publications_and_titled_property_hold_the_packet():
    s = "visitor:fl-timing"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", {**NOTICE, "demand_deadline": "2026-03-26"}).allowed
    assert record(store, s, t, lease, "mail_deposit", MAIL).allowed
    assert record(store, s, t, lease, "posting", POSTING).allowed
    assert record(store, s, t, lease, "publication", AD).allowed
    assert record(store, s, t, lease, "legal_check", {"check": "vehicle_or_watercraft", "answer": "unknown", "source": "staff review", "observed_on": "2026-03-13"}).allowed
    found = {f.requirement: f for f in liens.findings(store.books(s), the_case(store, s))}
    assert found["demand period"].status != "met"
    assert found["the newspaper advertisement"].status != "met"
    assert found["vehicle or watercraft"].status == "review" and "counsel" in found["vehicle or watercraft"].detail


def test_email_needs_agreement_permission_and_a_fallback_after_an_unconfirmed_send():
    s = "visitor:fl-email"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", NOTICE).allowed
    email = {"notice_id": 1, "sent_on": "2026-03-13", "sent_to": "ann@example.com", "address_basis": "rental agreement"}
    assert record(store, s, t, lease, "email_delivery", email).allowed
    assert record(store, s, t, lease, "in_person_delivery", {"notice_id": 1, "delivered_on": "2026-03-13", "delivered_by": "Jo", "delivered_to": "Ann"}).allowed
    found = {f.requirement: f for f in liens.findings(store.books(s), the_case(store, s))}
    assert found["e-mail fallback mailing"].status != "met"
    assert found["e-mail agreement permission"].status != "met"


def found_now(store, s):
    return {f.requirement: f for f in liens.findings(store.books(s), the_case(store, s))}


UNCONFIRMED_EMAIL = {"notice_id": 1, "sent_on": "2026-03-13", "sent_to": "ann@example.com", "address_basis": "rental agreement",
                     "agreement_allows_email": "yes"}


def test_advertisements_wait_until_the_notice_has_a_counted_delivery():
    s = "visitor:fl-ads-wait"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", NOTICE).allowed
    assert record(store, s, t, lease, "email_delivery", UNCONFIRMED_EMAIL).allowed
    assert record(store, s, t, lease, "posting", POSTING).allowed
    assert record(store, s, t, lease, "publication", AD).allowed
    assert record(store, s, t, lease, "publication", {**AD, "published_on": "2026-04-10"}, "second advertisement").allowed
    found = found_now(store, s)
    assert found["delivery of the notice"].status != "met"
    assert found["the newspaper advertisement"].status == "waiting" and "delivered" in found["the newspaper advertisement"].detail
    assert record(store, s, t, lease, "mail_deposit", MAIL).allowed                       # the fallback mailing starts the notice time
    assert found_now(store, s)["the newspaper advertisement"].status == "met"


def test_an_advertisement_on_or_before_the_payment_deadline_is_reported_as_early_even_when_it_is_the_only_one():
    s = "visitor:fl-ad-early"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", NOTICE).allowed
    assert record(store, s, t, lease, "mail_deposit", MAIL).allowed
    assert record(store, s, t, lease, "posting", POSTING).allowed
    assert record(store, s, t, lease, "publication", {**AD, "published_on": NOTICE["demand_deadline"]}).allowed
    ad = found_now(store, s)["the newspaper advertisement"]
    assert ad.status == "review" and "before the payment deadline" in ad.detail
    # one advertisement after the deadline is simply not enough yet
    other = "visitor:fl-ad-one"
    store, t, lease = prepared(other)
    for kind, facts in (("notice", NOTICE), ("mail_deposit", MAIL), ("posting", POSTING), ("publication", AD)):
        assert record(store, other, t, lease, kind, facts).allowed
    assert found_now(store, other)["the newspaper advertisement"].status == "missing"


def with_proof(store, s, t, lease, first, second):
    for kind, facts in (("notice", NOTICE), ("mail_deposit", MAIL), ("posting", POSTING)):
        assert record(store, s, t, lease, kind, facts).allowed
    assert record(store, s, t, lease, "publication", first).allowed
    assert record(store, s, t, lease, "publication", {**second, "published_on": "2026-04-10"}, "second advertisement").allowed
    return found_now(store, s)["the newspaper advertisement"]


def test_only_the_publishers_own_proof_counts_for_an_advertisement():
    for n, (proof, words) in enumerate((("clipping", "newspaper clipping"), ("staff_entry", "typed in by staff"))):
        s = f"visitor:fl-proof{n}"
        store, t, lease = prepared(s)
        ad = with_proof(store, s, t, lease, {**AD, "proof": "publisher_affidavit"}, {**AD, "proof": proof})
        assert ad.status == "review" and "publisher's own proof" in ad.detail and words in ad.detail, proof
    store, t, lease = prepared("visitor:fl-proof-ok")
    assert with_proof(store, "visitor:fl-proof-ok", t, lease, AD, AD).status == "met"


def test_the_publishers_proof_must_name_this_tenant_and_the_address_on_the_notice():
    for n, (extra, words) in enumerate(((("proof_tenant_name", "Bo Chen"), "Bo Chen"), (("proof_facility_address", "500 Lake Rd, Tampa, FL"), "500 Lake Rd"))):
        s = f"visitor:fl-names{n}"
        store, t, lease = prepared(s)
        ad = with_proof(store, s, t, lease, {**AD, extra[0]: extra[1]}, AD)
        assert ad.status == "review" and "does not match" in ad.detail and words in ad.detail
    store, t, lease = prepared("visitor:fl-names-ok")
    same = {**AD, "proof_tenant_name": " ann  REYES ", "proof_facility_address": "1 Main St, Orlando, FL"}
    assert with_proof(store, "visitor:fl-names-ok", t, lease, same, AD).status == "met"


def test_the_proof_address_is_checked_against_the_facility_city_not_the_owner_address():
    s = "visitor:fl-proof-city"
    store, t, lease = prepared(s)
    street = {**AD, "proof_facility_address": "88 Harbor Blvd, Orlando, FL"}     # a facility street that is not the owner's street
    assert with_proof(store, s, t, lease, street, AD).status == "met"
    other = "visitor:fl-proof-city-off"
    store, t, lease = prepared(other)
    ad = with_proof(store, other, t, lease, {**AD, "proof_facility_address": "1 Main St, Tampa, FL"}, AD)
    assert ad.status == "review" and "does not match" in ad.detail and "Tampa" in ad.detail


def test_a_later_mailing_never_resets_the_delivery_date():
    s = "visitor:fl-earliest"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", {**NOTICE, "demand_deadline": "2026-11-15", "sale_date": "2026-12-20"}).allowed
    assert record(store, s, t, lease, "mail_deposit", {**MAIL, "deposited_on": "2026-11-10"}).allowed
    assert record(store, s, t, lease, "mail_deposit", {**MAIL, "deposited_on": "2026-11-01"}, "another mailing receipt").allowed
    demand = found_now(store, s)["demand period"]
    assert demand.status == "met" and "2026-11-01" in demand.detail and "2026-11-15" in demand.detail


def test_a_mailing_must_go_to_the_last_written_address_on_the_file():
    s = "visitor:fl-addr"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", NOTICE).allowed
    assert record(store, s, t, lease, "mail_deposit", MAIL).allowed
    assert found_now(store, s)["delivery of the notice"].status == "met"
    assert staff(store, s, t, lease, "record_lien_address", {"address": "4 Elm St, Tampa, FL", "basis": "written change of address"}).allowed
    late = found_now(store, s)["delivery of the notice"]
    assert late.status == "review" and "4 Elm St" in late.detail and "9 Oak Ave" in late.detail
    other = "visitor:fl-addr-never"
    store, t, lease = prepared(other)
    assert record(store, other, t, lease, "notice", NOTICE).allowed
    assert record(store, other, t, lease, "mail_deposit", {**MAIL, "address": "77 Pine Rd, Miami, FL"}).allowed
    assert found_now(store, other)["delivery of the notice"].status == "review"
    same = "visitor:fl-addr-same"
    store, t, lease = prepared(same)
    assert record(store, same, t, lease, "notice", NOTICE).allowed
    assert record(store, same, t, lease, "mail_deposit", {**MAIL, "address": " 9 oak ave,  ORLANDO, fl "}).allowed
    assert found_now(store, same)["delivery of the notice"].status == "met"


def test_late_fee_above_safe_harbor_needs_review():
    s = "visitor:fl-fee"
    store, t, lease = prepared(s)
    case = the_case(store, s)
    case.copies[-1]["snapshot"]["claim"][1]["cents"] = 2100
    found = {f.requirement: f for f in liens.findings(store.books(s), case)}
    assert found["late fee reasonableness"].status != "met"
