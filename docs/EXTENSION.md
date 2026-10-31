# Extension Guide

mini-ork has 4 canonical extension points. None require forking the framework. All extensions live in user-land config dirs or recipe dirs.

For Python-first integrations, start with [`docs/PYTHON-SDK.md`](PYTHON-SDK.md).
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

### Node type dispatch semantics

The `workflow.yaml` fields above describe the *shape* of a node. They don't
describe what `mini_ork/cli/execute.py` actually *does* with each `type` at
dispatch time — and the difference matters: two node types that look
interchangeable have very different guarantees about whether your LLM
call's output actually becomes a usable artifact.

| Type | Write-target injected into prompt? | Response captured if the model doesn't write a file itself? | Gated on a `verdict`? |
|---|---|---|---|
| `researcher` | Yes — `"Write your output to: $RUN_DIR/context-<node_id>.json"` (a real resolved path, not a variable the model has to expand) | Yes — raw response text is saved to that path as a fallback | No |
| `reviewer` | Yes — same as `researcher`, saved to `$RUN_DIR/review-<node_id>.json` | Yes — same fallback | **Yes**, see below |
| `implementer` | No | **No.** `IMPL_LOG` only stores raw stdout for forensics; it is never treated as a data artifact | No |
| `verifier` | N/A (runs `verifier_ref` script, no LLM call) | N/A | N/A (exit code is the verdict) |
| `publisher` / `rollback` | N/A (`prompt_ref: null`, deterministic) | N/A | N/A |

**Use `researcher` for any stage whose job is "generate content and hand it
to the next node."** This is the default choice — it's what every shipped
lens/synthesis-style recipe (`chapter-review`, `dsp-planning-burst`) uses.
The model doesn't need a file-write tool at all: tell it in the prompt to
emit exactly one JSON value as its final response, and mini-ork saves it.

**Use `implementer` only for actual code-editing tasks against a git
checkout.** It's the only node type where `MO_TARGET_CWD` gets pinned to a
worktree, matching `code-fix` / `recursive-validate-impl`'s pattern of
having the model edit real files in a real repo. If you route a
"generate JSON, write it to the run dir" task through `implementer`, the
LLM call will report `[ok] implementer output` — success! — while
producing zero usable artifact, because nothing captures its response.
This failure mode is silent: the dispatch itself doesn't error.

**The `reviewer` verdict gate.** After capturing the response,
`lib/extract_verdict.py` looks for a `verdict` key and requires its value
to be one of `pass`/`approve`/`approved` to succeed. `revise`/
`needs_revision`/`request_changes`/`fail`/`failed`/`escalate` — and
critically, *anything else, including a missing `verdict` key entirely* —
fails the node (`return 1`) and is counted toward `FAIL_COUNT`. This gate
exists for recipes whose edges/loop genuinely branch on the verdict
(escalate-to-reflector patterns). If your reviewer node is informational
only — its prompt's real output contract has no `verdict` field, e.g.
`{"passed": bool, "notes": "..."}`  — this gate will unconditionally fail
that node regardless of how valid the content is. Use `type: researcher`
instead; it captures identical content without the gate.

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

Lane values resolve to entries in the provider registry, `config/providers.yaml`
(the committed lanes plus your own Anthropic / OpenAI-compatible keys — see
`docs/CONFIG.md` → "Bring-your-own providers"). Custom names like
`openai_api` or `openrouter` work anywhere a builtin lane value does.

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

**Registration at runtime (Python):**

```python
from mini_ork.gates import gate_registry

gate_registry.gate_register(
    db_path,
    "deterministic_verifier",
    "recipes/my-recipe/verifiers/schema_diff.sh",
    task_class_filter="db_migration",
)
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

### Option B — Extend native context assembly

Context assembly is owned by `mini_ork/context_assembler.py`. Add a bounded
producer there and compose its result in `context_assemble()` or the relevant
prompt helper. Keep database access parameterized with `db`, cite every emitted
record, enforce `MINI_ORK_CTX_BUDGET_TOKENS`, and add standalone contracts in
`tests/unit/test_context_assembler_py.py`.

There is no supported shell override directory. The previously documented
`${MINI_ORK_HOME}/config/context_assemblers/*.sh` hook was never implemented by
the runtime and was removed from the public configuration map when the Bash
assembler was retired.

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

Gates are **per task class**, not per node. Each gate is a row in the
`registered_gates` table; `mini-ork verify` evaluates every gate that applies
to the task class through `gate_registry.gate_run_all`. To check one node's
output, add a verifier node to the workflow — that is the per-node mechanism.
The Python module is `mini_ork.gates.gate_registry`.

Register a gate beyond the built-in types:

```python
from mini_ork.gates import gate_registry

# 1. Optionally add a new gate *type*. An evaluator is
#    Callable[[str, str, str, Optional[str]], str] and returns
#    "pass", "fail" or "defer".
def my_evaluator(condition: str, context_json: str, db_path: str,
                 mini_ork_root: str | None) -> str:
    return "pass" if condition in context_json else "fail"

gate_registry.register_gate_evaluator("my_custom_type", my_evaluator)

# 2. Register the gate row for a task class. ``condition`` is a bash function
#    name or a script path; the evaluator decides how to read it.
gate_id = gate_registry.gate_register(
    db_path,
    "my_custom_type",
    "recipes/my-recipe/verifiers/my_custom_gate.sh",
    task_class_filter="my_task_class",
    safety=False,
)
```

Exact signatures:

- `register_gate_evaluator(gate_type: str, evaluator: GateEvaluator) -> None`
  adds or replaces the evaluator for a type; unregistered types `defer`.
- `gate_register(db_path: str, gate_type: str, condition: str, task_class_filter: str = "", safety: bool = False) -> str`
  returns the new `gate_id` (empty string on a uuid collision) and raises
  `ValueError` for an unknown `gate_type`.
- `gate_run_all(db_path: str, task_class: str, context_json: str, mini_ork_root: Optional[str] = None) -> dict[str, Any]`
  evaluates every applicable gate and returns the summary `mini-ork verify`
  gates on (`all_pass`, `any_fail`, `any_defer`, `safety_violation`, `gates`).

Registered gates run per task class when `mini-ork verify` calls
`gate_run_all`. A `workflow.yaml` node no longer takes a gate list; to check
one node's output, add a verifier node (a `type: verifier` node with a
`verifier_ref`) — the recipe's own verifier script is the check.
