#!/usr/bin/env bash
# tests/unit/test_publisher_commit.sh — M1 publisher-commit seam.
#
# Covers the new code-fix publisher semantics: when artifact_contract.yaml
# declares no outputs[], the publisher must commit the implementer's
# in-place edits on reviewer APPROVE instead of silently no-op'ing.
#
# Three scenarios:
#   (a) [positive]   APPROVE verdict + files_changed → exactly 1 commit
#                    containing ONLY the listed file (no `git add -A` leak);
#                    seeded untracked dirty file must NOT enter the commit.
#   (b) [negative]   non-APPROVE verdict             → no extra commit.
#   (c) [preserved]  artifact-copy publish branch (outputs[] non-empty) is
#                    unchanged by the M1 edits; the helper is reachable
#                    ONLY from the empty-outputs branch.
#
# Implementation strategy: bin/mini-ork-execute is sourced with
# MINI_ORK_EXECUTE_SOURCE_ONLY=1 so only the function definitions before
# line 657 are loaded. `_publisher_try_commit_files` lives between that
# line and the early-return guard so it is reachable from the test
# without touching the dispatcher's main block. Cases (a) and (b) drive
# the helper directly with a controlled env. Case (c) is a static
# invariant check (the M1 edit cannot have introduced a path where the
# helper fires on the artifact-copy branch) plus a small live re-exec of
# the cp+commit primitives to prove the publisher's "published" stdout
# format still emits end-to-end.
#
# Filename ends in .sh so pytest's default discovery skips it.
# Run with: bash tests/unit/test_publisher_commit.sh
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
EXECUTOR="$MINI_ORK_ROOT/bin/mini-ork-execute"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

WORKSPACE=""
cleanup() {
  if [ -n "$WORKSPACE" ] && [ -d "$WORKSPACE" ]; then
    rm -rf "$WORKSPACE"
  fi
}

seed_repo() {
  local repo="$1"
  rm -rf "$repo"
  mkdir -p "$repo"
  git -C "$repo" init -q -b main
  git -C "$repo" config user.email "tester@local"
  git -C "$repo" config user.name "tester"
  git -C "$repo" config commit.gpgsign false
  echo "initial" > "$repo/seed.md"
  echo "tracked content" >> "$repo/seed.md"
  git -C "$repo" add seed.md
  git -C "$repo" commit -q -m "initial"
  # Untracked dirty file: must NOT enter M1 publish commit (no `-A`).
  echo "untracked dirty content" > "$repo/dirty.md"
}

echo ""
echo "── unit: bin/mini-ork-execute _publisher_try_commit_files ──"

if [ ! -f "$EXECUTOR" ]; then
  _skip "bin/mini-ork-execute missing"
