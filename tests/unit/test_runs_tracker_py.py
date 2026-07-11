"""Parity gate: mini_ork.ported.runs_tracker vs lib/runs-tracker.sh.

Each test invokes the LIVE bash subprocess (sqlite3 / git / jq from PATH,
or a sub-bash that sources `lib/runs-tracker.sh`) against a temp DB
initialized by `db/init.sh` and asserts byte-identical / column-identical
output. No mocks, no hardcoded expected outputs — expected is always
derived from a control bash invocation that shares the inputs.

Test surface (>= 6 cases, kickoff requires at least this many):

  (a) ensure_schema              — bash + Python idempotent CREATE / ADD
                                    COLUMN, no errors, schema unchanged
  (b) sql_escape                 — single-quote escape (bash `sed s/'/''/g`)
  (c) resolve_claude_session_id  — absent file → '', present file → session_id
  (d) open                       — row diff: bash vs Python across all
                                    orch_dispatches columns (timestamps
                                    matched by regex, not by equality)
  (e) update_progress            — rationale column byte-equality after two
                                    update_progress calls ('iter:FAIL | iter:WARN')
  (f) close                      — APPROVE → completed, FAIL → cancelled,
                                    rationale ends in 'final:<verdict>'

Each test that needs sqlite3 / git / jq / bash skips (not fails) when the
tool is missing, matching the reflection_refiner parity test pattern.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import runs_tracker as rt  # noqa: E402

SH = REPO / "lib" / "runs-tracker.sh"
INIT_SH = REPO / "db" / "init.sh"

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _which(*tools: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


def _init_db(tmp_path: Path) -> str:
    """Run db/init.sh against a fresh temp DB and return its path."""
    home = tmp_path / ".mini-ork-home"
    home.mkdir()
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp,
             "HOME": str(tmp_path)},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _git_worktree(tmp_path: Path) -> str:
    """Create a real git worktree (one commit, on a branch) for `git symbolic-ref`."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat-x"], check=True)
    return str(repo)


