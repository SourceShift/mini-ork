# Extension Guide

mini-ork has 4 canonical extension points. None require forking the framework. All extensions live in user-land config dirs or recipe dirs.

For Python-first integrations, start with [`docs/PYTHON_FRAMEWORK.md`](PYTHON_FRAMEWORK.md).
The Python facade exposes typed `RunRequest`, `RunResult`, `WorkflowSpec`,
`RecipeSpec`, `ProviderPolicy`, and `ExtensionRegistry` objects while preserving
the existing recipe directory format underneath.

```python
from pathlib import Path
from mini_ork import MiniOrk, RunRequest

result = MiniOrk().run(RunRequest(kickoff=Path("kickoff.md"), recipe="docs"))
print(result.command)
print(result.plan_path)
print(result.retained_home)
```

`RunRequest` auto-initializes `.mini-ork/` by default when embedding from
Python. Set `auto_init=False` if your host application manages bootstrap
explicitly.

---

## 1. WorkflowGraph

Add new nodes, edges, and workflow topologies by writing a `workflow.yaml` inside your recipe.

**Where:** `recipes/<your-recipe>/workflow.yaml`

**Current contract:** follow the shipped recipes in `recipes/*/workflow.yaml`. `schemas/workflow.schema.json` documents the target validation shape, but the live recipes currently include newer fields such as `verifier_ref` and human-decision edge types that the schema has not fully caught up with yet.

**Example — add a `researcher` node before `implementer`:**

```yaml
# recipes/my-recipe/workflow.yaml
version: "0.1.0"
task_class: code_fix
description: "Code fix with explicit research before implementation"

nodes:
  - name: planner
    type: planner
    model_lane: planner
    prompt_ref: prompts/planner.md
    dispatch_mode: serial

  - name: research
    type: researcher
    model_lane: researcher
    prompt_ref: prompts/research.md
    dispatch_mode: serial

  - name: implementer
    type: implementer
    model_lane: worker
    prompt_ref: prompts/implementer.md
    dispatch_mode: serial
    gates:
      - scope_gate
      - budget_gate

  - name: verify
    type: verifier
    prompt_ref: null
    verifier_ref: verifiers/targeted_test.sh
    dispatch_mode: serial

  - name: reviewer
    type: reviewer
    model_lane: reviewer
    prompt_ref: prompts/reviewer.md
    dispatch_mode: serial

  - name: publisher
    type: publisher
    prompt_ref: null
    dispatch_mode: serial

edges:
  - { from: planner,     to: research,     edge_type: depends_on }
  - { from: research,    to: implementer, edge_type: supplies_context_to }
  - { from: implementer, to: verify,      edge_type: verifies }
  - { from: verify,      to: reviewer,    edge_type: depends_on }
  - { from: reviewer,    to: publisher,   edge_type: depends_on }
```

**Allowed node types:** `planner` `researcher` `implementer` `reviewer` `verifier` `reflector` `publisher` `rollback`

**Common edge types:** `depends_on` `supplies_context_to` `verifies` `blocks` `retries` `escalates_to`. Some shipped recipes also use product-flow edges such as `human_decision_gate` and `verifies_user_choice`; treat those as live recipe dialect extensions until the schema is aligned.

There is no top-level `mini-ork validate` command yet. Until recipe validation is wired, use an existing recipe as the reference, then dry-run the recipe:

```bash
MINI_ORK_DRY_RUN=1 bin/mini-ork run my-recipe kickoff.md
```

---

## 2. AgentRegistry

Register new agent roles or model bindings without touching any `lib/` code.

**Where:** `${MINI_ORK_HOME}/config/agents.yaml` for lane-to-provider bindings, or provider-specific files under the framework `config/agents/` directory when a recipe ships an override.

**Runtime API:** `lib/agent_registry.sh:agent_register`

**Example — bind a custom reviewer lane to a local provider key:**

```yaml
# ${MINI_ORK_HOME}/config/agents.yaml
lanes:
  planner: opus
  worker: codex
  reviewer: kimi
  verifier: glm
  publisher: codex
```

Then reference it in your `workflow.yaml`:

```yaml
nodes:
  - name: review
    type: reviewer
    model_lane: reviewer
    prompt_ref: prompts/reviewer.md
    dispatch_mode: serial
```

**Shell registration at runtime:**

```bash
source lib/agent_registry.sh
agent_register \
  --role reviewer \
  --version "1.0" \
  --model "ollama/qwen2.5-72b" \
  --provider ollama \
  --task-classes "code_fix,blog_post"
```

Agent version metadata is persisted to `state.db:agent_versions`. Historical stats (success rate, cost, latency) accumulate in `agent_run_stats`.

---

## 3. VerifierRegistry

Add new deterministic verifiers — scripts that exit 0 on pass, non-zero on fail.

**Where (global):** `${MINI_ORK_HOME}/verifiers/<name>.sh`

