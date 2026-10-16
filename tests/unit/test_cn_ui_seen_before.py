"""``mini-ork cn seen-before`` and ``mini-ork cn outcome`` — the
project-scoped seen-before cache and the confirm-gated outcome settle.

Two co-designed surfaces — the verbs that read / write the per-run
``<run_dir>/contextnest-seen-before.json`` slot, and the failed outcome /
node pages that read the slot verbatim on render. The page-render path
NEVER calls ``retrieve`` (the verb is the only path that does), and the
stored envelope is the same wire shape the live 2026-10-15 response
delivers: ``hits: [{id, content, importance, similarity, metadata{},
session_id}]``.

The seen-before verb fails closed:

* scope-resolution failure → exit 3, no CN call, no file write.
* retrieve failure (no ``hits`` list, CN down, secure-tier misconfig) →
  exit 4 / 5, no file write.
* existing file → exit 0, no overwrite, no CN call.

The outcome verb is confirm-gated: a typed ``cn outcome`` invocation
asks on the TTY unless ``--yes``; the IDE buttons carry a ``confirm``
payload the IDE itself resolves. The verb reports the response's
``updated`` field (NOT ``delta_applied`` — the live 2026-10-15 wire
shape).

Tests use synthetic data in the verified wire shapes; the page
fixtures patch :mod:`mini_ork.cn_client` at every seam the render path
would otherwise call, then assert ``NOT called`` so a regression that
re-introduces ``retrieve()`` on render fails loud.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork import cn_client
from mini_ork.cli import cn_cmd
from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import outcome as outcome_page
from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages.node import build_node
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-abc123"
NODE_IMPL = "implementer"
T0 = 1_791_000_000


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty mini-ork home (no run, no slot file).

    Pins ``MINI_ORK_HOME`` to ``tmp_path/proj/.mini-ork`` so the
    ``<run_dir>/contextnest-seen-before.json`` slot lands in a per-test
    directory. Project root resolves to ``tmp_path/proj`` so the
    ``--all-projects`` semantics align with the verb's ``git rev-parse
    --show-toplevel``.
    """
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    for k in ("MO_DISABLE_CN", "CN_BASE_URL", "CN_TIER", "CN_TOKEN_KEY",
              "CN_PING_TIMEOUT_SEC", "CN_TIMEOUT_SEC", "CN_CACHE_TTL_SEC",
              "CN_PING_TTL", "MINI_ORK_SECRETS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    return h


@pytest.fixture()
def _no_cn_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Stub every cn_client HTTP seam the page render path could call.

    Returns the per-seam call counter so each test can assert ``NOT
    called`` — the page render path is cache-slot-only.
    """
    calls = {
        "retrieve": 0,
        "_get": 0,
        "_post_json": 0,
        "available": 0,
        "_disabled": 0,
    }

    def _bump(name: str, stub):
        def _wrapped(*args, **kwargs):
            calls[name] += 1
            return stub(*args, **kwargs)
        return _wrapped

    monkeypatch.setattr(cn_client, "retrieve",
                        _bump("retrieve", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "_get",
                        _bump("_get", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "_post_json",
                        _bump("_post_json", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "available",
                        _bump("available", lambda: False))
    monkeypatch.setattr(cn_client, "_disabled",
                        _bump("_disabled", lambda: True))
    return calls


# ── helpers ───────────────────────────────────────────────────────────────


def _write_seen_before(home: Path, *, run_id: str = RUN,
                       node_id: str | None = NODE_IMPL,
                       hits: list[dict[str, Any]] | None = None,
                       query: str = "boom at line 12",
                       ts: int | None = None,
                       project: str = "/proj") -> dict[str, Any]:
    """Persist a seen-before envelope for the page to read.

    Returns the written envelope. The default ``hits`` list is three
    cards in the verified 2026-10-15 wire shape, with distinct
    ``similarity`` values so the page's top-3 ordering is observable.
    """
    if hits is None:
        hits = [
            {"id": "h1", "content": "first match",
             "importance": 0.5, "similarity": 0.91,
             "metadata": {"kind": "atom", "ts": "2026-10-10"},
             "session_id": "sess-aaa"},
            {"id": "h2", "content": "second match",
             "importance": 0.4, "similarity": 0.78,
             "metadata": {"kind": "atom", "ts": "2026-09-10"},
             "session_id": "sess-bbb"},
            {"id": "h3", "content": "third match",
             "importance": 0.3, "similarity": 0.62,
             "metadata": {"kind": "atom", "ts": "2026-08-10"},
             "session_id": "sess-ccc"},
        ]
    if ts is None:
        ts = int(time.time())
    envelope = {
        "run_id": run_id,
        "node_id": node_id,
        "query": query,
        "ts": ts,
        "top_k": 5,
        "count": len(hits),
        "all_projects": False,
        "project": project,
        "hits": hits,
    }
    path = cn_cmd._seen_before_path(run_id)
    assert path, "MINI_ORK_HOME must be set"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh, ensure_ascii=False)
    return envelope


def _seed_failed_run(home: Path, *, run_id: str = RUN,
                     status: str = "failed",
                     node_id: str = NODE_IMPL,
                     finish_reason: str = "error") -> Path:
    """A run row + run dir + lifecycle events so outcome.resolve /
    build_node see the node.

    Seeds:

    * the ``task_runs`` row (so the loader finds the run),
    * a workflow.yaml with one ``implementer`` node (so the loader
      knows what nodes exist),
    * a ``run_events`` row carrying ``node_end`` with the requested
      ``finish_reason`` ("error" for a failed run, "done" for a
      successful one). ``_failing_node`` returns ``(<node_id>, ...)``
      only for an "error" finish; a "done" finish clears that branch
      and the run card classifies as done.
    """
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    # workflow.yaml so ``_load`` finds the implementer node.
    recipe = home / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True, exist_ok=True)
    (recipe / "workflow.yaml").write_text(
        "version: 1\n"
        "task_class: demo\n"
        "nodes:\n"
        f"  - {{name: {node_id}, type: implementer, model_lane: worker}}\n"
    )
    (recipe / "task_class.yaml").write_text(
        "name: demo\ndescription: a demo recipe\n"
    )
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "demo-recipe", status, 0.5, T0, T0 + 120, T0 + 100,
         "demo", str(kickoff), "latest", "tr-demo-1"))
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-start", run_id, "node_start",
         json.dumps({"node_id": node_id, "node_type": "implementer",
                     "model_lane": "worker"}), T0 + 10),
    )
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-end", run_id, "node_end",
         json.dumps({"node_id": node_id, "node_type": "implementer",
                     "model_lane": "worker",
                     "finish_reason": finish_reason}), T0 + 60),
    )
    con.commit()
    con.close()
    return run_dir


# ── 1. parser ─────────────────────────────────────────────────────────────


def test_seen_before_parser_minimal() -> None:
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["seen-before", RUN])
    assert args.subcommand == "seen-before"
    assert args.run_id == RUN
    assert args.node is None
    assert args.all_projects is False
    assert args.top_k == 5
    assert args.json is False


def test_seen_before_parser_with_node() -> None:
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["seen-before", RUN, "--node", NODE_IMPL])
    assert args.node == NODE_IMPL


def test_seen_before_parser_all_projects() -> None:
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["seen-before", RUN, "--all-projects",
                              "--top-k", "3"])
    assert args.all_projects is True
    assert args.top_k == 3


def test_outcome_parser_minimal() -> None:
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["outcome", "atom-1", "success", "--run", RUN])
    assert args.subcommand == "outcome"
    assert args.atom_id == "atom-1"
    assert args.outcome == "success"
    assert args.run == RUN
    assert args.yes is False


def test_outcome_parser_with_yes() -> None:
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["outcome", "atom-1", "failure",
                              "--run", RUN, "--yes", "--json"])
    assert args.yes is True
    assert args.json is True


def test_outcome_parser_rejects_unknown_outcome() -> None:
    """``outcome`` is restricted to ``success`` / ``failure`` — the live
    2026-10-15 wire rejects anything else, so the parser fails before
    the round-trip wastes a budget."""
    parser = cn_cmd.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["outcome", "atom-1", "maybe", "--run", RUN])


def test_cn_source_has_no_home_flag() -> None:
    """The homeless-verb set the IDE sweeps for requires ``"cn"`` to have
    no ``--home`` literal anywhere in its source. ``seen-before`` and
    ``outcome`` both reject ``--home``; the page's ``home=False`` is
    what keeps the IDE from appending a flag the verb rejects."""
    src = Path(cn_cmd.__file__).read_text(encoding="utf-8")
    assert '"--home"' not in src and "'--home'" not in src, (
        "cn_cmd.py must not declare --home; the IDE appends it to every "
        "subcommand unless the action says home=False"
    )


# ── 2. seen-before verb — wire / scope / store ────────────────────────────


def _stub_post_json(monkeypatch: pytest.MonkeyPatch,
                    canned: str = '{"hits": []}') -> dict[str, Any]:
    """Patch ``cn_client._post_json`` (and the ``_disabled()`` /
    ``available()`` gates) so ``_do_seen_before_retrieve`` reaches the
    stub without ever calling ContextNest. Returns the call-record dict
    so the test body can inspect path, body, and timeout."""
    captured: dict[str, Any] = {}

    def _record(path: str, payload: str,
                *, timeout: float | None = None) -> str:
        captured["path"] = path
        captured["body"] = payload
        captured["timeout"] = timeout
        return canned

    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", _record)
    return captured


def test_seen_before_calls_post_json_with_query_and_topk(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``_do_seen_before_retrieve`` POSTs to ``/api/v1/tools/retrieve``
    with ``{"query", "top_k": 5}`` and the ``CN_SEARCH_TIMEOUT_SEC``
    budget — same wire as the search verb's memories mode."""
    captured = _stub_post_json(
        monkeypatch,
        canned=json.dumps({
            "hits": [{"id": "h1", "metadata": {"cwd": "/proj/foo.py"}}],
        }),
    )
    monkeypatch.delenv("CN_SEARCH_TIMEOUT_SEC", raising=False)
    out = cn_cmd._do_seen_before_retrieve("boom", "/proj", False, 5)
    assert captured["path"] == "/api/v1/tools/retrieve"
    assert json.loads(captured["body"]) == {"query": "boom", "top_k": 5}
    assert captured["timeout"] == pytest.approx(90.0)
    assert isinstance(out, list) and out[0]["id"] == "h1"


def test_seen_before_honours_cn_search_timeout_sec(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``CN_SEARCH_TIMEOUT_SEC`` overrides the default 90 s read budget."""
    captured = _stub_post_json(monkeypatch, canned='{"hits": []}')
    monkeypatch.setenv("CN_SEARCH_TIMEOUT_SEC", "12.5")
    cn_cmd._do_seen_before_retrieve("q", "/proj", False, 5)
    assert captured["timeout"] == pytest.approx(12.5)


def test_seen_before_filters_by_project(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Project filter keeps only hits whose path lies under ``project``."""
    payload = json.dumps({
        "hits": [
            {"id": "keep", "metadata": {"cwd": "/proj/foo.py"}},
            {"id": "drop", "metadata": {"cwd": "/other/bar.py"}},
            {"id": "no_path"},
        ],
    })
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: payload)
    out = cn_cmd._do_seen_before_retrieve("q", "/proj", False, 10)
    assert [h["id"] for h in out] == ["keep"]


def test_seen_before_keeps_hits_from_the_projects_worktrees(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit recorded from a worktree still belongs to the project.

    ContextNest attributes a session to its launch directory, and this repo
    runs nearly everything from ``<repo>-worktrees/<slug>``, so the live
    server returns memories whose ``metadata.project_cwd`` is a worktree
    path. Matching the checkout alone dropped every one of them — measured
    live 2026-10-16: 0 hits scoped vs 5 unscoped, all five naming this
    project's own ``mini_ork/ide_pages/*.py``.
    """
    payload = json.dumps({
        "hits": [
            {"id": "checkout", "metadata": {"project_cwd": "/proj/foo.py"}},
            {"id": "worktree",
             "metadata": {"project_cwd": "/proj-worktrees/ide-node-stream/x.py"}},
            {"id": "elsewhere", "metadata": {"project_cwd": "/other/bar.py"}},
        ],
    })
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: payload)
    out = cn_cmd._do_seen_before_retrieve("q", "/proj", False, 10)
    assert [h["id"] for h in out] == ["checkout", "worktree"]


def test_seen_before_still_rejects_a_sibling_name_that_only_shares_a_prefix(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Widening to worktrees must not reopen the string-prefix hole.

    ``/proj2`` merely starts with ``/proj``; a matcher without the
    separator boundary would leak it, which is the same defect
    ContextNest's own ``project=`` filter has.
    """
    payload = json.dumps({
        "hits": [
            {"id": "keep", "metadata": {"project_cwd": "/proj/a.py"}},
            {"id": "leak", "metadata": {"project_cwd": "/proj2/a.py"}},
            {"id": "leak-wt", "metadata": {"project_cwd": "/proj2-worktrees/s/a.py"}},
        ],
    })
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: payload)
    out = cn_cmd._do_seen_before_retrieve("q", "/proj", False, 10)
    assert [h["id"] for h in out] == ["keep"]


def test_project_roots_honours_a_conventionally_named_worktrees_dir(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``MINI_ORK_WORKTREES_DIR`` widens the root set only when it is this
    repo's own worktrees dir — a shared directory of several repos' worktrees
    must not be accepted wholesale."""
    monkeypatch.delenv("MINI_ORK_WORKTREES_DIR", raising=False)
    assert cn_client.project_roots("/proj") == ("/proj", "/proj-worktrees")

    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", "/custom/proj-worktrees")
    assert cn_client.project_roots("/proj") == (
        "/proj", "/proj-worktrees", "/custom/proj-worktrees")

    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", "/shared/all-worktrees")
    assert cn_client.project_roots("/proj") == ("/proj", "/proj-worktrees")

    assert cn_client.project_roots("") == ()
    assert cn_client.filter_by_project_path(
        [{"path": "/proj-worktrees/s/a.py"}, {"path": "/other/a.py"}], "/proj",
    ) == [{"path": "/proj-worktrees/s/a.py"}]


def test_seen_before_all_projects_skips_filter(
        monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({
        "hits": [
            {"id": "keep", "metadata": {"cwd": "/proj/a.py"}},
            {"id": "other", "metadata": {"cwd": "/other/b.py"}},
        ],
    })
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: payload)
    out = cn_cmd._do_seen_before_retrieve("q", "/proj", True, 10)
    assert {h["id"] for h in out} == {"keep", "other"}


def test_seen_before_raises_when_no_hits(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A body without a ``hits`` list raises :class:`RetrieveFailed` —
    exit 4 path. The verb never writes the file on this branch (a
    partial write would poison the page's render cache)."""
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: "{}")
    monkeypatch.setenv("CN_SEARCH_TIMEOUT_SEC", "12.5")
    with pytest.raises(cn_cmd.RetrieveFailed) as exc:
        cn_cmd._do_seen_before_retrieve("q", "/proj", False, 5)
    msg = str(exc.value)
    assert msg.startswith("ContextNest retrieve did not answer within")
    assert "12.5" in msg


def test_seen_before_raises_when_disabled(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cn_client, "_disabled", lambda: True)
    monkeypatch.setattr(cn_client, "available", lambda: False)
    with pytest.raises(cn_cmd.RetrieveFailed) as exc:
        cn_cmd._do_seen_before_retrieve("q", "/proj", False, 5)
    assert "not reachable" in str(exc.value)


def test_cmd_seen_before_writes_envelope_to_run_dir(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        home: Path) -> None:
    """The verb persists the envelope to
    ``<run_dir>/contextnest-seen-before.json`` and exits 0."""
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "/proj")
    monkeypatch.setattr(cn_cmd, "_read_error_tail",
                        lambda run_id, node_id: "boom at line 12")
    captured = _stub_post_json(
        monkeypatch,
        canned=json.dumps({"hits": [
            {"id": "h1", "similarity": 0.91, "session_id": "sess-a",
             "content": "match", "metadata": {"cwd": "/proj/foo.py"}},
        ]}),
    )
    run_dir = _seed_failed_run(home)
    # The verb reads the error tail from ``impl-<node>.log``; the
    # failing run fixtures don't have one, so the verb falls back to
    # the run id. Patch the tail reader to keep the wire shape
    # observable.
    rc = cn_cmd._cmd_seen_before(
        cn_cmd.build_parser().parse_args(
            ["seen-before", RUN, "--node", NODE_IMPL])
    )
    assert rc == cn_cmd.EXIT_OK
    assert captured["path"] == "/api/v1/tools/retrieve"
    stored = cn_cmd._read_seen_before(cn_cmd._seen_before_path(RUN))
    assert stored["run_id"] == RUN
    assert stored["node_id"] == NODE_IMPL
    assert stored["count"] == 1
    assert stored["hits"][0]["id"] == "h1"
    # File landed under <run_dir>/contextnest-seen-before.json
    expected = run_dir / "contextnest-seen-before.json"
    assert expected.is_file()
    captured_out = capsys.readouterr()
    assert "stored=" in captured_out.out


