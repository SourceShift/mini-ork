"""``mini_ork.cli.execute`` run-verdict → ContextNest outcome settle.

Outcome feedback moved from ``hooks/subagent-stop.sh`` (per-subagent
completion) to ``_emit_run_verdict`` (run-level finalisation) so the
substrate is rewarded only when the run actually publishes, and debited
when the run is rejected or rolled back. The settle is idempotent and
records ``pending`` instead of failing the run when ContextNest is down.

Six acceptance behaviors the verifier checks (each is its own function so
a regression names the broken contract):

1. ``test_subagent_stop_hook_no_outcome_post`` — the hook shell script
   no longer contains the per-subagent outcome block, so a Claude
   ``SubagentStop`` event posts NOTHING to ``/api/v1/agent/outcome``.
2. ``test_published_run_posts_success_per_atom`` — a passing run
   (``verdict="pass"``) settles one ``success`` per atom the run was
   given. Each call's response body is judged on ``updated``.
3. ``test_rejected_run_posts_failure`` — a ``verdict="fail"`` run
   settles one ``failure`` per atom.
4. ``test_abstain_and_interrupted_post_nothing`` — ``verdict="abstain"``
   (and the no-verdict interrupted / parked paths) post nothing.
5. ``test_settle_twice_posts_once`` — a second ``_settle_cn_outcomes``
   on the same run dir reads the existing ``cn-outcomes.json`` and
   posts nothing new; the mock server sees one batch total.
6. ``test_cn_down_leaves_pending_and_verdict_untouched`` — CN is
   unreachable: ``cn-outcomes.json`` records the atoms as ``pending``,
   ``verdict.json`` is byte-identical to the no-CN case, and the
   settle call never raises.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "hooks" / "subagent-stop.sh"
sys.path.insert(0, str(REPO))

from mini_ork.cli import execute as ex  # noqa: E402


# ── helpers: mock CN server, run-dir fixtures, env mutation ────────────────────


class _OutcomeHandler(http.server.BaseHTTPRequestHandler):
    """Captures ``/api/v1/agent/outcome`` POSTs and serves the verified
    2026-10-15 response shape ``{updated, skipped_unknown, delta_applied}``.
    Health is 200 so ``available()`` returns True; other reads return the
    empty body so the page helpers don't blow up if a test reaches for
    them."""

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/v1/substrate/health"):
            self._send(b"")
            return
        self._send(b"{}")

    def do_POST(self) -> None:
        ln = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(ln) if ln else b"{}"
        if self.path.startswith("/api/v1/agent/outcome"):
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {}
            # Per the verified shape: every atom_id the POST names gets
            # counted as ``updated``; ``skipped_unknown`` and
            # ``delta_applied`` are non-zero only when CN has side-data
            # to report. The settle uses ``updated`` as the truth signal.
            ids = payload.get("atom_ids") or []
            self.server.outcome_posts.append({
                "outcome": payload.get("outcome"),
                "atom_ids": list(ids),
                "evidence": payload.get("evidence", ""),
                "session_id": payload.get("session_id", ""),
            })
            body = json.dumps({
                "updated": len(ids),
                "skipped_unknown": 0,
                "delta_applied": len(ids),
            }).encode()
            self._send(body)
            return
        # graph / retrieve / etc. — accept and ignore so the env probe
        # never crashes a test that ran a stray helper.
        self._send(b"{}")


def _server() -> tuple[http.server.HTTPServer, str]:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _OutcomeHandler)
    srv.outcome_posts = []  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _make_run_dir(base: Path) -> Path:
    """Fresh run dir with ``cn_prefetch/`` pre-populated so the settle has
    atom ids to credit. Three atom ids per fixture keeps each test's
    assertions short. ``base`` may be a deeper path (e.g.
    ``tmp_path / "abstain"``); the parent directories are created so a
    test can stage multiple run dirs side by side."""
    base.mkdir(parents=True, exist_ok=True)
    rd = base / "run"
    rd.mkdir()
    prefetch = rd / "cn_prefetch"
    prefetch.mkdir()
    atoms = (
        "cn-0123456789abcdef0123456789abcdef01234567",
        "cn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "cn-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    body = (
        "# ContextNest prefetch for session sess-test\n"
        "_Generated at 2026-10-15T00:00:00Z by mini_ork.cli.cn_hook._\n\n"
        "--- ContextNest atoms (fresh substrate retrieval) ---\n"
        # The full atom id is embedded so the cn-<hex>{8,40} regex matches.
        # The "sess=<8 chars>" markup renders only a prefix in production;
        # the substrate atom id itself appears later in the line as the
        # leading token the regex anchors to.
        f"- [decision sim=0.91 2026-10-15] {atoms[0]} chose sqlite over pg\n"
        f"- [decision sim=0.82 2026-10-15] {atoms[1]} query planner stats\n"
        f"- [pattern  sim=0.65 2026-10-15] {atoms[2]} short-circuit retry\n"
        "--- /ContextNest atoms ---\n"
    )
    (prefetch / "sess-test.md").write_text(body, encoding="utf-8")
    return rd


def _write_verdict_bytes(rd: Path, verdict: str) -> bytes:
    """Write the byte-stable verdict the engine would write and return the
    bytes so the test can pin them on a CN-down path."""
    blob = (
        '{"verdict":"%s","failed_nodes":0,"dispatched":3,'
        '"source":"execute@run-level"}\n' % verdict
    ).encode()
    (rd / "verdict.json").write_bytes(blob)
    return blob


def _cn_env(base: Path, base_url: str) -> dict:
    return {
        "CN_BASE_URL": base_url,
        "MINI_ORK_HOME": str(base / "home"),
        "CN_TIMEOUT_SEC": "5",
        "CN_HOOK_TIMEOUT_SEC": "5",
        "CN_PING_TTL": "0",
    }


# ── 1. subagent_stop hook no longer posts outcomes ─────────────────────────────


def test_subagent_stop_hook_no_outcome_post(tmp_path):
    """The hook script no longer contains the per-subagent outcome block.

    Posting on every SubagentStop rewarded atoms on every partial pass
    including runs that later rolled back; the settle moved to
    ``_emit_run_verdict`` so the substrate is rewarded when the run
    actually publishes.
    """
    text = HOOK.read_text(encoding="utf-8")
    # The block was the only place the hook called ``cn_hook outcome``.
    assert "cn_hook outcome" not in text, (
        "subagent-stop.sh still calls `cn_hook outcome`; outcome posting "
        "must move to run-verdict finalisation only"
    )
    # The block used ``MO_CN_PREFETCH_DIR`` to find atom ids; that env is
    # now consumed by the run-verdict settle only.
    assert "MO_CN_PREFETCH_DIR" not in text, (
        "subagent-stop.sh still references MO_CN_PREFETCH_DIR; the hook "
        "should leave prefetch-dir scanning to _settle_cn_outcomes"
    )
    # The DB update + the subagent_stop transcript post are the only
    # CN-touching work the hook still does.
    assert "subagent_stop" in text
    assert "UPDATE subagent_runs" in text


# ── 2. published run posts one success per atom ────────────────────────────────


def test_published_run_posts_success_per_atom(tmp_path):
    srv, base = _server()
    rd = _make_run_dir(tmp_path)
    old_env = dict(os.environ)
    os.environ.update(_cn_env(tmp_path, base))
    try:
        # The verifier owns ``_emit_run_verdict``'s byte-stability
        # contract (tests/unit/test_verify_levels.py::test_emit_run_verdict_bytes);
        # this test only exercises the settle path. We call the helper
        # directly with a pre-staged run dir.
        report = ex._settle_cn_outcomes(str(rd), "pass")
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        srv.shutdown()

    assert report == {
        "settled": [
            "cn-0123456789abcdef0123456789abcdef01234567",
            "cn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "cn-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ],
        "pending": [],
        "skipped": [],
    }, f"expected 3 settled atoms, got {report}"
    posts = srv.outcome_posts  # type: ignore[attr-defined]
    assert len(posts) == 1, f"expected 1 batch POST, got {len(posts)}"
    assert posts[0]["outcome"] == "success"
    assert posts[0]["atom_ids"] == [
        "cn-0123456789abcdef0123456789abcdef01234567",
        "cn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "cn-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    # Settle record on disk.
    record = json.loads((rd / "cn-outcomes.json").read_text(encoding="utf-8"))
    assert record["verdict"] == "pass"
    assert len(record["atoms"]) == 3
    for aid in posts[0]["atom_ids"]:
        entry = record["atoms"][aid]
        assert entry["outcome"] == "success"
        assert entry["result"]["status"] == "ok"
        assert entry["result"]["updated"] == 3


# ── 3. rejected / rolled-back run posts failure ────────────────────────────────


def test_rejected_run_posts_failure(tmp_path):
    srv, base = _server()
    rd = _make_run_dir(tmp_path)
    old_env = dict(os.environ)
    os.environ.update(_cn_env(tmp_path, base))
    try:
        report = ex._settle_cn_outcomes(str(rd), "fail")
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        srv.shutdown()

    assert report["settled"] == [
        "cn-0123456789abcdef0123456789abcdef01234567",
        "cn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "cn-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    posts = srv.outcome_posts  # type: ignore[attr-defined]
    assert len(posts) == 1
    assert posts[0]["outcome"] == "failure"
    record = json.loads((rd / "cn-outcomes.json").read_text(encoding="utf-8"))
    assert record["verdict"] == "fail"
    for aid in posts[0]["atom_ids"]:
        assert record["atoms"][aid]["outcome"] == "failure"


# ── 4. abstain / interrupted / parked post nothing ────────────────────────────


def test_abstain_and_interrupted_post_nothing(tmp_path):
    """Abstain (verifier toolchain failed) and the no-verdict paths
    (interrupted run, parked run) must NOT post any outcomes — crediting
    atoms on an unproven run would lie about what happened."""
    srv, base = _server()
    rd_a = _make_run_dir(tmp_path / "a")
    rd_i = _make_run_dir(tmp_path / "i")
    rd_p = _make_run_dir(tmp_path / "p")
    old_env = dict(os.environ)
    os.environ.update(_cn_env(tmp_path, base))
    try:
        rep_a = ex._settle_cn_outcomes(str(rd_a), "abstain")
        # Interrupted / parked runs never reach ``_settle_cn_outcomes``
        # (interrupt raises through the finally; parked runs return early
        # before ``_emit_run_verdict``), but the helper must still refuse
        # to post when called with one of those verdicts.
        rep_i = ex._settle_cn_outcomes(str(rd_i), "interrupted")
        rep_p = ex._settle_cn_outcomes(str(rd_p), "reviewing")
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        srv.shutdown()

    for rep in (rep_a, rep_i, rep_p):
        assert rep == {"settled": [], "pending": [], "skipped": []}
    assert srv.outcome_posts == []  # type: ignore[attr-defined]
    # No settle record on disk for any of them — the no-op settle writes
    # nothing when there are no pending atoms.
    for rd in (rd_a, rd_i, rd_p):
        assert not (rd / "cn-outcomes.json").exists(), (
            f"abstain/interrupted/parked wrote {rd/'cn-outcomes.json'}; "
            "should post nothing and write nothing"
        )


# ── 5. settling twice posts once ──────────────────────────────────────────────


def test_settle_twice_posts_once(tmp_path):
    """The settle record makes a second pass a no-op: ``cn-outcomes.json``
    lists every atom the run was given, so the second call has no pending
    atoms and POSTs nothing."""
    srv, base = _server()
    rd = _make_run_dir(tmp_path)
    old_env = dict(os.environ)
    os.environ.update(_cn_env(tmp_path, base))
    try:
        first = ex._settle_cn_outcomes(str(rd), "pass")
        second = ex._settle_cn_outcomes(str(rd), "pass")
        third = ex._settle_cn_outcomes(str(rd), "pass")
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        srv.shutdown()

    assert first["settled"], "first call should have settled atoms"
    assert second["settled"] == [] and second["pending"] == []
    assert third["settled"] == [] and third["pending"] == []
    posts = srv.outcome_posts  # type: ignore[attr-defined]
    assert len(posts) == 1, f"expected exactly 1 batch POST, got {len(posts)}"


# ── 6. CN-down leaves pending and verdict untouched ───────────────────────────


def test_cn_down_leaves_pending_and_verdict_untouched(tmp_path):
    """CN is unreachable (dead port). The settle must:

    * never raise
    * leave ``verdict.json`` byte-identical (the cost of truth)
    * write a settle record with every atom marked ``pending``
    * NOT modify the verdict in the settle record (verdict is read from
      the engine, not inferred)
    """
    rd = _make_run_dir(tmp_path)
    expected = _write_verdict_bytes(rd, "pass")

    # Dead port → connection refused. Short timeouts so the test stays
    # well under the gate budget.
    env = {
        "CN_BASE_URL": "http://127.0.0.1:1",
        "MINI_ORK_HOME": str(tmp_path / "home"),
        "CN_TIMEOUT_SEC": "1",
        "CN_HOOK_TIMEOUT_SEC": "1",
        "CN_PING_TTL": "0",
    }
    old_env = dict(os.environ)
    os.environ.update(env)
    try:
        report = ex._settle_cn_outcomes(str(rd), "pass")
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    # Verdict bytes are untouched — the cost of truth the byte-stability
    # tests pin.
    assert (rd / "verdict.json").read_bytes() == expected
    # Settle record on disk with every atom pending.
    assert (rd / "cn-outcomes.json").exists()
    record = json.loads((rd / "cn-outcomes.json").read_text(encoding="utf-8"))
    assert record["verdict"] == "pass"
    atoms = record["atoms"]
    assert set(atoms) == {
        "cn-0123456789abcdef0123456789abcdef01234567",
        "cn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "cn-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
    for aid, entry in atoms.items():
        assert entry["outcome"] == "success", (
            f"pending settle still records the verdict's outcome ({entry['outcome']}); "
            "the CN call is what was deferred, not the outcome classification"
        )
        assert entry["result"]["status"] == "pending", (
            f"expected status=pending for {aid}, got {entry['result']}"
        )
    # The report buckets them as pending, not settled.
    assert report["settled"] == []
    assert sorted(report["pending"]) == sorted(atoms)


# ── 7. end-to-end: the engine's _emit_run_verdict path ────────────────────────


def test_emit_run_verdict_calls_settle_after_bytes(tmp_path):
    """Driving the actual ``_emit_run_verdict`` (no panel gate, no
    abstention) must end with a settle record written to the run dir,
    AND the verdict.json bytes must match the byte-stable shape (the
    byte-stability invariant tests 8 / 11 enforce)."""
    srv, base = _server()
    rd = _make_run_dir(tmp_path)
    old_env = dict(os.environ)
    os.environ.update(_cn_env(tmp_path, base))
    try:
        # ``MO_LEVEL_VECTOR=0`` keeps today's bytes (the level-vector
        # branch appends extra keys; this test pins the byte-stable path).
        os.environ["MO_LEVEL_VECTOR"] = "0"
        ex._emit_run_verdict(str(rd), 0, 3)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        srv.shutdown()

    expected = (
        b'{"verdict":"pass","failed_nodes":0,"dispatched":3,'
        b'"source":"execute@run-level"}\n'
    )
    assert (rd / "verdict.json").read_bytes() == expected
    posts = srv.outcome_posts  # type: ignore[attr-defined]
    assert len(posts) == 1, f"expected 1 batch POST from _emit_run_verdict, got {len(posts)}"
    assert posts[0]["outcome"] == "success"
    record = json.loads((rd / "cn-outcomes.json").read_text(encoding="utf-8"))
    assert record["verdict"] == "pass"
    assert len(record["atoms"]) == 3


# ── 8. shell-script syntax guard (the verifier checks ``bash -n``) ────────────


def test_subagent_stop_hook_is_shell_clean():
    """``bash -n`` is the verifier's syntax gate. We re-run it here so a
    test-only edit to the hook surfaces as a unit failure rather than a
    verifier-only failure (the latter would only fire on the next
    run-level pass)."""
    proc = subprocess.run(
        ["bash", "-n", str(HOOK)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, (
        f"subagent-stop.sh fails bash -n:\n{proc.stderr}"
    )


# ── 9. python compile guard (the verifier checks ``py_compile``) ──────────────


def test_modified_python_files_compile():
    """``python3 -m py_compile`` is the verifier's syntax gate for the
    Python side. Re-running it in a unit test means a syntax slip in
    cn_client.py or execute.py is caught here rather than only at
    verify time."""
    for path in (REPO / "mini_ork" / "cn_client.py",
                 REPO / "mini_ork" / "cli" / "execute.py"):
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, (
            f"{path.name} fails py_compile:\n{proc.stderr}"
        )
