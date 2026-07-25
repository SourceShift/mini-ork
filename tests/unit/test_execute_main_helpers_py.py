"""Unit tests for the execute main() phase helpers (SOLID A5)."""
import os

import mini_ork.cli.execute as ex


def test_parse_defaults_from_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", "/p/plan.json")
    args, rc = ex._parse_execute_argv([])
    assert rc == 0 and args.dry_run is True and args.plan_path == "/p/plan.json"


def test_parse_flags_and_positional():
    args, rc = ex._parse_execute_argv(
        ["p.json", "--node-type", "implementer", "--dispatch-mode", "parallel"])
    assert rc == 0
    assert args.plan_path == "p.json"
    assert args.filter_node_type == "implementer"
    assert args.dispatch_mode_override == "parallel"


def test_parse_unknown_flag_and_extra_positional(capsys):
    assert ex._parse_execute_argv(["--bogus"])[1] == 2
    assert "Unknown flag: --bogus" in capsys.readouterr().err
    assert ex._parse_execute_argv(["a.json", "b.json"])[1] == 2
    assert "Unexpected argument: b.json" in capsys.readouterr().err


def test_parse_help(capsys):
    args, rc = ex._parse_execute_argv(["--help"])
    assert args is None and rc == 0
    assert "Usage: mini-ork execute" in capsys.readouterr().out


def test_resolve_plan_missing_errors(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_WORKFLOW", raising=False)
    monkeypatch.delenv("MINI_ORK_RECOVERY_CLOSURE", raising=False)
    monkeypatch.delenv("MINI_ORK_RECOVERY_FROM", raising=False)
    path, rc = ex._resolve_plan_path("", str(tmp_path), from_node="", recovery_active=False)
    assert rc == 2 and path == ""
    assert "No plan.json found" in capsys.readouterr().err


def test_resolve_plan_newest_wins(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    older = runs / "r1"
    newer = runs / "r2"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "plan.json").write_text("{}")
    (newer / "plan.json").write_text("{}")
    os.utime(older / "plan.json", (1000, 1000))
    os.utime(newer / "plan.json", (2000, 2000))
    monkeypatch.delenv("MINI_ORK_WORKFLOW", raising=False)
    path, rc = ex._resolve_plan_path("", str(tmp_path), from_node="", recovery_active=False)
    assert rc == 0 and path == str(newer / "plan.json")


def test_recovery_filter_requires_context(capsys, monkeypatch):
    monkeypatch.delenv("MINI_ORK_RECOVERY_CLOSURE", raising=False)
    monkeypatch.delenv("MINI_ORK_RECOVERY_FROM", raising=False)
    _, rc = ex._apply_recovery_filter([], from_node="", recovery_active=True,
                                      repair_budget="", workflow="")
    assert rc == 2
    assert "--recovery requires" in capsys.readouterr().err


def test_recovery_filter_narrows_to_closure(monkeypatch):
    monkeypatch.setenv("MINI_ORK_RECOVERY_CLOSURE", "impl review")
    monkeypatch.delenv("MINI_ORK_RECOVERY_FROM", raising=False)
    node_ids = [f"plan{ex._SEP}x", f"impl{ex._SEP}y", f"review{ex._SEP}z"]
    filtered, rc = ex._apply_recovery_filter(
        node_ids, from_node="", recovery_active=False, repair_budget="", workflow="")
    assert rc == 0
    assert [e.split(ex._SEP, 1)[0] for e in filtered] == ["impl", "review"]
    assert os.environ["MINI_ORK_RECOVERY_ACTIVE"] == "1"


def test_recovery_filter_bad_budget(capsys, monkeypatch):
    monkeypatch.setenv("MINI_ORK_RECOVERY_CLOSURE", "impl")
    _, rc = ex._apply_recovery_filter([], from_node="", recovery_active=False,
                                      repair_budget="abc", workflow="")
    assert rc == 2
    assert "must be a positive number" in capsys.readouterr().err