def test_cmd_seen_before_does_not_overwrite_existing(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        home: Path) -> None:
    """A second call for the same run short-circuits with exit 0 and
    does NOT overwrite the file. CN is never called on this branch."""
    _seed_failed_run(home)
    existing = _write_seen_before(home, hits=[
        {"id": "orig", "similarity": 0.5, "session_id": "s",
         "content": "original", "metadata": {}}
    ])
    called: list[str] = []
    monkeypatch.setattr(cn_client, "_post_json",
                        lambda *a, **k: (called.append("post"), "{}")[1])
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    rc = cn_cmd._cmd_seen_before(
        cn_cmd.build_parser().parse_args(["seen-before", RUN])
    )
    assert rc == cn_cmd.EXIT_OK
    assert called == []
    # File is byte-identical.
    re_read = cn_cmd._read_seen_before(cn_cmd._seen_before_path(RUN))
    assert re_read == existing


def test_cmd_seen_before_fails_closed_at_scope(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        home: Path) -> None:
    """Unresolved scope + ``--all-projects`` off → exit 3, no CN call,
    no file write. The page's empty-state path is unchanged."""
    _seed_failed_run(home)
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "")
    called: list[str] = []
    monkeypatch.setattr(cn_client, "_post_json",
                        lambda *a, **k: (called.append("post"), "{}")[1])
    rc = cn_cmd._cmd_seen_before(
        cn_cmd.build_parser().parse_args(["seen-before", RUN])
    )
    assert rc == cn_cmd.EXIT_COORD_UNREACHABLE
    assert called == []
    assert not cn_cmd._seen_before_path(RUN) or not os.path.isfile(
        cn_cmd._seen_before_path(RUN)
    )


