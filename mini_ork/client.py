"""Local runtime client for embedding mini-ork from Python."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Iterable

from .types import ProviderPolicy, RunEvent, RunRequest, RunResult, SpawnRequest, SpawnResult


class MiniOrkError(RuntimeError):
    """Raised when the Python facade cannot invoke the local mini-ork runtime."""


class MiniOrk:
    """Python facade over the local mini-ork CLI runtime.

    The client is deliberately transparent: every result includes the exact
    command, working directory, captured output, parsed run metadata, and the
    `.mini-ork` home used by the run.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        home: Path | str | None = None,
        db: Path | str | None = None,
    ) -> None:
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve()
        self.home = Path(home).resolve() if home else None
        self.db = Path(db).resolve() if db else None

    def run(self, request: RunRequest) -> RunResult:
        cwd = Path(request.cwd or Path.cwd()).resolve()
        kickoff = Path(request.kickoff)
        if not kickoff.is_absolute():
            kickoff = (cwd / kickoff).resolve()
        if not kickoff.exists():
            raise MiniOrkError(f"kickoff not found: {kickoff}")

        command = [str(self.root / "bin" / "mini-ork"), "run"]
        if request.recipe:
            command.append(request.recipe)
        command.append(str(kickoff))

        env = self._env(cwd, request)
        init_output = ""
        init_ran = False
        if request.auto_init and not self._is_initialized(Path(env["MINI_ORK_HOME"])):
            init_command = [str(self.root / "bin" / "mini-ork"), "init"]
            init_completed = subprocess.run(
                init_command,
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=request.timeout_seconds,
            )
            init_output = init_completed.stdout
            init_ran = True
            if init_completed.returncode != 0:
                return self._result(
                    init_command,
                    cwd,
                    init_completed.returncode,
                    init_output,
                    Path(env["MINI_ORK_HOME"]),
                    init_ran=init_ran,
                    init_output=init_output,
                )

        if request.provider_policy:
            self.write_provider_policy(request.provider_policy, Path(env["MINI_ORK_HOME"]))

        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=request.timeout_seconds,
        )
        return self._result(
            command,
            cwd,
            completed.returncode,
            completed.stdout,
            Path(env["MINI_ORK_HOME"]),
            init_ran=init_ran,
            init_output=init_output,
        )

    def classify(self, kickoff: Path | str, cwd: Path | str | None = None) -> RunResult:
        run_cwd = Path(cwd or Path.cwd()).resolve()
        command = [str(self.root / "bin" / "mini-ork"), "classify", str(kickoff)]
        completed = subprocess.run(
            command,
            cwd=run_cwd,
            env=self._base_env(run_cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return self._result(command, run_cwd, completed.returncode, completed.stdout, self._home_for(run_cwd))

    def write_provider_policy(self, policy: ProviderPolicy, home: Path | str | None = None) -> Path:
        target_home = Path(home) if home else self._home_for(Path.cwd())
        config_dir = target_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / "agents.yaml"
        lines = ["lanes:"]
        for lane, provider in sorted(policy.lanes.items()):
            lines.append(f"  {lane}: {provider}")
        lines.extend(
            [
                "budget:",
                f"  per_epic_usd: {policy.per_epic_usd:.2f}",
                f"  per_run_usd: {policy.per_run_usd:.2f}",
                f"  daily_cap_usd: {policy.daily_cap_usd:.2f}",
                "",
            ]
        )
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def stream(self, request: RunRequest) -> Iterable[RunEvent]:
        result = self.run(request)
        yield from result.events

    def spawn(self, request: SpawnRequest) -> SpawnResult:
        cwd = Path(request.cwd or Path.cwd()).resolve()
        kickoff = Path(request.kickoff)
        if not kickoff.is_absolute():
            kickoff = (cwd / kickoff).resolve()
        if not kickoff.exists():
            raise MiniOrkError(f"kickoff not found: {kickoff}")

        command = [
            str(self.root / "bin" / "mini-ork"),
            "spawn",
            "--parent-run",
            request.parent_run_id,
            "--kickoff",
            str(kickoff),
            "--authority",
            f"{request.authority_level:.3f}",
        ]
        if request.recipe:
            command.extend(["--recipe", request.recipe])
        if request.child_run_id:
            command.extend(["--child-run", request.child_run_id])
        if request.depth is not None:
            command.extend(["--depth", str(request.depth)])
        if request.allow_child_spawn:
            command.append("--allow-child-spawn")
        if not request.execute:
            command.append("--no-execute")

        env = self._base_env(cwd)
        env["MINI_ORK_DRY_RUN"] = "1" if request.mode == "dry-run" else "0"
        env.update(request.extra_env)

        if request.auto_init and not self._is_initialized(Path(env["MINI_ORK_HOME"])):
            init_command = [str(self.root / "bin" / "mini-ork"), "init"]
            subprocess.run(
                init_command,
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=request.timeout_seconds,
            )

        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=request.timeout_seconds,
        )
        return self._spawn_result(command, cwd, completed.returncode, completed.stdout)

    def _env(self, cwd: Path, request: RunRequest) -> dict[str, str]:
        env = self._base_env(cwd)
        env["MINI_ORK_DRY_RUN"] = "1" if request.mode == "dry-run" else "0"
        env.update(request.extra_env)
        return env

    def _base_env(self, cwd: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["MINI_ORK_ROOT"] = str(self.root)
        env.setdefault("MINI_ORK_NO_COLOR", "1")
        env["MINI_ORK_HOME"] = str(self._home_for(cwd))
        env["MINI_ORK_DB"] = str(self.db or self._home_for(cwd) / "state.db")
        return env

    def _home_for(self, cwd: Path) -> Path:
        return self.home or cwd / ".mini-ork"

    def _is_initialized(self, home: Path) -> bool:
        return (home / "state.db").exists() and (home / "config" / "task_classes").is_dir()

    def _result(
        self,
        command: list[str],
        cwd: Path,
        returncode: int,
        output: str,
        home: Path,
        init_ran: bool = False,
        init_output: str = "",
    ) -> RunResult:
        events = tuple(RunEvent(line=line) for line in output.splitlines())
        plan_raw = _last_value(output, "plan_path=")
        return RunResult(
            returncode=returncode,
            command=tuple(command),
            cwd=cwd,
            output=output,
            events=events,
            run_id=_last_value(output, "run_id="),
            task_class=_last_value(output, "task_class="),
            plan_path=Path(plan_raw) if plan_raw else None,
            verdict=_jsonish_verdict(output),
            retained_home=home,
            init_ran=init_ran,
            init_output=init_output,
        )

    def _spawn_result(
        self,
        command: list[str],
        cwd: Path,
        returncode: int,
        output: str,
    ) -> SpawnResult:
        events = tuple(RunEvent(line=line) for line in output.splitlines())
        workspace_raw = _last_value(output, "child_workspace=")
        kickoff_raw = _last_value(output, "child_kickoff=")
        return SpawnResult(
            returncode=returncode,
            command=tuple(command),
            cwd=cwd,
            output=output,
            events=events,
            parent_run_id=_last_value(output, "parent_run_id="),
            child_run_id=_last_value(output, "child_run_id="),
            spawn_id=_last_value(output, "spawn_id="),
            child_workspace=Path(workspace_raw) if workspace_raw else None,
            child_kickoff=Path(kickoff_raw) if kickoff_raw else None,
            spawn_status=_last_value(output, "spawn_status="),
        )


def _last_value(output: str, prefix: str) -> str:
    value = ""
    for line in output.splitlines():
        if line.startswith(prefix):
            value = line.split("=", 1)[1].strip()
    return value


def _jsonish_verdict(output: str) -> str:
    match = re.findall(r'"verdict"\s*:\s*"([^"]+)"', output)
    return match[-1] if match else ""
