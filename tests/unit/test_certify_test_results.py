"""Pure unit tests for ``mini_ork.certify.test_results``.

No subprocess, no env mutation — each helper is exercised directly against
fixture strings and fixture files. The three parser fixtures (jest JSON,
vitest JSON, JUnit XML) are shaped to produce the *same* (passed, failed)
id sets so cross-format parity is pinned, not assumed.
"""
from __future__ import annotations

import json

from mini_ork.certify.test_results import (
    augment_for_results,
    detect_runners,
    parse_results_dir,
)


# ── detect_runners ────────────────────────────────────────────────────────────


def test_detect_runners_jest_forms():
    assert detect_runners("bash node_modules/.bin/jest --config x a.test.ts") == {"jest"}
    assert detect_runners("npx jest") == {"jest"}
    assert detect_runners("node_modules/.bin/jest") == {"jest"}
    assert detect_runners("node_modules/.bin/jest.js") == {"jest"}


def test_detect_runners_vitest_forms():
    assert detect_runners("npx vitest run") == {"vitest"}
    assert detect_runners("node_modules/.bin/vitest") == {"vitest"}


def test_detect_runners_pytest():
    assert detect_runners("python -m pytest -q") == {"pytest"}


def test_detect_runners_not_a_jest_word():
    # `jest-is-a-word` is not a jest word (its basename is not `jest`).
    assert detect_runners("echo jest-is-a-word") == set()


def test_detect_runners_none():
    assert detect_runners("bash gate.sh") == set()


# ── augment_for_results ───────────────────────────────────────────────────────


def test_augment_two_jest_chain_gets_distinct_files():
    cmd = "npx jest a.test.js && npx jest b.test.js"
    new, runners = augment_for_results(cmd, "/tmp/results")
    assert runners == {"jest"}
    assert new == (
        "npx jest --json --outputFile=/tmp/results/jest-1.json a.test.js "
        "&& npx jest --json --outputFile=/tmp/results/jest-2.json b.test.js"
    )


def test_augment_jest_leaves_other_args_intact():
    cmd = "bash node_modules/.bin/jest --config server/jest.config.js a.test.ts"
    new, runners = augment_for_results(cmd, "/tmp/results")
    assert runners == {"jest"}
    assert new == (
        "bash node_modules/.bin/jest --json --outputFile=/tmp/results/jest-1.json "
        "--config server/jest.config.js a.test.ts"
    )


def test_augment_vitest():
    cmd = "npx vitest run"
    new, runners = augment_for_results(cmd, "/tmp/results")
    assert runners == {"vitest"}
    assert new == (
        "npx vitest --reporter=default --reporter=json "
        "--outputFile=/tmp/results/vitest-1.json run"
    )


def test_augment_pytest_byte_identical():
    cmd = "python -m pytest -q"
    new, runners = augment_for_results(cmd, "/tmp/results")
    assert new == cmd
    assert runners == set()


def test_augment_no_runner_unchanged():
    cmd = "bash gate.sh"
    new, runners = augment_for_results(cmd, "/tmp/results")
    assert new == cmd
    assert runners == set()


# ── parse_results_dir ─────────────────────────────────────────────────────────

# All three fixtures produce these exact id sets: one passed test, one failed
# test, one pending/skipped test (ignored), and one load-failed suite (failed).
EXPECTED_IDS = (
    {"tests/add.test.js::add works"},
    {"tests/add.test.js::add broken", "tests/load.test.js::<suite load failure>"},
)

JEST_JSON = {
    "testResults": [
        {
            "name": "/work/tests/add.test.js",
            "status": "passed",
            "assertionResults": [
                {"fullName": "add works", "status": "passed"},
                {"fullName": "add broken", "status": "failed"},
                {"fullName": "add pending", "status": "pending"},
            ],
        },
        {
            "name": "/work/tests/load.test.js",
            "status": "failed",
            "assertionResults": [],
        },
    ]
}

JUNIT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="suite">
    <testcase classname="tests/add.test.js" name="add works"/>
    <testcase classname="tests/add.test.js" name="add broken">
      <failure message="boom"/>
    </testcase>
    <testcase classname="tests/add.test.js" name="add pending">
      <skipped/>
    </testcase>
    <testcase classname="tests/load.test.js" name="&lt;suite load failure&gt;">
      <error message="load failed"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _write_json(d, name, obj):
    (d / name).write_text(json.dumps(obj))


def test_parse_jest_json(tmp_path):
    d = tmp_path / "results"
    d.mkdir()
    _write_json(d, "jest-1.json", JEST_JSON)
    assert parse_results_dir(str(d), "/work") == EXPECTED_IDS


def test_parse_vitest_json(tmp_path):
    # vitest's json reporter is jest-compatible, so the same shape yields the
    # same ids.
    d = tmp_path / "results"
    d.mkdir()
    _write_json(d, "vitest-1.json", JEST_JSON)
    assert parse_results_dir(str(d), "/work") == EXPECTED_IDS


def test_parse_junit_xml(tmp_path):
    d = tmp_path / "results"
    d.mkdir()
    (d / "results.xml").write_text(JUNIT_XML)
    assert parse_results_dir(str(d), "/work") == EXPECTED_IDS


def test_parse_empty_dir_is_none(tmp_path):
    d = tmp_path / "results"
    d.mkdir()
    assert parse_results_dir(str(d), "/work") is None


def test_parse_missing_dir_is_none(tmp_path):
    assert parse_results_dir(str(tmp_path / "nope"), "/work") is None


def test_ids_relative_to_cwd(tmp_path):
    # The same file under two different roots must yield equal ids.
    fixture = {
        "testResults": [
            {
                "name": "/rootA/tests/add.test.js",
                "status": "passed",
                "assertionResults": [{"fullName": "add works", "status": "passed"}],
            }
        ]
    }
    d_a = tmp_path / "a"
    d_a.mkdir()
    _write_json(d_a, "jest-1.json", fixture)
    passed_a, _ = parse_results_dir(str(d_a), "/rootA")

    fixture["testResults"][0]["name"] = "/rootB/tests/add.test.js"
    d_b = tmp_path / "b"
    d_b.mkdir()
    _write_json(d_b, "jest-1.json", fixture)
    passed_b, _ = parse_results_dir(str(d_b), "/rootB")

    assert passed_a == passed_b == {"tests/add.test.js::add works"}
