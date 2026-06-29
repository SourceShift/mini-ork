from __future__ import annotations

import json
from pathlib import Path

from traceotter.adapters import parse_file
from traceotter.pipeline import run_pipeline


def test_parse_codex_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "rollout-2026-01-01.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_context", "payload": {"cwd": "/repo/researcher"}},
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"role": "user", "content": [{"type": "input_text", "text": "Fix reader bug"}]},
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "response_item",
            "payload": {"role": "assistant", "content": [{"type": "output_text", "text": "I will inspect files and run tests."}]},
        },
        {
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "response_item",
            "payload": {"role": "assistant", "content": [{"type": "output_text", "text": "pytest tests/test_reader.py passed"}]},
        },
        {
            "timestamp": "2026-01-01T00:00:04Z",
            "type": "response_item",
            "payload": {"role": "assistant", "content": [{"type": "output_text", "text": "final: fixed"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    episode = parse_file(path)

    assert episode is not None
    assert episode.source == "codex_jsonl"
    assert episode.repo == "researcher"
    assert episode.user_goal == "Fix reader bug"
    assert episode.outcome.status == "completed"
    assert episode.outcome.tests_run


def test_pipeline_exports_llamafactory(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "rollout-demo.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_context", "payload": {"cwd": "/repo/researcher"}},
                {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item", "payload": {"role": "user", "content": "Fix bug using a worktree"}},
                {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item", "payload": {"role": "assistant", "content": "Created worktree and ran pytest tests/test_bug.py"}},
                {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item", "payload": {"role": "assistant", "content": "final: tests passed"}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = run_pipeline([root], tmp_path / "out")

    assert report["episodes"] == 1
    assert Path(report["episodePath"]).exists()
    assert Path(report["skillPath"]).exists()
    assert Path(report["llamafactory"]["dataset"]).exists()
    dataset = json.loads(Path(report["llamafactory"]["dataset"]).read_text(encoding="utf-8"))
    config = Path(report["llamafactory"]["config"]).read_text(encoding="utf-8")
    assert dataset
    assert dataset[0]["instruction"].startswith("Given this coding-agent task")
    assert config.startswith("stage: sft\n")
    assert "model_name_or_path: Qwen/Qwen2.5-Coder-1.5B-Instruct\n" in config
