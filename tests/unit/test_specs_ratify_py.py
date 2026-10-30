"""`mini-ork specs ratify` — list and acknowledge SDD ratification entries.

Hermetic: every run dir is built under pytest's ``tmp_path``; no network, no
provider credentials, and no run-local state the test does not create itself.
"""
from __future__ import annotations

import getpass
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import mini_ork
from mini_ork.cli import specs

FEAT_RECORD = {
    "spec_id": "feat",
    "source_hash": "sha256:x",
    "ratification": [
        {"source_excerpt": "Support CSV export", "reason": "no clause", "acknowledged": False},
        {"source_excerpt": "Must be fast", "reason": "conflict with C2", "acknowledged": False},
    ],
}

ACK_KEYS = {"acknowledged", "acknowledged_by", "acknowledged_at", "acknowledged_note"}

FEAT_LINES = [
    "feat\t0\tno clause\tSupport CSV export",
    "feat\t1\tconflict with C2\tMust be fast",
]


def _write_record(path: Path, record: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def _make_run_dir(tmp_path: Path, records: dict[str, dict] | None = None) -> Path:
    run_dir = tmp_path / "run"
    for name, record in (records or {"feat": FEAT_RECORD}).items():
        _write_record(run_dir / "ratification" / f"{name}.json", record)
    return run_dir


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_run_env(monkeypatch):
    """An in-run key makes an acknowledge refuse; clear them for every test."""
    for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_HOME"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------- --list


def test_list_prints_one_tsv_line_per_unacknowledged_entry(tmp_path, capsys):
    run_dir = _make_run_dir(tmp_path)
    before = (run_dir / "ratification" / "feat.json").read_bytes()

    assert specs.main(["ratify", "feat", "--list", "--run-dir", str(run_dir)]) == 0

    out = capsys.readouterr().out
    assert out.rstrip("\n").split("\n") == FEAT_LINES
    assert (run_dir / "ratification" / "feat.json").read_bytes() == before


def test_list_json_is_an_array_of_spec_index_reason_excerpt(tmp_path, capsys):
    run_dir = _make_run_dir(tmp_path)

    assert specs.main(["ratify", "feat", "--list", "--json", "--run-dir", str(run_dir)]) == 0

    items = json.loads(capsys.readouterr().out)
    assert isinstance(items, list) and len(items) == 2
    for item, expected in zip(items, FEAT_LINES):
        spec_id, index, reason, excerpt = expected.split("\t")
        assert item == {
            "spec_id": spec_id,
            "index": int(index),
            "reason": reason,
            "source_excerpt": excerpt,
        }


def test_list_prints_nothing_when_everything_is_acknowledged(tmp_path, capsys):
    run_dir = _make_run_dir(tmp_path)
    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--all", "--note", "n", "--run-dir", str(run_dir)]
    ) == 0
    capsys.readouterr()

    assert specs.main(["ratify", "feat", "--list", "--run-dir", str(run_dir)]) == 0
    assert capsys.readouterr().out == ""


def test_list_truncates_excerpt_to_120_chars_and_flattens_tabs_newlines(tmp_path, capsys):
    long_excerpt = "line1\tline2\nline3 " + "z" * 200
    record = {
        "spec_id": "feat",
        "source_hash": "sha256:x",
        "ratification": [
            {"source_excerpt": long_excerpt, "reason": "no clause", "acknowledged": False}
        ],
    }
    run_dir = _make_run_dir(tmp_path, {"feat": record})

    assert specs.main(["ratify", "feat", "--list", "--run-dir", str(run_dir)]) == 0

    line = capsys.readouterr().out.rstrip("\n")
    fields = line.split("\t")
    assert len(fields) == 4
    assert fields[3] == "line1 line2 line3 " + "z" * 102


def test_list_spans_every_record_and_skips_acknowledged(tmp_path, capsys):
    other = {
        "spec_id": "other",
        "source_hash": "sha256:y",
        "ratification": [
            {"source_excerpt": "Dark mode", "reason": "no clause", "acknowledged": False}
        ],
    }
    done = {
        "spec_id": "done",
        "source_hash": "sha256:z",
        "ratification": [
            {
                "source_excerpt": "Done thing",
                "reason": "no clause",
                "acknowledged": True,
                "acknowledged_by": "op",
                "acknowledged_at": "2026-10-16T00:00:00Z",
                "acknowledged_note": "ok",
            }
        ],
    }
    run_dir = _make_run_dir(tmp_path, {"feat": FEAT_RECORD, "other": other, "done": done})

    assert specs.main(["ratify", "r", "--list", "--run-dir", str(run_dir)]) == 0

    lines = set(capsys.readouterr().out.rstrip("\n").split("\n"))
    assert set(FEAT_LINES) <= lines
    assert "other\t0\tno clause\tDark mode" in lines
    assert not any(line.startswith("done\t") for line in lines)


