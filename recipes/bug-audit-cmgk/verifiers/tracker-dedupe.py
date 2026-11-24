#!/usr/bin/env python3
"""verifiers/tracker-dedupe.py — drop findings the target repo already has.

A lens panel reasons from code. It cannot recover maintainer intent that lives
only in a closed issue, and it does not know that the same defect was reported
last week. That produced a real duplicate on 2026-11-24: a 4/4 consensus for
`evaluate_script` `sourcePath` became issue #3600 + PR #3602 in
ChromeDevTools/chrome-devtools-mcp, both closed as duplicates of #3453, where
the maintainer had already said the behaviour was intended and two PRs with
the identical fix had been closed.

This verifier checks the synthesis against the target repository's tracker
before the publisher emits it, and moves a finding that is already reported out
of the published artifact into `<run>/already-reported.md`. Nothing is deleted:
the skipped block, the tracked issue and the match reason all land in that file.

Precision over recall, resolved by *where* a term appears. A symbol named in
the finding's title line names the subject; an identifier named only in its body
names the mechanism, and the same mechanism appears under many different issues.
So: a tracker **title** matching a distinctive symbol from the finding's title
line (backticked, snake_case, dotted, or CamelCase — `sourcePath`,
`evaluate_script`, `resolveScriptSource`) skips the finding. A match on a
body-only identifier, or on the file anchor's basename, is recorded as
"possible" and left in the synthesis. Dropping a genuinely new finding costs
more than publishing one duplicate, and a human can read the report.

That distinction is not hypothetical: the first version of this file ranked the
longest identifier in the block, which for the motivating finding was
`pathToFileURL` — a body token — while the tracker's title said `sourcePath`.
The real duplicate came back "possible" and would have shipped.

Inputs (env):
  MINI_ORK_RUN_DIR          run directory (set by the native execute runtime)
  MO_AUDIT_TARGET_REPO      owner/name override; otherwise inferred from git
                            origin of the cwd, then of `<run>/../../../..`
  MO_AUDIT_DEDUPE_OFF=1     skip the whole check (still writes the log)

Output (stdout): one JSON object
  {"verifier": "tracker-dedupe", "pass": bool, "status": "checked"|"partial"
   |"skipped"|"abstained", "skipped": [...], "possible": [...],
   "searched_terms": N, "failed_queries": N, "truncated": [...],
   "evidence_path": "...", "reason": "..."}

`status` is never silently "checked" when nothing could be queried: an absent
`gh`, an unauthenticated `gh`, a repository the token cannot see, or a
per-query failure all land in `abstained` (nothing answered) or `partial`
(some queries answered, some did not) with the failing queries named in
`reason` and in the run's already-reported.md. A tracker query that errors
returns no hits, which is indistinguishable from "no duplicates" — so the two
are counted separately and the count is published. The check never claims more
coverage than it got.

Stdout carries exactly one JSON object and nothing else: the runner tees this
process's stderr into the evidence capture, so a diagnostic printed there makes
`verifier_tracker-dedupe.json` unparsable. Diagnostics go to the LOG file.

Term budget. Each finding gets its own budget (`PER_FINDING_TERMS`), so a long
list cannot starve its tail: a global cap made coverage a function of the
finding's position in the document, and finding 33 onward went unsearched.
Terms already queried for an earlier finding are re-used from the cache and
cost nothing. A finding whose terms ran out before all of them were searched is
listed in `truncated` and in the run's report, so a partial check is visible in
the verdict instead of only in the log.

Exit code: always 0 (caller reads `.pass`). `pass` is false only when the
repository *was* searched, duplicates were found, and the rewrite could not be
applied — the one case where the artifact cannot be trusted to be clean.
"""

import json
import os
import re
import shutil
import subprocess

RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR", "")

