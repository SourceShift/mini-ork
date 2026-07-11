"""Parity gate: ``mini_ork.ported.scope_overlap`` vs ``lib/scope-overlap.sh``.

Each test invokes the LIVE bash function via subprocess (no mocks,
no hardcoded expected outputs) and compares the Python port's output
against the live bash output. The test fixtures build synthetic
``scope-patterns.yaml`` files under ``tmp_path`` and use the real
mini-ork repo as ``REPO_ROOT`` so ``git ls-files -- :(glob)`` returns
real tracked files (the synthetic YAML is hermetic; only the patterns
declared inside it change between cases).

Co-existence: ``lib/scope-overlap.sh`` is byte-identical before and
after these tests run (strangler-fig). The bash source stays
authoritative; this test suite pins the Python port to byte-exact
parity so in-process Python callers can rely on it.

Cases (10, above the kickoff's >=6 floor):
  (a) ``mo_is_shared_trunk`` — denylist coverage + negative case
  (b) ``mo_get_epic_patterns`` — multi-epic YAML, ``default:`` block
      is treated as a regular epic (bash quirk — preserved for
      parity), nested-key early-exit
  (c) ``mo_get_epic_symbols_for_file`` — declared symbols + non-
      declared file
  (d) ``mo_symbols_disjoint_for_file`` — disjoint (True), overlapping
      (False), missing yaml (False), one-side-missing (False)
  (e) ``mo_check_scope_overlap`` happy path — non-overlapping
      patterns, rc=0, no JSON, no stderr
  (f) ``mo_check_scope_overlap`` shared-trunk overlap — two epics
      claim ``.gitignore`` (shared-trunk), rc=1, JSON written with
      the byte-exact pairs+partitions shape, specific stderr
  (g) ``mo_check_scope_overlap`` epic-private overlap — two epics
      claim a non-shared-trunk file (``lib/scope-overlap.sh``),
      rc=0, no JSON, WARN-to-stderr
  (h) ``mo_check_scope_overlap`` symbol-disjoint downgrade — two
      epics claim ``.gitignore`` with disjoint symbol sets, rc=0,
      downgrade WARN emitted, no JSON (SERIALIZE downgraded to WARN)
  (i) ``compute_partitions`` — union-find on a synthetic pair list,
      multi-line JSON output byte-equal with bash
  (j) ``mo_partition_count`` — read the JSON, return the right
      partition count; missing/malformed file returns 1

Tolerance notes:
  * Bash JSON output contains literal newlines between partition
    array elements (``[["A"]\\n,["B"]\\n]``) — these are valid JSON
    whitespace; the Python port reproduces them byte-for-byte.
  * Stderr strings are compared byte-for-byte; em-dash (``—``) and
    ``↔`` are UTF-8 literal characters — UTF-8 mode is the default
    on both sides.
  * File lists inside each pair are in sorted order (the result of
    ``comm -12`` on pre-sorted input); the Python port must sort.
  * Outer partition order is sorted by root key; inner lists sorted
    alphabetically.
  * 1e-6 float tolerance per the kickoff — this module has no
    floats; the contract is identical.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from mini_ork.ported import scope_overlap as py  # noqa: E402

SH = REPO_ROOT / "lib" / "scope-overlap.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Tool / fixture availability
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "git", "jq"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not SH.exists():
        pytest.skip(f"missing lib/scope-overlap.sh at {SH}")


def _run_bash(script: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``bash -c <script>`` with the live ``lib/scope-overlap.sh``
    on PATH via the parent's source command. Env is inherited plus
    the overrides in ``env_extra`` (REPO_ROOT, MINI_ORK_HOME,
    JOB_RUN_DIR, EPICS).
    """
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True,
    )


def _bash_is_shared_trunk(file: str) -> bool:
    """Invoke live ``mo_is_shared_trunk`` and return its exit code as a bool."""
    r = _run_bash(
        f'. "{SH}"\nmo_is_shared_trunk "{file}"\necho "RC=$?"\n'
    )
    assert r.returncode == 0, f"bash failed: rc={r.returncode} stderr={r.stderr}"
    last = r.stdout.rstrip().splitlines()[-1]
    return last == "RC=0"


