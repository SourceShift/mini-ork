# Work intake: tickets, mandates, and the admission gate

Intake is how a ticket becomes work: a poll (`mini-ork intake once`) or a signed
webhook (`POST /api/v1/intake/{source}`) reads a ticket from a source declared in
`config/pm.yaml` and, if the source's trigger rules select it, starts or steers a
run.

This page covers the two questions that used to have no answer:

- **Why did (or didn't) this ticket start a run?**
- **Who allowed it to?**

## The three answers, in order

```
ticket ──▶ trigger rules ──▶ admission policies ──▶ thread + run
           (config/pm.yaml)   (mini_ork/policies)
             │                  │
             └── refused ───────┴── denied ──▶ mandate row, no run
```

1. **Rules** (`trigger_label`, `trigger_assignee`, `trigger_states`) select the
   ticket. All are opt-in and ANDed; a source with none configured starts
   nothing. `trigger_recipe` is not a rule — it picks the recipe a selected
   ticket runs.
2. **Policies** — anything registered under the `intake_admission` event — get
   the last word. `DENY` and `REQUIRE_APPROVAL` stop the arrival; a policy that
   raises counts as `DENY`, because starting work nobody authorized costs money
   and a machine. With no policy registered, every selected ticket is allowed.
3. **Dispatch** opens or steers the thread and launches the run.

## The mandate

Every arrival writes one `intake_events` row, and that row carries a JSON
mandate (`mini_ork/intake/mandate.py`):

| field | meaning |
|---|---|
| `actor` | the source id (`github`) the run descends from |
| `source` | `intake` (poll) or `intake-webhook` |
| `scope_ref` | the ticket uid |
| `rule_ids` | the trigger rules in force, as `key:value` — `[]` means none were configured |
| `policy_version` | `tv-<hash>` over the source's trigger settings only |
| `strength` | `granted`, `refused` (the rules do not select it now) or `denied` (a policy said no) |
| `detail` | the refusal reason, the policy's reason, or the dispatch detail |
| `decided_by` / `decision_id` | the policy that decided, and its `policy_decisions` row |
| `thread_id`, `at` | where it landed, and when |

```
$ mini-ork threads show github-acme-app-12
## Intake
acme/app#12@2026-10-11T09:00:00Z  launched  (intake)
  detail       acme/app#12 → github-acme-app-12
  mandate      granted by github via intake
  rules        trigger_label:agent, trigger_recipe:code-fix
  ruleset      tv-1f2ab3c4d5e6
  decided by   allow_agent (pd-9c1d2e3f4a5b)
```

`--json` carries the same rows under `intake`.

The mandate is a **record**, never an input: nothing reads it to decide whether
to run. It exists so an operator can tell "no rule matched" from "the poller
never ran", and so a denial can be followed into its policy decision.

## The rules are re-read on every arrival

`dispatch_issue` re-runs the trigger check against the ticket the source serves
*now*, before choosing launch, continue or steer. The poll's mirror is a cache;
this is the authoritative check. It is also what makes the mandate live rather
than historical: if the label was removed, the next arrival is `refused` and the
thread is neither continued nor steered — a run does not keep receiving work
under an authority nobody grants any more.

The expensive path is unchanged: the poll still filters the mirror first, so an
unmatching board costs one sync and zero dispatches. The re-check only runs for
a ticket that already passed the mirror filter.

## Refusals are promoted, dispatches are final

A refused arrival is recorded — that is what stops silence from being ambiguous.
Its ledger action is `refused` (a rule did not match, or a policy said no), and
its mandate says which of the two and why.

But a refusal is a judgement about the ticket's *current* state, and some
sources do not move `updated_at` when a label is added. So when the same
delivery id arrives again and now dispatches, the refused row is **promoted in
place** (`record_intake_event`) instead of being folded into the refusal. Once a
row says `launched` / `continued` / `steered`, a replay folds into it and is
reported as `duplicate` — a completed action is final.

## Running it

Simple tier (one machine, no server):

```bash
mini-ork intake once --dry-run     # what the rules would select
mini-ork intake once               # sync, dispatch, report
mini-ork threads show <thread-id>  # the mandate behind a run
```

Secure tier (shared deployment): set `MO_INTAKE_WEBHOOKS=1` on the serving
process, keep the webhook secret in sops/OpenBao/`command` (a vault key that
resolves from `env` or a plain file is refused with 503), and keep the token in
the same store. The trigger rules and the admission policies apply identically.

## Writing an admission policy

```python
from mini_ork.policies.engine import register_policy

def only_labelled_by_the_bot(event, config=None):
    if event["type"] != "intake_admission":
        return None  # abstain
    data = event["data"]
    if data["provider"] == "github" and "trigger_label:agent" not in data["rule_ids"]:
        return {
            "result": "DENY",
            "reason": "unlabelled tickets are never auto-dispatched from github",
            "policy_name": "only_labelled_by_the_bot",
        }
    return None

register_policy("only_labelled_by_the_bot", only_labelled_by_the_bot)
```

The event's `data` carries `provider`, `issue_uid`, `title`, `source`, `recipe`
and `rule_ids`. Returning `None` abstains; the first non-`None` response wins.
Register before intake runs — `mini-ork init` is the natural place to keep the
import.
