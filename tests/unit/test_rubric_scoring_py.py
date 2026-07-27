"""Unit tests for mini_ork.gates.rubric_scoring._extract_result_text.

The rubric_prescreen suite (test_rubric_prescreen_py.py) covers the public
helpers, but the internal ``_extract_result_text`` log parser (mirror of
the jq fallbacks formerly at lib/rubric-prescreen.sh lines 126-138) needs
direct coverage. These tests exercise its three extraction strategies in
isolation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import rubric_prescreen as rp
from mini_ork.gates.rubric_scoring import _extract_result_text


def test_missing_log_returns_empty(tmp_path):
    assert _extract_result_text(str(tmp_path / "nope.log")) == ""


def test_strategy1_top_level_result(tmp_path):
    log = tmp_path / "rubric.log"
    log.write_text(json.dumps({"type": "result", "result": "MODEL OUT"}, separators=(",", ":")) + "\n")
    assert _extract_result_text(str(log)) == "MODEL OUT"


def test_strategy1_prefers_result_over_assistant(tmp_path):
    log = tmp_path / "rubric.log"
    lines = [
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": "LEGACY"}]}}),
        json.dumps({"type": "result", "result": "PRIMARY"}, separators=(",", ":")),
    ]
    log.write_text("\n".join(lines) + "\n")
    assert _extract_result_text(str(log)) == "PRIMARY"


def test_strategy2_legacy_stream_json_shape(tmp_path):
    log = tmp_path / "rubric.log"
    log.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "LEGACY TEXT"},
            ]},
        }) + "\n"
    )
    assert _extract_result_text(str(log)) == "LEGACY TEXT"


def test_unparseable_log_returns_empty(tmp_path):
    log = tmp_path / "rubric.log"
    log.write_text("not json at all\n{'still': 'not json'}\n")
    assert _extract_result_text(str(log)) == ""


def test_reexported_from_rubric_prescreen():
    # The SRP split moved the code but kept the import surface.
    assert rp._extract_result_text is _extract_result_text
