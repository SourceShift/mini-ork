"""suite_adequacy — deterministic AST-mutation adequacy for the code-fix verifier.

A green target-repo suite is only evidence if that suite can detect broken
code. This module measures exactly that: it generates deterministic single-node
AST mutants over the files a candidate changed, runs the *real* test command
against each mutant in a throwaway copy of the repo, and scores the suite by
the fraction of non-equivalent mutants it kills. A green suite that cannot
kill its mutants is downgraded to UNVERIFIED (abstain), never silently passed.

Design constraints (all load-bearing):

* stdlib-only and offline — no LLM, no network, no ``mini_ork.dispatch``, and
  nothing imported from ``mini_ork.gates.mutation_adversary`` (that module
  mutates LLM-authored ``git apply``-able diffs, not AST nodes; there is no
  reusable AST mutator there). The only mini-ork import is ``scrubbed_test_env``
  so the child suite cannot leak the operator's credentials or mini-ork state
  pointers into the audit.
* copy, never touch — the live tree is never opened for writing. The audit runs
  inside a ``tempfile.mkdtemp`` + ``shutil.copytree`` copy that is always
  ``rmtree``-d in ``finally``.
* opt-in — ``enabled`` is True only for ``MO_SUITE_ADEQUACY == "1"``, because
  each audit costs up to 2+N extra full suite runs and changes code-fix
  verdicts.

Knobs (module defaults; see :func:`settings`):

  MO_SUITE_ADEQUACY                set to "1" to enable the audit
  MO_SUITE_ADEQUACY_MAX_MUTANTS    cap on generated mutants (default 12, 1..50)
  MO_SUITE_ADEQUACY_MIN_SCORE      adequacy threshold (default 0.6, 0..1)
  MO_SUITE_ADEQUACY_TIMEOUT_S      per-run suite timeout (default 300, >=1)
"""
from __future__ import annotations

import ast
import copy
import json
import os
import random
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_ork.verify.test_env import scrubbed_test_env

__all__ = [
    "OPERATORS",
    "MIN_VALID",
    "Mutant",
    "enabled",
    "settings",
    "generate_mutants",
    "audit_suite",
]

# Operator order is load-bearing: it is the bucket order for the deterministic
# round-robin selection and the ``OPERATORS.index(op)`` component of a site key.
OPERATORS = (
    "cmp_flip",
    "arith_swap",
    "bool_negate",
    "return_none",
    "cond_true",
    "cond_false",
    "const_off_by_one",
)

#: A suite must produce at least this many valid (killed + survived) mutants
#: before its score is trusted; below it the audit abstains UNVERIFIED.
MIN_VALID = 3

# ``<`` flips to ``>=`` (not ``>``): flipping must negate the predicate.
_CMP_FLIP = {
    ast.Lt: ast.GtE, ast.GtE: ast.Lt,
    ast.LtE: ast.Gt, ast.Gt: ast.LtE,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}

_ARITH_SWAP = {
    ast.Add: ast.Sub, ast.Sub: ast.Add,
    ast.Mult: ast.Div, ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult, ast.Mod: ast.FloorDiv, ast.Pow: ast.Mult,
}

_CANARY = 'raise ImportError("mini-ork suite-adequacy canary")\n'

#: ``original`` / ``mutated`` are clipped to this many chars.
_NODE_CHARS = 120


@dataclass(frozen=True)
class Mutant:
    """One single-node mutation of one source file.

    ``original`` / ``mutated`` are ``ast.unparse`` of the node before/after,
    clipped to ``_NODE_CHARS``. ``source`` is the full mutated file text; it is
    never serialised into the audit report.
    """

    id: str
    file: str
    line: int
    col: int
    operator: str
    original: str
    mutated: str
    source: str


@dataclass(frozen=True)
class _Site:
    """A surviving mutant candidate, pre-selection."""

    key: tuple
    op: str
    file: str
    line: int
    col: int
    original: str
    mutated: str
    source: str


def enabled(environ=None) -> bool:
    """True only when ``MO_SUITE_ADEQUACY == "1"`` (default OFF)."""
    src = os.environ if environ is None else environ
    return src.get("MO_SUITE_ADEQUACY", "0") == "1"


