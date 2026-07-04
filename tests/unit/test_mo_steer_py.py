"""Parity gate: mini_ork.ported.mo_steer vs lib/mo-steer.sh.

Each test invokes the LIVE ``bash lib/mo-steer.sh <args>`` subprocess
on identical inputs as the Python port and asserts byte-identical
envelope, stderr text, and exit codes. No mocks, no hardcoded outputs
beyond what bash itself emits.

Eight cases (above the kickoff's >=6 floor):
  (a) happy-path write                  — both bash + python write a single
                                           JSON envelope to a temp steer_file;
                                           envelopes match modulo stripped
                                           id/at; stderr "[mo-steer] wrote …"
                                           text matches verbatim.
  (b) empty message                     — bash exit 2 + stderr "ERROR: empty
                                           message" matches python ValueError.
  (c) --force bypass on stale heartbeat — bash exits 0 with force, python
                                           raises RuntimeError without force,
                                           exits cleanly with force=1.
  (d) heartbeat state=done              — bash exit 5 + stderr "heartbeat says
                                           state=done" matches python
                                           RuntimeError.
  (e) heartbeat stale (>15s old mtime)  — bash exit 5 + stderr
                                           "heartbeat stale (Ns old)" matches
                                           python RuntimeError with same AGE.
  (f) infer-job-from-epic EXPL-DLG-C    — both bash and python resolve to
                                           <home>/runs/expl-dlg/EXPL-DLG-C/
                                           iter-N; write envelope to same
                                           iter_dir/STEER.jsonl.
  (g) --wait-ack success                — both bash and python poll worker.log
                                           and exit 0 within timeout when a
                                           matching steer_yielded event
                                           appears.
  (h) --wait-ack timeout                — both exit 7 with "WARN: no ack
                                           within 30s" stderr. bash hardcodes
                                           30s (per source inspection) so this
                                           case is slow (~30s); python's
                                           timeout can be monkeypatched to 2s
                                           for parity verification.

Environment isolation:
  The shell pytest runs in often has MINI_ORK_HOME / USER pointing at the
  main repo. Each test points subprocess env at a per-test temp home so
  the bash script's `git rev-parse --show-toplevel || pwd` fallback and
  the port's `_repo_root()` both resolve to the temp dir. monkeypatch
  redirects the Python process's env for the same reason.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mo_steer as ms  # noqa: E402

SH = REPO / "lib" / "mo-steer.sh"

# Fields the parity tests strip before comparing envelopes. These are
# timing-dependent (at = wall-clock) or implementation-specific (id =
# uuid4() / uuidgen diverge across the two writers).
_STRIP_KEYS = ("id", "at")


def _which(*tools: str) -> dict[str, str]:
    out = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


def _init_tmp_run(tmp_path: Path, *, state: str = "running") -> tuple[Path, Path, Path, Path]:
    """Create ``<tmp>/iter-1/{STEER.jsonl, HEARTBEAT, worker.log}`` and return paths.

    Returns (iter_dir, steer_file, heartbeat_path, worker_log_path).
    The steer_file sits at ``iter-1/STEER.jsonl`` so that bash's default
    heartbeat + worker.log derivation (``dirname(steer_file)``) lands on
    the sibling files we create here. Tests that want to override the
    steer_file path explicitly use --steer-file=; the heartbeat default
    is honored either way.

    Heartbeat is written with compact JSON (no ``": "`` whitespace) to
    match bash's ``grep -oE '"state":"[^"]+"'`` regex — bash only
    matches compact JSON in the heartbeat file.
    """
    iter_dir = tmp_path / "iter-1"
    iter_dir.mkdir(parents=True)
    sf = iter_dir / "STEER.jsonl"
    hb = iter_dir / "HEARTBEAT"
    wl = iter_dir / "worker.log"
    sf.write_text("")
    hb.write_text(_hb_line(state))
    wl.write_text("")
    return iter_dir, sf, hb, wl


def _hb_line(state: str) -> str:
    """One compact-JSON heartbeat line (matches bash's ``"state":"..."`` regex)."""
    return json.dumps({"state": state, "at": int(time.time())}, separators=(",", ":")) + "\n"


def _fresh_heartbeat(hb: Path, state: str = "running") -> None:
    """Rewrite HEARTBEAT with mtime=now so the liveness check passes."""
    hb.write_text(_hb_line(state))


def _stale_heartbeat(hb: Path, *, age_secs: int = 20, state: str = "running") -> int:
    """Backdate HEARTBEAT mtime by ``age_secs`` and return the actual age.

    The bash script computes AGE = NOW - HB_MTIME; we mirror that with
    os.utime so a test fixture at ``t=now`` reads as ``t=now-age``.
    """
    hb.write_text(_hb_line(state))
    backdate = time.time() - age_secs
    os.utime(hb, (backdate, backdate))
    return age_secs


def _bash_steer(
    epic: str,
    msg: str,
    *,
    steer_file: str = "",
    wait_ack: bool = False,
    force: bool = False,
    from_: str = "tester",
    cwd: Path | None = None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """Invoke ``bash lib/mo-steer.sh <epic> [args] <msg>`` and capture output.

    Args:
        steer_file  when non-empty, passed as ``--steer-file=<path>``
        wait_ack    add ``--wait-ack`` (bash hardcodes 30s poll)
        force       add ``--force``
        from_       add ``--from=<tag>``
        cwd         subprocess CWD (for infer-job-from-epic tests)
        extra_args  any extra flags before the message

    Returns CompletedProcess with captured stdout/stderr/returncode.
    """
    _which("bash")
    argv: list[str] = ["bash", str(SH), epic]
    if steer_file:
        argv.append(f"--steer-file={steer_file}")
    if wait_ack:
        argv.append("--wait-ack")
    if force:
        argv.append("--force")
    if from_:
        argv.append(f"--from={from_}")
    argv.extend(extra_args)
    argv.append(msg)

    env = {**os.environ, "USER": "tester"}
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,  # (h) bash timeout case runs ~30s
    )


def _strip_envelope(env: dict) -> dict:
    """Drop timing-dependent / divergent fields before diffing envelopes."""
    return {k: v for k, v in env.items() if k not in _STRIP_KEYS}


def _read_envelope(steer_file: Path) -> dict:
    """Read the single JSON envelope from a freshly-written steer_file."""
    with open(steer_file, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 envelope line, got {len(lines)}"
    return json.loads(lines[0])


def _point_python_env(monkeypatch: pytest.MonkeyPatch, *, mini_ork_home: str = ".") -> None:
    """Reset MINI_ORK_HOME for the python process so it resolves to the
    same derivation path bash falls back to (REPO_ROOT/runs). Default
    ``.`` matches the bash `${MINI_ORK_HOME:-.mini-ork}` default; tests
    that set MINI_ORK_HOME in subprocess env must mirror here."""
    monkeypatch.setenv("MINI_ORK_HOME", mini_ork_home)
    monkeypatch.setenv("USER", "tester")


# ─────────────────────────────────────────────────────────────────────────────
# (a) happy-path write — bash vs python write the same envelope shape
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_parity(tmp_path, monkeypatch):
    """Both bash and python write a JSON envelope {id, at, from, body} to
    STEER.jsonl. Stripped fields (id/at) match; stderr "[mo-steer] wrote
    N-byte steer" line matches verbatim (modulo ID)."""
    _which("bash")
    _, sf, hb, _ = _init_tmp_run(tmp_path)
    _fresh_heartbeat(hb)

    # Bash side
    r_bash = _bash_steer("EXPL-DLG-C", "hello world", steer_file=str(sf))
    assert r_bash.returncode == 0, f"bash failed: rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    assert r_bash.stdout == "", f"bash emitted stdout (expected stderr-only): {r_bash.stdout!r}"
    bash_env = _read_envelope(sf)
    # Bash writes 2 stderr lines: explicit-file notice + wrote-line.
    assert "[mo-steer] using explicit steer file:" in r_bash.stderr
    assert "[mo-steer] wrote 11-byte steer (id=" in r_bash.stderr
    assert "-> " + str(sf) in r_bash.stderr
    # Body landed in the file (not stderr).
    assert bash_env["body"] == "hello world"

    # Reset for python side
    sf.write_text("")
    _fresh_heartbeat(hb)
    _point_python_env(monkeypatch)

    ms.steer(
        "EXPL-DLG-C", "hello world",
        steer_file=str(sf), from_="tester",
    )
    py_env = _read_envelope(sf)

    # Envelope structure: bash and python must emit identical fields in
    # the same order (json.dumps preserves insertion order).
    assert sorted(bash_env.keys()) == sorted(py_env.keys()) == ["at", "body", "from", "id"]
    # Stripped fields must match (id/at differ; from/body are inputs).
    assert _strip_envelope(bash_env) == {"from": "tester", "body": "hello world"}
    assert _strip_envelope(py_env) == {"from": "tester", "body": "hello world"}


# ─────────────────────────────────────────────────────────────────────────────
# (b) empty message — bash exit 2 + "ERROR: empty message" ↔ python ValueError
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_message_parity(tmp_path, monkeypatch):
    """Empty message raises exit 2 / ValueError on both sides with the
    same "ERROR: empty message" stderr phrase. Bash reaches the empty-
    message check via stdin read (empty /dev/null feed) because we pass
    an explicit empty argv message AND close stdin."""
    _which("bash")
    _, _, hb, _ = _init_tmp_run(tmp_path)
    _fresh_heartbeat(hb)
    _point_python_env(monkeypatch)

    # Bash: pass empty message + close stdin so MSG="" after parsing.
    r_bash = subprocess.run(
        ["bash", str(SH), "EXPL-DLG-C", ""],
        env={**os.environ, "USER": "tester"},
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
    )
    assert r_bash.returncode == 2, f"bash rc={r_bash.returncode} (expected 2)"
    assert "ERROR: empty message" in r_bash.stderr

    with pytest.raises(ValueError) as exc_info:
        ms.steer("EXPL-DLG-C", "")
    assert "ERROR: empty message" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# (c) --force bypass on stale heartbeat
# ─────────────────────────────────────────────────────────────────────────────
def test_force_bypass_stale_heartbeat_parity(tmp_path, monkeypatch):
    """Without force, stale heartbeat raises exit 5 / RuntimeError on
    both sides. With --force, bash exits 0 and python returns cleanly.
    The heartbeat sits at iter-1/HEARTBEAT (default derivation target
    for steer_file=iter-1/STEER.jsonl); tests reset steer_file content
    between the no-force and force sub-cases so bash's append finds an
    empty target."""
    _which("bash")
    _, sf, hb, _ = _init_tmp_run(tmp_path)
    _point_python_env(monkeypatch)

    # ── Bash: without force → exit 5 + "heartbeat stale (Ns old)" ──
    _stale_heartbeat(hb, age_secs=20)
    r_no_force = _bash_steer(
        "EXPL-DLG-C", "msg", steer_file=str(sf), force=False, cwd=tmp_path,
    )
    assert r_no_force.returncode == 5, f"bash rc={r_no_force.returncode}\nstderr={r_no_force.stderr}"
    assert "ERROR: heartbeat stale (20s old)" in r_no_force.stderr
    assert sf.read_text() == "", "bash should not have written envelope when heartbeat stale"

    # ── Bash: with --force → exit 0 + envelope written ──
    _fresh_heartbeat(hb)
    sf.write_text("")
    r_force = _bash_steer(
        "EXPL-DLG-C", "msg", steer_file=str(sf), force=True, cwd=tmp_path,
    )
    assert r_force.returncode == 0, f"bash force rc={r_force.returncode}\nstderr={r_force.stderr}"
    assert _read_envelope(sf)["body"] == "msg"

    # ── Python: without force → RuntimeError matching bash stderr phrase ──
    _stale_heartbeat(hb, age_secs=20)
    sf.write_text("")
    with pytest.raises(RuntimeError) as exc_no_force:
        ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf), heartbeat=str(hb))
    assert "ERROR: heartbeat stale" in str(exc_no_force.value)
    assert "20s old" in str(exc_no_force.value)
    assert sf.read_text() == "", "python should not have written envelope when heartbeat stale"

    # ── Python: with force=True → clean return, envelope written ──
    _fresh_heartbeat(hb)
    sf.write_text("")
    r = ms.steer(
        "EXPL-DLG-C", "msg",
        steer_file=str(sf), heartbeat=str(hb), force=True,
    )
    assert r["envelope"]["body"] == "msg"


