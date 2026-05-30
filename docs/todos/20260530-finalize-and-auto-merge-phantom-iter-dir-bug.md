# TODO — phantom iter-dir shadows real APPROVE verdict in finalize + auto-merge

**Date observed:** 2026-05-30
**Upstream symptom:** UCP-A2 dispatch on the researcher fork (commit context below) hit this:
- `--max-iter 2` set on `deliver.sh`
- iter-1 APPROVE, iter-2 APPROVE (both `verdict.json` files contained `"verdict": "APPROVE"`)
- Auto-merge SKIPPED with reason `verdict=UNKNOWN`
- Completion report wrote `Final verdict: **UNKNOWN** (iter-3)`

## Root cause

The orchestrator pre-creates the NEXT iter's directory (writes `feedback.md` + `cache-stats.json` ahead of time) before checking whether the iter loop has capped. When `MAX_ITER=2`, the loop runs iter-1 and iter-2, but during iter-2's wrap-up the orch creates `iter-3/` with the next iter's feedback files even though iter-3 will never run.

Both `lib/finalize.sh` and `lib/auto-merge.sh` then read "the last iter dir" (sorted) and try to read `verdict.json` from it. iter-3 has no `verdict.json` → both default to `UNKNOWN` → real APPROVE in iter-2 ignored → branch sits unmerged + COMPLETION_REPORT is misleading.

## Fix shape (already applied on the researcher fork in commit `<TBD>` 2026-05-30)

Change the iter-discovery in both `finalize.sh` and `auto-merge.sh` from:

```bash
# OLD — reads the highest-numbered iter dir, even if it's a phantom
last_iter_dir=$(ls -d "$epic_dir"iter-*/ 2>/dev/null | sort -V | tail -1)
```

to:

```bash
# NEW — reads the highest-numbered iter dir that ACTUALLY ran (has verdict.json)
last_iter_dir=""
for _d in $(ls -d "$epic_dir"iter-*/ 2>/dev/null | sort -V -r); do
  if [ -f "${_d}verdict.json" ]; then
    last_iter_dir="$_d"
    break
  fi
done
```

Same pattern in `finalize.sh` for the `last_iter` integer (display only).

## Files affected (researcher fork paths; upstream paths may differ)

- `.agentflow/mini-orch/lib/auto-merge.sh` line ~152
- `.agentflow/mini-orch/lib/finalize.sh` line ~42

In this upstream repo (`~/ps/mini-ork`) at the time this TODO was filed, `lib/` only contains `providers/` — there is no equivalent `finalize.sh` or `auto-merge.sh` here yet. When those files land in this repo (or when the orch runtime is mirrored back from a fork), apply the same fix.

## Optional cleanup (separate epic)

Fix the upstream cause: don't pre-create iter-N+1's dir from inside iter-N's wrap-up when N == MAX_ITER. Move the pre-create into the iter loop's "next iteration starts" path instead.

## Acceptance for this TODO

- [ ] When this upstream gains an equivalent of `finalize.sh` + `auto-merge.sh`, apply the patch above.
- [ ] Add a unit test: build a fake run-dir layout with iter-1 (verdict.json APPROVE) + iter-2 (no verdict.json, only feedback.md), confirm `pick_last_iter_with_verdict` returns iter-1.
- [ ] Optionally fix the root cause (move pre-create out of the prior iter's wrap-up).
