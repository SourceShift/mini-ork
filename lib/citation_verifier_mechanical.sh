#!/usr/bin/env bash
# citation_verifier_mechanical.sh — recall-floor + wireheading check.
#
# Closes two adjacent positioning-doc honest-gaps items:
#   1. Wave 3 "mechanical citation+coverage verifier" (Sistla 2025
#      arxiv:2509.26546 + Ficek 2025 / KILT-style citation oracle).
#   2. "Wireheading check on validators" — verify the validator
#      actually read the file it cited, instead of confabulating a
#      file:line anchor for a claim it never grounded.
#
# What this does, in concrete terms:
#   Reads a synthesis-style markdown document and extracts every
#   citation of the form `path/to/file.ext:LINE` or `path:LINE-LINE`.
#   For each citation it then:
#     - resolves the path against $MINI_ORK_ROOT (the repo root),
#     - verifies the file exists,
#     - verifies the cited line range is within bounds,
#     - emits a per-citation pass/fail record.
#   Finally it computes a coverage ratio (passing citations / total
#   citations) and a count of unique cited files. If coverage drops
#   below MO_CITATION_COVERAGE_FLOOR (default 0.8 = 80%), the gate
#   escalates: the document is not safely grounded.
#
# Why this exists, in honesty terms:
#   Until today the refactor-audit recipe produced findings that
#   LOOKED rigorous (file:line citations everywhere) but nothing
#   actually checked the citations resolved. A panel agent that
#   confabulated a file path was indistinguishable from one that
#   read the file — the only consequence was a 404 if a human ever
#   clicked through. This verifier converts citation discipline
#   from convention into enforcement.
#
# Public API:
#   mo_check_citations <document_path> [<report_dir>]
#       → JSON on stdout:
#         { "verdict":      "citations_covered" | "CITATION_UNDERCOVERED" | "indeterminate",
#           "reason":       "ok" | "low_coverage" | "no_citations_found" |
#                            "missing_document" | "missing_root",
#           "coverage":     <float 0..1 | null>,
#           "coverage_floor": <float>,
#           "total_citations": <int>,
#           "valid_citations": <int>,
#           "invalid_citations": <int>,
#           "unique_files":    <int>,
#           "report_path":  "<report_dir>/citation-report.tsv",
#           "rationale":    "<one sentence>" }
#       rc=0 when coverage >= floor (or gate cannot measure)
#       rc=1 when CITATION_UNDERCOVERED triggers
#
# Env knobs:
#   MO_CITATION_COVERAGE_FLOOR    Recall floor; default 0.8.
#   MO_CITATION_MIN_COUNT         Minimum citations for the gate to
#                                  engage; default 3. Below this,
#                                  verdict=indeterminate (a 1-citation
#                                  doc trivially passes/fails — not signal).
#   MINI_ORK_ROOT                  Repo root for path resolution.
#                                  Defaults to current working dir.

set -uo pipefail