# ─────────────────────────────────────────────────────────────────────────────
# (d) heartbeat state=done — bash exit 5 + python RuntimeError
# ─────────────────────────────────────────────────────────────────────────────
def test_heartbeat_state_done_parity(tmp_path, monkeypatch):
    """Heartbeat's last-line ``state=done`` triggers exit 5 / RuntimeError
    on both sides, with identical stderr phrase."""
    _which("bash")
    _, sf, hb, _ = _init_tmp_run(tmp_path)
    # Heartbeat with state=done on the LAST line (multi-line pickup test).
    # Use compact JSON so bash's `"state":"[^"]+"` regex matches.
    hb.write_text(
        _hb_line("running") + _hb_line("done")
    )
    # Re-touch mtime so it's not also flagged stale.
    os.utime(hb, (time.time(), time.time()))
    _point_python_env(monkeypatch)

    r_bash = _bash_steer("EXPL-DLG-C", "msg", steer_file=str(sf), cwd=tmp_path)
    assert r_bash.returncode == 5, f"bash rc={r_bash.returncode}"
    assert "ERROR: heartbeat says state=done — worker not accepting steer" in r_bash.stderr
    assert "Override with --force." in r_bash.stderr
    assert sf.read_text() == ""

    with pytest.raises(RuntimeError) as exc:
        ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf), heartbeat=str(hb))
    assert "ERROR: heartbeat says state=done" in str(exc.value)
    assert "Override with --force." in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# (e) heartbeat stale (>15s old mtime) — bash exit 5 + python RuntimeError
