"""The slice end to end through the desk's API, with the simulator running over HTTP."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from desk.adapter.cubby import CubbyAdapter
from desk.api import create_app
from desk.config import Settings

from .conftest import FULL_KEY, ORLANDO

TOKEN = "front-desk-token"


@pytest.fixture
def client(fms_url, sim):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY,
                        fms_timeout_seconds=0.5, reconcile_every_seconds=60, operator_token=TOKEN)
    app = create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 0.5), start_worker=False)
    with TestClient(app, headers={"X-Desk-Token": TOKEN}) as c:
        yield c


def dana_case(client) -> dict:
    assert client.post(f"/api/facilities/{ORLANDO}/sync").json()["cases"] >= 4
    rows = client.get(f"/api/facilities/{ORLANDO}/queue").json()["rows"]
    return next(r for r in rows if r["lease"] == "lease_WhitB1042")


def test_the_desk_refuses_calls_without_the_operator_token(client):
    assert client.get(f"/api/facilities/{ORLANDO}/queue", headers={"X-Desk-Token": "wrong"}).status_code == 401
    with TestClient(client.app) as bare:   # no token header at all
        assert bare.get(f"/api/facilities/{ORLANDO}/queue").status_code == 401


def test_a_desk_with_no_token_configured_refuses_everything(fms_url, sim):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY,
                        fms_timeout_seconds=0.5, reconcile_every_seconds=60, operator_token=None)
    with TestClient(create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 0.5), start_worker=False)) as c:
        assert c.get(f"/api/facilities/{ORLANDO}/queue").status_code == 503
        assert c.get("/api/health").status_code == 200


def test_queue_shows_what_is_owed_and_who_has_to_act(client):
    row = dana_case(client)
    assert row["total_due"] == 24000 and row["state"] == "open" and row["owner"] == "front desk"


def test_a_wrong_caller_learns_nothing_and_cannot_pay(client):
    row = dana_case(client)
    check = client.post(f"/api/cases/{row['case_id']}/identity",
                        json={"last_name": "Whitfield", "phone_last4": "1111", "unit_name": "B-1042"}).json()
    assert check["verified"] is False and "total_due" not in check
    r = client.post(f"/api/cases/{row['case_id']}/proposals", json={"identity_check_id": check["identity_check_id"], "amount": 24000})
    assert r.status_code == 409


def test_verified_payment_end_to_end_and_a_resend_charges_once(client, sim):
    row = dana_case(client)
    check = client.post(f"/api/cases/{row['case_id']}/identity",
                        json={"last_name": "Whitfield", "phone_last4": "0142", "unit_name": "B-1042"}).json()
    assert check["verified"] and check["total_due"] == 24000
    proposal = client.post(f"/api/cases/{row['case_id']}/proposals",
                           json={"identity_check_id": check["identity_check_id"], "amount": 24000}).json()
    assert proposal["card_last4"] == "4242" and "$240.00" in proposal["read_back"]
    confirm_url = f"/api/cases/{row['case_id']}/proposals/{proposal['proposal_id']}/confirm"
    first = client.post(confirm_url).json()
    again = client.post(confirm_url).json()
    assert first["state"] == "CONFIRMED" and again["id"] == first["id"]
    assert len(sim.payments("lease_WhitB1042")) == 1
    detail = client.get(f"/api/cases/{row['case_id']}").json()
    assert detail["state"] == "paid" and detail["ledger"]["total_due"] == 0
    kinds = [h["kind"] for h in detail["history"]]
    assert kinds[0] == "case opened" and "payment confirmed" in kinds


def test_identity_checks_lock_after_three_misses(client):
    cid = dana_case(client)["case_id"]
    wrong = {"last_name": "Whitfield", "phone_last4": "1111", "unit_name": "B-1042"}
    for _ in range(3):
        assert client.post(f"/api/cases/{cid}/identity", json=wrong).json()["verified"] is False
    right = {"last_name": "Whitfield", "phone_last4": "0142", "unit_name": "B-1042"}
    assert client.post(f"/api/cases/{cid}/identity", json=right).status_code == 429


def test_a_proposal_only_offers_what_the_fms_allows(client):
    cid = dana_case(client)["case_id"]
    check = client.post(f"/api/cases/{cid}/identity", json={"last_name": "Whitfield", "phone_last4": "0142", "unit_name": "B-1042"}).json()
    base = {"identity_check_id": check["identity_check_id"]}
    assert client.post(f"/api/cases/{cid}/proposals", json={**base, "amount": 99999}).status_code == 409
    another_tenants_card = client.post(f"/api/cases/{cid}/proposals", json={**base, "amount": 24000, "payment_method_id": "pm_MarcVisa1881"})
    assert another_tenants_card.status_code == 409 and "not on file" in another_tenants_card.json()["detail"]
    assert client.post(f"/api/cases/{cid}/proposals/made-up-id/confirm").status_code == 409


def test_a_proposal_cannot_outlive_the_identity_check(client):
    from datetime import timedelta

    from desk.models import IdentityCheck, PaymentProposal

    cid = dana_case(client)["case_id"]
    check = client.post(f"/api/cases/{cid}/identity", json={"last_name": "Whitfield", "phone_last4": "0142", "unit_name": "B-1042"}).json()
    proposal = client.post(f"/api/cases/{cid}/proposals", json={"identity_check_id": check["identity_check_id"], "amount": 24000}).json()
    with client.app.state.session_factory() as s:
        row = s.get(IdentityCheck, check["identity_check_id"])
        row.created_at -= timedelta(minutes=16)
        s.get(PaymentProposal, proposal["proposal_id"]).expires_at += timedelta(hours=1)   # even a proposal that has not expired
        s.commit()
    assert client.post(f"/api/cases/{cid}/proposals/{proposal['proposal_id']}/confirm").status_code == 409


def test_staff_can_release_a_held_payment_only_with_a_note(client, sim):
    cid = dana_case(client)["case_id"]
    sim.behave("lease_WhitB1042", "gateway_error")
    check = client.post(f"/api/cases/{cid}/identity", json={"last_name": "Whitfield", "phone_last4": "0142", "unit_name": "B-1042"}).json()
    proposal = client.post(f"/api/cases/{cid}/proposals", json={"identity_check_id": check["identity_check_id"], "amount": 24000}).json()
    held = client.post(f"/api/cases/{cid}/proposals/{proposal['proposal_id']}/confirm").json()
    for _ in range(4):
        client.post("/api/reconcile")
    assert client.get(f"/api/payments/{held['id']}").json()["state"] == "NEEDS_STAFF"
    assert client.post(f"/api/payments/{held['id']}/resolve", json={"charged": False, "note": "ok"}).status_code == 422
    cleared = client.post(f"/api/payments/{held['id']}/resolve",
                          json={"charged": False, "note": "No payment in the FMS or the processor portal, checked by J. Ortiz"}).json()
    assert cleared["state"] == "CLEARED_BY_STAFF" and "not charged" in cleared["tenant_message"]
    assert client.get(f"/api/cases/{cid}").json()["state"] == "open"


def test_a_database_set_up_for_one_fms_account_refuses_another(fms_url, sim, tmp_path):
    url = f"sqlite:///{tmp_path / 'desk.db'}"
    first = Settings(database_url=url, fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=0.5,
                     reconcile_every_seconds=60, operator_token=TOKEN)
    create_app(first, start_worker=False)
    other = Settings(database_url=url, fms_base_url=fms_url, fms_api_key="sim_org_harbor_full", fms_timeout_seconds=0.5,
                     reconcile_every_seconds=60, operator_token=TOKEN)
    with pytest.raises(RuntimeError, match="another FMS account"):
        create_app(other, start_worker=False)


def test_an_fms_outage_is_a_502_not_a_crash(fms_url, sim):
    from desk.adapter import FMSError

    class Down(CubbyAdapter):
        down = False

        def ledger(self, lease_provider_id):
            if self.down:
                raise FMSError("timeout")
            return super().ledger(lease_provider_id)

    down = Down(fms_url, FULL_KEY, 0.5)
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY,
                        fms_timeout_seconds=0.5, reconcile_every_seconds=60, operator_token=TOKEN)
    with TestClient(create_app(settings, down, start_worker=False), headers={"X-Desk-Token": TOKEN}) as c:
        cid = dana_case(c)["case_id"]
        check = c.post(f"/api/cases/{cid}/identity", json={"last_name": "Whitfield", "phone_last4": "0142", "unit_name": "B-1042"}).json()
        down.down = True
        r = c.post(f"/api/cases/{cid}/proposals", json={"identity_check_id": check["identity_check_id"], "amount": 24000})
        assert r.status_code == 502 and "did not answer" in r.json()["detail"]
