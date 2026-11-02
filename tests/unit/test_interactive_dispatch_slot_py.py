"""Person-initiated work gets one dispatch slot the batch cannot take (P9).

With ``MINI_ORK_MAX_PARALLEL`` saturated, a rerun a human just asked for used
to queue behind a whole batch. An interactive node now dispatches first and the
pool is sized one wider — but only while such a node is actually in the batch,
and the outcome list is put back in field order because every downstream
consumer pairs outcomes to fields positionally.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import execute  # noqa: E402

FIELDS = [("a", "researcher"), ("b", "researcher"), ("c", "researcher")]


def test_no_interactive_node_keeps_the_documented_limit(monkeypatch):
    monkeypatch.delenv("MO_INTERACTIVE", raising=False)
    monkeypatch.delenv("MO_INTERACTIVE_NODES", raising=False)
    monkeypatch.setenv("MINI_ORK_MAX_PARALLEL", "1")
    order, workers = execute._plan_batch(FIELDS)
    assert order == [0, 1, 2]
    assert workers == 1


def test_an_interactive_node_moves_to_the_front_and_buys_a_slot(monkeypatch):
    monkeypatch.delenv("MO_INTERACTIVE", raising=False)
    monkeypatch.setenv("MO_INTERACTIVE_NODES", "c")
    monkeypatch.setenv("MINI_ORK_MAX_PARALLEL", "1")
    order, workers = execute._plan_batch(FIELDS)
    assert order == [2, 0, 1]
    assert workers == 2


def test_the_whole_invocation_can_be_marked(monkeypatch):
    monkeypatch.setenv("MO_INTERACTIVE", "1")
    monkeypatch.setenv("MINI_ORK_MAX_PARALLEL", "2")
    order, workers = execute._plan_batch(FIELDS)
    assert order == [0, 1, 2]
    assert workers == 3


def test_the_extra_slot_never_exceeds_the_batch(monkeypatch):
    monkeypatch.setenv("MO_INTERACTIVE", "1")
    monkeypatch.setenv("MINI_ORK_MAX_PARALLEL", "4")
    _, workers = execute._plan_batch(FIELDS)
    assert workers == len(FIELDS)


def test_outcomes_come_back_in_field_order_after_reordering(monkeypatch):
    """The pool returns in dispatch order; the caller must see field order."""
    seen: dict = {}

    class _Pool:
        def __init__(self, max_workers):
            seen["workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, fn, payloads):
            seen["order"] = [p[0][0] for p in payloads]
            return [fn(p) for p in payloads]

    def _worker(payload):
        node = payload[0][0]
        return (0 if node == "c" else 1), f"{node}-done", f"stdout {node}", ""

    monkeypatch.setenv("MO_INTERACTIVE_NODES", "c")
    monkeypatch.setenv("MINI_ORK_MAX_PARALLEL", "1")
    monkeypatch.setattr(execute.concurrent.futures, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(execute, "_isolated_dispatch_worker", _worker)

    outcomes = execute._run_parallel_batch(
        FIELDS, root=".", run_dir=".", plan_path="", task_class="code_fix",
        db="", run_id="r1", recipe="", workflow="")

    assert seen["order"] == ["c", "a", "b"]
    assert seen["workers"] == 2
    assert [field[0] for field, _rc, _reason in outcomes] == ["a", "b", "c"]
    assert [rc for _field, rc, _reason in outcomes] == [1, 1, 0]
    assert [reason for _field, _rc, reason in outcomes] == ["a-done", "b-done", "c-done"]


def test_the_flag_parses_and_publishes_the_env(monkeypatch):
    monkeypatch.delenv("MO_INTERACTIVE", raising=False)
    args, rc = execute._parse_execute_argv(["--interactive"])
    assert rc == 0 and args is not None and args.interactive is True
    args, rc = execute._parse_execute_argv([])
    assert rc == 0 and args is not None and args.interactive is False


def test_the_env_default_is_honoured_without_the_flag(monkeypatch):
    monkeypatch.setenv("MO_INTERACTIVE", "1")
    args, rc = execute._parse_execute_argv([])
    assert rc == 0 and args is not None and args.interactive is True


def test_an_empty_batch_asks_for_no_workers():
    assert execute._plan_batch([]) == ([], 0)
