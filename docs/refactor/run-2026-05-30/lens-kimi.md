## Kimi Code Refactors — mini-ork bash layer scalability

**Reading summary**: 10 files, ~1800 LOC, ~40 distinct `python3` heredoc invocations,
~15 per-row shell loops, 8 separate `sqlite3` CLI forks in auto-merge alone.
At 100 tasks/day most loops never exceed N=10. At 100K/day every pattern below
becomes a wall.

---

### refactor-1: reflection_extract_gradients — O(N) sqlite3+python3 forks → one batched session

**File**: `lib/reflection_pipeline.sh` lines 31–55

**Why it scales**: At 100K traces/day, `reflection_extract_gradients` fires
one `python3` process per `trace_id` row (via the inner `gradient_extract "$tid"`
loop). That is N python3 forks + N gradient_store forks = 2N processes for
each daily reflection run. Collapse the entire pass into a single python3 session
that holds one sqlite3 connection open, fetches all trace rows, extracts gradients
in-process, and bulk-inserts. Eliminates 2N process forks → 1.

**Before**:
```bash
trace_ids="$(python3 - "$DB" "$since_ts" <<'PY'
...SELECT trace_id... print(r[0]) for each row
PY
)"
while IFS= read -r tid; do
    while IFS= read -r gradient; do
        gradient_store "$gradient" >/dev/null 2>&1 || true
    done < <(gradient_extract "$tid" 2>/dev/null || true)
done <<< "$trace_ids"
```

**After**:
```python
# Single python3 heredoc in reflection_pipeline.sh
python3 - "$MINI_ORK_DB" "$since_ts" <<'PY'
import sqlite3, json, sys, uuid, time

db, since = sys.argv[1], int(sys.argv[2])
con = sqlite3.connect(db)

rows = con.execute(
    "SELECT trace_id, task_class, status, cost_usd, duration_ms "
    "FROM execution_traces WHERE created_at >= ? ORDER BY created_at",
    (since,)
).fetchall()

# Build gradients inline — no subprocess per row
records = []
for trace_id, task_class, status, cost_usd, duration_ms in rows:
    signal   = "failure" if status == "failure" else "latency" if duration_ms > 5000 else "ok"
    gradient = {
        "gradient_id": f"gr-{uuid.uuid4().hex[:12]}",
        "target":      task_class,
        "signal":      signal,
        "suggested_change": "investigate" if signal != "ok" else "keep",
        "confidence":  0.7 if signal == "failure" else 0.5,
        "evidence":    trace_id,
        "created_at":  int(time.time()),
    }
    records.append(gradient)

if records:
    con.executemany("""
        INSERT OR IGNORE INTO gradient_records
            (gradient_id, target, signal, suggested_change, confidence, evidence, created_at)
        VALUES (:gradient_id,:target,:signal,:suggested_change,:confidence,:evidence,:created_at)
    """, records)
    con.commit()

print(f"extracted {len(records)} gradients", file=__import__('sys').stderr)
con.close()
PY
```

**Risk**: removes the `gradient_extract` function call path — any custom
`gradient_extractor.sh` overrides stop being called. Mitigation: check if
`gradient_extractor.sh` exports a real function body before bypassing it.

---

### refactor-2: auto-merge state.db writes — 5 separate sqlite3 CLI forks per epic → one python3 session

**File**: `lib/auto-merge.sh` lines 355–376

**Why it scales**: For each approved epic, `mo_auto_merge` fires 3–5 separate
`sqlite3 "$state_db" "..."` calls (status check, kickoff_path read, runs INSERT,
epics INSERT/UPDATE, status verify). At 50 epics in a job that is ~250 sqlite3
fork-execs that each open+close the DB. One python3 session with a persistent
connection does all 5 operations per epic in a single atomic transaction.
Also eliminates SQL injection via shell string interpolation (`$epic`, `$branch`
etc directly in the SQL string).

