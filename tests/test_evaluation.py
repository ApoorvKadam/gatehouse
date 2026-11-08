"""The evaluation report: it runs every scenario on its own simulator, and the page shows what it found."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from desk import evaluate, web

from .test_pages import preview


def test_the_evaluation_runs_clean_and_its_wrong_builds_are_caught(tmp_path):
    out = tmp_path / "evaluation.json"
    assert evaluate.main(["--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["ok"] and all(r["passed"] for r in report["rows"]) and len(report["rows"]) >= 26
    assert all(r["count"] == 0 for r in report["red_lines"]) and all(c["caught"] for c in report["controls"])
    assert {r["module"] for r in report["rows"]} == {"Payments", "Access", "Lien sales"}


def test_the_page_shows_the_report_or_says_there_is_none(fms_url, sim, monkeypatch, tmp_path):
    out = tmp_path / "evaluation.json"
    out.write_text(json.dumps({"generated_at": "2026-11-07T01:00:00+00:00", "ok": True,
                               "rows": [{"module": "Access", "name": "Paid, still locked", "fms": "f", "gatehouse": "g",
                                         "told": "t", "passed": True}],
                               "red_lines": [{"module": "Access", "line": "a physical outcome labeled verified", "count": 0}],
                               "controls": [{"name": "A build that closes on a task", "broken": "closes", "caught": True}]}))
    with TestClient(preview(fms_url)) as visitor:
        monkeypatch.setattr(web, "evaluation_report", lambda: {**json.loads(out.read_text()),
                                                               "generated": __import__("datetime").datetime(2026, 11, 7, 1)})
        page = visitor.get("/evaluation").text
        assert "Paid, still locked" in page and "1 of 1" in page and "Passed" in page
        assert page.count('<i class="seg ') == 3 and "The FMS did" in page and "Table view of every result" in page     # a mark for each result, and the table under them
        assert "Every build runs the failures" in visitor.get("/").text
        monkeypatch.setattr(web, "evaluation_report", lambda: None)
        assert "has no evaluation report" in visitor.get("/evaluation").text


def test_the_page_writes_the_state_codes_in_what_gatehouse_did_as_words(fms_url, sim, monkeypatch):
    report = {"generated_at": "2026-11-07T01:00:00+00:00", "generated": __import__("datetime").datetime(2026, 11, 7, 1), "ok": True,
              "rows": [{"module": "Payments", "name": "Response lost after charging", "fms": "charges and applies, answers too late",
                        "gatehouse": "NEEDS_STAFF with the FMS payment suggested; staff confirmed it: CONFIRMED; 1 charge", "told": "t", "passed": True},
                       {"module": "Access", "name": "Paid, still locked", "fms": "f", "gatehouse": "g", "told": "t", "passed": False}],
              "red_lines": [{"module": "Payments", "line": "a duplicate charge", "count": 0}, {"module": "Access", "line": "an item closed on a task", "count": 2}],
              "controls": [{"name": "A build that resends", "broken": "sends again", "caught": False}]}
    with TestClient(preview(fms_url)) as visitor:
        monkeypatch.setattr(web, "evaluation_report", lambda: report)
        page = visitor.get("/evaluation").text
        assert "needs staff with the FMS payment suggested; staff confirmed it: confirmed; 1 charge" in page and "NEEDS_STAFF" not in page
        assert page.index("Payments") < page.index("Access")                         # the parts keep the order the report gave them
        assert "1 of 2 behave as they should" in page
        assert page.count('class="seg fail"') == 3                                   # a failing scenario, a red line above zero and a build that was missed
