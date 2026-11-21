"""The money after a sale on Staddle's own auction platform: the sale money applied once to the lien debt, a surplus with a life of its
own that is never income and never spendable credit, an agreement that blocks the sale while it is active, a payment that comes back
after a cancellation, and a sale disputed after closing."""
import pickle
from datetime import date, timedelta

import pytest

from core import delinquency as col
from core import lien_money, liens
from core.auction import AuctionSite, drive
from core.books import at_local
from core.renting import Crash
from tests.test_core_auction import BIDDER, LATER, approved, listing, ok, pays_in_full, the_auction
from tests.test_core_delinquency import NOW, do, world
from tests.test_core_liens import ADDRESS, approve, complete, owed, run, staff, the_case

PROCEEDS = "sale.proceeds_applied"


def proceeds(store, s, t, lease, cents, surplus="SP0001", case="LC0001"):
    """The event that applies buyer money to the lease's open charges, oldest first, as the books would write it."""
    left, apps = cents, []
    for c in store.books(s).open_charges(t, skip_held=False):
        take = min(left, c.open_cents)
        if take:
            apps.append({"charge": c.id, "lease": c.lease, "cents": take})
            left -= take
    return {"case": case, "tenant": t, "lease": lease, "cents": cents, "applications": apps, "surplus_cents": left,
            "surplus": surplus if left else None}


# the records and accounts ----------------------------------------------------------------------------------------------
def test_sale_proceeds_pay_the_lease_charges_and_the_rest_is_a_surplus_owed_to_the_tenant_not_a_payment_or_credit():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:m1"
    before = store.books(s)
    p = proceeds(store, s, t, lease, 20000)
    do(store, s, lambda led: led.emit(PROCEEDS, NOW, p, "sale-proceeds:LC0001", source="collections"))
    b = store.books(s)
    assert b.receivable(lease) == 0 and p["surplus_cents"] == 8000
    assert b.payments == before.payments and b.credit == before.credit and b.lots == before.lots        # buyer money is not a payment or credit
    lines = [(a, c) for _, a, c, _ in store.journal(s)]
    assert ("clearing:auction", 20000) in lines and (f"surplus:{t}", -8000) in lines and sum(c for _, c in lines) == 0
    last = store.journal(s)[-1][0]
    assert not any(a.startswith(("income:", "credit:")) for e, a, _, _ in store.journal(s) if e == last)    # neither income nor spendable credit
    sale, surplus = liens.state(b).sales["LC0001"], liens.state(b).surpluses["SP0001"]
    assert (sale.cents, sale.applied_cents, sale.surplus_cents, sale.surplus) == (20000, 12000, 8000, "SP0001")
    assert (surplus.cents, surplus.state, surplus.tenant, surplus.lease, surplus.case) == (8000, "created", t, lease, "LC0001")
    assert b.next_id("SP") == "SP0002" and pickle.loads(pickle.dumps(b)).liens.surpluses["SP0001"].cents == 8000


def test_a_sale_that_does_not_cover_the_debt_leaves_no_surplus_and_pays_the_oldest_charge_first():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:m2"
    p = proceeds(store, s, t, lease, 11000)
    do(store, s, lambda led: led.emit(PROCEEDS, NOW, p, "sale-proceeds:LC0001", source="collections"))
    b = store.books(s)
    rent = next(c for c in b.tenant_charges(t) if c.obligation == f"rent:{lease}:2026-03")
    assert p["surplus"] is None and liens.state(b).surpluses == {} and b.receivable(lease) == 1000 and rent.paid == 10000
    assert not any(a.startswith("surplus:") for _, a, _, _ in store.journal(s))


