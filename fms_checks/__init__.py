"""Checks that an AI agent tells a self-storage tenant only what the facility management system's own record supports.

Three parts are kept apart, so a pass cannot come from the checks agreeing with themselves:
- the agent: any agent behind `reply(message, session)`; it gets the tenant's words and who the tenant is, nothing from the record side;
- the truth: fresh reads of the FMS made after the agent answers, each kept with its request id and time;
- the judge: fixed code. A model may only point at the agent's own words for each claim; the verdict compares the claim with the read.

The FMS here is a real one run by someone else (a developer test account), reached through one adapter file, `stora.py`.
"""
