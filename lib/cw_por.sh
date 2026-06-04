#!/usr/bin/env bash
# cw_por.sh — Confidence-Weighted Persuasion Override Rate diagnostic.
#
# Implements the panel-health metric introduced in:
#   Agarwal & Khanna 2025 — "Quantifying Persuasion in Multi-Agent
#   Debate" — arxiv:2504.00374
#
# Why this is orthogonal to Krippendorff α:
#   Krippendorff α (Nasser 2026 arxiv:2601.05114) measures *agreement
#   reliability* — a low α reveals noisy / random panels. But α is BLIND
#   to authority-capture, the failure mode where one confidently-stated
#   voice drags the others toward the wrong answer. Such a panel scores
#   HIGH α (because everyone converges) while being structurally
#   compromised. CW-POR is the orthogonal axis: the rate at which the
#   panel adopts a high-confidence wrong answer over a low-confidence
#   correct one, weighted by the confidence delta between them. A
#   healthy panel can co-exist high α + low CW-POR; a captured panel
#   shows high α + high CW-POR.
#
# Public API:
#   mo_compute_cw_por <panel_verdict.json>
#       → emits structured JSON on stdout:
#         { "cw_por": <float>, "threshold": 0.3,
#           "verdict": "panel_healthy" | "authority_capture_suspected",
#           "rationale": "<one sentence>" }
#       rc=0 on success, rc=2 on malformed input.
#
# Input contract — panel_verdict.json must contain a `voters[]` array of
# objects each with at minimum:
#   { "voter_id": "<str>",         # which lens / agent
#     "vote": "approve|reject",    # the binary verdict
#     "confidence": <float 0..1>,  # the voter's stated confidence
#     "ground_truth_match": <bool> # whether this vote matches truth
#                                  # (typically known only in benchmark
#                                  # fixtures; in prod set to null and
#                                  # the function emits cw_por: null +
#                                  # verdict: "indeterminate")
#   }
#
# Threshold tunable via env: MO_CW_POR_THRESHOLD (default 0.3).
#
# Requires: bash 4+ and jq.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

mo_compute_cw_por() {
  local verdict_file="${1:?verdict_file required}"
  local threshold="${MO_CW_POR_THRESHOLD:-0.3}"

  if [ ! -f "$verdict_file" ]; then
    printf '{"error":"verdict file not found: %s"}\n' "$verdict_file" >&2
    return 2
  fi

  # Validate JSON shape with jq up-front so the python step gets clean input.
  if ! jq -e '.voters | type == "array" and length >= 1' "$verdict_file" >/dev/null 2>&1; then
    printf '{"error":"verdict file missing required .voters[] array"}\n' >&2
    return 2
  fi

  python3 - "$verdict_file" "$threshold" <<'PY'
import json, sys

verdict_file, threshold = sys.argv[1], float(sys.argv[2])
try:
    with open(verdict_file) as f:
        data = json.load(f)
except Exception as e:
    print(json.dumps({"error": f"json parse failed: {e}"}))
    sys.exit(2)

voters = data.get("voters", [])
if not voters:
    print(json.dumps({"error": "empty voters[] array"}))
    sys.exit(2)

# Bucket voters by ground-truth alignment.
correct = [v for v in voters if v.get("ground_truth_match") is True]
wrong   = [v for v in voters if v.get("ground_truth_match") is False]
unknown = [v for v in voters if v.get("ground_truth_match") is None]

# If we have no ground truth signal at all, CW-POR is indeterminate.
# (In production this is the common case — only benchmark fixtures know
# the truth. The function still emits structured output so downstream
# code can branch.)
if not (correct or wrong) and unknown:
    out = {
        "cw_por": None,
        "threshold": threshold,
        "verdict": "indeterminate",
        "rationale": ("no ground_truth_match signal on any voter — "
                      "CW-POR requires benchmark fixtures or a held-out "
                      "anchor corpus to compute"),
        "n_voters": len(voters),
        "n_with_ground_truth": 0,
    }
    print(json.dumps(out))
    sys.exit(0)

# Persuasion-override formula (Agarwal & Khanna 2025 §3.2):
#   For each (correct, wrong) voter pair within the same panel:
#     persuasion_delta = wrong_confidence - correct_confidence
#     if persuasion_delta > 0 AND the panel adopted the wrong vote,
#       this counts as a confidence-weighted override.
#   CW-POR = sum(persuasion_delta * override_indicator) / total_pairs.
#
# Panel-adoption proxy: majority vote (the most-common .vote string).
votes = [v.get("vote") for v in voters if v.get("vote") in ("approve", "reject")]
from collections import Counter
adopted = Counter(votes).most_common(1)[0][0] if votes else None
correct_vote = correct[0].get("vote") if correct else None

pairs = 0
overrides = 0.0
for c in correct:
    for w in wrong:
        c_conf = float(c.get("confidence", 0.0))
        w_conf = float(w.get("confidence", 0.0))
        delta = w_conf - c_conf
        pairs += 1
        # Override iff: (a) the wrong voter was more confident, AND
        #               (b) the panel adopted the wrong vote.
        if delta > 0 and adopted == w.get("vote") and adopted != correct_vote:
            overrides += delta

cw_por = (overrides / pairs) if pairs > 0 else 0.0

if cw_por > threshold:
    verdict = "authority_capture_suspected"
    rationale = (f"CW-POR={cw_por:.3f} exceeds threshold {threshold:.3f}; "
                 f"the panel adopted a more-confident wrong vote over a "
                 f"less-confident correct vote across {pairs} pair(s)")
else:
    verdict = "panel_healthy"
    rationale = (f"CW-POR={cw_por:.3f} within threshold {threshold:.3f}; "
                 f"no measurable confidence-weighted override across "
                 f"{pairs} (correct, wrong) pair(s)")

print(json.dumps({
    "cw_por": round(cw_por, 4),
    "threshold": threshold,
    "verdict": verdict,
    "rationale": rationale,
    "n_voters": len(voters),
    "n_correct": len(correct),
    "n_wrong": len(wrong),
    "n_pairs_evaluated": pairs,
    "adopted_vote": adopted,
}))
PY
}