def test_every_surplus_record_folds_from_its_own_event_and_the_payout_moves_money_out_of_the_surplus_account():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:m3"
    p = proceeds(store, s, t, lease, 20000)
    at = NOW

    def write(led):
        led.emit(PROCEEDS, at, p, "sale-proceeds:LC0001", source="collections")
        led.emit("lien_case.surplus_notified", at, {"case": "LC0001", "surplus": "SP0001", "method": "in_person", "facts": {"delivered_on": "2026-03-13"},
                                                    "simulated": True, "by": "staff:front_desk"}, "surplus:SP0001:notified", source="staff:front_desk")
        led.emit("lien_case.surplus_review", at, {"case": "LC0001", "surplus": "SP0001", "review": 1, "kind": "claim", "detail": "a lender says it is owed",
                                                  "by": "staff:front_desk"}, "surplus:SP0001:review:1", source="staff:front_desk")
        led.emit("lien_case.surplus_review_resolved", at, {"case": "LC0001", "surplus": "SP0001", "review": 1, "reason": "counsel: no standing",
                                                           "by": "staff:manager"}, "surplus:SP0001:review:1:resolved", source="staff:manager")
        led.emit("lien_case.surplus_payout_requested", at, {"case": "LC0001", "surplus": "SP0001", "by": "staff:manager"},
                 "surplus:SP0001:payout", source="staff:manager")
        led.emit("surplus.paid", at, {"surplus": "SP0001", "tenant": t, "cents": 8000, "simulated": True, "by": "staff:manager"},
                 "surplus:SP0001:paid", source="staff:manager")
        led.emit("lien_case.agreement_recorded", at, {"case": "LC0001", "agreement": "AG0001", "kind": "pay_to_vacate", "terms": "leave by April 1",
                                                      "by": "staff:manager"}, "agreement:AG0001:recorded", source="staff:manager")
        led.emit("lien_case.agreement_accepted", at, {"case": "LC0001", "agreement": "AG0001", "accepted_by": "Ann Reyes", "how": "signed",
                                                      "document_sha256": "a" * 64, "by": "staff:manager"}, "agreement:AG0001:accepted", source="staff:manager")
        led.emit("lien_case.agreement_ended", at, {"case": "LC0001", "agreement": "AG0001", "how": "broken", "reason": "the tenant did not leave",
                                                   "by": "staff:manager"}, "agreement:AG0001:ended", source="staff:manager")
        led.emit("lien_case.sale_disputed", at, {"case": "LC0001", "dispute": 1, "raised_by": "tenant", "detail": "says the unit was not theirs to sell",
                                                 "by": "staff:front_desk"}, "sale:LC0001:dispute:1", source="staff:front_desk")
        led.emit("lien_case.sale_dispute_resolved", at, {"case": "LC0001", "dispute": 1, "reason": "counsel reviewed it", "by": "staff:manager"},
                 "sale:LC0001:dispute:1:resolved", source="staff:manager")
        led.emit("lien_case.return_noted", at, {"case": "LC0001", "payment": "P0009", "owed_cents": 500, "by": "collections"},
                 "lien_case:LC0001:return:P0009", source="collections")
    do(store, s, write)
    st = liens.state(store.books(s))
    surplus, agreement, sale = st.surpluses["SP0001"], st.agreements["AG0001"], st.sales["LC0001"]
    assert surplus.state == "paid" and surplus.notices[0]["method"] == "in_person" and surplus.notices[0]["simulated"] and surplus.paid["simulated"]
    assert surplus.reviews[0]["kind"] == "claim" and surplus.reviews[0]["resolved"]["reason"] == "counsel: no standing" and surplus.payout["by"] == "staff:manager"
    assert agreement.kind == "pay_to_vacate" and agreement.accepted["accepted_by"] == "Ann Reyes" and agreement.ended["how"] == "broken" and not agreement.active
    assert sale.disputes[0]["detail"].startswith("says the unit") and sale.disputes[0]["resolved"]["reason"] == "counsel reviewed it"
    assert st.returns == {"LC0001": ["P0009"]}
    lines = [(a, c) for _, a, c, _ in store.journal(s)]
    assert (f"surplus:{t}", 8000) in lines and ("clearing:auction", -8000) in lines and sum(c for _, c in lines) == 0


