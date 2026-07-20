"""Python port of the mini-ork entrypoint / universal-loop dispatcher.

Strangler-fig parity port. Simple subcommands delegate to bin/mini-ork-<sub>
(still bash until ported), except closed forks run as modules; the `run`
recipe-runner walks classify → profile →
plan → execute → rubric → verify → reflect with deadline soft-gates between
stages. The two embedded-python blocks (recipe resolution + run-profile
generation) are transcribed verbatim so their output byte-matches the bash.

    main(argv=None, *, root=None) -> int

Note: bash uses `exec` for the simple subcommands (process replacement); this
port uses subprocess with inherited stdio and returns the child's exit code —
observationally identical (same stdout/stderr, same exit code).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_EXEC_SUBS = {"plan", "execute", "reflect", "improve", "eval",
              "promote", "init", "update", "spawn", "scheduler", "epics", "bugs",
              "inject", "review", "traceotter", "metrics", "rollback", "resume", "recover",
              "serve", "validate", "garden", "recipe-eval"}

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

Recipe runner:
  run <kickoff.md>               Classify kickoff, resolve recipe, then walk
                                   classify → plan → execute → verify
  run <recipe-name> <kickoff.md> Force a recipe, then walk the same lifecycle

Lifecycle:
  init                           Bootstrap project (creates .mini-ork/)
  update                         Apply migrations + report config drift
  doctor                         Check deps + env vars + lib presence
  validate                       Pre-run static checks with Fix: hints
  garden                         Drift detection (collisions, orphans, stale runs)
  recipe-eval                    Static evaluation of recipe definitions
  version

Environment:
  MINI_ORK_HOME   project home dir  (default: .mini-ork/)
  MINI_ORK_DB     sqlite3 state db  (default: $MINI_ORK_HOME/state.db)
  MINI_ORK_DRY_RUN  set to 1 for dry-run mode on all subcommands
"""


def _bin(root, name):
    return os.path.join(root, "bin", f"mini-ork-{name}")


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


