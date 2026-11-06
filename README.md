# Past Due Desk

Collections reconciliation for storage FMS.

Past Due Desk works the delinquent accounts of a self-storage operator against the facility management system (FMS) that holds the truth about them. It takes a payment, a promise or a lien step, checks the FMS until the outcome is certain, and keeps the account in an explicit exception state whenever the two disagree. Nothing is reported as done until the FMS shows it.

It exists because the expensive failures in collections are not missing features. They are a payment that timed out and got charged twice, a "paid" message sent before the money was applied to the lease, a returned payment that never reopened the account, and a lien file that moved toward auction with a notice nobody can prove was delivered.

## What it does

- **Past-due queue.** One worklist per facility: what is owed, what was last attempted, what is blocking the account and who has to act.
- **Verified payments.** A payment is submitted once, with its intent recorded first. The desk reads the FMS's own payment record and the lease ledger before it calls the payment applied. Declines, pending gateway answers, lost responses and later returns each land in a defined state instead of a guess.
- **Exceptions you can act on.** Every account that cannot move forward says why, what evidence or decision is missing, and what changes when it arrives.
- **Lien evidence, not legal answers** (in progress). Notice and advertising steps are tracked against reviewed, versioned jurisdiction rules. Missing proof holds the account. Rules that have not been reviewed fail closed.

## How it is built

- A FastAPI service with a database and a reconciliation worker.
- An FMS simulator behind a real HTTP boundary, modeled on Cubby's published Operator API (integer cents, envelopes, roles, payment statuses where a decline is still a 200 and a pending payment must never be retried). It can decline, hang, return pending, lose a response after applying the money, or reverse a payment later, so the failure paths are exercised, not assumed.
- An adapter layer between the desk and the FMS. Vendor details stay in the adapter; the desk works on a small, vendor-neutral model.
- A scenario runner that plays each failure against the running system and reports what the desk did.

## Status

Under active development. The verified-payment path comes first, then the lien evidence workflow.

## Running it

Coming with the first release: `docker compose up` starts the desk, the worker and the FMS simulator with seeded synthetic facilities.

## Data, rules and limits

- All tenants, facilities and payments are synthetic.
- The FMS simulator follows Cubby's public API documentation. It is a simulation, not an official integration, and implies no endorsement.
- State lien-law reference data comes from the Stitch Storage self-storage lien law dataset (https://stitchstorage.com/self-storage-lien-laws/data), licensed CC BY 4.0. It is stored as published and used as reference material only.
- Past Due Desk does not determine legal compliance, process real payments, send statutory notices or authorize property sales. Jurisdiction-specific rules require independent legal review before any operational use.