def test_cmd_seen_before_rejects_escaping_run_id(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        home: Path) -> None:
    """A run id that is not one path segment is a usage error (exit 2).

    The id names a directory under ``$MINI_ORK_HOME/runs`` for both the
    write and the error-tail read; a value carrying ``..`` or a
    separator would let the verb touch a path outside that tree.
    """
    _seed_failed_run(home)
    called: list[str] = []
    monkeypatch.setattr(cn_client, "_post_json",
                        lambda *a, **k: (called.append("post"), "{}")[1])
    for bad in ("../escape", "/etc/passwd", "a/b", ".."):
        rc = cn_cmd._cmd_seen_before(
            cn_cmd.build_parser().parse_args(["seen-before", bad])
        )
        assert rc == cn_cmd.EXIT_USAGE, bad
    assert called == [], "an escaping id must never reach ContextNest"
    assert cn_cmd._seen_before_path("../escape") == ""


def test_cmd_seen_before_exits_4_on_no_hits(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        home: Path) -> None:
    """Body without a ``hits`` list → exit 4, no file write."""
    _seed_failed_run(home)
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "/proj")
    monkeypatch.setattr(cn_cmd, "_read_error_tail",
                        lambda run_id, node_id: "q")
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: "{}")
    rc = cn_cmd._cmd_seen_before(
        cn_cmd.build_parser().parse_args(["seen-before", RUN])
    )
    assert rc == cn_cmd.EXIT_COORD_HTTP
    assert not os.path.isfile(cn_cmd._seen_before_path(RUN))
    captured = capsys.readouterr()
    assert "did not answer within" in captured.err


