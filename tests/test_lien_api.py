"""A Florida lien file kept through the desk's API, with the facility's zone and the payments read from the simulator."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .test_api import client, dana_case  # noqa: F401  (the fixture and helper are shared)

ALBANY = "fac_NSalbany001"


def today_in_orlando() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def file_for(client, cid, delivered: date):  # noqa: F811
    deadline = delivered + timedelta(days=14)
    first = deadline + timedelta(days=1)
    sale = first + timedelta(days=15)
    common = {"entered_by": "J. Ortiz"}
    add = lambda kind, facts, **kw: client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": kind, "facts": facts, **common, **kw})  # noqa: E731
    r = add("notice", {"notice_version": "N1", "itemized_total_cents": 24000, "sum_due_since": "2026-10-01",
                       "property_description": "household goods and boxes", "demand_deadline": deadline.isoformat(), "sale_statement": True,
                       "sale_date": sale.isoformat(), "sale_place": "online auction", "owner_name": "Northstar Storage",
                       "owner_street_address": "1200 E Colonial Dr, Orlando FL", "owner_phone": "407-555-0100"},
            document_name="notice-N1.txt", document_text="NOTICE OF LIEN SALE ...")
    assert r.status_code == 200 and len(r.json()["document_sha256"]) == 64
    add("mail_deposit", {"notice_version": "N1", "deposited_on": delivered.isoformat(), "certificate_of_mailing": True,
                         "postage_prepaid": True, "address": "88 Pine St, Orlando FL", "address_basis": "rental agreement"})
    add("posting", {"notice_version": "N1", "posted_on": delivered.isoformat(), "location": "unit"})
    for on in (first, first + timedelta(days=7)):
        add("publication", {"newspaper": "Orlando Sentinel", "published_on": on.isoformat(), "sale_date": sale.isoformat(),
                            "sale_place": "online auction", "includes_property_description": True, "includes_facility_address": True,
                            "includes_tenant_name": True, "includes_sale_time_place_manner": True})
    for check, answer in (("military_disclosure_provision", "yes"), ("military_service", "no"), ("bankruptcy", "no"),
                          ("vehicle_or_watercraft", "no"), ("dispute", "no"), ("secured_party", "no")):
        add("legal_check", {"check": check, "answer": answer, "source": "rental agreement, PACER, UCC search", "observed_on": delivered.isoformat()})


def test_a_complete_florida_file_is_a_review_packet_with_its_basis_and_attribution(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    file_for(client, cid, today_in_orlando() - timedelta(days=40))
    lien = client.get(f"/api/cases/{cid}/lien").json()
    assert lien["state"] == "Review packet assembled", lien["findings"]
    assert lien["rule_version"]["label"] == "FL-r1" and "Not legal review" in lien["rule_version"]["review_note"]
    assert "CC BY 4.0" in lien["reference"]["attribution"] and lien["reference"]["published_summary"]["earliest_sale"] == "Day 29"
    assert client.get(f"/api/lien/evaluations/{lien['evaluation_id']}/replay").json()["same_result"] is True


def test_a_payment_after_the_notice_reopens_review(client, sim):  # noqa: F811
    cid = dana_case(client)["case_id"]
    file_for(client, cid, today_in_orlando() - timedelta(days=40))
    sim.post("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 5000}})
    lien = client.get(f"/api/cases/{cid}/lien").json()
    assert lien["state"] == "Account changed: re-review"


def test_evidence_dated_in_the_future_is_refused_as_evidence(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    file_for(client, cid, today_in_orlando() - timedelta(days=5))   # the publications would be in the future
    lien = client.get(f"/api/cases/{cid}/lien").json()
    assert lien["state"] == "Counsel review required"
    assert any(f["requirement"] == "evidence dates" for f in lien["findings"])


def test_a_correction_supersedes_and_nothing_is_edited(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    bad = client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "mail_deposit", "entered_by": "J. Ortiz", "facts": {
        "notice_version": "N1", "deposited_on": "2026-10-01", "certificate_of_mailing": False, "postage_prepaid": True,
        "address": "88 Pine St, Orlando FL", "address_basis": "rental agreement"}}).json()
    fixed = client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "mail_deposit", "entered_by": "J. Ortiz", "supersedes": bad["id"], "facts": {
        "notice_version": "N1", "deposited_on": "2026-10-01", "certificate_of_mailing": True, "postage_prepaid": True,
        "address": "88 Pine St, Orlando FL", "address_basis": "rental agreement"}})
    assert fixed.status_code == 200
    wrong_kind = client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "posting", "entered_by": "J. Ortiz", "supersedes": bad["id"],
                                                                      "facts": {"notice_version": "N1", "posted_on": "2026-10-01", "location": "unit"}})
    assert wrong_kind.status_code == 422


def test_unsupported_or_malformed_evidence_is_refused(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    assert client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "sent_flag", "entered_by": "J. Ortiz", "facts": {}}).status_code == 422
    assert client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "mail_deposit", "entered_by": "J. Ortiz",
                                                                "facts": {"notice_version": "N1"}}).status_code == 422


def test_a_new_york_case_goes_to_counsel(client):  # noqa: F811
    client.post(f"/api/facilities/{ALBANY}/sync")
    rows = client.get(f"/api/facilities/{ALBANY}/queue").json()["rows"]
    lien = client.get(f"/api/cases/{rows[0]['case_id']}/lien").json()
    assert lien["state"] == "Counsel review required" and lien["rule_version"] is None
    assert lien["reference"]["published_summary"]["state_code"] == "NY"
