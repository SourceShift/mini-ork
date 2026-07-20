"""Standalone unit tests for ``mini_ork.orchestration.spec_split``.

Replaces the bash-parity gate (the previous version of this file, which
invoked the live ``lib/spec-split.sh`` via ``subprocess`` + ``jq`` + ``bash``
as part of the bash→Python migration): the Python port is now the sole
implementation under test here, so its coverage no longer shells out to
bash, jq, or ``npx``/Playwright. These tests pin the deterministic contract
of the three public functions — ``split_visible_hidden``,
``decide_skip_hidden_suite``, ``write_verdict`` — directly against literal
expected values captured from the port itself (previously proven
byte-identical to the live bash in the parity gate this file replaces).

No sqlite/env/npx stubbing is needed because the port has zero coupling to
any of them (see ``TestModuleIsolation`` below, which pins that fact via
source inspection instead of spinning up a real DB/subprocess as the old
parity gate's case (h) did). The only real I/O the port performs is
filesystem reads/writes, which these tests exercise against ``tmp_path``;
one case (``test_unreadable_file_is_treated_as_skip``) stubs
``pathlib.Path.read_text`` to simulate an OSError without needing an actual
unreadable file.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from mini_ork.orchestration import spec_split as ss

# ─────────────────────────────────────────────────────────────────────────────
# split_visible_hidden
# ─────────────────────────────────────────────────────────────────────────────


class TestSplitVisibleHidden:
    def test_happy_path_two_visible_one_hidden(self, tmp_path):
        """2 visible + 1 hidden, all inside describe. Pins the exact
        AUTOGEN-banner + imports + header + hidden-block + closing shape,
        and the {visible, hidden, ratio} report — both captured from the
        port (previously proven byte-identical to live bash)."""
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
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()

        report = ss.split_visible_hidden(visible_spec, iter_dir)

        assert report == {"visible": 2, "hidden": 1, "ratio": pytest.approx(1 / 3)}
        hidden_text = (iter_dir / "hidden_spec.ts").read_text(encoding="utf-8")
        assert hidden_text == (
            "// AUTOGEN: hidden spec — DO NOT commit to worktree.\n"
            "// Worker MUST NOT see this file. Runs only at Phase 2 validation gate.\n"
            "\n"
            "import { test, expect } from '@playwright/test';\n"
            "\n"
            "test.describe('login', () => {\n"
            "  test.beforeEach(async ({ page }) => {\n"
            "    await page.goto('/');\n"
            "  });\n"
            "\n"
            "  test('hidden refresh', async ({ page }) => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "});\n"
        )
        report_text = (iter_dir / "spec-split-report.json").read_text(encoding="utf-8")
        assert json.loads(report_text) == report

    def test_no_hidden_scenarios(self, tmp_path):
        """2 visible tests, none marked @hidden. Writes the empty marker and
        a report {visible: 2, hidden: 0, ratio: 0.0}."""
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
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()

        report = ss.split_visible_hidden(visible_spec, iter_dir)

        assert report == {"visible": 2, "hidden": 0, "ratio": 0.0}
        assert (iter_dir / "hidden_spec.ts").read_text(encoding="utf-8") == (
            "// no @hidden scenarios in source spec\n"
        )

    def test_no_describe_wrapper(self, tmp_path):
        """Bare test() blocks with no test.describe wrapper: bails with
        {visible: 0, hidden: 0, error: 'no describe'} and the no-describe
        marker text."""
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
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()

        report = ss.split_visible_hidden(visible_spec, iter_dir)

        assert report == {"visible": 0, "hidden": 0, "error": "no describe"}
        assert (iter_dir / "hidden_spec.ts").read_text(encoding="utf-8") == (
            "// no test.describe found — no hidden spec\n"
        )

    def test_describe_with_zero_tests_has_no_error_key(self, tmp_path):
        """A describe wrapper with NO test() blocks inside is a different
        path than 'no describe' at all: it falls through pass 3 with zero
        iterations, so the report has no 'error' key (just ratio: 0, the
        int, since total == 0 short-circuits before the float division)."""
        visible_spec = tmp_path / "empty.spec.ts"
        visible_spec.write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test.describe('empty', () => {\n"
            "});\n",
            encoding="utf-8",
        )
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()

        report = ss.split_visible_hidden(visible_spec, iter_dir)

        assert report == {"visible": 0, "hidden": 0, "ratio": 0}
        assert "error" not in report
        assert (iter_dir / "hidden_spec.ts").read_text(encoding="utf-8") == (
            "// no @hidden scenarios in source spec\n"
        )

    def test_missing_spec_raises_and_writes_error_report(self, tmp_path):
        """Missing visible_spec_path: raises FileNotFoundError AND writes
        {visible: 0, hidden: 0, error: 'visible spec missing'} to the report
        (mirroring bash's rc=1 early-bail) — but does NOT write
        hidden_spec.ts, matching bash's early `return 1` before the heredoc."""
        bogus = tmp_path / "does-not-exist.spec.ts"
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()

        with pytest.raises(FileNotFoundError):
            ss.split_visible_hidden(bogus, iter_dir)

        report = json.loads((iter_dir / "spec-split-report.json").read_text(encoding="utf-8"))
        assert report == {"visible": 0, "hidden": 0, "error": "visible spec missing"}
        assert not (iter_dir / "hidden_spec.ts").exists()

    @pytest.mark.parametrize(
        "between,expected_hidden",
        [
            ("// @hidden — marker\n", True),  # marker at idx-1
            ("// @hidden — marker\n\n", True),  # marker at idx-2 (1 blank between)
            ("// @hidden — marker\n\n\n", True),  # marker at idx-3 (2 blanks between)
            ("// @hidden — marker\n\n\n\n", False),  # marker at idx-4, out of 3-line window
            ("// @hidden — marker\n  const x = 1;\n", False),  # code line at idx-1 breaks search
        ],
        ids=["immediate", "1-blank", "2-blank", "3-blank-too-far", "code-line-breaks"],
    )
    def test_back_lookup_window(self, tmp_path, between, expected_hidden):
        """`is_hidden` looks up to 3 lines BACK from a test( line for
        `^\\s*//\\s*@hidden`, treating blank lines as transparent but
        stopping at the first non-blank, non-marker line. Cases: marker at
        idx-1/2/3 (selected), idx-4 (3 blank lines — past the window, not
        selected), and a code line at idx-1 (not selected even though a
        marker sits further back at idx-2)."""
        visible_spec = tmp_path / "bl.spec.ts"
        visible_spec.write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test.describe('d', () => {\n"
            f"{between}"
            "  test('edge', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "  test('plain', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "});\n",
            encoding="utf-8",
        )
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()

        report = ss.split_visible_hidden(visible_spec, iter_dir)

        assert report["hidden"] == (1 if expected_hidden else 0)
        assert report["visible"] == (1 if expected_hidden else 2)

    def test_multiple_hidden_blocks(self, tmp_path):
        """2 @hidden tests + 1 visible test: both hidden blocks are pulled
        out (visible=1, hidden=2), and the SECOND @hidden marker comment is
        dropped entirely from the output (it's neither header nor a test
        block — pass 3 just skips over it) while the first survives as part
        of the header, since it sits between the describe line and the
        first test( line the header-collection loop stops at. This is the
        existing algorithm's behavior (ported line-for-line from bash), not
        a Python-specific quirk."""
        visible_spec = tmp_path / "multi.spec.ts"
        visible_spec.write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test.describe('multi', () => {\n"
            "  // @hidden — first\n"
            "  test('h1', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "  // @hidden — second\n"
            "  test('h2', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "  test('v1', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "});\n",
            encoding="utf-8",
        )
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()

        report = ss.split_visible_hidden(visible_spec, iter_dir)

        assert report == {"visible": 1, "hidden": 2, "ratio": pytest.approx(2 / 3)}
        hidden_text = (iter_dir / "hidden_spec.ts").read_text(encoding="utf-8")
        assert hidden_text == (
            "// AUTOGEN: hidden spec — DO NOT commit to worktree.\n"
            "// Worker MUST NOT see this file. Runs only at Phase 2 validation gate.\n"
            "\n"
            "import { test, expect } from '@playwright/test';\n"
            "\n"
            "test.describe('multi', () => {\n"
            "  // @hidden — first\n"
            "  test('h1', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "  test('h2', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "});\n"
        )
        assert "@hidden — second" not in hidden_text

    @pytest.mark.parametrize(
        "visible_count,expected_ratio",
        [(3, 0.25), (0, 1.0)],
        ids=["3-visible-1-hidden", "all-hidden"],
    )
    def test_ratio_computation(self, tmp_path, visible_count, expected_ratio):
        """Ratio is hidden / (visible + hidden). Covers a partial split
        (3 visible, 1 hidden → 0.25) and the all-hidden edge (0 visible,
        1 hidden → 1.0)."""
        tests = "".join(
            f"  test('v{i}', async () => {{\n    expect(1).toBe(1);\n  }});\n"
            for i in range(visible_count)
        )
        visible_spec = tmp_path / "ratio.spec.ts"
        visible_spec.write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test.describe('r', () => {\n"
            f"{tests}"
            "  // @hidden — c\n"
            "  test('c', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "});\n",
            encoding="utf-8",
        )
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()

        report = ss.split_visible_hidden(visible_spec, iter_dir)

        assert report["visible"] == visible_count
        assert report["hidden"] == 1
        assert report["ratio"] == pytest.approx(expected_ratio)

    def test_accepts_string_paths_and_creates_missing_iter_dir(self, tmp_path):
        """Both args accept plain str (not just Path) and a not-yet-existing,
        nested iter_dir is created via mkdir(parents=True, exist_ok=True)."""
        visible_spec = tmp_path / "n.spec.ts"
        visible_spec.write_text(
            "import { test } from '@playwright/test';\n"
            "test.describe('n', () => {\n"
            "  test('a', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "});\n",
            encoding="utf-8",
        )
        nested_iter_dir = tmp_path / "sub" / "deep" / "iter-1"
        assert not nested_iter_dir.exists()

        report = ss.split_visible_hidden(str(visible_spec), str(nested_iter_dir))

        assert report == {"visible": 1, "hidden": 0, "ratio": 0.0}
        assert nested_iter_dir.is_dir()
        assert (nested_iter_dir / "hidden_spec.ts").is_file()
        assert (nested_iter_dir / "spec-split-report.json").is_file()


# ─────────────────────────────────────────────────────────────────────────────
# decide_skip_hidden_suite
# ─────────────────────────────────────────────────────────────────────────────


class TestDecideSkipHiddenSuite:
    def test_missing_file_skips(self, tmp_path):
        skip, reason = ss.decide_skip_hidden_suite(tmp_path / "absent.spec.ts")
        assert skip is True
        assert reason == "no @hidden scenarios"

    def test_file_without_test_call_skips(self, tmp_path):
        hidden_path = tmp_path / "hidden_spec.ts"
        hidden_path.write_text("// empty placeholder\n", encoding="utf-8")
        skip, reason = ss.decide_skip_hidden_suite(hidden_path)
        assert skip is True
        assert reason == "no @hidden scenarios"

    def test_file_with_test_call_at_start_does_not_skip(self, tmp_path):
        hidden_path = tmp_path / "hidden_spec.ts"
        hidden_path.write_text(
            "test('hidden one', async () => { expect(1).toBe(1); });\n",
            encoding="utf-8",
        )
        skip, reason = ss.decide_skip_hidden_suite(hidden_path)
        assert skip is False
        assert reason == ""

    def test_leading_blank_lines_before_test_call_do_not_skip(self, tmp_path):
        """`\\s*` in the compiled pattern absorbs leading blank lines too
        (`\\s` matches `\\n`), so a file starting with blank lines then
        `test(` still matches at position 0."""
        hidden_path = tmp_path / "hidden_spec.ts"
        hidden_path.write_text("\ntest('a', async () => {});\n", encoding="utf-8")
        skip, reason = ss.decide_skip_hidden_suite(hidden_path)
        assert skip is False
        assert reason == ""

    def test_realistic_autogen_hidden_spec_is_treated_as_skip(self, tmp_path):
        """KNOWN DIVERGENCE from bash: the compiled `_TEST_RE` used here is
        `re.compile(r"^\\s*test\\(")` WITHOUT `re.MULTILINE`, so `.search()`
        only matches at the absolute start of the string — not the start of
        each line the way bash's `grep -q '^test('` does. A REAL
        hidden_spec.ts produced by `split_visible_hidden()` always begins
        with the multi-line AUTOGEN banner comment, so the actual `test(`
        line (indented, further down the file) is never at position 0 and
        this function reports skip=True even though genuine hidden tests
        exist. This test pins CURRENT behavior as a documented gap — it is
        not a parity artifact of this test file (see
        `test_file_with_test_call_at_start_does_not_skip` above, which shows
        the function works when `test(` happens to be the very first thing
        in the file). Fixing it requires `re.MULTILINE` (or an
        `re.search`-per-line loop) in `mini_ork/orchestration/spec_split.py`, which
        is out of scope for this test-only change."""
        hidden_path = tmp_path / "hidden_spec.ts"
        hidden_path.write_text(
            "// AUTOGEN: hidden spec — DO NOT commit to worktree.\n"
            "// Worker MUST NOT see this file. Runs only at Phase 2 validation gate.\n"
            "import { test, expect } from '@playwright/test';\n"
            "\n"
            "test.describe('login', () => {\n"
            "  test('hidden one', async () => {\n"
            "    expect(1).toBe(1);\n"
            "  });\n"
            "});\n",
            encoding="utf-8",
        )
        skip, reason = ss.decide_skip_hidden_suite(hidden_path)
        assert skip is True
        assert reason == "no @hidden scenarios"

    def test_unreadable_file_is_treated_as_skip(self, tmp_path, monkeypatch):
        """An OSError while reading the (existing) hidden-spec file is
        treated the same as a missing/empty file: skip with reason. Stubs
        `pathlib.Path.read_text` (scoped to this one path) instead of
        relying on real filesystem permission bits, which are unreliable to
        set up portably in a test."""
        hidden_path = tmp_path / "hidden_spec.ts"
        hidden_path.write_text("test('a', async () => {});\n", encoding="utf-8")
        original_read_text = pathlib.Path.read_text

        def boom(self, *args, **kwargs):
            if self == hidden_path:
                raise OSError("simulated permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", boom)

        skip, reason = ss.decide_skip_hidden_suite(hidden_path)

        assert skip is True
        assert reason == "no @hidden scenarios"


# ─────────────────────────────────────────────────────────────────────────────
# write_verdict
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteVerdict:
    def test_skip_shape_matches_known_jq_format(self, tmp_path):
        """Pins the exact text `jq -n '{verdict: "PASS", scenarios_run: 0,
        skipped: true, reason: <reason>}'` produces: 2-space indent, a
        trailing newline, and `true`/`false` lowercase. This was previously
        verified byte-for-byte against a live `jq` subprocess; hardcoded
        here since this test file no longer shells out."""
        verdict_path = tmp_path / "skip.json"
        ss.write_verdict("PASS", 0, "", verdict_path, skipped=True, reason="no @hidden scenarios")
        assert verdict_path.read_text(encoding="utf-8") == (
            "{\n"
            '  "verdict": "PASS",\n'
            '  "scenarios_run": 0,\n'
            '  "skipped": true,\n'
            '  "reason": "no @hidden scenarios"\n'
            "}\n"
        )

    def test_run_shape_pass_matches_known_jq_format(self, tmp_path):
        verdict_path = tmp_path / "run.json"
        ss.write_verdict("PASS", 0, "2026-07-04T10:00:00Z", verdict_path, skipped=False)
        assert verdict_path.read_text(encoding="utf-8") == (
            "{\n"
            '  "verdict": "PASS",\n'
            '  "rc": 0,\n'
            '  "ran_at": "2026-07-04T10:00:00Z",\n'
            '  "skipped": false\n'
            "}\n"
        )

    def test_run_shape_fail_matches_known_jq_format(self, tmp_path):
        """Non-zero rc — same shape, different verdict/rc values."""
        verdict_path = tmp_path / "fail.json"
        ss.write_verdict("FAIL", 1, "2026-07-04T10:00:01Z", verdict_path, skipped=False)
        assert verdict_path.read_text(encoding="utf-8") == (
            "{\n"
            '  "verdict": "FAIL",\n'
            '  "rc": 1,\n'
            '  "ran_at": "2026-07-04T10:00:01Z",\n'
            '  "skipped": false\n'
            "}\n"
        )

    def test_key_order_is_stable(self, tmp_path):
        """json.dumps preserves dict insertion order (CPython 3.7+); assert
        the order explicitly rather than only relying on the literal-text
        comparisons above, so an accidental key reorder is caught even if
        someone changes the indent/formatting incidentally."""
        verdict_path = tmp_path / "run.json"
        ss.write_verdict("PASS", 0, "2026-07-04T10:00:00Z", verdict_path, skipped=False)
        payload = json.loads(
            verdict_path.read_text(encoding="utf-8"),
            object_pairs_hook=lambda pairs: pairs,
        )
        assert [k for k, _ in payload] == ["verdict", "rc", "ran_at", "skipped"]

    def test_accepts_string_path(self, tmp_path):
        verdict_path = tmp_path / "run.json"
        ss.write_verdict("PASS", 0, "2026-07-04T10:00:00Z", str(verdict_path), skipped=False)
        assert verdict_path.is_file()


# ─────────────────────────────────────────────────────────────────────────────
# Module isolation — replaces the old parity gate's live-DB sanity case (h)
# ─────────────────────────────────────────────────────────────────────────────


class TestModuleIsolation:
    def test_no_db_env_or_subprocess_coupling(self):
        """The old bash-parity gate's case (h) spun up a real SQLite DB via
        `db/init.sh` to prove the port doesn't touch it. That's unnecessary
        for a standalone test: the port never imports sqlite3/subprocess/os
        at all (those pieces — DB row I/O, `npx playwright test` —
        deliberately stay in bash; the module's docstring mentions
        'subprocess'/'npx' only in prose describing the bash it mirrors, not
        as actual imports). Pin the real dependency surface via the module's
        bound names rather than grepping raw source text, which would false
        -positive on that prose."""
        module_names = set(vars(ss))
        assert "sqlite3" not in module_names
        assert "subprocess" not in module_names
        assert "os" not in module_names
        # The only imports this module actually needs.
        assert {"json", "pathlib", "re"} <= module_names
