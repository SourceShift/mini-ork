"""GEPA <-> mini-ork adapter.

Integrates the `gepa` framework (github.com/gepa-ai/gepa, `pip install gepa`)
into mini-ork's existing prompt-optimization scaffolding, upgrading the
`apo-prompt-tune` "propose a variation" step (kickoffs/auto/arx-6-apo-prompt-
mutation.md) from a single synthesizer call to GEPA's reflective Pareto evolution.

The text components GEPA optimizes are a recipe's node prompt files
(`recipes/<recipe>/prompts/{planner,implementer,reviewer}.md`). The score comes
from REAL downstream outcomes in `execution_traces` (status + verifier +
tests_passed) plus the `completion_honesty` signal we wired into the reward
vector. The reflective feedback is built from the failing traces, so GEPA learns
concrete rules ("on .tsx files run tsc --noEmit before claiming done").

Best-practice notes baked in:
- Isolation: each candidate is evaluated in a *copied* recipe dir; the real recipe
  is never mutated during search.
- Grounded scoring: only real verifier/test outcomes count (no keyword labels).
- Honesty-aware: a run that claims done but fails is penalized via completion_honesty.
- Cost-aware: token cost is a soft penalty so GEPA doesn't win by burning money.

Running the search executes REAL mini-ork runs (real spend); do it from the
mini-ork env. See run_gepa.py for the entry point.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from gepa.core.adapter import EvaluationBatch, GEPAAdapter
except ImportError as exc:  # pragma: no cover
    raise ImportError("GEPA not installed. Run: pip install gepa") from exc

# Which recipe prompt files are optimizable components. Keys become the GEPA
# candidate keys; values are the file names under recipes/<recipe>/prompts/.
DEFAULT_COMPONENTS = {"planner": "planner.md", "implementer": "implementer.md", "reviewer": "reviewer.md"}

# Score weights (sum of positives ~1.0; penalties subtract).
W_TESTS = 0.6      # verifier / tests passed
W_STATUS = 0.4     # run reached a successful terminal state
COST_PENALTY_PER_USD = 0.05   # soft: -0.05 per $1 spent (keeps it honest on cost)


@dataclass
class MiniOrkTask:
    """One training/eval item: a kickoff to run under a recipe/task_class."""
    kickoff: str            # path to a kickoff .md
    recipe: str
    task_class: str


@dataclass
class MiniOrkTrace:
    run_id: str
    status: str
    tests_passed: bool | None
    false_completion: bool
    cost_usd: float
    verifier_output: str
    reviewer_verdict: str | None
    files_written: list[str] = field(default_factory=list)


@dataclass
class MiniOrkOutput:
    run_id: str
    passed: bool


class MiniOrkGEPAAdapter(GEPAAdapter[MiniOrkTask, MiniOrkTrace, MiniOrkOutput]):
    def __init__(
        self,
        mini_ork_root: str | Path,
        recipe: str,
        state_db: str | Path,
        components: dict[str, str] | None = None,
        run_timeout_s: int = 1800,
        honesty_penalty: float = 0.6,
    ):
        self.root = Path(mini_ork_root)
        self.recipe = recipe
        self.state_db = str(state_db)
        self.components = components or DEFAULT_COMPONENTS
        self.run_timeout_s = run_timeout_s
        self.honesty_penalty = honesty_penalty
        self._ctr = itertools.count()

    # ---- GEPA interface -------------------------------------------------

    def evaluate(
        self, batch: list[MiniOrkTask], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvaluationBatch[MiniOrkTrace, MiniOrkOutput]:
        recipe_dir = self._materialize_recipe(candidate)
        outputs: list[MiniOrkOutput] = []
        scores: list[float] = []
        traces: list[MiniOrkTrace] | None = [] if capture_traces else None
        try:
            for task in batch:
                run_id = self._run_miniork(task, recipe_dir)
                trace = self._trace_from_db(run_id)
                score = self._score(trace)
                outputs.append(MiniOrkOutput(run_id=run_id, passed=score >= 0.9))
                scores.append(score)
                if traces is not None:
                    traces.append(trace)
        finally:
            shutil.rmtree(recipe_dir, ignore_errors=True)
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=traces)

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch[MiniOrkTrace, MiniOrkOutput],
        components_to_update: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Turn REAL failure evidence into per-component textual feedback GEPA
        reflects on. This is GEPA's edge over a blind synthesizer: concrete,
        grounded 'why it failed' rather than a scalar."""
        dataset: dict[str, list[dict[str, Any]]] = {c: [] for c in components_to_update}
        for i, trace in enumerate(eval_batch.trajectories or []):
            feedback = self._feedback_text(trace, eval_batch.scores[i])
            for comp in components_to_update:
                dataset[comp].append({
                    "Inputs": {"task_recipe": self.recipe, "run_id": trace.run_id},
                    "Generated Outputs": {
                        "status": trace.status,
                        "tests_passed": trace.tests_passed,
                        "files_written": trace.files_written[:10],
                        "reviewer_verdict": trace.reviewer_verdict,
                    },
                    "Feedback": feedback,
                })
        return dataset

    # ---- scoring + reflection (grounded in real outcomes) ---------------

    def _score(self, t: MiniOrkTrace) -> float:
        base = (W_TESTS if t.tests_passed else 0.0) + (W_STATUS if t.status == "success" else 0.0)
        if t.false_completion:            # claimed done but human/verifier rejected
            base -= self.honesty_penalty
        base -= COST_PENALTY_PER_USD * max(0.0, t.cost_usd)
        return max(0.0, min(1.0, base))

    def _feedback_text(self, t: MiniOrkTrace, score: float) -> str:
        parts = [f"score={score:.2f} status={t.status} tests_passed={t.tests_passed} cost=${t.cost_usd:.3f}"]
        if t.false_completion:
            parts.append("FALSE COMPLETION: the agent claimed the task was done but it was not — "
                         "tighten the prompt to require verification before declaring completion.")
        if t.tests_passed is False and t.verifier_output:
            parts.append("VERIFIER/TEST FAILURE. Output:\n" + t.verifier_output[:1200])
        if t.reviewer_verdict and t.reviewer_verdict not in ("approve", "pass"):
            parts.append(f"REVIEWER rejected with verdict={t.reviewer_verdict}.")
        if score >= 0.9:
            parts.append("SUCCESS — keep what worked; extract the reusable rule.")
        return "\n".join(parts)

    # ---- mini-ork integration -------------------------------------------

    def _materialize_recipe(self, candidate: dict[str, str]) -> Path:
        """mini-ork resolves recipes by NAME under recipes/, so we materialize the
        candidate as a temp recipe *inside* recipes/ (unique name), overwrite its
        prompts, and return its dir. The live recipe is never touched; the temp
        recipe is removed after evaluation."""
        src = self.root / "recipes" / self.recipe
        name = f"{self.recipe}__gepa_{os.getpid()}_{next(self._ctr)}"
        dst = self.root / "recipes" / name
        shutil.copytree(src, dst)
        prompts = dst / "prompts"
        for comp, fname in self.components.items():
            if comp in candidate:
                (prompts / fname).write_text(candidate[comp], encoding="utf-8")
        return dst

    def _run_miniork(self, task: MiniOrkTask, recipe_dir: Path) -> str:
        """SEAM: execute one real mini-ork run with the candidate prompts, return
        its run_id. Runs `mini-ork run <temp-recipe-name> <kickoff>` (recipe by
        name). Real spend. Runs are serial, so the newest `runs` row is ours."""
        env = {
            **os.environ,
            "MINI_ORK_ROOT": str(self.root),
            "MINI_ORK_NONINTERACTIVE": "1",
        }
        subprocess.run(
            [str(self.root / "bin" / "mini-ork"), "run", recipe_dir.name, task.kickoff],
            env=env, cwd=self.root, timeout=self.run_timeout_s,
            check=False, capture_output=True, text=True,
        )
        return self._latest_run_id()

    def _latest_run_id(self) -> str:
        con = sqlite3.connect(self.state_db)
        try:
            row = con.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            return str(row[0]) if row else ""
        finally:
            con.close()

    def _trace_from_db(self, run_id: str) -> MiniOrkTrace:
        con = sqlite3.connect(self.state_db)
        con.row_factory = sqlite3.Row
        try:
            et = con.execute(
                "SELECT status, tests_passed, verifier_output, reviewer_verdict, "
                "files_written, cost_usd FROM execution_traces WHERE run_id=? "
                "ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
            cost = con.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE run_id=?", (run_id,)).fetchone()[0]
        finally:
            con.close()
        tp = None
        false_completion = False
        status = "unknown"
        verifier = ""
        verdict = None
        files: list[str] = []
        if et:
            status = et["status"] or "unknown"
            tp = bool(et["tests_passed"]) if et["tests_passed"] is not None else None
            verifier = str(et["verifier_output"] or "")
            verdict = et["reviewer_verdict"]
            try:
                files = json.loads(et["files_written"] or "[]")
            except (ValueError, TypeError):
                files = []
            # a "success" status with failing tests is a false completion
            false_completion = status == "success" and tp is False
        return MiniOrkTrace(
            run_id=run_id, status=status, tests_passed=tp, false_completion=false_completion,
            cost_usd=float(cost or 0.0), verifier_output=verifier, reviewer_verdict=verdict,
            files_written=files,
        )
