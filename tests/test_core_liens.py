"""Lien cases on the core: opened only when the lease is lien-eligible now, kept as append-only records, and checked by the gate in
its order. No notices, no auction and no money here."""
import copy
import hashlib
import pickle    # the books pickle their own folded state, as the store does; nothing here reads a pickle from outside
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from core import delinquency as col
from core import lien_evidence as evidence
from core import liens
from core.books import ADMIN_FEE, at_local
from tests.test_core_delinquency import NOW, do, world
from tests.test_core_delinquency_gate import active_plan
from tests.test_core_delinquency_gate import run as gate_run


def run(store, stream, now=NOW, **kw):
    """The gate's run, naming the unit's open case on a case step, as a page does, unless the test names one itself."""
    if kw.get("action") in liens.CASE_ACTIONS and "case" not in (kw.get("form") or {}):
        open_case = liens.active_case(store.books(stream), kw.get("lease"))
        kw["form"] = {**(kw.get("form") or {}), **({"case": open_case.id} if open_case else {})}
    return gate_run(store, stream, now, **kw)


def owed(store, stream, tenant):
    return sum(c.open_cents for c in store.books(stream).open_charges(tenant))


def test_the_lien_flag_is_a_marker_that_step_state_can_look_past():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:l1"
    assert col.step_state(store.books(s), lease, "lien", NOW)[0] == "eligible"                 # due and never flagged
    assert run(store, s, action="flag_lien_eligible", tenant=t, lease=lease, source="staff:manager").allowed
    b = store.books(s)
    assert col.step_state(b, lease, "lien", NOW)[0] == "recorded"
    assert col.step_state(b, lease, "lien", NOW, ignore_recorded=True)[0] == "eligible"        # flagged and still due
    do(store, s, lambda led: led.receive_payment(t, owed(store, s, t), NOW))
    assert col.step_state(store.books(s), lease, "lien", NOW, ignore_recorded=True)[0] == "clear"   # flagged, then paid


def test_a_lien_case_record_folds_into_the_books_and_survives_the_base_snapshot():
    store, t, lease = world()
    opened = {"case": "LC0001", "lease": lease, "tenant": t, "unit": "A1", "episode": 1, "by": "staff:manager"}
    do(store, "base", lambda led: led.emit("lien_case.opened", NOW, opened, "lien_case:LC0001:opened"))
    b = store.books("visitor:f1")                                    # the base is folded once and pickled; a copy starts from it
    case = liens.state(b).cases["LC0001"]
    assert (case.lease, case.tenant, case.unit, case.episode, case.opened_by, case.opened_at) == (lease, t, "A1", 1, "staff:manager", NOW)
    assert b.next_id("LC") == "LC0002"                                # ids are counted per stream, so the next one never repeats
    assert pickle.loads(pickle.dumps(b)).liens.cases["LC0001"].lease == lease


def cases(store, stream):
    return liens.state(store.books(stream)).cases


def flagged(schedule=(10, 11), stream="visitor:o"):
    """A tenant two weeks behind whose unit the manager has flagged lien-eligible."""
    store, t, lease = world(schedule=schedule)
    assert run(store, stream, action="flag_lien_eligible", tenant=t, lease=lease, source="staff:manager").allowed
    return store, t, lease


def test_a_case_opens_only_on_a_unit_that_is_flagged_and_still_due_now():
    store, t, lease = world(schedule=(10, 11))
    s = "visitor:o1"
    never = run(store, s, action="open_lien_case", tenant=t, lease=lease, source="staff:manager")
    assert not never.allowed and never.check == "the unit has not been flagged lien-eligible; staff flag it first"
    assert run(store, s, action="flag_lien_eligible", tenant=t, lease=lease, source="staff:manager").allowed
    opened = run(store, s, action="open_lien_case", tenant=t, lease=lease, source="staff:office_manager", id="open-1")
    assert opened.allowed and "no notice is sent" in opened.check
    (case,) = cases(store, s).values()
    assert (case.lease, case.tenant, case.unit, case.episode, case.opened_by, case.closed) == (lease, t, "A1", 1, "staff:office_manager", None)
    assert liens.active_case(store.books(s), lease).id == case.id
    # flagged, then paid in full: the old marker authorizes nothing
    store2, t2, lease2 = flagged(stream="visitor:o2")
    do(store2, "visitor:o2", lambda led: led.receive_payment(t2, owed(store2, "visitor:o2", t2), NOW))
    paid = run(store2, "visitor:o2", action="open_lien_case", tenant=t2, lease=lease2, source="staff:manager")
    assert not paid.allowed and paid.check.startswith("not due on the owner's schedule: 11 days") and "0 days past due" in paid.check


def test_opening_is_checked_in_the_gate_order_and_names_its_reason():
    s = "visitor:o3"
    store, t, lease = flagged(stream=s)
    open_ = lambda **kw: run(store, s, action="open_lien_case", tenant=t, lease=kw.pop("lease", lease), **{"source": "staff:manager", **kw})
    assert open_(source="schedule").check == "only staff take a lien step"
    assert open_(source="operator").check == "only staff take a lien step"
    assert open_(lease="L0999").check == "no such unit on this tenant's account"
    sim = col.SimulatedProcessor()
    col.pay(store, s, sim, t, 1000, "card_pending", NOW)
    assert open_().check == "a payment from this tenant is waiting for the processor's answer"       # an unsettled payment waits
    store3, t3, lease3 = flagged(stream="visitor:o4")
    rent = next(c for c in store3.books("visitor:o4").open_charges(t3) if c.kind == "rent")
    assert run(store3, "visitor:o4", action="open_dispute", tenant=t3, charge=rent.id, quote="I paid March in cash").allowed
    held = run(store3, "visitor:o4", action="open_lien_case", tenant=t3, lease=lease3, source="staff:manager")
    assert not held.allowed and held.check == "a dispute on this tenancy is open"                    # the tenancy hold
    store4, t4, lease4 = flagged(stream="visitor:o5")
    do(store4, "visitor:o5", lambda led: led.end_lease(NOW, lease4))
    assert run(store4, "visitor:o5", action="open_lien_case", tenant=t4, lease=lease4, source="staff:manager").check == "that lease has ended; staff handle it"


def test_one_case_at_a_time_a_repeated_command_and_one_copy_never_seeing_another():
    store, t, lease = flagged(stream="visitor:o6")
    a, b = "visitor:o6", "visitor:o7"
    assert run(store, b, action="flag_lien_eligible", tenant=t, lease=lease, source="staff:manager").allowed
    first = run(store, a, action="open_lien_case", tenant=t, lease=lease, source="staff:manager", id="open-x")
    again = run(store, a, action="open_lien_case", tenant=t, lease=lease, source="staff:manager", id="open-x")
    assert first.allowed and again.repeated and again.allowed and len(cases(store, a)) == 1
    second = run(store, a, action="open_lien_case", tenant=t, lease=lease, source="staff:manager")
    assert not second.allowed and second.check == "a lien case is already open on this unit"
    assert cases(store, b) == {} and cases(store, "base") == {}                                      # copy b never sees copy a's case
    assert run(store, b, action="open_lien_case", tenant=t, lease=lease, source="staff:manager").allowed
    assert len(cases(store, a)) == len(cases(store, b)) == 1


NOTICE = {"itemized_charges": [{"description": "March rent", "category": "rent", "amount_cents": 10000, "due_on": "2026-03-01"},
                               {"description": "late fee", "category": "fee", "amount_cents": 2000, "due_on": "2026-03-06"}],
          "itemized_total_cents": 12000, "sum_due_since": "2026-03-01", "property_description": "the contents of unit A1",
          "description_matches_rental_agreement": "yes", "copy_version": 1, "demand_deadline": "2026-04-02", "sale_statement": True, "sale_date": "2026-04-20",
          "sale_time": "10:00 AM", "sale_place": "online, on the auction site", "owner_name": "Northstar Storage",
          "owner_street_address": "1 Main St, Orlando, FL", "owner_phone": "+14075550100"}
MAIL = {"notice_id": 1, "deposited_on": "2026-03-13", "first_class": True, "certificate_of_mailing": True, "postage_prepaid": True,
        "address": "9 Oak Ave, Orlando, FL", "address_basis": "rental agreement"}
POSTING = {"notice_id": 1, "posted_on": "2026-03-13", "location": "unit", "location_detail": "unit door", "conspicuous": "yes"}
AD = {"notice_id": 1, "newspaper": "Orlando Ledger", "general_circulation_in_facility_area": "yes", "circulation_source": "the paper's own statement",
      "proof": "publisher_affidavit", "published_on": "2026-04-03", "sale_date": "2026-04-20", "sale_time": "10:00 AM", "sale_place": "online, on the auction site",
      "includes_property_description": True, "includes_facility_address": True, "includes_tenant_name": True,
      "includes_sale_time_place_manner": True}
CHECK = {"check": "bankruptcy", "answer": "no", "source": "the federal court's public index", "observed_on": "2026-03-12"}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def opened(stream):
    """A flagged unit with a lien case open on it."""
    store, t, lease = flagged(stream=stream)
    assert run(store, stream, action="open_lien_case", tenant=t, lease=lease, source="staff:manager").allowed
    return store, t, lease


def record(store, stream, t, lease, kind, facts, document="a document", source="staff:front_desk", **form):
    form = {"kind": kind, "facts": facts, **({"document_sha256": sha(document)} if kind != "legal_check" else {}), **form}
    return run(store, stream, action="record_lien_evidence", tenant=t, lease=lease, source=source, form=form)


