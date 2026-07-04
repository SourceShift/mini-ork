"""Krippendorff α calibration gate — Python port of lib/krippendorff_alpha_gate.sh.

Faithful port of ``mo_check_panel_alpha`` from
``lib/krippendorff_alpha_gate.sh``. Mirrors:

  * missing_run_dir early-return (no score_matrix_path key in JSON)
  * score-source file loading (default: <run_dir>/panel-verdict.json)
  * JSON parse-error branch
  * lens_scores shape validation (must be dict; each value must be list)
  * per-entry numeric coercion (TypeError/ValueError → indeterminate)
  * ragged-matrix detection (lens arrays of unequal length)
  * insufficient_panel branch (lens_count < min_lenses OR item_count==0)
  * score-matrix TSV write (item_index \\t <lens>...) for audit
  * Krippendorff α for interval data: α = 1 - D_o/D_e
    D_o = mean pairwise squared diff per item (column-wise)
    D_e = mean pairwise squared diff across the flat matrix
  * constant-marginals fallback → α = 1.0
  * NaN/Inf guard
  * low_alpha threshold comparison (rc=1 ALPHA_ESCALATE) else rc=0
  * verdict-dict shape:
      {verdict, reason, alpha, alpha_threshold, lens_count, item_count,
       score_matrix_path, rationale}

Public API mirrors the bash contract:

    check_panel_alpha(run_dir, threshold=None, min_lenses=None,
                      input_path=None, scores_tsv=None) -> (verdict_dict, rc)

The bash script (lib/krippendorff_alpha_gate.sh) stays in place; this
port gives Python callers an in-process target and gives
``tests/unit/test_krippendorff_alpha_gate_py.py`` a stable surface to
compare against the LIVE bash subprocess.

Why this exists in Python: the bash implementation works by forking a
``python3 - <<PY`` subprocess every call. The Python port reproduces
that math in-process so callers that already have a Python runtime
(pipeline orchestrator, gate-bootstrap, decide()) don't pay fork-exec
cost on every panel decision.
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

__all__ = ["check_panel_alpha"]

DEFAULT_THRESHOLD: float = 0.4
DEFAULT_MIN_LENSES: int = 2


def _read_threshold(explicit: Optional[float]) -> float:
    if explicit is not None:
        return float(explicit)
    env = os.environ.get("MO_ALPHA_THRESHOLD")
    if env is not None and env != "":
        try:
            return float(env)
        except ValueError:
            pass
    return DEFAULT_THRESHOLD


def _read_min_lenses(explicit: Optional[int]) -> int:
    if explicit is not None:
        return int(explicit)
    env = os.environ.get("MO_ALPHA_MIN_LENSES")
    if env is not None and env != "":
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_MIN_LENSES


def _resolve_input_path(run_dir: str, explicit: Optional[str]) -> str:
    if explicit is not None:
        return explicit
    env = os.environ.get("MO_ALPHA_INPUT_PATH")
    if env is not None and env != "":
        return env
    return os.path.join(run_dir, "panel-verdict.json")


def _resolve_scores_tsv(run_dir: str, explicit: Optional[str]) -> str:
    if explicit is not None:
        return explicit
    return os.path.join(run_dir, "panel-alpha-scores.tsv")


def _load_lens_scores(path: str) -> dict:
    """Mirror bash: ``if not os.path.isfile(input_path)`` + ``json.load``.

    Raises ``FileNotFoundError`` on missing input (caller decides verdict).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"score input {path} missing")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _validate_matrix(lens_scores: dict, min_lenses: int) -> tuple[list, list, int, int]:
    """Validate lens_scores shape. Returns ``(lens_names, arrays, lens_count, item_count)``.

    Raises ``ValueError`` with a bash-identical rationale fragment for every
    invalid shape. Caller emits the indeterminate verdict.
    """
    if not isinstance(lens_scores, dict) or not lens_scores:
        raise ValueError("panel-verdict.json missing lens_scores object; cannot measure")

    lens_names = list(lens_scores.keys())
    arrays = []
    for name in lens_names:
        arr = lens_scores[name]
        if not isinstance(arr, list):
            raise ValueError(f"lens {name} score is not a list")
        coerced = []
        for x in arr:
            try:
                coerced.append(float(x))
            except (TypeError, ValueError):
                raise ValueError(f"lens {name} contains non-numeric score: {x!r}")
        arrays.append(coerced)

    lengths = {len(a) for a in arrays}
    if len(lengths) != 1:
        raise ValueError(f"ragged score matrix; lens arrays have lengths {sorted(lengths)}")

    item_count = lengths.pop()
    lens_count = len(lens_names)

    if lens_count < min_lenses or item_count == 0:
        raise ValueError(
            f"need >= {min_lenses} lenses and >= 1 item; got {lens_count}/{item_count}",
            lens_count, item_count,
        )
    return lens_names, arrays, lens_count, item_count


