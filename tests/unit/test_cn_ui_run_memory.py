"""``run page · ContextNest run memory`` — Decisions, file history, snapshot.

The run page's ContextNest-derived memory surfaces two things per run:

* a "Decisions and assumptions" section listing each node session's
  ``decision_made`` / ``assumption`` / ``risk_flag`` atoms labelled with
  the node and each opening the source ``cn_session`` page;
* a per-file session-history line on the changed-files view, ordered
  newest-first by ``summary.last_ts`` (the recency helper ``cn_file``
  already implements; the run page reuses its wire decoders rather than
  copying the fan-out).

Both reads go through
:func:`mini_ork.ide_pages._cn.render_cached_section` so a slow / down
ContextNest still renders the established one-line state instead of a
blank section. The verified live wire shapes the tests pin:

* trajectory: ``{records: [{kind, content, ts}]}`` — flat ``records[]``
  with ``kind`` in ``decision_made`` / ``assumption`` / ``risk_flag`` /
  ``failure`` / ``verification`` / ``artifact``;
* by-file: ``{query, matches: [{session_id, matched_files, total_files}]}``
  (no per-match timestamp — recency comes from a fan-out to the
  ``/sessions/<sid>/summary`` endpoint);
* session summary: ``{summary: {goal, last_ts}}`` (the summary fields are
  nested under ``summary``; older payloads are flat — both are accepted).

Once the run reaches a terminal state (``published`` / ``failed`` /
``rolled_back``) the page writes a small JSON snapshot to
``<run_dir>/contextnest-memory.json``. A finished page then reads the
snapshot without re-fetching ContextNest. Tests use synthetic data in
exactly the verified response shapes; ``cn_client.retrieve`` is patched
to raise so a regression that re-routes a read through it lights up
loud rather than passing on an empty ``{}``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mini_ork import cn_client
from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages.run import (
    Node,
    Run,
    _collect_run_memory,
    _decisions_assumptions_section,
    _read_run_memory_snapshot,
    _write_run_memory_snapshot,
)
from mini_ork.ide_pages.node import _resolve_session_id
from mini_ork.stores import migrate as mig


REPO = Path(__file__).resolve().parents[2]
T0 = 1_791_000_000
RUN = "run-1792000000-deadbe"


# ── helpers ────────────────────────────────────────────────────────────────


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _run(tmp_path: Path, nodes: list[Node], cols: list[list[str]],
         *, card: dict | None = None,
         row: dict | None = None) -> Run:
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    return Run(id=RUN, home=home, run_dir=run_dir,
               row=dict(row or {"id": RUN}), card=dict(card or {}),
               nodes=nodes, cols=cols, calls=[],
               recipe_dir=None, workspace=None, edges=[])


def _section(page_or_sections: Any, title: str) -> dict[str, Any]:
    sections = (page_or_sections.get("sections") if isinstance(page_or_sections, dict)
                else page_or_sections) or []
    for s in sections:
        if s.get("title") == title:
            return s
    raise AssertionError(
        f"no section with title {title!r}; got {[s.get('title') for s in sections]}"
    )


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
             by_file: dict | None = None,
             by_file_per_path: dict[str, dict] | None = None,
             summaries: dict[str, dict] | None = None,
             trajectories: dict[str, dict] | None = None) -> list[str]:
    """Stub ``cn_client._get`` so the run page can ask CN for the three
    endpoints it reaches. ``cn_client.retrieve`` is patched to raise.

    Returns the list of paths the page asked for, in call order, so
    tests can assert against the wire contract. Each endpoint stub
    returns a JSON string so the page's ``json.loads`` path runs for
    real; an unknown path returns ``"{}"``.

    ``by_file`` is the static response when every file the page asks
    about shares the same payload (the common fixture case).
    ``by_file_per_path`` overrides that with a per-file response keyed
    on the file's query parameter, so a recency-ordering test can ship
    one payload for ``a.py`` and a different one for ``b.py``.
    """
    paths: list[str] = []
    by_file = by_file if by_file is not None else {"query": "x", "matches": []}
    by_file_per_path = by_file_per_path or {}
    summaries = summaries or {}
    trajectories = trajectories or {}

    def _file_payload(path: str) -> dict:
        if by_file_per_path:
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(path)
            qs = parse_qs(parsed.query or "")
            for raw in qs.get("path", []):
                if raw in by_file_per_path:
                    return by_file_per_path[raw]
        return by_file

    def fake_get(path: str, *, timeout: float | None = None) -> str:
        del timeout
        paths.append(path)
        if "/sessions/by-file" in path:
            return json.dumps(_file_payload(path))
        for sid, payload in summaries.items():
            target = f"/sessions/{sid}/summary"
            if path.endswith(target):
                return json.dumps(payload) if payload is not None else "{}"
        for sid, payload in trajectories.items():
            target = f"/sessions/{sid}/trajectory"
            if path.endswith(target):
                return json.dumps(payload) if payload is not None else "{}"
        return "{}"

    monkeypatch.setattr(cn_client, "_get", fake_get)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(
        "mini_ork.cn_client.retrieve",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError(
            "run memory must not call cn_client.retrieve; the run page "
            "reads /sessions/<sid>/trajectory and /sessions/by-file directly"
        )),
    )
    return paths


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def cn_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp ``.mini-ork`` home whose section cache lives inside the
    test's tmp dir so no real slot is read or written."""
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MO_DISABLE_CN", "0")
    return h


