# Lane preflight (lane-repair B) — classify "unknown lane" failures and let the retry hint claim them

Sibling of `kickoffs/auto/lane-repair-hint.md` (A: quota/auth dead lanes). This one covers lanes
the effective providers registry does not know at all. Natural home if free:
worktree `recover-retries-edges` (its HEAD is already "retries edges are control flow").

## Why (live evidence, 2026-10-08)

Run `run-orchestrator-scaffold-20261008124718` (framework-edit self-edit: new
`mini_ork/orchestration/runner_orchestrator.py` + `mini_ork/cli/execute.py` wiring) died at the
implementer in 15 ms and every layer lost the cause — the exact failure shape kickoff (A) fixed
for quota, now on the registry side:

1. **The row.** `llm_calls` id 9454: `actor=codex_lens provider=anthropic model_id=sonnet
   status=failed duration_ms=15 error_category='unknown' retryable=0 error_message="lane
   preflight failed: unknown lane: 'sonnet'"`. The launcher had pointed `MINI_ORK_PROVIDERS` at
   `.mini-ork/runs/_scratch-learn-deepseek-20261007/providers.yaml` — a shadow registry that
   REPLACES (not merges) the shipped one and dropped the `sonnet` lane. The default coding tail
   `MO_FALLBACK_CODING="minimax,codex,sonnet"` (`mini_ork/dispatch/routing.py:45`) names lanes the
   registry lacked, preflight rejected `sonnet`, and 4 downstream nodes were blocked.
2. **`classify_error` cannot name it.** `mini_ork/dispatch/llm_dispatch.py:33-65`: the `config`
   branch matches `no providers\.yaml entry` but nothing matches `lane preflight failed` /
   `unknown lane`, so the row lands in `unknown`.
3. **The retry hint declines.** `_case_lane_unavailable`
   (`mini_ork/recovery/retry_hint.py:1031-1039`) claims only rows with `error_category` in
   `("quota", "auth")` or legacy NULL-category rows; a classified-but-`unknown` row matches
   neither filter, the case returns `None`, and `compute` ends at `strategy='none'` ("the cause
   was not classified"). The operator had to recover with `--force --ack-change` — the gate that
   exists to make revival honest refused because nothing owned this cause.
4. **It recurs.** `llm_calls` ids 7566/7574 (child run `child-1790402799-12666`) carry the same
   `"lane preflight failed: unknown lane: 'not-a-lane'"` with NULL category. Second occurrence in
   the wild.

## Files in scope

- `mini_ork/dispatch/llm_dispatch.py` — `classify_error` only
- `mini_ork/recovery/retry_hint.py` — `_case_lane_unavailable` only
- `mini_ork/recovery/retry_notify.py` — `fix_steps` lane branch only
- `tests/unit/test_lane_preflight_hint.py` (new); existing `tests/unit/test_retry_hint.py` /
  `tests/unit/test_retry_notify.py` only if an assertion there must change

Do NOT modify any other file. Do NOT touch the revived run's launcher env or `/tmp`
registries — this is the classifier/hint gap only.

## Changes (exact)

1. **`classify_error`: new category `lane_preflight`.** Text matching
   `lane preflight failed|unknown lane` (case-insensitive, as the function already lowercases)
   returns `"lane_preflight"`. Place the check after the rc-network shortcut and before the
   `config` branch. `error_retryable` is NOT extended — a preflight failure never heals by
   retrying, only by fixing the registry, so `retryable` stays `"0"`. All existing categories and
   their precedence are otherwise unchanged (regression-pinned by tests).
2. **`_case_lane_unavailable` claims `lane_preflight` rows.** Extend the candidate filters: a row
   whose `error_category == "lane_preflight"` is claimable exactly like a `quota`/`auth` row
   (same alias/node resolution, `error_kind = "lane_preflight"`). Also treat a legacy NULL-category
   row whose `error_message` matches the preflight wording as `lane_preflight` — the row itself
   carries the words, so no `live.jsonl` scan is needed for this kind. Keep `kind: "lane"` in
   `needs_change` (reuses the inbox/notify/command paths) with `error_kind` distinguishing it.
   `strategy: "resume"`, `from_node`: the unresolved node (here `implementer`).
   `summary` for this kind: `f"lane '{lane}' is missing from the effective providers registry
   (check MINI_ORK_PROVIDERS — a shadow registry replaces, not merges, the shipped one)"`.
   Suggestions via the existing `lane_suggest.suggest` remain valid: switching the alias to a lane
   the registry DOES know is a correct fix, so `command` stays
   `f"mini-ork recover {run_id} --lane {alias}={suggestions[0]['lane']}"` when a suggestion exists.
3. **`fix_steps` for `error_kind == "lane_preflight"`.** Line 1 unchanged (detail carries the
   preflight text). Line 2 with suggestions: unchanged switch line. Line 2 without suggestions
   becomes: `f"Restore '{lane}' in the effective providers registry (is MINI_ORK_PROVIDERS
   pointing at a shadow registry that dropped it?), then: mini-ork recover {run_id}"`. Line 3
   (nothing-changed note) applies by the existing rule.

## Tests (`tests/unit/test_lane_preflight_hint.py`, temp home/DB, no network)

- `classify_error` on the exact live text → `lane_preflight`; `error_retryable("lane_preflight")
  == "0"`; regressions: `no providers.yaml entry` text → `config`; 401 → `auth`; MiniMax quota
  text → `quota`.
- `compute` on a seeded run copying the live shape (implementer row: `codex_lens`/`sonnet`,
  category `lane_preflight`; `static_check_verifier`/`test_verifier` skipped as blocked children)
  → `kind == "lane"`, `error_kind == "lane_preflight"`, `failed_node == "implementer"`,
  `retryable is True`, `strategy == "resume"`, and the registry summary above; `command` names
  `--lane codex_lens=<suggestion>`.
- Legacy: NULL-category row with `"lane preflight failed: unknown lane: 'not-a-lane'"` in
  `error_message` → same hint (mirrors llm_calls ids 7566/7574).
- `fix_steps` both variants: with a suggestion (switch line) and without (registry-restore line);
  3 deterministic lines each.
- Negative: a classified `capacity` row on a workflow alias still declines the case (today's
  behaviour, pinned).

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_lane_preflight_hint.py tests/unit/test_retry_hint.py tests/unit/test_retry_notify.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/dispatch/llm_dispatch.py mini_ork/recovery/retry_hint.py
  mini_ork/recovery/retry_notify.py tests/unit/test_lane_preflight_hint.py` → clean.
- Read-only proof on the live run (after it finishes):
  `retry_hint.load_or_compute(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"),
  "run-orchestrator-scaffold-20261008124718", write=False)` and `retry_notify.fix_steps(hint)` —
  must name `sonnet`/`codex_lens` and the registry cause from llm_calls id 9454, not `?`.
  Paste both.
- `git diff --stat` touches only the files in scope.
