"""``cn_session`` page — fixture-driven coverage of every section path.

The page is the only consumer of the three CN endpoints
(``/api/v1/sessions/:id/summary``, ``/api/v1/sessions/:id/trajectory``,
``/api/v1/features``) in this repo. The kickoff's c:0 constraint forbids
the raw ``/sessions/:id`` dump — that endpoint takes 12–20 s on large
sessions, so this page never asks for it. Tests stub ``cn_client._get``
(the only network entry point the page calls) so they are fast and never
touch the network themselves; :func:`test_page_never_calls_raw_session_dump`
asserts the stub was never asked for the slow endpoint.

Fixtures use the **nested** CN response shape (everything under
``summary.summary``, records as a flat ``records[]`` keyed by ``kind``,
features as ``features[]`` carrying a ``session_id`` field) verified
against the live CN on 2026-10-15. Older flat shapes are still
defensively accepted by the page (the ``_unpack_summary`` fallback), but
every test here exercises the live wire shape.

A per-test ``MINI_ORK_HOME`` keeps the section cache scoped to
``tmp_path``; otherwise a previous test's slot bleeds into the next one
and a "fetch returns ``{}``" assertion passes only because
``cached_section`` fell back to healthy-looking cache data, not because
the live fetch was empty.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from mini_ork import cn_client
from mini_ork.ide_pages import _cn as CN
from mini_ork.ide_pages import cn_session


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def cn_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Scope the CN section cache to this test's tmp_path AND give the
    page a real ``.mini-ork`` directory whose parent is the project root.

    ``cn_client._section_cache_dir`` reads ``MINI_ORK_HOME`` (defaulting
    to ``.mini-ork`` in cwd). Pointing it at ``tmp_path/proj/.mini-ork``
    keeps every test starting from an empty cache, so a "fetch returns
    ``{}``" path actually reaches the unreachable one-liner instead of
    falling back to a previous test's stale payload.

    The page reads ``home.parent`` as the project root for
    Files-touched path resolution. Tests that need real files on disk
    create them under ``cn_home.parent`` (``tmp_path/proj``) so
    :func:`cn_session._opens_path` can resolve relative paths against
    it; tests that only need to assert "no Open" pick paths that the
    project does not contain.
    """
    project = tmp_path / "proj"
    home = project / ".mini-ork"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return home


# ── helpers ────────────────────────────────────────────────────────────────


def _walk(node: Any, acc: list[dict]) -> None:
    """Collect every dict under ``node`` so asserts can scan them all."""
    if isinstance(node, dict):
        acc.append(node)
        for v in node.values():
            _walk(v, acc)
    elif isinstance(node, list):
        for v in node:
            _walk(v, acc)


def _stub_cn(monkeypatch: pytest.MonkeyPatch, *,
             summary: dict | None = None,
             trajectory: dict | None = None,
             features: dict | None = None) -> list[str]:
    """Stub ``cn_client._get`` so the page hits fixtures, not the network.

    Returns the list of paths the page asked for, in call order, so
    :func:`test_page_never_calls_raw_session_dump` can assert no raw
    ``/sessions/:id`` call ever happened and the features endpoint was
    hit with the right ``since`` window.
    """
    paths: list[str] = []

    def _fake_get(path: str, *, timeout: float | None = None) -> str:
        del timeout
        paths.append(path)
        if path.endswith("/summary"):
            return json.dumps(summary) if summary is not None else "{}"
        if path.endswith("/trajectory"):
            return json.dumps(trajectory) if trajectory is not None else "{}"
        if "/api/v1/features" in path:
            return json.dumps(features) if features is not None else "{}"
        return "{}"

    monkeypatch.setattr(cn_client, "_get", _fake_get)
    return paths


_SESSION_ID = "sess-abc123def456"
_RUN_ID = "run-1792065257-aaaaab"
_RUN_CWD = f"/ps/mini-ork/.mini-ork/runs/{_RUN_ID}"

