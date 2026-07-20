"""Standalone unit tests for ``mini_ork.gates.scope_overlap``.

Replaces the bash-parity gate as part of the bash→Python migration: the
Python port is now the sole implementation under test, so its coverage no
longer shells out to ``bash -c`` to run ``lib/scope-overlap.sh`` — it
asserts the port's behaviour directly. Expected values below were derived
by hand-tracing the port's YAML line-state machine and union-find (both are
faithful transcriptions of the bash ``awk`` state machines and
``_mo_uf_find``/``_mo_uf_union``, documented inline in
``mini_ork/gates/scope_overlap.py``), not by re-running bash.

The only external dependency the port has is ``git ls-files`` (used to
expand glob patterns against the repo's tracked files). Tests that exercise
``mo_check_scope_overlap`` stub that call out via ``monkeypatch`` so the
suite is hermetic and does not depend on which files happen to be tracked
in the real mini-ork repo at test time.

Cases (10, matching/exceeding the former parity gate's floor):
  (a) ``mo_is_shared_trunk`` — denylist coverage + negative case
  (b) ``mo_get_epic_patterns`` — multi-epic YAML, ``default:`` block
      is itself queryable as an epic-id (quirk preserved), nested-key
      early-exit vs. the underscore-key non-exit quirk
  (c) ``mo_get_epic_symbols_for_file`` — declared symbols + non-
      declared file
  (d) ``mo_symbols_disjoint_for_file`` — disjoint (True), overlapping
      (False), missing yaml (False), one-side-missing (False)
  (e) ``mo_check_scope_overlap`` happy path — non-overlapping
      patterns, rc=0, no JSON, no stderr
  (f) ``mo_check_scope_overlap`` shared-trunk overlap — two epics
      claim ``.gitignore`` (shared-trunk), rc=1, JSON written with
      the exact pairs+partitions shape, specific stderr
  (g) ``mo_check_scope_overlap`` epic-private overlap — two epics
      claim a non-shared-trunk file, rc=0, no JSON, WARN-to-stderr
  (h) ``mo_check_scope_overlap`` symbol-disjoint downgrade — two
      epics claim ``.gitignore`` with disjoint symbol sets, rc=0,
      downgrade WARN emitted, no JSON (SERIALIZE downgraded to WARN)
  (i) ``compute_partitions`` — union-find on a synthetic pair list,
      exact multi-line JSON string pinned
  (j) ``mo_partition_count`` — read the JSON, return the right
      partition count; missing/malformed file returns 1

Tolerance notes:
  * Bash JSON output contains literal newlines between partition
    array elements (``[["A"]\\n,["B"]\\n]``) — these are valid JSON
    whitespace; the Python port reproduces them byte-for-byte, pinned
    here as exact string literals.
  * File lists inside each pair are in sorted order; outer partition
    order is sorted by root key, inner lists sorted alphabetically.
  * This module has no floats; the contract is exact-match, not
    tolerance-based.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import mini_ork.gates.scope_overlap as scope_overlap


def _write_yaml(home: Path, body: str) -> Path:
    """Stage a synthetic scope-patterns.yaml under ``home/config/``."""
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    yaml_path = cfg / "scope-patterns.yaml"
    yaml_path.write_text(body, encoding="utf-8")
    return yaml_path


@pytest.fixture(autouse=True)
def _stub_git_ls_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the only external dependency (``git ls-files``) so
    ``mo_check_scope_overlap`` tests are hermetic — they must not depend
    on which files happen to be tracked in the real mini-ork repo.

    Every test case here uses literal filenames (no ``*`` wildcards) as
    patterns, so treating each pattern as its own single-file match
    reproduces exactly what ``git ls-files -- :(glob)<literal-path>``
    would return for a tracked file.
    """

    def _fake_ls_files(repo_root: str, pattern: str) -> list[str]:
        del repo_root  # unused: stub ignores which repo, only the pattern
        return [pattern] if pattern else []

    monkeypatch.setattr(scope_overlap, "_git_ls_files_glob", _fake_ls_files)


