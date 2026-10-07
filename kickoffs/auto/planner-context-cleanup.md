# Planner context: no unverified gradients, no other sessions' or projects' material; full verify command

## Why (audit of live runs, 2026-10-07)

`mini_ork/cli/plan.py:_inject_context` (~:542-615) builds the planner's learned context. The latest
planner record (`runs/learned-db-default-20261007203031/learned/planner.md`, 8.7 KB) contains:

1. **"Learned graph context (failure-linked)"** (`context_assembler.graph_context_md`): raw
   `gradient_records` signals, with the text "Suggested fix (not verified as applied)". The
   user's standing rule is **only verified learnings in prompts** (raw gradients are already off
   for nodes behind `MO_INJECT_UNVERIFIED`). This block is a second path around that rule.
2. **"ContextNest planner pack — substrate digest (capsule)"** (`role_pack_md("planner", …)` /
   `_contextnest_atoms_md`): cross-session Claude memory matched on a keyword ("Agents"). The
   live block cites a BLOG POST
   (`/Users/admin/ps/blog/…/2026-09-24-same-loop-three-objectives-test-secure-agents.md`),
   unrelated config files and a stale "risk", for a kickoff about MINI_ORK_DB defaults.
3. **"ContextNest attention inbox"** (`mini_ork/cn_client.py:370`): OTHER projects' to-dos handed
   to the planner, e.g. "Start the campaign dev servers (BE on :7833…)", "re-trigger chapter 1",
   "ship the coverage NLI excerpt fix and hard-restart the prod worker". A planner may act on
   these.
4. **"ACTIVE STATE INDEX"** (`orchestration/active_state_index.render_active_state_block`):
   global state whose `pending_goals` are OTHER runs' kickoffs, presented as this planner's
   pending goals.

The SDD evidence review (docs/plans/2026-10-07-sdd-mechanisms-for-mini-ork.md) found generic
context null-to-negative for agent correctness. Context v2 already supplies the run-specific
part: the kickoff contract and the findings on the files in scope. Items 2–4 are injected in
BOTH the v1 and v2 arms (`other_blocks`).

## Files in scope (touch ONLY these)

- `mini_ork/cli/plan.py`: ONLY `_inject_context` (and a small helper it uses)
- `mini_ork/context_v2.py`: ONLY `_constraint_items` (change 4)
- `docs/CONFIG.md`: ONLY two new env-var rows
- `tests/unit/test_planner_context_cleanup.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **The graph-context block** is appended only when `MO_INJECT_UNVERIFIED == "1"`.
2. **Shared-session blocks** (role_pack / contextnest_atoms, contextnest_recent, active_state)
   are built and appended only when `MO_PLANNER_SHARED_CONTEXT == "1"` (default unset = off).
   When off, don't call their producers at all (no ContextNest HTTP calls, no DB scan).
3. **The planner injection record** (`context_v2.write_injection_record(... extra=...)`) gets
   `extra["skipped_blocks"] = {"graph_context": "unverified", "role_pack": "shared_context_off",
   …}` for every block left out, so the ledger shows what was withheld and why.
4. **The full verification command reaches agents** (`context_v2._constraint_items`, :468-470):
   `one = " ".join(cmd.split())[:240]` cuts long commands. In 3 of the last 30 kickoffs the
   command was longer (e.g. `eng-your-code-tab-r2.md`, 428 chars), so every node was told
   "Success is proven by: `<broken half-command>`". Keep the whole command up to 2000 chars.
   Beyond that, cut and append ` …(truncated)` so the cut is visible. Leave the
   `"kind": "context_v2"` source kind as it is: `mini_ork/cli/metrics_context.py:78` depends on it.
5. **`docs/CONFIG.md` rows:**
   - `MO_PLANNER_SHARED_CONTEXT` | unset | "1 adds ContextNest memory, the attention inbox and
     the active-state index to the planner prompt (off: they carry other sessions' and projects'
     items)"
   - `MO_INJECT_UNVERIFIED` | unset | "1 re-enables raw gradients (node learned block, planner
     graph context); default: verified learnings only"

## Tests (`tests/unit/test_planner_context_cleanup.py`; monkeypatch the producers)

- Defaults (no env): the returned prompt contains none of "Learned graph context",
  "ContextNest", "ACTIVE STATE INDEX". The producers for role pack / contextnest / active state
  are NOT called (assert via monkeypatched counters). The injection record's
  `extra.skipped_blocks` names them.
- `MO_INJECT_UNVERIFIED=1` → the graph-context block is present.
- `MO_PLANNER_SHARED_CONTEXT=1` → the role pack / active-state blocks are present (from stub
  producers).
- `context_v2._constraint_items({"verification": [<a 430-char command>]})`: the item text holds
  the whole command. A 2500-char command is cut and ends with `…(truncated)`.
- The verified failure-modes block (patterns) is still injected in the v1 arm.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_planner_context_cleanup.py tests/unit/test_mini_ork_plan_py.py tests/unit/test_context_v2.py tests/unit/test_context_v2_wiring.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/cli/plan.py mini_ork/context_v2.py tests/unit/test_planner_context_cleanup.py` → clean.
- `git diff --stat` touches only the files in scope.
