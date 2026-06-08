"""Execution engine for production scenario validation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from mini_ork import MiniOrk, ProviderPolicy, RunRequest, RunResult

from .catalog import expected_task_class
from .model import RunConfig, Scenario, ScenarioResult


def seed_project(root: Path, tmp_project: Path, scenario: Scenario) -> None:
    for rel in scenario.seed_paths:
        src = root / rel
        dest = tmp_project / rel
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        else:
            raise FileNotFoundError(f"scenario seed path missing: {rel}")


class ScenarioExecutor:
    def __init__(self, root: Path, config: RunConfig) -> None:
        self.root = root
        self.config = config

    def run(self, scenario: Scenario) -> ScenarioResult:
        if not scenario.kickoff.exists():
            return ScenarioResult(
                scenario=scenario,
                ok=False,
                error=f"kickoff missing: {scenario.kickoff}",
            )

        tmp_project = Path(tempfile.mkdtemp(prefix="mini-ork-prod-scenario-"))
        output_log = tmp_project / "output.log"
        env = self._env(tmp_project)

        try:
            subprocess.run(["git", "init", "-q"], cwd=tmp_project, env=env, check=True)
            seed_project(self.root, tmp_project, scenario)

            client = MiniOrk(root=self.root, home=tmp_project / ".mini-ork")
            policy = ProviderPolicy.codex_only() if self.config.provider_policy == "codex-only" else None
            result = client.run(
                RunRequest(
                    kickoff=scenario.kickoff,
                    recipe=None if self.config.md_only else scenario.recipe,
                    mode=self.config.mode,
                    cwd=tmp_project,
                    provider_policy=policy,
                    timeout_seconds=self.config.timeout_seconds,
                    extra_env={"MINI_ORK_NO_COLOR": "1"},
                )
            )
            output_log.write_text(result.output, encoding="utf-8")
            return self._result(scenario, result, output_log, tmp_project)
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            output_log.write_text(partial, encoding="utf-8")
            return ScenarioResult(
                scenario=scenario,
                ok=False,
                returncode=None,
                output=partial,
                output_log=output_log,
                tmp_project=tmp_project,
                error=f"timed out after {self.config.timeout_seconds}s",
            )
        except Exception as exc:
            return ScenarioResult(
                scenario=scenario,
                ok=False,
                output_log=output_log,
                tmp_project=tmp_project,
                error=str(exc),
            )
        finally:
            if self.config.mode == "dry-run" and not self.config.keep:
                shutil.rmtree(tmp_project, ignore_errors=True)

    def _env(self, tmp_project: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "MINI_ORK_HOME": str(tmp_project / ".mini-ork"),
                "MINI_ORK_DB": str(tmp_project / ".mini-ork" / "state.db"),
                "MINI_ORK_DRY_RUN": "1" if self.config.mode == "dry-run" else "0",
                "MINI_ORK_NO_COLOR": "1",
                "MINI_ORK_ROOT": str(self.root),
            }
        )
        return env

    def _result(
        self,
        scenario: Scenario,
        run_result: RunResult,
        output_log: Path,
        tmp_project: Path,
    ) -> ScenarioResult:
        expected = expected_task_class(self.root, scenario.recipe)
        actual = run_result.task_class
        has_verification = '"verdict"' in run_result.output or "[ok] verifier_ref" in run_result.output
        ok = run_result.returncode == 0 and actual == expected and has_verification
        return ScenarioResult(
            scenario=scenario,
            ok=ok,
            returncode=run_result.returncode,
            expected_task_class=expected,
            actual_task_class=actual,
            output=run_result.output,
            output_log=output_log,
            tmp_project=tmp_project,
            plan_path=run_result.plan_path,
            error="" if ok else "missing expected task class, rc=0, or verification evidence",
        )
