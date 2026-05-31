# Kimi Lens — Code-Level Refactor Proposals
## mini-ork v0.1.1 Scalability Audit

**Audit date**: 2026-05-31  
**Scope**: bin/, lib/ — read-only analysis; no source modifications  
**Method**: Direct code reading of ~1800 LOC across 18 key files  
**Summary**: ~40 distinct `python3` heredoc forks, ~8 `sqlite3` CLI forks in auto-merge alone,
3 source-inside-function patterns, 2 YAML-parse-via-awk patterns. At 1K/day invisible;
at 100K/day each pattern below becomes a measurable wall.

---

## Finding K-01: `sqlite3` CLI spawned N times in auto-merge epic loop

**File**: `lib/auto-merge.sh:170, 179`  
**Impact**: 2 sqlite3 CLI forks per epic in the merge loop; at 100 epics/job that is 200 process spawns per merge run, plus the per-epic `jq` call on `verdict.json` (line 159).  
**Effort**: M

### Before

```bash
# lib/auto-merge.sh:168-183
local epic_status
epic_status=$(sqlite3 "$state_db" "SELECT status FROM epics WHERE id='$epic';" 2>/dev/null)
if [ "$epic_status" = "done" ]; then
  ...
fi

local kickoff_path
kickoff_path=$(sqlite3 "$state_db" \
  "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
```

### After

```bash
# Batch all per-epic lookups into a single python3 session BEFORE the loop.
# Returns TAB-separated: epic_id \t status \t kickoff_path
declare -A EPIC_STATUS EPIC_KICKOFF
while IFS=$'\t' read -r eid estatus ekickoff; do
  EPIC_STATUS["$eid"]="$estatus"
  EPIC_KICKOFF["$eid"]="$ekickoff"
done < <(python3 - "$state_db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
for row in con.execute("SELECT id, status, kickoff_path FROM epics"):
    print(f"{row[0]}\t{row[1] or ''}\t{row[2] or ''}")
con.close()
PY
)

# Inside loop:
local epic_status="${EPIC_STATUS[$epic]:-}"
local kickoff_path="${EPIC_KICKOFF[$epic]:-}"
```

**Savings**: Reduces 2N sqlite3 CLI forks to 1 python3 fork regardless of N.

---

## Finding K-02: `python3` heredoc spawned per-call for single JSON field extraction

**File**: `lib/utility_function.sh:39-46`, `lib/context_assembler.sh:40-48`, `bin/mini-ork-execute:104-109`  
**Impact**: Each call to `utility_score`, `context_assemble`, and `_dispatch_node` spawns python3 just to extract one string field from JSON. Three separate invocations at the start of every node dispatch.  
**Effort**: S

### Before (utility_function.sh:39-46)

```bash
local task_class
task_class="$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('task_class',''))
except Exception:
    print('')
" "$run_result" 2>/dev/null || echo "")"
```

### After

```bash
# jq is already available in the dependency set (used elsewhere in auto-merge, healer)
local task_class
task_class="$(printf '%s' "$run_result" | jq -r '.task_class // ""' 2>/dev/null || echo "")"
```

**Savings**: Drops python3 startup (~30ms) to jq startup (~5ms) for a 6× speedup per call. Applies identically to the three sites listed above. For `context_assembler.sh:40-48` and `bin/mini-ork-execute:104-109`, replace the heredoc python3 invocations with the same single-line jq pattern.

---

## Finding K-03: `source` called inside hot-path function on every invocation

**File**: `lib/gradient_extractor.sh:65-71`, `lib/reflection_pipeline.sh:26-28`  
**Impact**: `gradient_extract()` calls `source lib/trace_store.sh` and `source lib/llm-dispatch.sh` every time it is invoked — once per trace_id in the reflection loop. Sourcing re-reads and re-executes the file, redefining all functions, on each call.  
**Effort**: S

### Before (gradient_extractor.sh:65-71)

```bash
gradient_extract() {
  local trace_id="${1:?trace_id required}"

  # Fetch the trace JSON
  local trace_json
  # shellcheck source=lib/trace_store.sh
  source "${MINI_ORK_ROOT}/lib/trace_store.sh" 2>/dev/null || true
  if ! declare -f trace_get > /dev/null 2>&1; then
    echo "gradient_extract: trace_store.sh not loaded" >&2
    return 1
  fi
  ...
  source "${MINI_ORK_ROOT}/lib/llm-dispatch.sh" 2>/dev/null || true
```