def test_cmd_seen_before_exits_5_on_secret_store_error(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        home: Path) -> None:
    """Secure-tier misconfig fires BEFORE the retrieve call — the verb
    surfaces the key name (never the value) and exits 5."""
    _seed_failed_run(home)
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "/proj")
    monkeypatch.setattr(cn_cmd, "_read_error_tail",
                        lambda run_id, node_id: "q")
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)

    def _raise(*a, **k):
        raise SecretStoreError("missing key 'CN_TOKEN'")
    monkeypatch.setattr(cn_client, "_post_json", _raise)
    rc = cn_cmd._cmd_seen_before(
        cn_cmd.build_parser().parse_args(["seen-before", RUN])
    )
    assert rc == cn_cmd.EXIT_SECRET_CONFIG
    assert not os.path.isfile(cn_cmd._seen_before_path(RUN))
    captured = capsys.readouterr()
    assert "CN_TOKEN" in captured.err
    assert "secure tier secret store" in captured.err


def test_cmd_seen_before_json_does_not_write_file(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        home: Path) -> None:
    """``--json`` short-circuits the slot write and prints the
    envelope on stdout. The path is never created."""
    _seed_failed_run(home)
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "/proj")
    monkeypatch.setattr(cn_cmd, "_read_error_tail",
                        lambda run_id, node_id: "q")
    _stub_post_json(monkeypatch, canned=json.dumps({"hits": [
        {"id": "h1", "similarity": 0.7, "session_id": "s",
         "content": "c", "metadata": {"cwd": "/proj/foo.py"}}
    ]}))
    rc = cn_cmd._cmd_seen_before(
        cn_cmd.build_parser().parse_args(["seen-before", RUN, "--json"])
    )
    assert rc == cn_cmd.EXIT_OK
    assert not os.path.isfile(cn_cmd._seen_before_path(RUN))
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["run_id"] == RUN
    assert payload["count"] == 1


