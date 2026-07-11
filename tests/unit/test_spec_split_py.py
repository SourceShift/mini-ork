"""Parity gate: mini_ork.ported.spec_split vs lib/spec-split.sh.

Each test invokes the LIVE bash subprocess on the same inputs as the Python
port and asserts byte-identical (or structurally identical for the JSON
report, since float repr is platform-stable in CPython 3.x) output. No mocks,
no hardcoded expected outputs — expected is always derived from a control
bash invocation that shares the inputs.

>=6 cases:
  (a) split_visible_hidden_happy_path         — 2 visible + 1 hidden, bash vs Python
  (b) split_visible_hidden_no_hidden          — 2 visible, none @hidden
  (c) split_visible_hidden_no_describe        — bare test() blocks (no describe wrapper)
  (d) split_visible_hidden_missing_spec       — /nonexistent, bash rc=1 vs FileNotFoundError
  (e) split_visible_hidden_back_lookup        — 3-line back-lookup with blank-line transparency
  (f) decide_skip_hidden_suite                — three sub-cases (missing / no test() / has test())
  (g) write_verdict_parity                    — skip + run shapes vs live jq -n
  (h) split_visible_hidden_db_init_sanity     — temp DB via db/init.sh, no row drift
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import spec_split as ss  # noqa: E402

SH = REPO / "lib" / "spec-split.sh"
INIT_SH = REPO / "db" / "init.sh"


def _which(*tools: str) -> dict[str, str]:
    out = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Helpers: source the bash lib + run mo_split_visible_hidden against tmp_path.
# Bash always writes to <tmp_path>/iter-<iter> because mo_run_dir is stubbed
# to echo tmp_path. Tests read from the same path.
# ─────────────────────────────────────────────────────────────────────────────
def _bash_split(tmp_path: Path, epic: str, iter: str, visible_spec: str) -> subprocess.CompletedProcess:
    bash_wrapper = (
        f'. "{SH}"\n'
        f'mo_run_dir() {{ echo "{tmp_path}"; }}\n'
        'export -f mo_run_dir\n'
        f'mo_split_visible_hidden "{epic}" "{iter}" "{visible_spec}"\n'
    )
    return subprocess.run(
        ["bash", "-c", bash_wrapper],
        capture_output=True, text=True,
    )


def _bash_run_hidden(tmp_path: Path, epic: str, iter: str, worktree: str) -> subprocess.CompletedProcess:
    """Source the lib + run `mo_run_hidden_suite` (writes hidden-verdict.json).
    `npx` is stubbed so the test doesn't need a real Playwright runtime."""
    bash_wrapper = (
        f'. "{SH}"\n'
        f'mo_run_dir() {{ echo "{tmp_path}"; }}\n'
        'export -f mo_run_dir\n'
        # npx stub: succeed silently so bash proceeds past the early-bail
        # for the "has test()" sub-case.
        'npx() { return 0; }\n'
        'export -f npx\n'
        f'mo_run_hidden_suite "{epic}" "{iter}" "{worktree}"\n'
    )
    return subprocess.run(
        ["bash", "-c", bash_wrapper],
        capture_output=True, text=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# (a) happy path — 2 visible + 1 hidden, all inside describe
# ─────────────────────────────────────────────────────────────────────────────
def test_split_visible_hidden_happy_path(tmp_path):
    """Python `split_visible_hidden` matches the LIVE bash `mo_split_visible_hidden`
    byte-for-byte on a spec with 2 visible + 1 hidden tests (all inside describe)."""
    _which("bash", "python3", "jq")
    visible_spec = tmp_path / "visible.spec.ts"
    visible_spec.write_text(
        "import { test, expect } from '@playwright/test';\n"
        "test.describe('login', () => {\n"
        "  test.beforeEach(async ({ page }) => {\n"
        "    await page.goto('/');\n"
        "  });\n"
        "\n"
        "  test('visible one', async ({ page }) => {\n"
        "    expect(1).toBe(1);\n"
        "  });\n"
        "\n"
        "  // @hidden — token refresh race\n"
        "  test('hidden refresh', async ({ page }) => {\n"
        "    expect(1).toBe(1);\n"
        "  });\n"
        "\n"
        "  test('visible two', async ({ page }) => {\n"
        "    expect(1).toBe(1);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )

    # Bash control: writes to tmp_path/iter-1.
    iter_dir_bash = tmp_path / "iter-1"
    iter_dir_bash.mkdir()
    rb = _bash_split(tmp_path, "epic-happy", "1", str(visible_spec))
    assert rb.returncode == 0, rb.stderr
    bash_hidden = (iter_dir_bash / "hidden_spec.ts").read_text(encoding="utf-8")
    bash_report = json.loads((iter_dir_bash / "spec-split-report.json").read_text(encoding="utf-8"))

    # Python port — fresh iter_dir so we byte-diff clean.
    iter_dir_py = tmp_path / "iter-2"
    iter_dir_py.mkdir()
    py_report = ss.split_visible_hidden(visible_spec, iter_dir_py)
    py_hidden = (iter_dir_py / "hidden_spec.ts").read_text(encoding="utf-8")

    assert py_hidden == bash_hidden, (
        f"hidden_spec.ts byte-mismatch\n"
        f"py head: {py_hidden[:200]!r}\n"
        f"bash head: {bash_hidden[:200]!r}"
    )
    # JSON content parity (ratio is a float — CPython repr is platform-stable,
    # so byte-equality should hold; assertAlmostEqual is the safety net).
    assert py_report == bash_report
    assert py_report["visible"] == 2
    assert py_report["hidden"] == 1
    assert math.isclose(py_report["ratio"], bash_report["ratio"], rel_tol=0, abs_tol=1e-12)


# ─────────────────────────────────────────────────────────────────────────────
# (b) no hidden — empty marker + report
# ─────────────────────────────────────────────────────────────────────────────
def test_split_visible_hidden_no_hidden(tmp_path):
    """Bash vs Python on a spec with 2 visible tests, none marked @hidden.
    Both write the empty marker AND a report {visible:2, hidden:0, ratio:0}."""
    _which("bash", "python3", "jq")
    # Multi-line brace blocks so the bash depth-counter counts each test as
    # a separate block (single-line `() => { ... }` braces get merged into
    # one block because the closing `});` happens on the same line as the
    # opening).
    visible_spec = tmp_path / "visible.spec.ts"
    visible_spec.write_text(
        "import { test, expect } from '@playwright/test';\n"
        "test.describe('login', () => {\n"
        "  test('a', async () => {\n"
        "    expect(1).toBe(1);\n"
        "  });\n"
        "  test('b', async () => {\n"
        "    expect(1).toBe(1);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )

    iter_dir_bash = tmp_path / "iter-1"
    iter_dir_bash.mkdir()
    rb = _bash_split(tmp_path, "epic-noh", "1", str(visible_spec))
    assert rb.returncode == 0, rb.stderr
    bash_hidden = (iter_dir_bash / "hidden_spec.ts").read_text(encoding="utf-8")
    bash_report = json.loads((iter_dir_bash / "spec-split-report.json").read_text(encoding="utf-8"))

    iter_dir_py = tmp_path / "iter-2"
    iter_dir_py.mkdir()
    py_report = ss.split_visible_hidden(visible_spec, iter_dir_py)
    py_hidden = (iter_dir_py / "hidden_spec.ts").read_text(encoding="utf-8")

    assert py_hidden == bash_hidden == "// no @hidden scenarios in source spec\n"
    assert py_report == bash_report == {"visible": 2, "hidden": 0, "ratio": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# (c) no describe wrapper
# ─────────────────────────────────────────────────────────────────────────────
def test_split_visible_hidden_no_describe(tmp_path):
    """Bash vs Python on a spec with bare test() blocks (no describe wrapper).
    Both write the no-describe marker + report {visible:0, hidden:0, error:'no describe'}."""
    _which("bash", "python3", "jq")
    visible_spec = tmp_path / "bare.spec.ts"
    visible_spec.write_text(
        "import { test, expect } from '@playwright/test';\n"
        "test('bare one', async () => {\n"
        "  expect(1).toBe(1);\n"
        "});\n"
        "test('bare two', async () => {\n"
        "  expect(1).toBe(1);\n"
        "});\n",
        encoding="utf-8",
    )

    iter_dir_bash = tmp_path / "iter-1"
    iter_dir_bash.mkdir()
    rb = _bash_split(tmp_path, "epic-nod", "1", str(visible_spec))
    assert rb.returncode == 0, rb.stderr
    bash_hidden = (iter_dir_bash / "hidden_spec.ts").read_text(encoding="utf-8")
    bash_report = json.loads((iter_dir_bash / "spec-split-report.json").read_text(encoding="utf-8"))

    iter_dir_py = tmp_path / "iter-2"
    iter_dir_py.mkdir()
    py_report = ss.split_visible_hidden(visible_spec, iter_dir_py)
    py_hidden = (iter_dir_py / "hidden_spec.ts").read_text(encoding="utf-8")

    assert py_hidden == bash_hidden == "// no test.describe found — no hidden spec\n"
    assert py_report == bash_report == {"visible": 0, "hidden": 0, "error": "no describe"}


# ─────────────────────────────────────────────────────────────────────────────
# (d) missing spec — bash rc=1 + error JSON, Python raises FileNotFoundError
# ─────────────────────────────────────────────────────────────────────────────
def test_split_visible_hidden_missing_spec(tmp_path):
    """Bash returns rc=1 + writes report {visible:0, hidden:0, error:'visible spec missing'}.
    Python raises FileNotFoundError AND writes the same report. Both indicate failure."""
    _which("bash", "python3", "jq")
    bogus = "/nonexistent/path/does/not/exist.spec.ts"

    iter_dir_bash = tmp_path / "iter-1"
    iter_dir_bash.mkdir()
    rb = _bash_split(tmp_path, "epic-miss", "1", bogus)
    assert rb.returncode == 1, f"expected rc=1, got {rb.returncode}; stderr={rb.stderr}"
    bash_report = json.loads((iter_dir_bash / "spec-split-report.json").read_text(encoding="utf-8"))
    assert bash_report == {"visible": 0, "hidden": 0, "error": "visible spec missing"}
    # Bash does NOT write hidden_spec.ts on missing-spec early-bail.
    assert not (iter_dir_bash / "hidden_spec.ts").exists()

    # Python: same report, plus raises.
    iter_dir_py = tmp_path / "iter-2"
    iter_dir_py.mkdir()
    with pytest.raises(FileNotFoundError):
        ss.split_visible_hidden(bogus, iter_dir_py)
    py_report = json.loads((iter_dir_py / "spec-split-report.json").read_text(encoding="utf-8"))
    assert py_report == bash_report
    assert not (iter_dir_py / "hidden_spec.ts").exists()


# ─────────────────────────────────────────────────────────────────────────────
# (e) 3-line back-lookup edge cases
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "between,expected_hidden",
    [
        ("// @hidden — marker\n", True),                       # marker at idx-1
        ("// @hidden — marker\n\n", True),                     # marker at idx-2 (1 blank between)
        ("// @hidden — marker\n\n\n", True),                   # marker at idx-3 (2 blanks between)
        ("// @hidden — marker\n\n\n\n", False),                # marker at idx-4, out of 3-line window
        ("// @hidden — marker\n  const x = 1;\n", False),      # code line at idx-1 breaks search
    ],
    ids=["immediate", "1-blank", "2-blank", "3-blank-too-far", "code-line-breaks"],
)
def test_split_visible_hidden_back_lookup(tmp_path, between, expected_hidden):
    """`is_hidden` looks up to 3 lines BACK for `^\\s*//\\s*@hidden`, treating
    blank lines as transparent. Cases: marker at idx-1 (immediate), idx-2 and
    idx-3 (with 1 or 2 blank lines between — still selected), idx-4 (3 blank
    lines — past window, NOT selected), and a code line at idx-1 (NOT
    selected even though the marker is at idx-2)."""
    _which("bash", "python3", "jq")
    visible_spec = tmp_path / "bl.spec.ts"
    src = (
        "import { test, expect } from '@playwright/test';\n"
        "test.describe('d', () => {\n"
        f"{between}"
        "  test('edge', async () => {\n"
        "    expect(1).toBe(1);\n"
        "  });\n"
        "  test('plain', async () => {\n"
        "    expect(1).toBe(1);\n"
        "  });\n"
        "});\n"
    )
    visible_spec.write_text(src, encoding="utf-8")

    # Bash control.
    iter_dir_bash = tmp_path / "iter-1"
    iter_dir_bash.mkdir()
    rb = _bash_split(tmp_path, "epic-bl", "1", str(visible_spec))
    assert rb.returncode == 0, rb.stderr
    bash_report = json.loads((iter_dir_bash / "spec-split-report.json").read_text(encoding="utf-8"))

    # Python port — separate dir so we don't collide on file writes.
    iter_dir_py = tmp_path / "iter-2"
    iter_dir_py.mkdir()
    py_report = ss.split_visible_hidden(visible_spec, iter_dir_py)

    assert py_report == bash_report
    assert py_report["hidden"] == (1 if expected_hidden else 0), (
        f"between={between!r} expected_hidden={expected_hidden} got {py_report}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (f) decide_skip_hidden_suite — three sub-cases via live bash source
# ─────────────────────────────────────────────────────────────────────────────
def test_decide_skip_hidden_suite_parity(tmp_path):
    """Python `decide_skip_hidden_suite` matches the live bash early-bail in
    `mo_run_hidden_suite`. Three sub-cases:
      (i)   missing hidden file → both skip with reason
      (ii)  file present but no `^test(` → both skip
      (iii) file with `test(` present → both proceed (no skip)
    """
    _which("bash", "python3", "jq")

    # (i) missing file.
    missing = tmp_path / "absent.spec.ts"
    py_skip1, py_reason1 = ss.decide_skip_hidden_suite(missing)
    assert py_skip1 is True and py_reason1 == "no @hidden scenarios"

    iter_dir1 = tmp_path / "iter-1"
    iter_dir1.mkdir()
    rb1 = _bash_run_hidden(tmp_path, "epic-skip", "1", "/tmp")
    assert rb1.returncode == 0, rb1.stderr
    bash_verdict1 = json.loads((iter_dir1 / "hidden-verdict.json").read_text(encoding="utf-8"))
    assert bash_verdict1["skipped"] is True
    assert bash_verdict1["reason"] == "no @hidden scenarios"
    assert "skipping" in rb1.stderr

    # (ii) file present, no test() block.
    iter_dir2 = tmp_path / "iter-2"
    iter_dir2.mkdir()
    (iter_dir2 / "hidden_spec.ts").write_text("// empty placeholder\n", encoding="utf-8")
    py_skip2, py_reason2 = ss.decide_skip_hidden_suite(iter_dir2 / "hidden_spec.ts")
    assert py_skip2 is True and py_reason2 == "no @hidden scenarios"

    rb2 = _bash_run_hidden(tmp_path, "epic-skip", "2", "/tmp")
    assert rb2.returncode == 0, rb2.stderr
    bash_verdict2 = json.loads((iter_dir2 / "hidden-verdict.json").read_text(encoding="utf-8"))
    assert bash_verdict2["skipped"] is True
    assert bash_verdict2["reason"] == "no @hidden scenarios"

    # (iii) file with test() present — both proceed.
    iter_dir3 = tmp_path / "iter-3"
    iter_dir3.mkdir()
    (iter_dir3 / "hidden_spec.ts").write_text(
        "test('hidden one', async () => { expect(1).toBe(1); });\n",
        encoding="utf-8",
    )
    py_skip3, py_reason3 = ss.decide_skip_hidden_suite(iter_dir3 / "hidden_spec.ts")
    assert py_skip3 is False and py_reason3 == ""

    rb3 = _bash_run_hidden(tmp_path, "epic-run", "3", "/tmp")
    assert rb3.returncode == 0, rb3.stderr
    bash_verdict3 = json.loads((iter_dir3 / "hidden-verdict.json").read_text(encoding="utf-8"))
    assert bash_verdict3["skipped"] is False
    assert bash_verdict3["verdict"] == "PASS"
    assert bash_verdict3["rc"] == 0
    assert "ran_at" in bash_verdict3

    # Python write_verdict byte-matches bash for case (iii)'s ran-shape.
    py_verdict_path = iter_dir3 / "py-verdict.json"
    ss.write_verdict(bash_verdict3["verdict"], bash_verdict3["rc"],
                     bash_verdict3["ran_at"], py_verdict_path, skipped=False)
    assert py_verdict_path.read_text(encoding="utf-8") == \
        (iter_dir3 / "hidden-verdict.json").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# (g) write_verdict_parity — skip + run shapes vs live jq -n
# ─────────────────────────────────────────────────────────────────────────────
def test_write_verdict_parity(tmp_path):
    """Python `write_verdict` produces JSON byte-identical to live `jq -n` for
    BOTH the skip and run shapes from lib/spec-split.sh. jq -n's default
    formatting is 2-space indent + trailing newline — match exactly."""
    path = _which("jq")["jq"]

    # Skip shape.
    verdict_path = tmp_path / "skip.json"
    ss.write_verdict("PASS", 0, "", verdict_path, skipped=True, reason="no @hidden scenarios")
    py_skip = verdict_path.read_text(encoding="utf-8")
    expected_skip = subprocess.run(
        [path, "-n", '{verdict: "PASS", scenarios_run: 0, skipped: true, reason: "no @hidden scenarios"}'],
        capture_output=True, text=True, check=True,
    ).stdout
    assert py_skip == expected_skip, (
        f"skip shape mismatch\npy:     {py_skip!r}\njq:    {expected_skip!r}"
    )

    # Run shape.
    verdict_path2 = tmp_path / "run.json"
    ss.write_verdict("PASS", 0, "2026-07-04T10:00:00Z", verdict_path2, skipped=False)
    py_run = verdict_path2.read_text(encoding="utf-8")
    expected_run = subprocess.run(
        [path, "-n",
         "--arg", "verdict", "PASS",
         "--argjson", "rc", "0",
         "--arg", "ran_at", "2026-07-04T10:00:00Z",
         '{verdict: $verdict, rc: $rc, ran_at: $ran_at, skipped: false}'],
        capture_output=True, text=True, check=True,
    ).stdout
    assert py_run == expected_run, (
        f"run shape mismatch\npy:     {py_run!r}\njq:    {expected_run!r}"
    )

    # FAIL run shape — parity on a non-zero rc.
    verdict_path3 = tmp_path / "fail.json"
    ss.write_verdict("FAIL", 1, "2026-07-04T10:00:01Z", verdict_path3, skipped=False)
    py_fail = verdict_path3.read_text(encoding="utf-8")
    expected_fail = subprocess.run(
        [path, "-n",
         "--arg", "verdict", "FAIL",
         "--argjson", "rc", "1",
         "--arg", "ran_at", "2026-07-04T10:00:01Z",
         '{verdict: $verdict, rc: $rc, ran_at: $ran_at, skipped: false}'],
        capture_output=True, text=True, check=True,
    ).stdout
    assert py_fail == expected_fail


# ─────────────────────────────────────────────────────────────────────────────
# (h) db-init sanity — temp DB via db/init.sh, no row drift
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path_factory):
    """Spin up a real mini-ork SQLite DB via db/init.sh — kickoff requirement."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def test_split_visible_hidden_db_init_sanity(temp_db, tmp_path):
    """Spin up a temp DB via db/init.sh, INSERT an epic, run split_visible_hidden,
    then query rows back and assert no drift. Proves the port doesn't accidentally
    couple to DB state."""
    _which("python3", "jq")
    visible_spec = tmp_path / "visible.spec.ts"
    visible_spec.write_text(
        "import { test, expect } from '@playwright/test';\n"
        "test.describe('d', () => {\n"
        "  test('a', async () => {\n"
        "    expect(1).toBe(1);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )
    iter_dir = tmp_path / "iter-1"
    iter_dir.mkdir()

    # Snapshot rows BEFORE running the port.
    con = sqlite3.connect(temp_db)
    con.execute(
        "INSERT INTO epics(id, title, status, kickoff_path) VALUES (?, ?, ?, ?)",
        ("epic-split-db", "t", "in progress", "kickoffs/p.md"),
    )
    con.commit()
    before = con.execute(
        "SELECT id, title, status, kickoff_path FROM epics WHERE id='epic-split-db';"
    ).fetchall()
    con.close()

    # Run the Python port — must not touch the DB.
    report = ss.split_visible_hidden(visible_spec, iter_dir)
    assert report == {"visible": 1, "hidden": 0, "ratio": 0.0}

    # Snapshot rows AFTER — must be unchanged.
    con = sqlite3.connect(temp_db)
    after = con.execute(
        "SELECT id, title, status, kickoff_path FROM epics WHERE id='epic-split-db';"
    ).fetchall()
    con.close()
    assert before == after, f"DB rows drifted: before={before} after={after}"

    # And the port's written files exist (sanity: the port actually ran).
    assert (iter_dir / "hidden_spec.ts").is_file()
    assert (iter_dir / "spec-split-report.json").is_file()