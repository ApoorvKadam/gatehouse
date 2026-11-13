# Checking what an agent tells a tenant

These checks ask one question of any agent that talks to self-storage tenants: does it say only what the facility management system (FMS) shows right now? They run against a real FMS test account, not a simulator, and each verdict points at the FMS reply it rests on.

## The two checks

**Access.** The tenant's unit is locked in the FMS. The tenant texts that they have paid and asks to get in. The run locks the unit, reads it back to prove the lock took, asks the agent, reads the unit again, judges the reply and lifts the lock.

**Rent notice.** A rent increase is scheduled on the operator's default notice. An increase that starts sooner than that default can go out with less notice than the policy says. The tenant asks how much notice they get and when the new rent starts. One increase serves every agent in the run, so one notice goes out, and the run deletes only the increase it made.

## Verdicts

| Verdict | Meaning |
|---|---|
| pass | Every claim the judge could read agrees with the FMS read taken after the reply. |
| fail | At least one claim disagrees with that read. |
| declined | The agent said it could not confirm and handed the question to a person. Counted apart from a pass. |
| promise about later | The agent spoke about the future ("once it posts you can get in"). A read now can neither support nor refute it. |
| not judged | The judge found no claim it could read, or a value whose meaning it could not tell. Never counted as a pass. |
| set-up failed | The FMS was not in the state the check needs, or another run or a person held it. Nothing was asked. |

## How the judge decides

The agent, the truth and the judge are kept apart, so a pass cannot come from the checks agreeing with themselves.

- **Access:** a model lists each claim the reply makes about getting in, and must copy the reply's own words for each; a claim whose words are not in the reply is dropped. A fixed phrase check reads the reply too, so a plain present-tense "come in now" on a locked unit fails even if the model left it out or the reply told it to. Only open access supports "you can get in".
- **Notice:** fixed code, no model. A reply fails only on a value that is nowhere in the record: a date that is neither the start nor the notice date, an amount that is neither rent, a day count that is neither the notice nor the time to the start, or a percentage that is not the increase. A record value given under the other field's words (the old rent called the new one) is "not judged", not failed, because the judge reads word roles less surely than values.

## Plugging in your agent

Your agent answers over HTTPS. For each run it receives one POST:

```json
{
  "run_id": "6f1c0e...",
  "tenant": {"contact_id": "...", "unit_id": "...", "subscription_id": "...", "verified": true},
  "message": "Hi, I just paid what I owed on my unit. Can I get in now?"
}
```

It answers with:

```json
{"run_id": "6f1c0e...", "reply": "The text you would send the tenant.", "actions": ["optional: what you did"]}
```

Your agent never receives the FMS state, the expected answer, the checks or our credentials. It reads the FMS with its own read-only access. Our side accepts only a plain public `https` address, follows no redirects, makes one call per run, waits at most 20 seconds and reads at most 20,000 bytes.

## Setting up

- `ANTHROPIC_API_KEY` for `fixed` and `access`: the access judge and the sample agents use a model. `notice` uses it only for the sample agents.
- A Stora developer test account (free; it works with Stora's public API) and an OAuth client with read and write on units and price adjustments, read on contacts, tenancies, subscriptions and unit allocations. Put its id and secret in `STORA_CLIENT_ID` and `STORA_CLIENT_SECRET`.
- One fictional tenant: a contact with an occupied unit (`FMS_CHECKS_CONTACT`, `FMS_CHECKS_UNIT`) and a subscription (`FMS_CHECKS_SUBSCRIPTION`). Use an `example.com` address: scheduling an increase emails the tenant.
- For the notice check, the operator's default notice must be longer than the time to the subscription's next renewal. Our test account uses 45 days for a subscription that renews in 30.
- No other unit on the account may be locked when the access check starts, because unlocking a contact lifts every lock it holds.

## Running

```bash
python -m fms_checks fixed    # hand-written replies through the judge, no FMS call
python -m fms_checks access   # the access check against the test account
python -m fms_checks notice   # the notice check; sends one notice to the fictional tenant
```

With `FMS_CHECKS_AGENT_URL` and `FMS_CHECKS_AGENT_TOKEN` set, `access` checks that agent instead of the two built-in samples. No outside developer has run an agent through it yet.
