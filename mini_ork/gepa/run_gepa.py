"""Run GEPA prompt optimization for one mini-ork recipe.

Seed = the recipe's current node prompts. GEPA reflectively evolves them against
a small trainset of real kickoffs, scored on REAL downstream outcomes. The winner
is written to a proposed/ dir for review before promotion via prompt_win_rates.

  pip install gepa
  python -m mini_ork.gepa.run_gepa \
      --recipe code-fix --task-class code_fix \
      --trainset kickoffs/gepa/code-fix/*.md \
      --reflection-lm anthropic/opus \
      --budget 40 --out recipes/code-fix/prompts.gepa-proposed

Executes REAL mini-ork runs (real spend). `--budget` caps total evaluations —
GEPA is sample-efficient (often ~35x fewer than RL), so 30-50 is a sensible start.
"""
from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path

import gepa

from mini_ork.gepa.miniork_adapter import DEFAULT_COMPONENTS, MiniOrkGEPAAdapter, MiniOrkTask


def _seed_from_recipe(root: Path, recipe: str, components: dict[str, str]) -> dict[str, str]:
    prompts = root / "recipes" / recipe / "prompts"
    seed = {}
    for comp, fname in components.items():
        p = prompts / fname
        if p.exists():
            seed[comp] = p.read_text(encoding="utf-8")
    if not seed:
        raise SystemExit(f"no optimizable prompts found under {prompts}")
    return seed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", required=True)
    ap.add_argument("--task-class", required=True)
    ap.add_argument("--trainset", nargs="+", required=True, help="kickoff .md paths / globs")
    ap.add_argument("--reflection-lm", default="anthropic/opus",
                    help="strong model for reflection (no-opus rule lifted for deep-reasoning roles)")
    ap.add_argument("--budget", type=int, default=40, help="max_metric_calls (total evaluations)")
    ap.add_argument("--root", type=Path, default=Path(os.environ.get("MINI_ORK_ROOT", ".")))
    ap.add_argument("--state-db", type=Path,
                    default=Path(os.environ.get("MINI_ORK_DB", ".mini-ork/state.db")))
    ap.add_argument("--out", type=Path, required=True, help="dir to write the winning prompts")
    args = ap.parse_args()

    kickoffs: list[str] = []
    for pat in args.trainset:
        kickoffs.extend(sorted(glob.glob(pat)))
    if not kickoffs:
        raise SystemExit("empty trainset")
    trainset = [MiniOrkTask(kickoff=k, recipe=args.recipe, task_class=args.task_class) for k in kickoffs]

    seed = _seed_from_recipe(args.root, args.recipe, DEFAULT_COMPONENTS)
    adapter = MiniOrkGEPAAdapter(mini_ork_root=args.root, recipe=args.recipe, state_db=args.state_db)

    print(f"GEPA: recipe={args.recipe} components={list(seed)} trainset={len(trainset)} budget={args.budget}")
    result = gepa.optimize(
        seed_candidate=seed,
        trainset=trainset,
        adapter=adapter,
        reflection_lm=args.reflection_lm,
        max_metric_calls=args.budget,
    )

    args.out.mkdir(parents=True, exist_ok=True)
    best = result.best_candidate
    for comp, text in best.items():
        (args.out / DEFAULT_COMPONENTS[comp]).write_text(text, encoding="utf-8")
    print(f"\nbest score: {getattr(result, 'best_score', 'n/a')}")
    print(f"winning prompts -> {args.out}")
    print("Next: review the diff, then promote via prompt_win_rates + the normal gate")
    print("(mini-ork's apo-prompt-tune promotion path). Do NOT overwrite the live recipe blindly.")


if __name__ == "__main__":
    main()
