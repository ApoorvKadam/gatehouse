"""Run every Florida label of the frozen lien set (core/graded/liens.json, part 2) on the core: build each timeline on the books,
ask what the label asks, and check the labelled decision and that none of its never records exist.

Some timelines are built as closely as the core allows, not word for word:
- l20, l21, l23: the core refuses a notice with no description, no deadline or no owner contact at the door, so the notice never
  reaches the file and its mailing and posting cannot be recorded; "not accepted" is then the file showing no notice.
- l9: a revised deadline is a new notice record that supersedes the first; the e-mail belongs to the first notice.
- l48: the core keeps one case per unit, so a notice that adds the other unit's debt is a notice that differs from its own copy.
- l65, l66: a titled vehicle reaches the file as a legal check that is not "no"; the 60-day timing and the counsel task are not kept.
- l73, l76: the core never edits a record, so an edited notice or a changed sale date is a new notice that supersedes the delivered one,
  and it has no delivery, posting or advertisement of its own.
The effects some labels list (staff and counsel tasks, an approval tied to the fingerprint) are not all checked here; only the
operative date is. The later parts (the auction, the money after a sale) are not in this file because no part 2 label needs them."""
import json
from datetime import date
from pathlib import Path

import pytest

from core import liens
from core.books import at_local
from core.lien_evidence import parse_evidence
from tests.test_core_delinquency import do, world
from tests.test_core_liens import AD, ADDRESS, MAIL, NOTICE, POSTING, approve, run, staff, the_case


SET = json.loads((Path(__file__).parent.parent / "core/graded/liens.json").read_text())
ITEMS = {item["id"]: item for item in SET["graded"] if item["label"]["part"] == 2}

BILLED_TWICE = {"l5", "l11", "l13", "l15", "l19"}                     # October rent and its late fee are on the books too
EMAIL = {"l7", "l8", "l10", "l64"}                                    # the notice went by e-mail
UNCONFIRMED = {"l10", "l64"}                                          # and nothing came back
ADS = {"l5", "l10", "l13", "l14", "l15", "l16", "l17", "l18", "l48", "l64", "l65", "l66", "l74", "l75", "l76"}
AD_DAYS = {"l14": ["2026-11-01"], "l16": ["2026-11-02"], "l17": ["2026-11-02", "2026-11-04"]}
NOTICE_LACKS = {"l20": "property_description", "l21": "demand_deadline", "l23": "owner_name"}     # the field the gate names when it refuses
VEHICLE = {"l65": ("yes", "the manager notes a titled vehicle"), "l66": ("unknown", "staff review: no title search on file")}
# the words a refused approval must give, so the refusal is for the labelled reason and not for some other gap
REFUSAL_WORDS = {"l16": "newspaper advertisement", "l48": "does not match copy", "l64": "e-mail fallback mailing", "l65": "vehicle or watercraft",
                 "l66": "vehicle or watercraft", "l76": "delivery of the notice"}


def when(day, hour=10):
    return at_local(date.fromisoformat(day), hour)


class Built:
    """One timeline on the books, with a record of everything put on the file so that an edit can be seen."""

    def __init__(self, item_id):
        self.id, self.s = item_id, "visitor:set-" + item_id
        self.store, self.tenant, self.lease = world(unpaid="2026-09", schedule=(10, 45))
        self.puts = []            # (kind, facts as the file keeps them, document fingerprint) in the order recorded
        self.refused = None       # the gate's answer when the notice itself was refused

    def case(self):
        return the_case(self.store, self.s)

    def found(self):
        return {f.requirement: f for f in liens.findings(self.store.books(self.s), self.case())}

    def staff(self, action, form, day):
        result = staff(self.store, self.s, self.tenant, self.lease, action, form, now=when(day))
        assert result.allowed, result.check

    def put(self, kind, facts, day, document="a", allowed=True, **form):
        form = {"kind": kind, "facts": facts, **({"document_sha256": document * 64} if kind != "legal_check" else {}), **form}
        result = run(self.store, self.s, when(day), action="record_lien_evidence", tenant=self.tenant, lease=self.lease, source="staff:front_desk", form=form)
        assert result.allowed == allowed, result.check
        if allowed:
            self.puts.append((kind, parse_evidence(kind, facts).model_dump(mode="json"), form.get("document_sha256")))
        return result


def ad_facts(item_id, n, day, notice_id=1, sale_date="2026-11-20"):
    extra = {"l18": {"proof": "clipping"},
             "l74": {"proof": "staff_entry"} if n == 1 else {},
             "l75": {"proof_tenant_name": "Bo Chen", "proof_facility_address": "500 Lake Rd, Tampa, FL"}}.get(item_id, {})
    return {**AD, "notice_id": notice_id, "published_on": day, "sale_date": sale_date, **extra}


