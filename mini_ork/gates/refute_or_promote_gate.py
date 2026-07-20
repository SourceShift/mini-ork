"""Python port of ``lib/refute_or_promote_gate.sh``.

Adversarial fabricated-bug injection oracle. Mirrors the bash semantics
byte-stable so the parity test in
``tests/unit/test_refute_or_promote_gate_py.py`` can diff live-bash vs
ported-Python output at dict-equality (json.loads'd) plus 1e-6 float
tolerance.

Public API:
    ``generate_fabrications(count, out_path, prefix=None)`` — writes a
    JSON array of fabrication records; mirrors ``mo_generate_fabrications``.
    Raises ``ValueError`` on bad args (count<1, non-int, or empty out_path).

    ``check_fabrication_survival(findings_path, fabrications_json,
    report_dir=None, ceiling=None)`` — returns ``(verdict_dict, rc)``.
    Mirrors ``mo_check_fabrication_survival``. Same dict shape bash emits
    on stdout; same rc semantics (rc=0 on validator_grounded or
    indeterminate; rc=1 on REFUTE_FAILED).

Env knobs honored (overridable via kwargs):
    ``MO_REFUTE_FP_CEILING``  default 0.1
    ``MO_REFUTE_PREFIX``      default "__fabricated_"
    ``MINI_ORK_RUN_DIR``      report_dir fallback (default ".")

Asymmetry (mirror bash):
    The two shell-level early-return dicts OMIT ``report_path``
    (findings_path or fabrications_json missing; python3 unavailable).
    Every other dict shape — including the inner heredoc's missing_inputs
    branches — INCLUDES ``report_path``.

The bash-only ``mo_grounded_rejection`` side-effect from
``gates_common.sh`` is intentionally NOT replicated here — Python callers
wire rejection through their own substrate (per
``citation_verifier_mechanical.py`` precedent).
"""
from __future__ import annotations

import json
import os
import secrets
from typing import Any


