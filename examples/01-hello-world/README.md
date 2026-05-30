# Example 01 — Hello World: Add a CHANGELOG Entry

The smallest possible mini-ork task. One file changed, no external services.
Use this to verify your install and confirm the `code-fix` recipe runs end-to-end
before trying anything larger.

## Prerequisites

| Requirement | Check |
|---|---|
| mini-ork installed | `mini-ork version` → `mini-ork 0.1.0` |
| Repo with a `CHANGELOG.md` | any git repo works |
| `sqlite3` 3.35+ | `sqlite3 --version` |
| `jq` 1.6+ | `jq --version` |
| `claude` CLI | `claude --version` (skip: smoke-test mode runs without it) |

## Command

```bash
# From the root of any git repo that has a CHANGELOG.md:
cp ~/ps/mini-ork/examples/01-hello-world/kickoff.md ./kickoff.md
mini-ork run code-fix kickoff.md
```

Expected wall-clock time: **2 – 5 minutes**.
Expected cost: **~$0.10 – $0.25** (planner + implementer + reviewer, Sonnet/Sonnet/Opus).

## What Happens

```
kickoff.md
    │
    ▼
classifier — detects task_class=code_fix (confidence ≥ 0.90)
    │
    ▼
planner (Sonnet) — emits plan.json: 1 step, edits CHANGELOG.md
    │
    ▼
implementer (Sonnet) — applies Edit to CHANGELOG.md, emits implementer_summary.json
    │
    ├──▶ verifier/typecheck.sh — auto-detects no typecheck tool → PASS (skipped)
    │
    └──▶ verifier/test.sh — detects npm test if present, else skipped → PASS
              │
              ▼
reviewer (Opus) — reads diff + verifier results → verdict=APPROVE
              │
              ▼
publisher — commits CHANGELOG.md on feature branch
              │
              ▼
state.db — verdict=APPROVE recorded
```

## Verification

After `mini-ork run code-fix` exits 0:

```bash
# Confirm the entry was added:
grep -A5 "\[Unreleased\]" CHANGELOG.md

# Inspect the run log:
ls .mini-ork/runs/

# Inspect the run in state.db (if mini-ork init was run in this repo):
sqlite3 .mini-ork/state.db \
  "SELECT id, status, verdict, cost_usd FROM runs ORDER BY created_at DESC LIMIT 1;"
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `mini-ork: command not found` | install.sh not run | `bash ~/ps/mini-ork/install.sh` |
| `sqlite3: command not found` | sqlite3 missing | `brew install sqlite3` / `apt install sqlite3` |
| `no recipe found: code-fix` | recipes dir not on path | check `MINI_ORK_ROOT` points to `~/ps/mini-ork` |
| Worker exits non-zero | `claude` CLI not authenticated | `claude auth login` |
| Reviewer issues REQUEST_CHANGES | Worker touched extra file | check kickoff scope; set `MINI_ORK_TYPECHECK_CMD=""` to skip typecheck |
| Cost higher than expected | Wrong model lane routed | add `model_lane: worker: claude-sonnet-4-5` override in kickoff |

## What changed from v0.0

- Command changed from `mini-ork deliver kickoff.md` to `mini-ork run code-fix kickoff.md`.
- The `code-fix` recipe now drives the run via `workflow.yaml` (explicit node graph).
- Verifier scripts (`typecheck.sh`, `test.sh`) replaced the old inline BDD runner.
- Reviewer prompt is now the generic `recipes/code-fix/prompts/reviewer.md` — no
  project-specific rules baked in.
- Cost estimate updated: planner + reviewer adds ~$0.10 overhead vs the old
  single-worker path, but review quality is substantially higher.
