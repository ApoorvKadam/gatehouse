"""When the FMS changes how it answers, in the evaluation. Each experiment takes a payment on a fresh simulator, then switches on one of the
simulator's drifts (a status renamed, an amount sent as dollars in text, no payment time), lets the desk read, and judges what the desk kept
against the simulator's own record. The drift changes only how the two payment calls write a payment; /sim/truth stays what happened.
It runs inside desk/evaluate.py, on the same simulator."""
from __future__ import annotations

import dataclasses
from datetime import timedelta

import httpx
from sqlalchemy import select

from .adapter import FMSError
from .adapter.cubby import CubbyAdapter
from .evaluate_texts import broken
from .integration import open_observations, save_notes
from .models import ObservedPayment, PaymentCommand
from .payments import Authorization, PaymentRefused, PaymentService
from .scenarios import DANA, DANA_CARD, Run

UNLISTED_READ = "an undocumented payment status kept as a documented one"
AMOUNT_READ = "an amount read although it is not whole cents"
CHARGED_AGAIN = "a second card charge after an answer the desk could not read"
LINES = (UNLISTED_READ, AMOUNT_READ, CHARGED_AGAIN)
RENAMED = "SETTLED"          # what the simulator writes for SUCCEEDED under the status drift


class Judge:
    """[found, checked] for each red line, across the experiments of this part."""

    def __init__(self):
        self.unlisted, self.amount, self.charged = [0, 0], [0, 0], [0, 0]


def drift(run: Run, kind: str) -> None:
    run.sim("/sim/drift", {"kind": kind})


def truth(run: Run) -> list[dict]:
    return httpx.get(f"{run.fms_url}/sim/truth", params={"leaseId": DANA}, timeout=15).json()["data"]["payments"]


def take(session, adapter, case, amount: int = 10000):
    return PaymentService(session, adapter).take_payment(case.id, DANA_CARD, Authorization.new(True, 24000, amount))


def noted(session, adapter, endpoint: str, field: str, kind: str) -> bool:
    """The desk saved the note, and Today would show it."""
    save_notes(session, adapter)
    return any((o.endpoint, o.field, o.kind) == (endpoint, field, kind) for o in open_observations(session))


def judge_statuses(session, judge: Judge) -> None:
    """Every payment status the desk kept while the drift was on must be the one the FMS sent, never a documented status put in its place."""
    for row in session.scalars(select(ObservedPayment)):
        judge.unlisted[1] += 1
        judge.unlisted[0] += row.status != RENAMED
    for command in session.scalars(select(PaymentCommand).where(PaymentCommand.provider_payment_id.is_not(None))):
        judge.unlisted[1] += 1
        judge.unlisted[0] += command.provider_status != RENAMED


def run_experiment(url: str, experiment, judge: Judge) -> dict:
    run = Run(url, timeout=5.0)
    session, adapter, case = run.start()
    try:
        return experiment(run, session, adapter, case, judge)
    finally:
        drift(run, "off")
        adapter.close()
        session.close()


# ------------------------------------------------------------------ the experiments
def exp_renamed_on_search(run: Run, session, adapter, case, judge: Judge) -> dict:
    command = take(session, adapter, case)
    drift(run, "status")
    Run.settle(session, adapter, passes=2)
    judge_statuses(session, judge)
    session.refresh(command)
    return {"kept": command.state == "CONFIRMED" and command.provider_status == RENAMED,
            "noted": noted(session, adapter, "payments search", "status", "unlisted value")}


def exp_renamed_on_submission(run: Run, session, adapter, case, judge: Judge) -> dict:
    drift(run, "status")
    command = take(session, adapter, case)
    unknown = command.state == "OUTCOME_UNKNOWN" and command.provider_payment_id is not None
    command.submitted_at -= timedelta(hours=2)          # the wait for the FMS to settle it is over
    session.commit()
    PaymentService(session, adapter).reconcile(command)
    staff = command.state == "NEEDS_STAFF"
    try:
        take(session, adapter, case)                      # the tenant asks again
        refused = False
    except PaymentRefused:
        refused = True
    Run.settle(session, adapter, passes=2)
    judge_statuses(session, judge)
    cards = sum(1 for p in truth(run) if p["methodType"] != "CASH")
    judge.charged[1] += 2                                  # the first request and the tenant's second one
    judge.charged[0] += max(0, cards - 1)
    return {"held": unknown and staff and refused, "cards": cards, "noted": noted(session, adapter, "make payment", "status", "unlisted value")}