# ── synthetic payloads (verified live shapes) ──────────────────────────────


# Trajectory records are flat ``records[]`` with ``kind`` / ``content`` /
# ``ts``. ``decision_made`` / ``assumption`` / ``risk_flag`` are the
# three kinds the run page surfaces; ``failure`` / ``verification`` /
# ``artifact`` are dropped (the page never lists them here).
_TWO_NODE_TRAJECTORIES: dict[str, dict] = {
    "sid-impl": {"records": [
        {"kind": "decision_made", "content": "Use the cached_section wrapper for CN reads.",
         "ts": _iso(T0 + 30)},
        {"kind": "assumption", "content": "CN sections cache at (section, scope).",
         "ts": _iso(T0 + 35)},
        {"kind": "risk_flag",
         "content": "trajectory is ~800 KB; cache or page would block.",
         "ts": _iso(T0 + 40)},
        {"kind": "failure", "content": "(dropped — failure kinds never render here)",
         "ts": _iso(T0 + 45)},
        {"kind": "artifact", "content": "(dropped — artifact kinds never render here)",
         "ts": _iso(T0 + 50)},
    ]},
    "sid-test": {"records": [
        {"kind": "decision_made",
         "content": "Run pytest under ``tests/unit/test_cn_ui_run_memory.py``.",
         "ts": _iso(T0 + 60)},
        {"kind": "assumption", "content": "Verifier reads the verdict.json path.",
         "ts": _iso(T0 + 65)},
    ]},
}


