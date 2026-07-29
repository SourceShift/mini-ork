"""Native mini-ork entrypoint / universal-loop dispatcher.

Subcommands run through native Python modules. The `run` recipe-runner walks
classify → profile → plan → execute → rubric → verify → reflect with deadline
soft-gates between stages. Recipe resolution and profile generation preserve
the golden outputs captured before Bash retirement.

    main(argv=None, *, root=None) -> int

Sibling command forks use subprocess with inherited stdio and return the
child's exit code, preserving the public stdout/stderr/exit-code contract.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mini_ork import trace_store
from mini_ork.dispatch import config_resolve, deadline_budget
from mini_ork.vcs import repo_integrity_guard
from mini_ork.gates import rubric_prescreen

_NATIVE_SUBS = {"apply", "classify", "plan", "verify", "reflect", "garden", "validate"}

# Native module subcommands. Compatibility launchers under bin/ re-exec this
# dispatcher, so there is one public command implementation.
_NATIVE_MODULE_SUBS = {
    "improve": "mini_ork.cli.improve",
    "eval": "mini_ork.cli.eval",
    "promote": "mini_ork.cli.promote",
    "init": "mini_ork.cli.init",
    "update": "mini_ork.cli.update",
    "spawn": "mini_ork.cli.spawn",
    "scheduler": "mini_ork.scheduler",
    "epics": "mini_ork.cli.epics",
    "bugs": "mini_ork.cli.bugs",
    "inject": "mini_ork.cli.inject",
    "review": "mini_ork.pre_push_review",
    "traceotter": "mini_ork.cli.traceotter",
    "metrics": "mini_ork.cli.metrics",
    "rollback": "mini_ork.cli.rollback",
    "resume": "mini_ork.cli.resume",
    "recover": "mini_ork.recovery.planner",
    "serve": "mini_ork.cli.serve",
    "bug-collector": "mini_ork.observability.bug_collector",
    "conductor": "mini_ork.orchestration.conductor",
    "coord": "mini_ork.orchestration.coord",
    "lifetime": "mini_ork.orchestration.lifetime",
    "self-improve": "mini_ork.cli.self_improve",
    "topology": "mini_ork.cli.topology",
    "usage-report": "mini_ork.observability.usage_report",
    "watchdog": "mini_ork.orchestration.watchdog",
}

_HELP = """mini-ork — task operating system for agents (v0.1)

Universal loop subcommands:
  classify <kickoff.md>          Route kickoff to a task_class
  plan <kickoff.md>              Generate plan (decomposition + verifier contract)
  execute [<plan.json>]          Dispatch to workflow nodes (planner/researcher/
                                   implementer/reviewer/verifier/reflector/
                                   publisher/rollback)
  verify <artifact-path>         Run verifiers + gates for the artifact contract
  reflect [--since <timestamp>]  Extract gradients + patterns from recent runs
  improve                        Propose workflow candidates via group_evolver
  eval --candidate <id>          Run benchmark suite against a workflow candidate
  promote --candidate <id>       Promotion gate decision (promote|quarantine)
  apply --task-class <name>      Close the apply loop: pick→materialize→score→gate→
  --target <file>                write|quarantine (IMPL-3, opt-in via MO_APPLY_ENABLED=1)

Recipe runner:
  run <kickoff.md>               Classify kickoff, resolve recipe, then walk
                                   classify → plan → execute → verify
  run <recipe-name> <kickoff.md> Force a recipe, then walk the same lifecycle

Lifecycle:
  init                           Bootstrap project (creates .mini-ork/)
  install                        Install or repair the per-user mini-ork command
  update                         Apply migrations + report config drift
  doctor                         Check deps, environment, and provider preflight
  providers                      Configure or inspect credentials for workflow lanes
  validate                       Pre-run static checks with Fix: hints
  garden                         Drift detection (collisions, orphans, stale runs)
  recipe-eval                    Static evaluation of recipe definitions
  version

Provider credentials:
  providers status <lane>                    Safely show configured or missing credentials
  providers configure <lane>                 Prompt securely to configure one provider lane
  providers configure --workflow <path>      Configure every provider lane used by a workflow
  providers --help                           See automation options; keys never use CLI flags

Environment:
  MINI_ORK_HOME   project home dir  (default: .mini-ork/)
  MINI_ORK_DB     sqlite3 state db  (default: $MINI_ORK_HOME/state.db)
  MINI_ORK_DRY_RUN  set to 1 for dry-run mode on all subcommands
