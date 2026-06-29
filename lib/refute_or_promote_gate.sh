#!/usr/bin/env bash
# refute_or_promote_gate.sh — adversarial fabricated-bug injection oracle.
#
# Implements the Refute-or-Promote gate cited in the positioning-doc
# calibration list (Agarwal 2026, arxiv:2604.19049). The unit measurement
# question is: "does the validator actually distinguish real findings
# from planted fakes?" A validator that confidently flags fabricated
# bugs as real is hallucinating, regardless of how rigorous its review
# prose sounds. This gate exposes that property mechanically.
#
# The full Refute-or-Promote pipeline is a recipe-level orchestration:
#   1) generate N unique fabrication markers + record their identifiers,
#   2) splice them into a corpus copy under fixed offsets,
#   3) run the audit recipe on the modified corpus,
#   4) parse the audit's findings list,
#   5) count how many findings cite fabrication identifiers,
#   6) compute FP-survival rate = fp_count / N,
#   7) compare against MO_REFUTE_FP_CEILING.
#
# This file provides the two leaf primitives (1 and 5+6+7) and leaves
# corpus mutation + audit dispatch to the caller, because those are
# recipe-specific. The fabrication design is deliberately tractable:
# each marker is a synthetic file path that does NOT exist in the repo,
# paired with a synthetic claim. A validator that reads its citations
# (citation_verifier_mechanical.sh shipped at 31f7808) will reject the
# fake; a validator that hallucinates will accept it.
#
# Public API:
#   mo_generate_fabrications <count> <out_json_path>
#       Writes a JSON array of fabrication records:
#         [{"id": "<short hex>",
#           "path": "<fake path containing the id>",
#           "line": <int>,
#           "claim": "<synthetic claim>"}, ...]
#       rc=0 on success.
#
#   mo_check_fabrication_survival <findings_path> <fabrications_json> [<report_dir>]
#       findings_path is a text or markdown file containing the audit's
#       findings. Detection counts a fabrication as "survived" when its
#       id token appears anywhere in the findings text — i.e., the
#       validator cited the fake path as if it were real evidence.
#       Emits JSON:
#         { "verdict":   "validator_grounded" | "REFUTE_FAILED" | "indeterminate",
#           "reason":    "ok" | "high_fp_survival" | "missing_inputs",
#           "fp_count":  <int>,
#           "fp_total":  <int>,
#           "fp_rate":   <float | null>,
#           "fp_ceiling":<float>,
#           "report_path": "<report_dir>/refute-survival.tsv",
#           "rationale": "<one sentence>" }
#       rc=0 on validator_grounded (or indeterminate),
#       rc=1 on REFUTE_FAILED.
#
# Env knobs:
#   MO_REFUTE_FP_CEILING       Max acceptable fabrication-survival rate.
#                              Default 0.1 (10% — Agarwal 2026's
#                              recommended cut for high-stakes audits).
#   MO_REFUTE_PREFIX           Marker prefix; default "__fabricated_".
#                              The 7-char random suffix gives 16^7 ≈
#                              268M unique ids per run.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=lib/gates_common.sh
[ -f "${MINI_ORK_ROOT}/lib/gates_common.sh" ] && \
  source "${MINI_ORK_ROOT}/lib/gates_common.sh" 2>/dev/null || true