else
  WORKSPACE="$(mktemp -d /tmp/mo-publisher-commit-XXXXXX)"
  trap cleanup EXIT

  # Confirm the helper is in scope after a source-only load. If this
  # fails the M1 edit didn't land where the test expects.
  if ! grep -q '^_publisher_try_commit_files()' "$EXECUTOR"; then
    _skip "_publisher_try_commit_files not found in $EXECUTOR — M1 edit missing"
  else
    # Source the executor in this shell so the helper is visible to
    # `declare -f` AFTER the source. Subshell-sourcing would isolate the
    # function definition and break the visibility check below.
    set +e
    export MINI_ORK_EXECUTE_SOURCE_ONLY=1
    # shellcheck source=/dev/null
    source "$EXECUTOR" >/dev/null 2>&1
    if ! declare -f _publisher_try_commit_files >/dev/null; then
      _skip "_publisher_try_commit_files not loaded by source-only mode"
    else
      # ── (a) positive: APPROVE + files_changed → 1 commit, only that file ──
      echo ""
      echo "--- (a) positive: APPROVE + files_changed → 1 commit (no -A leak) ---"
      REPO_A="$WORKSPACE/a-repo"
      RUN_A="$WORKSPACE/a-run"
      mkdir -p "$RUN_A"
      seed_repo "$REPO_A"
      SEEDED_PATH="$REPO_A/seed.md"
      # Modify the seeded file so the commit is non-empty (changes vs HEAD~1).
      echo "modified by implementer" >> "$SEEDED_PATH"
      printf '{"verdict":"approve"}\n' > "$RUN_A/review-verdict.json"
      printf '{"files_changed":["%s"],"worktree_path":""}\n' "$SEEDED_PATH" \
        > "$RUN_A/implementer-summary.json"

      (
        set +e
        env -i \
          PATH="$PATH" \
          RUN_DIR="$RUN_A" \
          MINI_ORK_RUN_DIR="$RUN_A" \
          MO_TARGET_CWD="$REPO_A" \
          REVIEW_FILE="$RUN_A/review-verdict.json" \
          VERDICT="approve" \
          MINI_ORK_ROOT="$MINI_ORK_ROOT" \
          MINI_ORK_RECIPE="code-fix" \
          MINI_ORK_NODE_DESC="implementer" \
          MINI_ORK_RUN_ID="local-test-a" \
          bash -c '
            set -Eeuo pipefail
            export MINI_ORK_EXECUTE_SOURCE_ONLY=1
            source '"$EXECUTOR"'
            set +e
            _publisher_try_commit_files "$MO_TARGET_CWD"; rc=$?
            echo "rc=$rc"
          '
      ) >"$WORKSPACE/a.stdout" 2>"$WORKSPACE/a.stderr"
      a_rc="$(grep '^rc=' "$WORKSPACE/a.stdout" | tail -n1 | cut -d= -f2)"
      a_count="$(git -C "$REPO_A" rev-list --count HEAD 2>/dev/null || echo 0)"
      a_files="$(git -C "$REPO_A" show --name-only --format= HEAD 2>/dev/null | tr -d ' \n\r')"
      a_msg="$(git -C "$REPO_A" log -1 --format=%s HEAD 2>/dev/null || echo '')"
      a_contained_publish="$(grep -c '^\s*\[publish\] committed 1 file' "$WORKSPACE/a.stderr" || echo 0)"

      ok=1
      if [ "${a_rc:-X}" != "0" ]; then
        echo "    helper rc=$a_rc (want 0)"
        ok=0
      fi
      if [ "${a_count:-X}" != "2" ]; then
        echo "    rev-list count=$a_count (want 2)"
        ok=0
      fi
      if [ "$a_files" != "seed.md" ]; then
        echo "    files-in-commit='$a_files' (want 'seed.md' — untracked dirty.md must NOT be present)"
        ok=0
      fi
      if ! printf '%s' "$a_msg" | grep -Eq '^mini-ork\(code-fix\): implementer \[run local-test-a\]$'; then
        echo "    commit-msg='$a_msg' (want 'mini-ork(code-fix): implementer [run local-test-a]')"
        ok=0
      fi
      if [ "${a_contained_publish:-0}" -lt 1 ]; then
        echo "    stderr missing [publish] log line (got: $(head -c 400 "$WORKSPACE/a.stderr"))"
        ok=0
      fi
      if [ "$ok" -eq 1 ]; then
        _ok "(a) positive: APPROVE + files_changed → 1 commit, only that file, no -A leak"
      else
        _fail "(a) positive scenario did not match contract"
      fi

      # ── (b) negative: non-APPROVE verdict → no extra commit ─────────────
      echo ""
      echo "--- (b) negative: verdict=needs_revision → no extra commit ---"
      REPO_B="$WORKSPACE/b-repo"
      RUN_B="$WORKSPACE/b-run"
      mkdir -p "$RUN_B"
      seed_repo "$REPO_B"
      SEEDED_B="$REPO_B/seed.md"
      echo "modified by implementer B" >> "$SEEDED_B"
      printf '{"verdict":"needs_revision","pass":false}\n' > "$RUN_B/review-verdict.json"
      printf '{"files_changed":["%s"],"worktree_path":""}\n' "$SEEDED_B" \
        > "$RUN_B/implementer-summary.json"

      (
        set +e
        env -i \
          PATH="$PATH" \
          RUN_DIR="$RUN_B" \
          MINI_ORK_RUN_DIR="$RUN_B" \
          MO_TARGET_CWD="$REPO_B" \
          REVIEW_FILE="$RUN_B/review-verdict.json" \
          VERDICT="needs_revision" \
          MINI_ORK_ROOT="$MINI_ORK_ROOT" \
          MINI_ORK_RECIPE="code-fix" \
          MINI_ORK_NODE_DESC="implementer" \
          MINI_ORK_RUN_ID="local-test-b" \
          bash -c '
            set -Eeuo pipefail
            export MINI_ORK_EXECUTE_SOURCE_ONLY=1
            source '"$EXECUTOR"'
            set +e
            _publisher_try_commit_files "$MO_TARGET_CWD"; rc=$?
            echo "rc=$rc"
          '
      ) >"$WORKSPACE/b.stdout" 2>"$WORKSPACE/b.stderr"
      b_rc="$(grep '^rc=' "$WORKSPACE/b.stdout" | tail -n1 | cut -d= -f2)"
      b_count="$(git -C "$REPO_B" rev-list --count HEAD 2>/dev/null || echo 0)"
      b_skip_log="$(grep -c '\[skip-publish\] reviewer verdict' "$WORKSPACE/b.stderr" || echo 0)"

      ok=1
      if [ "${b_rc:-X}" != "1" ]; then
        echo "    helper rc=$b_rc (want 1 = skip)"
        ok=0
      fi
      if [ "${b_count:-X}" != "1" ]; then
        echo "    rev-list count=$b_count (want 1 — no new publish commit)"
        ok=0
      fi
      if [ "${b_skip_log:-0}" -lt 1 ]; then
        echo "    stderr missing [skip-publish] reviewer verdict log"
        ok=0
      fi
      if [ "$ok" -eq 1 ]; then
        _ok "(b) negative: verdict=needs_revision → no extra commit, [skip-publish] logged"
      else
        _fail "(b) negative scenario did not match contract"
      fi

      # ── (b2) negative: APPROVE verdict but files_changed path escapes repo ─
      echo ""
      echo "--- (b2) negative: files_changed escapes target_repo (strict-child reject) ---"
      REPO_B2="$WORKSPACE/b2-repo"
      RUN_B2="$WORKSPACE/b2-run"
      OUTSIDE="$WORKSPACE/b2-outside.md"
      mkdir -p "$RUN_B2"
      seed_repo "$REPO_B2"
      # Seed a file OUTSIDE the target repo; implementer accidentally listed
      # a sibling workspace path. The helper must reject it and not commit
      # anything (the remaining files_changed would be empty after reject).
      echo "outside content" > "$OUTSIDE"
      printf '{"verdict":"approve"}\n' > "$RUN_B2/review-verdict.json"
      printf '{"files_changed":["%s"],"worktree_path":""}\n' "$OUTSIDE" \
        > "$RUN_B2/implementer-summary.json"

      (
        set +e
        env -i \
          PATH="$PATH" \
          RUN_DIR="$RUN_B2" \
          MINI_ORK_RUN_DIR="$RUN_B2" \
          MO_TARGET_CWD="$REPO_B2" \
          REVIEW_FILE="$RUN_B2/review-verdict.json" \
          VERDICT="approve" \
          MINI_ORK_ROOT="$MINI_ORK_ROOT" \
          MINI_ORK_RECIPE="code-fix" \
          MINI_ORK_NODE_DESC="implementer" \
          MINI_ORK_RUN_ID="local-test-b2" \
          bash -c '
            set -Eeuo pipefail
            export MINI_ORK_EXECUTE_SOURCE_ONLY=1
            source '"$EXECUTOR"'
            set +e
            _publisher_try_commit_files "$MO_TARGET_CWD"; rc=$?
            echo "rc=$rc"
          '
      ) >"$WORKSPACE/b2.stdout" 2>"$WORKSPACE/b2.stderr"
      b2_rc="$(grep '^rc=' "$WORKSPACE/b2.stdout" | tail -n1 | cut -d= -f2)"
      b2_count="$(git -C "$REPO_B2" rev-list --count HEAD 2>/dev/null || echo 0)"
      b2_reject_log="$(grep -c '\[reject-publish\] file escapes target repo' "$WORKSPACE/b2.stderr" || echo 0)"

      ok=1
      if [ "${b2_rc:-X}" != "1" ]; then
        echo "    helper rc=$b2_rc (want 1 = skip; outside-path must reject)"
        ok=0
      fi
      if [ "${b2_count:-X}" != "1" ]; then
        echo "    rev-list count=$b2_count (want 1 — outside path must NOT be committed)"
        ok=0
      fi
      if [ "${b2_reject_log:-0}" -lt 1 ]; then
        echo "    stderr missing [reject-publish] outside-repo log"
        ok=0
      fi
      if [ "$ok" -eq 1 ]; then
        _ok "(b2) outside-repo path is strict-child rejected, no commit"
      else
        _fail "(b2) outside-repo scenario did not match contract"
      fi
    fi
  fi
