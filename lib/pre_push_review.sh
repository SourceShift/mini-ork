#!/usr/bin/env bash
# pre_push_review.sh — multi-lens review of the diff about to be pushed.
#
# Layered architecture inside Layer 3 of .githooks/pre-push:
#
#   heuristic        free, ~100ms, runs always. Catches the obvious 60%.
#   llm_panel        opt-in via MO_REVIEW_LLM_LENSES=1. 4-lens panel
#                    (codex + kimi + glm + minimax) on the diff. ~$0.20/push.
#                    No opus arbiter — the heuristic+panel verdicts are
#                    aggregated by frequency × severity (cheap SQL).
#
# Public API:
#   review_run <source_sha> <target_branch> [--mode heuristic|llm_panel|hybrid]
#     Creates a pre_push_reviews row, runs the selected reviewer(s),
#     emits pre_push_review_issues per finding, computes a verdict
#     (approve/warn/block) based on severity counts + target branch.
#     Echoes the review_id on stdout.
#
#   review_verdict_for <review_id>     print just the verdict
#   review_show       <review_id>      print issues
#   review_forward_to_bug_reports <review_id>   create bug_reports rows
#                                                for each open issue

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

# ── heuristic checks ──────────────────────────────────────────────────────
# Each check function takes the diff path and emits JSONL lines on stdout:
# {"lens":"heuristic.<check>","severity":"...","file":"...","line":N,
#  "title":"...","description":"...","suggested_fix":"..."}

_check_bash_syntax() {
  local diff="$1"
  python3 - "$diff" <<'PY'
import json, re, subprocess, sys, os
diff = open(sys.argv[1]).read()
files = set()
for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M):
    f = m.group(1)
    if f.endswith(".sh") or f.startswith("bin/") and not f.endswith(".md"):
        files.add(f)
for f in sorted(files):
    if not os.path.isfile(f):
        continue
    # bin/ holds polyglot CLIs (Python, Node); only bash-shebang files
    # get the bash -n check. Otherwise we'd flag every Python script as
    # a "bash syntax error" critical issue.
    try:
        with open(f) as fh:
            first = fh.readline()
    except Exception:
        continue
    if "bash" not in first and not first.endswith("sh\n"):
        continue
    r = subprocess.run(["bash", "-n", f], capture_output=True, text=True)
    if r.returncode != 0:
        print(json.dumps({
            "lens":"heuristic.bash_syntax", "severity":"critical",
            "file": f, "title": f"bash syntax error in {f}",
            "description": (r.stderr or "")[:500],
            "suggested_fix": "Run `bash -n " + f + "` locally and fix before push.",
        }, separators=(",", ":")))
PY
}

_check_migration_safety() {
  local diff="$1"
  python3 - "$diff" <<'PY'
import json, re, sys
diff = open(sys.argv[1]).read()
# Block on DROP/DELETE in migration files without IF EXISTS / WHERE.
for m in re.finditer(r"^\+\+\+ b/(db/migrations/[^\n]+\.sql)$", diff, re.M):
    f = m.group(1)
    # Find the hunk for this file
    start = m.end()
    end = diff.find("\ndiff --git ", start)
    if end < 0:
        end = len(diff)
    hunk = diff[start:end]
    for added in re.finditer(r"^\+(.*)$", hunk, re.M):
        line = added.group(1)
        up = line.upper().strip()
        if up.startswith(("DROP TABLE","DROP INDEX","DROP COLUMN","DROP VIEW")):
            if "IF EXISTS" not in up:
                print(json.dumps({
                    "lens":"heuristic.migration_safety","severity":"high",
                    "file": f,
                    "title": "DROP without IF EXISTS in migration",
                    "description": f"Line: {line.strip()[:120]}",
                    "suggested_fix": "Add IF EXISTS so repeated runs are idempotent.",
                }, separators=(",", ":")))
        if up.startswith("DELETE FROM") and "WHERE" not in up:
            print(json.dumps({
                "lens":"heuristic.migration_safety","severity":"critical",
                "file": f,
                "title": "Unbounded DELETE in migration",
                "description": f"Line: {line.strip()[:120]}",
                "suggested_fix": "Add a WHERE clause or scope the delete.",
            }, separators=(",", ":")))
PY
}

