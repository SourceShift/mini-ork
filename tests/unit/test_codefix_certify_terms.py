"""Unit tests for `mini_ork/certify/codefix_terms.py` — the certify terms
(relations ① + differential ②) as run inside the code-fix test verifier.

Hermetic: temp dirs and repos, a fake dispatch only. No network, no lane call,
no docker.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork.certify.llm as llm_mod
from mini_ork.certify import codefix_terms, differential, relations
from mini_ork.certify.verdict import REFUTED

BASE_MED = "def median(xs):\n    s = sorted(xs)\n    return s[len(s) // 2]\n"
CHEAT_MED = (
    "def median(xs):\n    if xs == [1, 2, 3, 4]:\n        return 2.5\n"
    "    s = sorted(xs)\n    return s[len(s) // 2]\n"
)
AVG_MED = (
    "def median(xs):\n    s = sorted(xs)\n    n = len(s)\n"
    "    return (s[n // 2 - 1] + s[n // 2]) / 2\n"
)
GOOD_MED = (
    "def median(xs):\n    s = sorted(xs)\n    n = len(s)\n"
    "    if n % 2 == 0:\n        return (s[n // 2 - 1] + s[n // 2]) / 2\n"
    "    return s[n // 2]\n"
)
POC = "from med import median\ndef test_even():\n    assert median([1, 2, 3, 4]) == 2.5\n"
REL = (
    "from med import median\nSOURCE = [1, 2, 3, 4]\nFOLLOWUP = [4, 3, 2, 1]\n"
    'TRANSFORM = "reverse the list"\nRELATION = "median is unchanged"\n'
    "def test_relation():\n    assert median(SOURCE) == median(FOLLOWUP)\n"
)
SUITE = (
    "from med import median\nCASES = [\n    (\"bug_domain\", [1, 2, 3, 4]),\n"
    "    (\"preserve\", [1, 2, 3]),\n    (\"preserve\", [5]),\n"
    "    (\"preserve\", [3, 1, 2]),\n]\ndef observe(x):\n    return median(x)\n"
)

NINE_KEYS = {"mode", "status", "reason", "poc_file", "relations", "differential",
             "verdict", "vetoed_by", "would_block"}


def _fake_dispatch(prompt):
    if "METAMORPHIC RELATIONS" in prompt:
        return "```python\n" + REL + "```"
    if "DIFFERENTIAL INPUT SUITE" in prompt:
        return "```python\n" + SUITE + "```"
    return ""


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
             "GIT_CONFIG_SYSTEM": os.devnull},
    )


def _make_dirs(tmp_path, candidate_text):
    base = tmp_path / "base"
    cand = tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    _write(base / "med.py", BASE_MED)
    _write(cand / "med.py", candidate_text)
    return str(base), str(cand)


def _make_repo(tmp_path, candidate_text=None):
    """Commit base med.py + POC, then overwrite med.py, uncommitted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "med.py", BASE_MED)
    _write(repo / "test_med.py", POC)
    _git(repo, "init", "-q")
    _git(repo, "add", "med.py", "test_med.py")
    _git(repo, "commit", "-qm", "base")
    if candidate_text is not None:
        _write(repo / "med.py", candidate_text)
    base = tmp_path / "baseonly"
    base.mkdir()
    _write(base / "med.py", BASE_MED)
    return str(repo), str(base)


def _listing(root):
    out = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            out.add(os.path.relpath(os.path.join(dirpath, name), root))
    return out


# ── mode ─────────────────────────────────────────────────────────────────────

def test_mode_env_constant():
    assert codefix_terms.MODE_ENV == "MO_CODEFIX_CERTIFY"


@pytest.mark.parametrize("raw,expected", [
    ("", "shadow"), ("shadow", "shadow"), ("bogus", "shadow"),
    ("1", "gate"), ("gate", "gate"), ("GATE", "gate"),
    ("0", "off"), ("off", "off"), ("OFF", "off"),
    ("false", "off"), ("no", "off"), (" off ", "off"), (" gate ", "gate"),
])
def test_mode_parses(raw, expected):
    assert codefix_terms.mode({"MO_CODEFIX_CERTIFY": raw}) == expected


