"""Unit tests: ``mini_ork.dispatch.lane_helpers`` (bash parity halves removed; formerly vs ``lib/lane-helpers.sh``).

Each test drives the Python port on controlled inputs and asserts flag
arrays, error text, exit behavior, and ``cache-stats.json`` content. No
mocks beyond PATH shims for the ``claude`` feature-detect.

Eight cases:
  (a) lane_is_free                 — 5 lanes (free vs paid) parametrised
  (b) emit_budget_flag             — free lane returns [], paid returns [--max-budget-usd, <val>]
  (c) emit_cache_flags feature-detect — PATH-shim fake `claude --help` echoes
                                       the cache flag → [flag]
  (d) emit_cache_flags disabled    — MO_PROMPT_CACHE_DISABLED=1 → []
  (e) assert_lane_capability OK    — capabilities satisfied → no raise
  (f) assert_lane_capability fail  — missing capability → RuntimeError with
                                       the first missing cap token
  (g) aggregate_cache_stats happy  — 3 fixture logs → totals + per_file shape
  (h) aggregate_cache_stats empty  — zero-log dir → valid JSON with zeros +
                                       hit_rate:0 + per_file:[]

``mo_claude_print`` is exercised via in-process argv-shape verification
(a real network call is non-deterministic).

Environment isolation:
  The shell pytest runs in often has MINI_ORK_HOME / MINI_ORK_ROOT /
  MINI_ORK_RUN_DIR / MO_LANE_REQUIRES_CAPABILITY /
  MO_PROMPT_CACHE_DISABLED set to whatever the operator exported. Each
  test pops the relevant vars and re-applies only its own overrides so
  pytest's arbitrary collection order cannot leak a prior test's env
  into the next.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import lane_helpers as lh

# Env vars that the port reads; each test pops them before applying its
# own overrides.
_ENV_KEYS = (
    "MINI_ORK_HOME",
    "MINI_ORK_ROOT",
    "MINI_ORK_RUN_DIR",
    "MO_LANE_REQUIRES_CAPABILITY",
    "MO_PROMPT_CACHE_DISABLED",
)


def _point_python_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Reset lane-helper-sensitive env vars for the Python process and
    apply ``overrides``."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in overrides.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def _reset_cache_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the emit_cache_flags probe to re-run on next call."""
    monkeypatch.setattr(lh, "_CACHED_SUPPORTED", None)
    monkeypatch.setattr(lh, "_PROBE_DONE", False)


# ─────────────────────────────────────────────────────────────────────
# (a) lane_is_free — 5 lanes (free + paid)
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lane,expected", [
    ("glm", True), ("kimi", True), ("minimax", True),
    ("opus", False), ("sonnet", False),
])
def test_lane_is_free(lane: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway lanes are free; anthropic lanes are paid."""
    _point_python_env(monkeypatch)
    assert lh.lane_is_free(lane) is expected


# ─────────────────────────────────────────────────────────────────────
# (b) emit_budget_flag — free lane returns []; paid returns
#     [--max-budget-usd, <val>].
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "lane,val,expected",
    [
        ("glm", "0.80", []),                                        # free → []
        ("opus", "0.80", ["--max-budget-usd", "0.80"]),             # paid → [flag, val]
        ("sonnet", "1.20", ["--max-budget-usd", "1.20"]),           # paid → [flag, val]
    ],
    ids=["free_glm", "paid_opus", "paid_sonnet"],
)
def test_emit_budget_flag(
    lane: str, val: str, expected: list, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _point_python_env(monkeypatch)
    assert lh.emit_budget_flag(lane, val) == expected


# ─────────────────────────────────────────────────────────────────────
# (c) emit_cache_flags feature-detect — PATH shim echoes the flag in
#     `claude --help` → [flag].
# ─────────────────────────────────────────────────────────────────────
def test_emit_cache_flags_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATH-shim fake `claude --help` echoes the cache flag →
    ``["--exclude-dynamic-system-prompt-sections"]``."""
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

    _point_python_env(monkeypatch)
    # Make subprocess.run find the shim first.
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ.get('PATH', '')}")
    _reset_cache_probe(monkeypatch)

    py_flags = lh.emit_cache_flags()
    assert py_flags == ["--exclude-dynamic-system-prompt-sections"]