_check_added_todos() {
  local diff="$1"
  python3 - "$diff" <<'PY'
import json, re, sys
diff = open(sys.argv[1]).read()
added = 0
current_file = None
for line in diff.split("\n"):
    m = re.match(r"^\+\+\+ b/(.+)$", line)
    if m:
        current_file = m.group(1); continue
    if not line.startswith("+") or line.startswith("+++"):
        continue
    if re.search(r"\b(TODO|FIXME|HACK|XXX|KLUDGE)\b", line):
        added += 1
        if added <= 5:
            print(json.dumps({
                "lens":"heuristic.todo_marker","severity":"low",
                "file": current_file or "?",
                "title": "New TODO/FIXME/HACK added",
                "description": line.strip()[:200],
                "suggested_fix": "Resolve in this PR or open an explicit issue.",
            }, separators=(",", ":")))
PY
}

_check_diff_size() {
  local diff="$1"
  python3 - "$diff" <<'PY'
import json, sys, re
diff = open(sys.argv[1]).read()
added   = sum(1 for line in diff.split("\n") if line.startswith("+") and not line.startswith("+++"))
removed = sum(1 for line in diff.split("\n") if line.startswith("-") and not line.startswith("---"))
if added >= 800:
    print(json.dumps({
        "lens":"heuristic.diff_size","severity":"medium",
        "file": "_diff",
        "title": f"Large diff: +{added} / -{removed} lines",
        "description": "Big diffs are harder to review; consider splitting.",
        "suggested_fix": "Split into smaller logical commits where possible.",
    }, separators=(",", ":")))
PY
}

_check_test_pairing() {
  local diff="$1"
  python3 - "$diff" <<'PY'
import json, re, sys
diff = open(sys.argv[1]).read()
new_lib_bin = set()
new_tests   = set()
for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M):
    f = m.group(1)
    if (f.startswith("lib/") or f.startswith("bin/")) and f.endswith(".sh"):
        # only flag NEW files; check that the file appears as new (mode 100755 added)
        # heuristic: if "new file mode" appears in the file's diff block, mark it
        start = m.end(); end = diff.find("\ndiff --git ", start)
        if end < 0: end = len(diff)
        block = diff[start:end]
        if "new file mode" in diff[max(0, start-500):start]:
            new_lib_bin.add(f)
    if f.startswith("tests/"):
        new_tests.add(f)
if new_lib_bin and not new_tests:
    print(json.dumps({
        "lens":"heuristic.test_pairing","severity":"low",
        "file": ",".join(sorted(new_lib_bin))[:200],
        "title": f"{len(new_lib_bin)} new lib/bin file(s) without paired test changes",
        "description": "New executable code added but no tests/ files changed.",
        "suggested_fix": "Add at least one smoke test in tests/integration/ or tests/unit/.",
    }, separators=(",", ":")))
PY
}