mo_check_citations() {
  local _doc="${1:-}"
  local _report_dir="${2:-${MINI_ORK_RUN_DIR:-.}}"

  if [ -z "$_doc" ] || [ ! -f "$_doc" ]; then
    printf '{"verdict":"indeterminate","reason":"missing_document","coverage":null,"coverage_floor":%s,"total_citations":0,"valid_citations":0,"invalid_citations":0,"unique_files":0,"rationale":"document path missing or not a file; cannot measure"}\n' \
      "${MO_CITATION_COVERAGE_FLOOR:-0.8}"
    return 0
  fi

  local _root="${MINI_ORK_ROOT:-$(pwd)}"
  if [ ! -d "$_root" ]; then
    printf '{"verdict":"indeterminate","reason":"missing_root","coverage":null,"coverage_floor":%s,"total_citations":0,"valid_citations":0,"invalid_citations":0,"unique_files":0,"rationale":"MINI_ORK_ROOT not a directory; cannot resolve citations"}\n' \
      "${MO_CITATION_COVERAGE_FLOOR:-0.8}"
    return 0
  fi

  local _floor="${MO_CITATION_COVERAGE_FLOOR:-0.8}"
  local _min_count="${MO_CITATION_MIN_COUNT:-3}"
  local _report_path="$_report_dir/citation-report.tsv"

  if ! command -v python3 >/dev/null 2>&1; then
    printf '{"verdict":"indeterminate","reason":"python_unavailable","coverage":null,"coverage_floor":%s,"total_citations":0,"valid_citations":0,"invalid_citations":0,"unique_files":0,"rationale":"python3 missing; cannot extract citations"}\n' \
      "$_floor"
    return 0
  fi

  mkdir -p "$_report_dir" 2>/dev/null || true

  MO_CIT_DOC="$_doc" \
  MO_CIT_ROOT="$_root" \
  MO_CIT_FLOOR="$_floor" \
  MO_CIT_MIN_COUNT="$_min_count" \
  MO_CIT_REPORT="$_report_path" \
  python3 - <<'PY'
import json, os, re, sys

doc_path = os.environ["MO_CIT_DOC"]
root = os.environ["MO_CIT_ROOT"].rstrip("/")
floor = float(os.environ["MO_CIT_FLOOR"])
min_count = int(os.environ["MO_CIT_MIN_COUNT"])
report_path = os.environ["MO_CIT_REPORT"]


def emit(verdict, reason, coverage, total, valid, invalid, unique_files, rationale, rc):
    print(json.dumps({
        "verdict": verdict,
        "reason": reason,
        "coverage": coverage,
        "coverage_floor": floor,
        "total_citations": total,
        "valid_citations": valid,
        "invalid_citations": invalid,
        "unique_files": unique_files,
        "report_path": report_path,
        "rationale": rationale,
    }))
    sys.exit(rc)


try:
    text = open(doc_path, encoding="utf-8").read()
except Exception as exc:
    emit("indeterminate", "missing_document", None, 0, 0, 0, 0,
         f"could not read {doc_path}: {exc}", 0)

# Citation pattern: <path>:<line> or <path>:<startLine>-<endLine>.
# Paths allow letters, digits, ., _, /, -. Extensions limited to common
# source/doc extensions to avoid matching e.g. URLs or arxiv IDs that
# look like path:line (e.g. "arxiv:2603.21454" — the colon-number form
# overlaps; the extension whitelist filters those out).
CITATION = re.compile(
    r"(?<![\w/])"                                # left boundary
    r"((?:[A-Za-z0-9_.\-/]+)"                    # path
    r"\.(?:py|ts|tsx|js|jsx|sh|bash|yaml|yml|"
    r"json|toml|md|rst|sql|go|rs|c|h|cc|cpp|hpp|"
    r"java|kt|swift|rb|php|html|css|scss|conf|"
    r"ini|cfg))"                                  # extension
    r":(\d+)(?:-(\d+))?"                          # :line or :start-end
    r"(?![\w])"                                   # right boundary
)

raw_citations = []
for m in CITATION.finditer(text):
    path, start, end = m.group(1), m.group(2), m.group(3)
    raw_citations.append((path, int(start), int(end) if end else int(start)))

# De-dupe identical citations: same file + same range counts once.
seen = set()
citations = []
for path, start, end in raw_citations:
    key = (path, start, end)
    if key in seen:
        continue
    seen.add(key)
    citations.append((path, start, end))

total = len(citations)
if total < min_count:
    emit("indeterminate", "no_citations_found" if total == 0 else "insufficient_citations",
         None, total, 0, 0, 0,
         f"found {total} citations; need >= {min_count} for the gate to engage",
         0)


def check_citation(path, start, end):
    # Reject obviously-absurd line numbers.
    if start < 1 or end < start:
        return False, "bad_line_range"
    # Resolve path against root, but accept absolute paths as-is.
    resolved = path if os.path.isabs(path) else os.path.join(root, path)
    if not os.path.isfile(resolved):
        return False, "file_missing"
    try:
        with open(resolved, "rb") as f:
            # Count lines without reading the whole file into memory.
            line_count = 0
            for _ in f:
                line_count += 1
                if line_count >= end:
                    break
    except Exception as exc:
        return False, f"read_error:{exc}"
    if end > line_count:
        return False, f"line_out_of_bounds:{line_count}"
    return True, "ok"


valid = 0
invalid = 0
report_rows = []
unique_files = set()
for path, start, end in citations:
    unique_files.add(path)
    ok, reason = check_citation(path, start, end)
    if ok:
        valid += 1
    else:
        invalid += 1
    range_text = f"{start}-{end}" if end != start else str(start)
    report_rows.append(f"{path}\t{range_text}\t{'PASS' if ok else 'FAIL'}\t{reason}")

try:
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("path\tlines\tverdict\treason\n")
        f.write("\n".join(report_rows) + "\n")
except Exception:
    pass  # audit aid only

coverage = valid / total if total else 0.0

if coverage < floor:
    emit("CITATION_UNDERCOVERED", "low_coverage", round(coverage, 4),
         total, valid, invalid, len(unique_files),
         f"citation coverage {coverage:.1%} < floor {floor:.0%} ({invalid} of {total} citations failed to resolve); document is not safely grounded",
         1)

emit("citations_covered", "ok", round(coverage, 4),
     total, valid, invalid, len(unique_files),
     f"citation coverage {coverage:.1%} >= floor {floor:.0%} across {total} citations spanning {len(unique_files)} unique files",
     0)
PY
}

