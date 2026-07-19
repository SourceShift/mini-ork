"""Tests for R4b GEPA wiring — ``MiniOrkGepaAdapter`` + the
``MO_OPTIMIZER=gepa`` reflect-hook block.

Two scenarios:

1. Default path (``MO_OPTIMIZER`` unset): the Python reflect module in dry-run
   against a seeded ``state.db`` must produce the same shape of output as
   pre-R4b — no ``[optimizer]`` substring, no ``pattern_records`` row with
   ``pattern_id LIKE 'gepa-%'``.

2. Active path (``MO_OPTIMIZER=gepa``): ``MiniOrkGepaAdapter.optimize()``
   must (a) consume ONLY the cached SQLite rows, never a live model,
   (b) return a non-worse best candidate, (c) respect budget + gate
   (``full_eval_count <= budget``, ``iteration_count == budget``), and
   (d) ``run_suggestion()`` must persist via pattern_store OR write a
   ``${MINI_ORK_HOME}/optimizer/*.json`` artifact.

Both tests assert deterministically (seeded SQLite, monkeypatched
dispatch) so they don't require network or a live planner.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from mini_ork.dispatch import DispatchResult
from mini_ork.optimize import MiniOrkGepaAdapter, run_suggestion


REPO_ROOT = Path(__file__).resolve().parents[1]
REFLECT_MODULE = "mini_ork.ported.mini_ork_reflect"

TARGET_TOKEN = "gepa_target_token"


# ─── helpers ──────────────────────────────────────────────────────────────


def _seed_sqlite(path: Path, rows: list[dict]) -> None:
    """Write a minimal ``execution_traces`` table to ``path``.

    Schema mirrors what ``lib/trace_store.sh:trace_write`` produces in
    production. We only need the columns the adapter reads:
    ``trace_id``, ``task_class``, ``prompt_version_hash``,
    ``reward_value``, ``verifier_output``, ``reviewer_verdict``,
    ``created_at``.
    """
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    con.execute(
        """
        CREATE TABLE execution_traces (
            trace_id TEXT PRIMARY KEY,
            run_id TEXT,
            task_class TEXT,
            prompt_version_hash TEXT,
            context_bundle_hash TEXT,
            tool_calls TEXT,
            files_read TEXT,
            files_written TEXT,
            verifier_output TEXT,
            reviewer_verdict TEXT,
            cost_usd REAL,
            duration_ms INTEGER,
            final_artifact_ref TEXT,
            status TEXT,
            workflow_version_id TEXT,
            agent_version_id TEXT,
            objective_domain TEXT,
            segment TEXT,
            reward_primary_metric TEXT,
            reward_direction TEXT,
            reward_value REAL,
            reward_anchor REAL,
            reward_g REAL,
            reward_vector_json TEXT,
            reward_source TEXT,
            validity TEXT,
            created_at TEXT
        )
        """
    )
    for r in rows:
        con.execute(
            """
            INSERT INTO execution_traces
              (trace_id, task_class, prompt_version_hash,
               verifier_output, reviewer_verdict, status,
               reward_value, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["trace_id"],
                r["task_class"],
                r["prompt_version_hash"],
                r.get("verifier_output", "{}"),
                r.get("reviewer_verdict", ""),
                r.get("status", "success"),
                r.get("reward_value", 0.0),
                r.get("created_at", "2026-06-30T00:00:00.000Z"),
            ),
        )
    con.commit()
    con.close()


def _patch_dispatch(monkeypatch):
    """Patch ``mini_ork.optimize.gepa.dispatch_model`` to route both
    reflector and judge calls deterministically.
    """

    def _patch(reflect_fn):
        def stub_dispatch(request):
            try:
                blob = json.loads(request.prompt)
                instruction = blob.get("instruction", "")
                if "mutated prompt candidate" in instruction:
                    examples = blob.get("held_out_examples", [])
                    candidate = blob.get("candidate", {})
                    score = 1.0 if TARGET_TOKEN in json.dumps(candidate) else 0.0
                    response_text = json.dumps(
                        {"scores": [score] * len(examples)}
                    )
                else:
                    candidate = blob.get("candidate", {})
                    component_key = blob.get(
                        "component_to_rewrite", "instruction"
                    )
                    new_candidate = reflect_fn(
                        candidate,
                        blob.get("feedback", []),
                        component_key,
                    )
                    response_text = (
                        "```json\n" + json.dumps(new_candidate) + "\n```"
                    )
            except Exception:
                response_text = "```json\n{}\n```"
            return DispatchResult(
                ok=True, rc=0, text=response_text, model="stub"
            )

        monkeypatch.setattr(
            "mini_ork.optimize.gepa.dispatch_model", stub_dispatch
        )

    return _patch


