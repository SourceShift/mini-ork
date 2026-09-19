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
