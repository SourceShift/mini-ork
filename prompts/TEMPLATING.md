# mini-ork Prompt Templating

All prompts in this directory use `{{PLACEHOLDER}}` syntax for project-specific
values. The orchestrator substitutes these at dispatch time.

---

## Standard placeholders (substituted by orchestrator at dispatch)

| Placeholder | What it maps to | Example |
|-------------|-----------------|---------|
| `{{PROJECT_NAME}}` | Your app/product name | `MyApp` |
| `{{PROD_HOST}}` | Production hostname or SSH alias | `myprod` |
| `{{PROD_HOST_IP}}` | Production host IP (Tailscale or LAN) | `10.0.0.5` |
| `{{BACKEND_DIR}}` | Relative path to backend source root | `server` |
| `{{FRONTEND_DIR}}` | Relative path to frontend source root | `src` |
| `{{BACKEND_URL}}` | Backend base URL for bug-hunt probes | `http://localhost:3000` |
| `{{FRONTEND_URL}}` | Frontend base URL | `https://localhost:5173` |
| `{{JOB_QUEUE}}` | Job queue library name | `BullMQ`, `Sidekiq`, `Celery` |
| `{{SANDBOX}}` | Sandboxed execution environment | `Daytona`, `E2B`, `local` |
| `{{LLM_FRAMEWORK}}` | LLM type system / schema framework | `BAML`, `Instructor`, `none` |
| `{{QUEUE_PREFIX}}` | Redis queue key prefix | `prod_`, `dev_` |
| `{{PRIOR_BUG_EXAMPLE}}` | Concrete example bug ID from prior audit | `AUTH-001` |

---

## Per-dispatch placeholders (set by orchestrator per-run)

| Placeholder | Set by | Notes |
|-------------|--------|-------|
| `{{FEATURE}}` | Dispatcher | The feature name being hunted/refactored |
| `{{ROUND}}` | Dispatcher | Current hunt round (1, 2, 3…) |
| `{{HUNTER_ID}}` | Dispatcher | Unique hunter ID string (`glm`, `kimi`, `corr`, …) |
| `{{HUNTER_ROLE}}` | Dispatcher | Role enum: `correctness`, `security`, `ux_a11y`, `perf`, … |
| `{{TIER}}` | Dispatcher | Bug tier (1=critical, 2=major, 3=minor) |
| `{{REPORT_PATH}}` | Dispatcher | Absolute path where hunter writes NDJSON output |
| `{{SCOPE_GLOBS}}` | Dispatcher | Glob patterns the hunter may read/edit |
| `{{ENTRY_URLS}}` | Dispatcher | FE entry URLs to probe |
| `{{BE_ROUTES}}` | Dispatcher | BE route paths to probe |
| `{{TESTIDS}}` | Dispatcher | `data-testid` values used in assertions |
| `{{HUNT_RECIPE}}` | Dispatcher | Free-text hunting recipe for this round |
| `{{VOTE_MODE}}` | Dispatcher | `union` \| `weighted` \| `intersection` |
| `{{PRIOR_ROUND_REPORTS}}` | Dispatcher | Paths to prior round reports (round ≥ 2) |
| `{{KICKOFF_BODY}}` | Dispatcher | Full epic kickoff text |
| `{{KICKOFF_PATH}}` | Dispatcher | File path to the kickoff doc |
| `{{REVIEWER_FEEDBACK}}` | Dispatcher | REQUEST_CHANGES text from reviewer |
| `{{CURRENT_DIFF}}` | Dispatcher | `git diff main..HEAD` output |
| `{{DIFF_FILES}}` | Dispatcher | List of files changed by worker |
| `{{DIFF_SUMMARY}}` | Dispatcher | Summary of worker's diff |
| `{{FAILURE_SUMMARY}}` | Dispatcher | Top failing BDD scenarios |
| `{{ARCH_ID}}` | Dispatcher | ARCH-SPEC ID (e.g., `ARCH-STRUCT-compose-liveness`) |
| `{{ARCH_TITLE}}` | Dispatcher | One-line title of the ARCH-SPEC |
| `{{ARCH_PRE}}` | Dispatcher | ARCH-SPEC precondition |
| `{{ARCH_POST}}` | Dispatcher | ARCH-SPEC postcondition |
| `{{ARCH_FRAME}}` | Dispatcher | ARCH-SPEC frame (files NOT to touch) |
| `{{ARCH_VERIFIER}}` | Dispatcher | Shell command proving postcondition |
| `{{ARCH_EVIDENCE}}` | Dispatcher | Evidence file:line citations |
| `{{CYCLE_ID}}` | Dispatcher | Unique ID for this refactor cycle |
| `{{GIT_HEAD}}` | Dispatcher | `git rev-parse HEAD` at dispatch time |
| `{{SIGNATURE_YAML}}` | Dispatcher | Feature repo signature (key files + line counts) |
| `{{MODULE_ID}}` | Dispatcher | MODULE-PLAN ID |
| `{{CANDIDATE_ID}}` | Dispatcher | Chosen Pareto candidate ID |
| `{{CANDIDATE_LABEL}}` | Dispatcher | `balanced` \| `max cohesion` \| `min churn` etc. |
| `{{NODE_BATCH_JSON}}` | Dispatcher | JSON array of functions to annotate |
| `{{BATCH_ID}}` | Dispatcher | Batch identifier for annotator run |
| `{{BATCH_SIZE}}` | Dispatcher | Number of nodes in this batch |
| `{{VALIDATION_ID}}` | Dispatcher | Validator run ID |
| `{{VALIDATION_JSON}}` | Dispatcher | Full validator verdict JSON |
| `{{MODULE_FRAME}}` | Dispatcher | MODULE-PLAN frame JSON |
| `{{ROUTE_NODES_JSON}}` | Dispatcher | Function route for validator |
| `{{COMMUNITY_ID}}` | Dispatcher | Graph community ID for validator |
| `{{ADR_ID}}` | Dispatcher | ADR identifier |
| `{{SHIPPED_PRS}}` | Dispatcher | Comma-list of merged atom-PR IDs |

---

## How to add a custom placeholder

1. Pick a name in `{{SCREAMING_SNAKE_CASE}}`.
2. Add it to your orchestrator's dispatch config (wherever you call `envsubst` or
   the equivalent).
3. Document it in this table.
4. Reference it in any prompt file with `{{MY_PLACEHOLDER}}`.

The orchestrator substitutes values using simple string replacement — there is no
template engine. Placeholders that have no substitution value are left as-is in
the prompt (the model sees the literal `{{MY_PLACEHOLDER}}`). Always provide all
placeholders your prompt uses.

---

## Project bootstrap

To wire up your project, create `$MINI_ORK_HOME/config/project.env`:

```sh
export PROJECT_NAME="YourAppName"
export PROD_HOST="myprodserver"
export PROD_HOST_IP="10.0.0.5"
export BACKEND_DIR="server"
export FRONTEND_DIR="src"
export BACKEND_URL="http://localhost:3000"
export FRONTEND_URL="http://localhost:5173"
export JOB_QUEUE="BullMQ"
export SANDBOX="Daytona"
export LLM_FRAMEWORK="none"
export QUEUE_PREFIX="dev_"
export PRIOR_BUG_EXAMPLE="none-yet"
```

Source this file in your dispatch scripts before calling the orchestrator.
