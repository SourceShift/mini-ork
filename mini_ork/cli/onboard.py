"""``mini-ork onboard`` — the first-run wizard.

One command that takes a machine from "nothing installed" to "a workspace I can
run a recipe in", and offers the two optional satellites on the way:

* **ContextNest** — the local memory substrate (Rust service, HTTP).
* **Zed integration** — wires ``mini-ork acp`` / ``mini-ork mcp-context`` into
  Zed's ``settings.json`` via :mod:`mini_ork.cli.zed_cmd`.

The wizard never spends money and never calls a model. Every step is a local
check or a local build, so onboarding is safe to run on a fresh laptop.

Two tiers, as the project requires:

* **simple** — everything lands in files under ``$MINI_ORK_HOME``; a missing
  prerequisite is reported with the exact command that fixes it.
* **secure** — ``--secure`` refuses to continue when a component would fall
  back to a local file for a secret, instead of silently writing one.

Design notes
------------
``resolve_plan`` is pure: it turns argv + the environment into a dataclass, so
the interesting decisions are testable without a terminal or a network. The
orchestration in :func:`run` takes a :class:`Hooks` object so tests can replace
``which`` / ``run`` / ``prompt`` and assert the *plan* rather than a shell.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

CONTEXTNEST_REPO = "https://github.com/SourceShift/ContextNest.git"
CONTEXTNEST_DIR_ENV = "CONTEXTNEST_INSTALL_DIR"
CONTEXTNEST_REF_ENV = "CONTEXTNEST_REF"

#: Components the wizard can offer. Order is the order they are presented in.
COMPONENTS = ("contextnest", "zed")

_USAGE = """Usage:
  mini-ork onboard [options]

Takes a fresh machine to a runnable mini-ork workspace in four steps:
prerequisites → workspace → provider lanes → optional components.

Options:
  -y, --yes                Accept every default; no prompts (for scripts/CI)
      --workspace <dir>    Project directory to initialize (default: $PWD)
      --with <list>        Extra components to install: contextnest,zed
      --no-contextnest     Skip ContextNest even if it is installed
      --no-zed             Leave Zed's settings.json untouched
      --secure             Fail closed instead of writing local secret files
      --skip-build         Build nothing; report what is missing (for dry runs)
      --dry-run            Print the plan and every command; change nothing
      --strict             Exit 1 if a selected component cannot be installed
      --json               Print the step results as one JSON object

Environment:
  CONTEXTNEST_INSTALL_DIR   where ContextNest is cloned (default: ~/.local/share/contextnest)
  CONTEXTNEST_REPO          git URL to clone (default: SourceShift/ContextNest)
  CONTEXTNEST_REF           git ref to check out (default: main)

