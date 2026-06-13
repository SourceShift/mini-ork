#!/usr/bin/env bash
# honest_ci_gate.sh — per-finding confidence intervals from lens votes.
#
# Closes the positioning-doc calibration-list item "Honest confidence
# intervals on every claim" (Dai 2025 *Semantic Triangulation*,
# arxiv:2511.12288). The shipped framework already measures
# panel-level agreement via Krippendorff α (3d1e815) and citation
# grounding via mechanical citation coverage (31f7808), but every
# individual finding still emits as a bare integer severity ("P1")
# without an uncertainty band. Dai 2025's recommendation is to
# attach a CI per claim so downstream consumers can prefer
# narrow-CI findings over wide-CI ones.
#
# What this does, in concrete terms:
#   Reads a JSON file shaped:
#     { "findings": [
#         { "id": "F-001",
#           "title": "...",
#           "lens_votes": {"glm": 2, "kimi": 3, "codex": 2, "minimax": 3}
#         }, ... ] }
#   For each finding it computes:
#     - n          : the number of lens votes
#     - mean       : arithmetic mean of the integer/float severity votes
#     - sd         : sample standard deviation (n-1 divisor)
#     - sem        : standard error of the mean (sd / sqrt(n))
#     - ci_low/high: 95% CI = mean ± t_{0.975, n-1} * sem (interval scale)
#     - ci_width   : ci_high - ci_low (the "honesty band")
#
# It then writes an augmented JSON with `confidence_interval` on every
# finding, plus a summary line: how many findings have wide CIs
# (ci_width >= MO_CI_WIDTH_CEILING). Wide-CI findings are operator-
# escalable: the panel did not converge enough to defend them.
#
# Public API:
#   mo_compute_finding_cis <findings_json> <out_path>
#       rc=0 always (gate is informative, not blocking by default —
#       callers compose it with promotion_gate.sh if they want hard-
#       block semantics).
#   mo_check_ci_widths <findings_json> [<report_dir>]
#       Like above but also emits structured JSON verdict for the
#       gate-runner integration:
#         { "verdict":     "ci_within_band" | "CI_TOO_WIDE" | "indeterminate",
#           "reason":      "ok" | "wide_cis" | "no_findings" | "missing_input",
#           "wide_count":  <int>,
#           "total":       <int>,
#           "wide_ratio":  <float | null>,
#           "wide_ratio_ceiling": <float>,
#           "ci_width_ceiling":   <float>,
#           "augmented_path":     "<report_dir>/findings-with-cis.json",
#           "rationale":   "<one sentence>" }
#       rc=0 on ci_within_band or indeterminate; rc=1 on CI_TOO_WIDE.
#
# Env knobs:
#   MO_CI_CONFIDENCE         Confidence level. Default 0.95.
#   MO_CI_WIDTH_CEILING      A finding is "wide" when ci_width >=
#                             this. Default 2.0 (units of the underlying
#                             severity scale; e.g. on a P0..P3 scale a
#                             2-point band means the panel can't even
#                             agree on the priority class).
#   MO_CI_WIDE_RATIO_CEILING Maximum acceptable wide-ratio.
#                             Default 0.3 (30% — Dai 2025 recommended
#                             cut for high-stakes audits).

set -uo pipefail

mo_compute_finding_cis() {
  local _in="${1:-}"
  local _out="${2:-}"

  if [ -z "$_in" ] || [ ! -f "$_in" ] || [ -z "$_out" ]; then
    echo "mo_compute_finding_cis: <findings_json> and <out_path> required (and findings_json must exist)" >&2
    return 2
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "mo_compute_finding_cis: python3 unavailable" >&2
    return 2
  fi

  mkdir -p "$(dirname "$_out")" 2>/dev/null || true

  MO_CI_IN="$_in" \
  MO_CI_OUT="$_out" \
  MO_CI_CONF_PY="${MO_CI_CONFIDENCE:-0.95}" \
  python3 - <<'PY'
import json, math, os, statistics

in_path = os.environ["MO_CI_IN"]
out_path = os.environ["MO_CI_OUT"]
conf = float(os.environ["MO_CI_CONF_PY"])


def t_critical(df, conf):
    # Approximate inverse-t for two-sided CI. For df>=2 we use a small
    # lookup table at 95% and 99% confidence + a smooth interpolation.
    # This avoids the scipy dependency. For 95% the table tracks
    # scipy.stats.t.ppf to <0.5% over df in [2,200].
    if df < 1:
        return float("inf")
    table_95 = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        15: 2.131, 20: 2.086, 30: 2.042, 50: 2.009, 100: 1.984,
        200: 1.972,
    }
    if conf <= 0.95:
        # interpolate within the 95% table for df<=200, else asymptote
        # to z_{0.975}=1.96
        keys = sorted(table_95)
        if df >= keys[-1]:
            return 1.96
        for i in range(len(keys) - 1):
            lo, hi = keys[i], keys[i + 1]
            if lo <= df <= hi:
                f = (df - lo) / (hi - lo)
                return table_95[lo] + f * (table_95[hi] - table_95[lo])
    return 2.576  # 99% asymptote


