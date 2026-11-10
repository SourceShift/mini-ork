"""The pre-push drift gate must not read "unmeasured" as "clean".

Each layer of `.githooks/pre-push` turns a subprocess's JSON into a verdict.
The ported hook defaulted an unparseable answer to the permissive verdict at
two levels — `PANEL_SKIP` for the gatekeeper and `NO_DRIFT` for the panel — so
a provider outage (or a script that died before printing) let a push to main
through with every README claim unaudited.

These tests pin the failure direction: an unreadable router runs the panel, an
unreadable panel is INDETERMINATE, and INDETERMINATE blocks a main push unless
`MO_README_PANEL_INDETERMINATE=fail-open` says otherwise.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".githooks" / "pre-push"


@pytest.fixture()
def hook(monkeypatch):
    # The hook has no `.py` extension, so name its loader explicitly.
    loader = importlib.machinery.SourceFileLoader("pre_push_hook", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    try:
        loader.exec_module(module)
    except BaseException:
        sys.modules.pop(loader.name, None)
        raise
    monkeypatch.delenv("MO_README_PANEL_INDETERMINATE", raising=False)
    return module


# ── gatekeeper: can't decide → run the audit ─────────────────────────────────


def test_a_dead_gatekeeper_runs_the_panel(hook):
    assert hook._gatekeeper_verdict("") == "PANEL_NEEDED"
    assert hook._gatekeeper_verdict("Traceback (most recent call last):") == "PANEL_NEEDED"


def test_a_quiet_router_still_skips(hook):
    assert hook._gatekeeper_verdict('{"verdict": "PANEL_SKIP"}') == "PANEL_SKIP"
    assert hook._gatekeeper_verdict('{"verdict": "PANEL_NEEDED"}') == "PANEL_NEEDED"


# ── panel: no verdict → INDETERMINATE, never NO_DRIFT ────────────────────────


def test_a_dead_panel_is_indeterminate(hook):
    assert hook._panel_verdict("") == "INDETERMINATE"
    assert hook._panel_verdict("not json") == "INDETERMINATE"


def test_a_real_verdict_is_preserved(hook):
    assert hook._panel_verdict('{"verdict": "NO_DRIFT"}') == "NO_DRIFT"
    assert hook._panel_verdict('{"verdict": "DRIFT"}') == "DRIFT"
    assert hook._panel_verdict('{"verdict": "INDETERMINATE"}') == "INDETERMINATE"


# ── INDETERMINATE blocks by default ─────────────────────────────────────────


def test_indeterminate_blocks_unless_opted_out(hook, monkeypatch):
    assert hook._indeterminate_blocks() is True

    monkeypatch.setenv("MO_README_PANEL_INDETERMINATE", "block")
    assert hook._indeterminate_blocks() is True

    monkeypatch.setenv("MO_README_PANEL_INDETERMINATE", "fail-open")
    assert hook._indeterminate_blocks() is False
