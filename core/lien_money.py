"""The money after a sale on Staddle's own auction platform, kept as records beside the lien cases.

Buyer money is not a tenant payment: when the sale is final it is applied once to the lien debt as sale proceeds ("sale.proceeds_applied"),
and what is left is a surplus owed to the tenant, in an account of its own. The surplus is neither income nor credit the tenant can
spend: it has a life of its own (created, tenant notified and held, payout requested, paid), and a claim on it or the two-year mark
goes to a staff review item and never moves money by itself. An agreement to vacate or settle, and a dispute of the sale, are records
too; nothing here reverses a sale."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from . import delinquency as col
from . import liens
from .billing import Ledger
from .books import Books, local
from .evidence import day, money


@dataclass
class Sale:
    """What the sale brought and where it went. The record stands: a dispute is noted on it and nothing is reversed."""
    case: str
    tenant: str
    lease: str
    cents: int                          # the buyer's money
    applied_cents: int                  # what went to the lien debt
    surplus_cents: int
    surplus: str | None
    at: datetime
    disputes: list[dict] = field(default_factory=list)      # each: n, who raised it, what, when, and how staff resolved it


@dataclass
class Surplus:
    """Money left after the debt, owed to the tenant. The amount is fixed when it is created: nothing posted later reduces it."""
    id: str
    case: str
    tenant: str
    lease: str
    cents: int
    created_at: datetime
    state: str = "created"              # created, held (the tenant was notified), payout_requested, paid
    notices: list[dict] = field(default_factory=list)       # how the tenant was told: in person, or first-class mail with a certificate
    reviews: list[dict] = field(default_factory=list)       # a claim or the two-year mark, each for staff until they record it resolved
    payout: dict | None = None
    paid: dict | None = None


@dataclass
class Agreement:
    """A pay-to-vacate or settlement agreement on a case: its terms, the tenant's acceptance, and how it ended."""
    id: str
    case: str
    kind: str
    terms: str
    by: str
    at: datetime
    accepted: dict | None = None
    ended: dict | None = None

    @property
    def active(self) -> bool:
        return self.ended is None


def fold_event(b: Books, kind: str, at: datetime, p: dict) -> None:
    """Fold one sale-money record into the books. Books.apply reaches this through liens.fold_event."""
    st = b.liens
    if kind == "sale.proceeds_applied":
        for a in p["applications"]:
            ch = b.charges[a["charge"]]
            ch.paid += a["cents"]
            ch.paid_effective.append((at, a["cents"], f"sale:{p['case']}"))
        applied = sum(a["cents"] for a in p["applications"])
        st.sales[p["case"]] = Sale(p["case"], p["tenant"], p["lease"], p["cents"], applied, p["surplus_cents"], p["surplus"], at)
        if p["surplus"]:
            st.surpluses[p["surplus"]] = Surplus(p["surplus"], p["case"], p["tenant"], p["lease"], p["surplus_cents"], at)
            b._seen("SP", p["surplus"])
    elif kind == "surplus.paid":
        s = st.surpluses[p["surplus"]]
        s.state, s.paid = "paid", {"by": p["by"], "at": at, "simulated": p["simulated"]}
    elif kind == "lien_case.surplus_notified":
        s = st.surpluses[p["surplus"]]
        s.notices.append({"method": p["method"], "facts": p["facts"], "simulated": p["simulated"], "by": p["by"], "at": at})
        s.state = "held"
    elif kind == "lien_case.surplus_review":
        st.surpluses[p["surplus"]].reviews.append({"id": p["review"], "kind": p["kind"], "detail": p["detail"], "by": p["by"], "at": at, "resolved": None})
    elif kind == "lien_case.surplus_review_resolved":
        st.surpluses[p["surplus"]].reviews[p["review"] - 1]["resolved"] = {"reason": p["reason"], "by": p["by"], "at": at}
    elif kind == "lien_case.surplus_payout_requested":
        s = st.surpluses[p["surplus"]]
        s.state, s.payout = "payout_requested", {"by": p["by"], "at": at}
    elif kind == "lien_case.agreement_recorded":
        st.agreements[p["agreement"]] = Agreement(p["agreement"], p["case"], p["kind"], p["terms"], p["by"], at)
        b._seen("AG", p["agreement"])
    elif kind == "lien_case.agreement_accepted":
        st.agreements[p["agreement"]].accepted = {"accepted_by": p["accepted_by"], "how": p["how"], "document_sha256": p["document_sha256"], "by": p["by"], "at": at}
    elif kind == "lien_case.agreement_ended":
        st.agreements[p["agreement"]].ended = {"how": p["how"], "reason": p["reason"], "by": p["by"], "at": at}
    elif kind == "lien_case.sale_disputed":
        st.sales[p["case"]].disputes.append({"n": p["dispute"], "raised_by": p["raised_by"], "detail": p["detail"], "by": p["by"], "at": at, "resolved": None})
    elif kind == "lien_case.sale_dispute_resolved":
        st.sales[p["case"]].disputes[p["dispute"] - 1]["resolved"] = {"reason": p["reason"], "by": p["by"], "at": at}
    elif kind == "lien_case.return_noted":
        st.returns.setdefault(p["case"], []).append(p["payment"])


