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

Known divergence: the port adds Check 5 (lane liveness, below), which the
bash source never had. It is the only check that can touch the network — one
cached 1-token probe per configured lane, ten-minute TTL — and it is a no-op
whenever no probeable lane has its credential set.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from mini_ork.cli.main import _module_env

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
    env = {**_module_env(root), "MINI_ORK_DRY_RUN": "1"}
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
# Check 1b — task-class uniqueness + recipe-declaration consistency.
# ─────────────────────────────────────────────────────────────────────────────
def _check_task_class_names(root: str, findings: Findings,
                            *, check_duplicates: bool = True) -> None:
    """Error on task classes declared by more than one recipe; warn on drift.

    ``check_duplicates`` is False when ``main`` was given an explicit
    ``--recipe``: the duplicate scan is a whole-tree property, while a
    single-recipe validate must not fail on unrelated sibling recipes (the
    hermetic fixtures reuse one class name). Mismatch warnings are always
    scoped to each recipe and therefore always run.
    """
    import yaml

    recipes_dir = os.path.join(root, "recipes")
    if not os.path.isdir(recipes_dir):
        return

    declared: dict[str, list[str]] = {}
    per_recipe: dict[str, tuple[str, str]] = {}
    for entry in sorted(os.listdir(recipes_dir)):
        recipe_dir = os.path.join(recipes_dir, entry)
        if not os.path.isdir(recipe_dir):
            continue
        tc = os.path.join(recipe_dir, "task_class.yaml")
        if not os.path.isfile(tc):
            continue  # recipes without a task class are out of scope
        try:
            with open(tc) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue  # invalid YAML is reported by the recipe's own checks
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name.strip():
            continue
        declared.setdefault(name, []).append(entry)
        per_recipe[entry] = (name, tc)

    if check_duplicates:
        for name, recipes in sorted(declared.items()):
            if len(recipes) > 1:
                findings.error(
                    f"task class '{name}' is declared by {len(recipes)} "
                    f"recipes: {', '.join(sorted(recipes))}",
                    "give each recipe a distinct name in its task_class.yaml",
                )

    for recipe, (name, _tc) in sorted(per_recipe.items()):
        for manifest in ("workflow.yaml", "artifact_contract.yaml"):
            path = os.path.join(recipes_dir, recipe, manifest)
            if not os.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    doc = yaml.safe_load(f) or {}
            except Exception:
                continue
            declared_class = (doc.get("task_class")
                              if isinstance(doc, dict) else None)
            if declared_class is None or declared_class == name:
                continue
            findings.warn(
                f"recipe {recipe} {manifest} declares task_class "
                f"'{declared_class}' but task_class.yaml name is '{name}'",
                f"set task_class: {name} in {path}",
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


_WORKFLOW_SCHEMA_REL = os.path.join("schemas", "workflow.schema.json")
_SCHEMA_ERROR_CAP = 5


def _check_workflow_schema(root: str, recipe: str, wf: str,
                           findings: Findings) -> None:
    """Validate ``workflow.yaml`` against the engine's workflow schema.

    The schema is resolved from ``MINI_ORK_ROOT`` rather than the installed
    package: a hermetic tmp root that ships no ``schemas/`` cannot be schema
    checked, and skipping there keeps fixture-only roots (whose minimal
    workflows predate this check) valid instead of reporting them as broken.
    """
    schema_path = os.path.join(root, _WORKFLOW_SCHEMA_REL)
    if not os.path.isfile(schema_path):
        return
    try:
        import jsonschema  # noqa: F401 - presence is the point
        from jsonschema.validators import validator_for
    except ImportError:
        findings.warn(
            f"recipe {recipe} workflow schema check skipped: jsonschema is not importable",
            "install jsonschema (a declared dependency) to enable workflow "
            "schema validation",
        )
        return
    try:
        import json

        import yaml
        with open(schema_path) as f:
            schema = json.load(f)
        with open(wf) as f:
            document = yaml.safe_load(f)
        # Match the schema's declared draft (2020-12) rather than assuming it.
        validator = validator_for(schema)(schema)
        errors = list(validator.iter_errors(document))
    except Exception as exc:  # malformed schema or unreadable file
        findings.warn(
            f"recipe {recipe} workflow schema check skipped: {exc}",
            f"fix {schema_path}",
        )
        return
    for error in errors[:_SCHEMA_ERROR_CAP]:
        path = "/".join(str(part) for part in error.absolute_path) or "<root>"
        findings.error(
            f"recipe {recipe} workflow.yaml violates the schema at {path}: "
            f"{error.message}",
            f"fix {wf} or schemas/workflow.schema.json",
        )
    if len(errors) > _SCHEMA_ERROR_CAP:
        findings.error(
            f"recipe {recipe} workflow.yaml has "
            f"{len(errors) - _SCHEMA_ERROR_CAP} more schema error(s)",
            f"fix {wf} or schemas/workflow.schema.json",
        )


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


_REPAIR_LOOP_NODE_TYPES = ("planner", "reflector")


def _cycle_member_nodes(edges: list[tuple[str, str]]) -> set[str]:
    """Every node that lies on a directed cycle of the full edge graph.

    The compiler deliberately keeps non-control edges (``escalates_to``,
    ``recursive`` back edges) out of the scheduling DAG, so a repair loop can
    compile as acyclic while the *full* edge graph — which is what actually
    re-runs nodes — is cyclic. Reachability back to the start is the definition
    that matters here: a node is in a repair loop iff some edge path returns to
    it. Small graphs, so plain DFS per node is plenty.
    """
    graph: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())
    on_cycle: set[str] = set()
    for start in graph:
        stack = list(graph.get(start, ()))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == start:
                on_cycle.add(start)
                break
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return on_cycle


