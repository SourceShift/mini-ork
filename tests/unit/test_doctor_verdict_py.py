"""doctor is a gate: a verdict, one Next line, and a profile behind it (P13).

`doctor` used to print rows and return 0 whatever it found, so nothing could
branch on it — not the installer, not the IDE Setup page, not a person deciding
whether to run `init`. These tests pin the contract:

    exit 0  ready         nothing is failing
    exit 1  restartable   only a restart fixes the failures (a serve running
                          older code than the tree on disk)
    exit 2  needs_person  a human must act — missing tool, unmigrated home,
                          no lane credential, an unset required profile knob

and the corpus of the report: every check has a status in {ok, warn, fail}, a
warning never changes the verdict, and the output ends with exactly one
`Next: <command>` line when there is a failure.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import doctor  # noqa: E402
from mini_ork import profile as profile_mod  # noqa: E402
from mini_ork.cli import main as cli_main  # noqa: E402


def _ready_home(tmp_path: Path, monkeypatch) -> Path:
    """A home the doctor can call ready: state.db + a matching engine version."""
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE schema_migrations (mini_ork_version TEXT, applied_at TEXT)")
    con.execute("INSERT INTO schema_migrations VALUES ('0.7.0', '2026-01-01')")
    con.commit()
    con.close()
    monkeypatch.setattr(cli_main, "engine_version", lambda root: "0.7.0")
    monkeypatch.setattr(cli_main, "_engine_commit", lambda root: "abc1234")
    return home


def _run(home: Path, env: dict | None = None, **kwargs) -> dict:
    clean = {"MINI_ORK_HOME": str(home), **({"MINIMAX_API_KEY": "x"} if env is None else env)}
    kwargs.setdefault("serve_probe", lambda: None)
    return doctor.run(home=str(home), root=str(REPO), env=clean, **kwargs)


def _status(result: dict, check_id: str) -> str:
    by_id = {check["id"]: check for check in result["checks"]}
    return by_id[check_id]["status"]


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #

def test_a_good_home_is_ready_and_has_no_next_line(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    result = _run(home)
    assert result["verdict"] == "ready"
    assert result["next"] == ""
    assert "Next: " not in doctor.render(result)


def test_a_missing_home_needs_a_person_and_names_init(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    result = _run(tmp_path / "nope")
    assert result["verdict"] == "needs_person"
    assert result["next"] == "mini-ork init"
    assert doctor.EXIT_CODES[result["verdict"]] == 2
    assert _status(result, "home-exists") == "fail"


def test_a_home_without_state_db_needs_init(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    result = _run(home)
    assert result["verdict"] == "needs_person"
    assert result["next"] == "mini-ork init"


def test_no_lane_credential_blocks_and_suggests_providers_configure(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    for key in ("GLM_API_KEY", "KIMI_API_KEY", "MINIMAX_API_KEY",
                "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    result = _run(home, env={"MINI_ORK_HOME": str(home)})
    assert result["verdict"] == "needs_person"
    assert _status(result, "provider-key") == "fail"
    assert result["next"] == "mini-ork providers configure minimax"


def test_version_drift_needs_a_migrate(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_main, "engine_version", lambda root: "0.8.0")
    result = _run(home)
    assert result["verdict"] == "needs_person"
    assert _status(result, "db-version") == "fail"
    assert result["next"] == "mini-ork migrate"


def test_a_stale_serve_is_restartable_not_a_person_task(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    result = doctor.run(home=str(home), root=str(REPO),
                        env={"MINI_ORK_HOME": str(home), "MINIMAX_API_KEY": "x"},
                        serve_probe=lambda: (4242, 0.0))
    assert result["verdict"] == "restartable"
    assert doctor.EXIT_CODES[result["verdict"]] == 1
    assert result["next"] == "restart the server: mini-ork serve"
    assert _status(result, "serve") == "fail"


def test_a_person_failure_outranks_a_restart_even_if_later_in_the_list(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    result = doctor.run(home=str(home), root=str(REPO),
                        env={"MINI_ORK_HOME": str(home)},   # no provider key
                        serve_probe=lambda: (4242, 0.0))
    assert result["verdict"] == "needs_person"
    assert result["next"].startswith("mini-ork providers configure")


def test_warnings_never_change_the_verdict(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    result = _run(home)
    assert any(c["status"] == "warn" for c in result["checks"])
    assert result["verdict"] == "ready"


def test_no_serve_process_is_not_a_failure(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    assert _status(_run(home), "serve") == "ok"


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #

def test_checks_are_well_formed():
    result = doctor.run(home="/nonexistent-doctor-home", root=str(REPO),
                        env={"MINI_ORK_HOME": "/nonexistent-doctor-home"},
                        serve_probe=lambda: None)
    assert result["checks"]
    for check in result["checks"]:
        assert set(check) == {"id", "section", "label", "status", "blocking",
                              "detail", "fix"}
        assert check["status"] in {"ok", "warn", "fail"}
        assert check["detail"]
        if check["status"] != "fail":
            assert check["blocking"] is None
        else:
            assert check["blocking"] in {"person", "restart"}
            assert check["fix"]


def test_render_ends_with_exactly_one_next_line(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    text = doctor.render(_run(home, env={"MINI_ORK_HOME": str(home)}))
    lines = text.rstrip("\n").splitlines()
    assert lines[-1].startswith("Next: ")
    assert text.count("Next: ") == 1
    assert "verdict: needs_person" in text


def test_json_and_exit_code_agree(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = doctor.main(["--json", "--home", str(home)], root=str(REPO))
    doc = json.loads(buf.getvalue())
    assert rc == doctor.EXIT_CODES[doc["verdict"]]


# --------------------------------------------------------------------------- #
# the simple profile
# --------------------------------------------------------------------------- #

def _with_profile(tmp_path: Path, name: str, text: str) -> Path:
    """A throwaway engine root holding one profile file."""
    root = tmp_path / "engine"
    (root / "config").mkdir(parents=True)
    (root / "config" / f"profile.{name}.yaml").write_text(text, encoding="utf-8")
    return root


def test_the_shipped_simple_profile_loads():
    document = profile_mod.load("simple", str(REPO))
    singles, groups = profile_mod.required("simple", str(REPO))
    assert [key for key, _ in singles] == ["MO_TARGET_CWD"]
    assert len(groups) == 1 and "MINIMAX_API_KEY" in groups[0]
    assert document["defaults"]["MO_CODEFIX_CERTIFY"] == "shadow"


def test_profile_fills_only_unset_knobs(tmp_path):
    root = _with_profile(tmp_path, "t", "defaults:\n  A: '1'\n  B: '2'\n")
    env = {"B": "9"}
    applied = profile_mod.fill(env, name="t", root=str(root))
    assert applied == {"A": "1"}
    assert env == {"A": "1", "B": "9"}


def test_an_unknown_profile_fails_closed(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    result = _run(home, profile="does-not-exist")
    assert result["verdict"] == "needs_person"
    assert "not readable" in result["next"] or "create" in result["next"]


def test_a_missing_required_knob_blocks_with_the_export_command(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    root = _with_profile(tmp_path, "p", (
        "required:\n"
        "  all:\n"
        "    MO_TARGET_CWD: lanes run here\n"
        "  any_of:\n"
        "    - [MINIMAX_API_KEY]\n"
        "defaults:\n"
        "  MINI_ORK_MAX_PARALLEL: '2'\n"))
    result = doctor.run(home=str(home), root=str(root), profile="p",
                        env={"MINI_ORK_HOME": str(home), "MINIMAX_API_KEY": "x"},
                        serve_probe=lambda: None)
    assert result["verdict"] == "needs_person"
    assert result["next"].startswith("export MO_TARGET_CWD=")
    assert _status(result, "profile:MO_TARGET_CWD") == "fail"


def test_a_satisfied_profile_is_ready_and_reports_applied_defaults(tmp_path, monkeypatch):
    home = _ready_home(tmp_path, monkeypatch)
    root = _with_profile(tmp_path, "q", (
        "required:\n"
        "  all: [MO_TARGET_CWD]\n"
        "  any_of:\n"
        "    - [MINIMAX_API_KEY]\n"
        "defaults:\n"
        "  MINI_ORK_MAX_PARALLEL: '2'\n"
        "  MO_CODEFIX_CERTIFY: shadow\n"))
    result = doctor.run(home=str(home), root=str(root), profile="q",
                        env={"MINI_ORK_HOME": str(home), "MINIMAX_API_KEY": "x",
                             "MO_TARGET_CWD": "/tmp/proj"},
                        serve_probe=lambda: None)
    assert result["verdict"] == "ready"
    assert result["applied_defaults"] == {
        "MINI_ORK_MAX_PARALLEL": "2", "MO_CODEFIX_CERTIFY": "shadow"}
    assert _status(result, "profile:MO_TARGET_CWD") == "ok"


def test_a_store_persisted_key_satisfies_the_profile_group(tmp_path, monkeypatch):
    """The key can live in secrets.local.sh rather than the environment."""
    home = _ready_home(tmp_path, monkeypatch)
    (home / "config").mkdir()
    store = home / "config" / "secrets.local.sh"
    store.write_text("export MINIMAX_API_KEY=stored\n", encoding="utf-8")
    store.chmod(0o600)   # a group/world-readable store fails closed by design
    root = _with_profile(tmp_path, "r", (
        "required:\n"
        "  all: [MO_TARGET_CWD]\n"
        "  any_of:\n"
        "    - [MINIMAX_API_KEY]\n"))
    result = doctor.run(home=str(home), root=str(root), profile="r",
                        env={"MINI_ORK_HOME": str(home), "MO_TARGET_CWD": "/tmp/p"},
                        serve_probe=lambda: None)
    assert _status(result, "profile:MINIMAX_API_KEY") == "ok"


def test_the_profile_is_filled_through_run_context_apply(tmp_path, monkeypatch):
    root = _with_profile(tmp_path, "s", "defaults:\n  MO_SUITE_ADEQUACY: '1'\n")
    from mini_ork.context import RunContext

    monkeypatch.delenv("MO_SUITE_ADEQUACY", raising=False)
    env = {"MINI_ORK_PROFILE": "s"}
    RunContext(root=str(root)).apply(env=env)
    assert env["MO_SUITE_ADEQUACY"] == "1"


def test_apply_raises_on_a_requested_but_missing_profile(tmp_path):
    from mini_ork.context import RunContext

    with pytest.raises(profile_mod.ProfileError):
        RunContext(root=str(tmp_path / "empty")).apply(
            env={"MINI_ORK_PROFILE": "missing"})
