"""Stateless decision surface — Python port of lib/decision_service.sh.

decide() composes the ported brain modules (lane_router.preferred_lane, the
coalition family-diversity predicate, the reward-summary read, the recursion
hint) into the same JSON contract the bash emits:

    {"route", "coalition_ok", "reward_estimate", "recursion_hint",
     "sample_size", "segment", "code_region"}

Faithful semantics preserved:
  - routing: learned lane (GRPO sample floor >=3) else agents.yaml default —
    cold-start never invents a lane;
  - epsilon-greedy exploration (EPSILON/MO_LEARNING_EPSILON, default 0.10,
    SEED/MO_LEARNING_SEED for determinism) fires ONLY when a learned route
    exists (the rlm-4b cold-start invariant);
  - coalition: slice family-diversity, fail-open;
  - reward: mean reward_g over the slice, falling back to process_reward;
  - recursion_hint: env-driven policy slice.
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3

from mini_ork import lane_router

LANE_TO_FAMILY = {
    "sonnet": "anthropic", "opus": "anthropic",
    "glm": "zhipu", "glm_lens": "zhipu",
    "kimi": "moonshot", "kimi_lens": "moonshot",
    "codex": "openai", "codex_lens": "openai",
    "deepseek": "deepseek", "decomposer": "deepseek",
    "gemini": "google",
    "minimax": "minimax", "minimax_lens": "minimax",
}


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB") or os.environ.get("MO_STORE_DB")
    if env:
        return env
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


def resolve_agents_yaml() -> str:
    """Run-dir-first agents.yaml path (T1.0 precedence), mirrors
    mo_resolve_agents_yaml: $MINI_ORK_RUN_DIR/config -> $MINI_ORK_HOME/config
    -> $MINI_ORK_ROOT/config. Always returns a path."""
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if run_dir:
        cand = os.path.join(run_dir, "config", "agents.yaml")
        if os.path.isfile(cand):
            return cand
    cand = os.path.join(os.environ.get("MINI_ORK_HOME") or ".mini-ork",
                        "config", "agents.yaml")
    if not os.path.isfile(cand):
        cand = os.path.join(os.environ.get("MINI_ORK_ROOT") or ".",
                            "config", "agents.yaml")
    return cand


def _load_lanes(agents_yaml: str) -> dict[str, str]:
    """lanes: mapping from agents.yaml; pyyaml with regex fallback."""
    if not os.path.isfile(agents_yaml):
        return {}
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(open(agents_yaml, encoding="utf-8")) or {}
        return {str(k): str(v) for k, v in (cfg.get("lanes") or {}).items() if v}
    except ImportError:
        lanes: dict[str, str] = {}
        in_lanes = False
        for line in open(agents_yaml, encoding="utf-8"):
            line = line.rstrip()
            if re.match(r"^lanes:\s*$", line):
                in_lanes = True
                continue
            if in_lanes:
                if line and not line.startswith((" ", "\t")):
                    break
                m = re.match(r"^\s+([\w_-]+):\s*([\w_-]+)\s*(#.*)?$", line)
                if m:
                    lanes[m.group(1)] = m.group(2)
        return lanes


def default_lane(node_type: str) -> str:
    """agents.yaml lane for node_type; '' when not configured (never invent)."""
    return _load_lanes(resolve_agents_yaml()).get(node_type, "")


def _explore_route(exploit_route: str, epsilon: float, seed: str,
                   agents_yaml: str) -> str:
    """Epsilon-greedy lane swap — faithful to the bash heredoc: seeded RNG when
    SEED set (int seed if parseable), SystemRandom otherwise; candidates are the
    deduped agents.yaml lane VALUES excluding the exploit route, chosen from the
    sorted list for cross-run stability."""
    epsilon = min(max(epsilon, 0.0), 1.0)
    if seed:
        try:
            rng: random.Random = random.Random(int(seed))
        except ValueError:
            rng = random.Random(seed)
    else:
        rng = random.SystemRandom()
    if rng.random() >= epsilon:
        return exploit_route
    candidates, seen = [], set()
    for v in _load_lanes(agents_yaml).values():
        if not v or v == exploit_route or v in seen:
            continue
        seen.add(v)
        candidates.append(v)
    return rng.choice(sorted(candidates)) if candidates else exploit_route


def coalition_ok(objective_domain: str, task_class: str, node_type: str,
                 db: str) -> bool:
    """Slice family-diversity predicate; fail-open on any schema gap."""
    lane_map = dict(LANE_TO_FAMILY)
    for ay in (os.path.join(os.environ.get("MINI_ORK_HOME", ".mini-ork"),
                            "config", "agents.yaml"),
               os.path.join(os.environ.get("MINI_ORK_ROOT", "."),
                            "config", "agents.yaml")):
        if not os.path.isfile(ay):
            continue
        try:
            import yaml  # type: ignore
            cfg = yaml.safe_load(open(ay, encoding="utf-8")) or {}
            for k, v in (cfg.get("lanes") or {}).items():
                lane_map.setdefault(str(k).lower(), str(v).lower())
        except Exception:
            pass
        break

    def family_of(vid):
        if not vid:
            return "unknown"
        base = vid.split("-")[0].lower()
        return lane_map.get(base, base)

    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute(
            "PRAGMA table_info(execution_traces)").fetchall()}
    except sqlite3.OperationalError:
        con.close()
        return True
    if "agent_version_id" not in cols:
        con.close()
        return True

    sql = "SELECT agent_version_id FROM execution_traces WHERE task_class = ?"
    args: list = [task_class]
    if objective_domain and "objective_domain" in cols:
        sql += " AND objective_domain = ?"
        args.append(objective_domain)
    sql += " AND agent_version_id IS NOT NULL AND agent_version_id <> ''"
    if "node_type" in cols:
        sql += " AND node_type = ?"
        args.append(node_type)
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        con.close()
        return True
    lanes = [r["agent_version_id"] for r in rows if r["agent_version_id"]]

    if not rows and "node_type" not in cols:
        jsql = ("SELECT agent_version_id, verifier_output FROM execution_traces "
                "WHERE task_class = ?")
        jargs: list = [task_class]
        if objective_domain and "objective_domain" in cols:
            jsql += " AND objective_domain = ?"
            jargs.append(objective_domain)
        try:
            lanes = []
            for r in con.execute(jsql, jargs).fetchall():
                try:
                    vo = json.loads(r["verifier_output"] or "{}")
                    if vo.get("node_type") == node_type and r["agent_version_id"]:
                        lanes.append(r["agent_version_id"])
                except Exception:
                    continue
        except sqlite3.OperationalError:
            pass
    con.close()
    families = {family_of(x) for x in lanes}
    return len(lanes) < 2 or len(families) == len(lanes)


def reward_summary(objective_domain: str, task_class: str, node_type: str,
                   db: str) -> tuple[float, int]:
    """(mean, sample_size) over the slice: reward_g preferred, process_reward
    fallback; (0.0, 0) on an empty slice — matches '0.0000|0'."""
    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute(
            "PRAGMA table_info(execution_traces)").fetchall()}
    except sqlite3.OperationalError:
        con.close()
        return 0.0, 0
    primary = "reward_g" if "reward_g" in cols else (
        "process_reward" if "process_reward" in cols else None)
    if not primary:
        con.close()
        return 0.0, 0

    node_type_predicate = ""
    nt_args: list = []
    if "node_type" in cols:
        node_type_predicate = " AND node_type = ?"
        nt_args = [node_type]
    where = ["task_class = ?", f"{primary} IS NOT NULL"]
    args: list = [task_class]
    if objective_domain and "objective_domain" in cols:
        where.append("objective_domain = ?")
        args.append(objective_domain)
    args += nt_args
    try:
        rows = con.execute(
            f"SELECT {primary}, verifier_output FROM execution_traces "
            f"WHERE {' AND '.join(where)}{node_type_predicate}", args).fetchall()
    except sqlite3.OperationalError:
        con.close()
        return 0.0, 0
    if not rows and not node_type_predicate and "verifier_output" in cols:
        jargs: list = [task_class]
        extra = ""
        if objective_domain and "objective_domain" in cols:
            extra = " AND objective_domain = ?"
            jargs.append(objective_domain)
        try:
            rows = con.execute(
                f"SELECT {primary}, verifier_output FROM execution_traces "
                f"WHERE task_class = ?{extra} AND {primary} IS NOT NULL",
                jargs).fetchall()
        except sqlite3.OperationalError:
            rows = []
    con.close()
    vals = []
    for r in rows:
        v = r[primary]
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return 0.0, 0
    return round(sum(vals) / len(vals), 4), len(vals)


def recursion_hint() -> dict:
    """Trimmed recursive-policy slice — env-driven, matching bash."""
    def _b(name, dflt="0"):
        return os.environ.get(name, dflt).lower() in {"1", "true", "yes", "on"}
    return {
        "max_depth": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_DEPTH", "2")),
        "max_children_per_run": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_CHILDREN", "4")),
        "max_total_descendants": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_DESCENDANTS", "16")),
        "max_parallel_children": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_PARALLEL", "4")),
        "default_allow_child_spawn": _b("MINI_ORK_ALLOW_CHILD_SPAWN"),
    }


def decide(node_type: str, task_class: str, objective_domain: str = "",
           segment: str = "default", db: str | None = None) -> dict:
    """The stateless decision surface. Same JSON contract as bash decide."""
    code_region = segment if (segment and segment != "default") else ""
    dbp = _db_path(db)

    learned = lane_router.preferred_lane(
        task_class, node_type, objective_domain, code_region, db=dbp)
    learned_route = learned.split("|")[0] if learned else ""
    route = learned_route or default_lane(node_type)

    if learned_route:
        epsilon_s = os.environ.get("EPSILON",
                                   os.environ.get("MO_LEARNING_EPSILON", "0.10"))
        try:
            epsilon = float(epsilon_s)
        except (TypeError, ValueError):
            epsilon = 0.10
        seed = os.environ.get("SEED", os.environ.get("MO_LEARNING_SEED", ""))
        route = _explore_route(route, epsilon, seed, resolve_agents_yaml())

    ok = coalition_ok(objective_domain, task_class, node_type, dbp)
    mean, sample = reward_summary(objective_domain, task_class, node_type, dbp)
    return {
        "route": route,
        "coalition_ok": ok,
        "reward_estimate": mean,
        "recursion_hint": recursion_hint(),
        "sample_size": sample,
        "segment": segment,
        "code_region": code_region or None,
    }