def the_case(store, stream):
    (case,) = cases(store, stream).values()
    return case


def test_evidence_is_recorded_with_who_when_and_its_document_fingerprint():
    s = "visitor:e1"
    store, t, lease = prepared(s)
    d = record(store, s, t, lease, "notice", NOTICE, "the notice")
    assert d.allowed and d.check == "the notice record is added to the file"
    (e,) = the_case(store, s).evidence
    assert (e.id, e.kind, e.document_sha256, e.by, e.at, e.supersedes) == (1, "notice", sha("the notice"), "staff:front_desk", NOW, None)
    assert e.facts["itemized_total_cents"] == 12000 and e.facts["sale_date"] == "2026-04-20"
    check = record(store, s, t, lease, "legal_check", CHECK, source="staff:manager")        # a legal check names its source, no document
    assert check.allowed and the_case(store, s).evidence[1].document_sha256 is None


def test_evidence_the_file_cannot_use_is_refused_and_says_why():
    s = "visitor:e2"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", NOTICE, source="operator").check == "only staff take a lien step"
    assert record(store, s, t, lease, "memo", {}).check.startswith("the file keeps no evidence of the kind memo")
    no_doc = run(store, s, action="record_lien_evidence", tenant=t, lease=lease, source="staff:front_desk", form={"kind": "notice", "facts": NOTICE})
    assert no_doc.check == "a notice record needs the fingerprint of its document"
    assert record(store, s, t, lease, "notice", NOTICE, document_sha256="abc").check == "a notice record needs the fingerprint of its document"
    bad = record(store, s, t, lease, "notice", {**NOTICE, "itemized_total_cents": 0})
    assert not bad.allowed and bad.check.startswith("the notice record is not complete: itemized_total_cents")
    assert the_case(store, s).evidence == []
    other = "visitor:e3"
    assert record(store, other, t, lease, "notice", NOTICE).check == "no lien case is open on this unit"


def test_a_correction_supersedes_and_nothing_is_edited_or_deleted():
    s = "visitor:e4"
    store, t, lease = prepared(s)
    record(store, s, t, lease, "notice", NOTICE, "first notice")
    fixed = record(store, s, t, lease, "notice", {**NOTICE, "sale_time": "11:00 AM"}, "corrected notice", supersedes=1)
    assert fixed.allowed
    first, second = the_case(store, s).evidence
    assert (first.facts["sale_time"], second.facts["sale_time"], second.supersedes) == ("10:00 AM", "11:00 AM", 1)     # the first is kept as it was
    assert [e.id for e in liens.current_evidence(the_case(store, s))] == [2]
    assert record(store, s, t, lease, "notice", NOTICE, supersedes=1).check == "record 1 was already corrected by record 2; correct the latest one"
    assert record(store, s, t, lease, "notice", NOTICE, supersedes=9).check == "there is no record 9 on this file to correct"
    again = record(store, s, t, lease, "mail_deposit", {**MAIL, "notice_id": 2}, "certificate")
    assert again.allowed
    assert record(store, s, t, lease, "publication", {**AD, "notice_id": 2}, supersedes=3).check == "a correction must be of the same kind as the record it corrects"
    assert record(store, s, t, lease, "legal_check", CHECK).allowed
    assert record(store, s, t, lease, "legal_check", {**CHECK, "check": "military_service"}, supersedes=4).check == "a legal check is corrected only by a check on the same subject"
    assert len(the_case(store, s).evidence) == 4


def test_a_record_that_belongs_to_a_notice_names_the_current_one():
    s = "visitor:e5"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "mail_deposit", MAIL).check == "notice 1 is not a notice on this file"
    record(store, s, t, lease, "notice", NOTICE)
    record(store, s, t, lease, "notice", {**NOTICE, "sale_time": "11:00 AM"}, supersedes=1)
    old = record(store, s, t, lease, "mail_deposit", MAIL)
    assert old.check == "notice 1 was corrected by notice 2; record this for the current notice"
    assert record(store, s, t, lease, "mail_deposit", {**MAIL, "notice_id": 2}).allowed
    assert record(store, s, t, lease, "publication", {**AD, "notice_id": 3}).check == "notice 3 is not a notice on this file"


def test_notice_ids_must_be_positive_integers_for_every_evidence_kind():
    delivery = {"notice_id": 1, "delivered_on": "2026-03-13", "delivered_by": "Jo", "delivered_to": "Ann"}
    email = {"notice_id": 1, "sent_on": "2026-03-13", "sent_to": "ann@example.com", "address_basis": "rental agreement"}
    for kind, facts in (("mail_deposit", MAIL), ("in_person_delivery", delivery), ("email_delivery", email),
                        ("posting", POSTING), ("publication", AD)):
        for invalid in (True, 0, -1):
            with pytest.raises(ValidationError):
                evidence.parse_evidence(kind, {**facts, "notice_id": invalid})
    s = "visitor:invalid-notice-id"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", NOTICE).allowed
    assert not record(store, s, t, lease, "posting", {**POSTING, "notice_id": True}).allowed


def test_evidence_is_still_gathered_under_a_hold_and_the_packet_checklist_follows_it():
    s = "visitor:e6"
    store, t, lease = prepared(s)
    rent = next(c for c in store.books(s).open_charges(t) if c.kind == "rent")
    assert run(store, s, action="open_dispute", tenant=t, charge=rent.id, quote="I paid March in cash").allowed         # the tenancy is held
    assert col.enforcement_hold(store.books(s), lease, "lien", NOW)
    status = lambda: {f.requirement: f.status for f in liens.findings(store.books(s), the_case(store, s))}
    assert set(status().values()) == {"missing"} and len(status()) == 5
    assert record(store, s, t, lease, "notice", NOTICE).allowed
    assert list(status().values()) == ["met", "missing", "missing", "missing", "missing"]
    assert record(store, s, t, lease, "mail_deposit", MAIL).allowed and record(store, s, t, lease, "publication", AD).allowed
    assert status()["posting of the notice"] == "missing"
    assert record(store, s, t, lease, "posting", POSTING).allowed
    assert record(store, s, t, lease, "publication", {**AD, "published_on": "2026-04-10"}, "second advertisement").allowed
    assert set(status().values()) == {"met"}
    record(store, s, t, lease, "notice", {**NOTICE, "sale_time": "11:00 AM"}, supersedes=1)         # a corrected notice must be served again
    assert list(status().values()) == ["met", "missing", "missing", "missing", "missing"]


ADDRESS = {"address": "9 Oak Ave, Orlando, FL", "basis": "rental agreement"}


def staff(store, stream, t, lease, action, form=None, source="staff:front_desk", **kw):
    return run(store, stream, action=action, tenant=t, lease=lease, source=source, form=form or {}, **kw)


def prepared(stream):
    """A case with a written address whose notice copy has been prepared."""
    store, t, lease = opened(stream)
    assert staff(store, stream, t, lease, "record_lien_address", ADDRESS).allowed
    assert staff(store, stream, t, lease, "prepare_lien_notice").allowed
    return store, t, lease


def state_of(store, stream, now=NOW):
    return liens.case_state(store.books(stream), the_case(store, stream), now)


def test_preparing_the_notice_keeps_an_itemized_copy_of_the_ledger_as_it_stood():
    s = "visitor:c1"
    store, t, lease = opened(s)
    assert state_of(store, s)[0] == "review"
    assert staff(store, s, t, lease, "prepare_lien_notice").check == (
        "no written address is on file; record the one from the signed agreement or a written change of address first")
    assert not staff(store, s, t, lease, "record_lien_address", {**ADDRESS, "address": "x"}).allowed
    assert staff(store, s, t, lease, "record_lien_address", {**ADDRESS, "basis": "a phone call"}).check == (
        "an address comes from the rental agreement or a written change of address")
    assert staff(store, s, t, lease, "record_lien_address", ADDRESS).allowed
    d = staff(store, s, t, lease, "prepare_lien_notice")
    assert d.allowed and d.check == "the notice copy is prepared from the ledger as it stands: $120.00 across 2 charges"
    case = the_case(store, s)
    (made,) = case.copies
    assert (made["version"], made["by"], made["at"]) == (1, "staff:front_desk", NOW)
    snap = liens.frozen(case)
    assert snap["claim_cents"] == 12000
    assert [(x["kind"], x["cents"], x["due"]) for x in snap["claim"]] == [("rent", 10000, "2026-03-01"), ("late_fee", 2000, "2026-03-06")]
    assert (snap["tenant"], snap["unit"], snap["address"]) == ({"id": t, "name": "Ann Reyes"}, "A1", ADDRESS)
    assert snap["agreement"] == {"lease": lease, "start": store.books(s).leases[lease].start.isoformat(), "rent_cents": 10000, "schedule": ["late_fee"],
                                 "ended": None}
    assert state_of(store, s)[0] == "evidence incomplete"
    assert staff(store, s, t, lease, "prepare_lien_notice").check == "the notice copy is already prepared and nothing has changed since"


def other_unit_payment(led, t, lease):
    led.sign_lease(at_local(date(2026, 2, 20), 10), t, "A2", 10000, [])          # an older debt on another unit takes the money first
    led.receive_payment(t, 3214, NOW + timedelta(hours=1))


def lease_ended(led, t, lease):
    led.end_lease(NOW + timedelta(hours=1), lease)


def paid_then_returned(led, t, lease):
    led.return_payment(NOW + timedelta(hours=2), led.receive_payment(t, 1000, NOW + timedelta(hours=1)))


