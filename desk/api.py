"""The desk's HTTP API: the past-due queue, a case with its full history, identity checks and payments."""
from __future__ import annotations

import dataclasses
import hmac
import logging
import os
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm.exc import StaleDataError
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .adapter import FMSAdapter, FMSError, InboundText
from .adapter.cubby import CubbyAdapter
from .cases import queue, sync_facility
from .config import Settings
from .guard import ASK, LIFETIME, UNSET, Guard, Unreachable
from .conversation import Conversation
from . import spend
from .investigator import CALL_TIMEOUT, SALE_FRESH_FOR, investigate, run_view
from .investigator import progress as run_progress
from .lien_files import ensure_append_only, evaluate_case, record_evidence, replay
from .liens import FL_R1, ValidationError
from .db import bind_to_fms, make_session_factory, refresh_binding
from .messaging import inbox, match_pending, outbound_view, play_tenant, receive_text, send_text, unrouted
from .models import (
    AgentRun, CaseEvent, DelinquencyCase, DeskBinding, Facility, InboundMessage, Lease, LedgerSnapshot, LienEvaluation, PaymentCommand,
)
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


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=1600)
    staff: str = Field(min_length=2, description="the person at the desk who wrote the reply")
    request_id: str = Field(min_length=8, max_length=64,
                            description="chosen by the screen for each reply and kept for its retries; sending it twice sends one text")


class InvestigateIn(BaseModel):
    request_id: str = Field(min_length=8, max_length=64,
                            description="chosen by the screen for each request and kept for its retries; sending it twice is one run")
    again: bool = Field(default=False, description="prepare the case again on a text that already has notes")


class PlayIn(BaseModel):
    sender: str = Field(min_length=1, max_length=16, description="who is texting: dana, priya or unknown; the number comes from the desk")
    body: str = Field(max_length=2000, description="what the tenant types: 1 to 320 characters once control characters are removed")
    request_id: str = Field(min_length=8, max_length=64,
                            description="chosen by the screen for each press and kept for its retries; sending it twice is one text")


class VisitorIn(BaseModel):
    token: str = Field(min_length=1, max_length=2048, description="the answer the page's bot check gave")


class StaffResolutionIn(BaseModel):
    staff: str = Field(min_length=2, description="who checked")
    charged: bool = Field(description="what the FMS or the processor showed when staff checked")
    fms_payment_id: str | None = Field(default=None, description="required when charged: the FMS payment that is this attempt")
    ledger_checked: bool = Field(default=False, description="when charged: the person checked the FMS ledger shows this payment applied")
    note: str = Field(min_length=10, description="what was checked, where, and by whom")


