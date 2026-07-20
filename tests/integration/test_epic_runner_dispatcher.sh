#!/usr/bin/env bash
# Integration coverage for the deterministic epic-runner dispatcher.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

TMPROOT="$(mktemp -d /tmp/mini-ork-epic-dispatcher-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT

PASS=0
FAIL=0
_ok() { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

FAKE_SPAWN="$TMPROOT/fake-mini-ork-spawn"
cat > "$FAKE_SPAWN" <<'SH'
#!/usr/bin/env bash
set -uo pipefail

parent=""
kickoff=""
child=""
smoke=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --parent-run) parent="$2"; shift 2 ;;
    --kickoff) kickoff="$2"; shift 2 ;;
    --recipe) shift 2 ;;
    --child-run) child="$2"; shift 2 ;;
    --smoke-shape) smoke=1; shift ;;
    *) shift ;;
  esac
done

# The dispatcher gates publish via MINI_ORK_EPIC_PUBLISH env instead of a
# --smoke-shape flag on the spawn binary. Reflect that in the fake spawn log
# so the integration test still verifies smoke-shape child invocations.
if [ "${MINI_ORK_EPIC_PUBLISH:-false}" != "true" ]; then
  smoke=1
fi

echo "$child smoke=$smoke kickoff=$kickoff" >> "$MINI_ORK_HOME/fake-spawn.log"
child_dir="$MINI_ORK_HOME/runs/$child"
mkdir -p "$child_dir"
printf 'diff --git a/demo b/demo\n' > "$child_dir/framework-edit.diff"
if grep -q 'FAIL_EPIC' "$kickoff"; then
  cat > "$child_dir/verdict.json" <<'JSON'
{"pass": false, "files_written": ["demo"]}
JSON
  echo "child_run_id=$child"
  echo "child_run_dir=$child_dir"
  echo "spawn_status=failed"
  exit 1
fi
cat > "$child_dir/verdict.json" <<'JSON'
{"pass": true, "files_written": ["demo"]}
JSON
echo "spawn_id=fake"
echo "parent_run_id=$parent"
echo "child_run_id=$child"
echo "child_run_dir=$child_dir"
echo "spawn_status=completed"
SH
chmod +x "$FAKE_SPAWN"

run_dispatcher_case() {
  local name="$1"
  local plan_json="$2"
  local run_dir="$TMPROOT/$name/run"
  local home="$TMPROOT/$name/home"
  mkdir -p "$run_dir" "$home/runs"
  printf '%s\n' "$plan_json" > "$run_dir/epic-runner-plan.json"
  MINI_ORK_RUN_DIR="$run_dir" \
  MINI_ORK_HOME="$home" \
  MINI_ORK_RUN_ID="parent-$name" \
  MINI_ORK_EPIC_PUBLISH=false \
  MINI_ORK_EPIC_SPAWN_BIN="$FAKE_SPAWN" \
    python3 "$MINI_ORK_ROOT/recipes/epic-runner/lib/epic_dispatcher.py"
  return $?
}

run_aggregator_case() {
  local name="$1"
  local plan_json="$2"
  local results_json="$3"
  local run_dir="$TMPROOT/$name/run"
  mkdir -p "$run_dir"
  printf '%s\n' "$plan_json" > "$run_dir/epic-runner-plan.json"
  printf '%s\n' "$results_json" > "$run_dir/epic-results.json"
  MINI_ORK_RUN_DIR="$run_dir" \
    python3 "$MINI_ORK_ROOT/recipes/epic-runner/lib/wave_aggregator.py"
  return $?
}

echo "── integration: epic-runner dispatcher ──"

PLAN_PASS='{
  "epics": [
    {"id": "a", "depends_on": [], "framework_edit_kickoff": "# A"},
    {"id": "b", "depends_on": [], "framework_edit_kickoff": "# B"}
  ],
  "waves": [["a", "b"]]
}'
if run_dispatcher_case pass "$PLAN_PASS"; then
  _ok "dispatcher exits 0 when all epics pass"
else
  _fail "dispatcher should pass all-green wave"
fi

RESULTS="$TMPROOT/pass/run/epic-results.json"
python3 - "$RESULTS" "$TMPROOT/pass/home/fake-spawn.log" <<'PY'
import json, sys
results = json.load(open(sys.argv[1]))
log = open(sys.argv[2]).read()
assert results["waves_completed"] == 1
assert results["waves_total"] == 1
assert [e["status"] for e in results["epics"]] == ["passed", "passed"]
assert log.count("smoke=1") == 2
assert all(e["child_run_id"] for e in results["epics"])
PY
if [ "$?" -eq 0 ]; then
  _ok "results schema records passed epics and smoke-shape child invocations"
else
  _fail "passed results schema invalid"
fi

