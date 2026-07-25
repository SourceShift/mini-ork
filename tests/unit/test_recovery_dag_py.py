"""Direct unit coverage for ``mini_ork.recovery.dag`` (the pure DAG
data structure split out of the recovery planner).

These tests exercise the loader + ``descendants`` directly — no
checkpoint DB, no run dir, no planner env. The end-to-end closure
semantics are covered by ``tests/test_recovery_closure.py``; this file
pins the data-structure contract on its own.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.recovery.dag import DAG, load_dag
from mini_ork.recovery import planner as rp


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "workflow.yaml"
    p.write_text(body)
    return str(p)


# ─── load_dag: parsing + adjacency ──────────────────────────────────────────

def test_load_dag_linear_adjacency_and_topo(tmp_path: Path) -> None:
    wf = _write(tmp_path, """
nodes:
  - {name: A}
  - {name: B}
  - {name: C}
edges:
  - {from: A, to: B, edge_type: depends_on}
  - {from: B, to: C, edge_type: depends_on}
""")
    dag = load_dag(wf)
    assert dag.node_ids == ("A", "B", "C")
    assert dag.parents == {"A": (), "B": ("A",), "C": ("B",)}
    assert dag.children == {"A": ("B",), "B": ("C",), "C": ()}
    assert dag.topo == ("A", "B", "C")


def test_load_dag_topo_ties_break_by_declaration_order(tmp_path: Path) -> None:
    # Diamond: A → B, A → C, B+C → D. B and C become ready together;
    # declaration order (B before C) must win.
    wf = _write(tmp_path, """
nodes:
  - {name: A}
  - {name: B}
  - {name: C}
  - {name: D}
edges:
  - {from: A, to: B, edge_type: depends_on}
  - {from: A, to: C, edge_type: depends_on}
  - {from: B, to: D, edge_type: depends_on}
  - {from: C, to: D, edge_type: depends_on}
""")
    dag = load_dag(wf)
    assert dag.topo == ("A", "B", "C", "D")


def test_load_dag_excludes_escalates_to_edges(tmp_path: Path) -> None:
    wf = _write(tmp_path, """
nodes:
  - {name: W}
  - {name: V}
  - {name: R}
edges:
  - {from: W, to: V, edge_type: verifies}
  - {from: V, to: R, edge_type: escalates_to}
""")
    dag = load_dag(wf)
    # verifies counts as data flow; escalates_to does not.
    assert dag.children["W"] == ("V",)
    assert dag.children["V"] == ()
    assert dag.parents["R"] == ()


def test_load_dag_ignores_unknown_nodes_and_dedups(tmp_path: Path) -> None:
    wf = _write(tmp_path, """
nodes:
  - {name: A}
  - {name: B}
edges:
  - {from: A, to: B, edge_type: depends_on}
  - {from: A, to: B, edge_type: supplies_context_to}
  - {from: A, to: ghost, edge_type: depends_on}
  - {from: ghost, to: B, edge_type: depends_on}
""")
    dag = load_dag(wf)
    assert dag.children["A"] == ("B",)  # deduped, ghost edge dropped
    assert dag.parents["B"] == ("A",)
    assert "ghost" not in dag.node_ids


def test_load_dag_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_dag(str(tmp_path / "nope.yaml"))


def test_load_dag_cycle_raises(tmp_path: Path) -> None:
    wf = _write(tmp_path, """
nodes:
  - {name: A}
  - {name: B}
edges:
  - {from: A, to: B, edge_type: depends_on}
  - {from: B, to: A, edge_type: depends_on}
""")
    with pytest.raises(ValueError, match="cycle"):
        load_dag(wf)


def test_load_dag_nodes_not_a_list_raises(tmp_path: Path) -> None:
    wf = _write(tmp_path, "nodes: notalist\n")
    with pytest.raises(ValueError, match="nodes must be a list"):
        load_dag(wf)


# ─── descendants ─────────────────────────────────────────────────────────────

def test_descendants_includes_root_and_transitives(tmp_path: Path) -> None:
    wf = _write(tmp_path, """
nodes:
  - {name: A}
  - {name: B}
  - {name: C}
  - {name: X}
edges:
  - {from: A, to: B, edge_type: depends_on}
  - {from: B, to: C, edge_type: depends_on}
""")
    dag = load_dag(wf)
    assert dag.descendants("A") == {"A", "B", "C"}
    assert dag.descendants("B") == {"B", "C"}
    assert dag.descendants("C") == {"C"}
    assert dag.descendants("X") == {"X"}


def test_descendants_unknown_root_is_singleton() -> None:
    dag = DAG(node_ids=("A",), parents={"A": ()}, children={"A": ()}, topo=("A",))
    assert dag.descendants("nope") == {"nope"}


# ─── planner re-export parity ────────────────────────────────────────────────

def test_planner_reexports_dag_module() -> None:
    """The SRP split must keep ``mini_ork.recovery.planner`` import-compatible:
    the same DAG class and load_dag function object are re-exported."""
    assert rp.DAG is DAG
    assert rp.load_dag is load_dag
    for name in ("DAG", "RecoveryPlan", "RECOVERY_STRATEGIES", "load_dag",
                 "compute_recovery", "plan_recovery", "format_status", "main"):
        assert name in rp.__all__
        assert getattr(rp, name) is not None
