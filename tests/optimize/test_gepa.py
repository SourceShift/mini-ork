from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mini_ork.dispatch import DispatchResult
from mini_ork.optimize import MiniOrkGepaAdapter, optimize


TARGET = "gepa-target-improved-instruction"


def _seed_sqlite(path: Path, rows: list[dict]) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        """
        CREATE TABLE execution_traces (
            trace_id TEXT PRIMARY KEY,
            task_class TEXT,
            prompt_version_hash TEXT,
            reward_value REAL,
            verifier_output TEXT,
            reviewer_verdict TEXT,
            created_at TEXT
        )
        """
    )
    for row in rows:
        con.execute(
            """
            INSERT INTO execution_traces
              (trace_id, task_class, prompt_version_hash, reward_value,
               verifier_output, reviewer_verdict, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["trace_id"],
                row["task_class"],
                row["prompt_version_hash"],
                row.get("reward_value", 0.0),
                row.get("verifier_output", "{}"),
                row.get("reviewer_verdict", ""),
                row.get("created_at", "2026-07-10T00:00:00.000Z"),
            ),
        )
    con.commit()
    con.close()


def _patch_reflector_and_judge(monkeypatch) -> None:
    def router(request):
        blob = json.loads(request.prompt)
        instruction = blob.get("instruction", "")
        if "mutated prompt candidate" in instruction:
            examples = blob.get("held_out_examples", [])
            candidate = blob.get("candidate", {})
            score = 1.0 if candidate.get("instruction") == TARGET else 0.0
            return DispatchResult(
                ok=True,
                rc=0,
                text=json.dumps({"scores": [score] * len(examples)}),
                model=request.model,
            )
        key = blob.get("component_to_rewrite", "instruction")
        return DispatchResult(
            ok=True,
            rc=0,
            text=json.dumps({key: TARGET}),
            model=request.model,
        )

    monkeypatch.setattr("mini_ork.optimize.gepa.dispatch_model", router)


def test_bad_seed_better_mutation_accepted(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _seed_sqlite(
        db_path,
        [
            {
                "trace_id": f"tr-bad-{i}",
                "task_class": "code-fix",
                "prompt_version_hash": "bad-seed",
                "reward_value": 0.1,
                "verifier_output": '{"verdict":"fail"}',
            }
            for i in range(4)
        ],
    )
    _patch_reflector_and_judge(monkeypatch)

    adapter = MiniOrkGepaAdapter(
        db_path=db_path,
        task_class="code-fix",
        recipe="code-fix",
        evaluator_model="minimax",
    )
    seed = {"instruction": "bad seed", "prompt_version_hash": "bad-seed"}

    best, accepted = optimize(
        seed,
        adapter,
        budget=2,
        minibatch=2,
        model="minimax",
    )

    assert accepted
    assert adapter.full_eval_count > 0
    assert adapter.full_eval_count <= 2
    assert adapter.online_eval_count > 0
    assert best != seed
    assert best["instruction"] == TARGET
    assert "prompt_version_hash" not in best


def test_budget_bound(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _seed_sqlite(
        db_path,
        [
            {
                "trace_id": f"tr-budget-{i}",
                "task_class": "code-fix",
                "prompt_version_hash": "seed-hash",
                "reward_value": 0.2,
            }
            for i in range(3)
        ],
    )
    _patch_reflector_and_judge(monkeypatch)

    adapter = MiniOrkGepaAdapter(db_path=db_path, task_class="code-fix")
    optimize(
        {"instruction": "seed", "prompt_version_hash": "seed-hash"},
        adapter,
        budget=1,
        minibatch=2,
        model="minimax",
    )

    assert adapter.full_eval_count <= 1


def test_offline_hash_fast_path(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _seed_sqlite(
        db_path,
        [
            {
                "trace_id": f"tr-fast-{i}",
                "task_class": "code-fix",
                "prompt_version_hash": "cached-hash",
                "reward_value": 0.5,
            }
            for i in range(2)
        ],
    )

    def forbidden_dispatch(_request):
        raise AssertionError("cached-hash fast path must not dispatch")

    monkeypatch.setattr("mini_ork.optimize.gepa.dispatch_model", forbidden_dispatch)

    adapter = MiniOrkGepaAdapter(db_path=db_path, task_class="code-fix")
    scores, traces = adapter.evaluate(
        adapter.full_batch,
        {"instruction": "cached", "prompt_version_hash": "cached-hash"},
    )

    assert scores == [0.5, 0.5]
    assert traces == adapter.full_batch
    assert adapter.online_eval_count == 0
