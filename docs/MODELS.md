# Model Routing Reference

mini-ork selects models per role based on the task's reasoning requirements, context length, and cost sensitivity. All selections are overridable via env vars or `agents.yaml`.

## Routing Matrix

| Role | Default Model | Cost Tier | Context Needed | Why This Model |
|---|---|---|---|---|
| **decomposer** | `claude-opus-4` | high | full kickoff.md | Needs deep reasoning to decompose ambiguous specs into coherent, non-overlapping epics with correct complexity tags |
| **worker** | `claude-sonnet-4-5` | medium | epic context + codebase snippets | Best cost/quality for implementation. Handles multi-file diffs cleanly |
| **reviewer** | `claude-opus-4` | high | diff + kickoff constraints | Adversarial lens; must catch constraint violations the worker missed. Kimi-k2 acceptable for diffs > 64K tokens |
| **reviewer (long diff)** | `kimi-k2` | medium | 128K diff window | More cost-efficient than Opus for reviewing large diffs where breadth > depth |
| **spec-author** | `claude-sonnet-4-5` | medium | diff + acceptance criteria | BDD Gherkin generation is structured output; Sonnet produces clean feature files |
| **healer** | `claude-sonnet-4-5` | medium | epic context + correction | Same as worker; healer re-attempts with additional reviewer + BDD failure context |
| **hunter** | `glm-4` | low | file list + grep output | Fast, cheap. Used for structured analysis (bug scanning, perf hotspots) — not generation |
| **budget worker** | `deepseek-v3` | very low | epic context | ~10× cheaper than Sonnet; good for boilerplate-heavy epics (migrations, stubs, CRUD) |
| **escalation summary** | `claude-opus-4` | high | full iter trace | Escalation summaries need to be actionable; Opus produces higher-quality triage notes |

## Cost Estimates

All estimates assume average epic complexity (300–800 line diff, 3 iters max). Actual cost depends on kickoff size, codebase context injected, and iter count.

| Model | Input (per 1M tokens) | Output (per 1M tokens) | Typical cost / epic |
|---|---|---|---|
| `claude-opus-4` | $15.00 | $75.00 | $0.15 – $0.60 |
| `claude-sonnet-4-5` | $3.00 | $15.00 | $0.03 – $0.12 |
| `kimi-k2` | $0.60 | $2.50 | $0.02 – $0.08 |
| `glm-4` | $0.14 | $0.14 | $0.01 – $0.04 |
| `deepseek-v3` | $0.27 | $1.10 | $0.005 – $0.02 |

Costs from provider pricing pages as of 2026-05. Subject to change — verify before budgeting large runs.

## Switching Models

**Per-run (env):**
```bash
MINI_ORK_WORKER_MODEL=deepseek-v3 mini-ork deliver kickoff.md
```

**Per-repo (agents.yaml):**
```yaml
worker_model: claude-sonnet-4-5
reviewer_model: kimi-k2
decomposer_model: claude-opus-4
```

**Per-epic (agents.yaml):**
```yaml
epics:
  - name: boilerplate-crud
    model: deepseek-v3
  - name: security-audit
    model: claude-opus-4
```

Per-epic overrides take precedence over repo defaults and env vars.

## Adding a New Provider

1. Add a provider block to `lib/llm-dispatch.sh`:

```bash
dispatch_my_provider() {
  local model="$1" prompt_file="$2"
  # must write response to stdout, exit 0 on success, non-zero on error
  curl -s -X POST "https://api.myprovider.com/v1/chat" \
    -H "Authorization: Bearer ${MY_PROVIDER_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg m "$model" --rawfile p "$prompt_file" \
         '{model:$m, messages:[{role:"user",content:$p}]}')" \
  | jq -r '.choices[0].message.content'
}
```

2. Map the model prefix in `dispatch_model()`:
```bash
case "$model" in
  my-provider-*) dispatch_my_provider "$model" "$prompt_file" ;;
  ...
esac
```

3. Add the API key env var to `docs/CONFIG.md` and `.gitignore`.

No other changes needed. The orchestrator calls `dispatch_model` uniformly.