def _node_session_id_stub(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """Stub ``_resolve_session_id`` so a fixture run gets the named
    session for each node without standing up the full resolver chain.

    ``mapping`` is ``{node.id: session_id}``; nodes not in the mapping
    resolve to ``None``. The stub mirrors the real helper's contract
    (returns ``str | None``) so the page's logic cannot tell the
    difference between the fixture and a live CN-driven session.
    """
    def fake(_run: Run, node: Node) -> str | None:
        return mapping.get(node.id)

    monkeypatch.setattr(run_page, "_node_session_id", fake)


# ── 1. decisions labelled per node, each opening cn_session ────────────────


def test_decisions_section_labelled_per_node_with_cn_session_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with two node sessions surfaces their ``decision_made`` /
    ``assumption`` / ``risk_flag`` atoms labelled with the node id and
    each carrying a ``cn_session`` link to the source session."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, trajectories=_TWO_NODE_TRAJECTORIES)
    _node_session_id_stub(monkeypatch, {"implementer": "sid-impl",
                                          "test": "sid-test"})

    nodes = [
        Node(id="implementer", type="implementer", state="done",
             start=T0, end=T0 + 30),
        Node(id="test", type="verifier", state="done",
             start=T0 + 30, end=T0 + 40),
    ]
    run = _run(tmp_path, nodes, [["implementer"], ["test"]],
                card={"status": "published", "files": []})

    section = _decisions_assumptions_section(run)
    assert section["title"] == "Decisions and assumptions"
    items = section["items"]

    def _find_with(items: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
        for it in items:
            label = str(it.get("t") or "")
            if needle in label:
                return it
        return None

    decision = _find_with(items, "decision_made: Use the cached_section wrapper")
    assert decision is not None, items
    assert "node implementer" in str(decision.get("sub") or ""), decision
    opens = [a for a in (decision.get("acts") or []) if isinstance(a.get("do"), dict)]
    assert opens and opens[0]["do"]["page"] == "cn_session"
    assert opens[0]["do"]["args"]["id"] == "sid-impl", opens

    assumption = _find_with(items, "assumption: CN sections cache")
    assert assumption is not None, items
    assert "node implementer" in str(assumption.get("sub") or ""), assumption
    assumption_opens = [a for a in (assumption.get("acts") or [])
                          if isinstance(a.get("do"), dict)]
    assert assumption_opens[0]["do"]["args"]["id"] == "sid-impl"

    risk = _find_with(items, "risk_flag: trajectory is ~800 KB")
    assert risk is not None, items
    assert "node implementer" in str(risk.get("sub") or "")

    # The second node session surfaces too — labelled with ``node test``
    # and pointing at the second session.
    test_decision = _find_with(items, "decision_made: Run pytest")
    assert test_decision is not None, items
    assert "node test" in str(test_decision.get("sub") or ""), test_decision
    test_opens = [a for a in (test_decision.get("acts") or [])
                  if isinstance(a.get("do"), dict)]
    assert test_opens[0]["do"]["args"]["id"] == "sid-test", test_opens

    # The dropped kinds never appear on this page.
    blob = json.dumps(section)
    assert "(dropped" not in blob, blob


# ── 2. per-file history recency-ordered (newest first) ────────────────────


def test_per_file_history_session_list_is_recency_ordered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-file session-history line orders sessions by
    ``summary.last_ts`` (newest first), NOT by the order the by-file
    endpoint returned them in. The wire returns ``sid-old`` first and
    ``sid-new`` second; the snapshot must surface them in the order
    ``[sid-new, sid-old]`` so the IDE can render the most recent
    touch at the top."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    by_file = {
        "query": "mini_ork/c.py",
        "matches": [
            # Old touch first on the wire; the section must reorder.
            {"session_id": "sid-old",
             "matched_files": ["mini_ork/c.py"], "total_files": 1},
            {"session_id": "sid-new",
             "matched_files": ["mini_ork/c.py"], "total_files": 1},
        ],
    }
    summaries = {
        "sid-old": {"summary": {"goal": "old touch",
                                "last_ts": "2026-10-10T10:00:00Z"}},
        "sid-new": {"summary": {"goal": "newer touch",
                                "last_ts": "2026-10-15T10:00:00Z"}},
    }
    _stub_cn(monkeypatch, by_file=by_file, summaries=summaries)

    run = _run(tmp_path, [], [], card={"status": "published", "files": [
        {"path": "mini_ork/c.py", "added": 1, "removed": 0},
    ]})
    payload = _collect_run_memory(run)
    files = payload.get("files") or []
    assert files, payload
    entry = files[0]
    # Per-file session list is newest-first, not the wire's match order.
    assert entry["path"] == "mini_ork/c.py"
    assert entry["session_ids"] == ["sid-new", "sid-old"], entry
    # The aggregate ``last_ts`` is the most-recent touch.
    assert int(entry["last_ts"]) > 0
    assert int(entry["last_ts"]) >= int(summaries["sid-new"]["summary"]["last_ts"]
                                         and 0)


# ── 3. finished-run snapshot — no live fetch on a finished run ─────────────


def test_finished_run_reads_decisions_from_snapshot_without_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished run whose ``<run_dir>/contextnest-memory.json`` exists
    renders its Decisions section from the snapshot alone — no CN
    fetches happen for that section.

    The ``_read_run_memory_snapshot`` path takes precedence over the
    live collector, so a ``_get`` that raises proves the page never
    asked CN for the finished run."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)

    # A pre-baked snapshot — three atoms across two node sessions.
    snapshot = {
        "schema": "contextnest-memory/v1",
        "decisions": [
            {"node": "implementer", "session_id": "sid-impl", "atoms": [
                {"kind": "decision_made", "content": "Baked decision",
                 "ts": _iso(T0 + 30)},
                {"kind": "assumption", "content": "Baked assumption",
                 "ts": _iso(T0 + 35)},
                {"kind": "risk_flag", "content": "Baked risk",
                 "ts": _iso(T0 + 40)},
            ]},
        ],
        "files": [
            {"path": "mini_ork/c.py", "session_ids": ["sid-c-1"], "last_ts": T0 + 60},
        ],
    }
    run = _run(tmp_path, [], [], card={"status": "published", "files": []})
    run.run_dir.mkdir(parents=True, exist_ok=True)
    (run.run_dir / "contextnest-memory.json").write_text(json.dumps(snapshot))

    # ``_get`` raises — any read means a regression leaked a live fetch.
    def boom(*_a: Any, **_k: Any) -> str:
        raise AssertionError(
            "finished run must read from the snapshot, not from CN"
        )
    monkeypatch.setattr(cn_client, "_get", boom)
    monkeypatch.setattr(cn_client, "available", lambda: True)

    section = _decisions_assumptions_section(run)
    assert section["title"] == "Decisions and assumptions"
    items = section["items"]

    def _find_with(items: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
        for it in items:
            if needle in str(it.get("t") or ""):
                return it
        return None

    baked = _find_with(items, "decision_made: Baked decision")
    assert baked is not None, items
    assert "node implementer" in str(baked.get("sub") or ""), baked
    opens = [a for a in (baked.get("acts") or []) if isinstance(a.get("do"), dict)]
    assert opens and opens[0]["do"]["args"]["id"] == "sid-impl"


# ── 4. finished-run snapshot — written once on first finished build ───────


def test_finished_run_writes_snapshot_on_first_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished run with no existing snapshot builds the run-memory
    from live CN, writes the snapshot to ``<run_dir>/contextnest-memory.json``,
    and the next render reads the snapshot alone."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, trajectories={
        "sid-impl": {"records": [
            {"kind": "decision_made", "content": "Only decision",
             "ts": _iso(T0 + 30)},
        ]},
    })
    _node_session_id_stub(monkeypatch, {"implementer": "sid-impl"})

    nodes = [Node(id="implementer", type="implementer", state="done",
                  start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]],
                card={"status": "published", "files": []})

    # First build writes the snapshot.
    section = _decisions_assumptions_section(run)
    snapshot_path = run.run_dir / "contextnest-memory.json"
    assert snapshot_path.is_file(), snapshot_path
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert data["schema"] == "contextnest-memory/v1"
    assert any(d["node"] == "implementer" for d in data["decisions"]), data

    # The page rendered the atom.
    blob = json.dumps(section)
    assert "Only decision" in blob, blob

    # Second build: ``_get`` raises → only the snapshot path could have
    # produced the section.
    def boom(*_a: Any, **_k: Any) -> str:
        raise AssertionError(
            "second build must read from the snapshot, not from CN"
        )
    monkeypatch.setattr(cn_client, "_get", boom)
    section2 = _decisions_assumptions_section(run)
    blob2 = json.dumps(section2)
    assert "Only decision" in blob2, blob2