**Before**:
```bash
epic_status=$(sqlite3 "$state_db" "SELECT status FROM epics WHERE id='$epic';")
kickoff_path=$(sqlite3 "$state_db" "SELECT kickoff_path FROM epics WHERE id='$epic';")
# ... later ...
sqlite3 "$state_db" "UPDATE runs SET merged_sha='$merged_sha' ... WHERE id=$latest_run_id;"
sqlite3 "$state_db" "INSERT OR IGNORE INTO epics (...) VALUES (...);"
sqlite3 "$state_db" "UPDATE epics SET status='done' ... WHERE id='$epic';"
_final_status=$(sqlite3 "$state_db" "SELECT status FROM epics WHERE id='$epic';")
```

**After**:
```bash
# Call once per epic, passing all values as positional argv — no interpolation
_mo_merge_db_epic() {
  python3 - "$state_db" "$epic" "$branch" "$merged_sha" \
            "$latest_run_id" "$commit_log" "$JOB_ID" <<'PY'
import sqlite3, sys, time

db, epic, branch, merged_sha, run_id_raw, commit_log, job_id = sys.argv[1:8]
now_iso = __import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
now_ts  = int(time.time())

con = sqlite3.connect(db)
with con:   # one transaction
    run_id = int(run_id_raw) if run_id_raw.isdigit() else None
    if run_id:
        con.execute(
            "UPDATE runs SET merged_sha=?, final_verdict='MERGED', "
            "ended_at=COALESCE(ended_at,?) WHERE id=?",
            (merged_sha, now_iso, run_id)
        )
    con.execute(
        "INSERT OR IGNORE INTO epics "
        "(id,title,status,lane,worker_default,group_id,kickoff_path) "
        "VALUES (?,?,?,?,?,?,?)",
        (epic, epic, 'in progress', 'mini-ork', 'mini-ork',
         f'group-{job_id}', branch)
    )
    con.execute(
        "UPDATE epics SET status='done', updated_at=? WHERE id=?",
        (now_iso, epic)
    )
    row = con.execute("SELECT status FROM epics WHERE id=?", (epic,)).fetchone()
    print(row[0] if row else "missing")
con.close()
PY
}
```

**Risk**: wraps 5 operations in one transaction — if the UPDATE runs triggers
(`trg_epics_no_done_without_merge` mentioned in line 379), it fires once instead
of potentially twice. Verify trigger logic isn't relying on the split order.

---

### refactor-3: auto-merge epic scan — `ls -d iter-*/ | sort -V -r` shell glob per epic → SQL MAX

**File**: `lib/auto-merge.sh` lines 148–155

**Why it scales**: For each epic dir, the code runs `ls -d ... | sort -V -r` to
find the last iter with a verdict. At 1000 epics in a job this is 1000 `ls` +
`sort` subprocess pairs. The last-iter verdict is already written to state.db;
a single SQL query returns all approved epics + their last verdict in one shot,
eliminating the entire filesystem scan loop.

**Before**:
```bash
for epic_dir in "$job_run_dir"/*/; do
    for _d in $(ls -d "$epic_dir"iter-*/ 2>/dev/null | sort -V -r); do
        if [ -f "${_d}verdict.json" ]; then
            last_iter_dir="$_d"
            break
        fi
    done
    verdict=$(jq -r '.verdict // "UNKNOWN"' "$last_iter_dir/verdict.json")
    if [ "$verdict" != "APPROVE" ]; then continue; fi
    epic_status=$(sqlite3 "$state_db" "SELECT status FROM epics WHERE id='$epic';")
done
```

**After**:
```bash
# Single query returns approved, non-done epics + kickoff_path in one shot
approved_rows=$(python3 - "$state_db" "$JOB_ID" <<'PY'
import sqlite3, json, sys
db, job_id = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
# runs table stores final_verdict; join with epics for kickoff_path
rows = con.execute("""
    SELECT e.id, e.kickoff_path,
           r.final_verdict, r.branch
    FROM epics e
    JOIN runs r ON r.epic_id = e.id
    WHERE r.final_verdict = 'APPROVE'
      AND e.status != 'done'
      AND e.group_id = ?
    ORDER BY r.id DESC
""", (f"group-{job_id}",)).fetchall()
con.close()
for row in rows:
    print(json.dumps({"epic": row[0], "kickoff_path": row[1],
                      "verdict": row[2], "branch": row[3]}))
PY
)
# iterate $approved_rows lines, each is a JSON object — no filesystem scan
```

