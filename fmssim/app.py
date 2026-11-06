"""The simulator's HTTP API. Endpoint names, envelopes, error bodies and payment statuses follow
Cubby's Operator API documentation; the /sim routes are test controls a real FMS would not have."""
from __future__ import annotations

import time
from datetime import date, datetime

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import seed
from .store import (
    ApiKey, BookEntry, Customer, Facility, Lease, LeaseBehavior, Payment, PaymentMethod,
    apply_to_lease, lease_total_due, make_session_factory, new_id, reverse_on_lease, utcnow,
)

PII_FIELDS = ("name", "firstName", "lastName", "phone", "email")
PAYMENT_MODES = {"approve", "decline", "fail", "pending_then_succeed", "pending_then_decline",
                 "late_application", "lose_response", "server_error"}


class ApiError(Exception):
    def __init__(self, code: int, *errors: str):
        self.code, self.errors = code, list(errors)


def ok(data: dict | None = None) -> dict:
    return {"status": 200, "data": data} if data is not None else {"status": 200}


def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def create_app(database_url: str | None = None, lose_response_seconds: float = 8.0) -> FastAPI:
    factory: sessionmaker[Session] = make_session_factory(database_url)
    app = FastAPI(title="FMS simulator", docs_url=None, redoc_url=None)
    app.state.session_factory = factory
    app.state.lose_response_seconds = lose_response_seconds

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

    def strip_pii(obj: dict, key: ApiKey) -> dict:
        return obj if key.pii else {k: v for k, v in obj.items() if k not in PII_FIELDS}

    def lease_json(session: Session, lease: Lease) -> dict:
        due = lease_total_due(session, lease.lease_id)
        return {
            "leaseId": lease.lease_id, "facilityId": lease.facility_id, "customerId": lease.customer_id,
            "unitName": lease.unit_name, "createdAt": iso(lease.created_at), "updatedAt": iso(lease.updated_at),
            "moveInDate": iso(lease.move_in_date), "moveOutDate": None,
            "monthlyRentalAmount": lease.monthly_rental_amount, "billingCycle": lease.billing_cycle,
            "paidThroughDate": iso(lease.paid_through_date), "prepaidBalance": lease.prepaid_balance,
            "totalDue": due, "delinquencyStartDate": iso(lease.delinquency_start_date),
            "overlockNeeded": lease.overlock_needed,
        }

    def payment_json(p: Payment) -> dict:
        return {"paymentId": p.payment_id, "createdAt": iso(p.created_at), "amount": p.amount,
                "customerId": p.customer_id, "methodType": p.method_type, "status": p.status,
                "reason": p.reason, "explanation": p.explanation}

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
        return ok({"facilities": [{"facilityId": f.facility_id, "name": f.name, "state": f.state,
                                   "timeZone": f.time_zone, "billingCycle": f.billing_cycle} for f in rows]})

    @app.post("/v1/customers/search")
    def customers_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        cid = (body.get("where") or {}).get("customerId")
        rows = session.scalars(select(Customer).where(Customer.org_id == key.org_id,
                                                      *( [Customer.customer_id == cid] if cid else []))).all()
        return ok({"customers": [strip_pii({"customerId": c.customer_id, "firstName": c.first_name,
                                            "lastName": c.last_name, "name": f"{c.first_name} {c.last_name}",
                                            "phone": c.phone, "email": c.email}, key) for c in rows]})

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
        return ok({"payments": [payment_json(p) for p in rows]})

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
        return ok({"payment": payment_json(payment)})

    # ---------------- simulator controls ----------------
    @app.post("/sim/reset")
    def sim_reset(session: Session = Depends(db)):
        seed.reset(session)
        return ok({"reset": True})

    @app.post("/sim/behaviors")
    def sim_behavior(body: dict, session: Session = Depends(db)):
        mode = body.get("mode")
        if mode not in PAYMENT_MODES:
            raise ApiError(400, f"mode must be one of {sorted(PAYMENT_MODES)}")
        b = session.get(LeaseBehavior, body["leaseId"]) or LeaseBehavior(lease_id=body["leaseId"], mode=mode)
        b.mode, b.lag = mode, int(body.get("lag", 2))
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
        allocations: dict = body["allocations"]
        leases = [session.get(Lease, lid) for lid in allocations]
        payment = Payment(payment_id=new_id("payment"), customer_id=leases[0].customer_id,
                          facility_id=leases[0].facility_id, amount=sum(allocations.values()), method_type="CASH",
                          status="SUCCEEDED", reason="Approved", allocations=allocations, applied=True)
        session.add(payment)
        for lease in leases:
            apply_to_lease(session, lease, allocations[lease.lease_id], utcnow())
        session.commit()
        return ok({"payment": payment_json(payment)})

    @app.post("/sim/charges")
    def sim_charge(body: dict, session: Session = Depends(db)):
        """Post a new charge to a lease (a late fee, or a balance change between two reads)."""
        lease = session.get(Lease, body["leaseId"])
        session.add(BookEntry(book_entry_id=new_id("be"), lease_id=lease.lease_id, category=body.get("category", "FEE"),
                              description=body.get("description"), amount=int(body["amount"])))
        session.commit()
        return ok({"totalDue": lease_total_due(session, lease.lease_id)})

    with factory() as session:
        if session.scalars(select(Facility)).first() is None:
            seed.reset(session)
    return app

