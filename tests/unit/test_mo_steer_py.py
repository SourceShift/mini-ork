"""Standalone unit tests for ``mini_ork.ported.mo_steer``.

Replaces the bash-parity gate that used to live in this file (it drove a
LIVE ``bash lib/mo-steer.sh`` subprocess for eight cases, including a
hardcoded ~30s ``--wait-ack`` timeout wait) as part of the bash->Python
migration: the Python port is now the sole implementation, so its
coverage no longer shells out to bash — it asserts the port's behaviour
directly against its public surface (``mo_steer.__all__``): ``steer``,
``_resolve_steer_paths``, ``_infer_job_from_epic``, ``_check_heartbeat``,
``_heartbeat_state``, ``_new_steer_id``, and ``_now_iso``.

These pin the deterministic contract the CLI surface must keep
(envelope shape, heartbeat liveness gating, job/iter-dir derivation,
and wait-ack polling) independent of any bash oracle. Timing-sensitive
paths (stale heartbeat, wait-ack timeout) are exercised with
monkeypatched clocks/thresholds so the whole suite runs in well under a
second instead of the ~34s the old subprocess-based gate took.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mini_ork.ported import mo_steer as ms


def _hb_line(state: str) -> str:
    """One compact-JSON heartbeat line (matches the port's ``"state":"..."`` regex)."""
    return json.dumps({"state": state, "at": int(time.time())}, separators=(",", ":")) + "\n"


def _read_envelope(steer_file: Path) -> dict[str, str]:
    """Read the single JSON envelope written to ``steer_file``."""
    lines = [ln for ln in steer_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 envelope line, got {len(lines)}"
    return json.loads(lines[0])


def _make_iter_dir(
    tmp_path: Path, *, state: str = "running", age_secs: int = 0
) -> tuple[Path, Path, Path, Path]:
    """Create ``<tmp>/iter-1/{STEER.jsonl, HEARTBEAT, worker.log}``.

    Returns ``(iter_dir, steer_file, heartbeat, worker_log)``. When
    ``age_secs`` is nonzero the heartbeat's mtime is backdated by that
    many seconds (to simulate a stale heartbeat).
    """
    iter_dir = tmp_path / "iter-1"
    iter_dir.mkdir(parents=True)
    sf = iter_dir / "STEER.jsonl"
    hb = iter_dir / "HEARTBEAT"
    wl = iter_dir / "worker.log"
    sf.write_text("")
    hb.write_text(_hb_line(state))
    if age_secs:
        backdate = time.time() - age_secs
        os.utime(hb, (backdate, backdate))
    wl.write_text("")
    return iter_dir, sf, hb, wl


# ─────────────────────────────────────────────────────────────────────────────
# _now_iso
# ─────────────────────────────────────────────────────────────────────────────
class TestNowIso:
    def test_format_matches_utc_iso8601_with_trailing_z(self) -> None:
        s = ms._now_iso()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", s)

    def test_represents_the_current_instant(self) -> None:
        s = ms._now_iso()
        parsed = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert abs((now - parsed).total_seconds()) < timedelta(seconds=5).total_seconds()


# ─────────────────────────────────────────────────────────────────────────────
# _new_steer_id
# ─────────────────────────────────────────────────────────────────────────────
class TestNewSteerId:
    def test_returns_32_char_lowercase_hex(self) -> None:
        assert re.fullmatch(r"[0-9a-f]{32}", ms._new_steer_id())

    def test_ids_are_unique_across_calls(self) -> None:
        assert ms._new_steer_id() != ms._new_steer_id()


# ─────────────────────────────────────────────────────────────────────────────
# _infer_job_from_epic
# ─────────────────────────────────────────────────────────────────────────────
class TestInferJobFromEpic:
    @pytest.mark.parametrize(
        ("epic", "expected"),
        [
            ("EXPL-DLG-C", "expl-dlg"),
            ("FOO-9", "foo"),
            ("FOO-BAR-1", "foo-bar"),
            ("SINGLE", "single"),
            ("", ""),
            # No trailing "-<UPPER|DIGIT>" run to strip (already lowercase) —
            # the sub is a no-op, only .lower() applies.
            ("foo-bar", "foo-bar"),
        ],
    )
    def test_strips_trailing_suffix_and_lowercases(self, epic: str, expected: str) -> None:
        assert ms._infer_job_from_epic(epic) == expected


# ─────────────────────────────────────────────────────────────────────────────
# _heartbeat_state
# ─────────────────────────────────────────────────────────────────────────────
class TestHeartbeatState:
    def test_empty_path_is_unknown(self) -> None:
        assert ms._heartbeat_state("") == "unknown"

    def test_missing_file_is_unknown(self, tmp_path: Path) -> None:
        assert ms._heartbeat_state(str(tmp_path / "nope")) == "unknown"

    def test_empty_file_is_unknown(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text("")
        assert ms._heartbeat_state(str(hb)) == "unknown"

    def test_reads_state_from_compact_json(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(_hb_line("running"))
        assert ms._heartbeat_state(str(hb)) == "running"

    def test_picks_last_line_of_a_multiline_file(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(_hb_line("running") + _hb_line("done"))
        assert ms._heartbeat_state(str(hb)) == "done"

    def test_non_compact_json_is_unknown(self, tmp_path: Path) -> None:
        # bash's `grep -oE '"state":"[^"]+"'` requires no whitespace around
        # the colon; the port mirrors that exactly (see docstring).
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(json.dumps({"state": "running"}) + "\n")  # has ": " space
        assert ms._heartbeat_state(str(hb)) == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# _check_heartbeat
# ─────────────────────────────────────────────────────────────────────────────
class TestCheckHeartbeat:
    def test_missing_heartbeat_is_missing(self, tmp_path: Path) -> None:
        assert ms._check_heartbeat(str(tmp_path / "nope")) == "missing"

    def test_missing_heartbeat_is_missing_even_with_force(self, tmp_path: Path) -> None:
        assert ms._check_heartbeat(str(tmp_path / "nope"), force=True) == "missing"

    def test_fresh_running_heartbeat_is_ok(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(_hb_line("running"))
        assert ms._check_heartbeat(str(hb)) == "ok"

    def test_stale_heartbeat_is_stale(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(_hb_line("running"))
        backdate = time.time() - 20
        os.utime(hb, (backdate, backdate))
        assert ms._check_heartbeat(str(hb)) == "stale"

    def test_force_bypasses_staleness(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(_hb_line("running"))
        backdate = time.time() - 20
        os.utime(hb, (backdate, backdate))
        assert ms._check_heartbeat(str(hb), force=True) == "ok"

    def test_state_done_is_reported(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(_hb_line("done"))
        assert ms._check_heartbeat(str(hb)) == "done"

    def test_state_aborting_is_reported(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(_hb_line("aborting"))
        assert ms._check_heartbeat(str(hb)) == "aborting"

    def test_force_bypasses_done_state(self, tmp_path: Path) -> None:
        hb = tmp_path / "HEARTBEAT"
        hb.write_text(_hb_line("done"))
        assert ms._check_heartbeat(str(hb), force=True) == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_steer_paths
# ─────────────────────────────────────────────────────────────────────────────
class TestResolveSteerPaths:
    def test_override_branch_derives_heartbeat_and_log_from_dirname(self, tmp_path: Path) -> None:
        sf = tmp_path / "iter-1" / "STEER.jsonl"
        result = ms._resolve_steer_paths(epic="EXPL-DLG-C", steer_file=str(sf), job="", iter=None)
        assert result == (
            str(sf),
            str(tmp_path / "iter-1" / "HEARTBEAT"),
            str(tmp_path / "iter-1" / "worker.log"),
            "",
        )

    def test_override_branch_honors_explicit_heartbeat_and_log(self, tmp_path: Path) -> None:
        sf = tmp_path / "STEER.jsonl"
        hb = tmp_path / "custom-hb"
        wl = tmp_path / "custom-log"
        result = ms._resolve_steer_paths(
            epic="X", steer_file=str(sf), job="", iter=None, heartbeat=str(hb), log=str(wl)
        )
        assert result == (str(sf), str(hb), str(wl), "")

    def test_derive_branch_raises_when_epic_dir_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ms, "_repo_root", lambda: str(tmp_path))
        monkeypatch.setenv("MINI_ORK_HOME", ".")
        with pytest.raises(RuntimeError, match="ERROR: epic dir not found"):
            ms._resolve_steer_paths(epic="EXPL-DLG-C", steer_file="", job="", iter=None)

    def test_derive_branch_raises_when_iter_dir_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ms, "_repo_root", lambda: str(tmp_path))
        monkeypatch.setenv("MINI_ORK_HOME", ".")
        epic_dir = tmp_path / "runs" / "expl-dlg" / "EXPL-DLG-C"
        epic_dir.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="ERROR: iter dir not found"):
            ms._resolve_steer_paths(epic="EXPL-DLG-C", steer_file="", job="", iter=None)

    def test_derive_branch_infers_job_from_epic_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ms, "_repo_root", lambda: str(tmp_path))
        monkeypatch.setenv("MINI_ORK_HOME", ".")
        iter_dir = tmp_path / "runs" / "expl-dlg" / "EXPL-DLG-C" / "iter-1"
        iter_dir.mkdir(parents=True)
        sf, hb, wl, inferred_job = ms._resolve_steer_paths(
            epic="EXPL-DLG-C", steer_file="", job="", iter=None
        )
        assert inferred_job == "expl-dlg"
        assert sf == str(iter_dir / "STEER.jsonl")
        assert hb == str(iter_dir / "HEARTBEAT")
        assert wl == str(iter_dir / "worker.log")

    def test_derive_branch_explicit_job_overrides_inference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ms, "_repo_root", lambda: str(tmp_path))
        monkeypatch.setenv("MINI_ORK_HOME", ".")
        iter_dir = tmp_path / "runs" / "custom-job" / "EXPL-DLG-C" / "iter-1"
        iter_dir.mkdir(parents=True)
        sf, _hb, _wl, inferred_job = ms._resolve_steer_paths(
            epic="EXPL-DLG-C", steer_file="", job="custom-job", iter=None
        )
        assert inferred_job == "custom-job"
        assert sf == str(iter_dir / "STEER.jsonl")

    def test_derive_branch_picks_latest_iter_with_natural_sort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ms, "_repo_root", lambda: str(tmp_path))
        monkeypatch.setenv("MINI_ORK_HOME", ".")
        epic_dir = tmp_path / "runs" / "expl-dlg" / "EXPL-DLG-C"
        for n in (1, 2, 10):
            (epic_dir / f"iter-{n}").mkdir(parents=True)
        sf, _hb, _wl, _job = ms._resolve_steer_paths(
            epic="EXPL-DLG-C", steer_file="", job="", iter=None
        )
        # sort -V semantics: iter-10 sorts after iter-2, not before (lexical
        # sort would have picked iter-2 as "latest").
        assert sf == str(epic_dir / "iter-10" / "STEER.jsonl")

    def test_derive_branch_explicit_iter_pins_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ms, "_repo_root", lambda: str(tmp_path))
        monkeypatch.setenv("MINI_ORK_HOME", ".")
        epic_dir = tmp_path / "runs" / "expl-dlg" / "EXPL-DLG-C"
        for n in (1, 2, 10):
            (epic_dir / f"iter-{n}").mkdir(parents=True)
        sf, _hb, _wl, _job = ms._resolve_steer_paths(
            epic="EXPL-DLG-C", steer_file="", job="", iter=2
        )
        assert sf == str(epic_dir / "iter-2" / "STEER.jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# steer — end-to-end envelope + gating behavior
# ─────────────────────────────────────────────────────────────────────────────
class TestSteer:
    def test_missing_epic_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="missing epic-id"):
            ms.steer("", "hello")

    def test_empty_message_raises_value_error(self, tmp_path: Path) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path)
        with pytest.raises(ValueError, match="ERROR: empty message"):
            ms.steer("EXPL-DLG-C", "", steer_file=str(sf))

    def test_happy_path_writes_envelope_and_returns_id(self, tmp_path: Path) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path)
        result = ms.steer("EXPL-DLG-C", "hello world", steer_file=str(sf), from_="tester")
        env = _read_envelope(sf)
        assert sorted(env.keys()) == ["at", "body", "from", "id"]
        assert env["from"] == "tester"
        assert env["body"] == "hello world"
        assert env["id"] == result["id"]
        assert re.fullmatch(r"[0-9a-f]{32}", result["id"])
        assert result["steer_file"] == str(sf)
        assert result["envelope"] == env

    def test_stderr_wrote_line_matches_bash_format(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path)
        ms.steer("EXPL-DLG-C", "hello world", steer_file=str(sf), from_="tester")
        captured = capsys.readouterr()
        assert re.search(
            r"\[mo-steer\] wrote 11-byte steer \(id=[0-9a-f]{32}\) -> .*STEER\.jsonl", captured.err
        )

    def test_explicit_steer_file_logs_notice(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path)
        ms.steer("EXPL-DLG-C", "hi", steer_file=str(sf))
        captured = capsys.readouterr()
        assert "[mo-steer] using explicit steer file:" in captured.err

    def test_creates_parent_directory_if_missing(self, tmp_path: Path) -> None:
        sf = tmp_path / "nested" / "dir" / "STEER.jsonl"
        ms.steer("EXPL-DLG-C", "hi", steer_file=str(sf), force=True)
        assert sf.exists()
        assert _read_envelope(sf)["body"] == "hi"

    def test_appends_without_clobbering_existing_lines(self, tmp_path: Path) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path)
        ms.steer("EXPL-DLG-C", "first", steer_file=str(sf), from_="tester")
        ms.steer("EXPL-DLG-C", "second", steer_file=str(sf), from_="tester")
        lines = [ln for ln in sf.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2
        assert json.loads(lines[0])["body"] == "first"
        assert json.loads(lines[1])["body"] == "second"

    def test_stale_heartbeat_raises_runtime_error(self, tmp_path: Path) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path, age_secs=20)
        with pytest.raises(RuntimeError, match=r"ERROR: heartbeat stale \(\d+s old\)"):
            ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf))
        assert sf.read_text() == ""

    def test_force_bypasses_stale_heartbeat(self, tmp_path: Path) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path, age_secs=20)
        result = ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf), force=True)
        assert _read_envelope(sf)["body"] == "msg"
        assert result["envelope"]["body"] == "msg"

    def test_heartbeat_state_done_raises_runtime_error(self, tmp_path: Path) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path, state="done")
        with pytest.raises(RuntimeError, match="ERROR: heartbeat says state=done"):
            ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf))
        assert sf.read_text() == ""

    def test_heartbeat_state_aborting_raises_runtime_error(self, tmp_path: Path) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path, state="aborting")
        with pytest.raises(RuntimeError, match="ERROR: heartbeat says state=aborting"):
            ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf))

    def test_force_bypasses_heartbeat_state_done(self, tmp_path: Path) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path, state="done")
        result = ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf), force=True)
        assert result["envelope"]["body"] == "msg"

    def test_missing_heartbeat_file_is_allowed(self, tmp_path: Path) -> None:
        # No HEARTBEAT file at all -> bash's `[ -f "$HEARTBEAT" ]` guard
        # skips the liveness check entirely -> steer proceeds.
        sf = tmp_path / "iter-1" / "STEER.jsonl"
        result = ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf))
        assert result["envelope"]["body"] == "msg"

    def test_from_defaults_to_user_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path)
        monkeypatch.setenv("USER", "envuser")
        ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf))
        assert _read_envelope(sf)["from"] == "envuser"

    def test_from_defaults_to_user_literal_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path)
        monkeypatch.delenv("USER", raising=False)
        ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf))
        assert _read_envelope(sf)["from"] == "user"

    def test_derive_branch_infers_job_and_writes_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ms, "_repo_root", lambda: str(tmp_path))
        monkeypatch.setenv("MINI_ORK_HOME", ".")
        iter_dir = tmp_path / "runs" / "expl-dlg" / "EXPL-DLG-C" / "iter-1"
        iter_dir.mkdir(parents=True)
        (iter_dir / "HEARTBEAT").write_text(_hb_line("running"))
        result = ms.steer("EXPL-DLG-C", "msg-from-py", iter=1)
        assert result["steer_file"] == str(iter_dir / "STEER.jsonl")
        assert _read_envelope(iter_dir / "STEER.jsonl")["body"] == "msg-from-py"

    def test_derive_branch_missing_epic_dir_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ms, "_repo_root", lambda: str(tmp_path))
        monkeypatch.setenv("MINI_ORK_HOME", ".")
        with pytest.raises(RuntimeError, match="ERROR: epic dir not found"):
            ms.steer("EXPL-DLG-C", "msg")

    def test_wait_ack_success_returns_immediately_when_event_preexists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, sf, _hb, wl = _make_iter_dir(tmp_path)
        known_id = "0" * 32
        monkeypatch.setattr(ms, "_new_steer_id", lambda: known_id)
        wl.write_text(
            json.dumps({"event": "steer_yielded", "steer_id": known_id}, separators=(",", ":")) + "\n"
        )
        result = ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf), wait_ack=True)
        assert result["id"] == known_id

    def test_wait_ack_success_logs_ack_received(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, sf, _hb, wl = _make_iter_dir(tmp_path)
        known_id = "1" * 32
        monkeypatch.setattr(ms, "_new_steer_id", lambda: known_id)
        wl.write_text(
            json.dumps({"event": "steer_yielded", "steer_id": known_id}, separators=(",", ":")) + "\n"
        )
        ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf), wait_ack=True)
        captured = capsys.readouterr()
        assert "[mo-steer] ack received in 0s — steer delivered + queued" in captured.err

    def test_wait_ack_timeout_raises_runtime_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, sf, _hb, _wl = _make_iter_dir(tmp_path)
        # Shrink the timeout/tick so the timeout path resolves in well
        # under a second instead of bash's hardcoded 30s poll.
        monkeypatch.setattr(ms, "_WAIT_ACK_TIMEOUT_SECS", 0.05)
        monkeypatch.setattr(ms, "_WAIT_ACK_TICK_SECS", 0.01)
        with pytest.raises(RuntimeError, match=r"WARN: no ack within 0\.05s") as exc_info:
            ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf), wait_ack=True)
        assert "message in queue but unconfirmed" in str(exc_info.value)
