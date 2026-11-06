"""The desk's HTTP API: the past-due queue, a case with its full history, identity checks and payments."""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm.exc import StaleDataError
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, FMSError
from .adapter.cubby import CubbyAdapter
from .cases import queue, sync_facility
from .config import Settings
from .conversation import Conversation
from .lien_files import ensure_append_only, evaluate_case, record_evidence, replay
from .liens import FL_R1, ValidationError
from .db import bind_to_fms, make_session_factory
from .models import CaseEvent, DelinquencyCase, DeskBinding, Facility, Lease, LedgerSnapshot, LienEvaluation, PaymentCommand
from .payments import (
    IdentityClaim, PaymentRefused, PaymentService, check_identity, collected, confirm, propose, tenant_message,
)
from .rules import import_stitch, reference_row
from .worker import Worker

log = logging.getLogger("desk.api")


class IdentityIn(BaseModel):
    last_name: str
    phone_last4: str = Field(min_length=4, max_length=4)
    unit_name: str


class ChatIn(BaseModel):
    conversation_id: str | None = None
    sender: str = Field(min_length=3, max_length=200, description="the channel's authenticated sender (a phone number, a session id)")
    text: str = Field(min_length=1, max_length=1000)


class ProposalIn(BaseModel):
    identity_check_id: str
    amount: int = Field(gt=0)
    payment_method_id: str | None = Field(default=None, description="a saved card of this tenant; the first one when left out")


class EvidenceIn(BaseModel):
    kind: str
    facts: dict
    entered_by: str = Field(min_length=2)
    document_name: str | None = None
    document_text: str | None = Field(default=None, max_length=200_000, description="the document itself, or its transcription")
    supersedes: int | None = Field(default=None, description="the record this one corrects")


class ConfirmIn(BaseModel):
    confirmed_by: str = Field(min_length=2, description="the person at the desk who heard the tenant's yes")
    tenant_answer: str = Field(min_length=1, max_length=500, description="what the tenant said, in their words")


class StaffResolutionIn(BaseModel):
    staff: str = Field(min_length=2, description="who checked")
    charged: bool = Field(description="what the FMS or the processor showed when staff checked")
    fms_payment_id: str | None = Field(default=None, description="required when charged: the FMS payment that is this attempt")
    ledger_checked: bool = Field(default=False, description="when charged: the person checked the FMS ledger shows this payment applied")
    note: str = Field(min_length=10, description="what was checked, where, and by whom")


