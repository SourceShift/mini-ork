#!/usr/bin/env bash
# krippendorff_alpha_gate.sh — panel inter-rater agreement enforcement.
#
# Implements the Krippendorff's α calibration gate cited throughout
# mini-ork's positioning docs but, until now, NOT enforced. Nasser 2026
# ("Evaluative Fingerprints in Multi-Agent LLM Review", arxiv:2601.05114)
# reports α=0.042 cross-family vs much higher same-family on identical
# tasks — i.e., the multi-agent claim is structurally honest only when
# the panel disagrees enough to be doing independent work. The dual-edge
# observation is that when α is ALSO very low (< ~0.4), the panel is
# noise rather than signal and a single point-verdict cannot be defended.
#
# This gate implements the operator-facing form: compute α across the
# panel's first-round per-item judgments; if α < MO_ALPHA_THRESHOLD,
# emit ALPHA_ESCALATE and refuse to publish until a human resolves.
#
# Public API:
#   mo_check_panel_alpha <run_dir>
#       → JSON on stdout:
#         { "verdict":  "panel_calibrated" | "ALPHA_ESCALATE" | "indeterminate",
#           "reason":   "ok" | "low_alpha" | "insufficient_panel" |
#                       "no_panel_scores" | "python_unavailable",
#           "alpha":         <float | null>,
#           "alpha_threshold": <float>,
#           "lens_count":      <int>,
#           "item_count":      <int>,
#           "score_matrix_path": "<run_dir>/panel-alpha-scores.tsv",
#           "rationale":   "<one sentence>" }
#       rc=0 when α ≥ threshold (panel calibrated), OR when the gate
#         cannot measure (insufficient panel, no scores, python missing)
#         — fail-open by design, with verdict=indeterminate. The
#         framework's safety property holds because callers always
#         render the verdict text, not just the exit code.
#       rc=1 when ALPHA_ESCALATE triggers (low_alpha).
#
# Env knobs:
#   MO_ALPHA_THRESHOLD    α floor below which the gate escalates.
#                         Default 0.4 (Nasser 2026's recommended cut).
#   MO_ALPHA_MIN_LENSES   Minimum panel size for the gate to engage.
#                         Default 2; below this, verdict=indeterminate.
#   MO_ALPHA_INPUT_PATH   Override the score-source path. Default:
#                         <run_dir>/panel-verdict.json with shape
#                         { lens_scores: { <lens>: [<score1>, <score2>, ...] } }.
#                         Each lens's array must have identical length =
#                         number of scored items (DoD probes, rubric
#                         dimensions, etc.).
#
# Why fail-open on indeterminate:
#   A gate that cannot measure should not silently block. The verdict
#   text makes the operator's audit honest ("we couldn't tell"), the
#   exit code keeps the dispatch loop forward-moving. Same shape as
#   lib/coalition_gate.sh and lib/cw_por.sh.

set -uo pipefail