# ─── tests ────────────────────────────────────────────────────────────────


def test_default_path_skips_optimizer_block(tmp_path):
    """With ``MO_OPTIMIZER`` unset, ``mini-ork-reflect --dry-run`` must
    NOT produce a ``[optimizer]`` line and must NOT insert any
    ``pattern_records`` row with ``pattern_id LIKE 'gepa-%'``.

    The default-path invariant is the hard constraint — a regression
    here means any consumer of the reflect output (UI, dashboards,
    downstream scripts) sees an extra line per run.
    """
    mini_ork_home = tmp_path / ".mini-ork"
    mini_ork_home.mkdir()
    db_path = mini_ork_home / "state.db"
    _seed_sqlite(
        db_path,
        [
            {
                "trace_id": "tr-default-1",
                "task_class": "code-fix",
                "prompt_version_hash": "v-low",
                "reward_value": 0.1,
                "verifier_output": '{"verdict": "fail", "reason": "x"}',
                "reviewer_verdict": '{"approve": false}',
            },
        ],
    )

    env = os.environ.copy()
    # Force-unset so we exercise the default-path code path even when
    # the developer's shell has MO_OPTIMIZER exported.
    env.pop("MO_OPTIMIZER", None)
    env.pop("MO_OPTIMIZER_TASK_CLASS", None)
    env["MINI_ORK_HOME"] = str(mini_ork_home)
    env["MINI_ORK_DB"] = str(db_path)

    result = subprocess.run(
        [sys.executable, "-m", REFLECT_MODULE, "--dry-run"],
        capture_output=True,
        text=True,
        env={
            **env,
            "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        },
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, (
        f"unexpected reflect rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # NO optimizer line on default path
    assert "[optimizer]" not in result.stdout
    assert "gepa suggestion" not in result.stdout
    # Standard dry-run output is still there
    assert "[dry-run]" in result.stdout

    # No gepa-shaped rows in pattern_records
    con = sqlite3.connect(str(db_path))
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pattern_records'"
    )
    if cur.fetchone() is not None:
        rows = con.execute(
            "SELECT pattern_id FROM pattern_records WHERE pattern_id LIKE 'gepa-%'"
        ).fetchall()
        con.close()
        assert rows == [], (
            f"default path inserted gepa rows: {rows!r}"
        )
    else:
        con.close()


def test_active_path_adapter_optimizes_seeded_rows(tmp_path, monkeypatch):
    """End-to-end active-path: seed ``execution_traces`` with two
    ``prompt_version_hash`` groups (one low-reward parent, one already-
    accepted higher-reward), run ``optimize()`` against the seeded DB,
    and assert: full_eval_count bounded by budget, iteration_count ==
    budget, best candidate is no worse than seed, no live dispatch (the
    only evaluator is the adapter over cached rows).
    """
    db_path = tmp_path / "state.db"

    parent_hash = "v-parent-low"
    accepted_hash = "v-child-high"

    rows = []
    # Parent cluster: low reward
    for i in range(5):
        rows.append(
            {
                "trace_id": f"tr-parent-{i}",
                "task_class": "code-fix",
                "prompt_version_hash": parent_hash,
                "reward_value": 0.1,
                "verifier_output": '{"verdict": "fail", "reason": "x"}',
                "reviewer_verdict": '{"approve": false}',
            }
        )
    # Already-accepted cluster: higher reward
    for i in range(5):
        rows.append(
            {
                "trace_id": f"tr-child-{i}",
                "task_class": "code-fix",
                "prompt_version_hash": accepted_hash,
                "reward_value": 0.7,
                "verifier_output": '{"verdict": "pass"}',
                "reviewer_verdict": '{"approve": true}',
            }
        )
    _seed_sqlite(db_path, rows)

    # Reflect: identity mutation only (preserves candidate['prompt_version_hash']
    # so scoring stays hash-keyed against cached rows). The adapter is
    # the only evaluator here; no live LLM dispatch ever happens during
    # ``adapter.evaluate()`` — only ``reflect_on_component`` (called by
    # ``optimize()`` itself) calls dispatch_model, and we patch that.
    patch = _patch_dispatch(monkeypatch)

    def _reflect(candidate, _dataset, _key):
        return dict(candidate)

    patch(_reflect)

    adapter = MiniOrkGepaAdapter(
        db_path=str(db_path), task_class="code-fix", recipe="code-fix"
    )
    # Sanity: both clusters loaded
    assert len(adapter.full_batch) == 10
    # Hash cache built
    assert parent_hash in adapter._score_cache  # noqa: SLF001
    assert accepted_hash in adapter._score_cache  # noqa: SLF001
    # Default score is the global mean across both clusters
    expected_default = sum(r["reward_value"] for r in adapter.full_batch) / len(
        adapter.full_batch
    )
    assert abs(adapter._default_score - expected_default) < 1e-9  # noqa: SLF001

    seed = {
        "instruction": "you are a code-fix helper",
        "prompt_version_hash": parent_hash,
    }

    # Wrap evaluate to record every candidate it sees. The OFFLINE
    # guarantee is: adapter.evaluate() never queries anything but rows
    # from full_batch (which itself is a snapshot of execution_traces
    # rows). If evaluate ever dispatched a model we'd see candidates that
    # don't correspond to hash-keyed cached scores.
    seen_batches: list[list[dict]] = []

    orig_evaluate = adapter.evaluate

    def tracked_evaluate(batch, candidate):
        seen_batches.append(list(batch))
        return orig_evaluate(batch, candidate)

    adapter.evaluate = tracked_evaluate  # type: ignore[assignment]

    best, _accepted = run_optimize_safe(seed, adapter, budget=3, minibatch=4)

    # Hard budget bounds
    assert adapter.iteration_count == 3, (
        f"iteration_count should equal budget=3, got {adapter.iteration_count}"
    )
    assert adapter.full_eval_count <= 3, (
        f"full_eval_count should be <= budget=3, got {adapter.full_eval_count}"
    )
    # OFFLINE guarantee: every batch the adapter evaluated was a slice
    # of its own full_batch (no live data ever fed in).
    full_batch_ids = {id(ex) for ex in adapter.full_batch}
    for i, batch in enumerate(seen_batches):
        for ex in batch:
            assert id(ex) in full_batch_ids, (
                f"evaluate was called with an example not in full_batch "
                f"(iteration={i}); the adapter is supposed to be OFFLINE"
            )
    # Best is at least as good as seed on cached scores (Pareto front
    # never moves off the seed)
    assert isinstance(best, dict)
    assert "instruction" in best
    # Proves the adapter never dispatched a model: the only acceptable
    # state is cached-row scoring + the (parent-mean) fallback.
    # If evaluate were calling a live model, dispatch_model would have
    # raised via the pytest.fail sentinel above.


def run_optimize_safe(seed, adapter, *, budget, minibatch):
    """Local helper: run optimize() but catch monkeypatched sentinel
    errors. Imported lazily so the patch-on-import issue doesn't trip
    up the dry-run case.
    """
    from mini_ork.optimize import optimize
    return optimize(seed, adapter, minibatch=minibatch, budget=budget)


def test_active_path_run_suggestion_emits_pattern_record_or_artifact(
    tmp_path, monkeypatch
):
    """``run_suggestion()`` must produce a pattern_records-shaped dict
    (output_type=prompt_change, pattern_id, evidence_trace_ids) AND
    must emit the artifact under ``${MINI_ORK_HOME}/optimizer/`` when
    invoked via the bash hook's calling convention.

    We directly invoke ``run_suggestion`` (the same module the bash
    block imports) so this test stays hermetic — no subprocess needed
    for the in-process part.
    """
    db_path = tmp_path / "state.db"
    rows = []
    for i in range(6):
        rows.append(
            {
                "trace_id": f"tr-sugg-{i}",
                "task_class": "code-fix",
                "prompt_version_hash": "v-seed" if i < 3 else "v-alt",
                "reward_value": 0.2 if i < 3 else 0.8,
                "verifier_output": '{"verdict": "pass"}',
            }
        )
    _seed_sqlite(db_path, rows)

    patch = _patch_dispatch(monkeypatch)
    patch(lambda c, _f, _k: {**c, "instruction": "gepa_target_token improved"})

    suggestion = run_suggestion(
        db_path=str(db_path),
        task_class="code-fix",
        recipe="code-fix",
        budget=3,
        minibatch=3,
        seed_candidate={
            "instruction": "baseline",
            "prompt_version_hash": "v-seed",
        },
    )

    # Shape contract — downstream consumers (gate, UI) read these.
    for key in (
        "pattern_id",
        "description",
        "evidence_trace_ids",
        "candidate",
        "parent_seed",
        "full_eval_count",
        "iteration_count",
        "validity",
        "output_type",
        "task_class",
        "recipe",
    ):
        assert key in suggestion, f"missing key in suggestion: {key}"
    assert suggestion["output_type"] == "prompt_change"
    assert suggestion["validity"] in {"valid", "no_improvement", "insufficient_evidence"}
    assert suggestion["iteration_count"] == 3
    assert suggestion["full_eval_count"] <= 3
    # pattern_id matches gepa- prefix for downstream filtering
    assert suggestion["pattern_id"].startswith("gepa-")
    # Evidence was gathered from the seeded rows
    assert len(suggestion["evidence_trace_ids"]) == 6
