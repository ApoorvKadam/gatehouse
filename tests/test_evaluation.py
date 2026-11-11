"""The evaluation report: it runs every scenario on its own simulator, and the page shows what it found."""
from __future__ import annotations

import json
import re
from datetime import datetime

from fastapi.testclient import TestClient

from desk import evaluate, evaluate_reminders as reminders, evaluate_texts as texts, web
from desk.wording import TROUBLES

from .test_pages import preview


def test_the_evaluation_runs_clean_and_its_wrong_builds_are_caught(tmp_path):
    out = tmp_path / "evaluation.json"
    assert evaluate.main(["--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["ok"] and all(r["passed"] for r in report["rows"]) and len(report["rows"]) >= 53
    assert all(r["count"] == 0 for r in report["red_lines"]) and all(c["caught"] for c in report["controls"])
    # every red line says what it was counted over, and nothing is counted over nothing
    assert all(r["checked"] > 0 and r["of"] for r in report["red_lines"])
    assert {r["module"] for r in report["rows"]} == {"Payments", "Access", "Lien sales", "Texts", "Reminder check", "FMS changes", "Files"}
    # the Reminder check's five red lines are counted over real judged rows, failed readings, attempts, texts and payments, and its two wrong builds are caught
    checks = {l["line"]: l for l in report["red_lines"] if l["module"] == "Reminder check"}
    assert set(checks) == set(reminders.LINES) and all(l["count"] == 0 and l["checked"] >= 2 for l in checks.values())
    assert {"A build that treats a failed reading as clear", "A build that dates a payment by when the desk first looked"} <= {c["name"] for c in report["controls"] if c["caught"]}
    # the FMS changes part: its three red lines are counted over real readings, and both wrong builds are caught
    drift = {l["line"]: l for l in report["red_lines"] if l["module"] == "FMS changes"}
    assert len(drift) == 3 and all(l["count"] == 0 and l["checked"] >= 2 for l in drift.values())
    assert {"Maps a status", "Coerces an amount"} <= {c["short"] for c in report["controls"] if c["caught"]}
    files = {l["line"]: l for l in report["red_lines"] if l["module"] == "Files"}
    assert len(files) == 2 and all(l["count"] == 0 and l["checked"] >= 4 for l in files.values())
    assert {"Receipt as payment", "Quotes the file"} <= {c["short"] for c in report["controls"] if c["caught"]}
    # every scenario, red line and wrong build names its kind of trouble, from the fixed list the page colours, and every wrong build has
    # the short name its mark shows
    assert all(r["trouble"] in TROUBLES for r in report["rows"]) and all(l["trouble"] in TROUBLES for l in report["red_lines"])
    assert all(c["trouble"] in TROUBLES and c["short"] and len(c["short"]) <= 26 for c in report["controls"])
    assert {r["trouble"] for r in report["rows"]} == set(TROUBLES)       # no kind is unused, so no legend entry is empty


def _report(**more) -> dict:
    return {"generated_at": "2026-11-07T01:00:00+00:00", "generated": datetime(2026, 11, 7, 1), "ok": True, **more}


def test_the_page_shows_the_report_or_says_there_is_none(fms_url, sim, monkeypatch):
    report = _report(rows=[{"module": "Access", "name": "Paid, still locked", "trouble": "disagree", "fms": "f", "gatehouse": "g", "told": "t", "passed": True}],
                     red_lines=[{"module": "Access", "line": "a physical outcome labeled verified", "trouble": "disagree", "count": 0, "checked": 9, "of": "passes"}],
                     controls=[{"name": "A build that closes on a task", "short": "Closes on a task", "trouble": "disagree", "broken": "closes", "caught": True}])
    with TestClient(preview(fms_url)) as visitor:
        monkeypatch.setattr(web, "evaluation_report", lambda: report)
        page = visitor.get("/evaluation").text
        assert "Paid, still locked" in page and "1 of 1" in page and "Passed" in page
        assert page.count('<a class="seg') == 3 and page.count('data-tip="') == 3 and "The FMS did" in page and "Table view of every result" in page     # a mark for each result, and the table under them
        assert "Closes on a task" in page and "The records disagree" in page         # the wrong build's name inside its mark, and the kind in the legend
        assert "of 9 passes" in page and "Counted over" in page                       # the red line says what its zero was counted over
        assert "Every build runs the failures" in visitor.get("/").text
        monkeypatch.setattr(web, "evaluation_report", lambda: None)
        assert "has no evaluation report" in visitor.get("/evaluation").text


def test_the_page_writes_the_state_codes_in_what_gatehouse_did_as_words(fms_url, sim, monkeypatch):
    report = _report(rows=[{"module": "Payments", "name": "Response lost after charging", "trouble": "late", "fms": "charges and applies, answers too late",
                            "gatehouse": "NEEDS_STAFF with the FMS payment suggested; staff confirmed it: CONFIRMED; 1 charge", "told": "t", "passed": True},
                           {"module": "Access", "name": "Paid, still locked", "trouble": "disagree", "fms": "f", "gatehouse": "g", "told": "t", "passed": False}],
                     red_lines=[{"module": "Payments", "line": "a duplicate charge", "trouble": "twice", "count": 0, "checked": 12, "of": "scenarios"},
                                {"module": "Access", "line": "an item closed on a task", "trouble": "disagree", "count": 2, "checked": 14, "of": "passes"}],
                     controls=[{"name": "A build that resends", "short": "Resends", "trouble": "late", "broken": "sends again", "caught": False}])
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
    report = _report(rows=rows, red_lines=[{"module": "Payments", "line": line, "trouble": kind, "count": 0, "checked": 3, "of": "passes"}
                                           for line, kind in [("a", "disagree"), ("b", "twice"), ("c", "late")]],
                     controls=[{"name": name, "short": name, "trouble": kind, "broken": "sends again", "caught": True}
                               for name, kind in [("A build", "disagree"), ("B build", "late")]])
    view = web.evaluation_view(report)
    marks, lines, builds = dict(view["scenario_marks"]), dict(view["line_marks"]), dict(view["control_marks"])
    assert [m["key"] for m in marks["Payments"]] == ["normal", "no", "no", "late", "disagree"]      # in the order the legend lists the kinds
    assert [m["key"] for m in lines["Payments"]] == ["late", "twice", "disagree"] and [m["key"] for m in builds["Wrong builds"]] == ["late", "disagree"]
    ids = {r["id"] for _, group in view["scenarios"] for r in group} | {l["id"] for _, group in view["red_lines"] for l in group} | {c["id"] for c in view["controls"]}
    every = [m for key in ("scenario_marks", "line_marks", "control_marks") for _, group in view[key] for m in group]
    assert len(every) == 11 and all(m["href"][1:] in ids for m in every)                                # a mark never points at nothing
    assert [key for key, _ in view["kinds"]] == ["normal", "no", "late", "twice", "money", "disagree"]  # every kind the marks use, in the legend's order
    card = json.loads(marks["Payments"][0]["tip"])
    assert [line[0] for line in card["lines"]] == ["The FMS did", "Gatehouse did", "Who hears what"] and card["kind"] == TROUBLES["normal"]
    assert json.loads(lines["Payments"][0]["tip"])["kind"] == TROUBLES["late"]                          # a red line names the trouble it guards against


def test_a_report_without_kinds_still_draws_its_marks():
    report = _report(rows=[{"module": "Access", "name": "Paid, still locked", "fms": "f", "gatehouse": "g", "told": "t", "passed": True}],
                     red_lines=[], controls=[])
    view = web.evaluation_view(report)
    assert view["kinds"] == [] and view["scenario_marks"][0][1][0]["key"] is None


def test_a_red_line_with_nothing_checked_is_not_tested_and_stops_the_build(fms_url, sim, monkeypatch):
    lines = [{"module": "Access", "line": "a duplicate charge", "trouble": "twice", "count": 0, "checked": 0, "of": "passes"},
             {"module": "Access", "line": "a physical outcome labeled verified", "trouble": "disagree", "count": 0, "checked": 9, "of": "passes"}]
    with TestClient(preview(fms_url)) as visitor:
        monkeypatch.setattr(web, "evaluation_report", lambda: _report(rows=[], red_lines=lines, controls=[]))
        page = visitor.get("/evaluation").text
        assert "not tested" in page and "Not tested, so the build stops" in page and "of 9 passes" in page and "1 of 2 at zero" in page
        assert "1 of 2 at zero" in visitor.get("/").text                                  # the overview counts them the same way
    report = evaluate.Report("now", red_lines=[evaluate.RedLine("Access", "x", "twice", 0, 0, "passes")])
    assert not report.ok                                   # nothing checked proves nothing
    report.red_lines[0].checked = 3
    assert report.ok


def test_the_texts_part_counts_four_red_lines_over_real_attempts_and_catches_four_wrong_builds(tmp_path):
    out = tmp_path / "evaluation.json"
    assert evaluate.main(["--out", str(out)]) == 0
    report = json.loads(out.read_text())
    lines = {l["line"]: l for l in report["red_lines"] if l["module"] == "Texts"}
    assert set(lines) == {texts.OWN, texts.CLAIM, texts.STOP, texts.TWICE}
    assert all(l["count"] == 0 and l["checked"] >= 4 for l in lines.values())            # a zero over a handful of real attempts at least
    wrong = {c["short"] for c in report["controls"] if c["name"] in {"A build that cannot tell visitors apart", "A build that sends what the agent wrote",
                                                                    "A build that ignores a visitor's STOP", "A build that keeps a text for every press"}}
    assert len(wrong) == 4 and all(c["caught"] for c in report["controls"])


def test_the_judge_calls_a_sentence_unsupported_only_when_the_fms_does_not_bear_it_out():
    own, listed = {"B-1042"}, {"B-1042", "E-505"}
    said = "Hi Dana, we got your message from Nov 8 at 6:19 PM. Unit B-1042 is listed for a lien sale on Nov 10. Call the office if anything changes."
    assert texts.unsupported(said, own, listed, set()) == 0
    assert texts.unsupported("Unit E-505 is listed for a lien sale on Nov 10.", own, listed, set()) == 1       # a unit that is not hers
    assert texts.unsupported("Unit B-1042 is listed for a lien sale on Nov 10.", own, {"E-505"}, set()) == 1      # a sale the FMS does not list
    assert texts.unsupported("The auction is canceled.", own, listed, set()) == 1                              # a cancellation it does not show
    assert texts.unsupported("The auction is canceled.", own, listed, {"B-1042"}) == 0
