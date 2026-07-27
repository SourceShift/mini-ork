"""Unit tests for mini_ork.observability.blame_attributor.

A temp git repo with two trailer-tagged commits (run R1/opus lines 1-5, R2/sonnet
lines 6-10) + a defect spanning both is blamed by the port; emitted rows are
asserted on semantics (SHA-independent — they carry the trailer run_id + lane).
Plus per-helper tests: validate_penalty, normalize_severity, default_judge,
apportion, and a real DB insert (native init_db bootstrap).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.observability import blame_attributor as ba  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "auth@e",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "auth@e"}


def _g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **ENV})
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    _g(r, "init", "-q", "-b", "main")
    (r / "file.txt").write_text("".join(f"line{i}\n" for i in range(1, 6)))
    _g(r, "add", "-A")
    _g(r, "commit", "-qm", "run1\n\nMini-ork-run-id: R1\nMini-ork-lane: opus")
    (r / "file.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    _g(r, "add", "-A")
    _g(r, "commit", "-qm", "run2\n\nMini-ork-run-id: R2\nMini-ork-lane: sonnet")
    return r


def _defect(tmp_path, **over):
    d = {"file": "file.txt", "ranges": [[1, 3], [7, 9]], "severity": "high",
         "task_class": "code_fix", "code_region": "backend", "defect_report": "boom happened here"}
    d.update(over)
    p = tmp_path / "defect.json"
    p.write_text(json.dumps(d))
    return str(p)


def _norm(rows):
    """Sort by blamed_run_id for stable compare."""
    return sorted(rows, key=lambda r: r["blamed_run_id"])


def test_attribute_dry_run(tmp_path, repo):
    defect = _defect(tmp_path)
    rows = ba.attribute(defect, db="", found_run_id="RF", repo_root=str(repo), dry_run=True)
    rows = _norm(rows)
    # two groups, R1/opus + R2/sonnet, penalties sum to the judge total
    assert [r["blamed_run_id"] for r in rows] == ["R1", "R2"]
    assert [r["lane"] for r in rows] == ["opus", "sonnet"]
    assert all(r["found_run_id"] == "RF" for r in rows)
    assert all(r["severity"] == "high" for r in rows)
    assert all(r["task_class"] == "code_fix" for r in rows)
    assert all(r["code_region"] == "backend" for r in rows)
    total = float(ba.default_judge(json.load(open(defect))))
    assert abs(sum(r["penalty"] for r in rows) - total) < 1e-9
    # ranges [1,3] → 3 lines (R1) and [7,9] → 3 lines (R2): even split
    assert all("penalty" in r for r in rows)
    assert abs(rows[0]["penalty"] - rows[1]["penalty"]) < 1e-6


def test_attribute_insert(tmp_path, repo):
    defect = _defect(tmp_path)
    home = tmp_path / "home" / ".mini-ork"; home.mkdir(parents=True)
    db = str(home / "state.db")
    rc, out, err = init_db(db=db, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"

    ba.attribute(defect, db=db, found_run_id="RF", repo_root=str(repo))

    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT blamed_run_id, lane, code_region, task_class, severity, "
        "ROUND(penalty,6), decay_halflife_days FROM defect_attributions "
        "ORDER BY blamed_run_id"
    ).fetchall()
    con.close()
    assert len(rows) == 2
    assert [(r[0], r[1]) for r in rows] == [("R1", "opus"), ("R2", "sonnet")]
    for _, _, code_region, task_class, severity, penalty, halflife in rows:
        assert code_region == "backend"
        assert task_class == "code_fix"
        assert severity == "high"
        assert penalty < 0
        assert halflife  # decay config populated


@pytest.mark.parametrize("raw", ["-0.5", "0", "-1", "-1.0"])
def test_validate_penalty_accepts_in_range(raw):
    # canonical repr of the parsed float
    assert ba.validate_penalty(raw) == repr(float(raw))


@pytest.mark.parametrize("raw", ["-1.5", "0.5", "abc", "", "--3", "+-2"])
def test_validate_penalty_rejects_out_of_range_or_malformed(raw):
    with pytest.raises(ValueError):
        ba.validate_penalty(raw)


@pytest.mark.parametrize("sev,exp", [
    ("low", "low"), ("medium", "medium"), ("high", "high"), ("critical", "critical"),
    ("bogus", "medium"),
    ("", ""),   # quirk: empty matches the medium-branch but echoes ""
])
def test_normalize_severity(sev, exp):
    assert ba.normalize_severity(sev) == exp


@pytest.mark.parametrize("sev,exp", [
    ("low", "-0.1000"), ("medium", "-0.4000"),
    ("high", "-0.7000"), ("critical", "-0.9500"),
])
def test_default_judge_severity_base_mapping(sev, exp):
    # report length 8 → (8 % 17)/200 - 0.04 == 0.0 adjustment, so the penalty
    # is exactly the severity base.
    d = {"severity": sev, "defect_report": "x" * 8}
    assert ba.default_judge(d) == exp


def test_apportion_line_proportional_split():
    groups = [{"run_id": "R1", "lane": "opus", "line_count": 3},
              {"run_id": "R2", "lane": "sonnet", "line_count": 7}]
    rows = ba.apportion("-0.6", groups)
    # 3/10 → -0.18; the LAST group absorbs the rounding remainder → -0.42
    assert rows == [
        {"run_id": "R1", "lane": "opus", "line_count": 3, "penalty": -0.18},
        {"run_id": "R2", "lane": "sonnet", "line_count": 7, "penalty": -0.42},
    ]
    assert abs(sum(r["penalty"] for r in rows) - (-0.6)) < 1e-9