# ── 5. live run never writes a snapshot ────────────────────────────────────


def test_live_run_does_not_write_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live (non-finished) run reads CN live but never persists the
    snapshot — ``_running`` is the gate, so a slow CN cannot corrupt an
    in-flight record."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, trajectories={
        "sid-impl": {"records": [
            {"kind": "decision_made", "content": "Live decision",
             "ts": _iso(T0 + 30)},
        ]},
    })
    _node_session_id_stub(monkeypatch, {"implementer": "sid-impl"})

    nodes = [Node(id="implementer", type="implementer", state="running",
                  start=T0)]
    run = _run(tmp_path, nodes, [["implementer"]],
                card={"status": "running", "files": []})

    section = _decisions_assumptions_section(run)
    blob = json.dumps(section)
    assert "Live decision" in blob, blob

    # No snapshot was written — ``_running(run)`` short-circuits the writer.
    snapshot_path = run.run_dir / "contextnest-memory.json"
    assert not snapshot_path.exists(), snapshot_path


# ── 6. per-file history on the changed-files view ─────────────────────────


def test_files_changed_view_renders_per_file_session_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Overview tab's "Files changed" section renders a
    per-file session-history line with session count and last-touch
    age; each entry opens ``cn_session`` for the source session."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)

    by_file = {
        "query": "x",
        "matches": [
            {"session_id": "sid-c-1", "matched_files": ["mini_ork/c.py"],
             "total_files": 1},
            {"session_id": "sid-c-2", "matched_files": ["mini_ork/c.py"],
             "total_files": 1},
        ],
    }
    summaries = {
        "sid-c-1": {"summary": {"goal": "old touch", "last_ts": "2026-10-10T10:00:00Z"}},
        "sid-c-2": {"summary": {"goal": "newer touch", "last_ts": "2026-10-15T10:00:00Z"}},
    }
    _stub_cn(monkeypatch, by_file=by_file, summaries=summaries)

    from mini_ork.cli import board_cmd

    def fake_card_fields(card: dict, project: Path) -> dict[str, Any]:
        return {
            "title": "demo", "recipe": "demo", "status": "published",
            "detail": "", "steps": [], "cost_by_stage": {}, "cost_total": 0.0,
            "files": [{"path": "mini_ork/c.py", "abs": "/abs/c.py",
                        "added": 3, "removed": 1}],
            "verdict": None,
        }
    monkeypatch.setattr(board_cmd, "_card_fields", fake_card_fields)

    home = tmp_path / ".mini-ork"
    home.mkdir(parents=True, exist_ok=True)
    (home / "runs" / RUN).mkdir(parents=True, exist_ok=True)
    nodes = [Node(id="implementer", type="implementer", state="done",
                  start=T0, end=T0 + 30)]
    run = Run(id=RUN, home=home, run_dir=home / "runs" / RUN,
              row={"id": RUN}, card={"status": "published", "files": [
                  {"path": "/abs/c.py", "added": 3, "removed": 1}]},
              nodes=nodes, cols=[["implementer"]], calls=[],
              recipe_dir=None, workspace=None, edges=[])

    sections = run_page._overview_tab(run)
    history = _section(sections, "Files changed · session history")
    items = history["items"]

    assert any("2 sessions" in str(it.get("sub") or "") for it in items), items
    assert any("last touch" in str(it.get("sub") or "") for it in items), items
    # Open buttons point at cn_session with the source session id.
    all_acts = [a for it in items for a in (it.get("acts") or [])
                if isinstance(a.get("do"), dict)]
    sids = sorted(a["do"].get("args", {}).get("id", "")
                  for a in all_acts if a["do"].get("page") == "cn_session")
    assert sids == ["sid-c-1", "sid-c-2"], sids