# the sale money --------------------------------------------------------------------------------------------------------
SURPLUS_TASK = "surplus needs staff"


def settle(led: Ledger, case: liens.Case, now: datetime) -> None:
    """Apply the buyer's money to the lien debt once the sale is final, and close the episode. The buyer's money was never a tenant
    payment: it is applied here as sale proceeds, oldest charges first on this unit, and what is left over is the tenant's surplus.
    Safe to repeat and to run after a restart: a case whose proceeds are recorded is never applied again."""
    b = led.books
    st = liens.state(b)
    a = st.auctions.get(case.id)
    if a is None or a.status != "sold" or case.id in st.sales or case.closed:
        return
    left, apps = a.buyer_paid_cents, []
    for c in b.open_charges(case.tenant, skip_held=False, lease=case.lease):
        take = min(left, c.open_cents)
        if take:
            apps.append({"charge": c.id, "lease": c.lease, "cents": take})
            left -= take
    cents = a.buyer_paid_cents
    sid = b.next_id("SP") if left else None
    led.emit("sale.proceeds_applied", now, {"case": case.id, "tenant": case.tenant, "lease": case.lease, "cents": cents, "applications": apps,
                                            "surplus_cents": left, "surplus": sid}, f"sale-proceeds:{case.id}", source=col.SYSTEM)
    owing = b.receivable(case.lease)
    why = f"sold at auction for {money(cents)}; {money(cents - left)} went to the debt"
    why += f" and {money(left)} is held for the tenant" if left else ""
    why += f"; {money(owing)} is still owed" if owing else ""
    led.emit("lien_case.closed", now, {"case": case.id, "kind": "sold", "why": why, "by": col.SYSTEM}, f"lien_case:{case.id}:closed", source=col.SYSTEM)
    liens._settle_tasks(led, case, col.SYSTEM, now)
    if owing:
        col._task(led, liens.TASK, case.tenant, f"sale:{case.id}:shortfall", f"The sale did not cover the debt: {money(owing)} is still owed on unit {case.unit}. "
                  "Staff decide what follows.", local(now).date(), now)


def two_year_mark(s: Surplus) -> date:
    created = local(s.created_at).date()
    try:
        return created.replace(year=created.year + 2)
    except ValueError:                  # a surplus made on February 29
        return date(created.year + 2, 3, 1)


def _returns(books: Books, case: liens.Case) -> list:
    """The payments that paid a cured case's claim, came back, and that staff were not yet told about."""
    if not (case.closed and case.closed["kind"] == "cured" and case.copies):
        return []
    seen, snap = liens.state(books).returns.get(case.id, []), liens.frozen(case)
    return sorted((p for p in books.payments.values() if p.tenant == case.tenant and p.returned and p.id not in seen and p.id not in snap["payments_seen"]
                   and any(a["lease"] == case.lease for a in p.applications)), key=lambda p: p.id)


