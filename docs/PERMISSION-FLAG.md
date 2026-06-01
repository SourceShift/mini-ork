# Permission-flag discipline — failure signature + canonical fix

## TL;DR

**Every direct `claude --print` / `claude -p` invocation in this repo
MUST carry one of:**
- `--permission-mode bypassPermissions` (preferred — explicit)
- `--dangerously-skip-permissions` (legacy, equivalent)

Without the flag, backgrounded workers silently block on permission
prompts that have no TTY to answer. Worker exits `rc=0` with **zero
file edits**. Dispatcher reports PARTIAL or success. Downstream tracks
abort. Hours lost to silent failure.

## The failure signature

When you see this combination, you're hitting the no-flag bug:

| Symptom | Where to look |
|---|---|
| Worker log line `Awaiting permission for file writes` | claude stderr, dispatcher log |
| Worker exit `rc=0` (success!) | dispatcher status file |
| **Zero file edits** in the worktree | `git status --porcelain` shows nothing |
| Dispatcher reports PARTIAL or completes silently | status JSON / orchestrator log |

The four together = no-flag bug. The worker reached claude, claude
asked for permission, no TTY answered, claude bailed politely with
rc=0, dispatcher thought the worker just had nothing to do.

## The fix (three flavors, in order of preference)

### Flavor 1: route through `llm_dispatch()` (canonical)

`lib/llm-dispatch.sh` is the single load-bearing dispatcher. Lines 106
and 119 carry `--permission-mode bypassPermissions`; the daily cost
circuit breaker is also baked in (per D-2 from refactor-audit synthesis
2026-05-31). Any caller routing through this is safe.

```bash
source "$MINI_ORK_ROOT/lib/llm-dispatch.sh"
llm_dispatch --task-class my_task --node-type researcher \
             --prompt-text "$prompt"
```

### Flavor 2: use the `mo_claude_print()` wrapper

For dispatchers that need direct claude access but want the flag
discipline (and cache-flags + max-turns defaults), source
`lib/lane-helpers.sh` and call the wrapper:

```bash
source "$MINI_ORK_ROOT/lib/lane-helpers.sh"
mo_claude_print "$prompt" --output-format stream-json
```

The wrapper exports `--permission-mode bypassPermissions` + cache
flags + `--max-turns 40` automatically. Migration target for any
existing direct `claude --print` caller.

### Flavor 3: pass the flag inline (last resort)

If you must invoke claude directly (e.g. you're a one-off script that
shouldn't depend on lib/), pass the flag inline:

```bash
claude \
  --print \
  --permission-mode bypassPermissions \
  --output-format text \
  --max-turns 40 \
  "$prompt"
```

## Lint enforcement

`bin/mo-check-claude-invocations` scans `lib/` + `bin/` for direct
claude invocations missing the flag. Exit 0 = clean, exit 1 =
violations. Wire into CI as advisory (continue-on-error: true) to
surface drift without blocking PRs:

```yaml
- name: Lint claude permission flags
  continue-on-error: true
  run: bash bin/mo-check-claude-invocations
```

When you copy-paste a dispatcher pattern from this repo into a
downstream repo (per [feedback_mini_orch_fixes_mirror_to_upstream](https://github.com/SourceShift/mini-ork/blob/main/lib/llm-dispatch.sh)),
run the lint there too.

## Class-of-bug context

This is the same shape as D-2 from
[`docs/refactor/SCALABILITY-AUDIT.md`](refactor/SCALABILITY-AUDIT.md):
4 direct `claude -p` callers bypass `llm_dispatch()`, which means they
**also** bypass the daily cost circuit breaker (`MO_DAILY_BUDGET_USD`).
Permission-flag drift is the same fix shape — both are "direct callers
bypass the dispatcher's safety wrapper". The canonical fix for both is
**route through `llm_dispatch()` OR `mo_claude_print()`**.

## When NOT to use these flags

If you're writing an INTERACTIVE script (TTY available, user is
sitting at the terminal answering prompts), you want the default
permission prompts on. The flag is for non-interactive / backgrounded
contexts. Don't blindly apply it to user-facing CLIs.

## See also

- `lib/llm-dispatch.sh:106,119` — canonical permission-mode usage
- `lib/lane-helpers.sh` — `mo_claude_print()` wrapper definition
- `bin/_worker-launcher.sh:344,357` — `--dangerously-skip-permissions`
  used in the legacy worker path
- `bin/mo-check-claude-invocations` — lint script
- `docs/refactor/SCALABILITY-AUDIT.md` D-2 — same fix class for cost
  circuit breaker
