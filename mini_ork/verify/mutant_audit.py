"""Scores each code-fix gate by how well it kills security mutants versus benign controls.

A mutant is admitted only when an executable witness passes on the base tree and fails on the mutated tree, so a patch that does not trigger a real failure contributes nothing.

The instrument is diagnostic: a low kill rate is a finding, never a build failure, and it exits 0 whenever a report was produced.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

KIND_SECURITY = "security"
KIND_BENIGN = "benign"
KINDS = (KIND_SECURITY, KIND_BENIGN)
DEFAULT_TIMEOUT = 300
Z = 1.96
KILLED = "killed"
SURVIVED = "survived"
GATE_ERROR = "gate_error"
INVALID = "invalid"

_TAIL_CHARS = 400
_ABSTAIN_DETAIL = "verifier abstained — an abstention is not a kill"
_NO_ENVELOPE_DETAIL = "verifier emitted no JSON envelope"
_ABSTAIN_STATUSES = ("unverified", "abstain")
_ABSTAIN_VERDICTS = ("abstain", "vacuous")
_DEFAULT_CONTRACT = Path("recipes") / "code-fix" / "artifact_contract.yaml"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


class Mutant(NamedTuple):
    name: str
    kind: str
    patch: Path
    witness: Path


class GateOutcome(NamedTuple):
    gate: str
    mutant: str
    kind: str
    status: str
    detail: str


class GateScore(NamedTuple):
    gate: str
    security_kills: int
    security_total: int
    benign_kills: int
    benign_total: int
    tpr: float
    fpr: float
    separation: float
    demonstrated: bool
    promotion: str


def wilson_interval(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval for ``k/n``; ``(0.0, 0.0)`` when ``n == 0``."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def load_mutants(directory: str) -> tuple[list[Mutant], list[str]]:
    """Load one mutant per immediate subdirectory, sorted by name."""
    root = Path(directory)
    if not root.is_dir():
        return [], [f"mutants directory not found: {directory}"]
    mutants: list[Mutant] = []
    problems: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        patch = entry / "mutant.diff"
        witness = entry / "witness.py"
        if not patch.is_file():
            problems.append(f"{entry.name}: missing mutant.diff")
            continue
        if not witness.is_file():
            problems.append(f"{entry.name}: missing witness.py")
            continue
        kind = KIND_SECURITY
        kind_file = entry / "kind"
        if kind_file.is_file():
            kind = kind_file.read_text().strip()
        if kind not in KINDS:
            problems.append(f"{entry.name}: unknown kind {kind!r}")
            continue
        # Absolute paths: ``git -C <tree> apply`` resolves a relative patch
        # against the throwaway tree, where the mutant directory does not exist.
        mutants.append(
            Mutant(entry.name, kind, patch.resolve(), witness.resolve())
        )
    return mutants, problems


def touched_paths(patch: Path) -> list[str]:
    """The ``b/`` side of every ``+++ b/<path>`` line, deduped and sorted."""
    try:
        text = Path(patch).read_text()
    except OSError:
        return []
    paths: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("+++ "):
            continue
        target = line[4:].split("\t", 1)[0].strip()
        if target == "/dev/null":
            continue
        if target.startswith("b/"):
            paths.add(target[2:])
    return sorted(paths)


def _child_env() -> dict[str, str]:
    """The scrubbed test env minus every mini-ork knob."""
    from mini_ork.verify.test_env import scrubbed_test_env

    env = scrubbed_test_env()
    return {
        key: value
        for key, value in env.items()
        if not key.startswith(("MO_", "MINI_ORK_"))
    }


def _capture(
    argv: list[str],
    cwd: str,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, str, str | None]:
    """Run ``argv``, returning ``(returncode, stdout, stderr, error)``.

    Known limitation: a timeout kills the direct child only. A gate that spawns
    its own subprocesses can leave them running until they exit on their own.
    """
    child_env = _child_env() if env is None else env
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "", "", f"timed out after {timeout}s"
    except OSError as exc:
        return None, "", "", f"could not run {argv[0]}: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or "", None


def _run(argv: list[str], cwd: str, timeout: int) -> tuple[int | None, str]:
    """Run ``argv``; return the return code and the last 400 chars of output."""
    rc, stdout, stderr, error = _capture(argv, cwd, timeout)
    if error is not None:
        return None, error
    return rc, (stdout + stderr).strip()[-_TAIL_CHARS:]


def _fresh_pycache_prefix(tree: str) -> str:
    """A throwaway bytecode-cache prefix for one child run.

    A reused ``.pyc`` can mask a mutation that keeps the file's size and
    modification second unchanged (``a + b`` -> ``a - b``): the witness then
    passes against bytecode compiled from the base tree and the mutant is
    wrongly rejected as non-triggering. Redirecting the cache per run forces
    a fresh compile from the source actually on disk.
    """
    return tempfile.mkdtemp(dir=str(Path(tree).parent))


def run_witness(tree: str, witness: Path, timeout: int) -> tuple[int | None, str]:
    """Run a witness module inside the mutant tree."""
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:asyncio", str(witness)]
    env = _child_env()
    env["PYTHONPYCACHEPREFIX"] = _fresh_pycache_prefix(tree)
    rc, stdout, stderr, error = _capture(argv, tree, timeout, env)
    if error is not None:
        return None, error
    return rc, (stdout + stderr).strip()[-_TAIL_CHARS:]


def stage(repo: str, base: str) -> tuple[str, str]:
    """Create a detached throwaway worktree; return ``(tree, parent)``."""
    parent = tempfile.mkdtemp(prefix="mutant-audit-")
    tree = os.path.join(parent, "tree")
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "--detach", tree, base],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        shutil.rmtree(parent, ignore_errors=True)
        raise RuntimeError(f"could not run git for {base}: {exc}") from exc
    if proc.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git worktree add for {base} failed: {detail}")
    return tree, parent


def reset_tree(tree: str) -> None:
    """Discard the throwaway tree's dirt; failures are ignored."""
    subprocess.run(
        ["git", "-C", tree, "checkout", "--", "."],
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", tree, "clean", "-fdq"],
        capture_output=True,
        text=True,
    )