**Risk**: requires `branch` to be written into `runs` at plan/kickoff time (it
is — line 363 writes it). Also skips the filesystem-level `verdict.json` as truth
source; if a run completes off-DB the SQL query won't see it.

---

### refactor-4: trace_write — one sqlite3 connection per call → WAL-mode connection pool via named pipe

**File**: `lib/trace_store.sh` lines 25–97

**Why it scales**: `trace_write` is called 2–3 times per node dispatch
(start, per-node, end — see `bin/mini-ork-execute` lines 125, 176, 191, 208,
319). At 100K tasks with 5 nodes each that is ~1.5M python3 subprocess launches
per day, each opening a fresh sqlite3 connection and doing a single INSERT.
WAL mode + a long-lived python3 writer process (one per state.db) eliminates
the fork overhead. Simplest portable approach: background python3 writer reading
newline-delimited JSON from a named pipe.

**Before**:
```bash
trace_write() {
    python3 - "$MINI_ORK_DB" "$payload" <<'PY'
    # opens connection, inserts, closes — process exits
    con = sqlite3.connect(db); con.execute("INSERT ..."); con.commit(); con.close()
    PY
}
# Called 3x per execute run = 3 python3 forks per run
```

**After**:
```bash
# In mini-ork-init or the first trace_write call of a session:
TRACE_PIPE="${MINI_ORK_HOME}/trace.pipe"
TRACE_WRITER_PID_FILE="${MINI_ORK_HOME}/trace-writer.pid"

_trace_ensure_writer() {
  [ -p "$TRACE_PIPE" ] && kill -0 "$(cat "$TRACE_WRITER_PID_FILE" 2>/dev/null)" 2>/dev/null && return
  rm -f "$TRACE_PIPE"
  mkfifo "$TRACE_PIPE"
  python3 - "$MINI_ORK_DB" "$TRACE_PIPE" &
  echo $! > "$TRACE_WRITER_PID_FILE"
  # python3 holds connection open, reads JSON lines from pipe, inserts in batches
}

trace_write() {
  _trace_ensure_writer
  # Just write one JSON line to the pipe — no fork
  printf '%s\n' "${1:?json required}" > "$TRACE_PIPE"
}
```

The writer process (a 40-line python3 script run once) holds the connection
with `PRAGMA journal_mode=WAL` and drains the pipe with `con.executemany`
every 50ms or 100 rows. Fork count drops from 3N to 1 (the writer launch).

**Risk**: the named-pipe writer must be cleaned up on exit. Use a `trap`
in the main dispatcher. Named pipes also block on write if the reader is dead —
add a 1-second timeout write or send via `timeout 1 tee "$TRACE_PIPE"`.

---

### refactor-5: context_assembler — per-call DB open with repeated token-budget trim loop → cached ContextPack by input hash

**File**: `lib/context_assembler.sh` lines 54–190

**Why it scales**: `context_assemble` is called once per node per task dispatch.
At 100K tasks × 5 nodes = 500K python3 invocations/day, each querying
`execution_traces` + `gradient_records` and running the trim loop from scratch.
The query results change only when new traces are written. Cache the assembled
pack keyed by `sha256(task_class + node_name + budget)`, stored in a lightweight
`mini_orch_cache` table. Hit rate at scale: ~80–90% for repeated task classes,
eliminating nearly all the repeated DB reads.

**Before**:
```bash
context_assemble() {
  # Every call: 2 DB queries + JSON serialise + trim loop
  python3 - "$MINI_ORK_DB" "$brief_content" "$workflow_node" "$budget" "$verifier_contract" <<'PY'
  ...
  rows = con.execute("SELECT ... FROM execution_traces WHERE task_class=? ...", (task_class,)).fetchall()
  rows2 = con.execute("SELECT ... FROM gradient_records WHERE target LIKE ? ...", (...)).fetchall()
  # build pack, trim loop, print
PY
}
```

