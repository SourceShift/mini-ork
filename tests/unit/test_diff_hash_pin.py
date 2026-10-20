"""The published framework-edit.diff is the one the verifiers judged (RB-13).

Three clauses from kickoff ``rbf-diff-hash-pin.md``:

- the framework-edit verifiers record ``artifact_sha256`` — the sha256 of the
  ``framework-edit.diff`` they saw — in their JSON envelope;
- an unchanged diff publishes, a diff rewritten after verification is refused
  with ``[skip-publish] framework-edit.diff changed after verification``, writes
  ``publish-abstain.json``, and ends the run ``failed``;
- envelopes from older runs (no ``artifact_sha256`` field) do not block.

Hermetic: throwaway git repos, tmp run dirs, and in-process publisher calls with
its DB writes stubbed. No network, no live home, no provider credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import mini_ork  # noqa: E402
from mini_ork.cli import publisher  # noqa: E402

ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(mini_ork.__file__)))
VERIFIERS_DIR = os.path.join(ENGINE_ROOT, "recipes", "framework-edit", "verifiers")
REFUSAL = "[skip-publish] framework-edit.diff changed after verification"

_DIFF_TEXT = """diff --git a/docs/note.md b/docs/note.md
new file mode 100644
--- /dev/null
+++ b/docs/note.md
@@ -0,0 +1 @@
+hello
"""


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Strip ambient run identity; keep the orthogonal gates neutral."""
    for key in list(os.environ):
        if key.startswith(("MINI_ORK_", "MO_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "0")
    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")
    monkeypatch.setenv("MO_KICKOFF_GUARD", "off")
    monkeypatch.setenv("MO_PROBE_VALIDITY", "0")
    monkeypatch.setenv("MO_EVIDENCE_LEDGER", "0")


# ── verifier envelopes pin the diff they verified ───────────────────────────

def _git(repo, *argv, env):
    proc = subprocess.run(["git", "-C", str(repo), *argv], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.returncode == 0, (
        f"fixture git {' '.join(argv)} failed rc={proc.returncode}: "
        f"{proc.stderr.strip()[:400]}")
    return proc


def _git_env(home):
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _build_fixture(tmp_path):
    """A target repo plus a run dir whose framework-edit.diff applies to it."""
    home = tmp_path / "home"
    home.mkdir()
    env = _git_env(home)
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q", env=env)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A", env=env)
    _git(repo, "-c", "user.name=spec", "-c", "user.email=spec@example.invalid",
         "commit", "-q", "-m", "init", env=env)

    docs = repo / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("note\n", encoding="utf-8")
    _git(repo, "add", "-A", env=env)
    diff_text = _git(repo, "diff", "--cached", env=env).stdout
    _git(repo, "reset", "-q", env=env)
    (docs / "note.md").unlink()
    assert diff_text.strip()

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    diff_path = run_dir / "framework-edit.diff"
    diff_path.write_text(diff_text, encoding="utf-8")
    return repo, run_dir, hashlib.sha256(diff_path.read_bytes()).hexdigest()


def _run_verifier(script_name, repo, run_dir, home):
    script = os.path.join(VERIFIERS_DIR, script_name)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MO_TARGET_CWD": str(repo),
        "PYTHONPATH": ENGINE_ROOT,
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    proc = subprocess.run([sys.executable, script], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=300)
    assert proc.returncode == 0, (
        f"{script_name} exited rc={proc.returncode}; stderr tail: "
        f"{proc.stderr[-800:]}")
    text = proc.stdout.strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        assert start != -1 and end > start, (
            f"{script_name} wrote no JSON envelope: {text[:400]!r}")
        return json.loads(text[start:end + 1])


@pytest.mark.parametrize("script_name", ["test.py", "static-check.py"])
def test_envelope_carries_sha_of_verified_diff(tmp_path, script_name):
    repo, run_dir, expected_sha = _build_fixture(tmp_path)
    envelope = _run_verifier(script_name, repo, run_dir, tmp_path / "home")
    assert envelope.get("pass") is True, (
        f"fixture invalid — {script_name} did not pass: "
        f"{envelope.get('failed_checks')}")
    assert envelope.get("artifact_sha256") == expected_sha, (
        f"{script_name} envelope artifact_sha256="
        f"{envelope.get('artifact_sha256')!r} != sha256 of the diff it "
        f"verified {expected_sha!r}")


# ── publisher: refuse a diff that changed after verification ────────────────

class _PublishHarness:
    def __init__(self, tmp_path, monkeypatch):
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        self.diff = self.run_dir / "framework-edit.diff"
        self.diff.write_text(_DIFF_TEXT, encoding="utf-8")
        (self.run_dir / "verdict.json").write_text(json.dumps({
            "files_changed": 1, "tests_pass": True, "static_pass": True,
            "pass": True}), encoding="utf-8")
        monkeypatch.setenv("MINI_ORK_RUN_DIR", str(self.run_dir))
        monkeypatch.delenv("MINI_ORK_RECIPE_ROOT", raising=False)
        self.calls = {"set_status": [], "mark_published": 0}
        monkeypatch.setattr(
            publisher, "set_status",
            lambda db, run_id, status: self.calls["set_status"].append(status))
        monkeypatch.setattr(
            publisher, "_mark_published",
            lambda db, run_id: self.calls.__setitem__(
                "mark_published", self.calls["mark_published"] + 1))

    def diff_sha(self):
        return hashlib.sha256(self.diff.read_bytes()).hexdigest()

    def write_envelope(self, stem, fields):
        payload = {"verifier": stem, "pass": True, "verdict": "pass"}
        payload.update(fields)
        (self.run_dir / f"verifier_{stem}.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def publish(self, capsys):
        rc, finish = publisher.publisher_node(
            ENGINE_ROOT, str(self.run_dir), "", "", "framework-edit",
            "framework_edit")
        captured = capsys.readouterr()
        return rc, finish, captured.out + captured.err

    @property
    def abstain_record(self):
        return self.run_dir / "publish-abstain.json"


def test_unchanged_diff_publishes(tmp_path, monkeypatch, capsys):
    h = _PublishHarness(tmp_path, monkeypatch)
    sha = h.diff_sha()
    h.write_envelope("test", {"artifact_sha256": sha})
    h.write_envelope("static-check", {"artifact_sha256": sha})

    rc, finish, output = h.publish(capsys)

    assert REFUSAL not in output, output
    assert not h.abstain_record.exists()
    assert h.calls["mark_published"] == 1, output
    assert "failed" not in h.calls["set_status"]


def test_diff_rewritten_after_verification_is_refused(tmp_path, monkeypatch, capsys):
    h = _PublishHarness(tmp_path, monkeypatch)
    verified_sha = h.diff_sha()
    h.write_envelope("test", {"artifact_sha256": verified_sha})
    h.write_envelope("static-check", {"artifact_sha256": verified_sha})
    h.diff.write_text(_DIFF_TEXT + "+sneaked-in line\n", encoding="utf-8")

    rc, finish, output = h.publish(capsys)

    assert REFUSAL in output, f"rc={rc} finish={finish!r}; {output}"
    assert h.abstain_record.is_file()
    record = json.loads(h.abstain_record.read_text(encoding="utf-8"))
    assert record.get("reason")
    assert "failed" in h.calls["set_status"]
    assert h.calls["mark_published"] == 0


def test_any_single_stale_pin_refuses(tmp_path, monkeypatch, capsys):
    h = _PublishHarness(tmp_path, monkeypatch)
    h.write_envelope("test", {"artifact_sha256": h.diff_sha()})
    h.write_envelope("static-check", {"artifact_sha256": "0" * 64})

    rc, finish, output = h.publish(capsys)

    assert REFUSAL in output, f"rc={rc} finish={finish!r}; {output}"
    assert h.calls["mark_published"] == 0
    assert "failed" in h.calls["set_status"]


def test_older_envelopes_without_field_do_not_block(tmp_path, monkeypatch, capsys):
    h = _PublishHarness(tmp_path, monkeypatch)
    h.write_envelope("test", {})
    h.write_envelope("static-check", {})

    rc, finish, output = h.publish(capsys)

    assert REFUSAL not in output, output
    assert not h.abstain_record.exists()
    assert h.calls["mark_published"] == 1, output
