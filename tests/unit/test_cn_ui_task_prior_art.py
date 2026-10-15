"""Hermetic tests for the kickoff draft card's prior-art block.

The kickoff calls ``cn_client.sessions_by_feature`` and the
``/api/v1/prompt-context/atoms`` endpoint inside
:meth:`MiniOrkAcpAgent._offer_kickoff_draft_approval`, renders at most three
session-naming lines, and falls back to a one-line "skipped" notice when
ContextNest is unavailable. These tests stub every ContextNest read at both
plausible import sites (``mini_ork.cn_client`` and ``mini_ork.acp.agent``),
synthesize fixtures in the live-verified response shapes, and assert on the
strings that reach the ACP ``request_permission`` payload.

Acceptance cases:
  1. ``prior_art_capped_at_three_lines`` — five by-feature hits render as
     exactly three session-naming lines on the draft card.
  2. ``prior_art_runs_once_per_draft`` — the CN reads happen at most once
     per draft approval offer (never on keystrokes, never twice).
  3. ``prior_art_skipped_when_contextnest_down`` — with ContextNest down
     the card shows one "prior art ... skipped/unavailable/..." line and
     no session ids, but the approval still fires and the staged draft
     survives.

The fixtures use synthetic session ids (``cn-pa-*``) and synthetic atom
text — no real transcript content is touched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import mini_ork.acp.agent as agent_mod  # noqa: E402
import mini_ork.cn_client as cn_client_mod  # noqa: E402


DRAFT_ID = "fix-login-timeout"
TITLE = "Fix login timeout on checkout"
DRAFT_MARKDOWN = (
    "# Fix login timeout on checkout\n"
    "\n"
    "## Goal\n"
    "\n"
    "- stop the checkout login from timing out\n"
)
PROJECT_ROOT = "/proj/root"

ATOM_MATCH = "login timed out during checkout again"
ATOM_NOMATCH = "unrelated migration refactoring happened"


# ── synthetic ContextNest bodies in the live-verified shapes ────────────────


def _hit(session_id: str, feature: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "feature": feature,
        "ts": "2026-10-14T10:11:12.000Z",
        "files": ["mini_ork/acp/agent.py"],
        "refs": [],
        "layer": "agent",
    }


def _by_feature_body(hits: list[tuple[str, str]]) -> str:
    return json.dumps({"query": TITLE, "hits": [_hit(sid, f) for sid, f in hits]})


def _atom(session_id: str, text: str, project: str) -> dict[str, Any]:
    return {
        "kind": "failure",
        "text": text,
        "session_id": session_id,
        "project": project,
        "ts": "2026-10-13T08:09:10.000Z",
    }


def _atoms_body(atoms: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "since": "30d",
            "count": len(atoms),
            "total_matched": len(atoms),
            "atoms": atoms,
        }
    )


# ── recording ContextNest stub ──────────────────────────────────────────────


class _CNProbe:
    def __init__(self, *, by_feature: str, atoms: str) -> None:
        self.get_paths: list[str] = []
        self.get_text_paths: list[str] = []
        self.post_paths: list[str] = []
        self.capsule_calls: list[Any] = []
        self.retrieve_calls: list[Any] = []
        self._by_feature = by_feature
        self._atoms = atoms

    def _get(self, path: str, *, timeout: float | None = None) -> str:
        self.get_paths.append(str(path))
        p = str(path)
        if p.startswith("/api/v1/sessions/by-feature"):
            return self._by_feature
        if p.startswith("/api/v1/prompt-context/atoms"):
            return self._atoms
        return "{}"

    def _get_text(self, path: str, *, timeout: float | None = None) -> str:
        self.get_text_paths.append(str(path))
        return ""

    def _post_json(
        self, path: str, body: str, *, timeout: float | None = None
    ) -> str:
        self.post_paths.append(str(path))
        return "{}"

    def _capsule(self, *args: Any, **kwargs: Any) -> str:
        self.capsule_calls.append((args, kwargs))
        return ""

    def _retrieve(self, *args: Any, **kwargs: Any) -> str:
        self.retrieve_calls.append((args, kwargs))
        return "{}"


def _install_cn(
    monkeypatch: pytest.MonkeyPatch, probe: _CNProbe, *, available: bool
) -> None:
    """Patch the CN read surface at both plausible import sites."""
    fakes: dict[str, Any] = {
        "_get": probe._get,
        "_get_text": probe._get_text,
        "_post_json": probe._post_json,
        "capsule": probe._capsule,
        "retrieve": probe._retrieve,
        "available": lambda: available,
        "_disabled": lambda: False,
    }
    for mod in (cn_client_mod, agent_mod):
        for name, fn in fakes.items():
            monkeypatch.setattr(mod, name, fn, raising=False)


# ── recording ACP connection + agent scaffold ──────────────────────────────


class _Dismissed:
    option_id = None


class _PermResponse:
    outcome = _Dismissed()


class _FakeConn:
    def __init__(self) -> None:
        self.permissions: list[dict[str, Any]] = []
        self.updates: list[tuple[str, Any]] = []

    async def request_permission(self, **kwargs: Any) -> _PermResponse:
        self.permissions.append(kwargs)
        return _PermResponse()

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


def _walk(obj: Any, out: list[str]) -> None:
    """Collect every string reachable from a rendered card payload."""
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _walk(v, out)
    else:
        dump: Any = None
        model_dump = getattr(obj, "model_dump", None)
        if callable(model_dump):
            try:
                dump = model_dump()
            except Exception:  # noqa: BLE001 — fall back to str()
                dump = None
        if isinstance(dump, dict):
            _walk(dump, out)
        else:
            out.append(str(obj))


def _make_agent(tmp_path: Path):
    from mini_ork.acp.agent import MiniOrkAcpAgent

    root = tmp_path.resolve()
    home = root / ".mini-ork"
    (home / "kickoff-drafts").mkdir(parents=True, exist_ok=True)
    agent = MiniOrkAcpAgent(home=str(home))
    conn = _FakeConn()
    agent._conn = conn
    session_id = "orch-cn-pa"
    agent._sessions[session_id] = str(root)
    (home / "kickoff-drafts" / f"{DRAFT_ID}.md").write_text(
        DRAFT_MARKDOWN, encoding="utf-8"
    )
    return agent, conn, session_id, root, home


def _offer(agent: Any, session_id: str) -> None:
    asyncio.run(
        agent._offer_kickoff_draft_approval(
            session_id,
            DRAFT_ID,
            tool_call_id="call-pa",
            recipe="code-fix",
        )
    )


def _atoms_with_root(atoms: list[dict[str, Any]], root: Path) -> str:
    body = _atoms_body(atoms)
    return body.replace(PROJECT_ROOT, str(root))


def _title_string(conn: _FakeConn) -> str:
    """Return every title string that landed on the draft card."""
    parts: list[str] = []
    for kwargs in conn.permissions:
        _walk(kwargs, parts)
    return "\n".join(parts)


def _skipped_line(corpus: str) -> str | None:
    why = (
        "skip",
        "unavailable",
        "unreachable",
        "down",
        "disabled",
        "off",
        "not running",
        "contextnest",
    )
    for line in corpus.splitlines():
        low = line.lower()
        if "prior art" in low and any(w in low for w in why):
            return line
    return None


# ── tests ───────────────────────────────────────────────────────────────────


def test_prior_art_capped_at_three_lines(tmp_path, monkeypatch):
    """Five by-feature hits render as exactly three session-naming lines."""
    probe = _CNProbe(
        by_feature=_by_feature_body(
            [
                ("cn-pa-one", "login timeout"),
                ("cn-pa-two", "checkout retry"),
                ("cn-pa-three", "session gc"),
                ("cn-pa-four", "route table"),
                ("cn-pa-five", "cost guard"),
            ]
        ),
        atoms=_atoms_body([]),
    )
    agent, conn, sid, _root, _home = _make_agent(tmp_path)
    _install_cn(monkeypatch, probe, available=True)
    _offer(agent, sid)
    corpus = _title_string(conn)
    candidates = [f"cn-pa-{n}" for n in ("one", "two", "three", "four", "five")]
    rendered = {c for c in candidates if c in corpus}
    assert rendered, "no by-feature session id reached the draft card"
    assert len(rendered) <= 3, f"prior-art block exceeded three lines: {sorted(rendered)}"


def test_prior_art_runs_once_per_draft(tmp_path, monkeypatch):
    """The CN reads happen exactly once per draft approval offer.

    Covers the 'compute prior art exactly once' requirement and the 'never
    twice per draft' guard: the by-feature read and the atoms read each
    fire at most one time per ``_offer_kickoff_draft_approval`` call, and
    the by-feature query carries the draft title.
    """
    agent, conn, sid, root, _home = _make_agent(tmp_path)
    probe = _CNProbe(
        by_feature=_by_feature_body([("cn-pa-alpha", "login timeout")]),
        atoms=_atoms_with_root(
            [_atom("cn-pa-atom", ATOM_MATCH, PROJECT_ROOT)], root
        ),
    )
    _install_cn(monkeypatch, probe, available=True)
    _offer(agent, sid)
    by_feature_paths = [
        p for p in probe.get_paths if p.startswith("/api/v1/sessions/by-feature")
    ]
    atoms_paths = [
        p for p in probe.get_paths if p.startswith("/api/v1/prompt-context/atoms")
    ]
    assert len(by_feature_paths) == 1, (
        f"by-feature read did not run exactly once: {by_feature_paths}"
    )
    assert len(atoms_paths) == 1, (
        f"atoms read did not run exactly once: {atoms_paths}"
    )
    q = urllib.parse.parse_qs(
        urllib.parse.urlparse(by_feature_paths[0]).query
    ).get("q")
    assert q == [TITLE], f"by-feature query {q!r} is not the draft title {TITLE!r}"


def test_prior_art_skipped_when_contextnest_down(tmp_path, monkeypatch):
    """With ContextNest down, the card shows one skipped line and no hits.

    Acceptance: ``ContextNest down adds the skipped line and the draft card
    still works``. The card still fires the approval (the permission call is
    recorded) and the staged draft survives.
    """
    probe = _CNProbe(
        # populated by-feature body — a leak would be visible as a rendered id.
        by_feature=_by_feature_body([("cn-pa-leak", "login timeout")]),
        atoms="{}",
    )
    agent, conn, sid, _root, home = _make_agent(tmp_path)
    _install_cn(monkeypatch, probe, available=False)
    _offer(agent, sid)  # must not raise into the card
    corpus = _title_string(conn)
    line = _skipped_line(corpus)
    assert line, "no line on the draft card says prior art was skipped:\n" + corpus
    assert "cn-pa-leak" not in corpus, (
        "prior-art session id rendered while ContextNest was down"
    )
    assert len(conn.permissions) == 1, (
        "draft-card approval was not offered with CN down"
    )
    staged = home / "kickoff-drafts" / f"{DRAFT_ID}.md"
    assert staged.is_file(), "the staged draft did not survive the CN-down card"


def test_prior_art_atoms_endpoint_filters_by_project_and_word(tmp_path, monkeypatch):
    """Atoms with the wrong project or no shared significant word are dropped."""
    agent, conn, sid, root, _home = _make_agent(tmp_path)
    probe = _CNProbe(
        by_feature=_by_feature_body([]),
        atoms=_atoms_with_root(
            [
                _atom("cn-pa-keep", ATOM_MATCH, PROJECT_ROOT),
                _atom("cn-pa-wrong-project", ATOM_MATCH, "/some/other/project"),
                _atom("cn-pa-wrong-words", ATOM_NOMATCH, PROJECT_ROOT),
            ],
            root,
        ),
    )
    _install_cn(monkeypatch, probe, available=True)
    _offer(agent, sid)
    corpus = _title_string(conn)
    assert "cn-pa-keep" in corpus, "atom matching project+word was dropped"
    assert "cn-pa-wrong-project" not in corpus, (
        "atom from another project leaked onto the draft card"
    )
    assert "cn-pa-wrong-words" not in corpus, (
        "atom sharing no significant word with the title leaked onto the draft card"
    )


def test_prior_art_never_calls_capsule_or_retrieve(tmp_path, monkeypatch):
    """The draft card never touches the capsule endpoint or /tools/retrieve."""
    probe = _CNProbe(
        by_feature=_by_feature_body([("cn-pa-one", "login timeout")]),
        atoms=_atoms_body([]),
    )
    agent, conn, sid, _root, _home = _make_agent(tmp_path)
    _install_cn(monkeypatch, probe, available=True)
    _offer(agent, sid)
    assert probe.capsule_calls == [], "draft card called cn_client.capsule"
    assert probe.retrieve_calls == [], "draft card called cn_client.retrieve"
    assert probe.get_text_paths == [], (
        f"draft card used _get_text (capsule read path): {probe.get_text_paths}"
    )
    assert probe.post_paths == [], (
        f"draft card issued POST reads: {probe.post_paths}"
    )
    unexpected = [
        p
        for p in probe.get_paths
        if not p.startswith("/api/v1/sessions/by-feature")
        and not p.startswith("/api/v1/prompt-context/atoms")
    ]
    assert unexpected == [], (
        f"draft card read unexpected endpoints: {unexpected}"
    )

def test_prior_art_ignores_words_too_common_to_match(tmp_path, monkeypatch):
    """A failure atom sharing only a common word with the title is not prior art."""
    agent, _conn, sid, root, _home = _make_agent(tmp_path)
    probe = _CNProbe(
        by_feature=_by_feature_body([]),
        atoms=_atoms_with_root(
            [
                _atom("cn-pa-common", "a question about the farm schedule", PROJECT_ROOT),
                _atom("cn-pa-checkout", "checkout page broke again", PROJECT_ROOT),
            ],
            root,
        ),
    )
    _install_cn(monkeypatch, probe, available=True)
    session_ids, skipped = agent._compute_kickoff_prior_art(sid, title="Notes about checkout")
    assert skipped is None
    assert "cn-pa-checkout" in session_ids
    assert "cn-pa-common" not in session_ids