# A complete nested-summary fixture: every field the kickoff verified on
# 2026-10-15 is present so the page can render each one. Tasks have a
# mix of open / completed / deleted so the open-task gate can fire.
_FULL_SUMMARY: dict = {
    "summary": {
        "goal": "Add the cn_session IDE page",
        "current_state": "Rewriting for the nested response shape",
        "progress": "Summary + open tasks render",
        # ``tooling`` is a category, not a project root. File paths in the
        # fixtures (``mini_ork/...``, ``tests/unit/...``) never start
        # with ``tooling/``, so any future change that re-introduces a
        # domain-join onto a file path lights up the verbatim-render
        # assertions below.
        "domain": "tooling",
        "topics": ["ide", "cn"],
        "top_jobs": ["summary", "trajectory"],
        "facts": ["fixture"],
        "tasks": [
            {"id": "t1", "status": "open", "subject": "Render trajectory"},
            {"id": "t2", "status": "in_progress", "subject": "Wire features"},
            {"id": "t3", "status": "completed", "subject": "Earlier spike"},
            {"id": "t4", "status": "deleted", "subject": "Old idea"},
            {"id": "t5", "status": "needs_review", "subject": "Final review"},
        ],
        "started_at": "2026-10-14T00:00:00Z",
        "last_ts": "2026-10-15T12:00:00Z",
    },
}

# Trajectory as the live CN ships it: a flat ``records[]`` keyed by
# ``kind``. The artifact-kind record carries the run id in
# ``metadata.project_cwd`` so the Run button can render it; another
# artifact record carries a single ``metadata.path`` to test the files
# merge path. ``ts`` controls newest-first ordering inside each group.
_FULL_TRAJECTORY: dict = {
    "records": [
        {"id": "r1", "kind": "decision_made", "content": "Use render_cached_section",
         "ts": 5},
        {"id": "r2", "kind": "assumption", "content": "The two endpoints already exist",
         "ts": 4},
        {"id": "r3", "kind": "verification", "content": "Page builds ok with empty args",
         "ts": 3},
        {"id": "r4", "kind": "verification",
         "content": "Page still builds ok when CN is down", "ts": 2},
        {"id": "r5", "kind": "risk_flag", "content": "Slow CN would block summary",
         "ts": 1},
        {"id": "r6", "kind": "artifact", "content": "Summary renderer",
         "ts": 7, "metadata": {"project_cwd": _RUN_CWD,
                               "path": "mini_ork/ide_pages/cn_session.py"}},
        {"id": "r7", "kind": "artifact", "content": "Tests",
         "ts": 6, "metadata": {"project_cwd": _RUN_CWD,
                               "path": "tests/unit/test_cn_ui_session_page.py"}},
    ],
}

# Features as the live CN ships them — with a ``session_id`` field per
# feature so a page-side filter keeps each session on its own page. Two
# features for this session, one from a different session (must be
# dropped), one feature for this session without ``how_to_test``.
_FULL_FEATURES: dict = {
    "features": [
        {"name": "summary renderer", "session_id": _SESSION_ID,
         "how_to_test": "Open the cn_session page with a stubbed CN; the "
                        "summary block should render the goal as markdown.",
         "files": ["mini_ork/ide_pages/cn_session.py"]},
        {"name": "trajectory grouper", "session_id": _SESSION_ID,
         "how_to_test": "Pass a records[] fixture; the page groups by kind."},
        {"name": "unrelated feature", "session_id": "sess-other-zzzzzz",
         "how_to_test": "Should never appear on this page.",
         "files": ["some/other/file.py"]},
    ],
}

# The single feature whose ``how_to_test`` text the thread-seed
# assertion pins to. Two features → two Re-test actions; the test must
# look at the one whose seed carries the text.
_FEAT_WITH_HOW = _FULL_FEATURES["features"][0]
_HOW = _FEAT_WITH_HOW["how_to_test"]


# ── 1. empty state must not blank the page ────────────────────────────────


