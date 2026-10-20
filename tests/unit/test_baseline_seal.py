"""RBF-05: the run baseline is sealed outside the run dir, and self-made
verifier evidence is not graded.

The run dir and the target tree are both writable by the agents a run spawns,
so neither can witness its own baseline. ``_capture_pre_impl_baseline`` seals
the pre-implementer ref + roots hash under ``<MINI_ORK_HOME>/state/seals/``,
the ground-truth harvest compares against that seal (restoring and failing the
node on disagreement), and the rubric prescreen drops any
``verifier_<stem>.json`` no verifier ``node_end`` backs.

Hermetic: tmp MINI_ORK_HOME, tmp run dir, tmp git repo, tmp SQLite; the grader
dispatch is stubbed and never reaches a provider.
"""

import json
import os
import re
import sqlite3
import subprocess

MARKER = "RBF05-SELF-MADE-EVIDENCE-7Q"
RUBRIC_OK = '{"pass": true, "score": 8, "items": []}'


# ── helpers ──────────────────────────────────────────────────────────────────

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "target-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "seal@example.test")
    _git(repo, "config", "user.name", "Seal Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _make_run(tmp_path, repo, name="run-rbf05"):
    home = tmp_path / "mini-ork-home"
    home.mkdir()
    run_dir = tmp_path / name
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "recipe": "framework-edit",
        "roots": {
            "target": str(repo),
            "run_dir": str(run_dir),
            "home": str(home),
            "engine": "",
        },
    }, indent=2, sort_keys=True), encoding="utf-8")
    return home, run_dir


def _env(monkeypatch, home, repo, run_dir):
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.setenv("MINI_ORK_RUN_ID", os.path.basename(str(run_dir)))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_DB", raising=False)


def _capture(run_dir):
    from mini_ork.cli.execute import _capture_pre_impl_baseline
    _capture_pre_impl_baseline(str(run_dir))


def _seal_path(home, run_dir):
    return os.path.join(str(home), "state", "seals",
                        f"{os.path.basename(str(run_dir))}.json")


# ── baseline seal ────────────────────────────────────────────────────────────

