"""Lien sales against the FMS ledger: what opens, what closes it, and what never does."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from desk import demo
from desk.adapter.base import FMSError
from desk.cases import sync_facility
from desk.db import make_session_factory
from desk.lien_files import ensure_append_only
from desk.models import Facility, WorkItem
from desk.rules import import_stitch
from desk.sales import (AFTER_NOTICE, BALANCE_CHANGED, CANCELLED_BY, LISTED_PAID, OUTSIDE, SETTLED_REVIEW, AmbiguousTime,
                        reconcile_sales, sale_instant)

from .conftest import ORLANDO


def test_a_sale_time_that_names_no_single_moment_is_refused():
    assert sale_instant("2026-11-07T10:00:00", "America/New_York") == datetime(2026, 11, 7, 15, 0)
    with pytest.raises(AmbiguousTime):
        sale_instant("2026-11-01T01:30:00", "America/New_York")     # happens twice when the clocks go back
    with pytest.raises(AmbiguousTime):
        sale_instant("2026-03-08T02:30:00", "America/New_York")     # never happens when they go forward


def world(adapter, fms_url):
    factory = make_session_factory("sqlite:///:memory:")
    ensure_append_only(factory.kw["bind"])
    with factory() as s:
        import_stitch(s)
    demo.build(factory, adapter, fms_url, feed_url=fms_url)
    return factory


def items(s, **where):
    q = select(WorkItem).where(WorkItem.kind.in_((LISTED_PAID, BALANCE_CHANGED, AFTER_NOTICE, SETTLED_REVIEW, OUTSIDE)))
    for k, v in where.items():
        q = q.where(getattr(WorkItem, k) == v)
    return s.scalars(q.order_by(WorkItem.id)).all()


def sweep(s, adapter, fms_url):
    orlando = s.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
    return reconcile_sales(s, adapter, orlando, "local", fms_url)


def test_the_preview_world_opens_the_sale_still_listed_after_dana_paid(fms_url, sim, adapter):
    factory = world(adapter, fms_url)
    with factory() as s:
        dana = items(s, subject_id=items(s, kind=LISTED_PAID)[0].subject_id)
        kinds = sorted(i.kind for i in dana)
        assert kinds == sorted([LISTED_PAID, OUTSIDE])
        listed = next(i for i in dana if i.kind == LISTED_PAID)
        assert listed.subject_id.endswith("lease_WhitB1042") and listed.task_state == "created"
        assert "The FMS reports payments since the notice" in listed.problem and "$240.00" in listed.problem
        # Gina paid $50 after her notice was mailed; $115 is still owed, so a person and counsel decide
        gina = items(s, kind=AFTER_NOTICE)
        assert len(gina) == 1 and gina[0].subject_id.endswith("lease_OkfrE0505")
        assert "mailed, per the lien file" in gina[0].problem and "$115.00 is still owed" in gina[0].problem
    tasks = sim.post("/v1/tasks/search", {"where": {"unitId": "unit_WhitB1042"}}).json()["data"]["tasks"]
    assert len([t for t in tasks if t["title"].startswith("Cancel the lien sale")]) == 1


def test_a_completed_task_closes_nothing_and_only_the_fms_cancellation_closes_the_sale(fms_url, sim, adapter):
    factory = world(adapter, fms_url)
    with factory() as s:
        listed = items(s, kind=LISTED_PAID)[0]
        assert sim.post("/sim/tasks/complete", {"taskId": listed.task_id}).status_code == 200
        sweep(s, adapter, fms_url)
        s.refresh(listed)
        assert listed.state == "open" and listed.task_status == "COMPLETED"

        assert sim.post("/sim/auctions/cancel", {"leaseId": "lease_WhitB1042"}).status_code == 200
        sweep(s, adapter, fms_url)
        s.refresh(listed)
        assert listed.state == "closed" and listed.verification == CANCELLED_BY
        # the outside site still shows the sale: its own item stays open until its feed says otherwise
        outside = items(s, kind=OUTSIDE, subject_id=listed.subject_id)[0]
        assert outside.state == "open"

        assert sim.post("/sim/listings/withdraw", {"leaseId": "lease_WhitB1042"}).status_code == 200
        sweep(s, adapter, fms_url)
        s.refresh(outside)
        assert outside.state == "closed" and outside.verification == "Confirmed by the outside site's feed"


class LedgerDown:
    """The FMS adapter with its ledger read failing."""

    def __init__(self, adapter):
        self.adapter = adapter

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def ledger(self, lease_id):
        raise FMSError("/v1/ledgers/search: ReadTimeout")


def sale_for(sim, lease_id, days=2):
    today = datetime.now(ZoneInfo("America/New_York")).date()
    r = sim.post("/sim/auctions/schedule", {"facilityId": ORLANDO, "auctionTime": f"{today + timedelta(days=days)}T10:00:00",
                                            "leaseIds": [lease_id], "lienStartDate": (today - timedelta(days=20)).isoformat()})
    assert r.status_code == 200


def plain(adapter):
    factory = make_session_factory("sqlite:///:memory:")
    with factory() as s:
        sync_facility(s, adapter, ORLANDO)
    return factory


def test_a_payment_that_comes_back_moves_the_item_to_review_with_one_task(fms_url, sim, adapter):
    sale_for(sim, "lease_BellC0207")
    paid = sim.post("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}}).json()["data"]["payment"]
    factory = plain(adapter)
    with factory() as s:
        sweep(s, adapter, fms_url)
        item = items(s, kind=LISTED_PAID)[0]
        assert item.task_state == "created"
        assert sim.post("/sim/payments/return", {"paymentId": paid["paymentId"]}).status_code == 200
        sweep(s, adapter, fms_url)
        sweep(s, adapter, fms_url)
        s.refresh(item)
        assert item.state == "open" and item.kind == BALANCE_CHANGED
        assert "$185.00 owed" in item.problem and "Do not act on it" in item.observed["task_warning"]
        assert not items(s, kind=LISTED_PAID) and not items(s, kind=AFTER_NOTICE)
        # staff mark the old task done: the warning about it stays on the item
        assert sim.post("/sim/tasks/complete", {"taskId": item.task_id}).status_code == 200
        sweep(s, adapter, fms_url)
        s.refresh(item)
        assert item.task_status == "COMPLETED" and "Do not act on it" in item.observed["task_warning"]
        # a failed read adds its own note and leaves the warning about the old task in place
        sweep(s, LedgerDown(adapter), fms_url)
        s.refresh(item)
        assert "Not freshly verified" in item.note and "Do not act on it" in item.observed["task_warning"]
        sweep(s, adapter, fms_url)          # the next good reading clears the freshness note, never the warning
        s.refresh(item)
        assert not (item.note or "").startswith("Not freshly verified") and "Do not act on it" in item.observed["task_warning"]
    tasks = sim.post("/v1/tasks/search", {"where": {"unitId": "unit_BellC0207"}}).json()["data"]["tasks"]
    assert len(tasks) == 1


def test_nothing_owed_without_a_payment_is_not_called_a_tenant_payment(fms_url, sim, adapter):
    sale_for(sim, "lease_BellC0207")
    sim.post("/sim/charges", {"leaseId": "lease_BellC0207", "category": "CREDIT", "description": "Courtesy credit", "amount": -18500})
    factory = plain(adapter)
    with factory() as s:
        sweep(s, adapter, fms_url)
        item = items(s, kind=LISTED_PAID)[0]
        assert "No payment since the notice" in item.problem and "does not call it a tenant payment" in item.problem


def test_a_settled_sale_is_reviewed_without_a_claim_about_the_order(fms_url, sim, adapter):
    sale_for(sim, "lease_BellC0207")
    sim.post("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}})
    assert sim.post("/sim/auctions/settle", {"leaseId": "lease_BellC0207", "outcome": "SOLD", "bidAmount": 40000,
                                             "winningBidderId": "cust_MarcBell01", "force": True}).status_code == 200
    factory = plain(adapter)
    with factory() as s:
        sweep(s, adapter, fms_url)
        item = items(s, kind=SETTLED_REVIEW)[0]
        assert "makes no claim about the order" in item.problem and "A bidder's payment is not the tenant's" in item.problem
        assert not items(s, kind=LISTED_PAID)


def test_a_failed_read_closes_nothing_and_says_so(fms_url, sim, adapter):
    factory = world(adapter, fms_url)

    class Broken:
        def __getattr__(self, name):
            return getattr(adapter, name)

        def ledger(self, lease_id):
            raise FMSError("/v1/ledgers/search: ReadTimeout")

    with factory() as s:
        before = {i.id: i.state for i in items(s)}
        sim.post("/sim/auctions/cancel", {"leaseId": "lease_WhitB1042"})
        result = sweep(s, Broken(), fms_url)
        # the cancellation is read from auctions/search, so Dana's item closes on its own evidence; Gina's cannot be decided
        gina = items(s, kind=AFTER_NOTICE)[0]
        assert gina.state == "open" and "Not freshly verified" in gina.note
        assert result.failures
        assert all(before[i.id] == "open" for i in items(s) if i.id in before)


def test_gatehouse_never_cancels_a_sale_itself(fms_url, sim, adapter):
    world(adapter, fms_url)
    entries = httpx.get(f"{fms_url}/sim/auctions/truth", params={"facilityId": ORLANDO}).json()["data"]["entries"]
    assert entries and all(e["status"] == "LISTED" and e["changedBy"] != "api" for e in entries)


def test_a_sale_that_settles_under_an_open_item_turns_that_item_into_the_review(fms_url, sim, adapter):
    sale_for(sim, "lease_BellC0207")
    sim.post("/sim/payments/offline", {"allocations": {"lease_BellC0207": 18500}})
    factory = plain(adapter)
    with factory() as s:
        sweep(s, adapter, fms_url)
        item = items(s, kind=LISTED_PAID)[0]
        assert sim.post("/sim/auctions/settle", {"leaseId": "lease_BellC0207", "outcome": "SOLD", "bidAmount": 40000,
                                                 "winningBidderId": "cust_MarcBell01", "force": True}).status_code == 200
        sweep(s, adapter, fms_url)
        s.refresh(item)
        assert item.kind == SETTLED_REVIEW and item.state == "open" and "earlier reading" in item.observed["task_warning"]
        assert [i.id for i in items(s, state="open")] == [item.id]
        sweep(s, adapter, fms_url)          # a later clean pass keeps the warning while the old task exists
        s.refresh(item)
        assert "earlier reading" in item.observed["task_warning"] and item.task_status == "OPEN"
        assert sim.post("/sim/auctions/bidder-paid", {"leaseId": "lease_BellC0207"}).status_code == 200
        sweep(s, adapter, fms_url)          # the warning follows the sale's status
        s.refresh(item)
        assert "sold, and the bidder paid" in item.observed["task_warning"]


def test_an_unreadable_outside_feed_is_said_on_its_item(fms_url, sim, adapter):
    factory = world(adapter, fms_url)
    with factory() as s:
        orlando = s.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
        result = reconcile_sales(s, adapter, orlando, "local", "http://127.0.0.1:9")
        outside = items(s, kind=OUTSIDE)
        assert outside and all(i.state == "open" and "outside site's feed could not be read" in i.note for i in outside)
        assert any("outside listings" in f for f in result.failures)


def test_a_malformed_auction_answer_is_an_fms_error():
    from desk.adapter.cubby import CubbyAdapter
    answer = {"status": 200, "data": {"auctions": ["not an auction"]}}
    broken = CubbyAdapter("http://fms.invalid", "key", 1.0, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answer)))
    with pytest.raises(FMSError):
        broken.auction_entries(ORLANDO)


def test_with_no_notice_date_no_payment_is_said_to_follow_the_notice():
    from desk.adapter.base import ProviderAuctionEntry
    from desk.sales import SaleFacts, assess
    entry = ProviderAuctionEntry("auction_x", "2026-11-08T10:00:00", "lease_x", "unit_x", None, "LISTED")
    found = assess(SaleFacts(entry, 0, [], None, "the lien start date the FMS reports", "2026-11-08T15:00:00", None, "X-1"))
    assert found.kind == LISTED_PAID and "No notice date is known" in found.problem and "since the notice" not in found.problem


def test_the_outside_feed_still_decides_when_the_ledger_cannot_be_read(fms_url, sim, adapter):
    factory = world(adapter, fms_url)

    class LedgerDown:
        def __getattr__(self, name):
            return getattr(adapter, name)

        def ledger(self, lease_id):
            raise FMSError("/v1/ledgers/search: ReadTimeout")

    with factory() as s:
        dana = items(s, kind=OUTSIDE)
        assert sim.post("/sim/listings/withdraw", {"leaseId": "lease_WhitB1042"}).status_code == 200
        sweep(s, LedgerDown(), fms_url)
        closed = [i for i in items(s, kind=OUTSIDE, state="closed") if i.subject_id.endswith("lease_WhitB1042")]
        assert dana and len(closed) == 1 and closed[0].verification == "Confirmed by the outside site's feed"


def test_a_listing_missing_from_the_feed_is_not_taken_as_withdrawn(fms_url, sim, adapter):
    from desk.adapter.base import ProviderAuctionEntry
    from desk.sales import SalesPass, _outside
    factory = world(adapter, fms_url)
    with factory() as s:
        orlando = s.scalar(select(Facility).where(Facility.provider_id == ORLANDO))
        item = items(s, kind=OUTSIDE)[0]
        o = item.observed
        _outside(s, orlando, "local", ProviderAuctionEntry(o["auction_id"], o["auction_time"], o["lease_id"], None, None, "LISTED"),
                 [], SalesPass())      # the feed answers, but without this listing
        s.commit()
        assert item.state == "open" and "not proof it was withdrawn" in item.note


def test_an_outside_listing_is_checked_even_when_its_sale_left_the_fms_list(fms_url, sim, adapter):
    factory = world(adapter, fms_url)

    class NoSales:
        def __getattr__(self, name):
            return getattr(adapter, name)

        def auction_entries(self, facility_id):
            return []          # the FMS no longer lists the sale at all

    with factory() as s:
        assert sim.post("/sim/listings/withdraw", {"leaseId": "lease_WhitB1042"}).status_code == 200
        sweep(s, NoSales(), fms_url)
        closed = [i for i in items(s, kind=OUTSIDE, state="closed") if i.subject_id.endswith("lease_WhitB1042")]
        still = [i for i in items(s, kind=OUTSIDE, state="open") if i.subject_id.endswith("lease_OkfrE0505")]
        assert len(closed) == 1 and still and still[0].note is None


def test_the_outside_feed_still_decides_when_the_fms_sale_list_cannot_be_read(fms_url, sim, adapter):
    factory = world(adapter, fms_url)

    class SalesDown:
        def __getattr__(self, name):
            return getattr(adapter, name)

        def auction_entries(self, facility_id):
            raise FMSError("/v1/auctions/search: ReadTimeout")

    with factory() as s:
        assert sim.post("/sim/listings/withdraw", {"leaseId": "lease_WhitB1042"}).status_code == 200
        result = sweep(s, SalesDown(), fms_url)
        closed = [i for i in items(s, kind=OUTSIDE, state="closed") if i.subject_id.endswith("lease_WhitB1042")]
        listed = items(s, kind=LISTED_PAID)[0]
        assert len(closed) == 1 and listed.state == "open" and "Not freshly verified" in listed.note and result.failures
