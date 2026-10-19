"""Unit tests: trustworthy ``metrics context`` (LF-R13).

Hermetic — every run dir and every state DB lives under ``tmp_path``; nothing
reads or writes the live DB under ``.mini-ork/``.
"""
from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork import context_v2
from mini_ork.cli import execute_handlers, metrics_context
from mini_ork.learning import themes
from mini_ork.memory import semantic

REP = "the dispatch loop leaks file handles when a node retries"
REWORD = (
    "the dispatch loop leaks file handles when a node retries because the "
    "retry queue grows without bound and nobody closes the stream, the "
    "supervisor ignores backpressure signals, metrics dashboard panels freeze, "
    "logs rotate incorrectly, disk usage climbing steadily while alerts fire "
    "randomly across every region and cluster shard overnight"
)
UNRELATED = "documentation chapter ordering confuses new readers"

_DDL = """
CREATE TABLE IF NOT EXISTS code_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT    NOT NULL UNIQUE,
    run_id       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    file         TEXT,
    line         INTEGER,
    severity     TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    issue        TEXT    NOT NULL,
    snippet      TEXT,
    verdict      TEXT,
    ts           INTEGER NOT NULL,
    target_repo  TEXT
);
"""


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    con = sqlite3.connect(str(path))
    con.executescript(_DDL)
    con.commit()
    con.close()
    return path


def _run(tmp_path: Path, name: str, *, arm="v2", themes_once=None,
         kickoff=None, finding: str | None = None) -> Path:
    """One run dir with a hand-built v2 pack; optionally one harvested finding."""
    d = tmp_path / "runs" / name
    d.mkdir(parents=True)
    pack = {
        "run_id": name,
        "arm": arm,
        "item_ids": ["f:x"],
        "contract": {},
        "file_findings": [{"id": "f:x", "representative": REP, "n": 1}],
    }
    if themes_once is not None:
        pack["themes_once"] = themes_once
    if kickoff is not None:
        pack["kickoff_path"] = kickoff
    context_v2.write_json(str(d / "context_pack.json"), pack)
    (d / context_v2.PACK_FILENAME).write_text(json.dumps(pack), encoding="utf-8")
    if finding is not None:
        con = sqlite3.connect(str(tmp_path / "state.db"))
        con.execute(
            "INSERT INTO code_findings (fingerprint, run_id, source, severity, "
            "category, issue, ts) VALUES (?, ?, 'reviewer', 'medium', "
            "'correctness', ?, ?)",
            (f"fp-{name}", name, finding, int(time.time())),
        )
        con.commit()
        con.close()
    return d


def _records(tmp_path: Path, db: Path, names: list[str], harvested: set[str]):
    return [r for r in (
        metrics_context.run_record(tmp_path / "runs" / n, db=str(db),
                                   harvested=harvested)
        for n in names) if r]


# ── 1. arm split (Q5) ───────────────────────────────────────────────────────


def test_v2_without_themes_once_is_v2_legacy(tmp_path):
    db = _db(tmp_path)
    legacy = _run(tmp_path, "legacy", arm="v2")
    marked = _run(tmp_path, "marked", arm="v2", themes_once=True)
    shadow = _run(tmp_path, "shadow", arm="shadow")
    hold = _run(tmp_path, "hold", arm="holdout")

    assert metrics_context.run_record(legacy, db=str(db), harvested=set())["arm"] \
        == "v2_legacy"
    assert metrics_context.run_record(marked, db=str(db), harvested=set())["arm"] \
        == "v2"
    # The split only touches the v2 arm.
    assert metrics_context.run_record(shadow, db=str(db), harvested=set())["arm"] \
        == "shadow"
    assert metrics_context.run_record(hold, db=str(db), harvested=set())["arm"] \
        == "holdout"

    result = metrics_context.summarize(
        _records(tmp_path, db, ["legacy", "marked", "shadow", "hold"], set()))
    assert result["arms"]["v2_legacy"]["runs"] == 1
    assert result["arms"]["v2"]["runs"] == 1


