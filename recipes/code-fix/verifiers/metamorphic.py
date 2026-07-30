#!/usr/bin/env python3
"""verifiers/metamorphic.py — Layer-2 metamorphic verifier for code-fix.

Runs task-supplied metamorphic relations against a target function using the
gold-free engine in ``mini_ork.learning.metamorphic``. Emits a Layer-0-compatible
JSON verdict on stdout, so its result feeds the execution reward like any other
verifier. Execution-grounded: a relation "fails" only when running it produces a
real counterexample.

Spec sourcing (declarative, deterministic): set ``MO_METAMORPHIC_SPEC`` to a
Python file exposing:
  - TARGET:      a single-argument callable (the function under test)
  - SEED_INPUTS: an iterable of seed inputs
  - RELATIONS:   a list of mini_ork.learning.metamorphic.MetamorphicRelation

No spec → a clean ``vacuous`` verdict (Layer 0 excludes it; it never inflates or
deflates the reward). This keeps the verifier a safe no-op until a task provides
relations. LLM-proposed relations (arXiv 2603.24774) are the richer follow-on:
the proposer emits RELATIONS, and this same execution oracle certifies them.

Exit codes: 0 pass / vacuous, 1 fail (a metamorphic relation was violated).
"""

import importlib.util
import json
import os
import sys


def _load_spec(path: str):
    spec = importlib.util.spec_from_file_location("mo_metamorphic_spec", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    spec_path = os.environ.get("MO_METAMORPHIC_SPEC", "").strip()
    if not spec_path or not os.path.isfile(spec_path):
        print(json.dumps({"verdict": "vacuous", "note": "no metamorphic spec"}))
        return 0

    try:
        from mini_ork.learning import metamorphic as mm
        mod = _load_spec(spec_path)
        target = mod.TARGET
        seeds = list(mod.SEED_INPUTS)
        relations = list(getattr(mod, "RELATIONS", []))
    except Exception as exc:  # noqa: BLE001 — a broken spec must not crash the run
        print(json.dumps({"verdict": "vacuous", "note": f"spec load failed: {exc}"}))
        return 0

    result = mm.check(target, seeds, relations)
    env = result.to_verifier_json()
    print(json.dumps(env))
    # vacuous (no relation ran) is not a failure; only a real violation fails.
    return 1 if env.get("pass") is False else 0


if __name__ == "__main__":
    sys.exit(main())
