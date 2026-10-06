"""Metamorphic relations — the oracle term that proves a patch special-cases the reported
input by breaking a relation BETWEEN two executions.

WHAT IT IS FOR
───────────────
A reproduction test is a single point in input space. A patch that special-cases that
point passes it and nothing else (arXiv 2604.15149). Metamorphic amplification caught
that shape by holding a PROPERTY across perturbed inputs; relations attack the same hole
one level up: the LLM proposes a relation R between the output on a SOURCE input and the
output on a FOLLOWUP = T(SOURCE); a deterministic runner executes BOTH and evaluates R.
A patch that hard-codes the reported input fixes SOURCE but not FOLLOWUP → R breaks →
REFUTED, with the pair recorded as evidence.

THE LAW
───────
    **A relation is evidence only if it holds on base and breaks on a special-cased
      patch. Base only ATTRIBUTES, never admits.** A relation that already fails on base
    proves nothing about the patch (it is `violated_unattributed`); a relation that holds
    on base is exactly the one a special-cased patch breaks, so it is the only kind that
    may veto.

The LLM proposes, execution decides, no LLM approves. PROVEN | REFUTED | UNVERIFIED, and a
relation that cannot execute ABSTAINS and never counts as holding.

WHY THE KILL SWITCH IS ON BY DEFAULT
────────────────────────────────────
Every knob (`MO_ASSAY_RELATIONS`, `MO_ASSAY_RELATIONS_RESCUE`) is read at CALL time and
defaults to ON; `0` disables. A knob-less run dispatches the relation prompt and adds a
`relations` key; set the knob to `0` for a byte-identical-to-pre-relations oracle — the
measured recall/doctrine (arXiv 2602.10522, 2603.24774) remains reachable by explicit
opt-out.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from collections.abc import Callable

from mini_ork.certify.context import CodeContext, imports_code_under_test
from mini_ork.certify.verdict import PROVEN, REFUTED
from mini_ork.runtime import ExecOutcome

# Callable[[str], str]  — prompt -> text. Default = default_dispatch (lazy import to
# avoid an import cycle when llm.py imports the dispatcher).
DispatchFn = Callable[[str], str]


def _default_dispatch() -> DispatchFn:
    from mini_ork.certify.llm import default_dispatch

    return default_dispatch


# ── env knobs (read at CALL time; DEFAULT ON; `0` disables) ──────────────────
_TRUE = {"1", "true", "yes", "on"}


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUE


def enabled() -> bool:
    """True iff `MO_ASSAY_RELATIONS` is truthy. DEFAULT ON; `0` disables (kill switch
    released). A knob-less run dispatches the relation prompt."""
    return _truthy(os.environ.get("MO_ASSAY_RELATIONS", "1"))


def rescue_enabled() -> bool:
    """True iff `MO_ASSAY_RELATIONS_RESCUE` is truthy (DEFAULT ON; `0` disables). Ignored
    unless `enabled()`."""
    return _truthy(os.environ.get("MO_ASSAY_RELATIONS_RESCUE", "1"))


def k_from_env() -> int:
    """int of `MO_ASSAY_RELATIONS_K`, default 3, clamped [1, 5]; non-int -> 3."""
    raw = os.environ.get("MO_ASSAY_RELATIONS_K", "3")
    try:
        k = int(raw)
    except ValueError:
        return 3
    return max(1, min(5, k))


def veto_min_from_env() -> int:
    """int of `MO_ASSAY_RELATIONS_VETO_MIN`, default 1, clamped >= 1; non-int -> 1."""
    raw = os.environ.get("MO_ASSAY_RELATIONS_VETO_MIN", "1")
    try:
        v = int(raw)
    except ValueError:
        return 1
    return max(1, v)


# ── prompt ───────────────────────────────────────────────────────────────────
REL_PROMPT = """Here is a correctness test for a bug fix:

```python
{poc}
```

THE ISSUE:
{issue}

THE CANDIDATE PATCH UNDER TEST (this is what you must try to BREAK):
```diff
{patch}
```

This patch passes the test above. A patch that merely SPECIAL-CASES the reported input passes
the single reproduction test but fails on a transformed follow-up. Your job is to propose
METAMORPHIC RELATIONS that expose exactly that: the LLM proposes a relation, a deterministic
runner executes BOTH inputs, and the relation decides — no LLM approves.