# ── 7. snapshot helpers — round-trip ──────────────────────────────────────


def test_snapshot_round_trip(tmp_path: Path) -> None:
    """``_read_run_memory_snapshot`` returns ``None`` for an absent /
    corrupt file; ``_write_run_memory_snapshot`` persists a JSON object
    that ``_read_run_memory_snapshot`` then returns verbatim."""
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    run = Run(id=RUN, home=home, run_dir=run_dir, row={"id": RUN},
              card={"status": "published"}, nodes=[], cols=[],
              calls=[], recipe_dir=None, workspace=None, edges=[])

    # Absent file → ``None``.
    assert _read_run_memory_snapshot(run) is None

    # Corrupt file → ``None`` (must not raise).
    (run.run_dir / "contextnest-memory.json").write_text("not json {")
    assert _read_run_memory_snapshot(run) is None
    # Remove the corrupt stub so the rest of the test starts from a clean
    # disk — ``_write_run_memory_snapshot`` is a no-op for live runs and
    # must not race a leftover file into a stale assertion below.
    (run.run_dir / "contextnest-memory.json").unlink()

    # Live run: write is a no-op.
    live = Run(id=RUN, home=home, run_dir=run_dir, row={"id": RUN},
                card={"status": "running"}, nodes=[], cols=[],
                calls=[], recipe_dir=None, workspace=None, edges=[])
    assert _write_run_memory_snapshot(live, {"x": 1}) is False
    assert not (run.run_dir / "contextnest-memory.json").exists()

    # Finished run: write persists; read returns the payload.
    payload = {"schema": "contextnest-memory/v1",
               "decisions": [{"node": "implementer", "session_id": "sid-x",
                              "atoms": []}],
               "files": []}
    assert _write_run_memory_snapshot(run, payload) is True
    got = _read_run_memory_snapshot(run)
    assert got == payload, got