def test_list_on_empty_ratification_dir_is_silent_exit_0(tmp_path, capsys):
    run_dir = tmp_path / "run"
    (run_dir / "ratification").mkdir(parents=True)

    assert specs.main(["ratify", "r", "--list", "--run-dir", str(run_dir)]) == 0
    assert capsys.readouterr().out == ""


def test_list_is_not_refused_inside_a_run(tmp_path, monkeypatch, capsys):
    run_dir = _make_run_dir(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_ID", "r1")

    assert specs.main(["ratify", "feat", "--list", "--run-dir", str(run_dir)]) == 0
    assert capsys.readouterr().out.rstrip("\n").split("\n") == FEAT_LINES


# ------------------------------------------------------------- run dir


def test_run_dir_resolution_flag_env_then_default(tmp_path, monkeypatch, capsys):
    home_run = tmp_path / "home" / "runs" / "r9"
    _write_record(home_run / "ratification" / "feat.json", FEAT_RECORD)

    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    assert specs.main(["ratify", "r9", "--list"]) == 0
    assert capsys.readouterr().out.rstrip("\n").split("\n") == FEAT_LINES

    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_record(tmp_path / ".mini-ork" / "runs" / "d1" / "ratification" / "feat.json",
                  FEAT_RECORD)
    assert specs.main(["ratify", "d1", "--list"]) == 0
    assert capsys.readouterr().out.rstrip("\n").split("\n") == FEAT_LINES

    zed = {
        "spec_id": "zed",
        "source_hash": "sha256:q",
        "ratification": [
            {"source_excerpt": "Elsewhere", "reason": "no clause", "acknowledged": False}
        ],
    }
    other_dir = _make_run_dir(tmp_path / "elsewhere", {"zed": zed})
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    assert specs.main(["ratify", "r9", "--list", "--run-dir", str(other_dir)]) == 0
    assert capsys.readouterr().out.rstrip("\n").split("\n") == [
        "zed\t0\tno clause\tElsewhere"]


def test_missing_run_dir_or_missing_ratification_dir_exits_2(tmp_path, capsys):
    absent = tmp_path / "absent"
    assert specs.main(["ratify", "r", "--list", "--run-dir", str(absent)]) == 2
    assert "specs: no ratification records in" in capsys.readouterr().err

    bare = tmp_path / "bare"
    bare.mkdir()
    assert specs.main(["ratify", "r", "--list", "--run-dir", str(bare)]) == 2
    assert "specs: no ratification records in" in capsys.readouterr().err

    assert specs.main(
        ["ratify", "r", "--spec", "feat", "--all", "--note", "n", "--run-dir", str(absent)]
    ) == 2
    assert "specs: no ratification records in" in capsys.readouterr().err


# --------------------------------------------------------- acknowledge


def test_acknowledge_entry0_sets_four_audit_keys_and_leaves_entry1(tmp_path, capsys):
    run_dir = _make_run_dir(tmp_path)
    record_path = run_dir / "ratification" / "feat.json"

    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--entry", "0",
         "--note", "accepted: out of scope v1", "--run-dir", str(run_dir)]
    ) == 0
    capsys.readouterr()

    entry0, entry1 = _load(record_path)["ratification"]
    assert entry0["acknowledged"] is True
    assert entry0["acknowledged_note"] == "accepted: out of scope v1"
    assert entry0["acknowledged_by"] == getpass.getuser()
    stamp = entry0["acknowledged_at"]
    assert isinstance(stamp, str) and stamp.endswith("Z")
    assert datetime.fromisoformat(
        stamp.removesuffix("Z") + "+00:00").utcoffset().total_seconds() == 0
    assert ACK_KEYS <= set(entry0)
    assert entry1 == FEAT_RECORD["ratification"][1]

    assert specs.main(["ratify", "feat", "--list", "--run-dir", str(run_dir)]) == 0
    assert capsys.readouterr().out.rstrip("\n").split("\n") == [FEAT_LINES[1]]


def test_acknowledge_indexes_are_zero_based_and_repeatable(tmp_path, capsys):
    run_dir = _make_run_dir(tmp_path)

    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--entry", "1", "--entry", "0",
         "--note", "n", "--run-dir", str(run_dir)]
    ) == 0

    entry0, entry1 = _load(run_dir / "ratification" / "feat.json")["ratification"]
    assert entry0["acknowledged"] is True
    assert entry1["acknowledged"] is True
    assert capsys.readouterr().out.rstrip("\n") == (
        "ratified 2 entries in feat (0 still unacknowledged)")


