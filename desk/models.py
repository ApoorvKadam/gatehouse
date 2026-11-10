"""The desk's own records. Three kinds of fact are kept apart on purpose:

- what the FMS reports (ledger snapshots, provider payment status), copied with the time it was observed;
- what the desk itself did or saw (payment commands, case events);
- what the desk concludes from those (a case's state and hold reasons).

The desk never keeps a balance of its own. A balance always comes from the latest FMS snapshot."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow
from .wording import action_view, plain


class Facility(Base):
    __tablename__ = "facilities"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    provider_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    jurisdiction: Mapped[str] = mapped_column(String)
    time_zone: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("provider", "provider_id"),)


class Lease(Base):
    __tablename__ = "leases"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    provider: Mapped[str] = mapped_column(String)
    provider_id: Mapped[str] = mapped_column(String)
    provider_customer_id: Mapped[str] = mapped_column(String)
    unit_name: Mapped[str] = mapped_column(String)
    billing_cycle: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("provider", "provider_id"),)


class LedgerSnapshot(Base):
    """One read of a lease's ledger, exactly as the FMS answered."""

    __tablename__ = "ledger_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    total_due: Mapped[int] = mapped_column(Integer)
    prepaid: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # when the read began: the ledger shown is no older than this, and it may have been stored after later things happened
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON)


class DelinquencyCase(Base):
    """One delinquency episode on one lease, from the first missed charge until it is paid off."""

    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    state: Mapped[str] = mapped_column(String, default="open")
    hold_reasons: Mapped[list] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String, default="front desk")
    last_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_snapshots.id"), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # a refresh that read the FMS before another one committed must not publish its older picture
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version}


# a payment in one of these states still holds the account: no second payment may start until it settles
HOLDING_STATES = ("SUBMITTING", "AWAITING_APPLICATION", "PENDING_AT_FMS", "OUTCOME_UNKNOWN", "NEEDS_STAFF")
# what the FMS can later report about a payment that had succeeded
FMS_REVERSAL_STATUSES = ("DIRECT_DEBIT_RETURN", "REFUNDED", "REFUND_INITIATED", "DISPUTED")
_HOLDING_SQL = text("state IN (" + ", ".join(f"'{s}'" for s in HOLDING_STATES) + ")")
_RUNNING_SQL = text("status = 'running'")


class PaymentCommand(Base):
    """An authorized intent to take one payment, and everything learned about it afterwards.

    The row is written before the FMS is called, so a crash or a lost response leaves a record of what
    was attempted instead of nothing."""

    __tablename__ = "payment_commands"
    # the database, not a read before the insert, keeps two requests from charging the same lease at once, even
    # through two cases of that lease
    __table_args__ = (Index("one_holding_payment_per_lease", "lease_id", unique=True,
                            sqlite_where=_HOLDING_SQL, postgresql_where=_HOLDING_SQL),)
    id: Mapped[int] = mapped_column(primary_key=True)
    # every update checks the version it read, so two passes that read the same state cannot both write
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version}
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    payment_method_id: Mapped[str] = mapped_column(String)
    authorization: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String)
    balance_before: Mapped[int] = mapped_column(Integer)
    prepaid_before: Mapped[int] = mapped_column(Integer)
    method_type: Mapped[str | None] = mapped_column(String, nullable=True)
    # amountPaid of each ledger entry when the tenant agreed; only money landing on these counts as this payment
    entries_paid_before: Mapped[dict] = mapped_column(JSON, default=dict)
    # every payment the FMS already showed on the lease, with its status, just before this one was sent
    fms_payments_before: Mapped[dict] = mapped_column(JSON, default=dict)
    # an FMS payment that may be this lost submission; only staff can confirm that it is
    suggested_payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # when the FMS reported this payment returned, refunded or disputed
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # staff named this FMS payment as this attempt; that says which payment it is, not that it posted
    identified_by_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    checks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ObservedPayment(Base):
    """A payment as the FMS reports it, once per provider payment, however many leases it touched."""

    __tablename__ = "observed_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    provider_payment_id: Mapped[str] = mapped_column(String)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    lease_provider_ids: Mapped[list] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # when the FMS says the payment was made (its createdAt); None when it gave no time. first_seen_at is when the desk looked, which says
    # nothing about order: a desk that has just started sees every old payment for the first time now
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # when the desk saw the status change; None when the payment was already reversed the first time the desk saw it
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("provider", "provider_payment_id"),)