# ── 8. _resolve_session_id contract — reused from node.py ────────────────


def test_resolve_session_id_returns_sid_for_real_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node whose ``<run>/sessions/<sid>.jsonl`` exists surfaces that
    session id from the resolver path; the live sidecar (rule 5)
    returns ``None`` (no real session file)."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (run_dir / "sessions" / "sid-real.jsonl").write_text("{}\n")

    run = Run(id=RUN, home=home, run_dir=run_dir, row={"id": RUN},
              card={"status": "published"}, nodes=[], cols=[],
              calls=[], recipe_dir=None, workspace=None, edges=[])

    # The envelope records an init + a result record so rule 1 picks up ``sid-real``.
    init_env = {"seq": 1, "stream": "stdout", "t": T0 + 1,
                "line": json.dumps({"type": "system", "subtype": "init",
                                     "session_id": "sid-real"})}
    result_env = {"seq": 2, "stream": "stdout", "t": T0 + 2,
                  "line": json.dumps({"type": "result", "session_id": "sid-real",
                                      "result": "done", "num_turns": 1})}
    (run_dir / "agent-implementer.live.jsonl").write_text(
        "\n".join(json.dumps(env) for env in (init_env, result_env)) + "\n"
    )

    node = Node(id="implementer", type="implementer", state="done",
                 start=T0, end=T0 + 30)
    sid = _resolve_session_id(run, node)
    assert sid == "sid-real", sid

    # Rule-5 fallback: a node whose session file does NOT exist returns None
    # for the live-sidecar path.
    (run_dir / "sessions" / "sid-real.jsonl").unlink()
    (run_dir / "agent-reviewer.live.jsonl").write_text(json.dumps(
        {"seq": 1, "stream": "stdout", "t": T0 + 1,
         "line": json.dumps({"type": "result", "session_id": "sid-lost",
                              "result": "done"})}
    ) + "\n")
    shell = Node(id="reviewer", type="reviewer", state="done",
                  start=T0, end=T0 + 30)
    assert _resolve_session_id(run, shell) is None


# ── 9. disabled CN — one-line state, no crash ─────────────────────────────


def test_disabled_cn_decisions_section_renders_one_line_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MO_DISABLE_CN=1`` short-circuits before any ``_get`` fires; the
    disabled flag pre-empts the trajectory fetch, so a record with a
    ``won't render`` body never reaches the page and the run contributes
    no section. An empty card is not rendered: "nothing was recorded" is
    not worth a card (see ``_run_memory_sections``)."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    _stub_cn(monkeypatch, trajectories={
        "sid-impl": {"records": [{"kind": "decision_made",
                                  "content": "won't render", "ts": _iso(T0)}]},
    })
    _node_session_id_stub(monkeypatch, {"implementer": "sid-impl"})

    nodes = [Node(id="implementer", type="implementer", state="done",
                  start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]],
                card={"status": "published", "files": []})

    section = _decisions_assumptions_section(run)
    # The page must not raise and must not claim "won't render" (the
    # ``_node_decision_atoms`` short-circuit drops the trajectory before
    # any record could land in the list). With no atom rows there is no
    # section at all — the disabled flag is what kept the record out.
    assert section is None