def tenants(books: Books) -> set[str]:
    """The tenants the daily pass must look at for what a closed case still holds: a surplus not yet paid, and a payment that came back
    after the case was cured."""
    st = liens.state(books)
    return ({s.tenant for s in st.surpluses.values() if s.state != "paid"} | {c.tenant for c in st.cases.values() if _returns(books, c)})


def recheck(led: Ledger, tenant: str, now: datetime) -> None:
    """What is due on a tenant's closed cases and surpluses, written once: a surplus held to the two-year mark goes to staff, never to income."""
    today = local(now).date()
    for s in [s for s in liens.state(led.books).surpluses.values() if s.tenant == tenant and s.state != "paid"]:
        if today >= two_year_mark(s) and not any(r["kind"] == "two_year" for r in s.reviews):
            _review(led, s, "two_year", f"two years have passed since the surplus was created on {day(s.created_at)}; staff and counsel decide what "
                    "happens to it, and it is never moved to income", col.SYSTEM, now)
    b, st = led.books, liens.state(led.books)
    for case in [c for c in st.cases.values() if c.tenant == tenant]:
        for p in _returns(b, case):         # the books already reopened the debt; staff are told, and the case is not brought back
            owing = b.receivable(case.lease)
            auction = st.auctions.get(case.id)
            led.emit("lien_case.return_noted", now, {"case": case.id, "payment": p.id, "owed_cents": owing, "by": col.SYSTEM},
                     f"lien_case:{case.id}:return:{p.id}", source=col.SYSTEM)
            col._task(led, liens.TASK, tenant, f"lien:{case.id}:return:{p.id}",
                      f"A {money(p.cents)} payment came back after the case on unit {case.unit} was closed as cured, so {money(owing)} is owed again. "
                      + ("The cancelled auction is never brought back; " if auction and auction.cancel else "") + "a new lien needs a new episode, and staff decide "
                      "whether to open one.", local(now).date(), now)


def _review(led: Ledger, s: Surplus, kind: str, detail: str, by: str, now: datetime, command: str | None = None) -> None:
    n = len(s.reviews) + 1
    led.emit("lien_case.surplus_review", now, {"case": s.case, "surplus": s.id, "review": n, "kind": kind, "detail": detail, "by": by},
             f"surplus:{s.id}:review:{n}", source=by, command=command)
    col._task(led, SURPLUS_TASK, s.tenant, f"surplus:{s.id}:review:{n}", f"Surplus {s.id}, {money(s.cents)} for the tenant: {detail}.", local(now).date(), now)


# the surplus's own steps, as gate commands -----------------------------------------------------------------------------
MANAGER_WORDS = {"resolve_surplus_review": "only a manager records a surplus review resolved", "request_surplus_payout": "only a manager asks for a surplus payout",
                 "confirm_surplus_payout": "only a manager confirms a surplus payout"}