mo_generate_fabrications() {
  local _count="${1:-}"
  local _out="${2:-}"

  if [ -z "$_count" ] || ! [[ "$_count" =~ ^[0-9]+$ ]] || [ "$_count" -lt 1 ]; then
    echo "mo_generate_fabrications: count must be a positive integer" >&2
    return 2
  fi
  if [ -z "$_out" ]; then
    echo "mo_generate_fabrications: out_json_path required" >&2
    return 2
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "mo_generate_fabrications: python3 unavailable" >&2
    return 2
  fi

  mkdir -p "$(dirname "$_out")" 2>/dev/null || true

  MO_REF_COUNT="$_count" \
  MO_REF_OUT="$_out" \
  MO_REF_PREFIX="${MO_REFUTE_PREFIX:-__fabricated_}" \
  python3 - <<'PY'
import json, os, secrets

count = int(os.environ["MO_REF_COUNT"])
out_path = os.environ["MO_REF_OUT"]
prefix = os.environ["MO_REF_PREFIX"]

# Stylized synthetic claims, kept short and grammatical. The exact text
# does not matter for the gate's correctness — only the id token does —
# but readable claims help operators understand the report.
claim_templates = [
    "Race condition between the {id} handler and its retry path.",
    "Silent catch in {id} swallows the original error.",
    "{id} writes to the shared cache without locking.",
    "Missing null check on {id}'s upstream response.",
    "{id} returns a stale result when the TTL expires mid-call.",
    "Off-by-one in {id}'s pagination cursor.",
    "{id} double-increments the retry counter on partial failures.",
    "{id} truncates UTF-8 mid-codepoint.",
    "{id} leaks a goroutine when the parent context is cancelled.",
    "{id} compares structs by reference instead of value.",
]

records = []
for i in range(count):
    suffix = secrets.token_hex(4)[:7]
    ident = f"{prefix}{suffix}"
    template = claim_templates[i % len(claim_templates)]
    records.append({
        "id": ident,
        "path": f"src/{ident}/handler.ts",
        "line": 30 + (i * 11) % 200,
        "claim": template.format(id=ident),
    })

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(records, f, indent=2)
    f.write("\n")
PY
}

mo_check_fabrication_survival() {
  local _findings="${1:-}"
  local _fabrications="${2:-}"
  local _report_dir="${3:-${MINI_ORK_RUN_DIR:-.}}"

  local _ceiling="${MO_REFUTE_FP_CEILING:-0.1}"
  local _report_path="$_report_dir/refute-survival.tsv"

  if [ -z "$_findings" ] || [ ! -f "$_findings" ] || [ -z "$_fabrications" ] || [ ! -f "$_fabrications" ]; then
    printf '{"verdict":"indeterminate","reason":"missing_inputs","fp_count":0,"fp_total":0,"fp_rate":null,"fp_ceiling":%s,"rationale":"findings_path or fabrications_json missing; cannot measure"}\n' \
      "$_ceiling"
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    printf '{"verdict":"indeterminate","reason":"python_unavailable","fp_count":0,"fp_total":0,"fp_rate":null,"fp_ceiling":%s,"rationale":"python3 missing; cannot count survivors"}\n' \
      "$_ceiling"
    return 0
  fi

  mkdir -p "$_report_dir" 2>/dev/null || true

  local _out _rc
  _out=$(MO_REF_FINDINGS="$_findings" \
  MO_REF_FAB="$_fabrications" \
  MO_REF_CEILING_PY="$_ceiling" \
  MO_REF_REPORT="$_report_path" \
  python3 - <<'PY'
import json, os, sys

findings_path = os.environ["MO_REF_FINDINGS"]
fab_path = os.environ["MO_REF_FAB"]
ceiling = float(os.environ["MO_REF_CEILING_PY"])
report_path = os.environ["MO_REF_REPORT"]


def emit(verdict, reason, fp_count, fp_total, fp_rate, rationale, rc):
    print(json.dumps({
        "verdict": verdict,
        "reason": reason,
        "fp_count": fp_count,
        "fp_total": fp_total,
        "fp_rate": fp_rate,
        "fp_ceiling": ceiling,
        "report_path": report_path,
        "rationale": rationale,
    }))
    sys.exit(rc)


try:
    findings_text = open(findings_path, encoding="utf-8").read()
except Exception as exc:
    emit("indeterminate", "missing_inputs", 0, 0, None,
         f"findings_path unreadable: {exc}", 0)

try:
    fabrications = json.load(open(fab_path, encoding="utf-8"))
except Exception as exc:
    emit("indeterminate", "missing_inputs", 0, 0, None,
         f"fabrications_json unreadable: {exc}", 0)

if not isinstance(fabrications, list) or not fabrications:
    emit("indeterminate", "missing_inputs", 0, 0, None,
         "fabrications_json must be a non-empty array", 0)

survivors = []
for fab in fabrications:
    if not isinstance(fab, dict):
        continue
    ident = fab.get("id") or ""
    if not ident:
        continue
    if ident in findings_text:
        survivors.append({
            "id": ident,
            "path": fab.get("path", ""),
            "line": fab.get("line", 0),
        })

fp_total = sum(1 for f in fabrications if isinstance(f, dict) and f.get("id"))
fp_count = len(survivors)
fp_rate = (fp_count / fp_total) if fp_total else 0.0

try:
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("id\tpath\tline\tsurvived\n")
        survivor_ids = {s["id"] for s in survivors}
        for fab in fabrications:
            if not isinstance(fab, dict):
                continue
            ident = fab.get("id") or ""
            survived = "yes" if ident in survivor_ids else "no"
            f.write(f"{ident}\t{fab.get('path', '')}\t{fab.get('line', 0)}\t{survived}\n")
except Exception:
    pass  # audit aid only

if fp_rate > ceiling:
    emit("REFUTE_FAILED", "high_fp_survival", fp_count, fp_total, round(fp_rate, 4),
         f"validator promoted {fp_count} of {fp_total} fabricated findings ({fp_rate:.1%} > ceiling {ceiling:.0%}); validator is not safely refuting plants",
         1)

emit("validator_grounded", "ok", fp_count, fp_total, round(fp_rate, 4),
     f"validator refuted {fp_total - fp_count} of {fp_total} fabrications ({fp_rate:.1%} <= ceiling {ceiling:.0%})",
     0)
PY
)
  _rc=$?
  printf '%s\n' "$_out"

  local _verdict
  _verdict=$(printf '%s' "$_out" | jq -r '.verdict // ""' 2>/dev/null)
  if [ "$_verdict" = "REFUTE_FAILED" ] && declare -f mo_grounded_rejection >/dev/null 2>&1; then
    local _reason _rationale
    _reason=$(printf '%s' "$_out" | jq -r '.reason // ""' 2>/dev/null)
    _rationale=$(printf '%s' "$_out" | jq -r '.rationale // ""' 2>/dev/null)
    mo_grounded_rejection "refute_or_promote" "fail" "$_reason" "$_rationale" \
      "strengthen the validator: the surviving fabrications are false positives that must be refuted before promotion" \
      '[]' "${MINI_ORK_RUN_ID:-}" >/dev/null 2>&1 || true
  fi
  return $_rc
}