def test_seal_is_written_outside_the_run_dir_and_target_tree(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    home, run_dir = _make_run(tmp_path, repo)
    _env(monkeypatch, home, repo, run_dir)

    _capture(run_dir)

    ref = (run_dir / "pre-implementer-ref").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{40,64}", ref)

    seal = _seal_path(home, run_dir)
    assert os.path.isfile(seal)
    assert not os.path.realpath(seal).startswith(
        os.path.realpath(str(run_dir)) + os.sep)
    assert not os.path.realpath(seal).startswith(
        os.path.realpath(str(repo)) + os.sep)

    with open(seal, encoding="utf-8") as fh:
        sealed = json.load(fh)
    assert sealed["pre_implementer_ref"] == ref
    assert re.fullmatch(r"[0-9a-f]{64}", sealed["roots_sha256"])


def test_rewritten_ref_is_restored_and_the_node_fails(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    home, run_dir = _make_run(tmp_path, repo)
    _env(monkeypatch, home, repo, run_dir)
    _capture(run_dir)
    with open(_seal_path(home, run_dir), encoding="utf-8") as fh:
        sealed_ref = json.load(fh)["pre_implementer_ref"]

    # The implementer commits and re-points the recorded baseline, exactly as
    # run-1792224150-80303 did.
    (repo / "second.txt").write_text("moved\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "moved by implementer")
    other = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert other != sealed_ref
    (run_dir / "pre-implementer-ref").write_text(other + "\n", encoding="utf-8")

    from mini_ork.cli.execute import _harvest_framework_edit_ground_truth
    ok, finish_reason = _harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert ok is False
    assert finish_reason == "config"
    assert "baseline files rewritten during the run" in capsys.readouterr().err
    assert (run_dir / "pre-implementer-ref").read_text(
        encoding="utf-8").strip() == sealed_ref


def test_rewritten_roots_fail_the_node(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    home, run_dir = _make_run(tmp_path, repo)
    _env(monkeypatch, home, repo, run_dir)
    _capture(run_dir)

    profile_path = run_dir / "run_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["roots"]["target"] = str(tmp_path / "elsewhere")
    profile_path.write_text(json.dumps(profile, indent=2, sort_keys=True),
                            encoding="utf-8")

    from mini_ork.cli.execute import _harvest_framework_edit_ground_truth
    ok, finish_reason = _harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert ok is False
    assert finish_reason == "config"
    assert "baseline files rewritten during the run" in capsys.readouterr().err
    restored = json.loads(profile_path.read_text(encoding="utf-8"))
    assert restored["roots"]["target"] == str(repo), "roots must be restored"


def test_intact_baseline_passes_ground_truth(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    home, run_dir = _make_run(tmp_path, repo)
    _env(monkeypatch, home, repo, run_dir)
    _capture(run_dir)

    # A legitimate implementer edit: working-tree change, baseline untouched.
    (repo / "README.md").write_text("baseline\nedit\n", encoding="utf-8")

    from mini_ork.cli.execute import _harvest_framework_edit_ground_truth
    ok, finish_reason = _harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert (ok, finish_reason) == (True, "")
    assert "edit" in (run_dir / "framework-edit.diff").read_text(encoding="utf-8")


# ── evidence provenance ──────────────────────────────────────────────────────

def _provenance_env(tmp_path):
    home = tmp_path / "mini-ork-home"
    home.mkdir()
    root = tmp_path / "mini-ork-root"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "rubric-prescreen.md").write_text(
        "KICKOFF:\n{{KICKOFF_BODY}}\n\nARTIFACTS:\n{{DIFF_SUMMARY}}\n",
        encoding="utf-8")
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# kickoff\nFix the thing.\n", encoding="utf-8")
    run_dir = tmp_path / "run-rbf05-evidence"
    run_dir.mkdir()
    (run_dir / "verifier_test.json").write_text(
        json.dumps({"pass": True, "score": 8, "note": MARKER}), encoding="utf-8")
    db = home / "state.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE run_events (event_id TEXT, run_id TEXT, event_type TEXT, "
        "payload_json TEXT, created_at INTEGER, finish_reason TEXT)")
    con.commit()
    con.close()
    return home, root, kickoff, run_dir, db


def _emit_verifier_end(db, run_dir):
    payload = {
        "node_id": "test_verifier",
        "node_type": "verifier",
        "duration_ms": 1,
        "verdict": "pass",
        "artifact_path": os.path.join(str(run_dir), "verifier_test.json"),
        "finish_reason": "done",
    }
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json, "
        "created_at, finish_reason) VALUES (?, ?, 'node_end', ?, ?, ?)",
        ("ev-1", os.path.basename(str(run_dir)), json.dumps(payload), 1, "done"))
    con.commit()
    con.close()


def _run_prescreen(monkeypatch, home, root, kickoff, run_dir, db):
    import mini_ork.dispatch.llm_dispatch as llm_dispatch_module

    prompts = []

    def _fake_dispatch(argv, root=None, **kwargs):
        prompts.append(list(argv))
        print(RUBRIC_OK)
        return 0

    monkeypatch.setattr(llm_dispatch_module, "llm_dispatch", _fake_dispatch)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_RUN_ID", os.path.basename(str(run_dir)))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    from mini_ork.gates.rubric_prescreen import mo_rubric_run_score
    mo_rubric_run_score(str(kickoff), str(run_dir), task_class="framework-edit",
                        mini_ork_root=str(root), mini_ork_home=str(home),
                        mini_ork_db=str(db))
    assert prompts, "the prescreen never dispatched a grader prompt"
    argv = prompts[0]
    return argv[argv.index("--prompt-text") + 1]


def test_self_made_verifier_evidence_is_not_graded(tmp_path, monkeypatch, capsys):
    home, root, kickoff, run_dir, db = _provenance_env(tmp_path)

    prompt = _run_prescreen(monkeypatch, home, root, kickoff, run_dir, db)

    assert MARKER not in prompt, "unprovenanced evidence reached the grader"
    assert "verifier_test.json" in capsys.readouterr().err, "ignored file not logged"


def test_verifier_evidence_backed_by_node_end_is_graded(tmp_path, monkeypatch):
    home, root, kickoff, run_dir, db = _provenance_env(tmp_path)
    _emit_verifier_end(db, run_dir)

    prompt = _run_prescreen(monkeypatch, home, root, kickoff, run_dir, db)

    assert MARKER in prompt, "provenanced evidence must be used"