def test_empty_state_without_id_is_ok_true(cn_home: Any,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """``{}`` for ``args`` (the IDE actions sweep default) keeps ``ok: true``.

    An id-required page would fail ``test_every_page_builds_with_ok_true``
    because the sweep builds every page key with empty ``args``.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch)
    page = cn_session.build(cn_home, None, {})
    assert page["ok"] is True
    assert page["key"] == "cn_session"
    # Empty state carries no ``cli`` actions (the parser walk would otherwise
    # try to walk something with no subcommand head).
    for action in page["actions"]:
        assert "cli" not in action


# ── 2. full render with a healthy session ────────────────────────────────


def test_full_render_includes_header_and_sections(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session id renders summary, open tasks, trajectory, features, files."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch,
                     summary=_FULL_SUMMARY,
                     trajectory=_FULL_TRAJECTORY,
                     features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})

    assert page["ok"] is True
    # Header: goal drives the title; domain + last_ts drive the sub.
    assert page["title"] == _FULL_SUMMARY["summary"]["goal"]
    assert _FULL_SUMMARY["summary"]["domain"] in page["sub"]
    assert "last activity" in page["sub"]

    titles = [s.get("title") for s in page["sections"]]
    for expected in ("Summary", "Open tasks", "Trajectory",
                     "Delivered features", "Files touched"):
        assert expected in titles, (expected, titles)

    # Summary section is markdown with the nested goal.
    summary_section = next(s for s in page["sections"] if s.get("title") == "Summary")
    assert summary_section["type"] == "markdown"
    assert _FULL_SUMMARY["summary"]["goal"] in summary_section["text"]
    assert _FULL_SUMMARY["summary"]["current_state"] in summary_section["text"]

    # Three endpoints, called once each (the cache slot scope differs).
    assert any(p.endswith("/summary") for p in paths)
    assert any(p.endswith("/trajectory") for p in paths)
    assert any("/api/v1/features" in p for p in paths)


# ── 3. Run button appears only when a .mini-ork/runs/<id> cwd surfaces ───


def test_run_button_present_when_run_id_in_artifact_cwd(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.mini-ork/runs/run-<digits>-<short>`` cwd in an artifact record
    lights up the Run header button."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    run_actions = [a for a in page["actions"] if isinstance(a.get("do"), dict)
                   and "run" in a["do"]]
    assert run_actions, "no S.open_run action for a trajectory with a run cwd"
    assert run_actions[0]["do"]["run"] == _RUN_ID


def test_run_button_absent_without_canonical_run_id(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trajectory without a canonical ``.mini-ork/runs/<id>`` cwd carries no Run button."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    trajectory = {"records": [
        {"id": "r1", "kind": "decision_made", "content": "no run",
         "metadata": {"project_cwd": "/elsewhere/project"}},
    ]}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=trajectory, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    for action in page["actions"]:
        do = action.get("do")
        if isinstance(do, dict):
            assert "run" not in do


# ── 4. open tasks list excludes completed / deleted ──────────────────────


def test_open_tasks_exclude_completed_and_deleted(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed tasks must not appear in the Open tasks section."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    tasks_section = next(s for s in page["sections"] if s.get("title") == "Open tasks")
    texts = [it["t"] for it in tasks_section["items"]]
    # The fixture ships t1 (open), t2 (in_progress), t5 (needs_review) and
    # t3 (completed), t4 (deleted) — only the first three must appear.
    assert "Render trajectory" in texts
    assert "Wire features" in texts
    assert "Final review" in texts
    assert "Earlier spike" not in texts
    assert "Old idea" not in texts


# ── 5. trajectory groups cap at 10 and report "N of M" ───────────────────


def test_trajectory_groups_cap_at_ten_and_show_n_of_m(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each group header reads "… showing K of N" and shows only the newest K."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    # Build a trajectory with 12 verifications, newest-first sorted by ts.
    many = [{"id": f"v{i}", "kind": "verification", "content": f"v{i}",
             "ts": 100 - i} for i in range(12)]
    other = (
        [{"id": f"d{i}", "kind": "decision_made", "content": f"d{i}",
          "ts": 200 - i} for i in range(3)]
        + [{"id": f"a{i}", "kind": "artifact", "content": f"a{i}",
            "ts": 300 - i, "metadata": {"project_cwd": _RUN_CWD}}
           for i in range(1)]
    )
    trajectory = {"records": many + other}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=trajectory, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    traj = next(s for s in page["sections"] if s.get("title") == "Trajectory")
    by_label = {it["t"].split(" · ")[0]: it for it in traj["items"]}
    # Verifications column caps at 10 of 12.
    assert "showing 10 of 12" in by_label["Verifications"]["t"], by_label
    # Decisions have 3 — under cap, so "showing 3 of 3".
    assert "showing 3 of 3" in by_label["Decisions"]["t"], by_label
    # All six known kinds render in order.
    labels = [it["t"].split(" · ")[0] for it in traj["items"]]
    assert labels == [label for _k, label in cn_session._TRAJ_KINDS]


# ── 6. features are filtered client-side by session_id ───────────────────


def test_features_filter_to_this_session_only(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed-session features show only this session's rows, each as an item."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")
    item_names = [it["t"] for it in feats["items"]]
    # Two features for this session; one from another session; the page
    # drops the foreign one.
    assert "summary renderer" in item_names
    assert "trajectory grouper" in item_names
    assert "unrelated feature" not in item_names


# ── 7. how_to_test rides the Re-test thread seed verbatim ─────────────


def test_how_to_test_rides_re_test_thread_seed_verbatim(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``how_to_test`` rides the Re-test button's ``thread`` action
    verbatim — no CLI action, no threads subcommand.

    Each Re-test button has a ``do`` whose only payload is the draft
    agent thread text. The seed for a feature carries that feature's
    name and the verbatim ``how_to_test`` string, so a human reviews
    the test before the new thread runs it. A page-wide walk
    confirms no ``cli`` action survives at all, so the IDE actions
    sweep cannot parse a stale ``--text`` flag against the ``threads``
    subcommand.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})

    # 1) how_to_test shows up as plain text in the features section.
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")
    item_texts = [fe["t"] + " " + fe.get("sub", "") for fe in feats["items"]]
    assert any(_HOW in t for t in item_texts), item_texts

    # 2) Every Re-test button is a ``thread`` action whose text names
    # the feature and quotes the ``how_to_test`` verbatim. The action's
    # ``do`` carries no ``cli`` key, so the actions sweep cannot parse
    # it as a subcommand.
    thread_seeds_by_feature: dict[str, list[str]] = {}
    for feat in feats["items"]:
        name = feat["t"]
        for act in (feat.get("acts") or []):
            do = act.get("do") or {}
            assert not isinstance(do.get("cli"), list), (name, do)
            assert isinstance(do.get("thread"), str), (name, do)
            thread_seeds_by_feature.setdefault(name, []).append(do["thread"])

    summary_seeds = thread_seeds_by_feature.get("summary renderer", [])
    trajectory_seeds = thread_seeds_by_feature.get("trajectory grouper", [])
    assert len(summary_seeds) == 1, thread_seeds_by_feature
    assert len(trajectory_seeds) == 1, thread_seeds_by_feature
    assert _HOW in summary_seeds[0], summary_seeds
    # the other feature's seed must NOT carry this session's
    # how_to_test verbatim
    assert _HOW not in trajectory_seeds[0], trajectory_seeds
    for seed in summary_seeds + trajectory_seeds:
        assert "Re-test the **" in seed, seed

    # 3) Page-wide walk: NO cli action at all, and certainly no cli
    # action whose argv starts with the threads subcommand.
    _walk(page, accum := [])
    cli_actions = [n for n in accum
                   if isinstance(n.get("cli"), list) and n["cli"]]
    assert cli_actions == [], cli_actions
    for n in accum:
        if not isinstance(n.get("cli"), list):
            continue
        assert n["cli"][0] != "threads", (n, n["cli"])


# ── 8. Re-test button is a thread action, not a CLI action ─────────────


def test_re_test_button_is_thread_action_with_no_threads_cli(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Re-test button is a ``thread`` action whose text names the
    feature and quotes its ``how_to_test``; the page emits no
    ``mini-ork threads send ... --recipe`` CLI action.

    The sweep's :func:`test_every_cli_action_parses` walk has nothing
    to parse on this page when features exist: every Re-test button is
    a draft-thread action, not a CLI argv. The page also covers
    itself against a future regression that re-adds a ``cli``
    action — the walk explicitly fails on any ``cli`` whose head is
    the ``threads`` subcommand.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")

    # Every Re-test button is a thread action — one per feature in
    # this session (the unrelated-session feature is filtered out).
    re_test_buttons: list[dict] = []
    for it in feats["items"]:
        for act in (it.get("acts") or []):
            if act.get("label") == "Re-test":
                re_test_buttons.append(act)
    assert len(re_test_buttons) == 2, re_test_buttons
    for btn in re_test_buttons:
        do = btn.get("do") or {}
        assert isinstance(do.get("thread"), str), btn
        assert "cli" not in do, btn

    # The summary-renderer feature's seed carries its how_to_test.
    summary_btn = next(b for b in re_test_buttons
                       if _HOW in (b["do"]["thread"] or ""))
    assert "Re-test the **summary renderer**" in summary_btn["do"]["thread"]

    # Belt-and-braces: a page-wide walk finds no CLI action starting
    # with ``threads`` anywhere in the payload.
    _walk(page, accum := [])
    bad = [a for a in accum
           if isinstance(a.get("cli"), list) and a["cli"]
           and a["cli"][0] == "threads"]
    assert bad == [], bad


# ── 9. files-touched rows render verbatim and dedupe on the raw string ──


def test_files_section_renders_paths_verbatim_and_dedupes_on_raw_string(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files-touched lists feature ``files`` AND artifact ``metadata.path``
    exactly as ContextNest shipped them — never joined onto ``domain``.

    The fixture's ``domain`` is the category-shaped string ``"tooling"``
    so any code that re-introduces a domain-join onto a file path
    lights up the anti-prefix assertion: no rendered path carries the
    ``tooling/`` prefix a domain-prefix join would produce.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    # A feature whose file list overlaps with an artifact record's
    # path; another feature ships a file no artifact mentions; an
    # artifact record carries an absolute path.
    features = {"features": [
        {"name": "shared file", "session_id": _SESSION_ID,
         "how_to_test": "x",
         "files": ["mini_ork/ide_pages/cn_session.py"]},
        {"name": "feature-only", "session_id": _SESSION_ID,
         "how_to_test": "y", "files": ["mini_ork/cn_client.py"]},
    ]}
    trajectory = {"records": [
        {"id": "r1", "kind": "artifact", "content": "share",
         "metadata": {"path": "mini_ork/ide_pages/cn_session.py"}},
        {"id": "r2", "kind": "artifact", "content": "abs",
         "metadata": {"path": "/absolute/path.py"}},
    ]}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=trajectory, features=features)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    files = next(s for s in page["sections"] if s.get("title") == "Files touched")
    paths_rendered = [it["t"] for it in files["items"]]
    # Feature-only relative path rendered as-is, NOT joined onto domain.
    assert "mini_ork/cn_client.py" in paths_rendered, paths_rendered
    # Artifact absolute path kept verbatim.
    assert "/absolute/path.py" in paths_rendered, paths_rendered
    # Deduplication on the raw string: same path from feature + artifact
    # exactly once.
    target = "mini_ork/ide_pages/cn_session.py"
    assert paths_rendered.count(target) == 1, paths_rendered
    # Anti-domain-prefix: no rendered path carries a ``tooling/`` prefix.
    domain = _FULL_SUMMARY["summary"]["domain"]
    assert not any(p.startswith(domain + "/") for p in paths_rendered), paths_rendered


def test_files_open_action_only_for_paths_inside_project_root(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Open button appears only for paths that resolve to an existing
    regular file inside the project root (the parent of the
    ``.mini-ork`` directory the page was built with). Missing files
    and absolute paths outside the project stay plain text.
    """
    # Two real files in the project root (``cn_home.parent``).
    (cn_home.parent / "mini_ork").mkdir(exist_ok=True)
    (cn_home.parent / "mini_ork" / "present.py").write_text(
        "# exists\n", encoding="utf-8")
    (cn_home.parent / "sub_dir").mkdir(exist_ok=True)
    (cn_home.parent / "sub_dir" / "present.py").write_text(
        "# exists\n", encoding="utf-8")

    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    features = {"features": [
        {"name": "mix", "session_id": _SESSION_ID, "how_to_test": "z",
         "files": [
             # existing relative paths → Open
             "mini_ork/present.py",
             "sub_dir/present.py",
             # missing relative path → no Open
             "mini_ork/missing.py",
             # absolute path pointing outside the project → no Open
             "/tmp/elsewhere.py",
         ]},
    ]}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory={"records": []}, features=features)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    files = next(s for s in page["sections"] if s.get("title") == "Files touched")

    # All four paths render verbatim.
    paths_rendered = [it["t"] for it in files["items"]]
    assert paths_rendered == [
        "mini_ork/present.py",
        "sub_dir/present.py",
        "mini_ork/missing.py",
        "/tmp/elsewhere.py",
    ], paths_rendered

    # Per-row Open-action shape.
    opens_by_row: dict[str, list[dict]] = {}
    for it in files["items"]:
        acts = it.get("acts") or []
        opens = [a for a in acts
                 if isinstance(a.get("do"), dict) and "path" in a["do"]]
        opens_by_row[it["t"]] = [a["do"] for a in opens]

    # Existing project files get a single Open button pointing at the
    # verbatim path.
    assert opens_by_row["mini_ork/present.py"] == [{"path": "mini_ork/present.py"}]
    assert opens_by_row["sub_dir/present.py"] == [{"path": "sub_dir/present.py"}]
    # Missing file → no Open action.
    assert opens_by_row["mini_ork/missing.py"] == []
    # Absolute path outside project → no Open action.
    assert opens_by_row["/tmp/elsewhere.py"] == []


