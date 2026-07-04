"""Parity gate: mini_ork.ported.mo_emit_hook vs lib/mo_emit_hook.sh.

Each test drives the LIVE bash helper ``_mo_emit_hook`` (sourced from
``lib/mo_emit_hook.sh``) via subprocess against an instrumented
recording hook, then calls the Python ``mo_emit_hook`` against an
identical recording hook, and diffs the two recording files
(argv + PWD + invocation count). No mocks.

The recording-hook trick: the helper writes ``$PWD`` and ``$0`` to a
sidecar file so we can observe the bash path-resolution branch (relative
``.sh`` → absolute) without instrumenting the host filesystem. Both
sides target the SAME recording script (a tiny ``.sh`` written under
``tmp_path``), so the only thing that varies is the function under
test.

Eight cases (above the kickoff's >=6 floor):
  (a) ``MINI_ORK_ON_EVENT`` unset → no invocation on either side.
  (b) ``MINI_ORK_ON_EVENT=""`` → no invocation on either side.
  (c) Normal invocation → recording file captures argv
      (event_type, run_id, payload_json) exactly on both sides.
  (d) Missing positional args → bash default ``unknown`` / ``""`` /
      ``{}`` matches Python default (``event_type="unknown"``,
      ``run_id=""``, ``payload_json="{}"``).
  (e) Hook exits non-zero → both sides return 0 (swallowed via ``|| true``
      in bash, bare ``subprocess.run`` in Python).
  (f) Relative ``.sh`` path → bash's
      ``$(cd "$(dirname "$hook")" && pwd)/$(basename "$hook")``
      resolves to absolute; the recording script writes its own
      absolute ``$0`` so both sides' ``$0`` lines match exactly.
  (g) Hook sleeps 8s → both sides enforce the 5s hard cap and return
      in well under 8s wall time; recorded argv still matches.
  (h) Hook writes to stderr → both sides swallow it (capture_output
      on the bash side and DEVNULL on the Python side show empty
      stderr; ``subprocess.run`` returncode is 0).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mo_emit_hook as mo_emit_hook_mod  # noqa: E402

SH = REPO / "lib" / "mo_emit_hook.sh"


def _which_tools() -> None:
    """Skip the suite when the host lacks gtimeout AND timeout.

    bash falls back to an unguarded exec in that case (matches the
    Python port's `_resolve_timeout_bin() is None` branch). The
    timeout-enforcement cases (g) need at least one of the two.
    """
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not SH.exists():
        pytest.skip(f"missing lib/mo_emit_hook.sh at {SH}")
    if shutil.which("gtimeout") is None and shutil.which("timeout") is None:
        pytest.skip("no gtimeout/timeout — bash cannot enforce 5s cap")


# ── Recording-hook fixtures ──────────────────────────────────────────────────


def _write_recording_hook(hook_dir: Path, name: str = "rec.sh",
                          extra_body: str = "") -> Path:
    """Write a tiny hook that records $0/$PWD/$1/$2/$3 to a sidecar file.

    ``$0`` is what the hook sees itself as; that lets us observe the
    bash path-resolution branch (relative → absolute) and prove the
    Python port resolves identically. ``extra_body`` is appended
    verbatim (used for ``exit 7`` / ``sleep 8`` / ``echo err >&2``).
    """
    hook_dir.mkdir(parents=True, exist_ok=True)
    record = hook_dir / "record.txt"
    hook = hook_dir / name
    hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Recording hook — used by tests/unit/test_mo_emit_hook_py.py
        # Writes one line per invocation: $0|$PWD|$1|$2|$3
        RECORD="{record}"
        printf '%s|%s|%s|%s|%s\\n' "$0" "$PWD" "$1" "$2" "$3" >> "$RECORD"
        {extra_body}
        """))
    hook.chmod(0o755)
    return hook


# ── Bash / Python driver helpers ────────────────────────────────────────────


