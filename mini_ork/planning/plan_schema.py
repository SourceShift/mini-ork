"""Plan-JSON extraction and validation (pure; no I/O besides stderr diagnostics).

Extracted verbatim from ``mini_ork.cli.plan`` (strangler-fig parity port).
Contains the D-011/016/052 extraction chain, the D-008b node-type check, and
placeholder/parse rejection verdicts. ``mini_ork.cli.plan`` re-exports every
name defined here.
"""
from __future__ import annotations

import json
import re
import sys

_NODE_TYPES = {"planner", "researcher", "implementer", "reviewer", "verifier",
               "reflector", "publisher", "rollback"}


# ── plan-JSON extraction (D-011/016/052) ──

def _objects(s):
    i = 0
    while True:
        start = s.find("{", i)
        if start < 0:
            return
        depth = 0
        in_str = False
        esc = False
        for j in range(start, len(s)):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield s[start:j + 1]
                    i = j + 1
                    break
        else:
            return


# A genuine unfilled stub carries a stub word (the dry-run placeholder reads
# "<dry-run: not generated>"). A bare angle-bracketed identifier does NOT.
_PLACEHOLDER_HINT = re.compile(
    r"\b(dry[- ]?run|not[- ]generated|todo|tbd|fixme|fill[- ]?(me|in)?|xxx|placeholder)\b",
    re.I,
)


def _is_stub_string(v) -> bool:
    """True only for an UNFILLED template value — not for code that uses angle brackets."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not (s.startswith("<") and s.endswith(">")):
        return False
    inner = s[1:-1].strip()
    if not inner:
        return True
    # "<dry-run: not generated>", "<TODO>", "<fill me>" -> a real stub
    # "<ContentNodeCreationModal>", "<div>", "<Foo />"   -> real code
    return bool(_PLACEHOLDER_HINT.search(inner))


def _contains_placeholder(v):
    """True when the PLAN ITSELF was never generated (i.e. it is the dry-run stub).

    Scope matters. The old check recursed into EVERY nested string and flagged anything
    shaped like `<...>`. That rejected two entirely legitimate things:

      * JSX/HTML tags a plan naturally names — "<ContentNodeCreationModal>" — which made
        mini-ork unable to plan work on ANY React/JSX codebase; and
      * the planner's own step annotations — "<shell-only>", "<analysis-only — no edit>"
        — meaning "this step edits no file".

    A *placeholder plan* means the planner produced nothing: the dry-run stub, whose
    objective is "<dry-run: not generated>" and whose decomposition is empty. Judge the
    plan by its objective (and emptiness), not by every string inside it.
    """
    if isinstance(v, dict):
        if _is_stub_string(v.get("objective")):
            return True
        # the dry-run stub is an empty shell: no objective, nothing to do
        if not str(v.get("objective") or "").strip() and not (v.get("decomposition") or []):
            return True
        return False
    return _is_stub_string(v)


def _is_plan(obj):
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get("verifier_contract"), dict):
        return False
    if not obj.get("verifier_contract", {}).get("checks"):
        return False
    if _contains_placeholder(obj):
        return False
    return any(k in obj for k in ("objective", "decomposition", "artifact_contract"))


def extract_plan_json(raw: str) -> str:
    first = None
    for chunk in _objects(raw):
        if first is None:
            first = chunk
        try:
            parsed = json.loads(chunk)
        except Exception:
            continue
        if _is_plan(parsed):
            return json.dumps(parsed, indent=2)
    return first if first is not None else raw


def validate_plan(plan_json: str) -> str:
    """Return the HAS_VERIFIER verdict string (verbatim logic)."""
    try:
        p = json.loads(plan_json)
        vc = p.get("verifier_contract", {})
        if not vc.get("checks", []):
            return "missing_verifier_contract"
        if _contains_placeholder(p):
            return "placeholder_plan"
        ac = p.get("artifact_contract", {})
        if not isinstance(ac, dict):
            return "bad_artifact_contract"
        bad = []
        for i, step in enumerate(p.get("decomposition", []) or []):
            nt = (step.get("node_type") or "").strip()
            if not nt:
                bad.append(f'step[{i}] {step.get("id", "?")}: empty node_type')
            elif nt not in _NODE_TYPES:
                bad.append(f'step[{i}] {step.get("id", "?")}: node_type={nt!r} not in {sorted(_NODE_TYPES)}')
        if bad:
            sys.stderr.write("bad_node_types:" + "|".join(bad) + "\n")
            return "bad_node_types"
        return "ok"
    except Exception as e:
        sys.stderr.write(f"parse_error:{e}\n")
        return "parse_error"


def _detect_truncation(raw: str) -> bool:
    depth = 0
    in_str = False
    esc = False
    for c in (raw or "").rstrip()[-200:]:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
    return depth > 0
