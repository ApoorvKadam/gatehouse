"""The preview's synthetic world, built through the real flows against the simulator."""
from __future__ import annotations

import httpx
from sqlalchemy import select

from desk import demo
from desk.db import make_session_factory
from desk.lien_files import ensure_append_only
from desk.access import reconcile_access
from desk.models import DelinquencyCase, Facility, LienEvaluation, PaymentCommand, WorkItem
from desk.payments import PaymentService
from desk.rules import import_stitch


def test_the_preview_world_shows_each_failure_the_way_the_desk_handled_it(fms_url, sim, adapter):
    factory = make_session_factory("sqlite:///:memory:")
    ensure_append_only(factory.kw["bind"])
    with factory() as s:
        import_stitch(s)
    assert demo.build(factory, adapter, fms_url) is True
    with factory() as s:
        dana = demo.case_of(s, "lease_WhitB1042")
        command = s.scalar(select(PaymentCommand).where(PaymentCommand.case_id == dana.id))
        # charged once at the FMS, answer lost, held for a person with the FMS payment suggested
        assert command.state == "NEEDS_STAFF" and command.suggested_payment_id
        assert [p["status"] for p in sim.payments("lease_WhitB1042")] == ["SUCCEEDED"]

        marcus = s.scalar(select(PaymentCommand).where(PaymentCommand.lease_id == demo.case_of(s, "lease_BellC0207").lease_id))
        assert marcus.state == "RETURNED" and s.get(DelinquencyCase, marcus.case_id).state == "reopened"

        gina = demo.case_of(s, "lease_OkfrE0505")
        states = s.scalars(select(LienEvaluation.state).where(LienEvaluation.case_id == gina.id).order_by(LienEvaluation.id)).all()
        assert states == ["Review packet assembled", "Account changed: re-review"]

        teo = demo.case_of(s, "lease_VargD0310")
        assert s.scalar(select(LienEvaluation.state).where(LienEvaluation.case_id == teo.id)) == "Counsel review required"

    # a database that already holds the world is left alone
    assert demo.build(factory, adapter, fms_url) is False


def world(factory, adapter, fms_url) -> dict:
    """What a visitor would see, plus the simulator's own record of every seeded lease."""
    with factory() as s:
        out = {}
        for lease in ("lease_WhitB1042", "lease_BellC0207", "lease_OkfrE0505", "lease_VargD0310"):
            case = demo.case_of(s, lease)
            truth = httpx.get(f"{fms_url}/sim/truth", params={"leaseId": lease}).json()["data"]
            for p in truth["payments"]:
                del p["createdAt"]   # wall-clock time of each boot; everything else must match
            out[lease] = {
                "case": case.state,
                "payments": [(c.state, c.provider_payment_id, c.suggested_payment_id)
                             for c in s.scalars(select(PaymentCommand).where(PaymentCommand.case_id == case.id))],
                "lien": [e.state for e in s.scalars(select(LienEvaluation).where(LienEvaluation.case_id == case.id).order_by(LienEvaluation.id))],
                "truth": truth,
            }
        out["access"] = sorted((w.kind, w.subject_type, w.subject_id, w.state, w.task_state, w.task_status)
                               for w in s.scalars(select(WorkItem)))
        for fac in ("fac_NSorlando01", "fac_NSalbany001"):
            t = httpx.get(f"{fms_url}/sim/access/truth", params={"facilityId": fac}).json()["data"]
            out[fac] = {"codes": t["codes"], "leases": t["leases"], "tasks": sorted((x["title"], x["status"]) for x in t["tasks"])}
        return out


def fresh(adapter, fms_url):
    factory = make_session_factory("sqlite:///:memory:")
    ensure_append_only(factory.kw["bind"])
    with factory() as s:
        import_stitch(s)
    demo.build(factory, adapter, fms_url)
    return factory


def test_two_boots_give_the_same_world_and_the_worker_cannot_move_it(fms_url, sim, adapter):
    first = world(fresh(adapter, fms_url), adapter, fms_url)
    factory = fresh(adapter, fms_url)
    second = world(factory, adapter, fms_url)
    assert second == first
    for _ in range(5):   # the worker's passes read the FMS the way the hosted preview's worker does
        with factory() as s:
            PaymentService(s, adapter).reconcile_all()
            for facility in s.scalars(select(Facility)).all():
                reconcile_access(s, adapter, facility, "local")
    assert world(factory, adapter, fms_url) == first


def test_the_truth_reader_settles_nothing(fms_url, sim):
    sim.behave("lease_WhitB1042", "pending_then_succeed", lag=1)
    sim.post("/v1/leases/make-payment", {"leaseId": "lease_WhitB1042", "paymentMethodId": "pm_DanaVisa4242", "amount": 24000})
    for _ in range(3):
        truth = httpx.get(f"{fms_url}/sim/truth", params={"leaseId": "lease_WhitB1042"}).json()["data"]
        assert [p["status"] for p in truth["payments"]] == ["PENDING"] and truth["totalDue"] == 24000
    # Payments Search is the FMS's own read, and it is the one that moves a pending payment along
    assert [p["status"] for p in sim.payments("lease_WhitB1042")] == ["PENDING"]
    assert [p["status"] for p in sim.payments("lease_WhitB1042")] == ["SUCCEEDED"]


def test_readiness_turns_false_when_every_worker_pass_fails(adapter):
    import time

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from desk.worker import Worker

    # a database without Gatehouse's tables: every pass fails on its first query
    broken = sessionmaker(create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}))
    worker = Worker(broken, adapter, every_seconds=0.05, stale_after=0.3)
    worker.start()
    try:
        time.sleep(0.6)
        assert worker.last_error == "OperationalError" and not worker.alive()
    finally:
        worker.stop()


def test_a_worker_whose_fms_reads_fail_is_not_ready(fms_url, sim, adapter):
    import time

    from desk.adapter.cubby import CubbyAdapter
    from desk.cases import sync_facility
    from desk.worker import Worker

    factory = make_session_factory("sqlite:///:memory:")
    with factory() as s:
        sync_facility(s, adapter, "fac_NSorlando01")
    # the facility list could still answer while every payment and ledger read fails; here the FMS is gone entirely
    gone = CubbyAdapter("http://127.0.0.1:9", "sim_org_northstar_full", timeout_seconds=0.2)
    worker = Worker(factory, gone, every_seconds=0.05, stale_after=5)
    assert not worker.alive()            # not started: no pass yet
    worker.start()
    try:
        time.sleep(1.0)
        assert worker.last_error and "FMS read(s) failed" in worker.last_error and not worker.alive()
    finally:
        worker.stop()
    good = Worker(factory, adapter, every_seconds=60)
    good.start()
    try:
        for _ in range(50):
            if good.alive():
                break
            time.sleep(0.1)
        assert good.alive() and good.last_error is None   # the first pass runs at once
    finally:
        good.stop()
