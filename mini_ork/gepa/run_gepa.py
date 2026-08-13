"""Run GEPA prompt optimization — two modes.

RECIPE mode (optimize a mini-ork recipe's node prompts, scored on real runs):

  pip install gepa
  python -m mini_ork.gepa.run_gepa \
      --recipe code-fix --task-class code_fix \
      --trainset kickoffs/gepa/code-fix/*.md \
      --reflection-lm anthropic/opus \
      --budget 40 --out recipes/code-fix/prompts.gepa-proposed

EXTERNAL mode (optimize ANY seed prompt against ANY external critic — no recipe,
no state.db). The critic is a command that reads
``{"candidate": {...}, "task": {...}}`` JSON on stdin and prints
``{"score": <0..1>, "feedback": "<text>"}`` on stdout:

  python -m mini_ork.gepa.run_gepa \
      --seed-file seed_prompt.json \
      --eval-cmd "node dist/eval-c4.js" \
      --trainset fixtures/*.json \
      --reflection-lm anthropic/opus \
      --budget 30 --out out/gepa-c4

``--seed-file`` is either a JSON object ``{component: prompt_text, ...}`` or a
raw text file (wrapped as ``{"prompt": <text>}``). Each ``--trainset`` file is a
task object (JSON object, a JSON array of objects, or — for a non-JSON file —
``{"path": ..., "text": <contents>}``). Both modes execute REAL work (real
spend); ``--budget`` caps evaluations — GEPA is sample-efficient (~35x fewer than
RL), so 20-50 is a sensible start.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

# NOTE: ``import gepa`` is deferred into the two _run_* functions. The framework
# is an OPTIONAL dependency; importing this module (for --help, or to reuse the
# pure _seed_from_file / _tasks_from_trainset helpers) must not require it.


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


def _seed_from_file(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"prompt": text}
    if isinstance(parsed, dict) and parsed and all(isinstance(v, str) for v in parsed.values()):
        return {str(k): v for k, v in parsed.items()}
    # A JSON file that isn't a {component: text} map is treated as raw seed text.
    return {"prompt": text}


def _tasks_from_trainset(patterns: list[str]) -> list[object]:
    tasks: list[object] = []
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            raw = Path(path).read_text(encoding="utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                tasks.append({"path": path, "text": raw})
                continue
            if isinstance(parsed, list):
                tasks.extend(parsed)
            else:
                tasks.append(parsed)
    if not tasks:
        raise SystemExit("empty trainset")
    return tasks


def _run_external(args: argparse.Namespace) -> None:
    import gepa

    from mini_ork.gepa.backends import SubprocessRunBackend
    from mini_ork.gepa.generic_adapter import GEPARunBackendAdapter

    seed = _seed_from_file(Path(args.seed_file))
    tasks = _tasks_from_trainset(args.trainset)
    backend = SubprocessRunBackend(
        args.eval_cmd, timeout_s=args.eval_timeout, cwd=args.eval_cwd
    )
    adapter = GEPARunBackendAdapter(backend, components=list(seed))

    print(
        f"GEPA[external]: components={list(seed)} trainset={len(tasks)} "
        f"budget={args.budget} eval_cmd={args.eval_cmd!r}"
    )
    result = gepa.optimize(
        seed_candidate=seed,
        trainset=tasks,
        adapter=adapter,
        reflection_lm=args.reflection_lm,
        max_metric_calls=args.budget,
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    best = result.best_candidate
    for comp, text in best.items():
        (out / f"{comp}.txt").write_text(text, encoding="utf-8")
    (out / "best_candidate.json").write_text(
        json.dumps(best, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nbest score: {getattr(result, 'best_score', 'n/a')}")
    print(f"winning prompt(s) -> {out}")


def _run_recipe(args: argparse.Namespace) -> None:
    import gepa

    from mini_ork.gepa.miniork_adapter import DEFAULT_COMPONENTS, MiniOrkGEPAAdapter, MiniOrkTask

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


def main() -> None:
    ap = argparse.ArgumentParser()
    # External mode
    ap.add_argument("--seed-file", help="EXTERNAL mode: seed prompt (JSON {component: text} or raw text)")
    ap.add_argument("--eval-cmd", help="EXTERNAL mode: critic command (reads {candidate,task} JSON stdin, prints {score,feedback} stdout)")
    ap.add_argument("--eval-timeout", type=int, default=1800, help="EXTERNAL mode: per-eval subprocess timeout (s)")
    ap.add_argument("--eval-cwd", default=None, help="EXTERNAL mode: working dir for the eval command")
    # Recipe mode
    ap.add_argument("--recipe", help="RECIPE mode: recipe name under recipes/")
    ap.add_argument("--task-class", help="RECIPE mode: task class")
    # Shared
    ap.add_argument("--trainset", nargs="+", required=True,
                    help="RECIPE: kickoff .md paths/globs. EXTERNAL: task .json paths/globs.")
    ap.add_argument("--reflection-lm", default="anthropic/opus",
                    help="strong model for reflection (no-opus rule lifted for deep-reasoning roles)")
    ap.add_argument("--budget", type=int, default=40, help="max_metric_calls (total evaluations)")
    ap.add_argument("--root", type=Path, default=Path(os.environ.get("MINI_ORK_ROOT", ".")))
    ap.add_argument("--state-db", type=Path,
                    default=Path(os.environ.get("MINI_ORK_DB", ".mini-ork/state.db")))
    ap.add_argument("--out", type=Path, required=True, help="dir to write the winning prompt(s)")
    args = ap.parse_args()

    if args.eval_cmd:
        if not args.seed_file:
            raise SystemExit("EXTERNAL mode (--eval-cmd) requires --seed-file")
        _run_external(args)
    else:
        if not (args.recipe and args.task_class):
            raise SystemExit("RECIPE mode requires --recipe and --task-class (or use EXTERNAL mode: --seed-file --eval-cmd)")
        _run_recipe(args)


if __name__ == "__main__":
    main()