def the_notice(built, item_id, sale_date):
    charges = liens.frozen(built.case())["claim"]
    if item_id == "l48":          # one notice for two units: the other unit's debt is added to this unit's charges
        charges = [{"kind": c.kind, "cents": c.open_cents, "due": c.due.isoformat()} for c in built.store.books(built.s).open_charges(built.tenant)]
    notice = {**NOTICE, "itemized_charges": [{"description": c["kind"], "category": "rent" if c["kind"] == "rent" else "fee", "amount_cents": c["cents"], "due_on": c["due"]} for c in charges],
              "itemized_total_cents": sum(c["cents"] for c in charges), "sum_due_since": min(c["due"] for c in charges),
              "demand_deadline": "2026-10-31" if item_id == "l12" else "2026-11-04" if item_id == "l9" else "2026-11-01", "sale_date": sale_date}
    if item_id == "l19":
        notice["itemized_total_cents"] = 26000
    if item_id == "l22":
        notice["sale_statement"] = False
    return notice


def l9(built, notice):
    """The e-mail of Oct 18 gets no answer; the revised notice (a deadline of Nov 4) goes by first-class mail on Oct 21."""
    first = {**notice, "demand_deadline": "2026-11-01"}
    built.put("notice", first, "2026-10-18")
    built.put("email_delivery", {"notice_id": 1, "sent_on": "2026-10-18", "sent_to": "ann@example.com", "address_basis": "rental agreement",
                                 "agreement_allows_email": "yes"}, "2026-10-18")
    built.put("notice", notice, "2026-10-21", document="b", supersedes=1)
    revised = built.case().evidence[-1].id
    built.put("mail_deposit", {**MAIL, "notice_id": revised, "deposited_on": "2026-10-21"}, "2026-10-21")
    built.put("posting", {**POSTING, "notice_id": revised, "posted_on": "2026-10-21"}, "2026-10-21")
    for n, day in enumerate(["2026-11-05", "2026-11-12"]):
        built.put("publication", ad_facts("l9", n, day, revised, notice["sale_date"]), day)


def packet(item_id):
    built = Built(item_id)
    store, s, tenant, lease = built.store, built.s, built.tenant, built.lease
    if item_id in BILLED_TWICE:
        do(store, s, lambda led: (led.post_rent("2026-10"), led.assess_late_fees("2026-10")))
    if item_id == "l48":          # the same tenant also owes rent on a second unit
        do(store, s, lambda led: led.sign_lease(at_local(date(2026, 8, 1), 10), tenant, "A2", 10000, []))
    for action in ("flag_lien_eligible", "open_lien_case"):
        assert run(store, s, when("2026-10-17"), action=action, tenant=tenant, lease=lease, source="staff:manager").allowed
    built.staff("record_lien_address", ADDRESS, "2026-10-17")
    built.staff("prepare_lien_notice", {}, "2026-10-17")
    notice = the_notice(built, item_id, "2026-11-22" if item_id == "l9" else "2026-11-20")
    if item_id in NOTICE_LACKS:
        if item_id == "l20":
            lacking = {**notice, "property_description": ""}
        elif item_id == "l21":
            lacking = {k: v for k, v in notice.items() if k != "demand_deadline"}
        else:
            lacking = {k: v for k, v in notice.items() if not k.startswith("owner_")}
        built.refused = built.put("notice", lacking, "2026-10-18", allowed=False)
        return built
    if item_id == "l9":
        l9(built, notice)
        return built
    built.put("notice", notice, "2026-10-18")
    if item_id in EMAIL:
        email = {"notice_id": 1, "sent_on": "2026-10-18", "sent_to": "ann@example.com", "address_basis": "rental agreement",
                 "agreement_allows_email": "no" if item_id == "l8" else "yes"}
        if item_id not in UNCONFIRMED:
            email.update(confirmation="delivery confirmation", confirmation_from="ann@example.com", confirmed_on="2026-10-18")
        built.put("email_delivery", email, "2026-10-18")
    else:
        built.put("mail_deposit", {**MAIL, "deposited_on": "2026-10-18", "certificate_of_mailing": item_id != "l6"}, "2026-10-18")
    built.put("posting", {**POSTING, "posted_on": "2026-10-18"}, "2026-10-18")
    if item_id in ADS:
        for n, day in enumerate(AD_DAYS.get(item_id, ["2026-11-02", "2026-11-09"])):
            built.put("publication", ad_facts(item_id, n, day), day)
    if item_id in VEHICLE:
        answer, source = VEHICLE[item_id]
        built.put("legal_check", {"check": "vehicle_or_watercraft", "answer": answer, "source": source, "observed_on": "2026-10-17" if item_id == "l65" else "2026-11-19"},
                  "2026-11-19")
    if item_id == "l44":          # the signed change of address is found after the mailing went to the old address
        built.staff("record_lien_address", {"address": "4 Elm St, Tampa, FL", "basis": "written change of address"}, "2026-10-19")
    if item_id == "l73":          # the saved notice is edited: the file keeps the first record and takes the edit as a new one
        built.put("notice", {**notice, "property_description": "the contents of unit A1, as listed in the rental agreement"}, "2026-11-15", document="b", supersedes=1)
    if item_id == "l76":          # the sale date is moved on the manager's say-so, with no new notice to the tenant
        built.put("notice", {**notice, "sale_date": "2026-11-25"}, "2026-11-18", document="b", supersedes=1)
    return built