### After

```bash
gradient_extract() {
  local trace_id="${1:?trace_id required}"

  # Guard: source once per shell session, not once per call
  if ! declare -f trace_get > /dev/null 2>&1; then
    source "${MINI_ORK_ROOT}/lib/trace_store.sh" 2>/dev/null || true
  fi
  if ! declare -f mo_llm_dispatch > /dev/null 2>&1; then
    source "${MINI_ORK_ROOT}/lib/llm-dispatch.sh" 2>/dev/null || true
  fi
  if ! declare -f trace_get > /dev/null 2>&1; then
    echo "gradient_extract: trace_store.sh not loaded" >&2
    return 1
  fi
```

**Savings**: Eliminates redundant file reads + function-redefinition overhead on every call in the reflection loop (O(N) → O(1) source operations).

---

## Finding K-04: `CREATE TABLE IF NOT EXISTS` DDL on every `trace_write` call

**File**: `lib/trace_store.sh:49-62`  
**Impact**: Every single `trace_write` call executes the full `CREATE TABLE IF NOT EXISTS execution_traces (...)` DDL inside python3. SQLite processes it cheaply after the first time, but the python3 process still parses and sends the statement on every invocation. With 2 trace writes per node (start + end) and 10 nodes per plan, that is 20 DDL round-trips per run.  
**Effort**: S

### Before (trace_store.sh:26-62)

```bash
trace_write() {
  local payload="${1:?json_payload required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$payload" <<'PY'
import sqlite3, json, sys, uuid, time

db = sys.argv[1]
...
con = sqlite3.connect(db)
con.execute("""
    CREATE TABLE IF NOT EXISTS execution_traces (
        trace_id            TEXT PRIMARY KEY,
        ...
    )
""")
con.execute("""INSERT INTO execution_traces ... """, (...))
con.commit()
con.close()
PY
}
```

### After

```bash
# One-time schema bootstrap — call once at lib load time or bin startup
trace_init_schema() {
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("""
    CREATE TABLE IF NOT EXISTS execution_traces (
        trace_id            TEXT PRIMARY KEY,
        task_class          TEXT NOT NULL DEFAULT '',
        ...
        created_at          INTEGER NOT NULL
    )
""")
con.execute("CREATE INDEX IF NOT EXISTS idx_et_class_ts ON execution_traces(task_class, created_at)")
con.commit()
con.close()
PY
}

trace_write() {
  local payload="${1:?json_payload required}"
  # No DDL here — assume trace_init_schema already ran at bin startup
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$payload" <<'PY'
import sqlite3, json, sys, uuid, time
db = sys.argv[1]
...
con = sqlite3.connect(db)
con.execute("INSERT INTO execution_traces ... ON CONFLICT ...", (...))
con.commit()
con.close()
PY
}
```

**Savings**: Removes DDL parse overhead from hot path. Bonus: adds an index on `(task_class, created_at)` that `context_assembler.sh` queries at `lib/context_assembler.sh:91-96` with ORDER BY created_at DESC — currently that query does a full-table scan.

---

## Finding K-05: `python3` + `yaml.safe_load` spawned per `llm_dispatch` call for lane lookup

**File**: `lib/llm-dispatch.sh:163-173`  
**Impact**: Every `llm_dispatch` call (every node execution) spawns python3 to parse and query `agents.yaml` for a model lane. At 10 nodes/plan × 1K plans/day = 10K python3 spawns/day just for YAML lane resolution. The file is read-only and tiny; it should be cached.  
**Effort**: S

### Before (llm-dispatch.sh:158-175)

```bash
local model="${model_override:-${MINI_ORK_DEFAULT_MODEL:-sonnet}}"
if [ -z "$model_override" ] && [ -n "$node_type" ]; then
  local _agents_yaml="${MINI_ORK_HOME:-.mini-ork}/config/agents.yaml"
  [ ! -f "$_agents_yaml" ] && _agents_yaml="$MINI_ORK_ROOT/config/agents.yaml"
  if [ -f "$_agents_yaml" ]; then
    local _resolved
    _resolved=$(python3 - "$_agents_yaml" "$node_type" 2>/dev/null <<'PY'
import sys, yaml
try:
    d = yaml.safe_load(open(sys.argv[1])) or {}
    lanes = d.get('lanes', {})
    print(lanes.get(sys.argv[2]) or lanes.get('worker') or ... or 'sonnet')
except Exception:
    print('sonnet')
PY
    )
    [ -n "$_resolved" ] && model="$_resolved"
  fi
fi
```