_CLAIM_TEMPLATES = [
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


def _ceiling_default() -> float:
    return float(os.environ.get("MO_REFUTE_FP_CEILING", "0.1"))


def _prefix_default() -> str:
    return os.environ.get("MO_REFUTE_PREFIX", "__fabricated_")


def generate_fabrications(
    count: int, out_path: str, prefix: str | None = None
) -> None:
    """Python equivalent of ``mo_generate_fabrications <count> <out_json_path>``.

    Writes a JSON array of fabrication records:
        [{"id": "<prefix><7hex>",
          "path": "src/<id>/handler.ts",
          "line": 30 + (i * 11) % 200,
          "claim": "<rotated template with id filled in>"}, ...]

    Token suffix uses ``secrets.token_hex(4)[:7]`` (same as bash). Id
    strings differ run-to-run; consumers must compare schema/formula/
    template, not exact id strings.

    Raises ``ValueError`` on invalid args (mirrors bash's rc=2 stderr
    path: count<1, non-int, empty out_path).
    """
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("count must be a positive integer")
    if not out_path:
        raise ValueError("out_path required")
    if prefix is None:
        prefix = _prefix_default()

    records = []
    for i in range(count):
        suffix = secrets.token_hex(4)[:7]
        ident = f"{prefix}{suffix}"
        template = _CLAIM_TEMPLATES[i % len(_CLAIM_TEMPLATES)]
        records.append({
            "id": ident,
            "path": f"src/{ident}/handler.ts",
            "line": 30 + (i * 11) % 200,
            "claim": template.format(id=ident),
        })

    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
        f.write("\n")


def check_fabrication_survival(
    findings_path: str,
    fabrications_json: str,
    report_dir: str | None = None,
    ceiling: float | None = None,
) -> tuple[dict[str, Any], int]:
    """Python equivalent of ``mo_check_fabrication_survival``.

    Returns ``(verdict_dict, rc)`` with byte-stable dict shape and rc
    semantics matching the bash function:

      - verdict ∈ {validator_grounded, REFUTE_FAILED, indeterminate}
      - reason  ∈ {ok, high_fp_survival, missing_inputs, python_unavailable}
      - fp_count / fp_total are ints
      - fp_rate is ``round(rate, 4)`` or ``None``
      - fp_ceiling is the effective ceiling (env default 0.1)
      - report_path is OMITTED only on the two shell-level early returns
        (findings_path or fabrications_json missing/unreadable as file).
        Every other dict shape — including the inner heredoc's
        missing_inputs branches — INCLUDES it.
      - rationale is a one-sentence string

    rc=0 on validator_grounded or indeterminate; rc=1 on REFUTE_FAILED.
    """
    if ceiling is None:
        ceiling = _ceiling_default()
    if report_dir is None:
        report_dir = os.environ.get("MINI_ORK_RUN_DIR", ".")

    report_path = os.path.join(report_dir, "refute-survival.tsv")

    # Shell-level early return: missing inputs — matches bash printf path
    # that OMITS ``report_path`` from the dict. Rc=0.
    if (
        not findings_path
        or not os.path.isfile(findings_path)
        or not fabrications_json
        or not os.path.isfile(fabrications_json)
    ):
        return {
            "verdict": "indeterminate",
            "reason": "missing_inputs",
            "fp_count": 0,
            "fp_total": 0,
            "fp_rate": None,
            "fp_ceiling": ceiling,
            "rationale": (
                "findings_path or fabrications_json missing; cannot measure"
            ),
        }, 0

    # mkdir is best-effort in bash too; the heredoc runs after this point
    try:
        os.makedirs(report_dir, exist_ok=True)
    except Exception:
        pass

    # Read findings — bash's heredoc does this inside try/except. If the
    # file vanishes between the [ -f ] check and the open(), we emit a
    # missing_inputs shape WITH report_path (different from the
    # shell-level early return above).
    try:
        findings_text = open(findings_path, encoding="utf-8").read()
    except Exception as exc:
        return {
            "verdict": "indeterminate",
            "reason": "missing_inputs",
            "fp_count": 0,
            "fp_total": 0,
            "fp_rate": None,
            "fp_ceiling": ceiling,
            "report_path": report_path,
            "rationale": f"findings_path unreadable: {exc}",
        }, 0

    try:
        fabrications = json.load(open(fabrications_json, encoding="utf-8"))
    except Exception as exc:
        return {
            "verdict": "indeterminate",
            "reason": "missing_inputs",
            "fp_count": 0,
            "fp_total": 0,
            "fp_rate": None,
            "fp_ceiling": ceiling,
            "report_path": report_path,
            "rationale": f"fabrications_json unreadable: {exc}",
        }, 0

    if not isinstance(fabrications, list) or not fabrications:
        return {
            "verdict": "indeterminate",
            "reason": "missing_inputs",
            "fp_count": 0,
            "fp_total": 0,
            "fp_rate": None,
            "fp_ceiling": ceiling,
            "report_path": report_path,
            "rationale": "fabrications_json must be a non-empty array",
        }, 0

    survivors: list[dict[str, Any]] = []
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

    fp_total = sum(
        1 for f in fabrications if isinstance(f, dict) and f.get("id")
    )
    fp_count = len(survivors)
    fp_rate = (fp_count / fp_total) if fp_total else 0.0

    # TSV report — best-effort, exactly mirrors bash: header + one row
    # per fabrication (in original order), survived=yes/no.
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("id\tpath\tline\tsurvived\n")
            survivor_ids = {s["id"] for s in survivors}
            for fab in fabrications:
                if not isinstance(fab, dict):
                    continue
                ident = fab.get("id") or ""
                survived = "yes" if ident in survivor_ids else "no"
                f.write(
                    f"{ident}\t{fab.get('path', '')}\t"
                    f"{fab.get('line', 0)}\t{survived}\n"
                )
    except Exception:
        pass  # audit aid only — bash's exact posture

    if fp_rate > ceiling:
        return {
            "verdict": "REFUTE_FAILED",
            "reason": "high_fp_survival",
            "fp_count": fp_count,
            "fp_total": fp_total,
            "fp_rate": round(fp_rate, 4),
            "fp_ceiling": ceiling,
            "report_path": report_path,
            "rationale": (
                f"validator promoted {fp_count} of {fp_total} fabricated "
                f"findings ({fp_rate:.1%} > ceiling {ceiling:.0%}); "
                f"validator is not safely refuting plants"
            ),
        }, 1

    return {
        "verdict": "validator_grounded",
        "reason": "ok",
        "fp_count": fp_count,
        "fp_total": fp_total,
        "fp_rate": round(fp_rate, 4),
        "fp_ceiling": ceiling,
        "report_path": report_path,
        "rationale": (
            f"validator refuted {fp_total - fp_count} of {fp_total} "
            f"fabrications ({fp_rate:.1%} <= ceiling {ceiling:.0%})"
        ),
    }, 0