def _bash_get_epic_patterns(epic: str, yaml_path: str) -> list[str]:
    r = _run_bash(
        f'. "{SH}"\nmo_get_epic_patterns "{epic}" "{yaml_path}"\n'
    )
    assert r.returncode == 0, f"bash failed: stderr={r.stderr}"
    return [ln for ln in r.stdout.splitlines() if ln != ""]


def _bash_get_epic_symbols(epic: str, file: str, yaml_path: str) -> list[str]:
    r = _run_bash(
        f'. "{SH}"\nmo_get_epic_symbols_for_file "{epic}" "{file}" "{yaml_path}"\n'
    )
    assert r.returncode == 0, f"bash failed: stderr={r.stderr}"
    return [ln for ln in r.stdout.splitlines() if ln != ""]


def _bash_check_scope_overlap(
    epics: list[str],
    repo_root: str,
    mini_ork_home: str,
    job_run_dir: str,
    serialize_on_overlap: int | None = None,
) -> subprocess.CompletedProcess:
    """Invoke live ``mo_check_scope_overlap`` and return the bash
    process result. The function's exit code is captured into
    stdout as ``RC=N`` (the subprocess's own returncode reflects the
    last ``echo`` which is always 0)."""
    epic_assign = "EPICS=(" + " ".join(f'"{e}"' for e in epics) + ")"
    serialize_assign = (
        f'export MO_SERIALIZE_ON_OVERLAP={serialize_on_overlap}\n'
        if serialize_on_overlap is not None else ""
    )
    script = (
        f'. "{SH}"\n'
        f'export REPO_ROOT="{repo_root}"\n'
        f'export MINI_ORK_HOME="{mini_ork_home}"\n'
        f'export JOB_RUN_DIR="{job_run_dir}"\n'
        f'{serialize_assign}'
        f'{epic_assign}\n'
        f'mo_check_scope_overlap\n'
        f'echo "RC=$?"\n'
    )
    return _run_bash(script)


def _parse_rc_from_stdout(stdout: str) -> int:
    """Extract the ``RC=N`` line from bash output (function's exit
    code, NOT the subprocess's returncode which is always 0 from the
    trailing ``echo``)."""
    last = stdout.rstrip().splitlines()[-1]
    assert last.startswith("RC="), f"unexpected stdout tail: {last!r}"
    return int(last[3:])


def _bash_compute_partitions(pairs_json_inner: str, epics: list[str]) -> str:
    """Run live ``_mo_compute_partitions`` (internal, exposed by
    sourcing) and return its raw stdout."""
    epic_args = " ".join(f'"{e}"' for e in epics)
    # Escape any embedded " in pairs_json_inner for bash double-quote.
    inner = pairs_json_inner.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'. "{SH}"\n'
        f'_mo_compute_partitions "{inner}" {epic_args}\n'
    )
    r = _run_bash(script)
    assert r.returncode == 0, f"bash failed: stderr={r.stderr}"
    return r.stdout


def _bash_partition_count(job_run_dir: str) -> int:
    r = _run_bash(
        f'. "{SH}"\n'
        f'export JOB_RUN_DIR="{job_run_dir}"\n'
        f'mo_partition_count\n'
    )
    assert r.returncode == 0, f"bash failed: stderr={r.stderr}"
    return int(r.stdout.strip())


def _write_yaml(home: Path, body: str) -> Path:
    """Stage a synthetic scope-patterns.yaml under ``home/config/``."""
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    yaml_path = cfg / "scope-patterns.yaml"
    yaml_path.write_text(body, encoding="utf-8")
    return yaml_path


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
def test_is_shared_trunk_table_coverage(file_, expected):
    """Every denylist pattern in lib/scope-overlap.sh (and the default
    fall-through) must agree between bash and Python."""
    _which_tools()
    assert _bash_is_shared_trunk(file_) == expected
    assert py.mo_is_shared_trunk(file_) == expected