def test_legacy_harvest_uses_the_same_matcher(tmp_path):
    """A harvested legacy run with zero findings rows did not recur.

    ``v2_legacy`` is a measured arm like any other (it is only excluded from
    lift): inventing recurrence for it would turn its rate into a guess.
    """
    db = _db(tmp_path)
    _run(tmp_path, "legacy", arm="v2")                    # harvested, no findings
    _run(tmp_path, "marked", arm="v2", themes_once=True)  # harvested, no findings
    harvested = {"legacy", "marked"}

    result = metrics_context.summarize(
        _records(tmp_path, db, ["legacy", "marked"], harvested))
    assert result["arms"]["v2_legacy"]["recurrence_rate"] == 0.0
    assert result["arms"]["v2"]["recurrence_rate"] == 0.0


def test_lift_excludes_legacy_and_counts_it(tmp_path):
    db = _db(tmp_path)
    n = metrics_context.MIN_N
    names, harvested = [], set()
    for i in range(n):
        name = f"h{i}"
        _run(tmp_path, name, arm="holdout", finding=REP)
        names.append(name)
        harvested.add(name)
    for i in range(n):
        name = f"v{i}"
        _run(tmp_path, name, arm="v2", themes_once=True)
        names.append(name)
        harvested.add(name)
    for i in range(3):
        name = f"l{i}"
        _run(tmp_path, name, arm="v2", finding=REP)
        names.append(name)
        harvested.add(name)

    result = metrics_context.summarize(_records(tmp_path, db, names, harvested))
    assert result["arms"]["v2"]["recurrence_rate"] == 0.0
    assert result["arms"]["v2_legacy"]["recurrence_rate"] == 1.0
    lift = result["lift"]
    assert lift["n_v2_legacy"] == 3
    assert lift["n_v2"] == n and lift["n_holdout"] == n
    assert lift["value"] == pytest.approx(1.0)  # never 1.0 - legacy rate
    assert lift["status"] == "measured"


# ── 2. denominators: ITT, delivered, stem ───────────────────────────────────


def test_summarize_keeps_existing_keys_and_aliases_delivered(tmp_path):
    db = _db(tmp_path)
    _run(tmp_path, "a", themes_once=True, finding=REP)
    _run(tmp_path, "b", themes_once=True)
    harvested = {"a", "b"}
    row = metrics_context.summarize(
        _records(tmp_path, db, ["a", "b"], harvested))["arms"]["v2"]
    for key in ("runs", "planner_v2_rate", "node_v2_rate", "ack_rate",
                "invalid_ids", "outside_scope_rate", "deleted_tests_runs",
                "harvested_runs", "groups_checked", "recurrence_rate"):
        assert key in row
    assert row["recurrence_rate"] == pytest.approx(0.5)
    assert row["delivered_recurrence_rate"] == pytest.approx(0.5)
    assert row["delivered_runs"] == row["harvested_runs"] == 2
    assert row["itt_runs"] == 2
    assert row["itt_recurrence_rate"] == pytest.approx(0.5)


def test_itt_counts_unharvested_runs(tmp_path):
    db = _db(tmp_path)
    _run(tmp_path, "r1", themes_once=True, kickoff="/k/alpha-beta-r1.md", finding=REP)
    _run(tmp_path, "r2", themes_once=True, kickoff="/k/gamma-delta-r1.md")
    _run(tmp_path, "r3", themes_once=True, kickoff="/k/epsilon-zeta-r1.md")
    row = metrics_context.summarize(
        _records(tmp_path, db, ["r1", "r2", "r3"], {"r1", "r2"}))["arms"]["v2"]
    assert row["itt_runs"] == 3
    assert row["delivered_runs"] == 2
    assert row["itt_recurrence_rate"] == pytest.approx(0.333)
    assert row["delivered_recurrence_rate"] == pytest.approx(0.5)


