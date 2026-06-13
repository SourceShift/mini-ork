#!/usr/bin/env bash
# langfuse_score_mapper.sh — verdict/rollback/promotion → trace score.
#
# Implements roadmap Phase 3 item 8: Langfuse score mapping. The
# planned OTel-Langfuse exporter (docs/architecture/otel-langfuse.md)
# emits one trace per run. This file converts mini-ork's structured
# verdicts, rollback events, and promotion decisions into numeric
# Langfuse trace scores so traces self-rank by quality without
# operator effort.
#
# Score conventions (Langfuse "numeric" scores, -1.0 ..= +1.0):
#   reviewer APPROVE                        →  +1.0
#   reviewer REQUEST_CHANGES                →  -0.5
#   reviewer ESCALATE                       →   0.0  (operator decides)
#   verifier pass                           →  +0.5
#   verifier fail                           →  -0.5
#   verifier vacuous                        →  -0.25 (nothing checked is not pass)
#   panel COALITION_ABORT (lib/coalition_gate)   → -0.75
#   panel ALPHA_ESCALATE  (lib/krippendorff)     → -0.5
#   panel CITATION_UNDERCOVERED                  → -0.5
#   panel REFUTE_FAILED  (lib/refute_or_promote) → -0.75
#   panel CI_TOO_WIDE    (lib/honest_ci)         → -0.25
#   rollback fired                          →  -1.0
#   promoted (promotion_records.decision=promoted)         → +1.0
#   quarantined                                            → -1.0
#   pending_human_approval                                 →  0.0
#
# Public API:
#   langfuse_score_for_verdict <verdict_json>
#       Reads a verdict-shaped JSON object from stdin OR from the
#       arg path. Emits one JSON line per scoreable event:
#         {"name": "<event_name>",
#          "value": <numeric_score>,
#          "data_type": "NUMERIC",
#          "comment": "<one-line rationale>"}
#       Multiple events in one input emit multiple lines.
#       rc=0 always.
#   langfuse_score_table
#       Prints the score table for operator review (no input).
#
# Why this exists in honesty terms:
#   The framework already records verdicts and rollback events in
#   structured form. Without the mapping, every team writes its
#   own verdict→score conversion when wiring to a trace platform —
#   that produces inconsistent benchmarks across organizations
#   running the same recipe. Standardizing the mapping makes the
#   numbers comparable.

set -uo pipefail

# Score table (single source of truth). Adjust here, callers consume.
_LANGFUSE_SCORES_REVIEWER_APPROVE="1.0"
_LANGFUSE_SCORES_REVIEWER_REQUEST_CHANGES="-0.5"
_LANGFUSE_SCORES_REVIEWER_ESCALATE="0.0"
_LANGFUSE_SCORES_VERIFIER_PASS="0.5"
_LANGFUSE_SCORES_VERIFIER_FAIL="-0.5"
_LANGFUSE_SCORES_VERIFIER_VACUOUS="-0.25"
_LANGFUSE_SCORES_COALITION_ABORT="-0.75"
_LANGFUSE_SCORES_ALPHA_ESCALATE="-0.5"
_LANGFUSE_SCORES_CITATION_UNDERCOVERED="-0.5"
_LANGFUSE_SCORES_REFUTE_FAILED="-0.75"
_LANGFUSE_SCORES_CI_TOO_WIDE="-0.25"
_LANGFUSE_SCORES_ROLLBACK_FIRED="-1.0"
_LANGFUSE_SCORES_PROMOTED="1.0"
_LANGFUSE_SCORES_QUARANTINED="-1.0"
_LANGFUSE_SCORES_PENDING_APPROVAL="0.0"

langfuse_score_table() {
  cat <<EOF
event                          score   rationale
─────                          ─────   ─────────
reviewer APPROVE               +1.0    explicit approval, full credit
reviewer REQUEST_CHANGES       -0.5    review found defects; not a pass
reviewer ESCALATE               0.0    operator decides; do not pre-bias
verifier pass                  +0.5    deterministic pass, half-credit
                                       (full credit reserved for reviewer)
verifier fail                  -0.5    deterministic fail
verifier vacuous               -0.25   nothing checked != pass
panel COALITION_ABORT          -0.75   structural panel failure (ρ + family)
panel ALPHA_ESCALATE           -0.5    α<0.4, panel divergence too high
panel CITATION_UNDERCOVERED    -0.5    citations failed to resolve
panel REFUTE_FAILED            -0.75   validator hallucinated fabrications
panel CI_TOO_WIDE              -0.25   per-finding CIs honest-uncertain
rollback fired                 -1.0    publish was reverted post-hoc
promoted                       +1.0    candidate passed promotion gate
quarantined                    -1.0    candidate failed promotion gate
pending_human_approval          0.0    awaiting decision
EOF
}