CHANGES = {                                         # what happens after the notice copy is taken, and the first change the file sees
    "a payment": (lambda led, t, lease: led.receive_payment(t, 1000, NOW + timedelta(hours=1)), "payment"),
    "a payment applied to another unit": (other_unit_payment, "payment"),
    "a payment that came back": (paid_then_returned, "payment"),
    "a new charge": (lambda led, t, lease: led.charge_by_hand(NOW + timedelta(hours=1), lease, ADMIN_FEE, 2500, None), "charge"),
    "a waived charge": (lambda led, t, lease: led.waive(NOW + timedelta(hours=1), next(c.id for c in led.books.open_charges(t) if c.kind == "late_fee"),
                                                        "owner rule", "staff:manager"), "closed"),
    "the lease ending": (lease_ended, "agreement"),
}


def test_any_later_change_marks_the_case_review_required_and_the_copy_stays_as_it_was():
    for n, (what, (change, key)) in enumerate(CHANGES.items()):
        s = f"visitor:c2{n}"
        store, t, lease = prepared(s)
        was = copy.deepcopy(liens.frozen(the_case(store, s)))
        assert liens.changes(store.books(s), the_case(store, s)) == [], what
        do(store, s, lambda led: change(led, t, lease))
        found = liens.changes(store.books(s), the_case(store, s))
        assert found and found[0]["key"].split(":")[0] == key, (what, found)
        where, why = state_of(store, s)
        assert where == "review required" and why.startswith(found[0]["why"]), what
        assert liens.frozen(the_case(store, s)) == was, what
    other = "visitor:c2x"
    store, t, lease = prepared(other)
    do(store, other, lambda led: other_unit_payment(led, t, lease))
    assert "another unit" in liens.changes(store.books(other), the_case(store, other))[0]["why"]
    assert staff(store, other, t, lease, "record_lien_address", {**ADDRESS, "address": "4 Elm St, Tampa, FL", "basis": "written change of address"}).allowed
    assert [c["key"].split(":")[0] for c in liens.changes(store.books(other), the_case(store, other))] == ["payment", "address"]


def test_rent_and_late_fees_billed_on_schedule_after_the_notice_join_the_claim_without_a_review():
    """The lien covers rent and charges "present or future" (83.805) and the notice shows the sum due at its own time (83.806(2)(a)),
    so what billing posts on the signed schedule after the copy is not a change: otherwise rent on the 1st would send every file back
    to review and a fresh notice before any sale could come due. A rent charge posted by hand is still a change."""
    s = "visitor:c2s"
    store, t, lease = prepared(s)
    do(store, s, lambda led: led.post_rent("2026-04"))
    do(store, s, lambda led: led.assess_late_fees("2026-04"))
    books = store.books(s)
    assert {c.obligation for c in books.charges.values() if c.lease == lease} >= {f"rent:{lease}:2026-04", f"late_fee:{lease}:2026-04"}
    assert liens.changes(books, the_case(store, s)) == []
    assert state_of(store, s, NOW + timedelta(days=30))[0] != "review required"
    do(store, s, lambda led: led.charge_by_hand(NOW + timedelta(days=31), lease, "rent", 10000, None))
    assert [c["key"].split(":")[0] for c in liens.changes(store.books(s), the_case(store, s))] == ["charge"]


def test_a_review_is_written_once_per_change_and_opens_one_task_for_staff():
    s = "visitor:c3"
    store, t, lease = prepared(s)
    later = NOW + timedelta(hours=1)
    do(store, s, lambda led: led.receive_payment(t, 1000, later))
    for _ in range(2):                                                                                      # safe to repeat
        do(store, s, lambda led: liens.recheck(led, t, later))
    case = the_case(store, s)
    assert len(case.reviews) == 1 and case.reviews[0]["keys"][0].startswith("payment:") and case.reviews[0]["version"] == 1
    assert [k.kind for k in col.open_tasks(store.books(s))] == ["lien case needs staff"]
    assert staff(store, s, t, lease, "record_lien_address", {**ADDRESS, "address": "4 Elm St, Tampa, FL"}).allowed
    do(store, s, lambda led: liens.recheck(led, t, later))
    assert len(the_case(store, s).reviews) == 2 and len(col.open_tasks(store.books(s))) == 1            # one task for the copy, two history lines
    other = "visitor:c4"
    store2, t2, lease2 = opened(other)                                                                    # nothing to compare before a copy exists
    do(store2, other, lambda led: (led.receive_payment(t2, 1000, later), liens.recheck(led, t2, later)))
    assert the_case(store2, other).reviews == []


def complete(store, s, t, lease):
    """Every record the packet lists, for notice 1."""
    assert record(store, s, t, lease, "notice", NOTICE).allowed
    assert record(store, s, t, lease, "mail_deposit", MAIL).allowed and record(store, s, t, lease, "publication", AD).allowed
    assert record(store, s, t, lease, "posting", POSTING).allowed
    assert record(store, s, t, lease, "publication", {**AD, "published_on": "2026-04-10"}, "second advertisement").allowed


def fingerprint(store, s):
    return liens.fingerprint(store.books(s), the_case(store, s))


def approve(store, s, t, lease, fp="now", source="staff:manager", **kw):
    form = {} if fp is None else {"fingerprint": fingerprint(store, s) if fp == "now" else fp}
    return run(store, s, action="approve_lien_case", tenant=t, lease=lease, source=source, form=form, **kw)


def test_rent_billed_after_the_notice_must_be_paid_too_before_the_file_is_cured():
    s = "visitor:c2t"
    store, t, lease = prepared(s)
    do(store, s, lambda led: led.post_rent("2026-04"))
    first, second = NOW + timedelta(days=21), NOW + timedelta(days=22)
    do(store, s, lambda led: (led.receive_payment(t, 12000, first), liens.recheck(led, t, first)))     # the notice's $120, not April
    assert not the_case(store, s).closed
    do(store, s, lambda led: (led.receive_payment(t, 10000, second), liens.recheck(led, t, second)))
    assert the_case(store, s).closed["kind"] == "cured"


def test_waiving_a_fee_billed_after_the_notice_is_a_change():
    s = "visitor:c2u"
    store, t, lease = prepared(s)
    do(store, s, lambda led: (led.post_rent("2026-04"), led.assess_late_fees("2026-04")))
    assert liens.changes(store.books(s), the_case(store, s)) == []
    fee = next(c.id for c in store.books(s).charges.values() if c.obligation == f"late_fee:{lease}:2026-04")
    do(store, s, lambda led: led.waive(NOW + timedelta(days=26), fee, "owner rule", "staff:manager"))
    assert [c["key"] for c in liens.changes(store.books(s), the_case(store, s))] == [f"closed:{fee}"]


def test_rent_billed_after_an_approval_voids_it_without_sending_the_file_to_review():
    s = "visitor:c2v"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed and liens.approval(store.books(s), the_case(store, s))
    do(store, s, lambda led: led.post_rent("2026-04"))
    assert liens.approval(store.books(s), the_case(store, s)) is None
    assert state_of(store, s, NOW + timedelta(days=21))[0] != "review required"


def test_only_the_manager_records_a_legal_stop_resolved():
    s = "visitor:c9s"
    store, t, lease = opened(s)
    assert stop(store, s, t, lease).allowed
    sid = liens.open_stops(store.books(s), lease)[0]["id"]
    d = resolve(store, s, t, lease, sid, source="staff:front_desk")
    assert not d.allowed and d.check == "only the manager records a legal stop resolved"
    assert liens.open_stops(store.books(s), lease)
    assert resolve(store, s, t, lease, sid).allowed and not liens.open_stops(store.books(s), lease)


def test_a_delivery_or_an_advertisement_counts_only_when_its_own_facts_meet_the_rule():
    s = "visitor:c9d"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", NOTICE).allowed

    def finding(name):
        return next(f for f in liens.findings(store.books(s), the_case(store, s)) if f.requirement == name)

    assert record(store, s, t, lease, "mail_deposit", {**MAIL, "certificate_of_mailing": False}).allowed      # kept on the file as evidence
    assert finding("delivery of the notice").status == "review" and "certificate of mailing" in finding("delivery of the notice").detail
    unanswered = {"notice_id": 1, "sent_on": "2026-03-13", "sent_to": "ann@example.com", "address_basis": "rental agreement"}
    assert record(store, s, t, lease, "email_delivery", unanswered).allowed
    assert finding("delivery of the notice").status == "review"
    answered = {**unanswered, "confirmation": "delivery confirmation", "confirmation_from": "ann@example.com", "confirmed_on": "2026-03-13", "agreement_allows_email": "yes"}
    assert record(store, s, t, lease, "email_delivery", answered).allowed
    assert finding("delivery of the notice").status == "met"
    assert record(store, s, t, lease, "publication", {**AD, "general_circulation_in_facility_area": "unknown"}).allowed
    assert finding("the newspaper advertisement").status == "review"
    assert record(store, s, t, lease, "publication", {**AD, "includes_tenant_name": False}, supersedes=5).allowed
    assert "tenant name" in finding("the newspaper advertisement").detail
    assert record(store, s, t, lease, "publication", AD, supersedes=6).allowed
    assert record(store, s, t, lease, "publication", {**AD, "published_on": "2026-04-10"}).allowed
    assert finding("the newspaper advertisement").status == "met"


