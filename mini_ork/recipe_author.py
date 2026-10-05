"""Recipe authoring core: JSON spec → recipe files → draft → commit.

S3b-1 ships a deterministic renderer for the ``mini-ork`` recipe format.
A small JSON spec (id, description, keywords, steps, optional publish +
rollback flags, optional example kickoff) is validated against
:data:`SPEC_SCHEMA` and turned into the file tree a recipe directory needs:

* ``task_class.yaml`` — name, version, description, matcher, risk_class
* ``workflow.yaml`` — version, nodes, edges (with ``verifies`` semantics
  for ``implementer → verifier`` pairs and ``escalates_to`` for the
  rollback node)
* ``artifact_contract.yaml`` — task_class, expected_artifact,
  success_verifiers, failure_policy, rollback_policy
* ``prompts/<step>.md`` — model-step instructions preceded by an HTML
  comment banner
* ``recipes/<step>.py`` — auto-generated verifier that runs the spec's
  ``check`` command via ``subprocess.run(shell=True)`` and emits one
  JSON line on stdout (per the recipe-eval runner contract)
* ``README.md``, ``examples/basic/kickoff.md``, ``recipe.spec.json`` —
  human-facing context, the spec round-tripped for later edits

Three workspace primitives operate on a mini-ork home:

* :func:`draft` — render → write to ``<home>/recipe-drafts/<id>/``,
  run :func:`mini_ork.cli.recipe_eval.eval_recipe` against a symlink
  staging dir, return grade + previous contents (never writes into
  ``<home>/recipes/<id>/``).
* :func:`commit_draft` — promote ``recipe-drafts/<id>`` →
  ``recipes/<id>`` atomically (``shutil.copytree`` into a tempdir +
  ``os.replace``); when the target already exists it is first renamed
  into ``<home>/recipe-backups/<id>-<UTC-ts>/`` so a crash never leaves
  the user without their previous recipe.
* :func:`discard_draft` — wipe the draft dir; idempotent.

:func:`guide` exposes the schema + step types + the home's lane map +
authoring rules to MCP/orchestrator clients; :func:`get_spec` returns a
recipe's stored spec (so an editor can edit-by-draft-rerender instead
of editing YAML by hand).

Every public function is defensive: ``validate_spec`` never raises (the
kickoff says it must surface errors as a list of findings, not
exceptions). ``draft`` / ``commit_draft`` / ``discard_draft`` wrap
filesystem operations in try/except and return ``{"ok": False, "error":
...}`` so an MCP tool can never crash the server.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml  # a core dependency (pyproject)


# ─────────────────────────────────────────────────────────────────────────────
# SPEC_SCHEMA — JSON Schema (Draft 2020-12) for the recipe spec.
# ─────────────────────────────────────────────────────────────────────────────


SPEC_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["id", "description", "keywords", "input", "steps"],
    "additionalProperties": False,
    "properties": {
        "id": {
            "type": "string",
            "description": "Recipe id (also the directory name under <home>/recipes/).",
            "pattern": "^[a-z][a-z0-9-]{2,47}$",
        },
        "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 400,
        },
        "keywords": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {"type": "string", "minLength": 1},
        },
        "input": {
            "type": "string",
            "minLength": 1,
            "description": "One-line description of the input the recipe expects.",
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["id", "type"],
                "additionalProperties": False,
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_]{1,31}$",
                    },
                    "type": {
                        "enum": [
                            "planner",
                            "researcher",
                            "implementer",
                            "reviewer",
                            "verifier",
                            "eval",
                        ],
                    },
                    "role": {"type": "string", "minLength": 1},
                    "instructions": {
                        "type": "string",
                        "maxLength": 20000,
                    },
                    "check": {"type": "string", "minLength": 1},
                    "after": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "allOf": [
                    {
                        # verifier: needs check, must not carry a role.
                        "if": {"properties": {"type": {"const": "verifier"}}},
                        "then": {
                            "required": ["check"],
                            "properties": {"role": False},
                        },
                    },
                    {
                        # non-verifier model step: needs role + instructions.
                        "if": {
                            "properties": {
                                "type": {"not": {"const": "verifier"}},
                            }
                        },
                        "then": {"required": ["role", "instructions"]},
                    },
                ],
            },
        },
        "publish": {
            "type": "boolean",
            "description": "Render a publisher node + edge when true.",
            "default": True,
        },
        "rollback_on_failure": {
            "type": "boolean",
            "description": "Render a rollback node + escalates_to edge when true.",
            "default": False,
        },
        "example_kickoff": {
            "type": "string",
            "description": "Optional markdown body for examples/basic/kickoff.md.",
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


_STEP_TYPES: dict[str, str] = {
    "planner": "Plans how to approach the task; produces a step plan.",
    "researcher": "Reads the repo and produces findings or context.",
    "implementer": "Writes the change (code, doc, etc.).",
    "reviewer": "Reviews the change and emits a verdict.",
    "verifier": "Runs a shell check; exit 0 = pass.",
    "eval": "Advisory graded eval; never gates the run.",
}


_RULES: list[str] = [
    "Use the fewest steps that do the job.",
    "Every recipe needs at least one verifier, reviewer, or eval step.",
    "Checks must be commands that exit 0 on success.",
    "Instructions say what to read, what to produce, and what not to touch.",
    "Verifiers do not have a role; model steps do.",
    "Step `after` references must point at earlier step ids.",
    "`base`, when given, must equal the spec's `id` (editing in place).",
]


_EXAMPLE_SPEC: dict[str, Any] = {
    "id": "sql-migration-audit",
    "description": "Audit a SQL migration for locking and rollback risks.",
    "keywords": ["audit migration", "migration review", "sql migration"],
    "input": "The path of the migration file to audit.",
    "steps": [
        {
            "id": "analyzer",
            "type": "researcher",
            "role": "planner",
            "instructions": (
                "Read the migration named in the kickoff and list the "
                "tables / lock-acquisition order / down-migration steps."
            ),
        },
        {
            "id": "schema_check",
            "type": "verifier",
            "check": "alembic check",
            "after": ["analyzer"],
        },
        {
            "id": "reviewer",
            "type": "reviewer",
            "role": "reviewer",
            "instructions": (
                "Review the analyzer's findings; emit verdict and "
                "highlight any locking or rollback risk."
            ),
            "after": ["analyzer", "schema_check"],
        },
    ],
    "publish": False,
    "rollback_on_failure": False,
    "example_kickoff": (
        "# Audit migration 0042\n\n"
        "## Files in scope\n\n"
        "- db/migrations/0042_x.sql\n"
    ),
}


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_load_lanes(home: Path | None) -> dict[str, str]:
    """Wrap ``load_lanes`` in try/except so a broken config never crashes ``guide``."""
    try:
        from mini_ork.web.recipes import load_lanes

        return load_lanes(home) or {}
    except Exception:
        return {}


def _engine_recipe_ids() -> set[str]:
    """Engine root recipe ids — used by ``validate_spec`` for the shadow warning."""
    try:
        from mini_ork.recipes_catalog import list_recipes

        return {e.id for e in list_recipes(None)}
    except Exception:
        return set()


def _step_type_map(steps: list[dict]) -> dict[str, str]:
    return {s["id"]: s["type"] for s in steps}


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    """DFS cycle detector over ``id → after ids``. False-positive free."""
    color: dict[str, str] = {n: "white" for n in graph}

    def dfs(node: str) -> bool:
        if color[node] == "gray":
            return True
        if color[node] == "black":
            return False
        color[node] = "gray"
        for nxt in graph.get(node, []):
            if nxt in graph and dfs(nxt):
                return True
        color[node] = "black"
        return False

    for node in list(graph):
        if color[node] == "white" and dfs(node):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# guide
# ─────────────────────────────────────────────────────────────────────────────


def guide(home: Path | None) -> dict[str, Any]:
    """Authoring guide — schema + step types + roles + rules + example."""
    return {
        "spec_schema": SPEC_SCHEMA,
        "step_types": dict(_STEP_TYPES),
        "roles": _safe_load_lanes(home),
        "rules": list(_RULES),
        "example": json.loads(json.dumps(_EXAMPLE_SPEC)),  # deep copy
    }


# ─────────────────────────────────────────────────────────────────────────────
# validate_spec
# ─────────────────────────────────────────────────────────────────────────────


def validate_spec(spec: Any, home: Path | None) -> list[dict[str, Any]]:
    """Validate a recipe spec.

    Returns a list of findings shaped like ``{"sev": "error"|"warn", "msg": ...,
    "fix": ...}``. Schema errors are fatal — the function bails early once
    jsonschema fails so downstream checks don't pile up confusing follow-on
    errors. **Never raises** — every code path returns a list.
    """
    findings: list[dict[str, Any]] = []

    # ── schema ──
    if not isinstance(spec, dict):
        findings.append({
            "sev": "error",
            "msg": "spec must be an object",
            "fix": "pass a JSON object, not an array or scalar",
        })
        return findings

    try:
        import jsonschema
    except Exception as exc:
        findings.append({
            "sev": "error",
            "msg": f"schema: jsonschema import failed: {exc}",
            "fix": "install jsonschema (pyproject already pins it)",
        })
        return findings

    try:
        jsonschema.validate(spec, SPEC_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        findings.append({
            "sev": "error",
            "msg": f"schema: {exc.message}",
            "fix": f"fix the spec at {path}",
        })
        return findings
    except Exception as exc:
        findings.append({
            "sev": "error",
            "msg": f"schema: {exc}",
            "fix": "report a bug — jsonschema check failed unexpectedly",
        })
        return findings

    steps = spec.get("steps") or []
    recipe_id = spec.get("id") or "<unknown>"

    # ── id uniqueness + after references ──
    seen: set[str] = set()
    for s in steps:
        if s["id"] in seen:
            findings.append({
                "sev": "error",
                "msg": f"duplicate step id: {s['id']}",
                "fix": "rename one of the duplicate ids",
            })
        seen.add(s["id"])

    type_map = _step_type_map(steps)
    for s in steps:
        for after_id in s.get("after", []) or []:
            if after_id == s["id"]:
                findings.append({
                    "sev": "error",
                    "msg": f"step {s['id']} depends on itself",
                    "fix": f"remove {s['id']} from its own after list",
                })
            elif after_id not in type_map:
                findings.append({
                    "sev": "error",
                    "msg": (
                        f"step {s['id']} references unknown `after` id: "
                        f"{after_id}"
                    ),
                    "fix": (
                        f"add a step named {after_id} or remove it from "
                        f"the after list"
                    ),
                })

    # ── cycle ──
    if not any(f["sev"] == "error" for f in findings):
        graph = {s["id"]: list(s.get("after", []) or []) for s in steps}
        if _has_cycle(graph):
            findings.append({
                "sev": "error",
                "msg": "steps contain a cycle",
                "fix": "remove cyclic `after` references",
            })

    # ── unknown role (model steps only) ──
    valid_roles: set[str] = set()
    if home is not None:
        valid_roles = set(_safe_load_lanes(home).keys())
    for s in steps:
        if s["type"] == "verifier":
            continue
        role = s.get("role")
        if isinstance(role, str) and role and valid_roles and role not in valid_roles:
            findings.append({
                "sev": "error",
                "msg": f"step {s['id']} uses unknown role: {role}",
                "fix": (
                    "use one of: " + ", ".join(sorted(valid_roles))
                ),
            })

    # ── at least one verifier/reviewer/eval ──
    if not any(s["type"] in ("verifier", "reviewer", "eval") for s in steps):
        findings.append({
            "sev": "warn",
            "msg": "recipe has no verifier, reviewer, or eval step",
            "fix": (
                "add a verifier (or reviewer / eval) step so the result "
                "is checked"
            ),
        })

    # ── engine id collision ──
    if recipe_id in _engine_recipe_ids():
        findings.append({
            "sev": "warn",
            "msg": (
                f"id {recipe_id!r} collides with an engine recipe"
            ),
            "fix": (
                "your project recipe will override the engine's recipe; "
                "pick a different id if you do not want that"
            ),
        })

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# render
# ─────────────────────────────────────────────────────────────────────────────


def _verifier_script(recipe_id: str, step_id: str, check: str) -> str:
    """Generate a verifier script that runs ``check`` and emits one JSON line.

    The check command is JSON-encoded into a raw triple-quoted string so
    any character is safely embedded. The script keeps the original command
    verbatim in the result so ``recipe_eval`` and downstream consumers see
    the same string the user authored.
    """
    check_json = json.dumps(check)
    header = (
        f'"""Auto-generated verifier for recipe {recipe_id}, step {step_id}.\n'
        f"\n"
        f"Runs the spec's `check` command via subprocess.run(shell=True) "
        f"and emits ONE JSON line on stdout:\n"
        f"  {{'pass': rc == 0, 'check': <command>, 'rc': <int>, "
        f"'output_tail': <last 2000 chars of stdout+stderr>}}\n"
        f"\n"
        f"Timeout (>900s) → pass: false. Edit cautiously — "
        f"task_class.yaml's success_verifiers key off this script path.\n"
        f'"""\n'
    )
    body = (
        "import json\n"
        "import subprocess\n"
        "\n"
        "_CHECK_DATA = r\"\"\"\n"
        f"{check_json}\n"
        "\"\"\"\n"
        "CHECK = json.loads(_CHECK_DATA)\n"
        "\n"
        "\n"
        "def _main() -> None:\n"
        "    try:\n"
        "        proc = subprocess.run(\n"
        "            CHECK,\n"
        "            shell=True,\n"
        "            capture_output=True,\n"
        "            text=True,\n"
        "            timeout=900,\n"
        "        )\n"
        "        out = (proc.stdout or \"\") + (proc.stderr or \"\")\n"
        "        result = {\n"
        "            \"pass\": proc.returncode == 0,\n"
        "            \"check\": CHECK,\n"
        "            \"rc\": proc.returncode,\n"
        "            \"output_tail\": out[-2000:],\n"
        "        }\n"
        "    except subprocess.TimeoutExpired as exc:\n"
        "        stdout = exc.stdout or \"\"\n"
        "        stderr = exc.stderr or \"\"\n"
        "        if isinstance(stdout, bytes):\n"
        "            stdout = stdout.decode(errors=\"replace\")\n"
        "        if isinstance(stderr, bytes):\n"
        "            stderr = stderr.decode(errors=\"replace\")\n"
        "        out = stdout + stderr\n"
        "        result = {\n"
        "            \"pass\": False,\n"
        "            \"check\": CHECK,\n"
        "            \"rc\": -1,\n"
        "            \"output_tail\": out[-2000:],\n"
        "        }\n"
        "    print(json.dumps(result))\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    _main()\n"
    )
    return header + body


