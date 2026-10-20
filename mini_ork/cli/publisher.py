"""Publisher node: artifact-contract delivery + git commit (extracted from cli/execute.py).

Owns the two blocking pre-publish gates (oracle safety gates, panel-verdict
approval for recursive-validate-impl), the source_artifact -> outputs[] copy,
and the strict-child-path implementer commit. Re-exported from
mini_ork.cli.execute for backward compatibility.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from mini_ork.context import context_env, publish_env
from mini_ork.runtime.run_roots import load_run_roots
from mini_ork.verify import levels as _levels


def set_status(db, run_id, new_status):  # late binding — avoids the execute<->publisher cycle
    """Write ``new_status`` and prove it landed.

    ``execute.set_status`` only warns when its retries run out; a terminal
    status that silently fails to persist leaves the run in flight forever.
    Read it back and raise instead (zero-fallback).
    """
    from mini_ork.cli.execute import set_status as _impl  # noqa: PLC0415
    _impl(db, run_id, new_status)
    if not db or not run_id or not os.path.isfile(db):
        return None
    import sqlite3  # noqa: PLC0415
    con = sqlite3.connect(db, timeout=15.0)
    try:
        row = con.execute("SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        con.close()
    if row is not None and row[0] != new_status:
        raise RuntimeError(f"set_status({new_status!r}) for {run_id} did not persist (row says {row[0]!r})")
    return None

# The advisory rubric pre-screen's whole payload (rubric_scoring.build_panel_verdict).
# A file carrying ANY key outside this set has panel-authored content merged in —
# tier4_synth owns panel-verdict.json and writes verdict/reasons/dod_probe_results.
_PRESCREEN_VERDICT_KEYS = frozenset(
    {"panel_score", "pass", "source", "task_class", "scale", "prescreen"}
)


def is_rubric_prescreen(path) -> bool:
    """True when ``path`` is the advisory rubric pre-screen's score file.

    The rubric writes ``panel-verdict.json`` with ``source: rubric-prescreen``
    — the same name a real panel gate uses. It records a score; it never
    approves a commit, satisfies the panel approval gate, or replaces the run
    verdict (K0.5b AC1).

    The pre-screen MERGES (``setdefault``) into a verdict the panel already
    wrote, so the ``source: rubric-prescreen`` stamp lands on a real panel
    verdict that carries no ``source`` of its own. Keying on ``source`` alone
    then reads an APPROVED panel as "no panel verdict" and refuses the publish
    (runs -1791790859-83222--…--D1/D3: verdict APPROVE, every gate green,
    blocked "missing panel verdict" because the prescreen had stamped the
    file). A *pure* prescreen holds only ``build_panel_verdict``'s keys;
    anything more is the panel's verdict and must be honored.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("source") != "rubric-prescreen":
        return False
    return set(data) <= _PRESCREEN_VERDICT_KEYS


def _recipe_root(root):
    """Base dir for recipe assets. ``main.py`` resolves a recipe against the
    consumer's ``MINI_ORK_HOME`` overlay and threads the winning base via
    ``MINI_ORK_RECIPE_ROOT``; honor it so the artifact contract read here is the
    SAME copy the workflow and verifier scripts were loaded from. Without this
    the engine checkout's copy wins, so a consumer overlay that fixes its
    contract (this recipe's stale list-valued ``source_artifact`` → TypeError in
    ``_envsubst``, i.e. the whole run failing its ``exit_code !== 0`` gate while
    the synthesis and both verifiers pass) never takes effect."""
    return os.environ.get("MINI_ORK_RECIPE_ROOT") or root


def _envsubst(s):
    """B2-C: envsubst-equivalent — expand $VAR / ${VAR} from the environment, BLANKING
    unset vars (os.path.expandvars leaves them literal, which commits garbage
    ${VAR}-in-path files, the OSS-leak-garbage class the bash guard prevents)."""
    return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                  lambda m: context_env(m.group(1) or m.group(2)), s)



_PUBLISH_ABSTAIN_FILE = "publish-abstain.json"


def _record_publish_abstain(run_dir, reason, detail):
    """``publish-abstain.json``: the approved change was NOT committed, and why.

    The caller reads it to keep the run's status honest (``failed``, not
    ``published``) — a run whose authored patch could not be attributed or no
    longer applies has delivered nothing.
    """
    if not run_dir:
        return
    try:
        with open(os.path.join(run_dir, _PUBLISH_ABSTAIN_FILE), "w", encoding="utf-8") as fh:
            json.dump({"reason": reason, "detail": detail}, fh, indent=1)
    except OSError:
        pass


def _note(db, run_id, note):
    """Append ``note`` to ``task_runs.notes``; never masks the publish refusal."""
    if not db or not run_id or not os.path.isfile(db):
        return
    from mini_ork.verify import probe_validity as _pv  # noqa: PLC0415
    _pv.record_note(db, run_id, note)


