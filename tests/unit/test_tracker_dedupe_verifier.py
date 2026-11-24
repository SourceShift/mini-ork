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
  * an unavailable tracker (no `gh`) is reported as `abstained`, never as a
    silent pass that the published document could claim as a clean check.

`gh` is faked on PATH; nothing here touches the network.
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
import json, sys

argv = sys.argv[1:]
kind = argv[0]
term = argv[argv.index("--search") + 1]
TITLES = {titles}
emit = []
for number, title, state in TITLES.get(term, []):
    if kind == "issue":
        emit.append({{"number": number, "title": title, "state": state,
                     "url": "https://example.invalid/issues/{{}}".format(number)}})
print(json.dumps(emit))
'''


def _fake_gh(bin_dir, titles):
    path = bin_dir / "gh"
    path.write_text(
        FAKE_GH.format(python=sys.executable, titles=repr(titles)),
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


def _run(tmp_path, synth=SYNTH, titles=None, path_extra=None, env_extra=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    target = run_dir / "synthesis.md"
    if not target.exists():  # a second call continues from the first's output
        target.write_text(synth, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if titles is not None:
        _fake_gh(bin_dir, titles)
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
    payload = json.loads(proc.stdout)
    return payload, run_dir


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