def _check_repair_loop_nodes(wf: str, recipe: str, findings: Findings) -> None:
    """Warn about prompt-less planner/reflector nodes inside a repair cycle.

    Their early handlers ignore ``prompt_ref``, the node's lane and its declared
    outputs — the planner prints ``[skip]`` and the reflector runs the global
    reflect pipeline — so a cycle routed through one of them can never write the
    artifact the loop needs (``reflector.json``/``replan.json``) or converge.
    """
    import yaml

    try:
        with open(wf) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return  # a malformed workflow is already an error
    if not isinstance(data, dict):
        return

    nodes = [n for n in (data.get("nodes") or []) if isinstance(n, dict)]
    edges = []
    for edge in data.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from") or "").strip()
        target = str(edge.get("to") or "").strip()
        if source and target:
            edges.append((source, target))
    if not edges:
        return

    on_cycle = _cycle_member_nodes(edges)
    for node in nodes:
        name = str(node.get("name") or "").strip()
        node_type = str(node.get("type") or "").strip()
        if name not in on_cycle or node_type not in _REPAIR_LOOP_NODE_TYPES:
            continue
        if str(node.get("prompt_ref") or "").strip():
            continue
        findings.warn(
            f"recipe {recipe} node '{name}' ({node_type}) is on a repair cycle "
            f"but declares no prompt_ref — its early handler ignores the node's "
            f"prompt, lane and outputs, so the loop cannot converge",
            f"add 'prompt_ref: prompts/{name}.md' to node {name} in {wf}",
        )


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
    elif os.path.isfile(wf):
        # The `recursion:` block is read at dispatch time and a malformed one
        # raises there. Surface it pre-run instead, so an incomplete block is a
        # validate finding rather than a crash in the middle of a live run.
        from mini_ork.workflow.recursion import (
            RecursionConfigError,
            load_recursion_config,
        )

        try:
            load_recursion_config(wf)
        except RecursionConfigError as exc:
            findings.error(
                f"recipe {recipe} has an invalid recursion block",
                str(exc),
            )
        # A repair cycle routed through a prompt-less planner/reflector is a
        # no-op: the early handler drops the node's prompt/lane/outputs, so the
        # loop can never produce the artifact it re-reads. Warn pre-run.
        _check_repair_loop_nodes(wf, recipe, findings)
        # The workflow schema is the contract the compiler reads; checking it
        # pre-run is what makes later schema removals enforceable.
        _check_workflow_schema(root, recipe, wf, findings)

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
def _runtime_secret_env() -> tuple[dict[str, str] | None, str | None]:
    """The environment the dispatcher actually resolves credentials from.

    Every lane's env in ``providers.dispatch_model`` is built through
    ``provider_environment()``: the owner-only local store overlaid by the
    ambient environment. A check reading ``os.environ`` alone therefore reports
    a store-backed credential as MISSING — observed 2026-10-16, where Check 4
    named 12 lanes missing while the live probe authenticated against every one
    of them, burying the single real fault (kimi, HTTP 401) in the noise.

    Returns ``(runtime, None)``, or ``(None, message)`` when the store is
    unreadable or permissive. Failing closed is the point: an unusable store
    must never be reported as "credentials are fine, just not exported".
    """
    from mini_ork.dispatch import providers as providers_mod
    try:
        return providers_mod.provider_environment(), None
    except Exception as exc:  # SecretStoreError and friends
        return None, str(exc)


