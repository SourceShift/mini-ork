"""Hermetic tests for the SEC-G05-T02 tiered hypothesis cascade.

Every test is offline: the engine module is exercised as a library, its CLI
against throwaway git repositories under ``tmp_path``, and the ``code-fix`` node
as a subprocess with the same environment the engine gives it. The point of the
suite is the anti-regex invariant — a dangerous-API line is only a hypothesis
until an executed probe confirms it.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork.verify.hypothesis_cascade as hc

FAILING_PROBE = "import sys\nsys.exit(1)\n"
FAILING_PROBE_7 = "import sys\nsys.exit(7)\n"
PASSING_PROBE = "import sys\nsys.exit(0)\n"
SLEEPING_PROBE = "import time\ntime.sleep(5)\nimport sys\nsys.exit(0)\n"
ENV_PROBE = (
    "import json, os, pathlib, sys\n"
    "pathlib.Path('probe-env.json').write_text(json.dumps({\n"
    "    'MO_HYPOTHESIS_PATH': os.environ.get('MO_HYPOTHESIS_PATH'),\n"
    "    'MO_HYPOTHESIS_LINE': os.environ.get('MO_HYPOTHESIS_LINE'),\n"
    "    'MO_HYPOTHESIS_RULE': os.environ.get('MO_HYPOTHESIS_RULE'),\n"
    "    'executable': sys.executable,\n"
    "}))\n"
    "print('PROBE-STDOUT-NOISE')\n"
)

REPO = Path(__file__).resolve().parents[2]
NODE_SCRIPT = REPO / "recipes" / "code-fix" / "verifiers" / "hypothesis.py"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for these tests"
)


# --------------------------------------------------------------------- helpers

def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _hyp(rule: str, path: str, line: int, severity: int = 3, score: int = 6):
    return hc.Hypothesis(
        id="%s:%s:%d" % (rule, path, line),
        rule=rule, path=path, line=line,
        severity=severity, score=score, signals=("added",),
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false"] + list(args),
        cwd=str(repo), check=True, capture_output=True,
    )


def _repo_with_added_line(tmp: Path, name: str, extra_line: str) -> Path:
    """A real git repo whose working tree gained exactly ``extra_line``."""
    repo = tmp / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "app/x.py", "a = 1\nb = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t.example", "-c", "user.name=t",
         "commit", "-q", "-m", "init")
    with (repo / "app" / "x.py").open("a", encoding="utf-8") as fh:
        fh.write(extra_line)
    return repo


def _main(argv):
    try:
        return hc.main(argv)
    except SystemExit as exc:  # argparse usage errors exit, not return
        return exc.code


def _last_json(stdout: str):
    for line in reversed([l for l in stdout.splitlines() if l.strip()]):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise AssertionError("no JSON line on stdout: %r" % stdout[:500])


def _run_node(target_cwd: Path, run_dir: Path):
    env = dict(os.environ)
    for key in ("MINI_ORK_HOME", "MINI_ORK_RUN_ID", "MO_GOAL_SCOPED_BASE"):
        env.pop(key, None)
    env["MINI_ORK_TOUCHED_FILES"] = "app/x.py"
    env["MO_TARGET_CWD"] = str(target_cwd)
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    proc = subprocess.run(
        [sys.executable, str(NODE_SCRIPT)],
        cwd=str(target_cwd), env=env, capture_output=True, text=True,
        timeout=180,
    )
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert lines, "node printed no stdout envelope; stderr: %s" % (
        proc.stderr[-800:])
    assert len(lines) == 1, (
        "one single-line JSON envelope on stdout; got %d" % len(lines))
    return proc.returncode, json.loads(lines[0])


# ---------------------------------------------------------------- module shape

def test_module_docstring_future_annotations_and_stdlib_imports():
    source = Path(hc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert (hc.__doc__ or "").strip(), "a module docstring is required"
    allowed = {"ast", "json", "os", "re", "subprocess", "sys", "pathlib",
               "typing", "dataclasses", "mini_ork"}
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert imports
    future_lines, other_lines = [], []
    for node in imports:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future_lines.append(node.lineno)
            continue
        other_lines.append(node.lineno)
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        else:
            roots = {"mini_ork"} if node.level else {node.module.split(".")[0]}
        assert roots <= allowed, "non-stdlib import: %r" % roots
    assert future_lines, "from __future__ import annotations is required"
    assert min(future_lines) < min(other_lines)


def test_constants_and_sinks_verbatim():
    assert hc.CONFIRMED == "confirmed"
    assert hc.CLEARED == "cleared"
    assert hc.ABSTAINED == "abstained"
    assert hc.OUTCOMES == (hc.CONFIRMED, hc.CLEARED, hc.ABSTAINED)
    assert hc.TIER3_CAP == 20
    assert hc.PROBE_TIMEOUT_S == 60
    assert hc.PROBE_DIR == "hypotheses"
    assert {rule: severity for rule, _rx, severity in hc.SINKS} == {
        "sql-fstring": 3,
        "eval-exec": 3,
        "subprocess-shell": 3,
        "unsafe-deserialization": 2,
        "tls-verify-off": 2,
        "untrusted-path-join": 2,
    }
    for rule, pattern, severity in hc.SINKS:
        assert isinstance(pattern, str), rule
        re.compile(pattern)
        assert severity in (1, 2, 3), rule


def test_hypothesis_is_a_frozen_dataclass_with_defaults():
    hypothesis = hc.Hypothesis(
        id="eval-exec:app/x.py:3", rule="eval-exec", path="app/x.py",
        line=3, severity=3, score=6, signals=("added", "python"),
    )
    assert hypothesis.outcome == ""
    assert hypothesis.reason == ""
    names = [field.name for field in dataclasses.fields(hc.Hypothesis)]
    assert names == ["id", "rule", "path", "line", "severity", "score",
                     "signals", "outcome", "reason"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        hypothesis.line = 4


def test_probe_name_scheme():
    hypothesis = hc.Hypothesis(
        id="eval-exec:app/mod/x.py:3", rule="eval-exec", path="app/mod/x.py",
        line=3, severity=3, score=6, signals=(),
    )
    assert hc.probe_name(hypothesis) == "eval-exec__app~mod~x.py__3.py"


# --------------------------------------------------------------- span/signals

def test_function_span_innermost_and_module_fallback():
    lines = [
        "def outer():",
        "    def inner():",
        "        eval(x)",
        "    return inner",
        "",
    ]
    assert hc.function_span(lines, 3) == (2, 3)
    assert hc.function_span(lines, 4) == (1, 5)
    assert hc.function_span(["x = eval(y)", "z = 2"], 1) == (1, 2)


def test_function_span_includes_a_directly_attached_decorator():
    lines = ['@app.route("/x")', "def handler():", "    eval(payload)"]
    assert hc.function_span(lines, 3) == (1, 3)


def test_signals_exact_scores_and_sorted_names():
    handler = [
        '@app.route("/search")',
        "def search():",
        '    q = request.args.get("q")',
        "    eval(payload)",
    ]
    score, names = hc.signals(handler, 4, 3, "app/search.py")
    assert score == 3 + 2 + 2 + 2 + 1
    assert list(names) == sorted(names)
    assert set(names) == {"severity=3", "untrusted-source", "entrypoint",
                          "added", "python"}
    module_level = ["result = eval(payload)", "x = 1"]
    score2, names2 = hc.signals(module_level, 1, 3, "mod.py")
    assert score2 == 3 + 2 + 1
    assert set(names2) == {"severity=3", "added", "python"}


# ------------------------------------------------------------------- proposal

def test_propose_sql_and_eval_with_ids(tmp_path):
    _write(tmp_path, "app/x.py",
           'import os\nq = "SELECT * FROM u WHERE id = " + user\neval(payload)\n')
    added = {"app/x.py": {
        2: 'q = "SELECT * FROM u WHERE id = " + user',
        3: "eval(payload)",
    }}
    hypotheses = hc.propose(str(tmp_path), added)
    by_rule = {hypothesis.rule: hypothesis for hypothesis in hypotheses}
    assert set(by_rule) == {"sql-fstring", "eval-exec"}
    assert by_rule["sql-fstring"].severity == 3
    assert by_rule["eval-exec"].severity == 3
    assert by_rule["sql-fstring"].id == "sql-fstring:app/x.py:2"
    assert by_rule["eval-exec"].id == "eval-exec:app/x.py:3"
    assert all(hypothesis.outcome == "" for hypothesis in hypotheses)


def test_propose_sec_allow_marker_suppresses(tmp_path):
    line = "eval(payload)  # sec-allow: eval-exec"
    _write(tmp_path, "app/x.py", "a = 1\n" + line + "\n")
    added = {"app/x.py": {2: line}}
    assert hc.propose(str(tmp_path), added) == []


def test_propose_highest_severity_wins_one_hypothesis_per_line(tmp_path):
    _write(tmp_path, "app/x.py", "verify = False\neval(yaml.load(data))\n")
    added = {"app/x.py": {1: "verify = False", 2: "eval(yaml.load(data))"}}
    hypotheses = hc.propose(str(tmp_path), added)
    tls = [h for h in hypotheses if h.line == 1]
    both = [h for h in hypotheses if h.line == 2]
    assert len(tls) == 1 and tls[0].severity == 2
    assert tls[0].rule == "tls-verify-off"
    assert len(both) == 1
    assert both[0].severity == 3 and both[0].rule == "eval-exec"


def test_propose_ignores_preexisting_sink_outside_added(tmp_path):
    _write(tmp_path, "app/x.py", "a = 1\ntotal = a + b\nb = eval(old)\n")
    added = {"app/x.py": {2: "total = a + b"}}
    assert hc.propose(str(tmp_path), added) == []


def test_propose_missing_file_scores_added_lines_alone(tmp_path):
    added = {"gone/x.py": {2: "eval(payload)"}}
    hypotheses = hc.propose(str(tmp_path), added)
    assert len(hypotheses) == 1
    assert hypotheses[0].rule == "eval-exec"
    assert hypotheses[0].score == 3 + 2 + 1
    assert set(hypotheses[0].signals) == {"severity=3", "added", "python"}


def test_propose_sorted_by_negative_score_then_path_then_line(tmp_path):
    _write(tmp_path, "b.py", 'def f():\n    s = request.args["x"]\n    eval(a)\n')
    _write(tmp_path, "a.py", "eval(z)\n")
    _write(tmp_path, "d.py", "eval(w)\n")
    _write(tmp_path, "e.py", "eval(w)\n")
    _write(tmp_path, "c.py", "verify = False\n")
    added = {
        "b.py": {3: "    eval(a)"},
        "a.py": {1: "eval(z)"},
        "d.py": {1: "eval(w)"},
        "e.py": {1: "eval(w)"},
        "c.py": {1: "verify = False"},
    }
    hypotheses = hc.propose(str(tmp_path), added)
    assert [h.path for h in hypotheses] == ["b.py", "a.py", "d.py", "e.py", "c.py"]


# ----------------------------------------------------------------------- rank

def test_rank_deterministic_capped_and_deferred():
    hypotheses = [
        hc.Hypothesis(
            id="eval-exec:f%02d.py:%d" % (i, i + 1),
            rule="eval-exec", path="f%02d.py" % i, line=i + 1,
            severity=3 if i % 2 == 0 else 2, score=25 - i, signals=("added",),
        )
        for i in range(25)
    ]
    expected = sorted(
        hypotheses, key=lambda h: (-h.score, -h.severity, h.path, h.line))
    selected1, deferred1 = hc.rank(list(hypotheses))
    selected2, deferred2 = hc.rank(list(hypotheses))
    assert len(selected1) == hc.TIER3_CAP == 20
    assert [h.id for h in selected1] == [h.id for h in expected][:20]
    assert [h.id for h in selected1] == [h.id for h in selected2]
    assert [h.id for h in deferred1] == [h.id for h in expected][20:]
    assert [h.id for h in deferred1] == [h.id for h in deferred2]


# --------------------------------------------------------------------- probes

def test_probes_maps_only_existing_files_and_missing_dir_is_empty(tmp_path):
    run_dir = tmp_path / "rd"
    with_probe = _hyp("eval-exec", "app/x.py", 3)
    without_probe = _hyp("eval-exec", "app/x.py", 9)
    directory = run_dir / hc.PROBE_DIR
    directory.mkdir(parents=True)
    (directory / hc.probe_name(with_probe)).write_text(
        PASSING_PROBE, encoding="utf-8")
    mapping = hc.probes(str(run_dir), [with_probe, without_probe])
    assert set(mapping) == {with_probe.id}
    assert Path(mapping[with_probe.id]).is_absolute()
    assert Path(mapping[with_probe.id]).name == hc.probe_name(with_probe)
    assert hc.probes(str(tmp_path / "absent"), [with_probe]) == {}


# -------------------------------------------------------------------- confirm

def test_confirm_exit_matrix(tmp_path):
    hypothesis = _hyp("eval-exec", "app/x.py", 3)
    failing = tmp_path / "failing.py"
    failing.write_text(FAILING_PROBE, encoding="utf-8")
    assert hc.confirm(hypothesis, str(failing), str(tmp_path)) == (
        hc.CONFIRMED, "probe exited 1")
    failing7 = tmp_path / "failing7.py"
    failing7.write_text(FAILING_PROBE_7, encoding="utf-8")
    assert hc.confirm(hypothesis, str(failing7), str(tmp_path)) == (
        hc.CONFIRMED, "probe exited 7")
    passing = tmp_path / "passing.py"
    passing.write_text(PASSING_PROBE, encoding="utf-8")
    assert hc.confirm(hypothesis, str(passing), str(tmp_path)) == (
        hc.CLEARED, "probe exited 0")


def test_confirm_timeout_abstains(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "PROBE_TIMEOUT_S", 1)
    hypothesis = _hyp("eval-exec", "app/x.py", 3)
    sleeping = tmp_path / "sleeping.py"
    sleeping.write_text(SLEEPING_PROBE, encoding="utf-8")
    assert hc.confirm(hypothesis, str(sleeping), str(tmp_path)) == (
        hc.ABSTAINED, "probe timed out")


def test_confirm_missing_probe_abstains(tmp_path):
    hypothesis = _hyp("eval-exec", "app/x.py", 3)
    outcome, reason = hc.confirm(
        hypothesis, str(tmp_path / "absent.py"), str(tmp_path))
    assert outcome == hc.ABSTAINED
    assert reason.startswith("probe could not be executed")


def test_confirm_env_cwd_and_captured_output(tmp_path, capsys):
    hypothesis = _hyp("eval-exec", "app/x.py", 3)
    probe = tmp_path / "env_probe.py"
    probe.write_text(ENV_PROBE, encoding="utf-8")
    outcome, _reason = hc.confirm(hypothesis, str(probe), str(tmp_path))
    assert outcome == hc.CLEARED
    dumped = json.loads((tmp_path / "probe-env.json").read_text(encoding="utf-8"))
    assert dumped["MO_HYPOTHESIS_PATH"] == hypothesis.path
    assert dumped["MO_HYPOTHESIS_LINE"] == str(hypothesis.line)
    assert dumped["MO_HYPOTHESIS_RULE"] == hypothesis.rule
    assert dumped["executable"] == sys.executable
    assert "PROBE-STDOUT-NOISE" not in capsys.readouterr().out


# ------------------------------------------------------------------- evaluate

def test_evaluate_vacuous_when_nothing_dangerous(tmp_path):
    status, report = hc.evaluate(
        str(tmp_path), {}, str(tmp_path / "rd"))
    assert status == "vacuous"
    assert report["reason"] == "no dangerous-API line on the change surface"
    _write(tmp_path, "app/clean.py", "total = 1 + 2\n")
    status2, _report2 = hc.evaluate(
        str(tmp_path), {"app/clean.py": {1: "total = 1 + 2"}},
        str(tmp_path / "rd"))
    assert status2 == "vacuous"


def test_evaluate_no_probe_passes_with_an_abstention_and_no_finding(tmp_path):
    _write(tmp_path, "app/x.py", "a = 1\nb = 2\neval(payload)\n")
    added = {"app/x.py": {3: "eval(payload)"}}
    status, report = hc.evaluate(str(tmp_path), added, str(tmp_path / "rd"))
    assert status == "pass"
    assert report["counts"]["abstained"] == 1
    assert report["counts"]["confirmed"] == 0
    entry = next(e for e in report["hypotheses"] if e["rule"] == "eval-exec")
    assert entry["outcome"] == hc.ABSTAINED
    assert entry["reason"] == "no probe: nothing confirms this hypothesis"
    assert all(e["outcome"] != hc.CONFIRMED for e in report["hypotheses"])


def test_evaluate_failing_probe_fails(tmp_path):
    _write(tmp_path, "app/x.py", "a = 1\nb = 2\neval(payload)\n")
    added = {"app/x.py": {3: "eval(payload)"}}
    run_dir = tmp_path / "rd"
    directory = run_dir / hc.PROBE_DIR
    directory.mkdir(parents=True)
    hypothesis = _hyp("eval-exec", "app/x.py", 3)
    (directory / hc.probe_name(hypothesis)).write_text(
        FAILING_PROBE, encoding="utf-8")
    status, report = hc.evaluate(str(tmp_path), added, str(run_dir))
    assert status == "fail"
    assert report["counts"]["confirmed"] == 1
    entry = next(e for e in report["hypotheses"] if e["rule"] == "eval-exec")
    assert entry["outcome"] == hc.CONFIRMED
    assert entry["reason"] == "probe exited 1"


def test_evaluate_passing_probe_clears_and_is_never_a_finding(tmp_path):
    """The anti-regex point: a sink with a passing probe is CLEARED."""
    _write(tmp_path, "app/x.py", "a = 1\nb = 2\neval(payload)\n")
    added = {"app/x.py": {3: "eval(payload)"}}
    run_dir = tmp_path / "rd"
    directory = run_dir / hc.PROBE_DIR
    directory.mkdir(parents=True)
    hypothesis = _hyp("eval-exec", "app/x.py", 3)
    (directory / hc.probe_name(hypothesis)).write_text(
        PASSING_PROBE, encoding="utf-8")
    status, report = hc.evaluate(str(tmp_path), added, str(run_dir))
    assert status == "pass"
    assert report["counts"]["cleared"] == 1
    assert report["counts"]["confirmed"] == 0
    entry = next(e for e in report["hypotheses"] if e["rule"] == "eval-exec")
    assert entry["outcome"] == hc.CLEARED
    assert all(e["outcome"] != hc.CONFIRMED for e in report["hypotheses"])


def test_evaluate_report_counts_and_deferred_past_the_cap(tmp_path):
    count = 25
    lines = "".join("eval(arg_%d)\n" % i for i in range(1, count + 1))
    _write(tmp_path, "app/big.py", lines)
    added = {"app/big.py": {
        i: "eval(arg_%d)" % i for i in range(1, count + 1)}}
    status, report = hc.evaluate(str(tmp_path), added, str(tmp_path / "rd"))
    assert status == "pass"
    assert set(report["counts"]) == set(hc.OUTCOMES)
    assert report["counts"]["abstained"] == hc.TIER3_CAP
    assert report["counts"]["confirmed"] == 0
    assert report["counts"]["cleared"] == 0
    assert len(report["deferred"]) == count - hc.TIER3_CAP
    assert all(isinstance(item, str) for item in report["deferred"])
    keys = {"id", "rule", "path", "line", "severity", "score", "signals",
            "outcome", "reason"}
    assert len(report["hypotheses"]) == hc.TIER3_CAP
    for entry in report["hypotheses"]:
        assert keys <= set(entry)


# ------------------------------------------------------------------------- CLI

def test_main_usage_error_when_no_surface(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_TOUCHED_FILES", raising=False)
    assert _main(["--root", str(tmp_path), "--json"]) == 2


def test_main_git_failure_is_an_abstention_naming_git(
        tmp_path, monkeypatch, capsys):
    _write(tmp_path, "app/x.py", "a = 1\neval(payload)\n")  # not a git repo
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "rd"))
    _main(["--root", str(tmp_path), "--paths", "app/x.py", "--json"])
    data = _last_json(capsys.readouterr().out)
    assert data["status"] == "abstain"
    assert "git" in json.dumps(data).lower()


@requires_git
def test_main_exit_codes_uncertain_band_confirmed_and_clean(
        tmp_path, monkeypatch, capsys):
    repo = _repo_with_added_line(tmp_path, "repo1", "eval(payload_cli)\n")
    run_dir = tmp_path / "rd1"
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    argv = ["--root", str(repo), "--paths", "app/x.py", "--json"]

    # Nothing confirms it yet: the uncertain band, never a silent pass.
    assert _main(argv) == 3
    first = _last_json(capsys.readouterr().out)
    assert first["counts"]["confirmed"] == 0
    assert first["hypotheses"]
    entry = first["hypotheses"][0]
    probe = hc.Hypothesis(
        id="%s:%s:%s" % (entry["rule"], entry["path"], entry["line"]),
        rule=entry["rule"], path=entry["path"], line=entry["line"],
        severity=entry.get("severity", 3), score=entry.get("score", 0),
        signals=tuple(entry.get("signals", ())),
    )
    directory = run_dir / hc.PROBE_DIR
    directory.mkdir(parents=True)
    (directory / hc.probe_name(probe)).write_text(
        FAILING_PROBE, encoding="utf-8")

    assert _main(argv) == 1
    second = _last_json(capsys.readouterr().out)
    assert second["counts"]["confirmed"] == 1

    clean = _repo_with_added_line(tmp_path, "repo2", "total = 1 + 2\n")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "rd2"))
    assert _main(["--root", str(clean), "--paths", "app/x.py", "--json"]) == 0
    third = _last_json(capsys.readouterr().out)
    assert "counts" in third and "hypotheses" in third


@requires_git
def test_main_text_mode_blocks_summary_and_no_source_text(
        tmp_path, monkeypatch, capsys):
    repo = _repo_with_added_line(tmp_path, "repo3", "eval(zq_payload_9x)\n")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "rd3"))
    assert _main(["--root", str(repo), "--paths", "app/x.py"]) == 3
    out = capsys.readouterr().out
    assert "HYPOTHESIS: " in out
    summary = [l for l in out.splitlines()
               if l.startswith("hypothesis-cascade:")]
    assert len(summary) == 1
    for word in ("confirmed", "cleared", "abstained", "deferred"):
        assert word in summary[0]
    assert "app/x.py" in out
    assert "eval-exec" in out
    assert "zq_payload_9x" not in out


# ------------------------------------------------------------------------ node

@requires_git
def test_node_vacuous_when_no_dangerous_line(tmp_path):
    repo = _repo_with_added_line(tmp_path, "repo_v", "total = 1 + 2\n")
    rc, envelope = _run_node(repo, tmp_path / "rd_v")
    assert rc == 0
    assert envelope["pass"] is True
    assert envelope["verdict"] == "vacuous"
    assert "status" not in envelope
    assert envelope.get("error_summary")


@requires_git
def test_node_fails_only_when_a_probe_confirms(tmp_path):
    repo = _repo_with_added_line(tmp_path, "repo_f", "eval(payload_node)\n")
    run_dir = tmp_path / "rd_f"
    # Without a probe the pattern alone must not fail the node.
    rc_clean, envelope_clean = _run_node(repo, run_dir)
    assert rc_clean == 0
    assert envelope_clean["verdict"] == "pass"
    assert envelope_clean["findings"]
    assert all(f.get("gating") is False for f in envelope_clean["findings"])

    added, _unreadable, measured = hc.security_scan.collect_added(
        str(repo), "", ["app/x.py"])
    assert measured
    hypotheses = hc.propose(str(repo), added)
    assert len(hypotheses) == 1
    directory = run_dir / hc.PROBE_DIR
    directory.mkdir(parents=True)
    (directory / hc.probe_name(hypotheses[0])).write_text(
        FAILING_PROBE, encoding="utf-8")

    rc, envelope = _run_node(repo, run_dir)
    assert rc == 1
    assert envelope["pass"] is False
    assert envelope["verdict"] == "fail"
    finding = next(
        f for f in envelope["findings"]
        if f.get("rule") == hypotheses[0].rule
        and f.get("line") == hypotheses[0].line
    )
    assert finding["path"] == hypotheses[0].path
    assert finding["detail"] == "probe exited 1"


@requires_git
def test_node_git_failure_is_unverified_toolchain(tmp_path):
    plain = tmp_path / "plain"  # deliberately not a git repository
    _write(plain, "app/x.py", "a = 1\neval(payload_nogit)\n")
    rc, envelope = _run_node(plain, tmp_path / "rd_t")
    assert rc == 0
    assert envelope["status"] == "unverified"
    assert envelope["failure_kind"] == "toolchain"
