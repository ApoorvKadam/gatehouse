"""Lien sales checked against what the FMS ledger shows.

Four kinds of work, each read fresh every pass from auctions/search and the lease's own ledger and payments:
- a sale still listed while the ledger shows nothing owed (one FMS task asks staff to cancel it or say why it stands);
- a payment after the notice while the sale is still listed and something is still owed (a person and counsel decide);
- a sale settled (no bids, sold, bidder paid, complete) on an account with nothing owed or payments since the notice:
  the FMS does not say whether the sale came before or after those payments, so Gatehouse claims no order;
- a listing on an outside auction site, which the FMS cancellation does not take down.

Gatehouse never changes a sale. An item closes only on its own evidence: "FMS lien cancellation verified" needs a
fresh auctions/search showing that auction and lease CANCELLED, and an outside listing closes only when that site's
feed reports it withdrawn. A completed task, a failed read or a changed balance closes nothing; a balance that becomes
due again moves the item to review instead."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .access import ensure_task
from .adapter import FMSAdapter
from .adapter.base import FMSError, ProviderAuctionEntry
from .lien_files import current_evidence
from .liens import dollars, on
from .models import DelinquencyCase, Facility, Lease, WorkItem, utcnow

LISTED_PAID = "sale_listed_nothing_owed"
BALANCE_CHANGED = "sale_balance_changed"
AFTER_NOTICE = "sale_payment_after_notice"
SETTLED_REVIEW = "sale_settled_review"
OUTSIDE = "sale_outside_listing"
KIND_NAMES = {
    LISTED_PAID: "Sale still listed, nothing owed",
    BALANCE_CHANGED: "Sale listed, balance changed since",
    AFTER_NOTICE: "Payment after the notice, sale listed",
    SETTLED_REVIEW: "Sale settled: payment timing to review",
    OUTSIDE: "Outside listing not confirmed withdrawn",
}
SALE_KINDS = (LISTED_PAID, BALANCE_CHANGED, AFTER_NOTICE, SETTLED_REVIEW)
LISTED_KINDS = (LISTED_PAID, BALANCE_CHANGED, AFTER_NOTICE)
SETTLED = ("NO_BIDS", "SOLD", "PAYMENT_RECEIVED", "COMPLETE")
STATUS_WORDS = {"LISTED": "listed", "NO_BIDS": "settled with no bids", "SOLD": "sold", "PAYMENT_RECEIVED": "sold, and the bidder paid",
                "COMPLETE": "complete", "CANCELLED": "cancelled"}
CANCELLED_BY = "FMS lien cancellation verified"
OUTSIDE_NOTE = ("Cancelling the lien in the FMS does not take down a listing on an outside auction site; that needs its "
                "own evidence.")
CLOSING = {
    LISTED_PAID: "auctions/search shows this auction and lease CANCELLED. A completed task does not close it.",
    BALANCE_CHANGED: ("auctions/search shows this auction and lease CANCELLED, or a person records what happens to the sale. "
                      "Gatehouse cannot record that yet."),
    AFTER_NOTICE: ("auctions/search shows this auction and lease CANCELLED, or a person and counsel record whether the sale "
                   "stands. Gatehouse cannot record that yet."),
    SETTLED_REVIEW: "A person and counsel review the sale's timing against the payments. Gatehouse does not close this on its own.",
    OUTSIDE: "The outside site's own feed reports the listing withdrawn. The FMS cancellation does not close it.",
}
WHO = {LISTED_PAID: "Collections lead", BALANCE_CHANGED: "Collections lead", AFTER_NOTICE: "Collections lead, with counsel",
       SETTLED_REVIEW: "Manager, with counsel", OUTSIDE: "Front desk"}


class AmbiguousTime(ValueError):
    pass


def sale_instant(local_iso: str, zone: str) -> datetime:
    """The FMS gives the auction's local time. A time that does not exist on that day or happens twice (a daylight
    saving change) is refused rather than guessed. Returns naive UTC, like every other stored time."""
    naive = datetime.fromisoformat(local_iso)
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc).replace(tzinfo=None)
    tz = ZoneInfo(zone)
    early, late = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if early.utcoffset() != late.utcoffset():
        raise AmbiguousTime(f"{local_iso} does not name one moment in {zone}")
    return early.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class SaleFacts:
    entry: ProviderAuctionEntry
    owed: int
    payments: list[dict]          # succeeded payments on or after the notice date: id, amount, when (UTC ISO)
    notice_on: str | None
    notice_source: str
    sale_at: str | None           # UTC ISO, or None with sale_time_problem set
    sale_time_problem: str | None
    unit: str
    zone: str = "America/New_York"


@dataclass
class Finding:
    kind: str
    subject_id: str
    problem: str
    observed: dict
    task: tuple | None = None


@dataclass
class SalesPass:
    opened: int = 0
    closed: int = 0
    failures: list[str] = field(default_factory=list)


def subject_of(entry: ProviderAuctionEntry) -> str:
    return f"{entry.auction_id}/{entry.lease_provider_id}"


def sale_local(auction_time: str) -> str:
    """The auction's local time as the facility reads it, without converting it."""
    try:
        t = datetime.fromisoformat(auction_time)
    except ValueError:
        return auction_time
    return f"{t:%b} {t.day}, {t.year} at {t:%I:%M %p}".replace(" 0", " ")