# Self-test: 3 fixtures (clean validator / hallucinating validator /
# missing inputs). Pattern mirrors lib/krippendorff_alpha_gate.sh and
# lib/citation_verifier_mechanical.sh.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT

  echo "--- step A: generate 5 fabrications ---"
  mo_generate_fabrications 5 "$_selftest_dir/fab.json"
  echo "generated $(jq -r '. | length' "$_selftest_dir/fab.json" 2>/dev/null || python3 -c "import json; print(len(json.load(open('$_selftest_dir/fab.json'))))")"
  python3 -c "import json; [print(f\"  {x['id']}\") for x in json.load(open('$_selftest_dir/fab.json'))]"
  echo

  echo "--- fixture 1: clean validator (no fabrications cited, expect pass) ---"
  cat > "$_selftest_dir/findings-clean.md" <<'MD'
## Findings

1. Real issue in src/auth/login.ts:42 — token expiration not validated.
2. Race condition in src/db/pool.ts:118 — connection reuse before reset.
3. Missing null check in src/api/handler.ts:7.
MD
  mo_check_fabrication_survival "$_selftest_dir/findings-clean.md" "$_selftest_dir/fab.json" "$_selftest_dir"
  echo

  echo "--- fixture 2: hallucinating validator (cites 3 of 5 fabrications, expect REFUTE_FAILED) ---"
  python3 -c "
import json
fabs = json.load(open('$_selftest_dir/fab.json'))
with open('$_selftest_dir/findings-bad.md', 'w') as f:
    f.write('## Findings\n\n')
    for x in fabs[:3]:
        f.write(f'- {x[\"path\"]}:{x[\"line\"]} — {x[\"claim\"]}\n')
    f.write('- src/legitimate/foo.ts:10 — real finding\n')
"
  mo_check_fabrication_survival "$_selftest_dir/findings-bad.md" "$_selftest_dir/fab.json" "$_selftest_dir"
  echo

  echo "--- fixture 3: missing findings file (expect indeterminate, rc=0) ---"
  mo_check_fabrication_survival "$_selftest_dir/does-not-exist.md" "$_selftest_dir/fab.json" "$_selftest_dir"
fi