mo_check_panel_alpha() {
  local _run_dir="${1:-}"
  if [ -z "$_run_dir" ] || [ ! -d "$_run_dir" ]; then
    printf '{"verdict":"indeterminate","reason":"missing_run_dir","alpha":null,"alpha_threshold":%s,"lens_count":0,"item_count":0,"rationale":"run_dir not provided or not a directory; cannot measure"}\n' \
      "${MO_ALPHA_THRESHOLD:-0.4}"
    return 0
  fi

  local _threshold="${MO_ALPHA_THRESHOLD:-0.4}"
  local _min_lenses="${MO_ALPHA_MIN_LENSES:-2}"
  local _input_path="${MO_ALPHA_INPUT_PATH:-$_run_dir/panel-verdict.json}"
  local _scores_tsv="$_run_dir/panel-alpha-scores.tsv"

  if ! command -v python3 >/dev/null 2>&1; then
    printf '{"verdict":"indeterminate","reason":"python_unavailable","alpha":null,"alpha_threshold":%s,"lens_count":0,"item_count":0,"rationale":"python3 missing; cannot compute alpha"}\n' \
      "$_threshold"
    return 0
  fi

  MO_ALPHA_INPUT="$_input_path" \
  MO_ALPHA_THRESHOLD_PY="$_threshold" \
  MO_ALPHA_MIN_LENSES_PY="$_min_lenses" \
  MO_ALPHA_SCORES_TSV="$_scores_tsv" \
  python3 - <<'PY'
import json, math, os, sys

threshold = float(os.environ.get("MO_ALPHA_THRESHOLD_PY", "0.4"))
min_lenses = int(os.environ.get("MO_ALPHA_MIN_LENSES_PY", "2"))
input_path = os.environ["MO_ALPHA_INPUT"]
scores_tsv = os.environ["MO_ALPHA_SCORES_TSV"]


def emit(verdict, reason, alpha, lens_count, item_count, rationale, rc):
    print(json.dumps({
        "verdict": verdict,
        "reason": reason,
        "alpha": alpha if alpha is not None else None,
        "alpha_threshold": threshold,
        "lens_count": lens_count,
        "item_count": item_count,
        "score_matrix_path": scores_tsv,
        "rationale": rationale,
    }))
    sys.exit(rc)


if not os.path.isfile(input_path):
    emit("indeterminate", "no_panel_scores", None, 0, 0,
         f"score input {input_path} missing; gate cannot measure",
         0)

try:
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)
except Exception as exc:
    emit("indeterminate", "no_panel_scores", None, 0, 0,
         f"score input {input_path} failed to parse: {exc}", 0)

lens_scores = data.get("lens_scores") if isinstance(data, dict) else None
if not isinstance(lens_scores, dict) or not lens_scores:
    emit("indeterminate", "no_panel_scores", None, 0, 0,
         "panel-verdict.json missing lens_scores object; cannot measure",
         0)

# Validate shape: every lens MUST have an array of identical length, and
# every entry MUST be numeric. Reject silently-asymmetric input rather
# than computing alpha on a ragged matrix.
lens_names = list(lens_scores.keys())
arrays = []
for name in lens_names:
    arr = lens_scores[name]
    if not isinstance(arr, list):
        emit("indeterminate", "no_panel_scores", None, len(lens_names), 0,
             f"lens {name} score is not a list", 0)
    coerced = []
    for x in arr:
        try:
            coerced.append(float(x))
        except (TypeError, ValueError):
            emit("indeterminate", "no_panel_scores", None, len(lens_names), 0,
                 f"lens {name} contains non-numeric score: {x!r}", 0)
    arrays.append(coerced)

lengths = {len(a) for a in arrays}
if len(lengths) != 1:
    emit("indeterminate", "no_panel_scores", None, len(lens_names), 0,
         f"ragged score matrix; lens arrays have lengths {sorted(lengths)}",
         0)
item_count = lengths.pop()
lens_count = len(lens_names)

if lens_count < min_lenses or item_count == 0:
    emit("indeterminate", "insufficient_panel", None, lens_count, item_count,
         f"need >= {min_lenses} lenses and >= 1 item; got {lens_count}/{item_count}",
         0)

# Persist the score matrix for audit. TSV is enough — operators grep, not parse.
try:
    os.makedirs(os.path.dirname(scores_tsv), exist_ok=True)
    with open(scores_tsv, "w", encoding="utf-8") as f:
        f.write("item_index\t" + "\t".join(lens_names) + "\n")
        for i in range(item_count):
            row = [str(i)] + [f"{arrays[j][i]:.6g}" for j in range(lens_count)]
            f.write("\t".join(row) + "\n")
except Exception:
    pass  # audit aid only; failure here is not gate-blocking

# Krippendorff's alpha for interval data:
#   alpha = 1 - (D_o / D_e)
#   D_o = sum over items of pairwise squared diff / pair_count
#   D_e = same on the marginal distribution
#
# This implementation is the standard interval-metric form (Hayes & Krippendorff
# 2007 sec. 3.2). It assumes no missing values, which our shape gate above
# enforces. For nominal/ordinal data the difference function changes, but
# panel scores from rubric verifiers are interval by construction.
def pairwise_disagreement(values):
    n = len(values)
    if n < 2:
        return 0.0, 0
    total = 0.0
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += (values[i] - values[j]) ** 2
            pairs += 1
    return total, pairs


