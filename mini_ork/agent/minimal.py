"""Linear-history, stateless-action MinimalAgent.

Each turn: dispatch → parse assistant reply for either a completion sentinel
or a single fenced bash block → run the bash via ``mo_runtime_exec`` →
append the result to the in-memory history. Stops on sentinel or after
``max_turns``; never branches on ``MO_RUNTIME_BACKEND`` (the bash seam
honors it transparently).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any

from mini_ork.dispatch import DispatchRequest, dispatch_model

_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
_SENTINEL_RE = re.compile(rf"^{re.escape(_SENTINEL)}\s*(.*)$", re.MULTILINE)
_BASH_FENCE_RE = re.compile(r"```(?:bash|sh)?\s*\n(.*?)\n```", re.DOTALL)
_TIMEOUT_DEFAULT = 60


@dataclass
class MinimalAgentResult:
    messages: list[dict] = field(default_factory=list)
    turns: int = 0
    completed: bool = False
    final_output: str | None = None
    exit_status: str = "ok"


def _extract_text(resp: Any) -> str:
    if isinstance(resp, str):
        return resp
    return getattr(resp, "text", "") or ""


class MinimalAgent:
    def __init__(
        self,
        *,
        cwd: str,
        max_turns: int = 12,
        model: str | None = None,
        system_prompt: str | None = None,
        timeout: int = _TIMEOUT_DEFAULT,
    ) -> None:
        self.cwd = cwd
        self.max_turns = max_turns
        self.model = model
        self.system_prompt = system_prompt or (
            "You are a minimal bash agent. Each turn emit either "
            f"{_SENTINEL} <text> or a single fenced bash code block "
            "(```bash ... ```)."
        )
        self.timeout = timeout

    def run(self, task: str) -> MinimalAgentResult:
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        result = MinimalAgentResult(messages=list(messages))

        for i in range(self.max_turns):
            prompt = self._serialize(messages)
            req = DispatchRequest(prompt=prompt, model=self.model or "stub")
            try:
                resp = dispatch_model(req)
            except Exception as exc:  # dispatch failure surfaces as turn-end
                result.messages = list(messages)
                result.turns = i
                result.exit_status = f"dispatch_error:{type(exc).__name__}"
                return result

            text = _extract_text(resp)
            messages.append({"role": "assistant", "content": text})
            result.turns = i + 1
            result.messages = list(messages)

            m = _SENTINEL_RE.search(text)
            if m:
                result.completed = True
                final = m.group(1).strip()
                result.final_output = final
                result.exit_status = "completed"
                return result

            bm = _BASH_FENCE_RE.search(text)
            if not bm:
                messages.append({
                    "role": "user",
                    "content": (
                        f"error: malformed turn — emit {_SENTINEL} <text> "
                        "or a single fenced bash block."
                    ),
                })
                continue

            cmd = bm.group(1).strip()
            stdout, rc = self._run_bash(cmd)
            messages.append({
                "role": "user",
                "content": f"returncode: {rc}\nstdout:\n{stdout}",
            })

        result.exit_status = "max_turns_exceeded"
        result.messages = list(messages)
        return result

    @staticmethod
    def _serialize(messages: list[dict]) -> str:
        return "\n".join(f"{m['role']}: {m['content']}" for m in messages)

    def _run_bash(self, cmd: str) -> tuple[str, int]:
        root = os.environ.get("MINI_ORK_ROOT", "")
        if not root:
            return ("MINI_ORK_ROOT not set", 2)
        bash_cmd = (
            f'source "{root}/lib/runtime/contract.sh"; '
            f"mo_runtime_exec {shlex.quote(cmd)} "
            f"{shlex.quote(self.cwd)} {self.timeout}"
        )
        proc = subprocess.run(
            ["bash", "-c", bash_cmd],
            capture_output=True,
            text=True,
        )
        return (proc.stdout or ""), proc.returncode


def run_minimal(
    task: str,
    *,
    cwd: str,
    max_turns: int = 12,
    model: str | None = None,
) -> MinimalAgentResult:
    return MinimalAgent(cwd=cwd, max_turns=max_turns, model=model).run(task)