def create_app(settings: Settings | None = None, adapter: FMSAdapter | None = None, start_worker: bool = True,
               chat_client=None) -> FastAPI:
    settings = settings or Settings.from_env()
    if chat_client is None and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        chat_client = anthropic.Anthropic()
    # Conversations live in this process: a restart ends them (their proposals and payments are in the database).
    # Running more than one API process needs them stored, and the channel in front must route each tenant to one.
    conversations: dict[str, Conversation] = {}
    factory = make_session_factory(settings.database_url)
    adapter = adapter or CubbyAdapter(settings.fms_base_url, settings.fms_api_key, settings.fms_timeout_seconds)
    bind_to_fms(factory, adapter, settings.fms_base_url)
    ensure_append_only(factory.kw["bind"])
    with factory() as session:
        import_stitch(session)   # published lien-law reference data, stored once per file
    worker = Worker(factory, adapter, settings.reconcile_every_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(title="Gatehouse", lifespan=lifespan)
    app.state.session_factory = factory

    @app.exception_handler(StaleDataError)
    async def changed_meanwhile(_, exc: StaleDataError):
        return JSONResponse({"detail": "the record changed while this request was working on it; read it again and retry"}, status_code=409)

    @app.exception_handler(FMSError)
    async def fms_unavailable(_, exc: FMSError):
        log.warning("FMS call failed: %s", exc)
        return JSONResponse({"detail": "the facility system did not answer; nothing was changed by this request"}, status_code=502)

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

    def viewer(x_desk_token: str | None = Header(default=None)) -> str:
        """Reading. In the public preview anyone may read without a token; a token that is sent is still checked."""
        if settings.public_view and x_desk_token is None:
            return "visitor"
        return operator(x_desk_token)

    def check_facility(session: Session, facility_id: str) -> None:
        binding = session.get(DeskBinding, 1)
        if binding is not None and facility_id not in binding.facility_ids:
            raise HTTPException(404, "facility not found")

    def get_case(session: Session, case_id: int) -> DelinquencyCase:
        """A case on a facility the current FMS key can see; anything else is answered as not found."""
        case = session.get(DelinquencyCase, case_id)
        if case is not None:
            facility = session.get(Facility, session.get(Lease, case.lease_id).facility_id)
            binding = session.get(DeskBinding, 1)
            if binding is not None and facility.provider_id not in binding.facility_ids:
                case = None
        if case is None:
            raise HTTPException(404, "case not found")
        return case

    def command_json(c: PaymentCommand) -> dict:
        return {"id": c.id, "state": c.state, "amount": c.amount, "provider_payment_id": c.provider_payment_id,
                "provider_status": c.provider_status, "provider_reason": c.provider_reason, "checks": c.checks,
                "created_at": c.created_at, "confirmed_at": c.confirmed_at, "tenant_message": tenant_message(c)}

    @app.get("/health/live")
    def live():
        """The process answers. Nothing else is checked, so a slow FMS never gets the container restarted."""
        return {"ok": True}

    @app.get("/health/ready")
    def ready():
        """Ready to serve: the database answers, the FMS answers, and the worker has finished a pass lately."""
        checks: dict[str, str] = {}
        try:
            with factory() as session:
                session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = type(exc).__name__
        try:
            adapter.facilities()
            checks["fms"] = "ok"
        except Exception as exc:
            checks["fms"] = type(exc).__name__
        if start_worker:
            checks["worker"] = "ok" if worker.alive() else f"no recent pass ({worker.last_error or 'still working'})"
        ready_now = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ready_now, "checks": checks}, status_code=200 if ready_now else 503)

    @app.post("/api/facilities/{facility_id}/sync")
    def sync(facility_id: str, session: Session = Depends(db), _: str = Depends(operator)):
        check_facility(session, facility_id)
        try:
            cases = sync_facility(session, adapter, facility_id)
        except FMSError as exc:
            raise HTTPException(502, f"FMS read failed: {exc}") from exc
        return {"cases": len(cases)}

    @app.get("/api/facilities/{facility_id}/queue")
    def facility_queue(facility_id: str, session: Session = Depends(db), _: str = Depends(viewer)):
        check_facility(session, facility_id)
        return {"rows": [r.__dict__ for r in queue(session, facility_id)], "collected": collected(session, adapter.provider, facility_id)}

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: int, session: Session = Depends(db), _: str = Depends(viewer)):
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
            answer["total_due"] = adapter.ledger(session.get(Lease, case.lease_id).provider_id).total_due
            PaymentService(session, adapter)._refresh(case)
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
    def confirm_proposal(case_id: int, proposal_id: str, body: ConfirmIn, session: Session = Depends(db), _: str = Depends(operator)):
        """The tenant said yes, as heard by a named person. Sending this again returns the same payment."""
        get_case(session, case_id)
        try:
            command = confirm(session, adapter, case_id, proposal_id, f"desk: {body.confirmed_by}", body.tenant_answer)
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
            conversation = Conversation(case.id, adapter, chat_client, settings.chat_model, sender=body.sender)
            conversations[conversation.id] = conversation
        else:
            conversation = conversations.get(body.conversation_id)
            if conversation is None or conversation.case_id != case.id:
                raise HTTPException(404, "conversation not found")
            if not hmac.compare_digest(conversation.sender, body.sender):
                # a conversation id alone is not the tenant: the channel's sender has to match the one who started it
                raise HTTPException(403, "this conversation belongs to another sender")
        reply = conversation.say(session, body.text)
        return {"conversation_id": conversation.id, **reply.__dict__}

    @app.post("/api/cases/{case_id}/lien/evidence")
    def add_evidence(case_id: int, body: EvidenceIn, session: Session = Depends(db), _: str = Depends(operator)):
        case = get_case(session, case_id)
        try:
            row = record_evidence(session, case, body.kind, body.facts, body.entered_by, body.document_name, body.document_text, body.supersedes)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": row.id, "kind": row.kind, "facts": row.facts, "document_sha256": row.document_sha256}

    @app.get("/api/cases/{case_id}/lien")
    def lien_file(case_id: int, session: Session = Depends(db), _: str = Depends(operator)):
        """The lien file evaluated now: every finding with its basis, the timing under this policy, and the state."""
        case = get_case(session, case_id)
        ev = evaluate_case(session, adapter, case)
        facility = session.get(Facility, session.get(Lease, case.lease_id).facility_id)
        ref = reference_row(session, facility.jurisdiction)
        return {
            "evaluation_id": ev.id, "evaluated_at": ev.evaluated_at, "state": ev.state, "summary": ev.summary,
            "findings": ev.findings, "timing": ev.timing,
            "rule_version": ({"label": FL_R1.label, "code": ev.rule_version, "citations": FL_R1.citations, "review_note": FL_R1.review_note, "policies": FL_R1.policies}
                             if (ev.rule_version or "").startswith(FL_R1.label + "+") else None),
            "facts_digest": ev.facts_digest,
            "reference": ({"published_summary": ref.row, "attribution": ref.attribution,
                           "note": "Published reference data, not a rule: statute_read_on is when its authors read the statute, "
                                   "and earliest_sale is their summary, not a deadline."} if ref else None),
        }

    @app.get("/api/lien/evaluations/{evaluation_id}/replay")
    def replay_evaluation(evaluation_id: int, session: Session = Depends(db), _: str = Depends(viewer)):
        row = session.get(LienEvaluation, evaluation_id)
        if row is None:
            raise HTTPException(404, "evaluation not found")
        get_case(session, row.case_id)   # same facility rule as the case
        return {"evaluation_id": row.id, "same_result": replay(row)}

    @app.get("/api/payments/{command_id}")
    def payment(command_id: int, session: Session = Depends(db), _: str = Depends(viewer)):
        command = session.get(PaymentCommand, command_id)
        if command is None:
            raise HTTPException(404, "payment not found")
        get_case(session, command.case_id)   # same facility rule as the case
        return command_json(command)

    @app.post("/api/payments/{command_id}/resolve")
    def resolve(command_id: int, body: StaffResolutionIn, session: Session = Depends(db), _: str = Depends(operator),
                x_staff_token: str | None = Header(default=None)):
        """Staff settle a payment the desk could not: what they found at the FMS or the processor, in their words."""
        if settings.staff_token is None:
            raise HTTPException(503, "staff resolution is not configured")
        if x_staff_token is None or not hmac.compare_digest(x_staff_token, settings.staff_token):
            raise HTTPException(403, "staff token required")
        command = session.get(PaymentCommand, command_id)
        if command is None:
            raise HTTPException(404, "payment not found")
        get_case(session, command.case_id)
        if body.charged and not body.fms_payment_id:
            raise HTTPException(422, "a charged payment needs the FMS payment id")
        try:
            command = PaymentService(session, adapter).resolve_by_staff(command, body.charged, body.note, body.staff, body.fms_payment_id, body.ledger_checked)
        except PaymentRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        return command_json(command)

    @app.post("/api/reconcile")
    def reconcile(session: Session = Depends(db), _: str = Depends(operator)):
        return {"checked": PaymentService(session, adapter).reconcile_all()}

    return app
