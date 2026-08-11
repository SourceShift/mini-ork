"""Native Python port of a notional ``cl_opencode.sh`` wrapper (2026-08).

Drop-in replacement for an executable opencode wrapper, invoked exactly like
the codex transport::

    python3 -m mini_ork.dispatch.opencode_transport --print --output-format text "$prompt"

The prompt is also accepted on stdin (no positional arg + non-tty stdin), which
is how ``mini_ork.dispatch.core.dispatch`` drives it (E2BIG-proof end to end).

Contract ported faithfully from the codex transport (cl_opencode.sh has no
existence upstream — this is the *first* implementation, not a port):

  - arg dialect: ``--print`` / ``--permission-mode`` / ``--max-turns`` /
    ``--exclude-dynamic-system-prompt-sections`` accepted + ignored (claude
    compat); ``--output-format text|json``; any other ``-*`` flag ignored;
    first positional is the prompt. No prompt → stderr + rc 2.
  - ``opencode`` CLI missing from PATH → stderr + rc 3.
  - model pin: ``MO_OPENCODE_MODEL`` → ``-m <provider/model>``; absent → the
    model is omitted and opencode uses its configured default.
  - cwd: ``MO_TARGET_CWD`` → ``MINI_ORK_TARGET_REPO`` → process cwd; the
    framework-tree guard (``bin/mini-ork`` present, or a path matching the
    bash ``*/.mini-ork`` / ``*/mini-ork`` patterns) refuses with stderr + rc 2
    unless ``MO_ALLOW_FRAMEWORK_CWD=1``; ``--dir <dir>`` only when the dir exists.
  - env hardening: ``GIT_TERMINAL_PROMPT=0``, ``GIT_ASKPASS`` /
    ``SSH_ASKPASS=/bin/false``, batch-mode ``GIT_SSH_COMMAND`` (setdefault
    semantics only).
  - stream sidecar: ``${MO_USAGE_FILE%.tokens}.stream.jsonl`` when
    ``MO_USAGE_FILE`` is set, else a mktemp file; the two launch lines are
    written first; ``MO_OPENCODE_STREAM_FILE`` is exported.
  - invoke: ``opencode run --format json --auto --dir <cwd> [-m <provider/model>]
    -- "$PROMPT"`` with stdin from /dev/null and stdout+stderr appended to the
    stream file. rc != 0 → stderr lines + rc 4.
  - harvest: ``step_finish`` events accumulate ``part.tokens`` (input/output/
    cache_read) + ``part.cost``; writes ``MO_USAGE_FILE`` TSV,
    ``MO_TURNS_FILE`` jsonl, ``MO_COST_FILE`` cost. Absent fields → 0. Same
    6-decimal cost float as the codex transport.
  - body: concatenated ``text`` events' ``part.text`` joined with ``\\n\\n``;
    empty → the raw stream as the fallback.
  - output: ``--output-format json`` → claude-shaped envelope
    ``{"result", "total_cost_usd": 0.0, "model": "opencode"}``; else clean text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence


def _parse_args(argv: Sequence[str]) -> tuple[str, str]:
    """Dispatcher arg dialect → (format, prompt). Mirrors the codex transport's
    compat-flag consumption: unknown ``-*`` flags ignored, first bare positional
    is the prompt."""
    fmt = "text"
    prompt = ""
    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg == "--print":
            i += 1
        elif arg == "--output-format":
            if i + 1 < n:
                fmt = argv[i + 1]
                i += 2
            else:
                i += 1
        elif arg in ("--permission-mode", "--max-turns"):
            i += 2 if i + 1 < n else 1
        elif arg == "--exclude-dynamic-system-prompt-sections":
            i += 1
        elif arg.startswith("-"):
            i += 1
        else:
            if not prompt:
                prompt = arg
            i += 1
    return fmt, prompt


def _is_framework_tree(path: str) -> bool:
    """The bash guard: a mini-ork install root (bin/mini-ork present) or a path
    matching */.mini-ork, */.mini-ork/*, */mini-ork, */mini-ork/*."""
    if os.path.isfile(os.path.join(path, "bin", "mini-ork")):
        return True
    return (
        path.endswith("/.mini-ork")
        or "/.mini-ork/" in path
        or path.endswith("/mini-ork")
        or "/mini-ork/" in path
    )


def _resolve_cwd_guard(target_cwd: str, env: Mapping[str, str]) -> str | None:
    """Framework-tree cwd guard. Returns None when the cwd is safe; otherwise
    writes the refusal to stderr and the caller exits rc 2. A genuine framework
    self-edit opts in with MO_ALLOW_FRAMEWORK_CWD=1."""
    if env.get("MO_ALLOW_FRAMEWORK_CWD", "0") == "1" or not target_cwd:
        return None
    cg = os.path.realpath(target_cwd) if os.path.isdir(target_cwd) else target_cwd
    if not _is_framework_tree(cg):
        return None
    sys.stderr.write(
        f"[cl_opencode] cwd guard FAILED: '{cg}' looks like a mini-ork framework "
        "tree — refusing opencode dispatch (it would corrupt that repo instead "
        "of your target project). Set MO_TARGET_CWD to your TARGET repo; "
        "MO_ALLOW_FRAMEWORK_CWD=1 only for a genuine framework self-edit.\n"
    )
    return cg


def _read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _iter_events(text: str):
    """Yield parsed JSON events from the opencode stream; non-JSON / malformed
    lines are skipped, never fatal (same as the codex transport's heredoc)."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict):
            yield ev


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0
    return 0


def _coerce_float(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def harvest(
    stream_text: str,
    usage_path: str,
    turns_path: str,
    cost_path: str,
) -> None:
    """Parse opencode's JSON event stream into the dispatcher's sidecar files.

    Opencode emits one JSON object per line; ``step_finish`` is the only step
    that carries usage tokens (``part.tokens.{input,output}`` plus a nested
    ``part.tokens.cache.{read,write}``) and an optional cost (``part.cost``). Absent fields are coerced to 0 rather than
    failing the harvest — a model that doesn't surface tokens shouldn't break
    dispatch. ``usage_path``/``turns_path``/``cost_path`` left empty skip that
    sidecar (matches the codex transport's contract)."""
    in_tok = out_tok = cached_tok = 0
    cost_total = 0.0
    turns: list[dict] = []
    session_id = ""
    for ev in _iter_events(stream_text):
        ev_type = ev.get("type")
        if not session_id:
            session_id = str(ev.get("sessionID") or "")
        if ev_type == "step_finish":
            part = ev.get("part") or {}
            tokens = part.get("tokens") or {}
            t_in = _coerce_int(tokens.get("input"))
            t_out = _coerce_int(tokens.get("output"))
            # opencode emits `tokens.cache` as a nested {"write","read"} object;
            # cache-read is the billable-relevant half. Fall back to a scalar
            # `cache`/`cached` for forward-compat with a flatter shape.
            cache = tokens.get("cache")
            if isinstance(cache, Mapping):
                t_cached = _coerce_int(cache.get("read"))
            else:
                t_cached = _coerce_int(cache if cache is not None else tokens.get("cached"))
            in_tok += t_in
            out_tok += t_out
            cached_tok += t_cached
            cost_total += _coerce_float(part.get("cost"))
            turns.append(
                {
                    "turn_index": len(turns),
                    "input_tokens": t_in,
                    "output_tokens": t_out,
                    "cache_read_input_tokens": t_cached,
                    "model": "opencode",
                    "session_id": session_id,
                }
            )
    if usage_path and (in_tok or out_tok):
        with open(usage_path, "w", encoding="utf-8") as f:
            f.write(f"{in_tok}\t{out_tok}\n")
    if turns_path and turns:
        with open(turns_path, "w", encoding="utf-8") as f:
            for t in turns:
                f.write(json.dumps(t) + "\n")
    if cost_path and (in_tok or out_tok):
        with open(cost_path, "w", encoding="utf-8") as f:
            f.write(f"{cost_total:.6f}\n")


def reconstruct_body(stream_text: str) -> str:
    """Rebuild the assistant body from ``text`` events' ``part.text`` — opencode
    has no equivalent of codex's ``--output-last-message`` file, so the body
    always comes from the event stream."""
    msgs: list[str] = []
    for ev in _iter_events(stream_text):
        if ev.get("type") != "text":
            continue
        part = ev.get("part") or {}
        text = str(part.get("text") or "")
        if text:
            msgs.append(text)
    return "\n\n".join(msgs)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fmt, prompt = _parse_args(args)

    # Stdin prompt mode: no positional prompt + piped stdin (not a tty). This
    # is how mini_ork.dispatch drives the transport — the prompt never touches
    # argv, so the whole chain is E2BIG-proof.
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    if not prompt:
        sys.stderr.write("[cl_opencode] no prompt provided (positional arg or stdin)\n")
        return 2

    opencode = shutil.which("opencode")
    if opencode is None:
        sys.stderr.write(
            "[cl_opencode] opencode CLI not found on PATH — install via "
            "https://opencode.ai\n"
        )
        return 3

    env = os.environ

    # Model pin: MO_OPENCODE_MODEL → `-m <provider/model>`. Absent → omit the
    # flag and let opencode use its configured default (avoids "-m " becoming
    # an empty-value argument that the CLI rejects).
    model_flags: list[str] = []
    if env.get("MO_OPENCODE_MODEL"):
        model_flags = ["-m", env["MO_OPENCODE_MODEL"]]

    target_cwd = env.get("MO_TARGET_CWD") or env.get("MINI_ORK_TARGET_REPO") or os.getcwd()

    # cwd guard (cross-repo corruption prevention): refuse a cwd that resolves
    # to a mini-ork FRAMEWORK tree. Fail fast rather than corrupt;
    # MO_ALLOW_FRAMEWORK_CWD=1 opts in for a genuine self-edit.
    if _resolve_cwd_guard(target_cwd, env) is not None:
        return 2

    dir_flags: list[str] = []
    if target_cwd and os.path.isdir(target_cwd):
        dir_flags = ["--dir", target_cwd]

    # Opencode may `git ls-remote` before a turn; keep git fail-fast and
    # non-interactive (setdefault only — operator pins win).
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "/bin/false")
    env.setdefault("SSH_ASKPASS", "/bin/false")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0")

    # Real-time progress sidecar: tee opencode's JSON stream next to the usage
    # sidecar so observers can distinguish a live run from a deadlock.
    usage_file = env.get("MO_USAGE_FILE", "")
    if usage_file:
        stem = usage_file[:-7] if usage_file.endswith(".tokens") else usage_file
        stream_file = f"{stem}.stream.jsonl"
    else:
        fd, stream_file = tempfile.mkstemp(prefix="mini-ork-opencode-stream.")
        os.close(fd)
    env["MO_OPENCODE_STREAM_FILE"] = stream_file

    try:
        with open(stream_file, "a", encoding="utf-8") as sf:
            sf.write(f"[cl_opencode] launching opencode run cwd={target_cwd}\n")
            sf.write(f"[cl_opencode] prompt_bytes={len(prompt.encode('utf-8'))}\n")
    except OSError:
        pass

    cmd = [
        opencode,
        "run",
        "--format", "json",
        "--auto",
        *dir_flags,
        *model_flags,
        "--",
        prompt,
    ]
    with (
        open(os.devnull, "rb") as devnull,
        open(stream_file, "a", encoding="utf-8", errors="replace") as sf,
    ):
        rc = subprocess.run(
            cmd, stdin=devnull, stdout=sf, stderr=subprocess.STDOUT, check=False
        ).returncode
    stream_text = _read_file(stream_file)

    if rc != 0:
        sys.stderr.write(f"[cl_opencode] opencode run failed with rc={rc} — see stderr for cause\n")
        sys.stderr.write(stream_text + "\n")
        return 4

    # Harvest usage/turns/cost sidecars BEFORE the body becomes the output.
    # Best-effort (bash `|| true`): a harvest error must never break dispatch.
    if env.get("MO_USAGE_FILE") or env.get("MO_TURNS_FILE") or env.get("MO_COST_FILE"):
        try:
            harvest(
                stream_text,
                env.get("MO_USAGE_FILE", ""),
                env.get("MO_TURNS_FILE", ""),
                env.get("MO_COST_FILE", ""),
            )
        except Exception:
            pass

    body = reconstruct_body(stream_text)
    if not body:
        body = stream_text

    if fmt == "json":
        # Minimal claude-shaped envelope for the downstream jq parser. Opencode
        # exposes no per-call cost on this surface, so total_cost_usd is 0.
        sys.stdout.write(
            json.dumps({"result": body, "total_cost_usd": 0.0, "model": "opencode"}) + "\n"
        )
    else:
        sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