def test_a_manager_approves_the_file_as_it_stands_tied_to_its_fingerprint():
    s = "visitor:a1"
    store, t, lease = prepared(s)
    assert approve(store, s, t, lease).check == ("the packet is not complete; it still needs: the notice, delivery of the notice, "
                                                  "posting of the notice, the newspaper advertisement, demand period")
    complete(store, s, t, lease)
    assert state_of(store, s)[0] == "packet assembled"
    assert approve(store, s, t, lease, source="staff:front_desk").check == "only a manager approves a lien case"
    assert approve(store, s, t, lease, source="staff:office_manager").check == "only a manager approves a lien case"
    assert approve(store, s, t, lease, source="operator").check == "only staff take a lien step"
    assert approve(store, s, t, lease, fp=None).check == "an approval names the file as the manager saw it: send its fingerprint"
    assert approve(store, s, t, lease, fp="0" * 64).check == "the file changed since you looked at it; look at it again"
    seen = fingerprint(store, s)
    assert approve(store, s, t, lease, id="approve-1").allowed
    (a,) = the_case(store, s).approvals
    assert (a["id"], a["by"], a["at"], a["fingerprint"], a["voided"]) == (1, "staff:manager", NOW, seen, None)
    assert state_of(store, s)[0] == "approved" and liens.approval(store.books(s), the_case(store, s))["id"] == 1
    assert approve(store, s, t, lease).check == "the file is already approved as it stands"
    assert approve(store, s, t, lease, id="approve-1").repeated                                       # the first decision comes back
    do(store, s, lambda led: liens.recheck(led, t, NOW + timedelta(days=3)))                         # the clock alone never voids an approval
    assert not the_case(store, s).approvals[0]["voided"] and state_of(store, s, NOW + timedelta(days=3))[0] == "approved"


def test_a_hold_stops_the_approval_and_the_gate_says_which_hold():
    s = "visitor:a2"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    rent = next(c for c in store.books(s).open_charges(t) if c.kind == "rent")
    assert run(store, s, action="open_dispute", tenant=t, charge=rent.id, quote="I paid March in cash").allowed
    assert approve(store, s, t, lease).check == "a dispute on this tenancy is open"


def lien_check_added(store, s, t, lease):
    assert record(store, s, t, lease, "legal_check", CHECK).allowed


def new_address(store, s, t, lease):
    assert staff(store, s, t, lease, "record_lien_address", {**ADDRESS, "address": "4 Elm St, Tampa, FL"}).allowed


def bare(change):
    return lambda store, s, t, lease: do(store, s, lambda led: change(led, t, lease))


VOIDERS = {
    "evidence is added": (lien_check_added, "evidence", "the file changed since you looked at it"),
    "a new address": (new_address, "address", "the file is waiting for staff to decide"),
    "a payment": (bare(CHANGES["a payment"][0]), "payment", "the file is waiting for staff to decide"),
    "a new charge": (bare(CHANGES["a new charge"][0]), "charge", "the file is waiting for staff to decide"),
    "a payment that came back": (bare(CHANGES["a payment that came back"][0]), "payment", "the file is waiting for staff to decide"),
}


def test_any_change_to_the_facts_voids_the_approval_and_the_void_stays_in_the_history():
    for n, (what, (change, word, refusal)) in enumerate(VOIDERS.items()):
        s = f"visitor:a3{n}"
        store, t, lease = prepared(s)
        complete(store, s, t, lease)
        seen = fingerprint(store, s)
        assert approve(store, s, t, lease).allowed
        change(store, s, t, lease)
        case = the_case(store, s)
        assert liens.approval(store.books(s), case) is None and state_of(store, s)[0] != "approved", what       # void before anything is written
        assert approve(store, s, t, lease, fp=seen).check.startswith(refusal), what                          # and no later step goes ahead on it
        do(store, s, lambda led: liens.recheck(led, t, NOW + timedelta(hours=3)))
        (a,) = the_case(store, s).approvals
        assert a["voided"] and word in a["voided"]["why"], (what, a["voided"])
        voided_at = a["voided"]["at"]
        do(store, s, lambda led: liens.recheck(led, t, NOW + timedelta(hours=4)))
        assert the_case(store, s).approvals[0]["voided"]["at"] == voided_at and len(the_case(store, s).approvals) == 1     # written once
    s = "visitor:a4"                                                                                          # a payment that came back stays void
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    seen = fingerprint(store, s)
    approve(store, s, t, lease)
    bare(CHANGES["a payment that came back"][0])(store, s, t, lease)
    assert state_of(store, s)[0] == "review required" and liens.approval(store.books(s), the_case(store, s)) is None
    assert not approve(store, s, t, lease, fp=seen).allowed and not approve(store, s, t, lease).allowed


def test_a_new_approval_after_a_corrected_record_leaves_the_voided_one_in_the_history():
    s = "visitor:a5"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed
    lien_check_added(store, s, t, lease)
    do(store, s, lambda led: liens.recheck(led, t, NOW))
    assert state_of(store, s)[0] == "packet assembled"
    assert approve(store, s, t, lease).allowed
    first, second = the_case(store, s).approvals
    assert first["voided"] and second["voided"] is None and first["fingerprint"] != second["fingerprint"]
    assert state_of(store, s)[0] == "approved"


STOP = {"kind": "bankruptcy", "detail": "chapter 7 petition filed March 10, case 26-01234", "source": "the court's notice to the facility"}
WAR = {"kind": "military_service", "detail": "the tenant's spouse reported active duty by letter on March 11", "source": "a letter from the tenant's spouse"}


def stop(store, s, t, lease, form=STOP, source="staff:front_desk", **kw):
    return staff(store, s, t, lease, "record_lien_stop", form, source=source, **kw)


def resolve(store, s, t, lease, stop_id, reason="the case was dismissed on March 20", source="staff:manager", **kw):
    return staff(store, s, t, lease, "resolve_lien_stop", {"stop": stop_id}, source=source, reason=reason, **kw)


def test_a_legal_stop_blocks_the_steps_until_staff_resolve_it_with_a_reason():
    s = "visitor:s1"
    store, t, lease = opened(s)
    assert staff(store, s, t, lease, "record_lien_address", ADDRESS).allowed
    assert stop(store, s, t, lease, source="operator").check == "only staff take a lien step"
    assert stop(store, s, t, lease, {**STOP, "kind": "a hunch"}).check.startswith("a legal stop is one of")
    assert stop(store, s, t, lease, {**STOP, "detail": ""}).check == "a legal stop needs what was reported and where it came from"
    made = stop(store, s, t, lease, id="stop-1")
    assert made.allowed and made.check.startswith("a bankruptcy is recorded as a legal stop")
    (one,) = liens.unit_stops(store.books(s), lease)
    assert (one["id"], one["kind"], one["by"], one["at"], one["resolved"], one["case"]) == ("LS0001", "bankruptcy", "staff:front_desk", NOW, None, "LC0001")
    assert state_of(store, s)[0] == "held" and "a bankruptcy" in state_of(store, s)[1]
    blocked = staff(store, s, t, lease, "prepare_lien_notice")
    assert not blocked.allowed and blocked.check.startswith("a bankruptcy is on the file as a legal stop (chapter 7 petition")
    assert stop(store, s, t, lease).check == "a bankruptcy is already open as a legal stop on this unit"
    assert resolve(store, s, t, lease, "LS0001", source="operator").check == "only staff take a lien step"
    assert resolve(store, s, t, lease, "LS0001", reason="").check == "a stop is lifted with a reason: say why it no longer applies"
    assert resolve(store, s, t, lease, "LS0007").check == "there is no open stop LS0007 on this unit"
    assert resolve(store, s, t, lease, "LS0001").allowed
    assert liens.unit_stops(store.books(s), lease)[0]["resolved"] == {
        "reason": "the case was dismissed on March 20", "by": "staff:manager", "at": NOW, "case": "LC0001"}
    assert resolve(store, s, t, lease, "LS0001").check == "there is no open stop LS0001 on this unit"
    assert staff(store, s, t, lease, "prepare_lien_notice").allowed
    assert stop(store, s, t, lease).allowed                                                            # a new filing is a new stop


def test_no_approval_overrides_a_stop_which_also_voids_the_approval_and_names_itself_before_any_hold():
    s = "visitor:s2"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    seen = fingerprint(store, s)
    assert approve(store, s, t, lease).allowed
    assert stop(store, s, t, lease, WAR).allowed
    case = the_case(store, s)
    assert liens.approval(store.books(s), case) is None and state_of(store, s)[0] == "held"
    rent = next(c for c in store.books(s).open_charges(t) if c.kind == "rent")
    assert run(store, s, action="open_dispute", tenant=t, charge=rent.id, quote="I paid March in cash").allowed        # a hold as well
    for fp in (seen, "now"):
        refused = approve(store, s, t, lease, fp=fp)
        assert not refused.allowed and refused.check.startswith("reported military service is on the file as a legal stop"), refused.check
    assert record(store, s, t, lease, "legal_check", CHECK).allowed                                        # evidence is still gathered
    assert stop(store, s, t, lease, {**STOP, "kind": "court_order"}).allowed and stop(store, s, t, lease, {**STOP, "kind": "secured_party"}).allowed
    do(store, s, lambda led: liens.recheck(led, t, NOW))
    assert "a legal stop was recorded or resolved" in the_case(store, s).approvals[0]["voided"]["why"]
    assert [x["kind"] for x in liens.open_stops(store.books(s), lease)] == ["military_service", "court_order", "secured_party"]


