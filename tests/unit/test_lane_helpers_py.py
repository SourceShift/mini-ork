"""Parity gate: ``mini_ork.ported.lane_helpers`` vs ``lib/lane-helpers.sh``.

Each test invokes the LIVE ``bash lib/lane-helpers.sh <op>`` subprocess
(or ``bash -c '. lib/lane-helpers.sh && mo_<op> …'`` for ops that
require ``local -n`` semantics) on identical inputs as the Python port
and asserts byte-identical flag arrays, stderr text, exit codes, and
``cache-stats.json`` content. No mocks, no hardcoded outputs beyond
what bash itself emits.

Eight cases (above the kickoff's >=6 floor):
  (a) lane_is_free                 — 5 lanes (free vs paid) parametrised
  (b) emit_budget_flag             — free lane returns [], paid returns [--max-budget-usd, <val>]
  (c) emit_cache_flags feature-detect — PATH-shim fake `claude --help` echoes
                                       the cache flag → both return [flag]
  (d) emit_cache_flags disabled    — MO_PROMPT_CACHE_DISABLED=1 → both return []
  (e) assert_lane_capability OK    — capabilities satisfied → exit 0
  (f) assert_lane_capability fail  — missing capability → exit 1 + stderr
                                       contains the first missing cap token
  (g) aggregate_cache_stats happy  — 3 fixture logs → byte-diff cache-stats.json
                                       modulo per_file[].file + saved_usd float
                                       tolerance (1e-6)
  (h) aggregate_cache_stats empty  — zero-log dir → both still write a valid
                                       JSON with zeros + hit_rate:0 + per_file:[]

``mo_claude_print`` is intentionally NOT live-parity-tested under the
standard recipe — bash's ``claude --print`` is non-deterministic
(network calls, model output), and the plan explicitly documents this
gap. The function is exercised via in-process argv-shape verification
inside the (c)/(d) emit_cache_flags tests (the cache_flags are produced
by the same code path claude_print uses).

Environment isolation:
  The shell pytest runs in often has MINI_ORK_HOME / MINI_ORK_ROOT /
  MINI_ORK_RUN_DIR / MO_LANE_REQUIRES_CAPABILITY /
  MO_PROMPT_CACHE_DISABLED set to whatever the operator exported. Each
  test pops the relevant vars and re-applies only its own overrides so
  pytest's arbitrary collection order cannot leak a prior test's env
  into the next.

Strangler-fig co-existence: ``lib/lane-helpers.sh`` is byte-identical
before and after this test exists.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import lane_helpers as lh  # noqa: E402

LIB = REPO / "lib" / "lane-helpers.sh"

# Env vars that the bash source reads; each parity test pops them
# before applying its own overrides.
_ENV_KEYS = (
    "MINI_ORK_HOME",
    "MINI_ORK_ROOT",
    "MINI_ORK_RUN_DIR",
    "MO_LANE_REQUIRES_CAPABILITY",
    "MO_PROMPT_CACHE_DISABLED",
)


def _which(*tools: str) -> dict[str, str]:
    out = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


def _clean_env() -> dict:
    """Return os.environ minus the lane-helper-sensitive vars."""
    env = os.environ.copy()
    for k in _ENV_KEYS:
        env.pop(k, None)
    return env


def _bash_lane_is_free(lane: str, env: dict) -> bool:
    """Invoke bash's mo_lane_is_free; return True when exit 0."""
    r = subprocess.run(
        ["bash", "-c",
         f'. "{LIB}" >/dev/null 2>&1; mo_lane_is_free "$1"',
         "--", lane],
        env=env, capture_output=True, text=True,
    )
    return r.returncode == 0


