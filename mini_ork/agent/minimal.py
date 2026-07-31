"""Linear-history, stateless-action MinimalAgent.

Each turn: dispatch → parse assistant reply for either a completion sentinel
or a single fenced bash block → run the bash → append the result to the
in-memory history. Stops on sentinel or after ``max_turns``; never branches
on ``MO_RUNTIME_BACKEND`` (the runtime seam honors it transparently).

Tool-exec runs on the host via ``mini_ork.runtime.contract.mo_runtime_exec`` by
default. When a ``Workspace`` is supplied (opt-in, resolved from
``MO_SANDBOX_BACKEND``), the bash tool runs *inside that workspace* instead — a
docker container, say — so an agent executes in its own environment while still
reading/writing the shared drive mounted at ``exec_cwd``. Unset backend → no
Workspace → byte-for-byte today's host behavior.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mini_ork.dispatch import DispatchRequest, dispatch_model
from mini_ork.runtime.contract import mo_runtime_exec
from mini_ork.runtime.sandbox import Workspace

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
        workspace: Workspace | None = None,
        exec_cwd: str | None = None,
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
        # When set, bash tool-exec routes through the workspace (e.g. a docker
        # container) instead of the host; ``exec_cwd`` is where commands run
        # inside it (the in-container mount path for docker). ``None`` keeps
        # today's exact host execution.
        self.workspace = workspace
        self.exec_cwd = exec_cwd or cwd

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
        if self.workspace is not None:
            # Route into the workspace (docker container, …). Workspace.exec
            # returns (rc, output); flip to this method's (output, rc) shape.
            rc, output = self.workspace.exec(
                cmd, cwd=self.exec_cwd, timeout=self.timeout
            )
            return output, rc
        # Native port of the bash runtime contract (WS7): cwd pinned per turn,
        # pgid-kill timeout semantics, merged stdout+stderr — no bash libs.
        return mo_runtime_exec(cmd, cwd=self.cwd, timeout=self.timeout)


def run_minimal(
    task: str,
    *,
    cwd: str,
    max_turns: int = 12,
    model: str | None = None,
    env: Any | None = None,
) -> MinimalAgentResult:
    """Run a MinimalAgent, routing tool-exec through a sandbox if opted in.

    ``MO_SANDBOX_BACKEND`` unset → today's exact host execution (no Workspace is
    ever provisioned). ``local`` → LocalWorkspace parity. ``docker`` → each agent
    runs in its own container bind-mounting the shared drive at ``/workspace``;
    the container is brought up before the run and torn down after (the drive,
    the pet, is never touched here).
    """
    from mini_ork.runtime.agent_workspace import resolve_agent_workspace

    workspace, exec_cwd = resolve_agent_workspace(cwd, env=env)
    agent = MinimalAgent(
        cwd=cwd,
        max_turns=max_turns,
        model=model,
        workspace=workspace,
        exec_cwd=exec_cwd,
    )
    if workspace is None:
        # Unset backend: identical to the original call path — no lifecycle.
        return agent.run(task)
    workspace.up()
    try:
        return agent.run(task)
    finally:
        workspace.down()