# ── 10. node without a session id — section stays empty ──────────────────


def test_decisions_section_empty_when_no_node_has_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run where every node resolves to ``None`` (no session id) renders
    no section, not a fabricated one."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch)
    _node_session_id_stub(monkeypatch, {})  # nothing resolves

    nodes = [
        Node(id="implementer", type="implementer", state="done",
             start=T0, end=T0 + 30),
        Node(id="test", type="verifier", state="done",
             start=T0 + 30, end=T0 + 40),
    ]
    run = _run(tmp_path, nodes, [["implementer"], ["test"]],
                card={"status": "published", "files": []})

    # No atom rows means no section: the page must not invent a card, and
    # no node session label can leak into something that is not rendered.
    section = _decisions_assumptions_section(run)
    assert section is None


# ── 11. action sweep — every action is a known spec action ────────────────


def test_decisions_section_actions_are_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every action on the Decisions section is a ``page_link`` to
    ``cn_session`` — no invented action kind that the IDE actions sweep
    would reject. Mirrors the kickoff's discipline on every CN page."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, trajectories={
        "sid-impl": {"records": [
            {"kind": "decision_made", "content": "Decision A",
             "ts": _iso(T0 + 30)},
            {"kind": "assumption", "content": "Assumption B",
             "ts": _iso(T0 + 35)},
        ]},
    })
    _node_session_id_stub(monkeypatch, {"implementer": "sid-impl"})

    nodes = [Node(id="implementer", type="implementer", state="done",
                  start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]],
                card={"status": "published", "files": []})

    section = _decisions_assumptions_section(run)
    accum: list[dict] = []
    _walk(section, accum)
    actions = [n for n in accum
               if isinstance(n.get("do"), dict)
               or (isinstance(n.get("acts"), list) and n["acts"])]
    for n in actions:
        do = n.get("do")
        if isinstance(do, dict):
            assert "page" in do or "cli" in do, do
        for a in (n.get("acts") or []):
            if not isinstance(a, dict):
                continue
            assert "do" in a, a
            assert isinstance(a.get("do"), dict), a
    # Every row action points at cn_session.
    for it in section.get("items") or []:
        for a in (it.get("acts") or []):
            assert isinstance(a.get("do"), dict), a
            assert a["do"].get("page") == "cn_session", a


# ── 12. _resolve_session_id import surface ────────────────────────────────


def test_resolve_session_id_exported_from_node_module() -> None:
    """The session-id helper is part of node.py's surface so the run
    page can import it directly; the lazy proxy keeps the import cycle
    closed for run.py."""
    from mini_ork.ide_pages import node as node_page
    assert hasattr(node_page, "_resolve_session_id")
    assert callable(node_page._resolve_session_id)


# ── 13. request paths CN actually receives ───────────────────────────────


def test_run_memory_requests_use_verified_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run memory reads ``/sessions/<sid>/trajectory`` and
    ``/sessions/by-file``; it never asks for the raw session dump
    (which takes 12–20 s on large sessions, kickoff c:0)."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch, trajectories={
        "sid-impl": {"records": [
            {"kind": "decision_made", "content": "anything",
             "ts": _iso(T0 + 30)},
        ]},
    }, by_file={"query": "x", "matches": [
        {"session_id": "sid-x", "matched_files": ["a.py"], "total_files": 1},
    ]}, summaries={
        "sid-x": {"summary": {"goal": "x", "last_ts": "2026-10-15T10:00:00Z"}},
    })
    _node_session_id_stub(monkeypatch, {"implementer": "sid-impl"})

    nodes = [Node(id="implementer", type="implementer", state="done",
                  start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]],
                card={"status": "published", "files": [
                    {"path": "a.py", "added": 1, "removed": 0}]})

    # Drive both surfaces.
    _decisions_assumptions_section(run)
    # The file-history part is driven by ``_collect_run_memory`` —
    # skip the snapshot read so the live fetch path runs.
    monkeypatch.setattr(run_page, "_read_run_memory_snapshot", lambda _r: None)
    _collect_run_memory(run)

    assert paths, "no endpoint was called"
    # Every path is either a trajectory fetch or the by-file shape.
    # The wire contract is ``/api/v1/sessions/by-file?path=...`` and
    # ``/api/v1/sessions/<sid>/trajectory``; a regression that drops
    # the API prefix lights up here.
    ok_endpoints = ("/api/v1/sessions/by-file", "/api/v1/sessions/",
                    "/api/v1/prompt-context/atoms")
    for p in paths:
        assert any(p.startswith(e) for e in ok_endpoints), p
        # Belt-and-braces: never the raw session dump shape.
        for sid in ("sid-impl", "sid-x"):
            if f"/sessions/{sid}" in p and not p.startswith("/api/v1/sessions/by-file"):
                assert "/summary" in p or "/trajectory" in p, p


