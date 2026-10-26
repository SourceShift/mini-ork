"""The pre-dispatch filter and the kickoff writer (``rsi.select@v1``).

This is the second cost boundary of a cycle. The queue stops spend before the
lens panel; this module stops spend before the implementer — after the panel
has answered, and before any worktree exists.

Two jobs, both pure:

1. **Filter.** A panel can propose a file that does not exist, a frozen path, a
   six-file refactor, or a fix whose success cannot be measured. Discovering
   any of that after dispatch wastes the child's spend, so every check here
   runs first and a rejection records exactly one code and one sentence.
2. **Write the kickoff.** The accepted proposal becomes a ``framework-edit``
   kickoff. ``kickoff_lint`` treats even a warning as failure in this pipeline,
   so the writer emits both section vocabularies the lint checks — the contract
   sections and the recipe example's headings — and a test pins that the
   emitted text lints with zero findings.

The avoidance checks (already claimed / already failed / already merged) are
delegated to :func:`mini_ork.rsi.queue.avoidance` rather than reimplemented.
One implementation means "already failed" cannot mean two things at the two
boundaries a cycle crosses.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from mini_ork.rsi.candidates import Candidate
from mini_ork.rsi.harvest import (
    LEARNING_RULES,
    ROUTING_RULES,
    SYNTHETIC_PREFIX,
    VERIFICATION_RULES,
)
from mini_ork.rsi.probes import default_specs
from mini_ork.rsi.queue import LedgerView, QueueConfig, avoidance
from mini_ork.vcs.repo_integrity_guard import frozen_hits

__all__ = [
    "CODE_ALIASES",
    "DEFAULT_ALLOW_PREFIXES",
    "DEFAULT_MAX_FILES",
    "DENY_PREFIXES",
    "Proposal",
    "SELECT_CODES",
    "SelectConfig",
    "SelectContext",
    "Selection",
    "build_kickoff",
    "normalize_repo_path",
    "paths_overlap",
    "read_claims",
    "registered_success_commands",
    "select",
    "write_selection",
]

#: Fix scope is bounded so a candidate cannot become a rewrite.
DEFAULT_MAX_FILES = 5

#: Where a fix may land. Everything else is rejected *before* dispatch; the
#: gate stack checks the same surface again on the real diff (G4).
DEFAULT_ALLOW_PREFIXES: tuple[str, ...] = (
    "mini_ork/",
    "recipes/",
    "tests/",
    "config/",
    "db/migrations/",
    "scripts/",
    "docs/",
)

#: Paths a cycle may never propose, whatever the allowlist says: the code that
#: validates the cycle. An implementer that can edit G1–G11 can pass its own
#: review. ``config/kernel-frozen.yaml`` freezes a specific list repo-wide;
#: this is the cycle-local, unconditional floor, and it is deliberately not
#: configurable — an operator who wants a cycle to edit the gate does it by
#: hand, with the gates off.
DENY_PREFIXES: tuple[str, ...] = ("mini_ork/rsi/",)

#: Probe ids accepted as success commands although :func:`probes.default_specs`
#: only builds them conditionally (``tests`` needs a configured pytest path).
#: ``lane-preflight`` is deliberately absent: it was never built by any runner,
#: and its health check is a provider network probe — a config naming it used to
#: be accepted here and then fail the run at startup.
_EXTRA_PROBES: frozenset[str] = frozenset({"tests"})

#: The queue names a merged candidate ``already_merged``; the plan's select
#: table calls the same state ``already_fixed``. Map once, at the boundary, so
#: the ledger records one vocabulary.
CODE_ALIASES: Mapping[str, str] = {"already_merged": "already_fixed"}

SELECT_CODES: tuple[str, ...] = (
    "candidate_not_in_harvest",
    "panel_invented_file",
    "bad_path",
    "directory_scope",
    "empty_scope",
    "frozen_path",
    "self_edit_of_gate",
    "blast_radius",
    "outside_allowlist",
    "missing_success_command",
    "already_fixed",
    "already_claimed",
    "failed_signature",
    "low_severity",
    "worktree_claim_conflict",
)

_ABSOLUTE_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|~)")
_GLOB_CHARS = ("*", "?", "[")


# --------------------------------------------------------------------------
# Config + context
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectConfig:
    """Filter knobs. ``queue`` carries the avoidance policy shared with the queue."""

    max_files: int = DEFAULT_MAX_FILES
    allow_prefixes: tuple[str, ...] = DEFAULT_ALLOW_PREFIXES
    queue: QueueConfig = field(default_factory=QueueConfig)

    def __post_init__(self) -> None:
        if self.max_files < 1:
            raise ValueError("max_files must be >= 1")
        if not self.allow_prefixes:
            raise ValueError("allow_prefixes must not be empty (fail closed)")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "SelectConfig":
        """Build from one cycle-config mapping; unknown keys are ignored here."""
        data = dict(raw or {})
        prefixes = data.get("allow_prefixes")
        return cls(
            max_files=int(data.get("max_files", DEFAULT_MAX_FILES)),
            allow_prefixes=(
                tuple(str(p) for p in prefixes) if prefixes else DEFAULT_ALLOW_PREFIXES
            ),
            queue=QueueConfig.from_mapping(data),
        )


@dataclass(frozen=True, slots=True)
class SelectContext:
    """Everything the filter needs about the repo and the ledger.

    Injected rather than read so the filter stays pure: the CLI resolves
    frozen paths, claims, the ledger view and probe status once per cycle.
    """

    root: Path | str
    config: SelectConfig = field(default_factory=SelectConfig)
    #: ``(slug, path)`` pairs from the worktree claim registry.
    claims: tuple[tuple[str, str], ...] = ()
    #: ``fnmatch`` patterns from ``enforced_frozen_paths()``. Fail closed: the
    #: caller passes the enforced union, never an empty list.
    frozen_paths: tuple[str, ...] = ()
    view: LedgerView = field(default_factory=LedgerView)
    #: ``probe_id -> status`` from this cycle's probe run.
    probe_status: Mapping[str, str] = field(default_factory=dict)
    #: ``None`` resolves the registry lazily; tests pass an explicit set.
    success_commands: frozenset[str] | None = None
    now: int = 0


# --------------------------------------------------------------------------
# The proposal — what the SUGGEST node submitted
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Proposal:
    """A panel's answer, normalised.

    ``from_mapping`` tolerates the field names the lens/suggest prompts actually
    emit (``change_summary`` vs ``summary``, ``files_to_touch`` vs ``files``),
    because a shape mismatch should surface as a filter code, not a crash.

    ``new_files`` is the one place a path that does not exist yet is legitimate:
    a fix whose correct shape *is* a new file — a migration, a new test module.
    Keeping it separate from ``files`` is what keeps ``panel_invented_file``
    honest, because every path in ``files`` must exist today.
    """

    candidate_id: str
    files: tuple[str, ...] = ()
    new_files: tuple[str, ...] = ()
    summary: str = ""
    root_cause: str = ""
    ac_ids: tuple[str, ...] = ()
    do_not_touch: tuple[str, ...] = ()
    #: Optional override; defaults to ``candidate.success_command``.
    verification_command: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Proposal":
        def _first(*keys: str) -> Any:
            for key in keys:
                if raw.get(key) not in (None, ""):
                    return raw[key]
            return None

        files_raw = _first("files", "files_to_touch", "scope_files") or ()
        if isinstance(files_raw, str):
            files_raw = [files_raw]
        new_raw = _first("new_files", "files_to_create", "create_files") or ()
        if isinstance(new_raw, str):
            new_raw = [new_raw]
        dont_raw = _first("do_not_touch", "constraints") or ()
        if isinstance(dont_raw, str):
            dont_raw = [dont_raw]
        ac_raw = _first("ac_ids", "acceptance_ids") or ()
        if isinstance(ac_raw, str):
            ac_raw = [ac_raw]
        return cls(
            candidate_id=str(raw.get("candidate_id") or ""),
            files=tuple(str(f) for f in files_raw),
            new_files=tuple(str(f) for f in new_raw),
            summary=str(_first("summary", "change_summary", "title") or ""),
            root_cause=str(_first("root_cause", "why", "diagnosis") or ""),
            ac_ids=tuple(str(a) for a in ac_raw),
            do_not_touch=tuple(str(d) for d in dont_raw),
            verification_command=str(_first("verification_command", "success_command") or ""),
        )


@dataclass(frozen=True, slots=True)
class Selection:
    """Accepted (carries the kickoff) or rejected (carries code + detail)."""

    candidate_id: str
    accepted: bool
    code: str = ""
    detail: str = ""
    files: tuple[str, ...] = ()
    verification_command: str = ""
    kickoff: str = ""
    kickoff_sha: str = ""

    def __post_init__(self) -> None:
        if self.accepted and self.code:
            raise ValueError("an accepted selection carries no rejection code")
        if not self.accepted and self.code not in SELECT_CODES:
            raise ValueError(f"unknown select code {self.code!r}")
        if self.accepted and not self.kickoff:
            raise ValueError("an accepted selection must carry a kickoff")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "accepted": self.accepted,
            "code": self.code,
            "detail": self.detail,
            "files": list(self.files),
            "verification_command": self.verification_command,
            "kickoff": self.kickoff,
            "kickoff_sha": self.kickoff_sha,
        }


# --------------------------------------------------------------------------
# Path + claim helpers
# --------------------------------------------------------------------------


def normalize_repo_path(raw: str) -> str | None:
    """A cleaned repo-relative path, or ``None`` when it is not one.

    Rejects absolute paths, ``~``, drive letters, ``..`` and globs. A glob is
    rejected on purpose: the gate compares the real diff against the kickoff's
    file list, and a pattern makes that comparison unfalsifiable.
    """
    text = (raw or "").strip().replace("\\", "/")
    if not text or _ABSOLUTE_RE.match(text):
        return None
    if any(ch in text for ch in _GLOB_CHARS):
        return None
    while text.startswith("./"):
        text = text[2:]
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def paths_overlap(a: str, b: str) -> bool:
    """Path-prefix overlap, identical to ``scripts/mini_ork_worktree.py``.

    The script is the enforcement point; this is the pre-dispatch prediction.
    A test imports the script and pins the two implementations together so the
    copy cannot drift.
    """
    a, b = a.strip("/"), b.strip("/")
    if a == b:
        return True
    return (b + "/").startswith(a + "/") or (a + "/").startswith(b + "/")


def read_claims(worktrees_dir: Path | str) -> tuple[tuple[str, str], ...]:
    """Read the claim registry TSV (``slug<TAB>path``). Missing file -> ``()``.

    Unreadable lines are skipped rather than raising: predicting a conflict is
    advisory, and the script re-checks authoritative state on create.
    """
    path = Path(worktrees_dir) / ".ownership"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            out.append((parts[0].strip(), parts[1].strip()))
    return tuple(out)


def registered_success_commands() -> frozenset[str]:
    """Every command a candidate may name as its success check.

    Two families: the ``db:<RULE>`` synthetic probes (one per harvest rule) and
    the real probe ids. A candidate whose ``success_command`` is outside this
    set could not be validated by G8, so it is rejected before dispatch.
    """
    ids = {spec.probe_id for spec in default_specs(root=".")}
    ids |= set(_EXTRA_PROBES)
    for rules in (LEARNING_RULES, ROUTING_RULES, VERIFICATION_RULES):
        ids |= {f"{SYNTHETIC_PREFIX}{rule_id}" for rule_id, _ in rules}
    return frozenset(ids)


# --------------------------------------------------------------------------
# The filter
# --------------------------------------------------------------------------


def _reject(candidate_id: str, code: str, detail: str) -> Selection:
    return Selection(candidate_id=candidate_id, accepted=False, code=code, detail=detail)


def _allowlisted(path: str, prefixes: Sequence[str]) -> bool:
    for prefix in prefixes:
        p = prefix.strip().rstrip("/")
        if path == p or path.startswith(p + "/"):
            return True
    return False


def _probe_now_passes(candidate: Candidate, probe_status: Mapping[str, str]) -> bool:
    """A probe-derived candidate is fixed when its probe no longer reports."""
    source = candidate.source
    if not source.startswith("probe:"):
        return False
    probe_id = source.split(":", 1)[1]
    return probe_status.get(probe_id) == "ok"


def select(
    candidates: Mapping[str, Candidate],
    proposal: Proposal,
    ctx: SelectContext,
) -> Selection:
    """Apply every pre-dispatch check; first failure wins.

    Order is deliberately cheap-to-expensive and, within a class, most-fundamental
    first: identity and severity before paths, paths before side-effect
    prediction. A rejection always names one code — never a list — because the
    ledger and the operator both read one reason.
    """
    cand = candidates.get(proposal.candidate_id)
    if cand is None:
        return _reject(
            proposal.candidate_id,
            "candidate_not_in_harvest",
            "the proposal names a candidate this cycle's harvest did not find",
        )

    cfg = ctx.config
    if cand.severity < cfg.queue.min_severity:
        return _reject(cand.candidate_id, "low_severity",
                       f"severity {cand.severity} < {cfg.queue.min_severity}")

    files: list[str] = []
    declared_new: set[str] = set()
    for raw, is_new in [(f, False) for f in proposal.files] + [
            (f, True) for f in proposal.new_files]:
        clean = normalize_repo_path(raw)
        if clean is None:
            return _reject(cand.candidate_id, "bad_path",
                           f"{raw!r} is not a repo-relative file path")
        if is_new:
            declared_new.add(clean)
        if clean not in files:
            files.append(clean)
    if not files:
        return _reject(cand.candidate_id, "empty_scope",
                       "the proposal names no file to change")

    root = Path(ctx.root)
    for path in files:
        target = root / path
        if target.is_dir():
            # A directory is not a file to change, and claiming one is a
            # prefix claim that collides with every sibling worktree editing
            # any file under it. Name the concrete paths instead.
            return _reject(cand.candidate_id, "directory_scope",
                           f"{path} is a directory; name the files the change touches")
        if path in declared_new:
            if target.exists():
                declared_new.discard(path)  # already there — an ordinary file
            elif not target.parent.is_dir():
                return _reject(cand.candidate_id, "panel_invented_file",
                               f"{path} would be created under a directory that "
                               "does not exist")
        elif not target.exists():
            return _reject(cand.candidate_id, "panel_invented_file",
                           f"{path} does not exist in the target repo")

    hits = frozen_hits(files, list(ctx.frozen_paths))
    if hits:
        return _reject(cand.candidate_id, "frozen_path",
                       f"frozen path(s): {', '.join(hits)}")

    denied = [p for p in files if _allowlisted(p, DENY_PREFIXES)]
    if denied:
        return _reject(cand.candidate_id, "self_edit_of_gate",
                       "the cycle may not edit the code that validates it: "
                       + ", ".join(denied))

    if len(files) > cfg.max_files:
        return _reject(cand.candidate_id, "blast_radius",
                       f"{len(files)} files > max {cfg.max_files}")

    outside = [p for p in files if not _allowlisted(p, cfg.allow_prefixes)]
    if outside:
        return _reject(cand.candidate_id, "outside_allowlist",
                       f"outside the fix surface: {', '.join(outside)}")

    success = proposal.verification_command or cand.success_command
    allowed = ctx.success_commands
    if allowed is None:
        allowed = registered_success_commands()
    if success not in allowed:
        return _reject(cand.candidate_id, "missing_success_command",
                       f"{success!r} is not a registered probe id")

    if _probe_now_passes(cand, ctx.probe_status):
        return _reject(cand.candidate_id, "already_fixed",
                       f"probe {cand.source.split(':', 1)[1]} now reports ok")

    avoid = avoidance(cand, ctx.view, cfg.queue, now=ctx.now)
    if avoid is not None:
        code = CODE_ALIASES.get(avoid.code, avoid.code)
        return _reject(cand.candidate_id, code, avoid.detail)

    for slug, claimed in ctx.claims:
        if slug == f"rsi-{cand.candidate_id}":
            continue
        for path in files:
            if paths_overlap(path, claimed):
                return _reject(cand.candidate_id, "worktree_claim_conflict",
                               f"{path} overlaps '{claimed}' held by '{slug}'")

    kickoff = build_kickoff(cand, proposal, verification_command=success,
                            files=tuple(files), new_files=tuple(sorted(declared_new)))
    return Selection(
        candidate_id=cand.candidate_id,
        accepted=True,
        files=tuple(files),
        verification_command=success,
        kickoff=kickoff,
        kickoff_sha=hashlib.sha256(kickoff.encode("utf-8")).hexdigest(),
    )


# --------------------------------------------------------------------------
# The kickoff writer
# --------------------------------------------------------------------------

_AC_MAX = 5


def _bullets(items: Iterable[str], empty: str = "- (none)") -> str:
    rendered = [f"- {item}" for item in items if str(item).strip()]
    return "\n".join(rendered) if rendered else empty


def build_kickoff(
    candidate: Candidate,
    proposal: Proposal,
    *,
    verification_command: str,
    files: Sequence[str],
    new_files: Sequence[str] = (),
) -> str:
    """Render the implementer kickoff. Deterministic: same input, same bytes.

    Section set is the union of what ``kickoff_lint`` demands from a
    ``framework-edit`` kickoff: the five contract sections, the recipe
    example's extra headings, and an Acceptance body that actually carries AC
    ids. Nothing else is added — an extra heading is not free, since the lint
    warns on every example heading that is missing and the cycle treats a
    warning as a failed node.
    """
    ac_ids = [a for a in proposal.ac_ids if a.strip()][:_AC_MAX]
    if not ac_ids:
        ac_ids = ["AC1", "AC2", "AC3"]
    summary = proposal.summary.strip() or candidate.title
    root_cause = proposal.root_cause.strip() or candidate.detail.strip() or candidate.fix_hint
    noun = "file" if len(files) == 1 else "files"
    constraints = [
        f"Change only the {len(files)} {noun} listed under Files in scope — one diff.",
        f"The gap must stop reproducing: `{verification_command}` reports success.",
        "Keep `ruff check --select F,E9 --line-length 110` clean on every changed `.py` file.",
        "Do not edit this kickoff, `.mini-ork/**`, or the gate code that validates the change.",
    ]
    if proposal.do_not_touch:
        constraints.append("Do not touch: " + ", ".join(proposal.do_not_touch) + ".")

    ac_lines = [
        f"- {ac_ids[0]} `{verification_command}` reports success on the fixed tree "
        "and the same command does not on the unmodified tree.",
        f"- {ac_ids[1] if len(ac_ids) > 1 else 'AC2'} Every changed path appears under "
        "Files in scope, and no other path changes.",
        f"- {ac_ids[2] if len(ac_ids) > 2 else 'AC3'} No new finding appears in "
        "`validate --strict` or `garden`.",
        *[f"- {extra} (as specified by the review panel.)" for extra in ac_ids[3:]],
    ]

    return "\n".join([
        f"# RSI fix: {candidate.title}",
        "",
        f"<!-- generated by rsi.select@v1; candidate {candidate.candidate_id}; "
        f"signature {candidate.signature}. Written once, never edited after launch. -->",
        "",
        "## Goal",
        "",
        summary,
        "",
        f"Domain: **{candidate.domain}** · kind: `{candidate.kind}` · "
        f"severity: {candidate.severity} · source: `{candidate.source}`.",
        "",
        "## Scope Hint",
        "",
        "The fix is confined to the files listed below. Start at the file the "
        f"evidence points to (`{candidate.evidence_key}`).",
        "",
        "## Files in scope",
        "",
        # "(new)" is the literal marker ``kickoff_lint`` recognises to skip its
        # existence check — spell it exactly, or the emitted kickoff warns.
        _bullets(f"`{path}`" + (" (new)" if path in new_files else "")
                 for path in files),
        "",
        "## Out of scope",
        "",
        "- Every path not listed under Files in scope.",
        "- `.mini-ork/**`, `config/kernel-frozen.yaml`, and the RSI gate modules.",
        "- Refactors, renames, and formatting-only churn.",
        "",
        "## Expected Edit",
        "",
        root_cause or "Address the evidence above.",
        "",
        "## Requirements",
        "",
        _bullets(constraints),
        "",
        "## Acceptance criteria",
        "",
        "\n".join(ac_lines),
        "",
        "## Verification command",
        "",
        f"`{verification_command}`",
        "",
        "## Done When",
        "",
        "- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed patch.",
        '- `${MINI_ORK_RUN_DIR}/verdict.json` contains `{"pass": true, ...}`.',
        "",
    ])


def write_selection(selection: Selection, path: Path | str) -> Path:
    """Write an accepted kickoff once. Refuses to overwrite an existing file.

    The kickoff is the contract the gates later compare against; editing it
    after launch would make every gate agree with a different document.
    """
    if not selection.accepted:
        raise ValueError("cannot write a rejected selection")
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"kickoff already written: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(selection.kickoff, encoding="utf-8")
    return target