def test_mode_unset_and_os_environ(monkeypatch):
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    assert codefix_terms.mode() == "shadow"
    monkeypatch.setenv("MO_CODEFIX_CERTIFY", "0")
    assert codefix_terms.mode() == "off"
    monkeypatch.setenv("MO_CODEFIX_CERTIFY", "gate")
    assert codefix_terms.mode() == "gate"
    assert codefix_terms.mode({"MO_CODEFIX_CERTIFY": "shadow"}) == "shadow"


# ── poc_from_replay ──────────────────────────────────────────────────────────

def test_poc_prefers_strong_overlap(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    _write(cand / "a.py", "A = 1\n")
    _write(cand / "b.py", "B = 2\n")
    replay = {"strong_overlap": ["a.py::t1"], "overlap": ["b.py::t2"]}
    assert codefix_terms.poc_from_replay(replay, str(cand)) == ("A = 1\n", "a.py", "")


def test_poc_falls_through_to_overlap(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    _write(cand / "b.py", "B = 2\n")
    replay = {"strong_overlap": ["x.py::<suite load failure>"],
              "overlap": ["b.py::t2"]}
    assert codefix_terms.poc_from_replay(replay, str(cand)) == ("B = 2\n", "b.py", "")


def test_poc_skips_disqualified_ids(tmp_path):
    cand = tmp_path / "cand"
    (cand / "src").mkdir(parents=True)
    _write(cand / "x.py", "X = 1\n")
    outside = _write(tmp_path / "outside.py", "OUT = 1\n")
    replay = {"strong_overlap": [
        "x.py::<suite load failure>",
        "src/a.test.ts::t",
        "../outside.py::t",
        outside + "::t",
        "ghost.py::t",
    ]}
    assert codefix_terms.poc_from_replay(replay, str(cand)) == (
        None, None, "no python fail-to-pass test in replay overlap")


def test_poc_none_triple_non_dict_and_empty(tmp_path):
    cand = str(tmp_path / "cand")
    triple = (None, None, "no python fail-to-pass test in replay overlap")
    assert codefix_terms.poc_from_replay(None, cand) == triple
    assert codefix_terms.poc_from_replay("not-a-dict", cand) == triple
    assert codefix_terms.poc_from_replay(42, cand) == triple
    assert codefix_terms.poc_from_replay(["cand.py::t"], cand) == triple
    assert codefix_terms.poc_from_replay({}, cand) == triple


def test_poc_clips_and_decodes(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    _write(cand / "big.py", "x" * 10000)
    text, rel, why = codefix_terms.poc_from_replay(
        {"strong_overlap": ["big.py::t"]}, str(cand))
    assert len(text) == 8000 and rel == "big.py" and why == ""
    text2, _, _ = codefix_terms.poc_from_replay(
        {"strong_overlap": ["big.py::t"]}, str(cand), max_chars=10)
    assert len(text2) == 10
    (cand / "enc.py").write_bytes(b"valid = 1\n\xff\xfeinvalid\n")
    text3, _, _ = codefix_terms.poc_from_replay(
        {"strong_overlap": ["enc.py::t"]}, str(cand))
    assert "valid = 1" in text3 and "�" in text3


# ── LocalTreeRunner ──────────────────────────────────────────────────────────

def test_runner_base_failed_candidate_passed(tmp_path):
    base, cand = _make_dirs(tmp_path, GOOD_MED)
    runner = codefix_terms.LocalTreeRunner(base, cand)
    res_base = runner.run_in(base, POC)
    assert res_base.status == "failed"
    assert "AssertionError" in (res_base.exc or "")
    assert runner.run_test(POC).status == "failed"
    assert runner.run_test(POC, patch="   ").status == "failed"
    assert runner.run_test(POC, patch="x").status == "passed"


def test_runner_test_defect(tmp_path):
    base, cand = _make_dirs(tmp_path, GOOD_MED)
    res = codefix_terms.LocalTreeRunner(base, cand).run_in(
        base, "def test_boom():\n    return missing_thing\n")
    assert res.status == "test_defect"


def test_runner_up_false_when_dir_missing(tmp_path):
    base, cand = _make_dirs(tmp_path, GOOD_MED)
    assert codefix_terms.LocalTreeRunner(base, cand).up is True
    assert codefix_terms.LocalTreeRunner(base, str(tmp_path / "no")).up is False
    assert codefix_terms.LocalTreeRunner(str(tmp_path / "no"), cand).up is False


def test_runner_timeout(tmp_path):
    base, cand = _make_dirs(tmp_path, GOOD_MED)
    res = codefix_terms.LocalTreeRunner(base, cand, timeout_s=1).run_in(
        base, "import time\ndef test_t():\n    time.sleep(5)\n")
    assert (res.status, res.ran, res.output, res.exc, res.backend) == (
        "error", False, "timeout", "Timeout", "local-tree")


def test_runner_oserror_no_run(tmp_path):
    base, cand = _make_dirs(tmp_path, GOOD_MED)
    res = codefix_terms.LocalTreeRunner(
        base, cand, python="/nonexistent/python-zz").run_in(base, POC)
    assert res.status == "no_run" and res.backend == "local-tree"
    assert "/nonexistent/python-zz" in res.output


def test_runner_never_writes_in_tree(tmp_path):
    base, cand = _make_dirs(tmp_path, GOOD_MED)
    before = (_listing(base), _listing(cand))
    runner = codefix_terms.LocalTreeRunner(base, cand)
    for tree in (base, cand):
        runner.run_in(tree, POC)
        runner.run_in(tree, REL)
    assert (_listing(base), _listing(cand)) == before
    assert not (Path(cand) / "crucible_probe.py").exists()
    assert not (Path(base) / "__pycache__").exists()
    assert not (Path(cand) / ".pytest_cache").exists()


def _env_probe_src():
    return (
        "import json, os\n"
        "with open(os.environ['SPEC_OUT'], 'w', encoding='utf-8') as fh:\n"
        "    json.dump({'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "              'dontwrite': os.environ.get('PYTHONDONTWRITEBYTECODE'),\n"
        "              'addopts': os.environ.get('PYTEST_ADDOPTS'),\n"
        "              'mo_keys': sorted(k for k in os.environ"
        " if k.startswith('MO_'))}, fh)\n"
        "def test_probe():\n    assert True\n"
    )


def test_runner_env_scrubs_and_sets(tmp_path, monkeypatch):
    base, cand = _make_dirs(tmp_path, GOOD_MED)
    out = tmp_path / "env.json"
    monkeypatch.setenv("SPEC_OUT", str(out))
    monkeypatch.setenv("MO_SPEC_SENTINEL", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--spec-marker")
    monkeypatch.setenv("PYTHONPATH", "/spec-prev-marker")
    codefix_terms.LocalTreeRunner(base, cand).run_in(base, _env_probe_src())
    child = __import__("json").loads(out.read_text(encoding="utf-8"))
    assert child["dontwrite"] == "1"
    assert child["addopts"] is None
    assert child["mo_keys"] == []
    assert child["pythonpath"] == base + os.pathsep + "/spec-prev-marker"


# ── run_terms ────────────────────────────────────────────────────────────────

REL_REFUTED = {
    "verdict": REFUTED, "reason": "relation broken", "src": "REL-SRC",
    "records": [{"name": "rel_1", "status": "violated", "src": "r-src",
                 "detail": "r-detail"}],
    "counts": {"violated": 1},
}
REL_HELD = {
    "verdict": None, "reason": "", "src": "REL-SRC",
    "records": [{"name": "rel_1", "status": "held", "src": "r-src",
                 "detail": "r-detail"}],
    "counts": {"held": 1},
}
DIF_REFUTED = {
    "verdict": REFUTED, "reason": "preserve diverged", "src": "DIF-SRC",
    "state": "refuted", "records": [], "counts": {"preserve_diverged": 2},
}
DIF_EQ = {
    "verdict": None, "reason": "", "src": "DIF-SRC", "state": "equivalent",
    "records": [], "counts": {},
}


@pytest.fixture
def terms_env(monkeypatch):
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")


def _stub_runner():
    import types
    return types.SimpleNamespace(up=True)


def test_run_terms_slims_records(monkeypatch, terms_env):
    monkeypatch.setattr(relations, "check", lambda *a, **k: dict(REL_REFUTED))
    monkeypatch.setattr(differential, "check", lambda *a, **k: dict(DIF_EQ))
    res = codefix_terms.run_terms(issue="i", patch="p", poc="poc",
                                  runner=_stub_runner(), dispatch=_fake_dispatch)
    assert "src" not in res["relations"]
    for record in res["relations"]["records"]:
        assert "src" not in record and "detail" not in record
    assert "src" not in res["differential"]
    assert res["verdict"] == REFUTED and res["vetoed_by"] == ["relations"]


def test_run_terms_both_vetoed_in_order(monkeypatch, terms_env):
    monkeypatch.setattr(relations, "check", lambda *a, **k: dict(REL_REFUTED))
    monkeypatch.setattr(differential, "check", lambda *a, **k: dict(DIF_REFUTED))
    res = codefix_terms.run_terms(issue="i", patch="p", poc="poc",
                                  runner=_stub_runner(), dispatch=_fake_dispatch)
    assert res["vetoed_by"] == ["relations", "differential"]
    assert res["reason"] == "relation broken"
    assert set(res) == {"relations", "differential", "vetoed_by", "verdict", "reason"}


def test_run_terms_no_veto_reason(monkeypatch, terms_env):
    monkeypatch.setattr(relations, "check", lambda *a, **k: dict(REL_HELD))
    monkeypatch.setattr(differential, "check", lambda *a, **k: dict(DIF_EQ))
    res = codefix_terms.run_terms(issue="i", patch="p", poc="poc",
                                  runner=_stub_runner(), dispatch=_fake_dispatch)
    assert res["vetoed_by"] == [] and res["verdict"] is None
    assert res["reason"] == "no veto"


def test_run_terms_raising_term_gives_error_slot(monkeypatch, terms_env):
    monkeypatch.setattr(relations, "check", lambda *a, **k: dict(REL_HELD))

    def boom(*a, **k):
        raise RuntimeError("differential blew up")

    monkeypatch.setattr(differential, "check", boom)
    res = codefix_terms.run_terms(issue="i", patch="p", poc="poc",
                                  runner=_stub_runner(), dispatch=_fake_dispatch)
    assert res["differential"] == {"error": "RuntimeError: differential blew up"}
    assert res["vetoed_by"] == [] and res["verdict"] is None


def test_run_terms_relations_disabled(monkeypatch, terms_env):
    calls = {"relations": 0}

    def spy(*a, **k):
        calls["relations"] += 1
        return dict(REL_REFUTED)

    monkeypatch.setattr(relations, "check", spy)
    monkeypatch.setattr(differential, "check", lambda *a, **k: dict(DIF_EQ))
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "0")
    res = codefix_terms.run_terms(issue="i", patch="p", poc="poc",
                                  runner=_stub_runner(), dispatch=_fake_dispatch)
    assert calls["relations"] == 0
    assert res["relations"] is None and res["differential"] is not None


def test_run_terms_differential_disabled(monkeypatch, terms_env):
    calls = {"differential": 0}

    def spy(*a, **k):
        calls["differential"] += 1
        return dict(DIF_REFUTED)

    monkeypatch.setattr(relations, "check", lambda *a, **k: dict(REL_HELD))
    monkeypatch.setattr(differential, "check", spy)
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "0")
    res = codefix_terms.run_terms(issue="i", patch="p", poc="poc",
                                  runner=_stub_runner(), dispatch=_fake_dispatch)
    assert calls["differential"] == 0
    assert res["differential"] is None and res["relations"] is not None


# ── evaluate end to end ──────────────────────────────────────────────────────

def _e2e_env(monkeypatch, *, relations_on, differential_on):
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1" if relations_on else "0")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1" if differential_on else "0")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_VETO_MIN", "1")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_N", "4")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_VETO_MIN", "2")


