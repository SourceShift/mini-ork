"""The advisory rubric pre-screen must MERGE into ``panel-verdict.json``.

Incident (2026-10-12): ``run-1791763809-12583--survey-lens-langgraph--D1`` reached
the pre-screen *after* ``tier4_synth`` had already written the panel's verdict.
The pre-screen opened ``panel-verdict.json`` with ``"w"`` and replaced the whole
file with ``build_panel_verdict``'s five keys — the panel's ``ESCALATE``, its
per-lens probes and its reasons vanished, and the run's own record claimed a
*passing* pre-screen (``pass: true``) with no verdict at all.

``execute.log`` is the receipt that the panel wrote first::

    [info] reviewer verdict=ESCALATE → …--survey-lens-langgraph--D1/panel-verdict.json

so the file must still carry that ESCALATE after the pre-screen runs. These tests
drive :func:`mo_rubric_run_score` with a stubbed dispatcher — no LLM call.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.cli import publisher
from mini_ork.gates import rubric_prescreen

# A rubric payload the extractor accepts (see the schema in rubric_prescreen:
# {"pass": bool, "score": int, "items": [{"label", "verdict"}, …]}).
_RUBRIC = {
    "pass": True,
    "score": 7,
    "items": [{"label": "clarity", "verdict": "PASS"}],
}


def _stub_dispatch(raw: str):
    """A stand-in for ``llm_dispatch.llm_dispatch`` that prints ``raw`` and passes."""

    def _dispatch(argv=None, *, root=None, dispatch_fn=None):
        print(raw)
        return 0

    return _dispatch


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run-1791763809-12583--survey-lens-langgraph--D1"
    d.mkdir()
    return d


def _invoke(run_dir: Path, monkeypatch: pytest.MonkeyPatch, engine_root: Path,
            raw: str) -> None:
    kickoff = run_dir / "kickoff.md"
    kickoff.write_text("# kickoff\n\nDo the thing.\n", encoding="utf-8")
    monkeypatch.setattr(
        "mini_ork.dispatch.llm_dispatch.llm_dispatch", _stub_dispatch(raw))
    rubric_prescreen.mo_rubric_run_score(
        str(kickoff),
        str(run_dir),
        task_class="recursive_validate_impl",
        mini_ork_root=str(engine_root),
        mini_ork_home=str(run_dir.parent),
        mini_ork_db=str(run_dir.parent / "state.db"),
    )


def test_prescreen_does_not_destroy_a_panel_verdict(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: panel wrote first, pre-screen ran second."""
    verdict_path = run_dir / "panel-verdict.json"
    verdict_path.write_text(json.dumps({
        "verdict": "ESCALATE",
        "reasons": ["Rule 2: two lenses converged on fabricated surfaces"],
        "dod_probe_results": [{"id": "AC1", "panel_consensus": "pass"}],
    }), encoding="utf-8")

    _invoke(run_dir, monkeypatch, Path(__file__).resolve().parents[2],
            json.dumps(_RUBRIC))

    data = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert data["verdict"] == "ESCALATE", "the panel's verdict must survive"
    assert data["reasons"], "the blockers the revise loop feeds on must survive"
    # The advisory score is still recorded, namespaced.
    assert data["prescreen"]["source"] == "rubric-prescreen"


def test_prescreen_alone_is_still_recognized_as_one(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no panel verdict, the file is a pre-screen — and reads as one."""
    _invoke(run_dir, monkeypatch, Path(__file__).resolve().parents[2],
            json.dumps(_RUBRIC))

    verdict_path = run_dir / "panel-verdict.json"
    data = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert data["source"] == "rubric-prescreen"
    assert "verdict" not in data
    # The consumer guard must agree (keys ⊆ build_panel_verdict's + prescreen).
    assert publisher.is_rubric_prescreen(str(verdict_path)) is True