def _bash_emit_budget_flag(lane: str, val: str, env: dict) -> list[str]:
    """Drive ``mo_emit_budget_flag`` via bash, capturing the resulting array.

    bash's ``local -n`` requires the array name to be visible in the
    function's scope, so we source the lib, declare the array, call
    the function, then ``printf '%s\\n' "${arr[@]}"`` to dump its
    contents one per line. ``--`` guards against val starting with ``-``.
    """
    script = (
        f'. "{LIB}" >/dev/null 2>&1\n'
        f'declare -a out=()\n'
        f'mo_emit_budget_flag out "$1" "$2"\n'
        f'printf "%s\\n" "${{out[@]}}"\n'
    )
    r = subprocess.run(
        ["bash", "-c", script, "--", lane, val],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash emit_budget_flag failed: {r.stderr}"
    return [ln for ln in r.stdout.split("\n") if ln != ""]


def _bash_emit_cache_flags(env: dict) -> list[str]:
    """Drive ``mo_emit_cache_flags`` and dump the resulting array."""
    script = (
        f'. "{LIB}" >/dev/null 2>&1\n'
        f'declare -a out=()\n'
        f'mo_emit_cache_flags out\n'
        f'printf "%s\\n" "${{out[@]}}"\n'
    )
    r = subprocess.run(
        ["bash", "-c", script, "--"],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash emit_cache_flags failed: {r.stderr}"
    return [ln for ln in r.stdout.split("\n") if ln != ""]


def _bash_assert_lane_capability(lane: str, env: dict) -> subprocess.CompletedProcess:
    """Run ``mo_assert_lane_capability`` in a subshell, capturing rc + stderr."""
    script = (
        f'. "{LIB}" >/dev/null 2>&1\n'
        f'mo_assert_lane_capability "$1"\n'
    )
    return subprocess.run(
        ["bash", "-c", script, "--", lane],
        env=env, capture_output=True, text=True,
    )


def _bash_aggregate_cache_stats(iter_dir: str, env: dict) -> subprocess.CompletedProcess:
    """Run ``mo_aggregate_cache_stats`` in a subshell."""
    script = (
        f'. "{LIB}" >/dev/null 2>&1\n'
        f'mo_aggregate_cache_stats "$1"\n'
    )
    return subprocess.run(
        ["bash", "-c", script, "--", iter_dir],
        env=env, capture_output=True, text=True,
    )


def _point_python_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Reset lane-helper-sensitive env vars for the Python process and
    apply ``overrides``. Mirrors the env passed to bash subprocesses."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in overrides.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def _reset_cache_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the emit_cache_flags probe to re-run on next call.

    Mirrors the bash source's per-process ``_MO_CACHE_FLAG_SUPPORTED``
    env var. Tests that vary ``claude`` (via PATH shim) or
    ``MO_PROMPT_CACHE_DISABLED`` must reset the module-level cache so
    the new value gets probed.
    """
    monkeypatch.setattr(lh, "_CACHED_SUPPORTED", None)
    monkeypatch.setattr(lh, "_PROBE_DONE", False)


# ─────────────────────────────────────────────────────────────────────
# (a) lane_is_free parity — 5 lanes (free + paid)
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lane", ["glm", "kimi", "minimax", "opus", "sonnet"])
def test_lane_is_free_parity(lane: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each lane produces identical free/paid verdict from bash and python."""
    _which("bash")
    _point_python_env(monkeypatch)
    env = _clean_env()

    bash_free = _bash_lane_is_free(lane, env)
    py_free = lh.lane_is_free(lane)
    assert py_free == bash_free, (
        f"lane_is_free({lane!r}) drift: bash={bash_free} py={py_free}"
    )


# ─────────────────────────────────────────────────────────────────────
# (b) emit_budget_flag parity — free lane returns []; paid returns
#     [--max-budget-usd, <val>].
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "lane,val,expected_len",
    [
        ("glm", "0.80", 0),       # free → []
        ("opus", "0.80", 2),      # paid → [flag, val]
        ("sonnet", "1.20", 2),    # paid → [flag, val]
    ],
    ids=["free_glm", "paid_opus", "paid_sonnet"],
)
def test_emit_budget_flag_parity(
    lane: str, val: str, expected_len: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both bash and python produce the same flag array for a given lane/val."""
    _which("bash")
    _point_python_env(monkeypatch)
    env = _clean_env()

    bash_flags = _bash_emit_budget_flag(lane, val, env)
    py_flags = lh.emit_budget_flag(lane, val)

    assert len(bash_flags) == expected_len
    assert bash_flags == py_flags, (
        f"emit_budget_flag({lane!r}, {val!r}) drift: "
        f"bash={bash_flags!r} py={py_flags!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# (c) emit_cache_flags feature-detect — PATH shim echoes the flag in
#     `claude --help`, so both bash and python return [flag].
# ─────────────────────────────────────────────────────────────────────
def test_emit_cache_flags_supported_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATH-shim fake `claude --help` echoes the cache flag → both sides
    return ``["--exclude-dynamic-system-prompt-sections"]``."""
    _which("bash")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    fake_claude = shim_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        'echo "Usage: claude [options]"\n'
        'echo "  --exclude-dynamic-system-prompt-sections  Exclude dynamic system prompt sections"\n'
        'echo "  --max-turns <N>                            Max turns"\n'
    )
    fake_claude.chmod(0o755)

    env = _clean_env()
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
    _point_python_env(monkeypatch)
    # Make subprocess.run find the shim first.
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ.get('PATH', '')}")
    _reset_cache_probe(monkeypatch)

    bash_flags = _bash_emit_cache_flags(env)
    py_flags = lh.emit_cache_flags()

    assert bash_flags == py_flags, (
        f"emit_cache_flags (supported) drift: bash={bash_flags!r} py={py_flags!r}"
    )
    assert "--exclude-dynamic-system-prompt-sections" in bash_flags


