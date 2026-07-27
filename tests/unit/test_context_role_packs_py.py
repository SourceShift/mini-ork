"""Unit tests: mini_ork.steering.context_role_packs (bash parity halves removed; formerly vs lib/context_role_packs.sh).

The deterministic surface is the two brief extractors and the dispatcher's
graceful-degradation contract. Each extractor is driven against real brief
files and asserted on its documented semantics (first significant non-stopword
token for queries; the ``task_class`` JSON field for task class). The role
sub-packs are pure ContextNest orchestration (no CN in a test env → empty), so
role_pack_md is checked only on its deterministic guards (MO_DISABLE_CN,
missing brief, role-required).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.steering import context_role_packs as crp


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# ── extract_query ────────────────────────────────────────────────────────────

QUERY_BRIEFS = [
    ("json_title_objective",
     '{"title": "Kickoff: Wire grounded-rejection into pipeline", '
     '"objective": "Implement the gate"}',
     "grounded-rejection"),
    ("json_no_known_fields", '{"foo": "bar", "n": 3}', ""),
    ("json_task_class_only", '{"task_class": "code_fix"}', "code_fix"),
    ("md_heading_and_fence",
     "# Kickoff: Wire foobar-widget baz\n```python\ncode()\n```\nmore text",
     "foobar-widget"),
    ("md_plain", "Refactor the authentication middleware thoroughly", "Refactor"),
    ("md_all_stopwords_first", "task goal step then implement grounded-signal here",
     "grounded-signal"),
    ("md_inline_backticks", "Fix `the-parser` module now", "the-parser"),
    ("empty", "", ""),
]


@pytest.mark.parametrize("label,content,expect", QUERY_BRIEFS,
                         ids=[b[0] for b in QUERY_BRIEFS])
def test_extract_query(tmp_path, label, content, expect):
    path = _write(tmp_path, f"brief_{label}.txt", content)
    assert crp.extract_query(path) == expect


def test_extract_query_missing_file(tmp_path):
    path = str(tmp_path / "nope.txt")
    assert crp.extract_query(path) == ""


# ── extract_task_class ───────────────────────────────────────────────────────

TC_BRIEFS = [
    ("has_tc", '{"task_class": "code_fix", "title": "x"}', "code_fix"),
    ("empty_tc", '{"task_class": "", "title": "x"}', ""),
    ("no_tc", '{"title": "x"}', ""),
    ("markdown", "# Not JSON\nbody", ""),
    ("json_array", '["a", "b"]', ""),
]


@pytest.mark.parametrize("label,content,expect", TC_BRIEFS, ids=[b[0] for b in TC_BRIEFS])
def test_extract_task_class(tmp_path, label, content, expect):
    path = _write(tmp_path, f"tc_{label}.txt", content)
    assert crp.extract_task_class(path) == expect


def test_extract_task_class_missing_file(tmp_path):
    path = str(tmp_path / "nope.txt")
    assert crp.extract_task_class(path) == ""


# ── role_pack_md degradation contract ────────────────────────────────────────

def test_role_pack_md_disabled(tmp_path):
    brief = _write(tmp_path, "b.json", '{"title": "x"}')
    for role in ("planner", "implementer", "reviewer"):
        assert crp.role_pack_md(role, brief, cn_available=False) == ""


def test_role_pack_md_missing_brief(tmp_path):
    missing = str(tmp_path / "gone.json")
    assert crp.role_pack_md("planner", missing, cn_available=False) == ""


def test_role_pack_md_requires_role():
    # port raises ValueError on an empty role
    with pytest.raises(ValueError, match="role required"):
        crp.role_pack_md("", "/tmp/whatever", cn_available=False)


def test_planner_role_pack_uses_native_contextnest_client(tmp_path):
    brief = _write(tmp_path, "planner.json", '{"title":"Migrate planner","task_class":"self_migrate"}')

    class Client:
        @staticmethod
        def capsule(query, since):
            assert query == "Migrate" and since == "14d"
            return "# Prompt Context\n## Risks\n" + ("x" * 120)

        @staticmethod
        def sessions_by_intent(task_class):
            assert task_class == "self_migrate"
            return '{"sessions":[{"session_id":"abcdef1234","last_seen":"2026-07-20T00:00:00Z","title":"Earlier plan"}]}'

        @staticmethod
        def inbox_filtered(urgency, limit):
            assert (urgency, limit) == ("now", 5)
            return '{"items":[]}'

        @staticmethod
        def render_inbox_md(payload, limit):
            return ""

        @staticmethod
        def basins(project, limit):
            assert limit == 5
            return '{"basins":[]}'

        @staticmethod
        def render_basins_md(payload, limit):
            return ""

    rendered = crp.role_pack_md("planner", brief, cn_available=True, client=Client)

    assert "ContextNest planner pack — substrate digest" in rendered
    assert "abcdef12 (2026-07-20) Earlier plan" in rendered
