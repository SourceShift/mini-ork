# Example 01 — Hello World: Add a CHANGELOG Entry

The smallest possible mini-ork delivery. One epic, one file changed, no
external services. Use this to verify your install before trying anything
bigger.

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
mini-ork deliver kickoff.md
```

Expected wall-clock time: **< 60 seconds**.
Expected cost: **~$0.004** (single Sonnet worker, ~1 K tokens).

## What Happens

```
kickoff.md
    │
    ▼
decomposer (Opus) — seeds 1 epic: "add-changelog-entry"
    │
    ▼
worker (Sonnet) — edits CHANGELOG.md, adds entry under [Unreleased]
    │
    ▼
spec-reviewer — reads diff, approves (3-line change, no issues)
    │
    ▼
bdd-runner — runs 2 scenarios:
  1. [Unreleased] section has ≥1 new bullet
  2. Only CHANGELOG.md is in the diff
    │ PASS
    ▼
auto-merge — fast-forwards branch onto main
    │
    ▼
state.db — verdict=PASS recorded
```

## Verification

After `mini-ork deliver` exits 0:

```bash
# Confirm the entry was added:
grep -A5 "\[Unreleased\]" CHANGELOG.md

# Inspect the run in state.db:
sqlite3 .mini-ork/state.db \
  "SELECT id, status, verdict, cost_usd FROM epics ORDER BY created_at DESC LIMIT 1;"
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `mini-ork: command not found` | install.sh not run | `bash ~/ps/mini-ork/install.sh` |
| `sqlite3: command not found` | sqlite3 missing | `brew install sqlite3` / `apt install sqlite3` |
| `state.db not found` after deliver | `mini-ork init` skipped | run `mini-ork init` in the project root first |
| Worker exits non-zero | `claude` CLI not authenticated | `claude auth login` |
| BDD FAIL: "no other file modified" | Worker touched extra file | Check worker diff; set stricter scope in kickoff |
| Cost higher than expected | Wrong model routed | Add `model: claude-sonnet-4-5` to kickoff under `## Model Preference` |
