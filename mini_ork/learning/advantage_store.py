"""SQLite access for the GRPO lane router — extracted from lane_router (M9, SRP/DIP).

Owns everything ``mini_ork/lane_router.py`` used to do against the database
directly:

  * connection management (path resolution, busy_timeout, Row factory);
  * schema introspection (the ``PRAGMA table_info(execution_traces)`` probe and
    the defensive CREATEs for the advantage tables);
  * reads of the source rows (execution_traces) and the prior advantages
    (agent_performance_memory / lane_domain_advantage / lane_region_advantage /
    lane_slice_baseline) used by the EMA blend;
  * the UPSERTs of freshly computed advantages;
  * the WHERE-clause construction + candidate reads for ``preferred_lane``.

All MATH — recency decay weights, shrinkage, EMA blending, z-score
normalization, UCB ranking — stays in ``mini_ork/lane_router.py`` as pure
functions over the plain data this store passes in and out.

Do NOT merge this with ``mini_ork/learning/writeback.py``: writeback owns the
cli/execute reward-writeback path; this store owns the router's advantage
tables. The SQL here is a VERBATIM move from lane_router so the bash-parity
gates (tests/unit/test_lane_router_py.py) keep passing byte-for-byte.
"""
from __future__ import annotations

import os
import sqlite3


def resolve_db_path(db: str | None) -> str:
    """Resolution order: explicit arg > MINI_ORK_DB/MO_STORE_DB > MINI_ORK_HOME/state.db."""
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB") or os.environ.get("MO_STORE_DB")
    if env:
        return env
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


