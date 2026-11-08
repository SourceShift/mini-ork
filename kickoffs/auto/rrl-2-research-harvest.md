# RRL-2: `mini_ork/rsi/research.py` — turn a technique review into RSI candidates

## Goal

`recipes/verification-technique-review` (and its siblings) already research a domain:
they collect ~2000 arXiv papers, cluster techniques into 10 families, and write a
ranked `merged-review.json` into a run dir under the RSI home (`<home>/runs/<run-id>/`).
Nothing reads that file. This kickoff builds the reader: a pure-ish module that finds
the newest review for a domain and turns its top ranked techniques into
`rsi.candidates.Candidate` records of a new kind, `technique_unadopted`. Nothing
calls it yet (RRL-4 wires it into the harvest).

Shape of one `merged-review.json` (real file:
`.mini-ork/runs/verify-agentic-20261018-215339/merged-review.json`): a JSON object
with keys `reviewers, technique_count, unscored, disagreement_count, groups, ranked`.
`ranked` is a list of objects, each with at least:
`id` (e.g. `"G03-T02"`), `group_id` (`"G03"`), `technique` (str), `mechanism` (str,
may start/end with a literal `"`), `evidence` (list of `"arxiv:<id>"` strings),
`evidence_strength` (str), `miniork_surface` (str), `adoption_sketch` (str, may be
quoted), `priority` (number), `mean_impact` (number), `verdict` (str, e.g.
`implement-now` / `next` / `watch`), `single_rater` (bool), `disagreement` (bool),
`rank` (int). Any of these may be missing in a malformed row.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/rsi/candidates.py`

Add exactly one entry to `KIND_BASE`, placed between `"unused_capability": 46` and
`"stale_signal": 38`:

```python
    "technique_unadopted": 44,
```

with a one-line comment above it: `# a ranked research technique not yet adopted`.
Change nothing else in the file.

### 2. `mini_ork/rsi/research.py` (new)

Imports: standard library plus `from .candidates import Candidate`. No DB, network,
subprocess or model access.

**Constants**

- `KIND = "technique_unadopted"`
- `DEFAULT_VERDICTS = ("implement-now", "next")`
- `MAX_TEXT = 1200`

**`@dataclass(frozen=True, slots=True) class DomainSpec`** — fields in order:
`domain: str`, `review_glob: str`, `benchmark: str`, `files: tuple[str, ...]`,
`surface: tuple[str, ...]`, `recipe: str = ""`.

**`DEFAULT_SPECS: Mapping[str, DomainSpec]`** — exactly one entry:

```python
"verification": DomainSpec(
    domain="verification",
    review_glob="runs/verify-*/merged-review.json",
    benchmark="verifier-replay",
    files=("recipes/code-fix/verifiers/test.py",),
    surface=("recipes/code-fix/verifiers", "mini_ork/certify", "mini_ork/verify",
             "mini_ork/gates/suite_adequacy.py", "mini_ork/gates/suite_strengthen.py"),
    recipe="verification-technique-review",
)
```

**`latest_review(home: Path, review_glob: str) -> Path | None`**

- `sorted(Path(home).glob(review_glob))`, keep only files; return the one with the
  greatest `stat().st_mtime` (ties: the lexicographically greatest path); `None` if
  none.

**`load_ranked(path: Path) -> list[dict]`**

- `json.loads(path.read_text(encoding="utf-8"))`. Raise `ValueError` if the payload is
  not a dict or `payload.get("ranked")` is not a list. Return only the list items that
  are dicts.

**`slug(text: str) -> str`** — lowercase; every run of characters outside `[a-z0-9]`
becomes one `-`; strip `-` from both ends; truncate to 64 characters; strip a trailing
`-` again. Empty result → `"technique"`.

**`_clean(text: Any, limit: int = MAX_TEXT) -> str`** — `str(text or "")`, strip
whitespace, then strip one leading and one trailing `"` if present, strip again, and
truncate to `limit` characters (append `"…"` when truncated).

**`fix_hint(row: Mapping, spec: DomainSpec) -> str`** — exactly these lines joined by
`"\n"`:

```
Adopt research technique {id} — {technique}.
Mechanism: {mechanism}
Adoption sketch: {adoption_sketch}
Evidence: {evidence}
This change is measured by the `{benchmark}` benchmark, which exercises only: {surface}. Put the change inside that surface, or it cannot show a gain.
Implement the mechanism as described. Do not edit tests that pin existing behaviour, `evals/`, or the benchmark.
```

