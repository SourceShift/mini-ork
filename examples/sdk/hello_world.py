#!/usr/bin/env python3
"""mini-ork Python SDK — hello world for the in-process *primitives*.

Run it with no arguments and no configuration::

    python3 examples/sdk/hello_world.py

It exercises the building blocks that any application can embed directly —
no YAML recipe, no ``mini-ork`` subprocess, no provider credentials:

* ``available_backends()`` — where a verification could execute.
* ``memory.add`` / ``memory.search`` — a real verified-outcome memory roundtrip
  on a throwaway SQLite db using the stdlib ``HashEmbedder`` (no network).
* ``router.preferred_lane`` — the cost-free bandit's current lane choice.
* ``dispatch_model`` — proof that an unroutable call returns a *structured*
  ``ok=False`` result instead of raising.

The one primitive this script does NOT execute is ``Crucible.run_test`` — that
needs a container/runtime — but it shows how you would construct it.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Run from a fresh clone without `pip install`: put the repo root on the path
# so `import mini_ork` resolves even though this script lives two levels down.
if "mini_ork" not in sys.modules:
    _repo_root = Path(__file__).resolve().parents[2]
    if (_repo_root / "mini_ork" / "__init__.py").exists():
        sys.path.insert(0, str(_repo_root))

from mini_ork import (
    DispatchRequest,
    RuntimeSpec,
    available_backends,
    dispatch_model,
    memory,
    router,
)


def demo_backends() -> None:
    print("== runtime backends ==")
    print("  available_backends():", available_backends())
    # A RuntimeSpec is all Crucible needs; execution is deferred to a backend.
    spec = RuntimeSpec(image="python:3.11-slim", backend="auto")
    print("  example RuntimeSpec:", spec.image, "/", spec.backend)


def demo_memory(db_path: str) -> None:
    print("\n== verified-outcome memory ==")
    scope = "sdk-demo"
    # infer=False stores the text verbatim (infer=True would call a model).
    memory.add("the retry budget for the ingest lane is 3", scope=scope,
               infer=False, db_path=db_path)
    memory.add("codex is the implementer lane for python patches", scope=scope,
               infer=False, db_path=db_path)
    hits = memory.search("how many retries does ingest get?", scope=scope,
                         top_k=1, db_path=db_path)
    top = hits[0] if hits else {"text": "<none>", "score": 0.0}
    print(f"  top hit: {top['text']!r}  (score={top['score']:.3f})")


def demo_router(db_path: str) -> None:
    import sqlite3
    print("\n== cost-free bandit routing ==")
    try:
        lane = router.preferred_lane("code_fix", node_type="implementer", db=db_path)
        print("  preferred_lane(code_fix/implementer):", lane or "<no prior — default>")
    except sqlite3.OperationalError:
        # A throwaway db has no router tables; routing needs a warmed state db
        # (``mini-ork init`` + a few real runs). The call is safe once priors exist.
        print("  (no warmed router db yet — run `mini-ork init` and a few runs first)")


def demo_dispatch() -> None:
    print("\n== heterogeneous dispatch (fail-fast, no raise) ==")
    req = DispatchRequest(model="does-not-exist-lane", prompt="hello")
    result = dispatch_model(req, preflight_check=True)
    print(f"  ok={result.ok} rc={result.rc} error={result.error!r}")
    assert result.ok is False, "an unroutable lane must return ok=False, not raise"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "sdk_demo.db")
        demo_backends()
        demo_memory(db_path)
        demo_router(db_path)
        demo_dispatch()
    print("\nok — primitives are importable and run in-process.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
