"""Deterministic kickoff checks for the draft-kickoff flow (Zed S7b).

Pure, stdlib-only helpers — ``lint`` returns ``[{"sev", "msg", "fix"}]``
(findings never raise) and ``slug`` derives a filename slug from the ``# Title``
header. The function reuses the kickoff-scope parser from
``mini_ork.orchestration.concord_admission.parse_scope`` (the canonical
``## Files in scope`` reader — same heading-end rule, same backtick + bullet
handling, same commentary stripping) and the recipe-dir resolver from
``mini_ork.planning.recipe_plan.recipe_dir`` (home-first, then engine root),
plus ``find_recipe`` for the "is this recipe registered?" probe.

Every finding dict has the same shape ``mini_ork.recipe_author.validate_spec``
already emits, so the agent's existing card renderer can format findings
with a single ``{glyph} {msg} — {fix}`` formatter (where ``glyph`` is ``⚠``
on warns and ``✗`` on errors). The shapes line up deliberately — the
prior-art lens flagged this as the cheapest reuse path.

This module never raises: a malformed workflow.yaml or a missing recipe
fails open (no finding) rather than blocking the draft.
"""
from __future__ import annotations

import glob as _stdlib_glob
import re
from pathlib import Path
from typing import Any

__all__ = ["lint", "slug"]

#: A markdown ATX heading: up to 3 leading spaces, ``#+``, then the title.
#: MULTILINE so ``^`` / ``$`` anchor at every line, not just start/end of
#: the whole input — the title is rarely the last line of a kickoff.
_HEADING_RE = re.compile(r"^\s{0,3}(#+)\s+(.+?)\s*$", re.MULTILINE)
#: A single ``#`` title (H1 only).
_H1_TITLE_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)
#: Any non-slug character (``-`` and ``[a-z0-9]`` are kept).
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9-]+")
#: The success-section vocabulary, case-insensitive substring match.
_SUCCESS_WORDS: tuple[str, ...] = (
    "success",
    "acceptance",
    "done when",
    "verification",
)
#: The five contract sections every implementer-recipe kickoff must name, each
#: with its accepted heading spellings (case-insensitive exact match at any
#: ``#`` level). ``Done when`` / ``Success criteria`` are Acceptance synonyms,
#: ``Scope`` is a Files-in-scope synonym, ``Non-goals`` an Out-of-scope synonym
#: — the same section vocabulary ``gen_profile`` parses.
_ACCEPTANCE_SYNONYMS: tuple[str, ...] = (
    "acceptance",
    "acceptance criteria",
    "success criteria",
    "done when",
)
_CONTRACT_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Goal", ("goal",)),
    ("Acceptance", _ACCEPTANCE_SYNONYMS),
    ("Files in scope", ("files in scope", "scope")),
    ("Out of scope", ("out of scope", "non-goals")),
    ("Verification command", ("verification command", "verification commands",
                              "proof of success")),
)
#: An acceptance-criteria id like ``AC1``, ``AC-1`` or ``AC 1`` (any case).
_AC_ID_RE = re.compile(r"\bac[- ]?\d+\b", re.IGNORECASE)
#: Hard cap on kickoff length, per the spec.
_MAX_KICKOFF_CHARS = 20_000
#: Real slug cap — the kickoff §``slug`` spec pins ``<= 48`` chars.
_MAX_SLUG_LEN = 48


def slug(markdown: str) -> str:
    """A filename-safe slug derived from the kickoff's ``# Title`` line.

    Lowercase; only ``[a-z0-9-]``; collapses consecutive dashes; trims to
    48 characters (the kickoff spec). Falls back to ``"kickoff"`` when the
    markdown has no title or the title reduces to nothing usable.
    """
    # MULTILINE flag is baked into _H1_TITLE_RE; ``^`` and ``$`` anchor
    # at every line, not just the start / end of the input.
    m = _H1_TITLE_RE.search(markdown or "")
    title = (m.group(1) if m else "") or ""
    s = title.lower()
    s = _SLUG_STRIP_RE.sub("-", s).strip("-")
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        return "kickoff"
    return s[:_MAX_SLUG_LEN].rstrip("-")


def _looks_like_glob_impl(s: str) -> bool:
    """A token is a glob when it carries a wildcard character."""
    return "*" in s or "?" in s or "[" in s