def validate(tree: str, mutant: Mutant, timeout: int) -> str:
    """Return ``""`` when the mutant is admissible, else the reason it is not."""
    safe = _SAFE_NAME_RE.sub("_", mutant.name)
    witness_copy = Path(tree) / f"test_mutant_{safe}.py"
    shutil.copyfile(mutant.witness, witness_copy)

    rc, tail = run_witness(tree, witness_copy, timeout)
    if rc != 0:
        return f"witness does not pass on the base tree: {tail}"

    apply_proc = subprocess.run(
        ["git", "-C", tree, "apply", "--whitespace=nowarn", str(mutant.patch)],
        capture_output=True,
        text=True,
    )
    if apply_proc.returncode != 0:
        return f"patch does not apply: {apply_proc.stderr.strip()}"

    rc, tail = run_witness(tree, witness_copy, timeout)
    # The witness must not survive into the gate runs. A gate that collects the
    # whole suite would otherwise kill the mutant by running the witness, and
    # that kill would measure the mutant — which we already know fails — rather
    # than the gate.
    witness_copy.unlink(missing_ok=True)
    if rc != 1 or "failed" not in tail:
        return f"witness does not trigger on the mutant: {tail}"
    return ""


def _last_json_dict(stdout: str) -> dict | None:
    """The last line of ``stdout`` that parses as a JSON object."""
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line.strip())
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def run_gate(
    gate: str, tree: str, touched: list[str], timeout: int
) -> tuple[str, str]:
    """Run one gate inside the mutant tree and classify its result."""
    run_dir = tempfile.mkdtemp(dir=str(Path(tree).parent))
    env = _child_env()
    env["MO_TARGET_CWD"] = tree
    env["MINI_ORK_TOUCHED_FILES"] = "\n".join(touched)
    env["MINI_ORK_RUN_DIR"] = run_dir
    env["PYTHONPYCACHEPREFIX"] = _fresh_pycache_prefix(tree)

    rc, stdout, stderr, error = _capture([sys.executable, gate], tree, timeout, env)
    tail = error if error is not None else (stdout + stderr).strip()[-_TAIL_CHARS:]

    if rc not in (0, 1):
        return GATE_ERROR, tail
    envelope = _last_json_dict(stdout)
    if envelope is None:
        # Exit 1 with no envelope is how an unhandled exception leaves a
        # verifier. Counting it as a kill would let a crashing gate score a
        # perfect kill rate without ever examining the mutant.
        return GATE_ERROR, _NO_ENVELOPE_DETAIL
    if envelope.get("status") in _ABSTAIN_STATUSES or envelope.get(
        "verdict"
    ) in _ABSTAIN_VERDICTS:
        return GATE_ERROR, _ABSTAIN_DETAIL
    if rc == 1 and envelope.get("pass") is True:
        return GATE_ERROR, "verifier exited 1 while declaring pass — contradiction"
    return (KILLED if rc == 1 else SURVIVED), tail


