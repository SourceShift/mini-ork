#!/usr/bin/env bash
# Auto-answer run-profile questions with a cheap LLM lane.

set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/llm-dispatch.sh"

mo_answer_profile_questions() {
  local kickoff_path="${1:-}"
  local questions_json="${2:-}"
  local profile_answers_out_path="${3:-}"

  if [ -z "$kickoff_path" ] || [ -z "$questions_json" ] || [ -z "$profile_answers_out_path" ]; then
    echo "usage: mo_answer_profile_questions <kickoff_path> <questions_json> <profile_answers_out_path>" >&2
    return 2
  fi
  if [ ! -f "$kickoff_path" ]; then
    echo "profile answerer kickoff not found: $kickoff_path" >&2
    return 2
  fi

  local prompt
  prompt=$(python3 - "$kickoff_path" "$questions_json" <<'PY'
import json
import sys
from pathlib import Path

kickoff = Path(sys.argv[1]).read_text(encoding="utf-8")[:20000]
try:
    questions = json.loads(sys.argv[2] or "[]")
except json.JSONDecodeError:
    questions = []

print("""You answer mini-ork run profile questions for autonomous child runs.

Return ONLY a strict JSON object with this exact shape:
{"answers":[{"question":"...","answer":"..."}],"auto_answered":true}

Rules:
- Answer every question using the kickoff content.
- Keep answers concise and operational.
- Do not include markdown, prose, code fences, or extra keys.

Kickoff:
""" + kickoff + """

Questions JSON:
""" + json.dumps(questions, ensure_ascii=True))
PY
  )

  local raw=""
  if ! raw=$(llm_dispatch \
    --task-class "profile_answerer" \
    --node-type "profile_answerer" \
    --model "deepseek" \
    --prompt-text "$prompt"); then
    raw=$(llm_dispatch \
      --task-class "profile_answerer" \
      --node-type "profile_answerer" \
      --model "kimi" \
      --prompt-text "$prompt")
  fi

  mkdir -p "$(dirname "$profile_answers_out_path")"
  MO_PROFILE_ANSWER_RAW="$raw" python3 - "$questions_json" "$profile_answers_out_path" <<'PY'
import json
import os
import sys

questions_json, out_path = sys.argv[1:3]
raw = os.environ.get("MO_PROFILE_ANSWER_RAW", "").strip()
try:
    parsed = json.loads(raw)
except json.JSONDecodeError as exc:
    raise SystemExit(f"profile answerer returned non-json output: {exc}")

try:
    questions = json.loads(questions_json or "[]")
except json.JSONDecodeError:
    questions = []

def question_text(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("text") or item.get("question") or item)
    return str(item)

question_lookup = {question_text(q): question_text(q) for q in questions}
answers = []
for item in parsed.get("answers") or []:
    if not isinstance(item, dict):
        continue
    question = str(item.get("question") or "").strip()
    answer = str(item.get("answer") or "").strip()
    if question and answer:
        answers.append({"question": question_lookup.get(question, question), "answer": answer})

if questions and len(answers) < len(questions):
    answered = {a["question"] for a in answers}
    for q in questions:
        text = question_text(q)
        if text not in answered:
            raise SystemExit(f"profile answerer omitted question: {text}")

out = {"answers": answers, "auto_answered": True}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
    f.write("\n")
PY
}