def _recipe_has_implementer(recipe_dir: Path | None) -> bool:
    """True when the recipe's ``workflow.yaml`` declares an implementer node.

    Researcher-only recipes (audit, etc.) return ``False`` so the lint does
    not demand a ``## Files in scope`` section. Malformed YAML fails open too —
    a recipe that is structurally weird must not block the user's draft.
    """
    if recipe_dir is None:
        return False
    wf = recipe_dir / "workflow.yaml"
    if not wf.is_file():
        return False
    try:
        import yaml  # noqa: PLC0415 — defer PyYAML to module call sites
        text = wf.read_text(encoding="utf-8")
        doc = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001
        return False
    nodes = doc.get("nodes") or []
    return any(
        isinstance(n, dict) and n.get("type") == "implementer" for n in nodes
    )


def _example_kickoff_headings(recipe_dir: Path | None) -> list[str]:
    """Headings present in the recipe's example kickoff (in order).

    The kickoff §``mini_ork/kickoff_lint.py`` mandates looking under
    ``examples/*/kickoff.md`` first (the newer recipe-card convention),
    then ``example-kickoff.md`` at the recipe root. Empty list when no
    example is found or the file is unreadable.
    """
    if recipe_dir is None:
        return []
    candidates: list[Path] = []
    examples = recipe_dir / "examples"
    if examples.is_dir():
        candidates.extend(sorted(examples.glob("*/kickoff.md")))
    if not candidates:
        single = recipe_dir / "example-kickoff.md"
        if single.is_file():
            candidates = [single]
    seen: set[str] = set()
    out: list[str] = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            h = _HEADING_RE.match(line)
            if h is None or len(h.group(1)) != 2:
                continue  # only "## " sections; the example's "# Title" is its own
            title = h.group(2).strip()
            key = title.lower()
            if title and key not in seen:
                seen.add(key)
                out.append(title)
    return out


def _heading_present(text: str, heading: str) -> bool:
    """True when ``text`` has a ``## <heading>`` (any level) with that title."""
    needle = heading.strip().lower()
    for line in text.splitlines():
        h = _HEADING_RE.match(line)
        if h is None:
            continue
        if h.group(2).strip().lower() == needle:
            return True
    return False


def _headings(text: str) -> list[str]:
    """Lowercased heading titles in ``text``, in order."""
    out: list[str] = []
    for line in text.splitlines():
        h = _HEADING_RE.match(line)
        if h is not None:
            out.append(h.group(2).strip().lower())
    return out


def _section_body(text: str, synonyms: tuple[str, ...]) -> list[str]:
    """Non-heading lines under the first heading matching a synonym.

    Used to scope the AC-id probe to the Acceptance section's own body so an
    ``AC1`` elsewhere in the kickoff does not satisfy the Acceptance check.
    """
    current = False
    lines: list[str] = []
    for line in text.splitlines():
        h = _HEADING_RE.match(line)
        if h is not None:
            current = h.group(2).strip().lower() in synonyms
            continue
        if current:
            lines.append(line)
    return lines


def _scope_path_exists(project: Path, raw: str) -> bool:
    """A scope path exists when ``project/raw`` resolves, or it matches a glob.

    A literal path is checked with ``Path.exists()``. A glob (``*`` / ``?``
    / ``[``) is checked with ``glob.glob(root_dir=...)`` so it walks the
    whole tree. Trailing ``(new)`` markers (case-insensitive) and any
    commentary stripped by ``parse_scope`` are already gone — this helper
    sees the cleaned token only.
    """
    if _looks_like_glob_impl(raw):
        return bool(_stdlib_glob.glob(raw, root_dir=str(project)))
    candidate = project / raw
    return candidate.exists()