# the sale money -----------------------------------------------------------------------------------------------------------
def sell(s, cents, site=None):
    """An approved case taken through every auction step but the final sale, the buyer having paid `cents`."""
    site = site or AuctionSite()
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    ok(store, s, t, lease, site, "open_lien_bidding")
    ok(store, s, t, lease, site, "place_lien_bid", source=BIDDER, form={"cents": cents})
    ok(store, s, t, lease, site, "close_lien_bidding")
    ok(store, s, t, lease, site, "declare_lien_winner")
    ok(store, s, t, lease, site, "take_buyer_payment", source=BIDDER, form={"cents": cents})
    return store, t, lease, site


def final(store, s, t, lease, site):
    return ok(store, s, t, lease, site, "complete_lien_sale", source="staff:manager")


def kinds(store, s, kind):
    return [e for e in store.stream_events(s) if e.kind == kind]


def net(store, s):
    """The journal's balance of each account."""
    out = {}
    for _, account, cents, _ in store.journal(s):
        out[account] = out.get(account, 0) + cents
    return out


def test_the_buyers_money_stays_off_the_tenants_account_until_the_sale_is_final_then_pays_the_debt_once():
    s = "visitor:n1"
    store, t, lease, site = sell(s, 20000)
    before = store.books(s)
    assert owed(store, s, t) == 12000 and kinds(store, s, "sale.proceeds_applied") == []                  # a buyer's payment alone applies nothing
    final(store, s, t, lease, site)
    b, case = store.books(s), the_case(store, s)
    assert owed(store, s, t) == 0 and b.payments == before.payments and b.credit == before.credit and b.lots == before.lots
    sale = liens.state(b).sales[case.id]
    assert (sale.cents, sale.applied_cents, sale.surplus_cents) == (20000, 12000, 8000) and sale.surplus == "SP0001"
    assert case.closed["kind"] == "sold" and "$200.00" in case.closed["why"] and "$80.00" in case.closed["why"]
    rent = next(c for c in b.tenant_charges(t) if c.obligation == f"rent:{lease}:2026-03")
    assert [src for _, _, src in rent.paid_effective][-1] == f"sale:{case.id}"
    # repeating anything, or starting over from the events, applies it once
    count = len(store.stream_events(s))
    do(store, s, lambda led: lien_money.settle(led, liens.state(led.books).cases[case.id], NOW))
    do(store, s, lambda led: liens.recheck(led, t, NOW + timedelta(hours=1)))
    drive(store, s, site, NOW)
    assert len(store.stream_events(s)) == count and len(kinds(store, s, "sale.proceeds_applied")) == 1
    (event,) = kinds(store, s, "sale.proceeds_applied")
    lines = [(a, c) for e, a, c, _ in store.journal(s) if e == event.id]
    assert ("clearing:auction", 20000) in lines and (f"surplus:{t}", -8000) in lines and sum(c for _, c in lines) == 0


def test_a_lost_answer_to_the_final_sale_still_applies_the_money_exactly_once():
    s = "visitor:n2"
    store, t, lease, site = sell(s, 20000, AuctionSite(crash_after={"sale"}))
    assert run(store, s, action="complete_lien_sale", tenant=t, lease=lease, source="staff:manager").allowed
    with pytest.raises(Crash):
        drive(store, s, site, NOW)                                                                       # sold on the platform, answer lost
    assert kinds(store, s, "sale.proceeds_applied") == [] and owed(store, s, t) == 12000
    drive(store, s, site, NOW)
    drive(store, s, site, NOW)
    assert len(kinds(store, s, "sale.proceeds_applied")) == 1 and owed(store, s, t) == 0 and the_case(store, s).closed["kind"] == "sold"


