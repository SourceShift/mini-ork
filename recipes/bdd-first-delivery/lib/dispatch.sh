#!/usr/bin/env bash
# bdd-first-delivery — recipe-internal partition dispatch helper
#
# Handles the parallel sub-epic dispatch pattern specific to this recipe:
# for each sub-epic emitted by the decomposer, spawn a worker for
# spec_author and implementer in parallel, then run bdd_runner in
# foreground before handing off to the aggregate reviewer.
#
# NOT the framework's lib/dispatch.sh — this is recipe-private.
# Source from the recipe's run script; not meant to run alone.

set -Eeuo pipefail

# ─── Required env (set by mini-ork run before sourcing this) ─────────────
# MINI_ORK_HOME    — framework root (absolute)
# JOB_ID           — unique job identifier
# WORKDIR_BASE     — parent directory where sub-epic worktrees live
# DECOMPOSE_JSON   — path to the decomposer's output JSON

# ─── Derived paths ───────────────────────────────────────────────────────
_bfd_run_dir() {
  local sub_epic_id="$1"
  printf '%s/runs/%s/%s\n' "${MINI_ORK_HOME}" "${JOB_ID}" "${sub_epic_id}"
}

_bfd_worktree() {
  local sub_epic_id="$1"
  printf '%s/%s\n' "${WORKDIR_BASE}" "${sub_epic_id}"
}

# ─── Read decomposer output ──────────────────────────────────────────────
# Returns: newline-separated list of sub_epic IDs
bfd_list_sub_epics() {
  if [ ! -f "${DECOMPOSE_JSON}" ]; then
    printf '[bfd-dispatch] DECOMPOSE_JSON not found: %s\n' "${DECOMPOSE_JSON}" >&2
    return 1
  fi
  jq -r '.sub_epics[].id' "${DECOMPOSE_JSON}" 2>/dev/null
}

# Returns the field value for a given sub-epic ID and field name.
bfd_sub_epic_field() {
  local sub_epic_id="$1" field="$2"
  jq -r --arg id "${sub_epic_id}" --arg f "${field}" \
    '.sub_epics[] | select(.id == $id) | .[$f] // ""' \
    "${DECOMPOSE_JSON}" 2>/dev/null
}