class AdvantageStore:
    """Connection-owning repository for the lane-router advantage tables.

    Usage: ``store = AdvantageStore(db).open()`` … ``store.close()``, or as a
    context manager (``__exit__`` closes without committing — callers commit
    explicitly, matching the previous inline code).
    """

    def __init__(self, db: str | None = None):
        self.db_path = resolve_db_path(db)
        self._con: sqlite3.Connection | None = None

    # ── connection management ────────────────────────────────────────────────

    def open(self) -> "AdvantageStore":
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA busy_timeout=5000")
        con.row_factory = sqlite3.Row
        self._con = con
        return self

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            self.open()
        assert self._con is not None
        return self._con

    def __enter__(self) -> "AdvantageStore":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def commit(self) -> None:
        self.con.commit()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ── schema introspection + defensive DDL ─────────────────────────────────

    def execution_trace_columns(self) -> set[str]:
        return {row[1] for row in self.con.execute("PRAGMA table_info(execution_traces)").fetchall()}

    def ensure_advantage_tables(self) -> None:
        """CREATE IF NOT EXISTS for the three advantage tables + region index.

        The lane_slice_baseline CREATE is defensive so a recompute against a DB
        where migration 0049 has not yet applied still finds it (D2). The real
        schema lives in db/migrations/0049_lane_advantage_variance.sql.
        """
        self.con.execute("""CREATE TABLE IF NOT EXISTS lane_domain_advantage (
              agent_version_id TEXT NOT NULL, task_class TEXT NOT NULL,
              node_type TEXT NOT NULL DEFAULT '', objective_domain TEXT NOT NULL DEFAULT '',
              relative_advantage REAL NOT NULL DEFAULT 0.0, runs_count INTEGER NOT NULL DEFAULT 0,
              success_count INTEGER NOT NULL DEFAULT 0,
              advantage_var REAL NOT NULL DEFAULT 0.0, advantage_std REAL NOT NULL DEFAULT 0.0,
              z_score_advantage REAL NOT NULL DEFAULT 0.0,
              last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain))""")
        self.con.execute("""CREATE TABLE IF NOT EXISTS lane_region_advantage (
              agent_version_id TEXT NOT NULL, task_class TEXT NOT NULL,
              node_type TEXT NOT NULL DEFAULT '', objective_domain TEXT NOT NULL DEFAULT '',
              code_region TEXT NOT NULL DEFAULT '', relative_advantage REAL NOT NULL DEFAULT 0.0,
              runs_count INTEGER NOT NULL DEFAULT 0, success_count INTEGER NOT NULL DEFAULT 0,
              advantage_var REAL NOT NULL DEFAULT 0.0, advantage_std REAL NOT NULL DEFAULT 0.0,
              z_score_advantage REAL NOT NULL DEFAULT 0.0,
              last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain, code_region))""")
        self.con.execute("""CREATE INDEX IF NOT EXISTS idx_lane_region_adv ON lane_region_advantage(
              task_class, node_type, objective_domain, code_region, relative_advantage DESC)""")
        self.con.execute("""CREATE TABLE IF NOT EXISTS lane_slice_baseline (
              objective_domain TEXT NOT NULL DEFAULT '',
              task_class       TEXT NOT NULL,
              node_type        TEXT NOT NULL DEFAULT '',
              code_region      TEXT NOT NULL DEFAULT '',
              slice_mean       REAL NOT NULL DEFAULT 0.0,
              slice_var        REAL NOT NULL DEFAULT 0.0,
              slice_std        REAL NOT NULL DEFAULT 0.0,
              runs_count       INTEGER NOT NULL DEFAULT 0,
              last_updated     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              PRIMARY KEY (objective_domain, task_class, node_type, code_region))""")

    # ── reads: priors for the EMA blend ──────────────────────────────────────

    def fetch_prior_apm(self) -> dict:
        prior: dict = {}
        try:
            for row in self.con.execute(
                    "SELECT agent_version_id, task_class, relative_advantage "
                    "FROM agent_performance_memory").fetchall():
                prior[(row[0], row[1])] = row[2]
        except Exception:
            pass
        return prior

    def fetch_prior_domain(self) -> dict:
        prior: dict = {}
        try:
            for row in self.con.execute(
                    "SELECT agent_version_id, task_class, node_type, objective_domain, "
                    "relative_advantage FROM lane_domain_advantage").fetchall():
                prior[(row[0], row[1], row[2], row[3])] = row[4]
        except Exception:
            pass
        return prior

    def fetch_prior_region(self) -> dict:
        prior: dict = {}
        try:
            for row in self.con.execute(
                    "SELECT agent_version_id, task_class, node_type, objective_domain, "
                    "code_region, relative_advantage FROM lane_region_advantage").fetchall():
                prior[(row[0], row[1], row[2], row[3], row[4])] = row[5]
        except Exception:
            pass
        return prior

    def fetch_prior_baseline(self) -> dict:
        prior: dict = {}
        try:
            for row in self.con.execute(
                    "SELECT objective_domain, task_class, node_type, code_region, slice_mean, "
                    "slice_var, runs_count FROM lane_slice_baseline").fetchall():
                prior[(row[0], row[1], row[2], row[3])] = (row[4], row[5], row[6])
        except Exception:
            pass
        return prior

    # ── reads: source rows + defect penalties ────────────────────────────────

    def fetch_source_rows(self, since_iso: str) -> list:
        """All reward-bearing execution_traces at/after ``since_iso``.

        Column-expression fallbacks come from the PRAGMA probe so a DB missing
        the newer columns (code_region / created_at / cost_usd) still reads.
        """
        cols = self.execution_trace_columns()
        code_region_expr = "code_region" if "code_region" in cols else "NULL AS code_region"
        ts_expr = "created_at" if "created_at" in cols else "NULL AS created_at"
        cost_expr = "cost_usd" if "cost_usd" in cols else "0.0 AS cost_usd"
        return self.con.execute(f"""
            SELECT objective_domain, task_class, agent_version_id, verifier_output,
                   reward_g, {code_region_expr}, {ts_expr}, {cost_expr}
              FROM execution_traces
             WHERE created_at >= ? AND task_class IS NOT NULL AND task_class <> ''
               AND agent_version_id IS NOT NULL AND agent_version_id <> ''
               AND objective_domain IS NOT NULL AND objective_domain <> ''
               AND reward_g IS NOT NULL""", (since_iso,)).fetchall()

    def has_defect_attributions(self) -> bool:
        try:
            return bool(self.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='defect_attributions' LIMIT 1").fetchone())
        except Exception:
            return False

    def fetch_defect_penalties(self) -> list:
        return self.con.execute(
            "SELECT lane, code_region, task_class, penalty, decay_halflife_days, ts "
            "FROM defect_attributions WHERE penalty IS NOT NULL AND penalty <> 0").fetchall()

    # ── writes: computed advantages (UPSERTs) ────────────────────────────────

    def upsert_slice_baseline(self, objective_domain: str, task_class: str,
                              node_type: str, code_region: str,
                              slice_mean: float, slice_var: float,
                              slice_std: float, runs_count: int) -> None:
        self.con.execute("""INSERT INTO lane_slice_baseline
                (objective_domain, task_class, node_type, code_region,
                 slice_mean, slice_var, slice_std, runs_count, last_updated)
                VALUES (?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(objective_domain, task_class, node_type, code_region)
                DO UPDATE SET slice_mean=excluded.slice_mean,
                slice_var=excluded.slice_var, slice_std=excluded.slice_std,
                runs_count=excluded.runs_count, last_updated=excluded.last_updated""",
                         (objective_domain, task_class, node_type, code_region,
                          slice_mean, slice_var, slice_std, runs_count))

    def upsert_agent_performance(self, agent_version_id: str, role: str, model: str,
                                 task_class: str, runs_count: int, success_count: int,
                                 relative_advantage: float) -> None:
        self.con.execute("""INSERT INTO agent_performance_memory
                (agent_version_id, role, model, task_class, runs_count, success_count,
                 relative_advantage, last_updated)
                VALUES (?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(agent_version_id, task_class) DO UPDATE SET
                role=excluded.role, model=excluded.model, runs_count=excluded.runs_count,
                success_count=excluded.success_count,
                relative_advantage=excluded.relative_advantage, last_updated=excluded.last_updated""",
                         (agent_version_id, role, model, task_class, runs_count, success_count,
                          relative_advantage))

    def upsert_domain_advantage(self, agent_version_id: str, task_class: str,
                                node_type: str, objective_domain: str,
                                relative_advantage: float, runs_count: int,
                                success_count: int, advantage_var: float,
                                advantage_std: float, z_score_advantage: float) -> None:
        self.con.execute("""INSERT INTO lane_domain_advantage
                (agent_version_id, task_class, node_type, objective_domain,
                 relative_advantage, runs_count, success_count,
                 advantage_var, advantage_std, z_score_advantage, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(agent_version_id, task_class, node_type, objective_domain)
                DO UPDATE SET relative_advantage=excluded.relative_advantage,
                runs_count=excluded.runs_count, success_count=excluded.success_count,
                advantage_var=excluded.advantage_var,
                advantage_std=excluded.advantage_std,
                z_score_advantage=excluded.z_score_advantage,
                last_updated=excluded.last_updated""",
                         (agent_version_id, task_class, node_type, objective_domain,
                          relative_advantage, runs_count, success_count,
                          advantage_var, advantage_std, z_score_advantage))

    def upsert_region_advantage(self, agent_version_id: str, task_class: str,
                                node_type: str, objective_domain: str, code_region: str,
                                relative_advantage: float, runs_count: int,
                                success_count: int, advantage_var: float,
                                advantage_std: float, z_score_advantage: float) -> None:
        self.con.execute("""INSERT INTO lane_region_advantage
                (agent_version_id, task_class, node_type, objective_domain, code_region,
                 relative_advantage, runs_count, success_count,
                 advantage_var, advantage_std, z_score_advantage, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(agent_version_id, task_class, node_type, objective_domain, code_region)
                DO UPDATE SET relative_advantage=excluded.relative_advantage,
                runs_count=excluded.runs_count, success_count=excluded.success_count,
                advantage_var=excluded.advantage_var,
                advantage_std=excluded.advantage_std,
                z_score_advantage=excluded.z_score_advantage,
                last_updated=excluded.last_updated""",
                         (agent_version_id, task_class, node_type, objective_domain, code_region,
                          relative_advantage, runs_count, success_count,
                          advantage_var, advantage_std, z_score_advantage))

    # ── reads: preferred_lane candidates (WHERE construction lives here) ─────

    def fetch_region_candidates(self, task_class: str, objective_domain: str,
                                code_region: str, node_type: str,
                                min_samples: int) -> list:
        where = ("task_class=? AND objective_domain=? AND code_region=? AND runs_count>=?")
        params = [task_class, objective_domain, code_region, min_samples]
        if node_type:
            where += " AND node_type=?"
            params.append(node_type)
        return self._fetch_lane_candidates("lane_region_advantage", where, params)

    def fetch_domain_candidates(self, task_class: str, objective_domain: str,
                                node_type: str, min_samples: int) -> list:
        where = "task_class=? AND objective_domain=? AND runs_count>=?"
        params = [task_class, objective_domain, min_samples]
        if node_type:
            where += " AND node_type=?"
            params.append(node_type)
        return self._fetch_lane_candidates("lane_domain_advantage", where, params)

    def _fetch_lane_candidates(self, table: str, where: str, params: list) -> list:
        cur = self.con.execute(
            f"SELECT agent_version_id, printf('%.3f', relative_advantage) AS adv_str, "
            f"runs_count, z_score_advantage "
            f"FROM {table} WHERE {where} "
            f"ORDER BY relative_advantage DESC, runs_count DESC",
            params)
        return cur.fetchall()

    def fetch_global_best(self, task_class: str, node_type: str,
                          min_samples: int):
        """Global (agent_performance_memory) has no per-slice z-score — bandit
        ordering degrades to relative_advantage DESC on the global pool."""
        where = "task_class=? AND runs_count>=?"
        params = [task_class, min_samples]
        if node_type:
            where += " AND (role=? OR model=?)"
            params += [node_type, node_type]
        return self.con.execute(
            f"SELECT agent_version_id, printf('%.3f', relative_advantage), runs_count "
            f"FROM agent_performance_memory WHERE {where} "
            f"ORDER BY relative_advantage DESC, runs_count DESC LIMIT 1", params).fetchone()