# The executor exports ARTIFACT_PATH next to MINI_ORK_RUN_DIR. Honour it when
# it names the run's own synthesis — the node contract should not depend on the
# two agreeing about the filename — but ignore it when the publisher has
# already rewritten it to the repo output path.
_SYNTH = os.path.join(RUN_DIR, "synthesis.md") if RUN_DIR else ""
_ARTIFACT = os.environ.get("ARTIFACT_PATH", "").strip()
if _ARTIFACT and os.path.basename(_ARTIFACT) == "synthesis.md" and os.path.isfile(_ARTIFACT):
    _SYNTH = _ARTIFACT
SYNTH = _SYNTH
LOG = os.path.join(RUN_DIR, "verifier-tracker-dedupe.log") if RUN_DIR else ""
SKIPPED_MD = os.path.join(RUN_DIR, "already-reported.md") if RUN_DIR else ""

BUG_LINE = re.compile(r"^- \[(BUG-[0-9A-Za-z]+)\]\s*(.*)$")
TICKETED = re.compile(r"\[ALREADY-REPORTED:\s*#(\d+)\]")
PATH_ANCHOR = re.compile(r"`([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+):\d+`")
BACKTICK = re.compile(r"`([^`\n]+)`")
TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# A CamelCase identifier only reads as a *symbol* when it is made of at least
# two word segments. `sourcePath`, `OpenOptions`, `MCPClient`, `toJSON` are
# symbols; `Response`, `Session`, `Python` are ordinary capitalised English and
# appear in half the tracker titles on GitHub. Searching the latter as if they
# named the subject is what lets a generic word delete a real finding.
LOWER_HUMP = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*)+$")
UPPER_HUMP = re.compile(r"^[A-Z][A-Za-z0-9]*(?:[A-Z][a-z0-9]+)+$")
# A bare filename is not an identifier. `lib.rs` appears in half the titles of a
# Rust repository; matching one on that and deleting a finding is the same
# defect as matching a generic noun.
FILE_NAME = re.compile(
    r"^[A-Za-z0-9_.-]+\.(?:rs|py|pyi|ts|tsx|js|jsx|mjs|cjs|go|java|kt|rb|c|h|cc|"
    r"cpp|hpp|toml|yaml|yml|json|md|cfg|ini|sh|txt|lock)$",
    re.IGNORECASE,
)

# Per finding, not per run: a global cap made the tail of a long synthesis go
# unsearched (35 findings, 40 terms, findings 33-35 skipped). Terms already in
# the cache are free, so this bounds only *new* tracker queries.
PER_FINDING_TERMS = 4
# Safety valve for a pathological document; hitting it is logged, never silent.
MAX_TOTAL_TERMS = 200
GH_TIMEOUT = 60


def _log_write(lines):
    if not LOG:
        return
    with open(LOG, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _say(lines, message):
    # The LOG file is the evidence artifact; stderr is NOT. The runner tees
    # stderr into the capture that must contain exactly one JSON object, so a
    # line printed here makes verifier_tracker-dedupe.json unparsable and the
    # whole run records `contract: UNVERIFIED` despite passing.
    lines.append(message)


def _git_origin_slug(repo_dir):
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "remote", "get-url", "origin"],
            check=False, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    url = out.stdout.strip()
    match = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$", url)
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def _target_slug():
    explicit = os.environ.get("MO_AUDIT_TARGET_REPO", "").strip()
    if explicit:
        return explicit
    candidates = [os.getcwd()]
    if RUN_DIR:
        candidates.append(os.path.abspath(os.path.join(RUN_DIR, "..", "..", "..")))
    for repo_dir in candidates:
        slug = _git_origin_slug(repo_dir)
        if slug:
            return slug
    return ""