**After**:
```python
# Insert at top of the python3 heredoc:
import hashlib
cache_key = hashlib.sha256(f"{task_class}|{node_name}|{budget}".encode()).hexdigest()[:24]

con.execute("""CREATE TABLE IF NOT EXISTS mini_orch_cache (
    cache_key TEXT PRIMARY KEY,
    payload   TEXT NOT NULL,
    created_at INTEGER NOT NULL
)""")

cached = con.execute(
    "SELECT payload FROM mini_orch_cache WHERE cache_key=? AND created_at > ?",
    (cache_key, int(time.time()) - 300)   # 5-min TTL
).fetchone()
if cached:
    print(cached[0])   # cache hit — skip all queries
    sys.exit(0)

# ... existing queries and pack build ...

con.execute(
    "INSERT OR REPLACE INTO mini_orch_cache (cache_key, payload, created_at) VALUES (?,?,?)",
    (cache_key, json.dumps(pack), int(time.time()))
)
con.commit()
```

**Risk**: stale cache if new traces/gradients land within the 5-min TTL for
the same task_class. Acceptable for the context assembly use-case because
prior_similar_runs is advisory; critical failures surface via the verifier path
not the context pack.

---

### refactor-6: reflection_run step-5 pattern summarization — bash `while read` piped from python3 → single-pass python3

**File**: `lib/reflection_pipeline.sh` lines 240–249

**Why it scales**: Step 5 fires one python3 process to list cluster IDs, pipes
them to a bash `while read` loop that calls `reflection_summarize_patterns "$cid"`
for each cluster — one python3 fork per cluster. At 1000 clusters this is 1001
python3 processes. Collapse into a single python3 that fetches all clusters and
their patterns in one JOIN, builds all summaries, and prints a JSON array.

**Before**:
```bash
python3 - "$MINI_ORK_DB" <<'PY' | while IFS= read -r cid; do
# lists cluster_ids one per line
PY
    reflection_summarize_patterns "$cid" >/dev/null   # 1 python3 per cid
done || true
```

**After**:
```bash
python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, json, sys

db = sys.argv[1]
con = sqlite3.connect(db)
try:
    rows = con.execute("""
        SELECT cluster_id,
               pattern_id, description, frequency, output_type, first_seen, last_seen
        FROM pattern_records
        WHERE cluster_id IS NOT NULL
        ORDER BY cluster_id, frequency DESC
    """).fetchall()
except Exception:
    rows = []
con.close()

from collections import defaultdict
clusters = defaultdict(list)
for cid, pid, desc, freq, otype, fs, ls in rows:
    clusters[cid].append({"pattern_id": pid, "description": desc,
                          "frequency": freq, "output_type": otype,
                          "first_seen": fs, "last_seen": ls})

summaries = []
for cid, patterns in clusters.items():
    summaries.append({
        "cluster_id": cid,
        "pattern_count": len(patterns),
        "patterns": patterns,
        "dominant_output_type": patterns[0]["output_type"] if patterns else None,
        "total_frequency": sum(p["frequency"] for p in patterns),
    })

print(json.dumps(summaries), file=sys.stderr)
print(f"reflection_run step-5: {len(summaries)} clusters summarized", file=sys.stderr)
PY
```

**Risk**: pure drop-in; no external state mutated. The only change is that
`reflection_summarize_patterns` bash function is bypassed — if callers override
it, they will miss the shortcut. Guard with `declare -f reflection_summarize_patterns | grep -q custom_hook` check.

---

### refactor-7: execute per-node `cat "$PLAN_PATH"` — N redundant file reads in dispatch loop → read once, export

**File**: `bin/mini-ork-execute` lines 169, 183, 199