Exit codes: 0 done (or done with skipped components), 1 a selected component
failed under --strict, 2 usage error.
"""


# --------------------------------------------------------------------------
# Plan resolution (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """Everything the wizard needs to know before it touches the machine."""

    workspace: Path
    components: tuple[str, ...]
    yes: bool
    dry_run: bool
    strict: bool
    secure: bool
    skip_build: bool
    as_json: bool


@dataclass(frozen=True)
class StepResult:
    """One line of the final report."""

    name: str
    status: str  # "ok" | "skipped" | "failed"
    detail: str


def _split_components(raw: str) -> tuple[str, ...]:
    wanted = []
    for part in raw.replace(" ", ",").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in COMPONENTS:
            raise ValueError(
                f"unknown component {name!r}; choose from {', '.join(COMPONENTS)}"
            )
        if name not in wanted:
            wanted.append(name)
    return tuple(wanted)


def resolve_plan(argv: Sequence[str], *, cwd: str | Path | None = None) -> Plan:
    """Turn argv into a :class:`Plan`. Raises ``ValueError`` on bad input.

    Defaults follow the openclaw shape: the core install is unconditional and
    the optional components default to **on when their toolchain is present**.
    That keeps ``mini-ork onboard -y`` useful in CI without dragging a Rust
    build into a machine that cannot do one.
    """
    args = build_parser().parse_args(list(argv))

    chosen = list(_split_components(args.with_components))
    if args.no_contextnest and "contextnest" in chosen:
        raise ValueError("--with contextnest conflicts with --no-contextnest")
    if args.no_zed and "zed" in chosen:
        raise ValueError("--with zed conflicts with --no-zed")

    components: list[str] = []
    for name in COMPONENTS:
        blocked = args.no_contextnest if name == "contextnest" else args.no_zed
        if blocked:
            continue
        if name in chosen:
            components.append(name)
        elif not args.with_components:
            # Nothing was named explicitly: offer what the machine can host.
            if name == "contextnest" and shutil.which("cargo"):
                components.append(name)
            elif name == "zed" and shutil.which("zed"):
                components.append(name)

    workspace = Path(args.workspace).expanduser() if args.workspace else Path(cwd or os.getcwd())
    return Plan(
        workspace=workspace,
        components=tuple(components),
        yes=args.yes,
        dry_run=args.dry_run,
        strict=args.strict,
        secure=args.secure,
        skip_build=args.skip_build,
        as_json=args.json,
    )


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


@dataclass
class Hooks:
    """Indirection for the pieces tests replace."""

    which: Callable[[str], str | None] = shutil.which
    run: Callable[[list[str], Path | None], int] = field(default=None)  # type: ignore[assignment]
    prompt: Callable[[str, bool], bool] = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.run is None:
            self.run = _run
        if self.prompt is None:
            self.prompt = _prompt_yes_no


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    return subprocess.run(cmd, cwd=cwd, check=False).returncode


def _prompt_yes_no(question: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _contextnest_dir() -> Path:
    override = os.environ.get(CONTEXTNEST_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "contextnest"


def _mini_ork_home(workspace: Path) -> Path:
    configured = os.environ.get("MINI_ORK_HOME")
    return Path(configured).expanduser() if configured else workspace / ".mini-ork"


def _mini_ork_root() -> Path:
    configured = os.environ.get("MINI_ORK_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2]


def check_prereqs(hooks: Hooks) -> StepResult:
    """Report the tools the core runtime needs — never install them."""
    missing = [tool for tool in ("python3", "git") if not hooks.which(tool)]
    python = sys.version_info
    if python < (3, 11):
        return StepResult(
            "prerequisites",
            "failed",
            f"Python {python.major}.{python.minor} is too old; mini-ork needs 3.11+",
        )
    if missing:
        return StepResult(
            "prerequisites",
            "failed",
            f"missing {', '.join(missing)} — install it and re-run",
        )
    notes = ["python 3.11+", "git"]
    for optional in ("sqlite3", "jq"):
        if hooks.which(optional):
            notes.append(optional)
    return StepResult("prerequisites", "ok", ", ".join(notes))


def init_workspace(plan: Plan, hooks: Hooks) -> StepResult:
    """Create ``.mini-ork/`` in the workspace (idempotent)."""
    workspace = plan.workspace
    home = _mini_ork_home(workspace)
    if home.is_dir():
        return StepResult("workspace", "skipped", f"{home} already exists")
    if plan.dry_run:
        return StepResult("workspace", "ok", f"would initialize {home} (dry run)")

    if not (workspace / ".git").exists():
        if not hooks.which("git"):
            return StepResult("workspace", "failed", "git is required to initialize a project")
        if not plan.yes and not hooks.prompt(
            f"{workspace} is not a git repository. Run `git init` there?", True
        ):
            return StepResult("workspace", "failed", "not a git repository; nothing initialized")
        if hooks.run(["git", "init"], workspace) != 0:
            return StepResult("workspace", "failed", "`git init` failed")

    from mini_ork.cli.init import mini_ork_init

    workspace.mkdir(parents=True, exist_ok=True)
    try:
        mini_ork_init(workspace, _mini_ork_root(), mini_ork_home=home)
    except OSError as exc:  # permissions, odd filesystems
        return StepResult("workspace", "failed", f"init failed: {exc}")
    return StepResult("workspace", "ok", f"initialized {home}")


def check_lanes() -> StepResult:
    """Report whether any provider credential is stored — without calling one.

    ``providers status`` needs lane names, and onboarding must not guess which
    recipe the operator will run, so this reads the same secret store instead:
    a non-empty store means *some* lane can be configured from it.
    """
    try:
        from mini_ork.dispatch.secrets import read_secret_exports, secret_store_path

        store = secret_store_path()
        stored = read_secret_exports(store)
    except Exception as exc:  # noqa: BLE001 — onboarding must not crash on this
        return StepResult("provider lanes", "skipped", f"could not inspect credentials: {exc}")
    if stored:
        return StepResult("provider lanes", "ok", f"{len(stored)} credential(s) in {store}")
    return StepResult(
        "provider lanes",
        "skipped",
        "no credentials yet — run `mini-ork providers configure <lane>`",
    )


def install_contextnest(plan: Plan, hooks: Hooks) -> StepResult:
    """Clone, build and configure ContextNest. Builder is optional by design."""
    target = _contextnest_dir()
    repo = os.environ.get("CONTEXTNEST_REPO", CONTEXTNEST_REPO)
    ref = os.environ.get("CONTEXTNEST_REF", "main")

    if plan.skip_build:
        return StepResult("contextnest", "skipped", "--skip-build: not cloned or built")

    if not hooks.which("cargo"):
        return StepResult(
            "contextnest",
            "failed",
            "Rust toolchain missing — install rustup (https://rustup.rs) and re-run, "
            "or pass --no-contextnest",
        )

    if plan.dry_run:
        action = "update" if (target / "Cargo.toml").exists() else "clone"
        return StepResult("contextnest", "ok", f"would {action} {target} and cargo build (dry run)")

    if (target / "Cargo.toml").exists():
        hooks.run(["git", "fetch", "--depth", "1", "origin", ref], target)
        hooks.run(["git", "checkout", ref], target)
        hooks.run(["git", "merge", "--ff-only", f"origin/{ref}"], target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        if hooks.run(["git", "clone", "--depth", "1", "--branch", ref, repo, str(target)], None) != 0:
            return StepResult("contextnest", "failed", f"could not clone {repo}")

    if hooks.run(["cargo", "build", "--release"], target) != 0:
        return StepResult("contextnest", "failed", "cargo build failed; see the output above")

    config_note = _write_contextnest_config(plan)
    return StepResult("contextnest", "ok", f"built in {target}; start with `make cn-serve`{config_note}")


def _write_contextnest_config(plan: Plan) -> str:
    """Seed ``$MINI_ORK_HOME/config/contextnest.yaml`` from the shipped example."""
    home = _mini_ork_home(plan.workspace)
    dest = home / "config" / "contextnest.yaml"
    if dest.exists():
        return ""
    example = _mini_ork_root() / "config" / "contextnest.example.yaml"
    if not example.is_file():
        return ""
    if plan.secure:
        # Fail closed: the secure tier needs a real token source. Writing the
        # simple-tier default here would silently downgrade the operator.
        return " (secure tier: set CN_TOKEN before starting it)"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return f"; config seeded at {dest}"


def setup_zed(plan: Plan, hooks: Hooks) -> StepResult:
    """Wire mini-ork into Zed's settings.json when Zed is installed."""
    if not hooks.which("zed"):
        return StepResult(
            "zed",
            "skipped",
            "Zed not found — install it from https://zed.dev, then run `mini-ork zed setup`",
        )
    if plan.dry_run:
        return StepResult("zed", "ok", "would wire mini-ork into Zed's settings.json (dry run)")
    from mini_ork.cli import zed_cmd

    rc = zed_cmd.main(["setup"], str(_mini_ork_root()))
    if rc != 0:
        return StepResult("zed", "failed", "`mini-ork zed setup` failed; see the output above")
    return StepResult("zed", "ok", "mini-ork wired into Zed (acp + mcp-context)")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run(plan: Plan, hooks: Hooks | None = None) -> list[StepResult]:
    """Execute the plan and return the step results (the caller prints them)."""
    hooks = hooks or Hooks()
    results: list[StepResult] = []

    prereqs = check_prereqs(hooks)
    results.append(prereqs)
    if prereqs.status == "failed":
        return results

    results.append(init_workspace(plan, hooks))
    results.append(check_lanes())

    for name in plan.components:
        if name == "contextnest":
            results.append(install_contextnest(plan, hooks))
        elif name == "zed":
            results.append(setup_zed(plan, hooks))
    return results


