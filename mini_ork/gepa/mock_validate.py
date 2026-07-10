"""Mock validation of the GEPA <-> mini-ork loop (no real mini-ork runs).

Proves the wiring end-to-end cheaply: gepa.optimize calls our evaluate +
make_reflective_dataset, the DeepSeek reflection LM proposes improved prompts,
and GEPA climbs. Uses a learnable rule (a good planner prompt must instruct
verification before claiming done) so we can see the score rise and the winning
prompt acquire the rule.

Run: source ~/ps/scripts/cl_deepseek.sh && python -m mini_ork.gepa.mock_validate
"""
from __future__ import annotations

import json
import os
import urllib.request

import gepa

from mini_ork.gepa.miniork_adapter import MiniOrkGEPAAdapter, MiniOrkOutput, MiniOrkTrace
from gepa.core.adapter import EvaluationBatch

BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
TOK = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")


def deepseek_lm(prompt: str) -> str:
    body = json.dumps({"model": "deepseek-v4-flash", "max_tokens": 3000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{BASE}/v1/messages", data=body, headers={
        "x-api-key": TOK, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class MockAdapter(MiniOrkGEPAAdapter):
    """Fakes mini-ork execution; score depends on the candidate so GEPA has a
    real gradient. Reuses the REAL make_reflective_dataset from the base class."""
    def evaluate(self, batch, candidate, capture_traces=False):
        planner = candidate.get("planner", "").lower()
        good = ("verify" in planner or "tsc" in planner or "test" in planner)
        score = 1.0 if good else 0.3
        outputs, scores, traces = [], [], ([] if capture_traces else None)
        for _ in batch:
            outputs.append(MiniOrkOutput(run_id="mock", passed=good))
            scores.append(score)
            if traces is not None:
                traces.append(MiniOrkTrace(
                    run_id="mock", status="success", reward_g=(1.0 if good else -1.0),
                    false_completion=not good, cost_usd=0.5,
                    verifier_output="" if good else "TS2345: Argument of type 'X' not assignable",
                    reviewer_verdict=None if good else "needs-revision", files_written=["src/a.ts"]))
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=traces)


def main() -> None:
    if not TOK:
        raise SystemExit("no ANTHROPIC_AUTH_TOKEN — run: source ~/ps/scripts/cl_deepseek.sh")
    adapter = MockAdapter(mini_ork_root=".", recipe="code-fix", state_db="/dev/null")
    seed = {"planner": "You are the planner node. Produce a JSON plan for the code fix."}
    trainset = [{"id": i} for i in range(3)]

    result = gepa.optimize(
        seed_candidate=seed, trainset=trainset, adapter=adapter,
        reflection_lm=deepseek_lm, max_metric_calls=12, display_progress_bar=False,
    )
    best = result.best_candidate
    print("\n=== MOCK VALIDATION RESULT ===")
    print("best score:", getattr(result, "best_score", getattr(result, "val_aggregate_scores", "n/a")))
    grew = any(k in best["planner"].lower() for k in ("verify", "tsc", "test"))
    print("winning planner acquired a verification rule:", grew)
    print("\n--- winning planner prompt (head) ---")
    print(best["planner"][:600])


if __name__ == "__main__":
    main()
