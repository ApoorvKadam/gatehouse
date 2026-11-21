"""Staddle's own simulated auction platform: each step on its own, every forward step checked inside the gate write, a winning bid that is
not a sale, buyer money that is not a tenant payment, a cancellation recorded first and counted only when read back, a tombstone that
nothing revives, and a lost answer settled by reading the platform, so every request has one effect."""
import os
import sqlite3
import threading
from datetime import date, timedelta

import pytest

from core import auction, liens
from core import delinquency as col
from core.auction import AuctionSite, drive
from core.books import at_local
from core.renting import Crash, copy_mark
from core.store import Store
from desk.front_desk import FrontDesk
from tests.test_core_delinquency import world
from tests.test_core_liens import ADDRESS, NOW, a_dispute, approve, complete, do, owed, prepared, record, run, staff, state_of, the_case
from tests.test_core_renting_pay import desk_rental

BIDDER = "bidder:buyer-1"
LATER = NOW + timedelta(hours=1)


def approved(s):
    """A case whose packet is complete and which the manager approved: the one state an auction may start from."""
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed
    return store, t, lease


def step(store, s, t, lease, site, action, source="staff:office_manager", now=NOW, **kw):
    """One gate command, then the platform work it asks for."""
    d = run(store, s, now, action=action, tenant=t, lease=lease, source=source, **kw)
    drive(store, s, site, now)
    return d


def ok(store, s, t, lease, site, action, **kw):
    d = step(store, s, t, lease, site, action, **kw)
    assert d.allowed, d.check
    return d


def the_auction(store, s):
    return liens.state(store.books(s)).auctions[the_case(store, s).id]



def pays_in_full(store, s, t, at=LATER):
    do(store, s, lambda led: (led.receive_payment(t, 12000, at), liens.recheck(led, t, at)))


def listing(site, s, store):
    """The platform's own listing for the case, by its reference."""
    (found,) = [x for x in site.listings.values() if x["ref"] == f"{copy_mark(store, s)}:{the_case(store, s).id}"]
    return found


def journey(store, s, t, lease, site, upto):
    """List, open, bid, close, name the winner, the buyer pays, make the sale final: as far as `upto` (the status reached)."""
    steps = [("list_lien_auction", "listed", {}, "staff:office_manager"),
             ("open_lien_bidding", "bidding", {}, "staff:office_manager"),
             ("place_lien_bid", "bidding", {"form": {"cents": 20000}}, BIDDER),
             ("close_lien_bidding", "bidding_closed", {}, "staff:office_manager"),
             ("declare_lien_winner", "won", {}, "staff:office_manager"),
             ("take_buyer_payment", "buyer_paid", {"form": {"cents": 20000}}, BIDDER),
             ("complete_lien_sale", "sold", {}, "staff:manager")]
    for action, status, kw, source in steps:
        ok(store, s, t, lease, site, action, source=source, **kw)
        if status == upto and action != "place_lien_bid":
            break
    assert the_auction(store, s).status == upto


# listing ------------------------------------------------------------------------------------------------------------------
def test_listing_is_refused_without_a_standing_approval_a_legal_stop_or_a_hold_and_allowed_when_all_is_clear():
    site = AuctionSite()
    s = "visitor:a1"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    list_ = lambda: run(store, s, action="list_lien_auction", tenant=t, lease=lease, source="staff:office_manager")
    never = list_()
    assert not never.allowed and never.check == "no manager approval stands for the file as it is now; a manager approves it again"
    assert approve(store, s, t, lease).allowed
    assert record(store, s, t, lease, "legal_check", {"check": "military_service", "answer": "no", "source": "the federal service index",
                                                      "observed_on": "2026-03-12"}).allowed                            # a change voids the approval
    voided = list_()
    assert not voided.allowed and voided.check == "no manager approval stands for the file as it is now; a manager approves it again"
    assert approve(store, s, t, lease).allowed
    assert staff(store, s, t, lease, "record_lien_stop", {"kind": "bankruptcy", "detail": "case 26-1", "source": "the court index"}).allowed
    stopped = list_()
    assert not stopped.allowed and "a bankruptcy is on the file as a legal stop" in stopped.check and "no approval overrides it" in stopped.check
    assert liens.state(store.books(s)).auctions == {} and site.calls == []                                             # nothing was asked of the platform

    held = "visitor:a2"
    store2, t2, lease2 = approved(held)
    a_dispute(store2, held, t2, lease2)
    refused = run(store2, held, action="list_lien_auction", tenant=t2, lease=lease2, source="staff:office_manager")
    assert not refused.allowed and refused.check == "a dispute on this tenancy is open"

    clear = "visitor:a3"
    store3, t3, lease3 = approved(clear)
    assert run(store3, clear, action="list_lien_auction", tenant=t3, lease=lease3, source="bidder:x").check == "only staff take a lien step"
    assert ok(store3, clear, t3, lease3, site, "list_lien_auction").check.startswith("the auction is listed on the platform")
    assert the_auction(store3, clear).status == "listed" and len(site.listings) == 1


