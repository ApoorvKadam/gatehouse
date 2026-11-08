"""The evaluation report: it runs every scenario on its own simulator, and the page shows what it found."""
from __future__ import annotations

import json
import re
from datetime import datetime

from fastapi.testclient import TestClient

from desk import evaluate, web
from desk.wording import TROUBLES

from .test_pages import preview


def test_the_evaluation_runs_clean_and_its_wrong_builds_are_caught(tmp_path):
    out = tmp_path / "evaluation.json"
    assert evaluate.main(["--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["ok"] and all(r["passed"] for r in report["rows"]) and len(report["rows"]) >= 26
    assert all(r["count"] == 0 for r in report["red_lines"]) and all(c["caught"] for c in report["controls"])
    assert {r["module"] for r in report["rows"]} == {"Payments", "Access", "Lien sales"}
    # every scenario names what went wrong, from the fixed list the page colours, and every wrong build has the short name its mark shows
    assert all(r["trouble"] in TROUBLES for r in report["rows"])
    assert all(c["short"] and len(c["short"]) <= 26 for c in report["controls"])
    assert {r["trouble"] for r in report["rows"]} == set(TROUBLES)       # no kind is unused, so no legend entry is empty


def _report(**more) -> dict:
    return {"generated_at": "2026-11-07T01:00:00+00:00", "generated": datetime(2026, 11, 7, 1), "ok": True, **more}


def test_the_page_shows_the_report_or_says_there_is_none(fms_url, sim, monkeypatch):
    report = _report(rows=[{"module": "Access", "name": "Paid, still locked", "trouble": "disagree", "fms": "f", "gatehouse": "g", "told": "t", "passed": True}],
                     red_lines=[{"module": "Access", "line": "a physical outcome labeled verified", "count": 0}],
                     controls=[{"name": "A build that closes on a task", "short": "Closes on a task", "broken": "closes", "caught": True}])
    with TestClient(preview(fms_url)) as visitor:
        monkeypatch.setattr(web, "evaluation_report", lambda: report)
        page = visitor.get("/evaluation").text
        assert "Paid, still locked" in page and "1 of 1" in page and "Passed" in page
        assert page.count('<a class="seg') == 3 and page.count('data-tip="') == 3 and "The FMS did" in page and "Table view of every result" in page     # a mark for each result, and the table under them
        assert "Closes on a task" in page and "The records disagree" in page         # the wrong build's name inside its mark, and the kind in the legend
        assert "Every build runs the failures" in visitor.get("/").text
        monkeypatch.setattr(web, "evaluation_report", lambda: None)
        assert "has no evaluation report" in visitor.get("/evaluation").text


def test_the_page_writes_the_state_codes_in_what_gatehouse_did_as_words(fms_url, sim, monkeypatch):
    report = _report(rows=[{"module": "Payments", "name": "Response lost after charging", "trouble": "late", "fms": "charges and applies, answers too late",
                            "gatehouse": "NEEDS_STAFF with the FMS payment suggested; staff confirmed it: CONFIRMED; 1 charge", "told": "t", "passed": True},
                           {"module": "Access", "name": "Paid, still locked", "trouble": "disagree", "fms": "f", "gatehouse": "g", "told": "t", "passed": False}],
                     red_lines=[{"module": "Payments", "line": "a duplicate charge", "count": 0}, {"module": "Access", "line": "an item closed on a task", "count": 2}],
                     controls=[{"name": "A build that resends", "short": "Resends", "broken": "sends again", "caught": False}])
    with TestClient(preview(fms_url)) as visitor:
        monkeypatch.setattr(web, "evaluation_report", lambda: report)
        page = visitor.get("/evaluation").text
        assert "needs staff with the FMS payment suggested; staff confirmed it: confirmed; 1 charge" in page and "NEEDS_STAFF" not in page
        assert page.index("Payments") < page.index("Access")                         # the parts keep the order the report gave them
        assert "1 of 2 behave as they should" in page
        # a failing scenario, a red line above zero and a build that was missed
        assert len(re.findall(r'<a class="seg[^"]*\bfail\b', page)) == 3
        assert "2 found, so the build stops" in page and "Missed by the checks" in page


def test_every_mark_links_to_its_row_and_each_part_is_sorted_by_kind():
    rows = [{"module": "Payments", "name": name, "trouble": kind, "fms": "f", "gatehouse": "g", "told": "t", "passed": True}
            for name, kind in [("a", "disagree"), ("b", "no"), ("c", "normal"), ("d", "late"), ("e", "no")]]
    rows.append({"module": "Access", "name": "f", "trouble": "money", "fms": "f", "gatehouse": "g", "told": "t", "passed": True})
    report = _report(rows=rows, red_lines=[{"module": "Payments", "line": "a duplicate charge", "count": 0}],
                     controls=[{"name": "A build that resends", "short": "Resends", "broken": "sends again", "caught": True}])
    view = web.evaluation_view(report)
    marks = dict(view["scenario_marks"])
    assert [m["key"] for m in marks["Payments"]] == ["normal", "no", "no", "late", "disagree"]      # in the order the legend lists the kinds
    ids = {r["id"] for _, group in view["scenarios"] for r in group} | {l["id"] for _, group in view["red_lines"] for l in group} | {c["id"] for c in view["controls"]}
    every = [m for key in ("scenario_marks", "line_marks", "control_marks") for _, group in view[key] for m in group]
    assert len(every) == 8 and all(m["href"][1:] in ids for m in every)                                 # a mark never points at nothing
    assert [(key, n) for key, _, n in view["kinds"]] == [("normal", 1), ("no", 2), ("late", 1), ("money", 1), ("disagree", 1)]    # a kind with no scenario is not listed
    card = json.loads(marks["Payments"][0]["tip"])
    assert [line[0] for line in card["lines"]] == ["The FMS did", "Gatehouse did", "Who hears what"] and card["kind"] == TROUBLES["normal"]


def test_a_report_without_kinds_still_draws_its_marks():
    report = _report(rows=[{"module": "Access", "name": "Paid, still locked", "fms": "f", "gatehouse": "g", "told": "t", "passed": True}],
                     red_lines=[], controls=[])
    view = web.evaluation_view(report)
    assert view["kinds"] == [] and view["scenario_marks"][0][1][0]["key"] is None