"""


def _module_env(root):
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def resolve_recipe(root: str, task_class: str) -> str:
    """Verbatim transcription of the user-first recipe-resolution python."""
    import yaml
    recipes = os.path.join(root, "recipes")
    if os.path.isdir(recipes):
        for name in sorted(os.listdir(recipes)):
            tc = os.path.join(recipes, name, "task_class.yaml")
            if not os.path.isfile(tc):
                continue
            try:
                with open(tc) as f:
                    data = yaml.safe_load(f) or {}
            except Exception:
                continue
            if (data.get("name") or "").strip() == task_class:
                return name
    fallback = task_class.replace("_", "-")
    if os.path.isdir(os.path.join(recipes, fallback)):
        return fallback
    return ""


def gen_profile(kickoff_path, root, recipe, task_class, profile_path, agents_path) -> dict:
    """Verbatim transcription of the run-profile embedded python. Writes the
    profile.json and returns the same dict (caller prints the key=value lines)."""
    root = Path(root)
    kickoff = Path(kickoff_path)
    profile = Path(profile_path)
    text = kickoff.read_text(encoding="utf-8", errors="replace")

    def section_lines(names):
        wanted = {n.lower() for n in names}
        current = None
        lines = []
        for raw in text.splitlines():
            m = re.match(r"^\s*#{2,6}\s+(.+?)\s*$", raw)
            if m:
                title = m.group(1).strip().lower()
                current = title if any(w in title for w in wanted) else None
                continue
            if current:
                lines.append(raw.rstrip())
        return [line for line in lines if line.strip()]

    def bullets(lines):
        items = []
        for line in lines:
            stripped = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if stripped:
                items.append(stripped)
        return items

    def first_heading():
        for line in text.splitlines():
            m = re.match(r"^\s*#\s+(.+?)\s*$", line)
            if m:
                return m.group(1).strip()
        return kickoff.stem.replace("-", " ").replace("_", " ")

    def load_yaml(path):
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def command_hints():
        patterns = [
            r"\bpnpm\s+(?:test|run\s+test|type-check)\b[^\n`]*",
            r"\bnpm\s+(?:test|run\s+test)\b[^\n`]*",
            r"\bpytest\b[^\n`]*",
            r"\bcargo\s+test\b[^\n`]*",
            r"\bgo\s+test\b[^\n`]*",
            r"\bbash\s+tests/[^\n`]+",
            r"\bmake\s+(?:test|check|verify|smoke|probe|coverage|lint|ci)[A-Za-z0-9_.:-]*(?:\s+&&\s+make\s+[A-Za-z0-9_.:-]+)*[^\n`]*",
            r"\bbash\s+recipes/[^\s`]+\.sh\b[^\n`]*",
            r"\bbash\s+verifiers/[^\s`]+\.sh\b[^\n`]*",
            r"\b\./verifiers/[^\s`]+\.sh\b[^\n`]*",
            r"\bbin/mini-ork(?:-[a-z]+)?\s+(?:verify|run|classify|plan)\b[^\n`]*",
        ]
        found = []
        for pattern in patterns:
            found.extend(m.group(0).strip().rstrip(".") for m in re.finditer(pattern, text, re.I))
        return list(dict.fromkeys(found))

    success = bullets(section_lines(["success", "definition of done", "done when", "acceptance"]))
    scope_allow = bullets(section_lines(["scope allow", "in scope", "scope"]))
    scope_deny = bullets(section_lines(["scope deny", "out of scope", "forbidden"]))
    commands = command_hints()

    def _bullet_only(lines):
        out = []
        for line in lines:
            m = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
            if m:
                out.append(m.group(1).strip())
        return out
    _explicit_verify = _bullet_only(section_lines([
        "verification command", "verification commands", "proof of success"]))
    for _c in _explicit_verify:
        _c = _c.strip()
        if _c.startswith("`") and _c.endswith("`") and len(_c) >= 2:
            _c = _c[1:-1].strip()
        if _c and _c not in commands:
            commands.append(_c)

    task_yaml = load_yaml(root / "recipes" / recipe / "task_class.yaml")
    artifact_yaml = load_yaml(root / "recipes" / recipe / "artifact_contract.yaml")
    agents_yaml = load_yaml(agents_path)

    outputs = artifact_yaml.get("outputs") or []
    if isinstance(outputs, str):
        outputs = [outputs]

    lanes = {}
    if isinstance(agents_yaml.get("lanes"), dict):
        lanes = agents_yaml["lanes"]

    questions = []
    if not success:
        questions.append("What exact success criteria should the verifier use?")
    if not scope_allow:
        questions.append("Which files or directories are explicitly in scope?")
    if not commands:
        questions.append("What command should prove this run succeeded?")
    if task_class == "db_migration":
        questions = [
            "Which database engine and version should this migration target?",
            "Is downtime allowed, and what is the maximum acceptable window?",
            "What exact rollback or backup restore path should the planner assume?",
        ]
    elif task_class == "ui_audit" and len(questions) < 3:
        questions.append("Which target user profile or viewport should the audit prioritize?")

    questions = questions[:3]
    confidence = 0.35
    confidence += 0.15 if success else 0
    confidence += 0.15 if scope_allow else 0
    confidence += 0.15 if commands else 0
    confidence += 0.10 if outputs else 0
    confidence += 0.10 if lanes else 0
    confidence = min(confidence, 0.95)

    high_risk = task_class in {"db_migration", "bdd_first_delivery"}
    status = "ready"
    if questions:
        status = "blocked_profile" if high_risk else "needs_answers"

    data = {
        "schema_version": "1.0",
        "kickoff_path": str(kickoff.resolve()),
        "target_repo": str(Path.cwd().resolve()),
        "recipe": recipe,
        "task_class": task_class,
        "user_goal": first_heading(),
        "success_criteria": success,
        "scope_allow": scope_allow,
        "scope_deny": scope_deny,
        "risk_tolerance": "conservative" if high_risk else "standard",
        "budget_cap_usd": task_yaml.get("budget_cap_usd"),
        "provider_policy": {
            "source": str(Path(agents_path).resolve()),
            "lanes": lanes,
            "env": {"MINI_ORK_PROVIDER_POLICY": str(Path(agents_path).resolve()) if lanes else ""},
        },
        "artifact_destination": outputs,
        "verification_command": commands[:3],
        "human_questions": questions,
        "confidence": round(confidence, 2),
        "profile_status": status,
    }
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data


def _grep_kv(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return ""


def _deadline(root, *args) -> int:
    """Call the native deadline port while retaining the old helper contract."""
    del root
    fn = args[0]
    if fn == "mo_deadline_init":
        return deadline_budget.init(args[1], int(args[2]), args[3])
    if fn == "mo_deadline_check":
        return deadline_budget.check(args[1])
    raise ValueError(f"unsupported deadline function: {fn}")


def _run_lifecycle(argv, root) -> int:
    t0 = int(time.time())
    # ── flag pre-parse: pull --deadline out ──
    deadline = ""
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--deadline":
            if i + 1 >= len(argv):
                sys.stderr.write("--deadline requires <seconds>\n"); return 2
            v = argv[i + 1]
            if not v.isdigit():
                sys.stderr.write(f"--deadline: seconds must be a positive integer (got '{v}')\n"); return 2
            deadline = v; i += 2
        elif a.startswith("--deadline="):
            v = a.split("=", 1)[1]
            if not v.isdigit():
                sys.stderr.write(f"--deadline: seconds must be a positive integer (got '{v}')\n"); return 2
            deadline = v; i += 1
        else:
            rest.append(a); i += 1

    if not rest:
        sys.stderr.write("recipe name or kickoff.md path required\n"); return 2
    first = rest.pop(0)

    if os.path.isfile(first):
        kickoff = first
        probe = subprocess.run(
            [sys.executable, "-m", "mini_ork.cli.classify", kickoff],
            capture_output=True,
            text=True,
            env={**_module_env(root), "MINI_ORK_DRY_RUN": "1"},
        )
        if probe.returncode != 0:
            sys.stderr.write(probe.stderr); return probe.returncode
        probed_class = _grep_kv(probe.stdout, "task_class")
        recipe = resolve_recipe(root, probed_class)
        if not recipe:
            sys.stderr.write(f"could not resolve recipe for task_class={probed_class}\n"); return 2
    else:
        recipe = first
        if not rest:
            sys.stderr.write("kickoff.md path required\n"); return 2
        kickoff = rest.pop(0)
        if (not os.path.isdir(os.path.join(root, "recipes", recipe))
                and os.path.isdir(os.path.join(root, "recipes", recipe.replace("_", "-")))):
            recipe = recipe.replace("_", "-")

    if not os.path.isdir(os.path.join(root, "recipes", recipe)):
        sys.stderr.write(f"no recipe: {recipe} (ls {root}/recipes/)\n"); return 2
    os.environ["MINI_ORK_RECIPE"] = recipe
    os.environ["MINI_ORK_WORKFLOW"] = os.path.join(root, "recipes", recipe, "workflow.yaml")
    if not os.path.isfile(kickoff):
        sys.stderr.write(f"kickoff not found: {kickoff}\n"); return 2
    run_id = os.environ.setdefault("MINI_ORK_RUN_ID", f"run-{int(time.time())}-{os.getpid()}")

    # derived task_class from recipe's task_class.yaml::name
    derived = ""
    tc_yaml = os.path.join(root, "recipes", recipe, "task_class.yaml")
    if os.path.isfile(tc_yaml):
        try:
            import yaml
            derived = ((yaml.safe_load(open(tc_yaml)) or {}).get("name") or "").strip()
        except Exception:
            derived = ""
    if not derived:
        derived = recipe.replace("-", "_")

    # repo-integrity guard (best-effort native side-channel)
    try:
        repo_integrity_guard.check_and_heal()
    except Exception:
        pass

    # ── classify ──
    cl = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.classify",
         "--task-class", derived, kickoff],
        capture_output=True,
        text=True,
        env=_module_env(root),
    )
    if cl.returncode != 0:
        sys.stderr.write(cl.stderr); return cl.returncode
    sys.stdout.write(cl.stdout)
    task_class = _grep_kv(cl.stdout, "task_class")
    os.environ["MINI_ORK_TASK_CLASS"] = task_class

    home = os.environ.setdefault("MINI_ORK_HOME", os.path.join(os.getcwd(), ".mini-ork"))
    run_dir = os.path.join(home, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    try:
        config_resolve.snapshot_run_config(run_dir)
    except Exception:
        pass
    profile_path = os.path.join(run_dir, "run_profile.json")
    os.environ["MINI_ORK_PROFILE_PATH"] = profile_path

    if deadline and int(deadline) > 0:
        if _deadline(root, "mo_deadline_init", run_id, deadline, run_dir) != 0:
            sys.stderr.write("deadline init failed\n"); return 2

    def _gate(where, artifact="") -> bool:
        if deadline and _deadline(root, "mo_deadline_check", run_id) != 0:
            tail = f"; best-so-far artifact: {artifact or '<none>'}" if artifact else \
                   "; no best-so-far artifact yet"
            sys.stderr.write(f"deadline_hit after {where}{tail}; exiting cleanly\n")
            return False
        return True

    if not _gate("classify"):
        return 0

    # ── profile ──
    agents_path = os.path.join(home, "config", "agents.yaml")
    data = gen_profile(kickoff, root, recipe, task_class, profile_path, agents_path)
    sys.stdout.write(f"profile_path={profile_path}\n")
    sys.stdout.write(f"profile_status={data['profile_status']}\n")
    sys.stdout.write(f"profile_confidence={data['confidence']:.2f}\n")
    if data["human_questions"]:
        sys.stdout.write("profile_questions=" + json.dumps(data["human_questions"], separators=(",", ":")) + "\n")
    if os.environ.get("MINI_ORK_PROFILE_STRICT", "0") == "1" and data["profile_status"] == "blocked_profile":
        sys.stderr.write("profile blocked: answer profile_questions before planning\n"); return 2

    if not _gate("profile"):
        return 0

    # ── plan ──
    plan_env = dict(os.environ)
    plan_env["PYTHONPATH"] = root + (os.pathsep + plan_env["PYTHONPATH"]
                                      if plan_env.get("PYTHONPATH") else "")
    pl = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.plan", kickoff],
        capture_output=True, text=True, env=plan_env,
    )
    if pl.returncode != 0:
        sys.stderr.write(pl.stderr); return pl.returncode
    sys.stdout.write(pl.stdout)
    plan_path = _grep_kv(pl.stdout, "plan_path")
    os.environ["MINI_ORK_PLAN_PATH"] = plan_path
    if not _gate("plan", plan_path):
        return 0

    # ── execute ── (failures do not exit; verify+reflect still fire)
    from mini_ork.cli import execute as mini_ork_execute
    execute_stdout = io.StringIO()
    execute_stderr = io.StringIO()
    with contextlib.redirect_stdout(execute_stdout), contextlib.redirect_stderr(execute_stderr):
        run_rc = mini_ork_execute.main([], root=root)
    execute_out = execute_stdout.getvalue()
    execute_err = execute_stderr.getvalue()
    sys.stdout.write(execute_out)
    sys.stderr.write(execute_err)
    artifact = _grep_kv(execute_out, "artifact_path")
    if artifact:
        os.environ["MINI_ORK_ARTIFACT_PATH"] = artifact
    if deadline and _deadline(root, "mo_deadline_check", run_id) != 0:
        sys.stderr.write(f"deadline_hit after execute; best-so-far artifact: {artifact or '<none>'}\n")
        return run_rc

    _run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if not _run_dir and plan_path:
        _run_dir = os.path.dirname(plan_path)
    if _run_dir and _run_dir != "." and os.path.isdir(_run_dir) \
            and not os.path.isfile(os.path.join(_run_dir, "execute.log")):
        try:
            open(os.path.join(_run_dir, "execute.log"), "w").write(execute_out)
        except OSError:
            pass

    # ── rubric pre-screen (advisory, native side-channel) ──
    if _should_run_rubric(_run_dir):
        sys.stdout.write("── rubric (advisory pre-screen) ──\n")
        try:
            # The parity port contains real print() calls; keep the launcher's
            # stdout reserved for lifecycle key/value and verdict output.
            with contextlib.redirect_stdout(io.StringIO()):
                rubric_prescreen.mo_rubric_run_score(
                    kickoff,
                    _run_dir,
                    task_class or "generic",
                    mini_ork_root=root,
                    mini_ork_home=home,
                    mini_ork_db=os.environ.get("MINI_ORK_DB"),
                )
        except Exception:
            pass
        try:
            trace_store.grade_run_reward(
                _run_dir,
                run_id,
                db=os.environ.get("MINI_ORK_DB"),
            )
        except Exception:
            pass

    # ── verify ──
    if _run_dir and _run_dir != "." and os.path.isdir(_run_dir):
        os.environ["MINI_ORK_RUN_DIR"] = _run_dir
    vargs = [sys.executable, "-m", "mini_ork.cli.verify"] + ([artifact] if artifact else [])
    vr = subprocess.run(vargs, capture_output=True, text=True, env=_module_env(root))
    sys.stdout.write(vr.stdout); sys.stderr.write(vr.stderr)
    if run_rc == 0:
        run_rc = vr.returncode
    if not _gate("verify", artifact):
        return run_rc

    # ── reflect (best-effort, Python-sole entrypoint) ──
    if os.environ.get("MO_AUTO_REFLECT", "1") == "1":
        sys.stdout.write("── reflect (auto, since run start) ──\n")
        subprocess.run(
            [sys.executable, "-m", "mini_ork.cli.reflect", "--since", str(t0)],
            capture_output=True,
            env={
                **_module_env(root),
                "MO_REFLECTION_BATCH": os.environ.get("MO_REFLECTION_BATCH", "25"),
            },
        )

    # ── trajectory retention (roadmap Step 2 / A2): best-effort TTL prune of
    # turn_jsonl artifacts. MO_TRAJECTORY_TTL_DAYS=0 disables; never gates.
    try:
        from mini_ork.dispatch import retention as _retention  # noqa: PLC0415
        _pruned = _retention.prune_from_env()
        if _pruned:
            print(f"  [retention] pruned {_pruned} turn_jsonl artifact(s) past TTL")
    except Exception:
        pass
    return run_rc


def _should_run_rubric(run_dir: str) -> bool:
    """Return whether the provider-backed rubric side channel may run.

    A dry run may create plans and local artifacts, but it must not invoke a
    model-provider CLI.  The rubric prescreen shells out to one, so keep it
    out of that lifecycle while preserving the existing MO_RUBRIC opt-out for
    real runs.
    """
    return (
        os.environ.get("MO_RUBRIC", "1") == "1"
        and os.environ.get("MINI_ORK_DRY_RUN", "0") != "1"
        and bool(run_dir)
        and os.path.isdir(run_dir)
    )


def main(argv=None, *, root=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
        os.path.dirname(os.path.realpath(__file__)))
    os.environ["MINI_ORK_ROOT"] = root
    sub = argv[0] if argv else "help"
    rest = argv[1:]

    # Subcommand registry (OCP): adding a subcommand is register_subcommand()
    # — no edit to this dispatcher. Invocation mechanism (python -m subprocess,
    # in-process import, bash entrypoint) is a property of the handler.
    handler = SUBCOMMAND_REGISTRY.get(sub)
    if handler is None:
        sys.stderr.write(f"Unknown subcommand: {sub}. Try: mini-ork help\n")
        return 2
    return handler(rest, root)


# ── Subcommand registry (SOLID M7, OCP) ─────────────────────────────────────
# Handler signature: (rest: list[str], root: str) -> int (exit code).


def _native_module_handler(module: str):
    def _handler(rest, root):
        return subprocess.run(
            [sys.executable, "-m", module, *rest],
            env=_module_env(root),
        ).returncode
    return _handler


def _execute_inprocess(rest, root):
    from mini_ork.cli import execute as mini_ork_execute
    return mini_ork_execute.main(rest, root=root)


def _run_handler(rest, root):
    return _run_lifecycle(rest, root)


def _doctor_handler(rest, root):
    del rest
    import shutil
    # The native path contract defaults project home to cwd/.mini-ork.
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    print("=== mini-ork doctor ===")
    for d in ("sqlite3", "git", "curl", "claude", "codex", "python3"):
        print(f"  [OK]      {d}" if shutil.which(d) else f"  [MISSING] {d}")
    if os.environ.get("MINI_ORK_HOME"):
        print(f"  [OK]      MINI_ORK_HOME={os.environ['MINI_ORK_HOME']}")
    elif home:
        print(f"  [OK]      MINI_ORK_HOME={home}")
    if os.environ.get("MINI_ORK_DB"):
        print(f"  [OK]      MINI_ORK_DB={os.environ['MINI_ORK_DB']}")
    else:
        print("  [WARN]    MINI_ORK_DB unset (default: $MINI_ORK_HOME/state.db)")
    print("")
    print("Provider preflight:")
    for tool, name in (("claude", "anthropic"), ("codex", "codex")):
        if shutil.which(tool):
            print(f"  [OK]      {name} ({tool} CLI present)")
        else:
            print(f"  [WARN]    {name} ({tool} CLI missing)")
    from mini_ork.dispatch.providers import provider_environment
    from mini_ork.dispatch.secrets import SecretStoreError

    try:
        provider_env = provider_environment()
    except SecretStoreError as exc:
        print(f"  [WARN]    local credential store unavailable: {exc}")
        provider_env = dict(os.environ)
    for env_var, family in (("GLM_API_KEY", "glm"), ("KIMI_API_KEY", "kimi"),
                            ("MINIMAX_API_KEY", "minimax"), ("DEEPSEEK_API_KEY", "deepseek")):
        if provider_env.get(env_var):
            print(f"  [OK]      {family} (${env_var} set)")
        else:
            print(f"  [WARN]    {family} (${env_var} unset; run: mini-ork providers configure {family})")
    return 0


def _version_handler(rest, root):
    del rest, root
    print("mini-ork 0.6.0 (universal task loop runtime)")
    return 0


def _help_handler(rest, root):
    del rest, root
    sys.stdout.write(_HELP)
    return 0


def _providers_handler(rest, root):
    from mini_ork.cli import providers
    return providers.main(rest, root=root)


def _install_handler(rest, root):
    from mini_ork.cli import install_command
    return install_command.main(rest, root=root)


def _build_default_registry() -> dict:
    registry = {sub: _native_module_handler(f"mini_ork.cli.{sub}") for sub in _NATIVE_SUBS}
    # "recipe-eval" is native too, but the dash is not importable in a module
    # name — register it explicitly against mini_ork.cli.recipe_eval.
    registry["recipe-eval"] = _native_module_handler("mini_ork.cli.recipe_eval")
    registry["execute"] = _execute_inprocess
    for sub, module in _NATIVE_MODULE_SUBS.items():
        registry[sub] = _native_module_handler(module)
    registry["run"] = _run_handler
    registry["doctor"] = _doctor_handler
    registry["install"] = _install_handler
    registry["providers"] = _providers_handler
    registry["version"] = _version_handler
    for alias in ("help", "--help", "-h"):
        registry[alias] = _help_handler
    return registry


SUBCOMMAND_REGISTRY: dict = _build_default_registry()


def register_subcommand(name: str, handler) -> None:
    """Register (or replace) a subcommand: handler(rest, root) -> exit code."""
    SUBCOMMAND_REGISTRY[name] = handler


if __name__ == "__main__":
    raise SystemExit(main())