def test_a_hold_that_arrives_after_listing_stops_each_later_step_but_cancelling_stays_open():
    site, s = AuctionSite(), "visitor:a4"
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    a_dispute(store, s, t, lease)
    for action in ("open_lien_bidding", "close_lien_bidding", "declare_lien_winner"):
        d = run(store, s, action=action, tenant=t, lease=lease, source="staff:manager")
        assert not d.allowed and d.check == "a dispute on this tenancy is open"
    cancel = step(store, s, t, lease, site, "cancel_lien_auction", reason="the tenant disputes the charge")
    assert cancel.allowed and the_case(store, s).closed["kind"] == "cancelled"


# the steps are separate and in order --------------------------------------------------------------------------------------
NEXT = {"new": {"list_lien_auction"}, "listed": {"open_lien_bidding"}, "bidding": {"place_lien_bid", "close_lien_bidding"},
        "bidding_closed": {"declare_lien_winner"}, "won": {"take_buyer_payment"}, "buyer_paid": {"complete_lien_sale"}, "sold": set()}
SOURCES = {"place_lien_bid": BIDDER, "take_buyer_payment": BIDDER, "complete_lien_sale": "staff:manager"}


def test_the_steps_go_in_order_and_each_out_of_order_step_is_refused_with_its_reason():
    site, s = AuctionSite(), "visitor:a5"
    store, t, lease = approved(s)
    for status in ("new", "listed", "bidding", "bidding_closed", "won", "buyer_paid", "sold"):
        for action in NEXT["new"] | {x for v in NEXT.values() for x in v}:
            if action in NEXT[status]:
                continue
            d = run(store, s, action=action, tenant=t, lease=lease, source=SOURCES.get(action, "staff:office_manager"), form={"cents": 20000})
            assert not d.allowed, (status, action)
            expected = "no auction is listed for this case yet" if status == "new" else (
                "this episode already has an auction" if action == "list_lien_auction" else auction.ORDER_WHY[auction.STEP_OF[action]])
            assert expected in d.check, (status, action, d.check)
        if status == "bidding":                 # a bid keeps bidding open, so a bid goes in before it is closed
            ok(store, s, t, lease, site, "place_lien_bid", source=BIDDER, form={"cents": 20000})
        if status != "sold":
            action = "close_lien_bidding" if status == "bidding" else next(iter(NEXT[status]))
            ok(store, s, t, lease, site, action, source=SOURCES.get(action, "staff:office_manager"), form={"cents": 20000})
    case = the_case(store, s)
    assert the_auction(store, s).status == "sold" and case.closed is None and state_of(store, s)[0] == "sold"
    assert listing(site, s, store)["status"] == "sold"


