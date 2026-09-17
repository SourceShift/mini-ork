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

        @staticmethod
        def retrieve(query, limit):
            assert (query, limit) == ("Migrate", 2)
            return ('{"hits":[{"id":"a1b2c3d4deadbeefcafebabe00112233"},'
                    '{"id":"e5f6a7b8deadbeefcafebabe00112233"}]}')

        @staticmethod
        def graph_neighbors(node_id, limit):
            assert (node_id, limit) == ("a1b2c3d4deadbeefcafebabe00112233", 5)
            return '{"neighbors":[]}'

        @staticmethod
        def render_graph_neighbors_md(payload, limit):
            assert limit == 5
            return ("--- ContextNest graph — neighbours of the top retrieved memory ---\n"
                    "- a1b2c3d4 w=0.83\n--- /graph neighbours ---\n")

        @staticmethod
        def graph_path(src, dst):
            assert (src, dst) == ("a1b2c3d4deadbeefcafebabe00112233",
                                  "e5f6a7b8deadbeefcafebabe00112233")
            return '{"found": false}'

        @staticmethod
        def render_graph_path_md(payload, limit):
            assert limit == 3
            return ("--- ContextNest graph — how the top two memories connect ---\n"
                    "- a1b2c3d4 -> e5f6a7b8 (2 hops, w=1.23, dijkstra)\n"
                    "--- /graph path ---\n")

    rendered = crp.role_pack_md("planner", brief, cn_available=True, client=Client)

    assert "ContextNest planner pack — substrate digest" in rendered
    assert "abcdef12 (2026-07-20) Earlier plan" in rendered
    # both graph sections, seeded from retrieve's hit ids, are wired into the pack
    assert "--- ContextNest graph — neighbours of the top retrieved memory ---" in rendered
    assert "--- ContextNest graph — how the top two memories connect ---" in rendered


def test_planner_pack_seeds_graph_from_top_hit_ids(tmp_path):
    """The graph reads must be seeded with retrieve's own fragment ids: a
    mis-wired seed silently yields an empty neighbourhood instead of failing."""
    brief = _write(tmp_path, "seed.json",
                   '{"title":"Migrate planner","task_class":"self_migrate"}')
    first, second = "deadbeef" * 4, "cafebabe" * 4
    calls = []

    class Client:
        @staticmethod
        def capsule(query, since):
            return ""

        @staticmethod
        def sessions_by_intent(task_class):
            return '{"sessions":[]}'

        @staticmethod
        def inbox_filtered(urgency, limit):
            return '{"items":[]}'

        @staticmethod
        def render_inbox_md(payload, limit):
            return ""

        @staticmethod
        def basins(project, limit):
            return '{"basins":[]}'

        @staticmethod
        def render_basins_md(payload, limit):
            return ""

        @staticmethod
        def retrieve(query, limit):
            calls.append(("retrieve", query, limit))
            return '{"hits":[{"id":"%s"},{"id":"%s"}]}' % (first, second)

        @staticmethod
        def graph_neighbors(node_id, limit):
            calls.append(("graph_neighbors", node_id, limit))
            return '{"neighbors":[]}'

        @staticmethod
        def render_graph_neighbors_md(payload, limit):
            return ""

        @staticmethod
        def graph_path(src, dst):
            calls.append(("graph_path", src, dst))
            return '{"found": false}'

        @staticmethod
        def render_graph_path_md(payload, limit):
            return ""

    crp.role_pack_md("planner", brief, cn_available=True, client=Client)

    assert calls[0] == ("retrieve", "Migrate", 2)
    assert ("graph_neighbors", first, 5) in calls
    assert ("graph_path", first, second) in calls
