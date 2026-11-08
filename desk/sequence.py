"""The steps one text went through, for the Messages page: the message arriving, what the agent read, what the desk checked, the staff
review, the reply, and where the sale stands afterwards. Every step is written from records the desk already holds, in the order they
happened. Nothing here is timed or made up: a step the text has not reached yet is shown as waiting, and a step that did not happen is not
shown at all. The last two steps are the FMS cancellation and the outside listing. Each is read from its own work item and is done only on its
own evidence; the FMS cancelling the sale does not take the outside listing down."""
from __future__ import annotations

import re
from datetime import datetime

from .sales import AFTER_NOTICE, BALANCE_CHANGED, CANCELLED_BY, LISTED_PAID, OUTSIDE_NOTE, SETTLED_REVIEW, STATUS_WORDS
from .wording import plain

# what the page shows for the steps a text has not reached yet: the key, the icon, the title, and one line on what happens there
AHEAD = (("read", "list-checks", "Our agent reads the case", "It reads the tenant's account, payments and sale status. It changes nothing in the FMS."),
         ("check", "shield-check", "The desk checks the notes", "It checks what it can against its own records."),
         ("review", "users", "Staff review, if a sale is still listed", "The agent may ask for an FMS task. The desk makes it and reads it back."),
         ("reply", "send", "A short reply to the tenant", "The desk writes it from the records it confirmed and sends it once."))
# a number that sent STOP is never texted, so its text shows that instead of a reply
NO_REPLY = ("reply", "hand", "No reply will be sent", "This number asked to stop all texts, so the desk sends nothing to it.")
# what the page says about a sale that is still open in the FMS, by the kind of item the desk holds for it
FMS_OPEN = {
    LISTED_PAID: "The FMS still lists the sale and shows nothing owed. This step is done when it shows the auction and lease cancelled. A completed task does not count.",
    BALANCE_CHANGED: "The balance changed after this was opened, so a person reassesses the sale. The desk does not close it on its own.",
    AFTER_NOTICE: "A payment came after the notice, so a person and counsel decide whether the sale stands. The desk does not close it on its own.",
    SETTLED_REVIEW: "The sale settled, so a person and counsel review its timing against the payments. The desk does not close it on its own.",
}
OUTSIDE_CLOSED = "Confirmed by the outside site's feed"
# how a reply's state reads, and the colour its chip takes
REPLIES = {"sent": ("Sent", "done"), "blocked": ("Blocked", "act"), "rejected": ("Refused", "act"), "outcome_unknown": ("Not confirmed", "wait"),
           "intent": ("Sending", "wait")}


def _step(key: str, icon: str, title: str, line: str, tone: str = "plain", chip: str | None = None, at: datetime | None = None, state: str = "done",
          parts: list | None = None, dropped: list | None = None, bubble: tuple[str, str] | None = None, link: tuple[str, str] | None = None,
          at_label: str = "") -> dict:
    return {"key": key, "icon": icon, "title": title, "line": line, "tone": tone, "chip": chip, "at": at, "at_label": at_label, "state": state,
            "parts": parts or [], "dropped": dropped or [], "bubble": {"side": bubble[0], "text": bubble[1]} if bubble and bubble[1] else None,
            "link": {"href": link[0], "label": link[1]} if link else None}


def _first(text: str | None, limit: int = 220) -> str:
    """The first sentence of a longer text, so a step says one thing and the long text stays under the sequence."""
    text = (text or "").strip()
    found = re.match(r"(.+?[.!?])(\s|$)", text)
    first = found.group(1) if found else text
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + "..."


def _units(match: dict) -> str:
    units = [lease["unit"] for lease in match.get("leases") or [] if lease.get("unit")]
    return "" if not units else f"unit {units[0]}" if len(units) == 1 else "units " + ", ".join(units[:-1]) + f" and {units[-1]}"


def _received(t: dict) -> dict:
    match = t["match"]
    state = match["state"]
    if state == "matched":
        units = _units(match)
        line = f"From {match.get('customer_name') or 'the tenant'}, matched through the FMS" + (f" to {units}." if units else ".")
    elif state == "ambiguous":
        line = "Two tenants share this number. A person decides who it is."
    elif state == "pending":
        line = "The FMS is still being asked which tenant this number belongs to."
    else:
        line = "No tenant was found for this number, so there is nothing for the agent to read."
    tone = {"matched": "done", "ambiguous": "wait", "pending": "wait"}.get(state, "plain")
    if t.get("stopped"):
        return _step("received", "message-square", "Message received", line + " This number asked to stop all texts.", "act", "Texting stopped",
                     t["received_at"], bubble=("in", t["body"]))
    return _step("received", "message-square", "Message received", line, tone, None, t["received_at"], bubble=("in", t["body"]))