_NEXT_STEPS = """Next steps:
  1. mini-ork providers configure <lane>     # wire a model lane (keys stay out of argv)
  2. mini-ork doctor                         # confirm the environment
  3. mini-ork run code-fix ./kickoff.md      # run a verifier-backed recipe
"""


def _render(results: Sequence[StepResult]) -> str:
    lines = ["mini-ork onboard", ""]
    for result in results:
        mark = {"ok": "[ok]     ", "skipped": "[skip]   ", "failed": "[FAILED] "}[result.status]
        lines.append(f"  {mark}{result.name:<16} {result.detail}")
    if all(r.status != "failed" for r in results):
        lines.extend(["", _NEXT_STEPS.rstrip()])
    return "\n".join(lines)


def _results_json(results: Sequence[StepResult]) -> str:
    import json

    return json.dumps(
        {"steps": [{"name": r.name, "status": r.status, "detail": r.detail} for r in results]},
        indent=2,
    )


def build_parser() -> argparse.ArgumentParser:
    """Parser for the IDE's CLI-action surface (see tests/unit/test_ide_pages_actions.py)."""
    parser = argparse.ArgumentParser(prog="mini-ork onboard", add_help=False)
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--with", dest="with_components", default="")
    parser.add_argument("--no-contextnest", action="store_true")
    parser.add_argument("--no-zed", action="store_true")
    parser.add_argument("--secure", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(rest: list[str], root: str = "") -> int:
    """Entry point invoked by ``mini-ork onboard``."""
    del root  # dispatched positionally; the wizard resolves its own paths
    if rest and rest[0] in ("--help", "-h", "help"):
        sys.stdout.write(_USAGE)
        return 0
    try:
        plan = resolve_plan(rest)
    except ValueError as exc:
        sys.stderr.write(f"onboard: {exc}\n{_USAGE}")
        return 2

    hooks = Hooks()
    results = run(plan, hooks)
    if plan.as_json:
        print(_results_json(results))
    else:
        print(_render(results))

    failed = [r for r in results if r.status == "failed"]
    return 1 if (failed and plan.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
