# ContextNest LLM Cache Integration

> Route mini-ork's OpenAI-shape providers (codex, openai_api, deepseek, glm, kimi, minimax) through ContextNest's v0.3 self-caching LLM proxy for semantic-cache dedup. Cost + latency drop without any code change beyond pointing `base_url` at the local CN substrate.
>
> **Status:** OpenAI-compat support is live. Anthropic-compat endpoint is pending — claude / sonnet / opus dispatches don't benefit yet.

## What this gives you

ContextNest's v0.3 proxy at `http://127.0.0.1:28080/llm/v1/*` is a drop-in replacement for the OpenAI HTTP base URL. Every request goes through a semantic-cache layer backed by the CN substrate:

1. **First call** — proxies to upstream provider, stores `(prompt, completion)` as a memory attractor.
2. **Subsequent semantically-similar calls** — hit the cache from the substrate; bypass the upstream entirely.
3. **Telemetry** — every hit/miss + cost-saved is tracked at `GET /llm/v1/cache/stats`.

For mini-ork specifically, this matters most for:

- **Repeated rubric-prescreen / spec-author / spec-reviewer LLM calls** with similar inputs across iterations.
- **Decomposer + planner calls** on the same kickoff during re-dispatches.
- **Lens dispatches** on shared bottleneck scans during recursive-self-improve.

Expected hit-rate after ~20 dispatches on the same kickoff family: 30-60% per the v0.3 roadmap projections.

## How to enable (per-dispatch env override)

Add to `$MINI_ORK_HOME/config/secrets.local.sh` (or wherever you source it from):

```bash
# Route OpenAI-shape providers through ContextNest cache when available.
# Falls back to direct upstream when CN is down (curl -m 1 health check).
if curl -s -m 1 -o /dev/null -w '%{http_code}' http://127.0.0.1:28080/llm/v1/cache/stats 2>/dev/null | grep -q '^2'; then
  export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://127.0.0.1:28080/llm/v1}"
  export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-http://127.0.0.1:28080/llm/v1}"
  export MINIMAX_BASE_URL="${MINIMAX_BASE_URL:-http://127.0.0.1:28080/llm/v1}"
  # GLM uses ANTHROPIC_BASE_URL via cl_glm.sh — needs CN Anthropic-compat (pending)
  # Kimi similarly uses ANTHROPIC_BASE_URL — pending
fi
```

The `${VAR:-default}` form respects an existing override (so you can disable on a per-shell basis by setting `OPENAI_BASE_URL=` before dispatch).

## How to enable (per-provider override in providers.yaml)

For a single provider, you can also add a dedicated entry that always uses CN:

```yaml
providers:
  openai_cached:
    kind: openai-compat
    family: openai
    model: gpt-5.2-codex
    base_url: http://127.0.0.1:28080/llm/v1
    api_key_env: OPENAI_API_KEY  # CN proxies the upstream key
```

Then reference `openai_cached` in `agents.yaml` lane definitions instead of `openai_api`.

## Measuring the impact

After a few dispatches:

```bash
curl -s http://127.0.0.1:28080/llm/v1/cache/stats | jq
```

Expected fields: `total_entries`, `total_hits`, `total_misses`, `hit_rate`, `cost_saved_usd` (when CN's cost-tracking is configured).

Compare against your mini-ork dispatch telemetry:

```bash
sqlite3 .mini-ork/state.db "
  SELECT family, COUNT(*) AS calls, ROUND(SUM(cost_usd), 2) AS total_cost
  FROM llm_calls
  WHERE ts > strftime('%s','now') - 86400
  GROUP BY family ORDER BY total_cost DESC;
"
```

If your `openai` family `total_cost` drops noticeably across re-dispatches of the same kickoff while CN's `hit_rate` rises, the integration is working.

## What's NOT covered yet

- **Anthropic-compat providers** (claude CLI, codex via claude wrapper, glm/kimi/minimax via cl_*.sh which set `ANTHROPIC_BASE_URL`) need a CN Anthropic-compat endpoint at `POST /anthropic/v1/messages`. That's tracked as a v0.3 follow-up.
- **Multi-tenancy** (CN project keys per ContextNest project): mini-ork dispatches today don't carry a project_id; falls back to CN's default project. Per the v0.3 roadmap §"User-facing surface" — implement when CN ships the project-key auth header.
- **Sandbox-side LLM calls** (Daytona-spawned worker calls Claude/codex from inside the sandbox): the sandbox has its own network path; CN at `127.0.0.1:28080` is only reachable from the host. Route via the host's Tailscale IP when CN exposes the integration.

## Composes with

- ContextNest v0.3 LLM proxy roadmap: `~/ps/ContextNest/docs/roadmap/v0.3-llm-proxy.md`
- Mini-ork llm-dispatch: `lib/llm-dispatch.sh` (cost telemetry source)
- Per-spawn provider routing: `config/providers.yaml`
- Subagent-prefetch hook: `hooks/subagent-prefetch.sh` (companion CN integration for context, this PR is for LLM dispatch)