def test_a_sale_that_does_not_cover_the_debt_pays_the_oldest_charge_first_leaves_no_surplus_and_tells_staff():
    s = "visitor:n3"
    store, t, lease, site = sell(s, 11000)
    final(store, s, t, lease, site)
    b = store.books(s)
    assert owed(store, s, t) == 1000 and liens.state(b).surpluses == {} and not any(a.startswith("surplus:") for _, a, _, _ in store.journal(s))
    rent = next(c for c in b.tenant_charges(t) if c.obligation == f"rent:{lease}:2026-03")
    fee = next(c for c in b.tenant_charges(t) if c.kind == "late_fee")
    assert rent.open_cents == 0 and fee.open_cents == 1000                                               # rent before the late fee
    assert [k.kind for k in col.open_tasks(b)] == ["lien case needs staff"] and "$10.00 is still owed" in col.open_tasks(b)[0].why


# the surplus -------------------------------------------------------------------------------------------------------------
MAIL = {"deposited_on": "2026-03-20", "first_class": True, "certificate_of_mailing": True, "address": "9 Oak Ave, Orlando, FL"}


def sold(s, cents=20000):
    store, t, lease, site = sell(s, cents)
    final(store, s, t, lease, site)
    return store, t, lease


def money_step(store, s, t, lease, action, source="staff:front_desk", form=None, now=NOW, **kw):
    return run(store, s, now, action=action, tenant=t, lease=lease, source=source, form={"surplus": "SP0001", **(form or {})}, **kw)


def surplus(store, s):
    return liens.state(store.books(s)).surpluses["SP0001"]


def test_the_surplus_is_told_to_the_tenant_in_person_or_by_first_class_mail_with_a_certificate_and_then_held():
    s = "visitor:s1"
    store, t, lease = sold(s)
    assert surplus(store, s).state == "created"
    step = lambda form, **kw: money_step(store, s, t, lease, "record_surplus_notice", form=form, **kw)
    assert step({"method": "in_person"}, source="operator").check == "only staff take a lien step"
    assert step({"method": "by phone"}).check == "the tenant is told in person or by first-class mail with a certificate of mailing"
    assert step({"method": "in_person", "facts": {}}).check == "an in-person notice needs the date it was handed over"
    wrong = "the mailing went to 4 Elm St, Tampa, FL, but the last written address on the file is 9 Oak Ave, Orlando, FL"
    for fault, why in (({"certificate_of_mailing": False}, "a mailing needs a certificate of mailing"), ({"first_class": False}, "a mailing is first-class mail"),
                       ({"address": "4 Elm St, Tampa, FL"}, wrong)):
        d = step({"method": "first_class_mail", "facts": {**MAIL, **fault}})
        assert not d.allowed and d.check == why
    assert surplus(store, s).state == "created"
    d = step({"method": "first_class_mail", "facts": MAIL})
    assert d.allowed and "simulated" in d.check
    held = surplus(store, s)
    assert held.state == "held" and held.notices[0]["simulated"] is True and held.notices[0]["method"] == "first_class_mail"
    again = step({"method": "in_person", "facts": {"delivered_on": "2026-03-21"}})
    assert again.check == "the tenant was already told about this surplus"
    assert step({"surplus": "SP0099", "method": "in_person"}).check == "there is no surplus SP0099 on this unit"


