"""Lane-helpers — Python port of ``lib/lane-helpers.sh``.

Faithful Python port of the six bash functions in
``lib/lane-helpers.sh`` (lane predicate, budget flag, cache flag,
capability assertion, cache-stats aggregation, claude --print wrapper).
The bash source is the authoritative spec and stays byte-identical
(strangler-fig invariant); this module gives Python callers an
in-process target and gives ``tests/unit/test_lane_helpers_py.py`` a
stable surface to byte-diff against the LIVE bash subprocess (no mocks,
no hardcoded outputs).

Co-existence model (strangler-fig): ``lib/lane-helpers.sh`` is not
modified by this port. The Python port mirrors its CLI semantics
exactly. Parity is enforced by
``tests/unit/test_lane_helpers_py.py`` (>=6 live-bash-subprocess cases
that drive ``bash lib/lane-helpers.sh <op>`` / ``bash -c '. … && mo_<op>'``
on identical inputs and diff the resulting flags, stderr, exit codes,
and cache-stats.json byte-for-byte).

Pipeline map (bash function → Python):
  mo_lane_is_free(lane)              → lane_is_free(lane) -> bool
  (case glm|kimi|minimax)            (frozenset of free lanes)
  mo_emit_budget_flag <arr> …        → emit_budget_flag(lane, val) -> list[str]
  (local -n; out=(--max-budget-usd …)) (returns [] when free, [flag, val] when paid)
  mo_emit_cache_flags <arr>          → emit_cache_flags() -> list[str]
  (one-shot claude --help probe +     (module-level _CACHE_FLAG_SUPPORTED var;
   _MO_CACHE_FLAG_SUPPORTED env var)  one-shot probe via _PROBE_DONE flag)
  mo_assert_lane_capability <lane>   → assert_lane_capability(lane) -> None
  (heredoc python3; stderr <cap>)    (raises RuntimeError(<cap>); reuses
                                       mini_ork.dispatch.config_resolve
                                       for yaml path resolution; same
                                       external observable)
  mo_aggregate_cache_stats <dir>     → aggregate_cache_stats(iter_dir) -> dict
  (grep -oE + jq -n to write JSON)   (Python regex + json.dumps to
                                       cache-stats.json; same shape)
  mo_claude_print <prompt> [extra]   → claude_print(prompt, *extra) -> CP
  (claude --print --permission-mode  (subprocess.run of the same argv;
   bypassPermissions --output-format  returns CompletedProcess so caller
   … $@ $PROMPT)                      can inspect stdout/stderr/rc)

Public surface:
    lane_is_free(lane) -> bool
    emit_budget_flag(lane, val) -> list[str]
    emit_cache_flags() -> list[str]
    assert_lane_capability(lane) -> None
    aggregate_cache_stats(iter_dir: str) -> dict
    claude_print(prompt, *extra, max_turns=40, output_format="text") -> CompletedProcess
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import yaml

from mini_ork.dispatch import config_resolve

__all__ = [
    "lane_is_free",
    "emit_budget_flag",
    "emit_cache_flags",
    "assert_lane_capability",
    "aggregate_cache_stats",
    "claude_print",
]

_FREE_LANES = frozenset({"glm", "kimi", "minimax"})
_CACHE_FLAG = "--exclude-dynamic-system-prompt-sections"

# Module-level cache for the one-shot ``claude --help`` probe.
# Mirrors bash's `_MO_CACHE_FLAG_SUPPORTED` env var: probe once per
# process, then return the same result for the rest of the run. The
# test harness can ``monkeypatch.setattr`` this to force a re-probe.
_CACHED_SUPPORTED: bool | None = None
_PROBE_DONE: bool = False

# JSON output shape for ``mo_aggregate_cache_stats``. ``hit_rate`` and
# ``estimated_usd_saved`` are floats (4dp); everything else is integer.
_SAVED_USD_PER_TOKEN = 0.9 * 3 / 1_000_000  # 0.1× of $3/M input
# bash's `grep -oE '"<key>":[0-9]+' | awk -F: '{s+=$2} END{print s+0}'`
_CACHE_CREATION_RE = re.compile(r'"cache_creation_input_tokens":(\d+)')
_CACHE_READ_RE = re.compile(r'"cache_read_input_tokens":(\d+)')
_INPUT_TOKENS_RE = re.compile(r'"input_tokens":(\d+)')

# Full Claude Code flag set used by ``mo_claude_print`` — mirrors
# ``lib/lane-helpers.sh`` lines 242-249 exactly.
_CLAUDE_BASE = [
    "--print",
    "--permission-mode", "bypassPermissions",
    "--output-format", "{format}",
    "--max-turns", "{max_turns}",
]


def lane_is_free(lane: str) -> bool:
    """Mirror ``mo_lane_is_free`` — True for glm/kimi/minimax, False otherwise.

    Bash:

        case "$lane" in
            glm|kimi|minimax) return 0 ;;
            *) return 1 ;;
        esac
    """
    return lane in _FREE_LANES


def emit_budget_flag(lane: str, val: str) -> list[str]:
    """Mirror ``mo_emit_budget_flag`` — empty list when free, flag pair when paid.

    Bash:

        out=()
        if mo_lane_is_free "$lane"; then return 0; fi
        out=(--max-budget-usd "$val")

    Caller passes ``val`` as a string (matches bash's positional arg
    shape — the bash function takes a pre-resolved string, not a number).
    """
    if lane_is_free(lane):
        return []
    return ["--max-budget-usd", str(val)]


def emit_cache_flags() -> list[str]:
    """Mirror ``mo_emit_cache_flags`` — feature-detect the cache flag once.

    Bash probes ``claude --help`` for the
    ``--exclude-dynamic-system-prompt-sections`` flag, caches the result
    in the env var ``_MO_CACHE_FLAG_SUPPORTED`` for the rest of the
    process, and short-circuits when ``MO_PROMPT_CACHE_DISABLED=1``.
    Returns the empty list on either disable path.
    """
    global _CACHED_SUPPORTED, _PROBE_DONE

    if os.environ.get("MO_PROMPT_CACHE_DISABLED") == "1":
        return []

    if not _PROBE_DONE:
        try:
            r = subprocess.run(
                ["claude", "--help"],
                capture_output=True, text=True, timeout=10,
            )
            _CACHED_SUPPORTED = (_CACHE_FLAG in (r.stdout or ""))
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            _CACHED_SUPPORTED = False
        _PROBE_DONE = True

    if _CACHED_SUPPORTED:
        return [_CACHE_FLAG]
    return []


def _read_yaml(path: str) -> dict:
    """Load a yaml file → dict (empty dict on missing or empty)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