def settings(environ=None) -> dict:
    """Read the three audit knobs, clamped to their legal ranges.

    An unparsable value falls back to the module default; an out-of-range value
    clamps to the nearest bound (a typo cannot launch a 1000-mutant sweep).
    """
    src = os.environ if environ is None else environ

    def _int(key, default, lo, hi):
        try:
            v = int(src.get(key, str(default)))
        except (TypeError, ValueError):
            return default
        return min(max(v, lo), hi)

    def _float(key, default, lo, hi):
        try:
            v = float(src.get(key, str(default)))
        except (TypeError, ValueError):
            return default
        if lo is not None and v < lo:
            return lo
        if hi is not None and v > hi:
            return hi
        return v

    return {
        "max_mutants": _int("MO_SUITE_ADEQUACY_MAX_MUTANTS", 12, 1, 50),
        "min_score": _float("MO_SUITE_ADEQUACY_MIN_SCORE", 0.6, 0.0, 1.0),
        "timeout_s": _float("MO_SUITE_ADEQUACY_TIMEOUT_S", 300.0, 1.0, None),
    }


# ─────────────────────────────────────────────────────────────────────────────
# AST mutation — collection and single-site rewrite
# ─────────────────────────────────────────────────────────────────────────────

def _read_utf8(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _clip(s):
    return s[:_NODE_CHARS]


def _is_none_literal(node):
    return isinstance(node, ast.Constant) and node.value is None


def _is_excluded_if(node):
    """True when ``node`` is an ``If`` guard we must never mutate inside."""
    if not isinstance(node, ast.If):
        return False
    t = node.test
    if (
        isinstance(t, ast.Compare)
        and len(t.ops) == 1
        and len(t.comparators) == 1
        and isinstance(t.ops[0], ast.Eq)
        and isinstance(t.left, ast.Name)
        and t.left.id == "__name__"
        and isinstance(t.comparators[0], ast.Constant)
        and t.comparators[0].value == "__main__"
    ):
        return True
    if isinstance(t, ast.Name) and t.id == "TYPE_CHECKING":
        return True
    if (
        isinstance(t, ast.Attribute)
        and isinstance(t.value, ast.Name)
        and t.value.id == "typing"
        and t.attr == "TYPE_CHECKING"
    ):
        return True
    return False


def _site_ops(node):
    """The ``(op, op_index)`` mutation sites hosted by one AST node, in
    ``OPERATORS`` order. A ``Compare`` gets one site per op index."""
    if isinstance(node, ast.Compare):
        return [
            ("cmp_flip", i)
            for i, o in enumerate(node.ops)
            if type(o) in _CMP_FLIP
        ]
    if isinstance(node, (ast.BinOp, ast.AugAssign)):
        return [("arith_swap", 0)] if type(node.op) in _ARITH_SWAP else []
    if isinstance(node, ast.BoolOp):
        return [("bool_negate", 0)]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return [("bool_negate", 1)]
    if isinstance(node, ast.Return):
        if node.value is not None and not _is_none_literal(node.value):
            return [("return_none", 0)]
        return []
    if isinstance(node, ast.If):
        return [("cond_true", 0), ("cond_false", 1)]
    if isinstance(node, ast.IfExp):
        return [("cond_true", 0), ("cond_false", 1)]
    if isinstance(node, ast.While):
        return [("cond_false", 0)]
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return [("const_off_by_one", 0)]
    return []


def _iter_sites(node):
    """Yield ``(op, op_index, node)`` in deterministic preorder, skipping the
    excluded ``if __name__ == "__main__"`` / ``TYPE_CHECKING`` subtrees."""
    if _is_excluded_if(node):
        return
    for op, op_index in _site_ops(node):
        yield (op, op_index, node)
    for child in ast.iter_child_nodes(node):
        yield from _iter_sites(child)


def _mutate_node(node: Any, op: str, op_index: int) -> ast.AST:
    """Apply one mutation to a single (already deep-copied) node, in place.

    ``node`` is typed ``Any`` because the mutation dispatches across the AST
    node hierarchy (``BinOp``/``AugAssign``/``Compare``/``If``/``Return``/
    ``Constant``/…), whose per-node fields typeshed does not model on ``ast.AST``.

    Returns the replacement node (== ``node`` for every operator except
    ``bool_negate``'s ``not x -> x``, which returns the operand). The fallback
    returns ``node`` unchanged; ``_site_ops`` never emits such a site, but a
    no-op would be dropped by the ``src == norm`` dedup anyway.
    """
    if op == "cmp_flip":
        node.ops[op_index] = _CMP_FLIP[type(node.ops[op_index])]()
        return node
    if op == "arith_swap":
        node.op = _ARITH_SWAP[type(node.op)]()
        return node
    if op == "bool_negate":
        if op_index == 0:  # BoolOp: `and` <-> `or`
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            return node
        return node.operand  # `not x` -> `x`
    if op == "return_none":
        node.value = ast.Constant(value=None)
        return node
    if op == "cond_true":
        node.test = ast.Constant(value=True)
        return node
    if op == "cond_false":
        node.test = ast.Constant(value=False)
        return node
    if op == "const_off_by_one":
        node.value = node.value + 1
        return node
    return node


def _rewrite_one(tree: ast.AST, target_idx: int) -> tuple[ast.AST, bool]:
    """Return ``(tree, applied)`` where ``tree`` has its ``target_idx``-th site
    (in the same order ``_iter_sites`` walks) rewritten, if any."""
    counter = [0]
    applied = [False]

    def walk(node: ast.AST) -> ast.AST:
        if applied[0]:
            return node
        if _is_excluded_if(node):
            return node
        for op, op_index in _site_ops(node):
            if counter[0] == target_idx:
                applied[0] = True
                counter[0] += 1
                return _mutate_node(node, op, op_index)
            counter[0] += 1
        for field, old in ast.iter_fields(node):
            if isinstance(old, list):
                new_items = []
                for item in old:
                    if isinstance(item, ast.AST):
                        new_items.append(walk(item))
                    else:
                        new_items.append(item)
                setattr(node, field, new_items)
            elif isinstance(old, ast.AST):
                setattr(node, field, walk(old))
        return node

    new_tree = walk(tree)
    return new_tree, applied[0]


# ─────────────────────────────────────────────────────────────────────────────
# Mutant generation
# ─────────────────────────────────────────────────────────────────────────────

def _eligible_files(repo_dir, source_files):
    """Repo-relative, ``.py``, regular files inside ``repo_dir``, deduped and
    sorted. Absolute paths and ``..`` escapes are rejected."""
    root = os.path.abspath(repo_dir)
    out = []
    for f in source_files:
        if not isinstance(f, str) or not f:
            continue
        if os.path.isabs(f):
            continue
        if not f.endswith(".py"):
            continue
        norm = os.path.normpath(f)
        if norm.startswith(".."):
            continue
        if not os.path.isfile(os.path.join(root, norm)):
            continue
        out.append(norm)
    return sorted(set(out))


def generate_mutants(repo_dir, source_files, *, max_mutants=12, seed=0):
    """Generate up to ``max_mutants`` deterministic single-node mutants.

    Reads files only. Every candidate is ``ast.unparse`` of a deep-copied tree
    with exactly ONE site rewritten; a candidate that equals the normalised
    original (or an earlier mutant of the same file) is dropped. Selection is
    fully deterministic: sites are bucketed by operator in ``OPERATORS`` order,
    each bucket sorted by site key, the bucket order shuffled with ONE
    ``random.Random(seed)``, then drawn round-robin until ``max_mutants``.
    """
    files = _eligible_files(repo_dir, source_files)
    sites: list[_Site] = []
    for rel in files:
        path = os.path.join(repo_dir, rel)
        try:
            text = _read_utf8(path)
            tree = ast.parse(text)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        norm = ast.unparse(tree)
        seen = {norm}
        for k, (op, op_index, node) in enumerate(list(_iter_sites(tree))):
            mutated_tree, applied = _rewrite_one(copy.deepcopy(tree), k)
            if not applied:
                continue
            src = ast.unparse(mutated_tree)
            if src == norm or src in seen:
                continue
            seen.add(src)
            line = getattr(node, "lineno", 0)
            col = getattr(node, "col_offset", 0)
            original = _clip(ast.unparse(node))
            mutated = _clip(ast.unparse(_mutate_node(copy.deepcopy(node), op, op_index)))
            sites.append(_Site(
                key=(rel, line, col, OPERATORS.index(op), op_index),
                op=op, file=rel, line=line, col=col,
                original=original, mutated=mutated, source=src,
            ))

    if not sites:
        return []

    buckets = {op: [] for op in OPERATORS}
    for s in sites:
        buckets[s.op].append(s)
    ordered = [buckets[op] for op in OPERATORS]
    for b in ordered:
        b.sort(key=lambda s: s.key)
    random.Random(seed).shuffle(ordered)

    selected: list[_Site] = []
    idx = 0
    while len(selected) < max_mutants:
        progressed = False
        for b in ordered:
            if len(selected) >= max_mutants:
                break
            if idx < len(b):
                selected.append(b[idx])
                progressed = True
        if not progressed:
            break
        idx += 1

    selected.sort(key=lambda s: s.key)
    return [
        Mutant(id=f"M{i:02d}", file=s.file, line=s.line, col=s.col,
               operator=s.op, original=s.original, mutated=s.mutated,
               source=s.source)
        for i, s in enumerate(selected, 1)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Audit — copy, mutate, measure
# ─────────────────────────────────────────────────────────────────────────────

def _write(root, rel, content):
    with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
        fh.write(content)


def _run_cmd(cmd, cwd, env, timeout_s):
    """Run ``cmd`` in its own process group. Returns ``(rc, timed_out)``.

    ``rc`` is ``None`` when the process could not be spawned. On timeout the
    whole process group is SIGKILL-ed (pattern:
    ``probe_scorer._kill_process_group``), never leaked against a temp copy
    the caller is about to ``rmtree``.
    """
    try:
        p = subprocess.Popen(
            cmd, shell=True, cwd=cwd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None, False
    try:
        p.wait(timeout=timeout_s)
        return p.returncode, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait()
        except Exception:
            pass
        return None, True


def _child_env(copy_root):
    env = scrubbed_test_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = copy_root + (os.pathsep + prev if prev else "")
    return env


def _normalized_sources(repo_dir, files):
    norm = {}
    for rel in files:
        try:
            text = _read_utf8(os.path.join(repo_dir, rel))
            norm[rel] = ast.unparse(ast.parse(text))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
    return norm


def _build_result(verdict, reason, *, files, max_mutants, min_score, score=None,
                  killed=0, survived=0, invalid=0, baseline_rc=None,
                  canary_detected=False, mutant_timeout_s=0.0,
                  mutants=None, survivors=None):
    return {
        "verdict": verdict,
        "reason": reason,
        "score": score,
        "killed": killed,
        "survived": survived,
        "invalid": invalid,
        "total": killed + survived + invalid,
        "min_score": min_score,
        "min_valid": MIN_VALID,
        "max_mutants": max_mutants,
        "files": files,
        "baseline_rc": baseline_rc,
        "canary_detected": canary_detected,
        "mutant_timeout_s": mutant_timeout_s,
        "mutants": [] if mutants is None else mutants,
        "survivors": [] if survivors is None else survivors,
    }


def _audit(repo_dir, test_cmd, source_files, max_mutants, min_score, timeout_s):
    files = _eligible_files(repo_dir, source_files)
    if not files:
        return _build_result("UNVERIFIED", "no-sources: no eligible .py files in scope",
                             files=[], max_mutants=max_mutants, min_score=min_score)

    mutants = generate_mutants(repo_dir, source_files, max_mutants=max_mutants, seed=0)
    if not mutants:
        return _build_result("UNVERIFIED", "no-sites: no mutation sites in eligible files",
                             files=files, max_mutants=max_mutants, min_score=min_score)

    tmp = tempfile.mkdtemp(prefix="mo-suite-adequacy-")
    try:
        copy_root = os.path.join(tmp, "repo")
        shutil.copytree(
            repo_dir, copy_root, symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                ".ruff_cache", ".tox", ".venv", "venv", "node_modules", ".mini-ork",
            ),
        )
        norm = _normalized_sources(repo_dir, files)
        for rel, src in norm.items():
            _write(copy_root, rel, src)
        env = _child_env(copy_root)

        # Baseline: the suite must be green on the normalised original.
        t0 = time.monotonic()
        base_rc, base_timed_out = _run_cmd(test_cmd, copy_root, env, timeout_s)
        baseline_seconds = time.monotonic() - t0
        if base_timed_out:
            return _build_result("UNVERIFIED", "baseline-timeout: baseline run exceeded timeout",
                                 files=files, max_mutants=max_mutants, min_score=min_score)
        if base_rc != 0:
            return _build_result("UNVERIFIED", f"baseline-red: rc={base_rc}",
                                 files=files, max_mutants=max_mutants, min_score=min_score,
                                 baseline_rc=base_rc)

        mutant_timeout_s = min(timeout_s, max(10.0, 5.0 * baseline_seconds))

        # Canary: if the suite is still green when every in-scope file raises at
        # import, it never loads in-scope code from the copy (editable install
        # shadow), so every mutant would look green — abstain instead.
        for rel in norm:
            _write(copy_root, rel, _CANARY)
        canary_rc, _ = _run_cmd(test_cmd, copy_root, env, timeout_s)
        for rel, src in norm.items():
            _write(copy_root, rel, src)
        canary_detected = canary_rc is not None and canary_rc != 0
        if not canary_detected:
            return _build_result(
                "UNVERIFIED",
                "canary-undetected: suite never loads in-scope code from the copy",
                files=files, max_mutants=max_mutants, min_score=min_score,
                baseline_rc=base_rc,
            )

        mutants_report = []
        survivors_report = []
        killed = survived = invalid = 0
        for m in mutants:
            _write(copy_root, m.file, m.source)
            outcome = "invalid"
            rc_val = None
            try:
                try:
                    compile(m.source, m.file, "exec")
                except (SyntaxError, ValueError):
                    outcome = "invalid"
                else:
                    rc, timed_out = _run_cmd(test_cmd, copy_root, env, mutant_timeout_s)
                    if timed_out or rc is None:
                        outcome = "invalid"
                    elif "pytest" in test_cmd:
                        if rc == 0:
                            outcome = "survived"
                            rc_val = 0
                        elif rc == 1:
                            outcome = "killed"
                            rc_val = 1
                        else:
                            outcome = "invalid"
                            rc_val = rc
                    else:
                        if rc == 0:
                            outcome = "survived"
                            rc_val = 0
                        else:
                            outcome = "killed"
                            rc_val = rc
            finally:
                _write(copy_root, m.file, norm[m.file])

            if outcome == "killed":
                killed += 1
            elif outcome == "survived":
                survived += 1
                survivors_report.append({
                    "id": m.id, "file": m.file, "line": m.line,
                    "operator": m.operator, "original": m.original,
                    "mutated": m.mutated,
                })
            else:
                invalid += 1
            mutants_report.append({
                "id": m.id, "file": m.file, "line": m.line, "col": m.col,
                "operator": m.operator, "original": m.original,
                "mutated": m.mutated, "outcome": outcome, "rc": rc_val,
            })

        valid = killed + survived
        score = round(killed / valid, 3) if valid > 0 else None
        if valid < MIN_VALID:
            verdict = "UNVERIFIED"
            reason = f"too-few-valid: {valid}"
        elif score >= min_score:
            verdict = "ADEQUATE"
            reason = f"score {score} >= {min_score}"
        else:
            verdict = "INADEQUATE"
            reason = f"score {score} < {min_score}"

        return _build_result(
            verdict, reason, files=files, max_mutants=max_mutants, min_score=min_score,
            score=score, killed=killed, survived=survived, invalid=invalid,
            baseline_rc=base_rc, canary_detected=canary_detected,
            mutant_timeout_s=mutant_timeout_s,
            mutants=mutants_report, survivors=survivors_report,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def audit_suite(repo_dir, test_cmd, source_files, *, max_mutants=None,
                min_score=None, timeout_s=None, report_path=None):
    """Score ``test_cmd`` by how many of its own mutants it kills.

    Never raises: any unexpected exception becomes an UNVERIFIED
    ``harness-error``. A ``None`` argument is resolved from :func:`settings`.
    When ``report_path`` is given, the result dict is also written there as
    JSON (``indent=2``, parents created, ``OSError`` swallowed).
    """
    s = settings()
    max_mutants = s["max_mutants"] if max_mutants is None else max_mutants
    min_score = s["min_score"] if min_score is None else min_score
    timeout_s = s["timeout_s"] if timeout_s is None else timeout_s

    try:
        result = _audit(repo_dir, test_cmd, source_files, max_mutants, min_score, timeout_s)
    except Exception as exc:  # noqa: BLE001 — the audit must never raise
        result = _build_result(
            "UNVERIFIED", f"harness-error: {exc!r}",
            files=_eligible_files(repo_dir, source_files),
            max_mutants=max_mutants, min_score=min_score,
        )

    if report_path:
        try:
            p = Path(report_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except OSError:
            pass
    return result
