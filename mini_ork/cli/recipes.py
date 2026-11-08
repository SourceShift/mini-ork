"""``mini-ork recipes`` — structural integrity checks over recipe definitions.

``recipe-eval`` scores a recipe's shape (prompt sizes, missing prose, examples)
and never gates — its exit code is 0 whatever it finds. That is the right
contract for a style score, and the wrong one for the question this command
answers: **does everything a recipe points at still exist, and does it
compile?**

A recipe is a set of references — ``prompt_ref`` and ``verifier_ref`` in
``workflow.yaml`` name files beside it. Rename a prompt, delete a verifier, or
leave a dependency pointing at a node that was removed, and nothing local
notices: the recipe still parses, ``recipe-eval`` still scores it, and the
failure surfaces as a dispatch error inside a paid run. This command makes that
class of rot a red exit code instead.

    mini-ork recipes check [<recipe>…] [--json] [--strict]

Exit codes:
    0  every recipe is intact
    1  at least one finding of the severity the run considers fatal
    2  usage error (or no recipes directory)

Severity: a *dangling reference* or an *uncompilable workflow* is an error
because the recipe cannot run. An *incomplete* recipe — missing
``task_class.yaml`` or ``artifact_contract.yaml`` — is a warning: it is visible
in the inventory and runnable-or-not depending on how it is invoked, so it is
reported without failing the gate unless ``--strict`` is passed.

Two tiers: this is a repo-local static check — it reads the working tree, opens
no sockets and needs no credentials, so the simple tier and the trust-nothing
tier are the same binary. There is nothing to configure and nothing to leak.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HELP_TEXT = (
    "Usage: mini-ork recipes check [<recipe>...] [--json] [--strict]\n"
    "\n"
    "Structural checks over recipe definitions: every reference resolves and\n"
    "every workflow compiles. Exit 0 clean, 1 findings, 2 usage error.\n"
    "\n"
    "Arguments:\n"
    "  <recipe>   check only this recipe (repeatable); default: all of them\n"
    "\n"
    "Options:\n"
    "  --json     Emit JSON instead of a human-readable report.\n"
    "  --strict   Treat warnings (an incomplete recipe) as failures too.\n"
    "  --help     Show this help.\n"
)

REQUIRED_FILES = ("task_class.yaml", "workflow.yaml", "artifact_contract.yaml")


def _root() -> Path:
    return Path(
        os.environ.get("MINI_ORK_ROOT") or Path(__file__).resolve().parents[2]
    )


def recipe_names(root: Path) -> list[str]:
    base = root / "recipes"
    if not base.is_dir():
        return []
    return sorted(
        entry.name
        for entry in base.iterdir()
        if entry.is_dir() and not entry.is_symlink()
    )


def _load_yaml(path: Path):
    """Parsed YAML, or the parse error. ``None`` fields are never sentinels."""
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}, None
    except Exception as exc:  # any failure to parse is a finding, not a pass
        return None, f"{type(exc).__name__}: {exc}"


def check_recipe(root: Path, name: str) -> list[dict]:
    """Findings for one recipe: dangling refs, unparsable files, missing files."""
    rd = root / "recipes" / name
    findings: list[dict] = []

    def add(severity: str, kind: str, detail: str, fix: str) -> None:
        findings.append(
            {"recipe": name, "severity": severity, "kind": kind,
             "detail": detail, "fix": fix}
        )

    if not rd.is_dir():
        add("error", "missing-recipe", f"recipes/{name} does not exist",
            f"create recipes/{name}/ or drop the argument")
        return findings

    for fn in REQUIRED_FILES:
        if not (rd / fn).is_file():
            add("warning", "incomplete", f"missing {fn}",
                f"create {fn} in recipes/{name}/, or document why this entry "
                f"is not a full recipe")

    wf_path = rd / "workflow.yaml"
    if wf_path.is_file():
        data, err = _load_yaml(wf_path)
        if err is not None:
            add("error", "unparsable", f"workflow.yaml: {err}",
                "fix the YAML; nothing downstream can read this recipe")
        elif data:
            for node in data.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                node_name = node.get("name") or "<unnamed>"
                for key in ("prompt_ref", "verifier_ref"):
                    ref = node.get(key)
                    if not ref:
                        continue
                    if not (rd / str(ref)).is_file():
                        add("error", "dangling-ref",
                            f"node {node_name}: {key} -> {ref} does not exist",
                            f"create {ref}, or repoint {key} at the file that "
                            f"replaced it")
        # Compile last: a dangling ref is a more useful message than the
        # compiler's generic failure for the same recipe.
        if not any(f["kind"] == "unparsable" for f in findings):
            from mini_ork.workflow.compiler import compile_workflow

            try:
                compile_workflow(wf_path)
            except Exception as exc:  # any compile failure is a finding
                add("error", "uncompilable", f"{type(exc).__name__}: {exc}",
                    "fix workflow.yaml; this recipe cannot be dispatched")

    for fn in ("task_class.yaml", "artifact_contract.yaml"):
        path = rd / fn
        if path.is_file():
            _, err = _load_yaml(path)
            if err is not None:
                add("error", "unparsable", f"{fn}: {err}",
                    "fix the YAML; the loader fails closed on this file")

    return findings


def render_human(results: dict[str, list[dict]], fatal: int) -> str:
    lines = ["# recipes check", ""]
    for name, findings in results.items():
        if not findings:
            lines.append(f"✓ {name}")
            continue
        lines.append(f"✗ {name}")
        for f in findings:
            lines.append(f"  [{f['severity']}] {f['kind']}: {f['detail']}")
            lines.append(f"    Fix: {f['fix']}")
    errors = sum(1 for fs in results.values() for f in fs if f["severity"] == "error")
    warnings = sum(1 for fs in results.values() for f in fs if f["severity"] == "warning")
    lines.extend([
        "",
        f"{len(results)} recipe(s), {errors} error(s), {warnings} warning(s)"
        + (" — FAILED" if fatal else " — clean"),
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("--help", "-h", "help"):
        sys.stdout.write(HELP_TEXT)
        return 0
    if argv and argv[0] != "check":
        sys.stderr.write(f"Unknown recipes subcommand: {argv[0]}\n")
        sys.stderr.write(HELP_TEXT)
        return 2

    json_mode = False
    strict = False
    requested: list[str] = []
    for arg in argv[1:]:
        if arg == "--json":
            json_mode = True
        elif arg == "--strict":
            strict = True
        elif arg in ("--help", "-h"):
            sys.stdout.write(HELP_TEXT)
            return 0
        elif arg.startswith("-"):
            sys.stderr.write(f"Unknown flag: {arg}\n")
            sys.stderr.write(HELP_TEXT)
            return 2
        else:
            requested.append(arg)

    root = _root()
    available = recipe_names(root)
    if not available:
        sys.stderr.write(f"no recipes directory under {root}\n")
        return 2

    names = requested or available
    results = {name: check_recipe(root, name) for name in names}

    errors = sum(1 for fs in results.values() for f in fs if f["severity"] == "error")
    warnings = sum(1 for fs in results.values() for f in fs if f["severity"] == "warning")
    fatal = errors > 0 or (strict and warnings > 0)

    if json_mode:
        sys.stdout.write(json.dumps({
            "verdict": "FAIL" if fatal else "PASS",
            "recipes": len(results),
            "errors": errors,
            "warnings": warnings,
            "findings": [f for fs in results.values() for f in fs],
        }, indent=2) + "\n")
    else:
        sys.stdout.write(render_human(results, fatal))
    return 1 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
