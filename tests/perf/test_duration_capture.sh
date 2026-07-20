#!/usr/bin/env bash
# Regression: a real execute dispatch writes a non-zero duration_ms trace row.
set -euo pipefail

MINI_ORK_ROOT_REAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if ! command -v gdate >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
  echo "MISSING_TIME_SHIM: install coreutils gdate or python3" >&2
  exit 127
fi

TMPROOT=$(mktemp -d /tmp/mini-ork-duration-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_RUN_ID="duration-capture-$$"
RUN_DIR="$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID"
mkdir -p "$RUN_DIR" "$MINI_ORK_HOME/config" "$TMPROOT/root/lib/providers" "$TMPROOT/root/prompts"

cat > "$MINI_ORK_HOME/config/agents.yaml" <<'YAML'
lanes:
  implementer: fixture
YAML

cat > "$MINI_ORK_HOME/config/providers.yaml" <<'YAML'
providers:
  fixture:
    kind: executable
    family: fixture
    script: lib/providers/cl_fixture.sh
YAML

ln -s "$MINI_ORK_ROOT_REAL/lib/llm-dispatch.sh" "$TMPROOT/root/lib/llm-dispatch.sh"
ln -s "$MINI_ORK_ROOT_REAL/lib/trace_store.sh" "$TMPROOT/root/lib/trace_store.sh"
ln -s "$MINI_ORK_ROOT_REAL/mini_ork" "$TMPROOT/root/mini_ork"

cat > "$TMPROOT/root/lib/providers/cl_fixture.sh" <<'SH'
#!/usr/bin/env bash
sleep 0.05
printf 'fixture dispatch complete\n'
SH
chmod +x "$TMPROOT/root/lib/providers/cl_fixture.sh"

cat > "$RUN_DIR/plan.json" <<'JSON'
{
  "task_class": "duration_capture",
  "decomposition": [
    {
      "id": "duration-fixture",
      "node_type": "implementer",
      "description": "Run fixture provider once",
      "depends_on": []
    }
  ],
  "dependencies": [],
  "artifact_contract": {"outputs": [], "success_verifiers": []},
  "verifier_contract": {"checks": []}
}
JSON

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT_REAL/tests/lib/setup_state_db.sh"
test_apply_migrations

before=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM execution_traces WHERE task_class='duration_capture' AND COALESCE(duration_ms,0)>0")

MINI_ORK_ROOT="$TMPROOT/root" \
MINI_ORK_PLAN_PATH="$RUN_DIR/plan.json" \
MINI_ORK_DRY_RUN=0 \
MO_TRACE_RICH=0 \
  "$MINI_ORK_ROOT_REAL/bin/mini-ork" execute --node-type implementer >/dev/null || {
    cat "$RUN_DIR/execute.log" >&2 2>/dev/null || true
    exit 1
  }

after=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM execution_traces WHERE task_class='duration_capture' AND COALESCE(duration_ms,0)>0")

if [ "$after" -le "$before" ]; then
  echo "FAIL: duration_ms still empty after dispatch (before=$before after=$after)" >&2
  exit 1
fi

echo "PASS: duration_ms captured (before=$before after=$after)"