class CaseEvent(Base):
    """An append-only history: who did what, where it came from, when it happened and when we saw it."""

    __tablename__ = "case_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    kind: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdentityCheck(Base):
    """A caller proved who they are for one case. Payments cite it; a check expires after a short while."""

    __tablename__ = "identity_checks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    verified: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentProposal(Base):
    """An amount and a card read back to a verified tenant, waiting for their yes.

    The desk builds it from the FMS's own balance and the tenant's saved cards, never from what a client sends,
    and a payment can only be taken by confirming one. Its id is the payment's idempotency key, so a second yes
    or a double click returns the same payment."""

    __tablename__ = "payment_proposals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    identity_check_id: Mapped[str] = mapped_column(ForeignKey("identity_checks.id"))
    channel: Mapped[str] = mapped_column(String)
    amount: Mapped[int] = mapped_column(Integer)
    balance_seen: Mapped[int] = mapped_column(Integer)
    payment_method_id: Mapped[str] = mapped_column(String)
    card_last4: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    # who reported the tenant's yes, and the tenant's words: the chat itself, or a named person at the desk
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    tenant_answer: Mapped[str | None] = mapped_column(String, nullable=True)
    command_id: Mapped[int | None] = mapped_column(ForeignKey("payment_commands.id"), nullable=True)


class DemoSeed(Base):
    """Written as the last step of building the synthetic world, so a seed that stopped part-way is never mistaken
    for a finished one on the next start."""

    __tablename__ = "demo_seed"
    id: Mapped[int] = mapped_column(primary_key=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DeskBinding(Base):
    """The FMS account this database belongs to. One deployment serves one operator; pointing a database at another
    operator's FMS key would mix their accounts, so the desk refuses to start."""

    __tablename__ = "desk_binding"
    id: Mapped[int] = mapped_column(primary_key=True)
    fms_base_url: Mapped[str] = mapped_column(String, default="")
    facility_ids: Mapped[list] = mapped_column(JSON, default=list)
    # the organizations those facilities belong to, from the facility objects; empty when the FMS does not say
    org_ids: Mapped[list] = mapped_column(JSON, default=list)
    fms_fingerprint: Mapped[str] = mapped_column(String, default="")
    bound_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RuleSourceRow(Base):
    """One row of a published lien-law dataset, stored exactly as published with the file it came from. Rows are
    reference material for the people who write rule versions; nothing here is executable, and nothing edits them."""

    __tablename__ = "rule_source_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String)
    file_sha256: Mapped[str] = mapped_column(String)
    jurisdiction: Mapped[str] = mapped_column(String)
    row: Mapped[dict] = mapped_column(JSON)
    attribution: Mapped[str] = mapped_column(String)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("file_sha256", "jurisdiction"),)


class LienEvidence(Base):
    """One piece of lien evidence as someone recorded it, with the document it rests on. A correction is a new record
    that supersedes the old one; nothing is edited."""

    __tablename__ = "lien_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    kind: Mapped[str] = mapped_column(String)
    facts: Mapped[dict] = mapped_column(JSON)
    document_name: Mapped[str | None] = mapped_column(String, nullable=True)
    document_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    document_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    entered_by: Mapped[str] = mapped_column(String)
    entered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # one correction per record, enforced by the database so two concurrent corrections cannot both be current
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("lien_evidence.id"), nullable=True, unique=True)