def test_stem_from_kickoff_path_and_generic_falls_back(tmp_path):
    db = _db(tmp_path)
    named = metrics_context.run_record(
        _run(tmp_path, "s1", kickoff="/k/eng-path-rules-r2.md"),
        db=str(db), harvested=set())
    generic = metrics_context.run_record(
        _run(tmp_path, "s2", kickoff="/k/kickoff.md"),
        db=str(db), harvested=set())
    assert named["stem"] == "eng-path-rules"
    assert generic["stem"] == ""


def test_stem_buckets_collapse_retries_run_level_does_not(tmp_path):
    db = _db(tmp_path)
    _run(tmp_path, "run-a", themes_once=True, kickoff="/k/eng-path-rules-r2.md", finding=REP)
    _run(tmp_path, "run-b", themes_once=True, kickoff="/k/eng-path-rules-r5.md")
    row = metrics_context.summarize(
        _records(tmp_path, db, ["run-a", "run-b"], {"run-a", "run-b"}))["arms"]["v2"]
    assert row["stem_buckets"] == 1
    assert row["stem_recurrence_rate"] == pytest.approx(1.0)
    assert row["itt_runs"] == 2
    assert row["itt_recurrence_rate"] == pytest.approx(0.5)


def test_generic_stems_bucket_by_run_id(tmp_path):
    db = _db(tmp_path)
    _run(tmp_path, "run-c", themes_once=True, kickoff="/k/kickoff.md", finding=REP)
    _run(tmp_path, "run-d", themes_once=True, kickoff="/other/kickoff.md")
    row = metrics_context.summarize(
        _records(tmp_path, db, ["run-c", "run-d"], {"run-c", "run-d"}))["arms"]["v2"]
    assert row["stem_buckets"] == 2
    assert row["stem_recurrence_rate"] == pytest.approx(0.5)


def test_lift_gains_itt_keys_and_needs_min_n_per_arm(tmp_path):
    db = _db(tmp_path)
    n = metrics_context.MIN_N
    names, harvested = [], set()
    for i in range(n):
        name = f"v{i}"
        if i < n - 5:
            harvested.add(name)
        _run(tmp_path, name, arm="v2", themes_once=True)
        names.append(name)
    for i in range(n):
        name = f"h{i}"
        if i < n - 5:
            harvested.add(name)
        _run(tmp_path, name, arm="holdout", finding=REP)
        names.append(name)

    lift = metrics_context.summarize(_records(tmp_path, db, names, harvested))["lift"]
    for key in ("value", "status", "n_v2", "n_holdout", "n_v2_legacy",
                "value_itt", "status_itt", "n_v2_itt", "n_holdout_itt"):
        assert key in lift
    # delivered needs MIN_N harvested per arm (n-5 is short); ITT does not.
    assert lift["n_v2"] == n - 5 and lift["n_holdout"] == n - 5
    assert lift["status"] == "insufficient" and lift["value"] is None
    assert lift["n_v2_itt"] == n and lift["n_holdout_itt"] == n
    assert lift["status_itt"] == "measured"
    assert lift["value_itt"] == pytest.approx(round((n - 5) / n, 3))