def test_a_resolved_stop_does_not_bring_the_voided_approval_back():
    s = "visitor:s3"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed
    assert stop(store, s, t, lease).allowed and resolve(store, s, t, lease, "LS0001").allowed
    assert state_of(store, s)[0] == "packet assembled" and liens.approval(store.books(s), the_case(store, s)) is None
    assert approve(store, s, t, lease).allowed
    assert [bool(a["voided"]) for a in the_case(store, s).approvals] == [True, False] and state_of(store, s)[0] == "approved"


def test_a_stop_is_recorded_even_while_the_file_waits_for_staff_after_a_payment():
    s = "visitor:s4"
    store, t, lease = prepared(s)
    do(store, s, lambda led: led.receive_payment(t, 1000, NOW + timedelta(hours=1)))
    assert state_of(store, s)[0] == "review required"
    assert stop(store, s, t, lease).allowed and record(store, s, t, lease, "legal_check", CHECK).allowed          # gathered while it waits


def decide(store, s, t, lease, outcome="resume", reason="the tenant paid part and asked us to carry on", source="staff:office_manager", fp="now", **kw):
    form = {"outcome": outcome}
    if outcome == "resume" and fp is not None:
        form["fingerprint"] = fingerprint(store, s) if fp == "now" else fp
    return staff(store, s, t, lease, "decide_lien_case", form, source=source, reason=reason, **kw)


def test_a_payment_that_clears_the_whole_claim_closes_the_episode_as_cured_and_a_new_one_counts_on():
    s = "visitor:p1"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed
    later = NOW + timedelta(hours=2)
    do(store, s, lambda led: (led.receive_payment(t, 12000, later), liens.recheck(led, t, later)))
    case = the_case(store, s)
    assert case.closed["kind"] == "cured" and case.closed["why"] == "cured: the claim was paid in full" and case.closed["at"] == later
    assert state_of(store, s, later)[0] == "closed" and liens.active_case(store.books(s), lease) is None
    assert case.approvals[0]["voided"] and case.reviews == []                                  # no review: the episode ended
    assert col.open_tasks(store.books(s)) == []
    assert staff(store, s, t, lease, "record_lien_evidence", {"kind": "legal_check", "facts": CHECK}).check == "no lien case is open on this unit"
    do(store, s, lambda led: liens.recheck(led, t, later + timedelta(hours=1)))
    assert the_case(store, s).closed["at"] == later                                              # closed once
    gone = run(store, s, later, action="open_lien_case", tenant=t, lease=lease, source="staff:manager")
    assert not gone.allowed and gone.check.startswith("not due on the owner's schedule")        # paid up: the old marker authorizes nothing
    april = at_local(date(2026, 4, 15), 9)
    do(store, s, lambda led: led.post_rent("2026-04"))                                           # a new episode when the unit falls behind again
    assert run(store, s, april, action="open_lien_case", tenant=t, lease=lease, source="staff:manager").allowed
    second = liens.active_case(store.books(s), lease)
    assert (second.id, second.episode) == ("LC0002", 2) and the_case_ids(store, s) == ["LC0001", "LC0002"]


def the_case_ids(store, s):
    return sorted(cases(store, s))


def test_a_partial_payment_freezes_the_file_for_staff_until_they_record_a_decision():
    s = "visitor:p2"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed
    later = NOW + timedelta(hours=1)
    do(store, s, lambda led: (led.receive_payment(t, 1000, later), liens.recheck(led, t, later)))
    case = the_case(store, s)
    assert case.closed is None and case.approvals[0]["voided"] and len(case.reviews) == 1
    assert state_of(store, s, later)[0] == "review required"
    assert [k.kind for k in col.open_tasks(store.books(s))] == ["lien case needs staff"]
    assert not staff(store, s, t, lease, "prepare_lien_notice").allowed and not approve(store, s, t, lease).allowed          # nothing moves
    assert decide(store, s, t, lease, source="operator").check == "only staff take a lien step"
    assert decide(store, s, t, lease, "sell").check == "staff decide to resume the file or to close it"
    assert decide(store, s, t, lease, reason="").check == "a decision is recorded with a reason"
    assert decide(store, s, t, lease).allowed
    case = the_case(store, s)
    assert [(d["outcome"], d["by"], d["reason"], d["version"]) for d in case.decisions] == [
        ("resume", "staff:office_manager", "the tenant paid part and asked us to carry on", 2)]
    assert [c["version"] for c in case.copies] == [1, 2] and liens.frozen(case)["claim_cents"] == 11000           # the books as they stand now
    assert state_of(store, s, later)[0] == "evidence incomplete" and col.open_tasks(store.books(s)) == []       # service is made again for copy 2
    assert decide(store, s, t, lease).check == "there is nothing to decide: nothing has changed since the notice copy was prepared"
    assert record(store, s, t, lease, "notice", notice_for_copy(store, s)).allowed
    assert record(store, s, t, lease, "mail_deposit", {**MAIL, "notice_id": 6}).allowed and record(store, s, t, lease, "publication", {**AD, "notice_id": 6}).allowed
    assert record(store, s, t, lease, "posting", {**POSTING, "notice_id": 6}).allowed
    assert record(store, s, t, lease, "publication", {**AD, "notice_id": 6, "published_on": "2026-04-10"}).allowed
    assert state_of(store, s, later)[0] == "packet assembled"
    assert approve(store, s, t, lease).allowed and state_of(store, s, later)[0] == "approved"                           # a fresh approval
    assert [bool(a["voided"]) for a in the_case(store, s).approvals] == [True, False]


def test_resume_requires_the_fingerprint_staff_reviewed_and_keeps_the_task_open():
    s = "visitor:stale-resume"
    store, t, lease = prepared(s)
    later = NOW + timedelta(hours=1)
    do(store, s, lambda led: (led.receive_payment(t, 1000, later), liens.recheck(led, t, later)))
    seen = fingerprint(store, s)
    assert decide(store, s, t, lease, fp=None).check == "a decision to resume names the file staff saw: send its fingerprint"
    assert record(store, s, t, lease, "legal_check", CHECK).allowed
    assert decide(store, s, t, lease, fp=seen).check == "the file changed since you looked at it; look at it again"
    assert [task.kind for task in col.open_tasks(store.books(s))] == ["lien case needs staff"]
    assert not the_case(store, s).decisions and len(the_case(store, s).copies) == 1
    assert decide(store, s, t, lease).allowed and col.open_tasks(store.books(s)) == []


def test_a_file_with_an_open_stop_can_be_closed_but_the_stop_holds_the_next_episode():
    s = "visitor:p3"
    store, t, lease = prepared(s)
    do(store, s, lambda led: led.receive_payment(t, 1000, NOW + timedelta(hours=1)))
    assert stop(store, s, t, lease).allowed
    blocked = decide(store, s, t, lease)
    assert not blocked.allowed and blocked.check.startswith("a bankruptcy is on the file as a legal stop")
    closed = decide(store, s, t, lease, "close", "the tenant is in bankruptcy; counsel has the file")
    assert closed.allowed and closed.check == "the file is closed by staff; a new lien needs a new episode"
    case = the_case(store, s)
    assert case.closed["kind"] == "staff" and case.closed["by"] == "staff:office_manager" and "counsel" in case.closed["why"]
    assert liens.open_stops(store.books(s), lease)[0]["id"] == "LS0001"
    assert run(store, s, action="open_lien_case", tenant=t, lease=lease, source="staff:manager").allowed
    assert staff(store, s, t, lease, "prepare_lien_notice").check.startswith("a bankruptcy is on the file as a legal stop")


def test_a_card_payment_settled_by_the_processor_closes_a_cured_case_with_no_lien_command_run():
    s = "visitor:w1"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed
    col.pay(store, s, col.SimulatedProcessor(), t, 12000, "card_ok", NOW + timedelta(hours=1))
    case = the_case(store, s)
    assert case.closed["kind"] == "cured" and case.approvals[0]["voided"]


def test_the_gate_writes_what_changed_before_it_decides_even_when_it_refuses():
    s = "visitor:w2"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    seen = fingerprint(store, s)
    assert approve(store, s, t, lease).allowed
    later = NOW + timedelta(hours=1)
    bare(CHANGES["a payment that came back"][0])(store, s, t, lease)                 # a payment and its return, through no lien code
    case = the_case(store, s)
    assert case.reviews == [] and not case.approvals[0]["voided"]                    # nothing is written yet; the books still read void
    refused = run(store, s, later, action="approve_lien_case", tenant=t, lease=lease, source="staff:manager", form={"fingerprint": seen})
    assert not refused.allowed and refused.check.startswith("the file is waiting for staff to decide")
    case = the_case(store, s)
    assert len(case.reviews) == 1 and case.approvals[0]["voided"] and "payment" in case.approvals[0]["voided"]["why"]
    assert [k.kind for k in col.open_tasks(store.books(s))] == ["lien case needs staff"]
    assert not run(store, s, later, action="prepare_lien_notice", tenant=t, lease=lease, source="staff:front_desk").allowed        # still frozen


def test_an_ordinary_command_on_the_account_also_brings_a_lien_file_up_to_date():
    s = "visitor:w3"
    store, t, lease = prepared(s)
    do(store, s, lambda led: led.receive_payment(t, 1000, NOW + timedelta(hours=1)))
    assert the_case(store, s).reviews == []
    charge = next(iter(store.books(s).open_charges(t))).id
    assert run(store, s, action="explain_charge", tenant=t, charge=charge).allowed
    assert len(the_case(store, s).reviews) == 1