# ── 3. outcome verb — wire / confirm / error states ───────────────────────


def test_cmd_outcome_posts_to_agent_outcome_and_reports_updated(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """On success the verb prints ``outcome: <atom> success updated=<n>``,
    sourcing the integer from the response's ``updated`` field (NOT
    ``delta_applied`` — the live 2026-10-15 wire shape)."""
    monkeypatch.setattr(cn_cmd, "_outcome_settle_response",
                        lambda *a, **k: {
                            "status": "ok", "updated": 3,
                            "skipped_unknown": 0, "delta_applied": 0,
                        })
    rc = cn_cmd._cmd_outcome(
        cn_cmd.build_parser().parse_args(
            ["outcome", "atom-1", "success", "--run", RUN, "--yes"])
    )
    assert rc == cn_cmd.EXIT_OK
    captured = capsys.readouterr()
    assert "outcome: atom-1 success updated=3" in captured.out
    assert "delta_applied" not in captured.out


def test_cmd_outcome_json_prints_full_response(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(cn_cmd, "_outcome_settle_response",
                        lambda *a, **k: {
                            "status": "ok", "updated": 2,
                            "skipped_unknown": 0, "delta_applied": 0,
                        })
    cn_cmd._cmd_outcome(
        cn_cmd.build_parser().parse_args(
            ["outcome", "atom-1", "failure", "--run", RUN,
             "--yes", "--json"])
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload == {
        "status": "ok", "updated": 2,
        "skipped_unknown": 0, "delta_applied": 0,
    }


def test_cmd_outcome_pending_reports_reason(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """CN-down / config-error records ``pending``; the verb exits 0 and
    prints the reason — the audit log captures the failure, the
    operator sees the missing-key / down signal."""
    monkeypatch.setattr(cn_cmd, "_outcome_settle_response",
                        lambda *a, **k: {
                            "status": "pending", "reason": "cn_unavailable",
                        })
    rc = cn_cmd._cmd_outcome(
        cn_cmd.build_parser().parse_args(
            ["outcome", "atom-1", "success", "--run", RUN, "--yes"])
    )
    assert rc == cn_cmd.EXIT_OK
    captured = capsys.readouterr()
    assert "pending" in captured.out
    assert "cn_unavailable" in captured.out


def test_cmd_outcome_secret_store_error_exits_5(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Secure-tier misconfig surfaces as a named :class:`SecretStoreError`
    (exit 5). The verb echoes the KEY name (never the value)."""
    def _raise(*a, **k):
        raise SecretStoreError("missing key 'CN_TOKEN'")
    monkeypatch.setattr(cn_cmd, "_outcome_settle_response", _raise)
    rc = cn_cmd._cmd_outcome(
        cn_cmd.build_parser().parse_args(
            ["outcome", "atom-1", "success", "--run", RUN, "--yes"])
    )
    assert rc == cn_cmd.EXIT_SECRET_CONFIG
    captured = capsys.readouterr()
    assert "CN_TOKEN" in captured.err
    assert "secure tier secret store" in captured.err


def test_cmd_outcome_confirm_gate_is_tty_gated(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The confirm prompt is reachable only from a real terminal.

    On a TTY, 'no' and EOF abort (exit 2, no settle) and 'yes' settles.
    Without a TTY — a pipe, a captured harness, a daemonised process —
    there is no human to ask, so the verb must not block on ``input()``;
    it takes the same auto-answer path the other confirm-gated
    subcommands use and settles. ``MINI_ORK_NONINTERACTIVE=1`` forces
    that path even on a TTY.
    """
    called: list[str] = []
    monkeypatch.setattr(cn_cmd, "_outcome_settle_response",
                        lambda *a, **k: called.append("called") or {})
    args = cn_cmd.build_parser().parse_args(
        ["outcome", "atom-1", "success", "--run", RUN])

    class _TTY:
        def __init__(self, line: str) -> None:
            self._line = line

        def isatty(self) -> bool:
            return True

        def readline(self) -> str:
            return self._line

        def read(self, *a: Any) -> str:
            return self._line

    class _NoTTY(_TTY):
        def isatty(self) -> bool:
            return False

    # A real terminal: 'no' and EOF both abort without settling.
    monkeypatch.setattr(sys, "stdin", _TTY("no\n"))
    assert cn_cmd._cmd_outcome(args) == cn_cmd.EXIT_USAGE
    monkeypatch.setattr(sys, "stdin", _TTY(""))
    assert cn_cmd._cmd_outcome(args) == cn_cmd.EXIT_USAGE
    assert called == []
    # A real terminal: 'yes' settles.
    monkeypatch.setattr(sys, "stdin", _TTY("yes\n"))
    assert cn_cmd._cmd_outcome(args) == cn_cmd.EXIT_OK
    assert called == ["called"]
    # No TTY: the verb cannot ask a human, so it settles on the
    # auto-answer path rather than raising or deadlocking.
    called.clear()
    monkeypatch.setattr(sys, "stdin", _NoTTY(""))
    assert cn_cmd._cmd_outcome(args) == cn_cmd.EXIT_OK
    assert called == ["called"]
    # An explicit opt-out forces the auto-answer path even on a TTY.
    called.clear()
    monkeypatch.setenv("MINI_ORK_NONINTERACTIVE", "1")
    monkeypatch.setattr(sys, "stdin", _TTY("no\n"))
    assert cn_cmd._cmd_outcome(args) == cn_cmd.EXIT_OK
    assert called == ["called"]
    assert called == ["called"]


def test_cmd_outcome_rejects_bad_outcome() -> None:
    """The argparse ``choices=("success", "failure")`` rejects anything
    else at parse time — a verb-level guard rather than a runtime one."""
    parser = cn_cmd.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["outcome", "atom-1", "maybe", "--run", RUN,
                           "--yes"])


def test_cmd_outcome_passes_atom_and_run_to_seam(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The settle seam receives the outcome string, the atom id, the
    run id, and the evidence string. The seam routes to
    :func:`cn_client._settle_outcome` with the atom wrapped in a
    list and the run id as ``session_id``."""
    captured: dict[str, Any] = {}

    def _record(outcome_str: str, atom_id: str, run_id: str,
                evidence: str) -> dict:
        captured["outcome"] = outcome_str
        captured["atom_id"] = atom_id
        captured["run_id"] = run_id
        captured["evidence"] = evidence
        return {"status": "ok", "updated": 1, "skipped_unknown": 0,
                "delta_applied": 0}
    monkeypatch.setattr(cn_cmd, "_outcome_settle_response", _record)
    cn_cmd._cmd_outcome(
        cn_cmd.build_parser().parse_args(
            ["outcome", "atom-X", "failure", "--run", RUN,
             "--yes", "--evidence", "not useful"])
    )
    assert captured["outcome"] == "failure"
    assert captured["atom_id"] == "atom-X"
    assert captured["run_id"] == RUN
    assert captured["evidence"] == "not useful"


# ── 4. outcome page — read-only render from the stored file ──────────────


def _outcome_card(home: Path) -> dict[str, Any]:
    """Resolve the failed outcome card from the run's home."""
    run = run_page._load(home, RUN)
    assert run is not None
    return outcome_page.resolve(run)


def test_outcome_page_renders_seen_before_callout(
        home: Path, _no_cn_calls) -> None:
    """A failed run with a stored envelope renders a Seen-before
    callout with three cards, sorted by similarity, each opening its
    ``cn_session`` page."""
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)
    card = _outcome_card(home)
    assert card["state"] == "failed"
    callouts = card.get("callouts") or []
    titles = [c.get("title") for c in callouts]
    assert "Seen before" in titles
    sb = next(c for c in callouts if c.get("title") == "Seen before")
    items = sb.get("items") or []
    assert len(items) == 3
    # Sorted by similarity: 0.91 first, 0.78 second, 0.62 third.
    contents = [it["t"] for it in items]
    assert contents[0].startswith("first match")
    assert "sim=0.91" in items[0]["sub"]
    # Each card opens the cn_session page.
    for it in items:
        acts = it.get("acts") or []
        assert any(a.get("do", {}).get("page") == "cn_session" for a in acts), (
            f"no cn_session link in card: {it!r}"
        )


def test_outcome_page_no_callout_when_no_stored_file(
        home: Path, _no_cn_calls) -> None:
    """Failed run + no stored file → a single button row that runs the
    verb (no cards, no Helped/Wrong row)."""
    _seed_failed_run(home, status="failed")
    card = _outcome_card(home)
    callouts = card.get("callouts") or []
    titles = [c.get("title") for c in callouts]
    assert "Seen before" in titles
    sb = next(c for c in callouts if c.get("title") == "Seen before")
    items = sb.get("items") or []
    assert len(items) == 1
    # The single row carries the Find-similar-past-failures button.
    acts = items[0].get("acts") or []
    assert any(a.get("label") == "Find similar past failures" for a in acts)
    btn = next(a for a in acts if a.get("label") == "Find similar past failures")
    assert btn["do"]["cli"] == ["cn", "seen-before", RUN, "--node", NODE_IMPL]
    assert btn["do"].get("confirm")


def test_outcome_page_no_callout_when_run_published(
        home: Path, _no_cn_calls) -> None:
    """A published run does NOT render the Seen-before callout — the
    cache is the failing-run cache."""
    _seed_failed_run(home, status="published")
    card = _outcome_card(home)
    callouts = card.get("callouts") or []
    titles = [c.get("title") for c in callouts]
    assert "Seen before" not in titles


def test_outcome_page_helped_wrong_buttons_carry_confirm(
        home: Path, _no_cn_calls) -> None:
    """When a stored file is on disk, every card carries Helped and
    Wrong buttons. Each is a confirm-gated ``cn outcome`` action bound
    to that card's own atom id (the hit's ``id``)."""
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)
    card = _outcome_card(home)
    sb = next(c for c in card["callouts"] if c.get("title") == "Seen before")
    items = sb.get("items") or []
    assert len(items) == 3
    for item, atom in zip(items, ("h1", "h2", "h3")):
        acts = item.get("acts") or []
        labels = [a.get("label") for a in acts]
        assert "Helped" in labels and "Wrong" in labels
        helped = next(a for a in acts if a.get("label") == "Helped")
        assert helped["do"]["cli"] == ["cn", "outcome", atom, "success",
                                       "--run", RUN]
        assert helped["do"].get("confirm")
        wrong = next(a for a in acts if a.get("label") == "Wrong")
        assert wrong["do"]["cli"] == ["cn", "outcome", atom, "failure",
                                      "--run", RUN]
        assert wrong["do"].get("confirm")
    # The section carries no separate run-scoped outcome row.
    assert all(a.get("do", {}).get("cli", [])[:2] != ["cn", "outcome"]
               for a in (sb.get("actions") or []))


def test_outcome_page_does_not_call_retrieve(home: Path,
                                            _no_cn_calls) -> None:
    """The page-render path is read-only against the stored file. A
    regression that re-introduces ``retrieve()`` here blows the page
    budget (retrieve takes 8–38 s live)."""
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)
    _outcome_card(home)
    assert _no_cn_calls["retrieve"] == 0
    assert _no_cn_calls["_post_json"] == 0
    assert _no_cn_calls["_get"] == 0


def test_outcome_page_source_does_not_call_retrieve() -> None:
    """Static check: ``outcome.py`` never references ``retrieve`` on the
    render path. The verb is the only caller."""
    src = (REPO / "mini_ork" / "ide_pages" / "outcome.py").read_text(
        encoding="utf-8"
    )
    assert "retrieve(" not in src, (
        "outcome.py must not call retrieve(); the page reads the stored "
        "envelope the verb wrote."
    )


def test_outcome_page_second_open_reads_file_only(
        home: Path, _no_cn_calls) -> None:
    """A second open of the same run reads the stored file (the page
    render path) — never re-fetches."""
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)
    _outcome_card(home)
    # Reset counters so we can assert the second open is also quiet.
    for k in _no_cn_calls:
        _no_cn_calls[k] = 0
    _outcome_card(home)
    assert _no_cn_calls["retrieve"] == 0
    assert _no_cn_calls["_post_json"] == 0


def test_outcome_page_caps_at_three_cards(home: Path, _no_cn_calls) -> None:
    """``top_k=5`` keeps three cards and the empty-state diverging; the
    page renders at most three even when five are stored."""
    _seed_failed_run(home, status="failed")
    hits = [
        {"id": f"h{i}", "similarity": 0.9 - 0.05 * i, "session_id": f"s{i}",
         "content": f"hit {i}", "metadata": {}}
        for i in range(5)
    ]
    _write_seen_before(home, hits=hits)
    card = _outcome_card(home)
    sb = next(c for c in card["callouts"] if c.get("title") == "Seen before")
    assert len(sb["items"]) == 3
    # The first three (by similarity) are kept.
    assert sb["items"][0]["t"].startswith("hit 0")
    assert sb["items"][1]["t"].startswith("hit 1")
    assert sb["items"][2]["t"].startswith("hit 2")


# ── 5. node page — read-only render + button on miss ─────────────────────


def _node_payload(home: Path, *, node_id: str = NODE_IMPL) -> dict[str, Any]:
    """Resolve the node page for the failed implementer."""
    return build_node(home, RUN, node_id, view="overview")


def test_node_page_renders_seen_before_when_stored(
        home: Path, _no_cn_calls) -> None:
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)
    out = _node_payload(home)
    sb = out.get("seen_before") or {}
    cards = sb.get("cards") or []
    assert len(cards) == 3
    # Each card opens cn_session via the row's ``do`` field.
    for c in cards:
        assert c.get("do", {}).get("page") == "cn_session"
    # Every card carries its own confirm-gated Helped/Wrong row, bound
    # to that card's atom id.
    for card, atom in zip(cards, ("h1", "h2", "h3")):
        acts = card.get("acts") or []
        labels = [a.get("label") for a in acts]
        assert "Helped" in labels and "Wrong" in labels
        helped = next(a for a in acts if a.get("label") == "Helped")
        assert helped["do"]["cli"] == ["cn", "outcome", atom, "success",
                                       "--run", RUN]
        assert helped["do"].get("confirm")
        wrong = next(a for a in acts if a.get("label") == "Wrong")
        assert wrong["do"]["cli"] == ["cn", "outcome", atom, "failure",
                                      "--run", RUN]
        assert wrong["do"].get("confirm")


