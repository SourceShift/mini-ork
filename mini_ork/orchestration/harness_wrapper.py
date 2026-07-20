"""Python port of lib/harness_wrapper.sh.

Wraps a full coding-agent harness (claude-code, codex-cli, gemini-cli) as a
workflow node. Reads the bash source of record (lib/harness_wrapper.sh) and
re-implements its dispatch contract in Python so test code and future Python
callers can invoke it as an in-process callable. The bash file remains the
system of record — DO NOT edit lib/harness_wrapper.sh; the parity test in
tests/unit/test_harness_wrapper_py.py fails if the bash source drifts from
what the port expects.

Porting map (bash function → Python symbol):

  _mo_harness_log                  → _log(level, msg)
  _mo_harness_emit_verdict         → _emit_verdict(workspace, harness, status,
                                          exit_code, diff_lines, diff_path, notes)
  _mo_harness_git_init_if_needed   → _git_init_if_needed(workspace)
  _mo_harness_capture_diff         → _capture_diff(workspace, diff_path)
  _mo_harness_dispatch_claude_code → _dispatch_claude_code(workspace, kickoff,
                                          timeout_s)
  _mo_harness_dispatch_codex_cli   → _dispatch_codex_cli(workspace, kickoff,
                                          timeout_s)
  _mo_harness_dispatch_gemini_cli  → _dispatch_gemini_cli(workspace, kickoff,
                                          timeout_s)
  _mo_harness_run                  → _run(harness, kickoff, workspace)
  mo_harness_wrap                  → mo_harness_wrap(harness, kickoff_path)

Bash line refs: harness_wrapper.sh:50-54 (_mo_harness_log), :56-75
(_mo_harness_emit_verdict), :77-98 (_mo_harness_git_init_if_needed), :100-107
(_mo_harness_capture_diff), :109-141 (claude-code dispatch), :143-172
(codex-cli dispatch), :174-196 (gemini-cli dispatch), :198-275 (_mo_harness_run),
:277-293 (mo_harness_wrap).

Verdict JSON schema (bytes mirrored exactly — see _emit_verdict):

    {
      "harness":     "<harness>",
      "status":      "<status>",
      "exit_code":   <int>,
      "diff_lines":  <int>,
      "diff_path":   "<workspace>/harness.diff",
      "notes":       "<free text>"
    }

Status enum: dry_run | cli_absent | completed | no_changes | timeout |
harness_error.  rc=2 only on malformed args (missing harness, missing kickoff
file).  Otherwise returns 0 once the verdict file is written.

Env knobs (mirror bash):
  MO_HARNESS_TIMEOUT_S    Per-CLI wall-clock timeout. Default 900.
  MO_HARNESS_DRY_RUN      Set to 1 to skip the actual CLI dispatch.
  MO_HARNESS_PROMPT_FILE  Ignored here (bash reads it via mo_harness_wrap's
                          second arg); the Python port takes the kickoff path
                          as the function's second arg.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

HARNESSES = ("claude-code", "codex-cli", "gemini-cli")
BINARIES = {
    "claude-code": "claude",
    "codex-cli": "codex",
    "gemini-cli": "gemini",
}
DEFAULT_TIMEOUT_S = 900


def _log(level: str, msg: str) -> None:
    """Mirror bash _mo_harness_log (harness_wrapper.sh:50-54)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(
        f'{{"level":"{level}","subsystem":"harness","ts":"{ts}","msg":"{msg}"}}\n'
    )