LATE = at_local(date(2026, 3, 30), 7)


def a_dispute(store, s, t, lease):
    rent = next(c for c in store.books(s).open_charges(t) if c.kind == "rent")
    assert run(store, s, action="open_dispute", tenant=t, charge=rent.id, quote="I paid March in cash").allowed
    return NOW, "a dispute on this tenancy is open"


def an_exemption(store, s, t, lease):
    form = {"scope": ["lien"], "reason": "tenant in hospital", "until": "2026-03-20"}
    assert run(store, s, action="grant_exemption", tenant=t, lease=lease, form=form, source="staff:manager").allowed
    return NOW, "an exemption holds it until Mar 20: tenant in hospital"


def an_unsettled_payment(store, s, t, lease):
    col.pay(store, s, col.SimulatedProcessor(), t, 1000, "card_pending", NOW)
    return NOW, "a payment from this tenant is waiting for the processor's answer"


def a_plan_in_good_standing(store, s, t, lease):
    active_plan(store, s, t)
    return NOW, "the tenant's payment plan is in good standing"


def a_breached_plan(store, s, t, lease):
    active_plan(store, s, t)
    col.sweep(store, s, LATE)
    return LATE, "the payment plan was breached; staff review it before any step"


def test_a_hold_stops_every_forward_step_and_never_the_gathering_of_evidence():
    for n, hold in enumerate((a_dispute, an_exemption, an_unsettled_payment)):          # a hold that arrives after the notice copy was taken
        s = f"visitor:h1{n}"
        store, t, lease = prepared(s)
        complete(store, s, t, lease)
        assert state_of(store, s)[0] == "packet assembled"
        now, why = hold(store, s, t, lease)
        assert state_of(store, s, now) == ("held", why), why
        refused = run(store, s, now, action="approve_lien_case", tenant=t, lease=lease, source="staff:manager", form={"fingerprint": fingerprint(store, s)})
        assert not refused.allowed and refused.check == why
        assert record(store, s, t, lease, "legal_check", CHECK).allowed and stop(store, s, t, lease).allowed                 # still gathered
        assert staff(store, s, t, lease, "record_lien_address", {**ADDRESS, "address": "4 Elm St, Tampa, FL"}).allowed
        assert resolve(store, s, t, lease, "LS0001").allowed
        assert decide(store, s, t, lease, "close", "the tenant asked to withdraw").allowed                                  # cancelling stays open
    for n, hold in enumerate((a_dispute, an_exemption, an_unsettled_payment)):          # and a hold already there when staff try to resume
        s = f"visitor:h2{n}"
        store, t, lease = prepared(s)
        do(store, s, lambda led: led.receive_payment(t, 1000, NOW + timedelta(hours=1)))
        now, why = hold(store, s, t, lease)
        assert decide(store, s, t, lease, now=now).check == why


def test_a_plan_holds_the_notice_copy_and_a_breached_plan_waits_for_staff():
    for n, hold in enumerate((a_plan_in_good_standing, a_breached_plan)):
        s = f"visitor:h3{n}"
        store, t, lease = opened(s)
        assert staff(store, s, t, lease, "record_lien_address", ADDRESS).allowed
        now, why = hold(store, s, t, lease)
        refused = run(store, s, now, action="prepare_lien_notice", tenant=t, lease=lease, source="staff:front_desk")
        assert not refused.allowed and refused.check == why
        assert state_of(store, s, now) == ("held", why)
        assert record(store, s, t, lease, "legal_check", CHECK).allowed and record(store, s, t, lease, "notice", NOTICE).check.startswith("a notice is drafted")


def test_the_gate_order_is_scope_then_safety_then_the_schedule_then_holds_for_each_step():
    s = "visitor:h4"
    store, t, lease = opened(s)
    a_dispute(store, s, t, lease)
    # scope first, then safety (a written address, a stop, the lease), and the hold last
    assert staff(store, s, t, lease, "prepare_lien_notice", source="operator").check == "only staff take a lien step"
    assert staff(store, s, t, lease, "prepare_lien_notice").check.startswith("no written address is on file")
    assert staff(store, s, t, lease, "record_lien_address", ADDRESS).allowed
    assert staff(store, s, t, lease, "prepare_lien_notice").check == "a dispute on this tenancy is open"
    assert stop(store, s, t, lease, id="stop-first").allowed
    named = staff(store, s, t, lease, "prepare_lien_notice", id="named-once")
    assert named.check.startswith("a bankruptcy is on the file as a legal stop")
    assert resolve(store, s, t, lease, "LS0001").allowed
    assert approve(store, s, t, lease, fp="0" * 64).check == "the notice copy has not been prepared yet; prepare it first"
    assert staff(store, s, t, lease, "prepare_lien_notice", id="named-once").repeated                  # the first decision, not a fresh look
    assert staff(store, s, t, lease, "prepare_lien_notice", id="named-once").check == named.check


def test_a_stop_belongs_to_the_unit_so_a_cure_and_a_new_episode_do_not_escape_it():
    s = "visitor:r1"
    store, t, lease = prepared(s)
    assert stop(store, s, t, lease, WAR).allowed
    later = NOW + timedelta(hours=2)
    do(store, s, lambda led: (led.receive_payment(t, 12000, later), liens.recheck(led, t, later)))          # the claim is paid: the episode is cured
    assert cases(store, s)["LC0001"].closed["kind"] == "cured" and liens.active_case(store.books(s), lease) is None
    do(store, s, lambda led: led.post_rent("2026-04"))                                                       # and the unit falls behind again
    april = at_local(date(2026, 4, 15), 9)
    reopened = run(store, s, april, action="open_lien_case", tenant=t, lease=lease, source="staff:manager")
    assert reopened.allowed and reopened.check.endswith("; a legal stop on this unit still applies")
    second = liens.active_case(store.books(s), lease)
    assert second.episode == 2 and state_of_case(store, s, second.id, april)[0] == "held"
    assert staff(store, s, t, lease, "record_lien_address", ADDRESS, now=april).allowed
    assert staff(store, s, t, lease, "prepare_lien_notice", now=april).check.startswith("reported military service is on the file as a legal stop")
    assert run(store, s, april, action="approve_lien_case", tenant=t, lease=lease, source="staff:manager",
               form={"fingerprint": "0" * 64}).check.startswith("reported military service is on the file as a legal stop")
    assert stop(store, s, t, lease, WAR, now=april).check == "reported military service is already open as a legal stop on this unit"
    assert resolve(store, s, t, lease, "LS0001", "the spouse's orders ended", now=april).allowed        # lifted once, on the file that is open
    (one,) = liens.unit_stops(store.books(s), lease)                                                        # one record, never a second copy
    assert (one["id"], one["case"], one["resolved"]["case"]) == ("LS0001", "LC0001", "LC0002")
    assert liens.open_stops(store.books(s), lease) == []
    assert staff(store, s, t, lease, "prepare_lien_notice", now=april).allowed


def state_of_case(store, stream, case_id, now=NOW):
    return liens.case_state(store.books(stream), cases(store, stream)[case_id], now)


def notice_for_copy(store, s, lease=None, **over):
    """A notice that states the latest notice copy's claim exactly, naming that copy."""
    case = liens.active_case(store.books(s), lease) if lease else the_case(store, s)
    snap = liens.frozen(case)
    items = [{"description": c["kind"].replace("_", " "), "category": "rent" if c["kind"] == "rent" else "fee", "amount_cents": c["cents"], "due_on": c["due"]}
             for c in snap["claim"]]
    return {**NOTICE, "itemized_charges": items, "itemized_total_cents": snap["claim_cents"], "copy_version": case.copies[-1]["version"], "sum_due_since": min(i["due_on"] for i in items), **over}


def test_a_notice_is_drafted_from_a_copy_so_it_names_one_that_exists_and_a_legal_check_needs_none():
    s = "visitor:v0"
    store, t, lease = opened(s)
    assert record(store, s, t, lease, "legal_check", CHECK).allowed
    assert record(store, s, t, lease, "notice", NOTICE).check == "a notice is drafted from the notice copy; prepare the copy first"
    assert staff(store, s, t, lease, "record_lien_address", ADDRESS).allowed and staff(store, s, t, lease, "prepare_lien_notice").allowed
    assert record(store, s, t, lease, "notice", {**NOTICE, "copy_version": 2}).check == "the file has no notice copy 2"
    assert not record(store, s, t, lease, "notice", {**NOTICE, "copy_version": 0}).allowed
    assert record(store, s, t, lease, "notice", NOTICE).allowed


def paid_part(led, t, lease):
    led.receive_payment(t, 1000, NOW + timedelta(hours=1))


def paid_part_and_charged_more(led, t, lease):
    paid_part(led, t, lease)
    led.charge_by_hand(NOW + timedelta(hours=1), lease, ADMIN_FEE, 5000, None)


