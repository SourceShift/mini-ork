"""The three readme-drift layers, ported off bash.

`scripts/readme_drift.py` replaces `readme-drift-{gatekeeper,panel,
providers-doctor}.sh`. These tests pin the parts that were easy to get wrong in
shell and that a port silently changes: the fail-open exit codes, the
"silence is not a verdict" rule in the doctor, the honesty net that overrides
an arbiter claiming NO_DRIFT on a panel where no lens produced evidence, and
the JSON extraction that has to survive a trailing `<z-insight>` block.

No test calls a provider: `dispatch` is replaced with a fake per test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "readme_drift.py"


def _load():
    spec = importlib.util.spec_from_file_location("readme_drift", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


class Reply:
    """The slice of `DispatchResult` these layers read."""

    def __init__(self, text: str = "", *, ok: bool = True, error: str = ""):
        self.text = text
        self.ok = ok
        self.error = error


@pytest.fixture()
def dr(monkeypatch, tmp_path):
    module = _load()
    monkeypatch.setattr(module, "load_secrets", lambda: None)
    monkeypatch.setattr(module, "git", lambda *args: "")
    readme = tmp_path / "README.md"
    readme.write_text("# mini-ork\n\nA task OS.\n", encoding="utf-8")
    monkeypatch.setenv("MO_README", str(readme))
    return module


def _stdout_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# ── first_json ───────────────────────────────────────────────────────────────


def test_first_json_ignores_a_trailing_insight_block(dr):
    raw = '{"verdict": "NO_DRIFT"}\n<z-insight>{"work_unit": {}}</z-insight>'
    assert dr.first_json(raw) == {"verdict": "NO_DRIFT"}


def test_first_json_strips_a_markdown_fence(dr):
    raw = 'Here you go:\n```json\n{"verdict": "DRIFT"}\n```\ntrailing prose'
    assert dr.first_json(raw) == {"verdict": "DRIFT"}


def test_first_json_is_none_without_an_object(dr):
    assert dr.first_json("") is None
    assert dr.first_json("no json here") is None
    assert dr.first_json("[1, 2, 3]") is None
    assert dr.first_json("{not json") is None


# ── gatekeeper ───────────────────────────────────────────────────────────────


def test_gatekeeper_routes_to_the_panel(dr, monkeypatch, capsys):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    monkeypatch.setattr(
        dr, "dispatch", lambda *a, **k: Reply("VERDICT: PANEL_NEEDED\nREASON: counts moved")
    )
    assert dr.gatekeeper() == 0
    payload = _stdout_json(capsys)
    assert payload["verdict"] == "PANEL_NEEDED"
    assert payload["reason"] == "counts moved"


def test_gatekeeper_skips_a_trivial_push(dr, monkeypatch, capsys):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    monkeypatch.setattr(
        dr, "dispatch", lambda *a, **k: Reply("VERDICT: PANEL_SKIP\nREASON: typo only")
    )
    assert dr.gatekeeper() == 1
    assert _stdout_json(capsys)["verdict"] == "PANEL_SKIP"


def test_gatekeeper_missing_key_fails_open_on_stderr(dr, monkeypatch, capsys):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert dr.gatekeeper() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["verdict"] == "PANEL_SKIP"


def test_gatekeeper_unreachable_provider_fails_open(dr, monkeypatch, capsys):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    monkeypatch.setattr(dr, "dispatch", lambda *a, **k: Reply("", ok=False, error="rc=1"))
    assert dr.gatekeeper() == 2
    assert "unreachable" in json.loads(capsys.readouterr().err)["reason"]


def test_gatekeeper_unparseable_answer_fails_open(dr, monkeypatch, capsys):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    monkeypatch.setattr(dr, "dispatch", lambda *a, **k: Reply("I think maybe?"))
    assert dr.gatekeeper() == 2
    assert "unparseable" in json.loads(capsys.readouterr().err)["reason"]


# ── doctor ───────────────────────────────────────────────────────────────────


def _doctor_replies(monkeypatch, dr, replies):
    monkeypatch.setattr(dr, "dispatch", lambda lane, prompt, timeout: replies[lane])


def test_doctor_is_viable_at_the_threshold(dr, monkeypatch, capsys):
    _doctor_replies(
        monkeypatch,
        dr,
        {
            "codex": Reply("codex_lens_OK"),
            "kimi": Reply("kimi_lens_OK"),
            "minimax": Reply("", ok=False, error="rc=1"),
            "glm": Reply("", ok=False, error="rc=1"),
        },
    )
    assert dr.doctor() == 0
    payload = _stdout_json(capsys)
    assert payload["viable"] is True
    assert payload["responsive_count"] == 2


def test_doctor_silence_and_error_strings_do_not_count(dr, monkeypatch, capsys):
    _doctor_replies(
        monkeypatch,
        dr,
        {
            "codex": Reply(""),  # rc=0, empty stdout — a silent fail
            "kimi": Reply("Error: no stdin data received"),
            "minimax": Reply("minimax_lens_OK"),
            "glm": Reply("", ok=False, error="rc=1"),
        },
    )
    assert dr.doctor() == 1
    payload = _stdout_json(capsys)
    assert payload["responsive_count"] == 1
    assert payload["providers"]["codex_lens"]["note"].startswith("rc=0 but empty stdout")
    assert payload["providers"]["kimi_lens"]["note"].startswith("rc=0 but stdout contains")


def test_doctor_honours_the_threshold_knob(dr, monkeypatch, capsys):
    monkeypatch.setenv("MO_DRIFT_MIN_RESPONSIVE_LENSES", "1")
    _doctor_replies(
        monkeypatch,
        dr,
        {
            "codex": Reply("codex_lens_OK"),
            "kimi": Reply("", ok=False, error="rc=1"),
            "minimax": Reply("", ok=False, error="rc=1"),
            "glm": Reply("", ok=False, error="rc=1"),
        },
    )
    assert dr.doctor() == 0
    assert _stdout_json(capsys)["threshold"] == 1


# ── panel ────────────────────────────────────────────────────────────────────


def _panel_replies(monkeypatch, dr, lens_replies, arbiter):
    def fake(lane, prompt, timeout):
        if lane == dr.ARBITER_LANE:
            return arbiter
        return lens_replies[lane]

    monkeypatch.setattr(dr, "dispatch", fake)


def _lens(text: str, verdict: str = "NO_DRIFT", confidence: float = 0.8) -> str:
    return json.dumps(
        {"verdict": verdict, "drifted_claims": [], "confidence": confidence}
    ) + text


def test_panel_reports_no_drift_when_lenses_ran(dr, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MO_DRIFT_RUN_DIR", str(tmp_path / "run"))
    _panel_replies(
        monkeypatch,
        dr,
        {provider: Reply(_lens("")) for _, provider in dr.LANES},
        Reply(json.dumps({"verdict": "NO_DRIFT", "lens_verdicts": {}, "drifted_claims": []})),
    )
    assert dr.panel() == 0
    payload = _stdout_json(capsys)
    assert payload["verdict"] == "NO_DRIFT"
    assert Path(payload["report_path"]).exists()


def test_panel_overrides_nodrift_when_every_lens_failed(dr, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MO_DRIFT_RUN_DIR", str(tmp_path / "run"))
    _panel_replies(
        monkeypatch,
        dr,
        {provider: Reply("", ok=False, error="timeout") for _, provider in dr.LANES},
        # The arbiter *says* NO_DRIFT. Absence of evidence is not evidence of
        # absence, so the honesty net must refuse it.
        Reply(json.dumps({"verdict": "NO_DRIFT", "lens_verdicts": {}, "drifted_claims": []})),
    )
    assert dr.panel() == 1
    payload = _stdout_json(capsys)
    assert payload["verdict"] == "INDETERMINATE"
    assert "all 4 lenses failed" in payload["notes"]


def test_panel_promotes_an_unknown_arbiter_verdict(dr, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MO_DRIFT_RUN_DIR", str(tmp_path / "run"))
    _panel_replies(
        monkeypatch,
        dr,
        {provider: Reply(_lens("")) for _, provider in dr.LANES},
        Reply(json.dumps({"verdict": "MOSTLY_FINE", "lens_verdicts": {}, "drifted_claims": []})),
    )
    assert dr.panel() == 1
    assert _stdout_json(capsys)["verdict"] == "INDETERMINATE"


def test_panel_indeterminate_when_the_arbiter_is_unparseable(dr, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MO_DRIFT_RUN_DIR", str(tmp_path / "run"))
    _panel_replies(
        monkeypatch,
        dr,
        {provider: Reply(_lens("")) for _, provider in dr.LANES},
        Reply("no json at all"),
    )
    assert dr.panel() == 1
    payload = _stdout_json(capsys)
    assert payload["verdict"] == "INDETERMINATE"
    assert "unparseable" in payload["notes"]


def test_cli_rejects_an_unknown_layer(dr, capsys):
    assert dr.main([]) == 2


def test_the_shell_twins_are_gone(dr):
    for name in (
        "readme-drift-gatekeeper.sh",
        "readme-drift-panel.sh",
        "readme-drift-providers-doctor.sh",
    ):
        assert not (REPO / "scripts" / name).exists(), f"{name} still exists"