def _read(run: dict) -> list[dict]:
    """The agent's reading, the desk's check of it, and what the desk did afterwards, as steps."""
    notes, verified = run["notes"], run.get("verified") or {}
    person = bool(notes.get("needs_person"))
    steps = [_step("read", "list-checks", "Our agent read the case", _first(notes.get("reason") or notes.get("summary")), "act" if person else "done",
                   "A person has to act" if person else "No person needed", run.get("finished_at"))]
    checked, holds = bool(verified.get("checked")), bool(verified.get("sale_listed_nothing_owed"))
    steps.append(_step("check", "shield-check", "The desk checked the notes", plain(verified.get("says")) or "",
                       ("done" if holds else "wait") if checked else "plain",
                       ("The desk confirms the sale" if holds else "The desk does not confirm the sale") if checked else "Not checked by the desk", run.get("finished_at")))
    for action in run.get("actions") or []:
        if action["kind"] == "staff_review":
            steps.append(_step("review", "users", "Staff review", action["text"], action["tone"], action["label"], action.get("at")))
        elif action["kind"] == "reply":
            line = "The desk wrote it from the records below. The agent wrote no word of it." if action["parts"] else action["text"]
            steps.append(_step("reply", "send", "Reply to the tenant", line, action["tone"], action["label"], parts=action["parts"],
                               dropped=action["dropped"], bubble=("out", action.get("body") or "")))
    return steps


def _outcomes(outcome: dict | None) -> list[dict]:
    """The sale in the FMS and the listing on the outside site, each as a step that is done only on its own evidence. An open one is waiting and
    says when the desk last read it; a missing outside listing is not shown, because there is nothing to wait for."""
    if not outcome:
        return []
    sale, outside = outcome["sale"], outcome.get("outside")
    link = (f"/sales/{sale['id']}", "Open the sale")
    if sale["state"] == "closed":
        cancelled = sale["verification"] == CANCELLED_BY
        steps = [_step("fms", "gavel", "FMS lien cancellation", "The FMS shows the auction and lease cancelled." if cancelled else plain(sale["verification"]) or "The item is closed.",
                       "done" if cancelled else "plain", "Cancelled in the FMS" if cancelled else "Closed", sale["closed_at"], link=link)]
    else:
        status = STATUS_WORDS.get(sale.get("status") or "", "")
        line = FMS_OPEN.get(sale["kind"], "A person decides what happens to the sale.")
        if (sale.get("note") or "").startswith("Not freshly verified"):
            line += " " + plain(sale["note"])
        steps = [_step("fms", "gavel", "FMS lien cancellation", line, "wait", "Still listed in the FMS" if status == "listed" else f"In the FMS: {status or 'not cancelled'}",
                       sale["updated_at"], "waiting", link=link, at_label="Last read")]
    if outside is not None:
        link = (f"/sales/{outside['id']}", "Open the listing")
        if outside["state"] == "closed":
            withdrawn = outside["verification"] == OUTSIDE_CLOSED
            steps.append(_step("outside", "globe", "Outside listing", "The outside site's own feed reports the listing withdrawn." if withdrawn else plain(outside["verification"]) or "The item is closed.",
                               "done" if withdrawn else "plain", "Withdrawn on the outside site" if withdrawn else "Closed", outside["closed_at"], link=link))
        else:
            line = OUTSIDE_NOTE + " This step is done when the site's own feed reports the listing withdrawn."
            if (outside.get("note") or "").startswith("Not verified"):
                line += " " + plain(outside["note"])
            where = plain(outside.get("marketplace")) or "the outside site"
            steps.append(_step("outside", "globe", "Outside listing", line, "wait", f"Still listed on {where}", outside["updated_at"], "waiting", link=link, at_label="Last read"))
    return steps


def _placed(steps: list[dict], step: dict) -> None:
    """Put a step with a time where it belongs among the steps that have one, after the message itself; one with no later neighbour goes last."""
    if step["at"] is not None:
        for n, other in enumerate(steps[1:], 1):
            if other["at"] is not None and other["at"] > step["at"]:
                steps.insert(n, step)
                return
    steps.append(step)


def sequence(t: dict, explorable: bool = True) -> list[dict]:
    """The steps for one text of the inbox view, in the order they happened. A text nobody has answered or explored shows the steps that wait,
    unless the agent is switched off on this copy. A reply a person sent is placed by its own time."""
    run, replies = t.get("investigation"), t.get("replies") or []
    steps = [_received(t)]
    if run is None:
        if t["match"]["state"] == "matched" and not replies and explorable:
            ahead = AHEAD[:3] + (NO_REPLY,) if t.get("stopped") else AHEAD
            steps += [_step(key, icon, title, line, state="pending") for key, icon, title, line in ahead]
    elif run["status"] == "failed" or (run["status"] != "running" and not run["notes"].get("summary")):
        steps.append(_step("read", "list-checks", "Our agent could not finish", "No case notes were written, so a person has to read this message.",
                           "act", "No case notes", run.get("finished_at")))
    elif run["status"] != "running":
        steps += _read(run)
    # the reply step of this run, when it is the run that wrote the text. A later run proposes nothing for a message that was answered, so its
    # step carries no text, and the reply an earlier run sent is shown as a step of its own, by its time.
    agent = next((s for s in steps if s["key"] == "reply" and s["bubble"] is not None), None)
    for reply in replies:
        at = reply.get("sent_at") or reply["created_at"]
        if reply["author"] == "agent" and agent is not None:
            agent["at"] = agent["at"] or at
            continue
        chip, tone = REPLIES.get(reply["state"], (reply["state"], "plain"))
        who = "The desk sent it after an earlier exploration." if reply["author"] == "agent" else "A person at the desk sent it."
        line = who if reply["state"] == "sent" else (reply.get("reason") or "It was not sent.")
        _placed(steps, _step("reply", "send", "Reply to the tenant", line, tone, chip, at, parts=reply.get("parts"), dropped=reply.get("dropped"),
                             bubble=("out", reply["body"])))
    return steps + _outcomes(t.get("outcome"))
