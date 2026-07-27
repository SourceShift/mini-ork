#!/usr/bin/env bash
# Integration coverage for the deterministic doc-to-features-loop dispatcher.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

TMPROOT="$(mktemp -d /tmp/mini-ork-doc-features-dispatcher-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT

PASS=0
FAIL=0
_ok() { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

FAKE_MINI_ORK="$TMPROOT/fake-mini-ork"
cat > "$FAKE_MINI_ORK" <<'SH'
#!/usr/bin/env bash
set -uo pipefail

recipe="$2"
kickoff="$3"
[ "$1" = "run" ] || exit 2
[ "$recipe" = "recursive-validate-impl" ] || exit 2
echo "child_run_id=$MINI_ORK_RUN_ID"
echo "child_run_dir=$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID"
mkdir -p "$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID"
if grep -q 'feature-fail' "$kickoff"; then
  cat > "$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID/panel-verdict.json" <<'JSON'
{"verdict":"REQUEST_CHANGES","reasons":["needs more tests"]}
JSON
  exit 1
fi
cat > "$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID/panel-verdict.json" <<'JSON'
{"verdict":"APPROVE","reasons":["done"]}
JSON
cat > "$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID/implementer-summary.json" <<'JSON'
{"touched_files":["src/demo.py"]}
JSON
SH
chmod +x "$FAKE_MINI_ORK"

run_dispatcher_case() {
  local name="$1"
  local feature_json="$2"
  local run_dir="$TMPROOT/$name/run"
  local home="$TMPROOT/$name/home"
  mkdir -p "$run_dir" "$home/runs"
  printf '%s\n' "$feature_json" > "$run_dir/feature-index.json"
  MINI_ORK_RUN_DIR="$run_dir" \
  MINI_ORK_HOME="$home" \
  MINI_ORK_RUN_ID="parent-$name" \
  MINI_ORK_PER_FEATURE_MINI_ORK_BIN="$FAKE_MINI_ORK" \
    python3 "$MINI_ORK_ROOT/recipes/doc-to-features-loop/lib/per_feature_dispatcher.py"
  return $?
}

echo "── integration: doc-to-features-loop per-feature dispatcher ──"

FEATURES_PASS='{
  "schema_version": "1.0",
  "source_kickoff": "/tmp/source.md",
  "features": [
    {
      "id": "feature-pass",
      "title": "Feature Pass",
      "priority": "P0",
      "source_evidence": ["Section 1"],
      "dependencies": [],
      "modern_techniques_refs": [{"source":"arxiv-search-tool","title":"Technique","why_relevant":"testing"}],
      "rationale": "Implement pass."
    },
    {
      "id": "feature-later",
      "title": "Feature Later",
      "priority": "P1",
      "source_evidence": [],
      "dependencies": [],
      "modern_techniques_refs": [],
      "rationale": "Do not dispatch."
    }
  ]
}'
if run_dispatcher_case pass "$FEATURES_PASS"; then
  _ok "dispatcher exits 0 when all P0 child verdicts pass"
else
  _fail "dispatcher should pass all-green P0 set"
fi

python3 - "$TMPROOT/pass/run" <<'PY'
import json, pathlib, sys
run = pathlib.Path(sys.argv[1])
records = sorted(
    p.name
    for p in (run / "child-runs").glob("*.json")
    if not p.name.startswith("_") and not p.name.endswith(".verdict.json")
)
assert records == ["feature-pass.json"], records
rec = json.load(open(run / "child-runs" / "feature-pass.json"))
assert rec["feature_id"] == "feature-pass"
assert rec["status"] == "passed"
assert rec["child_run_id"] == "parent-pass-feature-pass"
assert rec["verdict_path"].endswith("feature-pass.verdict.json")
verdict = json.load(open(rec["verdict_path"]))
assert verdict["pass"] is True
kickoff = pathlib.Path(rec["child_kickoff"]).read_text()
assert "Preserve unrelated user changes" in kickoff
assert "arxiv-search-tool Modern Technique References" in kickoff
PY
if [ "$?" -eq 0 ]; then
  _ok "dispatcher writes one P0 record, kickoff, and normalized pass verdict"
else
  _fail "passed dispatcher artifacts invalid"
fi

if MINI_ORK_RUN_DIR="$TMPROOT/pass/run" \
  python3 "$MINI_ORK_ROOT/recipes/doc-to-features-loop/verifiers/per-feature-dispatch-results.py" >/tmp/doc-features-verifier-pass.log; then
  _ok "aggregate verifier accepts passed child dispatches"
else
  cat /tmp/doc-features-verifier-pass.log
  _fail "aggregate verifier should accept passed child dispatches"
fi

FEATURES_FAIL='{
  "features": [
    {
      "id": "feature-fail",
      "title": "Feature Fail",
      "priority": "P0",
      "source_evidence": ["Section 2"],
      "dependencies": ["feature-pass"],
      "modern_techniques_refs": [],
      "rationale": "Implement fail."
    }
  ]
}'
if run_dispatcher_case fail "$FEATURES_FAIL"; then
  _fail "dispatcher should fail when a P0 child verdict does not pass"
else
  _ok "dispatcher exits non-zero on failed P0 child verdict"
fi

python3 - "$TMPROOT/fail/run" <<'PY'
import json, pathlib, sys
run = pathlib.Path(sys.argv[1])
rec = json.load(open(run / "child-runs" / "feature-fail.json"))
assert rec["status"] == "failed"
assert rec["source_verdict_path"].endswith("panel-verdict.json")
verdict = json.load(open(rec["verdict_path"]))
assert verdict["pass"] is False
summary = json.load(open(run / "child-runs" / "_summary.json"))
assert summary["total"] == 1
assert summary["failed"] == 1
PY
if [ "$?" -eq 0 ]; then
  _ok "failed child verdict is normalized and summarized"
else
  _fail "failed dispatcher artifacts invalid"
fi

python3 -m py_compile "$MINI_ORK_ROOT/recipes/doc-to-features-loop/lib/per_feature_dispatcher.py"
if [ "$?" -eq 0 ]; then
  _ok "dispatcher script compiles"
else
  _fail "dispatcher script should compile"
fi

echo
echo "Result: $PASS OK / $FAIL FAIL"
[ "$FAIL" -eq 0 ]