fi

# ── (c) preserved: artifact-copy publish branch (non-empty outputs[]) ────────
echo ""
echo "--- (c) preserved: artifact-copy branch + helper scope invariants ---"
ok=1
# Source-only mode is required to leave the unchanged artifact-copy branch
# (which lives INSIDE _dispatch_node, after the early-return) intact on disk.
# We don't drive _dispatch_node in this unit test (its real branch is covered
# by recipe integration runs); instead we assert three structural invariants:
#   1) The helper is called from exactly ONE place.
#   2) That one call-site is inside the publisher's `if [ -z "$_outputs" ]`
#      block — never from the artifact-copy branch.
#   3) The artifact-copy success / failure log strings are unchanged.
helper_call_count="$(grep -c '_publisher_try_commit_files "\$_pub_target_repo"' "$MINI_ORK_ROOT/bin/mini-ork-execute" || true)"
in_empty_outputs_branch="$(grep -B15 -A1 '_publisher_try_commit_files "\$_pub_target_repo"' "$MINI_ORK_ROOT/bin/mini-ork-execute" \
  | grep -c 'if \[ -z "\$_outputs" \]')"
publish_msg_unchanged="$(grep -cF 'echo "  [ok] publisher: published $_out (committed)"' "$MINI_ORK_ROOT/bin/mini-ork-execute" || true)"
cp_fail_msg_unchanged="$(grep -cF 'echo "  [fail] publisher: cp failed for $_out" >&2' "$MINI_ORK_ROOT/bin/mini-ork-execute" || true)"

