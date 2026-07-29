"""Golden-contract tests for the native single-prompt invocation utility.

The provider is the only injected boundary.  No test creates or sources
``lib/llm-dispatch.sh``; that absence is part of the migration proof.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import invoke_prompt
from mini_ork.stores.migrate import init_db

BIN = REPO / "bin" / "mini-ork-invoke-prompt"


class StubProvider:
    def __init__(self, output: str = "OK", rc: int = 0, error: str = "") -> None:
        self.output = output
        self.rc = rc
        self.error = error
        self.calls: list[tuple[str, str, int, int]] = []

    def __call__(
        self, model: str, prompt: str, out_file: str, timeout_s: int, max_turns: int,
    ) -> int:
        self.calls.append((model, prompt, timeout_s, max_turns))
        Path(out_file).write_text(self.output)
        if self.error:
            Path(out_file + ".err.log").write_text(self.error)
        return self.rc


def _root(tmp_path: Path, *, lane: str = "kimi") -> Path:
    root = tmp_path / "engine"
    (root / "config").mkdir(parents=True)
    (root / "config" / "agents.yaml").write_text(
        f"lanes:\n  implementer: {lane}\n  reviewer: glm\n"
    )
    return root


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "MINI_ORK_HOME": str(tmp_path / "home"),
        "MO_DISABLE_CN": "1",
        "MO_DISPATCH_MAX_ATTEMPTS": "1",
    }
    env.update(extra)
    return env


def test_native_dispatch_succeeds_without_bash_library(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello {{MINI_ORK_SUBJECT}}")
    provider = StubProvider("native response")

    rc, out = invoke_prompt.invoke(
        prompt_file=prompt,
        mini_ork_root=root,
        env=_env(tmp_path, MINI_ORK_SUBJECT="world"),
        dispatch_fn=provider,
    )

    assert not (root / "lib" / "llm-dispatch.sh").exists()
    assert (rc, out) == (0, "native response\n")
    assert provider.calls == [("kimi", "hello world", 1500, 60)]


def test_native_wrapper_receives_exact_contract(
    tmp_path: Path, monkeypatch,
) -> None:
    from mini_ork.dispatch import llm_dispatch as native_dispatch

    root = _root(tmp_path)
    marker = StubProvider()

    def capture(argv, *, root: str, dispatch_fn) -> int:
        assert argv == [
            "--task-class", "code_fix", "--node-type", "reviewer",
            "--prompt-text", "inspect patch",
        ]
        assert root == str(root_path)
        assert dispatch_fn is marker
        print("provider stdout", end="")
        print(" then stderr", end="", file=sys.stderr)
        return 0

    root_path = root
    monkeypatch.setattr(native_dispatch, "llm_dispatch", capture)
    rc, merged = invoke_prompt._llm_dispatch(
        root, "code_fix", "reviewer", "inspect patch", _env(tmp_path), marker,
    )
    assert (rc, merged) == (0, "provider stdout then stderr")


def test_overrides_and_multiline_environment_reach_native_dispatch(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("before\n{{MINI_ORK_MULTI}}\nafter")
    provider = StubProvider("done\n\n")
    previous = os.environ.get("MINI_ORK_MULTI")

    rc, out = invoke_prompt.invoke(
        prompt_file=prompt,
        node_type="reviewer",
        task_class="code_fix",
        mini_ork_root=root,
        env=_env(tmp_path, MINI_ORK_MULTI="line1\nline2"),
        dispatch_fn=provider,
    )

    assert (rc, out) == (0, "done\n")
    assert provider.calls == [("glm", "before\nline1\nline2\nafter", 1500, 60)]
    assert os.environ.get("MINI_ORK_MULTI") == previous


def test_missing_prompt_is_bad_args_and_never_dispatches(tmp_path: Path) -> None:
    provider = StubProvider()
    rc, out = invoke_prompt.invoke(
        prompt_file=tmp_path / "missing.md",
        mini_ork_root=_root(tmp_path),
        env=_env(tmp_path),
        dispatch_fn=provider,
    )
    assert (rc, out) == (2, "")
    assert provider.calls == []


def test_native_failure_keeps_merged_dispatch_diagnostics(
    tmp_path: Path, capsys,
) -> None:
    root = _root(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("fail please")
    provider = StubProvider(rc=7, error="provider unavailable")

    rc, out = invoke_prompt.invoke(
        prompt_file=prompt,
        mini_ork_root=root,
        env=_env(tmp_path),
        dispatch_fn=provider,
    )

    assert rc == 1
    assert "[llm_dispatch FAIL model=kimi rc=7]" in out
    assert "LLM dispatch failed for implementer" in capsys.readouterr().err


def test_context_role_pack_is_appended_after_substitution(
    tmp_path: Path, monkeypatch,
) -> None:
    root = _root(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review {{MINI_ORK_TARGET}}")
    provider = StubProvider()

    def role_pack(node_type: str, brief: Path, _: str) -> str:
        assert node_type == "reviewer"
        assert Path(brief).read_text() == "review patch.py"
        assert brief.parent != root / "lib"
        return "ROLE PACK"

    monkeypatch.setattr(invoke_prompt._crp, "role_pack_md", role_pack)
    rc, _ = invoke_prompt.invoke(
        prompt_file=prompt,
        node_type="reviewer",
        mini_ork_root=root,
        env=_env(
            tmp_path,
            MINI_ORK_TARGET="patch.py",
            MO_DISABLE_CN="0",
            MO_USE_ROLE_PACKS="1",
        ),
        dispatch_fn=provider,
    )

    assert rc == 0
    assert provider.calls[0][1] == "review patch.py\n\nROLE PACK\n"
    assert not (root / "lib").exists()


def test_success_trace_row_is_written(tmp_path: Path) -> None:
    root = _root(tmp_path)
    # No lib/trace_store.sh anywhere: trace writes are native now
    # (mini_ork.trace_store), so the bash lib is not even consulted.
    prompt = tmp_path / "prompt.md"
    prompt.write_text("trace me")
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "state.db"
    rc_init, out_init, err_init = init_db(db=str(db), root=str(REPO))
    assert rc_init == 0, (
        f"init_db failed rc={rc_init}\nstdout={out_init}\nstderr={err_init}"
    )

    rc, _ = invoke_prompt.invoke(
        prompt_file=prompt,
        task_class="code_fix",
        mini_ork_root=root,
        state_db=db,
        env=_env(tmp_path, MO_NODE_PROMPT_SHA="abcdef0123456789"),
        dispatch_fn=StubProvider(),
    )

    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT task_class, status, prompt_version_hash FROM execution_traces"
        ).fetchone()
    assert rc == 0
    assert row == ("code_fix", "success", "abcdef0123456789")
    assert not (root / "lib" / "trace_store.sh").exists()


def test_invoke_prompt_has_no_bash_trace_store_dependency() -> None:
    """WS3 guard: the trace-write path must not shell out to bash anymore."""
    src = (REPO / "mini_ork" / "cli" / "invoke_prompt.py").read_text()
    assert "import subprocess" not in src
    assert "subprocess.run" not in src


def test_trace_write_is_best_effort_without_db(tmp_path: Path) -> None:
    """Best-effort contract: no MINI_ORK_DB must not raise."""
    invoke_prompt._trace_write('{"trace_id":"t","status":"success"}',
                               _env(tmp_path))


def test_public_launcher_is_python_and_preserves_bad_args_exit(tmp_path: Path) -> None:
    assert BIN.read_text().startswith("#!/usr/bin/env python3\n")
    result = subprocess.run(
        [str(BIN)],
        env={
            **os.environ,
            "MINI_ORK_ROOT": str(REPO),
            "MINI_ORK_PROMPT_FILE": str(tmp_path / "missing.md"),
            "MO_DISABLE_CN": "1",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "prompt not found:" in result.stderr
