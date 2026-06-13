#!/usr/bin/env bash
# anchor_corpus.sh — held-out anchor corpus loader + recall scorer.
#
# Implements roadmap Wave 2-A: per-recipe held-out anchor corpus
# (Wang 2026). The roadmap text correctly notes corpus selection is
# judgment-heavy — the operator hand-authors the corpus for each
# synthesis recipe — so this file does NOT generate corpora.
# It provides the substrate: load a corpus JSON, validate its
# shape, and score recipe findings against it for recall.
#
# Corpus shape (operator-authored):
#   {
#     "name":        "<corpus name>",
#     "task_class":  "<the task_class the corpus targets>",
#     "description": "<one paragraph describing the held-out set>",
#     "anchors": [
#       { "id":     "A-001",
#         "file":   "server/services/x.ts",
#         "line":   42,
#         "claim":  "Race condition between handler retry and cache invalidation.",
#         "tags":   ["race", "cache"],
#         "severity": "P1",
#         "must_be_found": true
#       },
#       ...
#     ]
#   }
#
# Recall scoring: an anchor is "found" when EITHER its id token OR
# its file:line citation appears in the findings text. This is the
# minimum mechanically defensible match — wider semantic matching
# requires an embedding model and is left to a follow-up.
#
# Public API:
#   anchor_corpus_load <corpus_path>
#       Validates the shape and emits the parsed corpus JSON on
#       stdout. rc=0 on valid, rc=2 on shape errors.
#   anchor_corpus_recall <findings_path> <corpus_path> [<report_dir>]
#       Computes recall = found_anchors / must_be_found_anchors.
#       Emits structured JSON:
#         { "verdict":  "recall_meets_floor" | "RECALL_BELOW_FLOOR" |
#                       "indeterminate",
#           "reason":   "ok" | "low_recall" | "no_must_be_found" |
#                       "missing_inputs",
#           "found":    <int>,
#           "must_be_found": <int>,
#           "recall":   <float | null>,
#           "recall_floor": <float>,
#           "missed_anchor_ids": ["A-003", "A-007"],
#           "report_path":   "<report_dir>/corpus-recall.tsv",
#           "rationale": "<one sentence>" }
#       rc=0 when recall >= floor (or gate cannot measure);
#       rc=1 when RECALL_BELOW_FLOOR triggers.
#
# Env knobs:
#   MO_CORPUS_RECALL_FLOOR    Default 0.8 (Wang 2026 recommended cut
#                              for the must_be_found subset).

set -uo pipefail

anchor_corpus_load() {
  local _path="${1:-}"
  if [ -z "$_path" ] || [ ! -f "$_path" ]; then
    echo "anchor_corpus_load: corpus path missing" >&2
    return 2
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "anchor_corpus_load: python3 unavailable" >&2
    return 2
  fi

  MO_CORPUS_PATH="$_path" python3 - <<'PY'
import json, os, sys

try:
    data = json.load(open(os.environ["MO_CORPUS_PATH"], encoding="utf-8"))
except Exception as exc:
    sys.stderr.write(f"anchor_corpus_load: parse error: {exc}\n")
    sys.exit(2)

if not isinstance(data, dict):
    sys.stderr.write("anchor_corpus_load: corpus must be a JSON object\n")
    sys.exit(2)

anchors = data.get("anchors")
if not isinstance(anchors, list) or not anchors:
    sys.stderr.write("anchor_corpus_load: anchors[] must be a non-empty list\n")
    sys.exit(2)

required_anchor_fields = {"id", "file", "line", "claim"}
for i, a in enumerate(anchors):
    if not isinstance(a, dict):
        sys.stderr.write(f"anchor_corpus_load: anchors[{i}] must be an object\n")
        sys.exit(2)
    missing = required_anchor_fields - a.keys()
    if missing:
        sys.stderr.write(f"anchor_corpus_load: anchors[{i}] missing {sorted(missing)}\n")
        sys.exit(2)

print(json.dumps(data, indent=2))
PY
}