# ─────────────────────────────────────────────────────────────────────────────
# (a) mo_is_shared_trunk — denylist coverage
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("file_,expected", [
    ("shared/types/promptSettings.ts", True),
    ("server/routes/auth.ts", True),
    ("package.json", True),
    ("package-lock.json", True),
    ("tsconfig.json", True),
    ("tsconfig.build.json", True),
    (".gitignore", True),
    (".mini-ork/config/agents.yaml", True),
    (".mini-ork/config/scope-patterns.yaml", True),
    ("server/migrations/0001_init.sql", True),
    # Negatives — outside the denylist
    ("lib/scope-overlap.sh", False),
    ("lib/active_state_index.sh", False),
    ("tests/unit/test_scope_overlap_py.py", False),
    ("README.md", False),
    ("", False),
    ("shared/foo.ts", False),       # not under shared/types/
    ("config/agents.yaml", False),  # not under .mini-ork/config/
])
def test_is_shared_trunk_table_coverage(file_: str, expected: bool) -> None:
    """Every denylist pattern in ``lib/scope-overlap.sh`` (and the default
    fall-through) is pinned directly against the port."""
    assert scope_overlap.mo_is_shared_trunk(file_) == expected


# ─────────────────────────────────────────────────────────────────────────────
# (b) mo_get_epic_patterns — multi-epic YAML, default + nested-key exits
# ─────────────────────────────────────────────────────────────────────────────
def test_get_epic_patterns_multi_epic_yaml(tmp_path: Path) -> None:
    """Three epics, each with a different number of patterns. The
    ``default:`` block is itself queryable as an epic-id (quirk
    preserved from the bash awk: its regex for an epic named
    ``"default"`` is identical to the early-exit sentinel regex).
    A nested key using underscores (``shared_trunk_symbols:``) does
    NOT match the exit regex ``^    [a-z]+:`` (no underscore support),
    so the patterns block does not end there — those lines fall
    through to the catch-all and are emitted verbatim."""
    body = (
        '  EPIC_A:\n'
        '    patterns:\n'
        '      - "lib/scope-overlap.sh"\n'
        '      - "lib/active_state_index.sh"\n'
        '    shared_trunk_symbols:\n'
        '      "lib/scope-overlap.sh":\n'
        '        - SYM_A\n'
        '  EPIC_B:\n'
        '    patterns:\n'
        '      - "lib/adaptive_stability.sh"\n'
        '  default:\n'
        '    patterns:\n'
        '      - "lib/x.sh"\n'
        '  EPIC_C:\n'
        '    patterns:\n'
        '      - "lib/y.sh"\n'
    )
    yaml_path = _write_yaml(tmp_path, body)

    # EPIC_A: two real patterns, then the underscored-key quirk emits
    # three more lines verbatim before EPIC_B's top-level key exits.
    assert scope_overlap.mo_get_epic_patterns("EPIC_A", yaml_path) == [
        "lib/scope-overlap.sh",
        "lib/active_state_index.sh",
        "    shared_trunk_symbols:",
        '      "lib/scope-overlap.sh":',
        "        - SYM_A",
    ]
    assert scope_overlap.mo_get_epic_patterns("EPIC_B", yaml_path) == [
        "lib/adaptive_stability.sh",
    ]
    # default: is itself a queryable epic-id (quirk preserved).
    assert scope_overlap.mo_get_epic_patterns("default", yaml_path) == [
        "lib/x.sh",
    ]
    assert scope_overlap.mo_get_epic_patterns("EPIC_C", yaml_path) == [
        "lib/y.sh",
    ]
    # Unknown epic → empty.
    assert scope_overlap.mo_get_epic_patterns("EPIC_MISSING", yaml_path) == []


def test_get_epic_patterns_missing_yaml_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does" / "not" / "exist.yaml"
    assert scope_overlap.mo_get_epic_patterns("EPIC_A", missing) == []


