"""Pure-logic port of ``lib/scope-overlap.sh``.

Faithful port of the deterministic core of the scope-overlap detector.
The bash source is a thin shell wrapping ``awk`` + ``jq`` + ``git
ls-files``; this module lifts that pipeline into a normal importable
surface and exposes the public functions ``dispatch.sh`` / callers rely
on.

Co-existence model (strangler-fig): bash ``lib/scope-overlap.sh`` is
the authoritative source. Parity is enforced by
``tests/unit/test_scope_overlap_py.py`` (>=6 cases) which invokes the
LIVE bash function via subprocess (no mocks) and asserts byte-equal
output. Floats compared at 1e-6 tolerance per the kickoff (this
module has no floats; the contract is identical).

Public API:

    mo_is_shared_trunk(file)                                       -> bool
    mo_get_epic_patterns(epic, yaml_path)                          -> list[str]
    mo_get_epic_symbols_for_file(epic, file, yaml_path)             -> list[str]
    mo_symbols_disjoint_for_file(file, epic_a, epic_b, yaml=None)  -> bool
    mo_check_scope_overlap(epics, repo_root, mini_ork_home,
                           job_run_dir, serialize_on_overlap=None) -> (int, dict)
    mo_partition_count(job_run_dir)                                -> int
    compute_partitions(pairs_json_inner, all_epics)                -> str

YAML parsing is line-based (mirrors the awk state machine verbatim) —
no PyYAML. Glob expansion uses ``git ls-files -- :(glob)<pat>`` via
subprocess so fnmatch/glob divergence cannot sneak in. Union-find
connected components reproduce the bash ``_mo_uf_find`` /
``_mo_uf_union`` semantics. Warnings go to stderr; return codes match
bash (0/1).

JSON output (byte-equal with bash)::

    {"pairs":[{"a":"X","b":"Y","files":["f1"]}],
     "partitions":[["A","B"]\\n,["C"]\\n]\\n}

The partitions array is multi-line: each inner partition is followed by
``\\n`` (bash's ``jq -c`` always appends a newline). The outer JSON
object also ends with ``\\n`` (the trailing newline from
``_mo_compute_partitions``'s final ``printf ']\\n'``).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

__all__ = [
    "mo_is_shared_trunk",
    "mo_get_epic_patterns",
    "mo_get_epic_symbols_for_file",
    "mo_symbols_disjoint_for_file",
    "mo_check_scope_overlap",
    "mo_partition_count",
    "compute_partitions",
]


# ─── shared-trunk denylist ──────────────────────────────────────────────
# Mirrors the case statement in lib/scope-overlap.sh::mo_is_shared_trunk
# (lines 18-46). Patterns are bash case-globs (only ``*`` is special),
# not regex — fnmatch is the right translation.
_SHARED_TRUNK_PATTERNS: tuple[str, ...] = (
    "shared/types/*",
    "server/routes/*",
    "package*.json",
    "tsconfig*.json",
    ".gitignore",
    ".mini-ork/config/*",
    "server/migrations/*",
)


def mo_is_shared_trunk(file: str) -> bool:
    """True iff ``file`` is on the shared-trunk denylist.

    Mirrors ``lib/scope-overlap.sh:18-46`` (the seven case patterns +
    the default fall-through ``return 1``). Empty string returns
    False (bash's ``case "" in ... *) return 1 ;; esac``).
    """
    if not file:
        return False
    for pat in _SHARED_TRUNK_PATTERNS:
        if fnmatch.fnmatchcase(file, pat):
            return True
    return False


# ─── YAML line-state machine (shared) ──────────────────────────────────
# Mirrors the awk state machines in lib/scope-overlap.sh verbatim.
# Top-level YAML key is 2-space-indent; ``patterns:`` /
# ``shared_trunk_symbols:`` are 4-space-indent; their list items are
# 6-/8-space-prefixed ``      - "..."``. Early-exit triggers:
#   * next top-level (2-space) key inside the epic block
#   * ``default:`` (sibling epic marker — bash exits both awk
#     functions on this)
#   * next 4-space-indented key inside the patterns/symbols block
_TOP_KEY_RE = re.compile(r"^  ([A-Za-z0-9_-]+):")
_TOP_DEFAULT_RE = re.compile(r"^  default:")
_NESTED_KEY_LOWER_RE = re.compile(r"^    [a-z]+:")
_PATTERNS_KEY_RE = re.compile(r"^    patterns:")
_SYMBOLS_KEY_RE = re.compile(r"^    shared_trunk_symbols:")
# Symbol-list item (8-space indent + "- "); file decl is 6-space.
_SYMBOL_LIST_ITEM_RE = re.compile(r'^        - "?')
_PATTERN_LIST_ITEM_RE = re.compile(r'^      - "?')
_SYMBOL_FILE_DECL_RE = re.compile(r'^      "([^"]+)":')


def _strip_optional_trailing_quote(s: str) -> str:
    """Strip one optional trailing ``"`` and any trailing whitespace.

    Mirrors awk's ``gsub(/"$/, "")`` after the leading-prefix strip.
    Used by both pattern and symbol list items.
    """
    s = s.rstrip()
    if s.endswith('"'):
        s = s[:-1]
    return s


# ─── Pattern extraction ─────────────────────────────────────────────────
# Mirrors lib/scope-overlap.sh:51-66 (mo_get_epic_patterns).
def mo_get_epic_patterns(epic: str, yaml_path: str | os.PathLike) -> list[str]:
    """Return the list of glob patterns declared for ``epic``.

    Empty list when ``epic`` is absent or ``yaml_path`` does not
    exist.

    Faithful-port quirk: bash's awk exit-on-nested-key regex is
    ``^    [a-z]+:`` (no underscore), so a nested key like
    ``shared_trunk_symbols:`` is NOT matched — the awk falls through
    to the catch-all ``in_patterns { gsub; print }`` block and the
    non-list-item line is emitted verbatim (after the no-op gsub).
    The Python port reproduces this behavior for byte-equal parity.
    Real-world yamls rarely have underscore-named nested keys
    adjacent to ``patterns:``, so the practical impact is small.
    """
    p = Path(yaml_path)
    if not p.is_file():
        return []
    epic_re = re.compile(rf"^  {re.escape(epic)}:")
    out: list[str] = []
    in_epic = False
    in_patterns = False
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not in_epic:
            if epic_re.match(raw):
                in_epic = True
            continue
        # in_epic: check the early-exit triggers.
        if _TOP_KEY_RE.match(raw) or _TOP_DEFAULT_RE.match(raw):
            break
        if _PATTERNS_KEY_RE.match(raw):
            in_patterns = True
            continue
        if in_patterns:
            # Bash awk: ``in_patterns && /^    [a-z]+:/ { in_patterns=0;
            # next }`` — exit patterns block on a 4-space lowercase
            # key (NOTE: bash's [a-z]+ has no underscore; the port
            # mirrors this exactly).
            if _NESTED_KEY_LOWER_RE.match(raw):
                in_patterns = False
                continue
            # Catch-all (mirrors awk's ``in_patterns { gsub; print }``):
            # any line that reaches here gets the gsub applied (which
            # is a no-op for non-list-item lines) and is printed.
            stripped = _PATTERN_LIST_ITEM_RE.sub("", raw, count=1)
            out.append(_strip_optional_trailing_quote(stripped))
    return out


# ─── Symbol-level claim extraction ─────────────────────────────────────
# Mirrors lib/scope-overlap.sh:87-108 (mo_get_epic_symbols_for_file).
def mo_get_epic_symbols_for_file(
    epic: str, file: str, yaml_path: str | os.PathLike
) -> list[str]:
    """Return the list of symbols claimed by ``epic`` for ``file``.

    Empty list when the epic does not declare symbols for this file
    (or the YAML is missing). Symbols are emitted in declaration
    order, no dedup (the caller's `sort -u` dedups later).
    """
    p = Path(yaml_path)
    if not p.is_file():
        return []
    epic_re = re.compile(rf"^  {re.escape(epic)}:")
    out: list[str] = []
    in_epic = False
    in_symbols = False
    in_file = False
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not in_epic:
            if epic_re.match(raw):
                in_epic = True
                in_symbols = False
                in_file = False
            continue
        # Early-exit triggers (same as mo_get_epic_patterns).
        if _TOP_KEY_RE.match(raw) or _TOP_DEFAULT_RE.match(raw):
            break
        if _SYMBOLS_KEY_RE.match(raw):
            in_symbols = True
            in_file = False
            continue
        if in_symbols:
            # Any 4-space lowercase key other than shared_trunk_symbols
            # itself ends the symbols block. bash uses
            # ``^    [a-z_]+:`` (allow underscore) — keep the regex
            # loose so future keys exit cleanly.
            if re.match(r"^    [a-z_]+:", raw) and not _SYMBOLS_KEY_RE.match(raw):
                in_symbols = False
                in_file = False
                continue
            m = _SYMBOL_FILE_DECL_RE.match(raw)
            if m:
                in_file = (m.group(1) == file)
                continue
        if in_file and _SYMBOL_LIST_ITEM_RE.match(raw):
            stripped = _SYMBOL_LIST_ITEM_RE.sub("", raw, count=1)
            out.append(_strip_optional_trailing_quote(stripped))
    return out


# ─── Disjointness check ─────────────────────────────────────────────────
# Mirrors lib/scope-overlap.sh:113-129 (mo_symbols_disjoint_for_file).
def mo_symbols_disjoint_for_file(
    file: str,
    epic_a: str,
    epic_b: str,
    yaml_path: str | os.PathLike | None = None,
) -> bool:
    """True iff ``epic_a`` and ``epic_b`` declare DISJOINT symbol sets
    for ``file``.

    False if either side has no declared symbols (fall back to
    SERIALIZE) OR the intersection is non-empty. ``yaml_path``
    defaults to ``$MINI_ORK_HOME/config/scope-patterns.yaml`` with
    the same fall-through chain as bash.
    """
    if yaml_path is None:
        home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
        yaml_path = Path(home) / "config" / "scope-patterns.yaml"
    else:
        yaml_path = Path(yaml_path)
    if not yaml_path.is_file():
        return False
    syms_a = {s for s in mo_get_epic_symbols_for_file(epic_a, file, yaml_path) if s}
    syms_b = {s for s in mo_get_epic_symbols_for_file(epic_b, file, yaml_path) if s}
    if not syms_a or not syms_b:
        return False
    return not (syms_a & syms_b)


# ─── Scope-overlap checker ──────────────────────────────────────────────
# Mirrors lib/scope-overlap.sh:140-280 (mo_check_scope_overlap).
def _git_ls_files_glob(repo_root: str, pattern: str) -> list[str]:
    """Run ``git -C repo_root ls-files -- :(glob)<pattern>`` and return
    the resulting file list (empty on no match or git failure).

    Bash's ``:(glob)`` pathspec is git magic — translating to
    pathlib/fnmatch would diverge on edge cases (``**`` semantics,
    negation patterns, etc.). Always shell out so the pathspec
    semantics stay exactly in lockstep with bash.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, "ls-files", "--", f":(glob){pattern}"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln]


def _comm_intersect(a: Iterable[str], b: Iterable[str]) -> list[str]:
    """Sorted intersection of two iterables (mirrors ``comm -12``).

    Both inputs are pre-deduped+sort by the caller (bash's
    ``sort -u``); this is a pure set intersection re-sorted. The
    order matches what ``comm -12`` emits on two pre-sorted unique
    files.
    """
    return sorted(set(a) & set(b))


def mo_check_scope_overlap(
    epics: list[str],
    repo_root: str,
    mini_ork_home: str | os.PathLike,
    job_run_dir: str | os.PathLike,
    serialize_on_overlap: int | None = None,
) -> tuple[int, dict]:
    """Detect shared-trunk overlap across a set of epics.

    Parameters
    ----------
    epics:
        List of epic ids, in iteration order (mirrors the bash
        ``EPICS`` array).
    repo_root:
        Path used as the ``-C`` arg to ``git ls-files`` for glob
        expansion.
    mini_ork_home:
        Directory whose ``config/scope-patterns.yaml`` is the
        scope-patterns source.
    job_run_dir:
        Directory where ``scope-overlap.json`` is written when
        shared-trunk overlap is found. Stale files are removed
        when no overlap is found.
    serialize_on_overlap:
        Override for the ``MO_SERIALIZE_ON_OVERLAP`` env var (1 =
        return 1 on overlap, 0 = log only and return 0). When None,
        reads from env with default 1.

    Returns
    -------
    (rc, payload) tuple:
        * ``rc`` — 0 (no overlap, or serialize bypassed) or 1
          (overlap + serialize-on-overlap=1).
        * ``payload`` — dict with keys ``pairs`` (list of
          ``{a, b, files[]}``) and ``partitions`` (list of
          sorted-epic lists).

    Side effects: writes ``<job_run_dir>/scope-overlap.json`` when
    shared-trunk overlap is detected; prints warnings to stderr;
    removes any stale ``scope-overlap.json`` when no overlap is
    found.
    """
    home = Path(mini_ork_home)
    yaml_path = home / "config" / "scope-patterns.yaml"
    job_run = Path(job_run_dir) if job_run_dir else None
    overlap_json = job_run / "scope-overlap.json" if job_run else None

    empty_payload = {"pairs": [], "partitions": []}

    if not yaml_path.is_file():
        print(
            "[mini-ork] WARN: scope-patterns.yaml not found — skipping overlap check",
            file=sys.stderr,
        )
        return (0, empty_payload)
    if not job_run_dir:
        print(
            "[mini-ork] WARN: JOB_RUN_DIR unset — skipping overlap check",
            file=sys.stderr,
        )
        return (0, empty_payload)

    # Build file sets per epic (mirrors bash `epic_files[epic]=...`).
    epics_list = list(epics)
    epic_files: dict[str, list[str]] = {}
    for epic in epics_list:
        patterns = mo_get_epic_patterns(epic, yaml_path)
        if not patterns:
            epic_files[epic] = []
            continue
        collected: list[str] = []
        for pat in patterns:
            if not pat:
                continue
            matched = _git_ls_files_glob(repo_root, pat)
            if matched:
                collected.extend(matched)
        # `sort -u | grep -v '^$'` in bash — dedup, sort, drop empty.
        epic_files[epic] = sorted({f for f in collected if f})

    # Pairwise intersection over the epic list.
    overlap_pairs: list[dict] = []
    n = len(epics_list)
    for i in range(n):
        for j in range(i + 1, n):
            a = epics_list[i]
            b = epics_list[j]
            files_a = epic_files.get(a, [])
            files_b = epic_files.get(b, [])
            if not files_a or not files_b:
                continue
            common = _comm_intersect(files_a, files_b)
            if not common:
                continue

            shared_trunk_files: list[str] = []
            downgraded_files: list[str] = []
            private_files: list[str] = []
            for f in common:
                if mo_is_shared_trunk(f):
                    if mo_symbols_disjoint_for_file(f, a, b, yaml_path=yaml_path):
                        downgraded_files.append(f)
                    else:
                        shared_trunk_files.append(f)
                else:
                    private_files.append(f)

            if downgraded_files:
                dg_count = len(downgraded_files)
                print(
                    f"[mini-ork] SCOPE OVERLAP (symbol-disjoint downgrade): "
                    f"{a} ↔ {b} — {dg_count} file(s) [WARN, declared symbols disjoint]",
                    file=sys.stderr,
                )
            if shared_trunk_files:
                file_count = len(shared_trunk_files)
                overlap_pairs.append({
                    "a": a,
                    "b": b,
                    "files": list(shared_trunk_files),
                })
                print(
                    f"[mini-ork] SCOPE OVERLAP (shared-trunk): {a} ↔ {b} — {file_count} file(s)",
                    file=sys.stderr,
                )
            elif private_files:
                file_count = len(private_files)
                print(
                    f"[mini-ork] SCOPE OVERLAP (epic-private): {a} ↔ {b} — "
                    f"{file_count} file(s) [WARN, proceeding parallel]",
                    file=sys.stderr,
                )

    if serialize_on_overlap is None:
        serialize = int(os.environ.get("MO_SERIALIZE_ON_OVERLAP", "1"))
    else:
        serialize = 1 if serialize_on_overlap else 0

    if not overlap_pairs:
        # Mirror bash: rm -f the stale JSON on no-overlap exit.
        if overlap_json is not None:
            try:
                overlap_json.unlink()
            except FileNotFoundError:
                pass
        return (0, empty_payload)

    pairs_str = ",".join(
        json.dumps(p, separators=(",", ":"), ensure_ascii=False)
        for p in overlap_pairs
    )
    partitions_str = compute_partitions(pairs_str, epics_list)

    # Bash's ``partitions_json=$(_mo_compute_partitions ...)`` strips
    # trailing newlines (POSIX command substitution). When the
    # captured value is interpolated into the printf format string,
    # the trailing ``\\n`` is gone, so the resulting JSON file has
    # only the inner-partition ``\\n`` (between the inner ``]`` and
    # outer ``]``), not between outer ``]`` and ``}``. Mirror by
    # stripping the trailing newline before assembly.
    partitions_inner = partitions_str.rstrip("\n")

    # Concatenate the multi-line partitions output as-is; newlines are
    # valid JSON whitespace, so the resulting file is still parseable.
    # Bash writes the file with ``printf '%s\\n' "..."`` which adds a
    # trailing newline after the closing ``}``; mirror that explicitly
    # so the on-disk file is byte-equal with bash.
    full_json_str = (
        '{"pairs":[' + pairs_str + '],"partitions":' + partitions_inner + '}\n'
    )

    # The payload returned to in-process callers is the parsed dict;
    # tests against the live bash compare the on-disk string instead.
    payload = json.loads(full_json_str)

    if overlap_json is not None:
        overlap_json.write_text(full_json_str, encoding="utf-8")

    partition_count = len(payload.get("partitions", []))
    total_epics = len(epics_list)
    print(
        f"[mini-ork] SERIALIZE recommendation: shared-trunk overlap → "
        f"{partition_count} partition(s) across {total_epics} epic(s) (see {overlap_json})",
        file=sys.stderr,
    )

    if serialize == 1:
        return (1, payload)
    print(
        "[mini-ork] MO_SERIALIZE_ON_OVERLAP=0 — overlap logged, serialization bypassed",
        file=sys.stderr,
    )
    return (0, payload)


# ─── Partition graph helper ─────────────────────────────────────────────
# Public name ``compute_partitions`` mirrors the bash internal
# ``_mo_compute_partitions`` (so callers from in-process Python code
# don't depend on the underscore-prefix). Args:
#   pairs_json_inner — comma-separated JSON pair objects (NO
#     surrounding brackets) — the same shape bash's
#     ``printf '%s\\n' "${overlap_pairs[@]}" | paste -sd ',' -`` produces.
#   all_epics — list of all epic ids in input order (from the EPICS
#     array, used to seed parents and group iteration order).
# Returns the multi-line JSON string bash produces:
#     [["W"]\\n,["X","Y"]\\n,["Z"]\\n]\\n
def compute_partitions(pairs_json_inner: str, all_epics: list[str]) -> str:
    """Compute connected components of the conflict graph via
    union-find. Output is byte-equal with bash:
    ``[["W"]\\n,["X","Y"]\\n,["Z"]\\n]\\n``.

    Outer partition order is sorted by root key; inner lists sorted
    alphabetically. Ties in iteration order inside a group don't
    matter because the inner list is sorted.
    """
    parent: dict[str, str] = {e: e for e in all_epics}

    def find(x: str) -> str:
        # Mirrors bash _mo_uf_find one-level path compression. The
        # final root is identical to a full-compression find.
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        parent[ra] = rb

    if pairs_json_inner:
        # Wrap as a JSON array and parse — equivalent to bash's
        # `echo "[$pairs_json]" | jq -r '.[] | [.a,.b] | @tsv'`.
        try:
            pairs = json.loads("[" + pairs_json_inner + "]")
        except json.JSONDecodeError:
            pairs = []
        for p in pairs:
            a = p.get("a")
            b = p.get("b")
            if a and b:
                union(a, b)

    # Build groups in input (all_epics) order — mirrors bash's
    # `groups[$root]="${groups[$root]:-}|$epic"`.
    groups: dict[str, list[str]] = {}
    for epic in all_epics:
        root = find(epic)
        if root not in groups:
            groups[root] = []
        groups[root].append(epic)

    # Output assembly: `[<json>\\n,<json>\\n...]\\n`
    # - ``[`` opener
    # - per partition: optional leading ``,`` (not on the first),
    #   then the inner JSON, then a literal ``\\n`` (mirrors jq's
    #   trailing newline)
    # - ``]\\n`` closer (from bash's ``printf ']\\n'``)
    parts: list[str] = ["["]
    for i, k in enumerate(sorted(groups.keys())):
        if i > 0:
            parts.append(",")
        inner = json.dumps(
            sorted(groups[k]), separators=(",", ":"), ensure_ascii=False
        )
        parts.append(inner)
        parts.append("\n")
    parts.append("]\n")
    return "".join(parts)


# ─── Public read-side accessor ─────────────────────────────────────────
# Mirrors lib/scope-overlap.sh:347-357 (mo_partition_count).
def mo_partition_count(job_run_dir: str | os.PathLike) -> int:
    """Read the partition count from the most-recent
    ``scope-overlap.json``.

    Returns 1 if the file is missing, malformed, has no
    ``partitions`` key, or partitions length < 1. Mirrors bash's
    `// 1` (null coalesce) + `[ "$n" -lt 1 ] && n=1` guards.
    """
    if not job_run_dir:
        return 1
    job_run = Path(job_run_dir)
    overlap_json = job_run / "scope-overlap.json"
    if not overlap_json.is_file():
        return 1
    try:
        doc = json.loads(overlap_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    parts = doc.get("partitions")
    if not isinstance(parts, list):
        return 1
    n = len(parts)
    if n < 1:
        return 1
    return n
