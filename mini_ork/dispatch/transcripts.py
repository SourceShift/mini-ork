"""Native transcript serialization for executable provider sidecars."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _bounded_text(path: Path, max_bytes: int) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")[: max_bytes + 1]
    truncated = len(text.encode("utf-8", errors="replace")) > max_bytes
    if truncated:
        text = text[: max(200, max_bytes // 4)] + "\n...[truncated]"
    return text, truncated


def write_exec_transcript(out_file: str | os.PathLike[str], model: str = "unknown") -> Path | None:
    """Write ``<out>.transcript.json`` from optional ``.turns.jsonl`` sidecar.

    Existing transcripts are never overwritten. Invalid/empty sidecars use the
    plain-text fallback, matching the legacy executable-lane contract.
    """
    output = Path(out_file)
    transcript = output.with_name(output.name + ".transcript.json")
    if not output.is_file() or transcript.exists():
        return transcript if transcript.exists() else None
    max_bytes = int(os.environ.get("MO_MAX_TRANSCRIPT_BYTES", "1048576"))
    turns: list[dict[str, Any]] = []
    total_in = total_out = 0
    sidecar = output.with_name(output.name + ".turns.jsonl")
    if sidecar.is_file() and sidecar.stat().st_size:
        for line in sidecar.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            t_in, t_out = int(raw.get("input_tokens") or 0), int(raw.get("output_tokens") or 0)
            total_in += t_in
            total_out += t_out
            turns.append({
                "turn_index": len(turns), "model": raw.get("model") or model,
                "input_tokens": t_in, "output_tokens": t_out,
                "text": raw.get("text") or "", "tool_uses": raw.get("tool_uses") or [],
                "cache_read_input_tokens": int(raw.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(raw.get("cache_creation_input_tokens") or 0),
                "stop_reason": raw.get("stop_reason"), "session_id": raw.get("session_id"),
            })
    text, truncated = _bounded_text(output, max_bytes)
    if turns:
        if text and not turns[-1]["text"]:
            turns[-1]["text"] = text
        payload: dict[str, Any] = {"turns": turns, "totals": {"input_tokens": total_in, "output_tokens": total_out}}
    else:
        payload = {"turns": [{"turn_index": 0, "model": model, "input_tokens": 0,
                              "output_tokens": 0, "text": text, "tool_uses": [],
                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                              "stop_reason": None, "session_id": None}], "fallback": "text-output"}
    if truncated:
        payload["truncated"] = True
    transcript.write_text(json.dumps(payload), encoding="utf-8")
    return transcript