# ─────────────────────────────────────────────────────────────────────────────
# (b) mo_get_epic_patterns — multi-epic YAML, default + nested-key exits
# ─────────────────────────────────────────────────────────────────────────────
def test_get_epic_patterns_multi_epic_yaml(tmp_path):
    """Three epics, each with a different number of patterns. The
    ``default:`` block is itself queryable as an epic-id (bash quirk
    — preserved for parity). Nested non-pattern keys (e.g.
    ``shared_trunk_symbols:``) must end the patterns block."""
    _which_tools()
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
    for epic in ("EPIC_A", "EPIC_B", "default", "EPIC_C", "EPIC_MISSING"):
        bash_lines = _bash_get_epic_patterns(epic, str(yaml_path))
        py_lines = py.mo_get_epic_patterns(epic, yaml_path)
        assert py_lines == bash_lines, (
            f"[{epic}] parity drift: bash={bash_lines!r} py={py_lines!r}"
        )

    # Spot-check EPIC_A returns the two patterns (the underscored
    # ``shared_trunk_symbols:`` block follows; bash's awk has a quirk
    # where the patterns block doesn't exit on underscored keys, so
    # the nested lines are emitted too — preserved for byte-equal
    # parity). The parity loop above already verified bash == py on
    # all five lines; this just pins the first two.
    a_bash = _bash_get_epic_patterns("EPIC_A", str(yaml_path))
    a_py = py.mo_get_epic_patterns("EPIC_A", yaml_path)
    assert a_bash[:2] == ["lib/scope-overlap.sh", "lib/active_state_index.sh"]
    assert a_py[:2] == ["lib/scope-overlap.sh", "lib/active_state_index.sh"]
    assert a_bash == a_py  # full parity (5 lines each)

    # Spot-check default: is queryable (bash quirk — preserved).
    d_bash = _bash_get_epic_patterns("default", str(yaml_path))
    d_py = py.mo_get_epic_patterns("default", yaml_path)
    assert d_bash == d_py == ["lib/x.sh"]


