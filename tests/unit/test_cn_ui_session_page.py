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
    """Scope the CN section cache to this test's tmp_path.

    ``cn_client._section_cache_dir`` reads ``MINI_ORK_HOME`` (defaulting
    to ``.mini-ork`` in cwd). Pointing it at ``tmp_path`` keeps every
    test starting from an empty cache, so a "fetch returns ``{}``" path
    actually reaches the unreachable one-liner instead of falling back
    to a previous test's stale payload.
    """
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    return tmp_path


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
        "domain": "/ps/mini-ork",
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


# ── 7. how_to_test is text only and seeds the thread message ─────────────


def test_how_to_test_is_text_only_and_seeds_thread_message(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``how_to_test`` is plain text and the Re-test cli action is its seed.

    The text rides the ``--text`` argument of the Re-test button's
    ``threads send`` action — that is the thread seed, not a shell
    exec. Every other ``cli`` action in the page must NOT carry the
    ``how_to_test`` string verbatim, and the seed action puts it only
    after ``--text``, never as a positional or another flag.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})

    # 1) how_to_test shows up as plain text in the features section.
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")
    item_texts = [fe["t"] + " " + fe.get("sub", "") for fe in feats["items"]]
    assert any(_HOW in t for t in item_texts), item_texts

    # 2) Walk every dict in the payload and pick out the cli actions.
    _walk(page, accum := [])
    cli_actions = [n for n in accum
                   if isinstance(n.get("cli"), list) and n["cli"]]

    # The action whose --text contains ``how_to_test`` is the seed for
    # the summary-renderer feature.
    seed_actions = [a for a in cli_actions
                    if a["cli"][0] == "threads"
                    and _HOW in (a["cli"][a["cli"].index("--text") + 1]
                                 if "--text" in a["cli"] else "")]
    assert len(seed_actions) == 1, (cli_actions, seed_actions)
    seed = seed_actions[0]["cli"]
    assert seed[0] == "threads"
    assert seed[1] == "send"
    assert seed[2] == _SESSION_ID

    text_idx = seed.index("--text")
    assert _HOW in seed[text_idx + 1]
    # how_to_test must NOT appear as anything other than the --text body.
    bad_positions = [i for i, arg in enumerate(seed)
                     if _HOW in str(arg) and i != text_idx + 1]
    assert not bad_positions, (seed, bad_positions)

    # 3) how_to_test never appears inside another feature's action.
    other_seed = [a for a in cli_actions
                  if a["cli"][0] == "threads" and a["cli"] is not seed]
    for action in other_seed:
        for arg in action["cli"]:
            assert _HOW not in str(arg), (action, arg)


# ── 8. Re-test button is a threads-send cli action ────────────────────────


def test_re_test_action_is_threads_send_with_recipe(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Re-test button dispatches ``mini-ork threads send ... --recipe code-fix``.

    The sweep's ``test_every_cli_action_parses`` walks this action; the
    threads CLI argparse accepts ``id`` as a positional and ``--text``,
    ``--recipe``, ``--home`` as flags.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=_FULL_FEATURES)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")
    actions: list[dict] = []
    for it in feats["items"]:
        actions.extend(it.get("acts") or [])
    cli_actions = [a for a in actions if isinstance(a.get("do"), dict)
                   and isinstance(a["do"].get("cli"), list)]
    assert cli_actions, "no cli action on the Re-test button"
    argv = cli_actions[0]["do"]["cli"]
    assert argv[0] == "threads"
    assert argv[1] == "send"
    assert argv[2] == _SESSION_ID
    assert "--text" in argv
    assert "--recipe" in argv
    assert argv[argv.index("--recipe") + 1] == "code-fix"


# ── 9. files-touched rows union feature files and artifact metadata.path ──


def test_files_section_unions_feature_files_and_artifact_paths(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files-touched lists feature ``files`` AND artifact ``metadata.path``,
    deduped. Relative paths resolve against the session's domain."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    # A feature whose file list overlaps with an artifact record's path.
    features = {"features": [
        {"name": "shared file", "session_id": _SESSION_ID,
         "how_to_test": "x", "files": ["mini_ork/ide_pages/cn_session.py"]},
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
    # Feature-only relative path resolved against domain.
    assert "/ps/mini-ork/mini_ork/cn_client.py" in paths_rendered
    # Artifact absolute path kept verbatim.
    assert "/absolute/path.py" in paths_rendered
    # Deduplication: same path from feature + artifact exactly once.
    target = "/ps/mini-ork/mini_ork/ide_pages/cn_session.py"
    assert paths_rendered.count(target) == 1, paths_rendered


def test_relative_files_resolve_against_project(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path from a feature's ``files`` joins the session's domain."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    features = {"features": [
        {"name": "fn", "session_id": _SESSION_ID, "how_to_test": "z",
         "files": ["mini_ork/ide_pages/cn_session.py"]},
    ]}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY,
             trajectory=_FULL_TRAJECTORY, features=features)
    page = cn_session.build(cn_home, None, {"id": _SESSION_ID})
    files = next(s for s in page["sections"] if s.get("title") == "Files touched")
    open_actions: list[dict] = []
    for it in files["items"]:
        for a in it.get("acts") or []:
            if isinstance(a.get("do"), dict) and "path" in a["do"]:
                open_actions.append(a["do"])
    assert any(a["path"] == "/ps/mini-ork/mini_ork/ide_pages/cn_session.py"
               for a in open_actions), open_actions


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


def test_files_touched_does_not_list_paths_from_foreign_features(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feature from another session carries ``files``; that path MUST
    NOT appear in Files touched. Both "Delivered features" and "Files
    touched" go through the same ``_session_features`` helper, so the
    slice-before-filter bug cannot leak foreign paths into either
    section in isolation."""
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
    assert "/ps/mini-ork/mini_ork/mine.py" in paths_rendered
    # Foreign path must not appear in either resolved or raw form.
    assert not any("secret.py" in p for p in paths_rendered), paths_rendered
    assert not any("foreign" in p for p in paths_rendered), paths_rendered


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