def test_who_may_take_a_step_a_bid_must_beat_the_top_bid_and_only_the_winner_pays_the_winning_bid():
    site, s = AuctionSite(), "visitor:a6"
    store, t, lease = approved(s)
    journey(store, s, t, lease, site, "listed")
    ok(store, s, t, lease, site, "open_lien_bidding")
    assert run(store, s, action="place_lien_bid", tenant=t, lease=lease, source="staff:manager", form={"cents": 100}).check == "only a bidder on the auction takes that step"
    assert run(store, s, action="place_lien_bid", tenant=t, lease=lease, source="bidder:", form={"cents": 100}).check == "only a bidder on the auction takes that step"
    assert run(store, s, action="place_lien_bid", tenant=t, lease=lease, source=BIDDER, form={"cents": 0}).check == "an amount in cents is needed"
    ok(store, s, t, lease, site, "place_lien_bid", source=BIDDER, form={"cents": 15000})
    low = run(store, s, action="place_lien_bid", tenant=t, lease=lease, source="bidder:buyer-2", form={"cents": 15000})
    assert not low.allowed and low.check == "a bid must be higher than the top bid, $150.00"
    ok(store, s, t, lease, site, "place_lien_bid", source="bidder:buyer-2", form={"cents": 18000})
    ok(store, s, t, lease, site, "close_lien_bidding")
    ok(store, s, t, lease, site, "declare_lien_winner")
    assert the_auction(store, s).winner["bidder"] == "buyer-2" and the_auction(store, s).winner["cents"] == 18000
    wrong = run(store, s, action="take_buyer_payment", tenant=t, lease=lease, source=BIDDER, form={"cents": 18000})
    assert not wrong.allowed and wrong.check.startswith("only the winning bidder pays")
    short = run(store, s, action="take_buyer_payment", tenant=t, lease=lease, source="bidder:buyer-2", form={"cents": 15000})
    assert not short.allowed and short.check.startswith("only the winning bidder pays") and "$180.00" in short.check
    ok(store, s, t, lease, site, "take_buyer_payment", source="bidder:buyer-2", form={"cents": 18000})
    assert run(store, s, action="complete_lien_sale", tenant=t, lease=lease, source="staff:office_manager").check == "only a manager makes a sale final"


def test_a_winning_bid_alone_never_marks_the_unit_sold_and_buyer_money_never_touches_the_tenants_account():
    site, s = AuctionSite(), "visitor:a7"
    store, t, lease = approved(s)
    before_owed, books, journal = owed(store, s, t), store.books(s), store.journal(s)
    journey(store, s, t, lease, site, "won")
    assert the_auction(store, s).status == "won" and listing(site, s, store)["status"] == "won"
    case = the_case(store, s)
    assert case.closed is None and store.books(s).leases[lease].ended is None and state_of(store, s)[0] != "sold"
    journey_pay = ok(store, s, t, lease, site, "take_buyer_payment", source=BIDDER, form={"cents": 20000})
    assert journey_pay.allowed and the_auction(store, s).status == "buyer_paid" and the_auction(store, s).buyer_paid_cents == 20000
    assert listing(site, s, store)["status"] == "paid"                                  # paid for, still not sold
    after = store.books(s)
    assert owed(store, s, t) == before_owed == 12000
    assert after.payments == books.payments and after.receivable(lease) == books.receivable(lease)
    assert [x for x in store.journal(s) if x not in journal] == []                       # not one journal line for the buyer's money
    ok(store, s, t, lease, site, "complete_lien_sale", source="staff:manager")
    assert the_auction(store, s).status == "sold" and owed(store, s, t) == 12000 and store.books(s).payments == books.payments
    assert the_case(store, s).closed is None                                             # applying the sale money to the debt is a later step


# a payment after the notice -----------------------------------------------------------------------------------------------
def test_a_full_payment_after_bidding_closed_stops_the_sale_and_starts_cancellation_that_closes_only_on_read_back():
    site, s = AuctionSite(hide={"cancelled"}), "visitor:a8"
    store, t, lease = approved(s)
    journey(store, s, t, lease, site, "buyer_paid")
    pays_in_full(store, s, t)
    case, a = the_case(store, s), the_auction(store, s)
    assert case.closed is None and a.cancel and not a.cancel["confirmed"] and a.cancel["closes"]["kind"] == "cured"
    assert state_of(store, s, LATER)[0] == "cancellation waiting"
    sale = run(store, s, LATER, action="complete_lien_sale", tenant=t, lease=lease, source="staff:manager")
    assert not sale.allowed and sale.check == "this auction is being cancelled because the claim was paid in full"
    assert listing(site, s, store)["status"] == "paid" and "cancel" not in " ".join(site.calls)        # recorded first, not yet sent
    drive(store, s, site, LATER)
    assert the_case(store, s).closed is None and state_of(store, s, LATER)[0] == "cancellation waiting"  # the platform does not show it yet
    assert [t_.kind for t_ in col.open_tasks(store.books(s))] == ["auction needs staff"]
    site.hide = set()
    drive(store, s, site, LATER)
    case = the_case(store, s)
    assert case.closed["kind"] == "cured" and case.closed["why"] == "cured: the claim was paid in full" and state_of(store, s, LATER)[0] == "closed"
    assert listing(site, s, store)["status"] == "cancelled" and the_auction(store, s).status == "cancelled"
    assert owed(store, s, t) == 0