def test_acknowledge_all_leaves_nothing_unacknowledged(tmp_path, capsys):
    run_dir = _make_run_dir(tmp_path)

    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--all", "--note", "n", "--run-dir", str(run_dir)]
    ) == 0
    assert capsys.readouterr().out.rstrip("\n") == (
        "ratified 2 entries in feat (0 still unacknowledged)")
    record = _load(run_dir / "ratification" / "feat.json")
    assert all(entry["acknowledged"] is True for entry in record["ratification"])


def test_rerunning_all_leaves_the_earlier_acknowledged_at_untouched(tmp_path, capsys):
    run_dir = _make_run_dir(tmp_path)
    record_path = run_dir / "ratification" / "feat.json"
    argv = ["ratify", "feat", "--spec", "feat", "--all", "--note", "first",
            "--run-dir", str(run_dir)]
    assert specs.main(argv) == 0
    capsys.readouterr()
    first = _load(record_path)["ratification"]

    time.sleep(1.1)
    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--all", "--note", "second",
         "--run-dir", str(run_dir)]
    ) == 0
    capsys.readouterr()

    again = _load(record_path)["ratification"]
    for before, after in zip(first, again):
        assert after["acknowledged_at"] == before["acknowledged_at"]
        assert after["acknowledged_note"] == "first"


def test_ratified_message_reports_singular_and_plural(tmp_path, capsys):
    run_dir = _make_run_dir(tmp_path)
    base = ["ratify", "feat", "--spec", "feat", "--run-dir", str(run_dir)]

    assert specs.main([*base, "--entry", "0", "--note", "n"]) == 0
    assert capsys.readouterr().out.rstrip("\n") == (
        "ratified 1 entry in feat (1 still unacknowledged)")

    fresh = _make_run_dir(tmp_path / "fresh")
    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--all", "--note", "n",
         "--run-dir", str(fresh)]
    ) == 0
    assert capsys.readouterr().out.rstrip("\n") == (
        "ratified 2 entries in feat (0 still unacknowledged)")


def test_out_of_range_entry_exits_2_without_writing(tmp_path):
    run_dir = _make_run_dir(tmp_path)
    record_path = run_dir / "ratification" / "feat.json"
    before = record_path.read_bytes()

    for entries in (["--entry", "5"], ["--entry", "-1"], ["--entry", "0", "--entry", "5"]):
        rc = specs.main(
            ["ratify", "feat", "--spec", "feat", *entries, "--note", "n",
             "--run-dir", str(run_dir)]
        )
        assert rc == 2, entries
        assert record_path.read_bytes() == before
    assert [p.name for p in (run_dir / "ratification").iterdir()] == ["feat.json"]


def test_non_dict_selected_entry_is_an_error_and_writes_nothing(tmp_path):
    record = {
        "spec_id": "feat",
        "source_hash": "sha256:x",
        "ratification": [
            "not-a-dict",
            {"source_excerpt": "Must be fast", "reason": "conflict with C2",
             "acknowledged": False},
        ],
    }
    run_dir = _make_run_dir(tmp_path, {"feat": record})
    record_path = run_dir / "ratification" / "feat.json"
    before = record_path.read_bytes()

    for select in (["--all"], ["--entry", "0"]):
        assert specs.main(
            ["ratify", "feat", "--spec", "feat", *select, "--note", "n",
             "--run-dir", str(run_dir)]
        ) == 2
        assert record_path.read_bytes() == before


def test_blank_or_missing_note_exits_2_without_writing(tmp_path):
    run_dir = _make_run_dir(tmp_path)
    record_path = run_dir / "ratification" / "feat.json"
    before = record_path.read_bytes()

    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--all", "--note", "   ",
         "--run-dir", str(run_dir)]
    ) == 2
    assert record_path.read_bytes() == before

    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--all", "--run-dir", str(run_dir)]
    ) == 2
    assert record_path.read_bytes() == before


def test_missing_or_spec_id_mismatched_record_exits_2(tmp_path):
    mismatched = {"spec_id": "different", "source_hash": "sha256:x", "ratification": []}
    run_dir = _make_run_dir(tmp_path, {"feat": FEAT_RECORD, "feat2": mismatched})
    feat_path = run_dir / "ratification" / "feat.json"
    feat2_path = run_dir / "ratification" / "feat2.json"
    before = (feat_path.read_bytes(), feat2_path.read_bytes())

    assert specs.main(
        ["ratify", "r", "--spec", "other", "--all", "--note", "n", "--run-dir", str(run_dir)]
    ) == 2
    assert specs.main(
        ["ratify", "r", "--spec", "feat2", "--all", "--note", "n", "--run-dir", str(run_dir)]
    ) == 2
    assert (feat_path.read_bytes(), feat2_path.read_bytes()) == before


