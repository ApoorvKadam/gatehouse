"""The desk's HTTP API: the past-due queue, a case with its full history, identity checks and payments."""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

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
from .models import CaseEvent, DelinquencyCase, Lease, LedgerSnapshot, PaymentCommand
from .payments import (
    IdentityClaim, PaymentRefused, PaymentService, check_identity, collected, confirm, propose, tenant_message,
)
from .worker import Worker

log = logging.getLogger("desk.api")


class IdentityIn(BaseModel):
    last_name: str
    phone_last4: str = Field(min_length=4, max_length=4)
    unit_name: str


class ChatIn(BaseModel):
    conversation_id: str | None = None
    text: str = Field(min_length=1, max_length=1000)


class ProposalIn(BaseModel):
    identity_check_id: str
    amount: int = Field(gt=0)
    payment_method_id: str | None = Field(default=None, description="a saved card of this tenant; the first one when left out")


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
    app.state.session_factory = factory

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
    def identity(case_id: int, body: IdentityIn, session: Session = Depends(db), _: str = Depends(operator)):
        case = get_case(session, case_id)
        check = check_identity(session, adapter, case, IdentityClaim(body.last_name, body.phone_last4, body.unit_name))
        if check is None:
            raise HTTPException(429, "too many failed identity checks on this account; try again in 30 minutes or verify the caller another way")
        verified, reason = check.verified, check.reason
        answer = {"identity_check_id": check.id, "verified": verified, "reason": reason}
        if verified:
            # the balance is read out only to a verified caller
            refresh_case(session, adapter, case)
            session.commit()
            answer["total_due"] = session.get(LedgerSnapshot, case.last_snapshot_id).total_due
        return answer

    @app.post("/api/cases/{case_id}/proposals")
    def make_proposal(case_id: int, body: ProposalIn, session: Session = Depends(db), _: str = Depends(operator)):
        """What the front desk reads back to the tenant before charging anything."""
        case = get_case(session, case_id)
        try:
            p = propose(session, adapter, case, body.identity_check_id, body.amount, body.payment_method_id, channel="desk")
        except PaymentRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"proposal_id": p.id, "amount": p.amount, "card_last4": p.card_last4, "expires_at": p.expires_at,
                "read_back": f"We will charge ${p.amount / 100:,.2f} to the card ending in {p.card_last4}. Is that okay?"}

    @app.post("/api/cases/{case_id}/proposals/{proposal_id}/confirm")
    def confirm_proposal(case_id: int, proposal_id: str, session: Session = Depends(db), _: str = Depends(operator)):
        """The tenant said yes. Sending this again returns the same payment and never charges twice."""
        get_case(session, case_id)
        try:
            command = confirm(session, adapter, case_id, proposal_id)
        except PaymentRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        log.info("case %s payment %s %s", case_id, command.id, command.state)
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