def score(outcomes: list[GateOutcome]) -> list[GateScore]:
    """Per gate name, sorted: kill rates, separation, and promotion.

    An empty outcome list has no gate to score and returns ``[]`` — the same
    shape as every other case, so callers can iterate without a special case.
    """
    if not outcomes:
        return []
    by_gate: dict[str, list[GateOutcome]] = {}
    for outcome in outcomes:
        by_gate.setdefault(outcome.gate, []).append(outcome)

    scores: list[GateScore] = []
    for gate in sorted(by_gate):
        security_kills = security_total = 0
        benign_kills = benign_total = 0
        for outcome in by_gate[gate]:
            if outcome.status not in (KILLED, SURVIVED):
                continue
            if outcome.kind == KIND_SECURITY:
                security_total += 1
                if outcome.status == KILLED:
                    security_kills += 1
            elif outcome.kind == KIND_BENIGN:
                benign_total += 1
                if outcome.status == KILLED:
                    benign_kills += 1
        tpr = security_kills / security_total if security_total else 0.0
        fpr = benign_kills / benign_total if benign_total else 0.0
        separation = tpr - fpr
        demonstrated = (
            security_total >= 1
            and benign_total >= 1
            and wilson_interval(security_kills, security_total)[0]
            > wilson_interval(benign_kills, benign_total)[1]
        )
        scores.append(
            GateScore(
                gate=gate,
                security_kills=security_kills,
                security_total=security_total,
                benign_kills=benign_kills,
                benign_total=benign_total,
                tpr=tpr,
                fpr=fpr,
                separation=separation,
                demonstrated=demonstrated,
                promotion="eligible" if demonstrated else "advisory",
            )
        )
    return scores


def _finalize(
    gates: list[str],
    mutants: list[Mutant],
    outcomes: list[GateOutcome],
    problems: list[str],
) -> dict:
    """Assemble the report, scoring requested gates that produced no outcomes."""
    scores = score(outcomes)
    scored = {item.gate for item in scores}
    for gate in gates:
        if gate in scored:
            continue
        scored.add(gate)
        scores.append(
            GateScore(
                gate=gate,
                security_kills=0,
                security_total=0,
                benign_kills=0,
                benign_total=0,
                tpr=0.0,
                fpr=0.0,
                separation=0.0,
                demonstrated=False,
                promotion="advisory",
            )
        )
    scores.sort(key=lambda item: item.gate)
    return {
        "gates": [item._asdict() for item in scores],
        "mutants": [
            {
                "name": mutant.name,
                "kind": mutant.kind,
                "patch": str(mutant.patch),
                "witness": str(mutant.witness),
            }
            for mutant in mutants
        ],
        "outcomes": [outcome._asdict() for outcome in outcomes],
        "problems": problems,
        "demonstrated_gates": sorted(
            item.gate for item in scores if item.demonstrated
        ),
    }