PLAN_FAIL='{
  "epics": [
    {"id": "a", "depends_on": [], "framework_edit_kickoff": "# FAIL_EPIC"},
    {"id": "b", "depends_on": ["a"], "framework_edit_kickoff": "# B"},
    {"id": "c", "depends_on": ["b"], "framework_edit_kickoff": "# C"}
  ],
  "waves": [["a"], ["b"], ["c"]]
}'
if run_dispatcher_case fail "$PLAN_FAIL"; then
  _fail "dispatcher should fail when a wave epic fails"
else
  _ok "dispatcher exits non-zero on failed wave"
fi

RESULTS="$TMPROOT/fail/run/epic-results.json"
python3 - "$RESULTS" "$TMPROOT/fail/home/fake-spawn.log" <<'PY'
import json, sys
results = json.load(open(sys.argv[1]))
log = open(sys.argv[2]).read()
statuses = {e["id"]: e["status"] for e in results["epics"]}
assert statuses == {"a": "failed", "b": "skipped", "c": "skipped"}, statuses
assert results["waves_completed"] == 1
assert "parent-fail-a" in log
assert "parent-fail-b" not in log
assert "parent-fail-c" not in log
PY
if [ "$?" -eq 0 ]; then
  _ok "failed wave skips direct and transitive downstream epics"
else
  _fail "failed wave skip semantics invalid"
fi

AGG_PLAN='{
  "epics": [
    {"id": "a", "depends_on": []},
    {"id": "b", "depends_on": ["a"]},
    {"id": "c", "depends_on": ["b"]}
  ],
  "waves": [["a"], ["b"], ["c"]]
}'
AGG_RESULTS='{
  "verdict": "in_progress",
  "waves_completed": 1,
  "waves_total": 3,
  "epics": [
    {"id": "a", "status": "failed", "final_artifact_ref": "", "files_written": []},
    {"id": "b", "status": "skipped", "final_artifact_ref": "", "files_written": []},
    {"id": "c", "status": "skipped", "final_artifact_ref": "", "files_written": []}
  ]
}'
if run_aggregator_case aggregate_skip "$AGG_PLAN" "$AGG_RESULTS"; then
  _ok "wave aggregator exits 0 for dependency-respecting failed/skipped chain"
else
  _fail "wave aggregator should accept dependency-respecting failed/skipped chain"
fi

AGGREGATE="$TMPROOT/aggregate_skip/run/wave-aggregate.json"
python3 - "$AGGREGATE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["verdict"] == "in_progress"
assert d["aggregate"] == {
    "epics_total": 3,
    "epics_passed": 0,
    "epics_failed": 1,
    "epics_skipped": 2,
    "waves_total": 3,
    "waves_completed": 0,
    "dependency_respected": True,
}
assert [w["first_failure"] for w in d["per_wave"]] == ["a", "b", "c"]
assert [f["status"] for f in d["findings"]] == ["failed", "skipped", "skipped"]
PY
if [ "$?" -eq 0 ]; then
  _ok "wave aggregate schema captures counts, first failures, and skipped chain"
else
  _fail "wave aggregate schema invalid for skipped chain"
fi

AGG_BAD_RESULTS='{
  "verdict": "in_progress",
  "waves_completed": 2,
  "waves_total": 2,
  "epics": [
    {"id": "a", "status": "failed", "final_artifact_ref": "", "files_written": []},
    {"id": "b", "status": "passed", "final_artifact_ref": "artifact-b", "files_written": ["b.txt"]},
    {"id": "c", "status": "passed", "final_artifact_ref": "artifact-c", "files_written": ["c.txt"]}
  ]
}'
if run_aggregator_case aggregate_dep_violation "$AGG_PLAN" "$AGG_BAD_RESULTS"; then
  _fail "wave aggregator should fail on dependency-violating passed epic"
else
  _ok "wave aggregator exits non-zero on dependency-violating passed epic"
fi

AGGREGATE="$TMPROOT/aggregate_dep_violation/run/wave-aggregate.json"
python3 - "$AGGREGATE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["aggregate"]["dependency_respected"] is False
assert d["aggregate"]["epics_passed"] == 0
assert d["aggregate"]["epics_failed"] == 3
assert [f["status"] for f in d["findings"]] == ["failed", "failed", "failed"]
PY
if [ "$?" -eq 0 ]; then
  _ok "wave aggregator blocks invalid passed epics from counted pass totals"
else
  _fail "dependency violation aggregate invalid"
fi

if bash -n "$MINI_ORK_ROOT/bin/mini-ork-spawn" \
  && python3 -m py_compile "$MINI_ORK_ROOT/mini_ork/cli/execute.py" \
  && python3 -m py_compile "$MINI_ORK_ROOT/recipes/epic-runner/lib/epic_dispatcher.py" \
    "$MINI_ORK_ROOT/recipes/epic-runner/lib/wave_aggregator.py"; then
  _ok "modified shell/Python entrypoints pass syntax checks"
else
  _fail "modified shell/Python entrypoints failed syntax checks"
fi

echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