# ─────────────────────────────────────────────────────────────────────────────
# (c) mo_get_epic_symbols_for_file — declared + non-declared
# ─────────────────────────────────────────────────────────────────────────────
def test_get_epic_symbols_declared_and_missing(tmp_path: Path) -> None:
    """Symbols declared for a specific file come back in declaration
    order; symbols for a non-declared file return empty (both sides).
    A file that has no symbol block returns empty."""
    body = (
        '  EPIC_A:\n'
        '    patterns:\n'
        '      - "lib/scope-overlap.sh"\n'
        '    shared_trunk_symbols:\n'
        '      "shared/types/promptSettings.ts":\n'
        '        - PROMPT_KEYS\n'
        '        - PromptKey\n'
        '      "shared/types/another.ts":\n'
        '        - OtherSym\n'
        '  EPIC_B:\n'
        '    patterns:\n'
        '      - "lib/scope-overlap.sh"\n'
        '    shared_trunk_symbols:\n'
        '      "shared/types/promptSettings.ts":\n'
        '        - PROMPT_KEYS\n'
        '        - HelperFn\n'
    )
    yaml_path = _write_yaml(tmp_path, body)

    cases = [
        ("EPIC_A", "shared/types/promptSettings.ts", ["PROMPT_KEYS", "PromptKey"]),
        ("EPIC_A", "shared/types/another.ts", ["OtherSym"]),
        ("EPIC_B", "shared/types/promptSettings.ts", ["PROMPT_KEYS", "HelperFn"]),
        ("EPIC_A", "shared/types/never_declared.ts", []),
        ("EPIC_B", "shared/types/another.ts", []),  # not declared by B
    ]
    for epic, file_, expected in cases:
        got = scope_overlap.mo_get_epic_symbols_for_file(epic, file_, yaml_path)
        assert got == expected, f"[{epic}/{file_}] expected={expected!r} got={got!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (d) mo_symbols_disjoint_for_file — disjoint, overlap, missing yaml
# ─────────────────────────────────────────────────────────────────────────────
def test_symbols_disjoint_full_matrix(tmp_path: Path) -> None:
    """The four cases:
       (1) disjoint sets (True) — downgrade safe
       (2) overlapping sets (False) — serialize
       (3) one side missing symbols (False) — fall back to serialize
       (4) yaml missing entirely (False) — fall back to serialize
    """
    body = (
        '  EPIC_A:\n'
        '    shared_trunk_symbols:\n'
        '      "shared/types/promptSettings.ts":\n'
        '        - SYM_A1\n'
        '        - SYM_A2\n'
        '  EPIC_B:\n'
        '    shared_trunk_symbols:\n'
        '      "shared/types/promptSettings.ts":\n'
        '        - SYM_B1\n'
        '        - SYM_B2\n'
        '  EPIC_C:\n'
        '    shared_trunk_symbols:\n'
        '      "shared/types/promptSettings.ts":\n'
        '        - SYM_C1\n'
        '        - SHARED\n'
        '  EPIC_D:\n'
        '    shared_trunk_symbols:\n'
        '      "shared/types/promptSettings.ts":\n'
        '        - SYM_D1\n'
        '        - SHARED\n'
        '  EPIC_NO_SYMBOLS:\n'
        '    patterns:\n'
        '      - "lib/scope-overlap.sh"\n'
    )
    yaml_path = _write_yaml(tmp_path, body)
    file_ = "shared/types/promptSettings.ts"

    # (1) disjoint: A vs B → True
    assert scope_overlap.mo_symbols_disjoint_for_file(
        file_, "EPIC_A", "EPIC_B", yaml_path=yaml_path
    ) is True

    # (2) overlapping: C vs D share SHARED → False
    assert scope_overlap.mo_symbols_disjoint_for_file(
        file_, "EPIC_C", "EPIC_D", yaml_path=yaml_path
    ) is False

    # (3) one side missing symbols: A vs EPIC_NO_SYMBOLS → False
    assert scope_overlap.mo_symbols_disjoint_for_file(
        file_, "EPIC_A", "EPIC_NO_SYMBOLS", yaml_path=yaml_path
    ) is False

    # (4) yaml missing entirely → False
    bogus_home = tmp_path / "totally_unrelated_dir"
    bogus_home.mkdir()
    bogus = bogus_home / "config" / "scope-patterns.yaml"
    assert scope_overlap.mo_symbols_disjoint_for_file(
        file_, "EPIC_A", "EPIC_B", yaml_path=bogus
    ) is False


