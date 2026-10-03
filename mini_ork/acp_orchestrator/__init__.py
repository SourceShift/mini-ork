"""Engine-agnostic orchestrator harness for Zed's conversational agent.

The orchestrator is a thin layer that picks a Claude-CLI-compatible lane,
builds the argv, runs one conversational turn, streams its stream-json events
to a callback, and keeps the conversation resumable via the claude CLI
``session_id``. The package is consumed by the future ``mini_ork.acp.agent``
slice (later work) and by ``bin/zed``; nothing here imports from
``mini_ork.acp`` so the dependency direction stays one-way.
"""

from __future__ import annotations

from .config import default_lane, orchestrator_lanes
from .harness import TurnResult, build_command, run_turn

__all__ = ["TurnResult", "default_lane", "orchestrator_lanes", "run_turn", "build_command"]