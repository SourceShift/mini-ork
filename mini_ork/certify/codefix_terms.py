"""Certify terms (relations ① + differential ②) run inside the code-fix verifier.

`mini_ork/cli/certify.py` reaches `relations.check` and `differential.check`
through `oracle.judge`, after a PROVEN delta and invariant verdict. The code-fix
test verifier has all four inputs those terms need at once — the candidate tree,
a base worktree, the replay's proven fail-to-pass ids, and the kickoff text — so
this module calls the two veto terms directly, in shadow: the verdict is recorded
in the verifier payload (`verifier_test.json` key `certify`) and in
`certify_terms.json`, and never changes the verifier's `pass` or exit code.

The replay has already proven the delta gate with a real test that fails on the
base and passes on the candidate, so that test is the PoC. Discrimination (needs
a pool of known-correct/known-bad patches) and hidden checks (the PoC is the
implementer's own test) are deliberately left out.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from mini_ork.certify import differential, relations
from mini_ork.certify.context import code_context
from mini_ork.certify.verdict import REFUTED
from mini_ork.context import scoped_environ
from mini_ork.runtime.engine import Crucible, ExecOutcome
from mini_ork.verify.test_env import scrubbed_test_env

MODE_ENV = "MO_CODEFIX_CERTIFY"

_NO_POC_REASON = "no python fail-to-pass test in replay overlap"


def mode(environ=None) -> str:
    """The certifying mode: ``"off"``, ``"gate"`` or ``"shadow"`` (default).

    A typo never gates: only an explicit ``1``/``gate`` enables gate mode and
    only ``0``/``off``/``false``/``no`` disables the terms entirely.
    """
    src = os.environ if environ is None else environ
    raw = str(src.get(MODE_ENV, "") or "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return "off"
    if raw in ("1", "gate"):
        return "gate"
    return "shadow"


def poc_from_replay(
    replay: dict, candidate_dir: str, *, max_chars: int = 8000
) -> tuple[str | None, str | None, str]:
    """The first replay overlap id that names a readable Python file in the tree.

    Returns ``(source, rel_path, "")`` or ``(None, None, reason)``. Never raises:
    a non-dict replay, a malformed id, or an unreadable file all fall through.
    """
    if not isinstance(replay, dict):
        return (None, None, _NO_POC_REASON)
    cand_real = os.path.realpath(candidate_dir)
    ids = list(replay.get("strong_overlap") or []) + list(replay.get("overlap") or [])
    for tid in ids:
        if not isinstance(tid, str):
            continue
        if tid.endswith("::<suite load failure>"):
            continue
        rel = tid.split("::", 1)[0]
        if not rel.endswith(".py"):
            continue
        path = os.path.realpath(os.path.join(candidate_dir, rel))
        if not (path == cand_real or path.startswith(cand_real + os.sep)):
            continue
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        return (text[:max_chars], rel, "")
    return (None, None, _NO_POC_REASON)


class LocalTreeRunner:
    """A duck-typed runner for `relations.check` / `differential.check`.

    Both terms consume only ``.up`` and ``.run_test``. The patch text is never
    applied: the candidate tree already contains it, so a non-blank ``patch``
    selects the candidate dir and a blank one selects the base dir. Probes are
    written to a temp dir — never inside either tree — so pytest's caches and
    bytecode cannot dirty the candidate.
    """

    def __init__(
        self,
        base_dir: str,
        candidate_dir: str,
        *,
        python: str | None = None,
        timeout_s: float = 120.0,
        deadline: float | None = None,
    ) -> None:
        self.base_dir = base_dir
        self.candidate_dir = candidate_dir
        self.python = python or sys.executable
        self.timeout_s = timeout_s
        # time.monotonic() deadline for the whole certify pass; None = unbounded.
        self.deadline = deadline

    @property
    def up(self) -> bool:
        return os.path.isdir(self.base_dir) and os.path.isdir(self.candidate_dir)

    def run_test(self, test_src: str, patch: str = "") -> ExecOutcome:
        tree = self.candidate_dir if patch.strip() else self.base_dir
        return self.run_in(tree, test_src)

    def run_in(self, tree_dir: str, test_src: str) -> ExecOutcome:
        timeout = self.timeout_s
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                # The pass budget is spent: abstain without spawning, so the
                # shadow terms can never push the verifier past its own timeout.
                return ExecOutcome(status="no_run", output="certify budget exhausted",
                                   backend="local-tree")
            timeout = min(timeout, max(1.0, left))
        tmp = tempfile.mkdtemp(prefix="mo-codefix-probe-")
        try:
            probe = os.path.join(tmp, "crucible_probe.py")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write(test_src)

            env = scrubbed_test_env()
            for key in list(env):
                if key.startswith("MO_"):
                    env.pop(key)
            env.pop("PYTEST_ADDOPTS", None)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            prev = env.get("PYTHONPATH")
            env["PYTHONPATH"] = tree_dir + (os.pathsep + prev if prev else "")

            try:
                proc = subprocess.run(
                    [self.python, "-m", "pytest", probe, "-q",
                     "-p", "no:cacheprovider", "--rootdir", tmp],
                    cwd=tree_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                return ExecOutcome(
                    status="error", ran=False, output="timeout",
                    backend="local-tree", exc="Timeout",
                )
            except OSError as exc:
                return ExecOutcome(status="no_run", output=str(exc), backend="local-tree")
            return Crucible._classify(
                proc.stdout + f"\nCRUCIBLE_RC={proc.returncode}\n", backend="local-tree"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def bounded_dispatch(deadline: float | None = None) -> Callable[[str], str]:
    """A lane dispatch bounded for a verifier probe; returns ``""`` on any error.

    With a ``time.monotonic()`` ``deadline`` it returns ``""`` without calling
    a lane once the pass budget is spent.
    """

    def fn(prompt: str) -> str:
        if deadline is not None and time.monotonic() >= deadline:
            return ""
        try:
            # Imported lazily, and looked up as a module attribute at call time,
            # so a test (or an embedder) can replace `llm.default_dispatch`.
            from mini_ork.certify import llm

            timeout = os.environ.get("MO_CODEFIX_CERTIFY_LANE_TIMEOUT_S", "90")
            with scoped_environ({
                "MO_CERTIFY_LANE_RETRIES": "0",
                "MO_CERTIFY_LANE_TIMEOUT_S": timeout,
            }):
                return llm.default_dispatch(prompt)
        except Exception:
            return ""

    return fn


def _slim_relations(result):
    """A copy of a relations record with per-record `src`/`detail` dropped."""
    if not isinstance(result, dict):
        return result
    slim = {k: v for k, v in result.items() if k != "src"}
    records = result.get("records")
    if isinstance(records, list):
        slim["records"] = [
            ({k: v for k, v in rec.items() if k not in ("src", "detail")}
             if isinstance(rec, dict) else rec)
            for rec in records
        ]
    return slim


def _slim_differential(result):
    """A copy of a differential record without the top-level `src`."""
    if not isinstance(result, dict):
        return result
    return {k: v for k, v in result.items() if k != "src"}


def run_terms(*, issue: str, patch: str, poc: str, runner, dispatch, context=None) -> dict:
    """Run both veto terms, slimming their records; a raising term becomes an error."""
    slots: dict = {}
    for name, term in (("relations", relations), ("differential", differential)):
        if not term.enabled():
            slots[name] = None
            continue
        try:
            if name == "relations":
                result = term.check(
                    poc, issue, patch, runner=runner, dispatch=dispatch,
                    context=context, k=term.k_from_env(),
                )
                slots[name] = _slim_relations(result)
            else:
                result = term.check(
                    poc, issue, patch, runner=runner, dispatch=dispatch,
                    context=context, n=term.n_from_env(),
                )
                slots[name] = _slim_differential(result)
        except Exception as exc:
            slots[name] = {"error": f"{type(exc).__name__}: {exc}"}

    vetoed_by = [
        name for name in ("relations", "differential")
        if isinstance(slots.get(name), dict) and slots[name].get("verdict") == REFUTED
    ]
    verdict = REFUTED if vetoed_by else None
    reason = slots[vetoed_by[0]].get("reason", "") if vetoed_by else "no veto"
    return {
        "relations": slots["relations"],
        "differential": slots["differential"],
        "vetoed_by": vetoed_by,
        "verdict": verdict,
        "reason": reason,
    }


def _record(mode_name: str, status: str, reason: str, poc_file) -> dict:
    return {
        "mode": mode_name,
        "status": status,
        "reason": reason,
        "poc_file": poc_file,
        "relations": None,
        "differential": None,
        "verdict": None,
        "vetoed_by": [],
        "would_block": False,
    }


def _candidate_patch(candidate_dir: str, base_ref: str) -> str:
    """The candidate's change set vs ``base_ref``, untracked files included.

    ``git diff <ref>`` covers committed and unstaged tracked changes but omits
    untracked files, so a fix delivered as new files would look like an empty
    (or, in a mixed change, partial) patch. Each untracked file is appended as
    a ``/dev/null`` diff, whose ``+++ b/<path>`` header
    `certify.context.changed_py_files` parses like any other added file.

    Returns ``""`` when the diff cannot be taken (not a repo, bad ref).
    """
    proc = subprocess.run(
        ["git", "-C", candidate_dir, "-c", "core.quotepath=false",
         "diff", "--no-color", base_ref or "HEAD"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ""
    parts = [proc.stdout]
    listed = subprocess.run(
        ["git", "-C", candidate_dir, "ls-files", "--others",
         "--exclude-standard", "-z"],
        capture_output=True, text=True,
    )
    if listed.returncode == 0:
        for rel in listed.stdout.split("\0"):
            if not rel:
                continue
            # `--no-index` exits 1 when the files differ (the expected case)
            # and 0 when they do not; either is a usable diff.
            extra = subprocess.run(
                ["git", "-C", candidate_dir, "-c", "core.quotepath=false",
                 "diff", "--no-color", "--no-index", "--", os.devnull, rel],
                capture_output=True, text=True,
            )
            if extra.returncode in (0, 1) and extra.stdout:
                parts.append(extra.stdout)
    return "".join(parts)


def evaluate(
    *,
    issue: str,
    poc: str,
    poc_file: str | None,
    base_dir: str,
    candidate_dir: str,
    base_ref: str = "",
    mode: str = "shadow",
    dispatch=None,
    python: str | None = None,
    timeout_s: float = 120.0,
    budget_s: float | None = None,
) -> dict:
    """Run the veto terms on the candidate's change set. Never raises.

    The patch is ``git diff <base>`` plus every untracked file, so a fix that
    adds new files runs the terms instead of abstaining on an empty diff.

    Returns exactly nine keys: `mode`, `status`, `reason`, `poc_file`,
    `relations`, `differential`, `verdict`, `vetoed_by`, `would_block`.
    """
    try:
        patch = _candidate_patch(candidate_dir, base_ref or "HEAD")
        if not patch.strip():
            return _record(mode, "skipped", "empty candidate diff", poc_file)

        deadline = (time.monotonic() + float(budget_s)) if budget_s else None
        runner = LocalTreeRunner(
            base_dir, candidate_dir, python=python, timeout_s=timeout_s,
            deadline=deadline,
        )
        if not runner.up:
            return _record(mode, "skipped", "runner unavailable", poc_file)

        try:
            context = code_context(Path(candidate_dir), base_ref or "HEAD", patch)
        except Exception:
            context = None

        terms = run_terms(
            issue=issue, patch=patch, poc=poc, runner=runner,
            dispatch=dispatch or bounded_dispatch(deadline), context=context,
        )
        return {
            "mode": mode,
            "status": "ran",
            "reason": terms["reason"],
            "poc_file": poc_file,
            "relations": terms["relations"],
            "differential": terms["differential"],
            "verdict": terms["verdict"],
            "vetoed_by": terms["vetoed_by"],
            "would_block": terms["verdict"] == REFUTED,
        }
    except Exception as exc:
        return _record(mode, "error", f"{type(exc).__name__}: {exc}", poc_file)


__all__ = [
    "MODE_ENV",
    "LocalTreeRunner",
    "bounded_dispatch",
    "evaluate",
    "mode",
    "poc_from_replay",
    "run_terms",
]
