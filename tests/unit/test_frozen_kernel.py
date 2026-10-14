"""Frozen-kernel boundary: an auto-opened (``origin=auto-triage``) framework-edit
epic may never edit the safety/sandbox/verifier surfaces that bound it.

The self-driving loop opens ``framework-edit`` fix epics on its own, including on
model-made diagnoses. Such a run carries the ``<!-- mini-ork:origin=auto-triage -->``
marker as the first line of its kickoff, and the ``frozen-kernel-guard`` check in
``recipes/framework-edit/verifiers/static-check.py`` refuses it whenever the diff
touches a path in ``config/kernel-frozen.yaml``. The list is the engine's built-in
default ∪ the target's committed ``git show HEAD:config/kernel-frozen.yaml`` — never
the working-tree copy the diff itself edits — so a diff cannot unfreeze a path by
deleting its own entry.

The origin read is fail-closed across three signals: the PRE-RUN snapshot
(``context-pack.v2.json``'s ``kickoff_snapshot``, the contract the run received),
the kickoff file's first line, and a sha drift between the pack's
``kickoff_sha256`` and the file. Rewriting the kickoff afterwards cannot un-mark
the run. A human-authored kickoff (no marker, intact sha) is unaffected, and the
legacy ``ALLOW_HIGH_BLAST_RADIUS`` token can never unlock the new check.

Hermetic: temp git repos + the verifier run as a subprocess, mirroring the fixture
style of ``tests/unit/test_framework_edit_static_check.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "static-check.py"

MARKER = "<!-- mini-ork:origin=auto-triage -->"

FULL_LIST_YAML = """version: 1
paths:
  - config/kernel-frozen.yaml
  - recipes/framework-edit/verifiers/static-check.py
  - mini_ork/vcs/repo_integrity_guard.py
  - mini_ork/runtime/sandbox.py
  - mini_ork/gates/gate_bootstrap.py
  - mini_ork/gates/abstain_gate.py
  - mini_ork/recovery/circuit_breaker.py
  - mini_ork/recovery/escalation_policy.py
  - mini_ork/dispatch/calibration.py
  - mini_ork/recovery/auto_repair.py
  - mini_ork/policies/engine.py
  - mini_ork/dispatch/cost_pause.py
  - mini_ork/stores/safety_events.py
  - mini_ork/observability/bug_report.py
