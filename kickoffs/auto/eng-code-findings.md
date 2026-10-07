# Code findings — harvest what reviews and verifiers said about each file, so engineers can see it

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`. Engineer-first redesign, step E1:
the data behind the "Your code" tab.

## Why

The Learning page talks about mini-ork's own machinery: task-class pass rates, stuck runs,
decaying memories. A software engineer can't use any of that. They need to know what mini-ork has
learned about THEIR code, file by file.

That knowledge already exists but is scattered over run directories:
- 314 `review-*.json` files hold 390 reviewer findings, and 254 of them name a file, e.g.
  `{"file": "mini_ork/acp/agent.py", "line": 1529, "severity": "low", "issue": "docstring
  contradicts fix #2 (mode no longer routes)"}`. `mini_ork/ide_pages/node.py` alone has 52.
- 126 `verifier-*.json` files record failed checks.

Nothing indexes either source.

Review file shapes seen live (all must parse):
1. Plain JSON `{"verdict", "notes": [str], "findings": [{"file", "line", "severity", "snippet",
   "issue"}], "reasons": [...]}`. `notes` / `reasons` may be strings or dicts.
2. JSON inside a ```json fence, sometimes with prose around it (251 of the
   `review-reviewer.json` files are not plain JSON).
3. Pure prose/markdown: bullet lines like ``- `framework-edit.diff` absent at
   `$MINI_ORK_RUN_DIR` root.``
4. When the `.json` is missing or empty, `review-<role>.json.stdout.md` carries the same content
   (a fenced JSON block or prose).

Reviewer roles seen: `reviewer`, `tiny_reviewer`, `opus_arbiter`, `final_reviewer`,
`opus_patch_critic`.

## Files in scope (touch ONLY these)

- `mini_ork/learning/code_findings.py` (new)
- `db/migrations/0064_code_findings.sql` (new; style of `db/migrations/0062_learning_ledger.sql`)
- `tests/unit/test_code_findings.py` (new)

Do NOT modify any other file. No reflect hook in this phase.

## Schema (0064, additive, `IF NOT EXISTS`)

- `code_findings(id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL, source TEXT NOT NULL, file TEXT, line INTEGER, severity TEXT NOT NULL,
  category TEXT NOT NULL, issue TEXT NOT NULL, snippet TEXT, verdict TEXT, ts INTEGER NOT NULL)`
  - indexes: `(file)`, `(run_id)`, `(category)`
  - `source` is `review:<role>` or `verifier:<name>`
  - `fingerprint` = `sha256(run_id|source|file|line|issue)`
- `code_findings_runs(run_id TEXT PRIMARY KEY, harvested_at INTEGER NOT NULL, n INTEGER NOT NULL)`

## `mini_ork/learning/code_findings.py` (exact)

Same DB resolution and `ensure_schema` discipline as `mini_ork/learning/ledger.py` (cold-safe,
busy_timeout, never raises on the write path).

1. `parse_review(text) -> list[dict]` (pure):
   - find a JSON object (plain, or the first ```json fence);
   - take `findings` dicts as-is;
   - for string items in `notes` / `reasons` / `findings`, and for prose bullet lines (`- `,
     `* `, `1. `), make one finding per item that names a file path. Path regex:
     `[\w./-]+\.(py|ts|tsx|js|rs|go|md|yaml|yml|json|sh|sql|toml)`, with an optional `:line`.
   Items naming no file are kept with `file=None` only when the verdict is not a pass (they
   explain a rejection).
2. `parse_verifier(name, payload) -> list[dict]`: a failed verifier (`pass` false, or verdict in
   `fail|FAIL|REFUTED|error`) → one finding, `severity='high'`, issue = its reason / first
   failed check (≤ 300 chars), file = the first path in that text, or None.
