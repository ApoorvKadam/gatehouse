"""A narrow tenant conversation about one past-due account.

Claude reads what the tenant says and decides which question to ask or which lookup to make. It cannot move
money: it has no tool that takes a payment, only one that proposes an amount. The payment runs when the tenant's
very next message is an explicit yes to that exact proposal, and that check is code, not the model. Every payment
result the tenant hears is the desk's own wording (payments.tenant_message), so the model never gets to say a
payment went through."""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .adapter import FMSAdapter, FMSError
from .cases import record
from .db import utcnow
from .models import DelinquencyCase, Facility, Lease, PaymentCommand, PaymentProposal
from .payments import IDENTITY_VALID_FOR, IdentityClaim, PaymentRefused, check_identity, confirm, identity_locked, propose, tenant_message

log = logging.getLogger("desk.conversation")

# dollars per million tokens (input, output), only to log what each conversation costs
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}
# a confirmation is a short, unambiguous yes and nothing else
CONFIRM = re.compile(r"^\s*(yes|yes please|yep|confirm|confirmed|i confirm|go ahead|yes,? go ahead)[\s.!]*$", re.I)
MAX_MODEL_TURNS = 6

SYSTEM = """You handle messages for {facility}, a self-storage facility, about one storage unit whose rent is past due.

What you can do:
- Ask the caller to confirm their identity with their last name, the last four digits of the phone number on file and their unit number, then call verify_identity. Until it succeeds, do not reveal anything about the account, including whether a balance exists.
- After verification, call get_balance and tell the caller what is owed. It lists the open charges; if asked about one, say what the ledger shows (its description, amount and period) and nothing more. A question about a charge is not a dispute.
- If the caller wants to pay, call propose_payment with the amount they choose (all of it or part of it). The desk then sends the confirmation question itself, naming the amount and card; it appears in the conversation as your message. If the caller's next message is a plain yes, the desk charges the card and gives the result. Anything else cancels that proposal: if they still want to pay, call propose_payment again. You never take a payment yourself and never say a payment went through.
- Call payment_status if they ask what happened to a payment.
- Call hand_to_staff for anything outside paying this balance: the caller says a charge is wrong, hardship or payment plans, military service, bankruptcy, questions about liens or auctions, legal questions, or a caller who is upset or asks for a person.

Write the way a good front-desk manager talks on the phone: short, plain sentences, never a list or numbered steps (ask for the three identity details in one sentence). Do not invent fees, dates or policies."""

TOOLS = [
    {"name": "verify_identity", "description": "Check the caller against the tenant on file for this unit.",
     "input_schema": {"type": "object", "properties": {
         "last_name": {"type": "string"}, "phone_last4": {"type": "string", "description": "last four digits of the phone number on file"},
         "unit_name": {"type": "string"}}, "required": ["last_name", "phone_last4", "unit_name"], "additionalProperties": False}},
    {"name": "get_balance", "description": "Read the current balance from the facility management system. Works only after verify_identity succeeded.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "propose_payment", "description": "Propose charging the card on file. The caller must then confirm; nothing is charged by this tool.",
     "input_schema": {"type": "object", "properties": {"amount_cents": {"type": "integer", "minimum": 1}},
                      "required": ["amount_cents"], "additionalProperties": False}},
    {"name": "payment_status", "description": "The state of the most recent payment on this account.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "hand_to_staff", "description": "Hand the conversation to a person, with the reason.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}},
]

STAFF_LINE = "A team member has your message and will follow up with you directly."
IDENTITY_ASK = ("Before I can talk about the account, I need the last name on the rental, the last four digits of the "
                "phone number on file and your unit number.")
NO_PAYMENT_LINE = "I don't see a payment from this conversation yet. If you'd like to pay, I can set that up."
# what the model must never say on its own: that money moved or that the account is settled
OUTCOME_CLAIM = re.compile(
    r"\bpayment\b[^.?!]{0,60}?\b(went through|has gone through|(has|have) been (applied|processed|posted|received|completed|charged)"
    r"|was (applied|processed|posted|received|successful|completed|charged))"
    r"|\bcard\b[^.?!]{0,40}?\b(has been|was|got) charged\b"
    r"|\b(it|that|everything) went through\b"
    r"|\byou('re| are) (now )?(all )?(paid up|paid in full|caught up|current|all set)\b"
    r"|\b(balance|account) (is|has been) (now )?(paid|cleared|settled)\b", re.I)
MONEY = re.compile(r"\$\s?\d")
MONEY_SPAN = re.compile(r"\$\s?[\d,]+(\.\d+)?")


@dataclass
class Reply:
    text: str
    verified: bool
    proposed_amount: int | None
    card_last4: str | None
    payment_id: int | None
    payment_state: str | None
    handed_to_staff: bool