def _render_readme(
    recipe_id: str,
    description: str,
    steps: list[dict],
    publish: bool,
    rollback_on_failure: bool,
) -> str:
    step_lines = [
        "| id | type | role | depends on |",
        "|---|---|---|---|",
    ]
    for s in steps:
        role = s.get("role") or "—"
        after = ", ".join(s.get("after", []) or []) or "—"
        step_lines.append(f"| {s['id']} | {s['type']} | {role} | {after} |")
    step_table = "\n".join(step_lines)
    parts = [
        f"# {recipe_id}",
        "",
        description,
        "",
        "## Steps",
        "",
        step_table,
        "",
        "## How to run",
        "",
        "```bash",
        f"mini-ork run {recipe_id} .mini-ork/recipes/{recipe_id}/examples/basic/kickoff.md",
        "```",
        "",
    ]
    if not publish:
        parts.append(
            "_This recipe does not include a publisher node — the "
            "workflow ends after the final reviewer / verifier step._"
        )
        parts.append("")
    if rollback_on_failure:
        parts.append(
            "_On failure the rollback node reverts the branch via "
            "`git checkout HEAD -- <changed-files>`._"
        )
        parts.append("")
    return "\n".join(parts)


def _render_example_kickoff(spec: dict[str, Any]) -> str:
    recipe_id = spec.get("id", "<recipe-id>")
    description = spec.get("description", "")
    input_line = spec.get("input", "")
    parts = [
        f"# {recipe_id} — example kickoff",
        "",
        description,
        "",
        "## Input",
        "",
        input_line,
        "",
        "## Files in scope",
        "",
        "- (fill in paths to the files this recipe should read or change)",
        "",
    ]
    return "\n".join(parts)