_check_secret_patterns() {
  local diff="$1"
  python3 - "$diff" <<'PY'
import json, re, sys
diff = open(sys.argv[1]).read()
PATTERNS = [
    (r"AKIA[0-9A-Z]{16}",                              "AWS access key"),
    (r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----",  "private key"),
    (r"ghp_[A-Za-z0-9]{30,}",                          "GitHub PAT"),
    (r"sk-[A-Za-z0-9]{20,}",                           "OpenAI-style API key"),
    (r"xoxb-\d+-\d+-",                                 "Slack bot token"),
]
current_file = None
for line in diff.split("\n"):
    m = re.match(r"^\+\+\+ b/(.+)$", line)
    if m:
        current_file = m.group(1); continue
    if not line.startswith("+") or line.startswith("+++"):
        continue
    for pat, name in PATTERNS:
        if re.search(pat, line):
            print(json.dumps({
                "lens":"heuristic.secret_leak","severity":"critical",
                "file": current_file or "?",
                "title": f"Possible {name} added to repo",
                "description": "Matched pattern: " + pat,
                "suggested_fix": "Remove the secret + rotate the credential immediately.",
            }, separators=(",", ":")))
            return
PY
}

# ── LLM-panel lens (opt-in via MO_REVIEW_LLM_LENSES=1) ────────────────────
# Reads the model panel from MO_REVIEW_PANEL (default "codex kimi glm
# minimax"; the cheap 4-family panel per the cost-conscious default) and
# dispatches each via mo_llm_dispatch from lib/llm-dispatch.sh. Each lens
# gets the diff + a review prompt; the response is parsed as a JSON
# {issues:[...]} array and merged into the same JSONL stream as the
# heuristic checks.
#
# Operators may add opus or sonnet to the panel for hard-judgment review
# (e.g. MO_REVIEW_PANEL="codex kimi glm minimax opus"); the agents.yaml
# STANDING DIRECTIVE allows opus + sonnet but bans gemini.
#
# Budget guard: total wall-clock capped at MO_REVIEW_PANEL_TIMEOUT_S
# (default 240s) across all lenses. Cost is bounded by per-lens
# max_turns=4 (review is a single-pass task, no tool use needed).

_review_llm_prompt() {
  # Stable review prompt — same across lenses so we can compare verdicts.
  cat <<'PROMPT'
You are a code reviewer for the mini-ork project. Review the unified diff
below and identify CONCRETE issues only. Do NOT critique style preferences
or speculate. Focus on:
  - bugs that will manifest at runtime
  - security concerns (secret leak, command injection, SQL injection)
  - test gaps for new behavior
  - migration safety (data loss, idempotence)
  - architectural concerns specific to the change

Return ONLY a JSON object with this exact shape (no prose, no markdown):

  {
    "issues": [
      {
        "severity": "low|medium|high|critical",
        "file":     "path/to/file",
        "line":     <integer or null>,
        "title":    "one-line summary <= 120 chars",
        "description": "what is wrong, max 400 chars",
        "suggested_fix": "what to do, max 300 chars"
      }
    ]
  }

If you find no issues, return {"issues": []}.

--- DIFF FOLLOWS ---
PROMPT
}

_check_llm_lens() {
  local model="$1" diff_file="$2"
  local dispatch_lib="$MINI_ORK_ROOT/lib/llm-dispatch.sh"
  [ -f "$dispatch_lib" ] || return 0
  # shellcheck disable=SC1090
  source "$dispatch_lib" 2>/dev/null || return 0
  declare -f mo_llm_dispatch >/dev/null 2>&1 || return 0

  local prompt_file out_file
  prompt_file=$(mktemp /tmp/mo-review-prompt-XXXXXX)
  out_file=$(mktemp /tmp/mo-review-out-XXXXXX)
  {
    _review_llm_prompt
    cat "$diff_file"
  } > "$prompt_file"

  local timeout="${MO_REVIEW_LENS_TIMEOUT_S:-180}"
  local prompt_text
  prompt_text="$(< "$prompt_file")"
  if ! mo_llm_dispatch "$model" "$prompt_text" "$out_file" "$timeout" 4 \
       >/dev/null 2>&1; then
    rm -f "$prompt_file" "$out_file" "${out_file}.err.log"
    return 0  # fail-open per lens
  fi

  # Parse the lens output. Tolerate prose-wrapped JSON by extracting the
  # outermost {...} block.
  python3 - "$out_file" "$model" <<'PY'
import json, re, sys
out_path, model = sys.argv[1:3]
try:
    text = open(out_path).read()
except OSError:
    sys.exit(0)
# Try direct parse first, then extract braces.
def _try_parse(s):
    try:
        return json.loads(s)
    except Exception:
        return None
d = _try_parse(text)
if not d:
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        d = _try_parse(m.group(0))
if not d or not isinstance(d, dict):
    sys.exit(0)
issues = d.get("issues", [])
if not isinstance(issues, list):
    sys.exit(0)
emitted = 0
for it in issues:
    if not isinstance(it, dict): continue
    sev = (it.get("severity") or "medium").lower()
    if sev not in {"low","medium","high","critical","info"}:
        sev = "medium"
    title = (it.get("title") or "").strip()[:300]
    if not title: continue
    print(json.dumps({
        "lens":         f"llm.{model}",
        "severity":     sev,
        "file":         (it.get("file") or "?")[:200],
        "line":         it.get("line"),
        "title":        title,
        "description":  (it.get("description") or "")[:1000],
        "suggested_fix": (it.get("suggested_fix") or "")[:500],
    }, separators=(",", ":")))
    emitted += 1
    if emitted >= 8: break
PY
  rm -f "$prompt_file" "$out_file" "${out_file}.err.log"
}

# Run the configured panel sequentially (parallel via & + wait is possible
# but sequential keeps cost predictable + output stable).
_run_llm_panel() {
  local diff_file="$1"
  local panel="${MO_REVIEW_PANEL:-codex kimi glm minimax}"
  local m
  for m in $panel; do
    # Enforce the agents.yaml gemini ban at the panel level too.
    case "$m" in
      gemini|*-gemini-*|gemini-*) continue ;;
    esac
    _check_llm_lens "$m" "$diff_file" 2>/dev/null || true
  done
}