For each relation you specify:
  - SOURCE   = an input where the bug manifests,
  - TRANSFORM T, and FOLLOWUP = T(SOURCE) — the SAME bug on a DIFFERENT input, off whatever
    the patch special-cases,
  - RELATION R — a property that the CORRECT behaviour (as the issue states it) preserves
    between running the code on SOURCE and on FOLLOWUP.

Write exactly {k} SEPARATE ```python``` blocks. Each block is a standalone pytest file that:
  - imports the code under test,
  - defines module-level `SOURCE = <input where the bug manifests>`,
  - defines module-level `FOLLOWUP = <T(SOURCE)>` (may be an expression of SOURCE),
  - defines module-level `TRANSFORM = "<one line>"`,
  - defines module-level `RELATION = "<one line>"`,
  - defines ONE `def test_relation():` that calls the code on SOURCE and on FOLLOWUP and
    `assert`s R between the two outputs.

HARD RULES:
  1. R must follow from the CORRECT behaviour the issue states — never from today's (buggy)
     output.
  2. FOLLOWUP must stay in the bug's domain but off whatever the patch special-cases.
  3. IMPORT EVERY NAME YOU USE. No network — the sandbox is OFFLINE.

Output ONLY the {k} ```python``` blocks, nothing else."""

# Appended to REL_PROMPT when a CodeContext surfaces the code under test. Mirrors
# MR_CONTEXT_SUFFIX: anchor the relation to the BASE source so it imports the code under
# test instead of redefining it.
REL_CONTEXT_SUFFIX = """

THE CODE UNDER TEST (the buggy version, before any fix):
{context_text}