def test_acknowledge_refuses_inside_a_run(tmp_path, monkeypatch, capsys):
    run_dir = _make_run_dir(tmp_path)
    record_path = run_dir / "ratification" / "feat.json"
    before = record_path.read_bytes()
    argv = ["ratify", "feat", "--spec", "feat", "--all", "--note", "n",
            "--run-dir", str(run_dir)]

    for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR"):
        monkeypatch.setenv(key, "r1" if key == "MINI_ORK_RUN_ID" else str(tmp_path))
        assert specs.main(argv) == 2, key
        assert record_path.read_bytes() == before
        captured = capsys.readouterr()
        assert "refuses inside a run" in captured.err
        assert captured.out == ""
        monkeypatch.delenv(key, raising=False)


# -------------------------------------------------------- write shape


def test_write_preserves_keys_order_indent_and_leaves_no_temp_files(tmp_path):
    run_dir = _make_run_dir(tmp_path)
    record_path = run_dir / "ratification" / "feat.json"

    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--entry", "0", "--note", "n",
         "--run-dir", str(run_dir)]
    ) == 0

    text = record_path.read_text(encoding="utf-8")
    assert text.split("\n")[1] == '  "spec_id": "feat",'
    record = json.loads(text)
    assert list(record.keys()) == ["spec_id", "source_hash", "ratification"]
    entry0 = record["ratification"][0]
    assert list(entry0.keys())[:3] == ["source_excerpt", "reason", "acknowledged"]
    assert ACK_KEYS <= set(entry0)
    assert [p.name for p in (run_dir / "ratification").iterdir()] == ["feat.json"]


def test_never_touches_spec_cards_or_any_other_file(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    _write_record(run_dir / "ratification" / "feat.json", FEAT_RECORD)
    _write_record(run_dir / "spec-cards" / "feat.json", {"spec_id": "feat"})
    (run_dir / "spec-index.json").write_text('{"specs": {}}', encoding="utf-8")
    (run_dir / "notes.txt").write_text("operator notes", encoding="utf-8")

    def snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(run_dir)): p.read_bytes()
            for p in sorted(run_dir.rglob("*")) if p.is_file()
        }

    before = snapshot()
    stray_cwd = tmp_path / "cwd"
    stray_cwd.mkdir()
    monkeypatch.chdir(stray_cwd)

    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--all", "--note", "n", "--run-dir", str(run_dir)]
    ) == 0

    after = snapshot()
    assert set(after) == set(before)
    for name, blob in before.items():
        if name != "ratification/feat.json":
            assert after[name] == blob, name
    assert list(stray_cwd.iterdir()) == []


def test_usage_and_module_docstring_gained_ratify(capsys):
    assert "ratify" in specs._USAGE
    assert specs.__doc__ and "ratify" in specs.__doc__

    assert specs.main(["help"]) == 0
    assert "ratify" in capsys.readouterr().out


def test_ingest_list_lint_behave_exactly_as_before(tmp_path):
    assert specs.main([]) == 2
    assert specs.main(["frobnicate"]) == 2
    assert specs.main(["list", str(tmp_path / "no-such-index.json")]) == 2


# ---------------------------------------------------- gate integration


def test_ratification_check_flips_from_violation_to_clean_after_all(tmp_path):
    verifier_dir = (
        Path(mini_ork.__file__).resolve().parents[1]
        / "recipes" / "spec-driven-delivery" / "verifiers"
    )
    run_dir = _make_run_dir(tmp_path)
    card = {"spec_id": "feat", "source_hash": "sha256:x", "clauses": {}, "acceptance": []}

    saved_path = list(sys.path)
    sys.path.insert(0, str(verifier_dir))
    try:
        loader_spec = importlib.util.spec_from_file_location(
            "w2_spec_gate_ratification_check", verifier_dir / "ratification-check.py")
        gate = importlib.util.module_from_spec(loader_spec)
        loader_spec.loader.exec_module(gate)
    finally:
        sys.path[:] = saved_path
        sys.modules.pop("w2_spec_gate_ratification_check", None)
        sys.modules.pop("_sdd_common", None)

    violations, reclassified = gate.check_ratification(run_dir, card)
    assert len(violations) == 2
    assert all("unacknowledged" in v for v in violations)
    assert reclassified == []

    assert specs.main(
        ["ratify", "feat", "--spec", "feat", "--all", "--note", "gate integration",
         "--run-dir", str(run_dir)]
    ) == 0

    violations, reclassified = gate.check_ratification(run_dir, card)
    assert violations == []
    assert reclassified == []
