# B3: lane affinity — a node's later rounds stay on the lane that served it

## Goal

Every revise round and every `recover` re-enters `dispatch_node`, which re-runs
routing, `dispatch_chain` and both fuse passes, then walks the chain from its
lead again (`mini_ork/cli/execute_handlers.py` ~650-816;
`mini_ork/dispatch/llm_dispatch.py` `llm_dispatch` ~842-1300).

Measured on 127 framework-edit runs with more than one implementer attempt:
16 runs (12.6%) moved to a different model between attempts.
- **5 were fallback-walk effects.** Round 1's lead lane timed out (e.g.
  `deepseek_flash` at 1500 s), a fallback lane served the attempt, and the
  next round started on the failed lead again.
- **11 were intentional lead remaps.**

Provider prompt caches are per lane: 11,109 `llm_calls` rows carry cached
input, including 2.6B cached tokens on minimax and 2.1B on deepseek_flash.

When the chain's lead is the same as in the round that first succeeded, put
the lane that actually served that round first. When the lead changed (an
operator remap, `recover --lane`, an agents.yaml edit), affinity does nothing:
the operator's choice wins. A fused lane is still dropped by the fuse, so
affinity never resurrects one.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/dispatch/lane_affinity.py` (new; stdlib only)

```
AFFINITY_REL = ".lane-affinity.json"
def enabled(env=None) -> bool                    # MO_LANE_AFFINITY != "0" (default on)
def preferred(run_dir: str, node_id: str, lead: str) -> str | None
def record(run_dir: str, node_id: str, lead: str, served: str) -> None
def apply(chain: list[str], lane: str | None) -> list[str]
```

1. **File.** `<run_dir>/.lane-affinity.json` maps
   `{node_id: {"lead": <chain[0] when it first succeeded>, "served": <lane>, "at": <epoch>}}`.
   A missing or corrupt file reads as `{}`. Writes are atomic (tmp +
   `os.replace`). Every function swallows `OSError` / `ValueError`.
2. **`record`.** Writes the entry only when `node_id` has NO entry yet: the
   first success defines the affinity, and later rounds do not overwrite it.
   It records the lead as well as the served lane.
3. **`preferred`.** Returns `served` only when the entry exists AND
   `entry["lead"] == lead`; otherwise `None`.
4. **`apply`.** If `lane` is in `chain`, return the chain with `lane` moved to
   index 0 and the relative order of the others kept; otherwise return the
   chain unchanged (a new list).

### 2. `mini_ork/dispatch/llm_dispatch.py` — `llm_dispatch()`

1. **Reorder the chain.** After the lane chain is resolved and BEFORE the
   first `_apply_lane_fuse` pass (~993):
   - read `run_dir = context_env("MINI_ORK_RUN_DIR")` and
     `node_id = context_env("MO_NODE_ID")`;
   - when both are non-empty and `lane_affinity.enabled()`, set
     `lead = chain[0]` (the routing lead before any reordering) and
     `chain = lane_affinity.apply(chain, lane_affinity.preferred(run_dir, node_id, lead))`;
   - keep `lead` for step 2;
   - when the order changed, print one stderr line:
     `[lane_affinity] <node_id>: <served> first (served an earlier round; lead <lead>)`.
   - The fuse passes run after this unchanged, so a fused affinity lane is
     dropped as usual.
2. **Record the serving lane.** On the success path, where
   `_write_lane_sidecar(selected_model)` is called (~1191), call
   `lane_affinity.record(run_dir, node_id, lead, selected_model)` with the
   same guards. `lead` is the step-1 value; if step 1 did not run, the
   chain's first lane.
3. Wrap both in `try/except Exception: pass`; affinity must never change a
   dispatch outcome.

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/lane_affinity.py` (new)
- `mini_ork/dispatch/llm_dispatch.py`
- `tests/unit/test_lane_affinity.py` (new)

Do NOT modify `mini_ork/dispatch/providers.py`, `mini_ork/cli/execute_handlers.py`,
`mini_ork/dispatch/routing.py`, `docs/`, any migration, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_lane_affinity.py`, hermetic, no LLM)

Unit:
1. `record` then `preferred` with the same lead → the served lane; with a
   different lead → `None`.
2. A second `record` for the same node does not overwrite the first.
3. `apply(["a","b","c"], "c") == ["c","a","b"]`; a lane not in the chain
   leaves it unchanged; `None` leaves it unchanged.
4. A corrupt file → `preferred` gives `None` and no exception.
5. `MO_LANE_AFFINITY=0` → `enabled()` is false.

Through `llm_dispatch`, with a fake 5-arg `dispatch_fn` in the style of
`tests/unit/test_lane_fuse_and_chain_py.py:264-302`, a tmp `MINI_ORK_RUN_DIR`
and `MO_NODE_ID=implementer`:

6. Round 1 with chain `glm,deepseek_flash`: the fake fails `glm` (a
   non-retryable error) and succeeds on `deepseek_flash` → the affinity file
   records `lead=glm`, `served=deepseek_flash`.
7. Round 2, same chain → the fake's first call is for `deepseek_flash`.
8. Round 2 with chain `opus,deepseek_flash` (lead remapped) → first call is
   `opus`.
9. `MO_LANE_AFFINITY=0` → round 2's first call is `glm`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_lane_affinity.py tests/unit/test_lane_fuse_and_chain_py.py tests/unit/test_fuse_placement.py tests/unit/test_llm_dispatch_py.py tests/unit/test_llm_dispatch_shape_fallback.py tests/unit/test_dispatch_fallback_py.py tests/unit/test_lane_chain_resolution.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_lane_affinity.py --junitxml="${MINI_ORK_RUN_DIR}/bif-b3-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-b3-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/dispatch/lane_affinity.py mini_ork/dispatch/llm_dispatch.py tests/unit/test_lane_affinity.py` is clean.
- An operator's lane remap always wins over affinity.