class LienEvaluation(Base):
    """An evaluation with the exact facts it read, so it can be replayed and compared with today's."""

    __tablename__ = "lien_evaluations"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    rule_version: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(String, default="")
    facts: Mapped[dict] = mapped_column(JSON)
    facts_digest: Mapped[str] = mapped_column(String)
    findings: Mapped[list] = mapped_column(JSON)
    timing: Mapped[dict] = mapped_column(JSON)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AccessPolicy(Base):
    """A facility's lockout rule as an operator stated it, with who approved it and from when. Cubby documents no such
    setting, so Gatehouse never infers it from the FMS; a facility without one gets a review item, not a verdict.
    gate_rule says what one gate code (shared by all of a customer's units) should do when only some of their units
    are past the rule: deny_if_any_unit_restricted or allow_if_any_unit_in_good_standing."""

    __tablename__ = "access_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    lockout_days: Mapped[int] = mapped_column(Integer)
    gate_rule: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    approved_by: Mapped[str] = mapped_column(String)
    effective_on: Mapped[date] = mapped_column(Date)
    version: Mapped[str] = mapped_column(String)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkItem(Base):
    """One piece of unfinished work, written by a module that found it (Access first). Repeated observations update
    the open episode; when the condition is gone it closes; if it comes back, a new episode opens and the old one stays.
    Closing needs the closing evidence from the system of record, never a task marked done."""

    __tablename__ = "work_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    kind: Mapped[str] = mapped_column(String)
    subject_type: Mapped[str] = mapped_column(String)          # facility, customer or lease
    subject_id: Mapped[str] = mapped_column(String)            # the FMS id of the subject
    episode_key: Mapped[str] = mapped_column(String)
    problem: Mapped[str] = mapped_column(Text)
    evidence_source: Mapped[str] = mapped_column(String)
    closing_evidence: Mapped[str] = mapped_column(Text)
    verification: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="open")
    who_acts: Mapped[str] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed: Mapped[dict] = mapped_column(JSON, default=dict)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # the FMS task asking a person to act: none, intent (recorded before the call), created, or outcome_unknown
    task_state: Mapped[str] = mapped_column(String, default="none")
    task_marker: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_status: Mapped[str | None] = mapped_column(String, nullable=True)
    task_intent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("run_id", "facility_id", "kind", "subject_type", "subject_id", "episode_key"),)


class InboundMessage(Base):
    """A text sent to a facility's number, kept as Cubby's SMS Received webhook reported it, with who the desk concluded
    sent it. It is stored before it is matched, so a text is never lost because the FMS could not be asked; a pending
    one is matched on a later pass. Cubby may deliver one text twice, so the carrier's message id is unique here."""

    __tablename__ = "inbound_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    # none until the text can be filed: the receiving number serves several facilities and none is set, or the desk has not
    # read the named facility yet. The text is kept either way.
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    # the facility the webhook named, kept while the desk has no row for it
    facility_provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_message_id: Mapped[str] = mapped_column(String, unique=True)
    phone: Mapped[str] = mapped_column(String)
    # the sender's whole number, normalized (+1 and ten digits for a US or Canadian number), never a suffix of it
    phone_key: Mapped[str] = mapped_column(String, index=True)
    to_number: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    stored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # pending (not checked yet), matched, unmatched, ambiguous (two tenants share the number: a person decides) or unrouted
    match_state: Mapped[str] = mapped_column(String, default="pending")
    match_note: Mapped[str | None] = mapped_column(String, nullable=True)
    # who Cubby named in the webhook, until a check says otherwise; the FMS stays the owner of both
    customer_provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # a text a visitor typed to play the tenant belongs to that visitor and is seen, counted and answered only inside their own
    # space. A text that came from the webhook has none, and nothing a visitor does can reach it.
    visitor: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


class OutboundMessage(Base):
    """A text the desk means to send, written before Cubby is called, like a payment command. Cubby's send takes no
    idempotency key and no look-up afterwards, so there is one call: `sent` only when Cubby answered 200, and a
    call that got no usable answer stays outcome_unknown for a person to check, never sent again by the desk."""

    __tablename__ = "outbound_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    # the tenant's text this one answers; none for a text the desk starts
    inbound_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_messages.id"), nullable=True)
    # Cubby texts a customer's number on file, so the recipient is a customer, never a number someone typed
    customer_provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    # who wrote the words: a person at the desk, or later Claude
    author: Mapped[str] = mapped_column(String)
    # the screen's own id for this reply, so a double click is one text; Cubby never sees it
    request_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    # intent, sent, blocked (the rule said no; Cubby was not called), rejected (Cubby refused; nothing went out) or
    # outcome_unknown (no usable answer)
    state: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # the visitor whose text this answers, copied from it, so a reply is never read outside the space its text lives in
    visitor: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