def create_app(settings: Settings | None = None, adapter: FMSAdapter | None = None, start_worker: bool = True,
               chat_client=None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.public_view and not (settings.demo_seed and urlsplit(settings.fms_base_url).hostname in ("127.0.0.1", "localhost")):
        # anyone can read the preview without a token, so it may only ever hold the synthetic world
        raise RuntimeError("the public preview runs only on the synthetic world against the local simulator")
    if settings.public_view and (settings.operator_token is None or settings.allow_without_token):
        raise RuntimeError("the public preview needs an operator token for every change")
    if settings.public_view:
        # anyone may read the preview, so it never opens a database that existed before this process: each start
        # gets a new one and builds the synthetic world in it (a configured database is left untouched)
        preview_dir = tempfile.mkdtemp(prefix="gatehouse-preview-")
        # the synthetic world's outside auction site is served by the same local simulator
        settings = dataclasses.replace(settings, database_url=f"sqlite:///{preview_dir}/desk.db",
                                       marketplace_url=settings.marketplace_url or settings.fms_base_url)
    if chat_client is None and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        # a call that hangs is cut off after a minute and tried once more, so no run sits on a dead connection for ten
        chat_client = anthropic.Anthropic(timeout=CALL_TIMEOUT, max_retries=1)
    # the bot check is on only in the preview that lets a visitor start a run, and only when both of its keys are set
    guard = Guard(settings.turnstile_sitekey, settings.turnstile_secret, settings.turnstile_host) if settings.public_view and settings.public_investigate and chat_client is not None else Guard()
    # Conversations live in this process: a restart ends them (their proposals and payments are in the database).
    # Running more than one API process needs them stored, and the channel in front must route each tenant to one.
    conversations: dict[str, Conversation] = {}
    factory = make_session_factory(settings.database_url)
    adapter = adapter or CubbyAdapter(settings.fms_base_url, settings.fms_api_key, settings.fms_timeout_seconds)
    bind_to_fms(factory, adapter, settings.fms_base_url)
    ensure_append_only(factory.kw["bind"])
    with factory() as session:
        import_stitch(session)   # published lien-law reference data, stored once per file
    trace: dict = {}
    if settings.demo_seed:
        from . import demo
        demo.build(factory, adapter, settings.fms_base_url, settings.run_id, settings.marketplace_url)
        if settings.public_view:
            try:
                trace = demo.trace(settings.fms_base_url)
            except Exception:
                # the pages still work without it; they just do not show the simulator's own record
                log.exception("could not save the simulator's record of the seeded payments")
    worker = Worker(factory, adapter, settings.reconcile_every_seconds, run_id=settings.run_id, feed_url=settings.marketplace_url,
                    text_numbers=settings.text_numbers)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(title="Gatehouse", lifespan=lifespan)
    app.state.session_factory = factory
    app.state.guard = guard
    if settings.public_view:
        # the operator screens; for now they exist only in the read-only preview, which needs no sign-in
        from fastapi.staticfiles import StaticFiles

        from .web import HERE, Pages, router
        app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
        app.include_router(router(Pages(factory, trace, worker if start_worker else None,
                                        live=chat_client is not None and settings.public_investigate, guard=guard)))

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

    def actor(x_desk_token: str | None = Header(default=None)) -> str:
        """Who is starting a run: the operator, or, in the preview with live runs switched on, any visitor, within the limits.
        A token that is sent is always checked."""
        if x_desk_token is None and settings.public_view and settings.public_investigate:
            return "visitor"
        return operator(x_desk_token)

    def viewer(x_desk_token: str | None = Header(default=None)) -> str:
        """Reading. In the public preview anyone may read without a token; a token that is sent is still checked."""
        if settings.public_view and x_desk_token is None:
            return "visitor"
        return operator(x_desk_token)

    def check_facility(session: Session, facility_id: str) -> None:
        binding = session.get(DeskBinding, 1)
        if binding is not None and facility_id not in binding.facility_ids:
            raise HTTPException(404, "facility not found")

    def seen_by(message: InboundMessage, who: str, request: Request) -> bool:
        """A text a visitor typed is seen by that visitor and by the operator; a text from the webhook by anyone who may read."""
        return message.visitor is None or who != "visitor" or message.visitor == spend.visitor_of(request)

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
        if not start_worker:
            checks["worker"] = "not running in this process"
        elif not worker.alive():
            checks["worker"] = f"no successful pass lately (last error: {worker.last_error or 'none, still working'})"
        else:
            checks["worker"] = "ok"
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

    def owns(binding: DeskBinding, text: InboundText) -> bool:
        """A text belongs to this desk when its facility is one the FMS key sees. A text with no facility (a number several
        facilities share) belongs to it when its organization is one the key sees."""
        if text.facility_provider_id is not None:
            return text.facility_provider_id in binding.facility_ids
        return text.org_id in (binding.org_ids or [])

    def known_facility(session: Session, facility_id: str) -> Facility | None:
        """The desk's row for a facility the FMS key can see, or None when nothing has been synced from it yet."""
        check_facility(session, facility_id)
        return session.scalar(select(Facility).where(Facility.provider_id == facility_id))

    @app.post("/webhooks/cubby/sms-received")
    def sms_received(payload: dict, x_webhook_token: str | None = Header(default=None), session: Session = Depends(db)):
        """Cubby's SMS Received webhook. Cubby documents no signing, so the sender must present the shared token, in a header
        that a gateway such as Make.com adds. A 200 tells Cubby the text is kept, and a text already kept is a 200 too,
        because Cubby may deliver one twice."""
        if settings.webhook_token is None:
            raise HTTPException(503, "the desk has no webhook token configured")
        if x_webhook_token is None or not hmac.compare_digest(x_webhook_token, settings.webhook_token):
            raise HTTPException(401, "webhook token required")
        try:
            text = adapter.inbound_text(payload)
        except ValueError as exc:
            raise HTTPException(422, f"not an SMS Received payload: {exc}") from exc
        binding = session.get(DeskBinding, 1)
        if binding is None:
            raise HTTPException(503, "the desk has not recorded which FMS account it serves yet")
        if not owns(binding, text):
            # a facility added to the account after the desk started is not in the binding yet: ask the FMS once
            try:
                refresh_binding(session, adapter)
            except FMSError as exc:
                raise HTTPException(503, "could not check which facilities this desk serves; try again") from exc
            session.refresh(binding)
            if not owns(binding, text):
                raise HTTPException(404, "this text is not for a facility or organization this desk serves")
        row = receive_text(session, text, settings.text_numbers, set(binding.facility_ids))
        if row is not None and row.facility_id is not None:
            try:
                match_pending(session, adapter, session.get(Facility, row.facility_id))
            except FMSError as exc:
                # the text is kept; the worker checks it on its next pass
                log.warning("could not match text %s yet: %s", row.id, exc)
        return {"stored": row is not None}

    @app.get("/api/facilities/{facility_id}/inbox")
    def facility_inbox(facility_id: str, limit: int = 50, offset: int = 0, session: Session = Depends(db), _: str = Depends(viewer)):
        """The texts people sent this facility, newest first, each matched to a tenant or marked as not matched."""
        facility = known_facility(session, facility_id)
        return {"messages": inbox(session, facility, min(max(limit, 1), 200), max(offset, 0)) if facility else []}

    @app.get("/api/inbox/unrouted")
    def unrouted_texts(limit: int = 50, offset: int = 0, session: Session = Depends(db), _: str = Depends(viewer)):
        """Texts kept without a facility because no facility is set for the number that received them."""
        binding = session.get(DeskBinding, 1)
        return {"messages": unrouted(session, binding.facility_ids if binding else [], min(max(limit, 1), 200), max(offset, 0))}

    @app.post("/api/inbox/{message_id}/reply")
    def reply_to_text(message_id: int, body: ReplyIn, session: Session = Depends(db), _: str = Depends(operator)):
        """A person at the desk answers a text through Cubby. The rule still applies: a number that texted STOP, one the FMS
        has switched off, or one that is not the customer's number on file gets nothing, and the answer says why."""
        message = session.get(InboundMessage, message_id)
        facility = session.get(Facility, message.facility_id) if message is not None and message.facility_id is not None else None
        if message is None or facility is None:
            raise HTTPException(404, "text not found")
        check_facility(session, facility.provider_id)
        if message.match_state in ("pending", "ambiguous"):
            raise HTTPException(409, "not sent: " + (message.match_note or "this text is not matched to a tenant yet; try again in a moment"))
        try:
            row = send_text(session, adapter, facility, message.customer_provider_id if message.match_state == "matched" else None,
                            body.text, f"staff: {body.staff}", reply_to=message, request_id=body.request_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if row.state in ("blocked", "rejected"):
            raise HTTPException(409, f"not sent: {row.reason}")
        return outbound_view(row)

    # the count of a visitor's texts and the new text are one step: presses that arrive together cannot each find a free place
    text_lock = threading.Lock()

    def from_elsewhere(request: Request) -> bool:
        origin = request.headers.get("origin")
        return bool(origin) and urlsplit(origin).netloc != (request.headers.get("x-forwarded-host") or request.headers.get("host", ""))

    def require_check(request: Request, who: str) -> None:
        """A visitor starts a run or types a text only after passing the bot check. The operator is not asked. A check with one key set and not
        the other is a mistake in how the desk was started, and it closes both paths."""
        if who != "visitor":
            return
        if guard.broken:
            raise HTTPException(503, UNSET)
        if guard.on and guard.valid(request.cookies.get(spend.COOKIE)) is None:
            raise HTTPException(403, ASK)

    @app.post("/api/visitor")
    def check_visitor(body: VisitorIn, request: Request, response: Response):
        """The bot check, once for each browser. The page sends what the check gave it; the desk asks whether it is genuine, was given on
        this address and was given for this check, and only then gives the browser its signed cookie. A browser that already holds one
        keeps it: a new cookie would be a new count to start from."""
        if guard.broken:
            raise HTTPException(503, UNSET)
        if not guard.on:
            raise HTTPException(404, "not found")
        if from_elsewhere(request):
            raise HTTPException(403, "this request came from another site")
        if guard.valid(request.cookies.get(spend.COOKIE)):
            return {"verified": True}
        if not guard.allow(spend.visitor_of(request)):
            raise HTTPException(429, "Too many tries from here. Wait a while and reload the page.")
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host", "")).split(",")[0].strip().lower().split(":")[0]
        try:
            passed = guard.passed(body.token, spend.address_of(request), host)
        except Unreachable as exc:
            log.warning("the bot check did not answer: %s", exc)
            raise HTTPException(503, "The quick check is not answering. Try again in a minute.") from exc
        if not passed:
            raise HTTPException(403, "The quick check did not pass. Reload the page and try again.")
        response.set_cookie(spend.COOKIE, guard.cookie(), max_age=LIFETIME, httponly=True, samesite="lax",
                            secure=request.headers.get("x-forwarded-proto") == "https")
        return {"verified": True}

    @app.post("/api/inbox/texts")
    def play_text(body: PlayIn, request: Request, session: Session = Depends(db), who: str = Depends(actor)):
        """A visitor plays the tenant: types a text as one of the tenants they may play. The desk keeps it the way it keeps a text from the
        webhook, matches it to a tenant by the number, and it belongs to this visitor alone. The visitor supplies only the words."""
        # the desk answers a typed text through the FMS it is attached to, so it keeps one only where that FMS is the synthetic one
        if not settings.public_view or chat_client is None or not settings.public_investigate:
            raise HTTPException(503, "texts can be typed only on the synthetic preview, where the agent is switched on")
        if from_elsewhere(request):
            raise HTTPException(403, "this request came from another site")
        require_check(request, who)
        visitor = spend.visitor_of(request)
        binding = session.get(DeskBinding, 1)
        with text_lock:
            reason = spend.text_refusal(session, visitor)
            if reason:
                raise HTTPException(429, reason)
            try:
                row = play_tenant(session, adapter, body.sender, body.body, visitor, body.request_id, set(binding.facility_ids) if binding else set())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        facility = session.get(Facility, row.facility_id) if row.facility_id is not None else None
        log.info("a visitor typed a text: visitor %s, sender %s, %d characters, %s", visitor[:6], body.sender, len(body.body), row.match_state)
        return {"id": row.id, "facility": facility.provider_id if facility is not None else None, "match": row.match_state}

    @app.post("/api/inbox/{message_id}/investigate")
    def investigate_text(message_id: int, body: InvestigateIn, request: Request, response: Response, session: Session = Depends(db),
                         who: str = Depends(actor)):
        """Reads a matched text, looks up the desk's records and prepares the case. The desk checks the packet against
        its own records. Needs a model key. A text has one run unless `again` is set, and a double click is one run."""
        if chat_client is None:
            raise HTTPException(503, "the desk has no model key configured")
        require_check(request, who)
        message = session.get(InboundMessage, message_id)
        facility = session.get(Facility, message.facility_id) if message is not None and message.facility_id is not None else None
        if message is None or facility is None or not seen_by(message, who, request):
            raise HTTPException(404, "text not found")
        check_facility(session, facility.provider_id)
        if message.match_state != "matched" or not message.customer_provider_id:
            raise HTTPException(409, "this text is not matched to one tenant yet, so there is nothing to look up")
        visitor = spend.visitor_of(request) if who == "visitor" else None
        try:
            # a sale item counts as read lately if the worker has passed over it a few times since, however slowly it is set to run
            run, _started = investigate(session, chat_client, message, body.request_id, body.again or who == "visitor",
                                        max(SALE_FRESH_FOR, timedelta(seconds=6 * settings.reconcile_every_seconds)), visitor,
                                        lambda s, exclude: spend.refusal(s, settings.daily_budget_usd, settings.visitor_cases_per_day, visitor, exclude), adapter=adapter)
        except spend.Refused as exc:
            raise HTTPException(429, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if run.status == "running":
            response.status_code = 202          # a retry gets the run that is going, so the screen can poll it by its id
        left = spend.allowance(session, settings.daily_budget_usd, settings.visitor_cases_per_day, visitor)
        return {**run_view(run), "limits": {"cases_left": left.cases_left, "budget_left_usd": round(left.budget_left_usd, 4)}}

    @app.get("/api/inbox/{message_id}/progress")
    def inbox_progress(message_id: int, request: Request, session: Session = Depends(db), who: str = Depends(viewer)):
        """The steps the latest run on a text has taken so far, in plain words, for a page that is waiting for the run to finish."""
        message = session.get(InboundMessage, message_id)
        facility = session.get(Facility, message.facility_id) if message is not None and message.facility_id is not None else None
        if message is None or facility is None or not seen_by(message, who, request):
            raise HTTPException(404, "text not found")
        check_facility(session, facility.provider_id)
        return run_progress(session, message_id)

    @app.get("/api/agent-runs/{run_id}")
    def agent_run(run_id: int, request: Request, session: Session = Depends(db), who: str = Depends(viewer)):
        """One run with its trace: every model turn and lookup, and the records each lookup returned."""
        run = session.get(AgentRun, run_id)
        message = session.get(InboundMessage, run.inbound_id) if run is not None else None
        facility = session.get(Facility, message.facility_id) if message is not None and message.facility_id is not None else None
        if run is None or facility is None or not seen_by(message, who, request):
            raise HTTPException(404, "run not found")
        check_facility(session, facility.provider_id)
        return run_view(run, trace=True)

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