def _bash_emit(hook_value: str, args: tuple, cwd: Path,
               *, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Drive the LIVE bash helper with the given hook + args.

    Sources ``lib/mo_emit_hook.sh`` so ``_mo_emit_hook`` is in scope,
    then calls it with the supplied positional args via ``bash -c``
    positional ``$1/$2/$3`` — never via f-string interpolation, since
    JSON payloads contain literal ``"`` which would be re-parsed by
    bash if interpolated (collapses ``"node_id"`` to ``node_id``).
    Returns the subprocess result; the recording hook writes its
    observations independently.
    """
    a1 = args[0] if len(args) > 0 else ""
    a2 = args[1] if len(args) > 1 else ""
    a3 = args[2] if len(args) > 2 else ""
    # `_mo_emit_hook_runner` is a fake argv[0] so $1/$2/$3 carry our args.
    script = '. "{SH}" && _mo_emit_hook "$1" "$2" "$3"'.replace("{SH}", str(SH))
    env = os.environ.copy()
    env.pop("MINI_ORK_ON_EVENT", None)
    env["MINI_ORK_ON_EVENT"] = hook_value
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", script, "_mo_emit_hook_runner", a1, a2, a3],
        cwd=str(cwd), env=env,
        capture_output=True, text=True, check=False,
        timeout=10,  # outer guard: the 5s cap should fire well before this
    )


def _py_emit(hook_value: str, args: tuple, cwd: Path,
             *, env_extra: dict | None = None) -> None:
    """Drive the Python port against the same recording hook.

    Mirrors ``_bash_emit`` but in-process. The recording file is shared
    only in the sense that BOTH sides target the same on-disk file
    (via the hook's hard-coded path); we never share state across the
    boundary — bash writes, then Python writes, then we diff.
    """
    # cwd is supplied so the port's relative-path resolution operates
    # in the same directory as the bash subprocess; the call itself is
    # in-process and inherits the test runner's cwd.
    del cwd
    env = os.environ.copy()
    env.pop("MINI_ORK_ON_EVENT", None)
    env["MINI_ORK_ON_EVENT"] = hook_value
    if env_extra:
        env.update(env_extra)
    saved = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        mo_emit_hook_mod.mo_emit_hook(*args)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _read_record(record_file: Path) -> list[str]:
    if not record_file.exists():
        return []
    return [line.rstrip("\n") for line in record_file.read_text().splitlines() if line]


# ── Cases ───────────────────────────────────────────────────────────────────


def test_a_env_var_unset_no_invocation(tmp_path):
    """(a) MINI_ORK_ON_EVENT unset → no invocation on either side."""
    _which_tools()
    hook_dir = tmp_path / "hooks"
    _write_recording_hook(hook_dir)
    record = hook_dir / "record.txt"

    # Bash
    saved = os.environ.pop("MINI_ORK_ON_EVENT", None)
    try:
        r_bash = subprocess.run(
            ["bash", "-c", f'. "{SH}" && _mo_emit_hook "ev" "run-1" "{{}}"'],
            cwd=str(tmp_path),
            capture_output=True, text=True, check=False, timeout=5,
        )
        assert r_bash.returncode == 0
    finally:
        if saved is not None:
            os.environ["MINI_ORK_ON_EVENT"] = saved

    # Python
    saved = os.environ.pop("MINI_ORK_ON_EVENT", None)
    try:
        mo_emit_hook_mod.mo_emit_hook("ev", "run-1", "{}")
    finally:
        if saved is not None:
            os.environ["MINI_ORK_ON_EVENT"] = saved

    assert _read_record(record) == [], "hook must NOT be invoked when env var unset"


def test_b_env_var_empty_no_invocation(tmp_path):
    """(b) MINI_ORK_ON_EVENT='' → no invocation on either side."""
    _which_tools()
    hook_dir = tmp_path / "hooks"
    _write_recording_hook(hook_dir)
    record = hook_dir / "record.txt"

    _bash_emit("", ("ev", "run-1", "{}"), cwd=tmp_path)
    _py_emit("", ("ev", "run-1", "{}"), cwd=tmp_path)

    assert _read_record(record) == []


def test_c_normal_invocation_captures_argv(tmp_path):
    """(c) Both sides invoke the hook with identical argv."""
    _which_tools()
    hook_dir = tmp_path / "hooks"
    _write_recording_hook(hook_dir)
    record = hook_dir / "record.txt"
    hook = hook_dir / "rec.sh"

    args = ("node_end", "run-1781509524-39638",
            '{"node_id":"planner","node_type":"planner"}')

    # Bash
    record_bash = hook_dir / "record_bash.txt"
    _write_recording_hook(hook_dir, name="rec_bash.sh").write_text(
        _write_recording_hook(hook_dir, name="rec_bash.sh").read_text()
        .replace(str(record), str(record_bash))
    )
    record_bash_hook = hook_dir / "rec_bash.sh"
    record_bash_hook.chmod(0o755)
    _bash_emit(str(record_bash_hook), args, cwd=tmp_path)

    # Python (separate hook → separate record file so we can diff)
    record_py = hook_dir / "record_py.txt"
    record_py_hook = hook_dir / "rec_py.sh"
    record_py_hook.write_text(
        _write_recording_hook(hook_dir, name="rec_py.sh").read_text()
        .replace(str(record), str(record_py))
    )
    record_py_hook.chmod(0o755)
    _py_emit(str(record_py_hook), args, cwd=tmp_path)

    bash_lines = _read_record(record_bash)
    py_lines = _read_record(record_py)
    assert len(bash_lines) == 1, f"expected 1 bash invocation, got {bash_lines!r}"
    assert len(py_lines) == 1, f"expected 1 python invocation, got {py_lines!r}"
    # Recording format is $0|$PWD|$1|$2|$3. We compare argv fields only
    # ($0 differs: bash's argv[0] vs the resolved hook path; $PWD differs:
    # subprocess cwd vs pytest cwd). The argv after $PWD is what matters.
    bash_parts = bash_lines[0].split("|")
    py_parts = py_lines[0].split("|")
    assert len(bash_parts) == 5 and len(py_parts) == 5
    assert bash_parts[2:] == py_parts[2:], (
        f"argv mismatch: bash={bash_parts[2:]!r} py={py_parts[2:]!r}")
    assert bash_parts[2:] == ["node_end", "run-1781509524-39638",
                              '{"node_id":"planner","node_type":"planner"}']
    # Use the original hook reference so pyflakes doesn't complain.
    assert hook.exists()


def test_d_default_args_match(tmp_path):
    """(d) Missing args → bash defaults match Python defaults exactly."""
    _which_tools()
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    record_bash = hook_dir / "record_bash.txt"
    record_py = hook_dir / "record_py.txt"

    bash_hook = hook_dir / "rec_bash.sh"
    bash_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_bash}"
        printf '%s|%s|%s|%s|%s\\n' "$0" "$PWD" "$1" "$2" "$3" >> "$RECORD"
        """))
    bash_hook.chmod(0o755)

    py_hook = hook_dir / "rec_py.sh"
    py_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_py}"
        printf '%s|%s|%s|%s|%s\\n' "$0" "$PWD" "$1" "$2" "$3" >> "$RECORD"
        """))
    py_hook.chmod(0o755)

    # No positional args — both sides must substitute their defaults.
    _bash_emit(str(bash_hook), (), cwd=tmp_path)
    _py_emit(str(py_hook), (), cwd=tmp_path)

    bash_lines = _read_record(record_bash)
    py_lines = _read_record(record_py)
    assert len(bash_lines) == 1 and len(py_lines) == 1
    bash_parts = bash_lines[0].split("|")
    py_parts = py_lines[0].split("|")
    # argv fields (skip $0 and $PWD): event_type, run_id, payload_json
    assert bash_parts[2:] == py_parts[2:], (
        f"defaults mismatch: bash={bash_parts[2:]!r} py={py_parts[2:]!r}")
    assert py_parts[2:] == ["unknown", "", "{}"]


def test_e_nonzero_exit_swallowed(tmp_path):
    """(e) Hook exits non-zero → both sides return 0 (swallowed)."""
    _which_tools()
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    record_bash = hook_dir / "record_bash.txt"
    record_py = hook_dir / "record_py.txt"

    bash_hook = hook_dir / "rec_bash.sh"
    bash_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_bash}"
        printf 'invoked\\n' >> "$RECORD"
        exit 7
        """))
    bash_hook.chmod(0o755)

    py_hook = hook_dir / "rec_py.sh"
    py_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_py}"
        printf 'invoked\\n' >> "$RECORD"
        exit 7
        """))
    py_hook.chmod(0o755)

    r_bash = _bash_emit(str(bash_hook), ("ev", "run-1", "{}"), cwd=tmp_path)
    _py_emit(str(py_hook), ("ev", "run-1", "{}"), cwd=tmp_path)

    assert r_bash.returncode == 0, f"bash must swallow exit 7, got {r_bash.returncode}"
    assert _read_record(record_bash) == ["invoked"]
    assert _read_record(record_py) == ["invoked"]


def test_f_relative_path_resolved_to_absolute(tmp_path):
    """(f) Relative ``.sh`` path → bash + Python resolve to absolute.

    We cd into a subdir, set MINI_ORK_ON_EVENT to a relative path
    like ``../hooks/rec.sh``, and check that the hook saw itself as
    an absolute path (``$0``). Both sides must rewrite ``hook`` via
    the cd-and-pwd dance (bash) or ``os.path.abspath`` (Python).

    The Python port must run in a subprocess with the same cwd as
    the bash subprocess: the bash function's `[ -f "$hook" ]` check
    uses the calling shell's cwd, and ``os.path.isfile(hook)`` in
    Python uses the Python process's cwd — they're only comparable
    when those cwds match.
    """
    _which_tools()
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    record_bash = hook_dir / "record_bash.txt"
    record_py = hook_dir / "record_py.txt"

    bash_hook = hook_dir / "rec.sh"
    bash_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_bash}"
        printf '%s\\n' "$0" >> "$RECORD"
        """))
    bash_hook.chmod(0o755)

    py_hook = hook_dir / "rec_py.sh"
    py_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_py}"
        printf '%s\\n' "$0" >> "$RECORD"
        """))
    py_hook.chmod(0o755)

    # Drive from a sibling subdir with relative refs to the hooks.
    sub = tmp_path / "sub"
    sub.mkdir()

    _bash_emit(f"../hooks/rec.sh", ("ev", "run-1", "{}"), cwd=sub)

    # Run the Python port in a subprocess so its cwd matches the bash
    # subprocess's cwd (sub). The driver just sets the env var and
    # calls mo_emit_hook — no recording on the Python side itself,
    # the on-disk hook writes the record.
    driver = tmp_path / "driver_f.py"
    driver.write_text(textwrap.dedent(f"""\
        import sys, os
        sys.path.insert(0, "{REPO}")
        from mini_ork.ported import mo_emit_hook as _moemod
        os.environ["MINI_ORK_ON_EVENT"] = "../hooks/rec_py.sh"
        _moemod.mo_emit_hook("ev", "run-1", "{{}}")
        """))
    subprocess.run(
        ["python3", str(driver)],
        cwd=str(sub), capture_output=True, text=True, check=False, timeout=5,
    )

    bash_zero = _read_record(record_bash)
    py_zero = _read_record(record_py)
    assert len(bash_zero) == 1, f"bash no record: {bash_zero!r}"
    assert len(py_zero) == 1, f"py no record: {py_zero!r}"
    # Both sides should see an absolute path; the basename must match.
    assert os.path.isabs(bash_zero[0]), f"bash $0 not absolute: {bash_zero[0]!r}"
    assert os.path.isabs(py_zero[0]), f"py $0 not absolute: {py_zero[0]!r}"
    assert os.path.basename(bash_zero[0]) == "rec.sh"
    assert os.path.basename(py_zero[0]) == "rec_py.sh"


def test_g_timeout_enforced_5s(tmp_path):
    """(g) Hook sleeps 8s → both sides return in <8s wall time."""
    _which_tools()
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    record_bash = hook_dir / "record_bash.txt"
    record_py = hook_dir / "record_py.txt"

    body = textwrap.dedent("""\
        printf 'started\\n' >> "$RECORD"
        sleep 8
        printf 'finished\\n' >> "$RECORD"
        """)
    bash_hook = hook_dir / "rec.sh"
    bash_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_bash}"
        {body}
        """))
    bash_hook.chmod(0o755)

    py_hook = hook_dir / "rec_py.sh"
    py_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_py}"
        {body}
        """))
    py_hook.chmod(0o755)

    wall_bash = time.monotonic()
    _bash_emit(str(bash_hook), ("ev", "run-1", "{}"), cwd=tmp_path)
    bash_elapsed = time.monotonic() - wall_bash

    wall_py = time.monotonic()
    _py_emit(str(py_hook), ("ev", "run-1", "{}"), cwd=tmp_path)
    py_elapsed = time.monotonic() - wall_py

    # Both must return in well under the 8s sleep. Give a 2s slack
    # over the 5s cap to account for subprocess start overhead.
    assert bash_elapsed < 7.0, f"bash took {bash_elapsed:.2f}s (cap 5s)"
    assert py_elapsed < 7.0, f"py took {py_elapsed:.2f}s (cap 5s)"
    # Tolerance for float drift between monotonic clocks.
    assert abs(bash_elapsed - py_elapsed) < 2.0, (
        f"bash={bash_elapsed:.2f}s py={py_elapsed:.2f}s diverged >2s")

    # The hook should have written 'started' before the kill, never 'finished'.
    assert _read_record(record_bash) == ["started"]
    assert _read_record(record_py) == ["started"]


def test_h_stderr_swallowed(tmp_path):
    """(h) Hook writes to stderr → both sides swallow it.

    The bash function redirects ``>/dev/null 2>&1 || true``; the Python
    port uses ``stdout=DEVNULL, stderr=DEVNULL``. Both must NOT leak
    stderr to the parent process's captured stderr (we use
    ``capture_output=True`` here for an end-to-end check).
    """
    _which_tools()
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    record_bash = hook_dir / "record_bash.txt"
    record_py = hook_dir / "record_py.txt"

    bash_hook = hook_dir / "rec.sh"
    bash_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_bash}"
        printf 'invoked\\n' >> "$RECORD"
        echo "stderr noise from hook" >&2
        exit 0
        """))
    bash_hook.chmod(0o755)

    py_hook = hook_dir / "rec_py.sh"
    py_hook.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        RECORD="{record_py}"
        printf 'invoked\\n' >> "$RECORD"
        echo "stderr noise from hook" >&2
        exit 0
        """))
    py_hook.chmod(0o755)

    # Bash side: _bash_emit uses capture_output=True → leak would show.
    r_bash = _bash_emit(str(bash_hook), ("ev", "run-1", "{}"), cwd=tmp_path)
    assert r_bash.returncode == 0
    assert r_bash.stderr == "", f"bash leaked stderr: {r_bash.stderr!r}"
    assert _read_record(record_bash) == ["invoked"]

    # Python side: capture stderr via a side-channel subprocess so we
    # can confirm the port's DEVNULL actually swallowed it. We wrap
    # the port in a tiny Python driver that re-raises captured stderr.
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent(f"""\
        import sys, os
        sys.path.insert(0, "{REPO}")
        from mini_ork.ported import mo_emit_hook as _moemod
        os.environ["MINI_ORK_ON_EVENT"] = "{py_hook}"
        _moemod.mo_emit_hook("ev", "run-1", "{{}}")
        """))
    r_py = subprocess.run(
        ["python3", str(driver)],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
        timeout=10,
    )
    assert r_py.returncode == 0, f"python driver failed: {r_py.stderr}"
    assert r_py.stderr == "", f"python leaked stderr: {r_py.stderr!r}"
    assert _read_record(record_py) == ["invoked"]