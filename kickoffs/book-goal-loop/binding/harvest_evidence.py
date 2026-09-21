#!/usr/bin/env python3
"""Deep failure-evidence harvester for the book goal-loop (MO_GOAL_EVIDENCE_CMD).

Invoked as ``python3 harvest_evidence.py <chapter_number>`` (argv, not shell)
inside ``MO_GOAL_TARGET_CWD`` (the researcher worktree). Emits a structured
markdown evidence block on stdout — the deep, per-unit failure signal the
one-line ``chapter_predicate.py`` reason cannot carry. The goal-loop threads
this into the fix child's kickoff as ``{{evidence}}`` (see
``recipes/goal-loop/lib/transforms.py::goal_sweep_plan``).

The point: a caller-schema-guard rejection lands in the DB as an opaque
``mini-ork artifact failed caller-supplied schema guard …`` string, truncated
to 80 chars by the predicate. That gives the fix child STRICTLY LESS signal
than the failing lane itself had — so the child guesses (usually at the prompt,
which is often already correct) and the loop never converges. This script
reconstructs the real picture from four best-effort tiers:

  1. DB      — the full ``book_chapter_lifecycle`` row + UNTRUNCATED last_error.
  2. Quality — the chapter's own gate receipts (``bg_compose_stage_artifact``):
               every final G-Eval verdict with its score + failing axes, so the
               child sees the ATTEMPT LADDER rather than only the last exception,
               plus the tell when a judge-PASSED draft never reached
               ``chapter_commit`` (a later gate blocked it after the judge was
               satisfied — an ordering defect the last_error cannot express).
  3. Sandbox — the newest preserved mini-ork ``verified-artifact`` sandboxes:
               the node that ran, the artifact the lane actually produced (its
               ``##``/``###`` headings + title), and — the smoking gun — whether
               mini-ork's IN-SANDBOX verify PASSED while the host guard rejected.
  4. Source  — for the produced node, the caller-contract requirement from the
               researcher source (``requiredSections`` + ``sectionPolicy``), the
               produced-vs-required heading DELTA, and a pointer to the
               in-sandbox repair-signal code so the child can trace WHY the lane
               never self-corrected.

Every tier is defensive: a tier that cannot resolve prints a ``NOTE:`` and the
harvest continues. Evidence is advisory and must never crash the wave. No secret
lives here; the DB tier reads libpq env vars.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Compose node families that carry a caller-schema guard (the ones a drift can
# wedge). Used only to label the newest sandbox; never to filter it out.
_COMPOSE_NODE_HINT = re.compile(r"^W\d+_", re.IGNORECASE)


def _emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")


# ── Tier 1: DB ───────────────────────────────────────────────────────────────

def _psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "-h", os.environ.get("PGHOST", "REDACTED-INTERNAL-IP"),
            "-p", os.environ.get("PGPORT", "5932"),
            "-U", os.environ.get("PGUSER", "researcher_user"),
            "-d", os.environ.get("PGDATABASE", "researcher_db"),
            "-tA", "-F", "|", "-c", sql,
        ],
        capture_output=True, text=True,
    )


def _tier_db(chapter: str, book: str) -> str | None:
    """Full lifecycle row + untruncated last_error. Returns the last_error text
    (for cross-referencing the sandbox) or None."""
    sql = (
        "SELECT status, coalesce(rubric_status,''), committed_complete, "
        "permanently_failed, degraded, generation_attempts, "
        "coalesce(committed_markdown_length, markdown_length, 0), "
        "coalesce(last_error,'') "
        "FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' AND chapter_number={chapter};"
    )
    proc = _psql(sql)
    _emit("## 1. Live generation-status (book_chapter_lifecycle)")
    _emit()
    if proc.returncode != 0:
        _emit(f"NOTE: db query failed rc={proc.returncode}: {proc.stderr.strip()[:200]}")
        _emit()
        return None
    rows = proc.stdout.strip().splitlines()
    if not rows:
        _emit(f"NOTE: no lifecycle row for chapter {chapter}.")
        _emit()
        return None
    cols = (rows[0].split("|") + [""] * 8)[:8]
    status, rubric, committed, permfail, degraded, attempts, mdlen, lasterr = cols
    _emit("```")
    _emit(f"status               = {status}")
    _emit(f"rubric_status        = {rubric or '(none)'}")
    _emit(f"committed_complete   = {committed}")
    _emit(f"permanently_failed   = {permfail}")
    _emit(f"degraded             = {degraded}")
    _emit(f"generation_attempts  = {attempts}")
    _emit(f"markdown_length      = {mdlen}")
    _emit("```")
    _emit()
    if lasterr:
        _emit("Full `last_error` (untruncated — the predicate only shows the first 80 chars):")
        _emit()
        _emit("```")
        _emit(lasterr[:4000])
        _emit("```")
        _emit()
    return lasterr or None


# ── Tier 1b: why the COMMIT gate refuses ─────────────────────────────────────

# probeId → the module that RAISES it. A chapter blocked on its decomposition
# receipt is usually blocked by a VALIDATOR, not by its prose, and the child
# dispatched to "fix the chapter" will rewrite prose instead of the probe. Naming
# the file is what turns that into a harness fix.
#
# ch6 (2026-09-20) is the exemplar: `decomposition_error_count=1` from
# `undefined_shared_symbol` fired on `"user@example.com"` inside a JSON sample —
# an email read as an `@example.` decorator by the C4 fence-hygiene probe. Every
# regeneration reproduced it, so the chapter could never commit until the probe
# itself was patched (validateFenceHygiene, 16a9c932f).
_DECOMPOSITION_PROBE_SOURCES = {
    "undefined_shared_symbol":
        "server/services/bookGeneration/chapterWritingContract/validateFenceHygiene.ts",
    "mislabeled_fence_language":
        "server/services/bookGeneration/chapterWritingContract/validateFenceHygiene.ts",
    "forbidden_fence_opener":
        "server/services/bookGeneration/chapterWritingContract/validateFenceHygiene.ts",
    "ascii_art_diagram":
        "server/services/bookGeneration/chapterWritingContract/validateFenceHygiene.ts",
    "orphan_list_after_fragment":
        "server/services/blockAudit/probes.ts",
}


def _tier_decomposition_findings(chapter: str, book: str) -> None:
    """The error-severity findings inside ``decomposition_diagnostics``.

    ``decomposition_error_count`` is a scalar that says a chapter is blocked but
    not by what. The diagnostics JSONB holds the findings themselves — probeId,
    message, sha8 of the offending line, blockUuid — which is enough to tell a
    PROSE defect (the chapter's own text) from a HARNESS defect (a probe whose
    predicate is wrong). Emitting them, with the probe's source module, is what
    lets the loop stop patching prose for a probe's false positive.
    """
    sql = (
        "WITH lc AS (SELECT decomposition_diagnostics AS d, "
        "  decomposition_error_count AS errs, "
        "  decomposition_warning_count AS warns, "
        "  coalesce(decomposition_parser_version,'(none)') AS pv, "
        "  coalesce(decomposition_status,'(none)') AS st, "
        "  decomposition_verified_at AS at "
        f"FROM book_chapter_lifecycle WHERE book_uuid='{book}' "
        f"AND chapter_number={chapter}), "
        "findings AS ("
        "  SELECT 'block' AS src, f FROM lc, "
        "    jsonb_array_elements(COALESCE(lc.d->'block_findings','[]'::jsonb)) f "
        "  UNION ALL "
        "  SELECT 'grammar' AS src, f FROM lc, "
        "    jsonb_array_elements(COALESCE(lc.d->'grammar_findings','[]'::jsonb)) f) "
        "SELECT COALESCE(lc.st,'(none)'), COALESCE(lc.errs,-1)::text, "
        "  COALESCE(lc.warns,-1)::text, lc.pv, "
        "  COALESCE(to_char(lc.at,'YYYY-MM-DD HH24:MI:SS'),''), "
        "  COALESCE(f.src,''), COALESCE(f.f->>'severity',''), "
        "  COALESCE(f.f->>'probeId',''), "
        "  COALESCE(f.f->'evidence'->>'line',''), "
        "  COALESCE(f.f->>'blockUuid',''), "
        "  replace(replace(COALESCE(f.f->>'message',''),'|','/'), E'\\n',' ') "
        "FROM lc LEFT JOIN findings f ON TRUE "
        "ORDER BY (f.f->>'severity' = 'error') DESC NULLS LAST, f.src "
        "LIMIT 30;"
    )
    _emit("### Decomposition receipt findings (book_chapter_lifecycle."
          "decomposition_diagnostics)")
    _emit()
    proc = _psql(sql)
    if proc.returncode != 0:
        _emit(f"NOTE: diagnostics query failed rc={proc.returncode}: "
              f"{proc.stderr.strip()[:200]}")
        _emit()
        return
    rows = [r for r in proc.stdout.strip().splitlines() if r]
    if not rows:
        _emit("NOTE: no lifecycle row for this chapter.")
        _emit()
        return
    head = (rows[0].split("|") + [""] * 5)[:5]
    status, errs, warns, pv, verified_at = head
    _emit("```")
    _emit(f"decomposition_status         = {status}")
    _emit(f"decomposition_error_count    = {errs}")
    _emit(f"decomposition_warning_count  = {warns}")
    _emit(f"decomposition_parser_version = {pv}")
    _emit(f"decomposition_verified_at    = {verified_at or '(never)'}")
    _emit("```")
    _emit()
    seen: set[tuple[str, str]] = set()
    errors: list[tuple[str, str, str, str, str, str]] = []
    for row in rows:
        cols = (row.split("|") + [""] * 11)[:11]
        _, _, _, _, _, src, severity, probe, line, block_uuid, message = cols
        if not probe:
            continue
        key = (probe, line)
        if key in seen:
            continue
        seen.add(key)
        if severity == "error":
            errors.append((src, probe, line, block_uuid, message,
                           _DECOMPOSITION_PROBE_SOURCES.get(
                               probe,
                               "server/services/bookGeneration/chapterWritingContract/ "
                               "(grep the probeId)")))
    if not errors:
        _emit("No ERROR-severity findings — the receipt's failures are warnings "
              "only, so the decomposition gate is NOT what refuses this commit. "
              "Read Tier 1b's humanization counts below.")
        _emit()
        return
    _emit(f"**{len(errors)} distinct ERROR-severity finding(s). Each one is "
          "emitted by a VALIDATOR, not by the chapter's prose — check the "
          "probe's predicate against the offending line before regenerating "
          "the chapter:**")
    _emit()
    for src, probe, line, block_uuid, message, module in errors:
        _emit(f"- `{probe}` (severity error) at {line or 'line n/a'}"
              + (f", block {block_uuid}" if block_uuid else ""))
        _emit(f"  - {message or '(no message)'}")
        _emit(f"  - raised by: `{module}`")
    _emit()


def _tier_commit_gate(chapter: str, book: str) -> None:
    """Why ``markTaskCompleted`` refuses the commit, in the gate's own terms.

    The scalar columns of ``book_chapter_lifecycle`` ARE the WHERE of the guarded
    ``committed_complete = true`` UPDATE in ``chapterLifecycleService``
    (``markTaskCompleted``). When that UPDATE matches zero rows the service falls
    through to a "zero rows updated, all gates passed" handler and returns the
    MISLEADING ``reason: 'structural_errors'`` — every JS-level gate did pass, so
    the label blames structure even when the real refusal is the always-on
    ``committedCompleteHumanizationClause`` appended to that WHERE. The chapter
    then re-dispatches, ``runOneChapter`` sees content present, SKIPS
    regeneration, and re-runs the identical refusal forever.

    That clause is an ``EXISTS`` over ``book_rubric_results`` demanding a
    chapter-scope native G-Eval row whose ``humanization_receipt`` binds
    ``content_hash`` — status/profile/skill/prompt/validator/protection fields
    pinned, ``output_content_hash = content_hash``, ``judge_geval_passed`` TRUE.
    With ZERO such rows the EXISTS can never be satisfied and no amount of
    re-generation reaches the commit. Counting them is the knock-out; evaluating
    the row's own predicates tells the child whether anything ELSE also fails.
    """
    _emit("## 1b. Why the commit gate refuses (book_chapter_lifecycle predicates)")
    _emit()
    # The gate's always-on EXISTS pins `rubric.job_id` to the JOB THE LIFECYCLE
    # ROW RESOLVES TO (`c10CompletionGate.resolvedLifecycleJobId`: latest writing
    # contract, else latest generation run behind the generated-book alias). A
    # chapter can therefore carry HUNDREDS of G-Eval rows and still have the gate
    # match none of them — every row belongs to a different job. So count rows
    # under the RESOLVED job specifically, and count the rows that bind the
    # chapter's CURRENT bytes, rather than counting rows in the table at large.
    sql = (
        "WITH lc AS (SELECT * FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' AND chapter_number={chapter}), "
        "pin AS (SELECT COALESCE("
        "  (SELECT wc.job_id FROM book_chapter_writing_contracts wc "
        "    WHERE wc.book_uuid=lc.book_uuid AND wc.chapter_number=lc.chapter_number "
        "    ORDER BY wc.built_at DESC, wc.iteration DESC LIMIT 1), "
        "  (SELECT run.job_id FROM book_identity_aliases a "
        "     JOIN book_generation_runs run ON run.book_id=a.book_id "
        "    WHERE a.alias_kind='generated_book_uuid' "
        "      AND a.alias_value=lc.book_uuid::text "
        "    ORDER BY run.started_at DESC NULLS LAST, "
        "             run.updated_at DESC NULLS LAST LIMIT 1)"
        ") AS job_id FROM lc) "
        "SELECT "
        "(lc.permanently_failed IS NOT TRUE), "
        "(lc.content_hash IS NOT NULL), "
        "COALESCE((lc.content_hash = (SELECT md5(COALESCE("
        "  anchor.properties->>'markdown_content','')) FROM blocks anchor "
        "  WHERE anchor.uuid=lc.chapter_anchor_uuid "
        "    AND anchor.source_type='book_chapter_anchor' "
        "    AND NOT anchor.is_deleted))::text,'null'), "
        "(lc.decomposition_status='pass'), "
        "(lc.decomposition_error_count=0), "
        "(lc.decomposition_tree_hash IS NOT NULL), "
        "(lc.decomposition_parser_version IS NOT NULL), "
        "(lc.receipt_projection_revision = lc.projection_revision), "
        "(lc.decomposition_child_count = (SELECT COUNT(*)::integer FROM blocks child "
        "  WHERE child.source_type='book_chapter' "
        "    AND child.source_id=lc.chapter_uuid::text "
        "    AND NOT child.is_deleted)), "
        "coalesce(lc.rubric_status,''), "
        "coalesce(lc.publication_contract_version,-1), "
        "coalesce(lc.projection_revision,-1), "
        "coalesce(lc.receipt_projection_revision,-1), "
        "COALESCE((SELECT pin.job_id::text FROM pin),'(none)'), "
        "(SELECT COUNT(*)::integer FROM book_rubric_results r "
        "  WHERE r.scope='chapter' AND r.chapter_number=lc.chapter_number "
        "    AND r.judge_prompt_version='j3-geval-v3' "
        "    AND r.job_id=(SELECT pin.job_id FROM pin)), "
        "(SELECT COUNT(*)::integer FROM book_rubric_results r "
        "  WHERE r.scope='chapter' AND r.chapter_number=lc.chapter_number "
        "    AND r.judge_prompt_version='j3-geval-v3'), "
        "(SELECT COUNT(*)::integer FROM book_rubric_results r "
        "  WHERE r.scope='chapter' AND r.chapter_number=lc.chapter_number "
        "    AND r.judge_prompt_version='j3-geval-v3' "
        "    AND r.humanization_receipt IS NOT NULL "
        "    AND r.judge_geval_passed IS TRUE "
        "    AND r.evaluated_content_hash = lc.content_hash), "
        "(SELECT COUNT(*)::integer FROM book_rubric_results r "
        "  WHERE r.scope='chapter' AND r.chapter_number=lc.chapter_number "
        "    AND r.judge_prompt_version='j3-geval-v3' "
        "    AND r.humanization_receipt IS NOT NULL), "
        "coalesce(lc.properties_snapshot->>'operator_retry_requested_at',''), "
        "coalesce(lc.markdown_length,-1) "
        "FROM lc;"
    )
    proc = _psql(sql)
    if proc.returncode != 0:
        _emit(f"NOTE: commit-gate query failed rc={proc.returncode}: "
              f"{proc.stderr.strip()[:200]}")
        _emit()
        return
    rows = proc.stdout.strip().splitlines()
    if not rows:
        _emit(f"NOTE: no lifecycle row for chapter {chapter}.")
        _emit()
        return
    (permfail, has_ch, anchor_ok, dec_pass, dec_errs, has_tree, has_pv,
     receipt_ok, kids_ok, rubric, pcv, proj_rev, recv_rev, pinned_job,
     rows_pinned, rows_any, binding_now, humanized_any, retry_marker,
     mdlen) = ((rows[0].split("|") + [""] * 20)[:20])

    _emit("Predicates of the guarded `committed_complete = true` UPDATE "
          "(`chapterLifecycleService.markTaskCompleted`). `f` on any row means "
          "the UPDATE matched ZERO rows, and the reported reason is the "
          "MISLEADING `structural_errors` fallback, not that predicate:")
    _emit()
    _emit("```")
    _emit(f"permanently_failed IS NOT TRUE        = {permfail}")
    _emit(f"content_hash IS NOT NULL              = {has_ch}")
    _emit(f"content_hash = md5(anchor markdown)   = {anchor_ok}")
    _emit(f"decomposition_status = 'pass'         = {dec_pass}")
    _emit(f"decomposition_error_count = 0         = {dec_errs}")
    _emit(f"decomposition_tree_hash IS NOT NULL   = {has_tree}")
    _emit(f"decomposition_parser_version NOT NULL = {has_pv}")
    _emit(f"receipt_projection_revision = proj_rev= {receipt_ok}")
    _emit(f"decomposition_child_count = live kids = {kids_ok}")
    _emit(f"publication_contract_version          = {pcv}")
    _emit(f"projection_revision                   = {proj_rev}")
    _emit(f"receipt_projection_revision           = {recv_rev}")
    _emit(f"markdown_length                       = {mdlen}")
    _emit(f"rubric_status                         = {rubric or '(none)'}")
    _emit("```")
    _emit()

    # Lead with the receipt's own findings when it HAS errors: `struct`/`humanized`
    # predicates below are downstream of them, and a child that reads only the
    # scalars will regenerate prose against a probe that will fail it again.
    if dec_errs == "f":
        _tier_decomposition_findings(chapter, book)

    _emit("The counts that decide the always-on clauses appended to that WHERE "
          "(`committedCompleteGevalHashClause` + `committedCompleteHumanization"
          "Clause`, both ALWAYS-ON — not flag-gated, unlike the rubric clause). "
          "The humanization `EXISTS` pins `rubric.job_id` to the job this "
          "lifecycle row RESOLVES to (`resolvedLifecycleJobId`), so rows under "
          "ANY OTHER job are invisible to the gate:")
    _emit()
    _emit("```")
    _emit(f"job the gate pins to (writing contract → run) = {pinned_job}")
    _emit(f"G-Eval rows under THAT job                    = {rows_pinned}")
    _emit(f"G-Eval rows under ANY job                     = {rows_any}")
    _emit(f"humanized rows binding THIS content_hash       = {binding_now}")
    _emit(f"humanized rows (any bytes, any job)           = {humanized_any}")
    _emit(f"operator_retry_requested_at                   = "
          f"{retry_marker or '(unset)'}")
    _emit("```")
    _emit()
    if rows_pinned == "0":
        _emit("### KNOCK-OUT: no G-Eval rows under the job the commit gate pins")
        _emit()
        _emit(
            "The humanization `EXISTS` appended to the commit UPDATE filters "
            "`rubric.job_id = <the job this chapter resolves to>` — the latest "
            f"writing contract's job (`{pinned_job}`). This chapter has "
            f"{rows_any} G-Eval rows, but ZERO under that job, so the `EXISTS` "
            "can NEVER be satisfied and the UPDATE matches zero rows forever — "
            "no retry, re-dispatch, or draft edit changes that. The chapter's "
            "retained content was written BEFORE the humanization stage existed, "
            "so no humanization receipt can bind it. Dropping or rewriting the "
            "rubric rows is NOT the fix (that is the publication invariant). "
            "The chapter must be driven THROUGH the humanization stage again: "
            "`chapterRunner.runOneChapter` sees content present and takes the "
            "'skipping regeneration (commit pending)' branch straight to "
            "`markTaskCompleted`, so the stage that would write the receipt is "
            "never re-entered. Find that branch and make it re-enter the "
            "humanize→G-Eval stage when the gate's receipt is missing for the "
            "CURRENT bytes (the `operatorRetryRequested` / `v2ReceiptInvalid` "
            "escape hatches at its top are the existing precedent)."
        )
        _emit()
    elif binding_now == "0":
        _emit("### KNOCK-OUT: humanized receipts exist, but bind OTHER bytes")
        _emit()
        _emit(
            f"{humanized_any} humanized G-Eval row(s) exist for this chapter, "
            "but NONE satisfies `evaluated_content_hash = content_hash` — the "
            "receipts prove older/different bytes, not the bytes now retained. "
            "The gate demands proof for the CURRENT content, so the chapter must "
            "be re-humanized (or regenerated) rather than re-committed."
        )
        _emit()
    elif rubric not in ("pass", "waived"):
        _emit(f"### GATE HELD: rubric_status={rubric!r} is not 'pass'/'waived'")
        _emit()
        _emit(
            "The commit UPDATE requires a passing rubric verdict, and a "
            "humanized row under the pinned job DOES bind the current bytes — "
            "so the judge has run on these bytes and its verdict is what holds "
            "the commit. Read the verdict ladder below for the failing axis."
        )
        _emit()


# ── Tier 2: quality-gate verdict ladder ──────────────────────────────────────

def _chapter_uuid(chapter: str, book: str) -> str | None:
    """The chapter's uuid, for joining the receipt table."""
    proc = _psql(
        "SELECT chapter_uuid::text FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' AND chapter_number={chapter};"
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _tier_quality(chapter: str, book: str) -> None:
    """The chapter's own gate receipts: the final-G-Eval attempt ladder and the
    failing axes behind it.

    Why this tier exists: every signal the other tiers carry is derived from
    ``book_chapter_lifecycle.last_error`` — which is OVERWRITTEN on each attempt
    and names only the last exception thrown. The judge's actual verdicts
    (score, axis, message, and the fact that a PASS was later blocked) live only
    in ``bg_compose_stage_artifact``. Without them the fix child patches the
    exception it was handed and never sees the trend: a draft the judge scored
    1.0 with zero violations can be blocked by a downstream gate, re-rolled, and
    come back worse — indistinguishable, to a last_error-only reader, from a
    plain quality failure.
    """
    _emit("## 2. Quality-gate verdict ladder (bg_compose_stage_artifact)")
    _emit()
    chapter_uuid = _chapter_uuid(chapter, book)
    if not chapter_uuid:
        _emit("NOTE: could not resolve chapter_uuid — skipping the receipt tier.")
        _emit()
        return

    ladder = _psql(
        "SELECT stage_key, verdict, "
        "to_char(created_at,'YYYY-MM-DD HH24:MI:SS'), "
        "coalesce(round(nullif(verdict_detail->>'overall_score','')::numeric, 3)::text,'-'), "
        "coalesce(jsonb_array_length(verdict_detail->'violations'),0) "
        "FROM bg_compose_stage_artifact "
        f"WHERE chapter_uuid='{chapter_uuid}' "
        "ORDER BY created_at DESC LIMIT 60;"
    )
    if ladder.returncode != 0:
        _emit(f"NOTE: receipt query failed rc={ladder.returncode}: "
              f"{ladder.stderr.strip()[:200]}")
        _emit()
        return
    rows = [r for r in ladder.stdout.strip().splitlines() if r]
    if not rows:
        _emit("NOTE: no compose-stage receipts recorded for this chapter yet.")
        _emit()
        return

    _emit("Newest 60 gate receipts, newest first. `nviol` is the count of "
          "FAILING AXES — the publication gates key on that count, NOT on the "
          "score (a 0.96 draft has failed; a 0.85 does not imply a pass):")
    _emit()
    cols = [r.split("|") for r in rows]
    _emit("```")
    for stage, verdict, at, score, nviol in ((c + [""] * 5)[:5] for c in cols):
        _emit(f"{at}  {stage:28} {verdict:6} score={score:>6} nviol={nviol}")
    _emit("```")
    _emit()

    # The ordering tell: a judge PASS with no chapter_commit at or after it.
    # Rows are newest-first, so a SMALLER index is NEWER.
    pass_idx = next(
        (i for i, c in enumerate(cols)
         if c[0] == "W27_final_geval" and c[1] == "pass"),
        None,
    )
    commit_idx = next((i for i, c in enumerate(cols) if c[0] == "chapter_commit"), None)
    if pass_idx is not None and (commit_idx is None or pass_idx < commit_idx):
        _emit("### TELL: the judge PASSED a draft that never reached chapter_commit")
        _emit()
        _emit(
            "The final G-Eval was satisfied but no `chapter_commit` receipt "
            "followed, so a LATER gate (chapter commit runs after the judge — "
            "e.g. the citation publish gate) blocked the chapter. Re-generating "
            "the draft does not address that: the same bytes would be blocked "
            "again. Trace the gates that run AFTER final G-Eval and fix the one "
            "that refuses."
        )
        _emit()

    # The final-G-Eval ladder, OLDEST→NEWEST so the trend is readable. The full
    # list above is newest-first and interleaves every stage; this isolates the
    # one series that decides the chapter and shows whether repair is converging
    # or thrashing. A rising nviol means each re-roll is LOSING ground.
    geval = [c for c in cols if c[0] == "W27_final_geval"]
    if geval:
        _emit("### Final G-Eval ladder (the trend — oldest first)")
        _emit()
        _emit("```")
        for i, (_, verdict, at, score, nviol) in enumerate(reversed(geval), start=1):
            _emit(f"attempt {i:>2}  {at}  {verdict:6} score={score:>6} nviol={nviol}")
        _emit("```")
        _emit()
        scores = [(n, c[4]) for n, c in enumerate(reversed(geval), start=1)]
        worst = max(scores, key=lambda t: int(t[1] or 0)) if scores else None
        if worst and int(worst[1] or 0) > 0:
            best = min(scores, key=lambda t: int(t[1] or 0))
            _emit(
                f"Read it left→right: {len(geval)} judged attempts. The fewest "
                f"failing axes was {best[1]} (attempt {best[0]}); the most is "
                f"{worst[1]} (attempt {worst[0]}). If the count is not falling, "
                "the repair path is not converging — the edits it makes are not "
                "touching the axes that fail."
            )
            _emit()

    # Failing axes, newest-first, so recurring axes are visible at a glance.
    viol = _psql(
        "SELECT a.stage_key, to_char(a.created_at,'HH24:MI:SS'), "
        "coalesce(v->>'axis','?'), coalesce(v->>'severity',''), "
        "regexp_replace(coalesce(v->>'message',''), '\\s+', ' ', 'g') "
        "FROM bg_compose_stage_artifact a, "
        "jsonb_array_elements(coalesce(a.verdict_detail->'violations','[]'::jsonb)) v "
        f"WHERE a.chapter_uuid='{chapter_uuid}' AND a.verdict='fail' "
        "ORDER BY a.created_at DESC LIMIT 15;"
    )
    if viol.returncode == 0:
        vrows = [r.split("|") for r in viol.stdout.strip().splitlines() if r]
        if vrows:
            _emit("Failing axes, newest first (fix the AXIS, not the score):")
            _emit()
            _emit("```")
            axes: dict[str, int] = {}
            for stage, at, axis, severity, message in ((v + [""] * 5)[:5] for v in vrows):
                axes[axis] = axes.get(axis, 0) + 1
                _emit(f"{at} {stage:24} [{axis}] {severity:8} {message[:160]}")
            _emit("```")
            _emit()
            ranked = sorted(axes.items(), key=lambda kv: kv[1], reverse=True)
            _emit("Axis frequency across recent failures: "
                  + ", ".join(f"{a}x{n}" for a, n in ranked))
            _emit()
    else:
        _emit(f"NOTE: violation query failed rc={viol.returncode}: "
              f"{viol.stderr.strip()[:200]}")
        _emit()


# ── Tier 3: preserved sandboxes ──────────────────────────────────────────────

def _sandbox_roots() -> list[Path]:
    """Candidate ``.mini-ork/runs`` roots, most-authoritative first."""
    cands: list[Path] = []
    for env in ("MO_GOAL_TARGET_CWD", "MO_RESEARCHER_DIR", "MINI_ORK_TARGET_REPO"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / ".mini-ork" / "runs")
    cands.append(Path.cwd() / ".mini-ork" / "runs")
    seen: set[Path] = set()
    out: list[Path] = []
    for c in cands:
        rc = c.resolve()
        if rc not in seen and c.is_dir():
            seen.add(rc)
            out.append(c)
    return out


def _headings(markdown: str) -> tuple[list[str], list[str]]:
    """Return (h2_lines, h3_lines) as the verbatim ``## …`` / ``### …`` text."""
    h2, h3 = [], []
    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("### "):
            h3.append(s)
        elif s.startswith("## "):
            h2.append(s)
    return h2, h3


def _recent_artifacts(limit: int = 8) -> list[tuple[Path, dict]]:
    """Newest preserved verified-artifact.json envelopes, newest first."""
    found: list[tuple[float, Path, dict]] = []
    for root in _sandbox_roots():
        for run_dir in root.glob("run-*"):
            art = run_dir / "verified-artifact.json"
            if not art.is_file():
                continue
            try:
                data = json.loads(art.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or "node_key" not in data:
                continue
            found.append((art.stat().st_mtime, run_dir, data))
    found.sort(key=lambda t: t[0], reverse=True)
    # Dedup by node_key+title so a shared primary/worktree root doesn't double.
    out: list[tuple[Path, dict]] = []
    seen: set[tuple[str, str]] = set()
    for _, run_dir, data in found:
        key = (str(data.get("node_key", "")), str(data.get("title", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append((run_dir, data))
        if len(out) >= limit:
            break
    return out


def _verify_note(run_dir: Path) -> str:
    verdict = "(no verdict.json)"
    vp = run_dir / "verdict.json"
    if vp.is_file():
        try:
            verdict = str(json.loads(vp.read_text(encoding="utf-8")).get("verdict", "?"))
        except (OSError, ValueError):
            verdict = "(unparseable)"
    vacuous = ""
    log = run_dir / "execute.log"
    if log.is_file():
        try:
            if "no outputs in artifact_contract" in log.read_text(encoding="utf-8", errors="replace"):
                vacuous = "  <-- in-sandbox verifier had NO declared outputs to check (vacuous pass)"
        except OSError:
            pass
    return f"mini-ork in-sandbox verdict = {verdict}{vacuous}"


def _tier_sandbox(lasterr: str | None) -> dict | None:
    """Report the newest produced artifacts. Returns the primary node's dict."""
    _emit("## 3. What the lane actually produced (preserved mini-ork sandboxes)")
    _emit()
    arts = _recent_artifacts()
    if not arts:
        _emit("NOTE: no preserved verified-artifact sandboxes found under any "
              ".mini-ork/runs root. (Set MO_RESEARCHER_DIR / MO_GOAL_TARGET_CWD.)")
        _emit()
        return None

    # Prefer the sandbox whose node_key the DB last_error names, else the newest.
    primary_run, primary = arts[0]
    if lasterr:
        for run_dir, data in arts:
            nk = str(data.get("node_key", ""))
            if nk and nk in lasterr:
                primary_run, primary = run_dir, data
                break

    node_key = str(primary.get("node_key", "?"))
    node_type = str(primary.get("node_type", "?"))
    title = str(primary.get("title", ""))
    h2, h3 = _headings(str(primary.get("markdown", "")))
    label = "compose node" if _COMPOSE_NODE_HINT.match(node_key) else "node"
    _emit(f"Most relevant produced artifact ({label}):")
    _emit()
    _emit("```")
    _emit(f"run_dir   = {primary_run}")
    _emit(f"node_key  = {node_key}")
    _emit(f"node_type = {node_type}")
    _emit(f"title     = {title!r}")
    _emit(f"produced ## H2 headings  = {h2 or '(none)'}")
    _emit(f"produced ### H3 headings = {h3 or '(none)'}")
    _emit(f"{_verify_note(primary_run)}")
    _emit("```")
    _emit()
    if len(arts) > 1:
        _emit("Recent produced nodes (pattern across the last few dispatches):")
        _emit()
        _emit("```")
        for run_dir, data in arts:
            nh2, _ = _headings(str(data.get("markdown", "")))
            _emit(f"{str(data.get('node_key','?')):28} title={str(data.get('title',''))!r:28} "
                  f"H2={nh2 or '(none)'}")
        _emit("```")
        _emit()
    return primary


# ── Tier 4: source contract requirement + repair-signal pointer ──────────────

def _find_lens_spec(node_type: str) -> tuple[list[str], str | None, Path | None]:
    """Parse the researcher lens source for ``<node_type>``'s requiredSections
    and sectionPolicy. Returns (required_sections, section_policy, source_path)."""
    target = os.environ.get("MO_GOAL_TARGET_CWD") or os.getcwd()
    # Search a few likely homes for the lens registry, longest/specific first.
    candidates = [
        "server/compose/ideaExploration/lensPrompts.ts",
        "server/compose/verifiedArtifact/lensPrompts.ts",
    ]
    src_path: Path | None = None
    text = ""
    for rel in candidates:
        p = Path(target) / rel
        if p.is_file():
            src_path = p
            text = p.read_text(encoding="utf-8", errors="replace")
            break
    if not text:
        # Last resort: scan the compose tree for the node_type key.
        for p in Path(target, "server", "compose").rglob("*.ts"):
            try:
                t = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(rf"\b{re.escape(node_type)}\s*:\s*{{", t):
                src_path, text = p, t
                break
    if not text:
        return [], None, None

    m = re.search(rf"\b{re.escape(node_type)}\s*:\s*{{", text)
    if not m:
        return [], None, src_path
    block = text[m.end(): m.end() + 1600]
    req: list[str] = []
    rm = re.search(r"requiredSections\s*:\s*\[([^\]]*)\]", block)
    if rm:
        req = re.findall(r"'([^']*)'|\"([^\"]*)\"", rm.group(1))
        req = [a or b for a, b in req]
    pol = None
    pm = re.search(r"sectionPolicy\s*:\s*'([^']*)'", block)
    if pm:
        pol = pm.group(1)
    return req, pol, src_path


def _tier_source(primary: dict | None) -> None:
    _emit("## 4. Caller-contract requirement vs. produced (from researcher source)")
    _emit()
    if not primary:
        _emit("NOTE: no produced node resolved in Tier 2 — cannot diff against the contract.")
        _emit()
        return
    node_type = str(primary.get("node_type", ""))
    required, policy, src = _find_lens_spec(node_type)
    if not required:
        _emit(f"NOTE: could not resolve requiredSections for node_type={node_type!r} "
              "in the compose source.")
        _emit()
        return

    produced_h2, _ = _headings(str(primary.get("markdown", "")))
    produced_norm = {h[3:].strip().lower() for h in produced_h2}  # strip "## "
    required_h2 = [f"## {s}" for s in required]
    missing = [rq for rq, s in zip(required_h2, required) if s.strip().lower() not in produced_norm]

    _emit("```")
    _emit(f"node_type        = {node_type}")
    _emit(f"source           = {src}")
    _emit(f"sectionPolicy    = {policy or '(unset -> PRESENCE policy: each required section must appear as a `## <name>` line; extras allowed)'}")
    _emit(f"requiredSections = {required}")
    _emit(f"required as H2   = {required_h2}")
    _emit(f"produced H2      = {produced_h2 or '(none)'}")
    _emit(f"MISSING required = {missing or '(none — headings satisfied; look elsewhere)'}")
    _emit("```")
    _emit()

    # The de-biasing pointer: presence-policy nodes get NO in-sandbox structural
    # repair signal, so a drift can never self-correct. State it as a HYPOTHESIS
    # for the child to confirm in source — do not prescribe the patch.
    if policy != "exact_h2":
        _emit("### Why this likely re-rolls forever (hypothesis to verify in source)")
        _emit()
        _emit(
            "This node uses **presence policy** (no `sectionPolicy: 'exact_h2'`). "
            "The prompt template already asks for the required headings verbatim, "
            "so binding the prompt harder is unlikely to help. Trace the in-sandbox "
            "repair path instead:"
        )
        _emit()
        _emit(
            "- `server/compose/verifiedArtifact/verifiedArtifactProduction.ts` — "
            "`requiredStructureTail()` and `structuralRepairFindings()` both gate on "
            "`sectionPolicy === 'exact_h2'` / `requiredH2Headings`. For a presence-policy "
            "node those return nothing, so when the lane drifts (e.g. emits "
            "`## Section scaffold` instead of `## H2 outline` / `## Per-section intent`) "
            "the repair turn is handed NO corrective finding — strictly less signal than "
            "the first attempt. `buildRepairInputs()` / `dispatchProductionNode()` never "
            "thread `promptSpec.requiredSections` + `sectionPolicy` down that path."
        )
        _emit(
            "- Cross-check: the sandbox's `verdict.json` shows mini-ork's own verify "
            "PASSED while the host `composeArtifactGuardFor` rejected post-hoc — the "
            "in-sandbox contract does not encode the host's presence requirement, so the "
            "lane is never told what it got wrong."
        )
        _emit()
        _emit(
            "A durable fix teaches the in-sandbox repair path (and/or the in-sandbox "
            "contract) about presence-policy `requiredSections`, so ANY presence-policy "
            "node that drifts gets an actionable `## <missing heading>` finding on its "
            "repair turn instead of re-rolling the whole chapter. Fix the class, not just "
            "this one node."
        )
        _emit()


def main(argv: list[str]) -> int:
    chapter = (argv[0].strip() if argv else "")
    if not re.fullmatch(r"\d+", chapter):
        _emit(f"NOTE: bad/absent chapter id {chapter!r}; emitting node-level evidence only.")
        chapter = ""
    book = (os.environ.get("BOOK_UUID") or "").strip()

    _emit(f"# Failure evidence for chapter {chapter or '(unknown)'} "
          f"of book {book or '(unset)'}")
    _emit()

    lasterr = None
    try:
        if chapter and re.fullmatch(r"[0-9a-fA-F-]{36}", book):
            lasterr = _tier_db(chapter, book)
        else:
            _emit("## 1. Live generation-status")
            _emit()
            _emit("NOTE: chapter/BOOK_UUID unresolved — skipping DB tier.")
            _emit()
    except Exception as exc:  # noqa: BLE001 — advisory; never crash the wave
        _emit(f"NOTE: DB tier crashed: {exc}")
        _emit()

    try:
        if chapter and re.fullmatch(r"[0-9a-fA-F-]{36}", book):
            _tier_commit_gate(chapter, book)
        else:
            _emit("## 1b. Why the commit gate refuses")
            _emit()
            _emit("NOTE: chapter/BOOK_UUID unresolved — skipping the commit-gate tier.")
            _emit()
    except Exception as exc:  # noqa: BLE001 — advisory; never crash the wave
        _emit(f"NOTE: commit-gate tier crashed: {exc}")
        _emit()

    try:
        if chapter and re.fullmatch(r"[0-9a-fA-F-]{36}", book):
            _tier_quality(chapter, book)
        else:
            _emit("## 2. Quality-gate verdict ladder")
            _emit()
            _emit("NOTE: chapter/BOOK_UUID unresolved — skipping the receipt tier.")
            _emit()
    except Exception as exc:  # noqa: BLE001
        _emit(f"NOTE: quality tier crashed: {exc}")
        _emit()

    primary = None
    try:
        primary = _tier_sandbox(lasterr)
    except Exception as exc:  # noqa: BLE001
        _emit(f"NOTE: sandbox tier crashed: {exc}")
        _emit()

    try:
        _tier_source(primary)
    except Exception as exc:  # noqa: BLE001
        _emit(f"NOTE: source tier crashed: {exc}")
        _emit()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