# The markers an attempt writes for a revise/retry loop. They describe the
# attempt that produced them, so a publish archives them instead of leaving a
# dead attempt's narrative at the run-dir root (RB-16).
_ATTEMPT_ARCHIVE_FILES = ("NEEDS-CHANGE.md", "retry-hint.json", "retry-gate.json")


def _current_attempt_number(db, run_id) -> int:
    """The attempt this run dir belongs to, from ``attempt <n> started`` notes.

    The live-status write appends that marker once per attempt; a row with no
    marker is still on attempt 1.
    """
    n = 0
    try:
        import sqlite3  # noqa: PLC0415
        con = sqlite3.connect(str(db), timeout=5.0)
        try:
            row = con.execute(
                "SELECT notes FROM task_runs WHERE id = ?", (str(run_id),)).fetchone()
            notes = str((row or [""])[0] or "")
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — archiving is best effort
        return 1
    for segment in notes.split("; "):
        parts = segment.strip().split(" ", 3)
        if (len(parts) >= 3 and parts[0] == "attempt" and parts[1].isdigit()
                and parts[2] == "started"):
            n = max(n, int(parts[1]))
    return n or 1


def _archive_stale_attempt_markers(run_dir, db, run_id) -> None:
    """Move a superseded attempt's markers under ``attempts/<n>/``.

    A revived run dir still holds the dead attempt's ``NEEDS-CHANGE.md`` /
    retry hints; the next grader reads them as the live attempt's evidence.
    Best effort — archiving must never turn a publish into a failure.
    """
    if not run_dir or not os.path.isdir(run_dir):
        return
    target = os.path.join(run_dir, "attempts", str(_current_attempt_number(db, run_id)))
    for name in _ATTEMPT_ARCHIVE_FILES:
        src = os.path.join(run_dir, name)
        if not os.path.isfile(src):
            continue
        try:
            os.makedirs(target, exist_ok=True)
            os.replace(src, os.path.join(target, name))
        except OSError:
            pass


def _run_verifier_envelopes(run_dir):
    """``(stem, envelope)`` for every readable ``verifier_<stem>.json`` at the run root.

    Read with :func:`mini_ork.verify.levels.read_single_verifier_payload`: the
    file may carry stderr prose before the envelope, and only the file's own
    single object is that verifier's verdict (a merged multi-object log is
    ``None``, i.e. unreadable evidence, never an implicit pass).
    """
    if not run_dir:
        return []
    try:
        names = sorted(name for name in os.listdir(run_dir)
                       if name.startswith("verifier_") and name.endswith(".json"))
    except OSError:
        return []
    out = []
    for name in names:
        envelope = _levels.read_single_verifier_payload(os.path.join(run_dir, name))
        if isinstance(envelope, dict):
            out.append((name[len("verifier_"):-len(".json")], envelope))
    return out


def _publish_truth_refusal(run_dir):
    """``(reason, detail)`` when the run's own evidence says it must not publish.

    RB-02 layer 4 / RB-14: the publisher shipped on the reviewer verdict alone,
    so a run whose recipe ``verdict.json`` said ``pass: false`` (7 published
    runs) or whose gating verifier failed still committed. A non-gating
    ``pass: false`` is advisory and exempt; an abstaining verifier
    (``failure_kind``) is the abstention gate's business, not a failed verdict.
    """
    if not run_dir:
        return None
    try:
        with open(os.path.join(run_dir, "verdict.json"), encoding="utf-8") as fh:
            recipe_verdict = json.load(fh)
    except (OSError, ValueError):
        recipe_verdict = None
    if isinstance(recipe_verdict, dict) and recipe_verdict.get("pass") is False:
        return ("recipe verdict.json pass=false",
                "the recipe's own verdict.json declares pass: false")
    for stem, envelope in _run_verifier_envelopes(run_dir):
        if envelope.get("pass") is not False:
            continue
        # Only an explicit gating:false exempts — a missing key is gating.
        if envelope.get("gating") is False:
            continue
        if _levels.abstain_kind(envelope):
            continue
        return (f"verifier {stem} pass=false",
                f"verifier_{stem}.json declares pass: false and is gating")
    return None


