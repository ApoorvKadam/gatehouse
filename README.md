# Past Due Desk

Collections reconciliation for storage FMS.

Past Due Desk works the delinquent accounts of a self-storage operator against the facility management system (FMS) that holds the truth about them. It takes a payment, a promise or a lien step, checks the FMS until the outcome is known, and keeps the account in an explicit exception state whenever the two disagree. Nothing is reported as done until the FMS shows it.

It exists because the expensive failures in collections are not missing features. They are a payment that timed out and got charged twice, a "paid" message sent before the money was applied to the lease, a returned payment that never reopened the account, and a lien file that moved toward auction with a notice nobody can prove was delivered.

## What it does

- **Past-due queue.** One worklist per facility: what is owed, what was last attempted, what is blocking the account and who has to act.
- **Payments the tenant actually agreed to.** After a fresh identity check, the desk builds a proposal from the FMS balance and the tenant's saved cards, and a payment can only be taken by confirming one, with the name of the person who heard the tenant's yes and the tenant's words; anything but a clear yes is refused. Confirming it twice returns the same payment.
- **One charge, confirmed by the ledger.** One open payment per unit, enforced by the database, and no new charge while any payment on the lease is still pending at the FMS, whoever took it. The intent is written before the FMS is called. A payment counts as applied only when money lands on the charges that were open when the tenant agreed and no other payment on the lease could have put it there. Cubby's ledger does not say which payment paid which charge, so when another payment is in motion the desk does not guess: a person decides.
- **Every outcome has a state.** Declines (an HTTP 200 at Cubby), pending gateway answers, lost responses, FMS failures, late application, refunds, disputes and returns each land in a defined state. A pending payment is never retried. A lost response gets a suggested FMS payment, never an automatic match. What the desk cannot establish goes to staff, who need their own token and record what they found (naming a payment says which one an attempt was; confirming it posted takes their statement that they checked the ledger); "not charged" is refused while the FMS shows the payment pending or succeeded, or while a possible match is unaccounted for. The unit stays held until then.
- **Tenant conversation.** A tenant can check and pay a past-due balance by message. Claude Haiku handles the conversation but has no tool that charges a card: it can propose an amount, and the card is charged only when the tenant's very next message is a plain yes to that proposal. The model never writes anything the tenant reads: it chooses an action (verify, read the balance, propose a payment, check its status, explain a charge, ask to clarify, hand off) and the desk sends its own sentence, built from the FMS ledger and the payment record. Text the model writes anyway is logged and not sent. Identity comes from what the caller types, lasts 15 minutes and is never renewed by the model; a conversation answers only the sender who started it. Three wrong identity answers, hardship, military service, liens and anything legal go to a person.
- **Lien evidence, not legal answers.** For a Florida account, operators record the notice, how it was delivered, the posting, each newspaper publication and the legal checks (military disclosure and service, bankruptcy, vehicle or watercraft, dispute, secured party), each with its document. The desk evaluates the file against rule version FL-r1 and lists every open item with its basis: a section of Fla. Stat. 83.801-83.809, or an operator policy the desk applies on top of the statute. The best a file can reach is "Review packet assembled": listed evidence checks completed; sale authorization is outside this desk. New York is reference only and every New York case goes to counsel; other states have no reviewed rule version.

## How it is built

- A FastAPI service with a database and a reconciliation worker.
- An FMS simulator behind a real HTTP boundary, modeled on Cubby's published Operator API: integer cents, envelopes, key roles and PII scope, payment statuses where a decline is still a 200 and a pending payment must never be retried. It can decline, refuse, fail, return pending, apply the money late, lose a response after charging, sit behind a failing gateway, or reverse a payment later, so the failure paths are exercised, not assumed.
- An adapter layer between the desk and the FMS. Vendor details stay in the adapter; the desk works on a small, vendor-neutral model.
- A scenario runner that plays each failure against the running system and reports what the desk did and what the tenant was told.

## Running it

```bash
cp .env.example .env            # set DESK_OPERATOR_TOKEN to a long random string
docker compose up --build       # desk on :8000, FMS simulator on :8100, reconciliation worker
```

There are no migrations yet: after pulling schema changes, start from fresh volumes (`docker compose down -v`).

Every desk route expects `X-Desk-Token: <DESK_OPERATOR_TOKEN>`; resolving a payment held for staff also needs `X-Staff-Token: <DESK_STAFF_TOKEN>`. Taking a payment at the front desk:

```bash
H="X-Desk-Token: $TOKEN"; J="Content-Type: application/json"
curl -X POST -H "$H" localhost:8000/api/facilities/fac_NSorlando01/sync
curl -H "$H" localhost:8000/api/facilities/fac_NSorlando01/queue
curl -X POST -H "$H" -H "$J" localhost:8000/api/cases/1/identity \
  -d '{"last_name": "Whitfield", "phone_last4": "0142", "unit_name": "B-1042"}'
curl -X POST -H "$H" -H "$J" localhost:8000/api/cases/1/proposals \
  -d '{"identity_check_id": "<from the identity check>", "amount": 24000}'
curl -X POST -H "$H" -H "$J" localhost:8000/api/cases/1/proposals/<proposal_id>/confirm \
  -d '{"confirmed_by": "J. Ortiz", "tenant_answer": "Yes, go ahead"}'
```

The conversation endpoint needs `ANTHROPIC_API_KEY` in `.env` (Claude Haiku 5.5 by default, `DESK_CHAT_MODEL` to change it). Each model call's tokens and estimated cost are logged; a whole conversation costs about a tenth of a cent.

```bash
curl -X POST -H "$H" -H "$J" localhost:8000/api/cases/1/chat \
  -d '{"sender": "+14075550142", "text": "Hi, how much do I owe on my unit?"}'
```