# ─── Spec synthesis sub-loop ─────────────────────────────────────────────
# Runs spec_author → spec_reviewer for one sub-epic.
# Args: sub_epic_id
# Returns: 0 (spec approved or skipped), 1 (infra failure), 2 (exhausted iters)
bfd_synth_spec() {
  local sub_epic_id="$1"
  local run_dir
  run_dir="$(_bfd_run_dir "${sub_epic_id}")"
  local worktree
  worktree="$(_bfd_worktree "${sub_epic_id}")"
  local max_sub_iters="${BFD_MAX_SPEC_ITERS:-2}"
  local sub_iter=1
  local feedback=""

  mkdir -p "${run_dir}"

  while [ "${sub_iter}" -le "${max_sub_iters}" ]; do
    local iter_dir="${run_dir}/spec-iter-${sub_iter}"
    mkdir -p "${iter_dir}"
    local spec_log="${iter_dir}/spec-author.log"
    local verdict_file="${iter_dir}/spec-verdict.json"

    printf '[bfd-dispatch] spec_author sub_epic=%s iter=%s\n' "${sub_epic_id}" "${sub_iter}" >&2

    # Invoke spec_author (framework dispatches this via model_lane resolution).
    # The framework passes MINI_ORK_PROMPT, SUB_EPIC_ID, WORKDIR, FEEDBACK_FILE.
    MINI_ORK_PROMPT="${MINI_ORK_HOME}/recipes/bdd-first-delivery/prompts/spec_author.md" \
    SUB_EPIC_ID="${sub_epic_id}" \
    WORKDIR="${worktree}" \
    FEEDBACK_FILE="${feedback}" \
    ITER="${sub_iter}" \
      "${MINI_ORK_HOME}/bin/mini-ork-invoke-prompt" \
      > "${spec_log}" 2>&1 || {
        printf '[bfd-dispatch] spec_author infra failure sub_epic=%s iter=%s\n' "${sub_epic_id}" "${sub_iter}" >&2
        return 1
      }

    # Check outcome.
    local outcome=""
    if grep -q 'SPEC_SKIPPED:' "${spec_log}" 2>/dev/null; then
      local reason
      reason=$(grep -oE 'SPEC_SKIPPED: [^\n]+' "${spec_log}" | head -1 | sed 's/SPEC_SKIPPED: //')
      printf '[bfd-dispatch] spec_author SKIPPED sub_epic=%s reason=%s\n' "${sub_epic_id}" "${reason}" >&2
      return 0
    fi

    local spec_file=""
    spec_file=$(ls -1 "${worktree}/e2e/${sub_epic_id}_"*.spec.ts 2>/dev/null | head -1 || true)
    if [ -z "${spec_file}" ]; then
      printf '[bfd-dispatch] spec_author wrote no file and no SKIPPED marker sub_epic=%s\n' "${sub_epic_id}" >&2
      return 1
    fi

    # Run spec_reviewer.
    local review_log="${iter_dir}/spec-reviewer.log"
    printf '[bfd-dispatch] spec_reviewer sub_epic=%s iter=%s spec=%s\n' "${sub_epic_id}" "${sub_iter}" "${spec_file}" >&2

    MINI_ORK_PROMPT="${MINI_ORK_HOME}/recipes/bdd-first-delivery/prompts/spec_reviewer.md" \
    SUB_EPIC_ID="${sub_epic_id}" \
    WORKDIR="${worktree}" \
    SPEC_PATH="${spec_file}" \
    ITER="${sub_iter}" \
      "${MINI_ORK_HOME}/bin/mini-ork-invoke-prompt" \
      > "${review_log}" 2>&1 || {
        printf '[bfd-dispatch] spec_reviewer infra failure sub_epic=%s iter=%s\n' "${sub_epic_id}" "${sub_iter}" >&2
        return 1
      }

    # Extract verdict.
    local verdict="UNKNOWN"
    if [ -f "${verdict_file}" ]; then
      verdict=$(jq -r '.verdict // "UNKNOWN"' "${verdict_file}" 2>/dev/null || echo "UNKNOWN")
    fi

    printf '[bfd-dispatch] spec_reviewer verdict=%s sub_epic=%s iter=%s\n' "${verdict}" "${sub_epic_id}" "${sub_iter}" >&2

    case "${verdict}" in
      APPROVE_SPEC)
        return 0
        ;;
      REQUEST_CHANGES_SPEC)
        feedback="${iter_dir}/spec-feedback.md"
        jq -r '"# Spec reviewer feedback (iter " + ($iter | tostring) + ")\n\nVerdict: " + .verdict + "\n\n## Issues\n\n" + ([.issues[]? | "- [" + .severity + "] " + .description] | join("\n")) + "\n\n## Direct feedback\n\n" + (.feedback_to_author // "(none)")' \
          --argjson iter "${sub_iter}" "${verdict_file}" > "${feedback}" 2>/dev/null || feedback=""
        sub_iter=$(( sub_iter + 1 ))
        continue
        ;;
      ESCALATE)
        printf '[bfd-dispatch] spec_reviewer ESCALATE sub_epic=%s iter=%s\n' "${sub_epic_id}" "${sub_iter}" >&2
        return 2
        ;;
      *)
        printf '[bfd-dispatch] spec_reviewer unknown verdict=%s — treating as ESCALATE\n' "${verdict}" >&2
        return 2
        ;;
    esac
  done

  printf '[bfd-dispatch] spec synth exhausted %s sub-iters without APPROVE_SPEC for %s\n' "${max_sub_iters}" "${sub_epic_id}" >&2
  return 2
}