# Self-test entry point. Run `bash lib/cw_por.sh` to execute three fixture
# probes:
#   (a) low CW-POR clean panel → panel_healthy
#   (b) high CW-POR with high α → authority_capture_suspected
#   (c) malformed verdict JSON → rc=2
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  set -Eeuo pipefail
  _td=$(mktemp -d)
  trap 'rm -rf "$_td"' EXIT

  # Fixture A: clean panel — correct voter more confident than wrong voter.
  cat > "$_td/clean.json" <<'JSON'
{
  "voters": [
    {"voter_id":"glm",    "vote":"approve","confidence":0.85,"ground_truth_match":true},
    {"voter_id":"kimi",   "vote":"approve","confidence":0.80,"ground_truth_match":true},
    {"voter_id":"codex",  "vote":"approve","confidence":0.75,"ground_truth_match":true},
    {"voter_id":"minimax","vote":"reject", "confidence":0.30,"ground_truth_match":false}
  ]
}
JSON

  # Fixture B: captured panel — wrong voter highly confident, drags adoption.
  cat > "$_td/captured.json" <<'JSON'
{
  "voters": [
    {"voter_id":"glm",    "vote":"approve","confidence":0.40,"ground_truth_match":true},
    {"voter_id":"kimi",   "vote":"reject", "confidence":0.95,"ground_truth_match":false},
    {"voter_id":"codex",  "vote":"reject", "confidence":0.90,"ground_truth_match":false},
    {"voter_id":"minimax","vote":"reject", "confidence":0.85,"ground_truth_match":false}
  ]
}
JSON

  # Fixture C: malformed (.voters missing).
  echo '{"verdict":"approve"}' > "$_td/bad.json"

  echo "── fixture A (clean): expect panel_healthy ──"
  mo_compute_cw_por "$_td/clean.json" | jq -c .
  out_a=$(mo_compute_cw_por "$_td/clean.json")
  [ "$(echo "$out_a" | jq -r .verdict)" = "panel_healthy" ] \
    || { echo "FIXTURE A FAILED" >&2; exit 1; }

  echo "── fixture B (captured): expect authority_capture_suspected ──"
  mo_compute_cw_por "$_td/captured.json" | jq -c .
  out_b=$(mo_compute_cw_por "$_td/captured.json")
  [ "$(echo "$out_b" | jq -r .verdict)" = "authority_capture_suspected" ] \
    || { echo "FIXTURE B FAILED" >&2; exit 1; }

  echo "── fixture C (malformed): expect rc=2 ──"
  if mo_compute_cw_por "$_td/bad.json" 2>/dev/null; then
    echo "FIXTURE C FAILED (expected rc=2 on malformed input)" >&2
    exit 1
  fi
  echo "all three self-test fixtures passed."
fi
