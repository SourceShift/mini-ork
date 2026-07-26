# Provider triage — when `readme-drift-providers-doctor.sh` flags providers down

The 4-lens panel (Layer 2b of README drift detection) requires at least
`MO_DRIFT_MIN_RESPONSIVE_LENSES` (default 2) of its 4 providers to respond
within `MO_DRIFT_PROBE_TIMEOUT_SEC` (default 20 sec). The
`scripts/readme-drift-providers-doctor.sh` pre-flight probe surfaces
non-responsive providers in <30 sec before the panel burns 4 × 90 sec
on guaranteed-empty calls.

This guide documents the triage path when the doctor returns
`viable=false`.

## Step 0 — Diagnose

```bash
bash scripts/readme-drift-providers-doctor.sh | jq .
```

Read the per-provider JSON. Each provider has:

- `rc=124` → timeout (gateway unreachable OR network slow)
- `rc=0 but empty stdout` → silent fail (key likely expired)
- `rc=0 but stdout contains error string` → wrapper-side mismatch (stdin
  handling, model name, etc — typically codex)
- `rc=0 + non-empty stdout + no error` → responsive ✓

Today's 2026-06-05 baseline (after live-smoke session): **only `opus`
fully responsive**; `kimi`, `glm`, `minimax` all timeout; `codex` rc=0
but stderr contains `Reading additional input from stdin... codex_c
ERROR`.

## Step 1 — Check and configure API keys

Use the native command first. It resolves workflow aliases, validates the
local store permissions, and never prints a credential:

```bash
mini-ork providers status --workflow recipes/refactor-audit/workflow.yaml
mini-ork providers configure minimax
mini-ork providers configure --workflow recipes/refactor-audit/workflow.yaml
```

For scripted terminal provisioning, use a hidden shell read and pass only a
`NAME=value` line over standard input, never a command-line flag:

```bash
read -r -s -p 'MiniMax API key: ' MINIMAX_API_KEY; echo
printf 'MINIMAX_API_KEY=%s\n' "$MINIMAX_API_KEY" \
  | mini-ork providers configure --from-stdin minimax
unset MINIMAX_API_KEY
```

CI should instead inject a masked secret through its standard-input or secret
manager integration; do not place a literal key in a workflow command.

The command stores values in `$MINI_ORK_HOME/config/secrets.local.sh` with
owner-only permissions. An exported shell value takes precedence for a single
run, which makes rotations and one-off smoke tests safe.

For legacy Bash-only checks, some providers read from env vars set in
`.mini-ork/config/secrets.local.sh` (or `~/.config/mini-ork/secrets.local.sh`).
The old manual presence check is below; it is not needed for normal setup.

To check WITHOUT printing the key values manually:

```bash
(
  source .mini-ork/config/secrets.local.sh 2>/dev/null
  for k in MINIMAX_API_KEY GLM_API_KEY KIMI_API_KEY DEEPSEEK_API_KEY; do
    v="${!k}"
    [ -n "$v" ] && printf "  %-20s len=%d ✓\n" "$k" "${#v}" \
                || printf "  %-20s NOT SET\n" "$k"
  done
)
```

Expected: all 4 keys non-empty. If any is empty → set it in the secrets
file (the wrapper's header comment includes the example: e.g.
`export MINIMAX_API_KEY=sk-cp-...`).

## Step 2 — Verify gateway URLs are current

Each `lib/providers/cl_<name>.sh` pins a gateway endpoint. Providers
occasionally migrate to new subdomains; check the canonical URL against
the provider's current docs:

| Provider | Wrapper var          | Default URL (2026-06)              | Canonical docs to verify against |
|---|---|---|---|
| minimax  | `ANTHROPIC_BASE_URL` | `https://api.minimax.io/anthropic` | https://www.minimaxi.com/en/document/anthropic |
| glm      | `ANTHROPIC_BASE_URL` | `https://open.bigmodel.cn/api/anthropic` | https://docs.bigmodel.cn/api-reference |
| kimi     | `ANTHROPIC_BASE_URL` | `https://api.moonshot.cn/anthropic`     | https://platform.moonshot.cn/docs/api-reference |
| deepseek | `ANTHROPIC_BASE_URL` | `https://api.deepseek.com/anthropic` | https://api-docs.deepseek.com/api/deepseek-api |
| codex    | n/a (executable)     | reads `~/.codex/config.toml`       | `codex login` to re-auth         |
| opus     | (uses Claude default)| Anthropic's main API               | Anthropic console                |

