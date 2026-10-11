# Zed S2 — the fleet view: `/runs` as a filterable table, `/status <run>` as a run card

Depends on S1 merged (`mini_ork/acp/task_state.py`). Serial with the other Zed slices.

## Goal

When more work is dispatched than anyone can watch, status has to come to the user. Make
`/runs` the fleet table — every run with its state mark, what it is, where it is, how long,
what it cost, how much it changed — with filters standing in for tabs, and make
`/status <run>` a run card that answers "what happened and why" without leaving Zed.

## Files in scope

- `mini_ork/acp/fleet.py` — NEW: rows + card data and their markdown.
- `mini_ork/acp/commands.py` — `/runs` and `/status` use it; `/help` text.
- `tests/unit/test_acp_fleet.py` — NEW.
- `tests/unit/test_acp_commands.py` — update the `/runs` and `/status` tests.

No other file changes.

## Mechanism (exact spec)

### `fleet.py`

```python
@dataclass(frozen=True)
class FleetRow:
    run_id: str; title: str; recipe: str; state: str; mark: str
    step: str; started_at: int | None; ended_at: int | None
    cost_usd: float; added: int; removed: int

def fleet_rows(home: Path, *, state: str = "all", recipe: str | None = None,
               limit: int = 20) -> tuple[list[FleetRow], dict[str, int]]   # rows, counts per state
def render_fleet(rows, counts, *, state: str, now: int) -> str
def run_card(home: Path, run_id: str) -> dict | None
def render_card(card: dict, *, now: int, serve_url: str | None) -> str
```

- `fleet_rows`: candidate runs from `mini_ork.acp.history.list_runs(home, limit=200)`
  (newest first). For each, `state`/`mark` from `task_state.run_mark(status, run_dir)` (cheap);
  only for rows that will be SHOWN (after filtering, first `limit`) read the snapshot
  (`history.read_snapshot`) to compute the precise `task_state` (state, step) and, for done
  rows, added/removed — from the diff cache only (`<run_dir>/acp-diffs.json`; no git calls),
  else 0/0. `counts` counts every candidate by its cheap state
  (`working`/`needs_you`/`done`/`failed`). `state` filter: `all|working|needs-you|done|failed`
  (accept `needs_you` too); `recipe` filter exact id. `started_at` = task_run `created_at`;
  `ended_at` = `updated_at` for done/failed rows, else None. Cost = task_run `cost_usd`.
- `render_fleet`: first line the tabs as counts —
  `**All** 41 · Working 2 · Needs you 1 · Done 35 · Failed 3` with the active filter in bold;
  then a table `| | run | recipe | step | time | cost | change |`: mark; title (≤ 48 chars)
  + the run id in backticks on the same cell; step (working/needs-you rows) or "—"; time =
  elapsed for working rows (`3m12s`, `1h04m`), duration for finished rows, plus age for
  finished rows older than a day (`2d ago`); cost `$0.41`; change `+12 −3` or "—". Empty →
  "No runs match." Last line: "Filter: `/runs working` · `/runs needs-you` · `/runs done` ·
  `/runs failed` · `/runs recipe:<id>` · details: `/status <run id>`".
- `run_card`: `None` for an unknown run. Otherwise a dict with: title, recipe, status,
  `task_state` (state, detail, step), times (created/updated), steps — one per node from the
  lifecycle events in order: node id, node type, lane (`model_lane`), start/end
  (`created_at`), duration, finish reason, state (done/running/failed: `finish_reason` not in
  ('done', '') ⇒ failed); cost by stage — `llm_calls` of the run grouped by `feature_name`
  without its `mini-ork:` prefix (via `RunDetailRepository.fetch_llm_calls_by_run_id`), total;
  files changed with per-file +/− from `diffs.cached_or_computed` (never writes a cache) and
  whether that came from the cache; the verdict (`<run_dir>/verdict.json` `verdict` and its
  reason/summary field if present); learnings — titles of `fetch_learning_records(run_id)`
  (≤ 5).
- `render_card`: title line with the mark; one-line state detail; a facts line (recipe,
  started, duration/elapsed, cost); a steps table `| step | type | model lane | time | result |`;
  "Cost by stage" as one line (`worker $0.49 · rubric $0.50 · learning $0.87 …` — map
  `gradient-extract`/`pattern-induct`/`reflect` to "learning", `profile_answerer` to
  "profiling"); files changed list (`path +a −b`, "(as the files are now)" when not from the
  cache); verdict; learnings; and, when `serve_url` is given, `Full forensics: <serve_url>/runs/<id>`.

### `commands.py`

- `/runs [state] [recipe:<id>] [n]` (any order; n ≤ 50, default 20) → `render_fleet`.
- `/status [run]` → resolve the run as today (explicit id, thread's latest, or the run
  session) → `render_card`; `serve_url` when `_probe(<serve>/health)` answers within 1 s
  (reuse the `/serve` probe and port). Unknown run → "No run <id> in this project."
- Update `/help` descriptions for both.

## Tests

`tests/unit/test_acp_fleet.py` (temp home, migrated db copied once per module, seeded
task_runs / run_events / llm_calls / a diff cache / a `.cost-pause`): rows and counts per
state; filters (state, recipe, limit); precise state only computed for shown rows (patch
`history.read_snapshot` and count calls); time formatting; change from the diff cache;
empty result; card steps with durations and a failed step; cost by stage grouping and
labels; files from the cache vs computed note; verdict and learnings; unknown run → None.

`tests/unit/test_acp_commands.py`: `/runs`, `/runs failed`, `/runs recipe:docs 5`,
`/status <id>`, `/status` in a thread without a run, unknown id.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_fleet.py tests/unit/test_acp_commands.py tests/unit/test_acp_task_state.py tests/unit/test_acp_agent_py.py` passes.
- `ruff check` clean; diff touches only the files in scope.