def test_a_resume_needs_new_service_for_the_new_copy_whatever_changed_the_claim():
    for n, (what, change, claim) in enumerate((("a payment and a larger new charge", paid_part_and_charged_more, 16000),
                                                ("a part payment", paid_part, 11000), ("a new address", None, 12000))):
        s = f"visitor:v1{n}"
        store, t, lease = prepared(s)
        complete(store, s, t, lease)
        status = lambda: {f.requirement: f.status for f in liens.findings(store.books(s), the_case(store, s))}
        assert set(status().values()) == {"met"}, what
        later = NOW + timedelta(hours=1)
        if change:
            do(store, s, lambda led: change(led, t, lease))
        else:
            assert staff(store, s, t, lease, "record_lien_address", {**ADDRESS, "address": "4 Elm St, Tampa, FL"}).allowed
        assert decide(store, s, t, lease, now=later).allowed
        assert liens.frozen(the_case(store, s))["claim_cents"] == claim and the_case(store, s).copies[-1]["version"] == 2
        assert status() == {"the notice": "missing", "delivery of the notice": "missing", "posting of the notice": "missing", "the newspaper advertisement": "missing", "demand period": "missing"}, what
        assert "copy 1" in liens.findings(store.books(s), the_case(store, s))[0].detail and "copy 2" in liens.findings(store.books(s), the_case(store, s))[0].detail
        assert state_of(store, s, later)[0] == "evidence incomplete"
        refused = approve(store, s, t, lease)
        assert refused.check == ("the packet is not complete; it still needs: the notice, delivery of the notice, posting of the notice, the newspaper advertisement, demand period"), what
        assert record(store, s, t, lease, "notice", notice_for_copy(store, s), "the second notice").allowed        # service for copy 2 counts
        assert list(status().values()) == ["met", "missing", "missing", "missing", "missing"]
        mailed_to = {} if change else {"address": "4 Elm St, Tampa, FL"}          # a mailing goes to the last written address on the file
        assert record(store, s, t, lease, "mail_deposit", {**MAIL, "notice_id": 6, **mailed_to}).allowed and record(store, s, t, lease, "publication", {**AD, "notice_id": 6}).allowed
        assert record(store, s, t, lease, "posting", {**POSTING, "notice_id": 6}).allowed
        assert record(store, s, t, lease, "publication", {**AD, "notice_id": 6, "published_on": "2026-04-10"}).allowed
        assert set(status().values()) == {"met"} and approve(store, s, t, lease).allowed, what


def two_units(stream):
    """One tenant behind on two units, both flagged, both with a case open, a written address and a notice copy."""
    store, t, lease = flagged(stream=stream)
    other = do(store, stream, lambda led: led.sign_lease(at_local(date(2026, 2, 20), 10), t, "A2", 10000, []))
    assert run(store, stream, action="flag_lien_eligible", tenant=t, lease=other, source="staff:manager").allowed
    for unit in (lease, other):
        assert run(store, stream, action="open_lien_case", tenant=t, lease=unit, source="staff:manager").allowed
        assert staff(store, stream, t, unit, "record_lien_address", ADDRESS).allowed
        assert staff(store, stream, t, unit, "prepare_lien_notice").allowed
        assert record(store, stream, t, unit, "notice", notice_for_copy(store, stream, unit)).allowed          # each unit's notice states its own claim
        assert record(store, stream, t, unit, "mail_deposit", MAIL).allowed and record(store, stream, t, unit, "publication", AD).allowed
        assert record(store, stream, t, unit, "posting", POSTING).allowed
        assert record(store, stream, t, unit, "publication", {**AD, "published_on": "2026-04-10"}).allowed
    return store, t, lease, other


def test_bankruptcy_and_military_service_protect_the_person_so_they_hold_every_unit_of_the_tenant():
    for n, (kind, form) in enumerate((("bankruptcy", STOP), ("military_service", WAR))):
        s = f"visitor:u1{n}"
        store, t, one, two = two_units(s)
        case_two = liens.active_case(store.books(s), two)
        assert approve_unit(store, s, t, two).allowed and state_of_case(store, s, case_two.id)[0] == "approved"
        assert stop(store, s, t, one, form).allowed                                                       # recorded on unit one
        books = store.books(s)
        assert [x["id"] for x in liens.unit_stops(books, two)] == ["LS0001"] == [x["id"] for x in liens.unit_stops(books, one)]
        where, why = state_of_case(store, s, case_two.id)
        assert where == "held" and why.startswith(liens.STOP_WORDS[kind]), kind                           # and it holds unit two
        assert liens.approval(books, liens.active_case(books, two)) is None                                # voiding the approval already given
        assert "a legal stop was recorded or resolved" in liens.active_case(books, two).approvals[0]["voided"]["why"]
        named = f"{liens.STOP_WORDS[kind]} recorded for this tenant on unit A1"                              # the stop says which unit it came from
        refused = staff(store, s, t, two, "prepare_lien_notice")
        assert not refused.allowed and refused.check.startswith(f"{named} is on the file as a legal stop")
        assert approve_unit(store, s, t, two, fp="0" * 64).check.startswith(f"{named} is on the file as a legal stop")
        assert decide(store, s, t, two, "close", "withdrawn").allowed
        assert liens.open_stops(store.books(s), two)[0]["id"] == "LS0001"
        assert stop(store, s, t, two, form).check == f"{named} is already open as a legal stop"
        assert staff(store, s, t, one, "prepare_lien_notice").check.startswith(f"{liens.STOP_WORDS[kind]} is on the file as a legal stop")   # its own unit: no "recorded on"
        assert resolve(store, s, t, two, "LS0001", "the stay was lifted").allowed                            # one resolution, made on the other unit
        assert liens.open_stops(store.books(s), one) == [] and liens.open_stops(store.books(s), two) == []
        assert state_of_case(store, s, liens.active_case(store.books(s), one).id)[0] == "packet assembled"


def test_a_court_order_and_a_secured_party_name_specific_property_so_they_hold_only_their_own_unit():
    for n, kind in enumerate(("court_order", "secured_party")):
        s = f"visitor:u2{n}"
        store, t, one, two = two_units(s)
        assert approve_unit(store, s, t, two).allowed
        assert stop(store, s, t, one, {**STOP, "kind": kind}).allowed
        books = store.books(s)
        assert liens.open_stops(books, two) == [] and [x["kind"] for x in liens.open_stops(books, one)] == [kind]
        assert state_of_case(store, s, liens.active_case(books, two).id)[0] == "approved"               # unit two goes on
        assert state_of_case(store, s, liens.active_case(books, one).id)[0] == "held"


def approve_unit(store, s, t, lease, fp="now", source="staff:manager", **kw):
    case = liens.active_case(store.books(s), lease)
    form = {"fingerprint": liens.fingerprint(store.books(s), case) if fp == "now" else fp}
    return run(store, s, action="approve_lien_case", tenant=t, lease=lease, source=source, form=form, **kw)


def test_the_daily_sweep_rechecks_a_tenant_with_an_open_lien_case_and_no_plan():
    s = "visitor:d1"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed and col.state(store.books(s)).plans == {}
    later = NOW + timedelta(hours=5)
    do(store, s, lambda led: led.receive_payment(t, 1000, later))
    assert the_case(store, s).reviews == [] and not the_case(store, s).approvals[0]["voided"]              # nothing recorded yet
    col.sweep(store, s, later)
    case = the_case(store, s)
    assert len(case.reviews) == 1 and case.approvals[0]["voided"] and [k.kind for k in col.open_tasks(store.books(s))] == ["lien case needs staff"]
    other = "visitor:d2"
    store2, t2, lease2 = prepared(other)
    do(store2, other, lambda led: led.receive_payment(t2, 12000, later))
    col.sweep(store2, other, later)
    assert the_case(store2, other).closed["kind"] == "cured"


def test_an_email_delivery_date_needs_a_confirmation_that_says_when_it_was_delivered():
    s = "visitor:m1"
    store, t, lease = prepared(s)
    record(store, s, t, lease, "notice", NOTICE)
    sent = {"notice_id": 1, "sent_on": "2026-10-03", "sent_to": "ann@example.com", "address_basis": "rental agreement"}
    assert record(store, s, t, lease, "email_delivery", sent).allowed                                   # an unconfirmed send claims no delivery
    claimed = record(store, s, t, lease, "email_delivery", {**sent, "delivered_on": "2026-10-04"})
    assert not claimed.allowed and claimed.check == "the e-mail record is not complete: a delivery date comes from a confirmation"
    confirmed = {**sent, "delivered_on": "2026-10-04", "confirmation": "return receipt", "confirmed_on": "2026-10-04"}
    assert record(store, s, t, lease, "email_delivery", confirmed).allowed


def test_the_evidence_parser_names_which_errors_a_caller_handles():
    assert evidence.parse_evidence("legal_check", CHECK).answer == "no"
    with pytest.raises(ValueError, match="unsupported evidence kind"):                                  # not a kind the file keeps
        evidence.parse_evidence("memo", {})
    with pytest.raises(ValidationError):                                                                # a kind it keeps, facts that do not fit
        evidence.parse_evidence("legal_check", {**CHECK, "answer": "maybe"})


def test_a_closed_case_has_no_approval_in_force_though_the_history_keeps_it():
    s = "visitor:k1"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed and liens.approval(store.books(s), the_case(store, s)) is not None
    assert decide(store, s, t, lease, "close", "the tenant asked to withdraw").allowed
    case = the_case(store, s)
    assert liens.approval(store.books(s), case) is None and not case.approvals[0]["voided"] and len(case.approvals) == 1       # kept as it was


