# recipes/framework-edit/verifiers/_verdict_merge.sh
#
# Shared helper sourced by static-check.sh and test.sh to maintain
# $RUN_DIR/verdict.json with the schema declared in artifact_contract.yaml:
#
#   {
#     "files_changed": <int>,
#     "tests_pass":    <bool>,
#     "static_pass":   <bool>,
#     "pass":          <bool>     # tests_pass && static_pass (missing -> false)
#   }
#
# Usage (from a verifier):
#
#   source "$(dirname "${BASH_SOURCE[0]}")/_verdict_merge.sh"
#   ...
#   write_verdict "<static_pass_bool>" "<files_changed_int>"   # static-check
#   write_verdict "" "" "<tests_pass_bool>"                    # test.sh
#
# The helper:
#   * tolerates either verifier running first (reads existing or {}),
#   * merges only the keys it's been told about (never wipes peer keys),
#   * writes atomically via mktemp + mv (POSIX-atomic on same FS),
#   * recomputes pass = bool(tests_pass) && bool(static_pass) with missing
#     keys defaulted to false (defensive default keeps schema stable).
#
# Serial dispatch is the current contract (workflow.yaml runs verifiers
# serially); the atomic write is cheap insurance against future parallelization.

# write_verdict <static_pass> [files_changed_count] [tests_pass]
write_verdict() {
  local static_pass="${1:-}"
  local files_changed_arg="${2:-}"
  local tests_pass="${3:-}"

  local verdict_path="$RUN_DIR/verdict.json"

  # Pre-create with empty object so the python merge always has input.
  [ -f "$verdict_path" ] || printf '{}' > "$verdict_path"

  local tmp
  tmp="$(mktemp "$RUN_DIR/.verdict.json.XXXXXX")"

  python3 - "$verdict_path" "$tmp" "$static_pass" "$files_changed_arg" "$tests_pass" <<'PY'
import json, sys, os

src, dst, static_pass, files_changed_arg, tests_pass = sys.argv[1:6]

try:
    with open(src) as f:
        cur = json.load(f)
    if not isinstance(cur, dict):
        cur = {}
except Exception:
    cur = {}

def _merge_bool(name, raw):
    if raw == "" or raw is None:
        # Caller didn't pass this key. If the existing verdict.json
        # already has it (peer verifier wrote earlier), keep that
        # value; otherwise default to false (defensive — schema
        # contract guarantees all four keys present).
        cur.setdefault(name, False)
        return
    if isinstance(raw, str):
        v = raw.strip().lower() in ("1", "true", "yes", "y", "t")
    else:
        v = bool(raw)
    cur[name] = v

def _merge_int(name, raw):
    if raw == "" or raw is None:
        cur.setdefault(name, 0)
        return
    try:
        cur[name] = int(raw)
    except (TypeError, ValueError):
        cur.setdefault(name, 0)

_merge_bool("static_pass", static_pass)
_merge_int("files_changed", files_changed_arg)
_merge_bool("tests_pass", tests_pass)

# Recompute pass — both required keys are now guaranteed present.
cur["pass"] = bool(cur["static_pass"]) and bool(cur["tests_pass"])

# Enforce schema: files_changed:int, others:bool.
cur["files_changed"] = int(cur["files_changed"])
cur["static_pass"] = bool(cur["static_pass"])
cur["tests_pass"] = bool(cur["tests_pass"])
cur["pass"] = bool(cur["pass"])

os.makedirs(os.path.dirname(dst), exist_ok=True)
with open(dst, "w") as f:
    json.dump(cur, f, indent=2, sort_keys=True)
    f.write("\n")
PY

  mv "$tmp" "$verdict_path"
}