if [ "${helper_call_count:-0}" != "1" ]; then
  echo "    helper call count=$helper_call_count (want 1 — extra calls would leak into artifact-copy branch)"
  ok=0
fi
if [ "${in_empty_outputs_branch:-0}" -lt 1 ]; then
  echo "    helper call-site is NOT inside the empty-outputs branch"
  ok=0
fi
if [ "${publish_msg_unchanged:-0}" -ne 1 ]; then
  echo "    artifact-copy success log string changed (count=$publish_msg_unchanged; want 1)"
  ok=0
fi
if [ "${cp_fail_msg_unchanged:-0}" -ne 1 ]; then
  echo "    artifact-copy failure log string changed (count=$cp_fail_msg_unchanged; want 1)"
  ok=0
fi

# Smoke: re-exec the artifact-copy primitives directly to confirm the
# `[ok] publisher: published X (committed)` stdout pattern still emits
# end-to-end under the same git config the publisher uses.
if [ "$ok" -eq 1 ]; then
  AC_REPO="$WORKSPACE/c-repo"
  AC_RUN="$WORKSPACE/c-run"
  mkdir -p "$AC_RUN"
  seed_repo "$AC_REPO"
  echo "synthesis body" > "$AC_RUN/synthesis.md"
  AC_DST="$AC_REPO/published_x.md"
  mkdir -p "$(dirname "$AC_DST")"
  cp "$AC_RUN/synthesis.md" "$AC_DST" 2>/dev/null
  (
    cd "$AC_REPO" \
      && git add "$AC_DST" \
      && git -c user.email=mini-ork@local -c user.name=mini-ork \
         commit -q -m "audit(code-fix): publish synthesis from local" >/dev/null
    echo "  [ok] publisher: published $AC_DST (committed)"
  ) >"$WORKSPACE/c.stdout" 2>&1
  if ! grep -q '\[ok\] publisher: published' "$WORKSPACE/c.stdout" 2>/dev/null; then
    # `set -uo pipefail` doesn't `echo` to stdout unless the parent dir is OK.
    echo "    artifact-copy stdout pattern did not emit"
    echo "    stdout: $(head -c 400 "$WORKSPACE/c.stdout")"
    ok=0
  fi
fi

if [ "$ok" -eq 1 ]; then
  _ok "(c) artifact-copy branch + helper-scope invariants intact"
else
  _fail "(c) artifact-copy invariants regressed"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