def test_node_page_button_when_no_stored_file(
        home: Path, _no_cn_calls) -> None:
    """Failed node + no stored file → the acts carry a Find-similar-
    past-failures button. The ``seen_before`` field is absent."""
    _seed_failed_run(home, status="failed")
    out = _node_payload(home)
    assert "seen_before" not in out
    acts = out.get("acts") or []
    btn = next((a for a in acts
                if a.get("label") == "Find similar past failures"), None)
    assert btn is not None
    assert btn["do"]["cli"] == ["cn", "seen-before", RUN,
                                 "--node", NODE_IMPL]
    assert btn["do"].get("confirm")


def test_node_page_no_seen_before_when_successful(
        home: Path, _no_cn_calls) -> None:
    """A successful (non-failed) node does not render the seen-before
    surface — the cache is the failing-run cache. Even when a stored
    envelope is on disk, the section stays hidden: only ``target.state
    == "failed"`` flips the gate."""
    _seed_failed_run(home, run_id="run-1791000000-pubbbb",
                     status="published", finish_reason="done")
    _write_seen_before(home, run_id="run-1791000000-pubbbb")
    out = build_node(home, "run-1791000000-pubbbb", NODE_IMPL,
                     view="overview")
    assert "seen_before" not in out
    acts = out.get("acts") or []
    assert not any(a.get("label") == "Find similar past failures"
                   for a in acts)
    assert not any(a.get("label") == "Helped" for a in acts)


