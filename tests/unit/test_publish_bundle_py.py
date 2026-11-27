"""Bundle-shape tests for ``mini_ork.publish.bundle``.

A fixture run directory, written the way a real run writes its files, is the
input. The assertions are the contract the control plane validates and the
share page renders: schema name, verdict mapping, DAG edges/status, per-agent
summaries, transcript parsing, and — the one that must never regress — no
secret survives anywhere in the output.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.publish import SCHEMA, collect_bundle, discover_run_dir
from mini_ork.publish.redact import scan_for_secrets_obj

AGENT_LINES = [
    {
        "seq": 1,
        "stream": "sdk",
        "t": 1.0,
        "line": json.dumps({
            "type": "system",
            "subtype": "init",
            "model": "claude-sonnet-5-5",
            "cwd": "/Users/alice/ps/mini-ork",
            "tools": ["Read", "Edit"],
        }),
    },
    {
        "seq": 2,
        "stream": "sdk",
        "t": 2.0,
        "line": json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Reading the file under /Users/alice/ps/mini-ork/x.py"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
                ],
            },
        }),
    },
    {
        "seq": 3,
        "stream": "sdk",
        "t": 3.0,
        "line": json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "is_error": False,
                     "content": [{"type": "text", "text": "file body"}]},
                ],
            },
        }),
    },
    {
        "seq": 4,
        "stream": "sdk",
        "t": 4.0,
        "line": json.dumps({
            "type": "result",
            "total_cost_usd": 0.5,
            "duration_api_ms": 1234,
            "num_turns": 2,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }),
    },
]


def _write(path: Path, value) -> None:
    path.write_text(
        value if isinstance(value, str) else json.dumps(value),
        encoding="utf-8",
    )


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    run = home / "runs" / "run-42-1"
    run.mkdir(parents=True)
    _write(run / "run_profile.json", {
        "recipe": "framework-edit",
        "task_class": "code-fix",
        "user_goal": "fix the parser",
        "success_criteria": ["parser handles empty input"],
        "roots": {"target": str(tmp_path / "not-a-repo")},
        "provider_policy": {"lanes": {"implementer": "sonnet"}},
    })
    _write(run / "plan.json", {
        "objective": "fix the parser under /Users/alice/ps/mini-ork",
        "task_class": "code-fix",
        "decomposition": [
            {
                "id": "implement",
                "node_type": "implementer",
                "description": "apply the patch",
                "depends_on": [],
            },
            {
                "id": "review",
                "node_type": "reviewer",
                "description": "review the patch",
                "depends_on": ["implement"],
            },
        ],
    })
    _write(run / "run-verdict.json", {
        "verdict": "pass",
        "failed_nodes": 1,
        "levels": {"L1": "pass"},
        "levels_ok": True,
        "levels_decision": "publish",
    })
    _write(run / "verifier_test.json", {
        "verdict": "pass",
        "pass": True,
        "checks": [
            {"name": "pytest", "pass": True, "expected": "rc=0", "actual": "rc=0"},
        ],
        "reasons": [],
    })
    _write(run / "verification_manifest.json", {
        "instruments": [{"instrument": "unit-suite", "status": "pass",
                         "knob": "tier2", "detail": "14 tests"}],
    })
    _write(
        run / "kickoff.md",
        "# Fix the parser\n\nSee https://github.com/acme/widget/issues/7\n"
        "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345\n",
    )
    _write(run / "pre-implementer-ref", "deadbeef")
    (run / "agent-implementer.live.jsonl").write_text(
        "\n".join(json.dumps(row) for row in AGENT_LINES) + "\n",
        encoding="utf-8",
    )
    _write(run / "impl-implement.log", "patch applied\n")
    # ``review`` failed: a terminal row names it, so the DAG says so. The
    # ``implementer`` ledger below records a *successful* attempt and must not
    # be read as a failure just because it lives in the same directory.
    (run / "retry-exhausted").mkdir()
    _write(run / "retry-exhausted" / "review.error.json", {
        "node_id": "review",
        "marker": "retry_exhausted",
        "finish_reasons": ["budget"],
    })
    _write(run / "retry-exhausted" / "implementer.attempts.json", [
        {"attempt": 1, "finish_reason": "done", "rc": 0},
    ])
    return run


def test_bundle_shape_and_verdict(run_dir: Path):
    bundle, _, report = collect_bundle(run_dir)
    assert bundle["schema"] == SCHEMA
    assert bundle["run"]["id"] == "run-42-1"
    assert bundle["run"]["title"] == "Fix the parser"
    assert bundle["run"]["verdict"] == "PROVEN"
    assert bundle["run"]["verdict_raw"] == "pass"
    assert bundle["run"]["external_url"] == "https://github.com/acme/widget/issues/7"
    assert bundle["run"]["recipe"] == "framework-edit"
    assert bundle["run"]["success_criteria"] == ["parser handles empty input"]
    assert bundle["producer"]["tool"] == "mini-ork"
    assert report.counts  # the fixture plants a key and a home path


def test_dag_edges_and_status(run_dir: Path):
    bundle, _, _ = collect_bundle(run_dir)
    dag = bundle["dag"]
    by_id = {node["id"]: node for node in dag["nodes"]}
    assert set(by_id) == {"implement", "review"}
    assert dag["edges"] == [{"from": "implement", "to": "review"}]
    assert by_id["implement"]["status"] == "done"
    assert "agent-implementer.live.jsonl" in by_id["implement"]["artifacts"]
    assert "impl-implement.log" in by_id["implement"]["artifacts"]
    assert by_id["review"]["status"] == "failed"
    assert dag["failed_nodes"] == 1


def test_a_successful_attempt_ledger_is_not_a_failure(run_dir: Path):
    """``retry-exhausted/implementer.attempts.json`` says "attempt 1: done"."""
    bundle, _, _ = collect_bundle(run_dir)
    by_id = {node["id"]: node for node in bundle["dag"]["nodes"]}
    assert by_id["implement"]["status"] == "done"


def test_verification_envelopes_and_instruments(run_dir: Path):
    bundle, _, _ = collect_bundle(run_dir)
    verification = bundle["verification"]
    assert verification["manifest"] == [{
        "instrument": "unit-suite", "status": "pass", "knob": "tier2",
        "detail": "14 tests",
    }]
    (envelope,) = verification["verifiers"]
    assert envelope["id"] == "test"
    assert envelope["verdict"] == "pass"
    assert envelope["checks"][0]["name"] == "pytest"


def test_agent_summary_and_transcript(run_dir: Path):
    bundle, transcripts, _ = collect_bundle(run_dir)
    (agent,) = bundle["agents"]
    assert agent["id"] == "implementer"
    assert agent["role"] == "implementer"
    assert agent["model"] == "claude-sonnet-5-5"
    assert agent["turns"] == 1  # one assistant message; the result is not a turn
    assert agent["tool_calls"] == 1
    assert agent["cost_usd"] == 0.5
    assert agent["truncated"] is False

    messages = transcripts["implementer"]
    roles = [m["role"] for m in messages]
    assert roles == ["system", "assistant", "user", "result"]
    assert messages[1]["blocks"][1] == {
        "type": "tool_use", "name": "Read", "input": {"file_path": "x.py"},
    }
    assert messages[2]["blocks"][0]["text"] == "file body"


def test_bundle_is_clean_of_secrets_and_home_paths(run_dir: Path):
    bundle, transcripts, _ = collect_bundle(run_dir)
    assert scan_for_secrets_obj(bundle) == []
    assert scan_for_secrets_obj(transcripts) == []
    objective = bundle["run"]["objective"]
    assert "/Users/alice" not in objective
    assert "ps/mini-ork" in objective
    assert "sk-ant-api03" not in json.dumps(bundle)
    assert "sk-ant-api03" not in json.dumps(transcripts)


def test_no_transcripts_flag_keeps_summaries_only(run_dir: Path):
    bundle, transcripts, _ = collect_bundle(run_dir, include_transcripts=False)
    assert transcripts == {}
    (agent,) = bundle["agents"]
    assert agent["transcript_bytes"] == 0
    assert agent["transcript_messages"] == 0


def test_agent_budget_truncates_with_a_marked_notice(run_dir: Path):
    bundle, transcripts, _ = collect_bundle(run_dir, max_agent_bytes=200)
    (agent,) = bundle["agents"]
    assert agent["truncated"] is True
    assert transcripts["implementer"][-1]["role"] == "notice"
    assert "truncated at 200 bytes" in transcripts["implementer"][-1]["blocks"][0]["text"]


def test_title_override(run_dir: Path):
    bundle, _, _ = collect_bundle(run_dir, title="A better title")
    assert bundle["run"]["title"] == "A better title"


def test_lines_that_are_not_json_are_skipped(run_dir: Path):
    with (run_dir / "agent-implementer.live.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps({"type": "stream_event"}) + "\n")
    _, transcripts, _ = collect_bundle(run_dir)
    assert len(transcripts["implementer"]) == 4


def test_discover_run_dir_by_id_and_path(run_dir: Path):
    home = run_dir.parents[1]
    assert discover_run_dir("run-42-1", home) == run_dir
    assert discover_run_dir(str(run_dir)).name == "run-42-1"
    with pytest.raises(FileNotFoundError):
        discover_run_dir("run-does-not-exist", home)
