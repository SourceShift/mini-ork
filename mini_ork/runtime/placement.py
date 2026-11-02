"""Per-run worktree placement — the run edits a checkout only it can write.

The implementer edits ``roots.target`` in place. When that target is a tree
another session is also editing, the artifact a reviewer reads can contain work
the run did not do: a *correct* gate grading the *wrong* diff, which is the worst
failure mode in the list because nothing about it looks like a failure. The
baseline capture's own comment records a review that swept another session's
work.

Declaring placement mints a per-run worktree through the **existing**
``mini_ork.workspaces`` machinery — branch ``mini-ork/<run_id>``, record at
``<home>/worktrees/<run_id>.json``, the project's setup hook, submodules — and
points the run's roots at it, so every node, verifier and git step sees one tree
that only this run can write.

Nothing new owns worktrees. The record, ``status``, ``merge`` and ``discard``
are the board's, and ``mini-ork inbox`` already lists the keep-or-discard
decision once the run ends (a terminal run with commits ahead). That is also why
the delta is not applied back automatically: merging a run's work into a person's
branch is theirs to approve, and the board is where they do it.

Two ways to declare it, because they answer different questions:

* ``placement: {worktree: true}`` at the top of a recipe's ``workflow.yaml`` —
  the run cannot forget it;
* ``MO_WORKTREE_PLACEMENT=1`` — for one run, including a recipe you do not own.
  ``MO_WORKTREE_PLACEMENT=0`` is an explicit opt-out and beats a recipe that
  declares it.

The base is the target's HEAD at run-start, recorded in the workspace record as
``base_sha``/``base_branch`` — so a red→green replay computes against the tree
the change was written on, not the shared checkout that has since moved.

Off by default. The shared checkout is the right tree for a single-session
project, and a copy costs disk plus a submodule fetch.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

__all__ = ["Declaration", "PlacementError", "SPLIT_ENV", "declared", "prepare"]

#: Operator switch. Truthy enables, a falsey explicit value disables even when
#: the recipe declares placement (the recipe is a default, not a cage).
SPLIT_ENV = "MO_WORKTREE_PLACEMENT"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})


class PlacementError(RuntimeError):
    """Placement was declared and could not be honoured. Fails the run start.

    A silent fallback to the shared checkout is the defect this module exists to
    prevent: the run would look isolated and be graded on someone else's tree.
    """


@dataclass(frozen=True, slots=True)
class Declaration:
    """Whether this run asked for its own checkout, and who asked."""

    worktree: bool = False
    source: str = ""  # "env" | "workflow" | ""

    def __bool__(self) -> bool:  # so ``if declared(...):`` reads naturally
        return self.worktree


def _read_profile(run_dir: str) -> dict:
    path = os.path.join(run_dir, "run_profile.json") if run_dir else ""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            prof = json.load(fh)
    except (OSError, ValueError):
        return {}
    return prof if isinstance(prof, dict) else {}


def _recipe_placement(run_dir: str, env: Mapping[str, str]) -> dict:
    """The recipe's ``placement`` block, or ``{}``.

    Read straight from the YAML rather than through the workflow compiler: the
    compiler validates nodes and edges, and a run-start decision has no business
    failing because a node is malformed (the compiler will say so, loudly, a
    moment later). A recipe that is missing, unreachable, or has no block
    simply declares nothing.
    """
    recipe = str(_read_profile(run_dir).get("recipe", "") or "").strip()
    engine = (env.get("MINI_ORK_ROOT", "") or env.get("MINI_ORK_ENGINE_ROOT", "")).strip()
    if not recipe or not engine:
        return {}
    path = Path(engine) / "recipes" / recipe / "workflow.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml  # local: the placement-off path never imports it
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — malformed YAML is the compiler's finding
        return {}
    block = raw.get("placement") if isinstance(raw, dict) else None
    return block if isinstance(block, dict) else {}


def declared(run_dir: str, *, env: Mapping[str, str] | None = None) -> Declaration:
    """Did this run ask for its own checkout?

    The operator's explicit value wins in both directions: ``1`` enables for a
    run whose recipe says nothing, ``0`` disables for a run whose recipe says
    yes. With no explicit value, the recipe's ``placement.worktree`` decides.
    """
    src = os.environ if env is None else env
    explicit = str(src.get(SPLIT_ENV, "") or "").strip().lower()
    if explicit in _FALSEY:
        return Declaration(False, "env")
    if explicit in _TRUTHY:
        return Declaration(True, "env")
    block = _recipe_placement(run_dir, src)
    return Declaration(bool(block.get("worktree")), "workflow" if block else "")


def prepare(run_dir: str, roots, *, env: Mapping[str, str] | None = None):
    """Point ``roots`` at a per-run worktree when placement was declared.

    Returns ``roots`` unchanged when it was not — the module costs one env read
    and one profile read on the default path. Raises :class:`PlacementError`
    (never returns a shared-checkout fallback) when it was declared and cannot
    be honoured.
    """
    if not declared(run_dir, env=env).worktree:
        return roots
    src = os.environ if env is None else env
    home = str(getattr(roots, "home", "") or src.get("MINI_ORK_HOME", "") or "").strip()
    run_id = os.path.basename(str(run_dir).rstrip(os.sep))
    if not home:
        raise PlacementError(
            "worktree placement was declared but MINI_ORK_HOME is unset, so there "
            "is nowhere to record the worktree"
        )
    if not run_id:
        raise PlacementError("worktree placement was declared but the run id is unknown")
    from mini_ork import workspaces  # local: keeps the default path import-free

    existing = workspaces.load(Path(home), run_id)
    if existing is not None:
        # Idempotent: a resumed run keeps the checkout it started in, exactly as
        # persist_run_roots promises for roots.
        return replace(roots, target=str(existing.path), placement="worktree")
    try:
        ws = workspaces.create(Path(roots.target), Path(home), run_id)
    except RuntimeError as exc:
        raise PlacementError(
            f"worktree placement was declared for {run_id} but the worktree could not "
            f"be created from {roots.target}: {exc}\n"
            "  Placement is opt-out, not best-effort: falling back to the shared "
            f"checkout would grade this run on a tree other sessions also edit. "
            f"Set {SPLIT_ENV}=0 to run in place deliberately."
        ) from exc
    return replace(roots, target=str(ws.path), placement="worktree")
