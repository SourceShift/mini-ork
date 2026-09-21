"""Hermetic gate fuzzer — the measurement half of a hacker-fixer loop.

Every RSI cycle so far hardened the *artifact*; none attacked the *gate*. This
module runs a corpus of probes against a gate evaluator and reports the
blind-spot rate (probes that should have been rejected but were accepted) and
the over-block rate (legitimate probes the gate rejected). It is deterministic
and model-free: no lane, no network, no DB.

The adapter (``artifact_contract_evaluator``) calls the real shipped
``artifact_contract.validate_artifact`` so the fuzzer measures the gate as it
ships, never a copy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Sequence

__all__ = [
    "fuzz_gate",
    "load_corpus",
    "artifact_contract_evaluator",
    "summarize",
    "DEFAULT_CORPUS",
]

#: Resolved shipped-corpus path. The CLI and the self-application measurement
#: both load through this so they can never disagree on which corpus was fuzzed.
DEFAULT_CORPUS = str(
    Path(__file__).resolve().parent / "probes" / "artifact_contract_probes.json"
)

_VERDICTS = ("pass", "fail", "defer")


def fuzz_gate(evaluate: Callable[[dict], str], cases: Sequence[dict]) -> dict:
    """Run ``evaluate`` over ``cases`` and score the gate's blind spots.

    ``evaluate(case)`` must return ``"pass"``, ``"fail"``, or ``"defer"``; any
    other value raises ``ValueError`` naming the offending case id. A case's
    ``"expect"`` is either ``"pass"`` or ``"fail"`` (anything else raises).

    A ``"defer"`` is counted only in ``defers`` — it is neither ``ok`` nor a
    blind spot nor an over block, so a gate that answers nothing scores no
    credit. Rates are ``None`` (never ``0.0``) when their denominator is zero.
    """
    n_expect_pass = 0
    n_expect_fail = 0
    blind_spots = 0
    over_blocks = 0
    defers = 0
    results: list[dict] = []

    for case in cases:
        cid = case["id"]
        expect = case["expect"]
        if expect not in ("pass", "fail"):
            raise ValueError(
                f"case {cid!r}: unknown expectation {expect!r} "
                "(expected 'pass' or 'fail')"
            )
        got = evaluate(case)
        if got not in _VERDICTS:
            raise ValueError(
                f"evaluator returned {got!r} for case {cid!r}; "
                "expected one of pass/fail/defer"
            )

        if expect == "fail":
            n_expect_fail += 1
            if got == "pass":
                blind_spots += 1
                ok = False
                detail = "blind spot: expected fail, got pass"
            elif got == "fail":
                ok = True
                detail = "correctly rejected"
            else:
                defers += 1
                ok = False
                detail = "defer: unmeasured"
        else:
            n_expect_pass += 1
            if got == "pass":
                ok = True
                detail = "correctly accepted"
            elif got == "fail":
                over_blocks += 1
                ok = False
                detail = "over block: expected pass, got fail"
            else:
                defers += 1
                ok = False
                detail = "defer: unmeasured"

        results.append(
            {"id": cid, "expect": expect, "got": got, "ok": ok, "detail": detail}
        )

    return {
        "n": len(cases),
        "n_expect_pass": n_expect_pass,
        "n_expect_fail": n_expect_fail,
        "blind_spots": blind_spots,
        "over_blocks": over_blocks,
        "defers": defers,
        "blind_spot_rate": (blind_spots / n_expect_fail) if n_expect_fail else None,
        "over_block_rate": (over_blocks / n_expect_pass) if n_expect_pass else None,
        "results": results,
    }


def load_corpus(path: str) -> list[dict]:
    """Load a JSON array of probe cases, validating shape and uniqueness.

    Raises ``ValueError`` (naming ``path``) for a missing file, non-array JSON,
    a non-object entry, a missing/empty/non-string ``"id"``, a duplicate id, or
    an ``"expect"`` other than ``"pass"``/``"fail"``. Validation lives here so
    ``fuzz_gate`` can assume well-formed cases.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"corpus file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"corpus file is not valid JSON: {path}: {exc}")

    if not isinstance(data, list):
        raise ValueError(f"corpus file must contain a JSON array: {path}")

    cases: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"corpus entry {i} is not an object: {path}")
        cid = entry.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"corpus entry {i} has a missing/empty/non-string id: {path}")
        if cid in seen:
            raise ValueError(f"corpus has duplicate id {cid!r}: {path}")
        seen.add(cid)
        expect = entry.get("expect")
        if expect not in ("pass", "fail"):
            raise ValueError(
                f"corpus entry {i} (id {cid!r}) has invalid expect {expect!r}: {path}"
            )
        cases.append(entry)
    return cases


def artifact_contract_evaluator(workdir: str) -> Callable[[dict], str]:
    """Return an evaluator that runs the shipped ``artifact_contract`` gate.

    Per case: writes ``case["artifact"]["content"]`` to
    ``workdir/case["artifact"]["name"]`` (``None`` content means no file — the
    not-found probe), then calls the real ``validate_artifact`` and returns
    ``payload.get("verdict", "defer")``.
    """
    from mini_ork.gates import artifact_contract

    def evaluate(case: dict) -> str:
        artifact = case["artifact"]
        name = artifact["name"]
        path = os.path.join(workdir, name)
        content = artifact["content"]
        if content is None:
            # A previous case may have written this same relative name; the
            # not-found probe must see an absent path, not a leaked file.
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        payload = artifact_contract.validate_artifact(case["contract"], path)
        return payload.get("verdict", "defer")

    return evaluate


def summarize(report: dict) -> str:
    """One human line; ``None`` rates render as ``-`` (e.g. ``0/0 (-)``)."""

    def fmt(rate) -> str:
        return "-" if rate is None else f"{rate:.3f}"

    return (
        f"gate-fuzz: n={report['n']} "
        f"blind_spots={report['blind_spots']}/{report['n_expect_fail']} "
        f"({fmt(report['blind_spot_rate'])}) "
        f"over_blocks={report['over_blocks']}/{report['n_expect_pass']} "
        f"({fmt(report['over_block_rate'])}) "
        f"defers={report['defers']}"
    )