def record_kind(record_id: str, text: str | None) -> str:
    """What kind of record this is, in the words a screen uses, from the id the desk gave it."""
    head = record_id.split(":")[0]
    if head == "workitem":
        return "Outside listing" if (text or "").startswith("Outside listing") else "Sale"
    return {"lease": "Account", "payment": "Payment attempt", "observed": "Payment at the FMS", "lien": "Lien file", "access": "Access", "texting": "Texting",
            "text": "Reply" if record_id.startswith("text:out:") else "Text"}.get(head, "Record")


class AgentRun(Base):
    """One investigation of one inbound text: what Claude was shown, which records it looked at, the packet it submitted, what
    the desk checked itself, and what the model calls cost. The packet is Claude's work; `verified` is the desk's own check of
    it against its records, and the two are kept apart on purpose."""

    __tablename__ = "agent_runs"
    # the database, not a read before the insert, keeps two requests from running the model twice on the same text: one run
    # at a time, and a text's runs are numbered, so a second run without `again` finds the first one's number taken
    __table_args__ = (Index("one_running_run_per_text", "inbound_id", unique=True, sqlite_where=_RUNNING_SQL, postgresql_where=_RUNNING_SQL),
                      UniqueConstraint("inbound_id", "generation"))
    id: Mapped[int] = mapped_column(primary_key=True)
    inbound_id: Mapped[int] = mapped_column(ForeignKey("inbound_messages.id"))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    # a preview visitor, as a short hash of where the request came from; none for an operator. It counts a visitor's cases for the day.
    visitor: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    model: Mapped[str] = mapped_column(String)
    effort: Mapped[str] = mapped_column(String)
    # running, done (a packet the desk accepted), needs_person (no accepted packet, or one the records do not support) or failed
    status: Mapped[str] = mapped_column(String, default="running")
    stop_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # written at the start of every model turn: a run that has not beaten for a while was cut off, however long ago it started
    beat_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # every model turn and every lookup it made, with the record ids the lookup returned
    trace: Mapped[list] = mapped_column(JSON, default=list)
    # record id -> the sentence the records say, for every record a lookup returned in this run
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    packet: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verified: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # what the desk did for this run after it checked the packet: for now, the staff review it asked the FMS for or linked. The model
    # only asks; the desk decides, acts and reads the result back from the FMS.
    actions: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def reading(self) -> dict:
        """What staff are shown of a run, in every place it appears: the case notes under their label, the records they rest on as
        the desk holds them, each with its kind, and the desk's own check apart from both."""
        packet = self.packet or {}
        facts = self.facts or {}
        return {"reminder": packet.get("reminder"),
                "notes": {"label": "Case notes", "intent": packet.get("intent"), "summary": plain(packet.get("summary")),
                          "reason": plain(packet.get("reason")), "needs_person": packet.get("needs_person")},
                "records": [{"id": i, "kind": record_kind(i, facts.get(i)), "text": facts.get(i) if i.startswith("text:") else plain(facts.get(i))} for i in packet.get("records", [])],
                "verified": self.verified, "actions": [v for v in (action_view(a) for a in self.actions or []) if v is not None]}


class AgentRequest(Base):
    """A request id, the text it was first used for, and the run it was answered with. The screen picks a request id for each
    request and keeps it for the retries, so a retry gets the same run, and an id that turns up for another text is a mistake
    and is refused. The run is the one the request started, or the one that already existed when the request was answered."""

    __tablename__ = "agent_requests"
    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    inbound_id: Mapped[int] = mapped_column(ForeignKey("inbound_messages.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)


class AccessView(Base):
    """The latest complete access reading for one lease, as the worker took it. Pages read this, never the FMS."""

    __tablename__ = "access_views"
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ReminderCheck(Base):
    """The latest complete reminder check for one past-due lease, as the worker took it: may a collections text go to this tenant, and if not,
    why. Pages and the API read this, never the FMS."""

    __tablename__ = "reminder_checks"
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class FmsObservation(Base):
    """An answer from the FMS that its documentation does not describe, kept once per call, field, kind and value, with how often it came and when.
    The answer itself is never used as if it were documented; a person reviews the record, and it opens again if the same thing arrives after the
    review. It holds no payment, customer or account data."""

    __tablename__ = "fms_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    endpoint: Mapped[str] = mapped_column(String)
    field: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("provider", "endpoint", "field", "kind", "value"),)