def test_evaluate_relations_veto_cheat(tmp_path, monkeypatch):
    _e2e_env(monkeypatch, relations_on=True, differential_on=False)
    repo, base = _make_repo(tmp_path, CHEAT_MED)
    rec = codefix_terms.evaluate(issue="median bug", poc=POC,
                                 poc_file="test_med.py", base_dir=base,
                                 candidate_dir=repo, dispatch=_fake_dispatch,
                                 python=sys.executable)
    assert set(rec) == NINE_KEYS
    assert rec["status"] == "ran" and rec["verdict"] == REFUTED
    assert rec["vetoed_by"] == ["relations"] and rec["would_block"] is True
    assert rec["relations"]["counts"]["violated"] == 1
    for record in rec["relations"]["records"]:
        assert "src" not in record


def test_evaluate_relations_held_good(tmp_path, monkeypatch):
    _e2e_env(monkeypatch, relations_on=True, differential_on=False)
    repo, base = _make_repo(tmp_path, GOOD_MED)
    rec = codefix_terms.evaluate(issue="median bug", poc=POC,
                                 poc_file="test_med.py", base_dir=base,
                                 candidate_dir=repo, dispatch=_fake_dispatch,
                                 python=sys.executable)
    assert rec["status"] == "ran" and rec["verdict"] is None
    assert rec["would_block"] is False
    assert rec["relations"]["counts"]["held"] == 1