def _emit_verdict(
    workspace: str,
    harness: str,
    status: str,
    exit_code: int,
    diff_lines: int,
    diff_path: str,
    notes: str,
) -> None:
    """Mirror bash _mo_harness_emit_verdict (harness_wrapper.sh:56-75).

    Writes JSON with the SAME key insertion order as the bash heredoc so the
    bytes match field-for-field.  `json.dump(..., indent=2) + '\\n'` produces
    output identical to the bash Python heredoc across Python 3.7+.
    """
    verdict = {
        "harness": harness,
        "status": status,
        "exit_code": exit_code,
        "diff_lines": diff_lines,
        "diff_path": diff_path,
        "notes": notes,
    }
    out_path = os.path.join(workspace, "harness-verdict.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(verdict, fh, indent=2)
        fh.write("\n")


def _git_init_if_needed(workspace: str) -> None:
    """Mirror bash _mo_harness_git_init_if_needed (harness_wrapper.sh:77-98).

    Suppresses all git errors (bash uses `2>/dev/null || return 1`); the
    caller logs a warning on failure but does not abort.
    """
    probe = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if probe.returncode != 0:
        subprocess.run(
            ["git", "-C", workspace, "init", "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        subprocess.run(
            [
                "git", "-C", workspace,
                "-c", "user.email=harness@mini-ork.local",
                "-c", "user.name=mini-ork harness",
                "commit", "--allow-empty", "-m", "harness baseline", "--quiet",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        subprocess.run(
            ["git", "-C", workspace, "add", "-A"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        subprocess.run(
            [
                "git", "-C", workspace,
                "-c", "user.email=harness@mini-ork.local",
                "-c", "user.name=mini-ork harness",
                "commit", "-m", "harness baseline", "--quiet", "--allow-empty",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )


def _capture_diff(workspace: str, diff_path: str) -> None:
    """Mirror bash _mo_harness_capture_diff (harness_wrapper.sh:100-107)."""
    subprocess.run(
        ["git", "-C", workspace, "add", "-A"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    with open(diff_path, "w", encoding="utf-8", errors="replace") as fh:
        subprocess.run(
            ["git", "-C", workspace, "diff", "--cached", "HEAD"],
            stdout=fh, stderr=subprocess.DEVNULL, check=False,
        )


def _count_diff_lines(diff_path: str) -> int:
    """Mirror bash's `wc -l < $diff_path | tr -d '[:space:]'`
    (harness_wrapper.sh:259-260).  Falls back to 0 when the file is missing
    or wc fails — matches bash's `[ -z "$_diff_lines" ] && _diff_lines=0`.
    """
    if not os.path.isfile(diff_path):
        return 0
    try:
        with open(diff_path, "rb") as fh:
            r = subprocess.run(
                ["wc", "-l"], stdin=fh, capture_output=True, check=False,
            )
    except OSError:
        return 0
    if r.returncode != 0:
        return 0
    out = r.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return 0
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


def _run_cli(
    workspace: str, kickoff: str, timeout_s: int, argv: list[str], use_stdin: bool,
) -> int:
    """Subprocess dispatch mirroring the bash subshell in each
    _mo_harness_dispatch_* function.

    Returns the CLI's exit code, or 124 if subprocess.TimeoutExpired fires
    (matches bash `timeout 900s ...` rc=124 on SIGTERM after the wall-clock
    cap).  Mirrors bash's `command -v timeout` fallback: when the system
    `timeout` binary is absent, runs without a wall-clock cap (timeout=None).
    """
    stdout_path = os.path.join(workspace, "harness-stdout.txt")
    stderr_path = os.path.join(workspace, "harness-stderr.txt")
    stdin_fh = open(kickoff, "r", encoding="utf-8") if use_stdin else None
    use_timeout_bin = shutil.which("timeout") is not None
    argv_full = (
        ["timeout", f"{timeout_s}s", *argv] if use_timeout_bin else argv
    )
    py_timeout = timeout_s if use_timeout_bin else None
    try:
        try:
            r = subprocess.run(
                argv_full,
                cwd=workspace,
                stdin=stdin_fh,
                stdout=open(stdout_path, "w", encoding="utf-8"),
                stderr=open(stderr_path, "w", encoding="utf-8"),
                timeout=py_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124
    finally:
        if stdin_fh is not None:
            stdin_fh.close()
    return r.returncode


def _dispatch_claude_code(workspace: str, kickoff: str, timeout_s: int) -> int:
    """Mirror bash _mo_harness_dispatch_claude_code (harness_wrapper.sh:109-141)."""
    argv = [
        "claude",
        "--print",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "Read,Write,Edit,Bash",
    ]
    return _run_cli(workspace, kickoff, timeout_s, argv, use_stdin=True)


def _dispatch_codex_cli(workspace: str, kickoff: str, timeout_s: int) -> int:
    """Mirror bash _mo_harness_dispatch_codex_cli (harness_wrapper.sh:143-172)."""
    with open(kickoff, "r", encoding="utf-8") as fh:
        prompt = fh.read()
    argv = [
        "codex", "exec",
        "--skip-git-repo-check",
        "--sandbox", "workspace-write",
        prompt,
    ]
    return _run_cli(workspace, kickoff, timeout_s, argv, use_stdin=False)


def _dispatch_gemini_cli(workspace: str, kickoff: str, timeout_s: int) -> int:
    """Mirror bash _mo_harness_dispatch_gemini_cli (harness_wrapper.sh:174-196)."""
    with open(kickoff, "r", encoding="utf-8") as fh:
        prompt = fh.read()
    argv = ["gemini", "-p", prompt]
    return _run_cli(workspace, kickoff, timeout_s, argv, use_stdin=False)


def _run(harness: str, kickoff: str, workspace: str) -> int:
    """Mirror bash _mo_harness_run (harness_wrapper.sh:198-275)."""
    try:
        timeout_s = int(os.environ.get("MO_HARNESS_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        timeout_s = DEFAULT_TIMEOUT_S
    diff_path = os.path.join(workspace, "harness.diff")
    # Truncate the diff file to zero bytes (matches bash `: > "$_diff_path"`).
    open(diff_path, "w").close()

    # Harness validation gate runs FIRST (before any side effect).
    if harness not in HARNESSES:
        _log(
            "error",
            f"unknown harness: {harness} (supported: {', '.join(HARNESSES)})",
        )
        return 2

    # Git init is best-effort; failure is logged but does not abort.
    _git_init_if_needed(workspace)

    # Dry-run path — synthetic verdict.
    if os.environ.get("MO_HARNESS_DRY_RUN", "") == "1":
        _log("info", "MO_HARNESS_DRY_RUN=1 — skipping real CLI dispatch")
        _emit_verdict(workspace, harness, "dry_run", 0, 0, diff_path, "dry-run mode")
        return 0

    # Per-CLI availability check.
    binary = BINARIES[harness]
    if shutil.which(binary) is None:
        _log("warn", f"{binary} CLI absent; harness wrapper emitting cli_absent verdict")
        _emit_verdict(
            workspace, harness, "cli_absent", 127, 0, diff_path, f"no {binary} on PATH",
        )
        return 0

    _log("info", f"dispatching {harness} in {workspace} (kickoff={kickoff})")
    if harness == "claude-code":
        rc = _dispatch_claude_code(workspace, kickoff, timeout_s)
    elif harness == "codex-cli":
        rc = _dispatch_codex_cli(workspace, kickoff, timeout_s)
    else:
        rc = _dispatch_gemini_cli(workspace, kickoff, timeout_s)

    _capture_diff(workspace, diff_path)
    diff_lines = _count_diff_lines(diff_path)

    if rc == 0 and diff_lines > 0:
        status = "completed"
    elif rc == 0 and diff_lines == 0:
        status = "no_changes"
    elif rc == 124:
        status = "timeout"
    else:
        status = "harness_error"

    _emit_verdict(
        workspace, harness, status, rc, diff_lines, diff_path,
        f"rc={rc} lines={diff_lines}",
    )
    return 0


def mo_harness_wrap(harness: str, kickoff_path: str) -> int:
    """Mirror bash mo_harness_wrap (harness_wrapper.sh:277-293).

    Returns 2 on malformed args (empty harness / missing kickoff file);
    otherwise returns 0 once the verdict file is written.
    """
    if not harness or not kickoff_path:
        _log("error", "mo_harness_wrap <harness> <kickoff>")
        return 2
    if not os.path.isfile(kickoff_path):
        _log("error", f"kickoff not found: {kickoff_path}")
        return 2

    run_dir = os.environ.get("MINI_ORK_RUN_DIR")
    workspace = run_dir if run_dir else os.path.join(os.getcwd(), ".mini-ork", "harness-work")
    os.makedirs(workspace, exist_ok=True)
    return _run(harness, kickoff_path, workspace)