where `id = _clean(row.get("id"), 40)`, `technique = _clean(row.get("technique"), 200)`,
`mechanism = _clean(row.get("mechanism"))`,
`adoption_sketch = _clean(row.get("adoption_sketch"))`,
`evidence = ", ".join(str(e) for e in (row.get("evidence") or [])[:12]) or "none"`,
`benchmark = spec.benchmark`, `surface = ", ".join(spec.surface)`.

**`eligible(row, *, verdicts=DEFAULT_VERDICTS, skip_disagreement=True, skip_single_rater=True) -> bool`**

False when any of: `row.get("verdict") not in verdicts`; `skip_disagreement and
row.get("disagreement")`; `skip_single_rater and row.get("single_rater")`;
`str(row.get("evidence_strength") or "").lower() == "weak"`;
`not _clean(row.get("technique"))`; `not _clean(row.get("id"))`. True otherwise.

**`technique_candidates(rows, *, spec, review_id, now, max_n=5, verdicts=DEFAULT_VERDICTS, skip_disagreement=True, skip_single_rater=True) -> list[Candidate]`**

- Keep eligible rows. Order by `(-float(priority or 0), int(rank or 10**6), id)`.
  Take the first `max_n` (`max_n < 1` → `[]`).
- For each row build with `Candidate.build(...)`:
  - `domain=spec.domain`, `source=f"research:{_clean(row.get('group_id'), 20) or 'G00'}"`,
    `kind=KIND`, `now=now`;
  - `title = f"adopt {id}: {technique}"` truncated to 160 characters;
  - `evidence_key = f"technique:{slug(technique)}"`;
  - `detail = _clean(row.get("mechanism"), 400)`;
  - `files = spec.files`;
  - `fix_hint = fix_hint(row, spec)`;
  - `success_metric = f"{spec.benchmark}: sealed paired gain (G12)"`;
  - `success_command = f"bench:{spec.benchmark}"`;
  - `reach = 1 + max(0, round(float(row.get("priority") or 0))) ** 2`;
  - `evidence = {"review": review_id, "technique_id": id, "group_id": <cleaned group_id>,
    "technique": technique, "priority": float(priority or 0),
    "verdict": row.get("verdict"), "papers": list(evidence ids, ≤ 12),
    "benchmark": spec.benchmark, "surface": list(spec.surface),
    "miniork_surface": _clean(row.get("miniork_surface"), 300)}`.
- A row whose `priority` cannot be parsed as a float counts as `0`.

**`@dataclass(frozen=True, slots=True) class ResearchHarvest`** — fields:
`domain: str`, `review: str | None`, `review_id: str | None`,
`age_days: int | None`, `candidates: tuple[Candidate, ...]`,
`notes: tuple[str, ...] = ()`, `error: str | None = None`; method `to_dict()`
returning those fields with `candidates` as a list of
`{"candidate_id", "title", "source", "severity"}` dicts.

**`harvest_domain(home: Path, spec: DomainSpec, *, now: int, max_n=5, verdicts=DEFAULT_VERDICTS, skip_disagreement=True, skip_single_rater=True) -> ResearchHarvest`**

- `path = latest_review(home, spec.review_glob)`.
- `None` → `ResearchHarvest(domain, None, None, None, (), notes=(f"no technique review for {spec.domain} matching {spec.review_glob}",))`.
- `review_id = path.parent.name`;
  `age_days = max(0, (now - int(path.stat().st_mtime)) // 86400)`.
- `load_ranked` raising `ValueError`, `OSError` or `json.JSONDecodeError` →
  `ResearchHarvest(..., candidates=(), error=f"unreadable review {path}: {exc}")`.
- Otherwise `candidates = tuple(technique_candidates(...))`; `notes` holds one line
  `f"{len(eligible rows)} eligible of {len(rows)} ranked; emitted {len(candidates)}"`.

**`harvest_all(home, specs: Mapping[str, DomainSpec] | None = None, *, now, domains=None, **kw) -> list[ResearchHarvest]`**

`specs or DEFAULT_SPECS`; iterate `sorted(specs)`; skip a domain not in `domains`
when `domains` is given; return one `ResearchHarvest` per domain.

**`__all__`** — every public name above, sorted.

