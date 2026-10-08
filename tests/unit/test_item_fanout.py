from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from mini_ork.orchestration.item_fanout import (
    STATUS_CACHED,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_TIMEOUT,
    item_id,
    result_path,
    run_items,
)


def _items(*ids: str) -> list[dict[str, str]]:
    return [{"id": i} for i in ids]


def _ok(_item: dict) -> dict:
    return {"status": STATUS_OK}


def _manifest(results_dir: Path) -> dict:
    return json.loads((results_dir / "manifest.json").read_text(encoding="utf-8"))


# --- identity and paths ----------------------------------------------------


def test_item_id_prefers_id_then_item_id():
    assert item_id({"id": "A-1"}) == "A-1"
    assert item_id({"item_id": "B-2"}) == "B-2"


def test_item_id_raises_without_an_identity():
    # A synthesized id would collide across items and silently mark work done.
    with pytest.raises(ValueError):
        item_id({"title": "no id here"})


def test_slug_cannot_escape_results_dir(tmp_path: Path):
    # Item ids are often relative paths; a raw f-string filename would land
    # outside results_dir for a parent-relative id.
    escapee = result_path(tmp_path / "results", "../../etc/passwd")
    assert escapee.parent == (tmp_path / "results")
    assert "/" not in escapee.name


# --- the happy path --------------------------------------------------------


def test_sequential_runs_every_item_and_writes_manifest(tmp_path: Path):
    seen: list[str] = []

    def worker(item: dict) -> dict:
        seen.append(item["id"])
        return {"status": STATUS_OK}

    result = run_items(
        _items("A", "B", "C"), results_dir=tmp_path, worker=worker, max_workers=1
    )

    assert seen == ["A", "B", "C"]
    assert result.count(STATUS_OK) == 3
    assert result.verdict == "pass"
    assert (tmp_path / "A.json").is_file()
    manifest = _manifest(tmp_path)
    # Input order is preserved so a manifest diff is stable across runs.
    assert [entry["item_id"] for entry in manifest["items"]] == ["A", "B", "C"]


def test_result_payload_is_checkpointed_verbatim(tmp_path: Path):
    run_items(
        _items("A"),
        results_dir=tmp_path,
        worker=lambda _i: {"status": STATUS_OK, "note": "audited"},
        max_workers=1,
    )
    payload = json.loads((tmp_path / "A.json").read_text(encoding="utf-8"))
    assert payload["note"] == "audited"
    assert payload["item_id"] == "A"


# --- checkpoint / resume (mutation target: skip-if-present) ----------------


def test_resume_skips_a_completed_item(tmp_path: Path):
    calls: list[str] = []

    def worker(item: dict) -> dict:
        calls.append(item["id"])
        return {"status": STATUS_OK}

    run_items(_items("A"), results_dir=tmp_path, worker=worker, max_workers=1)
    assert calls == ["A"]

    result = run_items(_items("A", "B"), results_dir=tmp_path, worker=worker, max_workers=1)

    # A is cached: the worker must NOT run again, and B is the only fresh call.
    assert calls == ["A", "B"]
    assert result.count(STATUS_CACHED) == 1
    assert [o.item_id for o in result.items if o.status == STATUS_CACHED] == ["A"]


def test_resume_false_reruns_a_completed_item(tmp_path: Path):
    calls: list[str] = []
    run_items(_items("A"), results_dir=tmp_path, worker=lambda i: calls.append(i["id"]) or {"status": STATUS_OK})

    run_items(
        _items("A"), results_dir=tmp_path, worker=lambda i: calls.append(i["id"]) or {"status": STATUS_OK},
        resume=False,
    )
    assert calls == ["A", "A"]


def test_truncated_checkpoint_is_not_treated_as_done(tmp_path: Path):
    # A partial write from a killed process must not read as "already audited".
    (tmp_path / "A.json").write_text('{"note": "no status key"}', encoding="utf-8")
    calls: list[str] = []

    run_items(
        _items("A"),
        results_dir=tmp_path,
        worker=lambda i: calls.append(i["id"]) or {"status": STATUS_OK},
    )
    assert calls == ["A"]


def test_corrupt_json_checkpoint_is_not_treated_as_done(tmp_path: Path):
    (tmp_path / "A.json").write_text("{not json", encoding="utf-8")
    calls: list[str] = []
    run_items(
        _items("A"),
        results_dir=tmp_path,
        worker=lambda i: calls.append(i["id"]) or {"status": STATUS_OK},
    )
    assert calls == ["A"]


# --- failure containment ---------------------------------------------------


def test_worker_exception_is_recorded_not_raised(tmp_path: Path):
    def worker(item: dict) -> dict:
        if item["id"] == "B":
            raise RuntimeError("boom")
        return {"status": STATUS_OK}

    result = run_items(_items("A", "B", "C"), results_dir=tmp_path, worker=worker)

    # The crash of B must not abort C.
    assert result.count(STATUS_OK) == 2
    assert result.count(STATUS_FAILED) == 1
    failed = next(o for o in result.items if o.status == STATUS_FAILED)
    assert "RuntimeError: boom" in failed.error
    assert result.verdict == "fail"


def test_non_dict_return_is_a_recorded_failure(tmp_path: Path):
    result = run_items(_items("A"), results_dir=tmp_path, worker=lambda _i: "nope")
    assert result.count(STATUS_FAILED) == 1


def test_worker_reported_failed_status_is_honoured(tmp_path: Path):
    result = run_items(
        _items("A"), results_dir=tmp_path, worker=lambda _i: {"status": STATUS_FAILED, "error": "gate"}
    )
    assert result.verdict == "fail"
    assert result.items[0].error == "gate"