### After

```bash
# Module-level cache — populated once on first call per shell session
declare -A _MO_LANE_CACHE 2>/dev/null || true
_mo_resolve_lane() {
  local node_type="$1" agents_yaml="$2"
  local cache_key="${agents_yaml}::${node_type}"
  if [[ -n "${_MO_LANE_CACHE[$cache_key]+x}" ]]; then
    echo "${_MO_LANE_CACHE[$cache_key]}"
    return
  fi
  local result
  result=$(python3 - "$agents_yaml" "$node_type" 2>/dev/null <<'PY'
import sys, yaml
try:
    d = yaml.safe_load(open(sys.argv[1])) or {}
    lanes = d.get('lanes', {})
    print(lanes.get(sys.argv[2]) or lanes.get('worker') or 'sonnet')
except Exception:
    print('sonnet')
PY
  )
  _MO_LANE_CACHE["$cache_key"]="${result:-sonnet}"
  echo "${_MO_LANE_CACHE[$cache_key]}"
}

# In llm_dispatch():
local model="${model_override:-${MINI_ORK_DEFAULT_MODEL:-sonnet}}"
if [ -z "$model_override" ] && [ -n "$node_type" ] && [ -f "$_agents_yaml" ]; then
  local _resolved
  _resolved=$(_mo_resolve_lane "$node_type" "$_agents_yaml")
  [ -n "$_resolved" ] && model="$_resolved"
fi
```

**Savings**: First call per session: 1 python3 spawn. All subsequent calls: 0. At 10 nodes/plan the savings ratio is 9:1 per plan.

---

## Finding K-06: Duplicate 15-line subshells in `mo_llm_dispatch` (timeout vs no-timeout)

**File**: `lib/llm-dispatch.sh:74-99`  
**Impact**: The executable and sourceable dispatch paths each have two nearly identical 10-15 line subshells that differ only in whether `$TIMEOUT_CMD` is prefixed. Any bug fix or flag change must be applied to 2 (or 4) copies. Not a performance issue but a maintenance multiplier — subtle flag divergence between the branches has already produced bugs.  
**Effort**: S

### Before (llm-dispatch.sh:74-99 — sourceable path)

```bash
if [[ -n "$TIMEOUT_CMD" ]]; then
  (
    set +u
    [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
    source "$cl_script"
    "$TIMEOUT_CMD" --kill-after=60 "$timeout_s" claude \
      --print \
      --permission-mode bypassPermissions \
      --output-format text \
      --max-turns "$max_turns" \
      "$prompt"
  ) > "$out_file" 2>"$err_log" || return $?
else
  (
    set +u
    [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
    source "$cl_script"
    claude \
      --print \
      --permission-mode bypassPermissions \
      --output-format text \
      --max-turns "$max_turns" \
      "$prompt"
  ) > "$out_file" 2>"$err_log" || return $?
fi
```

### After

```bash
# Build optional timeout prefix as an array — empty array when no timeout available
local -a _timeout_prefix=()
[[ -n "$TIMEOUT_CMD" ]] && _timeout_prefix=("$TIMEOUT_CMD" --kill-after=60 "$timeout_s")

(
  set +u
  [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
  source "$cl_script"
  "${_timeout_prefix[@]}" claude \
    --print \
    --permission-mode bypassPermissions \
    --output-format text \
    --max-turns "$max_turns" \
    "$prompt"
) > "$out_file" 2>"$err_log" || return $?
```

**Note**: Empty array expansion `"${arr[@]}"` in bash produces zero arguments when the array is empty, so `"${_timeout_prefix[@]}" claude ...` safely becomes `claude ...` when no timeout is set. Apply the same collapse to the executable-model branch above it.

---

## Finding K-07: `for _d in $(ls -d ...)` glob expansion via `ls` — breaks on spaces

**File**: `lib/auto-merge.sh:149`  
**Impact**: `for _d in $(ls -d "$epic_dir"iter-*/ 2>/dev/null | sort -V -r)` uses word-splitting on ls output. Any path containing a space silently splits. More importantly, it forks `ls` + `sort` as a pipeline in a subshell per epic. Should use `mapfile` with a glob.  
**Effort**: S

