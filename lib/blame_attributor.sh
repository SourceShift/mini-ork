#!/usr/bin/env bash
# lib/blame_attributor.sh — delayed side-effect credit assignment (frc-a4).
#
# When a later run (bug audit, failing gate, regression test, review finding)
# localizes a defect to specific file:line ranges, this library attributes the
# defect back to the prior run/lane(s) that authored those lines, computes a
# signed penalty in [-1, 0] (via a kimi judge — mockable for tests), and writes
# rows into the `defect_attributions` table defined by 0045_defect_attributions.sql.
#
# Design references: research §6 (Theme D — counterfactual + Shapley split) and
# research §7 (step #5 — `blame_attributor` lib).
#
# Provenance chain:
#   defect → file:line ranges → `git blame` → commit SHAs
#                                 → commit trailers (`Mini-ork-run-id:` /
#                                   `Mini-ork-lane:`) → (run_id, lane)
#                                 → fallback: commit author-email as lane id
#
# Apportionment:
#   Each line in the blamed range belongs to exactly one commit. We count lines
#   per (run_id, lane) and apportion the judge-supplied penalty in proportion
#   to lines-attributed. This is the "cheap Shapley approximation" from
#   research §6.2: weight by lines-attributed. Sum of apportionments == judge
#   penalty (signed) by construction.
#
# Public API:
#   blame_attributor_attribute <defect_json_path>
#                              [--db PATH]
#                              [--judge-cmd CMD]
#                              [--found-run-id ID]
#                              [--dry-run]
#       Reads a defect JSON document, performs attribution, and writes one
#       defect_attributions row per (found_run_id, blamed_run_id) pair.
#       Prints a JSONL summary on stdout (one row per inserted pair, or per
#       would-be insert under --dry-run).
#
#   blame_attributor_smoke [--tmp DIR]
#       Self-test that fabricates a mini git repo with two runs' commits,
#       runs attribution against a synthetic defect, and verifies the rows
#       written to defect_attributions. Exits non-zero on any assertion
#       failure. Used by the verifier's synthetic_defect_attribution check.
#
# Defect JSON shape:
#   {
#     "file":          "<repo-relative path, e.g. lib/foo.sh>",
#     "ranges":        [[start_line, end_line], ...],   // inclusive, 1-based
#     "severity":      "low"|"medium"|"high"|"critical",
#     "task_class":    "<string, e.g. code-fix>",
#     "code_region":   "<string, top-level dir>",
#     "defect_report": "<free-form prose>",
#     "diff":          "<unified diff body>"
#   }
#
# Env knobs:
#   MO_BLAME_JUDGE_CMD       Override for the penalty-judge command. Receives
#                            the defect JSON path on argv[1] and the diff
#                            path on argv[2]; must print a single number in
#                            [-1, 0] on stdout. When unset, falls back to
#                            the bundled _ba_default_judge (a deterministic
#                            mock so attribution remains testable offline).
#   MO_BLAME_DRY_RUN         When "1", skips DB writes but still runs the
#                            blame + judge + apportionment steps.
#   MO_BLAME_TRAILER_RUN     Trailer key for the prior run id (default
#                            `Mini-ork-run-id`). Override to use a different
#                            commit-message convention.
#   MO_BLAME_TRAILER_LANE    Trailer key for the prior lane id (default
#                            `Mini-ork-lane`).

# shellcheck disable=SC2155
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MINI_ORK_DB_DEFAULT="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

# ─────────────────────────────────────────────────────────────────────
# Severity bounds — table constraint allows low/medium/high/critical.
_ba_normalize_severity() {
  case "${1:-medium}" in
    low|medium|high|critical) printf '%s' "$1" ;;
    *)                        printf 'medium' ;;
  esac
}

# Validate a penalty is numeric and inside the inclusive [-1, 0] range.
# Echoes the canonical numeric form (e.g. "-0.5") on success; exits non-zero
# on any violation. The defect_attributions.penalty column is REAL NOT NULL
# and the table contract demands values inside [-1, 0].
_ba_validate_penalty() {
  local raw="${1:?penalty required}"
  case "$raw" in
    ''|*[!0-9.+-]*)
      echo "blame_attributor: penalty not numeric: $raw" >&2
      return 2
      ;;
  esac
  # Reject "++" / "--" / "+-" forms and bare "+/-" without digits.
  case "$raw" in
    [+-][+-]*) echo "blame_attributor: malformed penalty: $raw" >&2; return 2 ;;
  esac
  # Use python for robust float compare (avoids locale-dependent awk pitfalls).
  python3 - "$raw" <<'PY' || { echo "blame_attributor: penalty out of [-1, 0]: $raw" >&2; exit 3; }
