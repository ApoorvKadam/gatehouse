"""Leads as Cubby documents them, and the facility's phone system.

leads/search and leads/create follow Cubby's request and response shapes. leads/create takes no idempotency key, and with no
customerId it makes a new customer. A lead becomes CONVERTED only when a lease is created for it; /sim/leads/convert stands in
for that here. Missed calls are not in the FMS: they come from the phone system's own feed (/phone/v1/missed-calls), which
this simulator also plays, labelled as such. The /sim routes are what a test or the seed does; Gatehouse never calls them."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

from .store import ApiKey, Customer, Facility, Lead, LeadApiCall, MissedCall, new_id, utcnow

LEAD_STATUSES = ("NEW", "ON_HOLD", "IN_PROGRESS_HOT", "IN_PROGRESS_COLD", "RESERVATION", "CONVERTED", "UNQUALIFIED", "DUPLICATE")
SOURCES = ("WALK_IN", "CALL", "CONTACT_FORM", "SMS_MESSAGE", "WEBSITE", "REFERRAL", "AI_VOICE_AGENT", "OTHER")


def register(app: FastAPI, db, caller, ok, iso, ApiError) -> None:

    def lead_json(x: Lead) -> dict:
        return {"leadId": x.lead_id, "createdAt": iso(x.created_at), "updatedAt": iso(x.updated_at), "source": x.source, "origin": x.origin,
                "status": x.status, "facilityId": x.facility_id, "message": x.message, "customerId": x.customer_id,
                "reservedAt": None, "convertedAt": iso(x.converted_at), "unqualifiedAt": iso(x.unqualified_at),
                "leaseMoveInDate": x.converted_at.date().isoformat() if x.converted_at else None}

    def utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed

    @app.post("/v1/leads/search")
    def leads_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        where = body.get("where") or {}
        scopes = [k for k in ("organizationId", "facilityId", "customerId", "leadId") if where.get(k)]
        if not scopes:
            raise ApiError(400, "One of organizationId, facilityId, customerId or leadId is required")
        q = select(Lead).join(Facility, Facility.facility_id == Lead.facility_id).where(Facility.org_id == key.org_id)
        if where.get("organizationId") and where["organizationId"] != key.org_id:
            raise ApiError(403, "Not permitted for this organization")
        for k, column in (("facilityId", Lead.facility_id), ("customerId", Lead.customer_id), ("leadId", Lead.lead_id)):
            if where.get(k):
                q = q.where(column == where[k])
        if where.get("updatedAfter"):
            try:
                q = q.where(Lead.updated_at > utc(str(where["updatedAfter"])))
            except ValueError:
                raise ApiError(400, "updatedAfter must be an ISO-8601 timestamp")
        return ok({"leads": [lead_json(x) for x in session.scalars(q.order_by(Lead.created_at, Lead.lead_id)).all()]})

    @app.post("/v1/leads/create")
    def leads_create(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        session.add(LeadApiCall(facility_id=body.get("facilityId"), body=body))
        session.commit()
        facility = session.get(Facility, body.get("facilityId") or "")
        if facility is None or facility.org_id != key.org_id:
            raise ApiError(400, "facilityId must be a facility of this organization")
        if body.get("source") not in SOURCES:
            raise ApiError(400, f"source must be one of {', '.join(SOURCES)}")
        customer_id = body.get("customerId")
        if customer_id:
            customer = session.get(Customer, customer_id)
            if customer is None or customer.org_id != key.org_id:
                raise ApiError(400, "customerId must belong to a customer of this organization")
        else:
            # Cubby: with no customerId a new customer is made from the customer object, every time
            c = body.get("customer") or {}
            name = (c.get("name") or "").split(" ", 1)
            customer = Customer(customer_id=new_id("cust"), org_id=key.org_id, first_name=name[0], last_name=name[1] if len(name) > 1 else "",
                                phone=(c.get("contact") or {}).get("phone") or "", email=(c.get("contact") or {}).get("email") or "")
            session.add(customer)
            session.flush()
        lead = Lead(lead_id=new_id("lead"), facility_id=facility.facility_id, customer_id=customer.customer_id, source=body["source"],
                    message=body.get("message"))
        session.add(lead)
        session.commit()
        return ok({"leadId": lead.lead_id, "customerId": customer.customer_id})

    # ---------------- the facility's phone system (not the FMS) ----------------
    @app.get("/phone/v1/missed-calls")
    def missed_calls(facilityId: str, session: Session = Depends(db)):
        rows = session.scalars(select(MissedCall).where(MissedCall.facility_id == facilityId).order_by(MissedCall.at, MissedCall.call_id)).all()
        return ok({"calls": [{"callId": c.call_id, "from": c.phone or None, "at": iso(c.at), "ringSeconds": c.seconds,
                              "voicemail": c.voicemail} for c in rows]})

    # ---------------- what a test does: a caller rings, a lease is created for a lead ----------------
    @app.post("/sim/phone/missed-call")
    def add_call(body: dict, session: Session = Depends(db)):
        call = MissedCall(call_id=new_id("call"), facility_id=body["facilityId"], phone=body.get("from") or "", at=utcnow(),
                          seconds=int(body.get("ringSeconds", 20)), voicemail=body.get("voicemail"))
        session.add(call)
        session.commit()
        return ok({"callId": call.call_id})

    @app.post("/sim/leads/convert")
    def convert(body: dict, session: Session = Depends(db)):
        lead = session.get(Lead, body.get("leadId") or "")
        if lead is None:
            raise ApiError(404, "Lead not found")
        lead.status, lead.converted_at, lead.updated_at = "CONVERTED", utcnow(), utcnow()
        session.commit()
        return ok(lead_json(lead))

    @app.post("/sim/leads/status")
    def set_status(body: dict, session: Session = Depends(db)):
        """What staff do in the Cubby app: mark a lead unqualified or a duplicate, or move it along."""
        lead = session.get(Lead, body.get("leadId") or "")
        if lead is None:
            raise ApiError(404, "Lead not found")
        if body.get("status") not in LEAD_STATUSES or body["status"] in ("CONVERTED", "RESERVATION"):
            raise ApiError(400, "status cannot be set this way")
        lead.status, lead.updated_at = body["status"], utcnow()
        if body["status"] == "UNQUALIFIED":
            lead.unqualified_at = utcnow()
        session.commit()
        return ok(lead_json(lead))

    @app.get("/sim/leads/creates")
    def creates(session: Session = Depends(db)):
        return ok({"count": len(session.scalars(select(LeadApiCall)).all())})
