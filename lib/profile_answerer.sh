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
  # Treat both non-zero exit AND empty stdout as failure → fall over to kimi.
  # Several providers (esp. deepseek under transient throttling) exit 0 with
  # empty/whitespace output instead of raising; without the empty-check we'd
  # silently lose the answer and downstream JSON parsing would die at char 0.
  if ! raw=$(llm_dispatch \
    --task-class "profile_answerer" \
    --node-type "profile_answerer" \
    --model "kimi" \
    --prompt-text "$prompt") || [ -z "${raw// /}" ]; then
    raw=$(llm_dispatch \
      --task-class "profile_answerer" \
      --node-type "profile_answerer" \
      --model "kimi" \
      --prompt-text "$prompt") || raw=""
  fi

  mkdir -p "$(dirname "$profile_answers_out_path")"
  MO_PROFILE_ANSWER_RAW="$raw" python3 - "$questions_json" "$profile_answers_out_path" <<'PY'
import json
import os
import re
import sys

questions_json, out_path = sys.argv[1:3]
raw = os.environ.get("MO_PROFILE_ANSWER_RAW", "").strip()

# Strip optional markdown code fences (```json … ``` or ``` … ```). Most
# instruction-tuned LLMs add fences for any structured payload even when
# the prompt forbids them. Also strip a leading "json" word on its own line.
fence_pattern = re.compile(r"^```(?:json|JSON)?\s*\n?|\n?```\s*$", re.MULTILINE)
raw = fence_pattern.sub("", raw).strip()

# Some providers prepend a brief acknowledgement before the JSON object.
# Fall back to extracting the FIRST balanced {...} substring.
def extract_json_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text

try:
    parsed = json.loads(raw)
except json.JSONDecodeError:
    salvaged = extract_json_object(raw)
    try:
        parsed = json.loads(salvaged)
    except json.JSONDecodeError as exc:
        snippet = raw[:200].replace("\n", "\\n")
        raise SystemExit(
            f"profile answerer returned non-json output: {exc} | "
            f"raw_first_200={snippet!r}"
        )

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
