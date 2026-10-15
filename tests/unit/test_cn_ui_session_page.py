"""``cn_session`` page — fixture-driven coverage of every section path.

The page is the only consumer of ``/api/v1/sessions/:id/summary`` and
``/api/v1/sessions/:id/trajectory`` in this repo, and the kickoff's c:0
constraint forbids the raw ``/sessions/:id`` dump. Tests stub
``cn_client._get`` (the only network entry point the page calls) so they
are fast and never touch the network themselves; the ``test_no_raw_dump`` case
asserts the stub was never asked for the slow endpoint.

A per-test ``MINI_ORK_HOME`` keeps the section cache scoped to ``tmp_path``;
otherwise a previous test's slot bleeds into the next one and a "fetch
returns {}" assertion passes only because cached_section fell back to
healthy-looking cache data, not because the live fetch was empty.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mini_ork import cn_client
from mini_ork.ide_pages import _cn as CN
from mini_ork.ide_pages import cn_session


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def cn_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scope the CN section cache to this test's tmp_path.

    ``cn_client._section_cache_dir`` reads ``MINI_ORK_HOME`` (defaulting to
    ``.mini-ork`` in cwd). Pointing it at ``tmp_path`` keeps every test
    starting from an empty cache, so a "fetch returns {}" path actually
    reaches the unreachable one-liner instead of falling back to a
    previous test's stale payload.
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
             trajectory: dict | None = None) -> list[str]:
    """Stub ``cn_client._get`` so the page hits fixtures, not the network.

    Returns the list of paths the page asked for, in call order, so
    ``test_no_raw_dump`` can assert no raw ``/sessions/:id`` call ever
    happened.
    """
    paths: list[str] = []

    def _fake_get(path: str, *, timeout: float | None = None) -> str:
        del timeout
        paths.append(path)
        if path.endswith("/summary"):
            return json.dumps(summary) if summary is not None else "{}"
        if path.endswith("/trajectory"):
            return json.dumps(trajectory) if trajectory is not None else "{}"
        return "{}"

    monkeypatch.setattr(cn_client, "_get", _fake_get)
    return paths


# A complete fixture: a run-driven session, with delivered features (one
# carrying a ``how_to_test`` long enough to span lines) and files spread
# across absolute and project-relative paths.
_FULL_SUMMARY: dict = {
    "id": "sess-abc123def456",
    "goal": "Add the cn_session IDE page",
    "project": "/ps/mini-ork",
    "last_activity": "2026-10-15T12:00:00Z",
    "run_id": "run-1792065257-aaaaab",
    "delivered_features": [
        {
            "name": "summary renderer",
            "files": ["mini_ork/ide_pages/cn_session.py"],
            "how_to_test": "Open the cn_session page with a stubbed CN; the "
                           "summary block should render the goal as markdown.",
        },
        {"name": "feature without how_to_test", "files": []},
    ],
    "files_touched": [
        "mini_ork/ide_pages/cn_session.py",
        "/ps/mini-ork/tests/unit/test_cn_ui_session_page.py",
        {"path": "mini_ork/ide_pages/__init__.py"},
    ],
}

_FULL_TRAJECTORY: dict = {
    "decisions": [{"summary": "Use render_cached_section, not raw fetch"}],
    "assumptions": [{"summary": "The two endpoints already exist in CN"}],
    "failures": [],
    "verifications": [
        {"summary": "Page builds ok with empty args"},
        {"summary": "Page still builds ok when CN is down"},
    ],
    "open_user_actions": [{"summary": "Approve the merged session"}],
}

# The single feature whose ``how_to_test`` text the thread-seed assertion
# pins to. Two features → two Re-test actions; the test must look at the
# one whose seed carries the text.
_FEAT_WITH_HOW = _FULL_SUMMARY["delivered_features"][0]
_HOW = _FEAT_WITH_HOW["how_to_test"]


# ── 1. empty state must not blank the page ────────────────────────────────


def test_empty_state_without_id_is_ok_true(cn_home: Path,
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
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session id renders summary, trajectory, features, and files."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch,
                     summary=_FULL_SUMMARY, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})

    assert page["ok"] is True
    # Header: goal drives the title, project + last_activity drive the sub.
    assert page["title"] == _FULL_SUMMARY["goal"]
    assert _FULL_SUMMARY["project"] in page["sub"]
    assert "last activity" in page["sub"]

    # Summary section is markdown with the goal; trajectory section lists
    # the groups; features carry how_to_test; files carry paths.
    titles = [s.get("title") for s in page["sections"]]
    assert "Summary" in titles
    assert "Trajectory" in titles
    assert "Delivered features" in titles
    assert "Files touched" in titles

    # The summary section is a markdown block whose body carries the goal.
    summary_section = next(s for s in page["sections"] if s.get("title") == "Summary")
    assert summary_section["type"] == "markdown"
    assert _FULL_SUMMARY["goal"] in summary_section["text"]

    # Trajectory lists one row per group — failures is empty so it shows
    # the muted "0 · no entries" line, not nothing.
    traj = next(s for s in page["sections"] if s.get("title") == "Trajectory")
    traj_items = traj["items"]
    labels = [it["t"].split(" · ")[0] for it in traj_items]
    assert labels == [label for _key, label in cn_session._TRAJ_GROUPS]
    failure_row = next(it for it in traj_items if it["t"].startswith("Failures"))
    assert "0" in failure_row["t"]

    # Two endpoints, called once each.
    assert any(p.endswith("/summary") for p in paths)
    assert any(p.endswith("/trajectory") for p in paths)


# ── 3. Run button appears only when a run id is extractable ───────────────


def test_run_button_present_when_run_id_extractable(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canonical ``run-<digits>-<short>`` shows the Run header button."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    run_actions = [a for a in page["actions"] if isinstance(a.get("do"), dict)
                   and "run" in a["do"]]
    assert run_actions, "no S.open_run action for a session with run_id"
    assert run_actions[0]["do"]["run"] == _FULL_SUMMARY["run_id"]


def test_run_button_absent_without_canonical_run_id(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary without a canonical run id carries no Run button."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    summary = dict(_FULL_SUMMARY, run_id="not-a-run-id", trace_id="garbage")
    _stub_cn(monkeypatch, summary=summary, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    for action in page["actions"]:
        do = action.get("do")
        if isinstance(do, dict):
            assert "run" not in do


# ── 4. how_to_test appears only as text and in the thread seed ─────────────


def test_how_to_test_is_text_only_and_seeds_thread_message(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``how_to_test`` is shown as plain text and never executed (rule 4).

    The text is allowed to ride the ``--text`` argument of the Re-test
    button's ``threads send`` action — that is the thread seed, not a
    shell exec. Every other ``cli`` action in the page payload must NOT
    carry the how_to_test string verbatim, and the seed action must put
    it only after ``--text``, never as a positional or another flag.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})

    # 1) how_to_test shows up as plain text in the features section.
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")
    item_texts = [fe["t"] + " " + fe.get("sub", "") for fe in feats["items"]]
    assert any(_HOW in t for t in item_texts)

    # 2) Walk every dict in the payload and pick out the cli actions.
    _walk(page, accum := [])
    cli_actions = [n for n in accum
                   if isinstance(n.get("cli"), list) and n["cli"]]

    # The action whose --text contains ``how_to_test`` is the seed for the
    # summary-renderer feature. The other feature's action has no how_to_test
    # in its body at all.
    seed_actions = [a for a in cli_actions
                    if a["cli"][0] == "threads"
                    and _HOW in (a["cli"][a["cli"].index("--text") + 1]
                                 if "--text" in a["cli"] else "")]
    assert len(seed_actions) == 1, (cli_actions, seed_actions)
    seed = seed_actions[0]["cli"]
    assert seed[0] == "threads"
    assert seed[1] == "send"
    assert seed[2] == "sess-abc123def456"

    text_idx = seed.index("--text")
    assert _HOW in seed[text_idx + 1]
    # how_to_test must NOT appear as anything other than the --text body.
    bad_positions = [i for i, arg in enumerate(seed)
                     if _HOW in str(arg) and i != text_idx + 1]
    assert not bad_positions, (seed, bad_positions)

    # 3) how_to_test never appears inside the other feature's action.
    other_seed = [a for a in cli_actions
                  if a["cli"][0] == "threads" and a["cli"] is not seed]
    for action in other_seed:
        for arg in action["cli"]:
            assert _HOW not in str(arg), (action, arg)


# ── 5. Re-test button is a threads-send cli action ────────────────────────


def test_re_test_action_is_threads_send_with_recipe(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Re-test button dispatches ``mini-ork threads send ... --recipe code-fix``.

    The sweep's ``test_every_cli_action_parses`` walks this action; the
    threads CLI argparse accepts ``id`` as a positional and ``--text``,
    ``--recipe``, ``--home`` as flags.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
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
    assert argv[2] == "sess-abc123def456"
    assert "--text" in argv
    assert "--recipe" in argv
    assert argv[argv.index("--recipe") + 1] == "code-fix"


# ── 6. files-touched rows use S.open_path ─────────────────────────────────


def test_files_touched_rows_use_open_path(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each file row carries an Open button whose payload is ``{"path": ...}``."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    files = next(s for s in page["sections"] if s.get("title") == "Files touched")
    rows_with_open = [it for it in files["items"] if it.get("acts")]
    assert rows_with_open, "no file rows carry an Open button"
    for row in rows_with_open:
        acts = row["acts"]
        path_actions = [a for a in acts if isinstance(a.get("do"), dict)
                        and "path" in a["do"]]
        assert path_actions, (row["t"], acts)
        assert path_actions[0]["do"]["path"], path_actions[0]


def test_relative_files_resolve_against_project(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path in ``files_touched`` joins the session's project."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    summary = dict(_FULL_SUMMARY, project="/ps/mini-ork",
                   files_touched=["mini_ork/ide_pages/cn_session.py"])
    _stub_cn(monkeypatch, summary=summary, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    files = next(s for s in page["sections"] if s.get("title") == "Files touched")
    open_actions: list[dict] = []
    for it in files["items"]:
        for a in it.get("acts") or []:
            if isinstance(a.get("do"), dict) and "path" in a["do"]:
                open_actions.append(a["do"])
    assert any(a["path"] == "/ps/mini-ork/mini_ork/ide_pages/cn_session.py"
               for a in open_actions), open_actions


# ── 7. CN down / disabled still renders ok:true ──────────────────────────


def test_unreachable_cn_renders_one_liner(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``cn_client._get`` returns ``{}`` the page still builds ok.

    ``cn_client._get`` returns ``"{}"`` on any network error; the page's
    fetch wraps it in ``json.loads`` → ``{}`` → ``cached_section`` falls
    through to the unreachable payload. The page renders the one-line
    "not running" dot and the page itself stays ``ok: true``.
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

    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    assert page["ok"] is True
    assert "not running" in page["sections"][0]["items"][0]["text"]


def test_disabled_cn_renders_disabled_one_liner(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MO_DISABLE_CN=1`` short-circuits and shows the disabled dot."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    monkeypatch.setattr(cn_client, "_get", lambda *_a, **_k: "{}")
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    assert page["ok"] is True
    text = page["sections"][0]["items"][0]["text"]
    assert text.startswith("off")
    assert "MO_DISABLE_CN=1" in text


# ── 8. c:0 — never ask for the raw /sessions/:id dump ─────────────────────


def test_page_never_calls_raw_session_dump(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page calls ``/summary`` and ``/trajectory`` only — never the raw dump.

    The raw ``/api/v1/sessions/:id`` endpoint takes 12–20 s on large sessions
    (kickoff c:0). The stub records every path the page asks for; this test
    fails loudly if a future change starts fetching the slow endpoint.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch,
                     summary=_FULL_SUMMARY, trajectory=_FULL_TRAJECTORY)
    cn_session.build(cn_home, None, {"id": "sess-abc123def456"})

    assert paths, "no endpoint was called"
    for path in paths:
        # Each path must end in /summary or /trajectory — the raw dump has
        # no suffix, so an empty or bare segment would slip past this check.
        assert path.endswith("/summary") or path.endswith("/trajectory"), path
        # Belt-and-braces: no path may match ``/api/v1/sessions/<id>`` with
        # nothing after the id (the slow shape).
        assert not path.endswith("/sessions/sess-abc123def456"), path


# ── 9. summary section reads goal / project / last_activity ───────────────


def test_summary_section_carries_goal_project_last_activity(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    summary = next(s for s in page["sections"] if s.get("title") == "Summary")
    text = summary["text"]
    assert _FULL_SUMMARY["goal"] in text
    assert _FULL_SUMMARY["project"] in text
    assert _FULL_SUMMARY["last_activity"] in text


# ── 10. URL building URL-encodes the session id ────────────────────────────


def test_url_encodes_special_characters_in_session_id(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session id with a slash or space is percent-encoded in the URL."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    raw_id = "sess with spaces/and/slashes"

    def _fake_get(path: str, *, timeout: float | None = None) -> str:
        del timeout
        return json.dumps(_FULL_SUMMARY) if path.endswith("/summary") else "{}"

    monkeypatch.setattr(cn_client, "_get", _fake_get)
    cn_session.build(cn_home, None, {"id": raw_id})

    # ``_stub_cn`` only records summary; this version doesn't record, but the
    # fetch calls are observable via the patches above.
    # Walk the page sections for any embedded path string isn't possible
    # without access to the stub paths. Instead, probe ``_fetch_summary``
    # directly with the raw id and a captured path.
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


# ── 11. empty trajectory still renders ────────────────────────────────────


def test_empty_trajectory_renders_all_groups_with_zero(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trajectory with all empty groups still lists every group."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    empty_groups = {key: [] for key, _ in cn_session._TRAJ_GROUPS}
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY, trajectory=empty_groups)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    traj = next(s for s in page["sections"] if s.get("title") == "Trajectory")
    labels = [it["t"].split(" · ")[0] for it in traj["items"]]
    assert labels == [label for _key, label in cn_session._TRAJ_GROUPS]
    for item in traj["items"]:
        assert "· 0" in item["t"]


# ── 12. unknown payload shapes degrade to muted rows ─────────────────────


def test_unknown_payload_shapes_render_muted_rows(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trajectory whose groups are not arrays yields muted 0-rows, not crashes."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    weird = {
        "decisions": "not a list",
        "assumptions": None,
        "failures": 7,                      # number, not a list
        "verifications": [{"bogus": "row"}],  # dict without summary/text
        "open_user_actions": [{"summary": "approve merge"}],
    }
    _stub_cn(monkeypatch, summary=_FULL_SUMMARY, trajectory=weird)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    traj = next(s for s in page["sections"] if s.get("title") == "Trajectory")
    labels = [it["t"].split(" · ")[0] for it in traj["items"]]
    assert labels == [label for _key, label in cn_session._TRAJ_GROUPS]
    # Decisions/assumptions/failures had no list payload → muted 0 rows.
    for prefix in ("Decisions", "Assumptions", "Failures"):
        row = next(it for it in traj["items"] if it["t"].startswith(prefix))
        assert "· 0" in row["t"]


# ── 13. spec-level helper sanity ──────────────────────────────────────────


def test_section_status_lines_match_cn_wrapper(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
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


# ── 14. empty features list ──────────────────────────────────────────────


def test_no_features_renders_empty_row(
    cn_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary with no delivered_features renders one muted row, no buttons."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    summary = dict(_FULL_SUMMARY, delivered_features=[], features=[])
    _stub_cn(monkeypatch, summary=summary, trajectory=_FULL_TRAJECTORY)
    page = cn_session.build(cn_home, None, {"id": "sess-abc123def456"})
    feats = next(s for s in page["sections"] if s.get("title") == "Delivered features")
    assert len(feats["items"]) == 1
    assert not feats["items"][0].get("acts"), feats["items"][0]


# ── 15. page registers in PAGES ──────────────────────────────────────────


def test_cn_session_key_in_pages(cn_home: Path) -> None:
    from mini_ork.ide_pages import PAGES, build_page
    assert "cn_session" in PAGES
    # And the builder can be reached through the dispatcher with empty args
    # (the IDE actions sweep builds every page this way).
    page = build_page(cn_home, "cn_session")
    assert page["ok"] is True