def _real_date(value) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def check(books: Books, lease, cmd, now: datetime) -> tuple[bool, str]:
    """The gate's decision on a surplus step, as (allowed, the reason in plain words). Writes nothing. A surplus belongs to a unit and
    outlives its case, so these steps name the surplus and not an open case."""
    a, form = cmd.action, cmd.form or {}
    if a in liens.SALE_ACTIONS:
        return _sale_check(books, lease, cmd)
    s = liens.state(books).surpluses.get(form.get("surplus"))
    if s is None or s.lease != lease.id:
        return False, f"there is no surplus {form.get('surplus')} on this unit"
    if a in MANAGER_WORDS and cmd.source != liens.APPROVER:
        return False, MANAGER_WORDS[a]
    open_reviews = [r for r in s.reviews if not r["resolved"]]
    if a == "record_surplus_notice":
        if s.state != "created":
            return False, "the tenant was already told about this surplus"
        method, facts = form.get("method"), form.get("facts") if isinstance(form.get("facts"), dict) else {}
        if method not in ("in_person", "first_class_mail"):
            return False, "the tenant is told in person or by first-class mail with a certificate of mailing"
        if method == "in_person":
            if not _real_date(facts.get("delivered_on")):
                return False, "an in-person notice needs the date it was handed over"
        else:
            if not _real_date(facts.get("deposited_on")):
                return False, "a mailing needs the date it was deposited"
            if facts.get("first_class") is not True:
                return False, "a mailing is first-class mail"
            if facts.get("certificate_of_mailing") is not True:
                return False, "a mailing needs a certificate of mailing"
            written = liens.state(books).cases[s.case].addresses
            if written and not liens._same_text(str(facts.get("address") or ""), written[-1]["address"]):
                return False, f"the mailing went to {facts.get('address')}, but the last written address on the file is {written[-1]['address']}"
        return True, "the tenant's notice is recorded, as a simulated delivery; the surplus is held for the tenant"
    if a == "record_surplus_review":
        if s.state == "paid":
            return False, "that surplus was already paid"
        if not isinstance(form.get("detail"), str) or not form["detail"].strip():
            return False, "a claim is recorded with what is claimed and by whom"
        return True, "the claim is recorded and goes to staff review; it moves no money"
    if a == "resolve_surplus_review":
        if not any(r["id"] == form.get("review") for r in open_reviews):
            return False, f"there is no open review {form.get('review')} on this surplus"
        if not (cmd.reason or "").strip():
            return False, "a review is resolved with a reason"
        return True, "the review is recorded as resolved; it moves no money"
    if a == "request_surplus_payout":
        if s.state == "created":
            return False, "the tenant has not been told about this surplus yet; a payout waits for the notice"
        if s.state in ("payout_requested", "paid"):
            return False, "a payout is already requested for this surplus" if s.state == "payout_requested" else "that surplus was already paid"
        if _disputed(books, s):
            return False, DISPUTED
        if open_reviews:
            return False, "a claim or review is open on this surplus; staff record it resolved first"
        return True, "the payout is requested; it is paid when a manager confirms it"
    if s.state == "paid":
        return False, "that surplus was already paid"
    if s.state != "payout_requested":
        return False, "no payout is requested for this surplus"
    if _disputed(books, s):
        return False, DISPUTED
    if open_reviews:            # a claim or the two-year mark that came after the request still stops the money
        return False, "a claim or review is open on this surplus; staff record it resolved first"
    return True, "the payout is recorded as paid, as a simulated payout; nothing was sent"


def act(led: Ledger, cmd, now: datetime) -> None:
    """Record an allowed surplus or sale-dispute step (the gate has checked it)."""
    a, form = cmd.action, cmd.form or {}
    if a in liens.SALE_ACTIONS:
        return _act_sale(led, cmd, now)
    s = liens.state(led.books).surpluses[form["surplus"]]
    if a == "record_surplus_notice":
        facts = {k: form["facts"][k] for k in ("delivered_on", "deposited_on", "first_class", "certificate_of_mailing", "address") if k in form["facts"]}
        led.emit("lien_case.surplus_notified", now, {"case": s.case, "surplus": s.id, "method": form["method"], "facts": facts, "simulated": True,
                                                     "by": cmd.source}, f"surplus:{s.id}:notified", source=cmd.source, command=cmd.id)
    elif a == "record_surplus_review":
        _review(led, s, "claim", form["detail"].strip(), cmd.source, now, cmd.id)
    elif a == "resolve_surplus_review":
        led.emit("lien_case.surplus_review_resolved", now, {"case": s.case, "surplus": s.id, "review": form["review"], "reason": cmd.reason.strip(),
                                                            "by": cmd.source}, f"surplus:{s.id}:review:{form['review']}:resolved", source=cmd.source, command=cmd.id)
        for t in [t for t in col.state(led.books).tasks.values() if t.ref == f"surplus:{s.id}:review:{form['review']}" and not t.acknowledged]:
            led.emit("task.acknowledged", now, {"task": t.id, "by": cmd.source}, f"task:{t.ref}:seen", source=cmd.source, command=cmd.id)
    elif a == "request_surplus_payout":
        led.emit("lien_case.surplus_payout_requested", now, {"case": s.case, "surplus": s.id, "by": cmd.source}, f"surplus:{s.id}:payout",
                 source=cmd.source, command=cmd.id)
    else:
        led.emit("surplus.paid", now, {"surplus": s.id, "tenant": s.tenant, "cents": s.cents, "simulated": True, "by": cmd.source},
                 f"surplus:{s.id}:paid", source=cmd.source, command=cmd.id)


