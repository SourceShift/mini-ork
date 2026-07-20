"""honest_ci_gate — per-finding confidence intervals from lens votes (Python port).

Faithful strangler-fig port of ``lib/honest_ci_gate.sh``'s
``mo_compute_finding_cis`` and ``mo_check_ci_widths``. Closes the
positioning-doc calibration-list item "Honest confidence intervals on
every claim" (Dai 2025 *Semantic Triangulation*, arxiv:2511.12288).

For each finding it computes:

    n           number of numeric lens votes
    mean        statistics.fmean of the votes
    sd          sample stddev (n-1 divisor), statistics.stdev
    sem         sd / sqrt(n)
    ci_low/high mean +/- t_{conf, n-1} * sem
    ci_width    ci_high - ci_low (the "honesty band")

using a small t_critical() table (df<1 -> inf, df<=200 interpolation at
conf<=0.95 else 1.96; conf > 0.95 -> 2.576).

Public API mirrors the bash contract:

    compute_finding_cis(findings_json, out_path, confidence=None) -> int
        rc=0 on success, rc=2 on missing arg or unreadable input.

    check_ci_widths(findings_json, report_dir=None, width_ceiling=None,
                    wide_ratio_ceiling=None) -> tuple[dict, int]
        Verdict dict shape matches bash's ``print(json.dumps(...))``
        field-for-field. rc=0 on ``ci_within_band`` or ``indeterminate``;
        rc=1 on ``CI_TOO_WIDE``.

Env knobs (read only when the corresponding function argument is None):

    MO_CI_CONFIDENCE          default 0.95
    MO_CI_WIDTH_CEILING       default 2.0
    MO_CI_WIDE_RATIO_CEILING  default 0.3

The bash script (``lib/honest_ci_gate.sh``) stays in place; this port
gives Python callers an in-process target and gives
``tests/unit/test_honest_ci_gate_py.py`` a stable surface to compare
against the LIVE bash subprocess.

Bash's optional ``mo_grounded_rejection`` integration is NOT replicated
(out of scope for the port); the verdict JSON shape is preserved so the
parity test still diff-equals the live bash output.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
from typing import Optional

__all__ = ["compute_finding_cis", "check_ci_widths", "t_critical"]

DEFAULT_CONFIDENCE: float = 0.95
DEFAULT_WIDTH_CEILING: float = 2.0
DEFAULT_WIDE_RATIO_CEILING: float = 0.3


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_confidence(arg: Optional[float]) -> float:
    if arg is not None:
        return float(arg)
    return _read_float_env("MO_CI_CONFIDENCE", DEFAULT_CONFIDENCE)


def _read_width_ceiling(arg: Optional[float]) -> float:
    if arg is not None:
        return float(arg)
    return _read_float_env("MO_CI_WIDTH_CEILING", DEFAULT_WIDTH_CEILING)


def _read_wide_ratio_ceiling(arg: Optional[float]) -> float:
    if arg is not None:
        return float(arg)
    return _read_float_env("MO_CI_WIDE_RATIO_CEILING", DEFAULT_WIDE_RATIO_CEILING)


def t_critical(df: float, conf: float) -> float:
    """Inverse-t approximation. Mirrors bash lines 98-122 verbatim."""
    if df < 1:
        return float("inf")
    table_95 = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        15: 2.131, 20: 2.086, 30: 2.042, 50: 2.009, 100: 1.984,
        200: 1.972,
    }
    if conf <= 0.95:
        keys = sorted(table_95)
        if df >= keys[-1]:
            return 1.96
        for i in range(len(keys) - 1):
            lo, hi = keys[i], keys[i + 1]
            if lo <= df <= hi:
                f_ = (df - lo) / (hi - lo)
                return table_95[lo] + f_ * (table_95[hi] - table_95[lo])
    return 2.576


def _emit_ci(n: int, mean: Optional[float], sd: Optional[float],
             sem: Optional[float], ci_low: Optional[float],
             ci_high: Optional[float], ci_width: Optional[float],
             conf: float, extra: dict) -> dict:
    ci = {
        "n": n,
        "mean": mean,
        "sd": sd,
        "sem": sem,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "ci_width": ci_width,
        "confidence": conf,
    }
    ci.update(extra)
    return ci


def compute_finding_cis(findings_json: str,
                        out_path: str,
                        confidence: Optional[float] = None) -> int:
    """Faithful port of bash ``mo_compute_finding_cis``.

    Returns rc=0 on success, rc=2 on missing arg or unreadable input.
    """
    if not findings_json or not os.path.isfile(findings_json) or not out_path:
        sys.stderr.write(
            "mo_compute_finding_cis: <findings_json> and <out_path> required "
            "(and findings_json must exist)\n"
        )
        return 2

    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    except Exception:
        pass

    conf = _read_confidence(confidence)

    try:
        with open(findings_json, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        sys.stderr.write(
            f"mo_compute_finding_cis: parse error on {findings_json}: {exc}\n"
        )
        return 2

    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        sys.stderr.write("mo_compute_finding_cis: input must have findings[] array\n")
        return 2

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
            f["confidence_interval"] = _emit_ci(
                0, None, None, None, None, None, None, conf,
                {"note": "no_numeric_votes"},
            )
            continue
        if n == 1:
            m = nums[0]
            f["confidence_interval"] = _emit_ci(
                1, m, None, None, m, m, 0.0, conf,
                {"note": "single_vote_zero_width_is_misleading"},
            )
            continue
        m = statistics.fmean(nums)
        sd = statistics.stdev(nums)
        sem = sd / math.sqrt(n)
        tc = t_critical(n - 1, conf)
        half = tc * sem
        f["confidence_interval"] = _emit_ci(
            n, round(m, 4), round(sd, 4), round(sem, 4),
            round(m - half, 4), round(m + half, 4), round(2 * half, 4),
            conf, {"t_critical": round(tc, 4)},
        )

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return 0


def _missing_input_verdict(wide_ratio_ceiling: float,
                           ceiling: float,
                           rationale: str) -> dict:
    """Bash lines 193-197 (and the parallel 208-210) early-return shapes.

    8 keys: NO ``augmented_path``. Mirrors bash's top-level missing_input
    printf exactly so the parity test compares field-for-field.
    """
    return {
        "verdict": "indeterminate",
        "reason": "missing_input",
        "wide_count": 0,
        "total": 0,
        "wide_ratio": None,
        "wide_ratio_ceiling": wide_ratio_ceiling,
        "ci_width_ceiling": ceiling,
        "rationale": rationale,
    }


def check_ci_widths(findings_json: str,
                    report_dir: Optional[str] = None,
                    width_ceiling: Optional[float] = None,
                    wide_ratio_ceiling: Optional[float] = None
                    ) -> tuple[dict, int]:
    """Faithful port of bash ``mo_check_ci_widths``.

    Returns ``(verdict_dict, rc)``:

      * rc=0  ``ci_within_band`` or any ``indeterminate`` branch
      * rc=1  ``CI_TOO_WIDE`` (rationale cites ``wide_cis``)
    """
    width_ceiling = _read_width_ceiling(width_ceiling)
    wide_ratio_ceiling = _read_wide_ratio_ceiling(wide_ratio_ceiling)

    if report_dir is None:
        report_dir = os.environ.get("MINI_ORK_RUN_DIR") or "."

    # Mirror bash lines 193-197: missing-input branch returns BEFORE any
    # mkdir (no augmented_path either — 8-key shape).
    if not findings_json or not os.path.isfile(findings_json):
        return _missing_input_verdict(
            wide_ratio_ceiling, width_ceiling,
            "findings_json missing; cannot measure",
        ), 0

    # Now safe to create the report dir (matches bash line 205).
    try:
        os.makedirs(report_dir, exist_ok=True)
    except Exception:
        pass

    augmented = os.path.join(report_dir, "findings-with-cis.json")
    if compute_finding_cis(findings_json, augmented) != 0:
        return _missing_input_verdict(
            wide_ratio_ceiling, width_ceiling,
            "failed to compute CIs (see stderr)",
        ), 0

    try:
        with open(augmented, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        return {
            "verdict": "indeterminate",
            "reason": "missing_input",
            "wide_count": 0,
            "total": 0,
            "wide_ratio": None,
            "wide_ratio_ceiling": wide_ratio_ceiling,
            "ci_width_ceiling": width_ceiling,
            "augmented_path": augmented,
            "rationale": f"augmented findings unreadable: {exc}",
        }, 0

    findings = data.get("findings") if isinstance(data, dict) else None

    if not isinstance(findings, list) or not findings:
        return {
            "verdict": "indeterminate",
            "reason": "no_findings",
            "wide_count": 0,
            "total": 0,
            "wide_ratio": None,
            "wide_ratio_ceiling": wide_ratio_ceiling,
            "ci_width_ceiling": width_ceiling,
            "augmented_path": augmented,
            "rationale": "no findings to score CIs on",
        }, 0

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
        if width >= width_ceiling:
            wide += 1

    if total == 0:
        return {
            "verdict": "indeterminate",
            "reason": "no_findings",
            "wide_count": 0,
            "total": 0,
            "wide_ratio": None,
            "wide_ratio_ceiling": wide_ratio_ceiling,
            "ci_width_ceiling": width_ceiling,
            "augmented_path": augmented,
            "rationale": "no scorable findings (need >= 1 with numeric votes)",
        }, 0

    ratio = wide / total

    if ratio > wide_ratio_ceiling:
        return {
            "verdict": "CI_TOO_WIDE",
            "reason": "wide_cis",
            "wide_count": wide,
            "total": total,
            "wide_ratio": round(ratio, 4),
            "wide_ratio_ceiling": wide_ratio_ceiling,
            "ci_width_ceiling": width_ceiling,
            "augmented_path": augmented,
            "rationale": (
                f"{wide}/{total} findings ({ratio:.1%}) have CI width >= "
                f"{width_ceiling}; that exceeds the {wide_ratio_ceiling:.0%} "
                f"ceiling, audit is honest-uncertain on too many claims"
            ),
        }, 1

    return {
        "verdict": "ci_within_band",
        "reason": "ok",
        "wide_count": wide,
        "total": total,
        "wide_ratio": round(ratio, 4),
        "wide_ratio_ceiling": wide_ratio_ceiling,
        "ci_width_ceiling": width_ceiling,
        "augmented_path": augmented,
        "rationale": (
            f"{wide}/{total} findings have wide CIs ({ratio:.1%} <= "
            f"ceiling {wide_ratio_ceiling:.0%})"
        ),
    }, 0