def test_evaluate_differential_veto_avg(tmp_path, monkeypatch):
    _e2e_env(monkeypatch, relations_on=False, differential_on=True)
    repo, base = _make_repo(tmp_path, AVG_MED)
    rec = codefix_terms.evaluate(issue="median bug", poc=POC,
                                 poc_file="test_med.py", base_dir=base,
                                 candidate_dir=repo, dispatch=_fake_dispatch,
                                 python=sys.executable)
    assert rec["status"] == "ran" and rec["verdict"] == REFUTED
    assert rec["vetoed_by"] == ["differential"] and rec["would_block"] is True


def test_evaluate_differential_equivalent_good(tmp_path, monkeypatch):
    _e2e_env(monkeypatch, relations_on=False, differential_on=True)
    repo, base = _make_repo(tmp_path, GOOD_MED)
    rec = codefix_terms.evaluate(issue="median bug", poc=POC,
                                 poc_file="test_med.py", base_dir=base,
                                 candidate_dir=repo, dispatch=_fake_dispatch,
                                 python=sys.executable)
    assert rec["status"] == "ran" and rec["verdict"] is None
    assert rec["differential"]["state"] == "equivalent"


def test_evaluate_empty_diff_skipped(tmp_path, monkeypatch):
    _e2e_env(monkeypatch, relations_on=True, differential_on=True)
    repo, base = _make_repo(tmp_path, candidate_text=None)
    calls = []

    def spy(prompt):
        calls.append(prompt)
        return _fake_dispatch(prompt)

    rec = codefix_terms.evaluate(issue="median bug", poc=POC,
                                 poc_file="test_med.py", base_dir=base,
                                 candidate_dir=repo, dispatch=spy,
                                 python=sys.executable)
    assert set(rec) == NINE_KEYS
    assert rec["status"] == "skipped" and rec["reason"] == "empty candidate diff"
    assert calls == []
    assert rec["would_block"] is False and rec["vetoed_by"] == []