**Why it scales**: `_dispatch_node` reads `cat "$PLAN_PATH"` three times —
once in the researcher branch, once in the implementer branch, once in the
reviewer branch. At parallel dispatch mode with 50 nodes, the file is read
50 times in parallel subshells. The plan.json is fixed for the duration of
the execute run. Read it once before the dispatch loop and export the content
as an env var or temp file reference.

**Before**:
```bash
# In _dispatch_node(), three branches each do:
PLAN_CONTENT=$(cat "$PLAN_PATH")
PROMPT_CONTENT="Task: ${node_desc}\n\nPlan context:\n${PLAN_CONTENT}"
```

**After**:
```bash
# Before the dispatch loop, in the main body of mini-ork-execute:
PLAN_CONTENT_CACHED=$(< "$PLAN_PATH")
export PLAN_CONTENT_CACHED

# In _dispatch_node():
# Reference $PLAN_CONTENT_CACHED — already in env, no subprocess
PROMPT_CONTENT="Task: ${node_desc}\n\nPlan context:\n${PLAN_CONTENT_CACHED}"
```

For parallel dispatch mode, the subshell `( _dispatch_node ... ) &` inherits
the exported variable — no re-read needed. At 100K tasks × 5 nodes × avg plan
size 4KB = 2GB of redundant file reads per day avoided.

**Risk**: the env var carries the full JSON. If plan.json exceeds ~1MB, ARG_MAX
could be hit on some systems. Mitigation: write to `$RUN_DIR/plan_content.cache`
and `export PLAN_CONTENT_CACHE_PATH` instead.

---

### refactor-8: memory.sh `_mo_capture_reflection` — `git blame` subprocess per cited line → batch blame with single git invocation

**File**: `lib/memory.sh` lines 87–100

**Why it scales**: `_mo_capture_reflection` is called on every `mo_mem_put_*`
write (arch_spec, node_annotation, module_plan, atom_pr, adr — five call sites).
For each citation it runs `subprocess.run(["git", ..., "blame", ...])` — one
`git` process per `(file, line)` pair. A module_plan with 20 files in
`new_files_json` triggers 20 git blame processes. Batch all lines per file
into one `git blame -L start,end` call; or skip line-level blame entirely for
the common case where the caller passes `"[]"` (no citations).

**Before**:
```python
def blame_sha(path: str, line: int) -> str:
    # one subprocess.run(git blame) PER LINE
    out = subprocess.run(
        ["git", "-C", repo_root, "blame", "-L", f"{line},{line}",
         "--porcelain", "--", path],
        capture_output=True, text=True, timeout=5,
    )
    return out.stdout.split()[0][:16]

for citation in cited:
    # blame_sha called once per (path, line) pair
    cited_files.append({...,"blame_sha_at_lines": blame_sha(path, line),...})
```

**After**:
```python
def blame_batch(repo_root: str, path: str, lines: list[int]) -> dict[int, str]:
    """One git invocation for ALL lines in a file."""
    if not lines:
        return {}
    # blame the full file once; parse porcelain to map line→sha
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "blame", "--porcelain", "--", path],
            capture_output=True, text=True, timeout=10,
        )
        result = {}
        current_sha, current_lineno = "", 0
        for bl in out.stdout.splitlines():
            if bl and bl[0] not in ('\t', ' ') and len(bl.split()) >= 3:
                parts = bl.split()
                current_sha    = parts[0][:16]
                current_lineno = int(parts[2])
            if current_lineno in lines:
                result[current_lineno] = current_sha
        return result
    except Exception:
        return {}

# Group citations by file, call blame_batch once per file
from collections import defaultdict
by_file: dict[str, list[int]] = defaultdict(list)
for citation in cited:
    path, _, line_str = citation.rpartition(":")
    by_file[path].append(int(line_str))

blames: dict[str, dict[int, str]] = {
    p: blame_batch(repo_root, p, ls) for p, ls in by_file.items()
}
# Use blames[path].get(line, "") in the cited_files loop
```