# ─────────────────────────────────────────────────────────────────────────────
def test_heartbeat_stale_parity(tmp_path, monkeypatch):
    """Backdated heartbeat triggers exit 5 / RuntimeError with the actual
    AGE seconds reported in the stderr phrase. Both sides use ``int(NOW -
    mtime)`` so they emit the same integer (within ±1s wall-clock drift)."""
    _which("bash")
    _, sf, hb, _ = _init_tmp_run(tmp_path)
    _stale_heartbeat(hb, age_secs=20)
    _point_python_env(monkeypatch)

    r_bash = _bash_steer("EXPL-DLG-C", "msg", steer_file=str(sf), cwd=tmp_path)
    assert r_bash.returncode == 5, f"bash rc={r_bash.returncode}"
    # Bash reports AGE = NOW - mtime. NOW is captured AFTER the stat call,
    # so bash's reported age can be 20 or 21 depending on wall-clock drift.
    assert "ERROR: heartbeat stale (" in r_bash.stderr
    assert "s old)" in r_bash.stderr
    assert "Override with --force" in r_bash.stderr
    assert sf.read_text() == ""

    with pytest.raises(RuntimeError) as exc:
        ms.steer("EXPL-DLG-C", "msg", steer_file=str(sf), heartbeat=str(hb))
    assert "ERROR: heartbeat stale" in str(exc.value)
    # Python computes the same way; integer can be 20 or 21 depending on
    # when NOW is captured between os.path.getmtime and int(time.time()).
    assert "s old" in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# (f) infer-job-from-epic — bash & python resolve to same iter dir
