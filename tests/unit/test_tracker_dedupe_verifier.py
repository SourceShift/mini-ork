"""Unit tests for recipes/bug-audit-cmgk/verifiers/tracker-dedupe.py.

The verifier exists because a lens panel cannot see the target repo's issue
tracker: on 2026-11-24 a 4/4-consensus finding was filed as issue #3600 + PR
#3602 against ChromeDevTools/chrome-devtools-mcp and both were closed as
duplicates of #3453, where the maintainer had already declined the change.
These tests pin the three properties that keep that from recurring:

  * a finding whose primary symbol is in a tracker *title* is removed from the
    published synthesis and restored verbatim in `already-reported.md`;
  * a weaker overlap (secondary term only) is reported but NOT removed —
    precision over recall, because dropping a real finding costs more;
  * an unavailable tracker (no `gh`, or every query failing) is reported as
    `abstained`, a partly-answered one as `partial` — never as a silent
    `checked` that the published document could claim was clean;
  * a generic capitalised word (`Response`, `Session`) is not treated as a
    code symbol, so it cannot delete a finding;
  * every finding gets its own term budget, so a long synthesis cannot starve
    its tail the way one global cap did (35 findings, 40 terms, 33-35 skipped);

`gh` is faked on PATH; nothing here touches the network. The verifier's stderr
must stay empty — the runner tees it into the evidence capture, and a stray
line there makes `verifier_tracker-dedupe.json` unparsable.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "recipes/bug-audit-cmgk/verifiers/tracker-dedupe.py"

FAKE_GH = '''\
#!{python}
import json, os, sys

argv = sys.argv[1:]
kind = argv[0]
term = argv[argv.index("--search") + 1]
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(term + "\\n")
if os.environ.get("FAKE_GH_FAIL_ALL") == "1" or term in {failing!r}:
    sys.stderr.write("gh: simulated failure\\n")
    raise SystemExit(1)
TITLES = {titles}
emit = []
for number, title, state in TITLES.get(term, []):
    if kind == "issue":
        emit.append({{"number": number, "title": title, "state": state,
                     "url": "https://example.invalid/issues/{{}}".format(number)}})
print(json.dumps(emit))
'''


def _fake_gh(bin_dir, titles, failing=()):
    path = bin_dir / "gh"
    path.write_text(
        FAKE_GH.format(
            python=sys.executable, titles=repr(titles),
            failing=tuple(failing), log=str(bin_dir / "gh-calls.log"),
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


SYNTH = """\
# Bug audit (synthesis) — report only, no fixes

## Summary
- Total unique bugs: 2

## P0 — Critical bugs
- [BUG-001] [CONSENSUS: 4/4] `resolveScriptSource` mangles http(s) script URLs
  - File: `src/tools/script.ts:41`
  - Feature: script source resolution
  - Symptom: `resolveScriptSource` passes every non-`file:` value to
    `pathToFileURL`, so an https URL can never reach the fetch branch.
  - Root cause: the `http:` case is never constructed.
  - Impact: documented behaviour is unreachable.

## P2 — Degraded behaviour
- [BUG-002] [CONSENSUS: 1/4] response body not capped
  - File: `src/tools/network.ts:88`
  - Feature: network reads
  - Symptom: `loadResource` accumulates the whole body.
  - Impact: memory pressure on a large response.