# a sale disputed after closing -----------------------------------------------------------------------------------------
DISPUTE_TASK = "lien sale disputed"
DISPUTED = "the sale is disputed; staff and counsel decide before any surplus is paid out"
RAISED_BY = ("tenant", "buyer", "other")


def _disputed(books: Books, s: Surplus) -> bool:
    sale = liens.state(books).sales.get(s.case)
    return bool(sale and any(not d["resolved"] for d in sale.disputes))


def _sale_check(books: Books, lease, cmd) -> tuple[bool, str]:
    """A dispute of a sale is only recorded: the sale stands, and staff and counsel decide."""
    a, form = cmd.action, cmd.form or {}
    st = liens.state(books)
    case = st.cases.get(form.get("case"))
    if case is None or case.lease != lease.id or case.id not in st.sales:
        return False, f"there is no sold case {form.get('case')} on this unit"
    if a == "dispute_lien_sale":
        if not isinstance(form.get("detail"), str) or not form["detail"].strip():
            return False, "a dispute is recorded with what is disputed"
        if form.get("raised_by") not in RAISED_BY:
            return False, "a dispute is raised by the tenant, the buyer or someone else"
        return True, "the dispute is recorded and goes to staff and counsel; the sale stands and nothing is reversed"
    if cmd.source != liens.APPROVER:
        return False, "only a manager records a sale dispute resolved"
    if not any(d["n"] == form.get("dispute") and not d["resolved"] for d in st.sales[case.id].disputes):
        return False, f"there is no open dispute {form.get('dispute')} on that sale"
    if not (cmd.reason or "").strip():
        return False, "a dispute is resolved with a reason"
    return True, "the dispute is recorded as resolved; the sale is as it was and no money moves"


def _act_sale(led: Ledger, cmd, now: datetime) -> None:
    form = cmd.form or {}
    case = liens.state(led.books).cases[form["case"]]
    sale = liens.state(led.books).sales[case.id]
    if cmd.action == "dispute_lien_sale":
        n = len(sale.disputes) + 1
        led.emit("lien_case.sale_disputed", now, {"case": case.id, "dispute": n, "raised_by": form["raised_by"], "detail": form["detail"].strip(),
                                                  "by": cmd.source}, f"sale:{case.id}:dispute:{n}", source=cmd.source, command=cmd.id)
        col._task(led, DISPUTE_TASK, case.tenant, f"sale:{case.id}:dispute:{n}",
                  f"The sale of unit {case.unit} is disputed by the {form['raised_by']}: {form['detail'].strip()}. The sale record stands and nothing is "
                  "reversed here; staff and counsel decide.", local(now).date(), now)
        return
    led.emit("lien_case.sale_dispute_resolved", now, {"case": case.id, "dispute": form["dispute"], "reason": cmd.reason.strip(), "by": cmd.source},
             f"sale:{case.id}:dispute:{form['dispute']}:resolved", source=cmd.source, command=cmd.id)
    for t in [t for t in col.state(led.books).tasks.values() if t.ref == f"sale:{case.id}:dispute:{form['dispute']}" and not t.acknowledged]:
        led.emit("task.acknowledged", now, {"task": t.id, "by": cmd.source}, f"task:{t.ref}:seen", source=cmd.source, command=cmd.id)


