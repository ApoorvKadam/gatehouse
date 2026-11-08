"""Texting as Cubby documents it. Tenants text a facility's Cubby number, and Cubby turns each text into an SMS Received
webhook; here a /sim route hands back that payload, which a test or the seed delivers to Gatehouse's receiver.
Sending is POST /v1/messages/sms/send: it takes a customer, not a phone number, has no idempotency key and no way to look
a text up afterwards, and it does not look at the customer's preferences. A number that texted STOP is refused by the
carrier, and the FMS's own flag learns of it only when a send is rejected, so it can lag a STOP by one message.
The /sim routes are what a tenant does on their phone and what a test can read back; Gatehouse never calls them."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .store import AccessFault, ApiKey, Customer, Facility, Lease, TextMessage, carrier_id

STOP_WORDS = ("STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT")
START_WORDS = ("START", "UNSTOP", "YES")
SMS_LIMIT = 320


def e164(phone: str | None) -> str:
    """A US or Canadian number as +1 and ten digits; any other number as + and its digits. Whole numbers only, so two
    numbers that end the same way in different countries are two numbers."""
    digits = re.sub(r"\D", "", phone or "")
    return "+" + ("1" + digits if len(digits) == 10 else digits)


def register(app: FastAPI, db, caller, ok, iso, ApiError, customer_json) -> None:

    def own_facility(session: Session, key: ApiKey, facility_id) -> Facility:
        facility = session.get(Facility, facility_id or "")
        if facility is None or facility.org_id != key.org_id:
            raise ApiError(404, "Facility not found")
        return facility

    def text_json(m: TextMessage) -> dict:
        return {"messageId": m.message_id, "facilityId": m.facility_id, "customerId": m.customer_id, "direction": m.direction,
                "phone": m.phone, "body": m.body, "status": m.status, "createdAt": iso(m.created_at)}

    def utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed

    # ---------------- the one documented call: send a text to a customer ----------------
    @app.post("/v1/messages/sms/send")
    def sms_send(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        facility = own_facility(session, key, body.get("facilityId"))
        customer = session.get(Customer, body.get("customerId") or "")
        if customer is None or customer.org_id != key.org_id:
            raise ApiError(404, "Customer not found")
        text = body.get("body")
        if not isinstance(text, str) or not text.strip() or len(text) > SMS_LIMIT:
            raise ApiError(400, f"body must be 1 to {SMS_LIMIT} characters")
        if len(re.sub(r"\D", "", customer.phone or "")) < 10:
            raise ApiError(400, "The customer has no phone number to text")
        record = TextMessage(message_id=carrier_id(), facility_id=facility.facility_id, customer_id=customer.customer_id,
                             direction="OUTBOUND", phone=e164(customer.phone), body=text, status="SENT")
        if customer.sms_carrier_stop:
            # the carrier refuses, and only now does the FMS learn that the customer opted out
            customer.sms_suppressed, customer.sms_suppression_reason, customer.sms_marketing = True, "OPTED_OUT", False
            record.status = "REJECTED"
            session.add(record)
            session.commit()
            raise ApiError(422, "The carrier rejected the message: the recipient has opted out of texts")
        session.add(record)
        session.commit()
        lose = session.scalar(select(AccessFault).where(AccessFault.kind == "lose_send_response",
                                                        AccessFault.facility_id == facility.facility_id))
        if lose is not None:
            # the text went out; the answer never arrives complete
            def broken():
                yield b'{"status": '
                raise ConnectionResetError("connection dropped after sending the text")
            return StreamingResponse(broken(), media_type="application/json")
        # Cubby documents no response body for this call
        return ok()

    # ---------------- what a tenant does on their phone ----------------
    @app.post("/sim/messages/inbound")
    def sim_inbound(body: dict, session: Session = Depends(db)):
        """A tenant texts a facility's Cubby number. The answer is the SMS Received webhook payload Cubby sends for it."""
        facility = session.get(Facility, body.get("facilityId") or "")
        if facility is None:
            raise ApiError(404, "Facility not found")
        sender, text = body.get("from"), body.get("body")
        if not isinstance(sender, str) or len(re.sub(r"\D", "", sender)) < 10 or not isinstance(text, str) or not text.strip():
            raise ApiError(400, "from must be a phone number and body must not be empty")
        number = e164(sender)
        owners = [c for c in session.scalars(select(Customer).where(Customer.org_id == facility.org_id).order_by(Customer.customer_id))
                  if e164(c.phone) == number]
        word = text.strip().upper()
        for c in owners:
            if word in STOP_WORDS:
                c.sms_carrier_stop = True
            elif word in START_WORDS:
                c.sms_carrier_stop = False
        # Cubby names the tenant only when the number belongs to a customer with a lease
        tenant = next((c for c in owners if session.scalar(select(Lease.lease_id).where(Lease.customer_id == c.customer_id).limit(1))), None)
        message = TextMessage(message_id=carrier_id(), facility_id=facility.facility_id, customer_id=tenant.customer_id if tenant else None,
                              direction="INBOUND", phone=number, body=text, status="RECEIVED")
        if body.get("receivedAt"):
            try:
                message.created_at = utc(body["receivedAt"])
            except ValueError as exc:
                raise ApiError(400, "receivedAt must be an ISO time") from exc
        session.add(message)
        session.commit()
        return ok({"webhook": {
            "topicId": f"topic-{facility.org_id}", "messageId": message.message_id, "orgId": facility.org_id,
            "facilityId": facility.facility_id, "from": number, "to": facility.sms_number, "body": text,
            "receivedAt": iso(message.created_at) + "Z", "customer": customer_json(tenant) if tenant else None}})

    @app.post("/sim/customers/sms")
    def sim_customer_sms(body: dict, session: Session = Depends(db)):
        """What a customer told the FMS about texts (a tenant portal choice, or staff setting it for them). The carrier's
        own STOP list is separate: the FMS and the carrier do not tell each other until a send is rejected."""
        customer = session.get(Customer, body.get("customerId") or "")
        if customer is None:
            raise ApiError(404, "Customer not found")
        suppressed, reason = bool(body.get("suppressed", False)), body.get("reason")
        if suppressed and reason not in ("OPTED_OUT", "UNDELIVERABLE"):
            raise ApiError(400, "a suppressed channel needs a reason: OPTED_OUT or UNDELIVERABLE")
        customer.sms_suppressed, customer.sms_suppression_reason = suppressed, (reason if suppressed else None)
        # the FMS never reports promotions accepted on a suppressed channel
        customer.sms_marketing = False if suppressed else bool(body.get("marketing", customer.sms_marketing))
        session.commit()
        return ok({"customerId": customer.customer_id, "suppressed": suppressed, "marketing": customer.sms_marketing})

    @app.get("/sim/messages/truth")
    def sim_truth(facilityId: str, session: Session = Depends(db)):
        """Every text at a facility, both ways, and the customers the carrier will not text. Reading it changes nothing."""
        facility = session.get(Facility, facilityId)
        if facility is None:
            raise ApiError(404, "Facility not found")
        rows = session.scalars(select(TextMessage).where(TextMessage.facility_id == facilityId)
                               .order_by(TextMessage.created_at, TextMessage.message_id)).all()
        stopped = session.scalars(select(Customer.customer_id).where(Customer.org_id == facility.org_id, Customer.sms_carrier_stop.is_(True))
                                  .order_by(Customer.customer_id)).all()
        return ok({"messages": [text_json(m) for m in rows], "carrierStops": list(stopped)})