def _workflow_has_reviewer(root, recipe):
    """True when the recipe's workflow declares a reviewer-type node.

    A reviewer-less recipe has no approval to wait for: its verifier envelopes
    are the approval (docs run-1792397274-39629). Unreadable workflow data
    fails closed — without proof that no reviewer exists, one is required.
    """
    if not recipe:
        return True
    for base in (_recipe_root(root), root):
        workflow = os.path.join(base or "", "recipes", recipe, "workflow.yaml")
        if not os.path.isfile(workflow):
            continue
        try:
            import yaml  # noqa: PLC0415
            data = yaml.safe_load(open(workflow, encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — unreadable workflow fails closed
            return True
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            return True
        for node in nodes:
            if isinstance(node, dict) and str(
                    node.get("type") or node.get("node_type") or "").strip().lower() == "reviewer":
                return True
        return False
    return True


def _mark_published(db, run_id):
    """Set ``status='published'`` AND ``verdict='APPROVE'`` in one UPDATE (RB-28).

    The publisher used to write only the status, so ``task_runs.verdict`` was
    NULL on all 799 published rows and
    :func:`mini_ork.orchestration.active_state_index._established_facts`
    (``WHERE verdict = 'APPROVE'``) never had a fact to show the planner. The
    two columns move together so a published row can never be verdict-less.
    Terminal stamps match ``execute.set_status``.
    """
    if not db or not run_id or not os.path.isfile(db):
        # No DB to write: keep the status-only shim's behavior (and its
        # late-bound override in tests).
        set_status(db, run_id, "published")
        return None
    import sqlite3  # noqa: PLC0415
    import time  # noqa: PLC0415
    now = int(time.time())
    con = sqlite3.connect(db, timeout=15.0)
    try:
        con.execute("PRAGMA busy_timeout = 15000")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "UPDATE task_runs SET status = 'published', verdict = 'APPROVE', "
            "updated_at = ?, ended_at = COALESCE(ended_at, ?), "
            "duration_ms = CASE WHEN COALESCE(duration_ms, 0) = 0 "
            "THEN MAX(COALESCE(ended_at, ?) - created_at, 0) * 1000 "
            "ELSE duration_ms END WHERE id = ?",
            (now, now, now, run_id))
        con.commit()
        row = con.execute(
            "SELECT status, verdict FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        con.close()
    if row is not None and tuple(row) != ("published", "APPROVE"):
        raise RuntimeError(
            f"_mark_published({run_id!r}) did not persist (row says {tuple(row)!r})")
    return None


def _publisher_try_commit_files(root, target_repo, run_dir, review_file, verdict_env,
                                recipe, node_desc, run_id):
    """Commit the run's ACCEPTED, AUTHORED patch in place on reviewer APPROVE.

    The commit carries only the change the run itself authored — a recovery carry
    patch, a fresh run's implementer Write/Edit calls replayed from its session
    transcript, or (text/codex lanes) the implementer's own emitted diff
    (:mod:`mini_ork.cli.publisher_authored_patch`). It is landed through a
    PRIVATE index parented on the CURRENT HEAD, so the shared `.git/index` is never
    staged into and a peer's in-window edit in the same file is never swept in (the
    old whole-file `git add` did exactly that: run `ide-orca-b2b-story-20261008113340`
    committed a peer's `ThrottledSpinExt` work). Hunks the authored patch does not
    carry are listed in `publish-foreign-hunks.txt` and left untouched. Returns True
    on commit, False on skip/abstain."""
    def log(msg):
        print(msg, file=sys.stderr, flush=True)
    verdict = ""
    candidates = []
    if run_dir:
        for name in ("panel-verdict.json", "review-verdict.json"):
            p = os.path.join(run_dir, name)
            if os.path.isfile(p) and not is_rubric_prescreen(p):
                candidates.append(p)
    if review_file and os.path.isfile(review_file):
        candidates.append(review_file)
    for p in candidates:
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        seen = str(data.get("verdict", "")).strip().lower()
        if data.get("pass") is True or seen in {"approve", "approved", "pass"}:
            verdict = "approve"
            break
        # Keep a non-approving verdict for the skip message below. Reporting
        # '<none>' when a reviewer DID return verdict='fail' reads as "no verdict
        # found at all" and sends the reader hunting a missing file that is present.
        if seen and not verdict:
            verdict = seen
    if verdict != "approve" and review_file and root:
        try:
            out = subprocess.check_output(
                ["python3", os.path.join(root, "lib", "extract_verdict.py"), review_file],
                stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip().lower()
            if out in {"approve", "approved", "pass"}:
                verdict = "approve"
        except Exception:
            pass
    if verdict != "approve" and (verdict_env or "").strip().lower() in {"approve", "approved", "pass"}:
        verdict = "approve"
    if verdict != "approve":
        disp = verdict or (verdict_env or "").strip() or "<none>"
        msg = f"reviewer verdict (resolved: '{disp}') is not APPROVE — no commit"
        log(f"  [skip-publish] {msg}")
        # A skip with no abstain record used to leave the caller to mark the run
        # "published" with nothing committed (RB-14, docs run-1792397274-39629).
        _record_publish_abstain(run_dir, "publish-no-approval", msg)
        return False
    if not target_repo:
        msg = "no target_repo resolved (MO_TARGET_CWD empty and git toplevel failed)"
        log(f"  [skip-publish] {msg}")
        _record_publish_abstain(run_dir, "publish-no-target", msg)
        return False
    from mini_ork.cli import publisher_authored_patch as _pap  # noqa: PLC0415
    authored = _pap.resolve_authored_patch(run_dir, repo=target_repo)
    if isinstance(authored, _pap.Abstain):
        log(f"  [skip-publish] abstain {authored.reason}: {authored.detail}")
        _record_publish_abstain(run_dir, authored.reason, authored.detail)
        return False
    if authored.source.startswith("declared-files"):
        # No authorship evidence at all (the M1 empty-outputs in-place path): the
        # patch is the run's declared files taken whole, so a peer's in-window hunk
        # inside one of them cannot be separated. Say so — do not let it look like
        # an authored patch.
        log("  [warn] publisher: no authored source (carry patch, transcript replay or "
            "implementer diff) — committing the run's declared files_changed")
    # Cross-check (kickoff §3): changed lines in `git diff pre-implementer-ref`
    # that the authored patch does not carry are a peer's in-window edit. List
    # them; never commit them, never revert them.
    foreign = _pap.report_foreign_hunks(target_repo, run_dir, authored.patch_text)
    if foreign:
        log(f"  [warn] publisher: {len(foreign)} foreign hunk(s) left uncommitted "
            f"(publish-foreign-hunks.txt)")
    msg = f"mini-ork({recipe}): {node_desc} [run {run_id}]"
    landed = _pap.land_patch(target_repo, authored.patch_text, "", message=msg)
    if isinstance(landed, _pap.Abstain):
        log(f"  [skip-publish] abstain {landed.reason}: {landed.detail}")
        _record_publish_abstain(run_dir, landed.reason, landed.detail)
        return False
    # Keep the `[publish] committed N file(s): <sha>` shape: run pages and the
    # flow map parse it (ide_pages/run_flow.py `_PUBLISH_RE`).
    n_files = len(_pap._paths_in_patch(authored.patch_text))
    log(f"  [publish] committed {n_files} file(s): {landed} "
        f"(authored patch: {authored.source})")
    return True


def _kickoff_guard(run_dir, db, run_id):
    """Kickoff guard (MO_KICKOFF_GUARD=off|shadow|on, default shadow): a run that
    edited or deleted its own kickoff cannot publish. ``shadow`` records what it
    would block (kickoff-guard.json + a task_runs note) and never blocks; ``on``
    refuses and restores the kickoff from the pre-run snapshot. Returns the
    publisher's ``(rc, finish_reason)`` when blocking, else ``None``."""
    from mini_ork.verify import kickoff_guard as _kg  # noqa: PLC0415
    gate_mode = _kg.mode()
    if gate_mode == "off" or not run_dir:
        return None
    ok, reason, _report = _kg.publish_gate(run_dir, gate_mode=gate_mode)
    if ok:
        return None
    from mini_ork.verify import probe_validity as _pv  # noqa: PLC0415
    if gate_mode == "shadow":
        _pv.record_note(db, run_id, f"[shadow] kickoff guard would block: {reason}")
        return None
    print(f"  [BLOCK] kickoff-guard: {reason} — the run changed its own contract; "
          "kickoff restored, publish refused (kickoff-guard.json)")
    _pv.record_note(db, run_id, f"kickoff_guard: {reason}")
    return 1, "verdict_fail"


def _outputs_from_contract(d):
    """outputs[] entries as ``(path, required)`` tuples.

    A bare string or a mapping without ``required`` means required=True
    (fail-closed default — existing recipes keep their behavior). Only an
    explicit ``required: false`` opts out; the schema
    (schemas/artifact_contract.schema.json) allows the flag.
    """
    out: list[tuple[str, bool]] = []
    for o in (d.get("outputs") or []):
        if isinstance(o, dict) and o.get("path"):
            out.append((o["path"], bool(o.get("required", True))))
        elif isinstance(o, str):
            out.append((o, True))
    return out


def _run_local_output_status(dst, out, run_dir, required=True):
    """Classify a run-local publish output.

    Returns ``present`` / ``glob_present`` / ``optional_absent`` / ``missing``.

    Two green-run shapes a strict required-only check got wrong (first
    all-green SDD child, run-1791790859-83222--p1-5-parity-suite-and-docs--D1):

    - ``required: false`` entries (reflector.json / replan.json) are
      failure-loop artifacts; a green pass legitimately produces neither, and
      demanding them failed the publisher on a run where every gate APPROVED.
    - template paths like ``tier4-{family}.md`` name a FAMILY of files
      (tier4-glm.md, tier4-kimi.md, ...). The literal brace path never exists
      on disk; the contract means "the pattern's matches in that directory".
      spec-driven-delivery writes the placeholder as ``<spec_id>`` and puts the
      family in a subdirectory (``spec-cards/<spec_id>.json``); handling only
      braces at the run dir's top level failed the publisher of every SDD
      parent whose children all delivered (run-1791884666-31084).
    """
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return "present"
    if os.path.isdir(dst) and os.listdir(dst):
        return "present"
    base = os.path.basename(out)
    parts = re.split(r"(\{[^{}/]+\}|<[^<>/]+>)", base)

    def _placeholder(part):
        return (part.startswith("{") and part.endswith("}")) or \
            (part.startswith("<") and part.endswith(">"))

    if any(_placeholder(p) for p in parts):
        # fnmatch does NOT expand braces — translate the token → [^/]+ ourselves.
        pattern = "".join("[^/]+" if _placeholder(p) else re.escape(p) for p in parts)
        rx = re.compile(pattern + r"\Z")
        try:
            names = os.listdir(os.path.dirname(dst) or run_dir)
        except OSError:
            names = []
        if any(rx.match(n) for n in names):
            return "glob_present"
    if not required:
        return "optional_absent"
    return "missing"


def _delegated_to_children(db, run_id):
    """True when ``run_id`` spawned child runs, so its implementation was delegated.

    A delegating run owns no repository edits of its own — its children do the
    implementing — so an authored patch it cannot produce is expected, not a
    delivery failure. Returns False (never raises) when the DB is unreadable or
    predates ``run_spawns``: a home that cannot prove delegation keeps the
    strict reading.
    """
    if not db or not run_id or not os.path.isfile(db):
        return False
    import sqlite3  # noqa: PLC0415
    con = sqlite3.connect(db, timeout=15.0)
    try:
        row = con.execute(
            "SELECT 1 FROM run_spawns WHERE parent_run_id = ? LIMIT 1", (run_id,)).fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return row is not None


def publisher_node(root, run_dir, db, run_id, recipe, task_class, review_file="", verdict_env=""):
    """Port of bash publisher branch (:2909-3200). The panel found the port stubbed
    this to `set_status('published')` — this restores the two BLOCKING gates + delivery:
      1. oracle gates (block on safety_violation);
      2. recursive-validate-impl panel-verdict approval gate;
      3. artifact-contract copy source_artifact→outputs + git commit, OR the M1
         empty-outputs in-place implementer commit.
    Returns (rc, finish_reason). Template ${VAR} paths resolved via expandvars
    (envsubst-equivalent)."""
    # ── oracle gates: fire once pre-publish; only a definitive safety_violation blocks
    if os.environ.get("MO_ORACLE_GATES_AUTO", "1") == "1" and run_id and db:
        verdict_file = os.path.join(run_dir, "panel-verdict.json")
        ctx = json.dumps({"panel_run_id": run_id, "recipe": recipe or "unknown",
                          "task_class": task_class or "generic",
                          "verdict_file": verdict_file, "current_round": 1})
        try:
            from mini_ork.gates import gate_bootstrap, gate_registry
            gate_bootstrap.bootstrap_oracle_gates(db=db, root=root)
            result = gate_registry.gate_run_all(
                db,
                task_class or "generic",
                ctx,
                mini_ork_root=root,
            )
            sv = result.get("safety_violation", False)
            if sv is True or str(sv) == "True":
                print("  [BLOCK] oracle-gates: safety_violation — publish refused (COALITION_ABORT or equivalent)")
                return 1, "safety_violation"
            print("  [ok] oracle-gates: pre-publish pass")
        except Exception:
            pass
    # ── recursive-validate-impl requires an approved panel verdict
    if recipe == "recursive-validate-impl":
        pvf = os.path.join(run_dir, "panel-verdict.json")
        # A rubric pre-screen score is not the panel's verdict.
        if not (os.path.isfile(pvf) and os.path.getsize(pvf) > 0) or is_rubric_prescreen(pvf):
            print(f"  [BLOCK] publisher: missing panel verdict at {pvf}", file=sys.stderr)
            return 1, "verdict_fail"
        try:
            data = json.load(open(pvf, encoding="utf-8"))
            ok = data.get("pass") is True or str(data.get("verdict", "")).strip().lower() in {"approve", "approved", "pass"}
        except Exception:
            ok = False
        if not ok:
            print("  [BLOCK] publisher: panel verdict is not approved — publish refused", file=sys.stderr)
            return 1, "verdict_fail"
    # ── publish-truth precondition (RB-02 layer 4 / RB-14) ──────────────────
    # The publisher ships only what was verified, BEFORE any commit on either
    # delivery path (authored patch and outputs[] copy). A recipe verdict of
    # pass:false, or a gating verifier envelope of pass:false, is a refusal even
    # when a reviewer APPROVED: 7 published runs carried exactly that
    # contradiction. Exit 0 so no rollback discards the work left in the tree.
    refusal = _publish_truth_refusal(run_dir)
    if refusal is not None:
        reason, detail = refusal
        print(f"  [skip-publish] {reason}")
        _record_publish_abstain(run_dir, reason, detail)
        _note(db, run_id, f"publisher: {reason} — publish withheld")
        set_status(db, run_id, "failed")
        return 0, "publish_abstain"
    # ── verifier abstention gate (§failure_kind, audit 2026-10-11) ──────────
    # A verifier that could not run its tool abstained. The level-vector gate
    # below cannot catch this: `recursive_validate_impl` declares no required
    # levels, so `publish_decision` returns "publish" without reading evidence.
    # Checked BEFORE that gate so the withhold does not depend on MO_LEVEL_VECTOR
    # or on the publisher being a control descendant of the abstaining verifier.
    abstained = _levels.abstained_kinds(run_dir)
    if abstained:
        print(f"  [ABSTAIN] publisher: a verifier could not run its tool "
              f"({', '.join(abstained)}) — publish withheld")
        _note(db, run_id,
              f"publisher: a verifier could not run its tool ({', '.join(abstained)}) "
              "— publish withheld")
        set_status(db, run_id, "failed")
        return 0, "levels_unverified"
    # ── level-vector gate (MO_LEVEL_VECTOR): publish only when every required
    # level is PROVEN. `refute` fails the run (the rollback it triggers is the
    # right compensation); `abstain` withholds the commit WITHOUT rolling back
    # the edit (rc 0 so fail_count is not bumped — rc≠0 triggers revert_branch,
    # which DESTROYS the abstained edit and breaks test.py's abstention contract).
    if _levels.enabled():
        rep = _levels.level_report(run_dir, task_class)
        detail = " ".join(f"{k}={v}" for k, v in rep["levels"].items())
        decision = rep["levels_decision"]
        if decision == "refute":
            print(f"  [BLOCK] publisher: level vector REFUTED ({detail})")
            return 1, "levels_refuted"
        if decision == "abstain":
            print(f"  [ABSTAIN] publisher: level vector not proven ({detail}) — publish withheld")
            _note(db, run_id,
                  f"publisher: level vector not proven ({detail}) — publish withheld")
            set_status(db, run_id, "failed")
            return 0, "levels_unverified"
        print(f"  [ok] publisher: level gate pass (required={rep['levels_required'] or None})")
    # ── probe validity (I1, MO_PROBE_VALIDITY=1|shadow, default OFF): a verify
    # that proved nothing, an aliased probe, or a probe that already passes on
    # the untouched tree cannot publish. `shadow` evaluates and records what it
    # would block, but never blocks and prints nothing.
    _pv_mode = context_env("MO_PROBE_VALIDITY", "0")
    if _pv_mode in ("1", "shadow"):
        from mini_ork.verify import probe_validity as _pv  # noqa: PLC0415
        roots = load_run_roots(run_dir) if run_dir else None
        target = (roots.target if roots else "") or context_env("MO_TARGET_CWD", "")
        plan_path = context_env("MINI_ORK_PLAN_PATH", "") or (os.path.join(run_dir, "plan.json") if run_dir else "")
        try:
            plan = json.load(open(plan_path, encoding="utf-8")) if plan_path else {}
        except (OSError, ValueError):
            plan = {}
        shadow = _pv_mode == "shadow"
        ok, reason, _report = _pv.publish_gate(run_dir=run_dir, db=db, run_id=run_id, target_repo=target,
                                               plan=plan if isinstance(plan, dict) else {},
                                               gate_mode="shadow" if shadow else "enforce")
        if shadow:
            if not ok:
                _pv.record_note(db, run_id, f"[shadow] would block: {reason}")
        elif not ok:
            print(f"  [BLOCK] probe-validity: {reason} — publish refused (probe-validity.json)")
            _pv.record_note(db, run_id, f"probe_validity: {reason}")
            return 1, "verdict_fail"
        else:
            print("  [ok] probe-validity: pre-publish pass")
    # ── evidence ledger (I5, MO_EVIDENCE_LEDGER=1|shadow, default OFF): an
    # approval is backed only by ledger rows whose tree equals the tree being
    # published. `shadow` evaluates and records what it would block, but never
    # blocks and prints nothing. `0` leaves the publisher byte-identical.
    _el_mode = context_env("MO_EVIDENCE_LEDGER", "0")
    if _el_mode in ("1", "shadow"):
        from mini_ork.verify import evidence_ledger as _el  # noqa: PLC0415
        from mini_ork.verify import probe_validity as _pv  # noqa: PLC0415
        roots = load_run_roots(run_dir) if run_dir else None
        target = (roots.target if roots else "") or context_env("MO_TARGET_CWD", "")
        shadow = _el_mode == "shadow"
        ok, reason, _report = _el.publish_gate(run_dir=run_dir, target_repo=target,
                                               gate_mode="shadow" if shadow else "enforce")
        if shadow:
            if not ok:
                _pv.record_note(db, run_id, f"[shadow] evidence ledger would block: {reason}")
        elif not ok:
            print(f"  [BLOCK] evidence-ledger: {reason} — publish refused (evidence-ledger-gate.json)")
            _pv.record_note(db, run_id, f"evidence_ledger: {reason}")
            return 1, "verdict_fail"
        else:
            print("  [ok] evidence-ledger: pre-publish pass")
    blocked = _kickoff_guard(run_dir, db, run_id)
    if blocked:
        return blocked
    # ── artifact contract
    contract = (os.path.join(_recipe_root(root), "recipes", recipe, "artifact_contract.yaml")
                if recipe else "")
    if not contract or not os.path.isfile(contract):
        # Every gate above passed; there is just nothing to deliver. Leaving the
        # status open made these runs read as in flight forever (K0: libwit's
        # execute-only verified-artifact runs never see the overlay contract,
        # because only `mini-ork run` threads MINI_ORK_RECIPE_ROOT).
        msg = f"publisher: no artifact_contract.yaml at {contract or '(no recipe)'} — nothing delivered"
        print(f"  [warn] {msg}", file=sys.stderr)
        print(f"  [ok] {msg}; gates passed, run finalized as published")
        _archive_stale_attempt_markers(run_dir, db, run_id)
        if db and run_id and os.path.isfile(db):
            from mini_ork.orchestration.run_reaper import finalize_passed  # noqa: PLC0415
            finalize_passed(db, run_id, msg + "; artifact left in the run dir")
        return 0, "done"
    src_name, outputs = "synthesis.md", []
    try:
        import yaml  # noqa: PLC0415
        d = yaml.safe_load(open(contract, encoding="utf-8")) or {}
        if isinstance(d, dict):
            src_name = d.get("source_artifact") or "synthesis.md"
            outputs = _outputs_from_contract(d)
    except Exception:
        pass
    if not outputs:
        # M1 empty-outputs: commit the implementer's in-place edits (code-fix recipes)
        # Prefer the pinned target from run_profile.json["roots"] when present;
        # fall back to MO_TARGET_CWD for legacy run dirs without the record.
        roots = load_run_roots(run_dir) if run_dir else None
        target_repo = (roots.target if roots else context_env("MO_TARGET_CWD", ""))
        if not target_repo:
            try:
                target_repo = subprocess.check_output(
                    ["git", "-C", root or ".", "rev-parse", "--show-toplevel"],
                    stderr=subprocess.DEVNULL).decode().strip()
            except Exception:
                target_repo = root or "."
        print("  [warn] publisher: artifact_contract.yaml has no outputs[] — skipping publish", file=sys.stderr)
        abstain_path = os.path.join(run_dir, _PUBLISH_ABSTAIN_FILE) if run_dir else ""
        if abstain_path and os.path.isfile(abstain_path):
            os.remove(abstain_path)  # a stale record from an earlier attempt
        # A reviewer-less workflow has no approval to wait for: every verifier
        # envelope passing IS the approval (docs run-1792397274-39629). Fail
        # closed on no evidence at all — an unverified run approves nothing.
        commit_verdict_env = verdict_env
        envelopes = _run_verifier_envelopes(run_dir)
        if (not _workflow_has_reviewer(root, recipe or "")
                and envelopes
                and all(envelope.get("pass") is True for _stem, envelope in envelopes)):
            commit_verdict_env = "approve"
        _publisher_try_commit_files(root, target_repo, run_dir, review_file, commit_verdict_env,
                                    recipe or "code-fix", os.environ.get("MINI_ORK_NODE_DESC", "implementer"),
                                    run_id or "local")
        if abstain_path and os.path.isfile(abstain_path):
            # A run that DELEGATED its implementation to child runs, under a
            # contract declaring `outputs: []`, has nothing of its own to
            # publish: that is epic-runner's smoke_shape no-commit path by
            # contract ("outputs: [] so the publisher takes the no-commit
            # path"). Filing it as `failed` reported a green epic —
            # epic_verifier pass, final_reviewer pass 23/23 — as a delivery
            # failure (run-1791785967-43108, 2026-10-12). The abstain still
            # records that nothing was committed; only the status differs.
            if _delegated_to_children(db, run_id):
                msg = ("publisher: the run delegated its implementation to child runs and its "
                       "contract declares outputs: [] — nothing of its own to deliver")
                print(f"  [ok] {msg}", file=sys.stderr)
                _archive_stale_attempt_markers(run_dir, db, run_id)
                if db and run_id and os.path.isfile(db):
                    from mini_ork.orchestration.run_reaper import finalize_passed  # noqa: PLC0415
                    finalize_passed(db, run_id, msg + "; artifact left in the run dir")
                return 0, "done"
            # The approved change could not be committed as the run's own patch:
            # nothing was delivered, so the run is not "published". Exit 0 so no
            # rollback discards the work left in the tree.
            print("  [ABSTAIN] publisher: the authored patch was not committed "
                  f"(see {_PUBLISH_ABSTAIN_FILE}) — publish withheld")
            try:
                with open(abstain_path, encoding="utf-8") as fh:
                    abstain_reason = str(json.load(fh).get("reason") or "")
            except (OSError, ValueError, AttributeError):
                abstain_reason = ""
            note_tail = f" ({abstain_reason})" if abstain_reason else ""
            _note(db, run_id, f"publisher: nothing committed{note_tail} — publish withheld")
            set_status(db, run_id, "failed")
            return 0, "publish_abstain"
        _archive_stale_attempt_markers(run_dir, db, run_id)
        _mark_published(db, run_id)
        return 0, "done"
    # ── copy source_artifact → outputs[] + git-commit each.
    # B2-C: recipe-creator meta-recipe references ${MINI_ORK_DERIVED_RECIPE_NAME}; read
    # it from chosen/recipe_name and export BEFORE resolving templates (bash :3075-3079).
    if not os.environ.get("MINI_ORK_DERIVED_RECIPE_NAME"):
        chosen = os.path.join(run_dir, "chosen", "recipe_name")
        if os.path.isfile(chosen):
            try:
                publish_env({
                    "MINI_ORK_DERIVED_RECIPE_NAME": "".join(open(chosen).read().split())})
            except OSError:
                pass
    src = os.path.join(run_dir, _envsubst(src_name))
    src_is_dir = os.path.isdir(src)
    if not src_is_dir and not os.path.isfile(src):
        print(f"  [warn] publisher: expected source artifact missing: {src}", file=sys.stderr)
        return 1, "error"
    # B1: track copy/commit failures like bash (:3111-3184). A failed copy, or a
    # copy-OK-but-commit-failed with a dirty tree, is a real failure — return 1 and
    # do NOT mark 'published'. A commit that fails with nothing-to-commit (dst already
    # matches) is an OK no-op. Was: swallowed failures + unconditional 'published'.
    published_count = 0
    failed_count = 0
    run_root = os.path.realpath(run_dir)
    for out, out_required in outputs:
        out = _envsubst(out)
        # A BARE relative filename (no directory component) is a run-local
        # artifact the producer wrote into run_dir — e.g. goal-loop's
        # panel-verdict.json. It must resolve against run_dir, NOT `root`:
        # joining a bare name to `root` dumps it at the mini-ork repo root and
        # commits it there (the stray `audit(<recipe>): publish synthesis`
        # pollution that lands on every wave). Relative paths WITH a directory
        # component (docs/refactor/foo.md) stay repo-relative publish targets;
        # absolute ${MINI_ORK_RUN_DIR}/foo paths are already run-local.
        if not os.path.isabs(out) and os.path.dirname(out) == "":
            dst = os.path.join(run_dir, out)
        else:
            dst = os.path.join(root, out)
        dst_real = os.path.realpath(dst)
        run_local = dst_real == run_root or dst_real.startswith(run_root + os.sep)

        # Composite/propose-only recipes publish several heterogeneous files in
        # MINI_ORK_RUN_DIR (for example a diff plus JSON ledger and verdict).
        # Those files are already in their canonical destination: copying the
        # single source_artifact over every output corrupts the artifact set.
        # Preserve each run-local output byte-for-byte and fail closed when the
        # producer omitted one.
        if run_local:
            status = _run_local_output_status(dst, out, run_dir,
                                              required=out_required)
            if status == "present":
                print(f"  [ok] publisher: preserved run-local artifact {out}")
                published_count += 1
            elif status == "glob_present":
                print(f"  [ok] publisher: preserved run-local artifact {out} "
                      "(template matches)")
                published_count += 1
            elif status == "optional_absent":
                print(f"  [ok] publisher: optional artifact absent "
                      f"(green path): {out}")
            else:
                print(f"  [fail] publisher: missing run-local artifact {out}",
                      file=sys.stderr)
                failed_count += 1
            continue
        copied = False
        try:
            if src_is_dir:
                dst_norm = dst.rstrip("/")
                os.makedirs(dst_norm, exist_ok=True)
                copied = subprocess.run(["cp", "-R", src + "/.", dst_norm + "/"],
                                        capture_output=True).returncode == 0
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(src, dst)
                copied = True
        except Exception as e:
            print(f"  [fail] publisher: cp failed for {out}: {e}", file=sys.stderr)
        if not copied:
            print(f"  [fail] publisher: cp failed for {out}", file=sys.stderr)
            failed_count += 1
            continue
        subprocess.run(["git", "add", out], cwd=root, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        commit = subprocess.run(
            ["git", "-c", "user.email=mini-ork@local", "-c", "user.name=mini-ork",
             "commit", "-q", "-m",
             f"audit({recipe or 'unknown'}): publish synthesis from {run_id}\n\n"
             f"Run: {run_id}\nRecipe: {recipe or 'unknown'}\nOutput: {out}\n"
             "Dispatched by mini-ork-execute publisher node (D-037 v0.2-pt9)."],
            cwd=root, capture_output=True)
        if commit.returncode == 0:
            print(f"  [ok] publisher: published {out} (committed)")
            published_count += 1
        else:
            # nothing-to-commit (dst unchanged) is an OK no-op, not a failure.
            status = subprocess.run(["git", "status", "--porcelain", out], cwd=root,
                                    capture_output=True, text=True)
            if not status.stdout.strip():
                print(f"  [ok] publisher: {out} unchanged from prior cycle (no-op commit)")
                published_count += 1
            else:
                print(f"  [warn] publisher: copy OK but commit failed for {out}", file=sys.stderr)
                failed_count += 1
    if failed_count > 0:
        print(f"  [fail] publisher: {failed_count} of {published_count + failed_count} outputs failed",
              file=sys.stderr)
        return 1, "error"
    print(f"  [ok] publisher: {published_count} artifact(s) published")
    _archive_stale_attempt_markers(run_dir, db, run_id)
    _mark_published(db, run_id)
    return 0, "done"


