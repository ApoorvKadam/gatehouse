"""A file a tenant sends with a text. Our agent reads it in a call of its own; what it reads is the tenant's claim, and the desk compares it with
the payments the FMS reports. A file never confirms a payment or changes a command. The model is scripted here; the desk, the simulator and
the pages are the real ones."""
from __future__ import annotations

import html
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from desk import intake
from desk.evaluate_texts import Scripted as EvalScripted, world
from desk.investigator import investigate
from desk.messaging import play_tenant
from desk.models import InboundMessage, PaymentCommand, WorkItem

from .conftest import ORLANDO
from .test_conversation import tool
from .test_investigator import Scripted, factory, packet  # noqa: F401
from .test_pages import preview


def receipt_text(s) -> InboundMessage:
    return s.scalar(select(InboundMessage).where(InboundMessage.attachment == "receipt-dana"))


def read(**changes):
    found = {"kind": "card_receipt", "amount_cents": 24000, "card_last4": "4242", "unit": "B-1042", "date_written": "Nov 8, 2026", "carries_instructions": False}
    return tool("report_file", {**found, **changes}, id_="f1")


def then_packet(m: InboundMessage):
    return [tool("submit_case_packet", packet("payment_status", [f"text:in:{m.id}"], True, "a person checks the payment", "Dana sends a receipt."), id_="a1")]


def state(s) -> set:
    """Everything a file must never move: the desk's payment attempts and its open work."""
    return {("command", c.id, c.state, c.provider_payment_id) for c in s.scalars(select(PaymentCommand))} | {
        ("item", w.id, w.state) for w in s.scalars(select(WorkItem))}


def test_the_seeded_receipt_is_read_apart_from_the_text_and_compared_with_the_fms(factory):
    with factory() as s:
        m = receipt_text(s)
        before = state(s)
        model = Scripted([read()], then_packet(m))
        run, _ = investigate(s, model, m, "req-file-1")
        reading, text = model.calls
        # the file's call has the image and none of the text; the text's call has no image, so a file cannot change how a text is read
        assert reading["tools"][0]["name"] == "report_file" and "Here is the receipt" not in json.dumps(reading["messages"])
        assert '"image"' in json.dumps(reading["messages"]) and '"image"' not in json.dumps(text["messages"], default=str)
        assert run.file["verdict"] == "matches" and run.file["claims"]["amount_cents"] == 24000
        assert "The receipt says $240.00 paid, on unit B-1042" in run.file["lines"][0]
        assert "confirms nothing on its own" in run.file["lines"][1]
        assert state(s) == before                       # a matching receipt settles nothing
        assert run.status == "done" and run.turns == 2


@pytest.mark.parametrize("changes, verdict, words", [
    ({"amount_cents": 15000}, "no_match", "No payment the FMS reports for this tenant is $150.00"),
    ({"unit": "C-207", "amount_cents": 18500}, "not_theirs", "Unit C-207 is not one of this tenant's units"),
    ({"kind": "other", "amount_cents": None, "unit": None, "card_last4": None, "date_written": None}, "not_a_receipt", "It is not a payment receipt"),
])
def test_a_receipt_that_does_not_match_is_said_plainly_and_moves_nothing(factory, changes, verdict, words):
    with factory() as s:
        m = receipt_text(s)
        before = state(s)
        run, _ = investigate(s, Scripted([read(**changes)], then_packet(m)), m, f"req-file-{verdict}")
        assert run.file["verdict"] == verdict and words in " ".join(run.file["lines"])
        assert state(s) == before


def test_an_instruction_printed_on_a_file_is_named_and_not_followed(factory):
    with factory() as s:
        m = receipt_text(s)
        before = state(s)
        run, _ = investigate(s, Scripted([read(carries_instructions=True)], then_packet(m)), m, "req-file-note")
        assert run.file["lines"][-1] == "The file has text addressed to whoever reads it. Gatehouse takes no instruction from a file."
        assert state(s) == before


def test_a_file_the_model_does_not_report_is_left_to_a_person_and_the_text_is_still_read(factory):
    with factory() as s:
        m = receipt_text(s)
        run, _ = investigate(s, Scripted(("end_turn", []), then_packet(m)), m, "req-file-unread")
        assert run.file["verdict"] == "unread" and "A person looks at it" in run.file["lines"][0]
        assert run.status == "done"


def test_a_reading_with_the_wrong_shape_keeps_nothing_it_cannot_trust():
    found = intake.claims({"kind": "card_receipt", "amount_cents": True, "card_last4": "77", "unit": "F-610; DROP", "date_written": "x" * 40,
                           "carries_instructions": "yes"})
    assert found == {"kind": "card_receipt", "amount_cents": None, "card_last4": None, "unit": None, "date_written": None, "carries_instructions": False}


def test_a_visitor_attaches_only_a_sample(factory, adapter):
    with factory() as s:
        with pytest.raises(ValueError, match="sample files"):
            play_tenant(s, adapter, "omar", "Here is my receipt", "v1", "req-play-bad", {ORLANDO}, attachment="../../etc/passwd")
        row = play_tenant(s, adapter, "omar", "Here is my receipt", "v1", "req-play-ok", {ORLANDO}, attachment="receipt-omar")
        assert row.attachment == "receipt-omar" and row.visitor == "v1"


def test_messages_shows_the_file_and_its_reading(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/inbox").text
        assert "A card receipt for $240.00, sent with the text" in page and 'src="/static/samples/receipt-dana.png"' in page
        assert visitor.get("/static/samples/receipt-dana.png").headers["content-type"] == "image/png"
        app = visitor.app
        with app.state.session_factory() as s:
            m = receipt_text(s)
            investigate(s, Scripted([read()], then_packet(m)), m, "req-file-page")
        page = visitor.get(f"/inbox?text={m.id}").text
        assert "The file they sent" in page and "The receipt says $240.00 paid" in page


def test_a_visitor_playing_a_tenant_is_offered_the_samples_and_nothing_else(fms_url, sim):
    with TestClient(world(fms_url, EvalScripted())) as visitor:
        page = html.unescape(visitor.get("/inbox").text)
        assert all(label in page for label in intake.SAMPLES.values()) and '<option value="">No file</option>' in page
        assert '<input type="file"' not in page
