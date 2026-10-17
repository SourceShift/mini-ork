# LF-R7b the run profile records the effective lane map, not just aliases

## Goal

Two related holes, both about a lane's identity being unreadable after the fact.

1. **The profile's lane map is aliases only.** `gen_profile`
   (`mini_ork/cli/main.py:735-737, 791-795`) stores the effective
   `agents.yaml` `lanes:` mapping verbatim into
   `run_profile.json["provider_policy"]["lanes"]`. Those values are LANE NAMES
   (`glm`, `deepseek_flash`, `minimax`, `laguna_free`), so reading a profile
   alone you cannot tell which model family a role actually got — you must
   resolve the name through `providers.yaml` by hand.
2. **Nothing warns when the operator overlay collapses a panel onto one
   family.** The live overlay `.mini-ork/config/agents.local.yaml` maps the four
   "lens" aliases onto three lanes, and the names lie about families:
   `opus_lens: glm`, `glm_lens: glm` (both `zai`), `kimi_lens: minimax`,
   `minimax_lens: minimax` (both `minimax`), `codex_lens: deepseek_flash`,
   `deepseek_lens: deepseek_flash` (both `deepseek`). A panel launched with
   `opus_lens, kimi_lens, glm_lens, minimax_lens` believes it has four families
   and has two. `agents_config.merge` applies the overlay silently, so the
   mistake surfaces only when R7a's checker rejects the panel — after the run
   is paid for.

Decision record: `docs/plans/2026-10-15-learning-loop-fixes.md` (R7b; Q6 keeps
the overlay as-is and makes the system honest about it).

All paths are relative to the repository root.

## Mechanism (exact)

1. **Family resolution lives in one place.** Do NOT re-derive a family table.
   `mini_ork/verify/panel_heterogeneity.py` already owns
   `lane_family(lane) -> str` (`model_family(lane_model(lane))`, reading
   `providers.yaml`). Import it LAZILY inside the new functions (a module-level
   import would put `verify` on `dispatch`'s import path).
2. **`agents_config.lane_families(agents: dict) -> dict[str, dict]`** — new,
   pure. Given a loaded (merged) agents mapping, resolve every entry of its
   `lanes:` sub-mapping to `{"lane": <lane name>, "model": lane_model(lane),
   "family": lane_family(lane)}`. Aliases whose value is not a non-empty string
   are skipped. Never raises — an unreadable registry yields the lane name as
   its own family, which is what `lane_family` already does.
3. **`agents_config.lane_family_warnings(agents: dict) -> list[str]`** — new,
   pure, derived from (2). For each family that more than one LANE-RESOLVING
   ALIAS maps to, ONE line, sorted for determinism:
   `"lane aliases opus_lens, glm_lens all resolve to family 'zai' (lane glm) — a panel using them is not family-heterogeneous"`.
   A family claimed by exactly one alias produces no line. Restrict the sweep to
   aliases ending in `_lens` if that keeps the signal clean, but do make the
   real overlay above produce exactly three lines (`zai`, `minimax`,
   `deepseek`).
4. **Merge-time warning.** In `effective_path()` — the one place the merged
   config is materialised for consumers — after writing the effective file (or,
   when there is no overlay, after resolving `template_path()`), call
   `lane_family_warnings` on the loaded config and write each line to
   `sys.stderr` prefixed `[agents_config] `, once per call. Silence with
   `MO_LANE_FAMILY_WARN=0`. The warning is advisory: it must not raise, must not
   change the returned path, and must not block the merge.
5. **The profile carries the resolution.**
   `mini_ork/cli/main.py` `gen_profile`: keep `provider_policy.lanes` exactly as
   it is (readers depend on it), and ADD
   `provider_policy.lane_families` = `agents_config.lane_families({"lanes": lanes})`
   and `provider_policy.lane_family_warnings` =
   `agents_config.lane_family_warnings({"lanes": lanes})`. Field docs in the
   docstring: these are the CONFIGURED (effective) lanes — the served model per
   node is authoritative in `llm_calls.model_id` / `traces.agent_version_id`
   (the router's fallback walk can serve a different member), so a reader must
   not treat the profile's map as proof of what ran, only of what was asked.
   `gen_profile` is also called for reused profiles — the new keys must appear
   on a fresh generation only; do not mutate an existing `run_profile.json` in
   `_gen_profile_or_reuse`.

## Files in scope

- `mini_ork/dispatch/agents_config.py`
- `mini_ork/cli/main.py`
- `tests/unit/test_agents_config_family_warn.py` (new)

## Tests (`tests/unit/test_agents_config_family_warn.py`, hermetic temp dirs)

- `lane_families` resolves the live overlay's shape: an agents mapping with
  `opus_lens: glm, glm_lens: glm, kimi_lens: minimax, minimax_lens: minimax`
  gives `opus_lens` and `glm_lens` the same `family` (`zai`) and
  `kimi_lens`/`minimax_lens` both `minimax`; each entry's `model` equals
  `panel_heterogeneity.lane_model(<lane>)`;
- `lane_family_warnings` returns three lines for that mapping, naming the
  alias groups, and returns `[]` when every alias resolves to a distinct
  family;
- a non-string lane value (`opus_lens: null`) is skipped, not raised on;
- `gen_profile` on a temp kickoff + temp agents file writes
  `provider_policy.lane_families` and `provider_policy.lane_family_warnings`,
  leaving `provider_policy.lanes` byte-identical to before;
- `effective_path()` with `MO_LANE_FAMILY_WARN=1` prints the warning to stderr
  (capsys) for a collapsing overlay and prints nothing with the switch at `0`.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_agents_config_family_warn.py tests/unit/test_panel_heterogeneity.py tests/unit/test_run_profile_bullets.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_agents_config_family_warn.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r7b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r7b-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do NOT change `merge()`'s semantics, the effective
file's path or name, `provider_policy.lanes`, `panel_heterogeneity`'s public
functions, or any dispatch decision (the warning is advisory and must never
route a lane); never edit `.mini-ork/config/agents.local.yaml` (operator-owned),
and do not write to the live DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
