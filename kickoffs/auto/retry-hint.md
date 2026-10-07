# Retry hints: say whether a failed run can be retried, and what must change first

## Why

When a run fails, the user cannot tell whether it can be retried cheaply, retried after fixing
something, or needs a new code change. Example: researcher run `run-le-1791359434-64879-1`
(recipe `acq-wave5-rsi`) — every LLM node succeeded; only the `live_smoke` verifier failed with
`status: UNVERIFIED` ("cmd reports an unreachable surface"), because the backend lacked an env
var. The implementer's log even says "local BE must be restarted with `ONBOARDING_DEMO_BOOK_PATH`
exported — supervisor precondition". The user saw only "failed", and the loop paid ~$12 for a new
run. A parallel run (`recover-verify`) is adding `mini-ork recover --strategy verify`,
`--ack-change` and `--force`; this run computes the hint and surfaces it.

## Files in scope

- `mini_ork/recovery/retry_hint.py` (new)
- `mini_ork/cli/board_cmd.py` (new `retry` verb only)
- `mini_ork/ide_pages/run.py` (a Retry section on failed runs only)
- `tests/unit/test_retry_hint.py` (new — put the board and run-page tests here too)

Do NOT edit `mini_ork/recovery/planner.py`, `plan.py`, `mini_ork/cli/execute*.py`, `mini_ork/acp/*`.

## 1. `retry_hint.py`

`compute(home, run_id) -> dict | None` (pure read) and
`load_or_compute(home, run_id, *, write=True) -> dict | None` (returns `<run_dir>/retry-hint.json`
when it is newer than every file it was computed from, else computes and, when `write`, saves it).
Returns `None` for runs that are still working or that succeeded.

Shape (a contract the recover run reads — keep these keys exactly):

```json
{"version": 1, "run_id": "…", "failed_node": "live_smoke", "retryable": true,
 "strategy": "verify|resume|retry|resume-cost|none", "from_node": "live_smoke",
 "needs_change": null | {"kind": "environment|credentials|budget|code|unknown",
                         "summary": "…", "detail": "…", "evidence": "<abs path or ''>"},
 "notes": ["…"], "command": "mini-ork recover <run> --strategy verify --ack-change",
 "computed_at": "<iso>"}
```

Classification, first match wins (failed node = the first node in workflow topo order that failed;
workflow nodes use the `name:` key; resolve the recipe with
`mini_ork.recipes_catalog.find_recipe(recipe, home)` so project overlay recipes work; recipe from
`run_profile.json`):

1. `.cost-pause` sentinel in the run dir → `strategy: resume-cost`, retryable, needs_change
   `budget` ("Paused at the cost cap"), command `mini-ork resume <run>`.
2. Failed node is a verifier whose result JSON (`verifier_<stem>.json`, stem = basename of the
   node's `verifier_ref` without `.py`; fall back to the node name with `_`→`-`) has
   `status == "UNVERIFIED"` (or `pass` false with a reason containing "unreachable" / "not set" /
   "missing" / "precondition"), while every verifier before it passed → `strategy: verify`,
   retryable, needs_change `environment`: summary "The <node> check could not reach something it
   needs", detail = the verifier `reason` (first 800 chars), evidence = its `evidence_path`.
   `notes` = up to 3 lines from `impl-<implementer>.log` / `implementer-summary.json` that mention
   "precondition", "must be restarted", "export", "not set" or "env".
3. Failed node is a verifier with `status` `REFUTED`/`FAIL` or `pass` false (not case 2), or a
   reviewer/judge whose verdict is `needs_revision`/`reject`/`fail` (`review-*.json`
   `verdict` + `reasons`/`notes`) → `retryable: false`, `strategy: none`, needs_change `code`:
   summary "The change was judged wrong — it needs a revision", detail = reasons (first 3) or the
   verifier reason.
4. Failed LLM node with a `node_attempts.failure_class` (or node_end `finish_reason`) meaning
   provider trouble (rate limit, provider_limit, max turns, timeout, transport, 5xx) →
   `strategy: resume`, retryable, `needs_change: null`, unless it is auth (401/403, "credential",
   "api key") → needs_change `credentials`.
5. Anything else → `retryable: false`, `strategy: none`, needs_change `unknown`: summary
   "Failed at <node>; the cause was not classified", detail = last 20 lines of the node's log.

`command` = `mini-ork recover <run> --strategy <strategy>` (+ ` --ack-change` when needs_change is
set and retryable); for `resume-cost` the resume command; `""` when not retryable.
Read state through `mini_ork.web.db.db_for(home)`; a missing table/file never raises.

## 2. `board retry <run> [--ack-change] [--force] [--dry-run]`

- Always returns JSON `{"ok": …, "hint": <hint or null>, …}`.
- `--dry-run` → the hint only; never spawns.
- Not retryable and no `--force` → `ok: false`, `error` = the summary.
- `needs_change` set and no `--ack-change` → `ok: false`, `error` = "needs a change first: <summary>".
- Otherwise spawn the hint's command detached (same spawn shape as
  `mini_ork.acp.commands._spawn`: `start_new_session=True`, log to `<run_dir>/recover-<ts>.log`,
  cwd = the project root, env with `MINI_ORK_HOME`), pass `--ack-change` / `--force` through, return
  `{"ok": true, "pid": …, "log": …, "command": …}`. No hint → `ok: false`, "nothing to retry".

## 3. Run page (`ide_pages/run.py`)

On a failed run only, add a "Retry" section at the top of the overview tab using only section types,
colours and actions already in `mini_ork/ide_pages/spec.py` on this branch:
- retryable, no needs_change → one line "Can resume from <from_node>, reusing finished nodes" and a
  button "↻ Retry" → `cli("board", "retry", run_id, confirm="Retry from <from_node>?")`.
- needs_change → warn colour: "Needs a change before retrying (<kind>): <summary>", the detail
  (monospace, selectable), the notes, an evidence link (path action), and a button
  "I fixed it — retry" → `cli("board", "retry", run_id, "--ack-change", confirm=…)`.
- not retryable → "Can't be resumed: <summary>" + detail; no button.
Use `load_or_compute(home, run_id)`; any error drops the section, never the page.

## Tests (`tests/unit/test_retry_hint.py`, tmp home + run dirs + tmp state.db)

One test per classification case 1-5 (including the `notes` extraction and the overlay recipe
lookup), cache freshness (a newer run-dir file triggers recompute), working/succeeded runs →
`None`, every `board retry` branch with the spawn monkeypatched (never spawn), `--dry-run` never
spawns, and the run page shows the right section for cases 2, 3 and 4 and none for a succeeded run.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_retry_hint.py tests/unit/test_board_cmd.py tests/unit/test_ide_pages_run.py` passes — paste the summary line.
- `ruff check` on the touched files is clean.
- Read-only proof (never write into a real run dir, never spawn), paste both outputs:
  - `python3.11 -c "from pathlib import Path; from mini_ork.recovery.retry_hint import load_or_compute; import json; print(json.dumps(load_or_compute(Path('/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork'), 'run-le-1791359434-64879-1', write=False), indent=1))"`
    → `strategy: verify`, needs_change `environment`, a note quoting the `ONBOARDING_DEMO_BOOK_PATH` precondition.
  - same for `Path('/Volumes/docker-ssd/ps/mini-ork/.mini-ork'), 'ide-kickoff-search-20261007091357'`
    → `retryable: false`, needs_change `code` with the reviewer's reasons.
- Diff touches only files in scope.
