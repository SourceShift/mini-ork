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
    parse_log_results,
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


def test_detect_runners_go_and_cargo_test():
    assert detect_runners("go test ./...") == {"go"}
    assert detect_runners("go test -v ./pkg") == {"go"}
    assert detect_runners("/usr/local/go/bin/go test ./...") == {"go"}
    assert detect_runners("cargo test --all") == {"cargo"}
    # Toolchain/flags between the tool and the subcommand are tolerated.
    assert detect_runners("cargo +nightly test") == {"cargo"}


def test_detect_runners_build_tools_are_not_test_runs():
    # `go`/`cargo` are build tools first: a build/vet/run invocation is NOT a
    # test run and must not widen replay applicability.
    assert detect_runners("go build ./...") == set()
    assert detect_runners("go vet ./...") == set()
    assert detect_runners("cargo build --release") == set()
    assert detect_runners("cargo run") == set()


def test_detect_runners_toolchain_near_misses():
    # Word-boundary discipline: a filename that merely contains `go`/`cargo`
    # (or a `go test`-shaped argument) is not the runner.
    assert detect_runners("bash go-test.sh") == set()
    assert detect_runners("bash cargo.testing") == set()
    assert detect_runners("echo gotest") == set()


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


def test_augment_go_test_adds_json_on_stdout():
    # `go test` has no output-file flag: `-json` is inserted so the event
    # stream lands on stdout (the oracle parses it back from the run log).
    new, runners = augment_for_results("go test ./...", "/tmp/results")
    assert new == "go test -json ./..."
    assert runners == {"go"}


def test_augment_go_test_keeps_existing_flags():
    new, runners = augment_for_results("go test -v -count=1 ./pkg", "/tmp/results")
    assert new == "go test -json -v -count=1 ./pkg"
    assert runners == {"go"}


def test_augment_go_test_already_json_is_unchanged():
    cmd = "go test -json ./..."
    new, runners = augment_for_results(cmd, "/tmp/results")
    assert new == cmd
    assert runners == {"go"}


def test_augment_cargo_test_is_never_augmented():
    # cargo's JSON reporter is nightly-only; the command must already ask for
    # it, so the adapter leaves it alone (it degrades to the exit-code path).
    cmd = "cargo test --all"
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


# ── symlinked roots (#17) ─────────────────────────────────────────────────────

def test_ids_are_stable_across_a_symlinked_worktree_root(tmp_path):
    """A suite loaded through a symlinked root is reported by the runner under
    its REAL path, while the caller passes the symlink it created (the macOS
    ``/var`` ↔ ``/private/var`` case for every ``mkdtemp``). The id must be
    cwd-relative so it still matches the same suite on the other tree — a
    ``../../`` chain never can.
    """
    real = tmp_path / "real"
    (real / "src" / "__tests__").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)

    suite = real / "src" / "__tests__" / "x.test.ts"
    suite.write_text("it('works', () => {})\n")
    results = tmp_path / "results"
    results.mkdir()
    _write_json(results, "jest-1.json", {
        "testResults": [
            {
                "name": str(suite),  # the resolved path, as node reports it
                "status": "passed",
                "assertionResults": [{"fullName": "works", "status": "passed"}],
            }
        ]
    })

    passed, failed = parse_results_dir(str(results), str(link))
    assert passed == {"src/__tests__/x.test.ts::works"}
    assert failed == set()


def test_load_failure_id_is_stable_across_a_symlinked_root(tmp_path):
    """Same hazard on the load-failure id: a ``::<suite load failure>`` under a
    symlinked root must land on the same relative id, not a ``../../`` chain."""
    real = tmp_path / "real"
    (real / "src" / "__tests__").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)

    suite = real / "src" / "__tests__" / "x.test.ts"
    suite.write_text("import './missing'\n")
    results = tmp_path / "results"
    results.mkdir()
    _write_json(results, "jest-1.json", {
        "testResults": [{"name": str(suite), "status": "failed", "assertionResults": []}]
    })

    passed, failed = parse_results_dir(str(results), str(link))
    assert passed == set()
    assert failed == {"src/__tests__/x.test.ts::<suite load failure>"}


