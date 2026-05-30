# Configuration Reference

mini-ork is configured via three layers (highest precedence first):

1. Shell environment variables
2. `.mini-ork/config.env` (sourced at startup, gitignored)
3. `.mini-ork/agents.yaml`
4. Built-in defaults

## `agents.yaml` Format

Written to `.mini-ork/agents.yaml` by `mini-ork init`. Edit per-repo.

```yaml
# .mini-ork/agents.yaml

# --- Run behavior ---
max_iters: 3          # max worker+heal cycles per epic before escalation
max_lanes: 4          # max parallel lane subprocesses
bdd_runner: bash      # how to execute BDD specs: bash | pytest | bun | node
timeout_seconds: 600  # per-epic wall-clock timeout (0 = no limit)

# --- Model defaults (all overridable per-epic) ---
decomposer_model: claude-opus-4
worker_model: claude-sonnet-4-5
reviewer_model: claude-opus-4
spec_author_model: claude-sonnet-4-5
healer_model: claude-sonnet-4-5
hunter_model: glm-4

# --- Cost guard ---
budget_usd: 0.0       # 0 = no limit; non-zero halts new lanes when exceeded

# --- Hook scripts (relative to repo root) ---
hooks:
  pre_worker:   ""    # called before each worker subprocess
  post_review:  ""    # called after each reviewer run
  on_escalate:  ""    # called when an epic is escalated to INBOX
  post_merge:   ""    # called after successful auto-merge

# --- Per-epic model overrides ---
# epics:
#   - name: boilerplate-crud
#     model: deepseek-v3
#   - name: security-audit
#     model: claude-opus-4
```

## Environment Variables

### Core paths

| Variable | Default | Description |
|---|---|---|
| `MINI_ORK_HOME` | `<repo-root>/.mini-ork` | Base directory for all mini-ork state |
| `MINI_ORK_DB` | `$MINI_ORK_HOME/state.db` | sqlite3 state database path |
| `MINI_ORK_INBOX` | `$MINI_ORK_HOME/INBOX` | Escalation output directory |
| `MINI_ORK_KICKOFF_DIR` | `$MINI_ORK_HOME/kickoffs` | Where `mini-ork init` copies kickoff archives |
| `MINI_ORK_RUNS_DIR` | `$MINI_ORK_HOME/runs` | Per-run working directories (worktrees, logs) |
| `MINI_ORK_LOCKS_DIR` | `$MINI_ORK_HOME/locks` | Advisory lock files for lane coordination |

### Model routing overrides

| Variable | Overrides |
|---|---|
| `MINI_ORK_DECOMPOSER_MODEL` | `agents.yaml: decomposer_model` |
| `MINI_ORK_WORKER_MODEL` | `agents.yaml: worker_model` |
| `MINI_ORK_REVIEWER_MODEL` | `agents.yaml: reviewer_model` |
| `MINI_ORK_SPEC_AUTHOR_MODEL` | `agents.yaml: spec_author_model` |
| `MINI_ORK_HEALER_MODEL` | `agents.yaml: healer_model` |
| `MINI_ORK_HUNTER_MODEL` | `agents.yaml: hunter_model` |

### Run behavior overrides

| Variable | Default | Description |
|---|---|---|
| `MINI_ORK_MAX_ITERS` | `agents.yaml: max_iters` | Max iters per epic |
| `MINI_ORK_MAX_LANES` | `agents.yaml: max_lanes` | Max parallel lanes |
| `MINI_ORK_TIMEOUT` | `agents.yaml: timeout_seconds` | Per-epic timeout in seconds |
| `MINI_ORK_BUDGET_USD` | `agents.yaml: budget_usd` | Cost cap in USD (0 = off) |
| `MINI_ORK_DRY_RUN` | `0` | Set to `1` to decompose + print epics without running workers |
| `MINI_ORK_NO_MERGE` | `0` | Set to `1` to skip auto-merge (workers + review run normally) |
| `MINI_ORK_VERBOSE` | `0` | Set to `1` for debug-level log output |

### Provider API keys

Store these in `.mini-ork/config.env` (not in shell rc files — they apply only to mini-ork runs):

```bash
# .mini-ork/config.env  — gitignored, never commit

ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=...
GLM_API_KEY=...
KIMI_API_KEY=...
MY_CUSTOM_PROVIDER_API_KEY=...
```

`config.env` is sourced by `lib/llm-dispatch.sh` before any model call. Variable names are passed through unchanged to the provider dispatch functions.

## Provider env files

For multi-key setups (e.g. different keys per environment), place named env files in `.mini-ork/secrets/`:

```
.mini-ork/secrets/
  anthropic.env    # ANTHROPIC_API_KEY=...
  deepseek.env     # DEEPSEEK_API_KEY=...
```

Select a secrets file at run time:

```bash
MINI_ORK_SECRETS_FILE=.mini-ork/secrets/anthropic.env mini-ork deliver kickoff.md
```

`secrets/` is gitignored. Never place these files anywhere else in the repo.

## Hook Script Interface

Hook scripts receive the following env vars:

| Variable | Description |
|---|---|
| `MINI_ORK_RUN_ID` | UUID of the current run |
| `MINI_ORK_EPIC_ID` | 8-char epic ID (empty in `post_merge`) |
| `MINI_ORK_EPIC_NAME` | Human-readable epic name from kickoff |
| `MINI_ORK_VERDICT` | Current verdict: `pending \| pass \| fail \| escalated` |
| `MINI_ORK_ITER` | Current iter number (1-based) |
| `MINI_ORK_WORKTREE` | Absolute path to the epic's worktree |
| `MINI_ORK_DB` | Path to state.db (safe to read, not write) |

Exit 0 to continue. Exit non-zero to abort the step (epic is escalated, run continues for other lanes).