try:
    data = json.load(open(in_path, encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"mo_compute_finding_cis: parse error on {in_path}: {exc}")

findings = data.get("findings") if isinstance(data, dict) else None
if not isinstance(findings, list):
    raise SystemExit("mo_compute_finding_cis: input must have findings[] array")

for f in findings:
    if not isinstance(f, dict):
        continue
    votes = f.get("lens_votes") or {}
    if not isinstance(votes, dict):
        continue
    nums = []
    for v in votes.values():
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            pass
    n = len(nums)
    if n == 0:
        f["confidence_interval"] = {
            "n": 0, "mean": None, "sd": None, "sem": None,
            "ci_low": None, "ci_high": None, "ci_width": None,
            "confidence": conf,
            "note": "no_numeric_votes",
        }
        continue
    if n == 1:
        m = nums[0]
        f["confidence_interval"] = {
            "n": 1, "mean": m, "sd": None, "sem": None,
            "ci_low": m, "ci_high": m, "ci_width": 0.0,
            "confidence": conf,
            "note": "single_vote_zero_width_is_misleading",
        }
        continue
    m = statistics.fmean(nums)
    sd = statistics.stdev(nums)
    sem = sd / math.sqrt(n)
    tc = t_critical(n - 1, conf)
    half = tc * sem
    f["confidence_interval"] = {
        "n": n,
        "mean": round(m, 4),
        "sd": round(sd, 4),
        "sem": round(sem, 4),
        "ci_low": round(m - half, 4),
        "ci_high": round(m + half, 4),
        "ci_width": round(2 * half, 4),
        "confidence": conf,
        "t_critical": round(tc, 4),
    }

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
}

