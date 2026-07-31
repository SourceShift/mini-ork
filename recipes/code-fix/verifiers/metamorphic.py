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


def _load_json_spec(path: str):
    """Safe data-driven spec (from the auto-proposer): relations are resolved by
    NAME from the vetted library — no LLM-authored code is executed. The target
    function is imported from the run's own (patched) module."""
    import importlib
    from mini_ork.learning import metamorphic as mm
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    tgt = spec["target"]
    mod_ref, fn_name = tgt["module"], tgt["function"]
    if os.path.isfile(mod_ref):
        loaded = _load_spec(mod_ref)          # a file path → the patched module
    else:
        loaded = importlib.import_module(mod_ref)
    target = getattr(loaded, fn_name)
    relations = mm.resolve_relations(spec.get("relations", []))  # whitelist only
    return target, list(spec.get("seed_inputs", [])), relations


def main() -> int:
    from mini_ork.learning import metamorphic as mm
    json_spec = os.environ.get("MO_METAMORPHIC_SPEC_JSON", "").strip()
    spec_path = os.environ.get("MO_METAMORPHIC_SPEC", "").strip()

    if json_spec and os.path.isfile(json_spec):
        try:
            target, seeds, relations = _load_json_spec(json_spec)
        except Exception as exc:  # noqa: BLE001 — a broken spec must not crash the run
            print(json.dumps({"verdict": "vacuous", "note": f"json spec load failed: {exc}"}))
            return 0
    elif spec_path and os.path.isfile(spec_path):
        try:
            mod = _load_spec(spec_path)
            target = mod.TARGET
            seeds = list(mod.SEED_INPUTS)
            relations = list(getattr(mod, "RELATIONS", []))
        except Exception as exc:  # noqa: BLE001 — a broken spec must not crash the run
            print(json.dumps({"verdict": "vacuous", "note": f"spec load failed: {exc}"}))
            return 0
    else:
        print(json.dumps({"verdict": "vacuous", "note": "no metamorphic spec"}))
        return 0

    result = mm.check(target, seeds, relations)
    env = result.to_verifier_json()
    print(json.dumps(env))
    # vacuous (no relation ran) is not a failure; only a real violation fails.
    return 1 if env.get("pass") is False else 0


if __name__ == "__main__":
    sys.exit(main())