# ── orchestrator ──────────────────────────────────────────────────────────

review_run() {
  local source_sha="${1:?source_sha required}"
  local target_branch="${2:?target_branch required}"
  local mode="heuristic"
  local base_override=""
  shift 2
  while [ $# -gt 0 ]; do
    case "$1" in
      --mode) mode="$2"; shift 2 ;;
      --base) base_override="$2"; shift 2 ;;
      *) shift ;;
    esac
  done

  # Compute the diff (from origin's target_branch to source_sha; fall back to
  # other heuristics). --base <sha> overrides everything.
  local base
  if [ -n "$base_override" ]; then
    base="$base_override"
  else
    base=$(git merge-base "$source_sha" "origin/$target_branch" 2>/dev/null \
           || git merge-base "$source_sha" main 2>/dev/null \
           || echo "")
    # History rewrites can detach a branch from main entirely (no common
    # ancestor); fall back to the previous commit's SHA so we at least
    # review the tip's changes.
    if [ -z "$base" ]; then
      base=$(git rev-parse "$source_sha^" 2>/dev/null || echo "")
    fi
  fi
  local diff_file
  diff_file=$(mktemp /tmp/mo-review-diff-XXXXXX)
  if [ -n "$base" ]; then
    git diff "$base".."$source_sha" > "$diff_file"
  else
    git show "$source_sha" > "$diff_file"
  fi

  local files_changed lines_added lines_removed
  files_changed=$(git diff --shortstat "$base".."$source_sha" 2>/dev/null \
                  | grep -oE '[0-9]+ file' | grep -oE '[0-9]+' | head -1)
  files_changed="${files_changed:-0}"
  # grep -c always returns a number on its own line; do not chain `|| echo 0`
  # — it appends a second line when grep returns 0, breaking the SQL bind.
  lines_added=$(grep -cE "^\+[^+]" "$diff_file" 2>/dev/null)
  lines_added="${lines_added:-0}"
  lines_removed=$(grep -cE "^-[^-]" "$diff_file" 2>/dev/null)
  lines_removed="${lines_removed:-0}"

  # Insert the review row.
  local review_id
  review_id=$(sqlite3 "$STATE_DB" "
    INSERT INTO pre_push_reviews
      (reviewed_at, source_sha, target_branch, reviewer_mode,
       files_changed, lines_added, lines_removed, verdict)
    VALUES (strftime('%s','now'), '$source_sha', '$target_branch', '$mode',
            ${files_changed:-0}, ${lines_added:-0}, ${lines_removed:-0}, 'pending');
    SELECT last_insert_rowid();")

  # Run heuristics. Each emits JSONL on stdout.
  local issues_file
  issues_file=$(mktemp /tmp/mo-review-issues-XXXXXX)
  mv "$issues_file" "${issues_file}.jsonl"
  issues_file="${issues_file}.jsonl"
  touch "$issues_file"
  {
    _check_bash_syntax        "$diff_file"
    _check_migration_safety   "$diff_file"
    _check_added_todos        "$diff_file"
    _check_diff_size          "$diff_file"
    _check_test_pairing       "$diff_file"
    _check_secret_patterns    "$diff_file"
    # LLM-panel lens — opt-in. Runs the panel from MO_REVIEW_PANEL
    # (default: codex kimi glm minimax — STANDING DIRECTIVE in agents.yaml)
    # via mo_llm_dispatch from lib/llm-dispatch.sh, which auto-resolves
    # to either lib/providers/cl_<model>.sh or a config/providers.yaml
    # registry entry. NEVER hardcodes provider paths.
    if [ "$mode" = "llm_panel" ] || [ "$mode" = "hybrid" ]; then
      _run_llm_panel "$diff_file"
    fi
  } 2>/dev/null > "$issues_file" || true

  # Persist issues to pre_push_review_issues.
  python3 - "$STATE_DB" "$review_id" "$issues_file" <<'PY'
import json, sqlite3, sys
db, rid, issues_file = sys.argv[1:4]
con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
with open(issues_file) as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        con.execute("""
            INSERT INTO pre_push_review_issues
                (review_id, lens, severity, file_path, line_no,
                 title, description, suggested_fix, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """, (rid, d.get("lens","?"), d.get("severity","medium"),
              d.get("file","?"), d.get("line"),
              (d.get("title") or "")[:300],
              (d.get("description") or "")[:2000],
              (d.get("suggested_fix") or "")[:1000]))
con.commit(); con.close()
PY

  # Compute verdict.
  python3 - "$STATE_DB" "$review_id" "$target_branch" <<'PY'
import sqlite3, sys
db, rid, target = sys.argv[1:4]
con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
critical = con.execute(
    "SELECT COUNT(*) FROM pre_push_review_issues WHERE review_id=? AND severity='critical' AND status='open'",
    (rid,)).fetchone()[0]
high = con.execute(
    "SELECT COUNT(*) FROM pre_push_review_issues WHERE review_id=? AND severity='high' AND status='open'",
    (rid,)).fetchone()[0]
total = con.execute(
    "SELECT COUNT(*) FROM pre_push_review_issues WHERE review_id=? AND status='open'",
    (rid,)).fetchone()[0]

# Heuristic HIGHs are DETERMINISTIC checks (bash syntax, migration safety,
# secret leak, …). They are reliable, so they block on their own.
heuristic_high = con.execute(
    "SELECT COUNT(*) FROM pre_push_review_issues "
    "WHERE review_id=? AND severity='high' AND status='open' "
    "AND lens LIKE 'heuristic.%'",
    (rid,)).fetchone()[0]

# LLM-panel HIGHs are STOCHASTIC: a single lens flags inconsistently across
# byte-identical commits (observed: review 41 approve/high=0 vs review 43
# block/high=4 on the SAME sha). A single non-deterministic lens must not gate
# main. Require CONSENSUS — >=2 DISTINCT llm lenses flagging a HIGH on the same
# file — before a panel HIGH counts toward blocking. Single-lens panel HIGHs
# are still recorded and surfaced, but demoted to warn (they do not block).
consensus_high = con.execute(
    "SELECT COUNT(*) FROM ("
    "  SELECT file_path FROM pre_push_review_issues "
    "  WHERE review_id=? AND severity='high' AND status='open' "
    "    AND lens LIKE 'llm.%' AND file_path IS NOT NULL "
    "  GROUP BY file_path HAVING COUNT(DISTINCT lens) >= 2"
    ")",
    (rid,)).fetchone()[0]

blocking_high = heuristic_high + consensus_high

# Verdict policy:
#   any critical                         -> block always
#   blocking HIGH (heuristic or >=2-lens   -> block on main, warn on feature
#     consensus) + target=main
#   single-lens panel HIGH               -> warn (recorded, non-blocking)
#   medium/low                           -> warn if >5 else approve
to_main = target in ("main","master")
if critical > 0:
    verdict = "block"
elif blocking_high > 0:
    verdict = "block" if to_main else "warn"
elif high > 0 or total > 5:
    verdict = "warn"
else:
    verdict = "approve"

con.execute("""
    UPDATE pre_push_reviews
       SET verdict=?, issues_open=?, issues_critical=?,
           rationale=?
     WHERE id=?
""", (verdict, total, critical,
      f"target={target} crit={critical} high={high} blocking={blocking_high} (heuristic={heuristic_high}, consensus={consensus_high}) total={total}", rid))
con.commit(); con.close()
PY

  rm -f "$diff_file" "$issues_file"
  echo "$review_id"
}