### Before (auto-merge.sh:149)

```bash
for _d in $(ls -d "$epic_dir"iter-*/ 2>/dev/null | sort -V -r); do
  if [ -f "${_d}verdict.json" ]; then
    last_iter_dir="$_d"
    break
  fi
done
```

### After

```bash
# mapfile + compgen glob — no subshell, no word-split risk, no sort fork
local -a _iter_dirs=()
# shellcheck disable=SC2206  # glob expansion is intentional
mapfile -t _iter_dirs < <(compgen -G "${epic_dir}iter-*/" 2>/dev/null \
  | sort -V -r)
for _d in "${_iter_dirs[@]}"; do
  [ -f "${_d}verdict.json" ] && { last_iter_dir="$_d"; break; }
done
```

**Savings**: Eliminates `ls` + `sort` pipeline subshell per epic iteration.

---

## Finding K-08: SQL injection via bash string interpolation in `mo_cache_lookup` / `mo_cache_emit`

**File**: `lib/cache.sh:101-112` (lookup), `lib/cache.sh:152-163` (emit)  
**Impact**: `epic_id`, `stage`, and `input_hash` are interpolated directly into SQL strings using `'$epic'`. A single-quote character in any of those values (e.g. a task name like `user's-story`) crashes the query or, in the emit path, silently inserts truncated data. At scale this becomes a data-integrity bug.  
**Effort**: M

### Before (cache.sh:101-112)

```bash
mo_cache_lookup() {
  local stage="$1" epic="$2" iter="$3" input_hash="$4"
  local _db="${MINI_ORK_DB:-...}"
  sqlite3 "$_db" "
    SELECT output_path FROM mini_orch_sessions
    WHERE epic_id = '$epic'
      AND iter = $iter
      AND stage = '$stage'
      AND input_hash = '$input_hash'
      AND status = 'success'
      AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')
    ORDER BY updated_at DESC
    LIMIT 1;
  " 2>/dev/null
}
```

### After

```bash
mo_cache_lookup() {
  local stage="$1" epic="$2" iter="$3" input_hash="$4"
  local _db="${MINI_ORK_DB:-...}"
  python3 - "$_db" "$stage" "$epic" "$iter" "$input_hash" <<'PY'
import sqlite3, sys
db, stage, epic, iter_, input_hash = sys.argv[1:6]
con = sqlite3.connect(db)
row = con.execute("""
    SELECT output_path FROM mini_orch_sessions
    WHERE epic_id=? AND iter=? AND stage=? AND input_hash=?
      AND status='success'
      AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')
    ORDER BY updated_at DESC LIMIT 1
""", (epic, int(iter_), stage, input_hash)).fetchone()
con.close()
if row:
    print(row[0])
PY
}
```

**Note**: The same substitution must be applied to `mo_cache_record_hit` (cache.sh:115-128) and `mo_cache_emit` (cache.sh:135-163). The emit path also interpolates `$output_path` and `$log_path` which may contain single-quotes.

---

## Finding K-09: `awk` state machine parsing YAML in `_worker-launcher.sh`

**File**: `bin/_worker-launcher.sh:78-90`  
**Impact**: Scope patterns are parsed from `scope-patterns.yaml` using a multi-state awk script. This breaks silently on quoted values, flow-style YAML lists, and indentation changes. At 100K/day, any YAML reformatting causes silent scope omission, leading to scope-sentinel failures on otherwise valid workers.  
**Effort**: M

### Before (_worker-launcher.sh:78-90)

```bash
SCOPE_PATTERNS=$(awk -v id="$MO_EPIC" '
  /^epics:/ { in_epics = 1; next }
  in_epics && $0 ~ "^  " id ":" { in_epic = 1; next }
  in_epic && /^    patterns:/ { in_pat = 1; next }
  in_pat && /^      - / {
    sub(/^      - /, "")
    gsub(/^"|"$/, "")
    print
    next
  }
  in_pat && /^    [a-z]/ { in_pat = 0 }
  in_epic && /^  [A-Za-z]/ { in_epic = 0; in_pat = 0 }
' "$SCOPE_FILE" 2>/dev/null)
```

### After