def _when(f: SaleFacts) -> str:
    return f"the sale set for {sale_local(f.entry.auction_time)} facility time" if f.sale_at else (
        f"the sale (its time could not be worked out: {f.sale_time_problem})")


def _paid(f: SaleFacts) -> str:
    return "; ".join(f"{dollars(p['amount'])} on {on(p['on'])} ({p['id']})" for p in f.payments)


def observed_of(f: SaleFacts) -> dict:
    return {"auction_id": f.entry.auction_id, "lease_id": f.entry.lease_provider_id, "unit": f.unit, "status": f.entry.status,
            "auction_time": f.entry.auction_time, "sale_local": sale_local(f.entry.auction_time), "sale_at": f.sale_at,
            "sale_time_problem": f.sale_time_problem,
            "owed": f.owed, "payments": f.payments, "notice_on": f.notice_on, "notice_source": f.notice_source,
            "read_at": utcnow().isoformat(timespec="seconds")}


def assess(f: SaleFacts) -> Finding | None:
    """What this entry needs, from this pass's fresh readings only; None when nothing about payments touches it."""
    subject, status, observed = subject_of(f.entry), f.entry.status, observed_of(f)
    since = f"since the notice ({f.notice_source}, {on(f.notice_on)})" if f.notice_on else "since the notice (no notice date known)"
    if status == "LISTED" and f.owed == 0:
        why = ("No notice date is known, so Gatehouse does not say whether any payment came after the notice." if not f.notice_on else
               f"The FMS reports payments {since}: {_paid(f)}." if f.payments else
               f"No payment {since} was found, so a credit, waiver or correction may explain it; Gatehouse does not call it a tenant payment.")
        task = (f"Cancel the lien sale for unit {f.unit}, or record why it stands",
                f"The FMS ledger shows nothing owed on unit {f.unit}, and {_when(f)} is still listed in the FMS. {why}",
                f.entry.unit_id, None)
        return Finding(LISTED_PAID, subject, f"The FMS ledger shows nothing owed, and {_when(f)} is still listed. {why}", observed, task)
    if status == "LISTED" and f.payments:
        return Finding(AFTER_NOTICE, subject,
                       f"The FMS reports payments {since}: {_paid(f)}. {dollars(f.owed)} is still owed and {_when(f)} is still "
                       "listed. Whether the notice's claim was satisfied is for a person and counsel; Gatehouse draws no "
                       "conclusion about it.", observed)
    if status in SETTLED and (f.owed == 0 or f.payments):
        return settled_review(f)
    return None


def settled_review(f: SaleFacts) -> Finding:
    since = f"since the notice ({f.notice_source}, {on(f.notice_on)})" if f.notice_on else "since the notice (no notice date known)"
    owed = "nothing owed" if f.owed == 0 else f"{dollars(f.owed)} owed"
    paid = (f" It reports payments {since}: {_paid(f)}." if f.payments else
            " No notice date is known, so no payment is said to follow it." if not f.notice_on else "")
    return Finding(SETTLED_REVIEW, subject_of(f.entry),
                   f"The FMS records this sale as {STATUS_WORDS[f.entry.status]}, and its ledger shows {owed}.{paid} The FMS does "
                   "not say whether the sale came before or after these payments, so Gatehouse makes no claim about the "
                   "order. A bidder's payment is not the tenant's.", observed_of(f))


def notice_date(session: Session, lease: Lease | None, entry: ProviderAuctionEntry) -> tuple[str | None, str]:
    """The notice's mailing date from Gatehouse's own lien file when there is one, else the FMS's lien start date."""
    if lease is not None:
        case = session.scalar(select(DelinquencyCase).where(DelinquencyCase.lease_id == lease.id).order_by(DelinquencyCase.id.desc()).limit(1))
        if case is not None:
            mailed = sorted(e.facts.get("deposited_on") for e in current_evidence(session, case)
                            if e.kind == "mail_deposit" and e.facts.get("deposited_on"))
            if mailed:
                return mailed[0], "mailed, per the lien file"
    return entry.lien_start_date, "the lien start date the FMS reports"


