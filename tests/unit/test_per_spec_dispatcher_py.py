"""Unit tests for the per_spec_dispatcher recipe lib.

Covers the two behaviors added in the dispatch-waves patch:

- Spec-level ``Depends on:`` inheritance: a spec whose source file carries
  the header depends on EVERY deliverable of the named specs, so a
  deliverable with an empty ``depends_on`` (survey-synthesis D0,
  run-1791763809) can no longer be topologically scheduled ahead of the
  specs it needs.
- ``MO_DISPATCH_WAVES``: default 1 keeps the serial dispatch order; >= 2
  launches level-mates concurrently through the stub child binary.

The child is a stub shell script (``MINI_ORK_PER_SPEC_MINI_ORK_BIN``) that
echoes the ``child_run_id=`` / ``child_run_dir=`` key-values, writes a
verdict.json into its run dir, and appends start/end timestamps to a shared
overlap log so serial vs concurrent execution is observable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCHER = ROOT / "recipes" / "spec-driven-delivery" / "lib" / "per_spec_dispatcher.py"


def _load_dispatcher():
    spec = importlib.util.spec_from_file_location("per_spec_dispatcher", DISPATCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


psd = _load_dispatcher()


# ── fixtures ──────────────────────────────────────────────────────────────


def _spec_file(base: Path, spec_id: str, depends_header: str | None = None) -> Path:
    path = base / "specs" / f"{spec_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"Depends on: {depends_header}\n\n" if depends_header else ""
    path.write_text(f"# {spec_id}\n\n{header}Body of {spec_id}.\n", encoding="utf-8")
    return path


def _card(
    spec_id: str,
    deliverables: list[dict],
    base: Path,
    depends_header: str | None = None,
) -> dict:
    source = _spec_file(base, spec_id, depends_header)
    return {
        "spec_id": spec_id,
        "source_path": str(source),
        "source_hash": f"sha-{spec_id}",
        "deliverables": deliverables,
        "acceptance": [
            {"id": f"AC{i}", "clause_refs": ["F1"], "text": f"acceptance {i}"}
            for i in range(1, 5)
        ],
        "clauses": {"functional": [{"id": "F1", "text": "clause one"}]},
    }


def _deliverable(did: str, deps: list[str] | None = None, refs: list[str] | None = None):
    return {
        "id": did,
        "title": f"deliverable {did}",
        "depends_on": deps or [],
        "acceptance_refs": refs or ["AC1"],
    }


def _gates_doc() -> dict:
    return {
        "probes": [
            {"id": "P1", "acceptance_ref": "AC1", "command": "true"},
        ]
    }


def _build_run_dir(tmp_path: Path) -> Path:
    """A minimal SDD run dir: spec-index + two lens cards + a synthesis card
    whose D0 has NO own depends_on (the run-1791763809 shape) but whose
    source spec carries a spec-level Depends on: header."""
    run_dir = tmp_path / "run"
    (run_dir / "spec-cards").mkdir(parents=True)
    (run_dir / "gates").mkdir(parents=True)

    cards = {
        "lens-a": _card("lens-a", [_deliverable("D1", deps=["D0"]), _deliverable("D0")], tmp_path),
        "lens-b": _card("lens-b", [_deliverable("D1", deps=["D0"]), _deliverable("D0")], tmp_path),
        # D0 dep-free at the card level; the guarantee must come from the
        # spec-level header naming both lenses.
        "synthesis": _card(
            "synthesis",
            [_deliverable("D0"), _deliverable("D1", deps=["D0"])],
            tmp_path,
            depends_header="lens-a, lens-b",
        ),
    }
    # Synthesis FIRST in the spec index — run-1791763809's actual order,
    # which is what let dep-free D0 win the ready queue ahead of the lenses.
    (run_dir / "spec-index.json").write_text(
        json.dumps({"specs": {"synthesis": {}, "lens-a": {}, "lens-b": {}}}), encoding="utf-8"
    )
    for spec_id, card in cards.items():
        (run_dir / "spec-cards" / f"{spec_id}.json").write_text(
            json.dumps(card), encoding="utf-8"
        )
        (run_dir / "gates" / f"{spec_id}.json").write_text(
            json.dumps(_gates_doc()), encoding="utf-8"
        )
    return run_dir


STUB_CHILD = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Stub mini-ork child: echo dispatcher key-values, write a verdict, and
    # record start/end timestamps so overlap is observable. Armed with
    # MO_STUB_FAIL_LENS_A_D1=1 it fails ONLY lens-a's D1 deliverable.
    kickoff="$(basename "$3")"
    run_id="${MINI_ORK_RUN_ID:?stub needs MINI_ORK_RUN_ID}"
    run_dir="${MINI_ORK_HOME}/runs/${run_id}"
    mkdir -p "$run_dir"
    ts() { python3 -c 'import time; print(f"{time.time():.6f}")'; }
    echo "start ${run_id} $(ts)" >> "${MO_OVERLAP_LOG:?}"
    sleep "${MO_STUB_SLEEP:-0.2}"
    if [[ "$kickoff" == "lens-a--D1.md" && "${MO_STUB_FAIL_LENS_A_D1:-0}" == "1" ]]; then
        pass=false
    else
        pass=true
    fi
    printf '{"pass": %s}\\n' "$pass" > "$run_dir/verdict.json"
    echo "child_run_id=${run_id}"
    echo "child_run_dir=${run_dir}"
    echo "end ${run_id} $(ts)" >> "$MO_OVERLAP_LOG"
    if [[ "$pass" == "false" ]]; then
        exit 1
    fi
    """
)