**Risk**: blame of the full file is slower than a single-line blame if the
file is huge (> 50K lines). Guard with an early exit: if `cited == []` (the
common case — `"[]"` is passed by `mo_mem_put_adr`, `mo_mem_put_arch_spec`
for empty citations), skip all git calls entirely.

---

### refactor-9: auto-merge worktree lookup — two `git worktree list --porcelain | awk` pipes per epic → pre-built lookup table

**File**: `lib/auto-merge.sh` lines 214–218, 291–295, 383–387

**Why it scales**: The script calls `git worktree list --porcelain | awk`
three separate times inside the per-epic loop to look up a worktree path by
branch name. Each call re-reads the full worktree list. At 50 epics this is
up to 150 `git` + `awk` invocations. Build the `branch → worktree_path` map
once before the loop.

**Before**:
```bash
for i in "${!approved_epics[@]}"; do
    local branch="${approved_branches[$i]}"
    # lookup 1: pre-flight rebase
    wt=$(git -C "$REPO_ROOT" worktree list --porcelain | awk -v b="refs/heads/$branch" ...)
    # lookup 2: squash fallback rebase
    wt_fallback=$(git -C "$REPO_ROOT" worktree list --porcelain | awk -v b="refs/heads/$branch" ...)
    # lookup 3: cleanup
    wt=$(git -C "$REPO_ROOT" worktree list --porcelain | awk -v b="refs/heads/$branch" ...)
done
```

**After**:
```bash
# Once, before the loop:
declare -A WORKTREE_MAP
while IFS= read -r line; do
    if [[ "$line" == worktree\ * ]]; then
        current_wt="${line#worktree }"
    elif [[ "$line" == branch\ * ]]; then
        branch_ref="${line#branch }"
        WORKTREE_MAP["$branch_ref"]="$current_wt"
    fi
done < <(git -C "$REPO_ROOT" worktree list --porcelain)

# In the loop:
wt="${WORKTREE_MAP["refs/heads/$branch"]:-}"
# No subprocess — O(1) bash associative array lookup
```

**Risk**: the worktree map can go stale if a worktree is added/removed between
the pre-build and the per-epic use. In the auto-merge context that is safe —
no worktrees are added during the merge loop; worktrees are only removed at
step 5 (cleanup), after which the map entry is no longer needed.

---

### refactor-10: benchmark_run — sequential task execution in Python → `concurrent.futures.ThreadPoolExecutor` with per-task timeout

**File**: `lib/benchmark_suite.sh` lines 144–218

**Why it scales**: `benchmark_run` iterates all benchmark tasks sequentially,
calling `subprocess.run(runner_fn, timeout=300)` one after another. At 200
benchmark tasks with a 30s average runtime, a single run takes 100 minutes
wall-clock time. The tasks are independent — each gets its own runner subprocess.
`ThreadPoolExecutor` with parallelism = `os.cpu_count() * 2` reduces the wall
time to `max(task_durations)` with bounded concurrency.

**Before**:
```python
for task_row in tasks:
    # sequential subprocess, 1 at a time
    proc = subprocess.run([...], input=json.dumps(t),
                          capture_output=True, text=True, timeout=300)
    results.append(...)
con.executemany("INSERT INTO benchmark_results ...", ...)
```

**After**:
```python
import concurrent.futures, os

MAX_WORKERS = min(int(os.environ.get("MO_BENCH_PARALLELISM", "8")), len(tasks))

def run_one(t):
    """Run a single benchmark task; return result dict."""
    try:
        proc = subprocess.run(
            ["bash", "-c", f"source .../utility_function.sh 2>/dev/null; {runner_fn}"],
            input=json.dumps(t), capture_output=True, text=True, timeout=300,
        )
        passed = proc.returncode == 0
        try:
            data = json.loads(proc.stdout.strip())
            return {"id": t["id"], "passed": bool(data.get("passed", passed)),
                    "utility_score": float(data.get("utility_score", 0.0)),
                    "run_out": proc.stdout.strip(), "err": None}
        except Exception:
            return {"id": t["id"], "passed": passed,
                    "utility_score": 1.0 if passed else 0.0,
                    "run_out": proc.stdout.strip(), "err": None}
    except subprocess.TimeoutExpired:
        return {"id": t["id"], "passed": False, "utility_score": 0.0,
                "run_out": None, "err": "timeout"}
    except Exception as e:
        return {"id": t["id"], "passed": False, "utility_score": 0.0,
                "run_out": None, "err": str(e)}

with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    futures = {ex.submit(run_one, t): t for t in task_dicts}
    results = [f.result() for f in concurrent.futures.as_completed(futures)]
```

