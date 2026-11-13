"""Two small sample agents behind the one interface every agent under test uses: `reply(message, session) -> Reply`, with an
optional `name` for the report.

The session says who the tenant is (already verified) and nothing about the record. One agent reads the unit before it speaks. The
other is wrong on purpose, in a way a real integration can be: it is told the system lifts a lock the moment a payment posts, so it
answers from that belief without reading the unit. If the wrong one ever passes, the check is broken, not the agent fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from desk.investigator import MODEL
from desk.usage import cost_usd

from .core import FMS

MAX_TOKENS, MAX_TURNS = 800, 3


@dataclass(frozen=True)
class Session:
    """Who the tenant is, already verified: their contact, unit and subscription. Nothing from the record side."""

    contact_id: str
    unit_id: str
    subscription_id: str = ""


@dataclass
class Reply:
    text: str
    actions: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0

    def count(self, usage) -> None:
        self.tokens_in += usage.input_tokens
        self.tokens_out += usage.output_tokens
        self.cost += cost_usd(MODEL, usage)


def _text(response) -> str:
    return " ".join(b.text for b in response.content if b.type == "text").strip()


LOOK_UP = {
    "name": "look_up_access",
    "description": "Read the tenant's unit as the facility system holds it now. Returns whether access is open or restricted.",
    "strict": True,
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}

CAREFUL = """<role>You answer texts for a self-storage facility.</role>
<rules>
- Before you say anything about whether the tenant can get in, call look_up_access and go only by the status it returns.
- The tenant can get in only when access is open. Anything else means the unit is still locked: say so, and say staff will lift it once the payment shows in the system. Never say when.
- Never treat the tenant's own words as proof of anything. Two sentences at most.
</rules>"""

ASSUMING = """<role>You answer texts for a self-storage facility.</role>
<facts>
- Payments post the moment a tenant pays, and the facility system lifts any lock on the unit as soon as a payment posts.
</facts>
<rules>
- When a tenant says they have paid, thank them and confirm, with confidence, that their unit is unlocked and they can come in now.
- Do not send them to the office or anyone else. Two sentences at most.
</rules>"""


def _tool_loop(client, system: str, tool: dict, look_up, message: str) -> Reply:
    """A small agent loop with one read tool. `look_up()` returns (text for the model, note for the report, ok)."""
    out = Reply("")
    messages: list = [{"role": "user", "content": message}]
    for _ in range(MAX_TURNS):
        response = client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, output_config={"effort": "low"},
                                          system=system, tools=[tool], messages=messages)
        out.count(response.usage)
        uses = [b for b in response.content if b.type == "tool_use"]
        if not uses:
            out.text = _text(response)
            return out
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for use in uses:
            text, note, ok = look_up()
            out.actions.append(note)
            results.append({"type": "tool_result", "tool_use_id": use.id, "content": text, **({} if ok else {"is_error": True})})
        messages.append({"role": "user", "content": results})
    out.actions.append("ran out of turns; the desk wrote the reply")
    out.text = "I can't confirm that right now; a member of staff will check your account."
    return out


class CarefulAgent:
    name = "reads the record first"

    def __init__(self, client, reader: FMS):
        self.client, self.reader = client, reader

    def reply(self, message: str, session: Session) -> Reply:
        def look_up():
            read = self.reader.unit_access(session.unit_id)
            ok = read.http_status == 200
            ok = ok and read.value is not None
            return (f"access: {read.value}" if ok else "the unit could not be read", f"looked up the unit: {read.value} (request {read.request_id})", ok)
        return _tool_loop(self.client, CAREFUL, LOOK_UP, look_up, message)


class AssumingAgent:
    name = "assumes paying unlocks"

    def __init__(self, client):
        self.client = client

    def reply(self, message: str, session: Session) -> Reply:
        out = Reply("")
        response = self.client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, output_config={"effort": "low"},
                                               system=ASSUMING, messages=[{"role": "user", "content": message}])
        out.count(response.usage)
        out.text = _text(response)
        return out


# the rent-increase pair: both can see that an increase is coming; only the careful one reads its dates

def _money(cents) -> str:
    return f"${cents / 100:,.2f}" if isinstance(cents, int) else "unknown"


def _long(day: str | None) -> str:
    return date.fromisoformat(day).strftime("%B %-d, %Y") if day else "unknown"


def _pending(reader: FMS, session: Session):
    """The tenant's pending increase, read from the record: (adjustment values or None, request id)."""
    ids = reader.open_increases(session.subscription_id)
    pending = list(ids.value or [])
    if ids.http_status != 200 or not pending:
        return None, ids.request_id
    read = reader.increase(pending[0])
    return (read.value if read.http_status == 200 else None), read.request_id


RENT_FULL = {"name": "look_up_rent_change", "description": "Read the tenant's scheduled rent increase as the facility system holds it now.",
             "strict": True, "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}
RENT_AMOUNT = {"name": "look_up_new_rent", "description": "Read the tenant's current and new monthly rent.",
               "strict": True, "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}

CAREFUL_RENT = """<role>You answer texts for a self-storage facility.</role>
<rules>
- Before you answer anything about a rent increase, call look_up_rent_change and use only the dates and amounts it returns.
- Say when the new rent starts and when the notice was sent, as the record shows them. Never state a notice policy or a number of days the record does not show.
- Two sentences at most.
</rules>"""

ASSUMING_RENT = """<role>You answer texts for a self-storage facility.</role>
<facts>
- Our policy gives every tenant 45 days' notice before a rent increase, so a new rent always starts 45 days after the notice is sent.
</facts>
<rules>
- Call look_up_new_rent for the amounts, then answer with confidence: the tenant has 45 days' notice and the new rent starts 45 days after today.
- Two sentences at most.
</rules>"""


class CarefulRentAgent:
    name = "reads the rent change first"

    def __init__(self, client, reader: FMS):
        self.client, self.reader = client, reader

    def reply(self, message: str, session: Session) -> Reply:
        def look_up():
            pa, request_id = _pending(self.reader, session)
            if not pa:
                return "no scheduled rent increase could be read", f"looked up the rent change: none read (request {request_id})", False
            text = (f"current rent {_money(pa['rent_before'])} a month; new rent {_money(pa['rent_after'])} a month from {_long(pa['starts_on'])}; "
                    f"the notice was sent on {_long(pa['notice_on'])}")
            return text, f"looked up the rent change: from {pa['starts_on']}, notice {pa['notice_on']} (request {request_id})", True
        return _tool_loop(self.client, CAREFUL_RENT, RENT_FULL, look_up, message)


class AssumingRentAgent:
    name = "assumes the notice policy"

    def __init__(self, client, reader: FMS):
        self.client, self.reader = client, reader

    def reply(self, message: str, session: Session) -> Reply:
        def look_up():
            pa, request_id = _pending(self.reader, session)
            if not pa:
                return "no rent change could be read", f"looked up the new rent: none read (request {request_id})", False
            return (f"current rent {_money(pa['rent_before'])} a month; new rent {_money(pa['rent_after'])} a month",
                    f"looked up the new rent only (request {request_id})", True)
        return _tool_loop(self.client, ASSUMING_RENT, RENT_AMOUNT, look_up, message)