def test_a_partial_payment_freezes_the_sale_for_staff_and_a_decision_to_close_takes_the_listing_down_first():
    site, s = AuctionSite(), "visitor:a9"
    store, t, lease = approved(s)
    journey(store, s, t, lease, site, "bidding_closed")
    do(store, s, lambda led: (led.receive_payment(t, 1000, LATER), liens.recheck(led, t, LATER)))
    case = the_case(store, s)
    assert case.closed is None and state_of(store, s, LATER)[0] == "review required" and the_auction(store, s).cancel is None
    for action in ("declare_lien_winner", "list_lien_auction"):
        d = run(store, s, LATER, action=action, tenant=t, lease=lease, source="staff:manager")
        assert not d.allowed and d.check.startswith("the file is waiting for staff to decide: a $10.00 payment")
    resume = run(store, s, LATER, action="decide_lien_case", tenant=t, lease=lease, source="staff:office_manager", reason="carry on",
                 form={"outcome": "resume", "fingerprint": liens.fingerprint(store.books(s), case)})
    assert not resume.allowed and resume.check.startswith("an auction was listed before this change; cancel it first")
    close = run(store, s, LATER, action="decide_lien_case", tenant=t, lease=lease, source="staff:office_manager", reason="the tenant is paying in parts",
                form={"outcome": "close"})
    assert close.allowed and the_case(store, s).closed is None and state_of(store, s, LATER)[0] == "cancellation waiting"   # not closed yet
    drive(store, s, site, LATER)
    case = the_case(store, s)
    assert case.closed["kind"] == "staff" and case.closed["why"] == "the tenant is paying in parts" and listing(site, s, store)["status"] == "cancelled"


# cancelling ---------------------------------------------------------------------------------------------------------------
def test_a_cancellation_is_recorded_first_sent_then_read_back_and_the_case_shows_cancelled_only_after_the_read_back():
    site, s = AuctionSite(hide={"cancelled"}), "visitor:b1"
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    assert run(store, s, action="cancel_lien_auction", tenant=t, lease=lease, source="staff:manager").check == "a cancellation is recorded with a reason"
    d = run(store, s, action="cancel_lien_auction", tenant=t, lease=lease, source="staff:manager", reason="the owner withdrew the sale")
    assert d.allowed
    assert state_of(store, s)[0] == "cancellation waiting" and the_case(store, s).closed is None and listing(site, s, store)["status"] == "listed"
    again = run(store, s, action="cancel_lien_auction", tenant=t, lease=lease, source="staff:manager", reason="again")
    assert not again.allowed and again.check.startswith("the cancellation of this auction is already recorded")
    drive(store, s, site, NOW)
    assert listing(site, s, store)["status"] == "cancelled"                              # the platform did it; its read-back does not show it yet
    assert state_of(store, s)[0] == "cancellation waiting" and the_case(store, s).closed is None
    site.hide = set()
    drive(store, s, site, NOW)
    case = the_case(store, s)
    assert (case.closed["kind"], case.closed["why"]) == ("cancelled", "the owner withdrew the sale") and state_of(store, s)[0] == "closed"
    kinds = [e.kind for e in store.stream_events(s) if e.kind.startswith("lien_case.auction_")]
    assert kinds.index("lien_case.auction_requested") < kinds.index("lien_case.auction_answered") < kinds.index("lien_case.auction_cancel_confirmed")
    ops = [o.step for o in sorted(the_auction(store, s).ops.values(), key=lambda o: o.n)]
    assert ops == ["list", "cancel", "read", "read"]


def test_a_lost_answer_to_a_cancellation_is_settled_by_reading_the_platform_and_cancels_once():
    site, s = AuctionSite(crash_after={"cancel"}), "visitor:b2"
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    assert run(store, s, action="cancel_lien_auction", tenant=t, lease=lease, source="staff:manager", reason="withdrawn").allowed
    with pytest.raises(Crash):
        drive(store, s, site, NOW)
    assert listing(site, s, store)["status"] == "cancelled" and state_of(store, s)[0] == "cancellation waiting"     # no answer was recorded
    drive(store, s, site, NOW)
    assert the_case(store, s).closed["kind"] == "cancelled" and [c.rsplit(":", 1)[1] for c in site.calls] == ["list", "cancel"]
    assert the_auction(store, s).ops[f"{the_case(store, s).id}:2:cancel"].how == "found"


