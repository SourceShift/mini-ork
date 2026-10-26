"""Deterministic step rules — verify an intermediate artifact by lookup, not by a judge.

VPRMs (``2601.17223``): a neural judge on an intermediate step returns a verdict
that depends on how the step was phrased and which way the judge was feeling. A
rule returns the same verdict every time, and on the steps it covers the paper
measures it *more* accurate than the judge it replaces — the rules were not just
cheaper. The deal is honest partial coverage: a rule has nothing to say about a
step nobody wrote a rule for, and says so (``defer``) instead of guessing.

mini-ork took this step once already: the type-check and gate verifiers fire on a
project marker (a ``tsconfig``, a ``[tool.mypy]`` section) rather than on the
global presence of a tool, which stopped a class of phantom failures. This module
extends the pattern to the intermediate artifact — the patch — so a real failure
is caught before any judge sees the diff.

Two rules, both pure lookups:

  ``patch_applies_cleanly``   the patch actually applies to the workspace
  ``named_test_path_exists``  the paths the plan's verifier names exist in it

Neither asks a model anything, and neither charges for a check.

The known cost is maintenance, and the technique is explicit about it: a rule
that checks a path stops being true after a refactor, and then it vetoes good
work while still looking healthy. Two properties bound that. Each rule resolves
its input through the same lookup the production path uses, so a rule cannot
disagree with the thing it is guarding; and a rule with no input *defers* rather
than passing, so a stale rule goes quiet instead of going wrong.

Evaluator signature matches ``gate_registry.GateEvaluator`` and the
``native_gates`` registry:
``(condition, context_json, db_path, mini_ork_root) -> 'pass'|'fail'|'defer'``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from typing import Optional

__all__ = [
    "GATE_NAME",
    "run_rules",
    "gate_verdict",
    "evaluate",
]

#: Native gate name; ``gate_bootstrap`` seeds the ``native:step-rules``
#: sentinel and ``native_gates`` resolves it back to ``evaluate`` here.
GATE_NAME = "step-rules"

#: A patch is a text artifact; anything larger is not one and reading it all
#: would be the expensive part of a check that is supposed to be a lookup.
_MAX_PATCH_BYTES = 2 * 1024 * 1024

#: Path-shaped tokens inside a verifier command. Deliberately anchored to a
#: source-ish extension: a bare word in a command is far more likely to be a
#: flag value than a file the plan promises to have.
_PATH_TOKEN = re.compile(
    r"[\w./-]+\.(?:py|sh|js|mjs|ts|tsx|json|ya?ml|toml|cfg|ini)\b")

#: `--junitxml=reports/out.xml` names where a result will be *written*, not a
#: file that must already exist. Same for a `>` redirect target.
_ASSIGN_FLAG = re.compile(r"-{1,2}[\w-]+=[\"']?[\w./-]+")
_REDIRECT = re.compile(r"\d?>>?\s*[\"']?[\w./-]+")

#: `${NAME}` / `$NAME` in a verifier command. ``_expand_refs`` substitutes the
#: ones it can resolve; the rest survive to be recognised as unreadable.
_VAR_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")

#: A word carrying a variable, placeholder, glob or brace is not a literal
#: path: the rule cannot know which file it names, so it must not veto it.
_UNRESOLVED_WORD = re.compile(r"\S*[$<>*?{}]\S*")

#: A flag whose *operand is a program body*, not a shell word. The inline
#: program is source code for another interpreter: a path fragment inside it
#: (``R + '/proposal.json'``) or a substring test (``'routing.py' in summary``)
#: is not a file this command names. Reading one as shell arguments invented
#: five absent paths on a single rsi-cycle run and vetoed a healthy plan.
#: Only an interpreter's own code flag is skipped — `tar -c` names a file.
_INLINE_CODE = re.compile(r"^(?:-[ce]|--eval|--exec)$")
_INTERPRETER = re.compile(
    r"^(?:python[\d.]*|node|nodejs|perl|ruby|deno|bun|Rscript|pwsh)$")


def _git(workspace: str, args: list[str], *, timeout: int = 30):
    """Run git in ``workspace``; ``None`` when git cannot run at all.

    Routed via ``mini_ork.runtime.contract.run_check`` so a remote placement
    runs the step_rules git invocations on the replica instead of the host's
    ``MO_TARGET_CWD`` (kickoff ``remote-nodes-11`` §2 — ``step_rules`` is
    one of the listed call sites). The local branch stays byte-identical
    to the previous ``subprocess.run(..., stdout=PIPE, stderr=STDOUT)``
    shape so ``rule_patch_applies_cleanly`` and friends keep their
    ``run.stdout``/``run.returncode`` reads.
    """
    try:
        from mini_ork.runtime.contract import run_check
        rc, out = run_check(
            ["git", "-C", workspace, *args],
            cwd=workspace,
            env=None,
            evidence_path="",
            timeout=timeout,
        )
    except Exception:
        return None
    # ``run_check`` returns ``str | bytes``; ``subprocess.run`` returned
    # ``bytes``. Normalize to ``bytes`` for the call sites'
    # ``proc.stdout``/``proc.returncode`` reads.
    if isinstance(out, str):
        out = out.encode("utf-8", "replace")
    return _CompletedProcess(rc=rc, stdout=out or b"")


class _CompletedProcess:
    """``subprocess.CompletedProcess``-shaped object so call sites' ``rc.stdout`` /
    ``rc.returncode`` reads keep working after the ``run_check`` reroute."""

    __slots__ = ("rc", "stdout", "stderr", "returncode")

    def __init__(self, *, rc: int, stdout: bytes,
                 stderr: bytes = b"", returncode: int | None = None) -> None:
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode if returncode is not None else rc


def _is_git_workspace(workspace: str) -> bool:
    if not workspace or not os.path.isdir(workspace):
        return False
    # `git apply` needs a work tree, not just a repo (a bare checkout or a
    # `.git` file pointer both fail it), so ask git rather than stat `.git`.
    run = _git(workspace, ["rev-parse", "--is-inside-work-tree"])
    return bool(run and run.returncode == 0
                and (run.stdout or b"").strip() == b"true")


def _looks_like_a_patch(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    if os.path.getsize(path) > _MAX_PATCH_BYTES:
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192).decode("utf-8", "replace")
    except OSError:
        return False
    return "diff --git " in head or "\n--- a/" in head or head.startswith("--- a/")


# ── rule 1: the patch applies ────────────────────────────────────────────────


def rule_patch_applies_cleanly(artifact_path: str, workspace: str) -> tuple[str, str]:
    """``git apply --check`` the artifact's patch against the workspace.

    This is the rule for the failure mode ``feedback_never_ask_model_for_unified_diff``
    measured: 77% of model-authored unified diffs are rejected by ``git apply``.
    Catching that here costs one ``--check`` (no files are written) and saves a
    whole apply-test-revert cycle downstream.

    A patch that reverse-applies cleanly is reported as passing: that means it is
    already applied to this tree, which is a statement about *when* the check ran,
    not about whether the patch is well-formed. Failing it would make the rule
    punish a re-run of an otherwise healthy step — the exact way a good rule turns
    into a veto of good work.
    """
    if not _looks_like_a_patch(artifact_path):
        return "defer", "artifact is not a patch"
    if not _is_git_workspace(workspace):
        return "defer", "no git work tree to apply against"
    forward = _git(workspace, ["apply", "--check", "--whitespace=nowarn", artifact_path])
    if forward is None:
        return "defer", "git could not be run"
    if forward.returncode == 0:
        return "pass", "git apply --check"
    reverse = _git(workspace, ["apply", "--check", "-R", "--whitespace=nowarn",
                               artifact_path])
    if reverse is not None and reverse.returncode == 0:
        return "pass", "already applied (git apply --check -R)"
    tail = _last_line(forward.stdout)
    return "fail", f"git apply --check failed: {tail or 'rc=' + str(forward.returncode)}"


# ── rule 2: the paths the plan names exist ───────────────────────────────────


def _expand_refs(text: str, env: dict) -> str:
    """Substitute ``${NAME}`` / ``$NAME`` that resolve to a non-empty value.

    A reference the environment cannot resolve is left byte-for-byte intact so
    the unresolved-word scrub can recognise it as unreadable rather than the
    rule treating the literal ``$NAME`` as a file that does not exist.
    """
    def repl(match: re.Match) -> str:
        value = env.get(match.group(1) or match.group(2))
        return value if value else match.group(0)

    return _VAR_REF.sub(repl, text)


def _without_inline_programs(words: list[str]) -> list[str]:
    """Drop the operand of an interpreter's ``-c`` / ``-e``: it is code, not shell.

    Only the single word after the flag goes. Arguments that follow it are still
    shell words the command really passes, so a plan that hands a path to an
    inline program is still checked on that path.
    """
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        out.append(word)
        if (len(out) >= 2 and _INLINE_CODE.match(word)
                and _INTERPRETER.match(os.path.basename(out[-2]))):
            skip = True
    return out


def _shell_visible(command: str) -> str:
    """``command`` with inline program bodies removed; unchanged when unparseable.

    ``shlex`` is what makes the operand boundaries real: it knows that the body
    of ``python3 -c "import json; …"`` is one word even when it holds quotes and
    semicolons. A command the shell itself could not split is left whole — the
    rule has no better reading of it than it had before, so it keeps the old one
    rather than inventing a new opinion on malformed input.
    """
    try:
        words = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command
    return " ".join(_without_inline_programs(words))


def _command_paths(plan: dict, env: dict | None = None) -> list[str]:
    """Path-shaped tokens from the plan's verifier commands and verifier names."""
    commands: list[str] = []
    contract = plan.get("verifier_contract")
    if isinstance(contract, dict) and isinstance(contract.get("checks"), list):
        for check in contract["checks"]:
            if isinstance(check, dict):
                cmd = str(check.get("command") or "").strip()
                if cmd:
                    commands.append(cmd)
    artifact = plan.get("artifact_contract")
    if isinstance(artifact, dict):
        for name in artifact.get("success_verifiers") or []:
            if name:
                commands.append(str(name))

    seen: set[str] = set()
    out: list[str] = []
    for command in commands:
        # Resolve the variables we can, drop inline program bodies (their
        # contents are another interpreter's source, not shell words), then
        # drop where-outputs before looking for where-inputs, then drop the
        # words we cannot read at all.
        expanded = _expand_refs(command, env or {})
        scrubbed = _REDIRECT.sub(" ", _ASSIGN_FLAG.sub(
            " ", _shell_visible(expanded)))
        scrubbed = _UNRESOLVED_WORD.sub(" ", scrubbed)
        for token in _PATH_TOKEN.findall(scrubbed):
            if token not in seen:
                seen.add(token)
                out.append(token)
    return out