If a URL has changed: edit the relevant `lib/providers/cl_<name>.sh`,
then re-run the doctor to confirm.

## Step 3 — Quick smoke each provider individually

Direct test, isolates the wrapper from the panel infra:

```bash
# Sourceable providers (kimi / glm / minimax / opus / deepseek):
(
  source .mini-ork/config/secrets.local.sh 2>/dev/null
  source lib/providers/cl_minimax.sh 2>/dev/null
  timeout 30 claude --print "Say OK only." < /dev/null
)
echo "rc=$?"

# Executable providers (codex):
timeout 30 lib/providers/cl_codex.sh --print --output-format text \
  "Say OK only." < /dev/null
echo "rc=$?"
```

If a sourceable provider works in isolation but fails inside the panel:
- Check the wrapper isn't being re-sourced with stale env (subshell
  isolation should prevent this, but inspect for `unset` calls).
- Check `MO_DRIFT_PROBE_TIMEOUT_SEC` — bump to 30+ if the provider is
  slow but eventually responds.

## Step 4 — Common fixes

### Expired key (rc=0 silent fail)

Rotate the key in the provider's web console:
- MiniMax: https://www.minimaxi.com → API Keys
- Zhipu (GLM): https://open.bigmodel.cn → API Keys
- Moonshot (Kimi): https://platform.moonshot.cn → API Keys
- DeepSeek: https://platform.deepseek.com → API Keys

Update `.mini-ork/config/secrets.local.sh` with the new value. Re-run
doctor.

### Gateway URL changed (rc=124 timeout)

DNS resolves but the endpoint moved. Check the provider's current docs
(table above) and update `ANTHROPIC_BASE_URL` in
`lib/providers/cl_<name>.sh`. Commit the wrapper change so other clones
get the same fix.

### Codex stdin-handling quirk (rc=0 + "Reading additional input from
stdin..." error)

The `cl_codex.sh` executable wrapper calls `codex exec` which behaves
differently from `claude --print`. The wrapper needs to handle the
stdin contract specifically. This is a separate code fix in
`lib/providers/cl_codex.sh` — not a key rotation.

## Step 5 — Operate with a degraded panel

If you can only restore 2-3 of the 4 lens providers:

```bash
export MO_DRIFT_MIN_RESPONSIVE_LENSES=2   # allow 2-lens panel
```

The panel works correctly with fewer lenses, the heterogeneity claim
just gets weaker. Document the degraded state in the next commit
message so the reduced family-diversity isn't forgotten.

If providers are persistently down and you want pushes to proceed
anyway without the panel check:

```bash
export MO_README_PANEL_INDETERMINATE=fail-open   # default behavior
# OR
export MO_README_PANEL_SKIP=1   # skip panel entirely (L1 still runs)
```

To make outages BLOCK pushes (strict mode):

```bash
export MO_README_PANEL_INDETERMINATE=block
```

## Step 6 — Permanent fix: prompt-version + key-rotation cron

If keys keep expiring (common with 30-day trial tiers), set up a
monthly rotation reminder. The current secrets file structure is
manual — there's no automated key-rotation pipeline yet.

For longer-term reliability: pin to providers with persistent API
keys (Anthropic, OpenAI), accept the family-diversity loss, and use
the heterogeneous-by-design panel only on critical pre-release
audits where the operator is hands-on.

## Logging the triage outcome

After fixing, append a one-line entry to this doc's history:

```
## Triage history

- 2026-06-05 — codex (cl_codex.sh stdin quirk), kimi/glm/minimax
  (all key timeouts) — fixed via [...]
```

So future triage knows what's been tried.

## Triage history

- 2026-06-05 — Baseline doctor probe showed 0/4 lenses responsive.
  codex_lens emitted `Reading additional input from stdin... codex_c
  ERROR` (wrapper stdin-handling quirk); kimi/glm/minimax all
  timed out at 20s. Filed as a follow-up for key rotation /
  endpoint verification + a cl_codex.sh stdin fix. Drift detection
  currently runs L1 + L2a (gatekeeper) only; L2b panel skipped via
  fail-open because the doctor blocks dispatch.