def test_files_path_with_dotdot_escaping_root_is_not_opened(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that resolves outside the project root via ``..`` must
    not gain an Open action, even if the target file does happen to
    exist.
    """
    sibling = cn_home.parent.parent / "outside.py"
    sibling.write_text("# outside\n", encoding="utf-8")
    try:
        monkeypatch.delenv("MO_DISABLE_CN", raising=False)
        features = {"features": [
            {"name": "escape", "session_id": _SESSION_ID, "how_to_test": "z",
             "files": ["../outside.py"]},
        ]}
        _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
                 trajectory={"records": []}, features=features)
        page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
        files = next(s for s in page["sections"]
                     if s.get("title") == "Files touched")
        rows = {it["t"]: it for it in files["items"]}
        assert "../outside.py" in rows, rows
        opens = [a for a in (rows["../outside.py"].get("acts") or [])
                 if isinstance(a.get("do"), dict) and "path" in a["do"]]
        assert opens == [], opens
    finally:
        if sibling.exists():
            sibling.unlink()


# ── 10. features section has its OWN cache section ───────────────────────


def test_features_endpoint_called_with_since_window(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The features request carries a ``since=<Nd>`` window so a slow CN
    can't poison the summary / trajectory slots with a 10-day fetch."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
                     trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    cn_session.build(cn_home, None, {"id": _SESSION_ID})
    feature_paths = [p for p in paths if "/api/v1/features" in p]
    assert feature_paths, paths
    for p in feature_paths:
        assert "since=" in p, p
        # No server-side session filter must leak into the request — the
        # endpoint deliberately has no such param and the page must not
        # invent one.
        assert "session_id" not in p and "&id=" not in p, p


# ── 11. CN down / disabled still renders ok:true ─────────────────────────


def test_unreachable_cn_renders_one_liner(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``cn_client._get`` returns ``{}`` the page still builds ok.

    ``cn_client._get`` returns ``"{}"`` on any network error; the
    page's fetch wraps it in ``json.loads`` → ``{}`` → ``cached_section``
    falls through to the unreachable payload. The page renders the
    one-line "not running" dot and the page itself stays ``ok: true``.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_PING_TTL", "0")
    monkeypatch.setenv("CN_TIMEOUT_SEC", "0.2")
    monkeypatch.setenv("CN_PING_TIMEOUT_SEC", "0.2")

    def _always_empty(path: str, *, timeout: float | None = None) -> str:
        del path, timeout
        return "{}"
    monkeypatch.setattr(cn_client, "_get", _always_empty)

    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    assert page["ok"] is True
    assert "not running" in page["sections"][0]["items"][0]["text"]


def test_disabled_cn_renders_disabled_one_liner(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MO_DISABLE_CN=1`` short-circuits and shows the disabled dot."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    monkeypatch.setattr(cn_client, "_get", lambda *_a, **_k: "{}")
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    assert page["ok"] is True
    text = page["sections"][0]["items"][0]["text"]
    assert text.startswith("off")
    assert "MO_DISABLE_CN=1" in text


# ── 12. c:0 — never ask for the raw /sessions/:id dump ────────────────────


def test_page_never_calls_raw_session_dump(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page calls ``/summary`` and ``/trajectory`` and ``/features``
    only — never the raw dump.

    The raw ``/api/v1/sessions/:id`` endpoint takes 12–20 s on large
    sessions (kickoff c:0). The stub records every path the page asks
    for; this test fails loudly if a future change starts fetching the
    slow endpoint.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch,
                     summary=_FULL_SUMMARY,
                     trajectory=_FULL_TRAJECTORY,
                     features=_FULL_FEATURES)
    cn_session.build(cn_home, None, {"id": _SESSION_ID})

    assert paths, "no endpoint was called"
    for path in paths:
        # Each path must be one of the three live endpoints.
        ok = (
            path.endswith("/summary")
            or path.endswith("/trajectory")
            or "/api/v1/features" in path
        )
        assert ok, path
        # Belt-and-braces: no path may match ``/api/v1/sessions/<id>``
        # with nothing after the id (the slow shape).
        assert not path.endswith(f"/sessions/{_SESSION_ID}"), path


# ── 13. URL building URL-encodes the session id ───────────────────────────


def test_url_encodes_special_characters_in_session_id(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session id with a slash or space is percent-encoded in the URL."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    raw_id = "sess with spaces/and/slashes"

    captured: list[str] = []

    def _capture_get(path: str, *, timeout: float | None = None) -> str:
        del timeout
        captured.append(path)
        return json.dumps(_FULL_SUMMARY)

    monkeypatch.setattr(cn_client, "_get", _capture_get)
    cn_session._fetch_summary(raw_id)
    assert captured, "fetch was not called"
    for path in captured:
        assert raw_id not in path, (path, raw_id)
        assert "%2F" in path or "%20" in path, path


# ── 14. empty trajectory still renders ────────────────────────────────────


def test_empty_trajectory_renders_all_groups_with_zero(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trajectory with no records still lists every group."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory={"records": []}, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    traj = next(s for s in page["sections"] if s.get("title") == "Trajectory")
    labels = [it["t"].split(" · ")[0] for it in traj["items"]]
    assert labels == [label for _k, label in cn_session._TRAJ_KINDS]
    for item in traj["items"]:
        assert "· 0" in item["t"]


# ── 15. unknown payload shapes degrade to muted rows ─────────────────────


def test_unknown_payload_shapes_render_muted_rows(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trajectory whose ``records`` is not a list yields muted 0-rows, not crashes."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    weird = {"records": "not a list"}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=weird, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    traj = next(s for s in page["sections"] if s.get("title") == "Trajectory")
    labels = [it["t"].split(" · ")[0] for it in traj["items"]]
    assert labels == [label for _k, label in cn_session._TRAJ_KINDS]
    for prefix in ("Decisions", "Assumptions", "Failures", "Verifications",
                   "Risk flags", "Artifacts"):
        row = next(it for it in traj["items"] if it["t"].startswith(prefix))
        assert "· 0" in row["t"]


# ── 16. spec-level helper sanity ──────────────────────────────────────────


def test_section_status_lines_match_cn_wrapper(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each state (healthy / cached / unreachable / disabled) renders the
    expected one-line shape. Locks in the contract the page relies on."""
    # disabled wins regardless of the network
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    payload = CN.render_cached_section("atoms", "scope", lambda: {"hits": []})
    line = CN.section_status(payload)
    assert line["text"].startswith("off")
    monkeypatch.delenv("MO_DISABLE_CN")
    # unreachable: fetch raises ConnectionError
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_TIMEOUT_SEC", "0.2")
    payload = CN.render_cached_section("atoms", f"unique-{cn_home.name}",
                                       lambda _: (_ for _ in ()).throw(
                                           ConnectionError("refused")))
    line = CN.section_status(payload)
    assert "not running" in line["text"]


# ── 17. empty features list ──────────────────────────────────────────────


def test_no_features_renders_empty_row(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A features payload with no items renders one muted row, no buttons."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features={"features": []})
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")
    assert len(feats["items"]) == 1
    assert not feats["items"][0].get("acts"), feats["items"][0]


# ── 18. summary section reads nested goal / current_state / last_ts ─────


def test_summary_section_carries_nested_goal_and_state(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nested ``summary.summary`` fields surface in the markdown."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    summary = next(s for s in page["sections"] if s.get("title") == "Summary")
    text = summary["text"]
    for needle in (_FULL_SUMMARY["summary"]["goal"],
                   _FULL_SUMMARY["summary"]["current_state"],
                   _FULL_SUMMARY["summary"]["progress"],
                   _FULL_SUMMARY["summary"]["domain"],
                   _FULL_SUMMARY["summary"]["last_ts"]):
        assert needle in text, (needle, text)


# ── 19. page registers in PAGES ──────────────────────────────────────────


def test_cn_session_key_in_pages(cn_home: Any) -> None:
    from mini_ork.ide_pages import PAGES, build_page
    assert "cn_session" in PAGES
    # And the builder can be reached through the dispatcher with empty args
    # (the IDE actions sweep builds every page this way).
    page = build_page(cn_home, "cn_session")
    assert page["ok"] is True


# ── 20. foreign-feature cap does not drop this session's rows ────────────


def test_features_section_keeps_this_sessions_rows_behind_30_foreign(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/api/v1/features`` endpoint has no server-side session filter,
    so the page filters ``session_id == id`` client-side. The filter
    MUST run before any per-page slice — otherwise a long response with
    other sessions' rows ahead of this one's silently drops this
    session's delivered features. Pin with 35 foreign features first."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    foreign = [
        {"name": f"foreign-{i}", "session_id": f"sess-other-{i:04d}",
         "how_to_test": f"foreign how-to {i}",
         "files": [f"foreign/path/{i}.py"]}
        for i in range(35)
    ]
    mine = [
        {"name": "this-session-feature-a", "session_id": _SESSION_ID,
         "how_to_test": "A how-to.",
         "files": ["mini_ork/a.py"]},
        {"name": "this-session-feature-b", "session_id": _SESSION_ID,
         "how_to_test": "B how-to.",
         "files": ["mini_ork/b.py"]},
    ]
    features = {"features": foreign + mine}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=features)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")
    item_names = [it["t"] for it in feats["items"]]
    # Both this-session features must appear; no foreign row may sneak in.
    assert "this-session-feature-a" in item_names, item_names
    assert "this-session-feature-b" in item_names, item_names
    for i in range(35):
        assert f"foreign-{i}" not in item_names, (i, item_names)


# ── 21. Files touched paths come from the same filtered set ──────────────


def test_files_touched_excludes_foreign_features_and_keeps_paths_verbatim(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feature from another session carries ``files``; that path MUST
    NOT appear in Files touched. Both "Delivered features" and "Files
    touched" go through the same ``_session_features`` helper, so the
    slice-before-filter bug cannot leak foreign paths into either
    section in isolation.

    Own-session paths render verbatim — they never gain a
    domain-prefix. With the fixture's category-shaped ``"tooling"``
    domain, an accidental domain-prefix join would produce paths
    starting with ``tooling/``; the assertions below pin the
    verbatim-only contract.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    features = {"features": [
        {"name": "mine", "session_id": _SESSION_ID, "how_to_test": "x",
         "files": ["mini_ork/mine.py"]},
        {"name": "theirs", "session_id": "sess-other-aaaaaa",
         "how_to_test": "y", "files": ["foreign/secret.py"]},
    ]}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=features)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    files = next(s for s in page["sections"] if s.get("title") == "Files touched")
    paths_rendered = [it["t"] for it in files["items"]]
    # Own-session path rendered verbatim.
    assert "mini_ork/mine.py" in paths_rendered, paths_rendered
    # Foreign path must not appear in any form (no resolved, no raw).
    assert not any("secret.py" in p for p in paths_rendered), paths_rendered
    assert not any("foreign" in p for p in paths_rendered), paths_rendered
    # No domain-prefix join.
    domain = _FULL_SUMMARY["summary"]["domain"]
    assert not any(p.startswith(domain + "/") for p in paths_rendered), paths_rendered


# ── 22. missing started_at widens to the 365-day cap, not a tiny window ─


def test_since_window_is_365d_when_started_at_is_missing(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the summary omits ``started_at``, the page must NOT fall
    back to ``last_ts`` (a recent timestamp that would yield a tiny
    window and silently drop this session's older features). The
    kickoff says "cover started_at -> now"; when started_at is
    unknown, the safe default is the 365-day cap."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    summary_no_started = {"summary": {
        "goal": "no started_at here",
        "domain": "/ps/mini-ork",
        # Deliberately no started_at, but a recent last_ts so a naive
        # fallback would request e.g. ``1d`` and miss older features.
        "last_ts": "2026-10-15T12:00:00Z",
        "tasks": [],
    }}
    paths = _stub_cn(monkeypatch, summary=summary_no_started,
                     trajectory={"records": []},
                     features={"features": []})
    cn_session.build(cn_home, None, {"id": _SESSION_ID})
    feature_paths = [p for p in paths if "/api/v1/features" in p]
    assert feature_paths, paths
    for p in feature_paths:
        assert "since=365d" in p, p


def test_since_days_helper_returns_365d_when_started_at_missing() -> None:
    """Direct test of the helper: missing or malformed started_at
    widens to the 365-day cap, not a 1-day window."""
    assert cn_session._since_days({}) == "365d"
    assert cn_session._since_days({"last_ts": "2026-10-15T12:00:00Z"}) == "365d"
    assert cn_session._since_days({"started_at": "not a date"}) == "365d"
    # A real started_at one day ago yields a small window, not the cap.
    one_day_ago = int(time.time()) - 86400
    val = cn_session._since_days({"started_at": one_day_ago})
    assert val.endswith("d")
    assert int(val[:-1]) <= 3  # ~1 day + buffer
    assert int(val[:-1]) >= 1