# ─── Parallel sub-epic batch ─────────────────────────────────────────────
# Spawns spec_author+implementer workers for all sub-epics in parallel,
# then waits for all to complete.
# Args: (reads sub-epics from DECOMPOSE_JSON)
# Returns: 0 when all workers have exited (check individual verdict files).
bfd_dispatch_parallel() {
  local pids=()
  local sub_epic_ids=()

  while IFS= read -r sub_epic_id; do
    [ -z "${sub_epic_id}" ] && continue
    sub_epic_ids+=("${sub_epic_id}")

    local worktree run_dir
    worktree="$(_bfd_worktree "${sub_epic_id}")"
    run_dir="$(_bfd_run_dir "${sub_epic_id}")"
    mkdir -p "${run_dir}/iter-1"

    local log="${run_dir}/iter-1/worker.log"
    local pidfile="${run_dir}/iter-1/worker.pid"

    printf '[bfd-dispatch] spawning implementer sub_epic=%s worktree=%s\n' "${sub_epic_id}" "${worktree}" >&2

    (
      # Spec synthesis runs first (foreground, inside the worker subprocess).
      if ! bfd_synth_spec "${sub_epic_id}"; then
        printf '[bfd-dispatch] spec synth failed for %s — skipping implementer\n' "${sub_epic_id}" >&2
        exit 1
      fi

      # Implementer.
      MINI_ORK_PROMPT="${MINI_ORK_HOME}/recipes/bdd-first-delivery/prompts/implementer.md" \
      SUB_EPIC_ID="${sub_epic_id}" \
      WORKDIR="${worktree}" \
      ITER=1 \
        "${MINI_ORK_HOME}/bin/mini-ork-invoke-prompt" \
        >> "${log}" 2>&1
    ) > "${log}" 2>&1 &

    local pid=$!
    echo "${pid}" > "${pidfile}"
    pids+=("${pid}")
    printf '[bfd-dispatch] spawned sub_epic=%s pid=%s\n' "${sub_epic_id}" "${pid}" >&2
  done < <(bfd_list_sub_epics)

  # Wait for all.
  local all_ok=0
  for pid in "${pids[@]}"; do
    if ! wait "${pid}"; then
      all_ok=1
    fi
  done

  if [ "${all_ok}" -ne 0 ]; then
    printf '[bfd-dispatch] one or more sub-epic workers exited non-zero\n' >&2
  fi

  printf '[bfd-dispatch] all %s sub-epic workers finished\n' "${#pids[@]}" >&2
  return "${all_ok}"
}

# ─── BDD verification batch ──────────────────────────────────────────────
# Runs playwright_runner.sh for each sub-epic in sequence (or parallel if
# BFD_BDD_PARALLEL=1). Collects verdicts.
# Returns: 0 if all pass (or skip), 1 if any fail.
bfd_run_bdd_all() {
  local any_fail=0
  local runner="${MINI_ORK_HOME}/recipes/bdd-first-delivery/verifiers/playwright_runner.sh"

  if [ ! -x "${runner}" ]; then
    printf '[bfd-dispatch] playwright_runner.sh not found or not executable: %s\n' "${runner}" >&2
    return 1
  fi

  while IFS= read -r sub_epic_id; do
    [ -z "${sub_epic_id}" ] && continue
    local worktree run_dir
    worktree="$(_bfd_worktree "${sub_epic_id}")"
    run_dir="$(_bfd_run_dir "${sub_epic_id}")"
    local verdict_dir="${run_dir}/bdd"
    mkdir -p "${verdict_dir}"

    printf '[bfd-dispatch] bdd_runner sub_epic=%s\n' "${sub_epic_id}" >&2

    SUB_EPIC_ID="${sub_epic_id}" \
    WORKDIR="${worktree}" \
    MINI_ORK_VERDICT_DIR="${verdict_dir}" \
      bash "${runner}"

    local pass
    pass=$(jq -r '.pass' "${verdict_dir}/bdd-verdict.json" 2>/dev/null || echo "false")
    if [ "${pass}" != "true" ]; then
      printf '[bfd-dispatch] bdd_runner FAIL sub_epic=%s\n' "${sub_epic_id}" >&2
      any_fail=1
    else
      printf '[bfd-dispatch] bdd_runner PASS sub_epic=%s\n' "${sub_epic_id}" >&2
    fi
  done < <(bfd_list_sub_epics)

  return "${any_fail}"
}
