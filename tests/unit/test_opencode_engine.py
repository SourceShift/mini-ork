"""Tests for the opencode dispatch engine.

Two invariants are pinned here:

1. The ratchet — `opencode` is registered as an EXECUTABLE_MODEL so it has
   its own engine and the CLAUDE-only argv builder does NOT leak into it.
   This is the same ratchet `codex` rides on (test_engine_command_builder_py).

2. The provider-resolver contract — a `kind: opencode-native` entry in
   providers.yaml round-trips through resolve_provider into a ProviderSpec
   whose command is the opencode transport driver (not `codex` and not
   `claude`). The argparse-by-proxy happens in the transport itself, which
   the second suite exercises with a mocked opencode CLI on PATH.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.dispatch import opencode_transport as ot
from mini_ork.dispatch.providers import (
    ENGINE_COMMAND_BUILDERS,
    EXECUTABLE_MODELS,
    MODEL_DISPATCH_BACKENDS,
    PROVIDER_KIND_BUILDERS,
    _build_opencode_native,
    _opencode_transport_command,
    engine_of,
    resolve_provider,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Fake opencode CLI (python): logs its argv, emits a JSON event stream with a
# text event + a step_finish event carrying usage tokens + cost, and exits
# $FAKE_OPENCODE_RC. The transport parses the JSON to recover the body +
# sidecars.
FAKE_OPENCODE = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
log = os.environ.get("FAKE_OPENCODE_ARGV_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\\n")
print('{"type":"step_start","timestamp":1,"sessionID":"ses-unit"}')
print('{"type":"text","timestamp":2,"sessionID":"ses-unit","part":{"type":"text","text":"alpha body"}}')
print('{"type":"step_finish","timestamp":3,"sessionID":"ses-unit","part":{"type":"step-finish","tokens":{"input":1500,"output":250,"cache":{"read":500,"write":0}},"cost":0.0023,"text":""}}')
sys.exit(int(os.environ.get("FAKE_OPENCODE_RC", "0")))
"""

ENV_KEYS = (
    "MO_OPENCODE_MODEL",
    "MO_USAGE_FILE",
    "MO_TURNS_FILE",
    "MO_COST_FILE",
    "MO_TARGET_CWD",
    "MINI_ORK_TARGET_REPO",
    "MO_ALLOW_FRAMEWORK_CWD",
    "MO_OPENCODE_STREAM_FILE",
    "GIT_TERMINAL_PROMPT",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH_COMMAND",
    "FAKE_OPENCODE_RC",
    "FAKE_OPENCODE_ARGV_LOG",
)


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def target(tmp_path, monkeypatch):
    """A plain (non-framework) MO_TARGET_CWD so the guard passes."""
    d = tmp_path / "target"
    d.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(d))
    return d


