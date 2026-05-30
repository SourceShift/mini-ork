# Configuration Reference

mini-ork is configured via four layers (highest precedence first):

1. Shell environment variables
2. `${MINI_ORK_HOME}/config/` YAML files (per task class, per agent, per artifact contract)
3. Recipe-level `workflow.yaml` and `config/` overrides
4. Built-in defaults

---

## Directory Layout

```
${MINI_ORK_HOME}/
  config/
    task_classes/           # task class definitions (one YAML per class)
      code_fix.yaml
      research_synthesis.yaml
      blog_post.yaml
      db_migration.yaml
      ops_runbook.yaml
      ui_audit.yaml

    agents/                 # agent role + model bindings
      architect.yaml        # opus-class — planner, reviewer
      worker.yaml           # sonnet-class — implementer, researcher, reflector
      cheapfast.yaml        # glm-class — verifier, publisher, rollback
      long-context.yaml     # kimi-class — reviewer on large diffs
      budget.yaml           # deepseek-class — budget worker

    artifact_contracts/     # per-class artifact shapes (optional overrides)
      code_fix.yaml
      db_migration.yaml

    utility_functions/      # per-class utility score overrides (optional)
      db_migration.sh

    context_assemblers/     # per-class context assembly overrides (optional)
      research_synthesis.sh

    safety.yaml             # immutable safety constraints
```

---

## Task Class YAML

Schema: `schemas/task_class.schema.json`

```yaml
# ${MINI_ORK_HOME}/config/task_classes/code_fix.yaml
task_class: code_fix
artifact_type: patch          # patch | doc | report | config | migration | runbook
risk_class: medium            # low | medium | high | critical
required_verifiers:
  - typecheck
  - targeted_test
  - reviewer_gate
failure_policy: request_changes_or_escalate  # request_changes_or_escalate | escalate | abort
rollback_policy: git_branch_quarantine        # git_branch_quarantine | snapshot | none
human_review_level: none                      # none | optional | required
max_iters: 3
```

Built-in task classes: `code_fix`, `research_synthesis`, `blog_post`, `ui_audit`, `db_migration`, `ops_runbook`.

To add a new class, drop a YAML file in `config/task_classes/` — no code change needed.

---

## Agent Role Bindings

Schema: `schemas/agent_version.schema.json`

```yaml
# ${MINI_ORK_HOME}/config/agents/architect.yaml
lane: architect
model: claude-opus-4
provider: anthropic
context_window: 200000
cost_per_1m_input: 15.00
cost_per_1m_output: 75.00
task_classes: ["*"]           # wildcard = all classes
notes: "High reasoning — planner, reviewer, escalation summary"
```

```yaml
# ${MINI_ORK_HOME}/config/agents/worker.yaml
lane: worker
model: claude-sonnet-4-5
provider: anthropic
context_window: 200000
cost_per_1m_input: 3.00
cost_per_1m_output: 15.00
task_classes: ["*"]
notes: "Best cost/quality for implementation, research, reflection"
```

```yaml
# ${MINI_ORK_HOME}/config/agents/cheapfast.yaml
lane: cheapfast
model: glm-4
provider: zhipu
context_window: 128000
cost_per_1m_input: 0.14
cost_per_1m_output: 0.14
task_classes: ["*"]
notes: "Deterministic verifiers, publisher, rollback — no generation depth needed"
```

```yaml
# ${MINI_ORK_HOME}/config/agents/long-context.yaml
lane: long-context
model: kimi-k2
provider: moonshot
context_window: 131072
cost_per_1m_input: 0.60
cost_per_1m_output: 2.50
task_classes: ["code_fix", "ui_audit"]
notes: "Cost-efficient for large-diff review (> 64K tokens)"
```

```yaml
# ${MINI_ORK_HOME}/config/agents/budget.yaml
lane: budget
model: deepseek-v3
provider: deepseek
context_window: 65536
cost_per_1m_input: 0.27
cost_per_1m_output: 1.10
task_classes: ["code_fix"]
notes: "~10x cheaper than worker; good for boilerplate-heavy tasks"
```

---

## Artifact Contract YAML

Schema: `schemas/artifact_contract.schema.json`