def test_a_surplus_is_paid_out_only_after_notice_with_no_review_open_and_never_becomes_income_or_credit():
    s = "visitor:s2"
    store, t, lease = sold(s)
    step = lambda action, **kw: money_step(store, s, t, lease, action, **kw)
    early = step("request_surplus_payout", source="staff:manager")
    assert not early.allowed and early.check == "the tenant has not been told about this surplus yet; a payout waits for the notice"
    assert step("record_surplus_notice", form={"method": "in_person", "facts": {"delivered_on": "2026-03-21"}}).allowed
    assert step("request_surplus_payout", source="staff:office_manager").check == "only a manager asks for a surplus payout"
    claim = step("record_surplus_review", form={"detail": "a lender says the unit's contents were pledged to it"})
    assert claim.allowed and surplus(store, s).reviews[0]["kind"] == "claim"
    assert [k.kind for k in col.open_tasks(store.books(s))] == ["surplus needs staff"]
    blocked = step("request_surplus_payout", source="staff:manager")
    assert not blocked.allowed and blocked.check == "a claim or review is open on this surplus; staff record it resolved first"
    why = "counsel found no standing"
    assert step("resolve_surplus_review", source="staff:office_manager", form={"review": 1}, reason=why).check == "only a manager records a surplus review resolved"
    assert step("resolve_surplus_review", source="staff:manager", form={"review": 1}).check == "a review is resolved with a reason"
    assert step("resolve_surplus_review", source="staff:manager", form={"review": 7}, reason=why).check == "there is no open review 7 on this surplus"
    assert step("resolve_surplus_review", source="staff:manager", form={"review": 1}, reason=why).allowed
    assert col.open_tasks(store.books(s)) == []
    assert step("confirm_surplus_payout", source="staff:manager").check == "no payout is requested for this surplus"
    assert step("request_surplus_payout", source="staff:manager").allowed and surplus(store, s).state == "payout_requested"
    assert step("request_surplus_payout", source="staff:manager").check == "a payout is already requested for this surplus"
    paid = step("confirm_surplus_payout", source="staff:manager")
    assert paid.allowed and surplus(store, s).state == "paid" and surplus(store, s).paid["simulated"] is True and "simulated" in paid.check
    balance = net(store, s)
    assert balance[f"surplus:{t}"] == 0 and balance["clearing:auction"] == 12000                           # what stays on the platform's account is the facility's share
    assert store.books(s).credit.get(t, 0) == store.books("base").credit.get(t, 0)
    assert step("confirm_surplus_payout", source="staff:manager").check == "that surplus was already paid"


def test_nothing_posted_after_the_sale_reduces_the_surplus_and_credit_never_reaches_it():
    s = "visitor:s3"
    store, t, lease = sold(s)
    income = lambda: sum(c for _, a, c, _ in store.journal(s) if a.startswith("income:"))
    before = income()
    do(store, s, lambda led: led.charge_by_hand(NOW + timedelta(hours=1), lease, "admin_fee", 2500, None))      # a fee posted after the sale
    do(store, s, lambda led: led.apply_credit(NOW + timedelta(hours=2), t))
    b = store.books(s)
    assert surplus(store, s).cents == 8000 and b.receivable(lease) == 2500 and b.credit == store.books("base").credit     # the new fee stays owed; the surplus pays none of it
    assert net(store, s)[f"surplus:{t}"] == -8000 and income() == before - 2500                              # income is credits, so only the new fee added to it


def test_the_two_year_mark_and_a_claim_go_to_staff_review_and_never_move_money_by_themselves():
    s = "visitor:s4"
    store, t, lease = sold(s)
    before = list(store.journal(s))
    col.sweep(store, s, at_local(date(2028, 3, 11), 9))
    assert surplus(store, s).reviews == []
    later = at_local(date(2028, 3, 13), 9)
    col.sweep(store, s, later)                                                                              # the daily pass reaches a closed case's surplus
    col.sweep(store, s, later + timedelta(days=1))
    (review,) = surplus(store, s).reviews
    assert review["kind"] == "two_year" and "two years" in review["detail"] and review["resolved"] is None
    assert [(k.kind, k.ref) for k in col.open_tasks(store.books(s))] == [("surplus needs staff", "surplus:SP0001:review:1")]
    assert store.journal(s) == before and surplus(store, s).state == "created" and surplus(store, s).cents == 8000     # no money moved, none became income


# an agreement to vacate or settle -----------------------------------------------------------------------------------------
TERMS = "the tenant leaves unit A1 by April 1 and the facility drops the sale"


def agreement(store, s, t, lease, action, source="staff:office_manager", reason=None, **form):
    return run(store, s, action=action, tenant=t, lease=lease, source=source, reason=reason, form=form)