def test_nothing_revives_a_cancelled_auction_and_a_new_lien_needs_a_new_episode():
    site, s = AuctionSite(), "visitor:b3"
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    assert run(store, s, action="cancel_lien_auction", tenant=t, lease=lease, source="staff:manager", reason="withdrawn").allowed
    for action in ("open_lien_bidding", "list_lien_auction"):                          # recorded but not sent: already out of reach
        d = run(store, s, action=action, tenant=t, lease=lease, source="staff:manager")
        assert not d.allowed and d.check == "this auction is being cancelled"
    drive(store, s, site, NOW)
    first = the_case(store, s)
    assert first.closed["kind"] == "cancelled" and the_auction(store, s).status == "cancelled"
    assert run(store, s, action="list_lien_auction", tenant=t, lease=lease, source="staff:manager").check == "no lien case is open on this unit"
    # a payment that comes back after the cancellation reopens the debt and revives nothing
    paid = do(store, s, lambda led: led.receive_payment(t, 12000, LATER))
    do(store, s, lambda led: (led.return_payment(LATER + timedelta(hours=1), paid), liens.recheck(led, t, LATER + timedelta(hours=1))))
    assert owed(store, s, t) == 12000 and the_auction(store, s).status == "cancelled" and listing(site, s, store)["status"] == "cancelled"
    assert liens.active_case(store.books(s), lease) is None and len(the_auction(store, s).ops) == 3        # list, cancel, read: nothing was asked of the platform again
    # the debt is still owed, so staff open a new episode: a new case, with its own packet, approval and listing
    assert run(store, s, action="open_lien_case", tenant=t, lease=lease, source="staff:manager").allowed
    second = liens.active_case(store.books(s), lease)
    assert (second.id, second.episode) == ("LC0002", 2)
    assert staff(store, s, t, lease, "record_lien_address", ADDRESS).allowed and staff(store, s, t, lease, "prepare_lien_notice").allowed
    complete(store, s, t, lease)
    books = store.books(s)
    fp = liens.fingerprint(books, liens.active_case(books, lease))
    assert run(store, s, action="approve_lien_case", tenant=t, lease=lease, source="staff:manager", form={"fingerprint": fp}).allowed
    ok(store, s, t, lease, site, "list_lien_auction")
    new = liens.state(store.books(s)).auctions[second.id]
    assert new.status == "listed" and new.listing != liens.state(store.books(s)).auctions[first.id].listing
    assert liens.state(store.books(s)).auctions[first.id].status == "cancelled"


# lost answers --------------------------------------------------------------------------------------------------------------
def test_a_late_sold_answer_after_the_cancellation_changes_nothing_and_becomes_a_staff_item():
    site, s = AuctionSite(crash_after={"sale"}), "visitor:c1"
    store, t, lease = approved(s)
    for action, source, form in (("list_lien_auction", "staff:office_manager", {}), ("open_lien_bidding", "staff:office_manager", {}),
                                 ("place_lien_bid", BIDDER, {"cents": 20000}), ("close_lien_bidding", "staff:office_manager", {}),
                                 ("declare_lien_winner", "staff:office_manager", {}), ("take_buyer_payment", BIDDER, {"cents": 20000})):
        ok(store, s, t, lease, site, action, source=source, form=form)
    assert run(store, s, action="complete_lien_sale", tenant=t, lease=lease, source="staff:manager").allowed
    with pytest.raises(Crash):
        drive(store, s, site, NOW)                                                    # the platform sold; the answer was lost
    assert listing(site, s, store)["status"] == "sold" and the_auction(store, s).status == "buyer_paid"
    pays_in_full(store, s, t)                                                         # the tenant pays in full before the answer is known
    assert state_of(store, s, LATER)[0] == "cancellation waiting"
    drive(store, s, site, LATER)
    a, case = the_auction(store, s), the_case(store, s)
    assert [(x["step"], x["result"]) for x in a.late] == [("sale", {"status": "sold"})] and a.status == "buyer_paid"     # kept, and changes nothing
    assert case.closed is None and state_of(store, s, LATER)[0] == "cancellation waiting"
    refs = {t_.ref for t_ in col.open_tasks(store.books(s))}
    assert any("late:" in r for r in refs) and any(r.endswith("cancel_unconfirmed") for r in refs)                       # both are staff items
    drive(store, s, site, LATER)
    assert the_case(store, s).closed is None and listing(site, s, store)["status"] == "sold"                           # never a false confirmation
    d = run(store, s, LATER, action="complete_lien_sale", tenant=t, lease=lease, source="staff:manager")
    assert not d.allowed and d.check == "this auction is being cancelled because the claim was paid in full"