3. `categorize(issue) -> str`, deterministic keyword map, first match wins (module constant;
   unit-tested). Label in quotes, then its keywords:
   - `"test doesn't check the claim"`: test(s)? … (doesn't|does not|never) (test|check|assert),
     passes on base, vacuous, tautolog
   - `"comment or docstring contradicts code"`: docstring, comment … (contradict|false|wrong|stale)
   - `"missing guard or error handling"`: guard, cold-safe, OperationalError, raise, exception,
     None check, fail-soft
   - `"change outside the agreed scope"`: out of scope, outside scope, touches … not in scope,
     scope creep
   - `"wrong behaviour"`: wrong, incorrect, drops, misses, off by, regression, broken
   - `"performance"`: slow, quadratic, O(n, timeout, re-embed
   - `"security"`: secret, token, injection, unsafe, permission
   - `"missing artifact or output"`: absent, missing (file|output|artifact), not written
   - else `"other"`
4. `severity` normalized: blocker/blocking/critical/high → `high`; medium → `medium`;
   low/minor/nit → `low`; missing → `medium` (non-pass verdict) or `low`.
5. `file` normalization: strip `./` and a leading absolute repo or worktree prefix (anything up to
   `/mini-ork/`, `/mini-ork-worktrees/<slug>/`, or the run's `MO_TARGET_CWD` when it's in
   `run_profile.json`). A bare filename (`node.py`) is resolved to the unique matching path from
   `git -C <repo> ls-files` (repo = `MINI_ORK_ROOT`, else cwd) when exactly one matches;
   otherwise it stays bare.
6. `harvest(home, *, run_ids=None, db=None) -> dict`: for each run dir under `home/runs` not in
   `code_findings_runs` (or in `run_ids`), parse all review and verifier files,
   `INSERT OR IGNORE`, record the run. `ts` = the file's mtime. Returns `{"runs", "findings",
   "with_file", "skipped_unparseable"}`.
7. `areas(*, db=None, depth=3, since_days=30, limit=25) -> list[dict]`: group by the file's
   directory prefix up to `depth` segments, keeping the file itself when a single file dominates
   (≥ 60% of the area). Each row: `area`, `n_findings`, `n_runs`, `worst_severity`,
   `top_categories` (≤ 3 `(category, n)`), `last_ts`, `files` (top 5 `(file, n)`). Ordered by
   high-severity count, then `n_findings`. Read-only.
8. `findings_for(path_prefix, *, db=None, limit=50) -> list[dict]`: rows newest first, joined
   with the run title (the first `# ` heading of `task_runs.kickoff_path`, else the run id) and
   the run status. Read-only.
9. CLI: `python -m mini_ork.learning.code_findings {harvest | areas [--days N] | show <path>}
   [--db PATH] [--home PATH] [--json]`.

## Tests (`tests/unit/test_code_findings.py`, temp home + DB)

- `parse_review` on all four shapes, using fixture texts copied verbatim from live files (one each
  of: a plain JSON with `findings`; a fenced JSON with prose around it; a prose bullet review;
  notes as strings with `file.py:123`).
- `parse_verifier` on a failed and a passed payload.
- `categorize`: one real issue string per category, plus an "other".
- Bare-filename resolution with a fake repo (one match → full path; two → stays bare).
- `harvest` is incremental (second call adds 0) and idempotent per fingerprint.
- `areas` groups by directory, honours `since_days`, orders high severity first.
- `findings_for` returns the run titles.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_code_findings.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/code_findings.py tests/unit/test_code_findings.py` → clean.
- Proof on a COPY of the live DB, so the live one is never written:
  `python3.11 -c "import sqlite3; s=sqlite3.connect('/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db'); d=sqlite3.connect('/tmp/cf-proof.db'); s.backup(d)"`
  then `python3.11 -m mini_ork.learning.code_findings harvest --db /tmp/cf-proof.db --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork --json`
  and `... areas --db /tmp/cf-proof.db --days 60`, then delete `/tmp/cf-proof.db`. Paste the
  harvest stats and the top 10 areas. Expect `mini_ork/ide_pages/node.py` near the top.
- `git diff --stat` touches only the files in scope.
