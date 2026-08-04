"""Unit tests for the leaked-sandbox reaper (sandbox P4a).

The Docker CLI is faked entirely through the module's single ``_run`` seam and
``shutil.which`` — NO real daemon, no containers. Each test drives the reaper by
handing it a canned ``docker ps`` listing and asserting which ``docker rm -f``
calls it did (or, for dry-run, did not) issue.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from mini_ork.runtime import sandbox_reaper

_NOW = 1_700_000_000.0  # fixed clock so "age" is deterministic


def _fmt(epoch: float) -> str:
    """Render an epoch as Docker's ``{{.CreatedAt}}`` (``… +0000 UTC``)."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S %z") + " UTC"


def _cp(argv: list[str], rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=out, stderr="")


class FakeDocker:
    """A stand-in for ``sandbox_reaper._run`` that records every invocation.

    ``containers`` maps container-id → creation epoch (or ``None`` to emit an
    unparseable created-at). ``info_rc`` lets a test simulate a dead daemon.
    """

    def __init__(self, containers: dict[str, float | None], *, info_rc: int = 0):
        self.containers = containers
        self.info_rc = info_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        sub = argv[1] if len(argv) > 1 else ""
        if sub == "info":
            return _cp(argv, self.info_rc, "27.0.0\n" if self.info_rc == 0 else "")
        if sub == "ps":
            lines = []
            for cid, created in self.containers.items():
                stamp = "garbage-timestamp" if created is None else _fmt(created)
                lines.append(f"{cid} {stamp}")
            body = "\n".join(lines)
            return _cp(argv, 0, body + ("\n" if body else ""))
        if sub == "rm":
            return _cp(argv, 0, "")
        return _cp(argv, 0, "")

    @property
    def rm_ids(self) -> list[str]:
        return [c[-1] for c in self.calls if c[1:3] == ["rm", "-f"]]


@pytest.fixture
def install(monkeypatch):
    """Wire a FakeDocker in as the reaper's ``_run`` + present ``docker`` CLI."""

    def _install(fake: FakeDocker) -> FakeDocker:
        monkeypatch.setattr(sandbox_reaper, "_run", fake)
        monkeypatch.setattr(
            sandbox_reaper.shutil, "which", lambda name: "/usr/bin/docker"
        )
        return fake

    return _install


def test_reaps_only_containers_older_than_max_age(install):
    fake = install(
        FakeDocker(
            {
                "old111": _NOW - 7200,  # 2h old → reap at 1h TTL
                "young22": _NOW - 600,  # 10m old → keep
            }
        )
    )
    reaped = sandbox_reaper.reap_docker(max_age_s=3600, now=_NOW)
    assert reaped == ["old111"]
    assert fake.rm_ids == ["old111"]  # young one was never touched


def test_lists_only_the_mo_sandbox_label(install):
    fake = install(FakeDocker({"old111": _NOW - 7200}))
    sandbox_reaper.reap_docker(max_age_s=3600, now=_NOW)
    ps_calls = [c for c in fake.calls if c[1:2] == ["ps"]]
    assert ps_calls, "expected a docker ps call"
    assert "--filter" in ps_calls[0]
    assert "label=mo.sandbox=1" in ps_calls[0]


def test_dry_run_selects_same_set_but_removes_nothing(install):
    fake = install(
        FakeDocker({"old111": _NOW - 7200, "old222": _NOW - 9000})
    )
    reaped = sandbox_reaper.reap_docker(max_age_s=3600, dry_run=True, now=_NOW)
    assert sorted(reaped) == ["old111", "old222"]
    assert fake.rm_ids == []  # NO rm -f under dry-run


def test_unparseable_created_at_is_skipped(install):
    fake = install(FakeDocker({"weird1": None, "old111": _NOW - 7200}))
    reaped = sandbox_reaper.reap_docker(max_age_s=3600, now=_NOW)
    assert reaped == ["old111"]  # the unparseable one is never reaped on a guess
    assert fake.rm_ids == ["old111"]


def test_missing_docker_cli_returns_empty(install, monkeypatch):
    fake = install(FakeDocker({"old111": _NOW - 7200}))
    monkeypatch.setattr(sandbox_reaper.shutil, "which", lambda name: None)
    assert sandbox_reaper.reap_docker(max_age_s=3600, now=_NOW) == []
    assert fake.calls == []  # never shelled out at all


def test_dead_daemon_returns_empty(install):
    fake = install(FakeDocker({"old111": _NOW - 7200}, info_rc=1))
    assert sandbox_reaper.reap_docker(max_age_s=3600, now=_NOW) == []
    assert fake.rm_ids == []  # info failed → we never listed or removed


def test_reap_sandboxes_docker_shape(install):
    install(FakeDocker({"old111": 946_684_800.0}))  # 2000-01-01, always old
    out = sandbox_reaper.reap_sandboxes(backend="docker", max_age_s=3600)
    assert out == {"docker": ["old111"]}


def test_reap_sandboxes_all_has_both_keys(install):
    install(FakeDocker({"old111": 946_684_800.0}))
    out = sandbox_reaper.reap_sandboxes(backend="all", max_age_s=3600)
    assert set(out) == {"docker", "microvm"}
    assert out["docker"] == ["old111"]
    assert out["microvm"] == []


def test_reap_sandboxes_unknown_backend_raises():
    with pytest.raises(ValueError):
        sandbox_reaper.reap_sandboxes(backend="nope", max_age_s=3600)


def test_reap_microvm_returns_empty_without_sdk():
    # microsandbox is not installed on the test host; the guarded import must
    # swallow the ImportError and yield [] rather than raising.
    assert sandbox_reaper.reap_microvm(max_age_s=3600) == []


def test_max_age_falls_back_to_env(install, monkeypatch):
    monkeypatch.setenv("MO_SANDBOX_MAX_AGE", "100")
    fake = install(
        FakeDocker({"old111": _NOW - 200, "young22": _NOW - 50})
    )
    # max_age_s=None → resolver reads MO_SANDBOX_MAX_AGE=100.
    reaped = sandbox_reaper.reap_docker(max_age_s=None, now=_NOW)
    assert reaped == ["old111"]
    assert fake.rm_ids == ["old111"]


def test_default_max_age_is_generous():
    assert sandbox_reaper.DEFAULT_MAX_AGE_S == 6 * 60 * 60