To watch the desk handle each FMS failure, run the scenarios against the simulator:

```bash
python -m desk.scenarios --fms http://localhost:8100
```

```
PASS  Response lost after charging      FMS: charges and applies, answers too late
                                        desk: NEEDS_STAFF with the FMS payment suggested; staff confirmed it: CONFIRMED; 1 charge
                                        tenant hears: Your payment of $240.00 has been applied to your account.
PASS  FMS fails mid-request             FMS: HTTP 500, nothing recorded
                                        desk: NEEDS_STAFF, new attempts blocked until staff cleared it
                                        tenant hears: We checked your $240.00 payment with our records: it was not charged.
PASS  Counter payment at the same time  FMS: ours applies late; a cash payment lands first
                                        desk: AWAITING_APPLICATION (the ledger cannot say whose money is whose), NEEDS_STAFF, staff checked the ledger: CONFIRMED
                                        tenant hears: Your payment of $240.00 has been applied to your account.
...
12 of 12 scenarios behaved as they should.
```

Tests run against the simulator over real HTTP: `pip install -e ".[dev,chat]" && pytest`. One test calls the real model and is skipped unless `DESK_LIVE_MODEL=1` is set.

## Lien files (Florida, rule version FL-r1)

```bash
curl -X POST -H "$H" -H "$J" localhost:8000/api/cases/1/lien/evidence -d '{"kind": "mail_deposit", "entered_by": "J. Ortiz",
  "document_name": "certificate-of-mailing.pdf.txt", "document_text": "PS Form 3817 ...",
  "facts": {"notice_id": 12, "deposited_on": "2026-10-02", "certificate_of_mailing": true, "postage_prepaid": true,
            "address": "88 Pine St, Orlando FL", "address_basis": "rental agreement"}}'
curl -H "$H" localhost:8000/api/cases/1/lien
```

How dates work: every date is a calendar date at the facility, in the time zone the FMS reports for it (no default). A stated payment deadline stays open through its whole local day, and the next step can start the day after. With delivery on November 6, the earliest deadline is November 20, the first publication can run November 21, the second on November 28, and the sale threshold under this policy is December 6.

What the statute says and what is policy:

| Check | Basis |
|---|---|
| Delivery in person, by e-mail, or by first-class mail with a certificate of mailing; mail is presumed delivered on deposit | 83.806(1), (3) |
| Unconfirmed e-mail needs the first-class fallback before any sale | 83.806(1) |
| Notice content: itemized claim, property description, demand of at least 14 days after delivery, sale statement, owner contact | 83.806(2) |
| Conspicuous posting at the facility or on the unit | 83.806(1) |
| Advertisement once a week for two consecutive weeks after the demand period, with its required content; sale at least 15 days after the first publication | 83.806(4) |
| Rental agreement discloses uniformed-services membership; vehicles and watercraft; secured parties | 83.808(2), 83.806(10), 83.805 |
| The newspaper is one of general circulation in the facility's area, with how that was established | 83.806(4) |
| The itemized claim adds up and equals the FMS ledger snapshot the notice was built from, taken before delivery | 83.806(2)(a), compared as policy |
| Posting dated the day of delivery; publications exactly seven days apart; an e-mail confirmed only by a later reply is dated by the reply; unconfirmed e-mail counts from the fallback mailing; every legal check recorded as yes, no or unknown, and anything but a clean no goes to counsel; any payment after delivery, or any change to the claimed charges since the snapshot (a waiver, a return, a new amount), reopens review; a payment on the delivery day counts as after it | policy |
| Every record keeps its document (the notice, the certificate, the confirmation, the acknowledgment, the posting photo, the publisher's affidavit); delivery, posting and publication count only for the notice record they name, so a corrected notice starts over | policy |

Each evaluation keeps the exact facts it read, including each document's hash and the rule version, and can be replayed by that version's evaluator to the same result (`GET /api/lien/evaluations/{id}/replay`). Evidence is append-only in the database itself; a correction supersedes the record it fixes, once.

Not covered: sending notices, scheduling or conducting sales, commercial reasonableness, lien priority, proceeds and surplus notices, the no-newspaper posting route, vehicle towing, and any reading of Cubby's delinquency exemptions as legal clearance.

## Limits

- One deployment serves one operator: its own database, FMS key and operator token. The database records which FMS account it belongs to and the desk refuses to start against another.
- Conversations live in the API process; a restart ends them (proposals and payments are in the database). Identity checks are serialized per case within one process. Running several API processes needs both moved into the database.
- Card processing fees are not modeled. Where an FMS deducts a fee from the charged amount, the ledger shows less than the charge and the payment goes to staff instead of confirming.
- Ledger evidence: an outside payment that succeeded more than a day before a desk payment is taken to be on the ledger already. Cubby exposes no payment-to-charge allocation, so a shorter rule would send every payment to staff and a longer one cannot be proven either; it is a stated policy (`RECENT_OUTSIDE_PAYMENT`).
- Settled payments and paid cases are re-read for 180 days (card disputes run to about 120 days, ACH returns to 60); a later return is caught the next time the lease is read.

## Data, rules and limits of use

- All tenants, facilities and payments are synthetic.
- The FMS simulator follows Cubby's public API documentation. It is a simulation, not an official integration, and implies no endorsement.
- State lien-law reference data comes from the Stitch Storage self-storage lien law dataset (https://stitchstorage.com/self-storage-lien-laws/data), licensed CC BY 4.0. It is stored as published and used as reference material only.
- Past Due Desk does not determine legal compliance, process real payments, send statutory notices or authorize property sales. Jurisdiction-specific rules require independent legal review before any operational use.