# ─────────────────────────────────────────────────────────────────────
# (d) emit_cache_flags disabled — MO_PROMPT_CACHE_DISABLED=1 returns [].
# ─────────────────────────────────────────────────────────────────────
def test_emit_cache_flags_disabled_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MO_PROMPT_CACHE_DISABLED=1 short-circuits to [] on both sides.

    The bash source guards the feature-detect block on
    ``[ "${MO_PROMPT_CACHE_DISABLED:-0}" = "1" ]``. The Python port
    returns [] before the probe fires. We do NOT need a PATH shim
    because the disabled branch never calls ``claude``."""
    _which("bash")
    env = _clean_env()
    env["MO_PROMPT_CACHE_DISABLED"] = "1"
    _point_python_env(monkeypatch, MO_PROMPT_CACHE_DISABLED="1")
    _reset_cache_probe(monkeypatch)

    bash_flags = _bash_emit_cache_flags(env)
    py_flags = lh.emit_cache_flags()

    assert bash_flags == [] and py_flags == [], (
        f"disabled branch drift: bash={bash_flags!r} py={py_flags!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# (e) assert_lane_capability OK — capabilities satisfied → exit 0.
# ─────────────────────────────────────────────────────────────────────
def test_assert_lane_capability_satisfied_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed a tmpdir agents.yaml with capabilities satisfied; both bash
    and python exit 0 with empty stderr."""
    _which("bash")
    home = tmp_path / "home"
    cfg_dir = home / "config"
    cfg_dir.mkdir(parents=True)
    agents_yaml = cfg_dir / "agents.yaml"
    agents_yaml.write_text(
        "lanes:\n"
        "  implementer: opus\n"
        "capabilities:\n"
        "  opus:\n"
        "    tools: true\n"
        "    reasoning: true\n"
    )

    env = _clean_env()
    env["MINI_ORK_HOME"] = str(home)
    # MINI_ORK_ROOT must point at the real repo so bash can source
    # lib/config_resolve.sh. config/agents.yaml exists at the repo
    # root, but bash's mo_assert_lane_capability uses it only as a
    # FALLBACK when the primary is missing — primary is HOME's yaml
    # so the real REPO yaml is never consulted by the capabilities
    # lookup (it has no `capabilities` key at the top level that
    # would shadow the primary in this fixture; primary's caps win).
    env["MINI_ORK_ROOT"] = str(REPO)
    env["MINI_ORK_RUN_DIR"] = ""  # bash falls through to HOME
    env["MO_LANE_REQUIRES_CAPABILITY"] = "tools,reasoning"

    _point_python_env(
        monkeypatch,
        MINI_ORK_HOME=str(home),
        MINI_ORK_ROOT=str(REPO),
        MINI_ORK_RUN_DIR="",
        MO_LANE_REQUIRES_CAPABILITY="tools,reasoning",
    )

    r_bash = _bash_assert_lane_capability("implementer", env)
    assert r_bash.returncode == 0, (
        f"bash satisfied rc={r_bash.returncode}\nstderr={r_bash.stderr!r}"
    )
    assert r_bash.stderr == "", f"bash satisfied leaked stderr: {r_bash.stderr!r}"

    # resolve_agents_yaml prints the path; redirect stdout to keep the
    # test's own output clean (the helper is called for side-effect).
    with contextlib.redirect_stdout(io.StringIO()):
        lh.assert_lane_capability("implementer")  # must not raise