def test_evaluate_includes_untracked_files(tmp_path, monkeypatch):
    """A fix that adds new files is not an empty (or partial) diff."""
    _e2e_env(monkeypatch, relations_on=True, differential_on=False)
    repo, base = _make_repo(tmp_path, GOOD_MED)     # mixed: tracked + untracked
    _write(Path(repo) / "newmod.py", "def helper():\n    return 42\n")
    seen = {}

    def spy(poc, issue, patch, **kwargs):
        seen["patch"] = patch
        return {"verdict": None, "records": [], "reason": "stubbed"}

    monkeypatch.setattr(relations, "check", spy)
    rec = codefix_terms.evaluate(
        issue="median bug", poc=POC, poc_file="test_med.py", base_dir=base,
        candidate_dir=repo, dispatch=_fake_dispatch, python=sys.executable)
    assert rec["status"] == "ran"
    assert "+++ b/med.py" in seen["patch"]
    assert "+++ b/newmod.py" in seen["patch"]
    assert "def helper():" in seen["patch"]
    assert rec["verdict"] is None and rec["would_block"] is False


def test_evaluate_untracked_only_not_skipped(tmp_path, monkeypatch):
    """Untracked files alone are a non-empty candidate change set."""
    _e2e_env(monkeypatch, relations_on=False, differential_on=False)
    repo, base = _make_repo(tmp_path, candidate_text=None)
    _write(Path(repo) / "newmod.py", "def helper():\n    return 42\n")
    rec = codefix_terms.evaluate(
        issue="median bug", poc=POC, poc_file="test_med.py", base_dir=base,
        candidate_dir=repo, dispatch=_fake_dispatch, python=sys.executable)
    assert rec["status"] == "ran"
    assert rec["relations"] is None and rec["differential"] is None