import sys
try:
    v = float(sys.argv[1])
except (TypeError, ValueError):
    sys.exit(4)
if v < -1.0 or v > 0.0:
    sys.exit(5)
print(repr(v))
PY
}

# Run `git blame` over a single line range, parse to one record per line:
#   <sha>\t<author_email>\t<line_no>
# Each output line corresponds to exactly one source line in the range.
_ba_blame_lines() {
  local repo_root="$1" file="$2" start="$3" end="$4"
  [ -d "$repo_root" ] || { echo "blame_attributor: repo not found: $repo_root" >&2; return 10; }
  [ -f "$repo_root/$file" ] || { echo "blame_attributor: file not found: $repo_root/$file" >&2; return 11; }
  git -C "$repo_root" blame --line-porcelain \
      -L "${start},${end}" -- "$file" \
    | awk '
      # Porcelain header line: "<sha> <orig-lno> <final-lno> [<count>]".
      # Avoid {40} interval syntax (unsupported by POSIX/macOS awk) — detect by
      # an all-hex first field (len>=7) followed by a numeric field. Reset email
      # at each new block so a missing author-mail cannot bleed across commits.
      ($1 ~ /^[0-9a-f]+$/ && length($1) >= 7 && $2 ~ /^[0-9]+$/) {
        sha=$1; email=""; next
      }
      /^author-mail / { email=$2; sub(/^</, "", email); sub(/>$/, "", email); next }
      /^\t/           { if (sha != "") print sha "\t" email }
    '
}

