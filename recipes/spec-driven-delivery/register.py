"""Recipe-local registrations for the spec-driven-delivery recipe.

Loaded once per process via
``mini_ork.cli.recipe_register.load_recipe_register``, which executes
module-level side effects via ``importlib.util.spec_from_file_location``.
All imports must be import-safe when ``mini_ork`` is importable — never
reach for a missing dependency at module top level.

What this module does:

1. Registers the ``(spec-driven-delivery, per_spec_dispatcher)`` implementer
   submode with the deterministic Python fan-out driver at
   ``lib/per_spec_dispatcher.py``. The dispatcher reads
   ``${MINI_ORK_RUN_DIR}/spec-cards/*.json`` + ``gates/*.json``, walks every
   SpecCard's deliverables in topological order (resolving cross-spec
   ``<spec_id>/<id>`` edges), writes one child kickoff per deliverable
   under ``child-kickoffs/<spec>--<id>.md``, and dispatches each serially
   through ``bin/mini-ork run recursive-validate-impl <kickoff>``.

   The driver writes ``${MINI_ORK_RUN_DIR}/dispatch-results.json`` (the
   shape consumed by the downstream ``dispatch-aggregator`` verifier) and
   per-deliverable records under ``child-runs/``. When the SpecCard or
   gates lack information needed to dispatch a deliverable, the driver
   writes a structured ASK to ``asks/<spec>--<id>.json`` and records the
   deliverable as ``blocked``.

   Set ``MO_PER_SPEC_DISPATCH_FAIL_FAST=0`` (default) to keep iterating
   through downstream deliverables after a failure so the aggregator sees
   a complete picture; ``=1`` aborts after the first non-delivered
   verdict (mirrors the sibling per_feature_dispatcher opt-in). The
   override ``MINI_ORK_PER_SPEC_MINI_ORK_BIN`` is honored by the driver
   when a test wants to point at a different launcher than
   ``<root>/bin/mini-ork``.
"""

from __future__ import annotations

from mini_ork.cli.execute_handlers import register_implementer_submode

register_implementer_submode(
    recipe="spec-driven-delivery",
    node_id="per_spec_dispatcher",
    results_artifact="dispatch-results.json",
    script="spec-driven-delivery/lib/per_spec_dispatcher.py",
)