```yaml
# ${MINI_ORK_HOME}/config/artifact_contracts/code_fix.yaml
task_class: code_fix
expected_artifact: patch
success_verifiers:
  - typecheck
  - targeted_test
  - reviewer_gate
failure_policy: request_changes_or_escalate
rollback_policy: git_branch_quarantine
artifact_checks:
  - type: file_exists
    path: "{{ artifact_path }}"
  - type: non_empty
    path: "{{ artifact_path }}"
  - type: json_schema
    path: "{{ artifact_path }}"
    schema: schemas/artifact_contract.schema.json
    when: artifact_type == "json"
```

Artifact contracts are resolved at classify time. If no override exists in `config/artifact_contracts/`, the task class YAML's inline contract is used.

---

## Safety Config

```yaml
# ${MINI_ORK_HOME}/config/safety.yaml
# IMMUTABLE — changes require human gate (rung 7) + audit_log entry

constraints:
  no_autonomous_production_deploy: true
  no_fallback_on_model_failure: true
  no_silent_memory_write: true       # every write must carry run_id + agent_version_id
  no_promotion_without_benchmark: true
  audit_log_append_only: true        # enforced by sqlite trigger

promotion_gate:
  require_utility_delta_positive: true
  require_all_benchmarks_pass: true
  require_no_constraint_violation: true
  human_gate_required_for_rungs: [6, 7]

human_gate:
  inbox_dir: "${MINI_ORK_INBOX}"
  poll_interval_seconds: 30
  timeout_hours: 48                  # 0 = wait forever
```

---

## Environment Variables

### Core paths

| Variable | Default | Description |
|---|---|---|
| `MINI_ORK_HOME` | `<repo-root>/.mini-ork` | Base directory for all mini-ork state |
| `MINI_ORK_DB` | `$MINI_ORK_HOME/state.db` | sqlite3 state database |
| `MINI_ORK_INBOX` | `$MINI_ORK_HOME/INBOX` | Escalation + human-gate inbox |
| `MINI_ORK_KICKOFF_DIR` | `$MINI_ORK_HOME/kickoffs` | Kickoff archive dir |
| `MINI_ORK_CTX_BUDGET_TOKENS` | `8000` | Max tokens per context pack |

### Model and execution

| Variable | Default | Description |
|---|---|---|
| `MINI_ORK_TYPECHECK_CMD` | `npx tsc --noEmit` | Typecheck command for `typecheck` verifier |
| `MINI_ORK_TEST_CMD` | `npm test -- --passWithNoTests` | Test runner command |
| `MINI_ORK_PLAYWRIGHT_CMD` | `npx playwright test` | E2E test command |
| `MINI_ORK_GRADIENT_EXTRACTOR_FN` | `lib/gradient_extractor.sh:extract_gradients` | Override gradient extraction |

### Run behavior

| Variable | Default | Description |
|---|---|---|
| `MINI_ORK_MAX_ITERS` | task class `max_iters` | Max implementer+heal cycles before escalation |
| `MINI_ORK_MAX_LANES` | `4` | Max parallel node subprocesses |
| `MINI_ORK_TIMEOUT` | `600` | Per-node wall-clock timeout (seconds; 0 = no limit) |
| `MINI_ORK_BUDGET_USD` | `0` | Run cost cap in USD (0 = no limit; fires `budget_gate`) |
| `MINI_ORK_DRY_RUN` | `0` | `1` = classify + plan only, no execution |
| `MINI_ORK_VERBOSE` | `0` | `1` = debug-level log output |

### Provider API keys

Store in `.mini-ork/config.env` — sourced by `lib/agent_registry.sh` before any model call. Never committed (gitignored).

```bash
# .mini-ork/config.env — gitignored, never commit
ANTHROPIC_API_KEY=sk-ant-...
DEEPSEEK_API_KEY=...
GLM_API_KEY=...
KIMI_API_KEY=...
```

For multi-key setups, place named env files in `.mini-ork/secrets/`:

```
.mini-ork/secrets/
  anthropic.env    # ANTHROPIC_API_KEY=...
  deepseek.env     # DEEPSEEK_API_KEY=...
```

Select at run time:

```bash
MINI_ORK_SECRETS_FILE=.mini-ork/secrets/anthropic.env mini-ork run code-fix kickoff.md
```

`secrets/` is gitignored. Environment variable precedence: shell env > `config.env` > `agents/*.yaml` defaults > built-in defaults.