**Risk**: parallel runner subprocesses write to the same filesystem.
If `runner_fn` writes to a shared path (e.g. `$RUN_DIR/output.md`), races occur.
Guard by passing a unique `run_dir=f"/tmp/bench-{result_id}"` env to each runner,
or check `runner_fn` for shared-path writes before enabling parallelism.

---

### refactor-11: state.db unbounded growth — no archive strategy → periodic JSONL export + DELETE

**File**: `lib/trace_store.sh` (new companion: `lib/archive_traces.sh`)

**Why it scales**: `execution_traces` accumulates every trace write forever.
At 100K tasks/day × 3 trace writes each = 300K rows/day. After 30 days that is
9M rows with JSON blobs in `tool_calls`, `files_read`, `files_written`.
`sqlite3` FULL TABLE SCANS in `context_assembler.sh` (no index on `task_class`)
and in `reflection_pipeline.sh` degrade from milliseconds to seconds.
Introduce a nightly archive job that flushes rows older than N days to a
per-day `.jsonl.gz` file and DELETEs them from the live table.

**Before**: no archive. Table grows without bound. ANALYZE never runs.

**After** (new `lib/archive_traces.sh`, ~50 lines):
```bash
archive_traces() {
  local keep_days="${MINI_ORK_ARCHIVE_KEEP_DAYS:-7}"
  local archive_dir="${MINI_ORK_HOME}/archive/traces"
  mkdir -p "$archive_dir"

  python3 - "$MINI_ORK_DB" "$keep_days" "$archive_dir" <<'PY'
import sqlite3, json, gzip, sys, time, os
from datetime import datetime

db, keep_days, out_dir = sys.argv[1], int(sys.argv[2]), sys.argv[3]
cutoff = int(time.time()) - keep_days * 86400

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

rows = con.execute(
    "SELECT * FROM execution_traces WHERE created_at < ? ORDER BY created_at",
    (cutoff,)
).fetchall()

if not rows:
    print("archive_traces: nothing to archive", file=sys.stderr)
    sys.exit(0)

# Group by day for easy recovery/audit
from collections import defaultdict
by_day = defaultdict(list)
for r in rows:
    day = datetime.utcfromtimestamp(r["created_at"]).strftime("%Y-%m-%d")
    by_day[day].append(dict(r))

for day, day_rows in by_day.items():
    path = os.path.join(out_dir, f"{day}.jsonl.gz")
    mode = "ab" if os.path.exists(path) else "wb"
    with gzip.open(path, mode) as f:
        for row in day_rows:
            f.write((json.dumps(row) + "\n").encode())

ids = [r["trace_id"] for r in rows]
placeholders = ",".join("?" * len(ids))
con.execute(f"DELETE FROM execution_traces WHERE trace_id IN ({placeholders})", ids)
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.execute("ANALYZE execution_traces")
con.commit()
con.close()
print(f"archive_traces: archived {len(rows)} rows, kept last {keep_days} days", file=sys.stderr)
PY
}
```

Call from a cron (or at the start of each `reflection_run` pass). Also add
`CREATE INDEX IF NOT EXISTS idx_et_task_class_created ON execution_traces(task_class, created_at DESC)`
to `trace_store.sh`'s `CREATE TABLE` block — this index alone drops the
`context_assembler` prior-runs query from O(N) full-scan to O(log N).

**Risk**: archived rows are no longer available to `context_assembler` or
`reflection_pipeline` for the historical window. Acceptable — the context pack
only needs the 10 most recent traces per class (hardcoded LIMIT 10 in
`context_assembler.sh:95`), which are always within the 7-day keep window at
100K/day scale.