def test_print_human_keeps_old_and_new_rates_and_both_lifts(capsys):
    arm = {
        "runs": 30, "planner_v2_rate": 1.0, "node_v2_rate": 1.0,
        "ack_rate": 0.5, "invalid_ids": 0, "outside_scope_rate": 0.0,
        "deleted_tests_runs": 0, "harvested_runs": 30, "groups_checked": 30,
        "recurrence_rate": 0.4, "itt_runs": 30, "itt_recurrence_rate": 0.5,
        "delivered_runs": 30, "delivered_recurrence_rate": 0.4,
        "stem_buckets": 10, "stem_recurrence_rate": 0.2,
    }
    result = {
        "arms": {"v2": dict(arm), "holdout": dict(arm)},
        "lift": {"value": 0.25, "status": "measured", "n_v2": 30,
                 "n_holdout": 30, "n_v2_legacy": 4, "value_itt": 0.5,
                 "status_itt": "measured", "n_v2_itt": 30, "n_holdout_itt": 30},
        "min_n": metrics_context.MIN_N,
    }
    out = io.StringIO()
    metrics_context._print_human(result, out)
    text = out.getvalue()
    for col in ("recurrence_rate", "itt_recurrence_rate",
                "delivered_recurrence_rate", "stem_recurrence_rate"):
        assert col in text
    assert any("lift" in ln.lower() and "0.25" in ln for ln in text.splitlines())
    assert any("lift" in ln.lower() and "itt" in ln.lower() and "0.5" in ln
               for ln in text.splitlines())


def test_print_human_handles_empty_arms():
    out = io.StringIO()
    metrics_context._print_human(
        {"arms": {}, "min_n": metrics_context.MIN_N,
         "lift": {"value": None, "status": "insufficient", "n_v2": 0,
                  "n_holdout": 0, "value_itt": None,
                  "status_itt": "insufficient", "n_v2_itt": 0,
                  "n_holdout_itt": 0}}, out)
    assert out.getvalue()


# ── 3. ledger provenance ────────────────────────────────────────────────────


