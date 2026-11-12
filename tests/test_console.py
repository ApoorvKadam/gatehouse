"""The console pages: no sticky class used as a layout class, text with no ids or raw times, and live progress for a run."""
from __future__ import annotations

import html as htmllib
import re

from fastapi.testclient import TestClient

from desk.db import utcnow
from desk.models import AgentRun

from .test_investigator import message_ids, preview
from .test_live import live_app


def visible(page: str) -> str:
    """What a person reads of a page: its text, without scripts or markup."""
    text = re.sub(r"<(script|style).*?</\1>", "", page, flags=re.S)
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))


def test_only_the_header_is_sticky_and_no_layout_class_borrows_its_name(fms_url, sim):
    with TestClient(preview(fms_url, None)) as client:
        today = client.get("/today").text
        pages = ["/today", "/operations", "/inbox"] + sorted(set(re.findall(r'href="(/(?:sales|access)/\d+)"', today)))[:3] \
            + sorted(set(re.findall(r'href="(/accounts/\d+/lien)"', client.get("/").text)))[:1]
        assert len(pages) >= 5
        for path in pages:
            page = client.get(path).text
            assert page.count('class="top"') == 1 and not re.search(r'class="(?:[^"]*\s)?top(?:\s[^"]*)?"', page.replace('<header class="top">', "")), path


def test_today_reads_as_words_with_no_provider_id_state_code_or_raw_time(fms_url, sim):
    with TestClient(preview(fms_url, None)) as client:
        text = visible(client.get("/today").text)
        assert re.search(r"\b(?:payment|auction|listing|task|lease|cust|unit)_[A-Za-z0-9]{4,}", text) is None
        assert re.search(r"\b[A-Z]+_[A-Z_]+\b", text) is None and re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text) is None
        assert "Unit lease_" not in text and "Unit B-1042" in text


def test_the_progress_of_a_run_is_read_from_the_steps_it_really_took(fms_url, sim):
    with TestClient(live_app(fms_url, None)) as client:
        ids = message_ids(client)
        dana = ids["Dana Whitfield"]
        assert client.get(f"/api/inbox/{dana}/progress").json() == {"run": 0, "status": "none", "steps": []}
        with client.app.state.session_factory() as s:
            run = AgentRun(inbound_id=dana, model="m", effort="e", status="running", started_at=utcnow(),
                           trace=[{"turn": 1, "asked": ["get_account", "get_sale_status", "get_payments"],
                                   "calls": [{"tool": "get_account", "input": {}, "records": ["lease:1"], "error": None}]}])
            s.add(run)
            s.commit()
            run_id = run.id
        first = client.get(f"/api/inbox/{dana}/progress").json()
        assert first == {"run": run_id, "status": "running",
                         "steps": ["Reading the tenant's account", "Checking the sale and its outside listing", "Checking payments"]}
        with client.app.state.session_factory() as s:
            row = s.get(AgentRun, run_id)
            row.trace = row.trace + [{"turn": 2, "asked": ["request_staff_review"], "calls": []},
                                     {"turn": 3, "asked": ["submit_case_packet"], "calls": []}]
            s.commit()
        later = client.get(f"/api/inbox/{dana}/progress").json()
        assert later["steps"][-2:] == ["Asking staff to review the sale", "Writing the case notes"]
        assert "workitem" not in str(later) and "lease:" not in str(later)                      # steps are words, never a record id
        assert client.get("/api/inbox/99999/progress").status_code == 404


def test_a_sale_page_writes_its_times_as_a_person_reads_them(fms_url, sim):
    with TestClient(preview(fms_url, None)) as client:
        links = sorted(set(re.findall(r'href="(/sales/\d+)"', client.get("/today").text)))
        assert links
        for link in links:
            text = visible(client.get(link).text)
            assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text) is None, link


def test_the_front_page_names_what_the_fms_shows_in_words(fms_url, sim):
    with TestClient(preview(fms_url, None)) as client:
        text = visible(client.get("/").text)
        assert re.search(r"\b(?:payment|auction|listing|task|lease)_[A-Za-z0-9]{4,}", text) is None
        assert re.search(r"\b[A-Z]+_[A-Z_]+\b|\bSUCCEEDED\b", text) is None
        assert "A payment that may be this attempt" in text
