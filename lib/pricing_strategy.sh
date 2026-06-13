#!/usr/bin/env bash
# pricing_strategy.sh — config-driven (provider, model) → rate lookup.
#
# Closes the roadmap's Agent-ops hardening Phase 2 item 6: pricing
# strategy table. Replaces the inline cost math in lib/llm-dispatch.sh
# (and downstream consumers) with a config-driven lookup so:
#   - Rates are version-controlled YAML, not buried in shell.
#   - Same lookup serves input / output / cache_read / cache_write.
#   - Pricing updates are config edits, not code edits.
#   - The framework never calls home; operators tune the YAML to
#     whatever rates the actual contract specifies.
#
# Public API:
#   pricing_lookup <provider> <model> <token_kind>
#       → emits the matching rate (USD per million tokens) on stdout.
#       Returns "0" + warns on stderr when:
#         - the (provider, model, token_kind) triplet is missing,
#         - the YAML cannot be parsed,
#         - python3 is unavailable.
#       Token kinds: input | output | cache_read | cache_write.
#
# Source of truth:
#   ${MINI_ORK_HOME:-.mini-ork}/config/pricing.yaml (override via
#   MO_PRICING_YAML). See that file for shape + commented rationale.
#
# Wiring into lib/llm-dispatch.sh and the downstream cache-aware
# cost accounting (P2.5) is a deliberate follow-up — this commit
# only adds the lookup primitive + the YAML so the substrate exists
# before the call sites change.

set -uo pipefail

pricing_lookup() {
  local _provider="${1:-}"
  local _model="${2:-}"
  local _kind="${3:-}"

  if [ -z "$_provider" ] || [ -z "$_model" ] || [ -z "$_kind" ]; then
    echo "pricing_lookup: usage: pricing_lookup <provider> <model> <token_kind>" >&2
    echo "0"
    return 0
  fi

  local _yaml="${MO_PRICING_YAML:-${MINI_ORK_HOME:-.mini-ork}/config/pricing.yaml}"
  if [ ! -f "$_yaml" ]; then
    echo "pricing_lookup: pricing.yaml not found at $_yaml; returning 0" >&2
    echo "0"
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "pricing_lookup: python3 unavailable; returning 0" >&2
    echo "0"
    return 0
  fi

  MO_PRICING_YAML_RESOLVED="$_yaml" \
  MO_PRICING_PROVIDER="$_provider" \
  MO_PRICING_MODEL="$_model" \
  MO_PRICING_KIND="$_kind" \
  python3 - <<'PY'
import os, sys

try:
    import yaml
except ImportError:
    sys.stderr.write("pricing_lookup: pyyaml unavailable; returning 0\n")
    print("0")
    sys.exit(0)

path = os.environ["MO_PRICING_YAML_RESOLVED"]
provider = os.environ["MO_PRICING_PROVIDER"]
model = os.environ["MO_PRICING_MODEL"]
kind = os.environ["MO_PRICING_KIND"]

try:
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
except Exception as exc:
    sys.stderr.write(f"pricing_lookup: parse error on {path}: {exc}\n")
    print("0")
    sys.exit(0)

# Allowed kinds — keep the surface tight so callers cannot fish for
# arbitrary YAML keys. Maintainers extending the schema (e.g. tiered
# strategies) update this list explicitly.
ALLOWED = {"input", "output", "cache_read", "cache_write"}
if kind not in ALLOWED:
    sys.stderr.write(f"pricing_lookup: unknown token_kind {kind!r}; allowed={sorted(ALLOWED)}\n")
    print("0")
    sys.exit(0)

table = data.get("pricing") if isinstance(data, dict) else None
if not isinstance(table, dict):
    sys.stderr.write("pricing_lookup: pricing.yaml missing top-level 'pricing' map; returning 0\n")
    print("0")
    sys.exit(0)

provider_block = table.get(provider) if isinstance(table, dict) else None
if not isinstance(provider_block, dict):
    sys.stderr.write(f"pricing_lookup: provider {provider!r} not in pricing table; returning 0\n")
    print("0")
    sys.exit(0)

model_block = provider_block.get(model)
if not isinstance(model_block, dict):
    sys.stderr.write(f"pricing_lookup: model {model!r} not in {provider!r} pricing; returning 0\n")
    print("0")
    sys.exit(0)

rate = model_block.get(kind)
if rate is None:
    # Cache columns are optional; absent rate = 0 (negotiated default).
    print("0")
    sys.exit(0)

try:
    # Preserve the YAML literal so "3.00" stays as "3.00" rather than
    # the float-repr "3.0" — operator-facing display matters.
    raw = model_block.get(kind)
    print(str(raw))
except Exception:
    print("0")
PY
}

# Self-test fixtures: hit / miss / missing-yaml. Pattern mirrors
# lib/krippendorff_alpha_gate.sh and lib/honest_ci_gate.sh.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT

  mkdir -p "$_selftest_dir/config"
  cat > "$_selftest_dir/config/pricing.yaml" <<'YAML'
# self-test pricing.yaml — config-driven sample for hit + miss fixtures.
pricing:
  anthropic:
    claude-sonnet-4-6:
      input:       3.00
      output:     15.00
      cache_read:  0.30
      cache_write: 3.75
  openai:
    gpt-5:
      input:       2.50
      output:     10.00
YAML

  echo "--- fixture 1: hit (anthropic / claude-sonnet-4-6 / input, expect 3.00) ---"
  MO_PRICING_YAML="$_selftest_dir/config/pricing.yaml" \
    pricing_lookup anthropic claude-sonnet-4-6 input

  echo "--- fixture 2: hit cache_read (expect 0.30) ---"
  MO_PRICING_YAML="$_selftest_dir/config/pricing.yaml" \
    pricing_lookup anthropic claude-sonnet-4-6 cache_read

  echo "--- fixture 3: miss provider (expect 0 + warn on stderr) ---"
  MO_PRICING_YAML="$_selftest_dir/config/pricing.yaml" \
    pricing_lookup unknown unknown input

  echo "--- fixture 4: miss kind for known model (cache_write absent, expect 0) ---"
  MO_PRICING_YAML="$_selftest_dir/config/pricing.yaml" \
    pricing_lookup openai gpt-5 cache_write

  echo "--- fixture 5: missing yaml (expect 0 + warn) ---"
  MO_PRICING_YAML="$_selftest_dir/does-not-exist.yaml" \
    pricing_lookup anthropic claude-sonnet-4-6 input
fi
