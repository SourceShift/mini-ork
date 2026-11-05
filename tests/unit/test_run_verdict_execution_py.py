"""The run-level verdict belongs to the execution that just ran, not the first one.

`_emit_run_verdict` used to treat ``verdict.json`` as write-once per RUN DIR. A
run that executes twice (the RSI cycle redispatches its recipe after a candidate
displacement) therefore kept the FIRST pass's record: cycle
``rsi-c1793838883`` wrote ``pass/failed_nodes=0`` at 01:45, failed two nodes at
01:55, and left the file untouched — the run ended ``error`` with no artifact
naming the failing node, while ``execute.log`` said "2 node(s) failed".

The rule is write-once per EXECUTION: a nested lifecycle that inherited the run
dir still cannot replace the live run's outcome (it carries its own token, and
the outermost execution writes last), but a later execution of the same run
corrects it. The token lives in a sidecar so the four-key verdict bytes, which
tests 8 / 11 pin, are unchanged.
"""
from __future__ import annotations

import json

from mini_ork.cli import execute as ex


PINNED_KEYS = ("verdict", "failed_nodes", "dispatched", "source")


def _verdict(run_dir):
    # The level-vector stamp (MO_LEVEL_VECTOR=1) appends report keys; the four
    # pinned pairs are what this test is about either way.
    record = json.loads((run_dir / "verdict.json").read_text())
    return {key: record[key] for key in PINNED_KEYS}


def test_a_second_execution_corrects_the_first_pass_record(tmp_path) -> None:
    ex._emit_run_verdict(str(tmp_path), fail_count=0, dispatched=9, execution="pass-1")
    assert _verdict(tmp_path) == {
        "verdict": "pass", "failed_nodes": 0, "dispatched": 9,
        "source": "execute@run-level",
    }

    ex._emit_run_verdict(str(tmp_path), fail_count=2, dispatched=9, execution="pass-2")

    assert _verdict(tmp_path) == {
        "verdict": "fail", "failed_nodes": 2, "dispatched": 9,
        "source": "execute@run-level",
    }
    assert (tmp_path / "verdict.json.execution").read_text().strip() == "pass-2"


def test_the_same_execution_still_cannot_overwrite_its_own_record(tmp_path) -> None:
    ex._emit_run_verdict(str(tmp_path), fail_count=2, dispatched=9, execution="only")
    ex._emit_run_verdict(str(tmp_path), fail_count=0, dispatched=9, execution="only")

    assert _verdict(tmp_path)["verdict"] == "fail"


def test_a_nested_lifecycle_writing_first_loses_to_the_outer_execution(tmp_path) -> None:
    # The child inherits the run dir and records its own outcome; the outer run
    # finishes afterwards and owns the final word.
    ex._emit_run_verdict(str(tmp_path), fail_count=3, dispatched=4, execution="nested")
    ex._emit_run_verdict(str(tmp_path), fail_count=0, dispatched=9, execution="outer")

    assert _verdict(tmp_path) == {
        "verdict": "pass", "failed_nodes": 0, "dispatched": 9,
        "source": "execute@run-level",
    }


def test_recovery_refreshes_the_dead_attempts_record(tmp_path) -> None:
    ex._emit_run_verdict(str(tmp_path), fail_count=2, dispatched=9, execution="dead")
    ex._emit_run_verdict(str(tmp_path), fail_count=0, dispatched=9, execution="recovery",
                         recovery=True)

    assert _verdict(tmp_path)["verdict"] == "pass"


def test_without_a_token_the_old_write_once_behaviour_is_kept(tmp_path) -> None:
    # Callers that do not pass an execution (a rehearsal path, an embedder) must
    # not gain a new overwrite semantics by accident.
    ex._emit_run_verdict(str(tmp_path), fail_count=2, dispatched=9)
    ex._emit_run_verdict(str(tmp_path), fail_count=0, dispatched=9)

    assert _verdict(tmp_path)["verdict"] == "fail"
    assert not (tmp_path / "verdict.json.execution").exists()
