# Extension Guide

mini-ork has 4 canonical extension points. None require forking the framework. All extensions live in user-land config dirs or recipe dirs.

---

## 1. WorkflowGraph

Add new nodes, edges, and workflow topologies by writing a `workflow.yaml` inside your recipe.

**Where:** `recipes/<your-recipe>/workflow.yaml`

**Validated against:** `schemas/workflow.schema.json`

**Example — add a `researcher` node before `implementer`:**

```yaml
# recipes/my-recipe/workflow.yaml
version: "1.0"
task_class: code_fix

nodes:
  - id: plan
    type: planner
    model_lane: architect

  - id: research
    type: researcher
    model_lane: worker
    depends_on: [plan]

  - id: impl
    type: implementer
    model_lane: worker
    depends_on: [plan, research]

  - id: verify
    type: verifier
    scripts: [typecheck.sh, targeted_test.sh]
    verifies: impl
    retries: impl          # fires the retries edge on fail
    max_retries: 2

  - id: review
    type: reviewer
    model_lane: architect
    depends_on: [verify]

  - id: publish
    type: publisher
    depends_on: [review]

  - id: rollback
    type: rollback
    escalates_to: rollback  # verify escalates to this after max_retries
```

**Allowed node types:** `planner` `researcher` `implementer` `reviewer` `verifier` `reflector` `publisher` `rollback`

**Allowed edge fields per node:** `depends_on` `supplies_context_to` `verifies` `blocks` `retries` `escalates_to`

Run `mini-ork validate recipes/my-recipe/workflow.yaml` to check against the schema before running.

---

## 2. AgentRegistry

Register new agent roles or model bindings without touching any `lib/` code.

**Where:** `${MINI_ORK_HOME}/config/agents/<role>.yaml`

**Runtime API:** `lib/agent_registry.sh:agent_register`

**Example — register a custom reviewer using a local Ollama model:**

```yaml
# ${MINI_ORK_HOME}/config/agents/my-reviewer.yaml
role: reviewer
version: "1.0"
model: ollama/qwen2.5-72b
provider: ollama
tools: []
context_window: 32768
cost_per_1m_input: 0.0
cost_per_1m_output: 0.0
task_classes: [code_fix, blog_post]
notes: "Local reviewer — no external API call"
```

Then reference it in your `workflow.yaml`:

```yaml
nodes:
  - id: review
    type: reviewer
    agent: my-reviewer   # matches the role + version key
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

Reference it in `workflow.yaml`:

```yaml
nodes:
  - id: verify
    type: verifier
    scripts: [schema_diff.sh, targeted_test.sh]
    verifies: impl
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