**Where (recipe-scoped):** `recipes/<recipe>/verifiers/<name>.sh`

Recipe-scoped verifiers take precedence over global verifiers with the same name.

**Example — custom schema-diff verifier:**

```bash
#!/usr/bin/env bash
# recipes/my-recipe/verifiers/schema_diff.sh
# Receives: MINI_ORK_ARTIFACT_PATH, MINI_ORK_TASK_ID, MINI_ORK_RUN_ID
set -euo pipefail

diff_output=$(sqldiff "${MINI_ORK_BASELINE_DB:-/tmp/baseline.db}" "${MINI_ORK_ARTIFACT_PATH}")
if [[ -n "$diff_output" ]]; then
  echo "SCHEMA_DIFF_FAIL: unexpected schema changes"
  echo "$diff_output"
  exit 1
fi
echo "SCHEMA_DIFF_PASS"
exit 0
```

Reference it in `workflow.yaml` with `verifier_ref`:

```yaml
nodes:
  - name: verify
    type: verifier
    prompt_ref: null
    verifier_ref: verifiers/schema_diff.sh
    dispatch_mode: serial
```

**Shell registration at runtime:**

```bash
source lib/gate_registry.sh
gate_register \
  --name schema_diff \
  --type deterministic_verifier \
  --script "recipes/my-recipe/verifiers/schema_diff.sh" \
  --task-classes "db_migration"
```

---

## 4. ExperienceMemory

Extend the 8 built-in memory namespaces or override the context-assembly strategy.

### Option A — Add a new namespace via migration

Drop a new migration file:

```sql
-- db/migrations/008_my_namespace.sql
CREATE TABLE IF NOT EXISTS my_namespace_records (
  id          TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs(id),
  task_id     TEXT NOT NULL,
  agent_ver   TEXT,
  content     TEXT NOT NULL,
  tags        TEXT,     -- JSON array
  ts          INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_my_ns_task ON my_namespace_records(task_id);
```

Run `mini-ork init --migrate` to apply.

### Option B — Override context assembly per task class

Drop an override script:

```bash
# ${MINI_ORK_HOME}/config/context_assemblers/my_task_class.sh
# Override lib/context_assembler.sh:context_assemble() for task class my_task_class

context_assemble() {
  local task_id="$1"
  local budget_tokens="${MINI_ORK_CTX_BUDGET_TOKENS:-8000}"

  # Call base assembler
  source "${MINI_ORK_HOME}/lib/context_assembler.sh"
  local base_ctx
  base_ctx=$(context_assemble_base "$task_id" "$budget_tokens")

  # Inject custom namespace records
  local my_records
  my_records=$(sqlite3 "${MINI_ORK_DB}" \
    "SELECT content FROM my_namespace_records WHERE task_id='${task_id}' LIMIT 5")

  printf '%s\n\n## Custom Context\n%s\n' "$base_ctx" "$my_records"
}
```

The framework calls `context_assemble()` before dispatching each node. If a task-class override exists, it wins over the default implementation.

---

## 5. Custom Utility Scoring

Override the default utility function for a task class:

```bash
# ${MINI_ORK_HOME}/config/utility_functions/my_task_class.sh
# Must define: utility_score_override(run_id, task_id) -> float written to stdout

utility_score_override() {
  local run_id="$1" task_id="$2"

  # Read standard signals from state.db
  local success cost latency verifier_score
  success=$(sqlite3 "${MINI_ORK_DB}" \
    "SELECT CASE WHEN status='completed' THEN 1.0 ELSE 0.0 END FROM tasks WHERE id='${task_id}'")
  cost=$(sqlite3 "${MINI_ORK_DB}" \
    "SELECT COALESCE(SUM(cost_usd),0) FROM model_costs WHERE run_id='${run_id}'")
  verifier_score=$(sqlite3 "${MINI_ORK_DB}" \
    "SELECT COALESCE(AVG(passed),0) FROM gate_results WHERE task_id='${task_id}'")

  # Custom weighted formula
  python3 -c "
u = 0.5 * ${success} + 0.3 * ${verifier_score} - 0.2 * min(${cost}/5.0, 1.0)
print(round(u, 4))
"
}
```

The `promotion_gate` calls `utility_score_override()` if the file exists for the task class. Fall-through to the default `lib/utility_function.sh` if not.

---

## 6. Custom Gates

Register a gate beyond the 6 built-in types:

```bash
source lib/gate_registry.sh

gate_register \
  --name my_custom_gate \
  --type deterministic_verifier \
  --script "recipes/my-recipe/verifiers/my_custom_gate.sh" \
  --task-classes "my_task_class" \
  --continue-on-fail false
```

Reference in `workflow.yaml`:

```yaml
nodes:
  - id: verify
    type: verifier
    gates: [deterministic_verifier, my_custom_gate]
    verifies: impl
```

Gates fire in declaration order. First non-zero exit stops the chain unless `continue_on_fail: true`.