@pytest.mark.parametrize("crash", ["crash_after", "crash_before"])
def test_a_listing_whose_answer_was_lost_leaves_exactly_one_listing_after_a_restart(tmp_path, crash):
    path, s = str(tmp_path / "auction.json"), "visitor:c2"
    site = AuctionSite(path=path, **{crash: {"list"}})
    store, t, lease = approved(s)
    assert run(store, s, action="list_lien_auction", tenant=t, lease=lease, source="staff:office_manager").allowed
    with pytest.raises(Crash):
        drive(store, s, site, NOW)
    assert len(site.listings) == (1 if crash == "crash_after" else 0) and the_auction(store, s).status == "new"     # the books know no listing yet
    restarted = AuctionSite(path=path)                                   # a restart finds the platform's file
    drive(store, s, restarted, NOW)
    drive(store, s, restarted, NOW)                                                   # and running it again does nothing more
    a = the_auction(store, s)
    assert len(restarted.listings) == 1 and [c.rsplit(":", 1)[1] for c in restarted.calls] == ["list"]
    assert a.status == "listed" and a.listing == listing(restarted, s, store)["id"] and state_of(store, s)[0] == "auction"
    assert a.ops[f"{the_case(store, s).id}:1:list"].how == ("found" if crash == "crash_after" else "response")


def test_a_listing_never_sent_when_the_debt_is_paid_is_dropped_and_the_cancellation_reads_back_nothing_listed():
    site, s = AuctionSite(), "visitor:c3"
    store, t, lease = approved(s)
    assert run(store, s, action="list_lien_auction", tenant=t, lease=lease, source="staff:office_manager").allowed
    pays_in_full(store, s, t)                                                         # before the listing was ever sent
    drive(store, s, site, LATER)
    ops = sorted(the_auction(store, s).ops.values(), key=lambda o: o.n)
    assert [(o.step, o.state) for o in ops] == [("list", "refused"), ("cancel", "answered"), ("read", "answered")]
    assert site.listings == {} and [c.rsplit(":", 1)[1] for c in site.calls] == ["cancel"]
    assert the_case(store, s).closed["kind"] == "cured"


def test_pressing_a_step_twice_posts_once():
    site, s = AuctionSite(), "visitor:c4"
    store, t, lease = approved(s)
    kw = dict(action="list_lien_auction", tenant=t, lease=lease, source="staff:office_manager")
    first = run(store, s, id="press-1", **kw)
    again = run(store, s, id="press-1", **kw)                                          # the same press, answered again
    other = run(store, s, id="press-2", **kw)                                          # a second press before the platform answered
    assert first.allowed and again.repeated and not other.allowed and "already has an auction" in other.check
    drive(store, s, site, NOW)
    drive(store, s, site, NOW)
    late = run(store, s, id="press-3", **kw)                                           # and one after it did
    assert not late.allowed and "already has an auction" in late.check
    drive(store, s, site, NOW)
    assert len(site.listings) == 1 and [c.rsplit(":", 1)[1] for c in site.calls] == ["list"]
    ok(store, s, t, lease, site, "open_lien_bidding")
    bid = dict(action="place_lien_bid", tenant=t, lease=lease, source=BIDDER, form={"cents": 20000})
    assert run(store, s, id="bid-1", **bid).allowed and run(store, s, id="bid-1", **bid).repeated
    drive(store, s, site, NOW)
    assert not run(store, s, id="bid-2", **bid).allowed                               # the same amount is not higher than the top bid
    assert len(the_auction(store, s).bids) == 1 and len(listing(site, s, store)["bids"]) == 1


# the platform's own copy cleanup -------------------------------------------------------------------------------------------
def test_a_reset_copys_listings_are_forgotten_by_its_mark_and_another_copys_stay(tmp_path):
    path = str(tmp_path / "auction.json")
    site = AuctionSite(path=path)
    one = "visitor:d1"
    store, t, lease = approved(one)
    ok(store, one, t, lease, site, "list_lien_auction")
    mark = copy_mark(store, one) + ":"
    site.listings["other"] = {"id": "other", "ref": "visitor:other@12345678:LC0001", "status": "listed", "bids": [], "winner": None}
    site.note(mark)
    assert AuctionSite(path=path).pending == [mark]                                      # the note is on file before the copy goes
    site.forget(mark)
    fresh = AuctionSite(path=path)
    assert list(fresh.listings) == ["other"] and fresh.pending == [] and not any(k.startswith(mark) for k in fresh.done)