# ─────────────────────────────────────────────────────────────────────
# (f) assert_lane_capability fail — missing cap → exit 1 + stderr token.
# ─────────────────────────────────────────────────────────────────────
def test_assert_lane_capability_missing_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lane family lacks one required capability → both exit 1 and
    surface the first missing cap token in stderr / exception."""
    _which("bash")
    home = tmp_path / "home"
    cfg_dir = home / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "agents.yaml").write_text(
        "lanes:\n"
        "  implementer: opus\n"
        "capabilities:\n"
        "  opus:\n"
        "    tools: true\n"
    )
    # requirements include "vision" which opus does NOT have.

    env = _clean_env()
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_ROOT"] = str(REPO)
    env["MO_LANE_REQUIRES_CAPABILITY"] = "tools,vision"

    _point_python_env(
        monkeypatch,
        MINI_ORK_HOME=str(home),
        MINI_ORK_ROOT=str(REPO),
        MINI_ORK_RUN_DIR="",
        MO_LANE_REQUIRES_CAPABILITY="tools,vision",
    )

    r_bash = _bash_assert_lane_capability("implementer", env)
    assert r_bash.returncode == 1, (
        f"bash missing-cap rc={r_bash.returncode}\nstderr={r_bash.stderr!r}"
    )
    assert r_bash.stderr.strip() == "vision", (
        f"bash missing-cap stderr={r_bash.stderr!r}"
    )

    with contextlib.redirect_stdout(io.StringIO()):
        with pytest.raises(RuntimeError) as exc_info:
            lh.assert_lane_capability("implementer")
    assert str(exc_info.value) == "vision", (
        f"python missing-cap token={exc_info.value!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# (g) aggregate_cache_stats happy — byte-diff cache-stats.json modulo
#     per_file[].file (identical basename) + saved_usd float tolerance.
# ─────────────────────────────────────────────────────────────────────
def test_aggregate_cache_stats_parity(
    tmp_path: Path,
) -> None:
    """Seed 3 fixture logs; both sides must emit cache-stats.json with
    the same totals + per_file shape (same basename, identical ints)."""
    _which("bash")
    bash_dir = tmp_path / "bash" / "iter-1"
    py_dir = tmp_path / "py" / "iter-1"
    bash_dir.mkdir(parents=True)
    py_dir.mkdir(parents=True)

    fixture_body = (
        '{"event":"usage","cache_creation_input_tokens":100,"cache_read_input_tokens":50,"input_tokens":25}\n'
        '{"event":"usage","cache_creation_input_tokens":200,"cache_read_input_tokens":75,"input_tokens":50}\n'
    )
    for name in ("a.log", "b.log", "c.log"):
        (bash_dir / name).write_text(fixture_body)
        (py_dir / name).write_text(fixture_body)

    env = _clean_env()

    r_bash = _bash_aggregate_cache_stats(str(bash_dir), env)
    assert r_bash.returncode == 0, (
        f"bash aggregate rc={r_bash.returncode}\nstderr={r_bash.stderr!r}"
    )
    bash_stats_path = bash_dir / "cache-stats.json"
    assert bash_stats_path.exists(), f"bash didn't write {bash_stats_path}"

    lh.aggregate_cache_stats(str(py_dir))
    py_stats_path = py_dir / "cache-stats.json"
    assert py_stats_path.exists(), f"python didn't write {py_stats_path}"

    bash_stats = json.loads(bash_stats_path.read_text())
    py_stats_raw = json.loads(py_stats_path.read_text())

    # Identical key set.
    assert sorted(bash_stats.keys()) == sorted(py_stats_raw.keys()), (
        f"key drift: bash={sorted(bash_stats.keys())} "
        f"py={sorted(py_stats_raw.keys())}"
    )

    # Integer totals + log_files_scanned must be byte-identical.
    for k in ("cache_creation_tokens", "cache_read_tokens",
              "uncached_input_tokens", "log_files_scanned"):
        assert bash_stats[k] == py_stats_raw[k], (
            f"{k} drift: bash={bash_stats[k]} py={py_stats_raw[k]}"
        )
        assert isinstance(bash_stats[k], int), (
            f"bash emitted non-int for {k}: {type(bash_stats[k]).__name__}"
        )
        assert isinstance(py_stats_raw[k], int), (
            f"python emitted non-int for {k}: {type(py_stats_raw[k]).__name__}"
        )

    # Floats: hit_rate + estimated_usd_saved (1e-6 tolerance per kickoff).
    assert abs(bash_stats["hit_rate"] - py_stats_raw["hit_rate"]) < 1e-6, (
        f"hit_rate drift: bash={bash_stats['hit_rate']} "
        f"py={py_stats_raw['hit_rate']}"
    )
    assert abs(bash_stats["estimated_usd_saved"] - py_stats_raw["estimated_usd_saved"]) < 1e-6, (
        f"saved_usd drift: bash={bash_stats['estimated_usd_saved']} "
        f"py={py_stats_raw['estimated_usd_saved']}"
    )

    # per_file: same length, same basenames, identical int fields.
    assert len(bash_stats["per_file"]) == len(py_stats_raw["per_file"]) == 3
    bash_by_name = {e["file"]: e for e in bash_stats["per_file"]}
    py_by_name = {e["file"]: e for e in py_stats_raw["per_file"]}
    assert set(bash_by_name) == set(py_by_name), (
        f"per_file basenames drift: bash={set(bash_by_name)} "
        f"py={set(py_by_name)}"
    )
    for name, b in bash_by_name.items():
        p = py_by_name[name]
        for k in ("cache_creation", "cache_read", "uncached"):
            assert b[k] == p[k], f"per_file[{name}].{k} drift: bash={b[k]} py={p[k]}"


# ─────────────────────────────────────────────────────────────────────
# (h) aggregate_cache_stats empty — zero-log dir → both write a valid
#     JSON with zeros + hit_rate:0 + per_file:[].
# ─────────────────────────────────────────────────────────────────────
def test_aggregate_cache_stats_empty_dir_parity(
    tmp_path: Path,
) -> None:
    """No .log files in iter_dir → both sides write a valid JSON with
    all-zero totals and an empty per_file array."""
    _which("bash")
    bash_dir = tmp_path / "bash" / "iter-1"
    py_dir = tmp_path / "py" / "iter-1"
    bash_dir.mkdir(parents=True)
    py_dir.mkdir(parents=True)

    env = _clean_env()

    r_bash = _bash_aggregate_cache_stats(str(bash_dir), env)
    assert r_bash.returncode == 0, f"bash empty-iter rc={r_bash.returncode}"

    lh.aggregate_cache_stats(str(py_dir))

    bash_stats = json.loads((bash_dir / "cache-stats.json").read_text())
    py_stats_raw = json.loads((py_dir / "cache-stats.json").read_text())

    # Both must show zero totals + zero hit_rate + empty per_file.
    for k in ("cache_creation_tokens", "cache_read_tokens",
              "uncached_input_tokens", "log_files_scanned"):
        assert bash_stats[k] == 0 == py_stats_raw[k], (
            f"empty-dir {k} drift: bash={bash_stats[k]} py={py_stats_raw[k]}"
        )
    assert bash_stats["hit_rate"] == 0
    assert py_stats_raw["hit_rate"] == 0
    assert bash_stats["per_file"] == []
    assert py_stats_raw["per_file"] == []
    # saved_usd should be exactly 0.0 on both sides (read=0 → 0.0*0.9*3/1e6=0).
    assert bash_stats["estimated_usd_saved"] == 0
    assert py_stats_raw["estimated_usd_saved"] == 0


# ─────────────────────────────────────────────────────────────────────
# claude_print — documented gap (live parity NOT exercised; see
# module docstring). This is a smoke test that the function exists
# and constructs the right argv shape using the cache flag from the
# PATH-shim. We don't actually invoke `claude --print` because that
# hits the network / model and is non-deterministic.
# ─────────────────────────────────────────────────────────────────────
def test_claude_print_argv_shape_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude_print() must build the right argv when cache is supported.

    Uses a real PATH-shim ``claude`` that:
      - when called as ``claude --help``, echoes the cache flag (so
        ``emit_cache_flags()`` returns the flag), and
      - when called as ``claude --print ...``, records its argv to
        a file and exits 0 (so we can inspect the constructed argv
        without actually invoking the real model).
    """
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    argv_log = shim_dir / "argv.log"
    (shim_dir / "claude").write_text(
        "#!/usr/bin/env bash\n"
        "# Shim: log argv to argv.log; echo cache flag in --help.\n"
        'if [ "$1" = "--help" ]; then\n'
        '  echo "Usage: claude [options]"\n'
        '  echo "  --exclude-dynamic-system-prompt-sections  Exclude dynamic system prompt sections"\n'
        '  echo "  --max-turns <N>                            Max turns"\n'
        '  exit 0\n'
        "fi\n"
        f'echo "ARGS:$(basename "$0") $@" >> "{argv_log}"\n'
        'echo "fake claude output"\n'
        'exit 0\n'
    )
    (shim_dir / "claude").chmod(0o755)

    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ.get('PATH', '')}")
    _point_python_env(monkeypatch)
    _reset_cache_probe(monkeypatch)

    r = lh.claude_print(
        "hello", "extra-arg", max_turns=5, output_format="json",
    )
    assert r.returncode == 0

    # The shim only logs non-`--help` invocations. The cache-flag probe
    # (``claude --help``) hits the shim's early-exit branch and leaves
    # no ARGS line — only the print call is recorded. We assert on the
    # single recorded line.
    lines = [ln for ln in argv_log.read_text().splitlines() if ln.startswith("ARGS:")]
    assert len(lines) == 1, f"expected 1 claude print invocation, got {len(lines)}: {lines}"
    argv_str = lines[0][len("ARGS:"):]
    argv = argv_str.split(" ")
    assert argv[0] == "claude"
    assert "--print" in argv
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "bypassPermissions"
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "json"
    idx = argv.index("--max-turns")
    assert argv[idx + 1] == "5"
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert "extra-arg" in argv
    # prompt must be the LAST argv (matches bash's "$@" $PROMPT order).
    assert argv[-1] == "hello"
