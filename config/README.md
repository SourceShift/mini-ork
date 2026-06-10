# mini-ork Config Reference

## Directory layout

```
config/
  agents.yaml                  # Lane assignments + global budget caps
  agents/                      # Per-model configs (role, budget, capabilities, fallback)
    sonnet.yaml
    opus.yaml
    deepseek.yaml
    glm.yaml
    kimi.yaml
    minimax.yaml
    human.yaml
  providers.yaml               # BYO provider registry (use your own API keys/endpoints)
  secrets.example.sh           # Template → copy to .mini-ork/config/secrets.local.sh
  scope-patterns.yaml.example  # Template for per-project scope guards
  README.md                    # This file
```

---

## `agents.yaml` — lane assignments

Defines which model runs which orchestrator role:

```yaml
lanes:
  worker_default: sonnet      # Default worker for most epics
  reviewer_default: opus      # Default reviewer; gates PR merge
  decomposer: deepseek        # Epic decomposition (large context)
  healer: opus                # Self-correction on repeated REQUEST_CHANGES
  brain: opus                 # Orchestrator supervisor decisions
  spec_author: sonnet         # BDD spec writing
  spec_reviewer: opus         # BDD spec review
  bdd_runner: sonnet          # Playwright BDD execution
budget:
  per_epic_usd: 5.00          # Hard cap per sub-epic
  per_run_usd: 0.50           # Hard cap per orchestrator dispatch turn
  daily_cap_usd: 50.00        # Hard cap across all epics for the day
```

Override per-project: copy `agents.yaml` to `$MINI_ORK_HOME/config/agents.yaml` and edit.

---

## Per-model agent configs (`agents/*.yaml`)

Each file defines one model's identity, capabilities, and budget:

```yaml
id: glm
display: "GLM 5.1"
env_script: "${AGENT_SCRIPTS_DIR:-$HOME/ps/scripts}/cl_glm.sh"
model_arg: ""
budget_usd: null           # null = flat-plan/uncapped
role: worker
escalate_only: false
capabilities:
  - refactor
  - security
  - precision
tier:
  speed: 1                 # 1=slow, 4=fast
  precision: 4             # 1=low, 4=high
  max_context_tokens: 256000
fallback_above: deepseek   # Escalate UP on repeated failures
fallback_below: kimi       # (documented, not used at runtime)
```

### `env_script`

Shell script sourced before launching `claude`. Sets provider-specific env vars
(API base URL, auth token). Anthropic-native models (sonnet/opus) leave this
empty — they read `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` from the
parent process.

Convention: scripts live at `$AGENT_SCRIPTS_DIR` (default `$HOME/ps/scripts/`):
- `cl_glm.sh` — sets env for Zhipu GLM
- `cl_kimi.sh` — sets env for Moonshot Kimi
- `cl_deepseek.sh` — sets env for DeepSeek
- `cl_minimax.sh` — sets env for MiniMax

### `capabilities`

Free-form tags used by capability-driven dispatch. The orchestrator matches an
epic's `required_capabilities` against this set when picking a worker.

Standard tags defined across current configs:
`architecture`, `reviewer`, `large-context`, `judgment`, `test-mandatory`,
`observability`, `refactor`, `infra`, `contracts`, `security`, `precision`,
`multi-file`, `planning`, `reasoning`, `whole-tree-analysis`, `llm`,
`boilerplate`, `copy`, `bulletproof-spec`, `escalation`, `decision-spike`

### `fallback_above`

When a worker's diff receives REQUEST_CHANGES twice in a row, the orchestrator
escalates to `fallback_above`. This is always a higher-precision (usually slower,
more expensive) model. The chain always terminates at `opus` (fallback_above: null).

---

## `scope-patterns.yaml.example`

Defines which file globs each lane may modify. Prevents a backend worker from
accidentally editing frontend files (or vice versa). Copy to
`$MINI_ORK_HOME/config/scope-patterns.yaml` and populate for your project:

```yaml
lanes:
  frontend:
    allow: ["src/**/*.tsx", "src/**/*.ts", "tests/e2e/**"]
    deny: ["server/**", "db/**"]
  backend:
    allow: ["server/**/*.ts", "server/tests/**"]
    deny: ["src/**", "public/**"]
```

The orchestrator enforces scope-patterns by passing `--disallowedTools` or by
post-validating diffs before merge.

---

## Provider env wiring

Each model's `env_script` is responsible for setting:

| Var | Purpose |
|-----|---------|
| `ANTHROPIC_BASE_URL` | Override API base for non-Anthropic providers |
| `ANTHROPIC_API_KEY` | Auth key for the provider |
| `CLAUDE_CODE_EFFORT_LEVEL` | `max` for reasoning-heavy models (DeepSeek) |

The `model_arg` field (e.g., `--model claude-sonnet-4-6`) is passed verbatim to
`claude` CLI. Leave empty for providers that don't use Anthropic model IDs — the
`env_script` routes to the right model via `ANTHROPIC_BASE_URL`.

---

## BYO providers (`providers.yaml`)

The fastest way to use your own API keys — no wrapper script required.
Declare the endpoint in `config/providers.yaml`, put the key in
`$MINI_ORK_HOME/config/secrets.local.sh` (copy `config/secrets.example.sh`),
and point a lane at the entry:

```yaml
# config/providers.yaml
providers:
  openai_api:
    kind: openai-compat        # routed through the codex CLI
    family: openai
    model: gpt-5.2-codex
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY   # NAME of the env var, never the key itself
```

```yaml
# config/agents.yaml
lanes:
  implementer: openai_api
```

Kinds: `anthropic-native` (claude CLI + your `ANTHROPIC_API_KEY` or ambient
login), `anthropic-compat` (claude CLI → gateway via `base_url`),
`openai-compat` (codex CLI → any /v1 endpoint), `executable` (your own
script). Precedence: a `lib/providers/cl_<name>.sh` wrapper always wins over
a registry entry of the same name. Full schema + examples in
`config/providers.yaml`; regression suite: `tests/test_provider_registry.sh`.

---

## Adding a new provider

For most cases, add a `providers.yaml` entry (above) — done. Write a wrapper
only when the provider needs custom CLI invocation or env logic:

1. Add a wrapper at `lib/providers/cl_<model-id>.sh` (sourceable env-export
   for Anthropic-compatible endpoints, or executable honoring
   `<script> --print --output-format text "<prompt>"`).
2. Create `agents/<model-id>.yaml` following the schema above.
3. Add the model to `agents.yaml` lanes if you want it as a default.
4. Add any new capability tags to this README.