langfuse_score_for_verdict() {
  local _input="${1:-}"
  local _stdin_payload=""

  if [ -z "$_input" ] && [ -t 0 ]; then
    echo "langfuse_score_for_verdict: usage: langfuse_score_for_verdict <verdict_json_path>" >&2
    echo "                                 OR echo '<json>' | langfuse_score_for_verdict" >&2
    return 2
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "langfuse_score_for_verdict: python3 unavailable" >&2
    return 2
  fi

  # Buffer stdin BEFORE invoking the python heredoc — the heredoc binds
  # its own stdin to the inline script, so reading stdin from inside
  # python would see the script bytes, not the operator's JSON.
  if [ -z "$_input" ]; then
    _stdin_payload=$(cat)
    _input="$_stdin_payload"
  fi

  # Export the score constants so the python heredoc can read them
  # without re-encoding the table.
  MO_LF_APPROVE="$_LANGFUSE_SCORES_REVIEWER_APPROVE" \
  MO_LF_REQUEST_CHANGES="$_LANGFUSE_SCORES_REVIEWER_REQUEST_CHANGES" \
  MO_LF_ESCALATE="$_LANGFUSE_SCORES_REVIEWER_ESCALATE" \
  MO_LF_VPASS="$_LANGFUSE_SCORES_VERIFIER_PASS" \
  MO_LF_VFAIL="$_LANGFUSE_SCORES_VERIFIER_FAIL" \
  MO_LF_VVACUOUS="$_LANGFUSE_SCORES_VERIFIER_VACUOUS" \
  MO_LF_COALITION="$_LANGFUSE_SCORES_COALITION_ABORT" \
  MO_LF_ALPHA="$_LANGFUSE_SCORES_ALPHA_ESCALATE" \
  MO_LF_CITATION="$_LANGFUSE_SCORES_CITATION_UNDERCOVERED" \
  MO_LF_REFUTE="$_LANGFUSE_SCORES_REFUTE_FAILED" \
  MO_LF_CIWIDE="$_LANGFUSE_SCORES_CI_TOO_WIDE" \
  MO_LF_ROLLBACK="$_LANGFUSE_SCORES_ROLLBACK_FIRED" \
  MO_LF_PROMOTED="$_LANGFUSE_SCORES_PROMOTED" \
  MO_LF_QUARANTINED="$_LANGFUSE_SCORES_QUARANTINED" \
  MO_LF_PENDING="$_LANGFUSE_SCORES_PENDING_APPROVAL" \
  MO_LF_INPUT="$_input" \
  python3 - <<'PY'
import json, os, sys

table = {
    "reviewer_approve":         (float(os.environ["MO_LF_APPROVE"]),         "explicit approval"),
    "reviewer_request_changes": (float(os.environ["MO_LF_REQUEST_CHANGES"]), "review found defects"),
    "reviewer_escalate":        (float(os.environ["MO_LF_ESCALATE"]),         "operator decides"),
    "verifier_pass":            (float(os.environ["MO_LF_VPASS"]),             "deterministic pass"),
    "verifier_fail":            (float(os.environ["MO_LF_VFAIL"]),             "deterministic fail"),
    "verifier_vacuous":         (float(os.environ["MO_LF_VVACUOUS"]),          "nothing checked"),
    "coalition_abort":          (float(os.environ["MO_LF_COALITION"]),         "rho + family failure"),
    "alpha_escalate":           (float(os.environ["MO_LF_ALPHA"]),             "alpha below threshold"),
    "citation_undercovered":    (float(os.environ["MO_LF_CITATION"]),          "citations missing"),
    "refute_failed":            (float(os.environ["MO_LF_REFUTE"]),            "fabrications survived"),
    "ci_too_wide":              (float(os.environ["MO_LF_CIWIDE"]),            "per-finding CIs too wide"),
    "rollback_fired":           (float(os.environ["MO_LF_ROLLBACK"]),          "publish reverted"),
    "promoted":                 (float(os.environ["MO_LF_PROMOTED"]),          "candidate promoted"),
    "quarantined":              (float(os.environ["MO_LF_QUARANTINED"]),       "candidate quarantined"),
    "pending_human_approval":   (float(os.environ["MO_LF_PENDING"]),           "awaiting decision"),
}

raw = ""
inp = os.environ["MO_LF_INPUT"]
if inp:
    # If the value looks like a path AND points at an existing file,
    # read the file. Otherwise treat the value as inline JSON.
    if os.path.exists(inp) and os.path.isfile(inp):
        raw = open(inp, encoding="utf-8").read()
    else:
        raw = inp

try:
    data = json.loads(raw)
except Exception as exc:
    sys.stderr.write(f"langfuse_score_for_verdict: parse error: {exc}\n")
    sys.exit(2)


def emit(name):
    val, rationale = table[name]
    print(json.dumps({
        "name": name,
        "value": val,
        "data_type": "NUMERIC",
        "comment": rationale,
    }))


# Match against the structured shapes mini-ork emits today.
# Reviewer:  { decision: APPROVE | REQUEST_CHANGES | ESCALATE }
# Verifier:  { verdict:  pass | fail | indeterminate | vacuous }
# Coalition: { verdict:  panel_diverse | COALITION_ABORT, ... }
# Alpha:     { verdict:  panel_calibrated | ALPHA_ESCALATE, ... }
# Citation:  { verdict:  citations_covered | CITATION_UNDERCOVERED, ... }
# Refute:    { verdict:  validator_grounded | REFUTE_FAILED, ... }
# CI:        { verdict:  ci_within_band | CI_TOO_WIDE, ... }
# Rollback:  { event:    rollback_fired }
# Promotion: { decision: promoted | quarantined | pending_human_approval }

reviewer_map = {
    "APPROVE": "reviewer_approve",
    "REQUEST_CHANGES": "reviewer_request_changes",
    "ESCALATE": "reviewer_escalate",
}
verifier_map = {
    "pass": "verifier_pass",
    "fail": "verifier_fail",
    "vacuous": "verifier_vacuous",
}
oracle_map = {
    "COALITION_ABORT": "coalition_abort",
    "ALPHA_ESCALATE": "alpha_escalate",
    "CITATION_UNDERCOVERED": "citation_undercovered",
    "REFUTE_FAILED": "refute_failed",
    "CI_TOO_WIDE": "ci_too_wide",
}
promotion_map = {
    "promoted": "promoted",
    "quarantined": "quarantined",
    "pending_human_approval": "pending_human_approval",
}

if isinstance(data, dict):
    dec = data.get("decision")
    if dec in reviewer_map:
        emit(reviewer_map[dec])
    if dec in promotion_map:
        emit(promotion_map[dec])
    verd = data.get("verdict")
    if verd in verifier_map:
        emit(verifier_map[verd])
    if verd in oracle_map:
        emit(oracle_map[verd])
    if data.get("event") == "rollback_fired":
        emit("rollback_fired")
PY
}