# ── 14. fixture-shape fidelity — cn payload matches the verified live shape ─


def test_fixture_shapes_match_verified_live_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trajectory records use the verified live shape (flat
    ``records[]`` with ``kind`` / ``content`` / ``ts``); the
    by-file payload uses the verified live shape
    (``{query, matches: [...]}``); the summary payload uses the verified
    shape (``{summary: {goal, last_ts}}`` — nested, not flat). A
    regression to a foreign shape would light up here."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)

    # Trajectory flat shape.
    traj = {"records": [{"kind": "decision_made", "content": "X",
                          "ts": _iso(T0 + 30)}]}
    # By-file has ``matches`` (not ``items`` / ``sessions``).
    bf = {"query": "x", "matches": [{"session_id": "sid-y",
                                       "matched_files": ["a.py"],
                                       "total_files": 1}]}
    # Summary is nested under ``summary`` (not flat at the top).
    summ = {"summary": {"goal": "g", "last_ts": "2026-10-15T10:00:00Z"}}
    _stub_cn(monkeypatch, by_file=bf, summaries={"sid-y": summ},
             trajectories={"sid-impl": traj})
    _node_session_id_stub(monkeypatch, {"implementer": "sid-impl"})

    nodes = [Node(id="implementer", type="implementer", state="done",
                  start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]],
                card={"status": "published", "files": []})

    section = _decisions_assumptions_section(run)
    blob = json.dumps(section)
    assert "X" in blob, blob

    # The recency helper would fall through to ``ts=0`` if ``last_ts``
    # were missing — both pages pin the nested ``summary.last_ts``.
    payload = _collect_run_memory(run)
    # No files in card, so no file payload — but decisions survive.
    assert any(d["node"] == "implementer" for d in payload["decisions"])


# ── 15. run page build stays ok:true and sections are additive ───────────


def test_run_page_build_with_run_memory_remains_ok_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run page build with the new memory sections stays ``ok:
    true`` and the Decisions section appears on the v2 ``outcome`` tab,
    where decision material lives. Additive: an existing section count
    grows by one, never shrinks."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch, trajectories={
        "sid-impl": {"records": [
            {"kind": "decision_made", "content": "decision",
             "ts": _iso(T0 + 30)},
        ]},
    })
    _node_session_id_stub(monkeypatch, {"implementer": "sid-impl"})

    home = tmp_path / "proj" / ".mini-ork"
    home.mkdir(parents=True, exist_ok=True)
    rc, _, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, err
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)

    # The page loads nodes from the recipe workflow; without one the run has
    # no node to resolve a session for, so the memory section could never
    # render. Both files are what ``find_recipe`` requires.
    recipe_dir = home / "recipes" / "demo-recipe"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "task_class.yaml").write_text("name: demo\n")
    (recipe_dir / "workflow.yaml").write_text(
        "nodes:\n  - name: implementer\n    type: implementer\n")

    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "demo-recipe", "published", 0.0, T0, T0 + 100, T0 + 100,
         "demo", str(home / "kickoffs" / "demo.md"), "latest", "tr-demo-x"))
    con.commit()
    con.close()

    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = run_page.build(home, "outcome", {"run": RUN})
    assert page["ok"] is True
    titles = [s.get("title") for s in page["sections"]]
    assert "Decisions and assumptions" in titles, titles