# ─────────────────────────────────────────────────────────────────────
# (d) emit_cache_flags disabled — MO_PROMPT_CACHE_DISABLED=1 returns [].
# ─────────────────────────────────────────────────────────────────────
def test_emit_cache_flags_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MO_PROMPT_CACHE_DISABLED=1 short-circuits to [] (the probe never
    fires, so no PATH shim is needed)."""
    _point_python_env(monkeypatch, MO_PROMPT_CACHE_DISABLED="1")
    _reset_cache_probe(monkeypatch)

    assert lh.emit_cache_flags() == []


# ─────────────────────────────────────────────────────────────────────
# (e) assert_lane_capability OK — capabilities satisfied → no raise.
# ─────────────────────────────────────────────────────────────────────
def test_assert_lane_capability_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed a tmpdir agents.yaml with capabilities satisfied."""
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

    _point_python_env(
        monkeypatch,
        MINI_ORK_HOME=str(home),
        MINI_ORK_ROOT=str(REPO),
        MINI_ORK_RUN_DIR="",
        MO_LANE_REQUIRES_CAPABILITY="tools,reasoning",
    )

    # resolve_agents_yaml prints the path; redirect stdout to keep the
    # test's own output clean (the helper is called for side-effect).
    with contextlib.redirect_stdout(io.StringIO()):
        lh.assert_lane_capability("implementer")  # must not raise


# ─────────────────────────────────────────────────────────────────────
# (f) assert_lane_capability fail — missing cap → RuntimeError with token.
# ─────────────────────────────────────────────────────────────────────
def test_assert_lane_capability_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lane family lacks one required capability → RuntimeError carrying
    the first missing cap token."""
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

    _point_python_env(
        monkeypatch,
        MINI_ORK_HOME=str(home),
        MINI_ORK_ROOT=str(REPO),
        MINI_ORK_RUN_DIR="",
        MO_LANE_REQUIRES_CAPABILITY="tools,vision",
    )

    with contextlib.redirect_stdout(io.StringIO()):
        with pytest.raises(RuntimeError) as exc_info:
            lh.assert_lane_capability("implementer")
    assert str(exc_info.value) == "vision", (
        f"python missing-cap token={exc_info.value!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# (g) aggregate_cache_stats happy — totals + per_file shape
# ─────────────────────────────────────────────────────────────────────
def test_aggregate_cache_stats(
    tmp_path: Path,
) -> None:
    """Seed 3 fixture logs; cache-stats.json carries the summed totals +
    per_file rows (one per log)."""
    py_dir = tmp_path / "py" / "iter-1"
    py_dir.mkdir(parents=True)

    fixture_body = (
        '{"event":"usage","cache_creation_input_tokens":100,"cache_read_input_tokens":50,"input_tokens":25}\n'
        '{"event":"usage","cache_creation_input_tokens":200,"cache_read_input_tokens":75,"input_tokens":50}\n'
    )
    for name in ("a.log", "b.log", "c.log"):
        (py_dir / name).write_text(fixture_body)

    lh.aggregate_cache_stats(str(py_dir))
    py_stats_path = py_dir / "cache-stats.json"
    assert py_stats_path.exists(), f"python didn't write {py_stats_path}"

    stats = json.loads(py_stats_path.read_text())

    # Integer totals: 3 files × (100+200) creation, (50+75) read, (25+50) uncached.
    assert stats["cache_creation_tokens"] == 900
    assert stats["cache_read_tokens"] == 375
    assert stats["uncached_input_tokens"] == 225
    assert stats["log_files_scanned"] == 3

    # hit_rate is a ratio in (0, 1).
    assert 0 < stats["hit_rate"] < 1

    # per_file: one row per log with per-file sums.
    assert len(stats["per_file"]) == 3
    by_name = {e["file"]: e for e in stats["per_file"]}
    assert set(by_name) == {"a.log", "b.log", "c.log"}
    for e in by_name.values():
        assert e["cache_creation"] == 300
        assert e["cache_read"] == 125
        assert e["uncached"] == 75


# ─────────────────────────────────────────────────────────────────────
# (h) aggregate_cache_stats empty — zero-log dir → valid JSON zeros
# ─────────────────────────────────────────────────────────────────────
def test_aggregate_cache_stats_empty_dir(
    tmp_path: Path,
) -> None:
    """No .log files in iter_dir → a valid JSON with all-zero totals and
    an empty per_file array."""
    py_dir = tmp_path / "py" / "iter-1"
    py_dir.mkdir(parents=True)

    lh.aggregate_cache_stats(str(py_dir))

    stats = json.loads((py_dir / "cache-stats.json").read_text())

    for k in ("cache_creation_tokens", "cache_read_tokens",
              "uncached_input_tokens", "log_files_scanned"):
        assert stats[k] == 0
    assert stats["hit_rate"] == 0
    assert stats["per_file"] == []
    # saved_usd exactly 0.0 (read=0 → 0.0*0.9*3/1e6=0).
    assert stats["estimated_usd_saved"] == 0


# ─────────────────────────────────────────────────────────────────────
# claude_print — argv-shape smoke (no network call).
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
    # prompt must be the LAST argv (matches the "$@" $PROMPT order).
    assert argv[-1] == "hello"