@pytest.fixture
def fake_opencode(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "opencode"
    fake.write_text(FAKE_OPENCODE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake


def _argv_log(tmp_path, monkeypatch) -> Path:
    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_OPENCODE_ARGV_LOG", str(log))
    return log


def _read_argv(log: Path) -> list[str]:
    return json.loads(log.read_text().splitlines()[-1])


# ─── ratchet: opencode is registered with the right structural shape ─────────


def test_opencode_in_executable_models():
    """The first invariant. codex rides on this set; opencode must too so
    `engine_of('opencode') == 'opencode'` (its own engine, not 'claude')."""
    assert "opencode" in EXECUTABLE_MODELS


def test_engine_of_opencode_is_opencode_not_claude():
    """Matches test_engine_of_executable_is_the_model_itself for codex."""
    assert engine_of("opencode") == "opencode"


def test_opencode_has_no_engine_command_builder():
    """The load-bearing ratchet. Without this, dispatch_model would inject
    --allowedTools/--strict-mcp-config/--resume into opencode's argv, and the
    CLI would reject the unknown flags. test_engine_command_builder_py pins
    the same invariant for codex."""
    assert ENGINE_COMMAND_BUILDERS.get("opencode") is None


def test_opencode_provider_kind_builder_registered():
    """resolve_provider route must know about opencode-native — adding a new
    kind without registering the builder would raise 'unsupported kind'."""
    assert "opencode-native" in PROVIDER_KIND_BUILDERS
    assert PROVIDER_KIND_BUILDERS["opencode-native"] is _build_opencode_native


def test_opencode_dispatch_backend_registered():
    """The opencode transport writes usage/cost to sidecar files; the dispatch
    backend reads them back into the result. Without this entry, dispatch_model
    would fall back to _dispatch_standard and usage/cost would be 0."""
    assert "opencode" in MODEL_DISPATCH_BACKENDS


# ─── provider-resolver contract ──────────────────────────────────────────────


def test_opencode_transport_command_includes_module_invocation():
    """The driver argv shape. resolve_provider hands this exact tuple to
    subprocess; the verifier checks for the module path substring."""
    cmd = _opencode_transport_command()
    assert cmd[:3] == (sys.executable, "-m", "mini_ork.dispatch.opencode_transport")
    assert "--print" in cmd
    assert "text" in cmd


def test_resolve_provider_opencode_returns_opencode_driver(tmp_path, monkeypatch):
    """End-to-end: write a providers.yaml with an opencode-native entry, call
    resolve_provider, and assert the command is the opencode driver (not the
    codex driver, not the claude driver)."""
    registry = tmp_path / "config"
    registry.mkdir()
    (registry / "providers.yaml").write_text(
        "providers:\n"
        "  opencode_test:\n"
        "    kind: opencode-native\n"
        "    family: opencode\n"
    )
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(registry / "providers.yaml"))
    spec = resolve_provider("opencode_test", tmp_path)
    assert "mini_ork.dispatch.opencode_transport" in spec.command
    assert "mini_ork.dispatch.codex_transport" not in spec.command
    assert "claude" not in spec.command


def test_resolve_provider_opencode_injects_pythonpath(tmp_path, monkeypatch):
    """The opencode transport is a Python module — the subprocess needs
    PYTHONPATH set so `python3 -m mini_ork.dispatch.opencode_transport`
    resolves when the operator invokes mini-ork from outside the repo."""
    registry = tmp_path / "config"
    registry.mkdir()
    (registry / "providers.yaml").write_text(
        "providers:\n"
        "  opencode_test:\n"
        "    kind: opencode-native\n"
        "    family: opencode\n"
    )
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(registry / "providers.yaml"))
    spec = resolve_provider("opencode_test", tmp_path)
    assert "PYTHONPATH" in spec.env
    assert str(REPO_ROOT) in spec.env["PYTHONPATH"]


# ─── arg dialect (the same as codex's, accept-and-ignore claude flags) ───────


def test_parse_args_accepts_and_ignores_claude_compat_flags():
    fmt, prompt = ot._parse_args(
        [
            "--print",
            "--permission-mode",
            "bypassPermissions",
            "--max-turns",
            "3",
            "--exclude-dynamic-system-prompt-sections",
            "--unknown-flag",
            "-x",
            "--output-format",
            "json",
            "the prompt",
            "ignored-second-positional",
        ]
    )
    assert fmt == "json"
    assert prompt == "the prompt"


def test_parse_args_defaults_to_text():
    assert ot._parse_args(["hi"]) == ("text", "hi")
    assert ot._parse_args([]) == ("text", "")


# ─── rc 2 / 3 paths ──────────────────────────────────────────────────────────


def test_no_prompt_tty_stdin_exits_2(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Tty(""))
    assert ot.main(["--print", "--output-format", "text"]) == 2
    assert "no prompt provided" in capsys.readouterr().err


def test_missing_opencode_cli_exits_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))  # no opencode here
    rc = ot.main(["--print", "--output-format", "text", "hi"])
    assert rc == 3
    assert "opencode CLI not found on PATH" in capsys.readouterr().err


# ─── the actual argv the transport builds (the contract's load-bearing check) ─


