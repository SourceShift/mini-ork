#!/usr/bin/env bash
# Deterministically seed run_profile.json from a structured kickoff markdown.

mo_profile_seed_from_kickoff() {
  local kickoff_path="${1:-}"
  local out_profile_json="${2:-}"

  if [ -z "$kickoff_path" ] || [ -z "$out_profile_json" ]; then
    echo "usage: mo_profile_seed_from_kickoff <kickoff_path> <out_profile_json>" >&2
    return 2
  fi
  if [ ! -f "$kickoff_path" ]; then
    echo "profile seed kickoff not found: $kickoff_path" >&2
    return 1
  fi

  python3 - "$kickoff_path" "$out_profile_json" <<'PY'
import json
import re
import sys
from pathlib import Path

kickoff = Path(sys.argv[1])
profile = Path(sys.argv[2])
text = kickoff.read_text(encoding="utf-8", errors="replace")


def section_lines(*names):
    wanted = {name.lower() for name in names}
    current = None
    lines = []
    for raw in text.splitlines():
        match = re.match(r"^\s*#{2,6}\s+(.+?)\s*$", raw)
        if match:
            title = match.group(1).strip().lower()
            current = title if title in wanted else None
            continue
        if current:
            lines.append(raw.rstrip())
    return [line for line in lines if line.strip()]


def bullets(lines):
    items = []
    continuation = []
    for line in lines:
        match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if match:
            if continuation:
                items.append(" ".join(continuation).strip())
            continuation = [match.group(1).strip()]
            continue
        if continuation:
            continuation.append(line.strip())
    if continuation:
        items.append(" ".join(continuation).strip())
    return [item for item in items if item]


def first_heading():
    for line in text.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return kickoff.stem.replace("-", " ").replace("_", " ")


def clean_command(item):
    item = item.strip()
    fence = re.fullmatch(r"`([^`]+)`(?:\s+\(.+\))?", item)
    if fence:
        return fence.group(1).strip()
    return item


try:
    data = json.loads(profile.read_text(encoding="utf-8")) if profile.exists() else {}
except json.JSONDecodeError:
    data = {}

success = bullets(section_lines("success criteria", "success", "definition of done", "acceptance"))
scope = bullets(section_lines("scope", "scope allow", "in scope"))
provider_policy = bullets(section_lines("provider policy"))
commands = [clean_command(item) for item in bullets(section_lines("verification command", "verification commands"))]

seeded = bool(success or scope or commands)
existing_policy = data.get("provider_policy")
if not isinstance(existing_policy, dict):
    existing_policy = {}

data.update(
    {
        "schema_version": data.get("schema_version", "1.0"),
        "kickoff_path": str(kickoff.resolve()),
        "user_goal": first_heading(),
        "success_criteria": success,
        "scope_allow": scope,
        "provider_policy": {
            **existing_policy,
            "kickoff_policy": provider_policy,
        },
        "verification_command": commands[:3],
        "profile_status": "seeded" if seeded else "needs_answers",
    }
)

if not seeded:
    questions = data.get("human_questions")
    if not isinstance(questions, list) or not questions:
        data["human_questions"] = [
            "What exact success criteria should the verifier use?",
            "Which files or directories are explicitly in scope?",
            "What command should prove this run succeeded?",
        ]
else:
    data["human_questions"] = []

profile.parent.mkdir(parents=True, exist_ok=True)
profile.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(data["profile_status"])
PY
}