def _build_edges(
    steps: list[dict[str, Any]],
    *,
    has_publisher: bool,
    has_rollback: bool,
) -> list[dict[str, str]]:
    """Edge construction per the kickoff spec.

    1. Each explicit ``after`` produces ``{from: after, to: step,
       edge_type: depends_on}`` — or ``verifies`` when the target is a
       verifier AND the source is an implementer.
    2. Steps with no ``after`` AND not first depend on the previous step.
    3. The publisher depends on every step nothing else depends on
       (terminal nodes).
    4. The rollback edge is ``{from: publisher, to: rollback,
       edge_type: escalates_to}`` — or from the last step when there is
       no publisher.
    """
    type_map = _step_type_map(steps)
    edges: list[dict[str, str]] = []

    # 1. explicit after edges
    for s in steps:
        for source in s.get("after", []) or []:
            edge_type = (
                "verifies"
                if s["type"] == "verifier"
                and type_map.get(source) == "implementer"
                else "depends_on"
            )
            edges.append({
                "from": source,
                "to": s["id"],
                "edge_type": edge_type,
            })

    # 2. implicit chaining for steps with no after (and not first)
    for i, s in enumerate(steps):
        if i == 0:
            continue
        if s.get("after"):
            continue
        prev = steps[i - 1]["id"]
        if not any(e["from"] == prev and e["to"] == s["id"] for e in edges):
            edges.append({"from": prev, "to": s["id"], "edge_type": "depends_on"})

    # 3. publisher edges from terminal steps — the ends of the flow, i.e. steps
    #    no edge starts from (so publishing waits for every check).
    if has_publisher:
        sources = {e["from"] for e in edges}
        for s in steps:
            if s["id"] not in sources:
                edges.append({
                    "from": s["id"],
                    "to": "publisher",
                    "edge_type": "depends_on",
                })

    # 4. rollback edge
    if has_rollback:
        source = "publisher" if has_publisher else steps[-1]["id"]
        edges.append({
            "from": source,
            "to": "rollback",
            "edge_type": "escalates_to",
        })

    return edges


