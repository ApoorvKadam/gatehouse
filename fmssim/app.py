"""The simulator's HTTP API. Endpoint names, envelopes, error bodies and payment statuses follow
Cubby's Operator API documentation; the /sim routes are test controls a real FMS would not have."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import access, auctions, leads, messaging, seed
from .store import (
    ApiKey, BookEntry, Customer, Facility, Lease, LeaseBehavior, Payment, PaymentMethod,
    apply_to_lease, lease_total_due, make_session_factory, new_id, reverse_on_lease, unit_id_of, utcnow,
)

PII_FIELDS = ("name", "firstName", "lastName", "contact", "communicationPreferences")
# ways a vendor changes its answers without notice; each is a control a real FMS does not have
DRIFTS = {"off", "status", "amount", "created_at"}
PAYMENT_MODES = {"approve", "decline", "fail", "pending_then_succeed", "pending_then_decline",
                 "late_application", "lose_response", "drop_after_charge", "server_error", "gateway_error", "reject"}


class ApiError(Exception):
    def __init__(self, code: int, *errors: str):
        self.code, self.errors = code, list(errors)


def ok(data: dict | None = None) -> dict:
    return {"status": 200, "data": data} if data is not None else {"status": 200}


# billing periods in a year, for turning an amount per facility cycle into the lease's own cycle
PER_YEAR = {"WEEKLY": 52, "BI_WEEKLY": 26, "DAYS_28": 13, "MONTHLY": 12, "QUARTERLY": 4, "BI_ANNUAL": 2, "ANNUAL": 1}


def in_lease_cycle(amount: int, facility_cycle: str, lease_cycle: str) -> int:
    """Cubby: 27692 at a DAYS_28 facility on a MONTHLY lease is 27692 x 13 / 12 = 30000. Rounded half up, in whole cents."""
    num, den = amount * PER_YEAR[facility_cycle], PER_YEAR[lease_cycle]
    return (2 * num + den) // (2 * den)


def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def create_app(database_url: str | None = None, lose_response_seconds: float = 8.0) -> FastAPI:
    factory: sessionmaker[Session] = make_session_factory(database_url)
    app = FastAPI(title="FMS simulator", docs_url=None, redoc_url=None)
    app.state.session_factory = factory
    app.state.lose_response_seconds = lose_response_seconds
    app.state.drift = "off"

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "errors": exc.errors}, status_code=exc.code)

    def db():
        with factory() as session:
            yield session

    def caller(authorization: str | None = Header(default=None), session: Session = Depends(db)) -> ApiKey:
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "Missing API key")
        key = session.get(ApiKey, authorization.removeprefix("Bearer ").strip())
        if key is None:
            raise ApiError(401, "Invalid API key")
        return key

    def own_lease(session: Session, key: ApiKey, lease_id: str | None) -> Lease:
        lease = session.get(Lease, lease_id) if lease_id else None
        facility = session.get(Facility, lease.facility_id) if lease else None
        if lease is None or facility is None or facility.org_id != key.org_id:
            # another organization's lease is indistinguishable from a missing one
            raise ApiError(404, "Lease not found")
        return lease

    def strip_pii(obj: dict, key: ApiKey | None) -> dict:
        """No key means the organization's own webhook, which withholds nothing."""
        return obj if key is None or key.pii else {k: v for k, v in obj.items() if k not in PII_FIELDS}

    def lease_json(session: Session, lease: Lease) -> dict:
        due = lease_total_due(session, lease.lease_id)
        return {
            "leaseId": lease.lease_id, "facilityId": lease.facility_id, "customerId": lease.customer_id,
            "unitName": lease.unit_name, "createdAt": iso(lease.created_at), "updatedAt": iso(lease.updated_at),
            "moveInDate": iso(lease.move_in_date), "moveOutDate": None,
            "monthlyRentalAmount": lease.monthly_rental_amount, "billingCycle": lease.billing_cycle,
            "paidThroughDate": iso(lease.paid_through_date), "prepaidBalance": lease.prepaid_balance,
            "totalDue": due, "delinquencyStartDate": iso(lease.delinquency_start_date),
            "overlockNeeded": lease.overlock_needed, "unitId": unit_id_of(lease),
            # per one billing cycle of the facility, like monthlyRentalAmount
            "rentChangeScheduledDate": iso(lease.rent_change_scheduled_date), "rentChangeNewRentAmount": lease.rent_change_new_rent_amount,
        }

    def payment_json(p: Payment) -> dict:
        return {"paymentId": p.payment_id, "createdAt": iso(p.created_at), "amount": p.amount,
                "customerId": p.customer_id, "methodType": p.method_type, "status": p.status,
                "reason": p.reason, "explanation": p.explanation}

    def on_the_wire(p: Payment) -> dict:
        """A payment as the two payment calls write it. Under a drift the wire form departs from the documentation while the payment, the
        ledger and /sim/truth stay exactly what happened."""
        out, drift = payment_json(p), app.state.drift
        if drift == "status" and out["status"] == "SUCCEEDED":
            out["status"] = "SETTLED"
        elif drift == "amount":
            out["amount"] = f"{p.amount / 100:.2f}"
        elif drift == "created_at":
            del out["createdAt"]
        return out

    def settle(session: Session, payment: Payment) -> None:
        """Advance a payment one step, the way a real gateway and ledger catch up over time."""
        now = utcnow()
        if payment.status == "PENDING":
            if payment.resolve_after > 0:
                payment.resolve_after -= 1
            else:
                payment.status = payment.resolves_to or "SUCCEEDED"
                payment.reason = "Approved" if payment.status == "SUCCEEDED" else "Declined"
        if payment.status == "SUCCEEDED" and not payment.applied:
            if payment.apply_after > 0:
                payment.apply_after -= 1
            else:
                for lease_id, cents in payment.allocations.items():
                    apply_to_lease(session, session.get(Lease, lease_id), cents, now)
                payment.applied = True

    # ---------------- searches ----------------
    @app.post("/v1/facilities/search")
    def facilities_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        rows = session.scalars(select(Facility).where(Facility.org_id == key.org_id)).all()
        return ok({"facilities": [{"facilityId": f.facility_id, "organizationId": f.org_id, "name": f.name, "state": f.state,
                                   "timeZone": f.time_zone, "billingCycle": f.billing_cycle} for f in rows]})

    def customer_json(c: Customer, key: ApiKey | None = None) -> dict:
        return strip_pii({
            "customerId": c.customer_id, "name": f"{c.first_name} {c.last_name}", "firstName": c.first_name,
            "lastName": c.last_name, "contact": {"phone": c.phone, "email": c.email},
            "communicationPreferences": {
                "sms": {"marketing": c.sms_marketing, "suppressed": c.sms_suppressed, "suppressionReason": c.sms_suppression_reason},
                "email": {"marketing": True, "suppressed": False, "suppressionReason": None}},
        }, key)

    @app.post("/v1/customers/search")
    def customers_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        where = body.get("where") or {}
        q = select(Customer).where(Customer.org_id == key.org_id)
        if where.get("customerId"):
            q = q.where(Customer.customer_id == where["customerId"])
        if where.get("facilityId"):
            # a customer is at a facility through a lease there or a lead there: someone who only asked about a unit is still a customer
            from .store import Lead
            q = q.where(Customer.customer_id.in_(select(Lease.customer_id).where(Lease.facility_id == where["facilityId"]))
                        | Customer.customer_id.in_(select(Lead.customer_id).where(Lead.facility_id == where["facilityId"])))
        rows = session.scalars(q.order_by(Customer.customer_id)).all()
        if where.get("phone"):
            # Cubby's changelog says the customer phone filter remains a partial match, so a short number finds every
            # customer whose number contains it
            wanted = re.sub(r"\D", "", str(where["phone"]))
            rows = [c for c in rows if wanted and wanted in re.sub(r"\D", "", c.phone)]
        return ok({"customers": [customer_json(c, key) for c in rows]})

    @app.post("/v1/leases/search")
    def leases_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        where = body.get("where") or {}
        q = select(Lease).join(Facility, Facility.facility_id == Lease.facility_id).where(Facility.org_id == key.org_id)
        if where.get("facilityId"):
            q = q.where(Lease.facility_id == where["facilityId"])
        if where.get("leaseId"):
            q = q.where(Lease.lease_id == where["leaseId"])
        if where.get("delinquent"):
            q = q.where(Lease.delinquency_start_date.is_not(None))
        return ok({"leases": [lease_json(session, l) for l in session.scalars(q.order_by(Lease.lease_id)).all()]})

    @app.post("/v1/ledgers/search")
    def ledgers_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        lease = own_lease(session, key, (body.get("where") or {}).get("leaseId"))
        entries = session.scalars(select(BookEntry).where(BookEntry.lease_id == lease.lease_id)
                                  .order_by(BookEntry.entry_time, BookEntry.book_entry_id)).all()
        return ok({"ledgers": [{
            "leaseId": lease.lease_id,
            "balances": {"totalDue": lease_total_due(session, lease.lease_id), "deposit": 0,
                         "prepaid": lease.prepaid_balance, "credit": 0},
            "bookEntries": [{
                "bookEntryId": e.book_entry_id, "type": e.type, "entryTime": iso(e.entry_time), "amount": e.amount,
                "amountPaid": e.amount_paid, "paymentTime": iso(e.payment_time),
                "details": {"category": e.category, "description": e.description, "accruesOn": iso(e.accrues_on),
                            "accruesThrough": iso(e.accrues_through), "waive": e.waive},
            } for e in entries],
        }]})

    @app.post("/v1/reports/rent-roll")
    def rent_roll(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        """One entry per rented unit. Unlike Leases Search, monthlyRentalAmount here is already in the lease's own billing cycle, and
        lastRentChangeDate (the last completed rent change) is only here, omitted when the rent has never changed."""
        where = body.get("where") or {}
        wanted = where.get("facilityIds")
        if not isinstance(wanted, list) or not wanted or not where.get("throughDate"):
            raise ApiError(400, "where.facilityIds and where.throughDate are required")
        facilities = {f.facility_id: f for f in session.scalars(select(Facility).where(Facility.org_id == key.org_id, Facility.facility_id.in_(wanted)))}
        entries = []
        for lease in session.scalars(select(Lease).where(Lease.facility_id.in_(list(facilities))).order_by(Lease.unit_name)):
            f, c = facilities[lease.facility_id], session.get(Customer, lease.customer_id)
            entry = {"facilityId": f.facility_id, "facilityName": f.name, "status": "RENTED", "unitId": unit_id_of(lease), "unitName": lease.unit_name,
                     "customerId": lease.customer_id,
                     "totalDue": lease_total_due(session, lease.lease_id), "prepaid": lease.prepaid_balance,
                     "monthlyRentalAmount": in_lease_cycle(lease.monthly_rental_amount, f.billing_cycle, lease.billing_cycle),
                     "effectiveRate": in_lease_cycle(lease.monthly_rental_amount - lease.rent_discount, f.billing_cycle, lease.billing_cycle),
                     "rentChangeScheduledDate": iso(lease.rent_change_scheduled_date), "rentChangeNewRentAmount": lease.rent_change_new_rent_amount,
                     "billingCycle": lease.billing_cycle, "moveInDate": iso(lease.move_in_date), "paidThroughDate": iso(lease.paid_through_date)}
            if lease.last_rent_change_date:
                entry["lastRentChangeDate"] = iso(lease.last_rent_change_date)
            if key.pii and c is not None:
                entry["customerName"] = f"{c.first_name} {c.last_name}"        # a key without personal data sees ids only, as on every other call
            entries.append(entry)
        return ok({"entries": entries})

    @app.post("/v1/payments/search")
    def payments_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        where = body.get("where") or {}
        given = [k for k in ("facilityId", "customerId", "leaseId") if where.get(k)]
        if len(given) != 1:
            raise ApiError(400, "Exactly one of facilityId, customerId or leaseId is required")
        q = select(Payment).join(Facility, Facility.facility_id == Payment.facility_id).where(Facility.org_id == key.org_id)
        rows = session.scalars(q.order_by(Payment.created_at)).all()
        if where.get("leaseId"):
            own_lease(session, key, where["leaseId"])
            rows = [p for p in rows if where["leaseId"] in p.allocations]
        elif where.get("customerId"):
            rows = [p for p in rows if p.customer_id == where["customerId"]]
        else:
            rows = [p for p in rows if p.facility_id == where["facilityId"]]
        if where.get("paymentDateOnOrAfter"):
            rows = [p for p in rows if p.created_at.date().isoformat() >= where["paymentDateOnOrAfter"]]
        for p in rows:
            settle(session, p)
        session.commit()
        return ok({"payments": [on_the_wire(p) for p in rows]})

    @app.post("/v1/payment-methods/search")
    def payment_methods_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        cid = (body.get("where") or {}).get("customerId")
        customer = session.get(Customer, cid or "")
        if customer is None or customer.org_id != key.org_id:
            raise ApiError(404, "Customer not found")
        rows = session.scalars(select(PaymentMethod).where(PaymentMethod.customer_id == cid)).all()
        return ok({"paymentMethods": [{"paymentMethodId": m.payment_method_id, "methodType": m.method_type,
                                       "last4": m.last4} for m in rows]})

    # ---------------- make payment ----------------
    @app.post("/v1/leases/make-payment")
    def make_payment(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        if key.role != "OPERATOR":
            raise ApiError(403, "This API key cannot take payments")
        lease = own_lease(session, key, body.get("leaseId"))
        method = session.get(PaymentMethod, body.get("paymentMethodId") or "")
        if method is None or method.customer_id != lease.customer_id:
            raise ApiError(400, "Payment method not found for this customer")
        amount = body.get("amount")
        if not isinstance(amount, int) or amount <= 0:
            raise ApiError(400, "amount must be a positive integer number of cents")
        behavior = session.get(LeaseBehavior, lease.lease_id)
        mode = behavior.mode if behavior else "approve"
        lag = behavior.lag if behavior else 2
        if mode == "server_error":
            raise ApiError(500, "Internal error")
        if mode == "reject":
            raise ApiError(422, "Payment method has expired")
        if mode == "gateway_error":
            # a load balancer's page in front of the FMS, not an FMS answer
            return HTMLResponse("<html><body><h1>502 Bad Gateway</h1></body></html>", status_code=502)
        payment = Payment(payment_id=new_id("payment"), customer_id=lease.customer_id, facility_id=lease.facility_id,
                          amount=amount, method_type=method.method_type, status="SUCCEEDED", reason="Approved",
                          allocations={lease.lease_id: amount}, applied=False, apply_after=0)
        if mode in ("decline", "fail"):
            payment.status = "DECLINED" if mode == "decline" else "FAILED"
            payment.reason = behavior.decline_reason if mode == "decline" else "Processor error"
            payment.explanation = "The account did not have sufficient funds." if mode == "decline" else None
        elif mode in ("pending_then_succeed", "pending_then_decline"):
            payment.status, payment.reason = "PENDING", "Pending"
            payment.resolves_to = "SUCCEEDED" if mode == "pending_then_succeed" else "DECLINED"
            payment.resolve_after = lag
        elif mode == "late_application":
            payment.apply_after = lag
        session.add(payment)
        if payment.status == "SUCCEEDED" and payment.apply_after == 0:
            apply_to_lease(session, lease, amount, utcnow())
            payment.applied = True
        session.commit()
        if mode == "lose_response":
            # the money is taken and applied, then the response never arrives in time
            time.sleep(app.state.lose_response_seconds)
        if mode == "drop_after_charge":
            # the money is taken and applied, then the connection breaks before the answer is complete: the same
            # unknown outcome as a timeout, without depending on how fast this machine is
            def broken():
                yield b'{"status": 200, "data": {"payment": '
                raise ConnectionResetError("connection dropped after charging")
            return StreamingResponse(broken(), media_type="application/json")
        return ok({"payment": on_the_wire(payment)})

    # ---------------- simulator controls ----------------
    @app.get("/sim/truth")
    def sim_truth(leaseId: str, session: Session = Depends(db)):
        """What really happened on a lease, for a judge: every payment that touched it and the ledger balance.
        Unlike Payments Search it settles nothing, so reading it never changes the answer."""
        lease = control_lease(session, leaseId)
        rows = session.scalars(select(Payment).order_by(Payment.created_at)).all()
        return ok({"leaseId": lease.lease_id, "totalDue": lease_total_due(session, lease.lease_id),
                   "payments": [{**payment_json(p), "applied": p.applied, "allocations": p.allocations}
                                for p in rows if lease.lease_id in p.allocations]})

    @app.post("/sim/reset")
    def sim_reset(body: dict | None = None, session: Session = Depends(db)):
        id_seed = (body or {}).get("idSeed")
        if id_seed is not None and not isinstance(id_seed, str):
            raise ApiError(400, "idSeed must be a string")
        seed.reset(session, id_seed)
        app.state.drift = "off"
        return ok({"reset": True})

    @app.post("/sim/drift")
    def sim_drift(body: dict):
        """The vendor changes how its payment calls answer: a status renamed, an amount in dollars as text, or no createdAt."""
        kind = body.get("kind")
        if kind not in DRIFTS:
            raise ApiError(400, f"kind must be one of {sorted(DRIFTS)}")
        app.state.drift = kind
        return ok({"drift": kind})

    # test controls answer a bad request with 400 or 404, never a 500 that looks like the simulated server_error mode
    def control_lease(session: Session, lease_id) -> Lease:
        lease = session.get(Lease, lease_id or "")
        if lease is None:
            raise ApiError(404, "Lease not found")
        return lease

    def control_int(value, name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, f"{name} must be a whole number of cents") from exc

    @app.post("/sim/behaviors")
    def sim_behavior(body: dict, session: Session = Depends(db)):
        mode = body.get("mode")
        if mode not in PAYMENT_MODES:
            raise ApiError(400, f"mode must be one of {sorted(PAYMENT_MODES)}")
        lease = control_lease(session, body.get("leaseId"))
        b = session.get(LeaseBehavior, lease.lease_id) or LeaseBehavior(lease_id=lease.lease_id, mode=mode)
        b.mode, b.lag = mode, control_int(body.get("lag", 2), "lag")
        session.merge(b)
        session.commit()
        return ok({"leaseId": b.lease_id, "mode": b.mode, "lag": b.lag})

    @app.post("/sim/payments/return")
    def sim_return(body: dict, session: Session = Depends(db)):
        """A payment that cleared comes back days later (an ACH return or a chargeback)."""
        payment = session.get(Payment, body.get("paymentId") or "")
        if payment is None or payment.status != "SUCCEEDED" or not payment.applied:
            raise ApiError(400, "Only an applied, succeeded payment can be returned")
        now = utcnow()
        for lease_id, cents in payment.allocations.items():
            reverse_on_lease(session, session.get(Lease, lease_id), cents, now)
        payment.status = body.get("status", "DIRECT_DEBIT_RETURN")
        payment.reason = "Returned"
        session.commit()
        return ok({"payment": payment_json(payment)})

    @app.post("/sim/payments/offline")
    def sim_offline(body: dict, session: Session = Depends(db)):
        """One payment taken at the counter that covers several leases of the same customer."""
        allocations = body.get("allocations")
        if not isinstance(allocations, dict) or not allocations:
            raise ApiError(400, "allocations must map lease ids to amounts")
        leases = [control_lease(session, lid) for lid in allocations]
        allocations = {lid: control_int(v, f"allocation for {lid}") for lid, v in allocations.items()}
        if len({lease.customer_id for lease in leases}) != 1:
            raise ApiError(400, "an offline payment covers leases of one customer")
        payment = Payment(payment_id=new_id("payment"), customer_id=leases[0].customer_id,
                          facility_id=leases[0].facility_id, amount=sum(allocations.values()), method_type="CASH",
                          status="SUCCEEDED", reason="Approved", allocations=allocations, applied=True)
        if body.get("createdAt"):
            # a payment taken earlier that a reader has not looked at yet: a control the real FMS does not have, like receivedAt on an inbound text
            try:
                taken = datetime.fromisoformat(str(body["createdAt"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ApiError(400, "createdAt must be an ISO time") from exc
            payment.created_at = taken.astimezone(timezone.utc).replace(tzinfo=None) if taken.tzinfo else taken
        session.add(payment)
        for lease in leases:
            apply_to_lease(session, lease, allocations[lease.lease_id], utcnow())
        session.commit()
        return ok({"payment": payment_json(payment)})

    @app.post("/sim/charges")
    def sim_charge(body: dict, session: Session = Depends(db)):
        """Post a new charge to a lease (a late fee, or a balance change between two reads). A rent charge may name its period and what of it
        is already paid."""
        lease = control_lease(session, body.get("leaseId"))
        amount = control_int(body.get("amount"), "amount")
        paid = control_int(body.get("amountPaid"), "amountPaid") if body.get("amountPaid") is not None else 0
        period = [date.fromisoformat(body[k]) if isinstance(body.get(k), str) else None for k in ("accruesOn", "accruesThrough")]
        session.add(BookEntry(book_entry_id=new_id("be"), lease_id=lease.lease_id, category=body.get("category", "FEE"),
                              description=body.get("description"), amount=amount, amount_paid=paid, accrues_on=period[0], accrues_through=period[1]))
        session.commit()
        return ok({"totalDue": lease_total_due(session, lease.lease_id)})

    access.register(app, db, caller, own_lease, ok, iso, ApiError)
    auctions.register(app, db, caller, own_lease, ok, iso, ApiError)
    messaging.register(app, db, caller, ok, iso, ApiError, customer_json)
    leads.register(app, db, caller, ok, iso, ApiError)

    with factory() as session:
        if session.scalars(select(Facility)).first() is None:
            seed.reset(session)
    return app

