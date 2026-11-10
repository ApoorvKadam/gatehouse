"""Our agent's reading of tenants' own texts, measured with the real model on a set of labelled synthetic texts.

The labels were written before the first run. None of the texts was used to write the agent's instructions, and the set is not to be tuned on.
Each text is typed by a visitor of its own, as one tenant, so no run sees another's texts. For each text the report keeps what our agent
asked the facility to wait for, what the desk kept, whether a reply was proposed, and the words of the notes.

    python -m desk.evaluate_reading --out docs/reading-evaluation.json      (with the agent's key in the environment, as for the desk)

It costs about 10 cents a run and is not part of the build or the test suite."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .evaluate import start_simulator
from .evaluate_texts import explore, press, visitor, world

SET = Path(__file__).with_name("reading_set.json")
PRONOUNS = re.compile(r"\b(he|she|him|her|hers|his|himself|herself)\b", re.I)
# the parts of a reply that tell a tenant something about the account
FACT_PARTS = ("payment_applied", "balance_as_of", "sale_status", "staff_review")


def read_one(app, item: dict) -> dict:
    """One text, typed by a new visitor and explored: what our agent did with it."""
    row = {"id": item["id"], "group": item["group"], "label": item["label"], "reply_expect": item["reply"], "text": item["text"]}
    client = visitor(app)
    typed = press(client, "omar", item["text"])
    if not isinstance(typed, dict) or "id" not in typed:
        return {**row, "status": "not typed", "error": str(typed)[:120]}
    got = explore(client, typed["id"])
    run = got.json() if got.status_code == 200 else {}
    kept = run.get("reminder") or {}
    answer = (run.get("verified") or {}).get("reminder") or {}
    notes = run.get("notes") or {}
    reply = next((a for a in run.get("actions") or [] if a.get("kind") == "reply"), None)
    summary = notes.get("summary") or ""
    return {**row, "status": run.get("status") or f"http {got.status_code}", "kind": kept.get("kind", "none"),
            "dropped": answer.get("why", "") if answer.get("asked") and not answer.get("accepted") else "", "intent": notes.get("intent"),
            "reply": reply is not None, "reply sent": bool((reply or {}).get("body")),
            "reply with an account fact": any(p.get("kind") in FACT_PARTS for p in (reply or {}).get("parts") or []),
            "summary": summary, "pronouns": PRONOUNS.findall(summary),
            "cost": run.get("cost_usd") or 0.0, "error": (run.get("error") or "")[:120]}


def summarize(rows: list[dict]) -> dict:
    """Every number with its denominator. A text that did not finish is counted apart and left out of the rest."""
    ran = [r for r in rows if r.get("status") == "done"]
    holds = [r for r in ran if r["label"] != "none"]
    plain = [r for r in ran if r["label"] == "none"]
    expect = [r for r in ran if r["reply_expect"] == "expect"]
    wrong = [r for r in ran if r["label"] == "wrong_number"]
    by_group: dict[str, dict] = defaultdict(lambda: {"texts": 0, "right": 0})
    for r in ran:
        g = by_group[r["group"]]
        g["texts"] += 1
        g["right"] += r["kind"] == r["label"]
    return {"texts": len(rows), "finished": len(ran), "not finished": len(rows) - len(ran),
            "hold right": [sum(r["kind"] == r["label"] for r in holds), len(holds)],
            "hold missed": [sum(r["kind"] == "none" for r in holds), len(holds)],
            "hold of another kind": [sum(r["kind"] not in ("none", r["label"]) for r in holds), len(holds)],
            "none right": [sum(r["kind"] == "none" for r in plain), len(plain)],
            "false holds": [sum(r["kind"] != "none" for r in plain), len(plain)],
            "asked but dropped by the desk": [sum(bool(r["dropped"]) for r in ran), len(ran)],
            "reply proposed where the records can answer": [sum(r["reply"] for r in expect), len(expect)],
            "replies sent": [sum(r["reply sent"] for r in ran), len(ran)],
            "replies to a wrong number that carry an account fact": [sum(r["reply with an account fact"] for r in wrong), len(wrong)],
            "notes with a gendered word": [sum(bool(r["pronouns"]) for r in ran), len(ran)],
            "cost usd": round(sum(r.get("cost") or 0 for r in rows), 4), "by group": dict(by_group)}


def evaluate(url: str, only: int | None = None) -> dict:
    import anthropic
    items = json.loads(SET.read_text())["items"][:only]
    app = world(url, anthropic.Anthropic())
    rows = []
    for n, item in enumerate(items, 1):
        row = read_one(app, item)
        rows.append(row)
        print(f"{n:>2}/{len(items)} {item['id']} {item['label']:<15} -> {row.get('kind', '-'):<15} reply={row.get('reply')}  {item['text'][:58]}", flush=True)
    return {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "summary": summarize(rows), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="reading-evaluation.json")
    ap.add_argument("--only", type=int, default=None, help="run only the first N texts")
    args = ap.parse_args(argv)
    url, server, thread = start_simulator()
    try:
        report = evaluate(url, args.only)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1) + "\n")
    for name, value in report["summary"].items():
        if name != "by group":
            print(f"{name}: {value}")
    for group, g in report["summary"]["by group"].items():
        print(f"  {group}: {g['right']} of {g['texts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