def _write_scores_tsv(path: str, lens_names: list, arrays: list, item_count: int) -> None:
    """Write the audit TSV. Mirror bash ``f.write(... + "\n")`` exactly.

    bash uses ``f"{arrays[j][i]:.6g}"`` for the score cells; column header
    is ``item_index\\t<lens_names joined by \\t>``. Failure is non-fatal in
    bash (audit aid only) — we mirror by swallowing the exception.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("item_index\t" + "\t".join(lens_names) + "\n")
            for i in range(item_count):
                row = [str(i)] + [f"{arrays[j][i]:.6g}" for j in range(len(lens_names))]
                f.write("\t".join(row) + "\n")
    except Exception:
        pass


def _pairwise_disagreement(values: list) -> tuple[float, int]:
    """Sum of pairwise squared differences + pair count.

    Mirror bash ``pairwise_disagreement`` (lines 178-188):
      total += (values[i] - values[j]) ** 2 for i < j
      pairs += 1
    Returns ``(0.0, 0)`` for n < 2.
    """
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


def _compute_alpha(arrays: list, lens_count: int, item_count: int) -> float:
    """Krippendorff α for interval data: α = 1 - D_o/D_e.

    D_o = sum over items of column-wise pairwise squared diff / pair_count
    D_e = same on the marginal distribution (flat matrix).

    Constant marginals (exp_total == 0.0) or degenerate pair counts fall
    through to α = 1.0 per bash convention.
    """
    obs_total = 0.0
    obs_pairs = 0
    for i in range(item_count):
        col = [arrays[j][i] for j in range(lens_count)]
        s, p = _pairwise_disagreement(col)
        obs_total += s
        obs_pairs += p

    flat = [arrays[j][i] for j in range(lens_count) for i in range(item_count)]
    exp_total, exp_pairs = _pairwise_disagreement(flat)

    if obs_pairs == 0 or exp_pairs == 0 or exp_total == 0.0:
        return 1.0
    d_o = obs_total / obs_pairs
    d_e = exp_total / exp_pairs
    return 1.0 - (d_o / d_e)


def check_panel_alpha(run_dir: str,
                      threshold: Optional[float] = None,
                      min_lenses: Optional[int] = None,
                      input_path: Optional[str] = None,
                      scores_tsv: Optional[str] = None) -> tuple[dict, int]:
    """Faithful Python port of ``mo_check_panel_alpha`` from
    ``lib/krippendorff_alpha_gate.sh``.

    Returns ``(verdict_dict, rc)``:
      * rc=0  panel_calibrated OR indeterminate (fail-open by design)
      * rc=1  ALPHA_ESCALATE (alpha < threshold)

    The verdict_dict shape matches the bash stdout byte-for-byte at the
    structural level (floats within 1e-6; rationale strings identical;
    keys identical, EXCEPT the two early-return branches —
    missing_run_dir — which emit 7 keys without ``score_matrix_path``,
    matching the bash printf at lines 64-65).
    """
    threshold = _read_threshold(threshold)
    min_lenses = _read_min_lenses(min_lenses)

    # Mirror bash lines 63-67: missing run_dir → 7-key JSON, NO score_matrix_path.
    if not run_dir or not os.path.isdir(run_dir):
        verdict_dict = {
            "verdict": "indeterminate",
            "reason": "missing_run_dir",
            "alpha": None,
            "alpha_threshold": threshold,
            "lens_count": 0,
            "item_count": 0,
            "rationale": "run_dir not provided or not a directory; cannot measure",
        }
        return verdict_dict, 0

    input_path = _resolve_input_path(run_dir, input_path)
    scores_tsv = _resolve_scores_tsv(run_dir, scores_tsv)

    # Mirror bash heredoc branches in order, with verdict-dict shape that
    # ALWAYS includes score_matrix_path from this point on.
    base = {
        "alpha_threshold": threshold,
        "score_matrix_path": scores_tsv,
    }

    # Branch 1: missing input file.
    if not os.path.isfile(input_path):
        verdict_dict = {
            **base,
            "verdict": "indeterminate",
            "reason": "no_panel_scores",
            "alpha": None,
            "lens_count": 0,
            "item_count": 0,
            "rationale": f"score input {input_path} missing; gate cannot measure",
        }
        return verdict_dict, 0

    # Branch 2: JSON parse error.
    try:
        data = _load_lens_scores(input_path)
    except FileNotFoundError:
        # Race: file deleted between isfile check and open. Mirror bash
        # "missing" rationale verbatim.
        verdict_dict = {
            **base,
            "verdict": "indeterminate",
            "reason": "no_panel_scores",
            "alpha": None,
            "lens_count": 0,
            "item_count": 0,
            "rationale": f"score input {input_path} missing; gate cannot measure",
        }
        return verdict_dict, 0
    except Exception as exc:
        verdict_dict = {
            **base,
            "verdict": "indeterminate",
            "reason": "no_panel_scores",
            "alpha": None,
            "lens_count": 0,
            "item_count": 0,
            "rationale": f"score input {input_path} failed to parse: {exc}",
        }
        return verdict_dict, 0

    lens_scores = data.get("lens_scores") if isinstance(data, dict) else None

    # Branch 3: missing/malformed lens_scores → emit at lens_count=0
    # (matches bash: data.get("lens_scores") on empty dict returns None,
    # so lens_names = list(None) → TypeError → catches into emit below).
    # bash uses `emit(..., 0, 0, ...)` for this branch.
    if not isinstance(lens_scores, dict) or not lens_scores:
        verdict_dict = {
            **base,
            "verdict": "indeterminate",
            "reason": "no_panel_scores",
            "alpha": None,
            "lens_count": 0,
            "item_count": 0,
            "rationale": "panel-verdict.json missing lens_scores object; cannot measure",
        }
        return verdict_dict, 0

    # Branches 4-7: shape validation, ragged matrix, insufficient panel.
    # _validate_matrix raises ValueError with bash-identical rationale text.
    try:
        lens_names, arrays, lens_count, item_count = _validate_matrix(lens_scores, min_lenses)
    except ValueError as exc:
        # bash's ragged / non-list / non-numeric branches all emit with
        # lens_count = len(lens_names) (i.e. the number of keys present,
        # even though validation failed) and item_count = 0. The
        # insufficient_panel branch emits with the actual lens_count and
        # item_count values. We distinguish them by the rationale prefix
        # "need >= " (bash line 154) — but a cleaner signal is the
        # 3-tuple raise form: insufficient_panel passes (lens_count,
        # item_count) in exc.args[1:].
        msg = exc.args[0] if exc.args else str(exc)
        if len(exc.args) >= 3:
            lc, ic = exc.args[1], exc.args[2]
        else:
            # bash uses len(lens_names) here, but since validation failed
            # before reading lengths, we approximate via the dict size.
            lc = len(lens_scores)
            ic = 0
        verdict_dict = {
            **base,
            "verdict": "indeterminate",
            "reason": "insufficient_panel" if msg.startswith("need >= ") else "no_panel_scores",
            "alpha": None,
            "lens_count": lc,
            "item_count": ic,
            "rationale": msg,
        }
        return verdict_dict, 0

    # Audit aid: write score matrix TSV. Failure is non-fatal.
    _write_scores_tsv(scores_tsv, lens_names, arrays, item_count)

    # Alpha computation.
    alpha = _compute_alpha(arrays, lens_count, item_count)

    # Branch: NaN/Inf alpha → indeterminate.
    if math.isnan(alpha) or math.isinf(alpha):
        verdict_dict = {
            **base,
            "verdict": "indeterminate",
            "reason": "no_panel_scores",
            "alpha": None,
            "lens_count": lens_count,
            "item_count": item_count,
            "rationale": "alpha computation produced non-finite value; check score variance",
        }
        return verdict_dict, 0

    # Branch: low alpha → ALPHA_ESCALATE (rc=1).
    if alpha < threshold:
        verdict_dict = {
            **base,
            "verdict": "ALPHA_ESCALATE",
            "reason": "low_alpha",
            "alpha": alpha,
            "lens_count": lens_count,
            "item_count": item_count,
            "rationale": (
                f"alpha {alpha:.3f} < threshold {threshold} across "
                f"{lens_count} lenses x {item_count} items — panel divergence "
                f"too high for safe synthesis; escalate to human"
            ),
        }
        return verdict_dict, 1

    # Default: panel calibrated.
    verdict_dict = {
        **base,
        "verdict": "panel_calibrated",
        "reason": "ok",
        "alpha": alpha,
        "lens_count": lens_count,
        "item_count": item_count,
        "rationale": (
            f"alpha {alpha:.3f} >= threshold {threshold} across "
            f"{lens_count} lenses x {item_count} items"
        ),
    }
    return verdict_dict, 0