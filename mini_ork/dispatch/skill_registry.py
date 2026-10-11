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

**Sandbox runs copy instead of link.** A symlink carries an absolute host
path; inside a container that bind-mounts only the run's shared drive, the
registry's directory is not mounted, so the link dangles. When the node's
harness is spawned into a workspace (``MO_SANDBOX_SCOPE=agent`` /
``MO_SANDBOX_BACKEND`` other than host), grants are *copied* into the
checkout — the copy rides the bind mount, so it resolves in the container.
Release removes a copy only while it is still the directory we created.

A registry entry is a directory that contains a ``SKILL.md``. Anything else
(a stray directory, a file) is not a skill and is skipped at load time, so a
grant can never materialize something the harness would choke on.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: Where a harness looks for skills, relative to the directory the node runs in.
SKILL_DIR_RELATIVE = os.path.join(".claude", "skills")

#: The file that names a skill directory. Same contract as the Zed editor's
#: skill system (``crates/agent_skills``), so one directory serves both.
SKILL_FILE = "SKILL.md"


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
    """``{name: directory}`` for every skill the registries define.

    A directory without a ``SKILL.md`` is not a skill: it is skipped here
    rather than symlinked into a node's checkout, where the harness would
    find a name it cannot open. ``invalid_skill_entries`` reports those so
    ``mini-ork skills validate`` can name them.
    """
    skills: dict[str, Path] = {}
    for directory in skill_source_dirs(env):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if (
                entry.is_dir()
                and (entry / SKILL_FILE).is_file()
                and entry.name not in skills
            ):
                skills[entry.name] = entry
    return skills


def invalid_skill_entries(env: Mapping[str, str] | None = None) -> list[Path]:
    """Registry directories that look like a skill but have no ``SKILL.md``."""
    bad: list[Path] = []
    for directory in skill_source_dirs(env):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_dir() and not (entry / SKILL_FILE).is_file():
                bad.append(entry)
    return bad


def describe_skill(directory: str | Path) -> dict[str, str]:
    """``{name, description}`` from a skill directory's ``SKILL.md`` frontmatter.

    Stdlib-only parse of the leading ``---`` block: ``name:`` and
    ``description:`` keys, with indented continuation lines folded into the
    value. Missing frontmatter degrades to the directory name and an empty
    description — a listing must not crash on a hand-written skill.
    """
    d = Path(directory)
    name = d.name
    description = ""
    try:
        text = (d / SKILL_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"name": name, "description": description}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        key: str | None = None
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line[:1] not in (" ", "\t") and ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if key == "name" and value:
                    name = value
                elif key == "description":
                    # Strip a yaml block-scalar marker (">", "|", ">-"): the
                    # text is what matters, not the folding style.
                    description = value.lstrip(">|-").strip()
            elif key == "description" and line.strip():
                # Folded/yaml continuation of the description value.
                description = (description + " " + line.strip()).strip()
    return {"name": name, "description": description}


def resolve_materialize_mode(env: Mapping[str, str] | None = None) -> str:
    """``"symlink"`` on host dispatch, ``"copy"`` when the harness is sandboxed.

    ``MO_SKILLS_MODE`` wins outright (an operator pin for odd setups). Otherwise
    a sandboxed harness spawn (``MO_SANDBOX_SCOPE=agent``, or a non-host
    ``MO_SANDBOX_BACKEND``) means the registry's absolute paths are not visible
    inside the workspace, so grants must be copied in.
    """
    e = os.environ if env is None else env
    pinned = str(e.get("MO_SKILLS_MODE") or "").strip().lower()
    if pinned in ("symlink", "copy"):
        return pinned
    if pinned:
        sys.stderr.write(f"[skills] unknown MO_SKILLS_MODE {pinned!r}; ignoring\n")
    if str(e.get("MO_SANDBOX_SCOPE") or "").strip() == "agent":
        return "copy"
    backend = str(e.get("MO_SANDBOX_BACKEND") or "").strip()
    if backend and backend != "host":
        return "copy"
    return "symlink"


def target_skills_dir(working_directory: str | Path) -> Path:
    """The ``.claude/skills`` directory inside the checkout a node runs in."""
    return Path(working_directory) / SKILL_DIR_RELATIVE


@dataclass
class MaterializedSkills:
    """The entries created for one node. ``release()`` is idempotent.

    ``created`` holds symlinks; ``created_copies`` holds directories copied in
    for sandboxed spawns (a copy rides the bind mount; a symlink would dangle).
    ``granted`` mirrors what the caller asked for, so the outcome triple
    (created / missing / skipped) can be recorded per node without re-deriving
    the request.
    """

    granted: list[str] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)
    created_copies: list[Path] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def outcome(self) -> dict[str, list[str]]:
        """The grant outcome for a node event: names only, no paths."""
        return {
            "granted": list(self.granted),
            "created": [p.name for p in self.created] + [p.name for p in self.created_copies],
            "missing": list(self.missing),
            "skipped": list(self.skipped),
        }

    def release(self) -> None:
        """Remove only what we made, and only while it is still ours."""
        while self.created:
            link = self.created.pop()
            try:
                if link.is_symlink():
                    link.unlink()
                # Not a symlink any more: the node wrote something here. That is
                # its output, not our scaffolding, so it stays.
            except OSError as exc:
                sys.stderr.write(f"[skills] could not remove {link}: {exc}\n")
        while self.created_copies:
            tree = self.created_copies.pop()
            try:
                if tree.is_dir() and not tree.is_symlink():
                    shutil.rmtree(tree)
                # Replaced by something else: the node's output, not ours.
            except OSError as exc:
                sys.stderr.write(f"[skills] could not remove {tree}: {exc}\n")

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
    mode: str = "",
) -> MaterializedSkills:
    """Link (or, sandboxed, copy) each granted skill into the node's checkout.

    Returns a handle whose ``release()`` undoes it. A name the registry does not
    define is reported and skipped rather than failing the node — the same
    posture the MCP registry takes with an unconfigured server, so a typo costs
    a warning and not a run. ``mode`` defaults to
    ``resolve_materialize_mode(env)``: symlink on host dispatch, copy when the
    harness itself is spawned into a sandboxed workspace.
    """
    names = [str(name).strip() for name in granted if str(name).strip()]
    handle = MaterializedSkills(granted=names)
    if not names:
        return handle

    resolved_mode = mode or resolve_materialize_mode(env)
    sources = dict(registry) if registry is not None else load_skills(env)
    destination = target_skills_dir(working_directory)

    for name in names:
        source = sources.get(name)
        if source is None or not (source / SKILL_FILE).is_file():
            # Unknown, or a registry entry without a SKILL.md (load_skills
            # filters those; an explicitly passed registry map might not).
            # Either way the harness would get a name it cannot open — the
            # same warning-and-skip posture as a typo.
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
            if resolved_mode == "copy":
                shutil.copytree(source, link)
                handle.created_copies.append(link)
            else:
                link.symlink_to(source.resolve())
                handle.created.append(link)
        except OSError as exc:
            raise SkillRegistryError(
                f"cannot materialize skill {name!r} into {destination} "
                f"({resolved_mode}): {exc}"
            ) from exc
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