def test_an_agreement_to_vacate_or_settle_is_recorded_with_its_acceptance_and_blocks_every_sale_step_while_it_is_active():
    s, site = "visitor:g1", AuctionSite()
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    assert agreement(store, s, t, lease, "record_lien_agreement", source="operator", kind="settlement", terms=TERMS).check == "only staff take a lien step"
    assert agreement(store, s, t, lease, "record_lien_agreement", kind="barter", terms=TERMS).check == "an agreement is a pay-to-vacate or a settlement agreement"
    assert agreement(store, s, t, lease, "record_lien_agreement", kind="settlement", terms=" ").check == "an agreement is recorded with its terms"
    d = agreement(store, s, t, lease, "record_lien_agreement", kind="pay_to_vacate", terms=TERMS)
    assert d.allowed and the_agreement(store, s).kind == "pay_to_vacate" and the_agreement(store, s).accepted is None
    blocked = run(store, s, action="open_lien_bidding", tenant=t, lease=lease, source="staff:office_manager")
    assert not blocked.allowed and blocked.check == "a pay-to-vacate agreement is in force; no sale step goes ahead until it ends"       # offered counts too
    sha = "a" * 64
    other = agreement(store, s, t, lease, "accept_lien_agreement", agreement="AG0001", accepted_by="Someone Else", how="signed", document_sha256=sha)
    assert other.check == "the acceptance must be the tenant's own: Ann Reyes"
    assert agreement(store, s, t, lease, "accept_lien_agreement", agreement="AG0001", accepted_by="Ann Reyes", how="signed").check == "a signed acceptance needs the fingerprint of its document"
    assert agreement(store, s, t, lease, "accept_lien_agreement", agreement="AG0009", accepted_by="Ann Reyes", how="signed", document_sha256=sha).check == "there is no open agreement AG0009 on this case"
    assert agreement(store, s, t, lease, "accept_lien_agreement", agreement="AG0001", accepted_by="ann reyes", how="signed", document_sha256=sha).allowed
    accepted = the_agreement(store, s).accepted
    assert accepted["accepted_by"] == "ann reyes" and accepted["how"] == "signed" and accepted["document_sha256"] == sha
    assert agreement(store, s, t, lease, "accept_lien_agreement", agreement="AG0001", accepted_by="Ann Reyes", how="signed", document_sha256=sha).check == "that agreement was already accepted"
    for action, form in (("open_lien_bidding", {}), ("place_lien_bid", {"cents": 20000}), ("complete_lien_sale", {})):
        d = run(store, s, action=action, tenant=t, lease=lease, source=BIDDER if action == "place_lien_bid" else "staff:manager", form=form)
        assert not d.allowed and d.check.startswith("a pay-to-vacate agreement is in force"), action
    assert run(store, s, action="list_lien_auction", tenant=t, lease=lease, source="staff:manager").check.startswith("a pay-to-vacate agreement is in force")
    assert agreement(store, s, t, lease, "end_lien_agreement", agreement="AG0001", how="broken").check == "an agreement is ended with a reason"
    assert agreement(store, s, t, lease, "end_lien_agreement", agreement="AG0001", how="forgotten", reason="x").check == "an agreement ends as completed, broken or withdrawn"
    assert agreement(store, s, t, lease, "end_lien_agreement", agreement="AG0001", how="broken", reason="the tenant did not leave").allowed
    assert agreement(store, s, t, lease, "end_lien_agreement", agreement="AG0001", how="broken", reason="again").check == "there is no open agreement AG0001 on this case"
    ok(store, s, t, lease, site, "open_lien_bidding")                                                    # the agreement is over: the sale goes on
    assert the_auction(store, s).status == "bidding"


def the_agreement(store, s):
    return liens.state(store.books(s)).agreements["AG0001"]


