"""Real GEPA run driven entirely by codex (funded via ChatGPT OAuth).

- Reflection LM: `codex exec --output-last-message` (clean text out).
- mini-ork lanes: codex (planner/implementer/reviewer).
- Target: a scratch repo with a deliberately-broken test (safe; codex won't
  touch the framework tree, hence MO_TARGET_CWD).

Usage: python -m mini_ork.gepa.run_gepa_codex <target_repo> <kickoff.md> [budget]
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import gepa

from mini_ork.gepa.miniork_adapter import DEFAULT_COMPONENTS, MiniOrkGEPAAdapter, MiniOrkTask

ROOT = Path(os.environ.get("MINI_ORK_ROOT", "/Volumes/docker-ssd/ps/mo-fix"))


def codex_lm(prompt: str) -> str:
    """Reflection LM = codex exec, run in a throwaway dir so it can't edit anything."""
    with tempfile.TemporaryDirectory() as sandbox:
        outf = Path(sandbox) / "out.txt"
        subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--output-last-message", str(outf), prompt],
            cwd=sandbox, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300,
        )
        return outf.read_text(encoding="utf-8") if outf.exists() else ""


def main() -> None:
    target = str(Path(sys.argv[1]).resolve())
    kickoff = str(Path(sys.argv[2]).resolve())
    budget = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    # --- force everything onto codex + point verifiers at the scratch target ---
    home = Path(tempfile.mkdtemp(prefix="gepa-codex-home-"))
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(
        "lanes: {planner: codex, worker: codex, implementer: codex, reviewer: codex, "
        "worker_default: codex, reviewer_default: codex}\n")
    os.environ.update({
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": str(ROOT / ".mini-ork" / "state.db"),   # migrated schema
        "MO_REFLECTION_LANE": "codex", "MO_MUTATION_LANE": "codex",
        "MINI_ORK_NONINTERACTIVE": "1",
        "MINI_ORK_TEST_CMD": "python3 -m pytest -q",
        "MINI_ORK_TYPECHECK_CMD": "true",                       # scratch has no typechecker
    })

    adapter = MiniOrkGEPAAdapter(
        mini_ork_root=ROOT, recipe="code-fix",
        state_db=ROOT / ".mini-ork" / "state.db", target_cwd=target, run_timeout_s=1200)
    seed = {c: (ROOT / "recipes" / "code-fix" / "prompts" / f).read_text(encoding="utf-8")
            for c, f in DEFAULT_COMPONENTS.items()
            if (ROOT / "recipes" / "code-fix" / "prompts" / f).exists()}
    trainset = [MiniOrkTask(kickoff=kickoff, recipe="code-fix", task_class="code_fix")]

    print(f"GEPA(codex): target={target} budget={budget} components={list(seed)}", flush=True)
    result = gepa.optimize(
        seed_candidate=seed, trainset=trainset, adapter=adapter,
        reflection_lm=codex_lm, max_metric_calls=budget, display_progress_bar=False,
        raise_on_exception=False,
    )
    out = ROOT / "recipes" / "code-fix" / "prompts.gepa-codex-proposed"
    out.mkdir(parents=True, exist_ok=True)
    for c, text in (result.best_candidate or seed).items():
        (out / DEFAULT_COMPONENTS[c]).write_text(text, encoding="utf-8")
    print(f"\nbest score: {getattr(result, 'best_score', 'n/a')}")
    print(f"winning prompts -> {out}")


if __name__ == "__main__":
    main()