def test_node_page_does_not_call_retrieve(home: Path, _no_cn_calls) -> None:
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)
    _node_payload(home)
    assert _no_cn_calls["retrieve"] == 0
    assert _no_cn_calls["_post_json"] == 0
    assert _no_cn_calls["_get"] == 0


def test_node_page_source_does_not_call_retrieve() -> None:
    """Static check: ``node.py`` never references ``retrieve`` on the
    render path. The verb is the only caller."""
    src = (REPO / "mini_ork" / "ide_pages" / "node.py").read_text(
        encoding="utf-8"
    )
    assert "retrieve(" not in src, (
        "node.py must not call retrieve(); the page reads the stored "
        "envelope the verb wrote."
    )


def test_node_page_caps_at_three_cards(home: Path, _no_cn_calls) -> None:
    _seed_failed_run(home, status="failed")
    hits = [
        {"id": f"h{i}", "similarity": 0.9 - 0.05 * i, "session_id": f"s{i}",
         "content": f"hit {i}", "metadata": {}}
        for i in range(5)
    ]
    _write_seen_before(home, hits=hits)
    out = _node_payload(home)
    cards = (out.get("seen_before") or {}).get("cards") or []
    assert len(cards) == 3
    assert cards[0]["t"].startswith("hit 0")


# ── 6. actions sweep — every new ``S.cli('cn', ...)`` parses ─────────────