def _write_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "stub-mini-ork"
    stub.write_text(STUB_CHILD, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def _dispatch_env(run_dir: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_ROOT": str(ROOT),
        "MINI_ORK_HOME": str(tmp_path / "home"),
        "MINI_ORK_RUN_ID": "parent-test",
        "MINI_ORK_PER_SPEC_MINI_ORK_BIN": str(_write_stub(tmp_path)),
        "MO_OVERLAP_LOG": str(tmp_path / "overlap.log"),
        "MO_CHILD_VERDICT_TIMEOUT_SEC": "30",
        "MO_CHILD_VERDICT_POLL_SEC": "0.2",
        "MO_STUB_SLEEP": "0.2",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    env.update(extra)
    return env


def _run_dispatch(monkeypatch, env: dict[str, str]) -> int:
    monkeypatch.setattr(os, "environ", env)
    return psd.dispatch()


def _records(run_dir: Path) -> dict[tuple[str, str], dict]:
    raw = json.loads((run_dir / "dispatch-results.json").read_text(encoding="utf-8"))
    return {(r["spec_id"], r["deliverable_id"]): r for r in raw["deliverables"]}


def _spans(tmp_path: Path) -> dict[str, tuple[float, float]]:
    """run_id -> (start_ts, end_ts) from the stub's overlap log."""
    spans: dict[str, list[float]] = {}
    for line in (tmp_path / "overlap.log").read_text().splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        phase, run_id, ts = parts[0], parts[1], float(parts[2])
        bucket = spans.setdefault(run_id, [ts, ts])
        if phase == "start":
            bucket[0] = ts
        else:
            bucket[1] = ts
    return {run_id: (span[0], span[1]) for run_id, span in spans.items()}


# ── spec-level dependency inheritance ─────────────────────────────────────


def test_spec_level_dep_specs_parses_header_and_skips_unknowns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cards = {
        "lens-a": _card("lens-a", [_deliverable("D0")], tmp_path),
        "lens-b": _card("lens-b", [_deliverable("D0")], tmp_path),
        "synthesis": _card(
            "synthesis", [_deliverable("D0")], tmp_path,
            depends_header="lens-a, not-a-card",
        ),
    }
    got = psd._spec_level_dep_specs(cards["synthesis"], cards)
    assert got == ["lens-a"]  # unknown name skipped, known name kept, order stable


def test_spec_level_dep_specs_header_variants(tmp_path):
    cards = {
        "lens-a": _card("lens-a", [_deliverable("D0")], tmp_path),
        "synthesis": _card("synthesis", [_deliverable("D0")], tmp_path),
    }
    src = tmp_path / "specs" / "synthesis.md"
    src.write_text("# synthesis\n\n## Depends on: `lens-a`\n\nbody\n", encoding="utf-8")
    assert psd._spec_level_dep_specs(cards["synthesis"], cards) == ["lens-a"]


def test_spec_level_dep_specs_no_source_file(tmp_path):
    card = {"spec_id": "x", "source_path": str(tmp_path / "missing.md"), "deliverables": []}
    assert psd._spec_level_dep_specs(card, {"x": card}) == []


def test_effective_deps_unions_own_and_spec_level_fanout(tmp_path):
    cards = {
        "lens-a": _card("lens-a", [_deliverable("D0"), _deliverable("D1")], tmp_path),
        "synthesis": _card(
            "synthesis", [_deliverable("D0"), _deliverable("D1", deps=["D0"])], tmp_path
        ),
    }
    spec_dep_specs = {"synthesis": ["lens-a"], "lens-a": []}
    # synthesis D0: no own deps + fan-out of lens-a's every deliverable.
    pairs = psd._effective_deps("synthesis", cards["synthesis"]["deliverables"][0],
                                cards, spec_dep_specs)
    assert set(pairs) == {("lens-a", "D0"), ("lens-a", "D1")}
    # synthesis D1: own dep survives and de-dupes against the fan-out.
    pairs = psd._effective_deps("synthesis", cards["synthesis"]["deliverables"][1],
                                cards, spec_dep_specs)
    assert set(pairs) == {("lens-a", "D0"), ("lens-a", "D1"), ("synthesis", "D0")}
    assert len(pairs) == 3  # de-duped


def test_topological_order_honors_spec_level_deps(tmp_path):
    """The regression: dep-free synthesis D0 must land AFTER the lenses."""
    run_dir = _build_run_dir(tmp_path)
    cards = psd._load_cards(run_dir)
    spec_index = psd._load_spec_index(run_dir)
    spec_dep_specs = {
        sid: psd._spec_level_dep_specs(card, cards) for sid, card in cards.items()
    }
    assert spec_dep_specs["synthesis"] == ["lens-a", "lens-b"]

    ordered = psd._topological_order(cards, list(spec_index), spec_dep_specs)
    keys = [(s, d) for s, d, _ in ordered]
    synth_pos = keys.index(("synthesis", "D0"))
    for lens in (("lens-a", "D0"), ("lens-a", "D1"), ("lens-b", "D0"), ("lens-b", "D1")):
        assert keys.index(lens) < synth_pos

    # Without the spec-level map (legacy call shape) synthesis D0 can land
    # before the lens D1s — the ordering gap this patch closes at the
    # dispatcher (compile-order is no longer trusted for the guarantee).
    legacy = psd._topological_order(cards, list(spec_index))
    legacy_keys = [(s, d) for s, d, _ in legacy]
    assert legacy_keys.index(("synthesis", "D0")) < legacy_keys.index(("lens-a", "D1"))


# ── wave levels ───────────────────────────────────────────────────────────


def test_wave_levels_groups_by_longest_path(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    cards = psd._load_cards(run_dir)
    spec_index = psd._load_spec_index(run_dir)
    spec_dep_specs = {
        sid: psd._spec_level_dep_specs(card, cards) for sid, card in cards.items()
    }
    ordered = psd._topological_order(cards, list(spec_index), spec_dep_specs)
    levels = psd._wave_levels(ordered, cards, spec_dep_specs)
    # Level 0: both lens D0s. Level 1: both lens D1s. Level 2: synthesis D0
    # (spec-level fan-out satisfied at level 1) then its own D1 at level 3.
    assert [[(s, d) for s, d, _ in level] for level in levels] == [
        [("lens-a", "D0"), ("lens-b", "D0")],
        [("lens-a", "D1"), ("lens-b", "D1")],
        [("synthesis", "D0")],
        [("synthesis", "D1")],
    ]


# ── dispatch() end-to-end through the stub child ──────────────────────────


def test_dispatch_serial_default_delivers_everything(tmp_path, monkeypatch):
    run_dir = _build_run_dir(tmp_path)
    env = _dispatch_env(run_dir, tmp_path)
    assert _run_dispatch(monkeypatch, env) == 0
    recs = _records(run_dir)
    assert len(recs) == 6
    assert all(r["status"] == "delivered" for r in recs.values())

    # Serial: no two children ever overlapped (each start comes after the
    # previous end).
    spans = _spans(tmp_path)
    timeline = sorted(
        (ts, phase) for start, end in spans.values()
        for phase, ts in (("start", start), ("end", end))
    )
    running = 0
    for _, phase in timeline:
        running += 1 if phase == "start" else -1
        assert running <= 1, "serial dispatch overlapped two children"


def test_dispatch_waves_runs_level_mates_concurrently(tmp_path, monkeypatch):
    run_dir = _build_run_dir(tmp_path)
    env = _dispatch_env(run_dir, tmp_path, MO_DISPATCH_WAVES="2")
    assert _run_dispatch(monkeypatch, env) == 0
    recs = _records(run_dir)
    assert all(r["status"] == "delivered" for r in recs.values())

    # Waves=2: the two level-0 lens children MUST overlap (each sleeps
    # 0.2s; serial would serialize them).
    spans = _spans(tmp_path)
    a = spans["parent-test--lens-a--D0"]
    b = spans["parent-test--lens-b--D0"]
    assert b[0] < a[1] or a[0] < b[1], "level-mates did not run concurrently"

    # Dependency semantics hold: synthesis D0 started only after all four
    # lens deliverables ended.
    synth_start = spans["parent-test--synthesis--D0"][0]
    for did in ("lens-a--D0", "lens-a--D1", "lens-b--D0", "lens-b--D1"):
        assert spans[f"parent-test--{did}"][1] < synth_start


def test_dispatch_waves_fail_fast_stops_launching(tmp_path, monkeypatch):
    run_dir = _build_run_dir(tmp_path)
    env = _dispatch_env(
        run_dir, tmp_path,
        MO_DISPATCH_WAVES="2", MO_STUB_FAIL_LENS_A_D1="1",
        MO_PER_SPEC_DISPATCH_FAIL_FAST="1",
    )
    rc = _run_dispatch(monkeypatch, env)
    assert rc == 1
    recs = _records(run_dir)
    assert recs[("lens-a", "D1")]["status"] == "failed"
    # Level 1 chunk (lens-a D1 + lens-b D1) failed → fail_fast aborts before
    # synthesis D0 ever launches: no kickoff, and — matching the serial
    # fail_fast semantics — no record row at all (the aggregator recomputes
    # from child run dirs).
    assert not (run_dir / "child-kickoffs" / "synthesis--D0.md").exists()
    assert ("synthesis", "D0") not in recs


def test_dispatch_waves_dependency_failure_marks_dependents_pending(tmp_path, monkeypatch):
    run_dir = _build_run_dir(tmp_path)
    env = _dispatch_env(  # no fail_fast: run the whole graph to the end
        run_dir, tmp_path, MO_DISPATCH_WAVES="2", MO_STUB_FAIL_LENS_A_D1="1"
    )
    rc = _run_dispatch(monkeypatch, env)
    assert rc == 1
    recs = _records(run_dir)
    assert recs[("lens-a", "D1")]["status"] == "failed"
    assert recs[("lens-b", "D1")]["status"] == "delivered"
    # Spec-level fan-out: lens-a D1 failed → synthesis D0 is pending, and
    # so is synthesis D1 (its own dep D0 never delivered).
    assert recs[("synthesis", "D0")]["status"] == "pending"
    assert recs[("synthesis", "D1")]["status"] == "pending"
    assert not (run_dir / "child-kickoffs" / "synthesis--D0.md").exists()


def test_dispatch_rejects_malformed_waves_value(tmp_path, monkeypatch):
    run_dir = _build_run_dir(tmp_path)
    env = _dispatch_env(run_dir, tmp_path, MO_DISPATCH_WAVES="two")
    assert _run_dispatch(monkeypatch, env) == 1
    assert not (run_dir / "dispatch-results.json").exists()


def test_dispatch_waves_zero_is_rejected(tmp_path, monkeypatch):
    run_dir = _build_run_dir(tmp_path)
    env = _dispatch_env(run_dir, tmp_path, MO_DISPATCH_WAVES="0")
    assert _run_dispatch(monkeypatch, env) == 1


# ── resume reuses a delivered child ───────────────────────────────────────


def test_dispatch_reuses_a_delivered_child_instead_of_relaunching(tmp_path, monkeypatch):
    """A resume must not re-implement a deliverable whose child already published.

    ``_launch_child`` derives ``child_run_id`` as ``{parent}--{spec}--{deliverable}``,
    so a re-dispatched run addresses the *same* child directory. run-1791790859-83222
    resumed with its D1/D3 children already approved and every other deliverable
    pending; without this guard the dispatcher re-ran the two finished children
    (paying for the work twice) and only then dispatched the remaining four.
    """
    run_dir = _build_run_dir(tmp_path)
    env = _dispatch_env(run_dir, tmp_path)
    published = Path(env["MINI_ORK_HOME"]) / "runs" / "parent-test--lens-a--D0"
    published.mkdir(parents=True)
    (published / "panel-verdict.json").write_text(
        json.dumps({"verdict": "APPROVE"}), encoding="utf-8"
    )

    assert _run_dispatch(monkeypatch, env) == 0

    record = _records(run_dir)[("lens-a", "D0")]
    assert record["status"] == "delivered"
    assert record["child_run_id"] == "parent-test--lens-a--D0"
    assert record["verdict_path"].endswith("panel-verdict.json")
    assert "parent-test--lens-a--D0" not in _spans(tmp_path), (
        "an already-published child must not be relaunched"
    )


def test_dispatch_relaunches_a_child_whose_verdict_did_not_pass(tmp_path, monkeypatch):
    """Only a pass is reusable; a failed or unreadable child is dispatched again."""
    run_dir = _build_run_dir(tmp_path)
    env = _dispatch_env(run_dir, tmp_path)
    failed = Path(env["MINI_ORK_HOME"]) / "runs" / "parent-test--lens-a--D0"
    failed.mkdir(parents=True)
    (failed / "panel-verdict.json").write_text(
        json.dumps({"verdict": "ESCALATE"}), encoding="utf-8"
    )

    assert _run_dispatch(monkeypatch, env) == 0

    assert _records(run_dir)[("lens-a", "D0")]["status"] == "delivered"
    assert "parent-test--lens-a--D0" in _spans(tmp_path), "a non-passing child must be relaunched"


# ── a failed child under auto-repair ──────────────────────────────────────


def _failed_child(tmp_path: Path, repair_state: str | None):
    import subprocess

    child_dir = tmp_path / "home" / "runs" / "parent--S1--D1"
    child_dir.mkdir(parents=True)
    (child_dir / "verdict.json").write_text(json.dumps({"pass": False}), encoding="utf-8")
    if repair_state is not None:
        (child_dir / "repair.json").write_text(json.dumps({"state": repair_state}),
                                               encoding="utf-8")
    record = psd._record_for("S1", "D1", child_kickoff=None, status=psd.STATUS_PENDING,
                             child_run_id="parent--S1--D1", child_run_dir=str(child_dir))
    proc = subprocess.Popen(["sh", "-c", "exit 1"])
    return child_dir, record, proc


def _settle_repair_later(child_dir: Path, state: str, passed: bool) -> None:
    import threading

    def settle() -> None:
        (child_dir / "verdict.json").write_text(json.dumps({"pass": passed}), encoding="utf-8")
        (child_dir / "repair.json").write_text(json.dumps({"state": state}), encoding="utf-8")

    threading.Timer(0.3, settle).start()


def test_a_child_repaired_by_auto_repair_is_delivered(tmp_path, monkeypatch):
    """2026-10-15 m3: m3b failed, auto-repair took it, and the dispatcher at once
    sent m3d into the same worktree — two writers in one tree. It now waits, and
    a repair that succeeds records the deliverable delivered, not failed."""
    monkeypatch.setenv("MO_SDD_REPAIR_POLL_SEC", "0.05")
    child_dir, record, proc = _failed_child(tmp_path, "repairing")
    _settle_repair_later(child_dir, "repaired", passed=True)

    out = psd._collect_child(record, proc, None, tmp_path / "child.log", tmp_path / "home")

    assert out["status"] == psd.STATUS_DELIVERED
    assert out.get("repaired") is True
    assert "error" not in out


def test_a_child_whose_repair_gives_up_stays_failed_after_the_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_SDD_REPAIR_POLL_SEC", "0.05")
    child_dir, record, proc = _failed_child(tmp_path, "repairing")
    _settle_repair_later(child_dir, "gave_up", passed=False)

    out = psd._collect_child(record, proc, None, tmp_path / "child.log", tmp_path / "home")

    assert out["status"] == psd.STATUS_FAILED
    assert psd._repair_state(child_dir) == "gave_up"


def test_a_failed_child_without_a_repair_is_not_waited_on(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setenv("MO_SDD_REPAIR_POLL_SEC", "5")
    _, record, proc = _failed_child(tmp_path, None)

    started = _time.monotonic()
    out = psd._collect_child(record, proc, None, tmp_path / "child.log", tmp_path / "home")

    assert out["status"] == psd.STATUS_FAILED
    assert _time.monotonic() - started < 2


def test_a_repair_that_outlives_the_wait_is_recorded_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_SDD_REPAIR_WAIT_SEC", "0")
    monkeypatch.setenv("MO_SDD_REPAIR_POLL_SEC", "0.05")
    _, record, proc = _failed_child(tmp_path, "repairing")
    log_path = tmp_path / "child.log"

    out = psd._collect_child(record, proc, None, log_path, tmp_path / "home")

    assert out["status"] == psd.STATUS_FAILED
    assert "still running after 0s" in log_path.read_text(encoding="utf-8")