def _resolve(token: str, workspace: str, root: str, run_dir: str = "") -> bool:
    if os.path.isabs(token):
        return os.path.exists(token)
    for base in (workspace, root, run_dir):
        if base and os.path.exists(os.path.join(base, token)):
            return True
    return _resolves_via_the_verifier_dispatcher(token, root)


def _resolves_via_the_verifier_dispatcher(token: str, root: str) -> bool:
    """Ask ``verify.py``'s own resolver, so this rule cannot disagree with it.

    A recipe's verifiers do not live at ``<root>/verifiers`` — they live at
    ``<root>/recipes/<recipe>/verifiers``, which only the dispatcher's base list
    knows about. Resolving ``verifiers/typecheck.py`` as a plain path would
    therefore fail on the single most common plan shape in the repo and veto
    healthy runs while looking correct, which is the exact decay the technique
    warns about. Delegating the lookup is what keeps the rule honest: if this
    rule passes, the dispatcher will find it; if this rule fails, the dispatcher
    would have reported ``script_not_found``.
    """
    try:
        from mini_ork.cli import verify
    except Exception:
        return False
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    try:
        return bool(verify._find_verifier_script(token, root, home))
    except Exception:
        return False


def _last_line(blob) -> str:
    try:
        text = (blob or b"").decode("utf-8", "replace")
    except AttributeError:
        text = str(blob or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def rule_named_test_path_exists(plan_path: str, workspace: str,
                                root: str, run_dir: str = "") -> tuple[str, str]:
    """Every path the plan's verifier names must exist where the verifier will run.

    A command naming a test file that is not in the target is a plan that cannot
    succeed, and it is knowable before the command is ever executed. The rule
    fires only on path-shaped tokens, so `pytest -q` and `make test` (no path to
    check) defer rather than passing on nothing.

    Each token resolves either as a plain path or through the verifier
    dispatcher's own lookup, because a recipe's verifiers live under
    ``recipes/<recipe>/verifiers`` rather than at the root. Checking only the
    plain path would fail every recipe plan that names its verifier — a rule
    vetoing good work while looking healthy.
    """
    if not plan_path or not os.path.isfile(plan_path):
        return "defer", "no plan to read a command from"
    try:
        plan = json.load(open(plan_path, encoding="utf-8"))
    except Exception:
        return "defer", "plan is unreadable"
    if not isinstance(plan, dict):
        return "defer", "plan is not an object"
    env = {k: v for k, v in os.environ.items() if v}
    if root:
        env["MINI_ORK_ROOT"] = root
    if run_dir:
        env["MINI_ORK_RUN_DIR"] = run_dir
    if workspace:
        env.setdefault("MO_TARGET_CWD", workspace)
    tokens = _command_paths(plan, env)
    if not tokens:
        return "defer", "the plan's verifier names no path"
    if not workspace and not root and not run_dir:
        return "defer", "no root to resolve the named paths against"
    missing = [t for t in tokens if not _resolve(t, workspace, root, run_dir)]
    if missing:
        return "fail", "named path(s) absent: " + ", ".join(sorted(missing)[:5])
    return "pass", f"{len(tokens)} named path(s) resolved"


# ── aggregate ────────────────────────────────────────────────────────────────

_RULES = (
    # (name, callable(artifact_path, plan_path, workspace, root, run_dir))
    ("patch_applies_cleanly",
     lambda a, _p, w, _r, _d: rule_patch_applies_cleanly(a, w)),
    ("named_test_path_exists",
     lambda _a, p, w, r, d: rule_named_test_path_exists(p, w, r, run_dir=d)),
)


def run_rules(artifact_path: str, plan_path: str = "", workspace: str = "",
              root: str = "", run_dir: str = "") -> dict:
    """Evaluate every rule, recording each verdict and why."""
    rules = []
    for name, fn in _RULES:
        try:
            verdict, detail = fn(artifact_path, plan_path, workspace, root, run_dir)
        except Exception as exc:  # a rule that breaks is an unrun rule
            verdict, detail = "defer", f"rule error: {exc}"
        rules.append({"rule": name, "verdict": verdict, "detail": detail})
    return {"rules": rules}


def gate_verdict(report: Optional[dict]) -> str:
    """Map a rule report onto the gate contract: any fail → fail, none fired → defer.

    A partial firing is a pass, not a defer: at least one rule measured the
    artifact and held, and reporting that as unmeasured would hide a real
    measurement behind an honest-but-unhelpful label. ``defer`` is reserved for
    the state it names — no rule had anything to say.
    """
    if not isinstance(report, dict):
        return "defer"
    rules = report.get("rules")
    if not isinstance(rules, list) or not rules:
        return "defer"
    verdicts = [r.get("verdict") for r in rules if isinstance(r, dict)]
    if "fail" in verdicts:
        return "fail"
    if "pass" not in verdicts:
        return "defer"
    return "pass"


# ── native_gates entry point ─────────────────────────────────────────────────


def evaluate(condition: str, context_json: str, db_path: str,
             mini_ork_root: Optional[str]) -> str:
    """``native_gates`` evaluator: run the rules against the run's context."""
    del condition, db_path
    if os.environ.get("MO_STEP_RULES", "1") == "0":
        return "defer"
    try:
        ctx = json.loads(context_json)
    except Exception:
        return "defer"
    if not isinstance(ctx, dict):
        return "defer"
    root = mini_ork_root or os.environ.get("MINI_ORK_ROOT", "")
    workspace = (ctx.get("workspace") or os.environ.get("MO_TARGET_CWD")
                 or os.environ.get("MINI_ORK_TARGET_REPO") or "")
    return gate_verdict(run_rules(
        str(ctx.get("artifact_path") or ""),
        str(ctx.get("plan_path") or ""),
        workspace,
        root,
        run_dir=str(ctx.get("run_dir") or ""),
    ))