# Self-test fixtures: reviewer APPROVE, verifier fail, oracle CITATION_UNDERCOVERED,
# rollback event, promotion decision.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  echo "--- score table ---"
  langfuse_score_table
  echo

  echo "--- fixture 1: reviewer APPROVE (expect +1.0) ---"
  echo '{"decision":"APPROVE"}' | langfuse_score_for_verdict
  echo

  echo "--- fixture 2: verifier fail (expect -0.5) ---"
  echo '{"verdict":"fail"}' | langfuse_score_for_verdict
  echo

  echo "--- fixture 3: oracle CITATION_UNDERCOVERED (expect -0.5) ---"
  echo '{"verdict":"CITATION_UNDERCOVERED"}' | langfuse_score_for_verdict
  echo

  echo "--- fixture 4: rollback event (expect -1.0) ---"
  echo '{"event":"rollback_fired"}' | langfuse_score_for_verdict
  echo

  echo "--- fixture 5: promotion decision promoted (expect +1.0) ---"
  echo '{"decision":"promoted"}' | langfuse_score_for_verdict
  echo

  echo "--- fixture 6: combined (reviewer APPROVE + verifier pass, expect 2 lines) ---"
  echo '{"decision":"APPROVE","verdict":"pass"}' | langfuse_score_for_verdict
fi
