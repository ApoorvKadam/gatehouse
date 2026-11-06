"""A Florida lien file kept through the desk's API, with the facility's zone and the payments read from the simulator."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from desk.models import DelinquencyCase, LedgerSnapshot, LienEvidence

from .test_api import client, dana_case  # noqa: F401  (the fixture and helper are shared)

ALBANY = "fac_NSalbany001"


def today_in_orlando() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def snapshot_before(client, cid, delivered: date) -> int:  # noqa: F811
    """The desk's own ledger snapshot from the day before delivery, as the worker would have taken it."""
    with client.app.state.session_factory() as s:
        case = s.get(DelinquencyCase, cid)
        current = s.get(LedgerSnapshot, case.last_snapshot_id)
        old = LedgerSnapshot(lease_id=current.lease_id, total_due=current.total_due, prepaid=current.prepaid, raw=current.raw,
                             observed_at=datetime.combine(delivered - timedelta(days=1), datetime.min.time()) + timedelta(hours=15))
        s.add(old)
        s.commit()
        return old.id


def file_for(client, cid, delivered: date, newspaper_ok="yes"):  # noqa: F811
    deadline = delivered + timedelta(days=14)
    first = deadline + timedelta(days=1)
    sale = first + timedelta(days=15)

    def add(kind, facts, **kw):
        r = client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": kind, "facts": facts, "entered_by": "J. Ortiz",
                                                                 "document_name": f"{kind}.txt", "document_text": f"{kind} as kept", **kw})
        assert r.status_code == 200, r.text
        return r.json()

    due = (delivered - timedelta(days=30)).isoformat()
    n = add("notice", {"itemized_charges": [{"description": "Rent", "amount_cents": 21500, "due_on": due},
                                            {"description": "Late fee", "amount_cents": 2500, "due_on": due}],
                       "itemized_total_cents": 24000, "sum_due_since": due, "ledger_snapshot_id": snapshot_before(client, cid, delivered),
                       "property_description": "household goods and boxes", "description_matches_rental_agreement": "yes",
                       "demand_deadline": deadline.isoformat(), "sale_statement": True, "sale_date": sale.isoformat(), "sale_time": "10:00 AM",
                       "sale_place": "online auction", "owner_name": "Northstar Storage",
                       "owner_street_address": "1200 E Colonial Dr, Orlando FL", "owner_phone": "407-555-0100"})
    assert len(n["document_sha256"]) == 64
    nid = n["id"]
    add("mail_deposit", {"notice_id": nid, "deposited_on": delivered.isoformat(), "certificate_of_mailing": True,
                         "postage_prepaid": True, "address": "88 Pine St, Orlando FL", "address_basis": "rental agreement"})
    add("posting", {"notice_id": nid, "posted_on": delivered.isoformat(), "location": "unit"})
    for on in (first, first + timedelta(days=7)):
        add("publication", {"notice_id": nid, "newspaper": "Orlando Sentinel", "general_circulation_in_facility_area": newspaper_ok,
                            "circulation_source": "Florida Press Association listing", "published_on": on.isoformat(),
                            "sale_date": sale.isoformat(), "sale_time": "10:00 AM", "sale_place": "online auction",
                            "includes_property_description": True, "includes_facility_address": True,
                            "includes_tenant_name": True, "includes_sale_time_place_manner": True})
    for check, answer in (("military_disclosure_provision", "yes"), ("military_service", "no"), ("bankruptcy", "no"),
                          ("vehicle_or_watercraft", "no"), ("dispute", "no"), ("secured_party", "no")):
        add("legal_check", {"check": check, "answer": answer, "source": "rental agreement, PACER, UCC search",
                            "observed_on": delivered.isoformat()})
    return nid


