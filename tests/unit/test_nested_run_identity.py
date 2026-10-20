"""RB-04: a nested run must not adopt a live run's identity or profile.

A ``mini-ork run`` started from inside another run (a unit test, an embedding
app) inherited ``MINI_ORK_RUN_ID`` / ``MINI_ORK_RUN_DIR`` and wrote its
``*.dryrun.*`` artifacts — including a regenerated ``run_profile.json`` — into
the LIVE run dir. The parent's declared verification commands then vanished.

Three fixes, one test each:
1. id resolution mints a fresh id when the inherited id's ``.pid`` names a live
   foreign process (and drops the inherited run dir);
2. a dry run writes ``run_profile.dryrun.json`` and never rewrites the live
   ``run_profile.json``;
3. ``MiniOrk._base_env`` drops the caller's run identity (``extra_env`` can put
   it back deliberately).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mini_ork.cli import main as cli_main
from mini_ork.client import MiniOrk
from mini_ork.types import RunRequest

REPO = Path(__file__).resolve().parents[2]
LIVE_RUN_ID = "live-1"
SENTINEL = b"# live run_profile.json sentinel - a nested run must never rewrite this\n"


def _pid_home(tmp_path: Path, pid_text: str | None = None) -> tuple[Path, Path]:
    home = tmp_path / "home"
    live = home / "runs" / LIVE_RUN_ID
    live.mkdir(parents=True)
    (live / "run_profile.json").write_bytes(SENTINEL)
    if pid_text is not None:
        (live / ".pid").write_text(pid_text, encoding="utf-8")
    return home, live


def _scrubbed_env() -> dict[str, str]:
    """No inherited MINI_ORK_*/MO_* binding may reach the nested run."""
    return {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("MINI_ORK_") or k.startswith("MO_"))
    }


def _nested_run(home: Path, marker: Path, kickoff: Path) -> subprocess.CompletedProcess:
    env = _scrubbed_env()
    env.update({
        "PYTHONPATH": str(REPO) + os.pathsep + env.get("PYTHONPATH", ""),
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": str(home / "state.db"),
        "MINI_ORK_RUN_ID": LIVE_RUN_ID,
        "MINI_ORK_RUN_DIR": str(marker),
        "MINI_ORK_DRY_RUN": "1",
        "MINI_ORK_PROFILE_STRICT": "1",
        "MO_WORKSPACE_MODE": "in-place",
        "MO_RUN_ORCHESTRATOR": "0",
    })
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.main", "run", "audit-rb04", str(kickoff)],
        cwd=str(REPO), env=env, text=True, capture_output=True, timeout=150,
    )


def _lifecycle_home(tmp_path: Path, pid_text: str | None) -> tuple[Path, Path, Path]:
    """A tmp home whose ``runs/live-1`` looks live, a marker dir, a kickoff."""
    home, _live = _pid_home(tmp_path, pid_text)
    recipe = home / "recipes" / "audit-rb04"
    recipe.mkdir(parents=True)
    (recipe / "task_class.yaml").write_text("name: db_migration\n", encoding="utf-8")
    (recipe / "workflow.yaml").write_text("nodes: []\n", encoding="utf-8")
    (recipe / "artifact_contract.yaml").write_text("outputs: []\n", encoding="utf-8")
    marker = tmp_path / "inherited-run-dir"
    marker.mkdir()
    (marker / "keep.out").write_text("sentinel\n", encoding="utf-8")
    kickoff = tmp_path / "kickoff.md"
    # Prose only: the db_migration profile blocks, and the strict profile gate
    # ends the run after id resolution + profile write, before any dispatch.
    kickoff.write_text("# Nested identity probe\n\nProse only.\n", encoding="utf-8")
    return home, marker, kickoff


def test_live_pid_owner_mints_fresh_run_id_and_drops_run_dir(tmp_path):
    """A `.pid` naming a live foreign process forces a fresh id + run dir."""
    parent_pid = os.getppid()  # alive for the whole test, never our own pid
    assert parent_pid != os.getpid()
    home, marker, kickoff = _lifecycle_home(tmp_path, f"{parent_pid}\n")
    live = home / "runs" / LIVE_RUN_ID

    proc = _nested_run(home, marker, kickoff)

    assert proc.returncode == 2, proc.stderr  # stopped at the strict profile gate
    assert "profile blocked" in proc.stderr
    match = re.search(
        rf"^\[run\] inherited run id {LIVE_RUN_ID} belongs to live pid (\d+); "
        rf"using (run-\d+-\d+)$",
        proc.stderr, re.MULTILINE,
    )
    assert match is not None, proc.stderr
    named_pid, fresh_id = match.groups()
    assert int(named_pid) == parent_pid
    assert fresh_id != LIVE_RUN_ID

    # The fresh id is used; the live identity files and the inherited run dir
    # are untouched.
    assert (home / "runs" / fresh_id / "run_profile.dryrun.json").is_file()
    assert sorted(p.name for p in (home / "runs").iterdir()) == [LIVE_RUN_ID, fresh_id]
    assert (live / "run_profile.json").read_bytes() == SENTINEL
    assert not (live / "run_profile.dryrun.json").exists()
    assert sorted(p.name for p in marker.iterdir()) == ["keep.out"]


def test_dead_pid_still_adopts_inherited_id(tmp_path):
    """An inherited id whose ``.pid`` is dead is adopted (operator intent)."""
    reaped = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    home, _live = _pid_home(tmp_path, f"{reaped}\n")

    run_id, live_pid = cli_main._resolve_run_id(LIVE_RUN_ID, home, pid=os.getpid())

    assert (run_id, live_pid) == (LIVE_RUN_ID, None)


def test_missing_pid_file_adopts_inherited_id(tmp_path):
    """No ``.pid`` claim at all: the inherited id is adopted."""
    home, _live = _pid_home(tmp_path, None)

    run_id, live_pid = cli_main._resolve_run_id(LIVE_RUN_ID, home, pid=os.getpid())

    assert (run_id, live_pid) == (LIVE_RUN_ID, None)


def test_dry_run_profile_shadows_live_profile(tmp_path, monkeypatch):
    """Under ``MINI_ORK_DRY_RUN=1`` the profile write and reuse read the sibling."""
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    rbase = tmp_path / "engine"
    recipe = rbase / "recipes" / "audit-rb04"
    recipe.mkdir(parents=True)
    (recipe / "task_class.yaml").write_text("name: framework_edit\n", encoding="utf-8")
    (recipe / "artifact_contract.yaml").write_text("outputs: []\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    agents_path = home / "config" / "agents.yaml"
    agents_path.write_text("lanes: {}\n", encoding="utf-8")
    run_dir = home / "runs" / "run-1792092528-89023"
    run_dir.mkdir(parents=True)
    live_profile = run_dir / "run_profile.json"
    live_profile.write_bytes(SENTINEL)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(
        "# Dry-run profile probe\n\n"
        "## Success criteria\n\n- the dry-run profile shadows the live one\n\n"
        "## Verification commands\n\n- echo ok\n\n"
        "## Files in scope\n\n- allow: `mini_ork/cli/main.py`\n",
        encoding="utf-8",
    )
    canonical = str(run_dir / "run_profile.json")

    data = cli_main._gen_profile_or_reuse(
        str(kickoff), str(rbase), "audit-rb04", "framework_edit",
        canonical, str(agents_path), str(rbase),
    )

    dryrun = run_dir / "run_profile.dryrun.json"
    assert cli_main._profile_path(canonical) == str(dryrun)
    assert dryrun.is_file()
    assert live_profile.read_bytes() == SENTINEL
    assert json.loads(dryrun.read_text(encoding="utf-8")).get("recipe") == "audit-rb04"
    assert data.get("recipe") == "audit-rb04"

    # The reuse reader inside the dry run answers from the sibling, never live.
    seeded = {"profile_status": "ready", "answers": {"q": "dryrun"}, "marker": "dryrun"}
    dryrun.write_text(json.dumps(seeded), encoding="utf-8")
    live_profile.write_text(
        json.dumps({"profile_status": "ready", "answers": {"q": "live"},
                    "marker": "live"}),
        encoding="utf-8",
    )
    reused = cli_main._gen_profile_or_reuse(
        str(kickoff), str(rbase), "audit-rb04", "framework_edit",
        canonical, str(agents_path), str(rbase),
    )
    assert reused.get("marker") == "dryrun"


def test_base_env_strips_inherited_run_identity(tmp_path, monkeypatch):
    """The SDK base env drops identity; ``extra_env`` can re-add it deliberately."""
    monkeypatch.setenv("MINI_ORK_RUN_ID", "live-1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "runs" / "live-1"))
    client = MiniOrk(home=tmp_path / "home")

    env = client._base_env(tmp_path)

    assert "MINI_ORK_RUN_ID" not in env
    assert "MINI_ORK_RUN_DIR" not in env
    request = RunRequest(
        kickoff=tmp_path / "kickoff.md",
        extra_env={
            "MINI_ORK_RUN_ID": "wanted-run-id",
            "MINI_ORK_RUN_DIR": str(tmp_path / "runs" / "wanted-run-id"),
        },
    )
    explicit = client._env(tmp_path, request)
    assert explicit["MINI_ORK_RUN_ID"] == "wanted-run-id"
    assert explicit["MINI_ORK_RUN_DIR"] == str(tmp_path / "runs" / "wanted-run-id")