def _yaml_dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)


def render(spec: dict[str, Any]) -> dict[str, str]:
    """Render the spec into a {relative path → file content} map.

    Deterministic: same spec → same bytes. YAML is dumped with
    ``sort_keys=False`` (kickoff requirement); ``recipe.spec.json`` is
    pretty-printed with ``json.dumps(..., indent=2, sort_keys=False)``.
    """
    recipe_id = spec["id"]
    description = spec["description"]
    keywords = list(spec.get("keywords") or [])
    steps = list(spec.get("steps") or [])
    publish = bool(spec.get("publish", True))
    rollback_on_failure = bool(spec.get("rollback_on_failure", False))
    example_kickoff = spec.get("example_kickoff")

    task_class_name = recipe_id.replace("-", "_")

    # ── task_class.yaml ──
    task_class: dict[str, Any] = {
        "name": task_class_name,
        "task_class": task_class_name,
        "version": "0.1.0",
        "description": description,
        "artifact_contract_ref": "artifact_contract.yaml",
        "default_workflow": "workflow.yaml",
        "matches": {"keywords": keywords},
        "default_gates": [],
        "risk_class": "low",
    }

    # ── workflow.yaml ──
    nodes: list[dict[str, Any]] = []
    for s in steps:
        node: dict[str, Any] = {
            "name": s["id"],
            "type": s["type"],
            "dispatch_mode": "serial",
        }
        if s["type"] == "verifier":
            node["prompt_ref"] = None
            node["verifier_ref"] = f"verifiers/{s['id']}.py"
        else:
            node["model_lane"] = s["role"]
            node["prompt_ref"] = f"prompts/{s['id']}.md"
        nodes.append(node)

    if publish:
        nodes.append({
            "name": "publisher",
            "type": "publisher",
            "prompt_ref": None,
            "dispatch_mode": "serial",
        })
    if rollback_on_failure:
        nodes.append({
            "name": "rollback",
            "type": "rollback",
            "prompt_ref": None,
            "dispatch_mode": "serial",
        })

    edges = _build_edges(
        steps,
        has_publisher=publish,
        has_rollback=rollback_on_failure,
    )

    workflow: dict[str, Any] = {
        "version": "0.1.0",
        "task_class": task_class_name,
        "description": description,
        "nodes": nodes,
        "edges": edges,
    }
    if rollback_on_failure:
        workflow["rollback_strategy"] = "revert_branch"

    # ── artifact_contract.yaml ──
    artifact_contract: dict[str, Any] = {
        "task_class": task_class_name,
        "expected_artifact": "data",
        "success_verifiers": [
            f"verifiers/{s['id']}.py"
            for s in steps
            if s["type"] == "verifier"
        ],
        "failure_policy": "request_changes",
        "rollback_policy": (
            "git checkout HEAD -- <changed-files>\n"
            "if reviewer rejects after 3 iterations."
            if rollback_on_failure
            else ""
        ),
    }

    files: dict[str, str] = {
        "task_class.yaml": _yaml_dump(task_class),
        "workflow.yaml": _yaml_dump(workflow),
        "artifact_contract.yaml": _yaml_dump(artifact_contract),
    }

    # ── prompts/<step>.md ──
    for s in steps:
        if s["type"] == "verifier":
            continue
        instructions = s.get("instructions") or ""
        files[f"prompts/{s['id']}.md"] = (
            f"<!-- step {s['id']} of recipe {recipe_id} -->\n" + instructions
        )

    # ── verifiers/<step>.py ──
    for s in steps:
        if s["type"] != "verifier":
            continue
        files[f"verifiers/{s['id']}.py"] = _verifier_script(
            recipe_id, s["id"], s.get("check", "true")
        )

    # ── human-facing ──
    files["README.md"] = _render_readme(
        recipe_id, description, steps, publish, rollback_on_failure
    )
    # examples/<name>/kickoff.md is where the recipe evaluator looks.
    files["examples/basic/kickoff.md"] = (
        example_kickoff if example_kickoff else _render_example_kickoff(spec)
    )
    files["recipe.spec.json"] = json.dumps(spec, indent=2, sort_keys=False) + "\n"

    return files