# --- budget (mutation target: defer-the-remainder) -------------------------


def test_budget_defers_the_remainder(tmp_path: Path):
    def worker(item: dict) -> dict:
        return {"status": STATUS_OK, "cost_usd": 0.60}

    result = run_items(_items("A", "B", "C"), results_dir=tmp_path, worker=worker, budget_usd=1.0)

    # A costs 0.60 (< 1.0) so B is admitted and pushes spend to 1.20; C is deferred.
    statuses = {o.item_id: o.status for o in result.items}
    assert statuses == {"A": STATUS_OK, "B": STATUS_OK, "C": STATUS_DEFERRED}
    assert not (tmp_path / "C.json").exists()  # never attempted → no checkpoint


def test_budget_defers_remainder_in_the_pool_path(tmp_path: Path):
    # The pool must re-read spend between items. If it submits the whole queue
    # in one pass (while spent is still 0) nothing is ever deferred, so this
    # pins that the budget also binds when max_workers > 1.
    def worker(_item: dict) -> dict:
        return {"status": STATUS_OK, "cost_usd": 0.40}

    result = run_items(
        _items(*[f"I{n}" for n in range(6)]),
        results_dir=tmp_path,
        worker=worker,
        max_workers=2,
        budget_usd=1.0,
    )
    assert result.count(STATUS_DEFERRED) >= 1
    assert result.count(STATUS_OK) >= 1
    # Every item is accounted for exactly once.
    assert sum(
        result.count(s) for s in (STATUS_OK, STATUS_FAILED, STATUS_TIMEOUT, STATUS_CACHED, STATUS_DEFERRED)
    ) == 6


def test_all_deferred_is_inconclusive_not_pass(tmp_path: Path):
    # Reporting success while nothing ran is the vacuous-verdict failure.
    result = run_items(
        _items("A", "B"), results_dir=tmp_path, worker=_ok, budget_usd=0.0
    )
    assert result.count(STATUS_DEFERRED) == 2
    assert result.count(STATUS_OK) == 0
    assert result.verdict == "inconclusive"


# --- bounded pool (mutation target: the worker cap) ------------------------


def test_pool_bounds_concurrency_and_is_actually_concurrent(tmp_path: Path):
    lock = threading.Lock()
    live = 0
    peak = 0

    def worker(_item: dict) -> dict:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        try:
            time.sleep(0.05)
        finally:
            with lock:
                live -= 1
        return {"status": STATUS_OK}

    result = run_items(
        _items(*[f"I{n}" for n in range(12)]),
        results_dir=tmp_path,
        worker=worker,
        max_workers=3,
    )

    assert result.count(STATUS_OK) == 12
    assert peak <= 3, f"pool exceeded its cap: peak={peak}"
    assert peak >= 2, f"pool never ran concurrently: peak={peak}"


def test_pool_preserves_input_order_in_the_manifest(tmp_path: Path):
    ids = [f"I{n}" for n in range(8)]

    def worker(item: dict) -> dict:
        # Stagger completion so finishing order differs from submission order.
        time.sleep((7 - int(item["id"][1:])) * 0.01)
        return {"status": STATUS_OK}

    result = run_items(_items(*ids), results_dir=tmp_path, worker=worker, max_workers=4)
    assert [o.item_id for o in result.items] == ids
    assert [e["item_id"] for e in _manifest(tmp_path)["items"]] == ids


# --- timeout (mutation target: the deadline sweep) -------------------------


def test_item_timeout_is_recorded(tmp_path: Path):
    def worker(_item: dict) -> dict:
        time.sleep(0.4)
        return {"status": STATUS_OK}

    result = run_items(
        _items("A", "B"),
        results_dir=tmp_path,
        worker=worker,
        max_workers=2,
        per_item_timeout=0.05,
    )
    assert result.count(STATUS_TIMEOUT) == 2
    assert result.verdict == "fail"


# --- manifest shape --------------------------------------------------------


def test_manifest_counts_add_up(tmp_path: Path):
    (tmp_path / "B.json").write_text(json.dumps({"status": STATUS_OK, "item_id": "B"}), encoding="utf-8")

    def worker(item: dict) -> dict:
        return {"status": STATUS_FAILED} if item["id"] == "C" else {"status": STATUS_OK}

    result = run_items(_items("A", "B", "C", "D"), results_dir=tmp_path, worker=worker)
    assert result.count(STATUS_OK) == 2  # B is cached, so only A and D ran
    manifest = _manifest(tmp_path)
    assert manifest["total"] == 4
    assert manifest["ok"] + manifest["failed"] + manifest["timeout"] + manifest["skipped_cached"] + manifest["deferred"] == 4
    assert manifest["verdict"] == "fail"
    assert manifest["max_workers"] == 1


def test_manifest_path_override(tmp_path: Path):
    target = tmp_path / "nested" / "out.json"
    run_items(_items("A"), results_dir=tmp_path / "r", worker=_ok, manifest_path=target)
    assert target.is_file()


def test_fanout_module_never_imports_the_db_or_dispatch():
    # The library must stay a pure filesystem primitive: importing it inside a
    # dispatch worker must not pull in the state DB or an LLM client. Assert on
    # parsed imports, not raw text — the docstrings legitimately *mention*
    # subprocess and the DB, so a substring check would fail on correct code.
    import ast

    import mini_ork.orchestration.item_fanout as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {"sqlite3", "subprocess", "mini_ork.cost_ledger", "mini_ork.dispatch.llm_dispatch"}
    assert not (imported & forbidden), f"impure imports: {sorted(imported & forbidden)}"
