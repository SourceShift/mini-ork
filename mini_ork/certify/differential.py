"""Differential behavioural-equivalence — the oracle term that proves a patch breaks
NEIGHBOURING behaviour in the same function it fixed.

WHAT IT IS FOR
───────────────
A reproduction test is a single point in input space. A patch that fixes the reported
input and breaks neighbouring behaviour in the same function still passes that single
reproduction test (arXiv 2610.00182, 2602.15761). Differential equivalence attacks that
hole: the LLM proposes ONE input suite partitioned `bug_domain` / `preserve`; a
deterministic runner executes the SAME file on base and on the patch; canonical
observation equality (sha256 of canonical JSON) decides. A confirmed divergence on a
`preserve` input is collateral damage — the patch changed output where the issue's wrong
behaviour could not occur — so a would-be PROVEN is vetoed to REFUTED, with the input and
both observations recorded as evidence.

THE LAW
───────
    **A preserve input diverges only if a correct fix cannot change its output.** A
    correct fix returns EXACTLY the buggy output on every `preserve` input (same type and
    representation); the `bug_domain` inputs are where the patch is EXPECTED to change
    behaviour. The suite is anchored on at least one `bug_domain` divergence (re-establishing
    the PoC's fail@base ∧ pass@head on the suite) — without it the term proves nothing and
    abstains. A preserve divergence is believed only after a confirm re-run (base once, then
    head once) reproduces both first observations.

The LLM proposes, execution decides, no LLM approves. PROVEN | REFUTED | UNVERIFIED, and an
input not observed on BOTH sides is excluded and never counts as agreement.

WHY THE KILL SWITCH IS OFF BY DEFAULT
─────────────────────────────────────
Every knob (`MO_ASSAY_DIFFERENTIAL`, `MO_ASSAY_DIFFERENTIAL_N`,
`MO_ASSAY_DIFFERENTIAL_VETO_MIN`) is read at CALL time and defaults to OFF. A knob-less run
dispatches no differential prompt and adds no `differential` key, so Verdicts are
byte-identical to the pre-differential oracle until an operator opts in. This term can only
turn a would-be PROVEN into REFUTED; it never creates PROVEN and never runs on a would-be
REFUTED/UNVERIFIED.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from collections.abc import Callable

from mini_ork.certify.context import CodeContext, imports_code_under_test
from mini_ork.certify.verdict import REFUTED
from mini_ork.runtime import ExecOutcome

# Callable[[str], str]  — prompt -> text. Default = default_dispatch (lazy import to
# avoid an import cycle when llm.py imports the dispatcher).
DispatchFn = Callable[[str], str]


def _default_dispatch() -> DispatchFn:
    from mini_ork.certify.llm import default_dispatch

    return default_dispatch


# ── env knobs (read at CALL time; DEFAULT OFF) ───────────────────────────────
_TRUE = {"1", "true", "yes", "on"}


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUE


def enabled() -> bool:
    """True iff `MO_ASSAY_DIFFERENTIAL` is set to a truthy value. Default OFF (kill switch
    engaged): a knob-less run must be byte-identical to the pre-differential oracle."""
    return _truthy(os.environ.get("MO_ASSAY_DIFFERENTIAL", ""))


def n_from_env() -> int:
    """int of `MO_ASSAY_DIFFERENTIAL_N`, default 6, clamped [2, 8]; non-int -> 6."""
    raw = os.environ.get("MO_ASSAY_DIFFERENTIAL_N", "6")
    try:
        n = int(raw)
    except ValueError:
        return 6
    return max(2, min(8, n))


def veto_min_from_env() -> int:
    """int of `MO_ASSAY_DIFFERENTIAL_VETO_MIN`, default 2, clamped >= 1; non-int -> 2.

    Default 2 (not 1) is deliberate: ONE mislabelled `preserve` input must never sink a
    correct fix — the invariants' supermajority rule carried into the differential term."""
    raw = os.environ.get("MO_ASSAY_DIFFERENTIAL_VETO_MIN", "2")
    try:
        v = int(raw)
    except ValueError:
        return 2
    return max(1, v)