# Group line records by (run_id, lane). Reads stdin (blame output). Outputs
# one JSON line per group: {"run_id":..., "lane":..., "line_count":N}.
# Run_id comes from the trailer (preferred) or falls back to the commit SHA.
# Lane comes from the trailer; if absent we use the author email so the
# attribution is still actionable ("we don't know the run, but we know who").
_ba_group_by_run() {
  local repo_root="$1"
  local trailer_run="$2"
  local trailer_lane="$3"
  local sha_cache_file blame_in
  sha_cache_file="$(mktemp -t blame_attributor_sha.XXXXXX)"
  # Capture stdin (blame stream) to a file so BOTH passes can read it. The old
  # code consumed stdin in pass 1, leaving pass 2's `-` at EOF → zero groups
  # (frc-a4 critic fix). Use EXIT-safe cleanup.
  blame_in="$(mktemp -t blame_attributor_in.XXXXXX)"
  cat > "$blame_in"
  trap 'rm -f "$sha_cache_file" "$blame_in"' RETURN

  # Pass 1: collect unique SHAs and resolve their trailers.
  awk -F'\t' '{print $1}' "$blame_in" | sort -u | while IFS= read -r sha; do
    [ -n "$sha" ] || continue
    local rid lane
    # %(trailers:key=...,valueonly) is the ONLY valid trailer placeholder;
    # the old "%(${trailer_run})" was not a real format spec → always empty.
    # Pass the SHA as a REVISION (no `--`, which made it a pathspec) (frc-a4).
    rid="$(git -C "$repo_root" log -1 --format="%(trailers:key=${trailer_run},valueonly)" "$sha" 2>/dev/null \
            | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    lane="$(git -C "$repo_root" log -1 --format="%(trailers:key=${trailer_lane},valueonly)" "$sha" 2>/dev/null \
            | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    # Use sha as the synthetic run id when trailer absent (still unique).
    [ -n "$rid" ] || rid="sha:$sha"
    # Lane fallback: when the lane trailer is absent, attribute to the commit's
    # author email so the result is still actionable ("don't know the run, but
    # know who") instead of "unknown" (frc-a4 B3 critic fix).
    if [ -z "$lane" ]; then
      lane="$(awk -F'\t' -v s="$sha" '$1==s {print $2; exit}' "$blame_in")"
    fi
    printf '%s\t%s\t%s\n' "$sha" "$rid" "$lane"
  done > "$sha_cache_file.tmp"
  mv "$sha_cache_file.tmp" "$sha_cache_file"

  # Pass 2: for each line, look up (run_id, lane) and count.
  awk -F'\t' '
    NR==FNR { map[$1]=$2"\t"$3; next }
    {
      split($1, _, "\t")
      sha = $1
      key = map[sha]
      if (key == "") { key = "sha:" sha "\t" }
      split(key, parts, "\t")
      rid = parts[1]; lane = parts[2]
      if (lane == "") lane = "unknown"
      print rid "\t" lane
    }
  ' "$sha_cache_file" "$blame_in" \
    | sort \
    | awk -F'\t' '
        { if ($1==prev_rid && $2==prev_lane) c++; else { if (NR>1) print prev_rid"\t"prev_lane"\t"c; prev_rid=$1; prev_lane=$2; c=1 } }
        END { if (NR>0) print prev_rid"\t"prev_lane"\t"c }
      ' \
    | python3 -c '
import json, sys
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 3:
        continue
    rid, lane, n = parts
    print(json.dumps({"run_id": rid, "lane": lane, "line_count": int(n)}))
'
}

# Default judge: deterministic mock so attribution remains testable offline
# (and so a fresh checkout never silently calls Kimi without intent).
# Maps severity → fixed penalty, optionally perturbed by defect_report length.
_ba_default_judge() {
  local defect_json="$1"
  python3 - "$defect_json" <<'PY'
import json, sys, math
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
sev = (d.get("severity") or "medium").lower()
base = {"low": -0.1, "medium": -0.4, "high": -0.7, "critical": -0.95}.get(sev, -0.4)
# Mild perturbation from defect_report length so two distinct defects at
# same severity produce distinct (but still bounded) penalties.
rep = d.get("defect_report") or ""
adj = min(0.05, max(-0.05, (len(rep) % 17) / 200.0 - 0.04))
p = max(-1.0, min(0.0, base + adj))
print(f"{p:.4f}")
PY
}

# Validate + canonicalize the judge command's output.
_ba_call_judge() {
  local defect_json="$1" diff_path="$2"
  local cmd="${MO_BLAME_JUDGE_CMD:-}"
  local raw
  if [ -n "$cmd" ]; then
    raw="$("$cmd" "$defect_json" "$diff_path" 2>/dev/null | tail -n1)"
  else
    raw="$(_ba_default_judge "$defect_json")"
  fi
  _ba_validate_penalty "$raw"
}

# INSERT a single attribution row. Fails loud on schema mismatch.
_ba_insert_attribution() {
  local db="$1" row_json="$2"
  python3 - "$db" "$row_json" <<'PY' || { echo "blame_attributor: insert failed" >&2; exit 20; }
import json, sqlite3, sys
db, payload = sys.argv[1], sys.argv[2]
row = json.loads(payload)
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
required = ("found_run_id", "blamed_run_id", "lane", "code_region",
            "task_class", "severity", "penalty", "decay_halflife_days")
missing = [k for k in required if k not in row]
if missing:
    print(f"missing keys: {missing}", file=sys.stderr)
    sys.exit(21)
con.execute(
    """INSERT INTO defect_attributions
         (found_run_id, blamed_run_id, lane, code_region, task_class,
          severity, penalty, decay_halflife_days)
       VALUES (?,?,?,?,?,?,?,?)""",
    (row["found_run_id"], row["blamed_run_id"], row["lane"],
     row["code_region"], row["task_class"], row["severity"],
     float(row["penalty"]), float(row.get("decay_halflife_days", 30.0))),
)
con.commit()
print("inserted")
PY
}

# Apportion a signed total penalty across groups by line-count share.
# Reads groups JSONL on stdin (from _ba_group_by_run); emits one JSON row
# per group with the apportioned penalty. Sum of apportioned == total.
_ba_apportion() {
  local total_penalty="$1"
  # Capture the piped groups FIRST: `python3 - <<PY` makes the heredoc python's
  # stdin (the script), so it cannot also read piped data from stdin. Pass the
  # groups in via env instead (frc-a4 critic fix — apportion always emitted
  # nothing, so zero rows were ever inserted).
  local _groups_data
  _groups_data="$(cat)"
  _BA_GROUPS_DATA="$_groups_data" python3 - "$total_penalty" <<'PY'
import json, os, sys
total = float(sys.argv[1])
groups = []
for line in os.environ.get("_BA_GROUPS_DATA", "").splitlines():
    line = line.strip()
    if not line:
        continue
    groups.append(json.loads(line))
total_lines = sum(g["line_count"] for g in groups) or 1
running = 0.0
n = len(groups)
for i, g in enumerate(groups):
    share = g["line_count"] / total_lines
    if i == n - 1:
        # Last group absorbs rounding so sum == total exactly.
        p = total - running
    else:
        p = round(total * share, 6)
        running += p
    out = dict(g)
    out["penalty"] = round(p, 6)
    print(json.dumps(out))
PY
}

# ─────────────────────────────────────────────────────────────────────
# Public entry point.
#
#   blame_attributor_attribute <defect_json_path> [--db PATH] [--judge-cmd CMD]
#                              [--found-run-id ID] [--dry-run]
#
blame_attributor_attribute() {
  local defect_json="${1:?defect_json path required}"
  shift || true
  local db="$MINI_ORK_DB_DEFAULT"
  local judge_cmd=""
  local found_run_id="${MINI_ORK_RUN_ID:-}"
  local repo_root="$MINI_ORK_ROOT"
  local dry_run=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --db)            db="$2"; shift 2 ;;
      --judge-cmd)     judge_cmd="$2"; shift 2 ;;
      --found-run-id)  found_run_id="$2"; shift 2 ;;
      --repo-root)     repo_root="$2"; shift 2 ;;
      --dry-run)       dry_run=1; shift ;;
      *) echo "blame_attributor: unknown arg: $1" >&2; return 64 ;;
    esac
  done
  [ -n "$found_run_id" ] || { echo "blame_attributor: --found-run-id or MINI_ORK_RUN_ID required" >&2; return 65; }
  [ -f "$defect_json" ] || { echo "blame_attributor: defect json not found: $defect_json" >&2; return 66; }

  # Honor per-call --judge-cmd by overriding the env knob.
  if [ -n "$judge_cmd" ]; then
    export MO_BLAME_JUDGE_CMD="$judge_cmd"
  fi

  local trailer_run="${MO_BLAME_TRAILER_RUN:-Mini-ork-run-id}"
  local trailer_lane="${MO_BLAME_TRAILER_LANE:-Mini-ork-lane}"

  # Parse the defect document.
  local parsed
  parsed="$(python3 - "$defect_json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