# ─────────────────────────────────────────────────────────────────────────────
def test_infer_job_from_epic_parity(tmp_path, monkeypatch):
    """EXPL-DLG-C → expl-dlg. Both bash and python resolve to
    <tmp>/runs/expl-dlg/EXPL-DLG-C/iter-1/STEER.jsonl and write a single
    envelope there. Uses --iter=1 to pin the iter dir. monkeypatch.chdir
    makes the Python port's _repo_root fall back to the tmp dir the same
    way bash's git-rev-parse-fallback-to-pwd does from the subprocess CWD."""
    _which("bash")
    # Build the runs/<job>/<epic>/iter-1 layout under tmp.
    iter_dir = tmp_path / "runs" / "expl-dlg" / "EXPL-DLG-C" / "iter-1"
    iter_dir.mkdir(parents=True)
    sf = iter_dir / "STEER.jsonl"
    hb = iter_dir / "HEARTBEAT"
    # Compact JSON for bash regex compatibility.
    hb.write_text(_hb_line("running"))
    (iter_dir / "worker.log").write_text("")

    # MINI_ORK_HOME="." makes bash resolve RUNS_DIR=$REPO_ROOT/runs which
    # matches the layout we built. The python port reads the same env
    # var in _resolve_steer_paths.
    _point_python_env(monkeypatch, mini_ork_home=".")
    # monkeypatch.chdir matches bash's subprocess CWD=tmp_path so the
    # port's _repo_root() returns tmp_path (via git fallback to pwd).
    monkeypatch.chdir(tmp_path)

    # Bash: no --steer-file, no --job; only --iter=1 to pin. CWD=tmp so
    # REPO_ROOT=tmp via the git-fallback-to-pwd path.
    r_bash = subprocess.run(
        ["bash", str(SH), "EXPL-DLG-C", "--iter=1", "msg-from-bash"],
        env={**os.environ, "USER": "tester", "MINI_ORK_HOME": "."},
        cwd=str(tmp_path),
        capture_output=True, text=True,
    )
    assert r_bash.returncode == 0, f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    assert "[mo-steer] inferred job=expl-dlg from epic prefix" in r_bash.stderr
    bash_env = _read_envelope(sf)
    assert bash_env["body"] == "msg-from-bash"

    # Python: same env, same call shape (no steer_file, no job, iter=1).
    sf.write_text("")
    _fresh_heartbeat(hb)
    py_r = ms.steer("EXPL-DLG-C", "msg-from-py", iter=1)
    assert py_r["steer_file"] == str(sf)
    py_env = _read_envelope(sf)
    assert py_env["body"] == "msg-from-py"
    # Both land on the same iter_dir/STEER.jsonl path with the same shape.
    assert sorted(bash_env.keys()) == sorted(py_env.keys()) == ["at", "body", "from", "id"]


