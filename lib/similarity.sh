#!/usr/bin/env bash
# similarity.sh — inline TF-IDF cosine retrieval over mini-ork text columns.
#
# Track A item 1 (arXiv:2512.10696 ReMe, 2506.10484 ExpeRepair). Recalls
# semantically-similar prior observations — not just literal task_class
# matches. Makes the system answer "have I seen this kind of problem
# before?" instead of "have I seen this exact problem before?"
#
# Public API:
#   similarity_query <table> <text_column> <query_text> <limit>
#       Emit JSON array of {"score":x,"row":{...}} for the top-N TF-IDF
#       cosine matches. Table+column whitelisted against SQL injection.
#
# Tables + columns supported (extend ALLOWED in the python if needed):
#   bug_reports         title, description, suggested_fix
#   gradient_records    signal, suggested_change, target
#   learning_record     title, patch_summary
#   pattern_records     description

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

similarity_query() {
  local table="${1:?table required}"
  local text_col="${2:?text_column required}"
  local query="${3:?query_text required}"
  local limit="${4:-5}"

  python3 - "$STATE_DB" "$table" "$text_col" "$query" "$limit" <<'PY'
import json, math, re, sqlite3, sys
from collections import Counter

db, table, text_col, query, limit_str = sys.argv[1:6]
limit = int(limit_str)

ALLOWED = {
    "bug_reports":      {"title", "description", "suggested_fix"},
    "gradient_records": {"signal", "suggested_change", "target"},
    "learning_record":  {"title", "patch_summary"},
    "pattern_records":  {"description"},
}
if table not in ALLOWED or text_col not in ALLOWED[table]:
    print("[]"); sys.exit(0)

def _tok(s):
    s = (s or "").lower()
    s = re.sub(r"[^\w./_-]+", " ", s)
    return [t for t in s.split() if len(t) >= 3]

def _tf(toks):
    c = Counter(toks); total = sum(c.values()) or 1
    return {t: cnt/total for t, cnt in c.items()}

def _cos(a, b):
    keys = set(a) | set(b)
    dot = sum(a.get(k,0)*b.get(k,0) for k in keys)
    na = math.sqrt(sum(v*v for v in a.values()))
    nb = math.sqrt(sum(v*v for v in b.values()))
    return dot/(na*nb) if na and nb else 0.0

con = sqlite3.connect(db); con.row_factory = sqlite3.Row
rows = con.execute(f"SELECT rowid AS rid, * FROM {table} LIMIT 5000").fetchall()
con.close()

docs = [_tok((r[text_col] or "")) for r in rows]
df = Counter()
for d in docs:
    for t in set(d): df[t] += 1
N = max(len(docs), 1)
idf = {t: math.log(1.0 + N/(1+c)) for t,c in df.items()}
def _vec(toks): return {t: w*idf.get(t,0.0) for t,w in _tf(toks).items()}
q_vec = _vec(_tok(query))

scored = []
for r, d in zip(rows, docs):
    s = _cos(q_vec, _vec(d))
    if s > 0: scored.append((s, r))
scored.sort(reverse=True, key=lambda p: p[0])

print(json.dumps(
    [{"score": round(s, 4), "row": {k: r[k] for k in r.keys()}}
     for s, r in scored[:limit]],
    separators=(",", ":"), default=str,
))
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "similarity.sh — source me and call similarity_query <table> <text_col> <query> <limit>"
fi
