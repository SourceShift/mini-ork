"""Unit and subprocess contract tests for the native Codex transport.

Unit tests drive main() in-process with a fake ``codex`` CLI on PATH. The
subprocess section verifies the installed module invocation, stdout, sidecars,
and exit codes against the same fake CLI.
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

from mini_ork.dispatch import codex_transport as ct

REPO_ROOT = Path(__file__).resolve().parents[2]

# Fake codex CLI (python): logs its argv, emits a codex-style JSONL event
# stream (thread.started + turn.completed with usage incl. cached tokens + an
# agent_message), and honors --output-last-message according to
# FAKE_CODEX_MODE (ok: writes "alpha body"; nolast: leaves it empty so the
# transport must reconstruct from agent_message events; transcript: writes a
# full transcript envelope). Exits $FAKE_CODEX_RC.
FAKE_CODEX = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
log = os.environ.get("FAKE_CODEX_ARGV_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\\n")
out = ""
prompt = ""
i = 0
while i < len(argv):
    if argv[i] == "--output-last-message" and i + 1 < len(argv):
        out = argv[i + 1]
        i += 2
        continue
    if argv[i] == "--":
        prompt = argv[i + 1] if i + 1 < len(argv) else ""
        break
    i += 1
print('{"type":"thread.started","thread_id":"thr-unit"}')
print('{"type":"turn.completed","usage":{"input_tokens":1500,"output_tokens":250,"cached_input_tokens":500}}')
print('{"type":"item.completed","item":{"type":"agent_message","text":"alpha body"}}')
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
if mode == "transcript":
    with open(out, "w") as f:
        f.write(os.environ["FAKE_CODEX_TRANSCRIPT"])
elif mode != "nolast" and out:
    with open(out, "w") as f:
        f.write("alpha body\\n")
sys.exit(int(os.environ.get("FAKE_CODEX_RC", "0")))
"""