def _resolve_capability_yaml_paths() -> tuple[str, str, bool]:
    """Resolve the (run-dir-first agents.yaml, fallback agents.yaml) pair.

    Mirrors ``mo_assert_lane_capability`` lines 113-119. The bash source
    calls ``mo_resolve_agents_yaml`` to find the primary, then computes
    a fallback at ``$MINI_ORK_ROOT/config/agents.yaml`` (or the primary
    itself when the fallback is missing). We reuse the existing
    ``resolve_agents_yaml()`` Python helper, redirecting its stdout so
    the parity test doesn't see the bare path echo (which the bash
    version also suppresses — it's called inside a ``$(…)`` capture).
    """
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        config_resolve.resolve_agents_yaml()
    primary = buf.getvalue().rstrip("\n")

    primary_exists = os.path.isfile(primary)
    root = os.environ.get("MINI_ORK_ROOT", "")
    fallback = os.path.join(root, "config", "agents.yaml") if root else ""
    if not (fallback and os.path.isfile(fallback)):
        fallback = primary
    return primary, fallback, primary_exists


def assert_lane_capability(lane: str, required: str | None = None) -> None:
    """Mirror ``mo_assert_lane_capability`` — raise ``RuntimeError`` on missing.

    Bash exits 1 and prints the first missing capability token to stderr
    when the resolved lane family lacks one of the
    ``MO_LANE_REQUIRES_CAPABILITY``-listed capabilities. Empty
    requirements are a no-op (returns 0 / None). An empty lane arg
    produces the literal stderr token ``"lane"`` and exit 1.

    The Python port raises ``RuntimeError(<token>)`` with the same
    first-missing-cap token bash emits — the parity test asserts on
    ``rc=1`` + stderr phrase, not on the internal call shape.
    """
    required = (os.environ.get("MO_LANE_REQUIRES_CAPABILITY", "")
                if required is None else required)
    if not required:
        return
    if not lane:
        raise RuntimeError("lane")

    agents_yaml, fallback_yaml, exists = _resolve_capability_yaml_paths()
    if not exists:
        raise RuntimeError("capabilities")

    cfg = _read_yaml(agents_yaml)
    fallback_cfg = _read_yaml(fallback_yaml)
    lanes = cfg.get("lanes") or {}
    capabilities = cfg.get("capabilities") or fallback_cfg.get("capabilities") or {}
    family = str(lanes.get(lane) or lane).strip()
    family_caps = capabilities.get(family) or {}

    missing: list[str] = []
    for raw in required.split(","):
        name = raw.strip()
        if not name:
            continue
        if family_caps.get(name) is not True:
            missing.append(name)
    if missing:
        raise RuntimeError(missing[0])