```bash
SCOPE_PATTERNS=$(python3 - "$SCOPE_FILE" "$MO_EPIC" <<'PY'
import sys, yaml
try:
    data = yaml.safe_load(open(sys.argv[1])) or {}
    patterns = (data.get("epics", {}).get(sys.argv[2], {}) or {}).get("patterns", []) or []
    for p in patterns:
        print(p)
except Exception as e:
    import sys as _s
    print(f"scope parse error: {e}", file=_s.stderr)
PY
)
```

**Savings**: Correct YAML parse vs. fragile awk. Same startup cost (one python3 fork); eliminates an entire class of silent scope-omission bugs.

---

## Finding K-10: `mo_runs_ensure_schema` called on every `mo_runs_open`

**File**: `lib/runs-tracker.sh:87-93`  
**Impact**: `mo_runs_open` calls `mo_runs_ensure_schema` unconditionally on every dispatch open. `mo_runs_ensure_schema` fires two `sqlite3 "$_MO_DB"` calls (ALTER TABLE + CREATE TABLE IF NOT EXISTS). At 1 dispatch open per epic + 10 epics per job = 20 extra sqlite3 CLI forks per job run.  
**Effort**: S

### Before (runs-tracker.sh:87-93)

```bash
mo_runs_open() {
  local epic="$1" worktree="$2"
  mo_runs_ensure_schema      # ← unconditional on every open
  local branch
  branch=$(git -C "$worktree" symbolic-ref --short HEAD 2>/dev/null || echo "unknown")
  ...
}
```

### After

```bash
_MO_RUNS_SCHEMA_DONE=0

mo_runs_open() {
  local epic="$1" worktree="$2"
  # Run schema bootstrap once per shell session
  if [[ "$_MO_RUNS_SCHEMA_DONE" -eq 0 ]]; then
    mo_runs_ensure_schema
    _MO_RUNS_SCHEMA_DONE=1
  fi
  local branch
  branch=$(git -C "$worktree" symbolic-ref --short HEAD 2>/dev/null || echo "unknown")
  ...
}
```

**Savings**: Reduces schema-check overhead from O(N dispatches) to O(1) per shell session. For the canonical 10-epic job: 20 sqlite3 forks → 2.

---

## Finding K-11: `\n` literal instead of newline in prompt assembly in `bin/mini-ork-execute`

**File**: `bin/mini-ork-execute:203-206`  
**Impact**: `PROMPT_CONTENT="Task: ${node_desc}\n\nPlan context:\n${PLAN_CONTENT}"` — in bash double-quoted strings `\n` is a literal two-character sequence `\` + `n`, not a newline. The LLM receives `Task: <desc>\n\nPlan context:\n<plan>` with literal backslash-n. This corrupts prompt formatting for every node dispatch.  
**Effort**: S

### Before (mini-ork-execute:203-207)

```bash
PROMPT_CONTENT="Task: ${node_desc}\n\nPlan context:\n${PLAN_CONTENT}"
RESULT=$(llm_dispatch \
  --task-class "$TASK_CLASS" \
  --node-type "researcher" \
  --prompt-text "$PROMPT_CONTENT" 2>&1) || ...
```

### After

```bash
# $'...' ANSI-C quoting interprets \n as a real newline
PROMPT_CONTENT=$"Task: ${node_desc}