# ─────────────────────────────────────────────────────────────────────────────
# (c) mo_get_epic_symbols_for_file — declared + non-declared
# ─────────────────────────────────────────────────────────────────────────────
def test_get_epic_symbols_declared_and_missing(tmp_path):
    """Symbols declared for a specific file come back in declaration
    order; symbols for a non-declared file return empty (both sides).
    A file that has no symbol block returns empty."""
    _which_tools()
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
        bash_lines = _bash_get_epic_symbols(epic, file_, str(yaml_path))
        py_lines = py.mo_get_epic_symbols_for_file(epic, file_, yaml_path)
        assert py_lines == bash_lines, (
            f"[{epic}/{file_}] bash={bash_lines!r} py={py_lines!r}"
        )
        assert py_lines == expected, (
            f"[{epic}/{file_}] expected={expected!r} got={py_lines!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (d) mo_symbols_disjoint_for_file — disjoint, overlap, missing yaml
# ─────────────────────────────────────────────────────────────────────────────
def test_symbols_disjoint_full_matrix(tmp_path):
    """The four cases:
       (1) disjoint sets (True) — downgrade safe
       (2) overlapping sets (False) — serialize
       (3) one side missing symbols (False) — fall back to serialize
       (4) yaml missing entirely (False) — fall back to serialize
    """
    _which_tools()
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
    a_bash = _bash_symbols_disjoint_with(file_, "EPIC_A", "EPIC_B", str(yaml_path))
    a_py = py.mo_symbols_disjoint_for_file(file_, "EPIC_A", "EPIC_B", yaml_path=yaml_path)
    assert a_bash is True, f"A vs B bash: {a_bash}"
    assert a_py is True, f"A vs B py: {a_py}"

    # (2) overlapping: C vs D share SHARED → False
    b_bash = _bash_symbols_disjoint_with(file_, "EPIC_C", "EPIC_D", str(yaml_path))
    b_py = py.mo_symbols_disjoint_for_file(file_, "EPIC_C", "EPIC_D", yaml_path=yaml_path)
    assert b_bash is False
    assert b_py is False

    # (3) one side missing symbols: A vs EPIC_NO_SYMBOLS → False
    c_bash = _bash_symbols_disjoint_with(file_, "EPIC_A", "EPIC_NO_SYMBOLS", str(yaml_path))
    c_py = py.mo_symbols_disjoint_for_file(file_, "EPIC_A", "EPIC_NO_SYMBOLS", yaml_path=yaml_path)
    assert c_bash is False
    assert c_py is False

    # (4) yaml missing: bogus path → both return False. We use a
    # completely separate dir (not under tmp_path) so the parent
    # chain doesn't accidentally resolve to the test's own yaml
    # file.
    bogus_home = tmp_path / "totally_unrelated_dir"
    bogus_home.mkdir()
    bogus = bogus_home / "config" / "scope-patterns.yaml"
    d_bash = _bash_symbols_disjoint_with(file_, "EPIC_A", "EPIC_B", str(bogus))
    d_py = py.mo_symbols_disjoint_for_file(file_, "EPIC_A", "EPIC_B", yaml_path=bogus)
    assert d_bash is False
    assert d_py is False


def _bash_symbols_disjoint_with(file_: str, a: str, b: str, yaml_path: str) -> bool:
    r = _run_bash(
        f'. "{SH}"\n'
        f'export MINI_ORK_HOME="{Path(yaml_path).parent.parent}"\n'
        f'mo_symbols_disjoint_for_file "{file_}" "{a}" "{b}"\n'
        f'echo "RC=$?"\n'
    )
    assert r.returncode == 0, f"bash failed: stderr={r.stderr}"
    last = r.stdout.rstrip().splitlines()[-1]
    return last == "RC=0"


# ─────────────────────────────────────────────────────────────────────────────
# (e) mo_check_scope_overlap — happy path (no overlap)
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_happy_path(tmp_path):
    """Two epics with non-overlapping patterns → no overlap, rc=0,
    no JSON written, no stderr."""
    _which_tools()
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
    bash_r = _bash_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
    )
    assert _parse_rc_from_stdout(bash_r.stdout) == 0, f"bash failed: stderr={bash_r.stderr}"
    assert bash_r.stderr == "", f"unexpected stderr: {bash_r.stderr!r}"
    assert not (run_dir / "scope-overlap.json").exists()

    py_rc, py_payload = py.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
    )
    assert py_rc == 0
    assert py_payload == {"pairs": [], "partitions": []}
    assert not (run_dir / "scope-overlap.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# (f) mo_check_scope_overlap — shared-trunk overlap
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_shared_trunk(tmp_path):
    """Two epics both claim ``.gitignore`` (shared-trunk) → rc=1,
    JSON written with exact pairs+partitions shape, two stderr
    lines (SCOPE OVERLAP + SERIALIZE recommendation)."""
    _which_tools()
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
    bash_r = _bash_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
    )
    assert _parse_rc_from_stdout(bash_r.stdout) == 1, (
        f"expected rc=1, got rc={_parse_rc_from_stdout(bash_r.stdout)}, "
        f"stderr={bash_r.stderr!r}"
    )

    py_rc, py_payload = py.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
    )
    assert py_rc == 1, f"py rc={py_rc}, payload={py_payload}"
    assert py_payload["pairs"] == [
        {"a": "EPIC_A", "b": "EPIC_B", "files": [".gitignore"]}
    ]
    assert py_payload["partitions"] == [["EPIC_A", "EPIC_B"]]

    # JSON file byte-equal with bash
    overlap_json = run_dir / "scope-overlap.json"
    assert overlap_json.exists(), "py did not write scope-overlap.json"
    py_file = overlap_json.read_text(encoding="utf-8")

    # Re-run bash so its file is fresh; compare files byte-equal.
    run_dir_bash = tmp_path / "run_bash"
    run_dir_bash.mkdir()
    bash_r2 = _bash_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir_bash),
    )
    assert _parse_rc_from_stdout(bash_r2.stdout) == 1
    bash_file = (run_dir_bash / "scope-overlap.json").read_text(encoding="utf-8")
    assert py_file == bash_file, (
        f"JSON file drift:\n  bash={bash_file!r}\n  py  ={py_file!r}"
    )

    # Stderr: both must contain both WARN lines.
    expected_stderr_lines = [
        "[mini-ork] SCOPE OVERLAP (shared-trunk): EPIC_A ↔ EPIC_B — 1 file(s)",
        "[mini-ork] SERIALIZE recommendation: shared-trunk overlap → 1 partition(s) across 2 epic(s)",
    ]
    for line in expected_stderr_lines:
        assert line in bash_r.stderr, f"bash stderr missing: {line!r}\n  full={bash_r.stderr!r}"
    # Py emits to sys.stderr — capture via a redirected run.
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        py.mo_check_scope_overlap(
            ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
        )
    py_stderr = buf.getvalue()
    for line in expected_stderr_lines:
        assert line in py_stderr, f"py stderr missing: {line!r}\n  full={py_stderr!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (g) mo_check_scope_overlap — epic-private overlap
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_epic_private(tmp_path):
    """Two epics both claim a non-shared-trunk file
    (``lib/scope-overlap.sh``) → rc=0, no JSON, WARN-to-stderr."""
    _which_tools()
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

    bash_r = _bash_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
    )
    assert _parse_rc_from_stdout(bash_r.stdout) == 0, f"bash rc={_parse_rc_from_stdout(bash_r.stdout)}, stderr={bash_r.stderr}"
    expected_warn = (
        "[mini-ork] SCOPE OVERLAP (epic-private): EPIC_A ↔ EPIC_B — "
        "1 file(s) [WARN, proceeding parallel]"
    )
    assert expected_warn in bash_r.stderr, (
        f"bash stderr missing epic-private WARN: {bash_r.stderr!r}"
    )
    assert not (run_dir / "scope-overlap.json").exists()

    py_rc, py_payload = py.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
    )
    assert py_rc == 0
    assert py_payload == {"pairs": [], "partitions": []}
    assert not (run_dir / "scope-overlap.json").exists()

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        py.mo_check_scope_overlap(
            ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
        )
    assert expected_warn in buf.getvalue(), (
        f"py stderr missing: {buf.getvalue()!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (h) mo_check_scope_overlap — symbol-disjoint downgrade
# ─────────────────────────────────────────────────────────────────────────────
def test_check_scope_overlap_symbol_disjoint_downgrade(tmp_path):
    """Two epics claim a shared-trunk file (``lib/scope-overlap.sh``
    is private though — use ``.gitignore`` which IS shared-trunk)
    with DISJOINT symbol sets → downgrade WARN, rc=0, no JSON."""
    _which_tools()
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

    bash_r = _bash_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
    )
    assert _parse_rc_from_stdout(bash_r.stdout) == 0, f"bash rc={_parse_rc_from_stdout(bash_r.stdout)}, stderr={bash_r.stderr}"
    expected_warn = (
        "[mini-ork] SCOPE OVERLAP (symbol-disjoint downgrade): "
        "EPIC_A ↔ EPIC_B — 1 file(s) [WARN, declared symbols disjoint]"
    )
    assert expected_warn in bash_r.stderr, (
        f"bash stderr missing downgrade WARN: {bash_r.stderr!r}"
    )
    # No shared-trunk overlap, no JSON written.
    assert "SCOPE OVERLAP (shared-trunk)" not in bash_r.stderr
    assert not (run_dir / "scope-overlap.json").exists()

    py_rc, py_payload = py.mo_check_scope_overlap(
        ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
    )
    assert py_rc == 0
    assert py_payload == {"pairs": [], "partitions": []}
    assert not (run_dir / "scope-overlap.json").exists()

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        py.mo_check_scope_overlap(
            ["EPIC_A", "EPIC_B"], str(REPO_ROOT), str(home), str(run_dir),
        )
    py_stderr = buf.getvalue()
    assert expected_warn in py_stderr, f"py stderr: {py_stderr!r}"
    assert "SCOPE OVERLAP (shared-trunk)" not in py_stderr