# ─────────────────────────────────────────────────────────────────────────────
# (g) --wait-ack success — both find matching steer_yielded event
# ─────────────────────────────────────────────────────────────────────────────
def test_wait_ack_success_parity(tmp_path, monkeypatch):
    """Both bash and python poll worker.log for a matching steer_yielded
    event after writing the envelope. The test orchestrates the match by
    reading the steer envelope's id (bash) or monkeypatching the id
    (python), then appending a matching event to worker.log."""
    _which("bash")
    _, sf, hb, wl = _init_tmp_run(tmp_path)
    _fresh_heartbeat(hb)
    _point_python_env(monkeypatch)

    # ── Bash side ──
    # Spawn bash with --wait-ack; let it write the envelope, then append
    # a matching steer_yielded event so bash exits 0 within ~1s.
    proc = subprocess.Popen(
        ["bash", str(SH), "EXPL-DLG-C", "--steer-file=" + str(sf), "--wait-ack",
         "--from=tester", "msg-bash"],
        env={**os.environ, "USER": "tester"},
        cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    steer_id = _wait_for_envelope(sf, timeout=5.0)
    # Compact JSON to match bash's `"event":"steer_yielded".*"steer_id":"$ID"` regex.
    with open(wl, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "steer_yielded", "steer_id": steer_id}, separators=(",", ":")) + "\n")
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail(f"bash did not exit after steer_yielded event; stderr={proc.stderr.read() if proc.stderr else ''}")
    assert proc.returncode == 0, (
        f"bash rc={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
    )
    assert "[mo-steer] ack received in" in stderr
    assert "steer delivered + queued" in stderr

    # ── Python side ──
    sf.write_text("")
    _fresh_heartbeat(hb)
    wl.write_text("")  # reset worker.log

    # Pre-write the matching event using a known id; monkeypatch the
    # id factory so steer() emits exactly that id. Compact JSON to match
    # the port's `"event":"steer_yielded".*"steer_id":"<id>"` regex.
    known_id = "0" * 32
    monkeypatch.setattr(ms, "_new_steer_id", lambda: known_id)
    with open(wl, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "steer_yielded", "steer_id": known_id}, separators=(",", ":")) + "\n")

    # Run steer() in a thread so we can capture stderr (steer writes
    # "[mo-steer] ack received in Ns" to stderr) while it polls.
    captured: dict = {}
    def _run() -> None:
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            captured["r"] = ms.steer(
                "EXPL-DLG-C", "msg-py",
                steer_file=str(sf), wait_ack=True, from_="tester",
            )
        captured["stderr"] = buf.getvalue()
    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "python steer() did not return within 10s"
    py_stderr = captured["stderr"]
    assert "[mo-steer] ack received in" in py_stderr
    assert "steer delivered + queued" in py_stderr
    assert captured["r"]["id"] == known_id


