"""Native Python port of ``bin/mini-ork-recipe-eval`` — static evaluation of
recipe definitions.

Layer-1 analog of wshobson/agents plugin-eval: deterministic checks with
concrete ``Fix:`` hints. Scores each recipe 0-100 across static dimensions.

The bash script is a thin arg-parser around an embedded Python heredoc; this
module keeps that scoring logic verbatim and makes the CLI natively
importable. Stdout format (human table and ``--json``), usage text, and exit
codes match the bash source.

    main(argv=None) -> int

Exit codes (mirror bash exactly):

    0  evaluation completed (findings never gate the exit code)
    2  usage error (unknown flag / unexpected positional)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Help text — verbatim copy of bash's `cat <<'EOF' … EOF` block in _usage().
# ─────────────────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "Usage: mini-ork recipe-eval [recipe-name]\n"
    "\n"
    "Static evaluation of recipe definitions. Every finding includes a Fix: hint.\n"
    "\n"
    "Options:\n"
    "  --json     Emit JSON instead of human-readable table.\n"
    "  --help     Show this help.\n"
)

MAX_PROMPT_KB = 32
MAX_WORKFLOW_KB = 16


def size_kb(path: Path) -> int:
    return path.stat().st_size // 1024 if path.is_file() else 0


def load_yaml(path: Path):
    if not path.is_file():
        return None
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return None


def eval_recipe(root: Path, name: str) -> dict:
    rd = root / "recipes" / name
    findings = []
    score = 100

    required = {
        "task_class.yaml": 15,
        "workflow.yaml": 15,
        "artifact_contract.yaml": 15,
    }
    for fn, weight in required.items():
        if not (rd / fn).is_file():
            findings.append({"sev": "error", "msg": f"missing {fn}",
                             "fix": f"create recipes/{name}/{fn}"})
            score -= weight

    tc = load_yaml(rd / "task_class.yaml") or {}
    wf = load_yaml(rd / "workflow.yaml") or {}
    ac = load_yaml(rd / "artifact_contract.yaml") or {}

    if tc and not tc.get("name"):
        findings.append({"sev": "error", "msg": "task_class.yaml missing name",
                         "fix": f"add 'name:' to recipes/{name}/task_class.yaml"})
        score -= 10

    if tc and not tc.get("description"):
        findings.append({"sev": "warn", "msg": "task_class.yaml missing description",
                         "fix": f"add 'description:' to recipes/{name}/task_class.yaml"})
        score -= 5

    if wf and not wf.get("nodes"):
        findings.append({"sev": "error", "msg": "workflow.yaml missing nodes",
                         "fix": f"add nodes: to recipes/{name}/workflow.yaml"})
        score -= 10

    verifiers = (ac or {}).get("success_verifiers") or []
    if not verifiers:
        findings.append({"sev": "warn", "msg": "no success_verifiers",
                         "fix": f"add success_verifiers: to recipes/{name}/artifact_contract.yaml"})
        score -= 10

    # Prompt / workflow size checks
    for p in rd.glob("prompts/*.md"):
        kb = size_kb(p)
        if kb > MAX_PROMPT_KB:
            findings.append({"sev": "warn",
                             "msg": f"prompt {p.name} is {kb} KiB (cap {MAX_PROMPT_KB})",
                             "fix": "move detail to prompts/references/"})
            score -= 5

    wf_kb = size_kb(rd / "workflow.yaml")
    if wf_kb > MAX_WORKFLOW_KB:
        findings.append({"sev": "warn",
                         "msg": f"workflow.yaml is {wf_kb} KiB (cap {MAX_WORKFLOW_KB})",
                         "fix": "decompose into smaller nodes"})
        score -= 5

    # Example kickoff presence
    example_dirs = [d for d in (rd / "examples").iterdir() if d.is_dir()] \
        if (rd / "examples").is_dir() else []
    example_files = [f for f in (rd / "examples").iterdir() if f.is_file() and f.suffix == ".md"] \
        if (rd / "examples").is_dir() else []
    if not example_dirs and not example_files:
        findings.append({"sev": "warn", "msg": "no example kickoff",
                         "fix": f"add recipes/{name}/examples/<name>/kickoff.md"})
        score -= 5

    score = max(0, score)
    return {"recipe": name, "score": score, "findings": findings}


def _list_recipes(root: Path) -> list[str]:
    """bash: find "$MINI_ORK_ROOT/recipes" -maxdepth 1 -mindepth 1 -type d | sort."""
    recipes_dir = root / "recipes"
    if not recipes_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in recipes_dir.iterdir()
        if entry.is_dir() and not entry.is_symlink()
    )


def _grade(score: int) -> str:
    return ("A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70
            else "D" if score >= 60 else "F")


def render_human(results: list[dict]) -> str:
    lines = ["# RecipeEval (static)", ""]
    for r in results:
        lines.append(f"## {r['recipe']} — {r['score']}/100 ({_grade(r['score'])})")
        for f in r["findings"]:
            lines.append(f"- [{f['sev']}] {f['msg']}")
            lines.append(f"  Fix: {f['fix']}")
        if not r["findings"]:
            lines.append("- No static findings.")
        lines.append("")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# CLI dispatcher — mirrors bash's arg-parsing and exit-code flow exactly.
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    json_mode = False
    recipe = ""
    for arg in argv:
        if arg in ("--help", "-h"):
            sys.stdout.write(HELP_TEXT)
            return 0
        if arg == "--json":
            json_mode = True
        elif arg.startswith("-"):
            sys.stderr.write(f"Unknown flag: {arg}\n")
            sys.stderr.write(HELP_TEXT)
            return 2
        else:
            if not recipe:
                recipe = arg
            else:
                sys.stderr.write(f"Unexpected argument: {arg}\n")
                sys.stderr.write(HELP_TEXT)
                return 2

    root = Path(os.environ.get("MINI_ORK_ROOT")
                or Path(__file__).resolve().parents[2])

    recipes = [recipe] if recipe else _list_recipes(root)
    results = [eval_recipe(root, name) for name in recipes]

    if json_mode:
        sys.stdout.write(json.dumps(results, indent=2) + "\n")
    else:
        sys.stdout.write(render_human(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