# an agreement to vacate or settle --------------------------------------------------------------------------------------
AGREEMENT_KINDS = {"pay_to_vacate": "pay-to-vacate", "settlement": "settlement"}
AGREEMENT_ENDS = ("completed", "broken", "withdrawn")


def active_agreement(books: Books, case: liens.Case):
    """The agreement in force on the case, offered or accepted, or None."""
    return next((x for x in liens.state(books).agreements.values() if x.case == case.id and x.active), None)


def agreement_refusal(books: Books, case: liens.Case) -> str | None:
    """Why no sale step goes ahead: an agreement to vacate or settle is in force. Offered and not yet accepted counts, the safe side."""
    if ag := active_agreement(books, case):
        return f"a {AGREEMENT_KINDS[ag.kind]} agreement is in force; no sale step goes ahead until it ends"
    return None


def check_agreement(books: Books, lease, case: liens.Case, cmd, now: datetime) -> tuple[bool, str]:
    """The gate's decision on recording, accepting or ending an agreement on the open case. Open to staff whatever else holds the file."""
    a, form = cmd.action, cmd.form or {}
    if a == "record_lien_agreement":
        if form.get("kind") not in AGREEMENT_KINDS:
            return False, "an agreement is a pay-to-vacate or a settlement agreement"
        if not isinstance(form.get("terms"), str) or not form["terms"].strip():
            return False, "an agreement is recorded with its terms"
        if (open_ := active_agreement(books, case)):
            return False, f"a {AGREEMENT_KINDS[open_.kind]} agreement is already in force on this case; end it first"
        return True, "the agreement is recorded; no sale step goes ahead while it is active"
    ag = liens.state(books).agreements.get(form.get("agreement"))
    if ag is None or ag.case != case.id or not ag.active:
        return False, f"there is no open agreement {form.get('agreement')} on this case"
    if a == "accept_lien_agreement":
        if ag.accepted:
            return False, "that agreement was already accepted"
        name = books.tenants[case.tenant].name
        if not isinstance(form.get("accepted_by"), str) or not liens._same_text(form["accepted_by"], name):
            return False, f"the acceptance must be the tenant's own: {name}"
        if not isinstance(form.get("how"), str) or not form["how"].strip():
            return False, "an acceptance says how it was given"
        sha = form.get("document_sha256")
        if form["how"].strip().casefold() == "signed" and not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
            return False, "a signed acceptance needs the fingerprint of its document"
        return True, "the tenant's acceptance is recorded; the sale stays blocked while the agreement is active"
    if form.get("how") not in AGREEMENT_ENDS:
        return False, "an agreement ends as completed, broken or withdrawn"
    if not (cmd.reason or "").strip():
        return False, "an agreement is ended with a reason"
    return True, "the agreement is recorded as ended; the sale may go on if the file still allows it"


def act_agreement(led: Ledger, cmd, case: liens.Case, now: datetime) -> None:
    a, form = cmd.action, cmd.form or {}
    if a == "record_lien_agreement":
        aid = led.books.next_id("AG")
        led.emit("lien_case.agreement_recorded", now, {"case": case.id, "agreement": aid, "kind": form["kind"], "terms": form["terms"].strip(),
                                                       "by": cmd.source}, f"agreement:{aid}:recorded", source=cmd.source, command=cmd.id)
    elif a == "accept_lien_agreement":
        led.emit("lien_case.agreement_accepted", now, {"case": case.id, "agreement": form["agreement"], "accepted_by": form["accepted_by"].strip(),
                                                       "how": form["how"].strip(), "document_sha256": form.get("document_sha256"), "by": cmd.source},
                 f"agreement:{form['agreement']}:accepted", source=cmd.source, command=cmd.id)
    else:
        led.emit("lien_case.agreement_ended", now, {"case": case.id, "agreement": form["agreement"], "how": form["how"], "reason": cmd.reason.strip(),
                                                    "by": cmd.source}, f"agreement:{form['agreement']}:ended", source=cmd.source, command=cmd.id)