anchor_corpus_recall() {
  local _findings="${1:-}"
  local _corpus="${2:-}"
  local _report_dir="${3:-${MINI_ORK_RUN_DIR:-.}}"
  local _floor="${MO_CORPUS_RECALL_FLOOR:-0.8}"
  local _report_path="$_report_dir/corpus-recall.tsv"

  if [ -z "$_findings" ] || [ ! -f "$_findings" ] || [ -z "$_corpus" ] || [ ! -f "$_corpus" ]; then
    printf '{"verdict":"indeterminate","reason":"missing_inputs","found":0,"must_be_found":0,"recall":null,"recall_floor":%s,"missed_anchor_ids":[],"rationale":"findings_path or corpus_path missing; cannot measure"}\n' \
      "$_floor"
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    printf '{"verdict":"indeterminate","reason":"python_unavailable","found":0,"must_be_found":0,"recall":null,"recall_floor":%s,"missed_anchor_ids":[],"rationale":"python3 missing; cannot score recall"}\n' \
      "$_floor"
    return 0
  fi

  mkdir -p "$_report_dir" 2>/dev/null || true

  MO_CORPUS_FINDINGS="$_findings" \
  MO_CORPUS_PATH="$_corpus" \
  MO_CORPUS_FLOOR_PY="$_floor" \
  MO_CORPUS_REPORT="$_report_path" \
  python3 - <<'PY'
import json, os, sys

findings_path = os.environ["MO_CORPUS_FINDINGS"]
corpus_path = os.environ["MO_CORPUS_PATH"]
floor = float(os.environ["MO_CORPUS_FLOOR_PY"])
report_path = os.environ["MO_CORPUS_REPORT"]


def emit(verdict, reason, found, must, recall, missed, rationale, rc):
    print(json.dumps({
        "verdict": verdict,
        "reason": reason,
        "found": found,
        "must_be_found": must,
        "recall": recall,
        "recall_floor": floor,
        "missed_anchor_ids": missed,
        "report_path": report_path,
        "rationale": rationale,
    }))
    sys.exit(rc)


try:
    findings_text = open(findings_path, encoding="utf-8").read()
except Exception as exc:
    emit("indeterminate", "missing_inputs", 0, 0, None, [],
         f"findings unreadable: {exc}", 0)

try:
    corpus = json.load(open(corpus_path, encoding="utf-8"))
except Exception as exc:
    emit("indeterminate", "missing_inputs", 0, 0, None, [],
         f"corpus unreadable: {exc}", 0)

anchors = corpus.get("anchors") or []
must_be_found = [a for a in anchors if a.get("must_be_found")]
must_total = len(must_be_found)

if must_total == 0:
    emit("indeterminate", "no_must_be_found", 0, 0, None, [],
         "corpus has no must_be_found anchors; nothing to score", 0)


def matched(anchor):
    aid = anchor.get("id") or ""
    file = anchor.get("file") or ""
    line = anchor.get("line")
    if aid and aid in findings_text:
        return True
    if file and line is not None:
        for needle in (f"{file}:{line}", f"{file}#L{line}", f"{file}:{line}-"):
            if needle in findings_text:
                return True
    return False


found_count = 0
missed_ids = []
report_rows = ["anchor_id\tfile\tline\tseverity\tfound"]
for a in anchors:
    aid = a.get("id") or ""
    file = a.get("file") or ""
    line = a.get("line") or 0
    sev = a.get("severity") or ""
    ok = matched(a)
    report_rows.append(f"{aid}\t{file}\t{line}\t{sev}\t{'yes' if ok else 'no'}")
    if a.get("must_be_found"):
        if ok:
            found_count += 1
        else:
            missed_ids.append(aid)

try:
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_rows) + "\n")
except Exception:
    pass

recall = found_count / must_total

if recall < floor:
    emit("RECALL_BELOW_FLOOR", "low_recall", found_count, must_total,
         round(recall, 4), missed_ids,
         f"recall {recall:.1%} < floor {floor:.0%} ({len(missed_ids)} must-find anchors missed: {', '.join(missed_ids[:5])}{'...' if len(missed_ids) > 5 else ''})",
         1)

emit("recall_meets_floor", "ok", found_count, must_total,
     round(recall, 4), missed_ids,
     f"recall {recall:.1%} >= floor {floor:.0%} across {must_total} must-find anchors",
     0)
PY
}

# Self-test: load a corpus + score findings against it.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT

  cat > "$_selftest_dir/corpus.json" <<'JSON'
{
  "name": "selftest-corpus",
  "task_class": "refactor_audit",
  "description": "Self-test corpus with 4 anchors, 3 must_be_found.",
  "anchors": [
    {"id": "A-001", "file": "src/auth.ts",  "line": 42,
     "claim": "Token expiration unchecked", "severity": "P1",
     "must_be_found": true},
    {"id": "A-002", "file": "src/cache.ts", "line": 88,
     "claim": "Race on cache invalidation", "severity": "P0",
     "must_be_found": true},
    {"id": "A-003", "file": "src/db.ts",    "line": 117,
     "claim": "Connection leak under retry", "severity": "P1",
     "must_be_found": true},
    {"id": "A-004", "file": "src/util.ts",  "line": 30,
     "claim": "Minor: typo in error message", "severity": "P3",
     "must_be_found": false}
  ]
}
JSON

  echo "--- load corpus ---"
  anchor_corpus_load "$_selftest_dir/corpus.json" >/dev/null && echo "load ok"

  echo "--- fixture 1: findings hit all must_be_found (expect recall_meets_floor) ---"
  cat > "$_selftest_dir/findings-good.md" <<'MD'
## Findings

- A-001 src/auth.ts:42 — token expiration unchecked
- A-002 src/cache.ts:88 — race on cache invalidation
- A-003 src/db.ts:117 — connection leak under retry
MD
  anchor_corpus_recall "$_selftest_dir/findings-good.md" "$_selftest_dir/corpus.json" "$_selftest_dir"
  echo

  echo "--- fixture 2: findings miss one must_be_found (expect recall_meets_floor at 2/3=0.67 below 0.8 → BELOW_FLOOR) ---"
  cat > "$_selftest_dir/findings-bad.md" <<'MD'
## Findings

- A-001 src/auth.ts:42 — token expiration unchecked
- A-002 src/cache.ts:88 — race on cache invalidation
MD
  anchor_corpus_recall "$_selftest_dir/findings-bad.md" "$_selftest_dir/corpus.json" "$_selftest_dir"
  echo

  echo "--- fixture 3: missing inputs (expect indeterminate, rc=0) ---"
  anchor_corpus_recall "$_selftest_dir/does-not-exist.md" "$_selftest_dir/corpus.json" "$_selftest_dir"
fi