def test_transport_builds_opencode_argv_with_dir_and_auto_and_no_model(
    fake_opencode, target, tmp_path, monkeypatch
):
    """The mocked opencode CLI never sees MO_OPENCODE_MODEL → -m MUST be
    omitted (an empty -m value would make the CLI reject the call)."""
    log = _argv_log(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ot.main(["--print", "--output-format", "text", "hi"]) == 0
    argv = _read_argv(log)
    assert argv[:3] == ["run", "--format", "json"]
    assert "--auto" in argv
    assert "-m" not in argv
    assert "--dir" in argv and argv[argv.index("--dir") + 1] == str(target)
    assert argv[-2:] == ["--", "hi"]


def test_transport_passes_minus_m_when_MO_OPENCODE_MODEL_set(
    fake_opencode, target, tmp_path, monkeypatch
):
    """The MO_OPENCODE_MODEL lever — `-m <provider/model>` lands AFTER --dir
    so the model pin beats the working dir in the CLI's resolution order."""
    log = _argv_log(tmp_path, monkeypatch)
    monkeypatch.setenv("MO_OPENCODE_MODEL", "zai-coding-plan/glm-5.1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ot.main(["--print", "--output-format", "text", "hi"]) == 0
    argv = _read_argv(log)
    mi = argv.index("-m")
    assert argv[mi + 1] == "zai-coding-plan/glm-5.1"
    # model flag must come AFTER --dir (CLI ordering)
    assert argv.index("--dir") < mi


def test_transport_dir_flag_skipped_for_nonexistent_dir(fake_opencode, tmp_path, monkeypatch):
    log = _argv_log(tmp_path, monkeypatch)
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ot.main(["p"]) == 0
    assert "--dir" not in _read_argv(log)


# ─── framework-tree cwd guard (same contract as codex's) ─────────────────────


def test_guard_refuses_install_root(fake_opencode, tmp_path, monkeypatch, capsys):
    framework = tmp_path / "somewhere"
    (framework / "bin").mkdir(parents=True)
    (framework / "bin" / "mini-ork").write_text("#!/bin/sh\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(framework))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ot.main(["p"]) == 2
    assert "cwd guard FAILED" in capsys.readouterr().err


@pytest.mark.parametrize(
    "suffix", [".mini-ork", ".mini-ork/runs", "mini-ork", "mini-ork/sub"]
)
def test_guard_refuses_framework_path_patterns(fake_opencode, tmp_path, monkeypatch, capsys, suffix):
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path / suffix))  # may not exist
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ot.main(["p"]) == 2
    assert "cwd guard FAILED" in capsys.readouterr().err


def test_guard_optin_allows_framework_cwd(fake_opencode, tmp_path, monkeypatch):
    framework = tmp_path / "somewhere"
    (framework / "bin").mkdir(parents=True)
    (framework / "bin" / "mini-ork").write_text("#!/bin/sh\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(framework))
    monkeypatch.setenv("MO_ALLOW_FRAMEWORK_CWD", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ot.main(["p"]) == 0


# ─── env hardening (matches codex's setdefault semantics) ────────────────────


def test_env_hardening_setdefault(fake_opencode, target, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ot.main(["p"]) == 0
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
    assert os.environ["GIT_ASKPASS"] == "/bin/false"
    assert os.environ["SSH_ASKPASS"] == "/bin/false"
    assert "BatchMode=yes" in os.environ["GIT_SSH_COMMAND"]


def test_env_hardening_preserves_operator_pins(fake_opencode, target, monkeypatch):
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ot.main(["p"]) == 0
    assert os.environ["GIT_TERMINAL_PROMPT"] == "1"  # setdefault semantics


# ─── harvest math: token+cost sidecars from JSON events ──────────────────────


STREAM = "\n".join(
    [
        '[cl_opencode] launching opencode run cwd=/x',
        "[cl_opencode] prompt_bytes=5",
        '{"type":"step_start","timestamp":1,"sessionID":"s-1"}',
        "not-json-noise",
        '{"type":"text","timestamp":2,"sessionID":"s-1","part":{"type":"text","text":"alpha"}}',
        '{"type":"step_finish","timestamp":3,"sessionID":"s-1","part":{"tokens":{"input":1000,"output":500,"cache":{"read":200,"write":99}},"cost":0.0015}}',
        "{malformed",
        '{"type":"step_finish","timestamp":4,"sessionID":"s-1","part":{"tokens":{"input":2000,"output":700,"cache":{"read":100,"write":42}},"cost":0.0028}}',
    ]
)


def _harvest(tmp_path, **paths):
    usage = tmp_path / "u.tokens"
    turns = tmp_path / "turns.jsonl"
    cost = tmp_path / "cost.txt"
    ot.harvest(
        STREAM,
        str(usage) if paths.get("usage", True) else "",
        str(turns) if paths.get("turns", True) else "",
        str(cost) if paths.get("cost", True) else "",
    )
    return usage, turns, cost


def test_harvest_usage_and_turns_exact(tmp_path):
    usage, turns, _cost = _harvest(tmp_path)
    assert usage.read_text() == "3000\t1200\n"
    lines = turns.read_text().splitlines()
    assert json.loads(lines[0]) == {
        "turn_index": 0,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 200,
        "model": "opencode",
        "session_id": "s-1",
    }
    assert json.loads(lines[1]) == {
        "turn_index": 1,
        "input_tokens": 2000,
        "output_tokens": 700,
        "cache_read_input_tokens": 100,
        "model": "opencode",
        "session_id": "s-1",
    }


def test_harvest_cost_sums_step_finish_costs(tmp_path):
    _u, _t, cost = _harvest(tmp_path)
    assert cost.read_text() == "0.004300\n"


def test_harvest_cost_written_when_zero_cost_with_tokens(tmp_path):
    """Tokens exist but the provider didn't surface a cost → write 0.000000
    so the cost-file reader sees a deliberate zero, not a missing file."""
    stream = '{"type":"step_finish","timestamp":3,"sessionID":"s-1","part":{"tokens":{"input":1,"output":1,"cache":0}}}'
    usage = tmp_path / "u.tokens"
    turns = tmp_path / "turns.jsonl"
    cost = tmp_path / "cost.txt"
    ot.harvest(stream, str(usage), str(turns), str(cost))
    assert cost.read_text() == "0.000000\n"


def test_harvest_skips_sidecars_without_usage(tmp_path):
    """No step_finish events → no sidecars written (matches codex's contract)."""
    usage = tmp_path / "u.tokens"
    turns = tmp_path / "turns.jsonl"
    cost = tmp_path / "cost.txt"
    ot.harvest("noise only\n", str(usage), str(turns), str(cost))
    assert not usage.exists() and not turns.exists() and not cost.exists()


def test_harvest_cache_read_from_nested_object(tmp_path):
    """Real opencode `--format json` emits tokens.cache as a nested
    {"read","write"} object (verified against v1.18.4); cache-read must come
    from `.read`, never the `.write` half and never the whole dict coerced."""
    stream = '{"type":"step_finish","sessionID":"s","part":{"tokens":{"input":10,"output":5,"cache":{"read":77,"write":999}},"cost":0}}'
    turns = tmp_path / "turns.jsonl"
    ot.harvest(stream, str(tmp_path / "u.tokens"), str(turns), "")
    assert json.loads(turns.read_text().splitlines()[0])["cache_read_input_tokens"] == 77


def test_harvest_cache_scalar_forward_compat(tmp_path):
    """A flatter, scalar `tokens.cache` still coerces (forward-compat fallback
    for a hypothetical schema change)."""
    stream = '{"type":"step_finish","sessionID":"s","part":{"tokens":{"input":10,"output":5,"cache":33},"cost":0}}'
    turns = tmp_path / "turns.jsonl"
    ot.harvest(stream, str(tmp_path / "u.tokens"), str(turns), "")
    assert json.loads(turns.read_text().splitlines()[0])["cache_read_input_tokens"] == 33


# ─── body: reconstructed from text events ────────────────────────────────────


def test_reconstruct_body_joins_text_events():
    stream = "\n".join(
        [
            '{"type":"text","part":{"text":"one"}}',
            '{"type":"tool_call","part":{"skip":true}}',
            '{"type":"text","part":{"text":"two"}}',
        ]
    )
    assert ot.reconstruct_body(stream) == "one\n\ntwo"


def test_reconstruct_body_falls_back_when_no_text_events():
    assert ot.reconstruct_body("") == ""


# ─── subprocess contract: full pipeline against the fake opencode CLI ────────


def _run_transport(fmt, tmp_path, target, monkeypatch, fake_rc="0"):
    side = tmp_path / "native"
    side.mkdir()
    usage, turns, cost = (side / n for n in ("u.tokens", "turns.jsonl", "cost.txt"))
    env = dict(os.environ)
    env.update(
        {
            "MO_USAGE_FILE": str(usage),
            "MO_TURNS_FILE": str(turns),
            "MO_COST_FILE": str(cost),
            "MO_TARGET_CWD": str(target),
            "FAKE_OPENCODE_RC": fake_rc,
        }
    )
    for key in (
        "MO_OPENCODE_MODEL",
        "MINI_ORK_TARGET_REPO",
        "MO_ALLOW_FRAMEWORK_CWD",
        "MO_OPENCODE_STREAM_FILE",
        "FAKE_OPENCODE_ARGV_LOG",
    ):
        env.pop(key, None)
    cmd = [
        sys.executable,
        "-m",
        "mini_ork.dispatch.opencode_transport",
        "--print",
        "--output-format",
        fmt,
        "Reply with alpha",
    ]
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(target),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return proc, usage, turns, cost


def _read_or_none(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_native_subprocess_writes_expected_output_and_sidecars(
    fake_opencode, target, tmp_path, monkeypatch, fmt
):
    proc, usage, turns, cost = _run_transport(fmt, tmp_path, target, monkeypatch)

    assert proc.returncode == 0
    assert _read_or_none(usage) == "1500\t250\n"
    assert json.loads(turns.read_text()) == {
        "turn_index": 0,
        "input_tokens": 1500,
        "output_tokens": 250,
        "cache_read_input_tokens": 500,
        "model": "opencode",
        "session_id": "ses-unit",
    }
    assert _read_or_none(cost) == "0.002300\n"


def test_native_subprocess_maps_opencode_failure_to_rc4(fake_opencode, target, tmp_path, monkeypatch):
    proc, *_ = _run_transport("text", tmp_path, target, monkeypatch, fake_rc="1")
    assert proc.returncode == 4
    assert "opencode run failed with rc=1" in proc.stderr