@pytest.mark.parametrize("item_id", list(ITEMS))
def test_florida_label(item_id):
    label = ITEMS[item_id]["label"]
    built = packet(item_id)
    store, s, tenant, lease = built.store, built.s, built.tenant, built.lease
    case, found = built.case(), built.found()
    ask = label["ask"]
    decision = None
    if ask == "approve_lien_case":
        decision = approve(store, s, tenant, lease, now=when("2026-11-16" if item_id == "l15" else "2026-11-19"))
        case = built.case()
        allowed = decision.allowed
        if item_id in REFUSAL_WORDS:
            assert REFUSAL_WORDS[item_id] in decision.check, decision.check
    elif item_id in NOTICE_LACKS:
        assert built.refused.check.startswith("the notice record is not complete: ") and NOTICE_LACKS[item_id] in built.refused.check, built.refused.check
        allowed = found["the notice"].status == "met"
        assert found["the notice"].status == "missing" and not case.evidence
    else:
        requirement = {"count_notice_delivered": "delivery of the notice", "count_publication": "the newspaper advertisement",
                       "accept_notice": "demand period" if item_id in {"l11", "l12"} else "delivery of the notice" if item_id == "l73" else "the notice"}[ask]
        allowed = found[requirement].status == "met"
    assert allowed == (label["decision"] == "allow"), item_id
    for effect in label["effects"]:
        if effect.startswith("operative_date:"):      # "Oct 21": the notice time counts from the delivery that counts
            assert "delivery on 2026-10-21" in found["demand period"].detail and found["demand period"].status == "met"
            assert found["delivery of the notice"].detail == f"record {next(e.id for e in case.evidence if e.kind == 'mail_deposit')}"
    for never in label["never"]:
        if never in {"approval_recorded", "sale_approved", "approval_reused"}:
            assert len(case.approvals) == (1 if never == "approval_reused" else 0)
            if never == "approval_reused":
                assert case.approvals[0]["fingerprint"] == liens.fingerprint(store.books(s), case)
                change = run(store, s, when("2026-11-19"), action="record_lien_evidence", tenant=tenant, lease=lease, source="staff:manager",
                             form={"kind": "legal_check", "facts": {"check": "bankruptcy", "answer": "no", "source": "court record", "observed_on": "2026-11-19"}})
                assert change.allowed
                assert liens.approval(store.books(s), built.case()) is None
        elif never in {"notice_delivered", "publication_counted"}:
            name = "delivery of the notice" if never == "notice_delivered" else "the newspaper advertisement"
            assert found[name].status != "met"
        elif never == "publication_counted_before:Nov 2":
            assert not [e for e in case.evidence if e.kind == "publication"]
        elif never == "operative_date:Oct 18":
            assert "2026-10-18" not in found["demand period"].detail
        elif never == "evidence_edited":
            # every record is still there as it was put, and a correction sits beside the record it replaces
            assert [(e.kind, e.facts, e.document_sha256) for e in case.evidence] == built.puts
            if item_id == "l73":
                first = next(e for e in case.evidence if e.kind == "notice")
                second = next(e for e in case.evidence if e.supersedes)
                assert (first.supersedes, second.supersedes) == (None, first.id) and first.document_sha256 != second.document_sha256
        elif never == "deadline_invented":
            assert not [e for e in case.evidence if e.kind == "notice"]
        elif never == "other_unit_included":
            books = store.books(s)
            assert {c["charge"] for c in liens.frozen(case)["claim"]} <= set(books.of_lease[lease])
            other = next(x.id for x in books.leases.values() if x.id != lease)
            both = approve(store, s, tenant, other, now=when("2026-11-19"))      # the other unit has no case, so the combined file cannot sell it
            assert not both.allowed and both.check == "no lien case is open on this unit"
        elif never == "claim_unexplained_amount":
            assert sum(c.open_cents for c in store.books(s).open_charges(tenant)) == 24000
        elif never == "legal_guarantee_claimed":
            assert "guarantee" not in decision.check.lower()
        else:
            pytest.fail(f"No check for {never}")


def test_every_florida_label_of_the_set_runs():
    assert len(ITEMS) == 28