ENV_KEYS = (
    "MO_OAI_BASE_URL",
    "MO_OAI_ENV_KEY",
    "MO_OAI_MODEL",
    "MO_USAGE_FILE",
    "MO_TURNS_FILE",
    "MO_COST_FILE",
    "MO_TARGET_CWD",
    "MINI_ORK_TARGET_REPO",
    "MO_ALLOW_FRAMEWORK_CWD",
    "CODEX_SANDBOX",
    "MO_PRICING_YAML",
    "MINI_ORK_HOME",
    "MO_CODEX_USD_PER_MTOK_IN",
    "MO_CODEX_USD_PER_MTOK_CACHED",
    "MO_CODEX_USD_PER_MTOK_OUT",
    "MO_CODEX_STREAM_FILE",
    "GIT_TERMINAL_PROMPT",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH_COMMAND",
    "FAKE_CODEX_RC",
    "FAKE_CODEX_MODE",
    "FAKE_CODEX_ARGV_LOG",
    "FAKE_CODEX_TRANSCRIPT",
    "TEST_OAI_KEY",
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
def fake_codex(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake


def _argv_log(tmp_path, monkeypatch) -> Path:
    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGV_LOG", str(log))
    return log


def _read_argv(log: Path) -> list[str]:
    return json.loads(log.read_text().splitlines()[-1])


# ── arg dialect ─────────────────────────────────────────────────────────────


def test_parse_args_accepts_and_ignores_claude_compat_flags():
    fmt, prompt = ct._parse_args(
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
    assert ct._parse_args(["hi"]) == ("text", "hi")
    assert ct._parse_args([]) == ("text", "")


# ── rc 2 / 3 / 5 paths ──────────────────────────────────────────────────────


def test_no_prompt_tty_stdin_exits_2(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Tty(""))
    assert ct.main(["--print", "--output-format", "text"]) == 2
    assert "no prompt provided" in capsys.readouterr().err


def test_missing_codex_cli_exits_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))  # no codex here
    rc = ct.main(["--print", "--output-format", "text", "hi"])
    assert rc == 3
    assert "codex CLI not found on PATH" in capsys.readouterr().err


def test_byo_endpoint_with_empty_key_exits_5(fake_codex, target, monkeypatch, capsys):
    monkeypatch.setenv("MO_OAI_BASE_URL", "https://oai.example/v1")
    monkeypatch.setenv("MO_OAI_ENV_KEY", "TEST_OAI_KEY")  # var itself unset
    rc = ct.main(["--print", "--output-format", "text", "hi"])
    assert rc == 5
    assert "$TEST_OAI_KEY is empty" in capsys.readouterr().err


# ── BYO flag construction ───────────────────────────────────────────────────


def test_byo_flags_constructed(fake_codex, target, tmp_path, monkeypatch):
    log = _argv_log(tmp_path, monkeypatch)
    monkeypatch.setenv("MO_OAI_BASE_URL", "https://oai.example/v1")
    monkeypatch.setenv("MO_OAI_ENV_KEY", "TEST_OAI_KEY")
    monkeypatch.setenv("TEST_OAI_KEY", "secret")
    monkeypatch.setenv("MO_OAI_MODEL", "gpt-test")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    assert ct.main(["--print", "--output-format", "text", "hi"]) == 0
    argv = _read_argv(log)
    assert argv[:6] == [
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--json",
        "--output-last-message",
    ]
    assert "-C" in argv and argv[argv.index("-C") + 1] == str(target)
    provider_cfg = (
        'model_providers.mini_ork={ name = "mini-ork BYO", '
        'base_url = "https://oai.example/v1", env_key = "TEST_OAI_KEY", '
        'wire_api = "chat" }'
    )
    ci = argv.index("-c")
    assert argv[ci + 1] == provider_cfg
    assert argv[ci + 2] == "-c" and argv[ci + 3] == "model_provider=mini_ork"
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-test"
    assert argv[-2:] == ["--", "hi"]
    assert "secret" not in " ".join(argv)  # key value never on argv


def test_no_byo_flags_without_endpoint_env(fake_codex, target, tmp_path, monkeypatch):
    log = _argv_log(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["--print", "--output-format", "text", "hi"]) == 0
    argv = _read_argv(log)
    assert "-c" not in argv and "-m" not in argv


def test_codex_sandbox_override(fake_codex, target, tmp_path, monkeypatch):
    log = _argv_log(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEX_SANDBOX", "read-only")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 0
    argv = _read_argv(log)
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_cd_flag_skipped_for_nonexistent_dir(fake_codex, tmp_path, monkeypatch):
    log = _argv_log(tmp_path, monkeypatch)
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 0
    assert "-C" not in _read_argv(log)


# ── framework-tree cwd guard ────────────────────────────────────────────────


def test_guard_refuses_install_root(fake_codex, tmp_path, monkeypatch, capsys):
    framework = tmp_path / "somewhere"
    (framework / "bin").mkdir(parents=True)
    (framework / "bin" / "mini-ork").write_text("#!/bin/sh\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(framework))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 2
    assert "cwd guard FAILED" in capsys.readouterr().err


@pytest.mark.parametrize(
    "suffix", [".mini-ork", ".mini-ork/runs", "mini-ork", "mini-ork/sub"]
)
def test_guard_refuses_framework_path_patterns(fake_codex, tmp_path, monkeypatch, capsys, suffix):
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path / suffix))  # may not exist
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 2
    assert "cwd guard FAILED" in capsys.readouterr().err


def test_guard_optin_allows_framework_cwd(fake_codex, tmp_path, monkeypatch):
    framework = tmp_path / "somewhere"
    (framework / "bin").mkdir(parents=True)
    (framework / "bin" / "mini-ork").write_text("#!/bin/sh\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(framework))
    monkeypatch.setenv("MO_ALLOW_FRAMEWORK_CWD", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 0


# ── env hardening ───────────────────────────────────────────────────────────


def test_env_hardening_setdefault(fake_codex, target, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 0
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
    assert os.environ["GIT_ASKPASS"] == "/bin/false"
    assert os.environ["SSH_ASKPASS"] == "/bin/false"
    assert "BatchMode=yes" in os.environ["GIT_SSH_COMMAND"]


def test_env_hardening_preserves_operator_pins(fake_codex, target, monkeypatch):
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 0
    assert os.environ["GIT_TERMINAL_PROMPT"] == "1"  # setdefault semantics


# ── stream sidecar ──────────────────────────────────────────────────────────


def test_stream_sidecar_launch_lines(fake_codex, target, tmp_path, monkeypatch):
    usage = tmp_path / "u.tokens"
    monkeypatch.setenv("MO_USAGE_FILE", str(usage))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["hi"]) == 0
    stream = tmp_path / "u.stream.jsonl"
    assert os.environ["MO_CODEX_STREAM_FILE"] == str(stream)
    lines = stream.read_text().splitlines()
    assert lines[0] == (
        f"[cl_codex] launching codex exec cwd={target} sandbox=workspace-write"
    )
    assert lines[1] == "[cl_codex] prompt_bytes=2"


def test_stream_sidecar_mktemp_without_usage_file(fake_codex, target, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["hi"]) == 0
    stream = os.environ["MO_CODEX_STREAM_FILE"]
    assert "mini-ork-codex-stream." in stream
    assert Path(stream).is_file()


# ── harvest math ────────────────────────────────────────────────────────────

STREAM = "\n".join(
    [
        "[cl_codex] launching codex exec cwd=/x sandbox=workspace-write",
        "[cl_codex] prompt_bytes=5",
        '{"type":"thread.started","thread_id":"t-1"}',
        "not-json-noise",
        '{"type":"turn.completed","usage":{"input_tokens":1000,"output_tokens":500,"cached_input_tokens":200}}',
        "{malformed",
        '{"type":"turn.completed","usage":{"input_tokens":2000,"output_tokens":700,"cached_input_tokens":100}}',
    ]
)


def _harvest(tmp_path, monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    usage = tmp_path / "u.tokens"
    turns = tmp_path / "turns.jsonl"
    cost = tmp_path / "cost.txt"
    ct.harvest(STREAM, str(usage), str(turns), str(cost), os.environ)
    return usage, turns, cost


def test_harvest_usage_and_turns_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_PRICING_YAML", str(tmp_path / "none.yaml"))
    usage, turns, _cost = _harvest(tmp_path, monkeypatch)
    assert usage.read_text() == "3000\t1200\n"
    lines = turns.read_text().splitlines()
    assert json.loads(lines[0]) == {
        "turn_index": 0,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 200,
        "model": "codex",
        "session_id": "t-1",
    }
    assert json.loads(lines[1]) == {
        "turn_index": 1,
        "input_tokens": 2000,
        "output_tokens": 700,
        "cache_read_input_tokens": 100,
        "model": "codex",
        "session_id": "t-1",
    }


def test_harvest_cost_env_override_wins(tmp_path, monkeypatch):
    _u, _t, cost = _harvest(
        tmp_path,
        monkeypatch,
        MO_CODEX_USD_PER_MTOK_IN="2.0",
        MO_CODEX_USD_PER_MTOK_CACHED="0.5",
        MO_CODEX_USD_PER_MTOK_OUT="8.0",
        MO_PRICING_YAML=str(tmp_path / "none.yaml"),
    )
    # fresh_in = 3000-300 = 2700 → (2700*2.0 + 300*0.5 + 1200*8.0)/1e6
    assert cost.read_text() == "0.015150\n"


def test_harvest_cost_pricing_yaml_lookup(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text(
        "pricing:\n  openai:\n    gpt-5:\n      input: 2.50\n      output: 10.00\n"
    )
    _u, _t, cost = _harvest(tmp_path, monkeypatch, MO_PRICING_YAML=str(pricing))
    # input/cache from yaml (cache_read absent → default 0.125); output 10.00
    expected = (2700 * 2.50 + 300 * 0.125 + 1200 * 10.00) / 1e6
    assert cost.read_text() == f"{expected:.6f}\n"


def test_harvest_cost_hardcoded_defaults(tmp_path, monkeypatch):
    _u, _t, cost = _harvest(
        tmp_path, monkeypatch, MO_PRICING_YAML=str(tmp_path / "none.yaml")
    )
    expected = (2700 * 1.25 + 300 * 0.125 + 1200 * 10.0) / 1e6
    assert cost.read_text() == f"{expected:.6f}\n"


def test_harvest_skips_sidecars_without_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_PRICING_YAML", str(tmp_path / "none.yaml"))
    usage = tmp_path / "u.tokens"
    turns = tmp_path / "turns.jsonl"
    cost = tmp_path / "cost.txt"
    ct.harvest("noise only\n", str(usage), str(turns), str(cost), os.environ)
    assert not usage.exists() and not turns.exists() and not cost.exists()


# ── body: reconstruction + envelope stripping ───────────────────────────────


def test_body_reconstructed_from_agent_messages(fake_codex, target, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_MODE", "nolast")  # --output-last-message stays empty
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 0
    assert capsys.readouterr().out == "alpha body\n"


def test_reconstruct_body_joins_multiple_messages():
    stream = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"agent_message","text":"one"}}',
            '{"type":"item.completed","item":{"type":"tool_call","text":"skip"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"two"}}',
        ]
    )
    assert ct.reconstruct_body(stream) == "one\n\ntwo"


TRANSCRIPT = "\n".join(
    [
        "[2026-07-27T08:00:00] boot",
        "OpenAI Codex v0.5",
        "workdir: /tmp/x",
        "model: gpt-5",
        "provider: openai",
        "approval: never",
        "sandbox: workspace-write",
        "reasoning: high",
        "session id: s1",
        "User instructions: do x",
        "Reading additional input from stdin...",
        "hook: session-start",
        "----------------",
        "user",
        "the full prompt",
        "codex",
        "real answer line 1",
        "real answer line 2",
        "tokens used: 999",
    ]
)


def test_strip_envelope_keeps_text_after_final_codex_marker():
    assert ct.strip_envelope(TRANSCRIPT) == "real answer line 1\nreal answer line 2"


def test_strip_envelope_without_marker_drops_only_status_lines():
    text = "OpenAI Codex v0.5\nplain answer\ntokens used: 3\n"
    assert ct.strip_envelope(text) == "plain answer"


def test_transcript_envelope_stripped_e2e(fake_codex, target, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_MODE", "transcript")
    monkeypatch.setenv("FAKE_CODEX_TRANSCRIPT", TRANSCRIPT)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 0
    assert capsys.readouterr().out == "real answer line 1\nreal answer line 2\n"


# ── output shapes ───────────────────────────────────────────────────────────


def test_json_envelope_shape(fake_codex, target, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["--print", "--output-format", "json", "hi"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope == {"result": "alpha body", "total_cost_usd": 0.0, "model": "codex"}


def test_stdin_prompt_mode(fake_codex, target, tmp_path, monkeypatch, capsys):
    log = _argv_log(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("prompt over stdin"))
    assert ct.main(["--print", "--output-format", "text"]) == 0
    assert _read_argv(log)[-2:] == ["--", "prompt over stdin"]
    assert capsys.readouterr().out == "alpha body\n"


def test_failed_codex_exits_4(fake_codex, target, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_RC", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert ct.main(["p"]) == 4
    assert "codex exec failed with rc=1" in capsys.readouterr().err


# ── subprocess contract ──────────────────────────────────────────────────────


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
            "MO_CODEX_USD_PER_MTOK_IN": "1.0",
            "MO_CODEX_USD_PER_MTOK_CACHED": "0.1",
            "MO_CODEX_USD_PER_MTOK_OUT": "5.0",
            "FAKE_CODEX_RC": fake_rc,
        }
    )
    for key in (
        "MO_OAI_BASE_URL",
        "MO_OAI_ENV_KEY",
        "MO_OAI_MODEL",
        "MINI_ORK_TARGET_REPO",
        "MO_ALLOW_FRAMEWORK_CWD",
        "CODEX_SANDBOX",
        "MO_PRICING_YAML",
        "MINI_ORK_HOME",
        "FAKE_CODEX_MODE",
        "FAKE_CODEX_ARGV_LOG",
    ):
        env.pop(key, None)
    cmd = [
        sys.executable,
        "-m",
        "mini_ork.dispatch.codex_transport",
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
    fake_codex, target, tmp_path, monkeypatch, fmt
):
    proc, usage, turns, cost = _run_transport(fmt, tmp_path, target, monkeypatch)

    assert proc.returncode == 0
    assert _read_or_none(usage) == "1500\t250\n"
    assert json.loads(turns.read_text()) == {
        "turn_index": 0,
        "input_tokens": 1500,
        "output_tokens": 250,
        "cache_read_input_tokens": 500,
        "model": "codex",
        "session_id": "thr-unit",
    }
    # (1000*1.0 + 500*0.1 + 250*5.0)/1e6 = 0.0023
    assert _read_or_none(cost) == "0.002300\n"


def test_native_subprocess_maps_codex_failure_to_rc4(fake_codex, target, tmp_path, monkeypatch):
    proc, *_ = _run_transport("text", tmp_path, target, monkeypatch, fake_rc="1")
    assert proc.returncode == 4
    assert "codex exec failed with rc=1" in proc.stderr