# ─────────────────────────────────────────────────────────────────────────────
# (i) compute_partitions — union-find byte-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_compute_partitions_byte_equal():
    """Two disjoint pairs (X,Y) and (Z,W) → 2 partitions. Three
    overlapping pairs form one 3-way partition. The bash output is
    multi-line; the Python port must produce the exact same bytes."""
    _which_tools()

    # Case 1: two disjoint pairs → 2 partitions: {W,Z} and {X,Y}
    # Union-find layout: union(X,Y)→Y; union(Z,W)→W. Then
    # groups[Y]=[X,Y], groups[W]=[W,Z]. Sorted keys: W, Y.
    pairs_inner_1 = (
        '{"a":"X","b":"Y","files":["a"]},'
        '{"a":"Z","b":"W","files":["b"]}'
    )
    epics_1 = ["X", "Y", "Z", "W"]
    bash_out_1 = _bash_compute_partitions(pairs_inner_1, epics_1)
    py_out_1 = py.compute_partitions(pairs_inner_1, epics_1)
    assert py_out_1 == bash_out_1, (
        f"[disjoint pairs] partition drift:\n"
        f"  bash={bash_out_1!r}\n  py  ={py_out_1!r}"
    )
    # Sanity: output is valid JSON (newlines are legal JSON
    # whitespace); partitions are sorted-within-group and outer
    # key is sorted (W before X).
    parsed = json.loads(py_out_1)
    assert parsed == [["W", "Z"], ["X", "Y"]], parsed

    # Case 2: 3-way chain (X,Y) + (Y,Z) → {"X","Y","Z"} + {"W"}
    pairs_inner_2 = (
        '{"a":"X","b":"Y","files":["a"]},'
        '{"a":"Y","b":"Z","files":["b"]}'
    )
    epics_2 = ["X", "Y", "Z", "W"]
    bash_out_2 = _bash_compute_partitions(pairs_inner_2, epics_2)
    py_out_2 = py.compute_partitions(pairs_inner_2, epics_2)
    assert py_out_2 == bash_out_2, (
        f"[chain pairs] partition drift:\n"
        f"  bash={bash_out_2!r}\n  py  ={py_out_2!r}"
    )

    # Case 3: no pairs (empty inner) → 4 singletons in input order,
    # sorted by key for the outer (W, X, Y, Z).
    pairs_inner_3 = ""
    epics_3 = ["X", "Y", "Z", "W"]
    bash_out_3 = _bash_compute_partitions(pairs_inner_3, epics_3)
    py_out_3 = py.compute_partitions(pairs_inner_3, epics_3)
    assert py_out_3 == bash_out_3, (
        f"[empty pairs] drift:\n  bash={bash_out_3!r}\n  py  ={py_out_3!r}"
    )
    parsed3 = json.loads(py_out_3)
    assert parsed3 == [["W"], ["X"], ["Y"], ["Z"]], parsed3

    # Case 4: one pair (A,B) → 1 partition
    pairs_inner_4 = '{"a":"A","b":"B","files":["x"]}'
    epics_4 = ["A", "B", "C", "D"]
    bash_out_4 = _bash_compute_partitions(pairs_inner_4, epics_4)
    py_out_4 = py.compute_partitions(pairs_inner_4, epics_4)
    assert py_out_4 == bash_out_4, (
        f"[one pair] drift:\n  bash={bash_out_4!r}\n  py  ={py_out_4!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (j) mo_partition_count — read JSON
# ─────────────────────────────────────────────────────────────────────────────
def test_partition_count_reads_json(tmp_path):
    """``mo_partition_count`` returns the right partition count from
    a synthetic ``scope-overlap.json``. Missing file → 1. Length
    0 → 1 (defensive floor). Length >= 1 → that number."""
    _which_tools()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Case 1: missing file → 1
    assert py.mo_partition_count(str(run_dir)) == 1
    assert _bash_partition_count(str(run_dir)) == 1

    # Case 2: write 3 partitions → 3 (python); bash via subprocess
    # reads the same file → 3
    overlap_json = run_dir / "scope-overlap.json"
    overlap_json.write_text(
        '{"pairs":[],"partitions":[["A","B"],["C"],["D","E","F"]]}\n',
        encoding="utf-8",
    )
    assert py.mo_partition_count(str(run_dir)) == 3
    assert _bash_partition_count(str(run_dir)) == 3

    # Case 3: empty partitions list → 1 (defensive floor)
    overlap_json.write_text('{"pairs":[],"partitions":[]}\n', encoding="utf-8")
    assert py.mo_partition_count(str(run_dir)) == 1
    assert _bash_partition_count(str(run_dir)) == 1

    # Case 4: malformed JSON → 1
    overlap_json.write_text('not json\n', encoding="utf-8")
    assert py.mo_partition_count(str(run_dir)) == 1
    assert _bash_partition_count(str(run_dir)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Smoke: module imports + public API exists + bash source untouched
# ─────────────────────────────────────────────────────────────────────────────
def test_module_imports_and_api():
    """Module imports cleanly; every function in ``__all__`` is callable."""
    import mini_ork.ported.scope_overlap as mod
    for name in mod.__all__:
        fn = getattr(mod, name)
        assert callable(fn), f"{name} not callable"
    assert mod.mo_is_shared_trunk.__name__ == "mo_is_shared_trunk"
    assert mod.compute_partitions.__name__ == "compute_partitions"


def test_bash_source_untouched():
    """The strangler-fig KEEP: ``lib/scope-overlap.sh`` is byte-identical
    to its pre-test state. `git diff` should show no changes to it."""
    _which_tools()
    r = subprocess.run(
        ["git", "diff", "--stat", "lib/scope-overlap.sh"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    # When clean, `git diff --stat` is empty (no output).
    assert r.stdout.strip() == "", (
        f"lib/scope-overlap.sh was modified:\n{r.stdout}"
    )