def test_a_complete_florida_file_is_a_review_packet_with_its_basis_and_attribution(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    file_for(client, cid, today_in_orlando() - timedelta(days=40))
    lien = client.get(f"/api/cases/{cid}/lien").json()
    assert lien["state"] == "Review packet assembled", [f for f in lien["findings"] if f["status"] != "met"]
    assert lien["summary"] == "Listed evidence checks completed; sale authorization is outside this desk."
    assert lien["rule_version"]["label"] == "FL-r1" and "Not legal review" in lien["rule_version"]["review_note"]
    assert "CC BY 4.0" in lien["reference"]["attribution"] and lien["reference"]["published_summary"]["earliest_sale"] == "Day 29"
    assert client.get(f"/api/lien/evaluations/{lien['evaluation_id']}/replay").json()["same_result"] is True


def test_an_out_of_area_newspaper_keeps_the_file_in_review(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    file_for(client, cid, today_in_orlando() - timedelta(days=40), newspaper_ok="unknown")
    assert client.get(f"/api/cases/{cid}/lien").json()["state"] == "Counsel review required"


def test_a_payment_after_the_notice_reopens_review(client, sim):  # noqa: F811
    cid = dana_case(client)["case_id"]
    file_for(client, cid, today_in_orlando() - timedelta(days=40))
    sim.post("/sim/payments/offline", {"allocations": {"lease_WhitB1042": 5000}})
    assert client.get(f"/api/cases/{cid}/lien").json()["state"] == "Account changed: re-review"


def test_evidence_dated_in_the_future_is_refused_as_evidence(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    file_for(client, cid, today_in_orlando() - timedelta(days=5))   # the publications would be in the future
    lien = client.get(f"/api/cases/{cid}/lien").json()
    assert lien["state"] == "Counsel review required"
    assert any(f["requirement"] == "evidence dates" for f in lien["findings"])


def test_a_correction_supersedes_once_and_nothing_is_edited_or_deleted(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    nid = file_for(client, cid, today_in_orlando() - timedelta(days=40))
    mail = {"notice_id": nid, "deposited_on": "2026-10-01", "certificate_of_mailing": False, "postage_prepaid": True,
            "address": "88 Pine St, Orlando FL", "address_basis": "rental agreement"}
    post = lambda **kw: client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "mail_deposit", "entered_by": "J. Ortiz", **kw})  # noqa: E731
    bad = post(facts=mail).json()
    fixed = post(facts=mail | {"certificate_of_mailing": True}, supersedes=bad["id"])
    assert fixed.status_code == 200
    assert post(facts=mail, supersedes=bad["id"]).status_code == 422          # it was already corrected
    with client.app.state.session_factory() as s:
        assert s.get(LienEvidence, bad["id"]).facts["certificate_of_mailing"] is False   # the original is unchanged
        with pytest.raises(Exception, match="append-only"):
            s.execute(text("UPDATE lien_evidence SET document_text = 'rewritten' WHERE id = :i"), {"i": bad["id"]})
        s.rollback()
        with pytest.raises(Exception, match="append-only"):
            s.execute(text("DELETE FROM lien_evidence WHERE id = :i"), {"i": bad["id"]})
        s.rollback()


def test_evidence_must_name_a_notice_on_the_same_case(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    r = client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "posting", "entered_by": "J. Ortiz",
                                                             "facts": {"notice_id": 999, "posted_on": "2026-10-01", "location": "unit"}})
    assert r.status_code == 422 and "not a notice" in r.json()["detail"]


def test_unsupported_or_malformed_evidence_is_refused(client):  # noqa: F811
    cid = dana_case(client)["case_id"]
    assert client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "sent_flag", "entered_by": "J. Ortiz", "facts": {}}).status_code == 422
    assert client.post(f"/api/cases/{cid}/lien/evidence", json={"kind": "mail_deposit", "entered_by": "J. Ortiz",
                                                                "facts": {"notice_id": 1}}).status_code == 422


def test_a_new_york_case_goes_to_counsel(client):  # noqa: F811
    client.post(f"/api/facilities/{ALBANY}/sync")
    rows = client.get(f"/api/facilities/{ALBANY}/queue").json()["rows"]
    lien = client.get(f"/api/cases/{rows[0]['case_id']}/lien").json()
    assert lien["state"] == "Counsel review required" and lien["rule_version"] is None
    assert lien["reference"]["published_summary"]["state_code"] == "NY"
