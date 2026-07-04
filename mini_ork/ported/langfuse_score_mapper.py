"""langfuse_score_mapper — Python port of lib/langfuse_score_mapper.sh.

Faithful port of the bash verdict/rollback/promotion → numeric Langfuse
trace-score mapper in ``lib/langfuse_score_mapper.sh``. The bash script
is the authoritative source — this module gives Python callers an
in-process target and gives ``tests/unit/test_langfuse_score_mapper_py.py``
a stable surface to byte-diff against the LIVE bash subprocess (no
mocks, no hardcoded outputs beyond what bash itself emits).

Co-existence model (strangler-fig): bash ``lib/langfuse_score_mapper.sh``
stays byte-identical. The Python port mirrors its CLI semantics exactly.
Parity is enforced by ``tests/unit/test_langfuse_score_mapper_py.py``
(>=6 live-subprocess cases that drive ``bash lib/langfuse_score_mapper.sh
<func> <args>`` on identical inputs and diff the resulting stdout lines
+ exit codes / raised exceptions).

Pipeline map (bash → Python):
  bash _LANGFUSE_SCORES_REVIEWER_APPROVE="1.0"
      ... 14 lines (52-66)            →  SCORE_TABLE (dict[event_name] = (float, str))
  bash reviewer_map / verifier_map / oracle_map / promotion_map
      lines 191-212                   →  REVIEWER_MAP / VERIFIER_MAP / ORACLE_MAP /
                                         PROMOTION_MAP (module constants)
  bash langfuse_score_table           →  langfuse_score_table() -> str
      cat <<EOF ... EOF (lines 70-87)     (returns the heredoc text verbatim)

  bash langfuse_score_for_verdict
      [ -z "$1" ] && [ -t 0 ]         →  input_json None + stdin_text None
                                            → ValueError("usage: ...")
      python3 - <<'PY' (lines 132-227) →  langfuse_score_for_verdict(input_json,
                                                                     *,
                                                                     stdin_text=None)
                                            → list[dict]  (one emit per matched rule)
      table lookup + print(json.dumps)   The Python port returns a list so callers
                                         control JSON serialization; the bash
                                         function prints one JSON object per
                                         stdout line and exits with the parse
                                         status (rc=2 on parse error, rc=0
                                         otherwise including empty-match).

JSON output contract: each emitted dict has shape
  {"name": <event>, "value": <float>, "data_type": "NUMERIC",
   "comment": <one-line rationale>}
The ``comment`` strings match bash lines 136-150 verbatim (e.g.
"explicit approval", NOT the longer "explicit approval, full credit"
text shown in the operator-facing table). Float values mirror bash
lines 52-66 verbatim — no f-string truncation, no rounding. The order
of keys in the dict is the same insertion order used by the bash
Python heredoc so ``json.dumps`` is byte-equal.

Public surface (mirrors bash function names exactly):
    langfuse_score_table() -> str
    langfuse_score_for_verdict(input_json, *, stdin_text=None) -> list[dict]
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

__all__ = [
    "langfuse_score_table",
    "langfuse_score_for_verdict",
    "SCORE_TABLE",
    "REVIEWER_MAP",
    "VERIFIER_MAP",
    "ORACLE_MAP",
    "PROMOTION_MAP",
]

# Single source of truth for the numeric values. Mirrors bash
# _LANGFUSE_SCORES_* lines 52-66 exactly. The keys in this table
# are the event names emitted to Langfuse (matches the keys in
# REVIEWER_MAP / VERIFIER_MAP / ORACLE_MAP / PROMOTION_MAP).
SCORE_TABLE: Dict[str, Tuple[float, str]] = {
    "reviewer_approve":         (1.0,   "explicit approval"),
    "reviewer_request_changes": (-0.5,  "review found defects"),
    "reviewer_escalate":        (0.0,   "operator decides"),
    "verifier_pass":            (0.5,   "deterministic pass"),
    "verifier_fail":            (-0.5,  "deterministic fail"),
    "verifier_vacuous":         (-0.25, "nothing checked"),
    "coalition_abort":          (-0.75, "rho + family failure"),
    "alpha_escalate":           (-0.5,  "alpha below threshold"),
    "citation_undercovered":    (-0.5,  "citations missing"),
    "refute_failed":            (-0.75, "fabrications survived"),
    "ci_too_wide":              (-0.25, "per-finding CIs too wide"),
    "rollback_fired":           (-1.0,  "publish reverted"),
    "promoted":                 (1.0,   "candidate promoted"),
    "quarantined":              (-1.0,  "candidate quarantined"),
    "pending_human_approval":   (0.0,   "awaiting decision"),
}

# Map operator-facing decision/verdict/event strings to event names.
# Mirrors bash lines 191-212 exactly.
REVIEWER_MAP: Dict[str, str] = {
    "APPROVE":         "reviewer_approve",
    "REQUEST_CHANGES": "reviewer_request_changes",
    "ESCALATE":        "reviewer_escalate",
}
VERIFIER_MAP: Dict[str, str] = {
    "pass":     "verifier_pass",
    "fail":     "verifier_fail",
    "vacuous":  "verifier_vacuous",
}
ORACLE_MAP: Dict[str, str] = {
    "COALITION_ABORT":       "coalition_abort",
    "ALPHA_ESCALATE":        "alpha_escalate",
    "CITATION_UNDERCOVERED": "citation_undercovered",
    "REFUTE_FAILED":         "refute_failed",
    "CI_TOO_WIDE":           "ci_too_wide",
}
PROMOTION_MAP: Dict[str, str] = {
    "promoted":               "promoted",
    "quarantined":            "quarantined",
    "pending_human_approval": "pending_human_approval",
}

# Verbatim copy of bash ``langfuse_score_table`` heredoc body (lines
# 70-87). Returned as-is so parity tests can byte-diff against the
# LIVE bash output. No f-string interpolation — these are stable
# literals.
_SCORE_TABLE_TEXT = (
    "event                          score   rationale\n"
    "─────                          ─────   ─────────\n"
    "reviewer APPROVE               +1.0    explicit approval, full credit\n"
    "reviewer REQUEST_CHANGES       -0.5    review found defects; not a pass\n"
    "reviewer ESCALATE               0.0    operator decides; do not pre-bias\n"
    "verifier pass                  +0.5    deterministic pass, half-credit\n"
    "                                       (full credit reserved for reviewer)\n"
    "verifier fail                  -0.5    deterministic fail\n"
    "verifier vacuous               -0.25   nothing checked != pass\n"
    "panel COALITION_ABORT          -0.75   structural panel failure (ρ + family)\n"
    "panel ALPHA_ESCALATE           -0.5    α<0.4, panel divergence too high\n"
    "panel CITATION_UNDERCOVERED    -0.5    citations failed to resolve\n"
    "panel REFUTE_FAILED            -0.75   validator hallucinated fabrications\n"
    "panel CI_TOO_WIDE              -0.25   per-finding CIs honest-uncertain\n"
    "rollback fired                 -1.0    publish was reverted post-hoc\n"
    "promoted                       +1.0    candidate passed promotion gate\n"
    "quarantined                    -1.0    candidate failed promotion gate\n"
    "pending_human_approval          0.0    awaiting decision\n"
)


def langfuse_score_table() -> str:
    """Return the operator-facing score table (mirror bash ``langfuse_score_table``).

    Returns the same heredoc text bash ``cat <<EOF`` would print to
    stdout. No f-string interpolation — the table is a stable literal.
    """
    return _SCORE_TABLE_TEXT


def _load_input(input_json: Optional[str], stdin_text: Optional[str]) -> str:
    """Resolve the JSON payload text from input_json OR stdin_text.

    Mirrors bash lines 109-112 + 154-161: if no arg was given (empty
    ``_input``) bash reads stdin with ``cat``; if a value IS given AND
    it points at an existing file, bash reads that file; otherwise it
    treats the value as inline JSON.

    Returns the raw JSON text (still a string — caller parses).
    """
    if input_json is None and stdin_text is None:
        raise ValueError(
            "langfuse_score_for_verdict: usage: langfuse_score_for_verdict "
            "<verdict_json_path> OR via stdin_text=<inline_json>"
        )
    source = input_json if input_json is not None else stdin_text
    assert source is not None  # for type-checkers
    if os.path.exists(source) and os.path.isfile(source):
        with open(source, "r", encoding="utf-8") as fh:
            return fh.read()
    return source


def _emit(name: str) -> Dict[str, object]:
    """Mirror bash lines 170-177 ``emit(name)`` helper."""
    val, rationale = SCORE_TABLE[name]
    return {
        "name": name,
        "value": val,
        "data_type": "NUMERIC",
        "comment": rationale,
    }


def langfuse_score_for_verdict(
    input_json: Optional[str] = None,
    *,
    stdin_text: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Map a verdict/rollback/promotion dict into numeric Langfuse scores.

    Mirror bash ``langfuse_score_for_verdict``:

      * If neither ``input_json`` nor ``stdin_text`` is given, raise
        ``ValueError`` with the same usage-error message bash prints
        on stderr (bash: rc=2 on usage error).
      * If ``input_json`` is a path to an existing file, read it
        (bash line 158). Otherwise treat it as inline JSON. Same
        logic for ``stdin_text`` if used directly.
      * Parse the payload as JSON. On parse error raise
        ``ValueError`` with prefix
        ``"langfuse_score_for_verdict: parse error: ..."`` — matches
        bash's stderr prefix (bash: rc=2 on parse error).
      * Emit one score per matched rule, in the SAME order bash does:
        reviewer_map(decision), promotion_map(decision),
        verifier_map(verdict), oracle_map(verdict),
        then rollback event if ``event == "rollback_fired"``.

    Returns a list of score dicts (possibly empty if no rule matches).
    Empty list for non-dict input matches bash (bash emits 0 lines,
    rc=0 for non-dict input).
    """
    raw = _load_input(input_json, stdin_text)
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"langfuse_score_for_verdict: parse error: {exc}") from exc

    if not isinstance(data, dict):
        return []

    scores: List[Dict[str, object]] = []
    dec = data.get("decision")
    if dec in REVIEWER_MAP:
        scores.append(_emit(REVIEWER_MAP[dec]))
    if dec in PROMOTION_MAP:
        scores.append(_emit(PROMOTION_MAP[dec]))
    verd = data.get("verdict")
    if verd in VERIFIER_MAP:
        scores.append(_emit(VERIFIER_MAP[verd]))
    if verd in ORACLE_MAP:
        scores.append(_emit(ORACLE_MAP[verd]))
    if data.get("event") == "rollback_fired":
        scores.append(_emit("rollback_fired"))
    return scores