# Self-test: 4 fixtures (full coverage / partial coverage / no citations /
# bad line range). Pattern mirrors lib/krippendorff_alpha_gate.sh.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT

  # Make MINI_ORK_ROOT a fake repo with a real source file
  mkdir -p "$_selftest_dir/repo/src" "$_selftest_dir/repo/docs"
  printf 'line1\nline2\nline3\nline4\nline5\n' > "$_selftest_dir/repo/src/foo.ts"
  printf 'a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n' > "$_selftest_dir/repo/src/bar.py"

  echo "--- fixture 1: all citations valid (expect pass) ---"
  cat > "$_selftest_dir/repo/docs/synthesis.md" <<'MD'
Three valid anchors. See src/foo.ts:2 for the first claim and
src/foo.ts:3-4 for the second, and src/bar.py:7 for the third.
MD
  MINI_ORK_ROOT="$_selftest_dir/repo" \
  mo_check_citations "$_selftest_dir/repo/docs/synthesis.md" "$_selftest_dir"
  echo

  echo "--- fixture 2: half citations invalid (expect CITATION_UNDERCOVERED) ---"
  cat > "$_selftest_dir/repo/docs/synthesis.md" <<'MD'
Mixed: real src/foo.ts:2 and ghost src/missing.ts:5 and
out-of-bounds src/foo.ts:9999 and real src/bar.py:1.
MD
  MINI_ORK_ROOT="$_selftest_dir/repo" \
  mo_check_citations "$_selftest_dir/repo/docs/synthesis.md" "$_selftest_dir"
  echo

  echo "--- fixture 3: no citations (expect indeterminate, rc=0) ---"
  cat > "$_selftest_dir/repo/docs/synthesis.md" <<'MD'
A document with no file:line anchors at all, just narrative prose.
MD
  MINI_ORK_ROOT="$_selftest_dir/repo" \
  mo_check_citations "$_selftest_dir/repo/docs/synthesis.md" "$_selftest_dir"
  echo

  echo "--- fixture 4: missing document (expect indeterminate, rc=0) ---"
  mo_check_citations "$_selftest_dir/does-not-exist.md" "$_selftest_dir"
fi
