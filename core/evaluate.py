"""The graded runs: each arm on the hand-written sets, scored against the labels (core/graded/).

Every case starts from the same cloned state (its own scratch stream over the generated year) and is scored on the state it
met. A case is right only when every labelled claim got an acceptable action and none was dropped. Critical errors are counted
apart. Choices (model, effort) are made on the dev items only; the held-out items are run once and reported as paired counts.
The public report names arms, never vendors or models; the per-candidate detail goes to a local file.

    python -m core.evaluate --split dev --arms fixed,read,operator --runs 1 --out local.json"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import replace
from datetime import datetime, timezone

from .case import FIXED, OPERATOR, READ_ONLY, Arm, Result, handle
from .cases import labels_hash, load, resolve_any
from .evidence import Claim
from .seed import NOW, build
from .store import Store

UNKNOWN_PHONE = "+14075559999"
LABELS = Arm("perfect reading, table decides", reader="labels", chooser="table")


def world():
    year = build()
    store = Store()
    with store.write():
        store.append("base", year.led.pending)
    return year, store


def gold_claims(year, item) -> tuple[list[Claim], list[str]]:
    books, leases = year.led.books, (year.cast[item["role"]]["leases"] if item["role"] else [])
    out = []
    for c in item["claims"]:
        ids = resolve_any(books, leases, c["charge"]) if c["charge"] else []
        out.append(Claim(c["kind"], ids[0] if ids else None, c["quote"], c.get("date"), c.get("date_quote"), c.get("cents"), c.get("amount_quote")))
    return out, item["flags"]


def score_case(year, item, res: Result) -> dict:
    """Right only if every labelled claim got an acceptable action; critical errors listed by name."""
    books, leases = year.led.books, (year.cast[item["role"]]["leases"] if item["role"] else [])
    money = {"reverse_duplicate", "correct_fee_posted_in_error", "waive_fee"}
    critical, right = [], True
    if not item["claims"]:
        acts = {o.action for o in res.outcomes} or {"hand_to_staff"}
        right = acts <= set(item["case_act"]) and bool(res.handed)
        critical += [a for a in acts if a in item.get("case_critical", [])]
    for g in item["claims"]:
        ids = set(resolve_any(books, leases, g["charge"])) if g["charge"] else set()
        mine = [o for o in res.outcomes if ids and (o.claim.charge in ids or any(op.charge in ids for op in o.finding.options if op.action == o.action))]
        if not mine:
            ok = "hand_to_staff" in g["act"] and bool(res.handed) and not any(o.action in money and o.decision and o.decision.allowed for o in res.outcomes)
            right &= ok
            if not ok and not res.handed and not res.outcomes:
                critical.append("claim dropped without a hand-over")
            continue
        for o in mine:
            done = o.action if (o.decision is None or o.decision.allowed) else "hand_to_staff"
            right &= done in g["act"]
            if done in g["critical"]:
                critical.append(f"{done} where the rubric forbids it")
    return {"id": item["id"], "right": right, "critical": critical, "handed": bool(res.handed), "cents": round(res.cents, 4),
            "calls": len(res.calls), "actions": sorted(f"{o.action}{'' if not o.decision or o.decision.allowed else ' (refused)'}" for o in res.outcomes)}


def run_operate(year, store, arm: Arm, items: list[dict], runs: int, client=None, tag="") -> list[list[dict]]:
    out = []
    for item in items:
        per = []
        for run in range(runs):
            stream = f"graded:{tag}{arm.name}:{item['id']}:{run}"
            phone = year.cast[item["role"]]["phone"] if item["role"] else UNKNOWN_PHONE
            res = handle(store, stream, f"{item['id']}-{run}", phone, item["text"], arm, NOW, client=client,
                         labelled=gold_claims(year, item) if arm.reader == "labels" else None)
            per.append(score_case(year, item, res))
            store.drop_stream(stream)
        out.append(per)
    return out


def compare_choosers(year, store, items: list[dict], choosers: list[Arm], client=None, reader=None) -> dict:
    """Read each case once, then give exactly the same claims to the table and to every chooser, so differences come from
    choosing alone, not from a second reading. Returns {chooser name: [[score]]}. `reader` is (model, effort); None means
    the labelled claims (the perfect-reading diagnostic)."""
    from .agent import Budget, understand
    from .evidence import charge_list
    out = {a.name: [] for a in choosers}
    for item in items:
        t = year.cast[item["role"]]["tenant"] if item["role"] else None
        if reader and t:
            claims, flags, _dropped, _call = understand(client, reader[0], reader[1], item["text"], charge_list(year.led.books, t, NOW), NOW.date(), Budget())
            read = (claims or [], flags)
        else:
            read = gold_claims(year, item)
        for arm in choosers:
            stream = f"graded:choose:{arm.name}:{item['id']}"
            phone = year.cast[item["role"]]["phone"] if item["role"] else UNKNOWN_PHONE
            res = handle(store, stream, f"{item['id']}-c", phone, item["text"], replace(arm, reader="labels"), NOW, client=client, labelled=read)
            out[arm.name].append([score_case(year, item, res)])
            store.drop_stream(stream)
    return out


def score_reading(year, item, claims: list[Claim] | None) -> dict:
    """Every claim right (kind and charge), dates and amounts never wrong, disputes found."""
    if claims is None:
        return {"id": item["id"], "all_right": False, "wrong_amount": False, "dispute_missed": False, "unread": True}
    books, leases = year.led.books, (year.cast[item["role"]]["leases"] if item["role"] else [])
    want = []
    for g in item["claims"]:
        ids = set(resolve_any(books, leases, g["charge"])) if g["charge"] else set()
        want.append((g["kind"], ids, g.get("cents"), g.get("date")))
    got = list(claims)
    matched = 0
    wrong_amount = False
    for kind, ids, cents, day in want:
        hit = next((c for c in got if c.kind == kind and ((not ids and c.charge is None) or c.charge in ids)), None)
        if hit:
            got.remove(hit)
            matched += 1
            wrong_amount |= hit.cents is not None and cents is not None and hit.cents != cents
    disputes = [w for w, g in zip(want, item["claims"]) if "open_dispute" in g["act"]]
    found = all(any(c.kind in ("never_agreed", "challenges_record") and c.charge in ids for c in claims) for _, ids, _, _ in disputes)
    return {"id": item["id"], "all_right": matched == len(want) and not got, "wrong_amount": wrong_amount, "dispute_missed": not found}


def summarize(name: str, results: list[list[dict]]) -> dict:
    first = [r[0] for r in results]
    cents = [x["cents"] for r in results for x in r]
    stable = sum(1 for r in results if len({(x["right"], tuple(x["actions"])) for x in r}) == 1)
    return {"arm": name, "cases": len(first), "right": sum(x["right"] for x in first), "critical": sum(len(x["critical"]) for x in first),
            "handed": sum(x["handed"] for x in first), "wrong": [x["id"] for x in first if not x["right"]],
            "refused_proposals": sum(1 for x in first for a in x["actions"] if a.endswith("(refused)")),
            "cents_median": round(statistics.median(cents), 4) if cents else 0, "cents_max": round(max(cents), 4) if cents else 0,
            "calls_median": statistics.median([x["calls"] for x in first]) if first else 0, "stable_runs": f"{stable} of {len(results)}",
            "detail": first}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "held", "outside"], default="dev")
    ap.add_argument("--arms", default="fixed,labels")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--read-model", default="claude-haiku-5-5")
    ap.add_argument("--read-effort", default="low")
    ap.add_argument("--op", default="claude-haiku-5-5:low", help="model:effort for the choosing call")
    ap.add_argument("--choosers", default="", help="comma list of model:effort compared on one shared reading")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    year, store = world()
    items = load("outside")["items"] if a.split == "outside" else [i for i in load("cases")["items"] if i["op"] == a.split]
    client = None
    if any(x in a.arms for x in ("read", "operator", "reading", "choosers")):
        import anthropic
        client = anthropic.Anthropic(max_retries=1)
    model, effort = (a.op.split(":") + ["low"])[:2]
    base = {"fixed": FIXED, "labels": LABELS, "read": READ_ONLY, "operator": OPERATOR}
    report = {"ran_at": datetime.now(timezone.utc).isoformat(), "labels": labels_hash(), "split": a.split, "runs": a.runs, "arms": [],
              "local_models": {"read": f"{a.read_model}:{a.read_effort}", "op": a.op}}
    for key in a.arms.split(","):
        if key == "reading":
            # h52 to h60 went through the reader during the dev runs, so the held-out reading count uses h01 to h51 only
            reading_items = load("outside")["items"] if a.split == "outside" else [
                i for i in load("cases")["items"] if i["id"][0] == ("d" if a.split == "dev" else "h") and (a.split == "dev" or int(i["id"][1:]) <= 51)]
            rows = []
            from .agent import Budget, understand
            from .evidence import charge_list
            for it in reading_items:
                t = year.cast[it["role"]]["tenant"] if it["role"] else None
                charges = charge_list(year.led.books, t, NOW) if t else []
                claims, _f, _d, _c = understand(client, a.read_model, a.read_effort, it["text"], charges, NOW.date(), Budget())
                rows.append(score_reading(year, it, claims))
            report["reading"] = {"messages": len(rows), "all_right": sum(r["all_right"] for r in rows),
                                 "wrong_amounts": sum(r["wrong_amount"] for r in rows), "disputes_missed": sum(r["dispute_missed"] for r in rows),
                                 "wrong": [r["id"] for r in rows if not r["all_right"]]}
            continue
        if key == "choosers":
            choosers = [Arm("table", chooser="table")] + [Arm(f"chooser {spec}", chooser="model", op_model=spec.split(":")[0],
                                                               op_effort=(spec.split(":") + ["low"])[1]) for spec in a.choosers.split(",") if spec]
            res = compare_choosers(year, store, items, choosers, client, reader=(a.read_model, a.read_effort))
            for name, rows in res.items():
                report["arms"].append(summarize(f"same reading, {name}", rows))
            continue
        arm = replace(base[key], read_model=a.read_model, read_effort=a.read_effort, op_model=model, op_effort=effort or "low")
        res = run_operate(year, store, arm, items, a.runs, client)
        report["arms"].append(summarize(arm.name, res))
    with open(a.out, "w") as f:
        json.dump(report, f, indent=1, default=str)
    for arm in report["arms"]:
        print(f"{arm['arm']}: {arm['right']} of {arm['cases']} right, {arm['critical']} critical, {arm['handed']} to staff, "
              f"median {arm['cents_median']} cents, wrong {arm['wrong']}")
    if "reading" in report:
        r = report["reading"]
        print(f"reading: {r['all_right']} of {r['messages']} all right, {r['wrong_amounts']} wrong amounts, {r['disputes_missed']} disputes missed, wrong {r['wrong']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
