"""Score the framework-edit reviewer against incidents and known-clean changes.

The framework-edit reviewer is mini-ork's last LLM gate before publish, and
until now nothing measured it. This module turns the incidents the reviewer
missed (and the clean changes it must not block) into an eval set:

  * each case lives in ``evals/reviewer/cases/<case_id>/`` as ``case.json`` +
    ``kickoff.md`` + ``diff.patch``; a shipped seed may carry the latter two as
    ``"kickoff"``/``"diff"`` strings inside ``case.json``, materialised beside
    it on first prompt build (``add-case`` writes all three files directly);
  * every run is keyed by a fingerprint of prompt + envelope + lane + model,
    so a score is only ever compared with a score from the same reviewer;
  * results land in ``evals/reviewer/results/<date>-<fingerprint>.jsonl``.

The module is deliberately LLM-free: dispatch is a parameter (``dispatch_fn``)
so tests and the CLI can run cases without spending a token, and ``judge`` and
``score`` are pure functions over the parsed review.

Kickoff: rev6-r4 ``reviewer-eval``.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Caps mirror ``context_assembler``: a reviewer never needs the full tail of a
# 200 KB diff or a 50 KB kickoff, and an uncapped prompt is a cost bomb.
_KICKOFF_CAP = 40_000
_DIFF_CAP = 120_000
_TRUNCATION_MARKER = "[truncated]"

_PASS_VERDICTS = frozenset({"pass", "approve", "approved"})
_BLOCK_VERDICTS = frozenset(
    {"needs_revision", "revise", "request_changes", "fail", "reject", "escalate"}
)


@dataclass(frozen=True)
class Case:
    """One eval case: a change the reviewer should block (incident) or pass."""

    id: str
    kind: str
    expect: str
    source: dict
    defect: dict
    path: Path


@dataclass(frozen=True)
class CaseResult:
    """One reviewer run over one case."""

    case_id: str
    kind: str
    expect: str
    verdict: str
    flagged: bool
    hit_defect: bool
    findings_n: int
    cost_usd: float | None
    error: str


# ─────────────────────────────────────────────────────────────────────────────
# Case loading
# ─────────────────────────────────────────────────────────────────────────────


def _empty_defect() -> dict:
    return {"files": [], "summary": "", "keywords": []}


# A seed case may ship its two data files inside ``case.json`` under these
# keys. The file names are the documented case layout; the values are the
# materialised view. Keeping the seed self-contained means a case directory is
# one reviewable manifest, and ``add-case`` stays the only writer of the
# three-file layout for new cases.
_EMBEDDED_CASE_FILES = {"kickoff": "kickoff.md", "diff": "diff.patch"}


def _embedded_text(payload: dict, name: str) -> str:
    """The embedded ``case.json`` string for the file ``name`` (``""`` if none)."""
    for key, filename in _EMBEDDED_CASE_FILES.items():
        if filename == name:
            text = payload.get(key)
            return text if isinstance(text, str) else ""
    return ""


def _read_case_json(case_dir: Path) -> dict:
    try:
        payload = json.loads((Path(case_dir) / "case.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _materialise_case_files(case_dir: Path, payload: dict) -> None:
    """Write an embedded ``kickoff``/``diff`` beside ``case.json`` when absent.

    Best-effort by design: a read-only checkout leaves the files absent and
    callers fall back to the same embedded text, so the harness never depends on
    a writable repo.
    """
    for key, name in _EMBEDDED_CASE_FILES.items():
        text = payload.get(key)
        if not isinstance(text, str):
            continue
        target = Path(case_dir) / name
        if target.exists():
            continue
        try:
            target.write_text(text, encoding="utf-8")
        except OSError:
            continue


def _case_text(case_dir: Path, name: str) -> str:
    """``kickoff.md``/``diff.patch`` text, falling back to the ``case.json`` copy."""
    try:
        return (Path(case_dir) / name).read_text(encoding="utf-8")
    except OSError:
        return _embedded_text(_read_case_json(case_dir), name)


def load_cases(cases_dir: Path) -> list[Case]:
    """Load every case under ``cases_dir``, sorted by id.

    A missing directory is an empty set (``garden`` and ``status`` treat that as
    "not configured"). An invalid ``case.json`` raises :class:`ValueError`
    naming the file: a half-written case would otherwise silently shrink the
    eval and inflate the score.

    ``expect`` is derived from ``kind`` — incident → block, clean → pass — so a
    stale/mistyped ``expect`` in the JSON cannot make the harness grade a case
    against the wrong answer.
    """
    cases_dir = Path(cases_dir)
    if not cases_dir.is_dir():
        return []

    cases: list[Case] = []
    for entry in sorted(cases_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        case_file = entry / "case.json"
        if not case_file.is_file():
            continue
        try:
            payload = json.loads(case_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{case_file}: unreadable case.json: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{case_file}: case.json must be a JSON object")

        cid = payload.get("id")
        kind = payload.get("kind")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"{case_file}: missing string id")
        if kind not in ("incident", "clean"):
            raise ValueError(f"{case_file}: kind must be 'incident' or 'clean', got {kind!r}")

        source = payload.get("source")
        if not isinstance(source, dict):
            source = {"run": "", "commit": ""}

        raw_defect = payload.get("defect")
        if not isinstance(raw_defect, dict):
            raw_defect = {}

        defect = {
            "files": list(raw_defect.get("files") or []),
            "summary": str(raw_defect.get("summary") or ""),
            "keywords": list(raw_defect.get("keywords") or []),
        }

        if kind == "incident":
            if not defect["summary"].strip():
                raise ValueError(f"{case_file}: incident requires a non-empty defect.summary")
            if not defect["files"] and not defect["keywords"]:
                raise ValueError(
                    f"{case_file}: incident requires at least one defect file or keyword")
            expect = "block"
        else:
            if defect["summary"].strip() or defect["files"] or defect["keywords"]:
                raise ValueError(f"{case_file}: clean case must not declare a defect")
            expect = "pass"

        cases.append(Case(id=cid, kind=kind, expect=expect, source=source,
                          defect=defect, path=entry))

    cases.sort(key=lambda c: c.id)
    return cases


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer identity
# ─────────────────────────────────────────────────────────────────────────────


def reviewer_lane(root: Path) -> str:
    """The ``model_lane`` of the ``reviewer`` node in the framework-edit recipe.

    Raise (do not return ``""``) when the node is missing: an empty lane would
    dispatch nowhere, and every caller (CLI, garden) wraps this in a path that
    reports a clear error or stays silent.
    """
    import yaml

    wf_path = Path(root) / "recipes" / "framework-edit" / "workflow.yaml"
    document = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and node.get("name") == "reviewer":
            lane = node.get("model_lane")
            if not lane:
                raise ValueError(f"{wf_path}: reviewer node has no model_lane")
            return str(lane)
    raise ValueError(f"{wf_path}: no reviewer node")


def _prompt_sha(root: Path) -> str:
    md = Path(root) / "recipes" / "framework-edit" / "prompts" / "reviewer.md"
    return hashlib.sha256(md.read_bytes()).hexdigest()[:16]


def _envelope_sha() -> str:
    from mini_ork.cli.execute_handlers import reviewer_envelope

    # The literal placeholder, not a real path: the fingerprint must be stable
    # across work dirs, and the review file differs on every run.
    return hashlib.sha256(reviewer_envelope("<review_file>").encode("utf-8")).hexdigest()[:16]


def _resolved_lane(lane: str, root: Path, home: str) -> str:
    from mini_ork.dispatch import llm_dispatch

    return llm_dispatch.resolve_lane_family(lane, str(root), home)


def _model_for(resolved_lane: str, root: Path) -> str:
    try:
        from mini_ork.dispatch.providers import _load_providers_registry

        entry = _load_providers_registry(root).get(resolved_lane)
        if isinstance(entry, dict):
            return str(entry.get("model") or "")
    except Exception:  # noqa: BLE001 — a missing/broken registry is not an eval failure
        pass
    return ""


def fingerprint(root: Path, lane: str, home: str = "") -> dict:
    """Identity of the reviewer under test: prompt + envelope + lane + model.

    ``id`` is the hash of the five fields, so any prompt, envelope, lane or
    model change produces a new id and last run's score is no longer mistaken
    for this reviewer's.
    """
    root = Path(root)
    fields = {
        "prompt_sha": _prompt_sha(root),
        "envelope_sha": _envelope_sha(),
        "lane": lane,
        "resolved_lane": _resolved_lane(lane, root, home),
        "model": "",  # filled below; placeholder keeps the key order obvious
    }
    fields["model"] = _model_for(fields["resolved_lane"], root)
    digest = hashlib.sha256(
        json.dumps(fields, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {**fields, "id": digest}


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n" + _TRUNCATION_MARKER


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            if heading:
                return heading
    return None


def _repo_root() -> Path:
    """The checkout this module was imported from (``mini_ork/../..``)."""
    return Path(__file__).resolve().parents[2]


def _case_dir(case: Case, root: Path) -> Path:
    """The directory holding ``case``'s data files: ``case.path``, nothing else.

    No lookup by case id: a prompt built from some other directory's files
    than the case that was loaded would grade the reviewer on data the caller
    never chose.
    """
    del root
    return Path(case.path)


def build_prompt(case: Case, *, root: Path, review_file: str) -> str:
    """Mirror the classic reviewer branch of ``_handle_reviewer`` for one case.

    Order matches the runtime prompt: recipe prompt (system context), the
    task line, the plan (the case's kickoff), the reviewer inputs (the diff),
    then the envelope naming the artifact to write.
    """
    from mini_ork.cli.execute_handlers import reviewer_envelope

    case_dir = _case_dir(case, root)
    _materialise_case_files(case_dir, _read_case_json(case_dir))
    kickoff = _case_text(case_dir, "kickoff.md")
    diff = _case_text(case_dir, "diff.patch")

    reviewer_md_path = Path(root) / "recipes" / "framework-edit" / "prompts" / "reviewer.md"
    reviewer_md = reviewer_md_path.read_text(encoding="utf-8")

    heading = _first_heading(kickoff) or case.id
    return "".join([
        "\n\n--- Recipe prompt (system context) ---\n",
        reviewer_md,
        "\n--- /recipe prompt ---\n\n",
        "Review the implementation for: ", heading,
        "\n\nPlan:\n", _cap(kickoff, _KICKOFF_CAP),
        "\n\n# Reviewer inputs\n# review-diff.patch\n", _cap(diff, _DIFF_CAP),
        "\n", reviewer_envelope(review_file),
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Review parsing + judging
# ─────────────────────────────────────────────────────────────────────────────


def _balanced_objects(text: str) -> list[str]:
    """Every top-level balanced ``{…}`` in ``text``, honoring JSON strings."""
    found: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                found.append(text[start:i + 1])
                start = -1
    return found


def parse_review(text: str) -> dict | None:
    """Return the review object, or ``None`` when the reply is prose only.

    A plain JSON object wins; otherwise the LAST balanced object carrying a
    verdict (the reviewer's final answer after commentary); otherwise ``None``.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and "verdict" in parsed:
        return parsed

    for candidate in reversed(_balanced_objects(text)):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    return None


def _files_in_defect(defect: dict) -> set[str]:
    files = defect.get("files")
    return {str(f) for f in files} if isinstance(files, list) else set()


def _keywords_in_defect(defect: dict) -> list[str]:
    keywords = defect.get("keywords")
    return [str(k) for k in keywords if str(k)] if isinstance(keywords, list) else []


def _hits_defect(findings: list, defect: dict) -> bool:
    files = _files_in_defect(defect)
    keywords = [k.lower() for k in _keywords_in_defect(defect)]
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if files and str(finding.get("file") or "") in files:
            return True
        instances = finding.get("instances")
        if files and isinstance(instances, list):
            for instance in instances:
                if isinstance(instance, dict) and str(instance.get("file") or "") in files:
                    return True
        issue = finding.get("issue")
        if keywords and isinstance(issue, str):
            lowered = issue.lower()
            if any(keyword in lowered for keyword in keywords):
                return True
    return False


def judge(case: Case, review: dict | None) -> tuple[str, bool, bool, int]:
    """``(verdict, flagged, hit_defect, findings_n)`` for one parsed review."""
    if not isinstance(review, dict):
        return ("error", False, False, 0)

    raw = review.get("verdict")
    token = raw.strip().lower() if isinstance(raw, str) else ""
    if token in _PASS_VERDICTS:
        verdict = "pass"
    elif token in _BLOCK_VERDICTS:
        verdict = "block"
    else:
        verdict = "error"

    flagged = verdict == "block"
    findings = review.get("findings")
    findings_list = findings if isinstance(findings, list) else []
    findings_n = len(findings_list)

    hit = False
    if case.kind == "incident" and isinstance(case.defect, dict):
        hit = _hits_defect(findings_list, case.defect)

    return (verdict, flagged, hit, findings_n)


# ─────────────────────────────────────────────────────────────────────────────
# Running one case
# ─────────────────────────────────────────────────────────────────────────────


def _read_cost(out_txt: Path) -> float | None:
    try:
        return float(Path(str(out_txt) + ".cost").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def run_case(case: Case, *, root: Path, lane: str, work_dir: Path,
             dispatch_fn=None, timeout_s: int = 900) -> CaseResult:
    """Dispatch the reviewer over one case in an isolated working directory.

    The reviewer runs with ``cwd=work_dir/<case_id>`` so it cannot read the
    live repository: the only inputs it gets are the prompt. The caller's cwd
    is restored in ``finally`` even when dispatch raises.
    """
    case_dir = Path(work_dir) / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    review_file = case_dir / "review.json"
    out_txt = case_dir / "out.txt"

    # Never let a previous run's verdict satisfy "the reviewer wrote a review".
    try:
        review_file.unlink()
    except FileNotFoundError:
        pass

    prompt = build_prompt(case, root=Path(root), review_file=str(review_file))

    dispatch_lane = lane
    if dispatch_fn is None:
        from mini_ork.dispatch import llm_dispatch

        dispatch = llm_dispatch.mo_llm_dispatch
        # mo_llm_dispatch's preflight knows providers.yaml lanes, not
        # agents.yaml aliases: `opus_lens` is refused as an unknown lane. The
        # executor resolves the alias before dispatch; so must the eval, or
        # every case errors before the reviewer sees it (first run, 2026-10-26).
        dispatch_lane = _resolved_lane(lane, Path(root), os.environ.get("MINI_ORK_HOME", ""))
    else:
        dispatch = dispatch_fn

    error = ""
    previous_cwd = os.getcwd()
    try:
        os.chdir(case_dir)
        try:
            # The three required dispatch args are positional (the lane, the
            # prompt, the out file); the optional timeout/turn cap are keyword
            # so a dispatch fake that only declares the required ones still
            # matches instead of raising a TypeError the harness would record as
            # a case error.
            rc = dispatch(dispatch_lane, prompt, str(out_txt),
                          timeout_s=timeout_s, max_turns=30)
        except Exception as exc:  # noqa: BLE001 — a crashed dispatch is a case error
            rc = None
            error = str(exc) or exc.__class__.__name__
    finally:
        os.chdir(previous_cwd)

    if rc is not None and rc != 0:
        error = str(rc)

    text = ""
    try:
        if review_file.is_file() and review_file.stat().st_size > 0:
            text = review_file.read_text(encoding="utf-8")
        elif out_txt.is_file():
            text = out_txt.read_text(encoding="utf-8")
    except OSError:
        text = ""

    review = parse_review(text)
    verdict, flagged, hit_defect, findings_n = judge(case, review)

    return CaseResult(
        case_id=case.id,
        kind=case.kind,
        expect=case.expect,
        verdict=verdict,
        flagged=flagged,
        hit_defect=hit_defect,
        findings_n=findings_n,
        cost_usd=_read_cost(out_txt),
        error=error,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────


def score(results: list[CaseResult]) -> dict:
    """Summarize a run: incident recall (Wilson-bounded) and clean false blocks.

    Errors count in ``n`` (they are evidence the reviewer did not catch the
    incident) but in neither numerator.
    """
    from mini_ork.gates.judge_validity import wilson_lower

    incidents = [r for r in results if r.kind == "incident"]
    clean = [r for r in results if r.kind == "clean"]

    n_inc = len(incidents)
    caught = sum(1 for r in incidents if r.flagged)
    caught_on_defect = sum(1 for r in incidents if r.hit_defect)

    n_clean = len(clean)
    false_blocks = sum(1 for r in clean if r.flagged)

    return {
        "incidents": {
            "n": n_inc,
            "caught": caught,
            "caught_on_defect": caught_on_defect,
            "recall": (caught_on_defect / n_inc) if n_inc else None,
            "recall_wilson_lower": wilson_lower(caught_on_defect, n_inc) if n_inc else None,
        },
        "clean": {
            "n": n_clean,
            "false_blocks": false_blocks,
            "false_block_rate": (false_blocks / n_clean) if n_clean else None,
        },
        "errors": sum(1 for r in results if r.verdict == "error"),
        "cost_usd": sum(r.cost_usd for r in results if r.cost_usd is not None),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Results files
# ─────────────────────────────────────────────────────────────────────────────


def _summary_row(path: Path) -> dict | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "summary" in row:
            return row
    return None


def latest_result(results_dir: Path) -> tuple[Path, dict] | None:
    """The newest results file by name and its summary row."""
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        return None
    files = sorted(
        (p for p in results_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"),
        key=lambda p: p.name,
    )
    for path in reversed(files):
        row = _summary_row(path)
        if row is not None:
            return (path, row)
    return None


def today_name(fingerprint_id: str, *, now: datetime | None = None) -> str:
    """``<YYYY-MM-DD>-<fingerprint id>.jsonl`` (shared by CLI + tests)."""
    day = (now or datetime.now()).date().isoformat()
    return f"{day}-{fingerprint_id}.jsonl"