class Conversation:
    """One tenant thread. Holds no database session: every call gets the request's own."""

    def __init__(self, case_id: int, adapter: FMSAdapter, client, model: str = "claude-haiku-5-5"):
        self.id = uuid.uuid4().hex
        self.case_id, self.adapter, self.client, self.model = case_id, adapter, client, model
        self.lock = threading.Lock()
        self.messages: list = []
        self.identity_check_id: str | None = None
        self.verified_at = None
        self.proposal_id: str | None = None
        self.command_id: int | None = None
        self.handed_to_staff = False
        self.cost_usd = 0.0

    @property
    def identity_verified(self) -> bool:
        return self.verified_at is not None and utcnow() - self.verified_at <= IDENTITY_VALID_FOR

    # ------------------------------------------------------------------ one tenant message
    def say(self, session: Session, text: str) -> Reply:
        with self.lock:
            case = session.get(DelinquencyCase, self.case_id)
            record(session, case, "tenant said", {"conversation": self.id, "text": text}, actor="tenant", source="chat")
            self.messages.append({"role": "user", "content": text})
            proposal_id = self.proposal_id
            if proposal_id is not None and CONFIRM.match(text) and not self.handed_to_staff:
                self.proposal_id = None
                answer = self._confirm(session, proposal_id)
            else:
                self._void_proposal(session)   # a proposal is good for the very next message only
                answer = STAFF_LINE if self.handed_to_staff else self._model_turns(session)
            if self.messages[-1]["role"] != "assistant":
                self.messages.append({"role": "assistant", "content": answer})
            record(session, case, "desk replied", {"conversation": self.id, "text": answer}, source="chat")
            session.commit()
            return self._reply(session, answer)

    def _confirm(self, session: Session, proposal_id: str) -> str:
        try:
            command = confirm(session, self.adapter, self.case_id, proposal_id)
        except PaymentRefused as exc:
            log.info("conversation %s: payment refused: %s", self.id, exc)
            self._to_staff(session, session.get(DelinquencyCase, self.case_id), f"payment refused: {exc}")
            return "I can't take a payment on this account right now. A team member will check it and contact you."
        except FMSError as exc:
            # take_payment raises this only before anything is sent to the FMS
            log.warning("conversation %s: FMS unavailable on confirm: %s", self.id, exc)
            return "I couldn't reach our system, and nothing was charged. Please try again in a few minutes."
        self.command_id = command.id
        return tenant_message(command)

    def _void_proposal(self, session: Session) -> None:
        if self.proposal_id is not None:
            proposal = session.get(PaymentProposal, self.proposal_id)
            if proposal is not None and proposal.command_id is None:
                proposal.expires_at = utcnow()
            self.proposal_id = None

    # ------------------------------------------------------------------ the model
    def _model_turns(self, session: Session) -> str:
        lease = session.get(Lease, session.get(DelinquencyCase, self.case_id).lease_id)
        system = SYSTEM.format(facility=session.get(Facility, lease.facility_id).name)
        for _ in range(MAX_MODEL_TURNS):
            response = self.client.messages.create(model=self.model, max_tokens=2048, system=system, tools=TOOLS,
                                                   messages=self.messages, output_config={"effort": "low"})
            self._log_usage(response)
            if response.stop_reason in ("refusal", "max_tokens"):
                self._to_staff(session, session.get(DelinquencyCase, self.case_id), f"model stopped: {response.stop_reason}")
                return "I can't help with that here. A team member will follow up with you."
            # the whole content goes back as sent: it carries the thinking blocks the next call needs
            self.messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                text = " ".join(b.text for b in response.content if b.type == "text").strip()
                return self._checked(session, text) or "Sorry, could you say that again?"
            results = [{"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(self._safe_tool(session, lease, c))}
                       for c in calls]
            self.messages.append({"role": "user", "content": results})
            # these answers are the desk's own words, not the model's
            if self.handed_to_staff:
                return STAFF_LINE
            if self.proposal_id is not None:
                p = session.get(PaymentProposal, self.proposal_id)
                return f"To confirm: we will charge ${p.amount / 100:,.2f} to the card ending in {p.card_last4}. Reply yes to go ahead."
            if any(c.name == "payment_status" for c in calls):
                return self._status_line(session) if self.identity_verified else IDENTITY_ASK
        self._to_staff(session, session.get(DelinquencyCase, self.case_id), "conversation went in circles")
        return STAFF_LINE

    def _checked(self, session: Session, text: str) -> str:
        """The model's text goes out only if it claims nothing the desk cannot back: no account figures before the
        caller is verified, and no payment outcome unless this conversation's payment is confirmed."""
        replacement = None
        if MONEY.search(text) and not self.identity_verified:
            replacement = IDENTITY_ASK
        elif OUTCOME_CLAIM.search(MONEY_SPAN.sub("AMOUNT", text)):   # "$240.00" must not end the sentence early
            command = session.get(PaymentCommand, self.command_id) if self.command_id else None
            if command is None or command.state != "CONFIRMED":
                replacement = self._status_line(session)
        if replacement is None:
            return text
        record(session, session.get(DelinquencyCase, self.case_id), "model reply replaced",
               {"conversation": self.id, "model_said": text, "sent": replacement}, source="chat")
        return replacement

    def _status_line(self, session: Session) -> str:
        command = session.get(PaymentCommand, self.command_id) if self.command_id else None
        return tenant_message(command) if command else NO_PAYMENT_LINE

    def _safe_tool(self, session: Session, lease: Lease, call) -> dict:
        """Every tool call gets a result, whatever happens, or the conversation history can no longer be sent."""
        try:
            return self._run_tool(session, lease, call.name, call.input)
        except Exception:
            log.exception("conversation %s: tool %s failed", self.id, call.name)
            return {"error": "the desk could not do that just now"}

    def _run_tool(self, session: Session, lease: Lease, name: str, args: dict) -> dict:
        case = session.get(DelinquencyCase, self.case_id)
        try:
            if name == "verify_identity":
                claim = IdentityClaim(str(args.get("last_name", "")), str(args.get("phone_last4", "")), str(args.get("unit_name", "")))
                check = check_identity(session, self.adapter, case, claim)
                if check is None:
                    self._to_staff(session, case, "identity not confirmed after three tries")
                    return {"verified": False, "reason": "too many failed checks; a team member has to verify this caller"}
                record(session, case, "identity checked", {"conversation": self.id, "verified": check.verified, "reason": check.reason}, source="chat")
                if check.verified:
                    self.identity_check_id, self.verified_at = check.id, check.created_at
                    return {"verified": True}
                if identity_locked(session, case.id):
                    self._to_staff(session, case, "identity not confirmed after three tries")
                return {"verified": False, "reason": check.reason}
            if name == "get_balance":
                if not self.identity_verified:
                    return {"error": "identity not verified, or the check expired; verify again before discussing the account"}
                ledger = self.adapter.ledger(lease.provider_id)
                open_charges = [{"what": (e.get("details") or {}).get("description") or (e.get("details") or {}).get("category"),
                                 "open_cents": e["amount"] - e.get("amountPaid", 0), "for_period_starting": (e.get("details") or {}).get("accruesOn")}
                                for e in ledger.raw.get("bookEntries", []) if e.get("type") == "CHARGE" and e["amount"] > e.get("amountPaid", 0)]
                return {"total_due_cents": ledger.total_due, "open_charges": open_charges}
            if name == "propose_payment":
                if self.handed_to_staff:
                    return {"error": "this conversation is with staff now; no payment can be set up here"}
                if not self.identity_verified:
                    return {"error": "identity not verified, or the check expired"}
                try:
                    # the tenant's first saved card; choosing among several cards is not offered in chat yet
                    p = propose(session, self.adapter, case, self.identity_check_id, int(args.get("amount_cents", 0)), channel="chat")
                except PaymentRefused as exc:
                    return {"error": str(exc)}
                self.proposal_id = p.id
                return {"proposed_cents": p.amount, "card_last4": p.card_last4,
                        "status": "the desk is asking the caller to confirm; only a plain yes as their next message charges the card"}
            if name == "payment_status":
                if not self.identity_verified:
                    return {"error": "identity not verified, or the check expired"}
                command = session.get(PaymentCommand, self.command_id) if self.command_id else None
                return {"state": command.state if command else "no payment yet", "status": "the desk tells the caller the status itself"}
            if name == "hand_to_staff":
                self._to_staff(session, case, args.get("reason"))
                return {"handed_to_staff": True}
        except FMSError as exc:
            return {"error": f"the facility system did not answer: {exc}"}
        return {"error": f"unknown tool {name}"}

    def _to_staff(self, session: Session, case: DelinquencyCase, reason: str | None) -> None:
        self._void_proposal(session)
        if not self.handed_to_staff:
            self.handed_to_staff = True
            record(session, case, "handed to staff", {"conversation": self.id, "reason": reason}, source="chat")

    def _log_usage(self, response) -> None:
        u = response.usage
        price_in, price_out = PRICES.get(self.model, (0.0, 0.0))
        cost = (u.input_tokens * price_in + u.output_tokens * price_out) / 1_000_000
        self.cost_usd += cost
        log.info("conversation %s model=%s in=%d out=%d cost=$%.5f total=$%.5f", self.id, self.model,
                 u.input_tokens, u.output_tokens, cost, self.cost_usd)

    def _reply(self, session: Session, text: str) -> Reply:
        # what the channel may show next to the reply: nothing about the account once the verification lapses
        command = session.get(PaymentCommand, self.command_id) if self.command_id and self.identity_verified else None
        p = session.get(PaymentProposal, self.proposal_id) if self.proposal_id else None
        return Reply(text, self.identity_verified, p.amount if p else None, p.card_last4 if p else None,
                     command.id if command else None, command.state if command else None, self.handed_to_staff)