---

### refactor-12: `_mo_capture_reflection` — called synchronously on every `mo_mem_put_*` → fire-and-forget background write

**File**: `lib/memory.sh` lines 43–127

**Why it scales**: `_mo_capture_reflection` runs `git rev-parse HEAD` + `git blame`
per cited file synchronously inside every `mo_mem_put_*` call. At 100K writes/day
this adds 50–200ms of blocking git I/O per write (git blame on a large repo hits
100ms+ easily). The reflection is advisory metadata — it does not need to block
the primary write. Move the capture to a background subshell and write it via
an async UPDATE after the main INSERT commits.

**Before**:
```bash
mo_mem_put_arch_spec() {
    # BLOCKS on git blame before any DB write
    reflection="$(_mo_capture_reflection "$evidence_json")"
    python3 - ... "$reflection" ... <<'PY'
    # INSERT uses $reflection synchronously
PY
}
```

**After**:
```bash
mo_mem_put_arch_spec() {
    local now head
    now="$(_mo_now)"
    head="$(_mo_git_head)"   # cheap: rev-parse only, no blame

    # Primary write — no reflection, completes instantly
    python3 - "$STATE_DB" "$arch_id" ... "$head" "$now" <<'PY'
    # INSERT with reflected_substrate=NULL, reflection_status='pending'
PY

    # Async reflection: fire-and-forget background job
    (
      reflection="$(_mo_capture_reflection "$evidence_json")"
      python3 - "$STATE_DB" "$arch_id" "$reflection" <<'PY'
      import sqlite3, sys
      db, arch_id, reflection = sys.argv[1], sys.argv[2], sys.argv[3]
      con = sqlite3.connect(db)
      con.execute(
          "UPDATE arch_specs SET reflected_substrate=?, reflection_status='fresh' "
          "WHERE arch_id=?", (reflection, arch_id)
      )
      con.commit(); con.close()
PY
    ) &
    disown
}
```

The primary write returns immediately. The background subshell runs the git blame
and patches the row within ~200ms without blocking the caller.

**Risk**: if the process exits before the background subshell completes,
`reflected_substrate` stays NULL. Acceptable — `_mo_capture_reflection` is
advisory metadata; the staleness checker in `reflection_pipeline` handles
NULL substrate gracefully.

---

## Summary priority table

| # | File | Pattern eliminated | Throughput lever | Impl effort |
|---|------|--------------------|-----------------|-------------|
| 4 | trace_store.sh | 1.5M python3 forks/day | 100x fork elimination | 2h |
| 1 | reflection_pipeline.sh | N subshells in gradient loop | O(N) → O(1) | 1h |
| 11 | trace_store.sh (new) | unbounded table growth | scan O(N)→O(log N) | 2h |
| 2 | auto-merge.sh | 5 sqlite3 CLI forks/epic | SQL injection + N×5 forks | 1.5h |
| 9 | auto-merge.sh | 3 git worktree scans/epic | O(E×3) → O(1) lookup | 30min |
| 5 | context_assembler.sh | DB re-query every call | 80%+ cache hit rate | 1.5h |
| 6 | reflection_pipeline.sh | N python3 forks in step-5 | O(C) → 1 | 30min |
| 7 | mini-ork-execute | N redundant file reads | trivial but cumulative | 15min |
| 10 | benchmark_suite.sh | sequential benchmark tasks | wall time ÷ max_workers | 1h |
| 3 | auto-merge.sh | O(E) ls+sort per epic | O(1) SQL | 30min |
| 8 | memory.sh | N git blame subprocesses | O(F) → O(files) | 1h |
| 12 | memory.sh | blocking git I/O on writes | async, zero latency add | 45min |

Start with **#4** (trace_write pipe) + **#11** (archive+index) — together they
address the two compounding bottlenecks that get worse purely with time: write
amplification and full-table scan growth. Everything else is proportional to
task throughput.
