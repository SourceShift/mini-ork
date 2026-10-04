"""Spec-directory ingestion (spec-driven-delivery K1): scan, SpecCard, index,
lint, and the `mini-ork specs` CLI round-trip.

Everything under test is deterministic (no model calls). CLI tests spawn the
real launcher (`bin/mini-ork specs ...`) on THIS interpreter with the engine
root pinned to this checkout, and always write generated indexes under
tmp_path — never into tests/fixtures.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from mini_ork.cli import specs as specs_cli
from mini_ork.specdir import index as idx
from mini_ork.specdir import lint as lint_mod
from mini_ork.specdir import scan as scan_mod
from mini_ork.specdir.spec_card import (
    GATE_KINDS,
    STATUSES,
    SpecCard,
    SpecCardError,
    validate_card,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "specdir"
BIN = REPO / "bin" / "mini-ork"
CARD_SCHEMA = REPO / "schemas" / "spec-card.schema.json"
INDEX_SCHEMA = REPO / "schemas" / "spec-index.schema.json"


@pytest.fixture(autouse=True)
def _clean_sdd_env(monkeypatch):
    for var in ("MO_SDD_SPEC_GLOB", "MO_SDD_SPEC_MAX_BYTES", "MO_SDD_VAGUE_TERMS", "MO_SDD_MIN_SPECS"):
        monkeypatch.delenv(var, raising=False)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _index_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(INDEX_SCHEMA.read_text(encoding="utf-8")))


# ── schemas ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [CARD_SCHEMA, INDEX_SCHEMA])
def test_schemas_are_valid_draft_2020_12(path):
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_enums_mirror_design_doc():
    assert STATUSES == ("draft", "ratified", "dispatched", "delivered", "failed", "blocked")
    assert GATE_KINDS == ("cmd", "ui", "contract")


# ── scan ─────────────────────────────────────────────────────────────────


def test_scan_fixture_inventory():
    entries = scan_mod.scan_specdir(FIXTURE)
    assert [e.spec_id for e in entries] == ["bad-feature", "good-feature"]
    for e in entries:
        path = Path(e.source_path)
        assert path.is_absolute() and path.is_file()
        data = path.read_bytes()
        assert e.source_hash == "sha256:" + hashlib.sha256(data).hexdigest()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", e.source_hash)
        assert e.size_bytes == len(data)
    assert entries[0].title == "Better dashboard"
    assert entries[1].title == "CSV export for the run ledger"


def test_scan_skips_readme_index_hidden_and_honours_recursive(tmp_path):
    _write(tmp_path / "alpha.md", "# Alpha\n")
    _write(tmp_path / "README.md", "# readme\n")
    _write(tmp_path / "Index.md", "# index\n")
    _write(tmp_path / ".draft.md", "# hidden\n")
    _write(tmp_path / ".cache" / "gamma.md", "# hidden dir\n")
    _write(tmp_path / "sub" / "beta.md", "# Beta\n")
    _write(tmp_path / "notes.txt", "not a spec\n")
    assert [e.spec_id for e in scan_mod.scan_specdir(tmp_path)] == ["alpha"]
    assert [e.spec_id for e in scan_mod.scan_specdir(tmp_path, recursive=True)] == ["alpha", "beta"]


def test_scan_title_skips_fences_and_front_matter(tmp_path):
    _write(tmp_path / "No_Title.md", "---\n# yaml comment\n---\n```md\n# fenced\n```\n## Sub only\n")
    _write(tmp_path / "titled.md", "```\n# fenced\n```\n# Real Title #\n")
    by_id = {e.spec_id: e for e in scan_mod.scan_specdir(tmp_path)}
    assert by_id["no-title"].title == "No_Title"
    assert by_id["titled"].title == "Real Title"


def test_scan_glob_from_env_read_at_call_time(tmp_path, monkeypatch):
    _write(tmp_path / "one.spec.md", "# One\n")
    _write(tmp_path / "two.md", "# Two\n")
    assert [e.spec_id for e in scan_mod.scan_specdir(tmp_path)] == ["one-spec", "two"]
    monkeypatch.setenv("MO_SDD_SPEC_GLOB", "*.spec.md")
    assert [e.spec_id for e in scan_mod.scan_specdir(tmp_path)] == ["one-spec"]
    assert [e.spec_id for e in scan_mod.scan_specdir(tmp_path, glob="two.*")] == ["two"]


def test_scan_depends_on_lines(tmp_path):
    _write(tmp_path / "a.md", "# A\n- Depends on: B-Spec, `c.md`\n```\nDepends on: fenced\n```\n")
    _write(tmp_path / "b-spec.md", "# B\n**Depends on:** none\n")
    _write(tmp_path / "c.md", "# C\n")
    by_id = {e.spec_id: e for e in scan_mod.scan_specdir(tmp_path)}
    assert by_id["a"].depends_on == ("b-spec", "c")
    assert by_id["b-spec"].depends_on == ()


def test_scan_missing_dir_raises(tmp_path):
    with pytest.raises(NotADirectoryError):
        scan_mod.scan_specdir(tmp_path / "nope")


@pytest.mark.parametrize("raw", ["Session 11: Owner Dashboard", "--x--", "über.v2", "", "A__B  C"])
def test_slugify_matches_epics_and_is_idempotent(raw):
    from mini_ork.cli.epics import _slugify as epics_slugify

    slug = scan_mod._slugify(raw)
    assert scan_mod._slugify(slug) == slug
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug)
    if epics_slugify(raw) != "epic":
        assert slug == epics_slugify(raw)


# ── SpecCard ─────────────────────────────────────────────────────────────


def _card_dict(**overrides) -> dict:
    card = {
        "spec_id": "s11-owner-dashboard",
        "source_path": "/abs/specs/session-11.md",
        "source_hash": "sha256:" + "a" * 64,
        "title": "Owner dashboard",
        "clauses": {
            "functional": [{"id": "F1", "text": "Owner sees revenue per week."}],
            "quality": [{"id": "Q1", "text": "Page renders under 300 ms."}],
            "constitutional": [{"id": "C1", "text": "Never expose other owners' data."}],
            "architectural": [{"id": "A1", "text": "Reads go through the reporting service."}],
        },
        "acceptance": [{
            "id": "AC1",
            "clause_refs": ["F1"],
            "text": "Revenue table lists 7 rows for a 7-week owner.",
            "gate": {"kind": "cmd", "probe": "pytest -q tests/test_dash.py", "expect": "exit 0"},
        }],
        "deliverables": [
            {"id": "D1", "title": "Revenue query", "acceptance_refs": ["AC1"], "depends_on": []},
            {"id": "D2", "title": "Dashboard page", "acceptance_refs": ["AC1"], "depends_on": ["D1"]},
        ],
        "ui_craft": {"required": True, "design_sources": ["/abs/design/dashboard.png"]},
        "status": "draft",
    }
    card.update(overrides)
    return card


def test_spec_card_round_trip_validates():
    raw = _card_dict()
    assert validate_card(raw) == []
    card = SpecCard.from_dict(raw)
    assert card.to_dict() == raw
    assert card.validate() == []
    assert card.deliverables[1].depends_on == ("D1",)


def test_spec_card_from_dict_tolerates_absent_optional_lists():
    raw = {k: _card_dict()[k] for k in ("spec_id", "source_path", "source_hash", "title")}
    raw["acceptance"] = [{"id": "AC1", "text": "t", "gate": {"kind": "ui", "probe": "p", "expect": "e"}}]
    raw["deliverables"] = [{"id": "D1", "title": "d"}]
    out = SpecCard.from_dict(raw).to_dict()
    assert validate_card(out) == []
    assert out["clauses"] == {k: [] for k in ("functional", "quality", "constitutional", "architectural")}
    assert out["acceptance"][0]["clause_refs"] == []
    assert out["deliverables"][0]["depends_on"] == []
    assert out["ui_craft"] == {"required": False, "design_sources": []}
    assert out["status"] == "draft"


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.update(status="shipped"), "status"),
    (lambda d: d["acceptance"][0]["gate"].update(kind="shell"), "acceptance/0/gate/kind"),
    (lambda d: d.update(source_path="relative/spec.md"), "source_path"),
    (lambda d: d.update(source_hash="md5:abc"), "source_hash"),
    (lambda d: d.update(spec_id="Not A Slug"), "spec_id"),
    (lambda d: d.update(extra="x"), "<root>"),
    (lambda d: d["clauses"].pop("quality"), "clauses"),
])
def test_validate_card_rejects_bad_values(mutate, needle):
    raw = _card_dict()
    mutate(raw)
    errors = validate_card(raw)
    assert errors and any(e.startswith(needle) for e in errors), errors


def test_spec_card_from_dict_requires_scalars():
    raw = _card_dict()
    del raw["source_hash"]
    with pytest.raises(SpecCardError, match="source_hash"):
        SpecCard.from_dict(raw)
    raw = _card_dict()
    del raw["acceptance"][0]["gate"]
    with pytest.raises(SpecCardError, match=r"acceptance\[0\].*gate"):
        SpecCard.from_dict(raw)


# ── index ────────────────────────────────────────────────────────────────


def test_build_write_read_index(tmp_path):
    entries = scan_mod.scan_specdir(FIXTURE)
    index = idx.build_index(entries, FIXTURE)
    assert idx.validate_index(index) == []
    _index_validator().validate(index)
    assert index["schema_version"] == "1.0"
    assert index["root"] == str(FIXTURE.resolve())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", index["generated_at"])
    assert sorted(index["specs"]) == ["bad-feature", "good-feature"]
    for entry in index["specs"].values():
        assert Path(entry["source_path"]).is_absolute()
        assert entry["status"] == "draft"
        assert entry["depends_on"] == []

    out = idx.write_index(index, tmp_path / "nested" / "spec-index.json")
    assert out == (tmp_path / "nested" / "spec-index.json").resolve()
    text = out.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert text == json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert idx.read_index(out) == index
    assert [p.name for p in out.parent.iterdir()] == ["spec-index.json"]  # no temp left behind


def test_build_index_is_deterministic_and_takes_card_status():
    entries = scan_mod.scan_specdir(FIXTURE)
    a = idx.build_index(entries, FIXTURE, generated_at="2026-10-03T00:00:00Z")
    b = idx.build_index(list(reversed(entries)), FIXTURE, generated_at="2026-10-03T00:00:00Z")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    card = _card_dict(spec_id="good-feature", status="ratified", deliverables=[])
    c = idx.build_index(entries, FIXTURE, cards=[card])
    assert c["specs"]["good-feature"]["status"] == "ratified"
    assert c["specs"]["bad-feature"]["status"] == "draft"


def test_write_index_refuses_invalid_and_read_index_rejects_garbage(tmp_path):
    bad = idx.build_index(scan_mod.scan_specdir(FIXTURE), FIXTURE)
    bad["specs"]["good-feature"]["source_path"] = "relative.md"
    target = tmp_path / "spec-index.json"
    with pytest.raises(idx.SpecIndexError, match="source_path"):
        idx.write_index(bad, target)
    assert not target.exists()
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(idx.SpecIndexError, match="not valid JSON"):
        idx.read_index(target)
    target.write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
    with pytest.raises(idx.SpecIndexError, match="generated_at"):
        idx.read_index(target)


@pytest.mark.parametrize("graph, expected", [
    ({"a": ["b"], "b": ["a"]}, [["a", "b", "a"]]),
    ({"c": ["a"], "a": ["b"], "b": ["c"]}, [["a", "b", "c", "a"]]),
    ({"a": ["a"]}, [["a", "a"]]),
    ({"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}, []),
    ({"a": ["x"]}, []),
    ({}, []),
])
def test_find_cycles(graph, expected):
    assert idx.find_cycles(graph) == expected


def test_find_cycles_reports_each_cycle_once_in_sorted_order():
    graph = {"z": ["y"], "y": ["z"], "b": ["a"], "a": ["b", "z"]}
    assert idx.find_cycles(graph) == [["a", "b", "a"], ["y", "z", "y"]]


def test_deliverable_graph_qualifies_ids_and_catches_cross_spec_cycle():
    a = _card_dict(spec_id="spec-a", deliverables=[
        {"id": "D1", "title": "t", "acceptance_refs": [], "depends_on": ["spec-b/D1"]},
        {"id": "D2", "title": "t", "acceptance_refs": [], "depends_on": ["D1"]},
    ])
    b = _card_dict(spec_id="spec-b", deliverables=[
        {"id": "D1", "title": "t", "acceptance_refs": [], "depends_on": ["spec-a/D2"]},
    ])
    assert idx.deliverable_graph([a, SpecCard.from_dict(b)]) == {
        "spec-a/D1": ["spec-b/D1"], "spec-a/D2": ["spec-a/D1"], "spec-b/D1": ["spec-a/D2"],
    }
    entries = scan_mod.scan_specdir(FIXTURE)
    with pytest.raises(idx.SpecIndexError, match=r"spec-a/D1 -> spec-b/D1 -> spec-a/D2 -> spec-a/D1"):
        idx.build_index(entries, FIXTURE, cards=[a, b])
    assert idx.build_index(entries, FIXTURE, cards=[_card_dict()])  # D1 <- D2 is acyclic


def test_spec_dependency_cycle_is_a_hard_error(tmp_path):
    _write(tmp_path / "a.md", "# A\nDepends on: b\n")
    _write(tmp_path / "b.md", "# B\nDepends on: a\n")
    _write(tmp_path / "c.md", "# C\nDepends on: a\n")
    entries = scan_mod.scan_specdir(tmp_path)
    with pytest.raises(idx.SpecIndexError, match="a -> b -> a"):
        idx.build_index(entries, tmp_path)
    cycles = [f for f in lint_mod.lint_specdir(tmp_path) if f.code == "DEP_CYCLE"]
    assert [(f.spec_id, f.severity) for f in cycles] == [("a", "error"), ("b", "error")]
    assert all("a -> b -> a" in f.message for f in cycles)


def test_duplicate_spec_ids_are_a_hard_error(tmp_path):
    first = _write(tmp_path / "x" / "Foo.md", "# Foo upper\n")
    second = _write(tmp_path / "y" / "foo.md", "# foo lower\n")
    entries = scan_mod.scan_specdir(tmp_path, recursive=True)
    dups = idx.find_duplicate_ids(entries)
    assert list(dups) == ["foo"] and len(dups["foo"]) == 2
    with pytest.raises(idx.SpecIndexError, match="DUP_ID"):
        idx.build_index(entries, tmp_path)
    found = [f for f in lint_mod.lint_specdir(tmp_path, recursive=True) if f.code == "DUP_ID"]
    assert len(found) == 2 and {f.severity for f in found} == {"error"}
    assert str(first.resolve()) in found[0].message + found[1].message
    assert str(second.resolve()) in found[0].message + found[1].message


# ── lint ─────────────────────────────────────────────────────────────────


def test_lint_codes_and_severities():
    assert set(lint_mod.CODES) == {
        "NO_ACCEPTANCE", "NO_VERIFY_CMD", "VAGUE_CRITERIA", "OVERSIZE", "MISSING_SECTIONS",
        "DUP_ID", "DEP_CYCLE", "UI_PROBE_UNREACHABLE",
    }
    errors = {c for c, s in lint_mod.SEVERITY.items() if s == "error"}
    assert errors == {"DEP_CYCLE", "DUP_ID", "NO_ACCEPTANCE"}


def test_lint_fixture_good_clean_bad_dirty():
    findings = lint_mod.lint_specdir(FIXTURE)
    assert [f for f in findings if f.spec_id == "good-feature"] == []
    bad = {f.code: f for f in findings if f.spec_id == "bad-feature"}
    assert {"NO_ACCEPTANCE", "NO_VERIFY_CMD", "VAGUE_CRITERIA", "MISSING_SECTIONS"} <= set(bad)
    assert bad["NO_ACCEPTANCE"].severity == "error"
    assert bad["NO_VERIFY_CMD"].severity == "warning"
    vague = sorted(f.message for f in findings if f.code == "VAGUE_CRITERIA")
    assert [m.split("'")[1] for m in vague] == ["properly", "robust", "seamless", "should work"]
    assert lint_mod.has_errors(findings)
    assert findings == sorted(findings, key=lambda f: (f.spec_id, f.code, f.message))
    assert [f.to_dict() for f in findings] == [f.to_dict() for f in lint_mod.lint_specdir(FIXTURE)]
    assert set(findings[0].to_dict()) == {"spec_id", "code", "severity", "message"}


def test_lint_oversize_reads_env_at_call_time(monkeypatch):
    text = (FIXTURE / "good-feature.md").read_text(encoding="utf-8")
    assert lint_mod.lint_text("g", text) == []
    monkeypatch.setenv("MO_SDD_SPEC_MAX_BYTES", "100")
    [f] = lint_mod.lint_text("g", text)
    assert (f.code, f.severity) == ("OVERSIZE", "warning")
    monkeypatch.setenv("MO_SDD_SPEC_MAX_BYTES", "not-a-number")
    assert lint_mod.lint_text("g", text) == []
    assert [f.code for f in lint_mod.lint_text("g", text, max_bytes=10)] == ["OVERSIZE"]


_CLEAN = """# T
## Inputs
- x
## Acceptance criteria
- returns 3 for input `f(1, 2)`
"""


def test_lint_vague_terms_scoped_to_acceptance_and_configurable(monkeypatch):
    prose = _CLEAN.replace("## Inputs\n- x", "## Inputs\n- loads properly")
    assert lint_mod.lint_text("t", prose) == []  # outside the acceptance section
    in_code = _CLEAN.replace("`f(1, 2)`", "`run --robust`")
    assert lint_mod.lint_text("t", in_code) == []  # inside a code span
    vague = _CLEAN + "- Works Correctly\n"
    [f] = lint_mod.lint_text("t", vague)
    assert f.code == "VAGUE_CRITERIA" and "'correctly'" in f.message and "line 6" in f.message
    assert lint_mod.lint_text("t", _CLEAN + "- incorrectly\n") == []  # word boundary
    monkeypatch.setenv("MO_SDD_VAGUE_TERMS", "returns, fast enough")
    assert [f.code for f in lint_mod.lint_text("t", vague)] == ["VAGUE_CRITERIA"]
    assert "'returns'" in lint_mod.lint_text("t", vague)[0].message
    assert lint_mod.lint_text("t", vague, vague_terms=[]) == []


@pytest.mark.parametrize("body, has_cmd", [
    ("```bash\nmake test\n```\n", True),
    ("```\npytest -q\n```\n", True),
    ("```console\n$ ./run\n```\n", True),
    ("```python\nprint('x')\n```\n", False),
    ("```bash\n```\n", False),
    ("Run `pytest` now.\n", False),
    ("Run `./verify.sh` now.\n", True),
    ("Run `bin/mini-ork` now.\n", True),
    ("Run `make lint` now.\n", True),
])
def test_lint_verify_command_detection(body, has_cmd):
    text = "# T\n## Examples\n## Done when\n- a\n" + body
    codes = [f.code for f in lint_mod.lint_text("t", text)]
    assert ("NO_VERIFY_CMD" not in codes) is has_cmd


def test_lint_headings_inside_fences_do_not_count():
    text = "# T\n```md\n## Acceptance criteria\n## Inputs\n```\nRun `make test`.\n"
    codes = [f.code for f in lint_mod.lint_text("t", text)]
    assert "NO_ACCEPTANCE" in codes and "MISSING_SECTIONS" in codes


@pytest.mark.parametrize("heading", ["Definition of Done", "Success Criteria", "Done when", "ACCEPTANCE"])
def test_lint_acceptance_heading_variants(heading):
    text = f"# T\n## Edge cases\n## {heading}\n- exit code 0 from `make test`\n"
    assert lint_mod.lint_text("t", text) == []


def test_lint_output_independent_of_file_creation_order(tmp_path):
    names = ["b.md", "a.md", "c.md"]
    texts = {"a.md": "# A\n- should work\n", "b.md": "# B\n## Acceptance\n- x\n", "c.md": "# C\n"}
    for order, root in ((names, tmp_path / "one"), (list(reversed(names)), tmp_path / "two")):
        for n in order:
            _write(root / n, texts[n])
    one = [f.to_dict() for f in lint_mod.lint_specdir(tmp_path / "one")]
    two = [f.to_dict() for f in lint_mod.lint_specdir(tmp_path / "two")]
    assert one == two and one


# ── CLI (in-process) ─────────────────────────────────────────────────────


def test_cli_usage_and_bad_args(capsys, tmp_path):
    assert specs_cli.main([]) == 2
    assert specs_cli.main(["help"]) == 0
    assert "Usage: mini-ork specs" in capsys.readouterr().out
    assert specs_cli.main(["frobnicate"]) == 2
    assert specs_cli.main(["lint"]) == 2
    assert specs_cli.main(["lint", str(tmp_path / "missing")]) == 2
    assert specs_cli.main(["list", str(tmp_path / "missing.json")]) == 2


def test_cli_ingest_unbuildable_index_writes_nothing(tmp_path, capsys):
    _write(tmp_path / "specs" / "a.md", "# A\nDepends on: b\n## Acceptance\n- `make t` exits 0\n")
    _write(tmp_path / "specs" / "b.md", "# B\nDepends on: a\n## Acceptance\n- `make t` exits 0\n")
    out = tmp_path / "out" / "spec-index.json"
    assert specs_cli.main(["ingest", str(tmp_path / "specs"), "--out", str(out)]) == 1
    assert not out.exists()
    assert "DEP_CYCLE" in capsys.readouterr().err


def test_cli_ingest_min_specs(tmp_path, monkeypatch, capsys):
    (tmp_path / "empty").mkdir()
    assert specs_cli.main(["ingest", str(tmp_path / "empty")]) == 1
    assert not (tmp_path / "empty" / "spec-index.json").exists()
    assert "MO_SDD_MIN_SPECS" in capsys.readouterr().err
    monkeypatch.setenv("MO_SDD_MIN_SPECS", "0")
    assert specs_cli.main(["ingest", str(tmp_path / "empty")]) == 0
    assert idx.read_index(tmp_path / "empty" / "spec-index.json")["specs"] == {}


# ── CLI (subprocess, real launcher) ──────────────────────────────────────


def _cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    if not BIN.exists():
        pytest.skip("bin/mini-ork missing — subprocess test needs the launcher")
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_ENGINE_ROOT": str(REPO),
        # Stay on this interpreter: the launcher would otherwise re-exec into a
        # (possibly different) project venv.
        "MINI_ORK_USE_VENV": "0",
    }
    return subprocess.run([sys.executable, str(BIN), "specs", *args], cwd=cwd or REPO, env=env,
                          capture_output=True, text=True, timeout=120)


def test_cli_lint_json_round_trip():
    proc = _cli("lint", str(FIXTURE), "--json")
    assert proc.returncode == 1, proc.stderr
    findings = json.loads(proc.stdout)  # stdout carries the JSON array and nothing else
    assert any(f["code"] == "NO_ACCEPTANCE" and f["spec_id"] == "bad-feature" for f in findings)
    assert any(f["code"] == "NO_VERIFY_CMD" and f["spec_id"] == "bad-feature" for f in findings)
    assert all(set(f) == {"spec_id", "code", "severity", "message"} for f in findings)
    assert not [f for f in findings if f["spec_id"] == "good-feature"]


def test_cli_lint_relative_dir_and_human_output():
    proc = _cli("lint", "tests/fixtures/specdir")
    assert proc.returncode == 1
    assert "NO_ACCEPTANCE" in proc.stdout and "1 error(s)" in proc.stdout


def test_cli_ingest_and_list_round_trip(tmp_path):
    out = tmp_path / "spec-index.json"
    proc = _cli("ingest", str(FIXTURE), "--out", str(out))
    assert proc.returncode == 1, proc.stderr  # bad-feature has NO_ACCEPTANCE
    assert proc.stdout.strip() == str(out.resolve())
    index = json.loads(out.read_text(encoding="utf-8"))
    _index_validator().validate(index)
    assert Path(index["root"]).is_absolute()
    assert all(Path(e["source_path"]).is_absolute() for e in index["specs"].values())
    assert not (FIXTURE / "spec-index.json").exists()

    listing = _cli("list", str(out))
    assert listing.returncode == 0, listing.stderr
    rows = [line.split("\t") for line in listing.stdout.splitlines()]
    assert [r[0] for r in rows] == ["bad-feature", "good-feature"]
    assert all(len(r) == 4 and r[1] == "draft" and Path(r[3]).is_absolute() for r in rows)


def test_cli_ingest_clean_dir_exits_zero_with_default_out(tmp_path):
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    shutil.copy(FIXTURE / "good-feature.md", spec_dir / "good-feature.md")
    proc = _cli("ingest", str(spec_dir))
    assert proc.returncode == 0, proc.stderr
    default_out = spec_dir / "spec-index.json"
    assert proc.stdout.strip() == str(default_out.resolve())
    _index_validator().validate(json.loads(default_out.read_text(encoding="utf-8")))
    # Re-ingest ignores the generated index (default glob is *.md).
    assert _cli("ingest", str(spec_dir)).returncode == 0


def test_cli_missing_dir_exits_two(tmp_path):
    proc = _cli("ingest", str(tmp_path / "missing"))
    assert proc.returncode == 2
    assert "not a directory" in proc.stderr


def test_ui_probe_unreachable_warns_without_browser_fence_or_literals():
    text = (
        "# S\n\n## Inputs\n\n- x\n\n## Acceptance criteria\n\n"
        "- AC1: the page renders the badge element\n\n```bash\necho cmd-only\n```\n"
    )
    codes = [f.code for f in lint_mod.lint_text("s", text)]
    assert "UI_PROBE_UNREACHABLE" in codes


def test_ui_probe_reachable_via_browser_fence_is_clean():
    text = (
        "# S\n\n## Inputs\n\n- x\n\n## Acceptance criteria\n\n"
        '- AC1: renders data-testid="a-b-c"\n\n'
        "```bash\nagent-browser open \"$SDD_FE_BASE/en/x\" && agent-browser snapshot -i | grep -q a-b-c\n```\n"
    )
    codes = [f.code for f in lint_mod.lint_text("s", text)]
    assert "UI_PROBE_UNREACHABLE" not in codes


def test_ui_probe_reachable_via_literals_is_clean():
    text = (
        "# S\n\nPage `/en/audience` shows it.\n\n## Inputs\n\n- x\n\n## Acceptance criteria\n\n"
        '- AC1: renders data-testid="a-b-c"\n\n```bash\necho ok\n```\n'
    )
    codes = [f.code for f in lint_mod.lint_text("s", text)]
    assert "UI_PROBE_UNREACHABLE" not in codes