def split(n: int) -> tuple[int, int]:
    """`(b, p)` = the bug_domain / preserve counts for a suite of `n` inputs."""
    b = max(1, n // 3)
    return b, n - b


# ── prompt ───────────────────────────────────────────────────────────────────
DIFF_PROMPT = """Here is a correctness test for a bug fix:

```python
{poc}
```

THE ISSUE:
{issue}

THE CANDIDATE PATCH UNDER TEST (this is what you must try to BREAK):
```diff
{patch}
```

This patch passes the test above. A patch that fixes the reported input but breaks NEIGHBOURING
behaviour in the same function still passes that single reproduction test. You must expose exactly
that: you propose a DIFFERENTIAL INPUT SUITE partitioned into bug-domain inputs and preserve inputs,
a deterministic runner executes the SAME code on the base and on the patch for every input, and
canonical output equality decides — no LLM approves.

  - bug_domain = inputs where the bug manifests (the FIRST must be the issue's reported input),
  - preserve   = inputs where the issue's wrong behaviour CANNOT occur, so a correct fix must return
    EXACTLY the buggy output (same type and representation); prefer inputs that pass through the
    lines the patch changes.

Write ONE ```python``` block. The block is a plain module (NOT a pytest test) that:
  - imports the code under test,
  - defines module-level CASES = [("bug_domain", <input>), ..., ("preserve", <input>), ...] with
    exactly {b} bug_domain entries (the FIRST = the issue's reported input) followed by {p} preserve
    entries,
  - defines ONE def observe(x): that calls the code under test on x and RETURNS JSON-serialisable
    plain data.

HARD RULES:
  1. No asserts, no randomness, no time, no IO. Exceptions propagate — do not catch them.
  2. IMPORT EVERY NAME YOU USE. No network — the sandbox is OFFLINE.
  3. observe returns plain JSON-serialisable data (numbers, strings, lists, dicts) — never sets,
     bytes, or custom objects.

Output ONLY the ```python``` block, nothing else. The block MUST contain the phrase
DIFFERENTIAL INPUT SUITE in a comment so a routing guard recognises it."""

# Appended to DIFF_PROMPT when a CodeContext surfaces the code under test. Mirrors
# REL_CONTEXT_SUFFIX: anchor the suite to the BASE source so it imports the code under
# test instead of redefining it.
DIFF_CONTEXT_SUFFIX = """

THE CODE UNDER TEST (the buggy version, before any fix):
{context_text}

Every input above must call the code under test imported from the repository (e.g. `from {module_example} import <name>`).
NEVER copy, re-implement, or redefine the code under test — observing a copy proves nothing about the repository.
The structural guard rejects candidates that do not import the code under test, so they will be discarded."""


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
    """True iff `src` is a well-formed differential input suite; else (False, why).

    The suite comes from the code that RAN, never from model prose — so the shape is
    enforced structurally:
      * parses;
      * no `MO_DIFF` substring;
      * exactly one module-level `CASES = <List|Tuple>` of >= 2 2-tuples whose first
        element is a str Constant in {"bug_domain","preserve"};
      * >= 1 of each partition; no two inputs with equal `ast.dump`;
      * exactly one top-level `def observe` with one positional arg (no *args/**kwargs);
      * no top-level `def test_*` / `class Test*`;
      * `CASES`/`observe` bound nowhere else (incl. `global`, args);
      * if `context.modules`, `imports_code_under_test` holds (import it, never copy it).
    """
    if "MO_DIFF" in src:
        return False, "must not contain MO_DIFF"
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, f"does not parse ({e.msg})"

    module = tree.body

    for n in module:
        if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"):
            return False, "must not define a top-level test function"
        if isinstance(n, ast.ClassDef) and n.name.startswith("Test"):
            return False, "must not define a top-level Test class"

    cases_nodes = [
        n for n in module
        if isinstance(n, ast.Assign) and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "CASES"
    ]
    if len(cases_nodes) != 1:
        return False, "expected exactly one module-level assignment to CASES"
    cases_node = cases_nodes[0]
    if not isinstance(cases_node.value, (ast.List, ast.Tuple)):
        return False, "CASES must be a literal list or tuple"

    elts = cases_node.value.elts
    if len(elts) < 2:
        return False, "CASES must have at least two (partition, input) pairs"
    parts: list[str] = []
    dumps: set[str] = set()
    for elt in elts:
        if not isinstance(elt, (ast.Tuple, ast.List)) or len(elt.elts) != 2:
            return False, "every CASES entry must be a (partition, input) pair"
        part = elt.elts[0]
        if not (isinstance(part, ast.Constant) and isinstance(part.value, str)) \
                or part.value not in ("bug_domain", "preserve"):
            return False, "every CASES entry's first element must be 'bug_domain' or 'preserve'"
        d = ast.dump(elt.elts[1])
        if d in dumps:
            return False, "two CASES inputs are identical"
        dumps.add(d)
        parts.append(part.value)
    if "bug_domain" not in parts:
        return False, "CASES must contain at least one bug_domain input"
    if "preserve" not in parts:
        return False, "CASES must contain at least one preserve input"

    observe_fns = [n for n in module if isinstance(n, ast.FunctionDef) and n.name == "observe"]
    if len(observe_fns) != 1:
        return False, "must define exactly one top-level `def observe`"
    obs = observe_fns[0]
    args = obs.args
    if args.vararg is not None or args.kwarg is not None:
        return False, "observe must take exactly one positional argument"
    if len(args.posonlyargs) + len(args.args) != 1:
        return False, "observe must take exactly one positional argument"

    allowed = {id(cases_node), id(obs)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            if "CASES" in node.names or "observe" in node.names:
                return False, "must not declare CASES/observe global"
        if isinstance(node, ast.arg):
            if node.arg in ("CASES", "observe"):
                return False, "CASES/observe is rebound"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in ("CASES", "observe") and id(node) not in allowed:
                return False, "CASES/observe is rebound"
        for target in _module_targets(node):
            for nm in _name_ids(target):
                if nm in ("CASES", "observe") and id(node) not in allowed:
                    return False, "CASES/observe is rebound outside its single definition"

    if context is not None and context.modules:
        if not imports_code_under_test(src, context.modules):
            return False, "does not import the code under test"

    return True, ""


def cases(src: str) -> list[tuple[str, str]]:
    """`[(partition, input_str)]` in CASES order — from the code that RAN, never prose."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[tuple[str, str]] = []
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "CASES":
            if isinstance(n.value, (ast.List, ast.Tuple)):
                for elt in n.value.elts:
                    if isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) == 2:
                        part = elt.elts[0]
                        if isinstance(part, ast.Constant) and isinstance(part.value, str):
                            out.append((part.value, ast.unparse(elt.elts[1])[:300]))
    return out


def select(cases: list[tuple[str, str]], n: int) -> list[int]:
    """First `b` bug_domain + first `p` preserve indices, in index order."""
    b, p = split(n)
    bug = [i for i, (part, _) in enumerate(cases) if part == "bug_domain"][:b]
    preserve = [i for i, (part, _) in enumerate(cases) if part == "preserve"][:p]
    return sorted(bug + preserve)


# ── generator ────────────────────────────────────────────────────────────────
def build(
    poc: str,
    issue: str,
    patch: str = "",
    *,
    n: int = 4,
    tries: int = 2,
    dispatch: DispatchFn | None = None,
    context: CodeContext | None = None,
) -> tuple[str | None, str | None, list[dict]]:
    """Generate one differential input suite; return `(suite_name, suite_src, rejected)`.

    Blocks are named `suite_1, suite_2, …` across tries; the FIRST admissible block wins.
    `rejected` carries `{"name","status":"inadmissible","why","src"}`. Retry (feedback =
    the rejection whys) only while none admitted. Empty output or a raising dispatch
    yields no blocks for that try.
    """
    d_fn = dispatch or _default_dispatch()
    b, p = split(n)
    counter = 0
    rejected: list[dict] = []
    feedback = ""
    suffix = ""
    if context is not None and context.text:
        suffix = DIFF_CONTEXT_SUFFIX.format(
            context_text=context.text,
            module_example=_example_module(context.modules),
        )
    for _ in range(tries):
        prompt = DIFF_PROMPT.format(
            poc=poc[:1400],
            issue=issue[:1800],
            patch=(patch or "(not provided)")[:2500],
            b=b,
            p=p,
        ) + suffix + feedback
        try:
            raw = d_fn(prompt)
        except Exception:
            raw = ""
        blocks = _blocks(raw)
        for block in blocks:
            counter += 1
            name = f"suite_{counter}"
            ok, why = admissible(block, context)
            if ok:
                return name, block, rejected
            rejected.append({"name": name, "status": "inadmissible", "why": why, "src": block})
        whys = [r["why"] for r in rejected]
        if whys:
            feedback = (
                "\n\nIMPORTANT: some of your input suites were rejected by the structural guard:\n"
                + "\n".join(f"  - {w}" for w in whys)
                + "\nFix the suite so it imports the code under test and defines exactly one "
                  "module-level CASES list and one `def observe(x)`."
            )
    return None, None, rejected


# ── harness & observation parsing ────────────────────────────────────────────
# Appended to a suite via `harness`; `{case}` is substituted with `str.replace` (NOT
# `str.format` — the block contains dict braces). The test does not assert: every executed
# case returns status "passed", and the evidence flows through the sha256 of the canonical
# JSON emitted on `atexit`.
_HARNESS_BLOCK = r"""# -- mini-ork differential harness (appended by differential.py; not model-authored) --
MO_DIFF_CASE = {case}

def test_mo_differential_observe():
    import atexit, hashlib, json, os
    value = observe(CASES[MO_DIFF_CASE][1])
    canon = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    line = json.dumps({"case": MO_DIFF_CASE, "obs": canon[:300],
                       "sha256": hashlib.sha256(canon.encode()).hexdigest()}, sort_keys=True)
    atexit.register(os.write, 1, ("MO_DIFF_OBS " + line + "\n").encode())
"""


def harness(src: str, case: int) -> str:
    """`src` + the observation harness, running `observe(CASES[case][1])`."""
    return src.rstrip() + "\n\n\n" + _HARNESS_BLOCK.replace("{case}", str(case))


_OBS_RE = re.compile(r"^MO_DIFF_OBS (\{.*\})\s*$", re.M)
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def parse_obs(o: ExecOutcome, case: int) -> tuple[dict | None, str]:
    """Extract the canonical observation from a harness `ExecOutcome`.

    Returns `({"obs","sha256"}, "")` on a clean observation; else `(None, why)`. The
    observation line must survive `Crucible`'s `output[-800:]` tail.
    """
    if o.status != "passed":
        exc = o.exc if isinstance(getattr(o, "exc", ""), str) else ""
        return None, f"{o.status} ({exc})"
    out = o.output if isinstance(getattr(o, "output", ""), str) else ""
    lines = _OBS_RE.findall(out)
    if not lines:
        return None, "no observation line"
    if len(lines) > 1:
        return None, "ambiguous observation"
    try:
        data = json.loads(lines[0])
    except json.JSONDecodeError:
        return None, "malformed observation"
    if data.get("case") != case:
        return None, "malformed observation"
    sha = data.get("sha256")
    obs = data.get("obs")
    if not (isinstance(sha, str) and _SHA_RE.fullmatch(sha)):
        return None, "malformed observation"
    if not isinstance(obs, str):
        return None, "malformed observation"
    return {"obs": obs, "sha256": sha}, ""


def _confirm_divergence(
    runner, h: str, patch: str, base_sha: str, head_sha: str, case: int
) -> tuple[bool, str]:
    """Re-run base once then head once; both reproduce their first sha256 -> confirmed."""
    try:
        base2 = runner.run_test(h)
    except Exception as e:
        return False, f"base: run_test raised: {e}"
    try:
        head2 = runner.run_test(h, patch=patch)
    except Exception as e:
        return False, f"head: run_test raised: {e}"
    b2, _ = parse_obs(base2, case)
    if b2 is None:
        return False, "nondeterministic observation (base)"
    if b2["sha256"] != base_sha:
        return False, "nondeterministic observation (base)"
    h2, _ = parse_obs(head2, case)
    if h2 is None:
        return False, "nondeterministic observation (head)"
    if h2["sha256"] != head_sha:
        return False, "nondeterministic observation (head)"
    return True, ""


# ── execution & decision ─────────────────────────────────────────────────────
def decide(records: list[dict], *, veto_min: int) -> tuple[str | None, str, str]:
    """Turn executed cases into `(verdict, state, reason)`.

    No `bug_domain` `diverged` -> unverified (the suite does not observe what the patch
    changed). >= veto_min confirmed preserve divergences -> REFUTED. A sub-veto preserve
    divergence, or zero preserve agreements, -> unverified. Otherwise equivalent.
    """
    bug_diverged = [r for r in records if r["partition"] == "bug_domain" and r["status"] == "diverged"]
    if not bug_diverged:
        return (None, "unverified",
                "no bug_domain input diverged: the suite does not observe what the patch changed")
    observed_preserve = [
        r for r in records if r["partition"] == "preserve" and r["status"] in ("agree", "diverged")
    ]
    preserve_diverged = [r for r in observed_preserve if r["status"] == "diverged"]
    preserve_agree = [r for r in observed_preserve if r["status"] == "agree"]
    d = len(preserve_diverged)
    m = len(observed_preserve)
    if d >= veto_min:
        r = preserve_diverged[0]
        reason = (
            f"differential: collateral divergence on preserve input {r['name']}: "
            f"input={r['input']} base={r['base_obs']} head={r['head_obs']} "
            f"({d} of {m} observed preserve inputs diverged)"
        )
        return (REFUTED, "refuted", reason)
    if d > 0:
        return (None, "unverified",
                f"{d} of {m} observed preserve inputs diverged (below veto_min {veto_min})")
    if not preserve_agree:
        return (None, "unverified", "no preserve input agreed on both sides")
    k = len(bug_diverged)
    return (None, "equivalent", f"{len(preserve_agree)} preserve inputs agree; {k} bug_domain inputs diverge")


def _observe(result: dict) -> None:
    """Print ONE stderr line `[assay-differential] ` + json (minus `src`)."""
    slim = {k: v for k, v in result.items() if k != "src"}
    print("[assay-differential] " + json.dumps(slim, sort_keys=True), file=sys.stderr)


def check(
    poc: str,
    issue: str,
    patch: str,
    *,
    runner,
    dispatch: DispatchFn | None = None,
    context: CodeContext | None = None,
    n: int | None = None,
) -> dict:
    """Build + execute + decide, returning the full differential record.

    Exactly `{"n","veto_min","suite","src","anchored","records","counts","state",
    "verdict","reason"}`; `counts` = {"bug_diverged","bug_agree","preserve_agree",
    "preserve_diverged","excluded","inadmissible"}; `verdict` is REFUTED | None. No
    admissible suite -> `records == []`, `unverified`, "no admissible input suite".
    """
    n = n if n is not None else n_from_env()
    veto_min = veto_min_from_env()
    suite_name, suite_src, rejected = build(poc, issue, patch, n=n, dispatch=dispatch, context=context)

    counts = {"bug_diverged": 0, "bug_agree": 0, "preserve_agree": 0,
              "preserve_diverged": 0, "excluded": 0, "inadmissible": len(rejected)}
    if suite_src is None:
        result = {
            "n": n, "veto_min": veto_min, "suite": None, "src": None,
            "anchored": False, "records": [], "counts": counts,
            "state": "unverified", "verdict": None, "reason": "no admissible input suite",
        }
        _observe(result)
        return result

    all_cases = cases(suite_src)
    records: list[dict] = []
    for i in select(all_cases, n):
        partition, input_str = all_cases[i]
        rec = {
            "name": f"case_{i}", "case": i, "partition": partition, "input": input_str,
            "status": "", "why": "", "on_base": None, "on_head": None,
            "base_obs": None, "head_obs": None, "base_sha256": None, "head_sha256": None,
        }
        h = harness(suite_src, i)
        try:
            base = runner.run_test(h)
        except Exception as e:
            rec["status"] = "excluded"
            rec["why"] = f"base: run_test raised: {e}"
            records.append(rec)
            continue
        base_obs, why = parse_obs(base, i)
        if base_obs is None:
            rec["status"] = "excluded"
            rec["why"] = f"base: {why}"
            records.append(rec)
            continue
        rec["on_base"] = base.status
        try:
            head = runner.run_test(h, patch=patch)
        except Exception as e:
            rec["status"] = "excluded"
            rec["why"] = f"head: run_test raised: {e}"
            records.append(rec)
            continue
        head_obs, why = parse_obs(head, i)
        if head_obs is None:
            rec["status"] = "excluded"
            rec["why"] = f"head: {why}"
            records.append(rec)
            continue
        rec["on_head"] = head.status
        rec["base_obs"] = base_obs["obs"]
        rec["base_sha256"] = base_obs["sha256"]
        rec["head_obs"] = head_obs["obs"]
        rec["head_sha256"] = head_obs["sha256"]
        if base_obs["sha256"] == head_obs["sha256"]:
            rec["status"] = "agree"
        elif partition == "bug_domain":
            rec["status"] = "diverged"
        else:
            confirmed, why = _confirm_divergence(runner, h, patch,
                                                 base_obs["sha256"], head_obs["sha256"], i)
            if confirmed:
                rec["status"] = "diverged"
            else:
                rec["status"] = "excluded"
                rec["why"] = why
        records.append(rec)

    for r in records:
        if r["status"] == "excluded":
            counts["excluded"] += 1
        elif r["partition"] == "bug_domain":
            counts["bug_diverged" if r["status"] == "diverged" else "bug_agree"] += 1
        else:
            counts["preserve_diverged" if r["status"] == "diverged" else "preserve_agree"] += 1

    anchored = any(r["partition"] == "bug_domain" and r["status"] == "diverged" for r in records)
    verdict, state, reason = decide(records, veto_min=veto_min)
    result = {
        "n": n, "veto_min": veto_min, "suite": suite_name, "src": suite_src,
        "anchored": anchored, "records": records, "counts": counts,
        "state": state, "verdict": verdict, "reason": reason,
    }
    _observe(result)
    return result


__all__ = [
    "admissible",
    "build",
    "cases",
    "check",
    "decide",
    "enabled",
    "harness",
    "n_from_env",
    "parse_obs",
    "select",
    "split",
    "veto_min_from_env",
]