review_verdict_for() {
  local rid="${1:?review_id required}"
  sqlite3 "$STATE_DB" "SELECT verdict FROM pre_push_reviews WHERE id=$rid;"
}

review_show() {
  local rid="${1:?review_id required}"
  sqlite3 -separator ' | ' "$STATE_DB" \
    "SELECT verdict, files_changed, lines_added, lines_removed,
            issues_open, issues_critical, rationale
       FROM pre_push_reviews WHERE id=$rid;"
  echo
  sqlite3 -separator ' | ' "$STATE_DB" \
    "SELECT printf('%-9s', severity),
            printf('%-25s', substr(lens,1,25)),
            printf('%-30s', substr(COALESCE(file_path,'?'),1,30)),
            substr(title,1,70)
       FROM pre_push_review_issues
      WHERE review_id=$rid AND status='open'
      ORDER BY
        CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                      WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC;"
}

# Forward open issues to bug_reports so the existing fix-loop infra
# (bug_report_promote -> epics ingest -> scheduler) can act on them.
review_forward_to_bug_reports() {
  local rid="${1:?review_id required}"
  if [ ! -f "$MINI_ORK_ROOT/lib/bug_report.sh" ]; then
    echo "review_forward: lib/bug_report.sh not found" >&2
    return 1
  fi
  # shellcheck source=lib/bug_report.sh
  source "$MINI_ORK_ROOT/lib/bug_report.sh"
  local n=0
  while IFS=$'\t' read -r iid lens sev file title desc fix; do
    [ -z "$iid" ] && continue
    # Emit via bug_report channel
    MINI_ORK_RUN_DIR="${MINI_ORK_HOME:-.mini-ork}/runs/review-$rid" \
      bug_report_emit "review.$lens" "$sev" "$title" "$desc" "$fix" "$file" 0.85
    # Mark the issue as forwarded
    sqlite3 "$STATE_DB" \
      "UPDATE pre_push_review_issues SET status='open' WHERE id=$iid;"
    n=$((n+1))
  done < <(sqlite3 -separator $'\t' "$STATE_DB" \
            "SELECT id, lens, severity, COALESCE(file_path,''), title,
                    COALESCE(description,''), COALESCE(suggested_fix,'')
               FROM pre_push_review_issues
              WHERE review_id=$rid AND status='open';")
  # Sweep + promote so they land as epics.
  mkdir -p "${MINI_ORK_HOME:-.mini-ork}/runs/review-$rid"
  bug_report_sweep --all >/dev/null 2>&1 || true
  echo "$n"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "pre_push_review.sh — source me and call review_run / review_verdict_for / review_show / review_forward_to_bug_reports"
fi