def test_symbols_disjoint_uses_mini_ork_home_env_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``yaml_path`` is omitted, the port falls back to
    ``$MINI_ORK_HOME/config/scope-patterns.yaml`` (mirrors bash's
    default ``${MINI_ORK_HOME:-.mini-ork}``)."""
    home = tmp_path / "home"
    _write_yaml(home, (
        '  EPIC_A:\n'
        '    shared_trunk_symbols:\n'
        '      "f.ts":\n'
        '        - A1\n'
        '  EPIC_B:\n'
        '    shared_trunk_symbols:\n'
        '      "f.ts":\n'
        '        - B1\n'
    ))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    assert scope_overlap.mo_symbols_disjoint_for_file("f.ts", "EPIC_A", "EPIC_B") is True


# ─────────────────────────────────────────────────────────────────────────────
# (e) mo_check_scope_overlap — happy path (no overlap)
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two epics with non-overlapping patterns → no overlap, rc=0,
    no JSON written, no stderr."""
    home = tmp_path / "home"
    _write_yaml(home, (
        '  EPIC_A:\n'
        '    patterns:\n'
        '      - "lib/active_state_index.sh"\n'
        '  EPIC_B:\n'
        '    patterns:\n'
        '      - "lib/adaptive_stability.sh"\n'
    ))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc, payload = scope_overlap.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], "unused-repo-root", str(home), str(run_dir),
    )
    assert rc == 0
    assert payload == {"pairs": [], "partitions": []}
    assert not (run_dir / "scope-overlap.json").exists()
    assert capsys.readouterr().err == ""


# ─────────────────────────────────────────────────────────────────────────────
# (f) mo_check_scope_overlap — shared-trunk overlap
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_shared_trunk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two epics both claim ``.gitignore`` (shared-trunk) → rc=1,
    JSON written with exact pairs+partitions shape, two stderr
    lines (SCOPE OVERLAP + SERIALIZE recommendation)."""
    home = tmp_path / "home"
    _write_yaml(home, (
        '  EPIC_A:\n'
        '    patterns:\n'
        '      - ".gitignore"\n'
        '  EPIC_B:\n'
        '    patterns:\n'
        '      - ".gitignore"\n'
    ))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc, payload = scope_overlap.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], "unused-repo-root", str(home), str(run_dir),
    )
    assert rc == 1
    assert payload["pairs"] == [
        {"a": "EPIC_A", "b": "EPIC_B", "files": [".gitignore"]}
    ]
    assert payload["partitions"] == [["EPIC_A", "EPIC_B"]]

    overlap_json = run_dir / "scope-overlap.json"
    assert overlap_json.exists()
    on_disk = json.loads(overlap_json.read_text(encoding="utf-8"))
    assert on_disk == payload

    stderr = capsys.readouterr().err
    assert "[mini-ork] SCOPE OVERLAP (shared-trunk): EPIC_A ↔ EPIC_B — 1 file(s)" in stderr
    assert (
        "[mini-ork] SERIALIZE recommendation: shared-trunk overlap → "
        "1 partition(s) across 2 epic(s)"
    ) in stderr


def test_check_scope_overlap_shared_trunk_bypassed(tmp_path: Path) -> None:
    """``serialize_on_overlap=0`` logs the overlap but returns rc=0
    (mirrors ``MO_SERIALIZE_ON_OVERLAP=0``)."""
    home = tmp_path / "home"
    _write_yaml(home, (
        '  EPIC_A:\n'
        '    patterns:\n'
        '      - ".gitignore"\n'
        '  EPIC_B:\n'
        '    patterns:\n'
        '      - ".gitignore"\n'
    ))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc, payload = scope_overlap.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], "unused-repo-root", str(home), str(run_dir),
        serialize_on_overlap=0,
    )
    assert rc == 0
    assert payload["pairs"] == [
        {"a": "EPIC_A", "b": "EPIC_B", "files": [".gitignore"]}
    ]
    # The JSON is still written even when serialization is bypassed.
    assert (run_dir / "scope-overlap.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# (g) mo_check_scope_overlap — epic-private overlap
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_epic_private(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two epics both claim a non-shared-trunk file
    (``lib/scope-overlap.sh``) → rc=0, no JSON, WARN-to-stderr."""
    home = tmp_path / "home"
    _write_yaml(home, (
        '  EPIC_A:\n'
        '    patterns:\n'
        '      - "lib/scope-overlap.sh"\n'
        '  EPIC_B:\n'
        '    patterns:\n'
        '      - "lib/scope-overlap.sh"\n'
    ))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc, payload = scope_overlap.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], "unused-repo-root", str(home), str(run_dir),
    )
    assert rc == 0
    assert payload == {"pairs": [], "partitions": []}
    assert not (run_dir / "scope-overlap.json").exists()

    stderr = capsys.readouterr().err
    assert (
        "[mini-ork] SCOPE OVERLAP (epic-private): EPIC_A ↔ EPIC_B — "
        "1 file(s) [WARN, proceeding parallel]"
    ) in stderr