obs_total = 0.0
obs_pairs = 0
for i in range(item_count):
    col = [arrays[j][i] for j in range(lens_count)]
    s, p = pairwise_disagreement(col)
    obs_total += s
    obs_pairs += p

flat = [arrays[j][i] for j in range(lens_count) for i in range(item_count)]
exp_total, exp_pairs = pairwise_disagreement(flat)

if obs_pairs == 0 or exp_pairs == 0 or exp_total == 0.0:
    # Constant marginals (everyone scored everything identically) — by
    # convention alpha is reported as 1.0 (perfect agreement) since D_o
    # is also 0. Operators interpret "1.0 across single-value panels" as
    # "consensus is trivial; ρ-diversity already caught this elsewhere".
    alpha = 1.0
else:
    D_o = obs_total / obs_pairs
    D_e = exp_total / exp_pairs
    alpha = 1.0 - (D_o / D_e)

if math.isnan(alpha) or math.isinf(alpha):
    emit("indeterminate", "no_panel_scores", None, lens_count, item_count,
         "alpha computation produced non-finite value; check score variance",
         0)

if alpha < threshold:
    emit("ALPHA_ESCALATE", "low_alpha", alpha, lens_count, item_count,
         f"alpha {alpha:.3f} < threshold {threshold} across {lens_count} lenses x {item_count} items — panel divergence too high for safe synthesis; escalate to human",
         1)

emit("panel_calibrated", "ok", alpha, lens_count, item_count,
     f"alpha {alpha:.3f} >= threshold {threshold} across {lens_count} lenses x {item_count} items",
     0)
PY
}

# Self-test fixtures: run directly to see verdicts. Pattern mirrors
# lib/coalition_gate.sh's own self-test block.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT

  echo "--- fixture 1: high agreement (alpha near 1, expect pass) ---"
  cat > "$_selftest_dir/panel-verdict.json" <<JSON
{
  "lens_scores": {
    "glm":     [8, 7, 9, 8, 7],
    "kimi":    [8, 7, 9, 8, 7],
    "codex":   [8, 7, 9, 8, 7],
    "minimax": [8, 7, 9, 8, 7]
  }
}
JSON
  mo_check_panel_alpha "$_selftest_dir"
  echo

  echo "--- fixture 2: moderate agreement (alpha around 0.5-0.7, expect pass) ---"
  cat > "$_selftest_dir/panel-verdict.json" <<JSON
{
  "lens_scores": {
    "glm":     [8, 6, 9, 7, 8],
    "kimi":    [7, 7, 8, 6, 7],
    "codex":   [9, 5, 9, 8, 8],
    "minimax": [8, 6, 8, 7, 7]
  }
}
JSON
  mo_check_panel_alpha "$_selftest_dir"
  echo

  echo "--- fixture 3: low agreement (alpha < 0.4, expect ALPHA_ESCALATE) ---"
  cat > "$_selftest_dir/panel-verdict.json" <<JSON
{
  "lens_scores": {
    "glm":     [1, 9, 2, 8, 3],
    "kimi":    [9, 1, 8, 2, 9],
    "codex":   [2, 8, 3, 7, 2],
    "minimax": [8, 2, 9, 3, 8]
  }
}
JSON
  mo_check_panel_alpha "$_selftest_dir"
  echo

  echo "--- fixture 4: missing input (expect indeterminate, rc=0) ---"
  rm -f "$_selftest_dir/panel-verdict.json"
  mo_check_panel_alpha "$_selftest_dir"
  echo

  echo "--- fixture 5: ragged matrix (expect indeterminate, rc=0) ---"
  cat > "$_selftest_dir/panel-verdict.json" <<JSON
{
  "lens_scores": {
    "glm":     [8, 6, 9],
    "kimi":    [7, 7],
    "codex":   [9],
    "minimax": [8, 6, 8, 7]
  }
}
JSON
  mo_check_panel_alpha "$_selftest_dir"
fi
