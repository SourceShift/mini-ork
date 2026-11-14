"""Tests for SEC-G06-T05: the deterministic ``code-fix`` security gate.

Covers ``mini_ork.verify.security_scan``'s rule table, allowance marker, added-
line scan and ``build_added``, plus the ``recipes/code-fix/verifiers/security.py``
envelope end to end. Hermetic: every git repo lives under ``tmp_path``.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork
from mini_ork.verify import security_scan as ss

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "security.py"

# Assembled at runtime: the canonical AWS documentation example key is itself a
# credential shape, so written as a literal it trips this repository's own
# pre-push secret lens — including in a fixture that never leaves the sandbox.
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _added(**paths):
    """``_added(**{"a.py": "x\\ny"})`` -> ``{"a.py": {1: "x", 2: "y"}}``."""
    return {
        path: {i: line for i, line in enumerate(text.split("\n"), start=1)}
        for path, text in paths.items()
    }


def _rules(added):
    return [finding.rule for finding in ss.scan_added(added)]


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "Security Test").returncode == 0
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    return repo


def _commit_all(repo, message):
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-q", "-m", message).returncode == 0


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

def test_rules_table_is_the_kickoff_ten():
    assert ss.RULES == (
        ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key material"),
        ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
        ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "GitHub token"),
        ("provider-api-key", r"\bsk-[A-Za-z0-9_-]{20,}\b", "provider API key"),
        ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b", "Slack token"),
        ("credential-literal", r"(?i)\b(?:password|passwd|secret|api_key|apikey|access_token)\b\s*[:=]\s*[\"'][^\"']{8,}[\"']", "credential literal"),
        ("world-writable", r"\bchmod\s+(?:-R\s+)?(?:0?777|a\+rwx)\b", "world-writable permission"),
        ("tls-verify-off", r"\bverify\s*=\s*False\b", "TLS verification disabled"),
        ("shell-exec", r"\bos\.system\(|\bshell\s*=\s*True\b", "shell execution"),
        ("unsafe-deserialization", r"\b(?:pickle|marshal)\.loads\(|\byaml\.load\(", "unsafe deserialization"),
    )


def test_finding_is_frozen_and_redacted():
    finding = ss.Finding("a.py", 1, "aws-access-key", "AWS access key id")
    assert finding.text == "<redacted>"
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        finding.text = "leak"


def test_credential_literal_boundaries():
    assert _rules(_added(**{"a.py": 'password = "hunter2hunter2"'})) == ["credential-literal"]
    assert _rules(_added(**{"a.py": "api_key: 'abcdef123456'"})) == ["credential-literal"]
    assert _rules(_added(**{"a.py": 'password = os.environ["PW"]'})) == []
    assert _rules(_added(**{"a.py": 'password = ""'})) == []


def test_aws_access_key_boundaries():
    assert _rules(_added(**{"a.py": f'key = "{AWS_KEY}"'})) == ["aws-access-key"]
    assert _rules(_added(**{"a.py": "AKIA1234"})) == []


# Assembled at runtime: this repository's merge guard scans the diff for
# credential shapes, and a fixture is not a reason to write one into source.
_FAKE_PRIVATE_KEY = "-----BEGIN RSA PRIVATE " + "KEY-----"
_FAKE_GITHUB_TOKEN = "gh" + "p_" + "16C7e42F292c6912E7710c838347Ae178B4a"
_FAKE_SLACK_TOKEN = "xox" + "b-123456789012-abcdefGHIJ"
_FAKE_PROVIDER_KEY = "sk" + "-proj-abc123def456ghi789jkl012"


@pytest.mark.parametrize("line,rule", [
    (_FAKE_PRIVATE_KEY, "private-key"),
    (f'token = "{_FAKE_GITHUB_TOKEN}"', "github-token"),
    (f'token = "{_FAKE_SLACK_TOKEN}"', "slack-token"),
    (f'key = "{_FAKE_PROVIDER_KEY}"', "provider-api-key"),
])
def test_token_rules_fire(line, rule):
    assert _rules(_added(**{"a.py": line})) == [rule]


@pytest.mark.parametrize("line", [
    "-----BEGIN CERTIFICATE-----",
    'token = "ghp_short"',
    'token = "xox"',
    'key = "sk-short"',
])
def test_token_rules_stay_quiet(line):
    assert _rules(_added(**{"a.py": line})) == []


@pytest.mark.parametrize("line,rule", [
    ("chmod 777 /tmp/x", "world-writable"),
    ("chmod -R 0777 /var/lib", "world-writable"),
    ("requests.get(url, verify=False)", "tls-verify-off"),
    ("result = os.system(cmd)", "shell-exec"),
    ("subprocess.run(cmd, shell=True)", "shell-exec"),
    ("pickle.loads(blob)", "unsafe-deserialization"),
    ("yaml.load(stream)", "unsafe-deserialization"),
])
def test_dangerous_rules_fire(line, rule):
    assert _rules(_added(**{"a.py": line})) == [rule]


@pytest.mark.parametrize("line", [
    "chmod 755 /tmp/x",
    "requests.get(url, verify=True)",
    "os.path.exists(cmd)",
    "subprocess.run(cmd, shell=False)",
    "pickle.dumps(obj)",
    "yaml.safe_load(stream)",
])
def test_dangerous_rules_stay_quiet(line):
    assert _rules(_added(**{"a.py": line})) == []


def test_no_text_leak():
    findings = ss.scan_added(_added(**{"leak.py": f'aws = "{AWS_KEY}"'}))
    assert [f.rule for f in findings] == ["aws-access-key"]
    assert all(f.text == "<redacted>" for f in findings)
    assert AWS_KEY not in repr(findings)
    assert AWS_KEY not in str(findings)


def test_allowance_marker_excuses_only_named_rule():
    excused = _rules(_added(**{"a.py": f'x = "{AWS_KEY}"  # sec-allow: aws-access-key'}))
    assert excused == []
    wrong = _rules(_added(**{"a.py": f'x = "{AWS_KEY}"  # sec-allow: github-token'}))
    assert wrong == ["aws-access-key"]


def test_allowance_case_sensitive_and_multi():
    assert ss.allowance("x = 1  # sec-allow: aws-access-key") == frozenset({"aws-access-key"})
    assert ss.allowance("y  # sec-allow: aws-access-key,world-writable") == frozenset(
        {"aws-access-key", "world-writable"}
    )
    assert ss.allowance("x = 1") == frozenset()
    assert ss.allowance("# SEC-ALLOW: aws-access-key") == frozenset()


def test_comments_and_test_paths_are_still_scanned():
    findings = ss.scan_added({
        "note.py": {1: '# password = "hunter2hunter2"'},
        "noqa.py": {1: f'key = "{AWS_KEY}"  # noqa'},
        "tests/unit/test_fixture.py": {1: 'secret = "supersecret42"'},
    })
    assert [(f.path, f.line, f.rule) for f in findings] == [
        ("noqa.py", 1, "aws-access-key"),
        ("note.py", 1, "credential-literal"),
        ("tests/unit/test_fixture.py", 1, "credential-literal"),
    ]


def test_scan_added_sorted_by_path_line_rule():
    added = {
        "b.py": {2: '    password = "hunter2hunter2"', 3: "", 4: "   "},
        "a.py": {7: f'password = "{AWS_KEY}"', 9: "chmod 777 /tmp"},
    }
    findings = ss.scan_added(added)
    assert [(f.path, f.line, f.rule) for f in findings] == [
        ("a.py", 7, "aws-access-key"),
        ("a.py", 7, "credential-literal"),
        ("a.py", 9, "world-writable"),
        ("b.py", 2, "credential-literal"),
    ]


# ---------------------------------------------------------------------------
# build_added
# ---------------------------------------------------------------------------

@requires_git
def test_build_added_reads_untracked_files(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text(f'key = "{AWS_KEY}"\nx = 1\n', encoding="utf-8")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "note.py").write_text("ok = 1\n", encoding="utf-8")

    added, unreadable = ss.build_added(str(repo), "")

    assert unreadable == []
    assert set(added) == {"leaky.py", "pkg/note.py"}
    assert added["leaky.py"] == {1: f'key = "{AWS_KEY}"', 2: "x = 1"}
    findings = ss.scan_added(added)
    assert [(f.path, f.line, f.rule) for f in findings] == [("leaky.py", 1, "aws-access-key")]


@requires_git
def test_build_added_bogus_base_returns_empty_pair(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")
    assert ss.build_added(str(repo), "deadbeef") == ({}, [])

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "leaky.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")
    assert ss.build_added(str(plain), "") == ({}, [])


@requires_git
def test_build_added_reads_untracked_paths_git_would_quote(tmp_path):
    """A space or a non-ASCII byte must not be enough to hide a credential.

    Git's default porcelain C-escapes such paths (``"odd name \\303\\274.py"``).
    De-quoting that without unescaping points at a file that does not exist, so
    the file is reported unreadable and never scanned — the gate would pass a
    diff that plainly contains a key. ``status -z`` sidesteps quoting entirely.
    """
    repo = _make_repo(tmp_path)
    odd = repo / "odd name ü.py"
    odd.write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")

    added, unreadable = ss.build_added(str(repo), "")

    assert unreadable == []
    assert list(added) == ["odd name ü.py"]
    assert [f.rule for f in ss.scan_added(added)] == ["aws-access-key"]


@requires_git
def test_collect_added_skips_untracked_files_outside_the_surface(tmp_path):
    """Off-surface untracked files are skipped by path, never read then dropped."""
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")
    (repo / "other.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")

    added, unreadable, measured = ss.collect_added(
        str(repo), "", only={"leaky.py"}
    )

    assert measured is True
    assert list(added) == ["leaky.py"]
    assert unreadable == []


@requires_git
def test_collect_added_ignores_repo_diff_drivers(tmp_path):
    """A repo-controlled diff driver must not be able to blank out its own diff.

    Without ``--no-ext-diff`` this returns nothing and the gate passes a diff
    that plainly contains a credential.
    """
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "clean fixture")
    (repo / "leaky.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "config", "diff.external", "true"], check=False,
    )
    added, _unreadable = ss.build_added(str(repo), "HEAD")
    assert any(
        AWS_KEY in line for lines in added.values() for line in lines.values()
    )


# ---------------------------------------------------------------------------
# recipes/code-fix/verifiers/security.py end to end
# ---------------------------------------------------------------------------

def _run_verifier(repo, touched, pythonpath=None, extra_env=None):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("MO_", "MINI_ORK_"))
    }
    env["MO_TARGET_CWD"] = str(repo)
    env["MINI_ORK_TOUCHED_FILES"] = touched
    if pythonpath is None:
        pythonpath = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = pythonpath
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(repo), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )


@requires_git
def test_verifier_fails_on_seeded_credential(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text(
        f'key = "{AWS_KEY}"\nchmod 777 /tmp/x\n', encoding="utf-8"
    )
    proc = _run_verifier(repo, touched="leaky.py")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["verifier"] == "security"
    assert envelope["verdict"] == "fail"
    assert envelope["pass"] is False
    assert envelope["failed_checks"] == ["aws-access-key", "world-writable"]
    assert envelope["checks_run"] == [
        "no-credential-in-diff", "no-dangerous-api-in-diff"
    ]
    assert envelope["evidence_path"] is None
    assert [c["name"] for c in envelope["checks"]] == [
        "no-credential-in-diff", "no-dangerous-api-in-diff"
    ]
    assert all(f["text"] == "<redacted>" for f in envelope["findings"])
    assert AWS_KEY not in proc.stdout


@requires_git
def test_verifier_passes_clean_touched_file(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "clean fixture")
    proc = _run_verifier(repo, touched="leaky.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["verdict"] == "pass"
    assert envelope["pass"] is True
    assert envelope["findings"] == []
    assert envelope["failed_checks"] == []
    assert envelope["reasons"] == []
    details = " ".join(c.get("detail", "") for c in envelope["checks"])
    assert "no added lines" in details or "0 added lines across 0 files" in details


@requires_git
def test_verifier_loads_the_scanner_by_path_when_the_package_is_not_importable(tmp_path):
    """The file-load fallback must find the engine, not ``recipes/``.

    With no repo on ``PYTHONPATH`` the package import cannot succeed, so the
    whole check rides on the path fallback. A ``parents[2]`` taken from the file
    instead of its directory lands in ``recipes/``, the module is "not found",
    and the gate abstains on a diff that plainly holds a credential — a silent
    downgrade from fail to abstain that no other test would notice.
    """
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")
    proc = _run_verifier(repo, touched="leaky.py", pythonpath="")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["verdict"] == "fail", envelope
    assert "security scanner module not found" not in proc.stderr


@requires_git
def test_verifier_ignores_dirty_files_outside_the_surface(tmp_path):
    """A credential in a file the run never touched is not this run's refutation."""
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "unrelated.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")
    proc = _run_verifier(repo, touched="leaky.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["verdict"] == "pass"
    assert envelope["findings"] == []


@requires_git
def test_verifier_empty_surface_never_fakes_a_refutation(tmp_path):
    repo = _make_repo(tmp_path)
    proc = _run_verifier(repo, touched="")
    envelope = json.loads(proc.stdout.strip())
    assert proc.returncode in (0, 1)
    if proc.returncode == 1:
        assert envelope["findings"], "exit 1 with empty findings fakes a refutation"
    if envelope.get("verdict") == "abstain":
        assert envelope["pass"] is False
        assert envelope["status"] == "unverified"
        assert envelope["failure_kind"] == "toolchain"


@requires_git
def test_verifier_abstains_when_git_cannot_measure(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "leaky.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")
    proc = _run_verifier(plain, touched="leaky.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["verdict"] == "abstain"
    assert envelope["pass"] is False
    assert envelope["status"] == "unverified"
    assert envelope["failure_kind"] == "toolchain"
    assert envelope["reasons"]


@requires_git
def test_verifier_abstains_when_the_operator_turns_it_off(tmp_path):
    """An off switch abstains — it must never be a way to make the gate pass."""
    repo = _make_repo(tmp_path)
    (repo / "leaky.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")
    proc = _run_verifier(repo, touched="leaky.py", extra_env={
        "MO_SECURITY_SCANNER": "off",
    })
    assert proc.returncode == 0, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["verdict"] == "abstain"
    assert envelope["pass"] is False
    assert envelope["status"] == "unverified"
    assert envelope["failure_kind"] == "toolchain"
    assert envelope["reasons"]