# ─────────────────────────────────────────────────────────────────────────────
# (h) mo_check_scope_overlap — symbol-disjoint downgrade
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_symbol_disjoint_downgrade(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two epics claim a shared-trunk file (``.gitignore``) with
    DISJOINT symbol sets → downgrade WARN, rc=0, no JSON."""
    home = tmp_path / "home"
    _write_yaml(home, (
        '  EPIC_A:\n'
        '    patterns:\n'
        '      - ".gitignore"\n'
        '    shared_trunk_symbols:\n'
        '      ".gitignore":\n'
        '        - SYM_A1\n'
        '        - SYM_A2\n'
        '  EPIC_B:\n'
        '    patterns:\n'
        '      - ".gitignore"\n'
        '    shared_trunk_symbols:\n'
        '      ".gitignore":\n'
        '        - SYM_B1\n'
        '        - SYM_B2\n'
    ))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc, payload = scope_overlap.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], "unused-repo-root", str(home), str(run_dir),
    )
    assert rc == 0
    assert payload == {"pairs": [], "partitions": []}
    assert not (run_dir / "scope-overlap.json").exists()

    stderr = capsys.readouterr().err
    assert (
        "[mini-ork] SCOPE OVERLAP (symbol-disjoint downgrade): "
        "EPIC_A ↔ EPIC_B — 1 file(s) [WARN, declared symbols disjoint]"
    ) in stderr
    assert "SCOPE OVERLAP (shared-trunk)" not in stderr


# ─────────────────────────────────────────────────────────────────────────────
# mo_check_scope_overlap — degenerate config (missing yaml / JOB_RUN_DIR)
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_missing_yaml_warns_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "no-config-here"
    rc, payload = scope_overlap.mo_check_scope_overlap(
        ["EPIC_A"], "unused-repo-root", str(home), str(tmp_path / "run"),
    )
    assert rc == 0
    assert payload == {"pairs": [], "partitions": []}
    assert "scope-patterns.yaml not found" in capsys.readouterr().err


def test_check_scope_overlap_missing_job_run_dir_warns_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_yaml(home, '  EPIC_A:\n    patterns:\n      - "x"\n')
    rc, payload = scope_overlap.mo_check_scope_overlap(
        ["EPIC_A"], "unused-repo-root", str(home), "",
    )
    assert rc == 0
    assert payload == {"pairs": [], "partitions": []}
    assert "JOB_RUN_DIR unset" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────────
# (i) compute_partitions — union-find, exact string pinned
# ─────────────────────────────────────────────────────────────────────────────
def test_compute_partitions_disjoint_pairs() -> None:
    """Two disjoint pairs (X,Y) and (Z,W) → 2 partitions: {W,Z} and
    {X,Y}. Outer key order sorted (W before X); inner lists sorted."""
    pairs_inner = (
        '{"a":"X","b":"Y","files":["a"]},'
        '{"a":"Z","b":"W","files":["b"]}'
    )
    out = scope_overlap.compute_partitions(pairs_inner, ["X", "Y", "Z", "W"])
    assert out == '[["W","Z"]\n,["X","Y"]\n]\n'
    assert json.loads(out) == [["W", "Z"], ["X", "Y"]]


def test_compute_partitions_chain_pairs() -> None:
    """A 3-way chain (X,Y) + (Y,Z) merges into one partition; W is
    an untouched singleton."""
    pairs_inner = (
        '{"a":"X","b":"Y","files":["a"]},'
        '{"a":"Y","b":"Z","files":["b"]}'
    )
    out = scope_overlap.compute_partitions(pairs_inner, ["X", "Y", "Z", "W"])
    assert out == '[["W"]\n,["X","Y","Z"]\n]\n'
    assert json.loads(out) == [["W"], ["X", "Y", "Z"]]


def test_compute_partitions_no_pairs_all_singletons() -> None:
    """No pairs → every epic is its own singleton partition, sorted
    alphabetically by key."""
    out = scope_overlap.compute_partitions("", ["X", "Y", "Z", "W"])
    assert out == '[["W"]\n,["X"]\n,["Y"]\n,["Z"]\n]\n'
    assert json.loads(out) == [["W"], ["X"], ["Y"], ["Z"]]


def test_compute_partitions_one_pair() -> None:
    """One pair (A,B) merges into a single 2-element partition; C and
    D remain independent singletons."""
    pairs_inner = '{"a":"A","b":"B","files":["x"]}'
    out = scope_overlap.compute_partitions(pairs_inner, ["A", "B", "C", "D"])
    assert out == '[["A","B"]\n,["C"]\n,["D"]\n]\n'
    assert json.loads(out) == [["A", "B"], ["C"], ["D"]]


# ─────────────────────────────────────────────────────────────────────────────
# (j) mo_partition_count — read JSON
# ─────────────────────────────────────────────────────────────────────────────
def test_partition_count_reads_json(tmp_path: Path) -> None:
    """``mo_partition_count`` returns the right partition count from
    a synthetic ``scope-overlap.json``. Missing file → 1. Length
    0 → 1 (defensive floor). Length >= 1 → that number."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Case 1: missing file → 1
    assert scope_overlap.mo_partition_count(str(run_dir)) == 1

    # Case 2: 3 partitions → 3
    overlap_json = run_dir / "scope-overlap.json"
    overlap_json.write_text(
        '{"pairs":[],"partitions":[["A","B"],["C"],["D","E","F"]]}\n',
        encoding="utf-8",
    )
    assert scope_overlap.mo_partition_count(str(run_dir)) == 3

    # Case 3: empty partitions list → 1 (defensive floor)
    overlap_json.write_text('{"pairs":[],"partitions":[]}\n', encoding="utf-8")
    assert scope_overlap.mo_partition_count(str(run_dir)) == 1

    # Case 4: malformed JSON → 1
    overlap_json.write_text('not json\n', encoding="utf-8")
    assert scope_overlap.mo_partition_count(str(run_dir)) == 1

    # Case 5: empty job_run_dir string → 1
    assert scope_overlap.mo_partition_count("") == 1


# ─────────────────────────────────────────────────────────────────────────────
# Smoke: module imports + public API exists
# ─────────────────────────────────────────────────────────────────────────────
def test_module_imports_and_api() -> None:
    """Module imports cleanly; every function in ``__all__`` is callable."""
    for name in scope_overlap.__all__:
        fn = getattr(scope_overlap, name)
        assert callable(fn), f"{name} not callable"
    assert scope_overlap.mo_is_shared_trunk.__name__ == "mo_is_shared_trunk"
    assert scope_overlap.compute_partitions.__name__ == "compute_partitions"
