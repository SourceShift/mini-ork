"""mini_ork/ported/mini_ork_invoke_prompt — Python port of bin/mini-ork-invoke-prompt.

Bash-side contract this port mirrors (verbatim from bin/mini-ork-invoke-prompt):
  - Inputs (env, NOT positional): MINI_ORK_PROMPT_FILE (required),
    MINI_ORK_NODE_TYPE (default "implementer"), MINI_ORK_TASK_CLASS (default
    "generic"), MINI_ORK_ROOT (default = repo root, two parents up from the
    script). MINI_ORK_RECIPE / MINI_ORK_RUN_ID are advisory (read by trace).
  - Placeholder substitution: {{[A-Z][A-Z0-9_]*}} -> os.environ[name]; missing
    var leaves the token verbatim (matches bash's `os.environ.get(name,
    match.group(0))` heredoc).
  - llm-dispatch + trace-store peers are NOT ported; we delegate to bash
    subprocesses for those two calls (strangler-fig: bash is single source of
    truth). context_role_packs IS ported and imported directly.
  - Exit codes: 0 success, 1 llm-failure, 2 bad-args (prompt file missing OR
    env var unset), 3 missing-lib (lib/llm-dispatch.sh not present).
  - Output: bash's `RESPONSE=$(... 2>&1)` strips trailing newlines, then
    `printf '%s\\n' "$RESPONSE"` adds one back. This port mirrors via
    `response.rstrip('\\n') + '\\n'` so multi-newline LLM outputs remain
    byte-equal across backends.
  - Stderr merging: bash uses `$(... 2>&1)`; subprocess calls use
    `stderr=subprocess.STDOUT` to capture the same merged stream.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
import time as _time
from pathlib import Path
from typing import Optional, Tuple

from mini_ork.ported import context_role_packs as _crp

_DEFAULT_NODE_TYPE = "implementer"
_DEFAULT_TASK_CLASS = "generic"
_PLACEHOLDER = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
_TRACE_ID_PREFIX = "tr-invoke-py"


def _resolve_root(mini_ork_root: Optional[str | Path]) -> Path:
    """Mirror bash: `MINI_ORK_ROOT=${MINI_ORK_ROOT:-$(cd $(dirname ${BASH_SOURCE[0]})/.. && pwd)}`.

    Bash default = 2 parents up from the script (bin/..). We mirror that as
    2 parents up from THIS module (mini_ork/ported/..).
    """
    if mini_ork_root is not None:
        return Path(mini_ork_root)
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[2]


def _resolve_node_type(node_type: Optional[str]) -> str:
    return node_type if node_type is not None else os.environ.get(
        "MINI_ORK_NODE_TYPE", _DEFAULT_NODE_TYPE
    )


def _resolve_task_class(task_class: Optional[str]) -> str:
    return task_class if task_class is not None else os.environ.get(
        "MINI_ORK_TASK_CLASS", _DEFAULT_TASK_CLASS
    )


def _substitute_placeholders(text: str) -> str:
    """Mirror bash heredoc: `os.environ.get(name, match.group(0))`."""
    def sub(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(0))
    return _PLACEHOLDER.sub(sub, text)


def _prompt_version_hash(prompt_text: str) -> str:
    """Mirror bash: `echo -n "$PROMPT_TEXT" | shasum | cut -c1-16` (sha1 first 16 hex)."""
    return hashlib.sha1(prompt_text.encode("utf-8")).hexdigest()[:16]


def _build_prompt_text(
    prompt_file: Path,
    node_type: str,
    mini_ork_root: Path,
) -> str:
    """Pure prompt build: read -> sub placeholders -> optional role-pack append."""
    raw = prompt_file.read_text()
    prompt_text = _substitute_placeholders(raw)
    use_role_packs = (
        os.environ.get("MO_USE_ROLE_PACKS", "1") == "1"
        and os.environ.get("MO_DISABLE_CN", "0") != "1"
    )
    if not use_role_packs:
        return prompt_text
    # bash: writes PROMPT_TEXT to a tmpfile, calls context_role_pack_md NODE_TYPE
    # <brief> "", appends `\n\n<pack>\n` if non-empty. Python mirrors with the
    # ported helper; brief is a temp file under mini_ork_root/lib (matches
    # bash's mktemp scope) and is always cleaned up.
    brief_tmp = mini_ork_root / "lib" / f".__brief_{os.getpid()}.tmp"
    try:
        brief_tmp.write_text(prompt_text)
        # role_pack_md returns "" when MO_DISABLE_CN=1 (defensive) or
        # cn_available=False (default). With MO_USE_ROLE_PACKS=1 in tests we
        # need MO_DISABLE_CN=0 to even reach this branch — and parity is
        # preserved because bash's context_role_pack_md also degrades to ""
        # under MO_DISABLE_CN=1 (the bash call path is gated on it).
        node_pack = _crp.role_pack_md(node_type, brief_tmp, "")
    finally:
        try:
            brief_tmp.unlink()
        except OSError:
            pass
    if node_pack:
        prompt_text = f"{prompt_text}\n\n{node_pack}\n"
    return prompt_text


def _trace_write(mini_ork_root: Path, payload: str, env: dict) -> None:
    """Delegate to bash trace_write (not yet ported). Best-effort, never raises."""
    lib = mini_ork_root / "lib" / "trace_store.sh"
    if not lib.is_file():
        return
    try:
        subprocess.run(
            ["bash", "-c", f'. "{lib}" && trace_write {shlex.quote(payload)}'],
            env=env, capture_output=True,
        )
    except Exception:
        # bash's `>/dev/null 2>&1 || true` is best-effort — mirror by swallowing.
        return


def _llm_dispatch(
    mini_ork_root: Path,
    task_class: str,
    node_type: str,
    prompt_text: str,
    env: dict,
) -> Tuple[int, str]:
    """Delegate to bash llm_dispatch (not yet ported). Returns (rc, merged_stdout).

    Mirrors bash's `RESPONSE=$(llm_dispatch ... 2>&1)` via stderr=STDOUT so the
    captured string is byte-equal to what bash command-substitution saw. When
    the lib is missing, mirrors bash's `_require_lib` stderr message + rc=3.
    """
    lib = mini_ork_root / "lib" / "llm-dispatch.sh"
    if not lib.is_file():
        print("lib/llm-dispatch.sh not present", file=sys.stderr)
        return 3, ""
    cmd = (
        f'. "{lib}" && llm_dispatch '
        f'--task-class {shlex.quote(task_class)} '
        f'--node-type {shlex.quote(node_type)} '
        f'--prompt-text {shlex.quote(prompt_text)}'
    )
    res = subprocess.run(
        ["bash", "-c", cmd],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return res.returncode, res.stdout


def invoke(
    prompt_file: Optional[str | Path] = None,
    node_type: Optional[str] = None,
    task_class: Optional[str] = None,
    mini_ork_root: Optional[str | Path] = None,
    state_db: Optional[str | Path] = None,
    env: Optional[dict] = None,
) -> Tuple[int, str]:
    """Mirror bin/mini-ork-invoke-prompt. Returns (rc, response_text).

    Args:
        prompt_file: path to the prompt .md (defaults to $MINI_ORK_PROMPT_FILE).
        node_type: defaults to $MINI_ORK_NODE_TYPE or "implementer".
        task_class: defaults to $MINI_ORK_TASK_CLASS or "generic".
        mini_ork_root: defaults to $MINI_ORK_ROOT or repo root (2 parents up).
        state_db: optional override for $MINI_ORK_DB (for trace routing).
        env: subprocess env; defaults to os.environ with overrides applied.

    Exit codes: 0 success, 1 llm-failure, 2 bad-args, 3 missing-lib.
    """
    root = _resolve_root(mini_ork_root)
    nt = _resolve_node_type(node_type)
    tc = _resolve_task_class(task_class)

    # Mirror bash's env propagation: pass through os.environ, allow override.
    sub_env = dict(os.environ)
    if env is not None:
        sub_env.update(env)
    if state_db is not None:
        sub_env["MINI_ORK_DB"] = str(state_db)
    sub_env["MINI_ORK_ROOT"] = str(root)
    sub_env["MINI_ORK_NODE_TYPE"] = nt
    sub_env["MINI_ORK_TASK_CLASS"] = tc

    # bash: PROMPT_FILE="${MINI_ORK_PROMPT_FILE:?MINI_ORK_PROMPT_FILE required}"
    pf = prompt_file if prompt_file is not None else sub_env.get("MINI_ORK_PROMPT_FILE")
    if not pf:
        print("MINI_ORK_PROMPT_FILE required", file=sys.stderr)
        return 2, ""
    prompt_path = Path(pf)
    if not prompt_path.is_file():
        print(f"prompt not found: {pf}", file=sys.stderr)
        return 2, ""

    # Pure prompt build.
    try:
        prompt_text = _build_prompt_text(prompt_path, nt, root)
    except FileNotFoundError:
        print(f"prompt not found: {pf}", file=sys.stderr)
        return 2, ""

    # Mirror bash's trace_id format: `tr-invoke-$(date +%s)-<pid>`.
    # Bash uses `$$` (the bash process PID); we use os.getpid() (the python
    # process PID). The trace_id itself is NOT asserted across backends —
    # t7 selects only deterministic columns from execution_traces.
    trace_id = f"{_TRACE_ID_PREFIX}-{int(_time.time())}-{os.getpid()}"

    # trace 'running' (best-effort, like bash `>/dev/null 2>&1 || true`).
    running_payload = (
        '{"trace_id":"' + trace_id + '",'
        '"task_class":"' + tc + '",'
        '"status":"running",'
        '"prompt_version_hash":"' + _prompt_version_hash(prompt_text) + '"}'
    )
    _trace_write(root, running_payload, sub_env)

    # Invoke llm_dispatch.
    rc, response = _llm_dispatch(root, tc, nt, prompt_text, sub_env)
    if rc == 3:
        # bash's `_require_lib llm-dispatch` exits 3 BEFORE the llm_dispatch
        # call. Propagate rc=3 here so parity matches when the lib is missing.
        return 3, response
    if rc != 0:
        print(f"[invoke-prompt] LLM dispatch failed for {nt}", file=sys.stderr)
        failure_payload = (
            '{"trace_id":"' + trace_id + '","status":"failure"}'
        )
        _trace_write(root, failure_payload, sub_env)
        return 1, response

    # Mirror bash: `printf '%s\n' "$RESPONSE"` = strip trailing newlines, add one.
    out = response.rstrip("\n") + "\n"

    success_payload = (
        '{"trace_id":"' + trace_id + '","status":"success"}'
    )
    _trace_write(root, success_payload, sub_env)
    return 0, out


def main() -> int:
    """`python -m mini_ork.ported.mini_ork_invoke_prompt` shim."""
    rc, out = invoke()
    sys.stdout.write(out)
    return rc


if __name__ == "__main__":
    sys.exit(main())