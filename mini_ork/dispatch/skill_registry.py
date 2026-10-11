"""Skills: named procedure directories, materialized per node and removed after.

A recipe grants a node skills by name (``skills:`` in ``workflow.yaml``, carried
on the capability envelope as ``MO_SKILLS``). The name is a *reference*; this
module owns the other half — which directory that name refers to, and the rule
that a node sees exactly the skills it was granted, for as long as it runs.

Why materialize at all. A harness reads skills from a fixed directory — Claude
Code reads ``<cwd>/.claude/skills/`` — and has no "here is my skill list" flag.
So a grant has to become a directory in the place the harness looks. Doing that
at the node boundary, rather than once at the run level, is what makes the grant
a guarantee: the reviewer that was not granted the research skill cannot find
it, because it was never there while the reviewer ran.

The directory entry is a **symlink** to the registry's copy, so there is one
source of truth and nothing is copied. Two rules keep that safe inside a
working tree a human also uses:

* never replace something that is already there — a human's own skill of the
  same name wins, and the node simply does not get ours;
* release removes only the links *we* created, and only while they are still
  links to *our* source. A directory a node created in the meantime is its
  output, not our scaffolding, and is left alone.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: Where a harness looks for skills, relative to the directory the node runs in.
SKILL_DIR_RELATIVE = os.path.join(".claude", "skills")


class SkillRegistryError(ValueError):
    """A skill reference cannot be resolved into a directory."""


def skill_source_dirs(env: Mapping[str, str] | None = None) -> list[Path]:
    """Skill registry directories in precedence order (first name wins)."""
    e = os.environ if env is None else env
    dirs: list[Path] = []
    run_dir = str(e.get("MINI_ORK_RUN_DIR") or "").strip()
    home = str(e.get("MINI_ORK_HOME") or "").strip()
    root = str(e.get("MINI_ORK_ROOT") or "").strip()
    if run_dir:
        dirs.append(Path(run_dir) / "config" / "skills")
    if home:
        dirs.append(Path(home) / "config" / "skills")
    if root:
        # The same directory Zed reads, so a skill written once serves both the
        # editor and every node.
        dirs.append(Path(root) / ".agents" / "skills")
    return dirs


def load_skills(env: Mapping[str, str] | None = None) -> dict[str, Path]:
    """``{name: directory}`` for every skill the registries define."""
    skills: dict[str, Path] = {}
    for directory in skill_source_dirs(env):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_dir() and entry.name not in skills:
                skills[entry.name] = entry
    return skills


def target_skills_dir(working_directory: str | Path) -> Path:
    """The ``.claude/skills`` directory inside the checkout a node runs in."""
    return Path(working_directory) / SKILL_DIR_RELATIVE


@dataclass
class MaterializedSkills:
    """The links created for one node. ``release()`` is idempotent."""

    created: list[Path] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def release(self) -> None:
        """Remove only the links we made, and only while they are still ours."""
        while self.created:
            link = self.created.pop()
            try:
                if link.is_symlink():
                    link.unlink()
                # Not a symlink any more: the node wrote something here. That is
                # its output, not our scaffolding, so it stays.
            except OSError as exc:
                sys.stderr.write(f"[skills] could not remove {link}: {exc}\n")

    def __enter__(self) -> MaterializedSkills:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def materialize_skills(
    granted: Iterable[str],
    working_directory: str | Path,
    *,
    registry: Mapping[str, Path] | None = None,
    env: Mapping[str, str] | None = None,
) -> MaterializedSkills:
    """Symlink each granted skill into the node's checkout.

    Returns a handle whose ``release()`` undoes it. A name the registry does not
    define is reported and skipped rather than failing the node — the same
    posture the MCP registry takes with an unconfigured server, so a typo costs
    a warning and not a run.
    """
    names = [str(name).strip() for name in granted if str(name).strip()]
    handle = MaterializedSkills()
    if not names:
        return handle

    sources = dict(registry) if registry is not None else load_skills(env)
    destination = target_skills_dir(working_directory)

    for name in names:
        source = sources.get(name)
        if source is None:
            handle.missing.append(name)
            sys.stderr.write(
                f"[skills] {name!r} is granted but no skill by that name is in "
                f"{', '.join(str(d) for d in skill_source_dirs(env))}\n"
            )
            continue
        link = destination / name
        if link.exists() or link.is_symlink():
            # A human's skill, or another grant's leftover: never overwrite it.
            handle.skipped.append(name)
            sys.stderr.write(f"[skills] {link} already exists; leaving it as it is\n")
            continue
        try:
            destination.mkdir(parents=True, exist_ok=True)
            link.symlink_to(source.resolve())
        except OSError as exc:
            raise SkillRegistryError(f"cannot link skill {name!r} into {destination}: {exc}") from exc
        handle.created.append(link)
    return handle


def node_skill_names(node: Mapping[str, object]) -> tuple[str, ...]:
    """The skill names one workflow node declares (empty when it declares none).

    ``skills:`` accepts either a CSV string or a YAML list; both shapes appear in
    recipes. Shared with the IDE's node inspector so it and ``granted_skills``
    read a node's grants the same way.
    """
    declared = node.get("skills") or ()
    if isinstance(declared, str):
        return tuple(part.strip() for part in declared.split(",") if part.strip())
    if isinstance(declared, (list, tuple)):
        return tuple(str(part).strip() for part in declared if str(part).strip())
    return ()


def granted_skills(
    compiled_workflow: object, node_id: str
) -> tuple[str, ...]:
    """The skills a compiled workflow grants one node (empty when it declares none)."""
    nodes = getattr(compiled_workflow, "nodes", None)
    if not isinstance(nodes, Mapping) or node_id not in nodes:
        return ()
    return node_skill_names({"skills": getattr(nodes[node_id], "skills", ())})