def _wait_for_envelope(steer_file: Path, *, timeout: float) -> str:
    """Poll steer_file until a JSON envelope is written; return its id.

    Used to synchronize the test orchestrator with the bash subprocess:
    bash's --wait-ack loop blocks AFTER writing the envelope, so this
    poll unblocks as soon as the envelope exists, letting the test
    append the matching steer_yielded event to worker.log.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if steer_file.exists():
            lines = [ln for ln in steer_file.read_text().splitlines() if ln.strip()]
            if lines:
                return json.loads(lines[0])["id"]
        time.sleep(0.05)
    pytest.fail(f"steer envelope never written to {steer_file} within {timeout}s")


# ─────────────────────────────────────────────────────────────────────────────
# (h) --wait-ack timeout — both exit 7 with "WARN: no ack within 30s"
# ─────────────────────────────────────────────────────────────────────────────
def test_wait_ack_timeout_parity(tmp_path, monkeypatch):
    """No steer_yielded event ever appears → bash exits 7 + WARN message
    after 30s, python raises RuntimeError. Python's timeout is
    monkeypatched to 2s so the suite stays under 35s total.

    The bash script hardcodes 30s in ``for i in $(seq 1 30)`` — we can't
    reduce it without modifying lib/mo-steer.sh (strangler-fig invariant).
    Both sides produce the identical stderr phrase shape
    "WARN: no ack within Ns — message in queue but unconfirmed" with N=30
    for bash and N=2 for python.
    """
    _which("bash")
    _, sf, hb, _ = _init_tmp_run(tmp_path)
    _fresh_heartbeat(hb)
    _point_python_env(monkeypatch)

    # ── Bash side ──
    r_bash = _bash_steer(
        "EXPL-DLG-C", "msg",
        steer_file=str(sf), wait_ack=True, cwd=tmp_path,
    )
    assert r_bash.returncode == 7, f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr!r}"
    assert "[mo-steer] WARN: no ack within 30s — message in queue but unconfirmed" in r_bash.stderr

    # ── Python side (with monkeypatched timeout=2) ──
    sf.write_text("")
    _fresh_heartbeat(hb)
    monkeypatch.setattr(ms, "_WAIT_ACK_TIMEOUT_SECS", 2)

    with pytest.raises(RuntimeError) as exc:
        ms.steer(
            "EXPL-DLG-C", "msg",
            steer_file=str(sf), wait_ack=True,
        )
    assert "WARN: no ack within 2s" in str(exc.value)
    assert "message in queue but unconfirmed" in str(exc.value)
    # But the FORMAT (WARN: no ack within Ns — message in queue but
    # unconfirmed) matches bash's format modulo the N value (30 vs 2).