Plan context:
${PLAN_CONTENT}"
```

Or equivalently using `printf`:

```bash
PROMPT_CONTENT=$(printf 'Task: %s\n\nPlan context:\n%s' "$node_desc" "$PLAN_CONTENT")
```

**Impact**: Applies to the `researcher`, `implementer`, and `reviewer` dispatch blocks at lines 203, 218, and 233. Fixing this should immediately improve LLM response quality since the prompt structure reaches the model correctly.

---

## Finding K-12: `python3` spawned for `expires_at` date arithmetic in `mo_cache_emit`

**File**: `lib/cache.sh:146-149`  
**Impact**: `mo_cache_emit` spawns python3 (with a `date -u -v+30d` fallback) to compute a timestamp 30 days in the future. This is pure arithmetic — no JSON or DB work — yet it fires a new python3 process for every cache row emitted.  
**Effort**: S

### Before (cache.sh:146-149)

```bash
local expires_at
expires_at=$(python3 -c "
import datetime as d
print((d.datetime.utcnow() + d.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%fZ'))
" 2>/dev/null || date -u -v+30d +"%Y-%m-%dT%H:%M:%fZ" 2>/dev/null || echo "2099-01-01T00:00:00.000Z")
```

### After

```bash
# Use the sqlite3 that is already required in this function for the INSERT below.
# Compute inside the same python3 session that does the INSERT — zero extra spawn.
# (Move the expires_at calculation into the PY heredoc of mo_cache_emit.)

# In the python3 PY block that does the INSERT:
# expires_at = (datetime.utcnow() + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
# ... then use expires_at directly in the INSERT VALUES
```

Full collapse (merging the separate `expires_at` spawn into the existing INSERT heredoc in `mo_cache_emit`):

```bash
mo_cache_emit() {
  local stage="$1" epic="$2" iter="$3" input_hash="$4" status="$5"
  local output_path="$6" log_path="$7"
  local cost_usd="${8:-0}" turns="${9:-0}" duration_ms="${10:-0}"
  local prompt_version="${11:-v1}"
  local uuid
  uuid=$(uuidgen 2>/dev/null || printf '%s' "$(date +%s)-$$-$RANDOM")
  local _db="${MINI_ORK_DB:-...}"
  python3 - "$_db" "$stage" "$epic" "$iter" "$input_hash" "$status" \
            "$output_path" "$log_path" "$cost_usd" "$turns" "$duration_ms" \
            "$prompt_version" "$uuid" "${JOB_ID:-unknown}" <<'PY'
import sqlite3, sys
from datetime import datetime, timedelta
(db, stage, epic, iter_, input_hash, status, output_path, log_path,
 cost_usd, turns, duration_ms, prompt_version, uuid, job_id) = sys.argv[1:15]
expires_at = (datetime.utcnow() + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
con = sqlite3.connect(db)
con.execute("""INSERT INTO mini_orch_sessions
  (uuid, job_id, epic_id, iter, stage, input_hash, status,
   output_path, log_path, cost_usd, turns, duration_ms, expires_at, prompt_version)
  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  ON CONFLICT (uuid) DO NOTHING""",
  (uuid, job_id, epic, int(iter_), stage, input_hash, status,
   output_path, log_path, float(cost_usd), int(turns), int(duration_ms),
   expires_at, prompt_version))
con.commit(); con.close()
PY
}
```

**Savings**: Eliminates 1 python3 spawn per cache row; collapses `uuidgen` + `python3-date` + `sqlite3-insert` into a single python3 session.

---

## Summary Table

| ID   | File                          | Lines    | Category            | Spawn savings (per call) | Effort |
|------|-------------------------------|----------|---------------------|--------------------------|--------|
| K-01 | lib/auto-merge.sh             | 170, 179 | sqlite3 loop batch  | 2N → 1 per job          | M      |
| K-02 | lib/utility_function.sh       | 39–46    | python3→jq          | 1 python3 → 1 jq        | S      |
| K-03 | lib/gradient_extractor.sh     | 65–71    | source guard        | N source → 1            | S      |
| K-04 | lib/trace_store.sh            | 49–62    | DDL out of hot path | DDL per call → once     | S      |
| K-05 | lib/llm-dispatch.sh           | 163–173  | YAML cache          | N python3 → 1           | S      |
| K-06 | lib/llm-dispatch.sh           | 74–99    | subshell dedup      | maintainability          | S      |
| K-07 | lib/auto-merge.sh             | 149      | ls antipattern      | N ls+sort → 0           | S      |
| K-08 | lib/cache.sh                  | 101–163  | SQL injection fix   | correctness              | M      |
| K-09 | bin/_worker-launcher.sh       | 78–90    | awk→yaml.safe_load  | correctness + reliability| M      |
| K-10 | lib/runs-tracker.sh           | 87–93    | schema once/session | 2N → 2 per job          | S      |
| K-11 | bin/mini-ork-execute          | 203, 218 | \n literal bug      | correctness + LLM quality| S      |
| K-12 | lib/cache.sh                  | 146–149  | date spawn → in-PY  | 1 python3 per cache row  | S      |

**Priority order**: K-11 first (active correctness bug affecting every dispatch), then K-08 (data-integrity risk), then K-05 + K-03 + K-10 (quick S-effort wins), then K-01 + K-12 (batch wins at scale), then K-09 (correctness + reliability).