def test_suite_genuinely_outside_cwd_keeps_the_literal_escape(tmp_path):
    """A suite outside the cwd is still reported as before: the realpath retry
    escapes too, so the literal relative answer is kept (no regression)."""
    inside = tmp_path / "inside"
    inside.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    _write_json(results, "jest-1.json", {
        "testResults": [
            {
                "name": str(tmp_path / "other" / "a.test.js"),
                "status": "passed",
                "assertionResults": [{"fullName": "t", "status": "passed"}],
            }
        ]
    })

    passed, _ = parse_results_dir(str(results), str(inside))
    assert passed == {"../other/a.test.js::t"}


# ── parse_log_results: go test -json ──────────────────────────────────────────

GO_JSON_LOG = "\n".join([
    '{"Action":"start","Package":"example.com/m"}',
    '{"Action":"run","Package":"example.com/m","Test":"TestAdd"}',
    '{"Action":"output","Package":"example.com/m","Test":"TestAdd","Output":"=== RUN   TestAdd\\n"}',
    '{"Action":"pass","Package":"example.com/m","Test":"TestAdd","Elapsed":0}',
    '{"Action":"run","Package":"example.com/m","Test":"TestSub"}',
    '{"Action":"fail","Package":"example.com/m","Test":"TestSub","Elapsed":0}',
    # Package-level summary fail — NOT a load failure, because the package
    # produced per-test outcomes.
    '{"Action":"fail","Package":"example.com/m","Elapsed":0.01}',
])

# A package whose test binary could not build: newer go names it `build-fail`.
GO_BUILD_FAIL_LOG = "\n".join([
    '{"Action":"build-output","ImportPath":"example.com/m","Output":"# example.com/m\\n"}',
    '{"Action":"build-fail","ImportPath":"example.com/m"}',
])

# Older go reports only the package-level fail, with no per-test outcome.
GO_OLD_BUILD_FAIL_LOG = "\n".join([
    '{"Action":"start","Package":"example.com/m"}',
    '{"Action":"fail","Package":"example.com/m","Elapsed":0.02}',
])


def test_parse_go_json_log_per_test_outcomes():
    assert parse_log_results(GO_JSON_LOG, "/work", "go") == (
        {"example.com/m::TestAdd"},
        {"example.com/m::TestSub"},
    )


def test_parse_go_package_summary_fail_is_not_a_load_failure():
    # The trailing package-level `fail` must not be double-counted as weak
    # evidence when the package already produced a real failing assertion.
    assert parse_log_results(GO_JSON_LOG, "/work", "go")[1] == {"example.com/m::TestSub"}


def test_parse_go_build_fail_is_a_load_failure():
    assert parse_log_results(GO_BUILD_FAIL_LOG, "/work", "go") == (
        set(),
        {"example.com/m::<suite load failure>"},
    )


def test_parse_go_old_style_build_fail_is_a_load_failure():
    # A package-level fail with no per-test outcome is the older go's only
    # build-failure signal.
    assert parse_log_results(GO_OLD_BUILD_FAIL_LOG, "/work", "go") == (
        set(),
        {"example.com/m::<suite load failure>"},
    )


def test_parse_go_no_outcomes_is_none():
    assert parse_log_results('{"Action":"output","Package":"p","Output":"x\\n"}',
                             "/work", "go") is None


# ── parse_log_results: cargo's libtest JSON stream ───────────────────────────

CARGO_JSON_LOG = "\n".join([
    '{"type":"suite","event":"started","test_count":2}',
    '{"type":"test","event":"started","name":"tests::test_add"}',
    '{"type":"test","event":"ok","name":"tests::test_add"}',
    '{"type":"test","event":"failed","name":"tests::test_sub"}',
    '{"type":"test","event":"ignored","name":"tests::test_skip"}',
    '{"type":"suite","event":"failed","passed":1,"failed":1,"ignored":1}',
    '   Compiling m v0.1.0',
])


def test_parse_cargo_json_log_outcomes():
    assert parse_log_results(CARGO_JSON_LOG, "/work", "cargo") == (
        {"tests::test_add"},
        {"tests::test_sub"},
    )


def test_parse_cargo_build_failure_is_none():
    # No test events (the build never reached libtest): fall through to the
    # exit-code instrument rather than inventing an outcome.
    build = "   Compiling m v0.1.0\nerror[E0425]: cannot find value `x`\n"
    assert parse_log_results(build, "/work", "cargo") is None


def test_parse_log_results_unknown_runner_is_none():
    assert parse_log_results(GO_JSON_LOG, "/work", "jest") is None
    assert parse_log_results("", "/work", "go") is None