def _check_provider_secrets(home: str, findings: Findings) -> None:
    import yaml
    # Honour the override the dispatcher already supports (dispatch/providers.py),
    # or this check reports on a different file than the one lanes resolve from.
    providers_yaml = (
        os.environ.get("MINI_ORK_PROVIDERS")
        or os.path.join(home, "config", "providers.yaml")
    )
    if not os.path.isfile(providers_yaml):
        return
    try:
        with open(providers_yaml) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return
    # This check owns the unreadable-store message; Check 5 (which runs after
    # it) returns quietly on the same condition rather than repeating it.
    runtime, store_error = _runtime_secret_env()
    if runtime is None:
        findings.warn(
            f"cannot read the local secret store: {store_error}",
            f"check permissions on {home}/config/secrets.local.sh (owner-only, 0600)",
        )
        return
    for name, cfg in (data.get("providers") or {}).items():
        env_var = cfg.get("api_key_env") or cfg.get("env_key")
        if env_var and not runtime.get(env_var):
            findings.warn(
                f"provider secret missing: {name} -> {env_var}",
                f"set it in {home}/config/secrets.local.sh",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Check 5 — lane liveness. Check 4 proves a credential is PRESENT; a key that is
# set can still be dead, and that is the failure this check exists for. ``kimi``
# stayed in both ``dispatch_chain`` fallback tails with a 401-rejected key, so
# every coding/review node that fell through to it died with a misleading
# ``lane preflight failed`` instead of its lead's real error — and nothing at
# instantiation said so, because no instantiation surface consulted the health
# machinery that already existed.
#
# Reuses ``providers.lane_health`` (the same cached 1-token probe the run-start
# ``lane_preflight`` gate calls), so validate and the run cannot disagree about
# what "healthy" means. Cache lives at ``<home>/lane-probe-cache.json``
# (``_PROBE_TTL_S`` = 600s), so repeat validates are free.
#
# Two deliberate non-findings, both of which would otherwise make CI red for
# reasons that are not this check's to report:
#   * a lane whose ``api_key_env`` is unset — Check 4 already names it;
#   * a lane whose kind is not probeable — that is a providers.yaml shape
#     problem, and ``lane_health`` answers it with "unsupported kind", which is
#     not a liveness verdict.
# ─────────────────────────────────────────────────────────────────────────────
_PROBEABLE_KINDS = (
    "anthropic-compat", "openai-compat", "openai-chat", "uhp", "anthropic-native",
)


def _check_lane_liveness(root: str, home: str, findings: Findings) -> None:
    from mini_ork.dispatch import providers as providers_mod

    providers_yaml = (
        os.environ.get("MINI_ORK_PROVIDERS")
        or os.path.join(home, "config", "providers.yaml")
    )
    try:
        registry = providers_mod._load_providers_registry(root)
    except (OSError, ValueError):
        return  # a malformed registry is the dispatcher's to fail on, loudly
    if not registry:
        return
    try:
        runtime = providers_mod.provider_environment()
    except Exception:
        return  # Check 4 already reported the unreadable store it read first

    probed = 0
    unprobed: list[str] = []
    for name, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") not in _PROBEABLE_KINDS:
            continue
        try:
            needed = providers_mod.required_secret_envs(name, root)
        except ValueError:
            continue
        # CLI-owned auth (codex/opencode) declares no env var and is probed
        # anyway; a gateway lane with a missing var is Check 4's finding.
        if any(not runtime.get(var) for var in needed):
            continue
        health = providers_mod.lane_health(name, root, environment=runtime)
        if health.ok:
            # A native lane, or probes switched off: healthy, but nobody
            # actually asked the far end. Count it as verified only if the
            # probe ran — the reason string is where lane_health says which.
            if "unprobed" in health.reason:
                unprobed.append(name)
            else:
                probed += 1
            continue
        findings.error(
            f"lane '{name}' is unhealthy: {health.reason}",
            f"fix the credential in {home}/config/secrets.local.sh, or remove the "
            f"lane from {providers_yaml} — every dispatch on it will fail",
        )

    # "validate: OK" must not imply a liveness check that never ran.
    if probed == 0 and unprobed:
        findings.warn(
            f"lane liveness not verified for {len(unprobed)} lane(s): "
            f"{', '.join(sorted(unprobed))}",
            "set MO_LANE_PROBE=1 to probe live lanes (currently disabled by "
            "MO_LANE_PROBE=0 or MINI_ORK_DRY_RUN=1)",
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI dispatcher — mirrors bash's arg-parsing and exit-code flow exactly.
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    kickoff = ""
    recipe = ""
    recipe_explicit = False
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
            recipe_explicit = True
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
    # Runs on every invocation, after the kickoff checks. The duplicate-class
    # scan is repo-wide; an explicit --recipe scopes validate to one recipe and
    # must not fail on unrelated siblings (see _check_task_class_names).
    _check_task_class_names(root, findings, check_duplicates=not recipe_explicit)
    if recipe:
        _check_recipe(root, recipe, findings)
    _check_agents_yaml(root, home, findings)
    _check_provider_secrets(home, findings)
    _check_lane_liveness(root, home, findings)

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