def exp_amount(run: Run, session, adapter, case, judge: Judge) -> dict:
    take(session, adapter, case)
    run.sim("/sim/payments/offline", {"allocations": {DANA: 3000}})     # and one at the counter, so two payments are on the wire
    drift(run, "amount")
    real = truth(run)
    try:
        read = adapter.payments_for_lease(DANA)
    except FMSError:
        read = []
    judge.amount[1] += len(real)
    judge.amount[0] += len(read)        # every amount on the wire was text: any payment handed to the desk was read anyway, whatever its value
    Run.settle(session, adapter, passes=2)
    return {"refused": read == [], "noted": noted(session, adapter, "payments search", "amount", "not whole cents")}


def exp_no_time(run: Run, session, adapter, case, judge: Judge) -> dict:
    drift(run, "created_at")
    command = take(session, adapter, case)
    Run.settle(session, adapter, passes=2)
    row = session.scalar(select(ObservedPayment).where(ObservedPayment.provider_payment_id == command.provider_payment_id))
    return {"kept": command.state == "CONFIRMED" and row is not None and row.paid_at is None,
            "noted": noted(session, adapter, "payments search", "createdAt", "missing")}


# ------------------------------------------------------------------ the wrong builds
def maps_an_unlisted_status(original):
    """The wrong build: a status the documentation does not list is read as the nearest documented one."""
    def payment(self, p, endpoint, listed, amount=0):
        found = original(self, p, endpoint, listed, amount)
        return found if found.status in listed else dataclasses.replace(found, status="SUCCEEDED")
    return payment


def coerces_the_amount(original):
    """The wrong build: an amount sent as text is turned into a number and taken as cents."""
    def payment(self, p, endpoint, listed, amount=0):
        if isinstance(p.get("amount"), str):
            p = {**p, "amount": int(float(p["amount"]))}
        return original(self, p, endpoint, listed, amount)
    return payment


# ------------------------------------------------------------------ the part
def drift_part(url: str):
    """The scenarios, the red lines and the wrong builds of this part, as plain tuples for desk/evaluate.py."""
    judge = Judge()
    search, sent, amount, no_time = (run_experiment(url, exp, judge) for exp in (exp_renamed_on_search, exp_renamed_on_submission, exp_amount, exp_no_time))
    told = "Engineering: one row on Today until a person reviews it"
    rows = [
        ("A status renamed in a search", "disagree", "Payments Search gives a confirmed payment a new status name its documentation does not list",
         "keeps the new name as written; the payment stays confirmed from the earlier reading", told, search["kept"] and search["noted"]),
        ("A status renamed in a payment answer", "late", "Make Payment charges the card, then answers with that new status name",
         "holds it as outcome unknown, hands it to a person after the wait, refuses a second try", told, sent["held"] and sent["cards"] == 1 and sent["noted"]),
        ("An amount sent as dollars in text", "money", "Payments Search writes 10000 cents as \"100.00\"",
         "reads none of that lease's payments rather than round the amount", told, amount["refused"] and amount["noted"]),
        ("A payment with no time", "money", "both payment calls leave out createdAt",
         "keeps the payment with no time of its own, so it cannot end a reminder hold", told, no_time["kept"] and no_time["noted"]),
    ]
    lines = [(UNLISTED_READ, judge.unlisted[0], judge.unlisted[1], "statuses kept"), (AMOUNT_READ, judge.amount[0], judge.amount[1], "payments"),
             (CHARGED_AGAIN, judge.charged[0], judge.charged[1], "payment requests")]
    # each wrong build is the same experiment with one check taken out; its red line must rise
    a, b = Judge(), Judge()
    with broken(CubbyAdapter, "_payment", maps_an_unlisted_status(CubbyAdapter._payment)):
        run_experiment(url, exp_renamed_on_search, a)
    with broken(CubbyAdapter, "_payment", coerces_the_amount(CubbyAdapter._payment)):
        run_experiment(url, exp_amount, b)
    controls = [("A build that reads an undocumented status as the nearest documented one", "Maps a status", "disagree",
                 "reads the new name as succeeded, so a status nobody has checked counts as money that moved", a.unlisted[0] > 0),
                ("A build that turns an amount sent as text into cents", "Coerces an amount", "money",
                 "reads \"100.00\" as 100 cents, a hundredth of the payment", b.amount[0] > 0)]
    return rows, lines, controls