"""

# Captured from the pre-change default (non-framework-edit) renderer. The origin
# marker + redaction live strictly on the framework-edit branch, so the default
# body must stay byte-identical; this golden fails if that ever drifts.
GOLDEN_DEFAULT = (
    "# Bug-promoted epic: T\n\n## Goal\n\nFix the bug noticed by `reviewer` agent "
    "during a prior run:\n\n> T\n\n## Context\n\n- Severity: **high** (confidence "
    "0.90, observed 2x)\n- First noticed by: `reviewer` agent\n- Observed in: "
    "`lib/p`\n- Originating task_class: `code_fix`\n\n## Description\n\nD\n\n"
    "## Suggested fix (from the noticing agent)\n\nF\n\n## Verification commands\n\n"
    "- `python3 -m compileall -q mini_ork`\n- `python3 -m pytest -q "
    "tests/unit/test_mini_ork_epics_py.py tests/unit/test_scheduler_py.py`\n\n"
    "## Done When\n\n- `${MINI_ORK_RUN_DIR}/verdict.json` contains `{ \"pass\": "
    "true }`.\n- The originating noticed_bug fingerprint stops recurring in new "
    "runs.\n\n_Auto-promoted from bug_reports id=7 (fingerprint=abcdef012345)._\n"
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def _base_repo(tmp: Path) -> Path:
    """A minimal git repo holding the frozen/legacy paths the test diffs touch."""
    root = tmp / "target"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    files = {
        "mini_ork/recovery/circuit_breaker.py": "VALUE = 1\n",
        "mini_ork/recovery/retry_hint.py": "VALUE = 1\n",
        "lib/circuit_breaker.sh": "echo 1\n",
        "config/kernel-frozen.yaml": FULL_LIST_YAML,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _apply_edits_in_place(root: Path, edits: dict[str, str]) -> str:
    """Write the edits (framework-edit default: edit the tree in place) and cut
    the unified diff from them. The edits stay applied — that is what a diff the
    implementer already landed looks like to the verifier."""
    for rel, text in edits.items():
        (root / rel).write_text(text)
    return _git(root, "diff")


def _run_verifier(tmp: Path, root: Path, diff: str, *, kickoff: str | None = None,
                  profile_kickoff: str | None = None,
                  profile_path: str | None = None,
                  context_pack: str = "{}\n",
                  context_pack_v2: str | None = None) -> dict[str, str]:
    """Run the verifier against ``diff`` in a run dir staged the way a real run is.

    A run's kickoff reaches the verifier through ``run_profile.json`` ->
    ``kickoff_path`` (the file the runtime actually read); the runtime never writes
    a run-local ``kickoff.md``. ``kickoff=`` stages that fallback file, and
    ``profile_path=`` names a kickoff that is absent on disk (the fallback trigger).
    ``context_pack=`` seeds ``context-pack.json`` — the file the legacy
    high-blast-radius guard reads the ALLOW token from. ``context_pack_v2=`` seeds
    ``context-pack.v2.json`` — the PRE-RUN snapshot (``context_v2.load_pack``) the
    origin test now reads ``kickoff_snapshot`` / ``kickoff_sha256`` from; the two
    pack files are distinct and must not be conflated."""
    run_dir = tmp / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "framework-edit.diff").write_text(diff)
    (run_dir / "context-pack.json").write_text(context_pack)
    if context_pack_v2 is not None:
        (run_dir / "context-pack.v2.json").write_text(context_pack_v2)
    if kickoff is not None:
        (run_dir / "kickoff.md").write_text(kickoff)
    if profile_path is not None:
        (run_dir / "run_profile.json").write_text(
            json.dumps({"kickoff_path": str(profile_path)}) + "\n")
    elif profile_kickoff is not None:
        kpath = tmp / "kickoffs" / "auto" / "epic.md"
        kpath.parent.mkdir(parents=True, exist_ok=True)
        kpath.write_text(profile_kickoff)
        (run_dir / "run_profile.json").write_text(
            json.dumps({"kickoff_path": str(kpath)}) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env.update(MINI_ORK_RUN_DIR=str(run_dir), MINI_ORK_ROOT=str(root))
    subprocess.run([sys.executable, str(VERIFIER)], cwd=root, env=env,
                   capture_output=True, text=True, timeout=180)
    rows: dict[str, str] = {}
    for line in (run_dir / "verifier-static-check.checks.tsv").read_text().splitlines():
        if "\t" in line:
            cid, _desc, passed = line.split("\t", 2)
            rows[cid] = passed
    return rows


# ── frozen-kernel-guard ─────────────────────────────────────────────────────

def test_origin_marked_frozen_path_refused_even_with_allow_token(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    kickoff = f"{MARKER}\n\n# epic\n\nALLOW_HIGH_BLAST_RADIUS\n"
    rows = _run_verifier(tmp_path, root, diff, profile_kickoff=kickoff)
    assert rows["frozen-kernel-guard"] == "false"


def test_origin_marked_cannot_unfreeze_by_deleting_list_entry(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    shrunk = FULL_LIST_YAML.replace("  - mini_ork/recovery/circuit_breaker.py\n", "")
    assert shrunk != FULL_LIST_YAML
    diff = _apply_edits_in_place(root, {
        "config/kernel-frozen.yaml": shrunk,
        "mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n",
    })
    # The working-tree list no longer names the path; HEAD's committed copy and
    # the engine's built-in default still do, so the refusal must still happen.
    assert "circuit_breaker.py" not in (root / "config/kernel-frozen.yaml").read_text()
    rows = _run_verifier(tmp_path, root, diff, profile_kickoff=f"{MARKER}\n\n# epic\n")
    assert rows["frozen-kernel-guard"] == "false"


def test_origin_marked_allowed_path_passes(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/retry_hint.py": "VALUE = 2\n"})
    rows = _run_verifier(tmp_path, root, diff, profile_kickoff=f"{MARKER}\n\n# epic\n")
    assert rows["frozen-kernel-guard"] == "true"


def test_human_kickoff_frozen_path_passes(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    rows = _run_verifier(tmp_path, root, diff, profile_kickoff="# epic (operator authored)\n")
    assert rows["frozen-kernel-guard"] == "true"
    assert rows["high-blast-radius-guard"] == "true"


def test_no_kickoff_file_behaves_as_human(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    rows = _run_verifier(tmp_path, root, diff)
    assert rows["frozen-kernel-guard"] == "true"


def test_human_kickoff_quoting_marker_in_body_is_not_auto(tmp_path: Path) -> None:
    # A real operator kickoff may QUOTE the marker inside its own spec text (this
    # repo's kickoffs do). Only a marker on the FIRST line marks an auto run, so a
    # substring sniff would misclassify — and misclassifying a human run as auto
    # is the failure mode that fired on the guard's first dead version.
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    kickoff = (
        "# epic: harden the frozen kernel\n\n"
        f"Stamp the rendered kickoff with `{MARKER}` as its first line.\n"
    )
    rows = _run_verifier(tmp_path, root, diff, profile_kickoff=kickoff)
    assert rows["frozen-kernel-guard"] == "true"


def test_run_local_kickoff_md_fallback(tmp_path: Path) -> None:
    # When the run has no run_profile.json, a run-local kickoff.md is the
    # documented fallback source — marker on the first line still refuses.
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    rows = _run_verifier(tmp_path, root, diff, kickoff=f"{MARKER}\n\n# epic\n")
    assert rows["frozen-kernel-guard"] == "false"


def test_profile_kickoff_path_absent_falls_back_to_run_file(tmp_path: Path) -> None:
    # A profile naming a kickoff that is not on disk must not abort the sniff: the
    # run-local kickoff.md is then consulted, so an auto run is still refused.
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    rows = _run_verifier(tmp_path, root, diff, kickoff=f"{MARKER}\n\n# epic\n",
                         profile_path=str(tmp_path / "gone.md"))
    assert rows["frozen-kernel-guard"] == "false"


# ── origin read from the pre-run snapshot (context-pack.v2.json) ─────────────

def _pack_v2(**extra: object) -> str:
    """A minimal ``context-pack.v2.json`` body carrying ``extra`` top-level keys."""
    return json.dumps({"version": 2, **extra})


def test_snapshot_marker_refuses_stripped_kickoff_file(tmp_path: Path) -> None:
    # The run RECEIVED an auto kickoff (snapshot starts with the marker) and then
    # deleted the marker from the file on disk to read as human. The snapshot is
    # the pre-run contract, so the refusal must survive the strip.
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    rows = _run_verifier(
        tmp_path, root, diff,
        profile_kickoff="# epic (marker stripped after run start)\n",
        context_pack_v2=_pack_v2(kickoff_snapshot=f"{MARKER}\n\n# epic\n"))
    assert rows["frozen-kernel-guard"] == "false"


def test_sha_drift_refused_and_evidenced(tmp_path: Path) -> None:
    # No marker anywhere, but the pack's recorded sha no longer matches the file:
    # a run that rewrote its own contract gets no human privileges, and the drift
    # is named in the evidence log so the reason is auditable.
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    rows = _run_verifier(
        tmp_path, root, diff,
        profile_kickoff="# epic (operator authored, no marker)\n",
        context_pack_v2=_pack_v2(kickoff_snapshot="# the original contract\n",
                                 kickoff_sha256="0" * 64))
    assert rows["frozen-kernel-guard"] == "false"
    log = (tmp_path / "run" / "verifier-static-check.log").read_text()
    assert "frozen-kernel: kickoff modified since run start" in log


def test_human_intact_sha_still_passes(tmp_path: Path) -> None:
    # A human kickoff whose sha still matches the snapshot is unaffected: the new
    # signals only ever ADD refusals. Frozen path in the diff, human origin → pass.
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    kickoff = "# epic (operator authored)\n"
    sha = hashlib.sha256(kickoff.encode("utf-8")).hexdigest()
    rows = _run_verifier(
        tmp_path, root, diff, profile_kickoff=kickoff,
        context_pack_v2=_pack_v2(kickoff_snapshot=kickoff, kickoff_sha256=sha))
    assert rows["frozen-kernel-guard"] == "true"
    assert rows["high-blast-radius-guard"] == "true"


def test_snapshot_origin_allowed_path_still_passes(tmp_path: Path) -> None:
    # Even an auto-opened run passes when the diff touches no frozen path — the
    # snapshot signal gates on origin, not on the mere presence of a pack.
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/retry_hint.py": "VALUE = 2\n"})
    rows = _run_verifier(
        tmp_path, root, diff,
        profile_kickoff="# epic (marker stripped after run start)\n",
        context_pack_v2=_pack_v2(kickoff_snapshot=f"{MARKER}\n\n# epic\n"))
    assert rows["frozen-kernel-guard"] == "true"


def test_no_pack_keeps_file_only_behaviour(tmp_path: Path) -> None:
    # With no v2 pack the snapshot and sha-drift signals cannot fire: a human
    # kickoff still passes and a marked kickoff file still refuses.
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"mini_ork/recovery/circuit_breaker.py": "VALUE = 2\n"})
    human = _run_verifier(tmp_path, root, diff, profile_kickoff="# epic (human)\n")
    assert human["frozen-kernel-guard"] == "true"
    marked = _run_verifier(tmp_path, root, diff, profile_kickoff=f"{MARKER}\n\n# epic\n")
    assert marked["frozen-kernel-guard"] == "false"


# ── _high_blast_radius_guard ────────────────────────────────────────────────

# The legacy guard reads the ALLOW token from context-pack.json (the file that
# re-embeds the kickoff text), so the token is seeded there, not in kickoff.md.
_ALLOW_PACK = '{"kickoff": "ALLOW_HIGH_BLAST_RADIUS"}\n'


def test_high_blast_guard_human_token_still_unlocks(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"lib/circuit_breaker.sh": "echo 2\n"})
    rows = _run_verifier(tmp_path, root, diff, profile_kickoff="# epic\n",
                         context_pack=_ALLOW_PACK)
    assert rows["high-blast-radius-guard"] == "true"


def test_high_blast_guard_origin_marker_ignores_token(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    diff = _apply_edits_in_place(root, {"lib/circuit_breaker.sh": "echo 2\n"})
    rows = _run_verifier(tmp_path, root, diff, profile_kickoff=f"{MARKER}\n",
                         context_pack=_ALLOW_PACK)
    assert rows["high-blast-radius-guard"] == "false"


# ── promoted-kickoff rendering ──────────────────────────────────────────────

_RENDER_ARGS = dict(title="T", role="reviewer", sev="high", conf=0.9, freq=2,
                    obs_in="lib/p", task_class="code_fix", bid=7, fp="abcdef012345")


def test_render_promoted_kickoff_origin_marker_and_redaction() -> None:
    from mini_ork.observability.bug_report import _render_promoted_kickoff
    out = _render_promoted_kickoff(
        recipe="framework-edit",
        desc="see ALLOW_HIGH_BLAST_RADIUS here",
        fix="and ALLOW_HIGH_BLAST_RADIUS there",
        **_RENDER_ARGS,
    )
    assert out.startswith(MARKER)
    assert "ALLOW_HIGH_BLAST_RADIUS" not in out
    assert out.count("<redacted-allow-token>") == 2


def test_default_recipe_body_is_byte_identical() -> None:
    from mini_ork.observability.bug_report import _render_promoted_kickoff
    out = _render_promoted_kickoff(recipe=None, desc="D", fix="F", **_RENDER_ARGS)
    assert out == GOLDEN_DEFAULT
    assert MARKER not in out
    assert "<redacted-allow-token>" not in out


def test_default_recipe_does_not_redact_or_mark() -> None:
    from mini_ork.observability.bug_report import _render_promoted_kickoff
    out = _render_promoted_kickoff(
        recipe=None, desc="keep ALLOW_HIGH_BLAST_RADIUS", fix="", **_RENDER_ARGS)
    assert MARKER not in out
    assert "<redacted-allow-token>" not in out
    assert "ALLOW_HIGH_BLAST_RADIUS" in out


# ── load_frozen_paths / frozen_hits ─────────────────────────────────────────

def test_load_frozen_paths_missing_file_returns_nonempty_default(tmp_path: Path) -> None:
    from mini_ork.vcs.repo_integrity_guard import FROZEN_PATHS_DEFAULT, load_frozen_paths
    got = load_frozen_paths(str(tmp_path / "does-not-exist"))
    assert got == FROZEN_PATHS_DEFAULT
    assert len(got) == 14


def test_load_frozen_paths_garbage_file_returns_default(tmp_path: Path) -> None:
    from mini_ork.vcs.repo_integrity_guard import FROZEN_PATHS_DEFAULT, load_frozen_paths
    root = tmp_path / "g"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kernel-frozen.yaml").write_text("::: not: [valid: yaml\n\t- x\n")
    assert load_frozen_paths(str(root)) == FROZEN_PATHS_DEFAULT


def test_load_frozen_paths_empty_file_returns_default(tmp_path: Path) -> None:
    from mini_ork.vcs.repo_integrity_guard import FROZEN_PATHS_DEFAULT, load_frozen_paths
    root = tmp_path / "e"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kernel-frozen.yaml").write_text("")
    assert load_frozen_paths(str(root)) == FROZEN_PATHS_DEFAULT


def test_load_frozen_paths_reads_valid_file(tmp_path: Path) -> None:
    from mini_ork.vcs.repo_integrity_guard import load_frozen_paths
    root = tmp_path / "v"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kernel-frozen.yaml").write_text(
        "version: 1\npaths:\n  - a/b.py\n  - c/*.py\n")
    assert load_frozen_paths(str(root)) == ["a/b.py", "c/*.py"]


def test_frozen_hits_matches_globs_in_order() -> None:
    from mini_ork.vcs.repo_integrity_guard import frozen_hits
    assert frozen_hits(["a/b.py", "x/y.py"], ["a/*.py"]) == ["a/b.py"]
    assert frozen_hits(["x/y.py"], ["a/*.py"]) == []


# ── every frozen entry names a real path ────────────────────────────────────

# Forward declarations: frozen while the file they name does not exist yet, so
# the step that adds it cannot weaken the surface. The exception is pinned by
# test_pending_frozen_path_is_a_forward_declaration, which also fails once the
# file lands — so an entry cannot linger here as cover for a stale path.
_PENDING_FROZEN_PATHS = frozenset({
    "mini_ork/recovery/escalation_policy.py",
})


def _matches_in_checkout(root: Path, pattern: str) -> list[str]:
    """Real repo-relative paths a frozen pattern covers, from a checkout."""
    import glob
    if any(c in pattern for c in "*?["):
        return sorted(os.path.relpath(p, root)
                      for p in glob.glob(str(root / pattern), recursive=True))
    return [pattern] if (root / pattern).is_file() else []


def test_every_frozen_entry_matches_a_real_path() -> None:
    """No frozen entry may be a phantom — except an explicitly declared pending one.

    A path that names nothing protects nothing — worse, it reads as protection
    while leaving the surface open. Every entry in both the built-in floor and
    the committed ``config/kernel-frozen.yaml`` must match at least one real file
    in the checkout, unless it is listed in ``_PENDING_FROZEN_PATHS`` — a
    forward declaration frozen BEFORE the file lands, so the step that adds it
    cannot quietly weaken the surface. Any other entry here is a regression."""
    from mini_ork.vcs.repo_integrity_guard import (
        FROZEN_PATHS_DEFAULT,
        load_frozen_paths,
    )
    file_list = load_frozen_paths(str(REPO))
    assert file_list, "config/kernel-frozen.yaml parsed to nothing"
    entries = (set(FROZEN_PATHS_DEFAULT) | set(file_list)) - _PENDING_FROZEN_PATHS
    for pattern in sorted(entries):
        assert _matches_in_checkout(REPO, pattern), (
            f"frozen entry {pattern!r} matches no real path in the checkout")


def test_pending_frozen_path_is_a_forward_declaration() -> None:
    """The one sanctioned exception is present in BOTH lists, and is genuinely
    absent from the tree — so the exception cannot outlive the reason for it."""
    from mini_ork.vcs.repo_integrity_guard import FROZEN_PATHS_DEFAULT, load_frozen_paths
    for pending in _PENDING_FROZEN_PATHS:
        assert pending in FROZEN_PATHS_DEFAULT
        assert pending in load_frozen_paths(str(REPO))
        assert not _matches_in_checkout(REPO, pending), (
            f"pending entry {pending!r} now exists; drop it from _PENDING_FROZEN_PATHS")


def test_escalation_policy_frozen_in_default_and_yaml() -> None:
    """The new entry reaches both the built-in floor and the committed yaml, and
    the sibling auto_repair.py stays frozen alongside it."""
    from mini_ork.vcs.repo_integrity_guard import FROZEN_PATHS_DEFAULT, load_frozen_paths
    floor = set(FROZEN_PATHS_DEFAULT)
    yaml = set(load_frozen_paths(str(REPO)))
    assert "mini_ork/recovery/escalation_policy.py" in floor
    assert "mini_ork/recovery/escalation_policy.py" in yaml
    assert "mini_ork/recovery/auto_repair.py" in floor
    assert "mini_ork/recovery/auto_repair.py" in yaml


def test_frozen_list_yaml_matches_builtin_floor() -> None:
    """The committed list must not silently drop a floor entry: an auto-opened
    diff can edit the file, but the floor is what still refuses, so the two are
    only useful together when they agree."""
    from mini_ork.vcs.repo_integrity_guard import FROZEN_PATHS_DEFAULT, load_frozen_paths
    assert set(FROZEN_PATHS_DEFAULT) <= set(load_frozen_paths(str(REPO)))