def test_a_sale_step_asked_for_before_an_agreement_is_dropped_when_it_is_sent_and_cancelling_stays_open():
    s, site = "visitor:g2", AuctionSite()
    store, t, lease = approved(s)
    assert run(store, s, action="list_lien_auction", tenant=t, lease=lease, source="staff:office_manager").allowed       # asked for, not yet sent
    assert agreement(store, s, t, lease, "record_lien_agreement", kind="settlement", terms=TERMS).allowed
    drive(store, s, site, NOW)
    (op,) = the_auction(store, s).ops.values()
    assert op.state == "refused" and "a settlement agreement is in force" in op.result["why"] and site.listings == {}
    cancel = run(store, s, action="cancel_lien_auction", tenant=t, lease=lease, source="staff:manager", reason="the case is settled")
    assert cancel.check == "there is no auction on this case to cancel" and the_case(store, s).closed is None                # nothing was ever listed
    assert agreement(store, s, t, lease, "end_lien_agreement", agreement="AG0001", how="withdrawn", reason="the tenant changed their mind").allowed
    ok(store, s, t, lease, site, "list_lien_auction")                                                        # a new request, after the agreement ended
    assert the_auction(store, s).status == "listed" and len(site.listings) == 1


def test_cancelling_a_live_listing_stays_open_while_an_agreement_is_active():
    s, site = "visitor:g3", AuctionSite()
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    assert agreement(store, s, t, lease, "record_lien_agreement", kind="settlement", terms=TERMS).allowed
    ok(store, s, t, lease, site, "cancel_lien_auction", source="staff:manager", reason="the case is settled")
    assert the_case(store, s).closed["kind"] == "cancelled" and listing(site, s, store)["status"] == "cancelled"


# a payment that comes back after a cancellation ----------------------------------------------------------------------------
def test_a_payment_returned_after_the_cancellation_reopens_the_debt_and_a_staff_case_and_never_the_auction():
    s, site = "visitor:r1", AuctionSite()
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    pays_in_full(store, s, t)
    drive(store, s, site, LATER)
    case = the_case(store, s)
    assert case.closed["kind"] == "cured" and the_auction(store, s).cancel["confirmed"] and col.open_tasks(store.books(s)) == []
    (payment,) = [p.id for p in store.books(s).payments.values() if p.cents == 12000 and p.known_at >= LATER]
    back = LATER + timedelta(days=2)
    do(store, s, lambda led: led.return_payment(back, payment))
    assert owed(store, s, t) == 12000 and kinds(store, s, "lien_case.return_noted") == []                 # the books reopened the debt; staff do not know yet
    col.sweep(store, s, back)                                                                              # the daily pass notices it on a closed case
    col.sweep(store, s, back + timedelta(days=1))
    (noted,) = kinds(store, s, "lien_case.return_noted")
    assert noted.payload["payment"] == payment and noted.payload["owed_cents"] == 12000 and liens.state(store.books(s)).returns == {case.id: [payment]}
    (task,) = col.open_tasks(store.books(s))
    assert (task.kind, task.ref) == ("lien case needs staff", f"lien:{case.id}:return:{payment}")
    assert "came back" in task.why and "$120.00 is owed again" in task.why and "cancelled auction is never brought back" in task.why and "new episode" in task.why
    assert the_auction(store, s).status == "cancelled" and listing(site, s, store)["status"] == "cancelled" and the_case(store, s).closed["kind"] == "cured"
    for action in ("list_lien_auction", "open_lien_bidding", "complete_lien_sale"):
        assert run(store, s, back, action=action, tenant=t, lease=lease, source="staff:manager").check == "no lien case is open on this unit"
    assert run(store, s, back, action="open_lien_case", tenant=t, lease=lease, source="staff:manager").allowed          # only a new episode can begin again
    books = store.books(s)
    assert liens.active_case(books, lease).episode == 2 and len(liens.state(books).auctions[case.id].ops) == 3


