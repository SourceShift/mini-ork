"""Standalone unit tests for ``mini_ork.ported.refute_or_promote_gate``.

Replaces the bash-parity gate as part of the bash→Python migration: the
Python port is now the sole implementation, so its coverage no longer runs
``lib/refute_or_promote_gate.sh`` in a subprocess — it asserts the port's
behaviour directly. These pin the deterministic contract the gate must keep
(fabrication schema/formula/template, survival detection, ceiling
comparison, dict-shape asymmetry around ``report_path``, and rc semantics)
independent of any bash oracle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ported.refute_or_promote_gate import (
    check_fabrication_survival,
    generate_fabrications,
)


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

def _seed_fabs(path: Path, n: int, prefix: str | None = None) -> list[dict[str, Any]]:
    generate_fabrications(n, str(path), prefix=prefix)
    return json.loads(path.read_text())


def _clean_findings(path: Path) -> None:
    path.write_text(
        "## Findings\n\n"
        "1. Real issue in src/auth/login.ts:42 — token expiration not validated.\n"
        "2. Race condition in src/db/pool.ts:118 — connection reuse before reset.\n"
        "3. Missing null check in src/api/handler.ts:7.\n"
    )


def _hallucinating_findings(path: Path, cited: list[dict[str, Any]]) -> None:
    text = "## Findings\n\n"
    for x in cited:
        text += f'- {x["path"]}:{x["line"]} — {x["claim"]}\n'
    text += "- src/legitimate/foo.ts:10 — real finding\n"
    path.write_text(text)


# --------------------------------------------------------------------------- #
# generate_fabrications — arg validation
# --------------------------------------------------------------------------- #

class TestGenerateFabricationsValidation:
    def test_count_zero_raises(self, tmp_path: Path):
        with pytest.raises(ValueError):
            generate_fabrications(0, str(tmp_path / "fab.json"))

    def test_count_negative_raises(self, tmp_path: Path):
        with pytest.raises(ValueError):
            generate_fabrications(-3, str(tmp_path / "fab.json"))

    def test_count_non_int_raises(self, tmp_path: Path):
        with pytest.raises(ValueError):
            generate_fabrications("5", str(tmp_path / "fab.json"))  # type: ignore[arg-type]  # deliberate: exercise bash's non-integer rejection path

    def test_count_bool_true_raises(self, tmp_path: Path):
        # bool is a subclass of int in Python; the port explicitly rejects
        # it so `generate_fabrications(True, ...)` cannot masquerade as
        # count=1 (mirrors bash's `[[ "$_count" =~ ^[0-9]+$ ]]` regex gate,
        # which only ever sees a string).
        with pytest.raises(ValueError):
            generate_fabrications(True, str(tmp_path / "fab.json"))  # type: ignore[arg-type]  # deliberate: exercise the bool-is-int guard

    def test_empty_out_path_raises(self):
        with pytest.raises(ValueError):
            generate_fabrications(3, "")


# --------------------------------------------------------------------------- #
# generate_fabrications — schema / formula / template
# --------------------------------------------------------------------------- #

class TestGenerateFabricationsSchema:
    def test_writes_expected_count(self, tmp_path: Path):
        recs = _seed_fabs(tmp_path / "fab.json", 5)
        assert len(recs) == 5

    def test_schema_keys(self, tmp_path: Path):
        recs = _seed_fabs(tmp_path / "fab.json", 3)
        for rec in recs:
            assert set(rec.keys()) == {"id", "path", "line", "claim"}

    def test_line_formula(self, tmp_path: Path):
        recs = _seed_fabs(tmp_path / "fab.json", 5)
        for i, rec in enumerate(recs):
            assert rec["line"] == 30 + (i * 11) % 200

    def test_path_template(self, tmp_path: Path):
        recs = _seed_fabs(tmp_path / "fab.json", 5)
        for rec in recs:
            assert rec["path"] == f"src/{rec['id']}/handler.ts"

    def test_default_prefix_and_id_format(self, tmp_path: Path):
        recs = _seed_fabs(tmp_path / "fab.json", 4)
        for rec in recs:
            assert rec["id"].startswith("__fabricated_")
            suffix = rec["id"][len("__fabricated_"):]
            assert len(suffix) == 7
            int(suffix, 16)  # valid hex

    def test_custom_prefix(self, tmp_path: Path):
        recs = _seed_fabs(tmp_path / "fab.json", 2, prefix="__custom_")
        for rec in recs:
            assert rec["id"].startswith("__custom_")
            assert not rec["id"].startswith("__fabricated_")

    def test_prefix_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MO_REFUTE_PREFIX", "__envprefix_")
        recs = _seed_fabs(tmp_path / "fab.json", 2)
        for rec in recs:
            assert rec["id"].startswith("__envprefix_")

    def test_claim_template_rotation(self, tmp_path: Path):
        # 10 templates rotate by index; id=6 records covers 6 distinct
        # templates and confirms each claim fills in its own id.
        recs = _seed_fabs(tmp_path / "fab.json", 6)
        for rec in recs:
            assert rec["id"] in rec["claim"]

    def test_ids_are_unique(self, tmp_path: Path):
        recs = _seed_fabs(tmp_path / "fab.json", 20)
        ids = [r["id"] for r in recs]
        assert len(ids) == len(set(ids))

    def test_creates_parent_dirs(self, tmp_path: Path):
        out = tmp_path / "nested" / "dir" / "fab.json"
        generate_fabrications(2, str(out))
        assert out.is_file()
        assert len(json.loads(out.read_text())) == 2

    def test_output_ends_with_trailing_newline(self, tmp_path: Path):
        out = tmp_path / "fab.json"
        generate_fabrications(1, str(out))
        assert out.read_text().endswith("\n")


# --------------------------------------------------------------------------- #
# check_fabrication_survival — core verdicts
# --------------------------------------------------------------------------- #

class TestCheckFabricationSurvival:
    def test_clean_validator_grounded(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        _seed_fabs(fabs, 5)
        _clean_findings(findings)

        d, rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "validator_grounded"
        assert d["reason"] == "ok"
        assert d["fp_count"] == 0
        assert d["fp_total"] == 5
        assert d["fp_rate"] == 0.0
        assert d["fp_ceiling"] == pytest.approx(0.1)
        assert d["report_path"] == str(report_dir / "refute-survival.tsv")
        assert Path(d["report_path"]).is_file()

    def test_hallucinating_3_of_5_refute_failed(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        recs = _seed_fabs(fabs, 5)
        _hallucinating_findings(findings, recs[:3])

        d, rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        assert rc == 1
        assert d["verdict"] == "REFUTE_FAILED"
        assert d["reason"] == "high_fp_survival"
        assert d["fp_count"] == 3
        assert d["fp_total"] == 5
        assert d["fp_rate"] == pytest.approx(0.6)
        assert "validator promoted 3 of 5 fabricated findings" in d["rationale"]

    def test_missing_findings_indeterminate_no_report_path(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        _seed_fabs(fabs, 5)
        missing = tmp_path / "does-not-exist.md"

        d, rc = check_fabrication_survival(str(missing), str(fabs), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "indeterminate"
        assert d["reason"] == "missing_inputs"
        assert "report_path" not in d
        assert d["rationale"] == (
            "findings_path or fabrications_json missing; cannot measure"
        )

    def test_missing_fabrications_indeterminate_no_report_path(self, tmp_path: Path):
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        _clean_findings(findings)
        missing = tmp_path / "no-fabs.json"

        d, rc = check_fabrication_survival(str(findings), str(missing), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "indeterminate"
        assert d["reason"] == "missing_inputs"
        assert "report_path" not in d

    def test_empty_findings_path_indeterminate(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        _seed_fabs(fabs, 5)

        d, rc = check_fabrication_survival("", str(fabs), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "indeterminate"
        assert "report_path" not in d

    def test_empty_fabrications_json_path_indeterminate(self, tmp_path: Path):
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        _clean_findings(findings)

        d, rc = check_fabrication_survival(str(findings), "", str(report_dir))

        assert rc == 0
        assert d["verdict"] == "indeterminate"
        assert "report_path" not in d

    def test_empty_fabrications_array_indeterminate_with_report_path(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        fabs.write_text("[]")
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        _clean_findings(findings)

        d, rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "indeterminate"
        assert d["reason"] == "missing_inputs"
        assert "report_path" in d
        assert d["rationale"] == "fabrications_json must be a non-empty array"

    def test_fabrications_json_not_a_list_indeterminate_with_report_path(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        fabs.write_text(json.dumps({"not": "a list"}))
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        _clean_findings(findings)

        d, rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "indeterminate"
        assert "report_path" in d
        assert d["rationale"] == "fabrications_json must be a non-empty array"

    def test_malformed_fabrications_json_indeterminate_with_report_path(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        fabs.write_text("{not valid json")
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        _clean_findings(findings)

        d, rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "indeterminate"
        assert d["reason"] == "missing_inputs"
        assert "report_path" in d
        assert "fabrications_json unreadable" in d["rationale"]

    def test_non_dict_and_no_id_fabrication_entries_are_skipped(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        fabs.write_text(json.dumps([
            "not-a-dict",
            {"path": "src/x/handler.ts", "line": 1, "claim": "no id here"},
            {"id": "", "path": "src/y/handler.ts", "line": 2, "claim": "empty id"},
            {"id": "__fabricated_real01", "path": "src/z/handler.ts", "line": 3,
             "claim": "has __fabricated_real01 in it"},
        ]))
        findings = tmp_path / "findings.md"
        findings.write_text("cites __fabricated_real01 as if real")
        report_dir = tmp_path / "report"
        report_dir.mkdir()

        d, rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        assert rc == 1
        assert d["verdict"] == "REFUTE_FAILED"
        assert d["fp_total"] == 1  # only the entry with a truthy id counts
        assert d["fp_count"] == 1
        assert d["fp_rate"] == pytest.approx(1.0)

    def test_boundary_rate_equals_ceiling_does_not_trigger(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        recs = _seed_fabs(fabs, 10)
        _hallucinating_findings(findings, recs[:1])  # 1/10 == default ceiling 0.1

        d, rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "validator_grounded"
        assert d["fp_count"] == 1
        assert d["fp_total"] == 10
        assert d["fp_rate"] == pytest.approx(0.1)
        assert d["fp_ceiling"] == pytest.approx(0.1)

    def test_custom_ceiling_kwarg_override(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        recs = _seed_fabs(fabs, 5)
        _hallucinating_findings(findings, recs[:2])  # 2/5 = 0.4

        d, rc = check_fabrication_survival(
            str(findings), str(fabs), str(report_dir), ceiling=0.5
        )

        assert rc == 0
        assert d["verdict"] == "validator_grounded"
        assert d["fp_count"] == 2
        assert d["fp_total"] == 5
        assert d["fp_rate"] == pytest.approx(0.4)
        assert d["fp_ceiling"] == pytest.approx(0.5)

    def test_ceiling_from_env_when_kwarg_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MO_REFUTE_FP_CEILING", "0.5")
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        recs = _seed_fabs(fabs, 5)
        _hallucinating_findings(findings, recs[:2])  # 2/5 = 0.4 < 0.5

        d, rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        assert rc == 0
        assert d["verdict"] == "validator_grounded"
        assert d["fp_ceiling"] == pytest.approx(0.5)

    def test_report_dir_from_env_when_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        run_dir = tmp_path / "run_dir"
        monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        _seed_fabs(fabs, 3)
        _clean_findings(findings)

        d, rc = check_fabrication_survival(str(findings), str(fabs))

        assert rc == 0
        assert d["report_path"] == str(run_dir / "refute-survival.tsv")
        assert (run_dir / "refute-survival.tsv").is_file()

    def test_report_dir_defaults_to_dot_when_no_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        _seed_fabs(fabs, 3)
        _clean_findings(findings)

        d, rc = check_fabrication_survival(str(findings), str(fabs))

        assert rc == 0
        assert d["report_path"] == os.path.join(".", "refute-survival.tsv")
        assert (tmp_path / "refute-survival.tsv").is_file()

    def test_report_tsv_content_marks_survived_and_not_survived(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        recs = _seed_fabs(fabs, 3)
        _hallucinating_findings(findings, recs[:1])

        d, _rc = check_fabrication_survival(str(findings), str(fabs), str(report_dir))

        report_path = d["report_path"]
        lines = Path(report_path).read_text().splitlines()
        assert lines[0] == "id\tpath\tline\tsurvived"
        assert len(lines) == 1 + len(recs)
        survived_row = next(ln for ln in lines[1:] if ln.startswith(recs[0]["id"]))
        assert survived_row.endswith("\tyes")
        for rec in recs[1:]:
            row = next(ln for ln in lines[1:] if ln.startswith(rec["id"]))
            assert row.endswith("\tno")

    def test_fp_rate_is_rounded_to_4_digits(self, tmp_path: Path):
        fabs = tmp_path / "fab.json"
        findings = tmp_path / "findings.md"
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        recs = _seed_fabs(fabs, 3)  # 1/3 = 0.333333...
        _hallucinating_findings(findings, recs[:1])

        d, _rc = check_fabrication_survival(
            str(findings), str(fabs), str(report_dir), ceiling=0.9
        )

        assert d["fp_rate"] == round(1 / 3, 4)


# --------------------------------------------------------------------------- #
# Smoke: pure import + arg-validation without any filesystem fixtures
# --------------------------------------------------------------------------- #

class TestImportAndValidationSmoke:
    def test_generate_fabrications_count_zero_raises(self):
        with pytest.raises(ValueError):
            generate_fabrications(0, "/tmp/foo.json")

    def test_generate_fabrications_negative_count_raises(self):
        with pytest.raises(ValueError):
            generate_fabrications(-3, "/tmp/foo.json")

    def test_generate_fabrications_empty_out_path_raises(self):
        with pytest.raises(ValueError):
            generate_fabrications(3, "")

    def test_check_fabrication_survival_missing_inputs_shape(self):
        d, rc = check_fabrication_survival("/no/such/file.md", "/no/such/fabs.json")

        assert rc == 0
        assert d["verdict"] == "indeterminate"
        assert d["reason"] == "missing_inputs"
        assert "report_path" not in d
        assert set(d.keys()) == {
            "verdict", "reason", "fp_count", "fp_total",
            "fp_rate", "fp_ceiling", "rationale",
        }