def lint(
    markdown: str,
    *,
    project: Path,
    recipe: str | None = None,
    home: Path | None = None,
) -> list[dict[str, Any]]:
    """Run deterministic kickoff checks; never raises.

    Findings are dicts ``{"sev": "error"|"warn", "msg": str, "fix": str}``.
    The agent's card renderer formats them with ``{glyph} {msg} — {fix}``
    where ``glyph`` is ``⚠`` for warns and ``✗`` for errors — same shape
    as ``mini_ork.recipe_author.validate_spec`` findings, which is the
    deliberate reuse path the prior-art lens flagged.

    Errors: empty or whitespace-only markdown (terminal; emit stops).

    Warns: missing title; missing scope (recipe-implementer recipes only);
    a scope path that does not exist under ``project`` (unless the token
    carries a ``(new)`` marker, case-insensitive); missing success
    section; an example-kickoff heading the user's kickoff doesn't have;
    markdown longer than 20 000 chars; unknown recipe id.

    ``project`` is the path the kickoff's scope paths are validated
    against. ``home`` (when given) is the project's ``.mini-ork`` root
    passed to ``find_recipe`` for the "recipe registered?" probe. The
    recipe's ``workflow.yaml`` is resolved home-first via
    ``mini_ork.planning.recipe_plan.recipe_dir`` — passing the project root
    here so the engine's recipes/``recipes/`` dir is consulted as fallback.
    """
    findings: list[dict[str, Any]] = []

    text = markdown or ""
    if not text.strip():
        findings.append({
            "sev": "error",
            "msg": "The kickoff is empty.",
            "fix": "Write the kickoff before drafting.",
        })
        return findings

    if not _H1_TITLE_RE.search(text):
        findings.append({
            "sev": "warn",
            "msg": "The kickoff has no '# ' title line.",
            "fix": "Start the kickoff with a one-line title.",
        })

    # Imports deferred so unit tests can load this module without spinning
    # up the full mini-ork context (mini_ork is not importable as a lib
    # without its env contract resolved).
    from mini_ork.orchestration.concord_admission import parse_scope
    from mini_ork.planning.recipe_plan import recipe_dir
    from mini_ork.recipes_catalog import find_recipe

    rdir = recipe_dir(recipe, project) if recipe else None

    if recipe and find_recipe(recipe, home) is None:
        findings.append({
            "sev": "warn",
            "msg": f"Recipe `{recipe}` is not registered.",
            "fix": "Use one of the recipes in the Recipe picker, or omit `recipe`.",
        })

    scope_warned = False
    if recipe and _recipe_has_implementer(rdir):
        scope_paths = parse_scope(text)
        # ``Scope`` is an accepted synonym for the Files-in-scope section (AC1);
        # ``parse_scope`` only reads the literal ``Files in scope`` heading, so
        # the section-presence check is heading-based and the path checks stay
        # scoped to what ``parse_scope`` actually parsed.
        has_scope_heading = bool(scope_paths) or any(
            h in ("files in scope", "scope") for h in _headings(text))
        if not has_scope_heading:
            scope_warned = True
            findings.append({
                "sev": "warn",
                "msg": "The kickoff has no '## Files in scope' section.",
                "fix": "Add a '## Files in scope' section listing real paths.",
            })
        if scope_paths:
            new_lines = [ln for ln in text.splitlines() if "(new)" in ln.lower()]
            for raw in scope_paths:
                if any(raw in ln for ln in new_lines):
                    continue  # the run creates this — existence check skipped
                if not _scope_path_exists(project, raw):
                    findings.append({
                        "sev": "warn",
                        "msg": f"Not in the project: {raw}",
                        "fix": "Fix the path, or mark it (new) if the run creates it.",
                    })

    # Contract-section lint (AC1): one warn per missing section, gated on the
    # same condition as the Files-in-scope warn above — recipe given AND the
    # recipe declares an implementer node. Findings stay ``warn`` so no caller
    # starts refusing kickoffs over a missing heading.
    if recipe and _recipe_has_implementer(rdir):
        headings = _headings(text)
        for name, synonyms in _CONTRACT_SECTIONS:
            if name == "Files in scope" and scope_warned:
                continue  # the scope warn above already names this section
            if not any(h in synonyms for h in headings):
                findings.append({
                    "sev": "warn",
                    "msg": f"The kickoff has no '## {name}' section.",
                    "fix": f"Add a '## {name}' section.",
                })
        acceptance_body = _section_body(text, _ACCEPTANCE_SYNONYMS)
        if acceptance_body and not _AC_ID_RE.search("\n".join(acceptance_body)):
            findings.append({
                "sev": "warn",
                "msg": "The Acceptance section has no AC ids (AC1, AC2, …).",
                "fix": "Number the acceptance criteria: AC1, AC2, …",
            })

    has_success = False
    for line in text.splitlines():
        h = _HEADING_RE.match(line)
        if h is None:
            continue
        title_lower = h.group(2).lower()
        if any(word in title_lower for word in _SUCCESS_WORDS):
            has_success = True
            break
    if not has_success:
        findings.append({
            "sev": "warn",
            "msg": "The kickoff has no success section.",
            "fix": "Say how success is checked — ideally a command that exits 0.",
        })

    for heading in _example_kickoff_headings(rdir):
        if not _heading_present(text, heading):
            findings.append({
                "sev": "warn",
                "msg": (
                    f"The {recipe or 'recipe'} examples have a "
                    f"'## {heading}' section."
                ),
                "fix": f"Add a '## {heading}' section to your kickoff.",
            })

    if len(text) > _MAX_KICKOFF_CHARS:
        findings.append({
            "sev": "warn",
            "msg": (
                f"Kickoff is {len(text)} chars long "
                f"(cap {_MAX_KICKOFF_CHARS})."
            ),
            "fix": "Trim the kickoff to the relevant scope and success criteria.",
        })

    return findings