def read_listings(feed_url: str | None, facility_provider_id: str) -> list[dict] | None:
    """The outside auction site's own feed, or None when it is not configured or could not be read."""
    if not feed_url:
        return None
    try:
        r = httpx.get(f"{feed_url.rstrip('/')}/marketplace/v1/listings", params={"facilityId": facility_provider_id}, timeout=5.0)
        body = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    data = body.get("data") if isinstance(body, dict) else None
    rows = data.get("listings") if isinstance(data, dict) else None
    return [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else None


def reconcile_sales(session: Session, adapter: FMSAdapter, facility: Facility, run_id: str, feed_url: str | None = None) -> SalesPass:
    """One pass over the facility's lien sales. Safe to repeat: open items are updated, never duplicated."""
    result = SalesPass()
    open_items = session.scalars(select(WorkItem).where(WorkItem.run_id == run_id, WorkItem.facility_id == facility.id,
                                                        WorkItem.kind.in_(KIND_NAMES), WorkItem.state == "open")).all()
    now = utcnow()
    try:
        entries = adapter.auction_entries(facility.provider_id)
    except FMSError as exc:
        for item in open_items:
            item.note = f"Not freshly verified: auctions/search could not be read at {now:%b %-d, %H:%M} UTC."
        session.commit()
        result.failures.append(f"auctions at {facility.provider_id}: {exc}")
        return result
    by_subject: dict[str, list[WorkItem]] = {}
    for item in open_items:
        by_subject.setdefault(item.subject_id, []).append(item)
    listings = read_listings(feed_url, facility.provider_id)
    if feed_url and listings is None:
        for item in open_items:
            if item.kind == OUTSIDE:
                item.note = f"Not freshly verified: the outside site's feed could not be read at {now:%b %-d, %H:%M} UTC."
        result.failures.append(f"outside listings at {facility.provider_id}: the feed could not be read")
    decided: set[str] = set()
    for entry in entries:
        subject = subject_of(entry)
        mine = by_subject.get(subject, [])
        if entry.status == "CANCELLED":
            for item in mine:
                if item.kind in SALE_KINDS and item.kind != SETTLED_REVIEW:
                    item.state, item.closed_at, item.updated_at = "closed", now, now
                    item.verification, item.note = CANCELLED_BY, OUTSIDE_NOTE
                    result.closed += 1
        elif entry.status in ("LISTED",) + SETTLED:
            lease = session.scalar(select(Lease).where(Lease.provider_id == entry.lease_provider_id))
            try:
                ledger = adapter.ledger(entry.lease_provider_id)
                payments = adapter.payments_for_lease(entry.lease_provider_id)
            except FMSError as exc:
                for item in mine:
                    if item.kind != OUTSIDE:
                        item.note = f"Not freshly verified: the lease's ledger or payments could not be read at {now:%b %-d, %H:%M} UTC."
                result.failures.append(f"ledger for {entry.lease_provider_id}: {exc}")
                _outside(session, facility, run_id, entry, listings, result)   # the outside site's feed is its own evidence
                continue
            notice_on, source = notice_date(session, lease, entry)
            zone = ZoneInfo(facility.time_zone or "America/New_York")
            paid = []
            for p in payments:
                if p.status != "SUCCEEDED" or p.created_at is None:
                    continue    # pending, declined, returned, refunded or disputed money is not counted
                stamp = p.created_at if p.created_at.tzinfo else p.created_at.replace(tzinfo=timezone.utc)
                day = stamp.astimezone(zone).date().isoformat()
                if notice_on is not None and day >= notice_on:   # with no notice date, no payment is said to follow it
                    paid.append({"id": p.provider_id, "amount": p.amount, "when": stamp.astimezone(timezone.utc).isoformat(timespec="seconds"),
                                 "on": day})
            try:
                sale_at, problem = sale_instant(entry.auction_time, facility.time_zone).isoformat(timespec="seconds"), None
            except (AmbiguousTime, ValueError) as exc:
                sale_at, problem = None, str(exc)
            facts = SaleFacts(entry, ledger.total_due, paid, notice_on, source, sale_at, problem,
                              lease.unit_name if lease else (entry.unit_id or entry.lease_provider_id), facility.time_zone)
            for item in mine:
                if item.kind != OUTSIDE and (item.note or "").startswith("Not freshly verified"):
                    item.note = None   # this pass read the lease afresh
                if item.kind == SETTLED_REVIEW and entry.status in SETTLED and (item.observed or {}).get("task_warning"):
                    _warn_task(item, settled_warning(entry.status))
            if entry.status == "LISTED" and ledger.total_due > 0:
                _balance_changed(session, mine, facts, now)   # before the new finding, so it updates this item, not a second one
            found = assess(facts)
            if found is None and entry.status in SETTLED and any(i.kind in LISTED_KINDS for i in mine):
                found = settled_review(facts)   # a sale that settled under an open item is reviewed, whatever the balance now
            _apply(session, facility, run_id, mine, found, facts, result)
            decided.add(subject)
        _outside(session, facility, run_id, entry, listings, result)
    session.commit()
    for item in session.scalars(select(WorkItem).where(WorkItem.run_id == run_id, WorkItem.facility_id == facility.id,
                                                       WorkItem.kind.in_((LISTED_PAID, BALANCE_CHANGED, SETTLED_REVIEW)),
                                                       WorkItem.state == "open")):
        task = item.observed.get("task")
        if item.task_state == "none" and not (item.kind == LISTED_PAID and item.subject_id in decided):
            task = None   # a new task only on this pass's fresh reading; a task already sent is still looked up by its marker
        ensure_task(session, adapter, facility, item, task, result.failures, keep_note=True)
    return result


def _carry(item: WorkItem, observed: dict) -> dict:
    """New facts for an open item, keeping its FMS task and any warning that the task rests on an older reading. That
    warning lives apart from the note, so a freshness note or task news can never erase it."""
    old = item.observed or {}
    return dict(observed, task=observed.get("task") or old.get("task"), task_warning=old.get("task_warning"))


def settled_warning(status: str) -> str:
    return (f"This item was opened while the sale was listed. The FMS now records it as {STATUS_WORDS[status]}; its FMS task "
            "asking for a cancellation rests on the earlier reading.")


def _warn_task(item: WorkItem, warning: str) -> None:
    if item.task_state != "none":
        item.observed = dict(item.observed or {}, task_warning=warning)


def _open(session: Session, facility: Facility, run_id: str, kind: str, subject: str, problem: str, observed: dict) -> WorkItem:
    episodes = session.scalar(select(func.count()).select_from(WorkItem).where(
        WorkItem.run_id == run_id, WorkItem.facility_id == facility.id, WorkItem.kind == kind,
        WorkItem.subject_type == "auction_lease", WorkItem.subject_id == subject)) or 0
    now = utcnow()
    item = WorkItem(run_id=run_id, facility_id=facility.id, kind=kind, subject_type="auction_lease", subject_id=subject,
                    episode_key=str(episodes + 1), verification="FMS reported", who_acts=WHO[kind],
                    evidence_source="auctions/search, ledgers/search and payments/search, read fresh this pass",
                    closing_evidence=CLOSING[kind], problem=problem, observed=observed, opened_at=now, updated_at=now)
    session.add(item)
    session.flush()
    return item


def _retitle(session: Session, item: WorkItem, kind: str) -> None:
    """Change an open item's kind, keeping its history and its FMS task, with an episode number free for the new kind."""
    taken = session.scalar(select(func.count()).select_from(WorkItem).where(
        WorkItem.run_id == item.run_id, WorkItem.facility_id == item.facility_id, WorkItem.kind == kind,
        WorkItem.subject_type == item.subject_type, WorkItem.subject_id == item.subject_id)) or 0
    item.kind, item.episode_key, item.who_acts, item.closing_evidence = kind, f"{taken + 1}", WHO[kind], CLOSING[kind]


def _apply(session: Session, facility: Facility, run_id: str, mine: list[WorkItem], found: Finding | None, facts: SaleFacts,
           result: SalesPass) -> None:
    now = utcnow()
    if found is None:
        for item in mine:
            if item.kind in SALE_KINDS:
                # nothing about payments touches the sale on this reading (a payment came back, say); a person still owns it
                item.observed, item.updated_at = _carry(item, observed_of(facts)), now
                if item.kind == AFTER_NOTICE:
                    item.note = ("On the latest reading no payment since the notice touches this sale any more. The item "
                                 "stays open for a person.")
        return
    observed = dict(found.observed, task=list(found.task) if found.task else None)
    current = next((i for i in mine if i.kind == found.kind), None)
    if current is None and found.kind == LISTED_PAID:
        # a payment-after-notice item becomes the more urgent one when nothing is owed any more
        current = next((i for i in mine if i.kind == AFTER_NOTICE), None)
        if current is not None:
            _retitle(session, current, LISTED_PAID)
    if current is None and found.kind == SETTLED_REVIEW:
        # the sale settled under an open item: that item becomes the review, keeping its history and task
        current = next((i for i in mine if i.kind in LISTED_KINDS), None)
        if current is not None:
            _retitle(session, current, SETTLED_REVIEW)
            current.problem, current.observed, current.updated_at = found.problem, _carry(current, dict(observed, task=None)), now
            _warn_task(current, settled_warning(facts.entry.status))
            return
    if current is None and found.kind in (LISTED_PAID, AFTER_NOTICE):
        # once the balance changed under an open item, a person owns it: it carries the current facts, nothing new opens
        current = next((i for i in mine if i.kind == BALANCE_CHANGED), None)
        if current is not None:
            current.observed, current.updated_at = _carry(current, dict(observed, task=None)), now
            return
    if current is None:
        item = _open(session, facility, run_id, found.kind, found.subject_id, found.problem, observed)
        mine.append(item)
        result.opened += 1
        return
    current.problem, current.observed, current.updated_at, current.note = found.problem, _carry(current, observed), now, None


def _balance_changed(session: Session, mine: list[WorkItem], facts: SaleFacts, now: datetime) -> None:
    """A sale item opened when nothing was owed, and now something is (a payment came back, a charge posted): it is not
    closed and the sale is not cleared; a person reassesses. Its task asked for a cancellation on the older reading."""
    for item in mine:
        if item.kind != LISTED_PAID:
            continue
        _retitle(session, item, BALANCE_CHANGED)
        item.problem = (f"When this was opened the FMS ledger showed nothing owed. It now shows {dollars(facts.owed)} owed, and "
                        f"{_when(facts)} is still listed. Gatehouse does not close this or treat the sale as cleared; a person "
                        "reassesses the notice, the charges and the sale.")
        item.observed = _carry(item, observed_of(facts))
        _warn_task(item, "The FMS task opened for this item asked staff to cancel the sale on the older reading. Do not act on "
                         "it without this review.")
        item.updated_at = now


def _outside(session: Session, facility: Facility, run_id: str, entry: ProviderAuctionEntry, listings: list[dict] | None,
             result: SalesPass) -> None:
    """An outside listing becomes work once a payment touches the sale; it closes only on that site's own feed."""
    if listings is None:
        return
    subject = subject_of(entry)
    listing = next((x for x in listings if x.get("auctionId") == entry.auction_id and x.get("leaseId") == entry.lease_provider_id), None)
    item = session.scalar(select(WorkItem).where(WorkItem.run_id == run_id, WorkItem.facility_id == facility.id, WorkItem.kind == OUTSIDE,
                                                 WorkItem.subject_id == subject, WorkItem.state == "open"))
    now = utcnow()
    if listing is None:
        if item is not None:
            # a listing that drops out of the feed is not proof it was withdrawn
            item.note = (f"Not verified: the outside site's feed did not list this sale at {now:%b %-d, %H:%M} UTC. A listing "
                         "missing from the feed is not proof it was withdrawn.")
        return
    if item is not None and (item.note or "").startswith(("Not freshly verified", "Not verified")):
        item.note = None   # the feed reported this listing on this pass
    if listing.get("status") == "WITHDRAWN":
        if item is not None:
            item.state, item.closed_at, item.updated_at = "closed", now, now
            item.verification = "Confirmed by the outside site's feed"
            item.note = f"{listing.get('marketplace')} reported listing {listing.get('listingId')} withdrawn at {listing.get('observedAt')}."
            result.closed += 1
        return
    touched = session.scalar(select(func.count()).select_from(WorkItem).where(
        WorkItem.run_id == run_id, WorkItem.facility_id == facility.id, WorkItem.kind.in_(SALE_KINDS), WorkItem.subject_id == subject))
    if not touched:
        return
    problem = (f"{listing.get('marketplace')} still shows listing {listing.get('listingId')} for this sale (its feed, at "
               f"{listing.get('observedAt')}). {OUTSIDE_NOTE}")
    if item is None:
        _open(session, facility, run_id, OUTSIDE, subject, problem, {"listing": listing, "auction_id": entry.auction_id,
                                                                     "lease_id": entry.lease_provider_id, "status": entry.status,
                                                                     "auction_time": entry.auction_time})
        result.opened += 1
    else:
        item.problem, item.updated_at = problem, now
        item.observed = dict(item.observed, listing=listing)


__all__ = ["reconcile_sales", "KIND_NAMES", "SALE_KINDS", "OUTSIDE", "LISTED_PAID", "BALANCE_CHANGED", "AFTER_NOTICE",
           "SETTLED_REVIEW", "CANCELLED_BY", "OUTSIDE_NOTE", "sale_instant", "AmbiguousTime", "assess", "SaleFacts"]
