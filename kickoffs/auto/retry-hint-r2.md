# Retry hints — revision 2 (Opus review of run retry-hint-20261007103745)

WIP commit a3a19a05. Verified OK by Opus: 64 tests pass, ruff clean, scope, spec helpers, the
researcher proof (strategy verify, environment, ONBOARDING_DEMO_BOOK_PATH note). Fix only the list.

## Files in scope

- `mini_ork/recovery/retry_hint.py`, `mini_ork/cli/board_cmd.py`
- `tests/unit/test_retry_hint.py`

## Fixes (exact)

1. **No hint for a run that is running again** (`retry_hint.py:692-711`). `load_or_compute` must
   check the run's CURRENT status (task_runs / run card) before returning a cached hint: anything
   not terminal-failed (`failed`, `rolled_back`, `error`, …) → `None`, and delete nothing. Add the
   status to the cache record and treat a status change as stale. Test: compute a hint, set
   `task_runs.status='executing'` → `load_or_compute(write=True)` returns `None`; a second
   `board retry` on it returns `ok: false` "nothing to retry" and spawns nothing.
2. **Auth detection** (`:47`, `:392`): drop bare `"401"` / `"403"` substrings. Match
   `\b(401|403)\b` only within 30 chars of `status|http|unauthori[sz]ed|forbidden`, plus the
   existing word tokens. Test: an impl log with `tokens_in=14012 cost=$0.21` is NOT credentials;
   `HTTP 401 Unauthorized` IS.
3. **Topo order** (`:98-131`): ignore `retries` and `escalates_to` edges (control flow), and guard
   with a visited set so a cycle never recurses. Test: `recipes/prompt-graph-loop/workflow.yaml`
   (has `edge_type: retries`) → `compute()` on a failed run of that recipe returns a hint, no
   exception.
4. **REFUTED sibling is a code change.** When any verifier (before or after the failed one) is
   REFUTED/FAIL with `pass` false, the result is case 3 `code` with THAT verifier's reason as the
   detail — never case 5 with an empty detail. Fix the "earlier" test fixture so a REFUTED
   verifier sits BEFORE the UNVERIFIED one (→ code) and a test where every earlier verifier passed
   (→ environment).
5. **Reviewer reasons** (`_extract_review_detail`, `:274`): parse `review-*.json` and join the first 3
   `reasons` (or `notes`) entries as plain text, one per line; when the reviewer said
   `needs_revision`, that is case 3 even if a verifier also failed (prefer the reviewer). Proof 2
   below must show the reviewer's reasons.
6. **Spawn like `/recover`** (`board_cmd.py:490-505`): use
   `[sys.executable, str(_mini_ork_root() / "bin" / "mini-ork"), …]`, `MINI_ORK_ROOT` set,
   `MINI_ORK_VENV_ACTIVE` popped, exactly as `mini_ork/acp/commands.py:681-694`. For
   `resume-cost` hints spawn `mini-ork resume <run>` with no `--force` / `--ack-change`. `--force`
   alone must not add `--ack-change`; the hint `command` string must not embed `--ack-change`
   (the verb adds it only when the user passed it).
7. Cache deps also include `node_attempts` rows for the run (max `ended_at`) and `run_profile.json`.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_retry_hint.py tests/unit/test_board_cmd.py tests/unit/test_ide_pages_run.py` → 0 failed. Paste it.
- `uvx ruff check` on the touched files → clean.
- Read-only proofs (write=False, never spawn), paste both JSONs:
  researcher `run-le-1791359434-64879-1` → `verify`, `environment`, the ONBOARDING_DEMO_BOOK_PATH note;
  `/Volumes/docker-ssd/ps/mini-ork/.mini-ork` `ide-kickoff-search-20261007091357` → `retryable:
  false`, `code`, detail = the reviewer's first reasons.
- Diff touches only files in scope.