def test_evaluate_runner_unavailable(tmp_path, monkeypatch):
    _e2e_env(monkeypatch, relations_on=True, differential_on=True)
    repo, _base = _make_repo(tmp_path, GOOD_MED)
    rec = codefix_terms.evaluate(issue="median bug", poc=POC,
                                 poc_file="test_med.py",
                                 base_dir=str(tmp_path / "missing-base"),
                                 candidate_dir=repo, dispatch=_fake_dispatch,
                                 python=sys.executable)
    assert rec["status"] == "skipped" and rec["reason"] == "runner unavailable"


# ── bounded_dispatch ─────────────────────────────────────────────────────────

def test_bounded_dispatch_env_and_cleanup(monkeypatch):
    for key in ("MO_CERTIFY_LANE_RETRIES", "MO_CERTIFY_LANE_TIMEOUT_S",
                "MO_CODEFIX_CERTIFY_LANE_TIMEOUT_S"):
        monkeypatch.delenv(key, raising=False)
    seen = {}

    def fake_dispatch(prompt):
        seen["retries"] = os.environ.get("MO_CERTIFY_LANE_RETRIES")
        seen["timeout"] = os.environ.get("MO_CERTIFY_LANE_TIMEOUT_S")
        return "dispatched!"

    fn = codefix_terms.bounded_dispatch()
    monkeypatch.setattr(llm_mod, "default_dispatch", fake_dispatch)
    assert fn("prompt") == "dispatched!"
    assert seen == {"retries": "0", "timeout": "90"}
    assert "MO_CERTIFY_LANE_RETRIES" not in os.environ
    assert "MO_CERTIFY_LANE_TIMEOUT_S" not in os.environ


def test_bounded_dispatch_timeout_override(monkeypatch):
    monkeypatch.delenv("MO_CERTIFY_LANE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("MO_CODEFIX_CERTIFY_LANE_TIMEOUT_S", "42")
    seen = {}

    def fake_dispatch(prompt):
        seen["timeout"] = os.environ.get("MO_CERTIFY_LANE_TIMEOUT_S")
        return "ok"

    monkeypatch.setattr(llm_mod, "default_dispatch", fake_dispatch)
    assert codefix_terms.bounded_dispatch()("p") == "ok"
    assert seen["timeout"] == "42"


def test_bounded_dispatch_exception_returns_empty(monkeypatch):
    def boom(prompt):
        raise ValueError("no lane configured")

    monkeypatch.setattr(llm_mod, "default_dispatch", boom)
    assert codefix_terms.bounded_dispatch()("p") == ""


def test_runner_past_deadline_abstains_without_spawning(tmp_path, monkeypatch):
    # Review fix: the shadow pass has a total budget, so it can never push the
    # code-fix verifier past its own node timeout.
    import subprocess as _sp
    import time as _time
    from mini_ork.certify import codefix_terms as ct

    def _boom(*a, **k):
        raise AssertionError("a probe was spawned after the budget ran out")

    monkeypatch.setattr(_sp, "run", _boom)
    runner = ct.LocalTreeRunner(str(tmp_path), str(tmp_path),
                                deadline=_time.monotonic() - 1)
    out = runner.run_in(str(tmp_path), "def test_x():\n    assert True\n")
    assert out.status == "no_run"
    assert "budget" in out.output


def test_bounded_dispatch_past_deadline_makes_no_lane_call(monkeypatch):
    import time as _time
    from mini_ork.certify import codefix_terms as ct
    from mini_ork.certify import llm

    def _boom(prompt):
        raise AssertionError("a lane was called after the budget ran out")

    monkeypatch.setattr(llm, "default_dispatch", _boom)
    assert ct.bounded_dispatch(_time.monotonic() - 1)("prompt") == ""