def audit(
    repo: str,
    base: str,
    mutants_dir: str,
    gates: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Run every gate against every admissible mutant in a throwaway tree."""
    mutants, problems = load_mutants(mutants_dir)
    # A relative gate path would otherwise resolve against the throwaway tree,
    # running whatever copy happens to sit at that path in the base revision.
    gates = [str(Path(gate).resolve()) for gate in gates]

    try:
        tree, parent = stage(repo, base)
    except RuntimeError as exc:
        return _finalize(
            gates, mutants, [], problems + [f"could not stage {base}: {exc}"]
        )

    outcomes: list[GateOutcome] = []
    try:
        for mutant in mutants:
            reset_tree(tree)
            reason = validate(tree, mutant, timeout)
            if reason:
                problems.append(f"{mutant.name}: {reason}")
                for gate in gates:
                    outcomes.append(
                        GateOutcome(
                            gate, mutant.name, mutant.kind, INVALID, reason
                        )
                    )
                continue
            touched = touched_paths(mutant.patch)
            for gate in gates:
                status, detail = run_gate(gate, tree, touched, timeout)
                outcomes.append(
                    GateOutcome(gate, mutant.name, mutant.kind, status, detail)
                )
            reset_tree(tree)
    finally:
        subprocess.run(
            ["git", "-C", repo, "worktree", "remove", "--force", tree],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(parent, ignore_errors=True)

    return _finalize(gates, mutants, outcomes, problems)


def _default_gates(repo: str) -> tuple[list[str], list[str]]:
    """Resolve the default gate list from the code-fix artifact contract."""
    contract = Path(repo) / _DEFAULT_CONTRACT
    try:
        text = contract.read_text()
    except OSError:
        return [], [f"could not read default gates from {contract}"]
    gates: list[str] = []
    for line in text.splitlines():
        if not line.startswith("  - verifiers/"):
            continue
        relative = line[len("  - ") :].strip()
        gates.append(str(Path(repo) / "recipes" / "code-fix" / relative))
    if not gates:
        return [], [f"no default gates found in {contract}"]
    return gates, []


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-ork-mutant-audit",
        description=(
            "Score code-fix gates by security-kill TPR versus benign-kill FPR."
        ),
    )
    parser.add_argument("--repo", default=".", help="target repository root")
    parser.add_argument("--base", required=True, help="base revision to audit")
    parser.add_argument(
        "--mutants", required=True, help="directory of mutant subdirectories"
    )
    parser.add_argument(
        "--gate",
        action="append",
        default=None,
        help="verifier script path (repeatable)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="per-run timeout in seconds",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the report as one JSON object"
    )
    parser.add_argument(
        "--require-separation",
        action="store_true",
        help="exit 1 when no gate demonstrates separation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = _build_parser().parse_args(argv)

    gates = list(args.gate or [])
    problems: list[str] = []
    if not gates:
        gates, problems = _default_gates(args.repo)

    try:
        report = audit(
            args.repo, args.base, args.mutants, gates, timeout=args.timeout
        )
    except (OSError, ValueError) as exc:
        print(f"mutant-audit: {exc}", file=sys.stderr)
        return 2

    report["problems"] = problems + report["problems"]

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for problem in report["problems"]:
            print(f"problem: {problem}")
        for item in report["gates"]:
            print(
                f"{item['gate']}: tpr={item['security_kills']}/"
                f"{item['security_total']} fpr={item['benign_kills']}/"
                f"{item['benign_total']} separation={item['separation']:.3f} "
                f"{item['promotion']}"
            )
        print(
            f"mutant-audit: {len(report['demonstrated_gates'])} of "
            f"{len(report['gates'])} gate(s) demonstrated"
        )

    if args.require_separation and not report["demonstrated_gates"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