print(d.get("file", ""))
print(json.dumps(d.get("ranges") or []))
print((d.get("severity") or "medium"))
print(d.get("task_class") or "")
print(d.get("code_region") or "")
print(d.get("diff") or "")
PY
)"
  local file ranges severity task_class code_region diff_body
  file="$(printf '%s\n' "$parsed" | sed -n '1p')"
  ranges="$(printf '%s\n' "$parsed" | sed -n '2p')"
  severity="$(printf '%s\n' "$parsed" | sed -n '3p')"
  task_class="$(printf '%s\n' "$parsed" | sed -n '4p')"
  code_region="$(printf '%s\n' "$parsed" | sed -n '5p')"
  diff_body="$(printf '%s\n' "$parsed" | sed -n '6,$p')"
  severity="$(_ba_normalize_severity "$severity")"

  [ -n "$file" ] || { echo "blame_attributor: defect.file required" >&2; return 67; }
  [ -n "$task_class" ] || task_class="unknown"
  [ -n "$code_region" ] || code_region="unknown"

  # Materialize diff to a temp file so the judge command can read it.
  local diff_tmp
  diff_tmp="$(mktemp -t blame_attributor_diff.XXXXXX)"
  trap 'rm -f "$diff_tmp"' RETURN
  printf '%s\n' "$diff_body" > "$diff_tmp"

  # Collect blame records across every range. Then group.
  local blame_all
  blame_all="$(mktemp -t blame_attributor_blame.XXXXXX)"
  trap 'rm -f "$diff_tmp" "$blame_all"' RETURN
  local range_count=0
  local total_lines=0
  # Iterate the ranges array.
  while IFS= read -r range; do
    [ -n "$range" ] || continue
    # range is JSON like [10, 20]
    local s e
    s="$(printf '%s' "$range" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0])')"
    e="$(printf '%s' "$range" | python3 -c 'import json,sys; print(json.load(sys.stdin)[1])')"
    if ! _ba_blame_lines "$repo_root" "$file" "$s" "$e" >> "$blame_all"; then
      echo "blame_attributor: git blame failed for $file:$s-$e" >&2
      return 68
    fi
    range_count=$((range_count + 1))
    total_lines=$((total_lines + (e - s + 1)))
  done <<< "$(printf '%s' "$ranges" | python3 -c '
