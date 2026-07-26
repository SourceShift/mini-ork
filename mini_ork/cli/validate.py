"""Native Python port of ``bin/mini-ork-validate`` — pre-run static checks
with concrete ``Fix:`` hints.

Mirrors wshobson/agents' ``make validate`` discipline: every finding ships a
remediation command. Exit 0 if no errors; exit 1 if errors found.

This is a parity port: stdout/stderr text, finding order, and exit codes
match the bash source.

    main(argv=None) -> int

Path resolution replaces ``lib/paths.sh`` sourcing with the conventions used
by the other native CLI modules:

    root = $MINI_ORK_ROOT or <package>/../.. (engine root)
    home = $MINI_ORK_HOME or ./.mini-ork

Exit codes (mirror bash exactly):

    0  OK, or only warnings in non-strict mode
    1  errors found, or warnings found with --strict
    2  usage error (unknown flag / unexpected positional)

Known divergence: bash's ``--recipe`` with no following argument aborts
under ``set -u`` (``$2: unbound variable``, exit 1). The port treats a
missing ``--recipe`` value as a usage error (usage to stderr, exit 2).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Help text — verbatim copy of bash's `cat <<'EOF' … EOF` block in _usage().
# ─────────────────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "Usage: mini-ork validate [kickoff.md] [--recipe <name>]\n"
    "\n"
    "Pre-run static checks. Every finding includes a concrete Fix: hint.\n"
    "\n"
    "Options:\n"
    "  --recipe <name>   Validate a specific recipe instead of inferring from kickoff.\n"
    "  --strict          Treat warnings as errors.\n"
    "  --help            Show this help.\n"
)

# bash: grep -qiE '^#{1,6}\s*(Goal|Done[- ]When|Definition of Done|Acceptance)'
_HEADING_RE = re.compile(
    r"^#{1,6}\s*(Goal|Done[- ]When|Definition of Done|Acceptance)", re.IGNORECASE)

_REQUIRED_MANIFESTS = ("task_class.yaml", "workflow.yaml", "artifact_contract.yaml")


# ─────────────────────────────────────────────────────────────────────────────
# Findings collector — mirrors bash's _error/_warn + ERRORS/WARNINGS counters.
# ─────────────────────────────────────────────────────────────────────────────
class Findings:
    def __init__(self) -> None:
        self.errors = 0
        self.warnings = 0

    def _emit(self, tag: str, msg: str, fix: str) -> None:
        sys.stderr.write(f"{tag} {msg}\n")
        sys.stderr.write(f"          Fix: {fix}\n")

    def error(self, msg: str, fix: str) -> None:
        self._emit("[error]  ", msg, fix)
        self.errors += 1

    def warn(self, msg: str, fix: str) -> None:
        self._emit("[warning]", msg, fix)
        self.warnings += 1


# ─────────────────────────────────────────────────────────────────────────────
# Recipe resolution — bash: classify the kickoff (dry-run) and map
# task_class=… through `tr '_' '-'` when --recipe was not given.
# ─────────────────────────────────────────────────────────────────────────────
def _infer_recipe(kickoff: str, root: str) -> str:
    if not os.path.isfile(kickoff):
        return ""
    env = dict(os.environ)
    env["MINI_ORK_DRY_RUN"] = "1"
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"]
                                if env.get("PYTHONPATH") else "")
    result = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.classify", kickoff],
        capture_output=True,  # bash: 2>/dev/null, stdout piped to grep
        text=True,
        env=env,
        check=False,
    )
    # bash: grep -E '^task_class=' | head -1 | cut -d= -f2 | tr '_' '-'
    for line in result.stdout.splitlines():
        if line.startswith("task_class="):
            return line.split("=")[1].replace("_", "-")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Check 1 — kickoff checks (recognizable Goal/Done-When heading + size cap).
# ─────────────────────────────────────────────────────────────────────────────
def _check_kickoff(kickoff: str, findings: Findings) -> None:
    if not kickoff or not os.path.isfile(kickoff):
        return
    with open(kickoff, errors="replace") as f:
        text = f.read()
    if not any(_HEADING_RE.search(line) for line in text.splitlines()):
        findings.warn(
            f"kickoff {kickoff} has no recognizable Goal/Done-When heading",
            f"add a '## Goal' and '## Done When' section to {kickoff}",
        )
    size_bytes = os.path.getsize(kickoff)
    max_bytes = int(os.environ.get("MO_MAX_KICKOFF_BYTES", "1048576"))
    if size_bytes > max_bytes:
        findings.error(
            f"kickoff {kickoff} is {size_bytes} bytes (cap: {max_bytes})",
            "split into single-deliverable kickoffs; move detail to kickoffs/references/",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 2 — recipe manifest schema checks + output-path collision guard.
# ─────────────────────────────────────────────────────────────────────────────
def _valid_yaml(path: str) -> bool:
    import yaml
    try:
        with open(path) as f:
            yaml.safe_load(f)
        return True
    except Exception:
        return False


def _contract_outputs(ac: str) -> list[str]:
    """Output paths declared in an artifact_contract.yaml (bash heredoc port)."""
    import yaml
    try:
        with open(ac) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return []
    out = []
    for o in data.get("outputs") or []:
        path = o.get("path") if isinstance(o, dict) else o
        if isinstance(path, str):
            out.append(path)
    return out


def _output_collision_count(root: str, out: str) -> int:
    """bash: grep -lRxF "outputs:" recipes/ | xargs grep -lF "$out" | wc -l —
    count files under recipes/ that both declare an exact ``outputs:`` line
    and mention the output path (the recipe's own file included)."""
    count = 0
    recipes_dir = os.path.join(root, "recipes")
    for dirpath, _dirs, files in os.walk(recipes_dir):
        for fn in files:
            path = os.path.join(dirpath, fn)
            try:
                with open(path, errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if "outputs:" in text.splitlines() and out in text:
                count += 1
    return count


def _check_recipe(root: str, recipe: str, findings: Findings) -> None:
    recipe_dir = os.path.join(root, "recipes", recipe)
    if not os.path.isdir(recipe_dir):
        findings.error(
            f"recipe not found: {recipe}",
            "check recipes/ or run 'mini-ork classify $KICKOFF'",
        )
        return

    for req in _REQUIRED_MANIFESTS:
        if not os.path.isfile(os.path.join(recipe_dir, req)):
            findings.error(
                f"recipe {recipe} missing {req}",
                f"create {recipe_dir}/{req} from the recipe template",
            )

    # workflow.yaml: basic structural sanity
    wf = os.path.join(recipe_dir, "workflow.yaml")
    if os.path.isfile(wf) and not _valid_yaml(wf):
        findings.error(
            f"recipe {recipe} workflow.yaml is not valid YAML",
            f"fix YAML syntax in {wf}",
        )

    # artifact_contract.yaml: output-path collision guard
    ac = os.path.join(recipe_dir, "artifact_contract.yaml")
    if os.path.isfile(ac):
        for out in _contract_outputs(ac):
            if not out:
                continue
            # Run-dir internal paths are intentionally shared.
            if out.startswith("${MINI_ORK_RUN_DIR}"):
                continue
            # Count how many other recipes target the same path.
            collisions = _output_collision_count(root, out)
            if collisions > 1:
                findings.warn(
                    f"recipe {recipe} output path '{out}' is also targeted by "
                    f"{collisions - 1} other recipe(s)",
                    f"use a recipe-specific output path in {ac}",
                )


# ─────────────────────────────────────────────────────────────────────────────
# Check 3 — active lane profile: first of $MINI_ORK_HOME/config/agents.yaml,
# $MINI_ORK_ROOT/config/agents.yaml (config_resolve precedence).
# ─────────────────────────────────────────────────────────────────────────────
def _check_agents_yaml(root: str, home: str, findings: Findings) -> None:
    agents_yaml = ""
    for candidate in (os.path.join(home, "config", "agents.yaml"),
                      os.path.join(root, "config", "agents.yaml")):
        if os.path.isfile(candidate):
            agents_yaml = candidate
            break
    if agents_yaml:
        if not _valid_yaml(agents_yaml):
            findings.error(
                f"agents.yaml is not valid YAML: {agents_yaml}",
                "fix YAML syntax",
            )
    else:
        findings.warn(
            "no agents.yaml found in MINI_ORK_HOME/config or ENGINE_ROOT/config",
            "run 'mini-ork init' to seed default config",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 4 — secrets presence for executable lanes ($MINI_ORK_HOME/config/
# providers.yaml). A broken providers.yaml yields no warnings (bash heredoc
# stderr is discarded).
# ─────────────────────────────────────────────────────────────────────────────
def _check_provider_secrets(home: str, findings: Findings) -> None:
    import yaml
    providers_yaml = os.path.join(home, "config", "providers.yaml")
    if not os.path.isfile(providers_yaml):
        return
    try:
        with open(providers_yaml) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return
    for name, cfg in (data.get("providers") or {}).items():
        env_var = cfg.get("api_key_env") or cfg.get("env_key")
        if env_var and not os.environ.get(env_var):
            findings.warn(
                f"provider secret missing: {name} -> {env_var}",
                f"set it in {home}/config/secrets.local.sh",
            )


# ─────────────────────────────────────────────────────────────────────────────
# CLI dispatcher — mirrors bash's arg-parsing and exit-code flow exactly.
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    kickoff = ""
    recipe = ""
    strict = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            sys.stdout.write(HELP_TEXT)
            return 0
        if arg == "--strict":
            strict = True
            i += 1
        elif arg == "--recipe":
            if i + 1 >= len(argv):
                # bash aborts under set -u (exit 1); the port reports usage.
                sys.stderr.write(HELP_TEXT)
                return 2
            recipe = argv[i + 1]
            i += 2
        elif arg.startswith("-"):
            sys.stderr.write(f"Unknown flag: {arg}\n")
            sys.stderr.write(HELP_TEXT)
            return 2
        else:
            if not kickoff:
                kickoff = arg
                i += 1
            else:
                sys.stderr.write(f"Unexpected argument: {arg}\n")
                sys.stderr.write(HELP_TEXT)
                return 2

    root = (os.environ.get("MINI_ORK_ROOT")
            or str(Path(__file__).resolve().parents[2]))
    home = (os.environ.get("MINI_ORK_HOME")
            or os.path.join(os.getcwd(), ".mini-ork"))

    # ── recipe resolution ──
    if kickoff and not recipe:
        recipe = _infer_recipe(kickoff, root)

    findings = Findings()
    _check_kickoff(kickoff, findings)
    if recipe:
        _check_recipe(root, recipe, findings)
    _check_agents_yaml(root, home, findings)
    _check_provider_secrets(home, findings)

    # ── summary (order mirrors bash) ──
    e, w = findings.errors, findings.warnings
    if e > 0:
        sys.stderr.write(f"validate: {e} error(s), {w} warning(s)\n")
        return 1
    if strict and w > 0:
        sys.stderr.write(f"validate: {e} error(s), {w} warning(s) (strict mode)\n")
        return 1
    if w > 0:
        sys.stderr.write(f"validate: {e} error(s), {w} warning(s)\n")
        return 0
    sys.stdout.write("validate: OK\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
