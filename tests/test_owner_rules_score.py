"""The owner-rule runner: python -m core.evaluate --owner-rules reads the graded owner sentences several times and writes one public
result. A scripted compiler stands in for the model."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from core import evaluate, rules
from core.rules import Proposal, expected_typed
from core.seed import WAIVER_TYPED


# the runner --------------------------------------------------------------------------------------------------------------
def compiler(wrong=None, failed=(), typed=None):
    """Stands in for the model call: every graded sentence comes out as its label, except the {run number: {sentence id: outcome}} given.
    A call listed in `failed` as (run number, sentence id) did not run: the answer is "cannot" and the call says why. `typed` gives the
    rule fields to return for a (run number, sentence id)."""
    doc = json.loads((Path(rules.__file__).parent / "graded" / "owner_rules.json").read_text())["graded"]
    by_text, state = {x["text"]: x for x in doc}, {"calls": 0, "per_run": len(doc)}

    def fake(client, model, effort, sentence, current, budget=None):
        label = by_text[sentence]
        run_no = state["calls"] // state["per_run"]
        state["calls"] += 1
        outcome = (wrong or {}).get(run_no, {}).get(label["id"], label["label"])
        if (run_no, label["id"]) in failed:
            return Proposal("cannot", reason="the sentence could not be read (error: RateLimitError)", call=SimpleNamespace(cents=0.0, outcome="error: RateLimitError"))
        fields = (typed or {}).get((run_no, label["id"])) or expected_typed(label, WAIVER_TYPED) or dict(WAIVER_TYPED)
        return Proposal(outcome, label.get("rule", "waiver"), fields, call=SimpleNamespace(cents=0.02, outcome="ok"))
    fake.doc, fake.state = doc, state
    return fake


def test_the_runner_aggregates_runs_from_fake_proposals_and_names_no_model(monkeypatch):
    probe = compiler()
    refuse = [x for x in probe.doc if x["label"] == "refuse"]
    adopt = [x for x in probe.doc if x["label"] == "adopt"]
    fake = compiler({1: {refuse[0]["id"]: "adopt", adopt[0]["id"]: "cannot"},          # run 2: one unsafe rule offered, one rule not read
                     2: {adopt[0]["id"]: "cannot", adopt[1]["id"]: "cannot"}})         # run 3: two rules not read
    monkeypatch.setattr(rules, "compile_rule", fake)
    out = evaluate.owner_rules(None, 3)
    refused, read = out["rows"]
    assert fake.state["calls"] == 3 * len(fake.doc) and out["runs"] == 3 and out["labels"] == evaluate.rules_hash()
    assert (refused["cases"], refused["right"], refused["right_low"], refused["right_high"], refused["same_every_run"], refused["unsafe"]) == (10, 9.7, 9, 10, 9, 1)
    assert (read["cases"], read["right"], read["right_low"], read["right_high"], read["same_every_run"], read["cents_median"]) == (20, 19.0, 18, 20, 18, 0.02)
    assert out["bar_met"] == 2                                                   # run 2 offered an unsafe rule
    assert out["missed"] == [{"text": adopt[0]["text"], "label": "adopt", "got": "cannot"}]      # wrong in two of three runs
    assert "claude" not in json.dumps(out).lower() and "haiku" not in json.dumps(out).lower()


def test_a_reading_that_failed_to_run_is_counted_as_wrong_and_counted_apart(monkeypatch):
    probe = compiler()
    refuse = next(x for x in probe.doc if x["label"] == "refuse")
    monkeypatch.setattr(rules, "compile_rule", compiler(failed={(1, refuse["id"])}))
    out = evaluate.owner_rules(None, 2)
    refused = out["rows"][0]
    assert (out["calls"], out["failed_calls"]) == (2 * len(probe.doc), 1)
    assert (refused["right_low"], refused["right_high"], refused["unsafe"]) == (9, 10, 0)       # it is a miss, never an unsafe rule accepted
    monkeypatch.setattr(rules, "compile_rule", compiler())
    assert evaluate.owner_rules(None, 2)["failed_calls"] == 0


def test_a_failed_call_is_wrong_even_for_a_sentence_labelled_cannot_and_the_bar_follows(monkeypatch):
    probe = compiler()
    cannot = next(x for x in probe.doc if x["label"] == "cannot")
    adopt = [x for x in probe.doc if x["label"] == "adopt"]
    monkeypatch.setattr(rules, "compile_rule", compiler(failed={(0, cannot["id"])}))
    run = rules.grade(None)
    row = next(r for r in run["rows"] if r["id"] == cannot["id"])
    assert row["got"] == "cannot" and row["outcome_call"] != "ok" and row["right"] is False        # "cannot" is the label, but the call never ran
    assert run["right_others"] == run["others"] - 1
    # four more rules not read: 15 of 20 right is under the bar of 16, which the failed call alone decides (16 before it was counted)
    monkeypatch.setattr(rules, "compile_rule", compiler({0: {x["id"]: "cannot" for x in adopt[:4]}}, failed={(0, cannot["id"])}))
    out = evaluate.owner_rules(None, 1)
    assert (out["rows"][1]["right"], out["bar_met"], out["failed_calls"]) == (15.0, 0, 1)
    monkeypatch.setattr(rules, "compile_rule", compiler({0: {x["id"]: "cannot" for x in adopt[:4]}}))
    out = evaluate.owner_rules(None, 1)
    assert (out["rows"][1]["right"], out["bar_met"]) == (16.0, 1)


def test_two_runs_that_adopt_different_wrong_rules_are_not_the_same_answer(monkeypatch):
    probe = compiler()
    adopt = next(x for x in probe.doc if x["label"] == "adopt")
    right = expected_typed(adopt, WAIVER_TYPED)
    monkeypatch.setattr(rules, "compile_rule", compiler(typed={(0, adopt["id"]): {**right, "per_tenant": 5}, (1, adopt["id"]): {**right, "per_tenant": 6}}))
    out = evaluate.owner_rules(None, 2)
    assert out["rows"][1]["right"] == 19.0 and out["rows"][1]["right_low"] == 19         # adopted, but with the wrong fields, both times
    assert out["rows"][1]["same_every_run"] == 19                                         # two different wrong rules are not one answer
    monkeypatch.setattr(rules, "compile_rule", compiler(typed={(0, adopt["id"]): {**right, "per_tenant": 5}, (1, adopt["id"]): {**right, "per_tenant": 5}}))
    assert evaluate.owner_rules(None, 2)["rows"][1]["same_every_run"] == 20              # the same wrong rule twice is
    cannot = next(x for x in probe.doc if x["label"] == "cannot")
    monkeypatch.setattr(rules, "compile_rule", compiler(failed={(1, cannot["id"])}))
    assert evaluate.owner_rules(None, 2)["rows"][1]["same_every_run"] == 19               # a call that failed once is not the same answer


def test_the_command_prints_the_summary_and_writes_the_result_to_out(tmp_path, monkeypatch, capsys):
    """The whole command without a paid call, as a person runs it: the summary on screen and the same numbers in the --out file."""
    monkeypatch.setattr(rules, "compile_rule", compiler())
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda **kw: None))
    out = tmp_path / "owner_rules.json"
    assert evaluate.main(["--owner-rules", "--runs", "2", "--out", str(out)]) == 0
    result = json.loads(out.read_text())
    refused, read = result["rows"]
    assert (result["runs"], result["bar_met"], result["calls"], result["failed_calls"], result["missed"]) == (2, 2, 60, 0, [])
    assert (refused["right"], refused["cases"], refused["unsafe"], read["right"], read["cases"], read["verb"]) == (10.0, 10, 0, 20.0, 20, "right")
    shown = capsys.readouterr().out
    assert "10 of 10 refused" in shown and "20 of 20 right" in shown and "bar met in 2 of 2 runs" in shown