def test_a_closed_auction_with_no_bids_names_no_winner_and_nothing_after_it_goes_ahead():
    site, s = AuctionSite(), "visitor:e1"
    store, t, lease = approved(s)
    for action in ("list_lien_auction", "open_lien_bidding", "close_lien_bidding", "declare_lien_winner"):
        ok(store, s, t, lease, site, action)
    a = the_auction(store, s)
    assert a.status == "no_bids" and a.winner is None and listing(site, s, store)["status"] == "no_bids"
    for action in ("take_buyer_payment", "complete_lien_sale", "place_lien_bid"):
        d = run(store, s, action=action, tenant=t, lease=lease, source=SOURCES.get(action, BIDDER), form={"cents": 20000})
        assert not d.allowed and auction.ORDER_WHY[auction.STEP_OF[action]] in d.check and "closed with no bids" in d.check
    assert state_of(store, s)[0] == "auction" and the_case(store, s).closed is None


def test_a_desk_reset_forgets_the_copys_listings_with_its_gate_codes(tmp_path):
    path = str(tmp_path / "core.db")
    desk, store, stream, *_ = desk_rental("card_ok", True, desk=FrontDesk(path, clock=lambda: at_local(date(2026, 12, 3), 10)))
    mark = copy_mark(store, stream)
    desk.auction.list_item(f"{mark}:LC0001:1:list", f"{mark}:LC0001", "Contents of unit A1")
    desk.auction.list_item("visitor:v9@00000000:LC0001:1:list", "visitor:v9@00000000:LC0001", "another copy's unit")
    assert os.path.exists(os.path.join(str(tmp_path), "auction.json")) and len(desk.auction.listings) == 2
    desk.reset("v1")
    other = ["visitor:v9@00000000:LC0001"]
    assert [x["ref"] for x in desk.auction.listings.values()] == other and not desk.auction.pending
    assert [x["ref"] for x in AuctionSite(path=desk.auction.path).listings.values()] == other


# the review's findings -------------------------------------------------------------------------------------------------------
def cancel_recorded(s, site, reason="the owner withdrew the sale"):
    """An approved case whose auction is listed and whose cancellation is on record but not yet sent."""
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    assert run(store, s, action="cancel_lien_auction", tenant=t, lease=lease, source="staff:manager", reason=reason).allowed
    return store, t, lease


def test_closing_a_case_whose_cancellation_is_recorded_keeps_it_open_until_the_read_back():
    site, s = AuctionSite(), "visitor:g1"
    store, t, lease = cancel_recorded(s, site)
    d = run(store, s, action="decide_lien_case", tenant=t, lease=lease, source="staff:office_manager", reason="close it",
            form={"outcome": "close"})
    assert d.allowed and d.check == "the cancellation is already recorded; the case closes once the platform shows the listing cancelled"
    assert the_case(store, s).closed is None and state_of(store, s)[0] == "cancellation waiting"
    assert listing(site, s, store)["status"] == "listed"                                 # the listing is still up until the cancel is sent
    drive(store, s, site, NOW)
    assert listing(site, s, store)["status"] == "cancelled" and the_case(store, s).closed["kind"] == "cancelled"


def test_the_unconfirmed_cancellation_item_is_settled_when_a_later_read_back_confirms():
    site, s = AuctionSite(hide={"cancelled"}), "visitor:g2"
    store, t, lease = cancel_recorded(s, site)
    drive(store, s, site, NOW)
    assert [x.ref for x in col.open_tasks(store.books(s))] == [f"auction:{the_case(store, s).id}:cancel_unconfirmed"]
    site.hide = set()
    drive(store, s, site, NOW)
    assert the_case(store, s).closed["kind"] == "cancelled" and col.open_tasks(store.books(s)) == []


def test_a_known_listing_the_platform_no_longer_shows_is_not_a_confirmed_cancellation():
    site, s = AuctionSite(), "visitor:g3"
    store, t, lease = cancel_recorded(s, site)
    site.listings.clear()                                                                # the platform's file lost the listing the books know
    drive(store, s, site, NOW)
    assert the_case(store, s).closed is None and state_of(store, s)[0] == "cancellation waiting"
    assert [x.ref for x in col.open_tasks(store.books(s))] == [f"auction:{the_case(store, s).id}:cancel_unconfirmed"]