def aggregate_cache_stats(iter_dir: str) -> dict:
    """Mirror ``mo_aggregate_cache_stats`` — scan ``*.log`` files, write JSON.

    Bash iterates ``<iter_dir>/*.log`` and sums three token counts via
    ``grep -oE '"<key>":[0-9]+' | awk -F: '{s+=$2} END{print s+0}'``,
    then writes ``<iter_dir>/cache-stats.json`` with a ``jq -n`` template.

    Returns 1 / no write when ``iter_dir`` is not a directory. The
    written JSON has the exact same keys + value shapes (compact form,
    no indent) as bash's ``jq -n`` output — the parity test diffs the
    two JSON files byte-for-byte modulo path fields and float rounding.
    """
    if not os.path.isdir(iter_dir):
        raise FileNotFoundError(f"not a directory: {iter_dir}")
    stats_file = os.path.join(iter_dir, "cache-stats.json")

    creation = 0
    read = 0
    uncached = 0
    file_count = 0
    per_file: list[dict] = []

    for name in sorted(os.listdir(iter_dir)):
        if not name.endswith(".log"):
            continue
        log = os.path.join(iter_dir, name)
        if not os.path.isfile(log):
            continue
        with open(log, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        c = sum(int(m) for m in _CACHE_CREATION_RE.findall(text))
        r = sum(int(m) for m in _CACHE_READ_RE.findall(text))
        u = sum(int(m) for m in _INPUT_TOKENS_RE.findall(text))
        creation += c
        read += r
        uncached += u
        file_count += 1
        per_file.append({
            "file": name,
            "cache_creation": c,
            "cache_read": r,
            "uncached": u,
        })

    saved = round(read * _SAVED_USD_PER_TOKEN, 4)
    total = creation + read + uncached
    hit_rate = round(read / total, 4) if total > 0 else 0
    payload = {
        "cache_creation_tokens": creation,
        "cache_read_tokens": read,
        "uncached_input_tokens": uncached,
        "hit_rate": hit_rate,
        "estimated_usd_saved": saved,
        "log_files_scanned": file_count,
        "per_file": per_file,
    }
    # bash's `jq -n ... > file` writes compact JSON (no indent). The
    # parity test byte-diffs against bash's output, so we use
    # ``json.dumps`` default (no indent) + ``sort_keys=False`` to
    # preserve insertion order.
    with open(stats_file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload))
    return payload


def claude_print(
    prompt: str,
    *extra: str,
    max_turns: int = 40,
    output_format: str = "text",
) -> subprocess.CompletedProcess:
    """Mirror ``mo_claude_print`` — canonical ``claude --print`` wrapper.

    Bash argv order (line 242-249 of ``lib/lane-helpers.sh``)::

        claude \
            --print \
            --permission-mode bypassPermissions \
            --output-format "$_format" \
            --max-turns "$_max_turns" \
            "${_cache_flags[@]}" \
            "$@" \
            "$prompt"

    The Python port constructs the same argv list, interspersing cache
    flags (via ``emit_cache_flags()``) between the fixed flags and the
    caller's extra args + prompt. Returns the raw
    ``subprocess.CompletedProcess`` so callers can inspect rc/stdout/
    stderr — the bash version passes the exit code through to the
    caller; the Python version preserves that by NOT calling
    ``check=True``.
    """
    if not prompt:
        raise ValueError("prompt required")
    cache_flags = emit_cache_flags()
    base = [
        arg.format(format=output_format, max_turns=max_turns)
        for arg in _CLAUDE_BASE
    ]
    argv = ["claude", *base, *cache_flags, *extra, prompt]
    return subprocess.run(argv, capture_output=True, text=True)
