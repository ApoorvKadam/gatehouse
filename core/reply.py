"""The reply a tenant gets, written by code from what the gate recorded. No model writes a word of it: every amount, date and
outcome comes from the records, so the reply can only say what actually happened."""
from __future__ import annotations

from datetime import datetime

from .books import Books
from .evidence import KIND_WORDS, day, money

STAFF = "A team member has your message and will follow up with you directly."
# the gate's checks are written about the tenant; the reply is written to them
YOU = (("the tenant was", "you were"), ("the tenant already had", "you already had"), ("the tenant raises", "you raise"),
       ("the tenant's", "your"))


def you(check: str) -> str:
    for a, b in YOU:
        check = check.replace(a, b)
    return check


def write(books: Books, tenant: str | None, res, now: datetime) -> str:
    if tenant is None:
        return STAFF
    name = books.tenants[tenant].name.split()[0]
    lines = [f"Hi {name},"]
    refused = {d.charge: d.check for d in res.asked if not d.allowed}
    stays = lambda ch, check: f"The {money(ch.cents)} {KIND_WORDS.get(ch.kind, ch.kind)} due {day(ch.due)} stays on the account: {you(check)}."
    for o in res.outcomes:
        c = books.charges.get(o.finding.claim.charge or "") if o.finding.claim.charge else None
        what = f"{money(c.cents)} {KIND_WORDS.get(c.kind, c.kind)}" if c else "charge"
        if c and c.id in refused and o.action == "explain_charge":
            lines.append(stays(c, refused.pop(c.id)))
            continue
        d = o.decision
        if d is None or not d.allowed:
            if o.action != "hand_to_staff":
                lines.append(f"About the {what}: we could not change it here ({d.check if d else 'not checked'}). A team member will look at it.")
            continue
        if o.action == "explain_charge":
            lines.append(o.finding.says + (f" The {what} stays on the account." if c and c.open_cents and "stands" not in o.finding.says else ""))
        elif o.action == "correct_fee_posted_in_error":
            lines.append(f"You are right: the {what} was posted in error and has been removed.")
        elif o.action == "reverse_duplicate":
            lines.append(f"You are right: the {what} was posted twice for the same thing. The second one has been reversed.")
        elif o.action == "waive_fee":
            lines.append(f"The {what} due {day(c.due)} has been waived under the owner's once-a-year rule." if c
                         else "The fee has been waived under the owner's once-a-year rule.")
        elif o.action == "open_dispute":
            dispute = next((x for x in books.disputes.values() if x.charge == (c.id if c else None) and x.outcome is None), None)
            when = day(dispute.review_by) if dispute else "within 10 days"
            held = dispute is not None and dispute.hold is not None
            lines.append(f"Your dispute of the {what} is recorded. The office manager will review it by {when}"
                         + (", and it will not be collected until then." if held else "."))
    for cid, check in refused.items():
        ch = books.charges.get(cid or "")
        lines.append(stays(ch, check) if ch else f"About a fee you asked about: {you(check)}.")
    if res.handed:
        lines.append(STAFF)
    return "\n".join(lines)
