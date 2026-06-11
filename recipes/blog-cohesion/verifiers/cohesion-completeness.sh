#!/usr/bin/env bash
# verifiers/cohesion-completeness.sh — verify all 5 lens JSONs +
# applied_post.md + apply_log.md exist, parse, and meet structural floors.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by mini-ork-execute)
#
# Output: JSON to stdout
#   { "verifier": "cohesion-completeness", "pass": bool,
#     "evidence_path": "...", "lenses_passed": N, "missing": [...] }
#
# Exit codes: always 0 (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"

EVIDENCE="$RUN_DIR/verifier-cohesion-completeness.log"
exec 3>"$EVIDENCE"

missing=()
lenses_passed=0

for lens in thesis bridge topic entity rhythm; do
  f="$RUN_DIR/context-$lens.json"
  if [ ! -f "$f" ]; then
    echo "MISSING: $f" >&3
    missing+=("context-$lens.json")
    continue
  fi
  # Parseable JSON with top-level verdict field
  verdict=$(python3 -c "
import json, sys
try:
    d = json.load(open('$f'))
    print(d.get('verdict', 'NO_VERDICT'))
except Exception as e:
    print('UNPARSEABLE: %s' % e)
" 2>/dev/null)
  echo "context-$lens.json: verdict=$verdict" >&3
  case "$verdict" in
    PASS|REQUEST_CHANGES)
      lenses_passed=$((lenses_passed + 1))
      ;;
    *)
      missing+=("context-$lens.json (verdict=$verdict)")
      ;;
  esac
done

# applied_post.md present + non-empty + frontmatter preserved
applied="$RUN_DIR/applied_post.md"
if [ ! -f "$applied" ]; then
  missing+=("applied_post.md")
  echo "MISSING: $applied" >&3
else
  ap_size=$(wc -c < "$applied" | tr -d ' ')
  ap_words=$(wc -w < "$applied" | tr -d ' ')
  echo "applied_post.md: $ap_size bytes, $ap_words words" >&3
  if [ "$ap_size" -lt 200 ]; then
    missing+=("applied_post.md (too small: $ap_size bytes)")
  fi
  # Frontmatter check — must start with `---` and contain `title:` + `pubDate:`
  if ! head -1 "$applied" | grep -q '^---$'; then
    missing+=("applied_post.md (missing frontmatter opener)")
  fi
  if ! head -30 "$applied" | grep -q '^title:'; then
    missing+=("applied_post.md (missing title in frontmatter)")
  fi
  if ! head -30 "$applied" | grep -q '^pubDate:'; then
    missing+=("applied_post.md (missing pubDate in frontmatter)")
  fi
fi

# apply_log.md present + has required sections
log="$RUN_DIR/apply_log.md"
if [ ! -f "$log" ]; then
  missing+=("apply_log.md")
  echo "MISSING: $log" >&3
else
  for section in "Inputs read" "Applied" "Rejected" "Open carve-outs"; do
    if ! grep -q "^## $section" "$log"; then
      missing+=("apply_log.md (missing section: $section)")
    fi
  done
fi

# Compose verdict
if [ "${#missing[@]}" -eq 0 ]; then
  python3 -c "import json; print(json.dumps({
    'verifier': 'cohesion-completeness',
    'pass': True,
    'evidence_path': '$EVIDENCE',
    'lenses_passed': $lenses_passed,
    'missing': []
  }))"
else
  python3 - <<PY
import json
missing = """${missing[@]}""".split()
print(json.dumps({
  'verifier': 'cohesion-completeness',
  'pass': False,
  'evidence_path': '$EVIDENCE',
  'lenses_passed': $lenses_passed,
  'missing': missing
}))
PY
fi

exit 0