def _deadline(root, *args) -> subprocess.CompletedProcess:
    """Shell into lib/deadline_budget.sh (not yet ported) for init/check."""
    lib = os.path.join(root, "lib", "deadline_budget.sh")
    fn = args[0]
    return subprocess.run(["bash", "-c", f'source "{lib}" && {fn} "$@"', "_", *args[1:]],
                          capture_output=True, text=True)


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
            [sys.executable, "-m", "mini_ork.ported.mini_ork_classify", kickoff],
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

    # repo-integrity guard (best-effort bash)
    guard = os.path.join(root, "lib", "repo_integrity_guard.sh")
    if os.path.isfile(guard):
        subprocess.run(["bash", "-c", f'source "{guard}" && repo_integrity_check_and_heal || true'],
                       capture_output=True)

    # ── classify ──
    cl = subprocess.run(
        [sys.executable, "-m", "mini_ork.ported.mini_ork_classify",
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
    cfg = os.path.join(root, "lib", "config_resolve.sh")
    if os.path.isfile(cfg):
        subprocess.run(["bash", "-c", f'source "{cfg}" && mo_snapshot_run_config "$1" || true', "_", run_dir],
                       capture_output=True)
    profile_path = os.path.join(run_dir, "run_profile.json")
    os.environ["MINI_ORK_PROFILE_PATH"] = profile_path

    if deadline and int(deadline) > 0:
        if _deadline(root, "mo_deadline_init", run_id, deadline, run_dir).returncode != 0:
            sys.stderr.write("deadline init failed\n"); return 2

    def _gate(where, artifact="") -> bool:
        if deadline and _deadline(root, "mo_deadline_check", run_id).returncode != 0:
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
    pl = subprocess.run([_bin(root, "plan"), kickoff], capture_output=True, text=True)
    if pl.returncode != 0:
        sys.stderr.write(pl.stderr); return pl.returncode
    sys.stdout.write(pl.stdout)
    plan_path = _grep_kv(pl.stdout, "plan_path")
    os.environ["MINI_ORK_PLAN_PATH"] = plan_path
    if not _gate("plan", plan_path):
        return 0

    # ── execute ── (failures do not exit; verify+reflect still fire)
    ex = subprocess.run([_bin(root, "execute")], capture_output=True, text=True)
    run_rc = ex.returncode
    sys.stdout.write(ex.stdout)
    artifact = _grep_kv(ex.stdout, "artifact_path")
    if artifact:
        os.environ["MINI_ORK_ARTIFACT_PATH"] = artifact
    if deadline and _deadline(root, "mo_deadline_check", run_id).returncode != 0:
        sys.stderr.write(f"deadline_hit after execute; best-so-far artifact: {artifact or '<none>'}\n")
        return run_rc

    _run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if not _run_dir and plan_path:
        _run_dir = os.path.dirname(plan_path)
    if _run_dir and _run_dir != "." and os.path.isdir(_run_dir) \
            and not os.path.isfile(os.path.join(_run_dir, "execute.log")):
        try:
            open(os.path.join(_run_dir, "execute.log"), "w").write(ex.stdout)
        except OSError:
            pass

    # ── rubric pre-screen (advisory, bash) ──
    if os.environ.get("MO_RUBRIC", "1") == "1" and _run_dir and os.path.isdir(_run_dir):
        sys.stdout.write("── rubric (advisory pre-screen) ──\n")
        subprocess.run(["bash", "-c",
            'source "$1/lib/llm-dispatch.sh" 2>/dev/null||true; source "$1/lib/trace_store.sh" 2>/dev/null||true; '
            'source "$1/lib/rubric-prescreen.sh" 2>/dev/null||true; '
            'declare -f mo_rubric_run_score >/dev/null 2>&1 && mo_rubric_run_score "$2" "$3" "$4"; '
            'declare -f mo_grade_run_reward >/dev/null 2>&1 && mo_grade_run_reward "$3" "$5" || true',
            "_", root, kickoff, _run_dir, task_class or "generic", run_id])

    # ── verify ──
    if _run_dir and _run_dir != "." and os.path.isdir(_run_dir):
        os.environ["MINI_ORK_RUN_DIR"] = _run_dir
    vargs = [sys.executable, "-m", "mini_ork.ported.mini_ork_verify"] + ([artifact] if artifact else [])
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
            [sys.executable, "-m", "mini_ork.ported.mini_ork_reflect", "--since", str(t0)],
            capture_output=True,
            env={
                **_module_env(root),
                "MO_REFLECTION_BATCH": os.environ.get("MO_REFLECTION_BATCH", "25"),
            },
        )
    return run_rc


def main(argv=None, *, root=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
        os.path.dirname(os.path.realpath(__file__)))
    os.environ["MINI_ORK_ROOT"] = root
    sub = argv[0] if argv else "help"
    rest = argv[1:]

    if sub == "verify":
        return subprocess.run(
            [sys.executable, "-m", "mini_ork.ported.mini_ork_verify", *rest],
            env=_module_env(root),
        ).returncode
    if sub == "classify":
        return subprocess.run(
            [sys.executable, "-m", "mini_ork.ported.mini_ork_classify", *rest],
            env=_module_env(root),
        ).returncode
    if sub in _EXEC_SUBS:
        return subprocess.run([_bin(root, sub), *rest]).returncode
    if sub == "run":
        return _run_lifecycle(rest, root)
    if sub == "doctor":
        import shutil
        # lib/paths.sh semantics: default PROJECT_HOME is cwd/.mini-ork.
        home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
        print("=== mini-ork doctor ===")
        for d in ("bash", "sqlite3", "jq", "git", "yq", "curl", "claude", "python3"):
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
        print("Lib presence:")
        for lib in ("trace_store", "llm-dispatch", "gate_registry", "group_evolver",
                    "reflection_pipeline", "benchmark_suite", "utility_function",
                    "promotion_gate", "version_registry", "paths"):
            if os.path.isfile(os.path.join(root, "lib", f"{lib}.sh")):
                print(f"  [OK]      lib/{lib}.sh")
            else:
                print(f"  [MISSING] lib/{lib}.sh (P1 in flight?)")
        print("")
        print("Provider preflight:")
        for tool, name in (("claude", "anthropic"), ("codex", "codex")):
            if shutil.which(tool):
                print(f"  [OK]      {name} ({tool} CLI present)")
            else:
                print(f"  [WARN]    {name} ({tool} CLI missing)")
        for env_var, family in (("GLM_API_KEY", "glm"), ("KIMI_API_KEY", "kimi"),
                                ("MINIMAX_API_KEY", "minimax"), ("DEEPSEEK_API_KEY", "deepseek")):
            if os.environ.get(env_var):
                print(f"  [OK]      {family} (${env_var} set)")
            else:
                print(f"  [WARN]    {family} (${env_var} unset)")
        return 0
    if sub == "version":
        print("mini-ork 0.6.0 (universal task loop runtime)")
        return 0
    if sub in ("help", "--help", "-h"):
        sys.stdout.write(_HELP)
        return 0
    sys.stderr.write(f"Unknown subcommand: {sub}. Try: mini-ork help\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