def test_write_injection_record_carries_arm_and_held_out(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    context_v2.write_injection_record(
        str(run_dir), "planner", text="block", sources=[],
        extra={"context_v2": {"arm": "holdout"}})
    rec = json.loads((run_dir / "learned" / "planner.json").read_text())
    assert rec["arm"] == "holdout" and rec["held_out"] is True

    context_v2.write_injection_record(
        str(run_dir), "planner", text="block", sources=[],
        extra={"context_v2": {"arm": "v2"}})
    rec = json.loads((run_dir / "learned" / "planner.json").read_text())
    assert rec["arm"] == "v2" and rec["held_out"] is False

    for extra in (None, {}, {"context_v2": {}}, {"context_v2": {"arm": 7}}):
        context_v2.write_injection_record(
            str(run_dir), "planner", text="block", sources=[], extra=extra)
        rec = json.loads((run_dir / "learned" / "planner.json").read_text())
        assert rec["arm"] is None and rec["held_out"] is None


def test_write_learned_record_run_id_is_keyword_only_default_none():
    import inspect
    param = inspect.signature(
        execute_handlers._write_learned_record).parameters["run_id"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


def test_write_learned_record_records_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "1.0")
    hold = tmp_path / "hold"
    hold.mkdir()
    execute_handlers._write_learned_record(
        str(hold), "impl", "implementer", "lane", "framework_edit",
        attempt=1, block="text", sources=[], run_id="spec-run")
    rec = json.loads((hold / "learned" / "impl.json").read_text())
    assert rec["arm"] == "holdout" and rec["held_out"] is True

    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0.0")
    v2 = tmp_path / "v2"
    v2.mkdir()
    execute_handlers._write_learned_record(
        str(v2), "impl", "implementer", "lane", "framework_edit",
        attempt=1, block="text", sources=[], run_id="spec-run")
    rec = json.loads((v2 / "learned" / "impl.json").read_text())
    assert rec["arm"] == "v2" and rec["held_out"] is False

    # No run_id: the record keeps the pre-R13 shape with None provenance.
    plain = tmp_path / "plain"
    plain.mkdir()
    execute_handlers._write_learned_record(
        str(plain), "impl", "implementer", "lane", "framework_edit",
        attempt=1, block="text", sources=[])
    rec = json.loads((plain / "learned" / "impl.json").read_text())
    assert rec["arm"] is None and rec["held_out"] is None and rec["injected"] is True


# ── 4. paraphrase-tolerant recurrence ───────────────────────────────────────


def _cluster(cid="f:1", rep=REP):
    return {"id": cid, "representative": rep, "n": 1}


def test_containment_and_embed_constants():
    assert context_v2.CONTAINMENT_MIN == 0.6
    assert context_v2.EMBED_MIN == 0.6
    assert context_v2.DEFAULT_SIM == 0.35


def test_reworded_pair_recurs_via_containment(monkeypatch):
    monkeypatch.delenv("MO_EMBED_PROVIDER", raising=False)
    idf, unseen = context_v2._idf_over(
        [context_v2.clean_issue(REP), context_v2.clean_issue(REWORD)])
    cos = themes._dot(
        themes._vector(context_v2.clean_issue(REP), idf, unseen),
        themes._vector(context_v2.clean_issue(REWORD), idf, unseen))
    assert cos < context_v2.DEFAULT_SIM  # the premise: cosine alone misses it

    hits = context_v2.recurrence([_cluster()], [{"issue": REWORD}])
    assert hits["f:1"] is True
    assert context_v2.recurrence([_cluster()], [{"issue": REP}])["f:1"] is True
    assert context_v2.recurrence([_cluster()], [{"issue": UNRELATED}])["f:1"] is False


def test_token_set_and_containment_helpers():
    assert context_v2._token_set("") == set()
    assert context_v2._token_set(REP) == set(
        themes._tokenize(context_v2.clean_issue(REP)))
    f = context_v2._containment
    assert f(set(), {"a"}) == 0.0 and f({"a"}, set()) == 0.0
    # Symmetric: a subset restatement (either direction) is full containment.
    assert f({"a", "b"}, {"a", "b", "c"}) == pytest.approx(1.0)
    assert f({"a", "b", "c", "d"}, {"a", "b"}) == pytest.approx(1.0)
    assert f({"a", "b", "c"}, {"a", "b", "d"}) == pytest.approx(2 / 3)
    assert f({"a", "b"}, {"c", "d"}) == 0.0


def test_embedding_path_off_without_provider(monkeypatch):
    monkeypatch.delenv("MO_EMBED_PROVIDER", raising=False)

    def _bomb(*a, **k):
        raise AssertionError("provider lookup while MO_EMBED_PROVIDER is unset")

    monkeypatch.setattr(semantic, "get_embedder", _bomb)
    assert context_v2._embedding_cosine("a", "b") is None
    assert context_v2.recurrence([_cluster()], [{"issue": UNRELATED}])["f:1"] is False


def test_fake_provider_makes_unrelated_pair_recur(monkeypatch):
    monkeypatch.setenv("MO_EMBED_PROVIDER", "unit-identical-vectors")

    class _Same:
        def embed(self, texts):
            return [[0.6, 0.8] for _ in texts]

    monkeypatch.setitem(semantic.EMBEDDER_PROVIDERS, "unit-identical-vectors", _Same)
    assert context_v2._embedding_cosine(REP, UNRELATED) == pytest.approx(1.0)
    assert context_v2.recurrence([_cluster()], [{"issue": UNRELATED}])["f:1"] is True


def test_broken_provider_degrades_to_none(monkeypatch):
    monkeypatch.setenv("MO_EMBED_PROVIDER", "unit-embed-boom")

    class _Boom:
        def embed(self, texts):
            raise RuntimeError("down")

    monkeypatch.setitem(semantic.EMBEDDER_PROVIDERS, "unit-embed-boom", _Boom)
    assert context_v2._embedding_cosine("a", "b") is None
    assert context_v2.recurrence([_cluster()], [{"issue": UNRELATED}])["f:1"] is False


def test_cluster_unchanged():
    clusters = context_v2.cluster(
        [{"issue": REP, "severity": "high", "run_id": "r1"},
         {"issue": UNRELATED, "severity": "high", "run_id": "r1"}])
    assert len(clusters) == 2