def _split_findings(text):
    """Return (preamble_lines, [(bug_id, block_lines)]) for a `- [BUG-NNN]` list."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if BUG_LINE.match(line):
            start = index
            break
    if start is None:
        return lines, []
    preamble, findings = lines[:start], []
    current = []
    for line in lines[start:]:
        head = BUG_LINE.match(line)
        if head and current:
            findings.append(current)
            current = [line]
        elif line.startswith("## ") and current:
            findings.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        findings.append(current)
    trimmed = []
    for block in findings:
        while block and not block[-1].strip():
            block.pop()
        if block and BUG_LINE.match(block[0]):
            trimmed.append(block)
        else:
            preamble.extend(block)
    return preamble, trimmed


def _distinctive(token):
    """True for a token that reads as a code symbol, not prose.

    `sourcePath`, `evaluate_script`, `McpContext.loadResource` are symbols;
    `response`, `body`, `capped`, `Response`, `Session` are words that every
    tracker is full of. A single capital letter inside a word is not enough
    evidence of code — the shape has to be at least two word segments.
    """
    if len(token) < 6 or not TOKEN.match(token):
        return False
    if FILE_NAME.match(token):
        return False
    if "_" in token or "." in token:
        return True
    return bool(LOWER_HUMP.match(token) or UPPER_HUMP.match(token))


def _finding_terms(block):
    """(strong, weak) search terms for one `- [BUG-NNN] …` block.

    Strong terms come from the title line and are what a duplicate is
    *identified* by. Weak terms — body identifiers and the file anchor's
    basename — can only raise a "possible", never a skip.
    """
    head = BUG_LINE.match(block[0])
    title = head.group(2) if head else block[0]
    # The synthesis writes its own bookkeeping in brackets — `[CONSENSUS: 4/4]`,
    # `[DISPUTED-SEVERITY: …]`, `[ALREADY-REPORTED: #N]`. Those are not subject
    # symbols: `CONSENSUS` is CamelCase-shaped, and searching it matches every
    # tracker that says the word anywhere.
    title = re.sub(r"\[[^\]]*\]", " ", title)
    strong = []
    for code in BACKTICK.findall(title):
        token = code.strip()
        # Backticks are the synthesis author's claim that this is code, but the
        # author is a model: `\`Response\`` in a title is as likely as
        # `\`sourcePath\``. Require the same symbol shape either way, so one
        # generic word cannot delete a genuine finding.
        if _distinctive(token) and token not in strong:
            strong.append(token)
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", title):
        if _distinctive(word) and word not in strong:
            strong.append(word)

    body = "\n".join(block[1:])
    weak = []
    for code in BACKTICK.findall(body):
        token = code.strip()
        if PATH_ANCHOR.fullmatch(f"`{token}`"):
            continue
        if " " in token or "/" in token or "\\" in token:
            continue
        # Same shape rule as the title: a plain capitalised or lowercase word
        # is prose, and searching it only produces a misleading "possible".
        if _distinctive(token):
            if token not in strong and token not in weak:
                weak.append(token)
    for anchor in PATH_ANCHOR.findall(body):
        base = re.split(r"[\\/]", anchor)[-1]
        # A basename shared by every finding in a file (`lib.rs`, `index.ts`)
        # only produces a "possible" line that names nothing.
        if len(base) >= 6 and not FILE_NAME.match(base) \
                and base not in strong and base not in weak:
            weak.append(base)
    return strong[:4], weak[:6]


def _gh(argv):
    try:
        proc = subprocess.run(
            ["gh", *argv], check=False, capture_output=True, text=True,
            timeout=GH_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip()
    try:
        return json.loads(proc.stdout or "[]"), ""
    except ValueError as exc:
        return None, f"unparseable gh output: {exc}"


def _search(slug, term, kind, cache, lines, stats):
    key = (kind, term.lower())
    if key in cache:
        return cache[key]
    argv = [kind, "list", "--repo", slug, "--state", "all",
            "--search", term, "--limit", "20",
            "--json", "number,title,state,url"]
    stats["queries"] += 1
    payload, error = _gh(argv)
    if payload is None:
        # A failed query returns no hits, which is the same value as "this repo
        # has nothing on that term". Count it so the verdict can say the check
        # was incomplete instead of reporting the outage as clean coverage.
        stats["failures"].append(f"{kind} {term!r}: {error}")
        _say(lines, f"gh {kind} list failed for {term!r}: {error}")
        cache[key] = []
        return []
    hits = [item for item in payload if isinstance(item, dict)]
    cache[key] = hits
    _say(lines, f"{kind} search {term!r} -> {len(hits)} hit(s)")
    return hits


def _match(hits, term):
    found = []
    needle = term.lower()
    for item in hits:
        title = str(item.get("title") or "")
        if needle in title.lower():
            found.append({
                "number": item.get("number"),
                "title": title,
                "state": item.get("state"),
                "url": item.get("url"),
            })
    return found


def _bug_id(block):
    match = BUG_LINE.match(block[0])
    return match.group(1) if match else ""


def _rewrite(text, drop_ids, banner):
    """``drop_ids`` maps a dropped finding id to the tracker number it matched."""
    """Replace each dropped finding with a one-line pointer, in place.

    A parse-and-rebuild would reorder the document: the synthesis carries
    trailing sections (`## Disputed entries`, `## Coverage gap report`) after
    the bug list, and any rebuild hoists them above the severity bands. So the
    rewrite walks lines and only ever touches a dropped finding's own block.
    Everything else passes through unchanged, in order.
    """
    if not drop_ids:
        return text
    out = []
    skipping = False
    banner_done = not banner
    for line in text.splitlines():
        head = BUG_LINE.match(line)
        if head:
            skipping = head.group(1) in drop_ids
            if skipping:
                # Written in the `[ALREADY-REPORTED: #N]` shape the analysis
                # step already recognises, so a second pass over this synthesis
                # leaves it alone instead of searching its own breadcrumb.
                number = drop_ids[head.group(1)]
                out.append(
                    f"- [{head.group(1)}] [ALREADY-REPORTED: #{number}] see "
                    f"already-reported.md (was: {head.group(2)[:120]})"
                )
                continue
            out.append(line)
            continue
        if skipping:
            # A finding's body is indented continuation lines. Blank lines carry
            # no content and are kept, so the placeholder does not butt up
            # against the next heading; the first unindented line ends the block.
            if not line.strip():
                out.append("")
                continue
            if not line[:1].isspace():
                skipping = False
            else:
                continue
        out.append(line)
        if not banner_done and line.startswith("# "):
            out.extend(["", banner])
            banner_done = True
    return "\n".join(out).rstrip() + "\n"


def main():
    lines = []
    if not RUN_DIR or not SYNTH:
        print(json.dumps({
            "verifier": "tracker-dedupe", "pass": False, "status": "abstained",
            "skipped": [], "possible": [], "searched_terms": 0,
            "evidence_path": "", "reason": "MINI_ORK_RUN_DIR is not set",
        }))
        return 0
    if not os.path.isfile(SYNTH):
        _log_write(["MISSING synthesis.md"])
        print(json.dumps({
            "verifier": "tracker-dedupe", "pass": False, "status": "abstained",
            "skipped": [], "possible": [], "searched_terms": 0,
            "evidence_path": LOG, "reason": f"no synthesis at {SYNTH}",
        }))
        return 0

    text = open(SYNTH, encoding="utf-8", errors="replace").read()
    if os.environ.get("MO_AUDIT_DEDUPE_OFF") == "1":
        _log_write(["skipped: MO_AUDIT_DEDUPE_OFF=1"])
        print(json.dumps({
            "verifier": "tracker-dedupe", "pass": True, "status": "skipped",
            "skipped": [], "possible": [], "searched_terms": 0,
            "evidence_path": LOG, "reason": "disabled by MO_AUDIT_DEDUPE_OFF",
        }))
        return 0

    slug = _target_slug()
    if not slug:
        _log_write(["abstained: no target repository could be resolved"])
        print(json.dumps({
            "verifier": "tracker-dedupe", "pass": True, "status": "abstained",
            "skipped": [], "possible": [], "searched_terms": 0,
            "evidence_path": LOG,
            "reason": "no target repo: set MO_AUDIT_TARGET_REPO=owner/name",
        }))
        return 0
    if shutil.which("gh") is None:
        _log_write([f"abstained: gh not on PATH for {slug}"])
        print(json.dumps({
            "verifier": "tracker-dedupe", "pass": True, "status": "abstained",
            "skipped": [], "possible": [], "searched_terms": 0,
            "evidence_path": LOG,
            "reason": "gh is not on PATH; duplicate check did not run",
        }))
        return 0

    _say(lines, f"target {slug}")
    findings = _split_findings(text)[1]
    if not findings:
        _log_write(lines + ["no [BUG-NNN] entries parsed"])
        print(json.dumps({
            "verifier": "tracker-dedupe", "pass": True, "status": "abstained",
            "skipped": [], "possible": [], "searched_terms": 0,
            "evidence_path": LOG,
            "reason": "synthesis has no parseable [BUG-NNN] entries",
        }))
        return 0

    cache = {}
    stats = {"queries": 0, "failures": []}
    terms_searched = set()
    skip = []
    possible = []
    truncated = []
    for block in findings:
        bug_id = _bug_id(block)
        if not bug_id:
            continue
        if TICKETED.search("\n".join(block)):
            _say(lines, f"{bug_id}: already annotated, left as is")
            continue
        strong, weak = _finding_terms(block)
        if not strong and not weak:
            _say(lines, f"{bug_id}: no searchable symbol, left for review")
            continue
        candidates = []
        seen = set()
        fresh = 0
        for term in strong + weak:
            # Terms another finding already searched come from the cache and are
            # free, so they never count against this finding's budget — only a
            # new tracker query does.
            if term not in terms_searched:
                if fresh >= PER_FINDING_TERMS:
                    _say(lines, f"{bug_id}: per-finding budget "
                                f"({PER_FINDING_TERMS} terms) reached; "
                                "remaining terms unsearched")
                    truncated.append(bug_id)
                    break
                if len(terms_searched) >= MAX_TOTAL_TERMS:
                    _say(lines, f"{bug_id}: global budget ({MAX_TOTAL_TERMS} "
                                "terms) reached; remaining terms unsearched")
                    truncated.append(bug_id)
                    break
                terms_searched.add(term)
                fresh += 1
            for kind in ("issue", "pr"):
                hits = _search(slug, term, kind, cache, lines, stats)
                for match in _match(hits, term):
                    key = (kind, match["number"])
                    if key in seen:
                        continue
                    seen.add(key)
                    match["kind"] = kind
                    match["term"] = term
                    candidates.append(match)
            # One identifying symbol is enough; more terms only re-list the same
            # threads under a second name.
            if term in strong and any(c["term"] == term for c in candidates):
                break
        identified_by = [c for c in candidates if c["term"] in strong]

        def _rank(match):
            # The citation a reader follows should be the thread that actually
            # names the subject: an issue open on it outranks a merged PR that
            # merely mentions one of its symbols, and both outrank a closed one.
            state = str(match["state"]).upper()
            state_rank = 0 if state == "OPEN" else 1 if state == "MERGED" else 2
            title = str(match["title"]).lower()
            named = sum(1 for term in strong if term.lower() in title)
            return (state_rank, -named, match["number"])

        identified_by.sort(key=_rank)
        if identified_by:
            skip.append({"bug": bug_id, "primary": identified_by[0]["term"],
                         "matches": identified_by, "block": list(block)})
        elif candidates:
            possible.append({"bug": bug_id,
                             "primary": strong[0] if strong else candidates[0]["term"],
                             "matches": candidates[:3], "block": list(block)})

    failed = len(stats["failures"])
    if failed and failed >= stats["queries"]:
        status = "abstained"
        reason = (f"all {failed} tracker queries failed; the duplicate check "
                  "did not run")
    elif failed:
        status = "partial"
        reason = (f"{failed} of {stats['queries']} tracker queries failed; "
                  "coverage is incomplete")
    else:
        status = "checked"
        reason = ""
    if failed:
        _say(lines, f"{failed}/{stats['queries']} tracker queries failed "
                    f"-> status {status}")

    if skip or possible or failed or truncated:
        report = ["# Already reported — skipped by tracker-dedupe", ""]
        report.append(
            f"Target: `{slug}`  ·  searched {len(terms_searched)} term(s)"
            + (f"  ·  {failed} quer{'y' if failed == 1 else 'ies'} failed"
               if failed else "")
        )
        report.append("")
        if skip:
            report.append("## Skipped (tracker title matched a symbol from the finding's title)")
            report.append("")
            for entry in skip:
                report.append(f"- `{entry['bug']}` matched on `{entry['primary']}`")
                for match in entry["matches"][:5]:
                    state = str(match["state"]).lower()
                    report.append(
                        f"  - {match['kind']} #{match['number']} "
                        f"({state}) {match['title']} — {match['url']}"
                    )
                extra = len(entry["matches"]) - 5
                if extra > 0:
                    report.append(f"  - …and {extra} more in the same search")
                # The synthesis now holds only a one-line pointer, so the
                # finding's own text has to live here or it is gone for good.
                report.append("  - Original finding:")
                report.append("")
                report.append("    ```")
                report.extend(f"    {line}" for line in entry["block"])
                report.append("    ```")
            report.append("")
        if possible:
            report.append("## Possible (weaker overlap — left in the synthesis)")
            report.append("")
            for entry in possible:
                report.append(f"- `{entry['bug']}` near `{entry['primary']}`")
                for match in entry["matches"]:
                    state = str(match["state"]).lower()
                    report.append(
                        f"  - {match['kind']} #{match['number']} "
                        f"({state}) {match['title']} — {match['url']}"
                    )
            report.append("")
        if truncated:
            report.append("## Findings whose terms ran out of budget (coverage gap)")
            report.append("")
            for bug_id in truncated:
                report.append(f"- `{bug_id}`: later terms were not searched")
            report.append("")
        if stats["failures"]:
            report.append("## Tracker queries that failed (coverage gap)")
            report.append("")
            for failure in stats["failures"][:20]:
                report.append(f"- {failure}")
            extra = len(stats["failures"]) - 20
            if extra > 0:
                report.append(f"- …and {extra} more")
            report.append("")
        with open(SKIPPED_MD, "w", encoding="utf-8") as handle:
            handle.write("\n".join(report))

    skipped_ids = {
        entry["bug"]: (int(entry["matches"][0]["number"]) if entry["matches"] else 0)
        for entry in skip
    }
    needed = any(
        _bug_id(block) in skipped_ids
        and not TICKETED.search("\n".join(block))
        for block in findings
    )
    banner = ""
    if skipped_ids:
        banner = (
            f"> {len(skipped_ids)} finding(s) omitted as already reported upstream — "
            "see `already-reported.md` (restored verbatim in the run directory)."
        )
    if needed:
        rewritten = _rewrite(text, skipped_ids, banner)
        try:
            with open(SYNTH, "w", encoding="utf-8") as handle:
                handle.write(rewritten)
        except OSError as exc:
            lines.append(f"rewrite failed: {exc}")
            _log_write(lines)
            print(json.dumps({
                "verifier": "tracker-dedupe", "pass": False, "status": status,
                "skipped": skip, "possible": possible,
                "searched_terms": len(terms_searched),
                "failed_queries": failed,
                "truncated": truncated,
                "evidence_path": LOG,
                "reason": f"duplicates found but synthesis rewrite failed: {exc}",
            }))
            return 0
        _say(lines, f"omitted {len(skipped_ids)} already-reported finding(s)")

    _log_write(lines)
    print(json.dumps({
        "verifier": "tracker-dedupe", "pass": True, "status": status,
        "skipped": skip, "possible": possible,
        "searched_terms": len(terms_searched),
        "failed_queries": failed,
        "truncated": truncated,
        "evidence_path": LOG,
        "reason": reason,
        "already_reported_path": SKIPPED_MD if (skip or possible or failed or truncated) else "",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
