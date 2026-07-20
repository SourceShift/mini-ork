"""Parity gate: mini_ork.observability.blame_attributor vs lib/blame_attributor.sh.

A temp git repo with two trailer-tagged commits (run R1/opus lines 1-5, R2/sonnet
lines 6-10) + a defect spanning both is blamed by the LIVE bash entry and the
port; emitted rows are compared (SHA-independent — they carry the trailer run_id
+ lane). Plus per-helper parity: validate_penalty, normalize_severity,
default_judge, apportion, and a real DB insert.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.observability import blame_attributor as ba

SH = REPO / "lib" / "blame_attributor.sh"
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "auth@e",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "auth@e"}


def _g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **ENV})
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _bash(fn, *args, env_extra=None):
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", "-c", f'. "{SH}" && {fn} "$@"', "_", *args],
                          capture_output=True, text=True, env=env)


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
    """Drop nothing but sort by blamed_run_id for stable compare."""
    return sorted(rows, key=lambda r: r["blamed_run_id"])


def test_attribute_dry_run_parity(tmp_path, repo):
    defect = _defect(tmp_path)
    r = _bash("blame_attributor_attribute", defect, "--found-run-id", "RF",
              "--repo-root", str(repo), "--dry-run")
    assert r.returncode == 0, r.stderr
    rows_b = [json.loads(l) for l in r.stdout.splitlines() if l.strip().startswith("{")]
    rows_p = ba.attribute(defect, db="", found_run_id="RF", repo_root=str(repo), dry_run=True)
    assert _norm(rows_b) == _norm(rows_p)
    # sanity: two groups, R1/opus + R2/sonnet, penalties sum to the judge total
    assert {r["blamed_run_id"] for r in rows_p} == {"R1", "R2"}
    assert {r["lane"] for r in rows_p} == {"opus", "sonnet"}
    assert abs(sum(r["penalty"] for r in rows_p) - float(ba.default_judge(json.load(open(defect))))) < 1e-9


def test_attribute_insert_parity(tmp_path, repo):
    defect = _defect(tmp_path)
    dbs = []
    for side in ("b", "p"):
        home = tmp_path / side / ".mini-ork"; home.mkdir(parents=True)
        db = str(home / "state.db")
        subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                       env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                       capture_output=True, text=True, check=True)
        dbs.append(db)
    _bash("blame_attributor_attribute", defect, "--found-run-id", "RF", "--repo-root", str(repo),
          "--db", dbs[0])
    ba.attribute(defect, db=dbs[1], found_run_id="RF", repo_root=str(repo))
    q = ("SELECT blamed_run_id, lane, code_region, task_class, severity, "
         "ROUND(penalty,6), decay_halflife_days FROM defect_attributions ORDER BY blamed_run_id")
    rows_b = subprocess.run(["sqlite3", dbs[0], q], capture_output=True, text=True).stdout
    rows_p = subprocess.run(["sqlite3", dbs[1], q], capture_output=True, text=True).stdout
    assert rows_b == rows_p and rows_b.strip()


@pytest.mark.parametrize("raw,ok", [
    ("-0.5", True), ("0", True), ("-1", True), ("-1.0", True),
    ("-1.5", False), ("0.5", False), ("abc", False), ("", False), ("--3", False), ("+-2", False),
])
def test_validate_penalty_parity(raw, ok):
    r = _bash("_ba_validate_penalty", raw)
    if ok:
        assert r.returncode == 0
        assert r.stdout.strip() == ba.validate_penalty(raw)
    else:
        assert r.returncode != 0
        with pytest.raises(ValueError):
            ba.validate_penalty(raw)


@pytest.mark.parametrize("sev,exp", [
    ("low", "low"), ("medium", "medium"), ("high", "high"), ("critical", "critical"),
    ("bogus", "medium"), ("", ""),   # bash quirk: empty matches medium-branch but echoes ""
])
def test_normalize_severity_parity(sev, exp):
    out = _bash("_ba_normalize_severity", sev).stdout
    assert out == ba.normalize_severity(sev) == exp


def test_default_judge_parity(tmp_path):
    for sev in ("low", "medium", "high", "critical"):
        d = {"severity": sev, "defect_report": "x" * (ord(sev[0]) % 20)}
        p = tmp_path / "d.json"; p.write_text(json.dumps(d))
        out_b = _bash("_ba_default_judge", str(p)).stdout.strip()
        assert out_b == ba.default_judge(d)


def test_apportion_parity(tmp_path):
    groups = [{"run_id": "R1", "lane": "opus", "line_count": 3},
              {"run_id": "R2", "lane": "sonnet", "line_count": 7}]
    jsonl = "\n".join(json.dumps(g) for g in groups)
    out_b = subprocess.run(
        ["bash", "-c", f'. "{SH}"; printf "%s\\n" \'{jsonl}\' | _ba_apportion "-0.6"'],
        capture_output=True, text=True, env={**os.environ, "MINI_ORK_ROOT": str(REPO)}).stdout
    rows_b = [json.loads(l) for l in out_b.splitlines() if l.strip()]
    rows_p = ba.apportion("-0.6", groups)
    assert rows_b == rows_p
    assert abs(sum(r["penalty"] for r in rows_p) - (-0.6)) < 1e-9