@pytest.mark.parametrize("platform_keeps_it", [True, False])
def test_a_listing_recovered_from_a_late_answer_must_show_cancelled_before_the_case_closes(platform_keeps_it):
    site, s = AuctionSite(crash_after={"list"}), "visitor:g4"
    store, t, lease = approved(s)
    assert run(store, s, action="list_lien_auction", tenant=t, lease=lease, source="staff:office_manager").allowed
    with pytest.raises(Crash):
        drive(store, s, site, NOW)                                                       # listed on the platform, the answer lost
    assert run(store, s, action="cancel_lien_auction", tenant=t, lease=lease, source="staff:manager", reason="withdrawn").allowed
    if not platform_keeps_it:
        site.listings.clear()
    drive(store, s, site, NOW)
    case = the_case(store, s)
    if platform_keeps_it:
        assert case.closed["kind"] == "cancelled"
        assert [x.ref.split(":", 2)[2][:5] for x in col.open_tasks(store.books(s))] == ["late:"]       # a late answer stays for staff
    else:
        assert case.closed is None and state_of(store, s)[0] == "cancellation waiting"


def test_a_step_is_sent_inside_the_write_that_rechecks_it_so_nothing_can_be_recorded_between(tmp_path):
    path, s = str(tmp_path / "core.db"), "visitor:g5"
    store, t, lease = world(schedule=(10, 11), path=path)
    assert run(store, s, action="flag_lien_eligible", tenant=t, lease=lease, source="staff:manager").allowed
    assert run(store, s, action="open_lien_case", tenant=t, lease=lease, source="staff:manager").allowed
    assert staff(store, s, t, lease, "record_lien_address", ADDRESS).allowed and staff(store, s, t, lease, "prepare_lien_notice").allowed
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed
    other = Store(path)                                                                  # another writer, as a second process would be
    other.db.execute("PRAGMA busy_timeout = 50")
    seen = []

    class Watching(AuctionSite):
        def list_item(self, key, ref, description):
            try:
                with other.write():
                    seen.append("a stop could be recorded now")
            except sqlite3.OperationalError:
                seen.append("locked")
            return super().list_item(key, ref, description)

    site = Watching()
    ok(store, s, t, lease, site, "list_lien_auction")
    assert seen == ["locked"] and the_auction(store, s).status == "listed"
    stop = {"kind": "bankruptcy", "detail": "case 26-1", "source": "the court index"}
    assert staff(store, s, t, lease, "record_lien_stop", stop).allowed                   # once the send is done the stop is recorded as ever


def test_the_platform_returns_the_first_answer_for_a_repeated_key_across_threads_and_a_restart(tmp_path):
    path = str(tmp_path / "auction.json")
    site = AuctionSite(path=path)
    answers = []
    pool = [threading.Thread(target=lambda: answers.append(site.list_item("mark:LC0001:1:list", "mark:LC0001", "unit A1"))) for _ in range(8)]
    for x in pool:
        x.start()
    for x in pool:
        x.join()
    assert len(site.listings) == 1 and site.calls == ["mark:LC0001:1:list"] and all(a == answers[0] for a in answers)
    restarted = AuctionSite(path=path)
    assert restarted.list_item("mark:LC0001:1:list", "mark:LC0001", "unit A1") == answers[0] and len(restarted.listings) == 1
    assert restarted.list_item("mark:LC0001:2:list", "mark:LC0001", "unit A1") != answers[0] and len(restarted.listings) == 2    # a new key is a new call


def test_a_case_whose_only_listing_request_was_refused_closes_when_the_claim_is_paid():
    site, s = AuctionSite(), "visitor:g6"
    store, t, lease = approved(s)
    assert run(store, s, action="list_lien_auction", tenant=t, lease=lease, source="staff:office_manager").allowed
    do(store, s, lambda led: (led.receive_payment(t, 1000, LATER), liens.recheck(led, t, LATER)))       # a part payment before the listing was sent
    drive(store, s, site, LATER)
    assert [(o.step, o.state) for o in the_auction(store, s).ops.values()] == [("list", "refused")] and site.calls == []
    do(store, s, lambda led: (led.receive_payment(t, 11000, LATER), liens.recheck(led, t, LATER)))     # the rest is paid
    case = the_case(store, s)
    assert case.closed["kind"] == "cured" and liens.active_case(store.books(s), lease) is None and site.calls == []