## Files in scope (touch ONLY these)

- `mini_ork/rsi/research.py` (new)
- `mini_ork/rsi/candidates.py` — only the one `KIND_BASE` line and its comment
- `tests/unit/test_rsi_research_py.py` (new)

Do NOT modify any other file (not `harvest.py`, `cycle.py`, `select.py`, `queue.py`).

## Tests (`tests/unit/test_rsi_research_py.py`: hermetic, `tmp_path`, no network)

Helper `_review(dirpath, rows)` writes
`{"reviewers": ["a","b"], "technique_count": len(rows), "unscored": [], "disagreement_count": 0, "groups": [], "ranked": rows}`
to `dirpath / "merged-review.json"` (creating the dir). Helper `_row(i, **kw)` returns a
valid row: `id=f"G0{i}-T01"`, `group_id=f"G0{i}"`, `technique=f"Technique number {i}"`,
`mechanism='"Does a thing."'`, `evidence=["arxiv:2601.00001"]`,
`evidence_strength="strong"`, `miniork_surface="verifier"`,
`adoption_sketch='"Add it."'`, `priority=10-i`, `mean_impact=4`,
`verdict="implement-now"`, `single_rater=False`, `disagreement=False`, `rank=i`,
updated with `kw`.

- **KIND_BASE:** `candidates.KIND_BASE["technique_unadopted"] == 44`, and
  `candidates.severity("technique_unadopted", domain="verification") == 49`.
- **latest_review:** two reviews `runs/verify-a` and `runs/verify-b`; set
  `os.utime` so `verify-a` is newer → returns `verify-a/merged-review.json`; no match
  → `None`; a directory named `merged-review.json` is ignored.
- **load_ranked:** a list payload raises `ValueError`; `{"ranked": "x"}` raises; a
  ranked list with a non-dict item drops it.
- **slug:** `slug("Oracle-integrity auditing, with  invariants!") ==
  "oracle-integrity-auditing-with-invariants"`; `slug("!!!") == "technique"`;
  a 200-char input gives ≤ 64 chars not ending in `-`.
- **eligible:** `verdict="watch"` False; `disagreement=True` False (True when
  `skip_disagreement=False`); `single_rater=True` False; `evidence_strength="Weak"`
  False; missing `technique` False.
- **technique_candidates:**
  - five rows with priorities 9,8,7,6,5 and `max_n=3` → three candidates, the first
    titled `adopt G01-T01: Technique number 1`;
  - every candidate: `kind == "technique_unadopted"`, `domain == "verification"`,
    `source == "research:G01"` for the first, `success_command == "bench:verifier-replay"`,
    `files == ("recipes/code-fix/verifiers/test.py",)`;
  - `fix_hint` contains `Mechanism: Does a thing.` (quotes stripped), the benchmark
    name and every surface entry;
  - severity: priority 9 → reach 82 → `severity == 44 + 9 + 5 == 58`
    (`isqrt(81) = 9`, verification criticality 5, one file → no blast cost);
  - the same row harvested twice gives the same `candidate_id`; renaming its `id`
    (`G01-T01` → `G07-T03`) keeps the `candidate_id` (identity is the technique name);
  - `priority="n/a"` is treated as 0; `max_n=0` → `[]`.
- **harvest_domain:**
  - no review → `review is None`, `candidates == ()`, the note mentions the glob;
  - a malformed file (`"not json"`) → `error` starts with `unreadable review`,
    `candidates == ()`;
  - a valid review → `review_id == "verify-b"` (the dir name), `age_days` computed
    from mtime with a fixed `now`, notes like `"3 eligible of 4 ranked; emitted 3"`.
- **harvest_all:** with `domains=["routing"]` → `[]`; default → one harvest for
  `verification`.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_research_py.py tests/unit/test_rsi_candidates_py.py`
- `pytest -q -p no:asyncio tests/unit/test_rsi_harvest_py.py tests/unit/test_rsi_queue_py.py tests/unit/test_rsi_select_py.py`
- `ruff check --select F,E9 mini_ork/rsi/research.py mini_ork/rsi/candidates.py tests/unit/test_rsi_research_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_research_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-2-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-2-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged.
- Running `technique_candidates` on the real
  `.mini-ork/runs/verify-agentic-20261018-215339/merged-review.json` (if present)
  yields 5 candidates without raising.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