def test_a_hold_that_takes_away_every_past_due_charge_still_shows_the_file_as_held():
    s = "visitor:k2"
    store, t, lease = prepared(s)
    complete(store, s, t, lease)
    assert approve(store, s, t, lease).allowed
    charges = store.books(s).open_charges(t)
    do(store, s, lambda led: [led.open_dispute(NOW, c.id, "I paid in cash", True, "staff:front_desk") for c in charges])
    assert col.step_state(store.books(s), lease, "lien", NOW, ignore_recorded=True)[0] == "clear"     # held charges are not past due
    assert state_of(store, s) == ("held", "a dispute on this tenancy is open")
    other = "visitor:k3"                                                                                 # and the gate keeps its own schedule check
    store2, t2, lease2 = prepared(other)
    complete(store2, other, t2, lease2)
    do(store2, other, lambda led: [led.open_dispute(NOW, c.id, "I paid in cash", True, "staff:front_desk") for c in store2.books(other).open_charges(t2)])
    refused = approve(store2, other, t2, lease2)
    assert not refused.allowed and refused.check.startswith("not due on the owner's schedule")


def test_a_case_step_names_the_open_case_and_a_command_for_an_earlier_episode_is_refused():
    s = "visitor:n1"
    store, t, lease = prepared(s)
    step = lambda action, form, now=NOW, **kw: run(store, s, now, action=action, tenant=t, lease=lease, source="staff:office_manager", form=form, **kw)
    assert step("decide_lien_case", {"outcome": "close", "case": None}, reason="paid").check == (
        "name the lien case this step is for; the open case on this unit is LC0001")
    assert step("record_lien_address", {**ADDRESS, "case": "LC0009"}).check == "lien case LC0009 is not the open case on this unit; the open case is LC0001"
    later = NOW + timedelta(hours=2)
    do(store, s, lambda led: (led.receive_payment(t, 12000, later), liens.recheck(led, t, later)))      # episode 1 is cured
    do(store, s, lambda led: led.post_rent("2026-04"))
    april = at_local(date(2026, 4, 15), 9)
    assert run(store, s, april, action="open_lien_case", tenant=t, lease=lease, source="staff:manager").allowed      # episode 2 opens
    assert liens.active_case(store.books(s), lease).id == "LC0002"
    stale = step("decide_lien_case", {"outcome": "close", "case": "LC0001"}, april, reason="prepared before the cure")
    assert not stale.allowed and stale.check == "lien case LC0001 is not the open case on this unit; the open case is LC0002"
    assert cases(store, s)["LC0002"].closed is None
    assert step("record_lien_address", {**ADDRESS, "case": "LC0001"}, april).allowed is False and the_second(store, s).addresses == []
    assert step("decide_lien_case", {"outcome": "close", "case": "LC0002"}, april, reason="paid an arrangement").allowed         # naming it still works
    assert cases(store, s)["LC0002"].closed["kind"] == "staff" and cases(store, s)["LC0001"].closed["kind"] == "cured"


def the_second(store, s):
    return cases(store, s)["LC0002"]


def test_a_stop_is_recorded_and_resolved_on_the_unit_whether_or_not_a_case_is_open():
    s = "visitor:q1"
    store, t, lease = prepared(s)
    assert stop(store, s, t, lease).allowed
    later = NOW + timedelta(hours=2)
    do(store, s, lambda led: (led.receive_payment(t, 12000, later), liens.recheck(led, t, later)))      # the claim is paid: the episode is cured
    assert liens.active_case(store.books(s), lease) is None and [x["id"] for x in liens.open_stops(store.books(s), lease)] == ["LS0001"]
    assert resolve(store, s, t, lease, "LS0001", "counsel confirms the petition was withdrawn", now=later, source="operator").check == "only staff take a lien step"
    assert resolve(store, s, t, "L0999", "LS0001", now=later).check == "no such unit on this tenant's account"
    assert resolve(store, s, t, lease, "LS0001", "", now=later).check == "a stop is lifted with a reason: say why it no longer applies"
    assert resolve(store, s, t, lease, "LS0001", "counsel confirms the petition was withdrawn", now=later).allowed       # no open case needed
    (one,) = liens.unit_stops(store.books(s), lease)
    assert one["resolved"]["case"] is None and one["case"] == "LC0001" and liens.open_stops(store.books(s), lease) == []
    news = stop(store, s, t, lease, WAR, now=later)                                                          # a later report, with no case open
    assert news.allowed and liens.unit_stops(store.books(s), lease)[-1]["case"] is None
    do(store, s, lambda led: led.post_rent("2026-04"))
    april = at_local(date(2026, 4, 15), 9)
    reopened = run(store, s, april, action="open_lien_case", tenant=t, lease=lease, source="staff:manager")
    assert reopened.allowed and reopened.check.endswith("a legal stop on this unit still applies")
    assert state_of_case(store, s, "LC0002", april)[0] == "held"


def findings_of(store, s):
    return {f.requirement: f for f in liens.findings(store.books(s), the_case(store, s))}


def test_a_notice_counts_only_if_it_states_the_claim_of_the_copy_it_names():
    for n, (what, bad, gap) in enumerate((
            ("a smaller total", {"itemized_charges": NOTICE["itemized_charges"][:1], "itemized_total_cents": 10000}, "it states $100.00 and the copy's claim is $120.00"),
            ("other charges with the same total", {"itemized_charges": [{**NOTICE["itemized_charges"][0], "amount_cents": 11000},
                                                                          {**NOTICE["itemized_charges"][1], "amount_cents": 1000}]},
             "its itemized charges differ from the copy's"))):
        s = f"visitor:x1{n}"
        store, t, lease = prepared(s)
        assert record(store, s, t, lease, "notice", {**NOTICE, **bad}).allowed                            # staff can record it as evidence
        assert record(store, s, t, lease, "mail_deposit", MAIL).allowed and record(store, s, t, lease, "publication", AD).allowed
        found = findings_of(store, s)
        assert found["the notice"].status == "review" and gap in found["the notice"].detail, what
        assert found["delivery of the notice"].status == "missing"                                       # nothing counts for a notice that does not match
        assert state_of(store, s)[0] == "evidence incomplete"
        refused = approve(store, s, t, lease)
        assert not refused.allowed and refused.check.startswith("the packet is not complete; it still needs: the notice (record 1 does not match copy 1: "), what
        assert record(store, s, t, lease, "notice", NOTICE, "the corrected notice", supersedes=1).allowed     # a matching one is eligible
        assert findings_of(store, s)["the notice"].status == "met"
        assert record(store, s, t, lease, "mail_deposit", {**MAIL, "notice_id": 4}).allowed and record(store, s, t, lease, "publication", {**AD, "notice_id": 4}).allowed
        assert record(store, s, t, lease, "posting", {**POSTING, "notice_id": 4}).allowed
        assert record(store, s, t, lease, "publication", {**AD, "notice_id": 4, "published_on": "2026-04-10"}).allowed
        assert approve(store, s, t, lease).allowed, what


def test_an_advertisement_counts_only_if_it_describes_the_sale_the_notice_names():
    for n, (what, bad, gap) in enumerate((
            ("another date", {"sale_date": "2026-04-21"}, "sale date Apr 21 where the notice says Apr 20"),
            ("another time", {"sale_time": "2:00 PM"}, "sale time 2:00 PM where the notice says 10:00 AM"),
            ("another place", {"sale_place": "at the facility office"}, "sale place at the facility office where the notice says online, on the auction site"))):
        s = f"visitor:x2{n}"
        store, t, lease = prepared(s)
        assert record(store, s, t, lease, "notice", NOTICE).allowed and record(store, s, t, lease, "mail_deposit", MAIL).allowed
        assert record(store, s, t, lease, "publication", {**AD, **bad}).allowed                           # recorded as evidence
        ad = findings_of(store, s)["the newspaper advertisement"]
        assert ad.status == "review" and gap in ad.detail, what
        assert not approve(store, s, t, lease).allowed and state_of(store, s)[0] == "evidence incomplete"
        assert record(store, s, t, lease, "publication", AD, "the corrected advertisement", supersedes=3).allowed      # correct it
        assert record(store, s, t, lease, "posting", POSTING).allowed
        assert record(store, s, t, lease, "publication", {**AD, "published_on": "2026-04-10"}).allowed
        assert findings_of(store, s)["the newspaper advertisement"].status == "met" and approve(store, s, t, lease).allowed, what
    s = "visitor:x29"                                                                                     # the same time written another way still matches
    store, t, lease = prepared(s)
    complete_with(store, s, t, lease, {"sale_time": "10 am", "sale_place": "Online,  on the auction site"})
    assert findings_of(store, s)["the newspaper advertisement"].status == "met"


def complete_with(store, s, t, lease, ad):
    assert record(store, s, t, lease, "notice", NOTICE).allowed and record(store, s, t, lease, "mail_deposit", MAIL).allowed
    assert record(store, s, t, lease, "publication", {**AD, **ad}).allowed
    assert record(store, s, t, lease, "posting", POSTING).allowed
    assert record(store, s, t, lease, "publication", {**AD, **ad, "published_on": "2026-04-10"}).allowed


def test_online_sale_places_preserve_path_and_query_case():
    s = "visitor:url-sale"
    store, t, lease = prepared(s)
    url = "https://Auction.Example/Sale/ABC?lot=One"
    assert record(store, s, t, lease, "notice", {**NOTICE, "sale_place": url}).allowed
    for place, status in (("HTTPS://auction.example/Sale/ABC?lot=One", "met"),
                          ("https://auction.example/sale/ABC?lot=One", "review"),
                          ("https://auction.example/Sale/ABC?lot=one", "review")):
        assert record(store, s, t, lease, "publication", {**AD, "sale_place": place}).allowed
        ad = liens.current_evidence(the_case(store, s))[-1]
        notice = liens.current_evidence(the_case(store, s))[0]
        assert ("review" if liens._sale_gap(ad, notice) else "met") == status