# a sale disputed after closing ---------------------------------------------------------------------------------------------
def test_a_sale_disputed_after_closing_stands_as_recorded_opens_a_staff_and_counsel_item_and_holds_the_surplus_payout():
    s = "visitor:d1"
    store, t, lease = sold(s)
    case = the_case(store, s)
    before = (list(store.journal(s)), liens.state(store.books(s)).sales[case.id].cents, owed(store, s, t), surplus(store, s).cents)
    dispute = lambda action="dispute_lien_sale", source="staff:front_desk", reason=None, **form: run(
        store, s, action=action, tenant=t, lease=lease, source=source, reason=reason, form=form)
    assert dispute(source="operator", case=case.id, detail="x", raised_by="tenant").check == "only staff take a lien step"
    assert dispute(case="LC0099", detail="x", raised_by="tenant").check == "there is no sold case LC0099 on this unit"
    assert dispute(case=case.id, detail=" ", raised_by="tenant").check == "a dispute is recorded with what is disputed"
    assert dispute(case=case.id, detail="says the unit was not theirs to sell", raised_by="neighbour").check == "a dispute is raised by the tenant, the buyer or someone else"
    d = dispute(case=case.id, detail="says the unit was not theirs to sell", raised_by="tenant")
    assert d.allowed and "nothing is reversed" in d.check
    (item,) = col.open_tasks(store.books(s))
    assert item.kind == "lien sale disputed" and item.ref == f"sale:{case.id}:dispute:1" and "staff and counsel" in item.why
    sale = liens.state(store.books(s)).sales[case.id]
    assert len(sale.disputes) == 1 and sale.disputes[0]["resolved"] is None
    after = (list(store.journal(s)), sale.cents, owed(store, s, t), surplus(store, s).cents)
    assert after == before and the_case(store, s).closed["kind"] == "sold"                                # the record stands: nothing reversed
    step = lambda action, **kw: money_step(store, s, t, lease, action, **kw)
    assert step("record_surplus_notice", form={"method": "in_person", "facts": {"delivered_on": "2026-03-21"}}).allowed
    held = step("request_surplus_payout", source="staff:manager")
    assert not held.allowed and held.check == "the sale is disputed; staff and counsel decide before any surplus is paid out"
    assert dispute("resolve_lien_sale_dispute", source="staff:office_manager", case=case.id, dispute=1).check == "only a manager records a sale dispute resolved"
    assert dispute("resolve_lien_sale_dispute", source="staff:manager", case=case.id, dispute=1).check == "a dispute is resolved with a reason"
    assert dispute("resolve_lien_sale_dispute", source="staff:manager", case=case.id, dispute=3, reason="x").check == "there is no open dispute 3 on that sale"
    assert run(store, s, action="resolve_lien_sale_dispute", tenant=t, lease=lease, source="staff:manager", reason="counsel reviewed it: the sale stands",
               form={"case": case.id, "dispute": 1}).allowed
    assert col.open_tasks(store.books(s)) == [] and step("request_surplus_payout", source="staff:manager").allowed
    assert list(store.journal(s)) == before[0] and owed(store, s, t) == 0                                  # resolving moved no money either


def test_a_claim_that_comes_after_the_payout_was_asked_for_still_stops_the_money():
    s = "visitor:s9"
    store, t, lease = sold(s)
    step = lambda action, **kw: money_step(store, s, t, lease, action, **kw)
    assert step("record_surplus_notice", form={"method": "in_person", "facts": {"delivered_on": "2026-03-21"}}).allowed
    assert step("request_surplus_payout", source="staff:manager").allowed
    assert step("record_surplus_review", form={"detail": "a lender says the contents were pledged to it"}).allowed
    late = step("confirm_surplus_payout", source="staff:manager")
    assert not late.allowed and late.check == "a claim or review is open on this surplus; staff record it resolved first"
    assert surplus(store, s).state == "payout_requested" and net(store, s)[f"surplus:{t}"] != 0       # no money moved


def test_a_signed_acceptance_with_spaces_around_the_word_still_needs_its_document():
    s, site = "visitor:g9", AuctionSite()
    store, t, lease = approved(s)
    ok(store, s, t, lease, site, "list_lien_auction")
    assert agreement(store, s, t, lease, "record_lien_agreement", kind="pay_to_vacate", terms=TERMS).allowed
    name = store.books(s).tenants[t].name
    padded = agreement(store, s, t, lease, "accept_lien_agreement", agreement=the_agreement(store, s).id, accepted_by=name, how=" signed ")
    assert not padded.allowed and padded.check == "a signed acceptance needs the fingerprint of its document"