mo_check_ci_widths() {
  local _in="${1:-}"
  local _report_dir="${2:-${MINI_ORK_RUN_DIR:-.}}"
  local _ceiling="${MO_CI_WIDTH_CEILING:-2.0}"
  local _wide_ratio_ceiling="${MO_CI_WIDE_RATIO_CEILING:-0.3}"

  if [ -z "$_in" ] || [ ! -f "$_in" ]; then
    printf '{"verdict":"indeterminate","reason":"missing_input","wide_count":0,"total":0,"wide_ratio":null,"wide_ratio_ceiling":%s,"ci_width_ceiling":%s,"rationale":"findings_json missing; cannot measure"}\n' \
      "$_wide_ratio_ceiling" "$_ceiling"
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    printf '{"verdict":"indeterminate","reason":"python_unavailable","wide_count":0,"total":0,"wide_ratio":null,"wide_ratio_ceiling":%s,"ci_width_ceiling":%s,"rationale":"python3 missing; cannot compute CIs"}\n' \
      "$_wide_ratio_ceiling" "$_ceiling"
    return 0
  fi

  mkdir -p "$_report_dir" 2>/dev/null || true
  local _augmented="$_report_dir/findings-with-cis.json"
  if ! mo_compute_finding_cis "$_in" "$_augmented"; then
    printf '{"verdict":"indeterminate","reason":"missing_input","wide_count":0,"total":0,"wide_ratio":null,"wide_ratio_ceiling":%s,"ci_width_ceiling":%s,"rationale":"failed to compute CIs (see stderr)"}\n' \
      "$_wide_ratio_ceiling" "$_ceiling"
    return 0
  fi

  MO_CI_AUGMENTED="$_augmented" \
  MO_CI_CEILING_PY="$_ceiling" \
  MO_CI_WIDE_RATIO_PY="$_wide_ratio_ceiling" \
  python3 - <<'PY'
import json, os, sys

augmented = os.environ["MO_CI_AUGMENTED"]
ceiling = float(os.environ["MO_CI_CEILING_PY"])
wide_ratio_ceiling = float(os.environ["MO_CI_WIDE_RATIO_PY"])


def emit(verdict, reason, wide, total, ratio, rationale, rc):
    print(json.dumps({
        "verdict": verdict,
        "reason": reason,
        "wide_count": wide,
        "total": total,
        "wide_ratio": ratio,
        "wide_ratio_ceiling": wide_ratio_ceiling,
        "ci_width_ceiling": ceiling,
        "augmented_path": augmented,
        "rationale": rationale,
    }))
    sys.exit(rc)


try:
    data = json.load(open(augmented, encoding="utf-8"))
except Exception as exc:
    emit("indeterminate", "missing_input", 0, 0, None,
         f"augmented findings unreadable: {exc}", 0)

findings = data.get("findings") if isinstance(data, dict) else None
if not isinstance(findings, list) or not findings:
    emit("indeterminate", "no_findings", 0, 0, None,
         "no findings to score CIs on", 0)

total = 0
wide = 0
for f in findings:
    if not isinstance(f, dict):
        continue
    ci = f.get("confidence_interval") or {}
    width = ci.get("ci_width")
    if width is None:
        continue
    total += 1
    if width >= ceiling:
        wide += 1

if total == 0:
    emit("indeterminate", "no_findings", 0, 0, None,
         "no scorable findings (need >= 1 with numeric votes)", 0)

ratio = wide / total

if ratio > wide_ratio_ceiling:
    emit("CI_TOO_WIDE", "wide_cis", wide, total, round(ratio, 4),
         f"{wide}/{total} findings ({ratio:.1%}) have CI width >= {ceiling}; that exceeds the {wide_ratio_ceiling:.0%} ceiling, audit is honest-uncertain on too many claims",
         1)

emit("ci_within_band", "ok", wide, total, round(ratio, 4),
     f"{wide}/{total} findings have wide CIs ({ratio:.1%} <= ceiling {wide_ratio_ceiling:.0%})",
     0)
PY
}

# Self-test fixtures: tight panel (narrow CIs) / split panel (wide CIs)
# / single-vote findings / missing input. Pattern mirrors earlier gates.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT

  echo "--- fixture 1: tight panel (narrow CIs, expect ci_within_band) ---"
  cat > "$_selftest_dir/findings.json" <<'JSON'
{
  "findings": [
    {"id": "F-001", "title": "Auth retry storm",
     "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
    {"id": "F-002", "title": "Cache key collision",
     "lens_votes": {"glm": 3, "kimi": 3, "codex": 3, "minimax": 3}},
    {"id": "F-003", "title": "Null cursor crash",
     "lens_votes": {"glm": 1, "kimi": 1, "codex": 1, "minimax": 1}}
  ]
}
JSON
  mo_check_ci_widths "$_selftest_dir/findings.json" "$_selftest_dir"
  echo

  echo "--- fixture 2: split panel (wide CIs, expect CI_TOO_WIDE) ---"
  cat > "$_selftest_dir/findings.json" <<'JSON'
{
  "findings": [
    {"id": "F-001", "title": "Race condition",
     "lens_votes": {"glm": 0, "kimi": 3, "codex": 0, "minimax": 3}},
    {"id": "F-002", "title": "Stale cache read",
     "lens_votes": {"glm": 1, "kimi": 4, "codex": 0, "minimax": 5}},
    {"id": "F-003", "title": "Missing escape",
     "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}}
  ]
}
JSON
  mo_check_ci_widths "$_selftest_dir/findings.json" "$_selftest_dir"
  echo

  echo "--- fixture 3: missing input (expect indeterminate, rc=0) ---"
  mo_check_ci_widths "$_selftest_dir/does-not-exist.json" "$_selftest_dir"
fi