# ─────────────────────────────────────────────────────────────────────────────
# grade letter — inline A/B/C/D/F boundary, mirrors recipe_eval._grade
# (without importing the private symbol).
# ─────────────────────────────────────────────────────────────────────────────


def _letter_for(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


# ─────────────────────────────────────────────────────────────────────────────
# draft
# ─────────────────────────────────────────────────────────────────────────────


def draft(home: Path, spec: dict[str, Any], *, base: str | None = None) -> dict[str, Any]:
    """Render the spec → write to ``<home>/recipe-drafts/<id>/`` → grade.

    Returns ``{"ok": True, "draft_id", "target", "exists", "files",
    "warnings", "grade"}`` on success; ``{"ok": False, "errors": [...]}"``
    on validation failure (no files written). Other exceptions are
    surfaced as ``{"ok": False, "error": ...}``.
    """
    findings = validate_spec(spec, home)
    errors = [f for f in findings if f["sev"] == "error"]
    if errors:
        return {"ok": False, "errors": errors}

    recipe_id = spec["id"]
    if base is not None and base != recipe_id:
        return {
            "ok": False,
            "errors": [{
                "sev": "error",
                "msg": f"base {base!r} must equal spec id {recipe_id!r}",
                "fix": "either drop `base` or set it to the same id",
            }],
        }

    warnings = [f for f in findings if f["sev"] == "warn"]
    rendered = render(spec)

    drafts_root = home / "recipe-drafts"
    drafts_dir = drafts_root / recipe_id
    target_dir = home / "recipes" / recipe_id

    try:
        # Wipe any older draft for the same id.
        if drafts_dir.exists():
            shutil.rmtree(drafts_dir)
        drafts_dir.mkdir(parents=True, exist_ok=True)

        files_payload: list[dict[str, Any]] = []
        for rel_path, content in rendered.items():
            target_file = target_dir / rel_path
            previous: str | None = None
            if target_file.is_file():
                try:
                    previous = target_file.read_text(encoding="utf-8")
                except OSError:
                    previous = None

            out_file = drafts_dir / rel_path
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(content, encoding="utf-8")
            files_payload.append({
                "path": rel_path,
                "content": content,
                "previous": previous,
            })

        draft_meta = {
            "spec": spec,
            "base": base,
            "created_at": _utcnow_iso(),
        }
        (drafts_dir / "draft.json").write_text(
            json.dumps(draft_meta, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        return {"ok": False, "error": f"draft write failed: {exc}"}

    # ── grade via recipe_eval (temp symlink staging) ──
    grade = _grade_draft(drafts_root, recipe_id)

    return {
        "ok": True,
        "draft_id": recipe_id,
        "target": str(target_dir),
        "exists": target_dir.exists(),
        "files": files_payload,
        "warnings": warnings,
        "grade": grade,
    }


def _grade_draft(drafts_root: Path, recipe_id: str) -> dict[str, Any]:
    """Run eval_recipe against ``drafts_root`` via a per-call symlink stage.

    ``recipe_eval.eval_recipe`` looks at ``<root>/recipes/<id>/``; drafts
    live at ``<drafts_root>/<id>/``. A symlink bridge lets us reuse the
    existing evaluator without editing recipe_eval (out of scope per the
    kickoff: "No other file changes").
    """
    try:
        with tempfile.TemporaryDirectory(
            prefix=".recipe_author_stage."
        ) as tmp:
            tmp_path = Path(tmp)
            link = tmp_path / "recipes"
            # Absolute target: a relative one would resolve against the temp dir.
            link.symlink_to(drafts_root.resolve(), target_is_directory=True)
            from mini_ork.cli.recipe_eval import eval_recipe

            ev = eval_recipe(tmp_path, recipe_id) or {}
            score = int(ev.get("score", 0) or 0)
            findings = list(ev.get("findings") or [])
            return {
                "score": score,
                "letter": _letter_for(score),
                "findings": findings,
            }
    except Exception as exc:
        return {
            "score": 0,
            "letter": "F",
            "findings": [{
                "sev": "error",
                "msg": f"eval_recipe raised: {exc}",
                "fix": "report a bug — recipe_author.draft grading failed",
            }],
        }


# ─────────────────────────────────────────────────────────────────────────────
# commit_draft / discard_draft
# ─────────────────────────────────────────────────────────────────────────────


def commit_draft(home: Path, recipe_id: str) -> dict[str, Any]:
    """Promote ``<home>/recipe-drafts/<id>`` → ``<home>/recipes/<id>``.

    Atomic: build the new tree in a tempdir beside the target, then
    ``os.replace`` it into place. When the target already exists it is
    first renamed to ``<home>/recipe-backups/<id>-<UTC-ts>/`` so a crash
    mid-commit can be reversed by re-running commit_draft.
    """
    drafts_dir = home / "recipe-drafts" / recipe_id
    if not drafts_dir.is_dir():
        return {"ok": False, "error": f"no draft for {recipe_id!r}"}

    target_dir = home / "recipes" / recipe_id
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    backups_root = home / "recipe-backups"
    backups_root.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    promoted = False
    try:
        with tempfile.TemporaryDirectory(
            dir=target_dir.parent, prefix=f".{recipe_id}."
        ) as tmp:
            staging = Path(tmp) / recipe_id
            shutil.copytree(
                drafts_dir,
                staging,
                ignore=shutil.ignore_patterns("draft.json"),
            )

            if target_dir.exists():
                ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = backups_root / f"{recipe_id}-{ts}"
                os.replace(target_dir, backup_path)

            os.replace(staging, target_dir)
            promoted = True

        shutil.rmtree(drafts_dir)

        return {
            "ok": True,
            "path": str(target_dir),
            "backup": str(backup_path) if backup_path else None,
        }
    except Exception as exc:
        if backup_path is not None and not promoted and backup_path.exists():
            try:
                if not target_dir.exists():
                    os.replace(backup_path, target_dir)
            except Exception:
                pass
        return {"ok": False, "error": f"{exc}"}


def discard_draft(home: Path, recipe_id: str) -> dict[str, Any]:
    """Delete the draft dir. Idempotent — absent → ``{"ok": True}``."""
    drafts_dir = home / "recipe-drafts" / recipe_id
    if drafts_dir.exists():
        try:
            shutil.rmtree(drafts_dir)
        except Exception as exc:
            return {"ok": False, "error": f"{exc}"}
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# get_spec
# ─────────────────────────────────────────────────────────────────────────────


def get_spec(home: Path | None, recipe_id: str) -> dict[str, Any] | None:
    """Return the parsed ``recipe.spec.json`` of a project / engine recipe.

    Returns ``None`` when the recipe is absent OR was not authored from a
    spec (legacy / engine recipes that predate S3b-1 carry no
    ``recipe.spec.json``).
    """
    try:
        from mini_ork.recipes_catalog import find_recipe

        entry = find_recipe(recipe_id, home)
    except Exception:
        return None
    if entry is None:
        return None

    spec_path = entry.path / "recipe.spec.json"
    if not spec_path.is_file():
        return None
    try:
        with spec_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


__all__ = [
    "SPEC_SCHEMA",
    "guide",
    "validate_spec",
    "render",
    "draft",
    "commit_draft",
    "discard_draft",
    "get_spec",
]