"""Native Python port of ``lib/providers/cl_codex.sh`` (bash-removal WS6).

Drop-in replacement for the executable codex wrapper, invoked exactly like it::

    python3 -m mini_ork.dispatch.codex_transport --print --output-format text "$prompt"

The prompt is also accepted on stdin (no positional arg + non-tty stdin), which
is how ``mini_ork.dispatch.core.dispatch`` drives it (E2BIG-proof end to end).

Contract ported faithfully from the bash wrapper:

  - arg dialect: ``--print`` / ``--permission-mode`` / ``--max-turns`` /
    ``--exclude-dynamic-system-prompt-sections`` accepted + ignored (claude
    compat); ``--output-format text|json``; any other ``-*`` flag ignored;
    first positional is the prompt. No prompt → stderr + rc 2.
  - ``codex`` CLI missing from PATH → stderr + rc 3.
  - BYO endpoint: ``MO_OAI_BASE_URL`` + ``MO_OAI_ENV_KEY`` set → the named key
    var must be non-empty (else stderr + rc 5); passes
    ``-c model_providers.mini_ork={...wire_api="chat"}`` +
    ``-c model_provider=mini_ork``; ``MO_OAI_MODEL`` → ``-m``.
  - cwd: ``MO_TARGET_CWD`` → ``MINI_ORK_TARGET_REPO`` → process cwd; the
    framework-tree guard (``bin/mini-ork`` present, or a path matching the
    bash ``*/.mini-ork`` / ``*/mini-ork`` patterns) refuses with stderr + rc 2
    unless ``MO_ALLOW_FRAMEWORK_CWD=1``; ``-C <dir>`` only when the dir exists.
  - env hardening: ``GIT_TERMINAL_PROMPT=0``, ``GIT_ASKPASS`` /
    ``SSH_ASKPASS=/bin/false``, batch-mode ``GIT_SSH_COMMAND`` (setdefault
    semantics only).
  - stream sidecar: ``${MO_USAGE_FILE%.tokens}.stream.jsonl`` when
    ``MO_USAGE_FILE`` is set, else a mktemp file; the two launch lines are
    written first; ``MO_CODEX_STREAM_FILE`` is exported.
  - invoke: ``codex exec --skip-git-repo-check --sandbox ${CODEX_SANDBOX:-
    workspace-write} --json --output-last-message <tmp> [-C cwd] [BYO flags]
    -- "$PROMPT"`` with stdin from /dev/null and stdout+stderr appended to the
    stream file. rc != 0 → stderr lines + rc 4.
  - harvest: thread.started → thread_id; turn.completed → usage sums + turns;
    writes ``MO_USAGE_FILE`` TSV, ``MO_TURNS_FILE`` jsonl, ``MO_COST_FILE``
    cost at rates: ``MO_CODEX_USD_PER_MTOK_{IN,CACHED,OUT}`` env override →
    pricing.yaml lookup (openai gpt-5 input/cache_read/output, via
    :mod:`mini_ork.dispatch.pricing_strategy`) → defaults 1.25/0.125/10.0.
  - body: ``--output-last-message`` content when non-empty, else reconstructed
    from ``item.completed`` ``agent_message`` events joined with ``\\n\\n``;
    the transcript envelope is stripped (text after the final bare ``codex``
    line; status-line regexes dropped). Empty clean → raw fallback.
  - output: ``--output-format json`` → claude-shaped envelope
    ``{"result", "total_cost_usd": 0.0, "model": "codex"}``; else clean text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence

import yaml

from mini_ork.dispatch.pricing_strategy import lookup as pricing_lookup

# Codex list-price defaults (USD per million tokens) — the last-resort rates
# that shipped pre-pricing.yaml, identical to cl_codex.sh.
_DEFAULT_USD_PER_MTOK_IN = 1.25
_DEFAULT_USD_PER_MTOK_CACHED = 0.125
_DEFAULT_USD_PER_MTOK_OUT = 10.0

# Transcript-envelope status lines, dropped verbatim from the bash port.
_DROP_RES = tuple(
    re.compile(p)
    for p in (
        r"^\[20[0-9]{2}-[0-9]{2}-[0-9]{2}T",
        r"^tokens used:",
        r"^User instructions:",
        r"^OpenAI Codex",
        r"^Reading additional input from stdin",
        r"^[-]{8,}$",
        r"^(workdir|model|provider|approval|sandbox|reasoning|session id):",
        r"^hook: ",
    )
)


def _parse_args(argv: Sequence[str]) -> tuple[str, str]:
    """Dispatcher arg dialect → (format, prompt). Mirrors the bash case loop:
    compat flags consumed (+ their values where applicable), unknown ``-*``
    flags ignored, first bare positional is the prompt."""
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
    # bash: _cg="$(cd "$dir" 2>/dev/null && pwd -P || echo "$dir")"
    cg = os.path.realpath(target_cwd) if os.path.isdir(target_cwd) else target_cwd
    if not _is_framework_tree(cg):
        return None
    sys.stderr.write(
        f"[cl_codex] cwd guard FAILED: '{cg}' looks like a mini-ork framework "
        "tree — refusing codex dispatch (it would corrupt that repo instead of "
        "your target project). Set MO_TARGET_CWD to your TARGET repo; "
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
    """Yield parsed JSONL events from the codex stream; non-JSON / malformed
    lines are skipped, never fatal (same as the bash heredoc)."""
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


def _pricing_rate(provider: str, model: str, kind: str, env: Mapping[str, str]) -> float | None:
    """pricing.yaml lookup — the I/O layer of bash ``pricing_lookup`` feeding
    the pure :func:`mini_ork.dispatch.pricing_strategy.lookup` core. Any miss
    (file absent, unparsable, triplet absent, rate "0") returns None so the
    caller falls back to the hardcoded default."""
    path = env.get("MO_PRICING_YAML") or os.path.join(
        env.get("MINI_ORK_HOME") or ".mini-ork", "config", "pricing.yaml"
    )
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception:  # missing file / parse error — bash warns + prints "0"
        return None
    value = pricing_lookup(data, provider, model, kind)
    if not value or value == "0":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _rate(model: str, kind: str, env_name: str, default: float, env: Mapping[str, str]) -> float:
    """Rate precedence (2026-06-15 fix, ported): 1. env-var override wins
    (operator-set; the telemetry gate tests rely on this), 2. pricing.yaml,
    3. hardcoded default."""
    env_val = env.get(env_name)
    if env_val:
        try:
            return float(env_val)
        except (ValueError, TypeError):
            pass
    configured = _pricing_rate("openai", model, kind, env)
    if configured is not None:
        return configured
    return float(default)


def harvest(
    stream_text: str,
    usage_path: str,
    turns_path: str,
    cost_path: str,
    env: Mapping[str, str],
) -> None:
    """Parse the JSONL event stream into the dispatcher's sidecar files.

    ``usage_path`` gets the TSV ``in<TAB>out`` envelope totals; ``turns_path``
    gets stream-json-shaped per-turn lines; ``cost_path`` gets the estimated
    cost at 6 decimals. ``usage.input_tokens`` INCLUDES cached tokens (billed
    at the discounted rate), so they are subtracted before the full input
    rate — same as cl_codex.sh. Missing/empty paths skip that sidecar."""
    in_tok = out_tok = cached_tok = 0
    turns: list[dict] = []
    thread_id = None
    for ev in _iter_events(stream_text):
        if ev.get("type") == "thread.started":
            thread_id = ev.get("thread_id")
        if ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            t_in = int(u.get("input_tokens") or 0)
            t_out = int(u.get("output_tokens") or 0)
            t_cached = int(u.get("cached_input_tokens") or 0)
            in_tok += t_in
            out_tok += t_out
            cached_tok += t_cached
            turns.append(
                {
                    "turn_index": len(turns),
                    "input_tokens": t_in,
                    "output_tokens": t_out,
                    "cache_read_input_tokens": t_cached,
                    "model": "codex",
                    "session_id": thread_id,
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
        p_in = _rate("gpt-5", "input", "MO_CODEX_USD_PER_MTOK_IN",
                     _DEFAULT_USD_PER_MTOK_IN, env)
        p_cached = _rate("gpt-5", "cache_read", "MO_CODEX_USD_PER_MTOK_CACHED",
                         _DEFAULT_USD_PER_MTOK_CACHED, env)
        p_out = _rate("gpt-5", "output", "MO_CODEX_USD_PER_MTOK_OUT",
                      _DEFAULT_USD_PER_MTOK_OUT, env)
        fresh_in = max(in_tok - cached_tok, 0)
        cost = (fresh_in * p_in + cached_tok * p_cached + out_tok * p_out) / 1e6
        with open(cost_path, "w", encoding="utf-8") as f:
            f.write(f"{cost:.6f}\n")


def reconstruct_body(stream_text: str) -> str:
    """Rebuild the assistant body from ``item.completed`` ``agent_message``
    events (used when --output-last-message came back empty)."""
    msgs: list[str] = []
    for ev in _iter_events(stream_text):
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            msgs.append(item.get("text") or "")
    return "\n\n".join(m for m in msgs if m)


def strip_envelope(text: str) -> str:
    """Strip codex's transcript envelope so downstream parsers see only the
    assistant body: keep text after the final bare ``codex`` marker line when
    present, then drop status lines (timestamps, "tokens used:", etc.)."""
    lines = text.splitlines()
    last_codex = -1
    for i, line in enumerate(lines):
        if line.strip() == "codex":
            last_codex = i
    if last_codex >= 0:
        lines = lines[last_codex + 1:]
    kept = [line for line in lines if not any(rx.search(line) for rx in _DROP_RES)]
    return "\n".join(kept).strip()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fmt, prompt = _parse_args(args)

    # Stdin prompt mode: no positional prompt + piped stdin (not a tty). This
    # is how mini_ork.dispatch drives the transport — the prompt never touches
    # argv, so the whole chain is E2BIG-proof.
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    if not prompt:
        sys.stderr.write("[cl_codex] no prompt provided (positional arg or stdin)\n")
        return 2

    codex = shutil.which("codex")
    if codex is None:
        sys.stderr.write(
            "[cl_codex] codex CLI not found on PATH — install via "
            "https://github.com/openai/codex\n"
        )
        return 3

    env = os.environ

    # BYO OpenAI-compatible endpoint (providers.yaml registry contract). The
    # key VALUE never appears on argv — codex reads it via
    # model_providers.<id>.env_key from its own process environment.
    byo_flags: list[str] = []
    base_url = env.get("MO_OAI_BASE_URL", "")
    env_key = env.get("MO_OAI_ENV_KEY", "")
    if base_url and env_key:
        if not env.get(env_key):
            sys.stderr.write(
                f"[cl_codex] ${env_key} is empty — set it in secrets.local.sh "
                "or the environment\n"
            )
            return 5
        byo_flags += [
            "-c",
            'model_providers.mini_ork={ name = "mini-ork BYO", '
            f'base_url = "{base_url}", env_key = "{env_key}", wire_api = "chat" }}',
            "-c",
            "model_provider=mini_ork",
        ]
    if env.get("MO_OAI_MODEL"):
        byo_flags += ["-m", env["MO_OAI_MODEL"]]

    sandbox = env.get("CODEX_SANDBOX") or "workspace-write"
    fd, last_message = tempfile.mkstemp(prefix="mini-ork-codex-last.")
    os.close(fd)

    # Pin codex's working root explicitly (-C) so it cannot drift to an
    # ambient cwd. Resolution: MO_TARGET_CWD → MINI_ORK_TARGET_REPO → the
    # process cwd (what bash's ${PWD:-} resolves to at wrapper start).
    target_cwd = env.get("MO_TARGET_CWD") or env.get("MINI_ORK_TARGET_REPO") or os.getcwd()

    # cwd guard (cross-repo corruption prevention): a codex turn runs
    # `git reset --hard refs/codex/curated-sync` inside its cwd — refuse a cwd
    # that resolves to a mini-ork FRAMEWORK tree. Fail fast rather than
    # corrupt; MO_ALLOW_FRAMEWORK_CWD=1 opts in for a genuine self-edit.
    if _resolve_cwd_guard(target_cwd, env) is not None:
        _unlink(last_message)
        return 2

    cd_flags: list[str] = []
    if target_cwd and os.path.isdir(target_cwd):
        cd_flags = ["-C", target_cwd]

    # Codex may `git ls-remote` before a turn; keep git fail-fast and
    # non-interactive (setdefault only — operator pins win).
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "/bin/false")
    env.setdefault("SSH_ASKPASS", "/bin/false")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0")

    # Real-time progress sidecar: tee codex's JSONL stream next to the usage
    # sidecar so observers can distinguish a live run from a deadlock.
    usage_file = env.get("MO_USAGE_FILE", "")
    if usage_file:
        stem = usage_file[:-7] if usage_file.endswith(".tokens") else usage_file
        stream_file = f"{stem}.stream.jsonl"
    else:
        fd, stream_file = tempfile.mkstemp(prefix="mini-ork-codex-stream.")
        os.close(fd)
    env["MO_CODEX_STREAM_FILE"] = stream_file

    try:
        with open(stream_file, "a", encoding="utf-8") as sf:
            sf.write(f"[cl_codex] launching codex exec cwd={target_cwd} sandbox={sandbox}\n")
            sf.write(f"[cl_codex] prompt_bytes={len(prompt.encode('utf-8'))}\n")
    except OSError:
        pass

    cmd = [
        codex,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--json",
        "--output-last-message",
        last_message,
        *cd_flags,
        *byo_flags,
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
    raw_out = stream_text.rstrip("\n")  # bash RAW_OUT="$(cat ...)" strips

    if rc != 0:
        sys.stderr.write(f"[cl_codex] codex exec failed with rc={rc} — see stderr for cause\n")
        sys.stderr.write(raw_out + "\n")
        _unlink(last_message)
        return 4

    # Harvest usage/turns/cost sidecars BEFORE raw_out becomes the body.
    # Best-effort (bash `|| true`): a harvest error must never break dispatch.
    if env.get("MO_USAGE_FILE") or env.get("MO_TURNS_FILE") or env.get("MO_COST_FILE"):
        try:
            harvest(
                stream_text,
                env.get("MO_USAGE_FILE", ""),
                env.get("MO_TURNS_FILE", ""),
                env.get("MO_COST_FILE", ""),
                env,
            )
        except Exception:
            pass

    last_content = _read_file(last_message)
    if last_content:
        raw_out = last_content.rstrip("\n")
    else:
        reconstructed = reconstruct_body(stream_text)
        if reconstructed:
            raw_out = reconstructed
    _unlink(last_message)

    clean = strip_envelope(raw_out)
    if not clean:
        clean = raw_out

    if fmt == "json":
        # Minimal claude-shaped envelope for the downstream jq parser. Codex
        # exposes no per-call cost on this surface, so total_cost_usd is 0.
        sys.stdout.write(
            json.dumps({"result": clean, "total_cost_usd": 0.0, "model": "codex"}) + "\n"
        )
    else:
        sys.stdout.write(clean + "\n")
    return 0


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
