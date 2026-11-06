"""The desk's HTTP API: the past-due queue, a case with its full history, identity checks and payments."""
from __future__ import annotations

import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, FMSError
from .adapter.cubby import CubbyAdapter
from .cases import queue, refresh_case, sync_facility
from .config import Settings
from .conversation import Conversation
from .db import make_session_factory, utcnow
from .models import CaseEvent, DelinquencyCase, IdentityCheck, Lease, LedgerSnapshot, PaymentCommand
from .payments import Authorization, IdentityClaim, PaymentRefused, PaymentService, collected, tenant_message, verify_identity
from .worker import Worker

log = logging.getLogger("desk.api")
IDENTITY_VALID_FOR = timedelta(minutes=15)


class IdentityIn(BaseModel):
    last_name: str
    phone_last4: str = Field(min_length=4, max_length=4)
    unit_name: str


class ChatIn(BaseModel):
    conversation_id: str | None = None
    text: str = Field(min_length=1, max_length=1000)


class PaymentIn(BaseModel):
    identity_check_id: str
    authorization_id: str = Field(min_length=8, description="chosen by the client when the tenant says yes; resending it never charges twice")
    amount: int = Field(gt=0)
    balance_seen: int = Field(ge=0)
    payment_method_id: str


def create_app(settings: Settings | None = None, adapter: FMSAdapter | None = None, start_worker: bool = True,
               chat_client=None) -> FastAPI:
    settings = settings or Settings.from_env()
    if chat_client is None and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        chat_client = anthropic.Anthropic()
    # ponytail: conversations live in this process and end with it; persist them when the desk runs more than one API process
    conversations: dict[str, Conversation] = {}
    factory = make_session_factory(settings.database_url)
    adapter = adapter or CubbyAdapter(settings.fms_base_url, settings.fms_api_key, settings.fms_timeout_seconds)
    worker = Worker(factory, adapter, settings.reconcile_every_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(title="Past Due Desk", lifespan=lifespan)

    def db():
        with factory() as session:
            yield session

    def operator(x_desk_token: str | None = Header(default=None)) -> str:
        if settings.operator_token is None:
            if settings.allow_without_token:
                return "local"
            raise HTTPException(503, "the desk has no operator token configured")
        if x_desk_token is None or not hmac.compare_digest(x_desk_token, settings.operator_token):
            raise HTTPException(401, "operator token required")
        return "operator"

    def get_case(session: Session, case_id: int) -> DelinquencyCase:
        case = session.get(DelinquencyCase, case_id)
        if case is None:
            raise HTTPException(404, "case not found")
        return case

    def command_json(c: PaymentCommand) -> dict:
        return {"id": c.id, "state": c.state, "amount": c.amount, "provider_payment_id": c.provider_payment_id,
                "provider_status": c.provider_status, "provider_reason": c.provider_reason, "checks": c.checks,
                "created_at": c.created_at, "confirmed_at": c.confirmed_at, "tenant_message": tenant_message(c)}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/facilities/{facility_id}/sync")
    def sync(facility_id: str, session: Session = Depends(db), _: str = Depends(operator)):
        try:
            cases = sync_facility(session, adapter, facility_id)
        except FMSError as exc:
            raise HTTPException(502, f"FMS read failed: {exc}") from exc
        return {"cases": len(cases)}

    @app.get("/api/facilities/{facility_id}/queue")
    def facility_queue(facility_id: str, session: Session = Depends(db), _: str = Depends(operator)):
        return {"rows": [r.__dict__ for r in queue(session, facility_id)], "collected": collected(session, adapter.provider)}

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: int, session: Session = Depends(db), _: str = Depends(operator)):
        case = get_case(session, case_id)
        lease = session.get(Lease, case.lease_id)
        snap = session.get(LedgerSnapshot, case.last_snapshot_id) if case.last_snapshot_id else None
        commands = session.scalars(select(PaymentCommand).where(PaymentCommand.case_id == case.id).order_by(PaymentCommand.id)).all()
        events = session.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.id)).all()
        return {
            "id": case.id, "state": case.state, "owner": case.owner, "holds": case.hold_reasons,
            "lease": {"id": lease.provider_id, "unit": lease.unit_name, "billing_cycle": lease.billing_cycle},
            "ledger": ({"total_due": snap.total_due, "prepaid": snap.prepaid, "observed_at": snap.observed_at,
                        "entries": snap.raw.get("bookEntries", [])} if snap else None),
            "payments": [command_json(c) for c in commands],
            "history": [{"kind": e.kind, "actor": e.actor, "source": e.source, "detail": e.detail, "at": e.occurred_at} for e in events],
        }

    @app.post("/api/cases/{case_id}/identity")
    def check_identity(case_id: int, body: IdentityIn, session: Session = Depends(db), _: str = Depends(operator)):
        case = get_case(session, case_id)
        lease = session.get(Lease, case.lease_id)
        verified, reason = verify_identity(adapter, lease, IdentityClaim(body.last_name, body.phone_last4, body.unit_name))
        check = IdentityCheck(id=uuid.uuid4().hex, case_id=case.id, verified=verified, reason=reason)
        session.add(check)
        session.commit()
        answer = {"identity_check_id": check.id, "verified": verified, "reason": reason}
        if verified:
            # the balance is read out only to a verified caller
            refresh_case(session, adapter, case)
            session.commit()
            answer["total_due"] = session.get(LedgerSnapshot, case.last_snapshot_id).total_due
        return answer

    @app.post("/api/cases/{case_id}/payments")
    def take_payment(case_id: int, body: PaymentIn, session: Session = Depends(db), _: str = Depends(operator)):
        case = get_case(session, case_id)
        check = session.get(IdentityCheck, body.identity_check_id)
        fresh = check is not None and check.case_id == case.id and check.verified and utcnow() - check.created_at <= IDENTITY_VALID_FOR
        auth = Authorization(body.authorization_id, bool(fresh), body.balance_seen, body.amount, channel="desk")
        try:
            command = PaymentService(session, adapter).take_payment(case.id, body.payment_method_id, auth)
        except PaymentRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        log.info("case %s payment %s %s", case.id, command.id, command.state)
        return command_json(command)

    @app.post("/api/cases/{case_id}/chat")
    def chat(case_id: int, body: ChatIn, session: Session = Depends(db), _: str = Depends(operator)):
        """One tenant message in, the desk's reply out. The channel in front of it (SMS, web chat) calls this."""
        case = get_case(session, case_id)
        if chat_client is None:
            raise HTTPException(503, "the desk has no model key configured")
        if body.conversation_id is None:
            conversation = Conversation(case.id, adapter, chat_client, settings.chat_model)
            conversations[conversation.id] = conversation
        else:
            conversation = conversations.get(body.conversation_id)
            if conversation is None or conversation.case_id != case.id:
                raise HTTPException(404, "conversation not found")
        reply = conversation.say(session, body.text)
        return {"conversation_id": conversation.id, **reply.__dict__}

    @app.get("/api/payments/{command_id}")
    def payment(command_id: int, session: Session = Depends(db), _: str = Depends(operator)):
        command = session.get(PaymentCommand, command_id)
        if command is None:
            raise HTTPException(404, "payment not found")
        return command_json(command)

    @app.post("/api/reconcile")
    def reconcile(session: Session = Depends(db), _: str = Depends(operator)):
        return {"checked": PaymentService(session, adapter).reconcile_all()}

    return app