## Coverage gap report
- `tools/script`: one finding; `tools/network`: one finding.
"""


def _run(tmp_path, synth=SYNTH, titles=None, path_extra=None, env_extra=None,
         failing=()):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    target = run_dir / "synthesis.md"
    if not target.exists():  # a second call continues from the first's output
        target.write_text(synth, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if titles is not None:
        _fake_gh(bin_dir, titles, failing=failing)
    env = {
        **os.environ,
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MO_AUDIT_TARGET_REPO": "acme/widget",
        "PATH": str(bin_dir) if path_extra is None else str(path_extra),
    }
    env.pop("ARTIFACT_PATH", None)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True,
        env=env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    # The runner tees stderr into the evidence capture, which must hold exactly
    # one JSON object. Any diagnostic the verifier prints there makes
    # verifier_tracker-dedupe.json unparsable and the run records
    # `contract: UNVERIFIED` even when the verdict is pass.
    assert proc.stderr == "", proc.stderr
    payload = json.loads(proc.stdout)
    return payload, run_dir


def _gh_calls(tmp_path):
    log = tmp_path / "bin" / "gh-calls.log"
    return log.read_text(encoding="utf-8").split("\n") if log.exists() else []


def test_removes_finding_whose_primary_symbol_is_in_a_tracker_title(tmp_path):
    titles = {"resolveScriptSource": [
        (3453, "resolveScriptSource turns https into file URLs", "open"),
        (7000, "resolveScriptSource mentioned in an unrelated cleanup", "closed"),
    ]}
    payload, run_dir = _run(tmp_path, titles=titles)

    assert payload["pass"] is True
    assert payload["status"] == "checked"
    assert [entry["bug"] for entry in payload["skipped"]] == ["BUG-001"]

    synth = (run_dir / "synthesis.md").read_text(encoding="utf-8")
    assert "ALREADY-REPORTED" in synth
    assert (
        "[ALREADY-REPORTED: #3453]" in synth
    ), "the citation must be the open thread, not a closed one that also matched"
    assert "pathToFileURL" not in synth, "the dropped finding's body must not survive"
    assert "BUG-002" in synth and "loadResource" in synth, "other findings are untouched"

    restored = (run_dir / "already-reported.md").read_text(encoding="utf-8")
    assert "#3453" in restored
    assert "resolveScriptSource turns https into file URLs" in restored


def test_keeps_findings_when_only_a_secondary_term_matches(tmp_path):
    titles = {"matchesFilter": [(3601, "matchesFilter vocabulary is asymmetric", "open")]}
    synth = SYNTH.replace(
        "- [BUG-002] [CONSENSUS: 1/4] response body not capped\n"
        "  - File: `src/tools/network.ts:88`\n",
        "- [BUG-002] [CONSENSUS: 1/4] `getFilterableResourceType` mismatch\n"
        "  - File: `src/tools/network.ts:88`\n"
        "  - Note: `matchesFilter` reads the stored vocabulary.\n",
    )
    payload, run_dir = _run(tmp_path, synth=synth,
                            titles={**titles, "resolveScriptSource": []})

    assert payload["status"] == "checked"
    assert payload["skipped"] == []
    assert [entry["bug"] for entry in payload["possible"]] == ["BUG-002"]
    assert (run_dir / "synthesis.md").read_text(encoding="utf-8") == synth, (
        "a possible match must not edit the artifact"
    )
    restored = (run_dir / "already-reported.md").read_text(encoding="utf-8")
    assert "Possible" in restored and "#3601" in restored


def test_title_symbol_identifies_the_duplicate_not_the_longest_body_symbol(tmp_path):
    # The finding this verifier was built from: the title names the subject
    # (`evaluate_script`, `sourcePath`), the body names the mechanism
    # (`pathToFileURL`). Ranking the longest identifier picked the mechanism and
    # the real duplicate came back only "possible".
    synth = (
        "# Bug audit (synthesis)\n\n## P0 — Critical bugs\n"
        "- [BUG-001] [CONSENSUS: 4/4] evaluate_script sourcePath http(s) URLs mangled into file://\n"
        "  - File: `src/tools/script.ts:111`\n"
        "  - Symptom: `sourcePath` goes through `pathToFileURL`, so the fetch branch is unreachable.\n"
    )
    titles = {
        "pathToFileURL": [(4001, "pathToFileURL is not exported", "open")],
        "evaluate_script": [],
        "sourcePath": [(3453, "`evaluate_script` `sourcePath` http(s) URLs are mangled into `file://` paths", "OPEN")],
    }
    payload, run_dir = _run(tmp_path, synth=synth, titles=titles)

    assert [entry["bug"] for entry in payload["skipped"]] == ["BUG-001"]
    assert payload["skipped"][0]["primary"] in {"evaluate_script", "sourcePath"}
    assert payload["possible"] == []
    assert "ALREADY-REPORTED" in (run_dir / "synthesis.md").read_text(encoding="utf-8")


def test_a_body_symbol_alone_cannot_identify_a_duplicate(tmp_path):
    titles = {"pathToFileURL": [(4001, "pathToFileURL is not exported", "open")]}
    payload, run_dir = _run(tmp_path, titles=titles)

    assert payload["skipped"] == []
    assert [entry["bug"] for entry in payload["possible"]] == ["BUG-001"]
    assert (run_dir / "synthesis.md").read_text(encoding="utf-8") == SYNTH


def test_preserves_existing_annotation_without_searching(tmp_path):
    synth = SYNTH.replace(
        "- [BUG-002] [CONSENSUS: 1/4] response body not capped",
        "- [BUG-002] [ALREADY-REPORTED: #9999] response body not capped",
    )
    titles = {"resolveScriptSource": [], "loadResource": []}
    payload, run_dir = _run(tmp_path, synth=synth, titles=titles)

    assert payload["status"] == "checked"
    assert payload["skipped"] == []
    assert (run_dir / "synthesis.md").read_text(encoding="utf-8") == synth


def test_abstains_when_gh_is_not_on_path(tmp_path):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    payload, run_dir = _run(tmp_path, path_extra=empty)

    assert payload["pass"] is True
    assert payload["status"] == "abstained"
    assert "gh" in payload["reason"]
    assert (run_dir / "synthesis.md").read_text(encoding="utf-8") == SYNTH


def test_abstains_when_no_target_repo_resolves(tmp_path):
    payload, _ = _run(
        tmp_path, titles={},
        env_extra={"MO_AUDIT_TARGET_REPO": ""},
    )
    # tmp_path is not a git checkout, and neither is <run>/../../../..
    assert payload["status"] == "abstained"
    assert payload["pass"] is True


def test_fails_when_a_duplicate_is_found_but_cannot_be_removed(tmp_path):
    # The synthesis must be the read-only file from the start: a second pass
    # over an already-annotated synthesis has nothing left to search, which is
    # idempotent but would not exercise the rewrite failure.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    synth = run_dir / "synthesis.md"
    synth.write_text(SYNTH, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(bin_dir, {
        "resolveScriptSource": [(3453, "resolveScriptSource mangles URLs", "closed")],
    })
    synth.chmod(0o444)
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True,
            env={
                **os.environ,
                "MINI_ORK_RUN_DIR": str(run_dir),
                "MO_AUDIT_TARGET_REPO": "acme/widget",
                "PATH": str(bin_dir),
            },
            cwd=str(tmp_path),
        )
    finally:
        synth.chmod(0o644)

    assert proc.returncode == 0
    blocked = json.loads(proc.stdout)
    assert blocked["pass"] is False
    assert blocked["status"] == "checked"
    assert "rewrite failed" in blocked["reason"]
    assert [entry["bug"] for entry in blocked["skipped"]] == ["BUG-001"]


def test_skips_whole_check_when_disabled(tmp_path):
    payload, run_dir = _run(
        tmp_path, titles={},
        env_extra={"MO_AUDIT_DEDUPE_OFF": "1"},
    )
    assert payload["status"] == "skipped"
    assert payload["pass"] is True
    assert (run_dir / "synthesis.md").read_text(encoding="utf-8") == SYNTH


def test_abstains_when_synthesis_has_no_bug_entries(tmp_path):
    payload, _ = _run(
        tmp_path,
        synth="# Bug audit (synthesis)\n\nNo findings.\n", titles={},
    )
    assert payload["status"] == "abstained"
    assert payload["pass"] is True


def test_rewrite_keeps_trailing_sections_after_the_bug_list():
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("tracker_dedupe", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)

    rewritten = module._rewrite(SYNTH, {"BUG-001": 3453}, "> banner")
    assert rewritten.index("## Coverage gap report") > rewritten.index("- [BUG-002]")
    assert rewritten.index("> banner") < rewritten.index("## Summary")
    assert "[BUG-001] [ALREADY-REPORTED: #3453]" in rewritten


def test_second_pass_leaves_its_own_breadcrumb_alone(tmp_path):
    # Idempotency: the placeholder is written in the `[ALREADY-REPORTED: #N]`
    # shape the analysis step recognises, so a re-run does not search its own
    # output — and does not resurrect the finding from the breadcrumb text.
    titles = {"resolveScriptSource": [(3453, "resolveScriptSource mangles URLs", "closed")]}
    first, run_dir = _run(tmp_path, titles=titles)
    assert [entry["bug"] for entry in first["skipped"]] == ["BUG-001"]
    after_first = (run_dir / "synthesis.md").read_text(encoding="utf-8")

    second, _ = _run(tmp_path, titles=titles)
    assert second["status"] == "checked"
    assert second["skipped"] == []
    assert (run_dir / "synthesis.md").read_text(encoding="utf-8") == after_first


def test_every_query_failing_abstains_instead_of_reporting_a_clean_check(tmp_path):
    # A failed `gh` call returns no hits, which is the same value as "this repo
    # has nothing on that term". Reporting `checked` there is a lie the
    # published document would repeat.
    payload, run_dir = _run(
        tmp_path, titles={}, env_extra={"FAKE_GH_FAIL_ALL": "1"},
    )

    assert payload["pass"] is True
    assert payload["status"] == "abstained"
    assert payload["failed_queries"] > 0
    assert "failed" in payload["reason"]
    assert (run_dir / "synthesis.md").read_text(encoding="utf-8") == SYNTH
    restored = (run_dir / "already-reported.md").read_text(encoding="utf-8")
    assert "coverage gap" in restored
    assert "simulated failure" in restored


def test_partial_tracker_failure_reports_partial_coverage(tmp_path):
    payload, run_dir = _run(
        tmp_path, titles={}, failing=("loadResource",),
    )

    assert payload["pass"] is True
    assert payload["status"] == "partial"
    assert payload["failed_queries"] == 2  # one failing term, two queries (issue+pr)
    assert "incomplete" in payload["reason"]
    restored = (run_dir / "already-reported.md").read_text(encoding="utf-8")
    assert "loadResource" in restored


def test_a_generic_capitalised_word_is_not_a_symbol(tmp_path):
    # `Response` is capitalised, so a shape-agnostic "has an uppercase letter"
    # rule treats it as code. A tracker title that says the word then matches,
    # and a real finding is deleted by a generic noun.
    synth = (
        "# Bug audit (synthesis)\n\n## P1 — Bugs\n"
        "- [BUG-001] `Response` buffers the whole body\n"
        "  - File: `src/tools/network.ts:88`\n"
        "  - Symptom: `Response` is read without a cap.\n"
    )
    titles = {"Response": [(1, "Response buffers too much", "open")]}
    payload, run_dir = _run(tmp_path, synth=synth, titles=titles)

    assert payload["status"] == "checked"
    assert payload["skipped"] == []
    assert payload["possible"] == []
    assert "Response" not in _gh_calls(tmp_path)
    assert (run_dir / "synthesis.md").read_text(encoding="utf-8") == synth


def test_each_finding_gets_its_own_term_budget(tmp_path):
    # One global cap made coverage a function of position: with 35 findings and
    # a 40-term ceiling, the last three were never searched. Every finding must
    # get its own budget instead.
    bases = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
        "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
        "oscar", "papa",
    ]
    suffixes = ["Alpha", "Bravo", "Charlie", "Delta"]
    lines = ["# Bug audit (synthesis)", "", "## P1 — Bugs"]
    for index, base in enumerate(bases, 1):
        words = " ".join(f"`{base}{suffix}`" for suffix in suffixes)
        lines.append(f"- [BUG-{index:03d}] {words} mangled")
        lines.append("  - File: `src/thing.py:1`")
    synth = "\n".join(lines) + "\n"

    payload, _ = _run(tmp_path, synth=synth, titles={})

    calls = set(_gh_calls(tmp_path))
    for base in bases:
        assert f"{base}Alpha" in calls, (
            f"{base}'s terms were never searched — the budget is global again"
        )
    assert payload["searched_terms"] >= len(bases) * 4