import json, sys
try:
    arr = json.loads(sys.stdin.read())   # ranges arrive on stdin, not argv
except Exception:
    arr = []
for r in arr:
    if isinstance(r, (list, tuple)) and len(r) == 2:
        print(json.dumps(r))
')"

  if [ "$range_count" -eq 0 ] || [ ! -s "$blame_all" ]; then
    echo "blame_attributor: no blameable lines (empty ranges or file gone)" >&2
    return 69
  fi

  local groups_jsonl
  groups_jsonl="$(_ba_group_by_run "$repo_root" "$trailer_run" "$trailer_lane" < "$blame_all")"
  [ -n "$groups_jsonl" ] || { echo "blame_attributor: grouping produced no rows" >&2; return 70; }

  # Penalty: judge → validate → apportion.
  local judge_penalty
  judge_penalty="$(_ba_call_judge "$defect_json" "$diff_tmp")"
  local apportioned
  apportioned="$(printf '%s\n' "$groups_jsonl" | _ba_apportion "$judge_penalty")"

  # Write rows.
  local inserted=0
  while IFS= read -r group; do
    [ -n "$group" ] || continue
    local row
    # Pass the real metadata env vars to the SAME invocation that BUILDS the
    # row. Previously they were exported only on a later printf (after row was
    # already built), so every row was stamped unknown/medium (frc-a4 fix).
    row="$(RID="$found_run_id" GROUP="$group" \
          CODE_REGION="$code_region" TASK_CLASS="$task_class" \
          SEVERITY="$severity" python3 - <<'PY'
import json, os
g = json.loads(os.environ["GROUP"])
row = {
    "found_run_id": os.environ["RID"],
    "blamed_run_id": g["run_id"],
    "lane": g["lane"],
    "code_region": os.environ.get("CODE_REGION", "unknown"),
    "task_class": os.environ.get("TASK_CLASS", "unknown"),
    "severity": os.environ.get("SEVERITY", "medium"),
    "penalty": g["penalty"],
    "decay_halflife_days": float(os.environ.get("DECAY", "30.0")),
}
# Tag line_count + apportionment as extra context (consumed by caller).
row["_line_count"] = g["line_count"]
print(json.dumps(row))
PY
)"
    if [ "$dry_run" = "1" ]; then
      printf '%s\n' "$row"
    else
      _ba_insert_attribution "$db" "$row" >/dev/null
      printf '%s\n' "$row"
    fi
    inserted=$((inserted + 1))
  done <<< "$apportioned"

  # Summary line on stderr for the verifier.
  {
    echo "blame_attributor: file=$file ranges=$range_count lines=$total_lines"
    echo "blame_attributor: groups=$inserted penalty_total=$judge_penalty severity=$severity"
  } >&2
  return 0
}