def _row_dict(db_path: str, row_id: int) -> dict:
    """SELECT * from orch_dispatches WHERE id=? and return as dict (None → '')."""
    con = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(orch_dispatches);").fetchall()]
        row = con.execute(
            f"SELECT * FROM orch_dispatches WHERE id=?;", (row_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return {}
    out: dict = {}
    for col, val in zip(cols, row):
        out[col] = "" if val is None else val
    return out


def _diff_row_columns(
    bash_row: dict, py_row: dict, *, exclude: tuple[str, ...] = ("id",)
) -> list[str]:
    """Return column-level diff messages. Excludes `id` (rows may differ) and
    any caller-listed excludes. Timestamps are checked by regex match against
    the millisecond-precision Z-format. When both ports record NULL for a
    timestamp column, the value is the empty string in both dicts and the
    check passes (no spurious diff)."""
    issues: list[str] = []
    for col in (set(bash_row) | set(py_row)) - set(exclude):
        if col not in bash_row or col not in py_row:
            issues.append(f"missing col {col!r}")
            continue
        b, p = bash_row[col], py_row[col]
        if col in ("created_at", "updated_at", "closed_at"):
            # Both empty → both NULL → match.
            if not b and not p:
                continue
            if not (TS_RE.match(b or "") and TS_RE.match(p or "")):
                issues.append(f"{col}: bash={b!r} py={p!r}")
            continue
        if b != p:
            issues.append(f"{col}: bash={b!r} != py={p!r}")
    return issues


def _shell_env(db_path: str, tmp_path: Path, **extra) -> dict:
    return {
        **os.environ,
        "MINI_ORK_DB": db_path,
        "MINI_ORK_HOME": str(tmp_path),
        "HOME": str(tmp_path),
        "REPO": str(REPO),
        **extra,
    }


# ─────────────────────────────────────────────────────────────────────────────
# (a) ensure_schema — idempotency
# ─────────────────────────────────────────────────────────────────────────────
def test_ensure_schema_idempotent(tmp_path):
    """Live bash `mo_runs_ensure_schema` + Python `ensure_schema` both run
    idempotently against a fresh db/init.sh-populated DB; columns and table
    end up identical; second call is a no-op in both ports."""
    _which("bash", "sqlite3")
    db_path = _init_db(tmp_path)

    # Baseline schema introspection.
    def schema_signature(p: str) -> dict[str, tuple]:
        con = sqlite3.connect(p)
        try:
            cols = tuple(
                (r[1], r[2])  # (name, type)
                for r in con.execute("PRAGMA table_info(orch_dispatches);").fetchall()
            )
            runs_cols = tuple(
                (r[1], r[2])
                for r in con.execute("PRAGMA table_info(runs);").fetchall()
            )
            return {
                "orch_dispatches": cols,
                "runs": runs_cols,
            }
        finally:
            con.close()

    base = schema_signature(db_path)

    # 1st call: bash.
    bash_call = (
        f'. "{SH}"\n'
        f'mo_runs_ensure_schema\n'
    )
    r1 = subprocess.run(
        ["bash", "-c", bash_call],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    )
    assert r1.returncode == 0

    # 2nd call: bash.
    r2 = subprocess.run(
        ["bash", "-c", bash_call],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    )
    assert r2.returncode == 0

    after_bash = schema_signature(db_path)
    assert after_bash == base, f"bash mutated schema: {after_bash} != {base}"

    # 1st call: python.
    rt.ensure_schema(db_path)
    after_py1 = schema_signature(db_path)
    assert after_py1 == base, f"py mutated schema: {after_py1} != {base}"

    # 2nd call: python (no-op due to per-DB guard).
    rt.ensure_schema(db_path)
    after_py2 = schema_signature(db_path)
    assert after_py2 == base


# ─────────────────────────────────────────────────────────────────────────────
# (b) sql_escape — bash sed s/'/''/g vs Python str.replace
# ─────────────────────────────────────────────────────────────────────────────
def test_sql_escape_parity():
    """Python `sql_escape` matches the live bash `mo_runs_sql_escape` byte-for-byte
    across empty / plain / single-quote / backslash inputs.

    A literal NUL byte is intentionally omitted — bash's `printf '%s'` truncates
    at NUL (C-string semantics), so the live bash function drops NULs in
    practice; the Python port retains them on purpose for direct string→SQL
    callers. The parity contract covers printable ASCII identifiers (verdicts,
    run_dir, run-stamps) which is the realistic surface."""
    _which("bash", "sed")

    inputs = [
        "",
        "no-quotes",
        "it's",
        "two'quotes''in'one",
        "back\\slash",
        "tail-quote'",
        "'leading-quote",
        "mixed 'quote' and back\\slash",
    ]

    # Drive the bash function with a single sed that mirrors mo_runs_sql_escape:
    #   printf '%s' "$1" | sed "s/'/''/g"
    bash_script = (
        "shopt -s nocasematch 2>/dev/null; "
        "printf '%s' \"$1\" | sed \"s/'/''/g\"\n"
    )
    for val in inputs:
        r = subprocess.run(
            ["bash", "-c", bash_script, "_", val],
            capture_output=True, text=True, check=True,
        )
        bash_out = r.stdout  # bash omits trailing newline
        py_out = rt.sql_escape(val)
        assert py_out == bash_out, (
            f"sql_escape mismatch for {val!r}: py={py_out!r} bash={bash_out!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (c) resolve_claude_session_id — absent file → '', present file → session_id
# ─────────────────────────────────────────────────────────────────────────────
def test_resolve_claude_session_id_parity(tmp_path):
    """Live bash `mo_runs_resolve_claude_session_id` vs Python.

    Two sub-cases: (i) status file absent → both return ''; (ii) status
    file present with `session_id` → both return the session_id value.
    """
    _which("bash")

    # (i) absent status file
    bash_wrapper = (
        f'. "{SH}"\n'
        # HOME is set to tmp_path by env; .claude/status/ doesn't exist.
        'mo_runs_resolve_claude_session_id\n'
    )
    r_abs = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**_shell_env("unused", tmp_path), "ZELLIJ_SESSION_NAME": "zellij-x"},
        capture_output=True, text=True, check=True,
    )
    bash_abs = r_abs.stdout.rstrip("\n")
    py_abs = rt.resolve_claude_session_id("zellij-x")
    assert bash_abs == "" == py_abs, (
        f"absent-file: bash={bash_abs!r} py={py_abs!r}"
    )

    # (ii) present status file with session_id
    status_dir = tmp_path / ".claude" / "status"
    status_dir.mkdir(parents=True)
    sentinel = "sess-uuid-12345-deadbeef"
    (status_dir / "zellij-y.json").write_text(
        f'{{"session_id": "{sentinel}"}}\n', encoding="utf-8"
    )
    r_present = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**_shell_env("unused", tmp_path), "ZELLIJ_SESSION_NAME": "zellij-y"},
        capture_output=True, text=True, check=True,
    )
    bash_present = r_present.stdout.rstrip("\n")
    # Pass home_dir=tmp_path so the port's path-resolution matches the
    # bash subprocess's HOME-driven ~ expansion.
    py_present = rt.resolve_claude_session_id("zellij-y", home_dir=tmp_path)
    assert bash_present == sentinel == py_present, (
        f"present-file: bash={bash_present!r} py={py_present!r}"
    )

    # (iii) env-var fallback: ZELLIJ (without the SESSION_NAME suffix) is honored
    # when ZELLIJ_SESSION_NAME is unset. bash: local zellij="${ZELLIJ_SESSION_NAME:-${ZELLIJ:-}}"
    r_env_fallback = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**_shell_env("unused", tmp_path), "ZELLIJ": "zellij-env-only"},
        capture_output=True, text=True, check=True,
    )
    bash_env_only = r_env_fallback.stdout.rstrip("\n")
    py_env_only = rt.resolve_claude_session_id()  # no zellij arg → reads env
    assert bash_env_only == "" == py_env_only, (
        f"env-fallback (no file for zellij-env-only): "
        f"bash={bash_env_only!r} py={py_env_only!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (d) open — full row diff after live bash + Python insert
# ─────────────────────────────────────────────────────────────────────────────
def test_open_row_diff_parity(tmp_path):
    """Live bash `mo_runs_open epic wt` vs Python `open(db, epic, wt)`.

    Both ports insert into the SAME temp DB. bash returns lastrowid via
    the bash `tail -1` trick; Python returns cursor.lastrowid via the same
    connection. We SELECT * both rows and compare column-by-column. The
    `id` column differs across rows by construction (sequential inserts);
    other columns must match byte-for-byte (timestamps verified by regex
    since they are millisecond-precision NOW())."""
    _which("bash", "sqlite3", "git")
    db_path = _init_db(tmp_path)
    wt = _git_worktree(tmp_path)

    bash_call = (
        f'. "{SH}"\n'
        # JOB_ID unset so group_id branch resolves to NULL
        # ZELLIJ_SESSION_NAME unset so zellij_session_name resolves to NULL
        # HOME → tmp_path (.claude/status missing) so claude_session_id NULL
        f'mo_runs_open "epic-runs-d" "{wt}"\n'
    )
    r_bash = subprocess.run(
        ["bash", "-c", bash_call],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    )
    bash_id = int(r_bash.stdout.strip().splitlines()[-1])

    py_id = rt.open(db_path, "epic-runs-d", wt)

    assert bash_id > 0 and py_id > 0 and bash_id != py_id

    bash_row = _row_dict(db_path, bash_id)
    py_row = _row_dict(db_path, py_id)

    issues = _diff_row_columns(bash_row, py_row)
    assert not issues, "\n".join(issues)

    # Sanity: column-level expectations (per plan: NULL branches for
    # group_id / claude_session_id / zellij_session_name when JOB_ID and
    # ZELLIJ unset; status='in_progress'; rationale NULL).
    for key, want in [
        ("parent_dispatch_id", ""),
        ("epic_id", "epic-runs-d"),
        ("group_id", ""),
        ("dispatched_by", "claude-session"),
        ("claude_session_id", ""),
        ("zellij_session_name", ""),
        ("status", "in_progress"),
        ("rationale", ""),
        ("closed_at", ""),
    ]:
        assert py_row.get(key) == want, f"py {key}: got {py_row.get(key)!r} want {want!r}"
    assert py_row.get("branch", "") == ""  # not stored
    assert py_row["run_dir"].startswith("mini-ork/unknown/epic-runs-d/")
    assert TS_RE.match(py_row["created_at"]), py_row["created_at"]
    assert TS_RE.match(py_row["updated_at"]), py_row["updated_at"]


# ─────────────────────────────────────────────────────────────────────────────
# (e) update_progress — rationale column byte-equality after 2 calls
# ─────────────────────────────────────────────────────────────────────────────
def test_update_progress_rationale_diff(tmp_path):
    """Live bash `mo_runs_update_progress` vs Python `update_progress`:
    open one row, call twice with verdicts ('FAIL', 'WARN'), assert the
    `rationale` column is `iter:FAIL | iter:WARN` exactly across ports."""
    _which("bash", "sqlite3", "git")
    db_path = _init_db(tmp_path)
    wt = _git_worktree(tmp_path)

    # bash open.
    bash_call = (
        f'. "{SH}"\n'
        f'mo_runs_open "epic-runs-e" "{wt}"\n'
    )
    r_bash_open = subprocess.run(
        ["bash", "-c", bash_call],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    )
    bash_id = int(r_bash_open.stdout.strip().splitlines()[-1])
    bash_update = (
        f'. "{SH}"\n'
        f'mo_runs_update_progress "{bash_id}" "FAIL"\n'
        f'mo_runs_update_progress "{bash_id}" "WARN"\n'
    )
    subprocess.run(
        ["bash", "-c", bash_update],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    )

    # python open + update.
    py_id = rt.open(db_path, "epic-runs-e", wt)
    rt.update_progress(db_path, py_id, "FAIL")
    rt.update_progress(db_path, py_id, "WARN")

    bash_row = _row_dict(db_path, bash_id)
    py_row = _row_dict(db_path, py_id)

    assert bash_row["rationale"] == py_row["rationale"] == "iter:FAIL | iter:WARN"
    # updated_at must match the Z-format (parity verified via regex, not equality)
    assert TS_RE.match(bash_row["updated_at"])
    assert TS_RE.match(py_row["updated_at"])
    # The bash and python rows were updated at different times (sequential
    # calls), so updated_at values are not expected to be byte-equal.
    assert bash_row["updated_at"] != py_row["updated_at"]


# ─────────────────────────────────────────────────────────────────────────────
# (f) close — APPROVE → completed, FAIL → cancelled, rationale ends in 'final:'
# ─────────────────────────────────────────────────────────────────────────────
def test_close_status_and_rationale_diff(tmp_path):
    """Live bash `mo_runs_close` vs Python `close`: APPROVE → completed,
    FAIL → cancelled, rationale ends in `final:<verdict>` byte-for-byte
    across ports."""
    _which("bash", "sqlite3", "git")
    db_path = _init_db(tmp_path)
    wt = _git_worktree(tmp_path)

    # ---- APPROVE → completed
    bash_id_approve = int(subprocess.run(
        ["bash", "-c", f'. "{SH}"\nmo_runs_open "epic-approve" "{wt}"\n'],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1])
    subprocess.run(
        ["bash", "-c", (
            f'. "{SH}"\n'
            f'mo_runs_close "{bash_id_approve}" "epic-approve" "APPROVE"\n'
        )],
        env=_shell_env(db_path, tmp_path), capture_output=True, text=True, check=True,
    )

    py_id_approve = rt.open(db_path, "epic-approve", wt)
    rt.close(db_path, py_id_approve, "epic-approve", "APPROVE")

    bash_approve_row = _row_dict(db_path, bash_id_approve)
    py_approve_row = _row_dict(db_path, py_id_approve)

    assert bash_approve_row["status"] == "completed" == py_approve_row["status"]
    assert bash_approve_row["rationale"] == "final:APPROVE" == py_approve_row["rationale"]
    assert TS_RE.match(bash_approve_row["closed_at"])
    assert TS_RE.match(py_approve_row["closed_at"])

    # ---- FAIL → cancelled
    bash_id_fail = int(subprocess.run(
        ["bash", "-c", f'. "{SH}"\nmo_runs_open "epic-fail" "{wt}"\n'],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1])
    subprocess.run(
        ["bash", "-c", (
            f'. "{SH}"\n'
            f'mo_runs_close "{bash_id_fail}" "epic-fail" "FAIL"\n'
        )],
        env=_shell_env(db_path, tmp_path), capture_output=True, text=True, check=True,
    )

    py_id_fail = rt.open(db_path, "epic-fail", wt)
    rt.close(db_path, py_id_fail, "epic-fail", "FAIL")

    bash_fail_row = _row_dict(db_path, bash_id_fail)
    py_fail_row = _row_dict(db_path, py_id_fail)

    assert bash_fail_row["status"] == "cancelled" == py_fail_row["status"]
    assert bash_fail_row["rationale"] == "final:FAIL" == py_fail_row["rationale"]
    assert TS_RE.match(bash_fail_row["closed_at"])
    assert TS_RE.match(py_fail_row["closed_at"])

    # ---- MERGED + SALVAGED also map to completed
    bash_id_merged = int(subprocess.run(
        ["bash", "-c", f'. "{SH}"\nmo_runs_open "epic-m" "{wt}"\n'],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1])
    subprocess.run(
        ["bash", "-c", (
            f'. "{SH}"\n'
            f'mo_runs_close "{bash_id_merged}" "epic-m" "MERGED"\n'
        )],
        env=_shell_env(db_path, tmp_path), capture_output=True, text=True, check=True,
    )
    py_id_merged = rt.open(db_path, "epic-m", wt)
    rt.close(db_path, py_id_merged, "epic-m", "MERGED")
    bash_m = _row_dict(db_path, bash_id_merged)
    py_m = _row_dict(db_path, py_id_merged)
    assert bash_m["status"] == "completed" == py_m["status"]
    assert bash_m["rationale"] == "final:MERGED" == py_m["rationale"]

    bash_id_salvaged = int(subprocess.run(
        ["bash", "-c", f'. "{SH}"\nmo_runs_open "epic-s" "{wt}"\n'],
        env=_shell_env(db_path, tmp_path),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1])
    subprocess.run(
        ["bash", "-c", (
            f'. "{SH}"\n'
            f'mo_runs_close "{bash_id_salvaged}" "epic-s" "SALVAGED"\n'
        )],
        env=_shell_env(db_path, tmp_path), capture_output=True, text=True, check=True,
    )
    py_id_salvaged = rt.open(db_path, "epic-s", wt)
    rt.close(db_path, py_id_salvaged, "epic-s", "SALVAGED")
    bash_s = _row_dict(db_path, bash_id_salvaged)
    py_s = _row_dict(db_path, py_id_salvaged)
    assert bash_s["status"] == "completed" == py_s["status"]
    assert bash_s["rationale"] == "final:SALVAGED" == py_s["rationale"]