def test_helped_wrong_buttons_parse_under_cn_cmd(
        home: Path, _no_cn_calls) -> None:
    """The page-builds-tests' actions sweep walks every ``cli`` action.
    Re-assert that the new buttons' argv parses under the cn parser."""
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)
    parser = cn_cmd.build_parser()
    for argv in (
        ["outcome", RUN, "success", "--run", RUN],
        ["outcome", RUN, "failure", "--run", RUN],
        ["seen-before", RUN, "--node", NODE_IMPL],
        ["seen-before", RUN, "--all-projects"],
    ):
        try:
            parser.parse_known_args(argv)
        except SystemExit as exc:
            pytest.fail(f"cn parser rejected {argv!r}: exit {exc.code}")


def test_outcome_verb_accepts_argvectors_under_parse_known(
        home: Path, _no_cn_calls) -> None:
    """``parse_args`` rejects ``--home`` (the cn verb has no such flag).
    The IDE's actions sweep uses ``parse_known_args`` which would NOT
    surface this; the per-action ``home=False`` keeps the IDE from
    appending a flag the verb rejects — covered by the next test."""
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)
    parser = cn_cmd.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["seen-before", RUN, "--node", NODE_IMPL,
                           "--home", str(home)])


def test_run_page_actions_have_home_false_for_cn_verbs(
        home: Path, _no_cn_calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``S.cli("cn", ...)`` action the run page, the failed node
    page, AND the failed outcome card's callouts emit carries
    ``home=False`` — the cn verb rejects ``--home`` and the IDE
    appends it unless the action says so. The sweep's
    ``parse_known_args`` does NOT catch this class of bug (it accepts
    unknown flags silently), so we walk the three places where the
    pages can emit a cn cli action and assert ``home=False`` on each.
    The outcome callouts are only rendered in the v2 IDE (level >= 2);
    we set the env so the test exercises the same code path the live
    IDE uses."""
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    _seed_failed_run(home, status="failed")
    _write_seen_before(home)

    def _walk(node: object) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if isinstance(node, dict):
            cli = node.get("cli")
            if isinstance(cli, list) and cli and cli[0] == "cn":
                out.append(node)
            for value in node.values():
                out.extend(_walk(value))
        elif isinstance(node, list):
            for value in node:
                out.extend(_walk(value))
        return out

    # 1. The run page (story tab) — the v2 path renders the failed
    # outcome card's callouts inline, so Helped/Wrong + the Find button
    # both flow through this walk.
    run_page = build_page(home, "run", tab="story", args={"run": RUN})
    assert run_page.get("ok") is True, run_page.get("error")

    # 2. The node page (failed implementer, stored envelope) — emits
    # Seen-before cards with Helped/Wrong in their ``acts``.
    node_page = build_node(home, RUN, NODE_IMPL, view="overview")

    # 3. The outcome card directly — its ``callouts`` list is the
    # authoritative source for the Seen-before section regardless of
    # which page renders it. Walking it explicitly closes the gap
    # between the run-page and the node-page code paths.
    card = _outcome_card(home)
    assert card["state"] == "failed"
    outcome_callouts = card.get("callouts") or []

    actions = _walk(run_page) + _walk(node_page) + _walk(outcome_callouts)
    assert actions, (
        "no cn cli actions across the run page, the node page, or the "
        "outcome card callouts"
    )
    for a in actions:
        assert a.get("home", True) is False, (
            f"cn action carries --home implicitly: {a!r}"
        )


# ── 7. resolve / build_node are read-only ─────────────────────────────────


def test_outcome_resolve_does_not_write_run_dir(
        home: Path, _no_cn_calls) -> None:
    _seed_failed_run(home, status="failed")
    run_dir = home / "runs" / RUN
    before = {p.name: p.stat().st_mtime_ns
              for p in run_dir.iterdir()} if run_dir.exists() else {}
    _outcome_card(home)
    after = {p.name: p.stat().st_mtime_ns
             for p in run_dir.iterdir()} if run_dir.exists() else {}
    assert before == after


def test_build_node_does_not_write_run_dir(
        home: Path, _no_cn_calls) -> None:
    _seed_failed_run(home, status="failed")
    run_dir = home / "runs" / RUN
    before = {p.name: p.stat().st_mtime_ns
              for p in run_dir.iterdir()} if run_dir.exists() else {}
    _node_payload(home)
    after = {p.name: p.stat().st_mtime_ns
             for p in run_dir.iterdir()} if run_dir.exists() else {}
    assert before == after