# ─────────────────────────────────────────────────────────────────────
# blame_attributor_smoke — synthetic verification entry point.
#
# Builds a tmp git repo with two fake runs' commits (one run_id=R1 lane=opus,
# another R2 lane=sonnet), fabricates a defect spanning lines from both,
# runs attribution against a tmp SQLite DB, and asserts:
#   - exactly 2 rows written (one per run/lane)
#   - penalties split by line-count share
#   - signed penalties are negative (penalty in [-1, 0])
#   - found_run_id + severity come from the defect doc
#
# Exits non-zero on any assertion failure; writes a JSONL receipt to the
# verifier run dir when MO_BLAME_SMOKE_RECEIPT is set.
blame_attributor_smoke() {
  local tmp="${MO_BLAME_SMOKE_TMP:-}"
  if [ -z "$tmp" ]; then
    tmp="$(mktemp -d -t blame_attributor_smoke.XXXXXX)"
    # Function-scoped cleanup: RETURN fires while $tmp is still in scope,
    # so set -u doesn't trip when the trap dereferences it.
    trap 'rm -rf "$tmp"' RETURN
  fi
  local repo="$tmp/repo"
  local db="$tmp/state.db"
  local runner="$tmp/runner.sh"
  local found_run="RUN-SMOKE-$$"
  local run_a="RUN-A-$$"
  local run_b="RUN-B-$$"

  # Build the synthetic repo.
  git init -q -b main "$repo"
  git -C "$repo" config user.email "smoke@example.com"
  git -C "$repo" config user.name  "smoke"
  # Don't inherit a global commit.gpgsign=true — synthetic commits must not
  # require a signing key in CI / arbitrary dev environments (frc-a4 robustness).
  git -C "$repo" config commit.gpgsign false
  git -C "$repo" config tag.gpgsign false

  # Run A: commits a 4-line file (lines 1-4 in the final shape).
  printf 'alpha-1\nalpha-2\nalpha-3\nalpha-4\n' > "$repo/lib_smoke.sh"
  git -C "$repo" add lib_smoke.sh
  git -C "$repo" commit -q -m "smoke: A

Mini-ork-run-id: $run_a
Mini-ork-lane: opus"
  local sha_a
  sha_a="$(git -C "$repo" rev-parse HEAD)"

  # Run B: appends 3 more lines (lines 5-7 in the final shape).
  printf '\nbeta-1\nbeta-2\nbeta-3\n' >> "$repo/lib_smoke.sh"
  git -C "$repo" add lib_smoke.sh
  git -C "$repo" commit -q -m "smoke: B append

Mini-ork-run-id: $run_b
Mini-ork-lane: sonnet"
  local sha_b
  sha_b="$(git -C "$repo" rev-parse HEAD)"

  # Apply a stripped 0045 migration so defect_attributions exists. The
  # production migration tracks schema_migrations which isn't present in
  # this throwaway DB; we recreate the table/index portions only.
  python3 - "$db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.executescript("""
CREATE TABLE IF NOT EXISTS defect_attributions (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  found_run_id         TEXT    NOT NULL,
  blamed_run_id        TEXT    NOT NULL,
  lane                 TEXT    NOT NULL,
  code_region          TEXT    NOT NULL,
  task_class           TEXT    NOT NULL,
  severity             TEXT    NOT NULL DEFAULT 'medium'
                         CHECK (severity IN ('low','medium','high','critical')),
  penalty              REAL    NOT NULL,
  decay_halflife_days  REAL    NOT NULL DEFAULT 30.0,
  ts                   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_defect_attributions_lookup
  ON defect_attributions(lane, code_region, task_class);
CREATE INDEX IF NOT EXISTS idx_defect_attributions_pair
  ON defect_attributions(found_run_id, blamed_run_id);
""")
con.commit()
PY

  # Synthetic defect JSON spanning lines 1-7 of lib_smoke.sh.
  local defect="$tmp/defect.json"
  cat > "$defect" <<JSON
{
  "file": "lib_smoke.sh",
  "ranges": [[1, 7]],
  "severity": "high",
  "task_class": "code-fix",
  "code_region": "smoke",
  "defect_report": "synthetic defect spanning lines from two runs",
  "diff": "--- a/lib_smoke.sh\n+++ b/lib_smoke.sh\n@@ -1,3 +1,4 @@\n+broken\n"
}
JSON

  # External runner script so the smoke harness doesn't need nested bash -c
  # quoting. Receives <defect.json> <found_run_id> <repo_root> as argv.
  cat > "$runner" <<'RUNNER'
#!/usr/bin/env bash
set -uo pipefail
source "$MINI_ORK_ROOT/lib/blame_attributor.sh"
# Real insert (NOT --dry-run) so the smoke exercises _ba_insert_attribution and
# the DB write path A4 requires; the function still prints the JSONL rows for
# the existing assertions (frc-a4 critic fix).
blame_attributor_attribute "$1" --found-run-id "$2" --repo-root "$3"
RUNNER
  chmod +x "$runner"

  # Judge stub: a real executable that ignores its args and prints the penalty.
  # (MO_BLAME_JUDGE_CMD must be a single command path — "echo -0.6" cannot be
  # run as one argv[0], which silently produced an empty penalty.)
  local judge="$tmp/judge.sh"
  printf '#!/usr/bin/env bash\necho -0.6\n' > "$judge"
  chmod +x "$judge"

  # Run the attributor against the synthetic repo + db.
  local log
  log="$(
    env MINI_ORK_ROOT="$MINI_ORK_ROOT" \
        MINI_ORK_DB="$db" \
        MINI_ORK_RUN_ID="$found_run" \
        MO_BLAME_JUDGE_CMD="$judge" \
        bash "$runner" "$defect" "$found_run" "$repo" \
      2>&1
  )"

  # Assertions.
  local pass=1
  _ba_assert() {
    local label="$1" needle="$2"
    if printf '%s' "$log" | grep -q -- "$needle"; then
      echo "  [OK]   $label"
    else
      echo "  [FAIL] $label (missing: $needle)"
      pass=0
    fi
  }
  _ba_assert "dry-run produced JSONL rows"   "blamed_run_id"
  _ba_assert "trailer A surfaced as run_id"  "$run_a"
  _ba_assert "trailer B surfaced as run_id"  "$run_b"
  _ba_assert "lane opus surfaced"            '"lane": "opus"'
  _ba_assert "lane sonnet surfaced"          '"lane": "sonnet"'
  _ba_assert "signed penalty negative"       '"penalty": -'
  _ba_assert "found_run_id stamped"          "$found_run"
  _ba_assert "severity preserved"            '"severity": "high"'
  _ba_assert "summary line printed"          "blame_attributor: groups=2"

  # Penalty range: read apportionment rows, check each ∈ [-1, 0].
  local oob
  oob="$(printf '%s\n' "$log" | python3 -c '
import json, sys, re
rows = [json.loads(l) for l in sys.stdin if l.strip().startswith("{")]
for r in rows:
    p = r.get("penalty")
    if p is None or p < -1.0 or p > 0.0:
        print("out-of-range:", p)
        sys.exit(1)
sum_p = sum(r["penalty"] for r in rows)
if abs(sum_p - (-0.6)) > 1e-3:
    print("apportionment sum mismatch:", sum_p, "expected -0.6")
    sys.exit(2)
print("range-and-sum-ok", len(rows), "rows sum=", round(sum_p, 6))
')" || pass=0
  if [ "$pass" = "1" ]; then echo "  [OK]   $oob"; fi

  # Real DB-write assertions (A4 requires rows actually persisted, not dry-run).
  local db_check
  db_check="$(python3 - "$db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
n = con.execute("SELECT COUNT(*) FROM defect_attributions").fetchone()[0]
bad = con.execute(
    "SELECT COUNT(*) FROM defect_attributions "
    "WHERE code_region!='smoke' OR task_class!='code-fix' OR severity!='high'"
).fetchone()[0]
lanes = sorted(r[0] for r in con.execute(
    "SELECT DISTINCT lane FROM defect_attributions"))
print(f"{n}|{bad}|{','.join(lanes)}")
PY
)"
  local n_rows="${db_check%%|*}"
  local rest="${db_check#*|}"; local bad_meta="${rest%%|*}"; local lanes="${rest#*|}"
  if [ "$n_rows" = "2" ]; then echo "  [OK]   2 rows persisted to defect_attributions";
    else echo "  [FAIL] expected 2 persisted rows, got '$n_rows'"; pass=0; fi
  if [ "$bad_meta" = "0" ]; then echo "  [OK]   persisted rows carry real metadata (smoke/code-fix/high)";
    else echo "  [FAIL] $bad_meta rows stamped wrong metadata (unknown/medium regression)"; pass=0; fi
  if [ "$lanes" = "opus,sonnet" ]; then echo "  [OK]   lanes attributed: $lanes";
    else echo "  [FAIL] lanes wrong: '$lanes' (expected opus,sonnet)"; pass=0; fi

  if [ "$pass" = "1" ]; then
    echo "blame_attributor_smoke: PASS"
    if [ -n "${MO_BLAME_SMOKE_RECEIPT:-}" ]; then
      {
        echo "{\"status\":\"pass\",\"found_run_id\":\"$found_run\","
        echo "\"run_a\":\"$run_a\",\"sha_a\":\"$sha_a\","
        echo "\"run_b\":\"$run_b\",\"sha_b\":\"$sha_b\","
        echo "\"groups\":2,\"penalty_sum\":-0.6}"
      } > "$MO_BLAME_SMOKE_RECEIPT"
    fi
    return 0
  fi
  echo "blame_attributor_smoke: FAIL"
  echo "--- log ---"; printf '%s\n' "$log"
  return 1
}

# When invoked directly (not sourced) run the smoke test. Lets a verifier
# execute `bash lib/blame_attributor.sh` to self-validate offline.
if [ "${0:-}" = "${BASH_SOURCE[0]:-}" ]; then
  blame_attributor_smoke "$@"
fi