Every relation below MUST import the code under test from the repository (e.g. `from {module_example} import <name>`)
and call it on both SOURCE and FOLLOWUP. NEVER copy, re-implement, or redefine the code under test —
a relation of a copy proves nothing about the repository. The structural guard rejects
candidates that do not import the code under test, so they will be discarded."""


def _example_module(modules: tuple[str, ...]) -> str:
    """Pick a representative module for the prompt example — first entry, parent preferred."""
    if not modules:
        return "<module>"
    first = modules[0]
    return first.split(".")[0] if "." in first else first


# ── fenced block extraction (EVERY python block, not just the first) ────────
_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)


def _blocks(txt: str) -> list[str]:
    """Return every fenced python block in `txt`, in emission order, stripped."""
    if not txt:
        return []
    return [m.strip() for m in _BLOCK_RE.findall(txt) if m.strip()]


# ── structural law ───────────────────────────────────────────────────────────
def _name_ids(target: ast.AST) -> set[str]:
    """Collect every bare `Name` id a target binds (Tuple/List/Starred recursion)."""
    ids: set[str] = set()
    if isinstance(target, ast.Name):
        ids.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            ids |= _name_ids(elt)
    elif isinstance(target, ast.Starred):
        ids |= _name_ids(target.value)
    return ids


def _module_targets(node: ast.AST) -> list[ast.expr]:
    """Binding targets of an assignment-ish node, or []. Handles Assign/AnnAssign/
    AugAssign/For/withitem/NamedExpr so `admissible` can find every rebind."""
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, ast.AnnAssign) and node.target is not None:
        return [node.target]
    if isinstance(node, ast.AugAssign):
        return [node.target]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.target]
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [node.optional_vars]
    if isinstance(node, ast.NamedExpr):
        return [node.target]
    return []


def admissible(src: str, context: CodeContext | None = None) -> tuple[bool, str]:
    """True iff `src` is a well-formed metamorphic relation; else (False, why).

    The pair comes from the code that RAN, never from model prose — so the shape is
    enforced structurally:
      * parses;
      * exactly one top-level `def test_*`;
      * exactly one module-level assignment each to `SOURCE` and `FOLLOWUP`;
      * their assigned values differ (ast.dump) and FOLLOWUP is not bare `SOURCE`;
      * the test loads BOTH names and has >=1 `assert`;
      * neither name is rebound elsewhere (incl. `global`);
      * `TRANSFORM`/`RELATION` are non-empty module-level str constants;
      * if `context.modules`, `imports_code_under_test` holds (import it, never copy it).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, f"does not parse ({e.msg})"

    module = tree.body

    test_fns = [n for n in module if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    if len(test_fns) != 1:
        return False, "must define exactly one top-level `def test_*` function"
    test_fn = test_fns[0]

    def assigned_at_module(name: str) -> list[ast.Assign | ast.AnnAssign]:
        found: list[ast.Assign | ast.AnnAssign] = []
        for n in module:
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        found.append(n)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
                found.append(n)
        return found

    source_asgns = assigned_at_module("SOURCE")
    followup_asgns = assigned_at_module("FOLLOWUP")
    if len(source_asgns) != 1:
        return False, "expected exactly one module-level assignment to SOURCE"
    if len(followup_asgns) != 1:
        return False, "expected exactly one module-level assignment to FOLLOWUP"

    src_val = source_asgns[0].value
    fup_val = followup_asgns[0].value
    if src_val is None or fup_val is None:
        return False, "SOURCE/FOLLOWUP must have a value"
    if ast.dump(src_val) == ast.dump(fup_val):
        return False, "SOURCE and FOLLOWUP are the same input"
    if isinstance(fup_val, ast.Name) and fup_val.id == "SOURCE":
        return False, "FOLLOWUP is just SOURCE (no transform)"

    transform_ok = relation_ok = False
    for n in module:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            name = n.targets[0].id
            if name == "TRANSFORM" and isinstance(n.value, ast.Constant) \
                    and isinstance(n.value.value, str) and n.value.value.strip():
                transform_ok = True
            if name == "RELATION" and isinstance(n.value, ast.Constant) \
                    and isinstance(n.value.value, str) and n.value.value.strip():
                relation_ok = True
    if not transform_ok:
        return False, "TRANSFORM must be a non-empty module-level string constant"
    if not relation_ok:
        return False, "RELATION must be a non-empty module-level string constant"

    loaded = {n.id for n in ast.walk(test_fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    if "SOURCE" not in loaded:
        return False, "the test never loads SOURCE"
    if "FOLLOWUP" not in loaded:
        return False, "the test never loads FOLLOWUP"
    if not any(isinstance(n, ast.Assert) for n in ast.walk(test_fn)):
        return False, "the test has no assert"

    allowed = {id(source_asgns[0]), id(followup_asgns[0])}
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            if "SOURCE" in node.names or "FOLLOWUP" in node.names:
                return False, "must not declare SOURCE/FOLLOWUP global"
        if isinstance(node, ast.arg):
            if node.arg in ("SOURCE", "FOLLOWUP"):
                return False, "SOURCE/FOLLOWUP is rebound"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in ("SOURCE", "FOLLOWUP"):
                return False, "SOURCE/FOLLOWUP is rebound"
        for target in _module_targets(node):
            for nm in _name_ids(target):
                if nm in ("SOURCE", "FOLLOWUP") and id(node) not in allowed:
                    return False, "SOURCE/FOLLOWUP is rebound outside its single definition"

    if context is not None and context.modules:
        if not imports_code_under_test(src, context.modules):
            return False, "does not import the code under test"

    return True, ""


def describe(src: str) -> dict:
    """Describe a relation from its CODE (never from model prose).

    Returns `transform` / `relation` (<=200 chars) and `source` / `followup`
    (`ast.unparse` of the assigned expressions, <=300 chars).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"transform": "", "relation": "", "source": "", "followup": ""}
    out: dict[str, str] = {"transform": "", "relation": "", "source": "", "followup": ""}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            name = n.targets[0].id
            if name in ("TRANSFORM", "RELATION"):
                if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                    out[name.lower()] = n.value.value[:200]
            elif name in ("SOURCE", "FOLLOWUP"):
                out[name.lower()] = ast.unparse(n.value)[:300]
    return out


# ── generator ────────────────────────────────────────────────────────────────
def build(
    poc: str,
    issue: str,
    patch: str = "",
    *,
    k: int = 3,
    tries: int = 2,
    dispatch: DispatchFn | None = None,
    context: CodeContext | None = None,
) -> tuple[list[tuple[str, str]], list[dict]]:
    """Generate relations and return `(admitted, rejected)`.

    `admitted` is `[(name, standalone_source), ...]` — at most `k`, named `rel_1, rel_2, …`
    in emission order across tries. `rejected` carries `{"name","status":"inadmissible",
    "why","src"}`. Retries (feedback = the rejection whys) only while admitted < k.
    Empty model output -> `([], [])`.
    """
    d_fn = dispatch or _default_dispatch()
    admitted: list[tuple[str, str]] = []
    rejected: list[dict] = []
    counter = 0
    feedback = ""
    suffix = ""
    if context is not None and context.text:
        suffix = REL_CONTEXT_SUFFIX.format(
            context_text=context.text,
            module_example=_example_module(context.modules),
        )
    for _ in range(tries):
        if len(admitted) >= k:
            break
        prompt = REL_PROMPT.format(
            poc=poc[:1400],
            issue=issue[:1800],
            patch=(patch or "(not provided)")[:2500],
            k=k,
        ) + suffix + feedback
        raw = d_fn(prompt)
        blocks = _blocks(raw)
        if not blocks:
            continue
        for block in blocks:
            counter += 1
            name = f"rel_{counter}"
            ok, why = admissible(block, context)
            if ok:
                admitted.append((name, block))
                if len(admitted) >= k:
                    break
            else:
                rejected.append({"name": name, "status": "inadmissible", "why": why, "src": block})
        if len(admitted) >= k:
            break
        whys = [r["why"] for r in rejected]
        if whys:
            feedback = (
                "\n\nIMPORTANT: some of your relations were rejected by the structural guard:\n"
                + "\n".join(f"  - {w}" for w in whys)
                + "\nFix every relation so it imports the code under test, defines exactly one "
                  "`def test_relation()` and one module-level SOURCE / FOLLOWUP / TRANSFORM / "
                  "RELATION, and asserts a relation between the two outputs."
            )
    return admitted[:k], rejected


# ── execution & decision ─────────────────────────────────────────────────────
_ASSERTION_EXCS = ("AssertionError", "Failed")


def _informative(o: ExecOutcome) -> bool:
    """A relation outcome is evidence only if it passed, or failed on an assertion."""
    st = o.status
    exc = o.exc if isinstance(getattr(o, "exc", ""), str) else ""
    return st == "passed" or (st == "failed" and exc in _ASSERTION_EXCS)


def classify(on_patch: ExecOutcome, on_base: ExecOutcome | None) -> tuple[str, bool]:
    """Classify a relation from its two outcomes; `(status, repaired)`.

    `on_base is None` only when base must not/has not run — defensively abstain. Base only
    ATTRIBUTES: a relation that holds on base is exactly the one a special-cased patch breaks.
    """
    st = on_patch.status
    exc = on_patch.exc if isinstance(getattr(on_patch, "exc", ""), str) else ""
    if st == "passed":
        if on_base is None:
            return ("abstained", False)
        base_exc = on_base.exc if isinstance(getattr(on_base, "exc", ""), str) else ""
        repaired = on_base.status == "failed" and base_exc in _ASSERTION_EXCS
        return ("held", repaired)
    if st == "failed" and exc in _ASSERTION_EXCS:
        if on_base is None:
            return ("abstained", False)
        if on_base.status == "passed":
            return ("violated", False)
        return ("violated_unattributed", False)
    return ("abstained", False)


def _abstain_why(o: ExecOutcome) -> str:
    st = o.status
    exc = o.exc if isinstance(getattr(o, "exc", ""), str) else ""
    if st == "test_defect":
        return f"test_defect on patch ({exc})" if exc else "test_defect on patch"
    if st == "error":
        return f"error on patch ({exc})" if exc else "error on patch"
    if st == "no_run":
        return "no_run on patch"
    return f"patch {st} with exc {exc!r} (not an assertion failure)"


def _tail(text: str, n: int) -> str:
    return text[-n:] if isinstance(text, str) else ""


def _patch_failure_text(o: ExecOutcome) -> str:
    exc = o.exc if isinstance(getattr(o, "exc", ""), str) else ""
    out = o.output if isinstance(getattr(o, "output", ""), str) else ""
    return f"{exc}\n{out}".strip()


def decide(records: list[dict], *, veto_min: int, rescue: bool) -> tuple[str | None, str]:
    """Turn executed relations into `(verdict, reason)`.

    >= veto_min attributed `violated` -> REFUTED (names the first violated relation, its
    RELATION, TRANSFORM, and source/follow-up pair). Else, under rescue, held >= 2 and
    repaired >= 1 with no violated/violated_unattributed -> PROVEN "relative to" the held
    relations. Otherwise (None, "") — the caller must not treat that as a verdict change.
    """
    violated = [r for r in records if r.get("status") == "violated"]
    if len(violated) >= veto_min:
        r = violated[0]
        reason = (
            f"metamorphic relation {r['name']} violated: RELATION={r['relation']!r} "
            f"TRANSFORM={r['transform']!r} SOURCE={r['source']!r} FOLLOWUP={r['followup']!r}"
        )
        return (REFUTED, reason)
    held = [r for r in records if r.get("status") == "held"]
    repaired = [r for r in held if r.get("repaired")]
    vu = [r for r in records if r.get("status") == "violated_unattributed"]
    if rescue and len(held) >= 2 and len(repaired) >= 1 and not violated and not vu:
        names = ", ".join(f"{r['name']} ({r['relation']})" for r in held)
        return (PROVEN, f"PROVEN relative to {len(held)} relations: {names}")
    return (None, "")


def _observe(result: dict) -> None:
    """Print ONE stderr line `[assay-relations] ` + json (minus src/detail per record)."""
    slim_records = [
        {k: v for k, v in r.items() if k not in ("src", "detail")}
        for r in result["records"]
    ]
    slim = {k: v for k, v in result.items() if k != "records"}
    slim["records"] = slim_records
    print("[assay-relations] " + json.dumps(slim, sort_keys=True), file=sys.stderr)


def check(
    poc: str,
    issue: str,
    patch: str,
    *,
    runner,
    dispatch: DispatchFn | None = None,
    context: CodeContext | None = None,
    k: int | None = None,
    rescue: bool = False,
) -> dict:
    """Build + execute + decide, returning the full relations record.

    Exactly `{"mode", "k", "veto_min", "records", "counts", "verdict", "reason"}` where
    `counts` = {"held","repaired","violated","violated_unattributed","abstained",
    "inadmissible"} and `verdict` is REFUTED | PROVEN | None.
    """
    k = k if k is not None else k_from_env()
    veto_min = veto_min_from_env()
    admitted, rejected = build(poc, issue, patch, k=k, dispatch=dispatch, context=context)

    records: list[dict] = []
    for name, src in admitted:
        d = describe(src)
        rec = {
            "name": name, "status": "", "why": "",
            "transform": d["transform"], "relation": d["relation"],
            "source": d["source"], "followup": d["followup"],
            "on_patch": None, "on_base": None, "repaired": False, "src": src,
        }
        try:
            on_patch = runner.run_test(src, patch=patch)
        except Exception as e:  # a raising run_test abstains; why names it
            rec["status"] = "abstained"
            rec["why"] = f"run_test raised: {e}"
            records.append(rec)
            continue
        rec["on_patch"] = on_patch.status
        if not _informative(on_patch):
            rec["status"] = "abstained"
            rec["why"] = _abstain_why(on_patch)
            records.append(rec)
            continue
        try:
            on_base = runner.run_test(src)
        except Exception as e:
            rec["status"] = "abstained"
            rec["why"] = f"base run_test raised: {e}"
            records.append(rec)
            continue
        rec["on_base"] = on_base.status
        status, repaired = classify(on_patch, on_base)
        rec["status"] = status
        rec["repaired"] = repaired
        if status == "violated":
            rec["pair"] = {"source": d["source"], "followup": d["followup"]}
            rec["detail"] = _tail(_patch_failure_text(on_patch), 600)
        records.append(rec)

    counts = {"held": 0, "repaired": 0, "violated": 0, "violated_unattributed": 0,
              "abstained": 0, "inadmissible": len(rejected)}
    for r in records:
        st = r["status"]
        if st == "held":
            counts["held"] += 1
            if r["repaired"]:
                counts["repaired"] += 1
        elif st in ("violated", "violated_unattributed", "abstained"):
            counts[st] += 1

    verdict, reason = decide(records, veto_min=veto_min, rescue=rescue)
    result = {
        "mode": "rescue" if rescue else "veto",
        "k": k,
        "veto_min": veto_min,
        "records": records,
        "counts": counts,
        "verdict": verdict,
        "reason": reason,
    }
    _observe(result)
    return result


__all__ = [
    "admissible",
    "build",
    "check",
    "classify",
    "decide",
    "describe",
    "enabled",
    "k_from_env",
    "rescue_enabled",
    "veto_min_from_env",
]
