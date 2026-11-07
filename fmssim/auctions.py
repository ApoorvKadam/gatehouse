"""Lien sales in the shapes of Cubby's Operator API (auctions/search, create, start-lien, cancel-lien, settle, complete),
and a separate feed for an outside auction site.

Where Cubby documents a rule it is followed: settle is refused before the auction's day, for a lease not in the lien
process, one whose lien process was cancelled, or one already settled; complete is refused while the lease is still
SOLD. Where Cubby is silent the simulator picks a rule and says so here: search takes exactly one of auctionId or
facilityId, and cancel-lien accepts only a LISTED lease. The /sim routes are what staff do from the dashboard;
Gatehouse never calls them, and it never calls cancel-lien, settle or complete either."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

from .store import ApiKey, Auction, AuctionApiCall, AuctionedLease, ExternalListing, Facility, Lease, facility_today, new_id, unit_id_of, utcnow

STATUSES = ("LISTED", "NO_BIDS", "SOLD", "PAYMENT_RECEIVED", "COMPLETE", "CANCELLED")


def register(app: FastAPI, db, caller, own_lease, ok, iso, ApiError) -> None:

    def own_facility(session: Session, key: ApiKey, facility_id) -> Facility:
        facility = session.get(Facility, facility_id or "")
        if facility is None or facility.org_id != key.org_id:
            raise ApiError(404, "Facility not found")
        return facility

    def entry_json(e: AuctionedLease) -> dict:
        out = {"leaseId": e.lease_id, "unitId": e.unit_id, "lienStartDate": iso(e.lien_start_date), "status": e.status}
        if e.winning_bidder_id:
            out.update(winningBidderId=e.winning_bidder_id, bidAmount=e.bid_amount)
        return out

    def auction_json(session: Session, a: Auction) -> dict:
        entries = session.scalars(select(AuctionedLease).where(AuctionedLease.auction_id == a.auction_id)
                                  .order_by(AuctionedLease.id)).all()
        return {"auctionId": a.auction_id, "auctionTime": a.auction_time.isoformat(timespec="seconds"),
                "advertisementDeadline": a.advertisement_deadline.isoformat(timespec="seconds") if a.advertisement_deadline else None,
                "leaseConfigurationId": a.lease_configuration_id, "auctionedLeases": [entry_json(e) for e in entries]}

    def current_entry(session: Session, lease_id: str) -> AuctionedLease | None:
        return session.scalar(select(AuctionedLease).where(AuctionedLease.lease_id == lease_id)
                              .order_by(AuctionedLease.id.desc()).limit(1))

    def local_time(value, name: str) -> datetime:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, f"{name} must be a local date and time") from exc

    @app.post("/v1/auctions/search")
    def auctions_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        where = body.get("where") or {}
        if bool(where.get("auctionId")) == bool(where.get("facilityId")):
            raise ApiError(400, "Exactly one of auctionId or facilityId is required")
        q = select(Auction).join(Facility, Facility.facility_id == Auction.facility_id).where(Facility.org_id == key.org_id)
        if where.get("facilityId"):
            q = q.where(Auction.facility_id == own_facility(session, key, where["facilityId"]).facility_id)
        else:
            q = q.where(Auction.auction_id == where["auctionId"])
        return ok({"auctions": [auction_json(session, a) for a in session.scalars(q.order_by(Auction.auction_time)).all()]})

    def on_record(session: Session, path: str, body: dict) -> None:
        session.add(AuctionApiCall(path=path, lease_id=body.get("leaseId") if isinstance(body.get("leaseId"), str) else None))
        session.commit()

    @app.post("/v1/auctions/create")
    def auctions_create(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        on_record(session, "/v1/auctions/create", body)
        facility = own_facility(session, key, body.get("facilityId"))
        if not body.get("leaseConfigurationId"):
            raise ApiError(400, "leaseConfigurationId is required")
        auction = Auction(auction_id=new_id("auction"), facility_id=facility.facility_id,
                          auction_time=local_time(body.get("auctionTime"), "auctionTime"),
                          lease_configuration_id=body["leaseConfigurationId"])
        session.add(auction)
        session.commit()
        return ok({"auctionId": auction.auction_id})

    def start_lien(session: Session, lease: Lease, auction: Auction, by: str) -> AuctionedLease:
        if auction.facility_id != lease.facility_id:
            raise ApiError(400, "The auction is at another facility")
        current = current_entry(session, lease.lease_id)
        if current is not None and current.status in ("LISTED", "SOLD", "PAYMENT_RECEIVED"):
            raise ApiError(400, "The lease is already in the lien process")
        entry = AuctionedLease(auction_id=auction.auction_id, lease_id=lease.lease_id, unit_id=unit_id_of(lease),
                               lien_start_date=facility_today(session, lease.facility_id), changed_by=by, changed_at=utcnow())
        session.add(entry)
        session.commit()
        return entry

    @app.post("/v1/auctions/start-lien")
    def auctions_start_lien(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        on_record(session, "/v1/auctions/start-lien", body)
        # sendEmailAndSms and sendToAlternativeContacts default to false; the simulator sends nothing either way, and
        # starting the lien process says nothing about whether any statutory notice was delivered
        lease = own_lease(session, key, body.get("leaseId"))
        auction = session.get(Auction, body.get("auctionId") or "")
        if auction is None or session.get(Facility, auction.facility_id).org_id != key.org_id:
            raise ApiError(404, "Auction not found")
        start_lien(session, lease, auction, "api")
        return ok()

    def cancel(session: Session, lease: Lease, by: str) -> AuctionedLease:
        entry = current_entry(session, lease.lease_id)
        if entry is None or entry.status != "LISTED":
            raise ApiError(400, "The lease is not in a lien process that can still be cancelled")
        entry.status, entry.changed_by, entry.changed_at = "CANCELLED", by, utcnow()
        session.commit()
        return entry

    @app.post("/v1/auctions/cancel-lien")
    def auctions_cancel_lien(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        on_record(session, "/v1/auctions/cancel-lien", body)
        cancel(session, own_lease(session, key, body.get("leaseId")), "api")
        return ok()

    def settle(session: Session, lease: Lease, body: dict, by: str) -> AuctionedLease:
        entry = current_entry(session, lease.lease_id)
        if entry is None or entry.status == "CANCELLED":
            raise ApiError(400, "The lease is not in the lien process, or its lien process was cancelled")
        if entry.status != "LISTED":
            raise ApiError(400, "The auction was already settled")
        auction = session.get(Auction, entry.auction_id)
        if facility_today(session, lease.facility_id) < auction.auction_time.date():
            raise ApiError(400, "The auction has not taken place yet")
        if body.get("outcome") == "UNSOLD":
            entry.status = "NO_BIDS"
        elif body.get("outcome") == "SOLD":
            bid = body.get("bidAmount")
            if not isinstance(bid, int) or bid <= 0 or not body.get("winningBidderId"):
                raise ApiError(400, "A sale needs a winningBidderId and a bidAmount greater than zero")
            # ponytail: the bidder's charge, tax and deposit are not simulated; the entry records the sale only
            entry.status, entry.winning_bidder_id, entry.bid_amount = "SOLD", body["winningBidderId"], bid
        else:
            raise ApiError(400, "outcome must be SOLD or UNSOLD")
        entry.changed_by, entry.changed_at = by, utcnow()
        session.commit()
        return entry

    @app.post("/v1/auctions/settle")
    def auctions_settle(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        on_record(session, "/v1/auctions/settle", body)
        return ok({"auctionedLease": entry_json(settle(session, own_lease(session, key, body.get("leaseId")), body, "api"))})

    @app.post("/v1/auctions/complete")
    def auctions_complete(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        on_record(session, "/v1/auctions/complete", body)
        entry = current_entry(session, own_lease(session, key, body.get("leaseId")).lease_id)
        if entry is None or entry.status in ("LISTED", "NO_BIDS", "CANCELLED", "COMPLETE"):
            raise ApiError(400, "There is no sale to complete")
        if entry.status == "SOLD":
            raise ApiError(403, "The winning bidder has not paid")
        entry.status, entry.changed_by, entry.changed_at = "COMPLETE", "api", utcnow()
        session.commit()
        return ok({"auctionedLease": entry_json(entry), "depositRefund": None})

    # ---------------- the outside auction site's own feed (not the FMS) ----------------
    @app.get("/marketplace/v1/listings")
    def marketplace_listings(facilityId: str, session: Session = Depends(db)):
        rows = session.scalars(select(ExternalListing).where(ExternalListing.facility_id == facilityId)
                               .order_by(ExternalListing.listing_id)).all()
        return ok({"listings": [{"listingId": x.listing_id, "marketplace": x.marketplace, "auctionId": x.auction_id,
                                 "leaseId": x.lease_id, "status": x.status, "observedAt": iso(x.observed_at)} for x in rows]})

    # ---------------- what staff do from the dashboard ----------------
    def control_lease(session: Session, lease_id) -> Lease:
        lease = session.get(Lease, lease_id or "")
        if lease is None:
            raise ApiError(404, "Lease not found")
        return lease

    @app.post("/sim/auctions/schedule")
    def sim_schedule(body: dict, session: Session = Depends(db)):
        """Staff set up a sale for some leases, optionally advertised on an outside site."""
        facility = session.get(Facility, body.get("facilityId") or "")
        if facility is None:
            raise ApiError(404, "Facility not found")
        auction = Auction(auction_id=new_id("auction"), facility_id=facility.facility_id,
                          auction_time=local_time(body.get("auctionTime"), "auctionTime"), lease_configuration_id="leaseconfig_sim")
        session.add(auction)
        session.flush()
        for lease_id in body.get("leaseIds") or []:
            entry = start_lien(session, control_lease(session, lease_id), auction, body.get("by", "staff"))
            if body.get("lienStartDate"):
                entry.lien_start_date = date.fromisoformat(body["lienStartDate"])
            if body.get("marketplace"):
                session.add(ExternalListing(listing_id=new_id("listing"), marketplace=body["marketplace"],
                                            facility_id=facility.facility_id, auction_id=auction.auction_id, lease_id=lease_id))
        session.commit()
        return ok({"auctionId": auction.auction_id})

    @app.post("/sim/auctions/cancel")
    def sim_cancel(body: dict, session: Session = Depends(db)):
        """A person cancels the lien process from the dashboard."""
        return ok({"auctionedLease": entry_json(cancel(session, control_lease(session, body.get("leaseId")), body.get("by", "staff")))})

    @app.post("/sim/auctions/settle")
    def sim_settle(body: dict, session: Session = Depends(db)):
        """A person records the outcome; "force": true skips the day check, to build a sale in the past."""
        lease = control_lease(session, body.get("leaseId"))
        if body.get("force"):
            entry = current_entry(session, lease.lease_id)
            auction = session.get(Auction, entry.auction_id) if entry else None
            if auction is not None:
                auction.auction_time = min(auction.auction_time, datetime.combine(facility_today(session, lease.facility_id), datetime.min.time()))
        return ok({"auctionedLease": entry_json(settle(session, lease, body, body.get("by", "staff")))})

    @app.post("/sim/auctions/bidder-paid")
    def sim_bidder_paid(body: dict, session: Session = Depends(db)):
        """The winning bidder pays in Cubby: SOLD becomes PAYMENT_RECEIVED."""
        entry = current_entry(session, control_lease(session, body.get("leaseId")).lease_id)
        if entry is None or entry.status != "SOLD":
            raise ApiError(400, "There is no sale awaiting the bidder's payment")
        entry.status, entry.changed_by, entry.changed_at = "PAYMENT_RECEIVED", "bidder", utcnow()
        session.commit()
        return ok({"auctionedLease": entry_json(entry)})

    @app.post("/sim/listings/withdraw")
    def sim_withdraw(body: dict, session: Session = Depends(db)):
        """Staff take the sale down on the outside site; its feed then reports it withdrawn."""
        listing = session.scalar(select(ExternalListing).where(ExternalListing.lease_id == (body.get("leaseId") or ""))
                                 .order_by(ExternalListing.observed_at.desc()).limit(1))
        if listing is None:
            raise ApiError(404, "Listing not found")
        listing.status, listing.observed_at = "WITHDRAWN", utcnow()
        session.commit()
        return ok()

    @app.get("/sim/auctions/truth")
    def sim_auctions_truth(facilityId: str, session: Session = Depends(db)):
        """Every entry with who changed it last, and every sale-changing API call, for the evaluation."""
        rows = session.scalars(select(AuctionedLease).join(Auction, Auction.auction_id == AuctionedLease.auction_id)
                               .where(Auction.facility_id == facilityId).order_by(AuctionedLease.id)).all()
        calls = session.scalars(select(AuctionApiCall).order_by(AuctionApiCall.id)).all()
        return ok({"entries": [{**entry_json(e), "auctionId": e.auction_id, "changedBy": e.changed_by} for e in rows],
                   "apiCalls": [{"path": c.path, "leaseId": c.lease_id, "at": iso(c.at)} for c in calls]})
