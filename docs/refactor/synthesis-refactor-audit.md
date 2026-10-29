# Synthesis — merge review `wt/t1-collapse-floor-breaker` → `main`

**Diff under review:** `mini_ork/recovery/circuit_breaker.py` (+133), `tests/unit/test_circuit_breaker_floor.py` (+300) at `0f4778e7`.
**Panel:** 5 anonymous responses, 5 distinct lenses (integration-seam, dispatch/cost, tactical scan, refactor, architecture).
**Panel verdict:** 5 of 5 said **ADJUST**.

**ID scheme.** The panel's own numbering is normalized here so every cross-reference is traceable: Response A `A-1…A-18`, Response B `B-1…B-15`, Response C `C-1…C-26`, Response D `D-1…D-12` (= its `refactor-1…12`), Response E `E-1…E-10` (= its `F1…F10`). Response A's "Missing tests" list is `A-T1…A-T6`.

**Consensus marker.** ★ = raised by 2+ responses; the count is `n/5`.

**One-line verdict.** The commit is a faithful transcription of its spec — the defects are in the *seam design*, and they are severe: the latch (the feature's entire durable state) is cleared by any non-evaluation, keyed at the wrong scope, consumed by the first caller, and the trip itself never reaches the only mechanism in the framework that actually stops spend. All five responses independently reached ADJUST.

---

## Section 1: Severity × leverage matrix

**Leverage.** **HIGH** = decides whether the signal fires, mis-fires, or has any effect on the live RSI cycle (fix ≤ 3 eng-days). **MED** = decides whether the operator can see, diagnose, or trust the signal, or whether it survives >10× run volume (fix ≤ 1 eng-week). **LOW** = hygiene, naming, documentation drift, cost micro-optimisation (non-blocking).

### P1 × HIGH leverage

| Finding | IDs | Cons. |
|---|---|---|
| The latch is cleared by **any non-evaluation** — disabled, import-fail, detector-raise, insufficient-history are all `fired=False`, and the token is rebuilt from scratch every verdict. One transient error ⇒ one re-trip. | A-2, A-3, C-7, D-1, E-3 | ★ **5/5** |
| **Latch scope ≠ signal scope.** The floor measures `task_class` (`collapse_floor.evaluate(con, tr["task_class"])`, `collapse_floor.py:88-97`); the latch lives in the `task_class::recipe` row (`circuit_breaker.py:522`). Two recipes ⇒ two latches ⇒ one breach trips N times. | A-1, C-2, C-3, D-3, E-2 | ★ **4/5** |
| **The trip is consumed by whichever caller evaluates first.** `verify.py:636` then `publisher.py:579` both call `gate_run_all`; the first trips and latches, the second sees the latch and returns `PROCEED`. The publisher's `safety_violation` block can therefore **never** fire on a floor trip. | A-6, B-1, E-1 | ★ **3/5** |
| **The trip never reaches the only pre-spend stop.** Signal 5 lands on the liveness gate (verify/publish — after the tokens are paid for); the framework's real halt is `rsi/learn.py:147 → <home>/rsi/DISABLED` (`:215`), which stops the *next* cycle from dispatching. The diff does not use it. | A-13, B-4, B-7, E-8 | ★ **3/5** |
| **OPEN is erased on the very next evaluation**, so the 1800 s cooldown never engages and the breaker is not operationally open for a second. | A-6, B-1 | ★ **2/5** |

### P1 × MED leverage

| Finding | IDs | Cons. |
|---|---|---|
| `report.get("recommendation")` sits **outside** the `try` at `circuit_breaker.py:452`. A non-dict report raises out of `check_liveness_breaker` → caught by `native_gates.py:180-181` `except Exception: return "defer"` → **the whole liveness gate** (3 stagnation signals + `collapse_halt` + the floor) becomes a no-op. Fail-*vanish*, not fail-open. Latent today only because `collapse_floor.evaluate:97` raises `TypeError` first; the pre-existing `_eval_collapse_signal` has the same unguarded read at `:358-361`, so the pattern is inherited. | C-1 | ★ 1/5 — **latent-high, verifiable, one-line fix ⇒ required** |
| `task_key = kickoff_path` (`collapse_floor.py:81`) ⇒ every group has one member ⇒ `any(group[:3])` ≡ `group[0]` ⇒ **pass@k ≡ pass@1** and `FLOOR_K=3` is dead code. Live: `verified_artifact` 399 runs / 399 distinct kickoff paths. | A-9, B-3, C-5 | ★ **3/5** |
| The floor's terminal filter is `status IN ('published','failed')` (`collapse_floor.py:21`) while the canonical set is `run_status.TERMINAL` (`:37`) and `UNSUCCESSFUL` (`:41`) — `rolled_back` runs are invisible, so a **rollback-driven** collapse never breaches. | A-11, B-2 | ★ **2/5** |
| The floor report is **produced on every evaluation and discarded at the gate boundary**: `native_gates.py:169-180` and `gate_registry.py:223-224` read only `payload["verdict"]`. The whole `out["collapse"]` subtree has zero production readers. A permanently broken floor is indistinguishable from a quiet one. | A-4, C-8, E-4 | ★ **3/5** |
| **Zero lane telemetry reads as "narrowed."** Missing `tr-implementer-%` traces ⇒ `recent.lane_count == 0` ⇒ `0 < prior and 0 <= max_lanes` ⇒ `narrowed=True` ⇒ `halt`. Missing telemetry is being read as evidence of collapse. | C-10, D-2 | ★ **2/5** |
| The latch is a **read-modify-write with no transaction** (read `:524-527`, write `:678-696`); two concurrent evaluations in one scope both read "unlatched" and both trip. | A-14, C-17, E-5 | ★ **3/5** |
| `MO_CB_FLOOR_WINDOW` is **unbounded** (`collapse_floor.py:26-36`). A plausible operator value silently switches the safety signal off: window 100 ⇒ 3 of 15 classes still evaluate; window 200 ⇒ 1; window 500 ⇒ 0 — all reporting `"insufficient history"`, indistinguishable from healthy. | A-16, B-6, B-13, C-6, C-20, D-10 | ★ **4/5** |
| The `narrowed` predicate never fires on live data (lane sets *grow* half-over-half), so **the wired signal is inert on arrival** — consistent with the kickoff's own backtest ("today they recommend `none` for every class"). Root cause is pre-existing `collapse_detector.py:239-242`, not this diff. | C-4 | ★ 1/5 — high + live receipt; **required as a shipping constraint, not a patch** |

### P1 × LOW leverage

| Finding | IDs | Cons. |
|---|---|---|
| The LIVENESS_TRIP `remediation` string (`:755-761`) names `MO_CB_DISABLE` and window knobs but **not** `MO_CB_COLLAPSE_FLOOR=0` — the only knob that silences *this* trip. An operator following the text cannot stop a floor trip without disabling the whole breaker. | C-18, E-7 | ★ **2/5** |
| `MO_CB_COLLAPSE=0` now also kills the floor (`:487`), silencing two signals with **opposite trigger semantics** (level-triggered `collapse_halt` vs edge-triggered floor) — and the act of silencing resets the latch. | A-8 | ★ 1/5 |
| The web panel compares `state = 'open'` (lowercase, `web/routes/learning.py:670,688`) against the stored `'OPEN'` (written at `circuit_breaker.py:602, 607`). The open-breaker count is structurally 0 and open breakers never sort first. | A-5 | ★ 1/5 — verified; folded into W4 |

### P2 × HIGH leverage

| Finding | IDs | Cons. |
|---|---|---|
| **One collapse oracle, three consumers.** Promote `collapse_floor.evaluate` to the single read-side oracle; let `rsi/learn.py` read it beside `detect` and write the same `DISABLED` sentinel; the breaker then carries a verdict, not collapse logic. | B-4, B-(arch A), E-8 | ★ **3/5** |
| **Move the actuator to the router/promotion controller**, not the per-run admission gate — the run gate only *reads* the latch. | B-(arch A), E-8 | ★ **2/5** |
| **Durable latch state**: `breaker_latch(scope, signal, tripped_run_id, opened_at, cleared_at)` + `BEGIN IMMEDIATE`; stop encoding durable state as a CSV token inside the `last_reason` audit column. | B-10, C-16, C-17, D-8, E-5 | ★ **3/5** |
| **`register_breaker_signal(name, fn, trigger="level"\|"edge", scope=…)`** — edge-trigger semantics written once, not re-derived per signal. | E-10 | ★ 1/5 |

### P2 × MED leverage

| Finding | IDs | Cons. |
|---|---|---|
| **Adaptive, memoised floor read**: skip entirely below `n < 2*window`; fetch `min(2w, available)`; cache on `(task_class, count, max(created_at))`. The floor read is ~48% of the breaker's DB time and runs ≥2× per run. | B-5, B-6, C-11, C-20 | ★ **4/5** |
| **One terminal vocabulary**: derive the floor's filter from `run_status.TERMINAL` / `UNSUCCESSFUL` instead of a hand-written two-value literal. | A-11, B-2, B-(arch C) | ★ **4/5** |
| **Promotion-grade inputs**: stable `task_key` (content hash, not path), normalised lane family set (`agent_version_id` may be a two-element retry family — `trace_store.py:195-205`), per-run lane width rather than a union across the half. | A-9, A-10, B-8, B-(arch C), C-14 | ★ **4/5** |
| **Composite index** `task_runs(task_class, status, created_at DESC)` — today the class partition is fully sorted per evaluation (`USE TEMP B-TREE FOR ORDER BY`). | C-12, E-6 | ★ **2/5** |
| **Report-only / observe-first mode** (`MO_CB_FLOOR_MODE=report`) for a default-ON halt on a live 10-hour autopilot. | D-12 | ★ 1/5 — rollout safety; single-source but a design defect with a concrete anchor |

### P2 × LOW leverage

| Finding | IDs | Cons. |
|---|---|---|
| One lazy-import path; drop the module-level `try: import mini_ork.learning.*` side effect that only exists to make `getattr(learning, "collapse_floor")` resolve. | A-15, C-24, D-4, D-6, E-10 | ★ **4/5** |
| Rename the `floor_runs` kwarg (it shadows `collapse_floor.floor_runs`, the production reader) and make `floor_runs=[]` mean "not injected" rather than "signal off". | A-17, A-18, C-26, D-9 | ★ **4/5** |
| `out["floor"]` as a **sibling** of `out["collapse"]` — the module docstring itself argues against nesting an independent signal inside another signal's namespace. | C-22, D-5 | ★ **2/5** |
| Funnel both breaker call sites through one wrapper — `native_gates.py:169` forwards the `MO_CB_*` knobs, `gate_registry.py:223` forwards none; same row, different parameters, order decides the outcome. | A-14, E-1 | ★ **2/5** |
| Per-signal log dedupe (`_LAST_COLLAPSE_LOG` is one global string shared by two signals). | D-7 | ★ 1/5 |
| Frozen-kernel contract: `config/kernel-frozen.yaml:18` lists `circuit_breaker.py`; the diff arms a halt by editing the frozen path. Record the exception. | B-15, C-21 | ★ **2/5** |

### P3 (long-horizon) — see Section 4

`A-7`/`B-11`/`C-19` env-var registry ★3/5 · `A-12` two incompatible report shapes · `A-14` dual-writer defaults · `B-9` audit trail overwritten · `B-12` payload size · `C-22` namespace · `C-25` no gate-level test · `E-8`/`E-9` docstring + ARCHITECTURE drift · `E-9` statistical false-positive rate.

---

## Section 2: Top 5 immediate wins (P1)

Total **9.0 eng-days ≈ 1.8 weeks** — inside the <2-week budget. Effort is one implementer who knows the breaker.

### W1 — The latch must survive anything that is not an affirmative no-breach
**IDs:** A-2, A-3, C-7, D-1, E-3 (★5/5) · **Effort: 1.0 day**
**One-line fix:** make `_eval_floor_signal` return a tri-state `(fired, evaluated, report)` and clear the `collapse_floor` token only when the floor was *enabled, ran, and reported `recommendation != "halt"`* — disabled / import-fail / raise / insufficient-history must carry the previous token forward (`circuit_breaker.py:412-418, 425-450, 565-569, 671-676`).
**Why first:** it is the cheapest fix with the largest blast radius. Today one transient `sqlite3.OperationalError` inside `collapse_floor.floor_runs` — which *swallows* the error and returns `[]` (`collapse_floor.py:49-50, 68-69`) — silently converts "we could not read" into "no breach", and the next evaluation re-trips. The commit's central claim ("trips once per breach") is false without this. D-1 supplies the exact patch shape; A-2/A-3 supply the reproductions.

### W2 — Harden the floor→trip boundary at one line
**IDs:** C-1 (latent-high), D-1, D-2, C-10 · **Effort: 1.5 days**
**One-line fix:** move `report.get("recommendation")` inside the `try` at `circuit_breaker.py:452`, and honor `halt` only when `report["recent"]["lane_count"] >= 1` and `n >= 2*window` — otherwise return `fired=False` with the reason preserved.
**Why second:** `report.get` on line 452 is the one unguarded expression in the new code, and the only handler above it is `native_gates.py:180-181` `except Exception: return "defer"`. One defective report does not disable the floor — it disables the *entire liveness gate*, i.e. all three stagnation signals and `collapse_halt` as well. **One correction to C-1's receipt, which I verified in the worktree:** C-1 says the pre-existing `_eval_collapse_signal` "wraps *and* reads the report inside the same guarded region (`:350-361`)". It does not — its `try` ends at `:352` and its own `report.get(...)` reads sit outside it at `:358-361`. The pattern is pre-existing, not new. What *is* new is the exposure: the collapse signal catches only `(KeyError, TypeError, ValueError)`, while the floor's helper catches `Exception` around the call and then reads the result unguarded, and `AttributeError` on a non-dict report is in neither guard's scope for the *read*. In production the path is currently saved by an accident one level down — `collapse_floor.evaluate` does `report["task_class"] = task_class` (`:97`), which raises `TypeError` inside the `try` for a non-dict — so C-1 is **latent-high, not live-critical**: it is one callee change away from a live incident, and the fix costs one line. Required on that basis, not on C-1's severity label. The zero-lane clause (D-2) is the same edit: `0 < prior and 0 <= max_lanes` currently turns a telemetry gap into a halt.

### W3 — Key the latch where the signal is measured
**IDs:** A-1, C-2, C-3, D-3, E-2 (★4/5) · **Effort: 1.5 days**
**One-line fix:** hold the floor latch at `task_class` scope — a dedicated `"{task_class}::__collapse_floor__"` row or a dedicated column — instead of inside the `task_class::recipe` row (`circuit_breaker.py:522, 565-568, 671-696`).
**Why third:** the measurement is per-class (`collapse_floor.evaluate(con, tr["task_class"])`, `collapse_floor.py:88-97`; SQL has no recipe predicate at `:19-23`) and the latch is per-scope. Two recipes under one class therefore see the same breach and hold independent latches — A-1 verified two `LIVENESS_TRIP`s for one breach; C-2 measured 9 recipes under `code_fix`; E-2 observed that every new `code-fix__probe_*` scope starts unlatched. Each extra trip marks a run `failed`, and `failed` runs are *inside* the floor's own history (`collapse_floor.py:21`) — the deadlock the feature exists to prevent. E-2's `<recipe>__probe_*` case is the worst: probes are the mechanism that explores other options, and a diversity collapse blocks exactly those.

### W4 — Make the breach *reported*, not just detected
**IDs:** A-4, A-5, C-8, C-18, E-4, E-7 (★4/5 + ★2/5) · **Effort: 2.0 days**
**One-line fix:** carry the floor's `recommendation`/`reason`/`pass_at_k_drop`/`narrowed` into the `gate_run_all` result row and into `verify.py`'s `detail`; fix `web/routes/learning.py:670,688` to compare `'OPEN'`; add `MO_CB_COLLAPSE_FLOOR=0` to the remediation text at `circuit_breaker.py:755-761`.
**Why fourth:** the spec promises "later runs proceed **with the breach reported**". Today `native_gates.py:169-180` and `gate_registry.py:223-224` read only `payload["verdict"]`; `verify.py:648-657` emits `"gate(s) failed: oracle-liveness"`; the entire `out["collapse"]` subtree (`:743-747`) has **zero** production readers. A halted RSI cycle names no cause, and a permanently broken floor is indistinguishable from a quiet one — which collides head-on with the project rule at `docs/SAFETY.md:64` ("must not add catch blocks that hide failures"). Failing open is right for the *verdict*; failing *quiet* is wrong for the *record*. **Ship A-12 with this one:** the report has two incompatible shapes (`:412-418` returns three keys, `:452` returns the full report), so wiring it into the gate detail turns `out["collapse"]["floor"]["prior"]` into a live `KeyError` on exactly the paths where an operator most wants to look.

### W5 — Make the floor able to fire at all
**IDs:** A-9, A-11, B-2, B-3, C-5, C-6 (★4/5) · **Effort: 3.0 days**
**One-line fix:** derive `task_key` from a stable task identity instead of the epic's kickoff path (`collapse_floor.py:81`); derive the terminal filter from `run_status.TERMINAL`/`UNSUCCESSFUL` instead of a two-value literal (`:21`); clamp `MO_CB_FLOOR_WINDOW` and make "insufficient history" a *distinguishable* status rather than `"recommendation": "none"` (`:26-36`, `collapse_detector.py:232-233`).
**Why fifth:** it is the largest single item and the highest ROI, because without it the feature is dead code on arrival. The metric the commit names ("pass@k") is not the metric computed — `any(group[:3])` over one-element groups is `group[0]`, so `FLOOR_K=3` never fires and the signal is structurally inert for repeated-kickoff task classes (B-3: identical data, `drop=0.0` and no trip with a repeated kickoff; C-5: `verified_artifact` 399 runs / 399 distinct paths). `rolled_back` runs — the normal outcome of a rollback — never enter the history, so a rollback-driven collapse reads as *healthier* than it is (B-2, A-11). And an over-large window silently removes the signal from most classes while still reporting "no breach" (C-6).

### P1 remainder — also required, ordered after the top 5

| ID | Requirement | IDs | Effort |
|---|---|---|---|
| R4 | While the latch is held, keep `state = "OPEN"` and retain `opened_at` so the cooldown actually gates the next evaluation; and make the trip **idempotent per run** (store `tripped_run_id`; re-evaluating the *same* run must trip again). Otherwise verify's evaluation consumes the trip and the publisher's `safety_violation` block never fires. | A-6, B-1, E-1 (★3/5) | 1.5 d |
| R7 | Wrap the latch read + state upsert in `BEGIN IMMEDIATE`, or move to a conditional upsert on a dedicated `breaker_latch` row. | A-14, C-17, E-5 (★3/5) | 1.0 d |
| R8 | Test the **failure contracts against the production read path**, not the seam: two recipes in one class; latch preserved across a fail-open and across a disabled run; re-evaluating the tripping run; `fired_count == 0` invariant on a floor-only trip; zero-lane-is-not-a-halt; a gate-level `fail` through `native_gates._eval_oracle_liveness`; `MO_CB_FLOOR_WINDOW` through the breaker. | A-T1…A-T6, B-14, C-9, C-25, D-11, E-6 (★6/6) | 2.0 d |

**Full P1 set: 13.5 eng-days ≈ 2.7 weeks.** R8 is the regression net for W1–W5, R4 and R7 — without it, the other seven can silently regress, which is precisely the state the current 300-line test file is in.

---

## Section 3: v0.x+1 architectural shifts (P2)

Bundled by theme. These are the substrate-level changes; each bundle is a coherent unit of work.

### Bundle 1 — Data layer: durable, scoped, typed breaker state — **2 eng-wks**
- A `breaker_latch(scope, signal, tripped_run_id, opened_at, cleared_at)` table replacing the CSV token inside the `last_reason` audit column (`circuit_breaker.py:565-568` parse, `:671-676` write, `:693` join). `last_reason` becomes what it says: an audit field.
- A migration adding the composite index `task_runs(task_class, status, created_at DESC)` — the floor's query currently sorts the whole class partition per evaluation.
- One terminal vocabulary: the floor and every other terminal-run reader consume `run_status.TERMINAL` / `UNSUCCESSFUL` (`run_status.py:37, 41`) instead of hand-written literals.
- **Prerequisite P1s:** W1, W3, R7.
- **Risk if deferred:** the latch is the *entire* durable state of an edge-triggered safety signal, and it is stored in a free-text column a second signal will corrupt. Every future signal re-litigates the same encoding bug, and the first one to write a token containing a comma breaks the latch with no error.

### Bundle 2 — Runtime: one collapse oracle, three consumers — **3 eng-wks**
- Promote `collapse_floor.evaluate` to the single read-side oracle. `rsi/learn.py:147` reads it beside `collapse_detector.detect` and writes the same `<home>/rsi/DISABLED` sentinel (`:215`) — the only mechanism in the framework that stops *dispatch*, not just publish.
- The breaker then carries **no** collapse logic; it carries a verdict it was handed. The latch can leave the breaker entirely: a spend-stop needs a durable "collapsed since" record with an operator-controlled re-arm rule, not an edge latch.
- Add `register_breaker_signal(name, fn, trigger="level"|"edge", scope=…)` following the existing `register_gate_evaluator` pattern, so edge semantics (latch scope, run idempotency, preserve-on-fail-open) are written once.
- **Prerequisite P1s:** W4, W5 (the oracle must be trustworthy before two consumers read it).
- **Risk if deferred:** the feature's protection stays at roughly 2.5% of a collapsed episode (B-4: ~$39 of a $40 episode at $1/run), and every trip costs a run while saving $0 of LLM spend. **The signal as merged is an audit notification dressed up as a halt.** That is the single most expensive line in this audit.

### Bundle 3 — LLM-dispatch: move the actuator to the router — **3–4 eng-wks**
- The floor becomes an input to the lane router (force exploration) and the promotion controller (freeze promotion for the class), not to the per-run admission gate. The run gate only *reads* the latch.
- Per-recipe opt-out in `task_class.yaml` instead of a global env var; probe recipes (`<recipe>__probe_*`) inherit their parent class's latch.
- **Prerequisite P1s:** Bundle 2 complete.
- **Risk if deferred:** a population signal keeps steering a per-run gate. The only causal path from the trip back to the policy is the failed trace's reward through GRPO writeback — and E-1 shows that path is order-dependent, so the surviving lane may be penalised for a condition of the whole population. That is an unmeasured, unintended learning signal on the live RSI cycle.

### Bundle 4 — Observability: a loud record, and a safe rollout — **2 eng-wks**
- A `safety_events` row (or breaker-event log) carrying the floor report and the fail-open reason, attached to the run's trace; OTel span attributes later. This satisfies `docs/SAFETY.md:64` without turning detector bugs into kill switches.
- `MO_CB_FLOOR_MODE=report` — observe-first mode: the report says `breach=true, would_trip=true` while `fired=False`. The same report→act split `mini-ork collapse-check --floor` already models (`cli/collapse.py:41-71`).
- Documentation: `MO_CB_COLLAPSE_FLOOR` / `MO_CB_FLOOR_WINDOW` into `docs/operator/env-vars.md` (grep for `MO_CB` in `docs/` returns **nothing** today — the entire `MO_CB_*` family is undocumented); a "the diversity floor" subsection in `docs/operator/rsi-cycle.md` next to "The collapse halt"; `docs/ARCHITECTURE.md:92` updated so `liveness_gate` covers population-collapse signals; a first-class "latched" field on `/circuit-breakers`.
- **Prerequisite P1s:** W4; W2 and W5 for the report to be safe to publish.
- **Risk if deferred:** the floor already fails open *silently* — `collapse_floor.floor_runs` swallows `sqlite3.OperationalError` and returns `[]`, both gate call sites discard the payload, and the only trace is one stderr line per process. The operator who needs `MO_CB_COLLAPSE_FLOOR=0` cannot discover it exists. This bundle is the difference between "the signal is armed and wrong" and "the signal is armed and *visible*".

### Bundle 5 — Statistics: promotion-grade detection — **2–3 eng-wks**
- Replace the half-window `Δ > 0.2` test with a sequential test (CUSUM/SPRT) and pool sparse classes by recipe family. Normalise the lane family set and measure lane width per run. Give the floor a stable task identity.
- **Prerequisite P1s:** W5, Bundle 1.
- **Risk if deferred:** at pass@1 ≈ 0.7 with 20-run windows, SE(Δ) ≈ 0.145, so a 0.2 drop is ≈ 1.4σ — roughly 8% one-sided per independent window pair *before* the narrowing conjunction. Live healthy classes already show drops of 0.10–0.17. The false-trip rate is unmeasured, and only 6 classes have enough history to measure it.

---

## Section 4: Long-horizon (P3 + advisory)

Tracked, not load-bearing today — with one exception flagged below.

| ID | Item | Why tracked |
|---|---|---|
| **A-12** | The floor report has **two incompatible shapes**: three keys on the disabled/fail-open paths (`:412-418`, `:431-435`, `:446-450`), the full report on the success path (`:452`). | **Not optional.** `out["collapse"]["floor"]["prior"]` raises `KeyError` on exactly the paths an operator most wants to look. It becomes a live bug the moment W4 wires the report into the gate detail. **Ship it with W4.** |
| A-7 / B-11 / C-19 | `MO_CB_COLLAPSE_FLOOR` and `MO_CB_FLOOR_WINDOW` exist only in the module docstring (`:24-27`). `grep -rn "MO_CB" docs/` → nothing. | The whole `MO_CB_*` family is missing from `docs/operator/env-vars.md`, `feature-flags.md`, and `rsi-cycle.md`. Part of Bundle 4. |
| A-8 | `MO_CB_COLLAPSE=0` is now a two-signal kill switch with opposite trigger semantics. | Document the widened scope, or split the knob. |
| A-14 | Two breaker call sites write the same row with different parameters — `native_gates.py:169` forwards the knobs, `gate_registry.py:223` forwards none. | Order decides the outcome. Part of Bundle 1's wrapper. |
| B-5 / B-6 / C-11 / C-12 | Floor read cost: ~48% of the breaker's DB time, ≥2 calls per run, `USE TEMP B-TREE FOR ORDER BY` on the class partition. | Trivial at 1000 runs/day; a profile entry at 100×. Bundles 1 and 2. |
| B-9 | The floor branch replaces `last_reason_tokens` with `["collapse_floor"]`, discarding `fired_names`. | Kills the audit trail of *which* signals fired. Cheap fix, real diagnosis-time value. |
| B-12 | The full `floor_report` (with `prior`/`recent` lane lists) is embedded in the JSON on every evaluation. | Window-proportional payload on a path that runs twice per run. Project it to a fixed shape for the audit row. |
| B-15 / C-21 | `config/kernel-frozen.yaml:18` lists `mini_ork/recovery/circuit_breaker.py`. | The freeze covers run-authored patches, not framework merges — so the commit is legitimate — but the operator should **record the exception**: the load-bearing module gained a fifth signal that can halt publishes. |
| C-4 | The `narrowed` predicate (`collapse_detector.py:239-242`) never fires on live data because lane sets *grow* half-over-half. | Root cause is pre-existing, not this diff. **Constraint, not a patch:** do not ship the signal armed until the predicate is validated against a shadow-mode cycle (Bundle 4) or made baseline-relative. |
| C-25 | No test asserts the floor through the gate (`native_gates` → `"fail"`). | Part of R8. |
| C-26 / D-9 | `floor_runs` (kwarg) shadows `collapse_floor.floor_runs` (reader); `floor_runs=[]` silently disables the signal. | Rename to `floor_history`; make `[]` mean "not injected". |
| D-7 | `_LAST_COLLAPSE_LOG` is one global string shared by two signals. | The two signals evict each other's dedupe slot — the exact spam the function exists to stop. |
| E-8 / E-9 / E-R7 | The module docstring still calls this a "faithful port" of `lib/circuit_breaker.sh` — which no longer exists — and omits `floor_runs` from the public surface. `docs/ARCHITECTURE.md:92` is not updated. | Stale, not harmful, but it is the kind of comment a future reader trusts. This module is now the source of truth; describe it as such and list each signal with its trigger type. |
| E-10 | `register_breaker_signal` registry. | Bundle 2. The diff copies the collapse pattern a second time; there will be a third. |

---

## Section 5: Hardest open question

**Chosen (from Response E):** *Does blocking one run per breach change the policy that caused the breach?* — and its statistical twin: *what is the false-trip rate of a 0.2 drop threshold on 20-run windows?*

**My assessment: the proposed mitigations are NOT sufficient. More research is needed — specifically, a measurement, not another patch.**

Why the panel's mitigations do not close the question:

1. **The causal path is severed in the common ordering.** The only route from a trip back to the policy is the failed trace's reward through GRPO writeback. E-1 shows that path is order-dependent: `verify.py:636` consumes the trip and latches it, so `publisher.py:579` sees `PROCEED` and the `safety_violation` block never fires. R4 (idempotency, keep-OPEN) fixes the *frequency* of trips; it does not make one trip change a routing policy. No proposed mitigation measures whether a single 0.0 reward moves `lane_router` toward exploring.
2. **Every mitigation here is about the detector, not the actuator.** W1–W8 make the signal correct, scoped, atomic, reported and tested. Bundle 3 (move the actuator to the router) is the only change that could answer the question — and it is explicitly a v0.x+1 item with no measurement plan attached.
3. **The statistics are not good enough to arm a safety signal.** SE(Δ) ≈ 0.145 at pass@1 ≈ 0.7 with 20-run windows makes a 0.2 threshold ≈ 1.4σ, ≈ 8% one-sided per independent window pair *before* the narrowing conjunction. The union-of-halves lane measure (A-10) and the string-counted lane families (B-8) make the conjunction's false-positive rate unmeasurable from the current data — only 6 classes have n ≥ 40 and none breaches (E-2). You cannot certify a halt on an unmeasured rate.
4. **There is a live confound nobody has separated.** `failed` includes infrastructure failures, and lane preflight drops dead providers. A provider outage produces "pass@k fell" *and* "lanes narrowed" simultaneously, with no policy collapse at all. Until the floor distinguishes policy regression from environment failure, its false-trip rate is unbounded by the numbers above.

**What would settle it** — in order of cost:
- **Shadow mode for one full RSI cycle** (D-12 / Bundle 4): `MO_CB_FLOOR_MODE=report`, logging `would_trip` on every evaluation. This measures the empirical false-trip rate on live data *and* produces the replay corpus the statistical question needs. It is the cheapest experiment available and it is a prerequisite for arming the signal honestly.
- **A historical replay** over `task_runs` with a bootstrap/CUSUM instead of the half-window Δ, reporting per-class false-trip counts and a paired comparison against the current predicate — plus the same replay with provider-outage runs excluded, to size the confound.
- **A GRPO feedback measurement**: does a single failed trace on the surviving lane measurably shift `lane_router` toward exploration? If not, the feature is a notification; if yes, it is an accidental learning signal that must be made deliberate (Bundle 3) or removed.

**Interim stance:** keep the floor **off** (or in report mode) on the live RSI cycle until at least one shadow cycle and one replay exist. The commit's default-ON setting is the part I would not merge as-is: a default-ON *halt* over an unmeasured statistic, on a loop that runs for 10 hours unattended, is the exact risk the kickoff exists to catch.

---

## Section 6: Dogfood reflection

**Was this audit reproducible via the framework?** Yes — cleanly, and the recipe's coordination contract held.

- The run executed all 11 nodes declared in `recipes/refactor-audit/workflow.yaml`: planner → 5 lenses → `panel.anonymize@v1` → synthesizer → `lens_completeness` verifier → publisher → rollback.
- All five lens nodes and the transform finished on **attempt 1**, `finish_reason=done`, `rc=0` (`retry-exhausted/*.attempts.json`) — no retries, no exhausted lanes.
- All five lens artifacts exist and are substantial (16–31 KB each), and the deterministic transform materialized exactly one anonymous response per lens. The synthesizer's declared input manifest contained **only** `panel_reports` — the anonymity boundary held, and no lens could see another lens's file.
- **Groundedness: yes.** The responses are not opinions about the diff; they are reproductions. A and D built scratch DBs and captured the trip→latch transcripts; B captured probe output showing `floor_runs` at 48% of the breaker call; C ran `EXPLAIN QUERY PLAN` on the live `state.db` and evaluated the floor per class; E ran a scratch probe through the `floor_runs=` seam. I independently re-verified a sample of the load-bearing anchors in the review worktree and they all hold: `circuit_breaker.py:452` (unguarded `report.get`), `:465` (`floor_runs` kwarg), `:487` (knob wiring), `:522` (scope key), `:565-569` (latch read), `:595-598` (independent trip), `:612` (`new_state = "CLOSED"`), `:675-676` (latch write), `:747` (report nested in `out["collapse"]`); `collapse_floor.py:21` (terminal filter), `:26-36` (unbounded window), `:81` (`task_key` = `kickoff_path`), `:88` (`evaluate` takes `task_class` only); `collapse_detector.py:152-153` (`FLOOR_WINDOW=20`, `FLOOR_K=3`), `:232-233` (`n < 2*window`), `:239-242` (`narrowed`); `native_gates.py:169-181` and `gate_registry.py:223-224` (payload discarded, `except Exception: return "defer"`); `run_status.py:37,41` (`TERMINAL`, `UNSUCCESSFUL`); `web/routes/learning.py:670,688` (`state='open'` vs the stored `'OPEN'`); `rsi/learn.py:147,215` (`detect` → `rsi/DISABLED`); `config/kernel-frozen.yaml:18`; `docs/SAFETY.md:64`; and `grep -rn "MO_CB" docs/` → no matches. Every anchor I checked is accurate.

**Did any lens get blocked by something the audit itself identified?** **Yes — three times, and that is the most useful output of this run.**

1. **The audit's own W2 applies to this audit.** `lane-preflight.json` for this run reports two **unverified** lanes — `codex` and `opus`, both researcher lenses, reason *"ok (unprobed: native lane — auth owned by the CLI)"* — and `execute.log` carries the matching `warn:` lines. The framework proceeded anyway. Under the rule this panel spent 5 responses arguing for ("unobservable is not evidence" — W2, D-2, `docs/SAFETY.md:64`), two of the five inputs to this audit were unverifiable at dispatch. The framework's own `MO_LANE_REQUIRE_VERIFICATION=1` would have refused the run. **The audit was run in the fail-quiet mode it condemns.**
2. **The composition step did not run on the pinned lane.** `execute.log`: `[route] policy=learning_governed node=reviewer lane=reviewer->nemotron_free source=explore`. The operator overlay pins `reviewer: opus`, but the `learning_governed` routing policy overrode it to `nemotron_free` — a *free* OpenRouter lane, which the operator's own config annotates as "tool-less — advisory only, never here". So this document's cross-lens consensus — the entire product of the synthesis node — was composed on an unpinned free-tier lane, while the panel's own W4 complains that a floor report is discarded at exactly this kind of boundary.
3. **Panel heterogeneity was flagged by the framework and the run proceeded anyway.** The launcher's own config check printed: *"lane aliases deepseek_lens, kimi_lens all resolve to family 'deepseek' (lane deepseek_flash) — a panel using them is not family-heterogeneous"*. The overlay also records `kimi_lens: deepseek_flash  # kimi lane dead since 10-16` and `minimax_lens: minimax` quota-dead until ~Nov 8 — yet `lens-kimi.md` and `lens-minimax.md` were both produced, attempt 1, rc 0, i.e. served from a fallback. **The recipe's anonymity contract forbids me from mapping any individual response to a family, and I am not doing so.** But the panel-level fact is reportable and it matters: **a ★ count of 2/5 is weaker evidence than it looks**, because at least two of the five lanes were configured to the same family. Read every consensus marker in Section 1 as "≥2 independent-ish observations", not as "two independent families agreed". Where a finding reached 4/5 or 5/5 it remains decisive — the A/C/D/E agreement on scope and the 5/5 agreement on latch-clearing are structural facts about the code, reproducible without any model at all.

**One more meta-check — did the panel contradict itself?** Yes, once, and the synthesizer should resolve it rather than average it: **B-1 lists "the scope key is per-(task_class, recipe) … so one task class's collapse does not halt another's" under "What's already right (do not break these)" — while B-3, A-1, C-2/C-3, D-3 and E-2 all say that same scope key is the defect.** B's "already right" bullet is wrong. The correct statement: the *verdict* is computed per class, so a class-wide breach can trip every scope of that class. That bullet is superseded, and it is a good illustration of why a five-lens panel needs a synthesis step that argues rather than tallies.

**Cost of the audit:** $2.71 for the full run. Cheap relative to what it found, and cheaper than the single rollback the feature could have caused on a live RSI cycle.

---

## Section 7: How to re-run

### 7.1 Re-run the audit itself (identical configuration)

```bash
S=/private/tmp/claude-501/-Volumes-docker-ssd-Migration-Development-researcher/69c08fc2-54d1-4754-ba39-2d8679676baf/scratchpad/review
R=/Volumes/docker-ssd/ps/mini-ork

export MINI_ORK_ROOT=$R MINI_ORK_HOME=$R/.mini-ork
export MO_TARGET_CWD=/Volumes/docker-ssd/ps/mini-ork-worktrees/review-t1-collapse-floor-breaker
export MINI_ORK_AGENTS=$S/agents-review.yaml
export MO_STATIC_RECIPE_PLAN=1 MINI_ORK_NONINTERACTIVE=1 MINI_ORK_PROFILE_GATE=0 MO_PAUSE_EVERY_USD=20
export MINI_ORK_RUN_ID=review-t1-collapse-floor-breaker-$(date +%Y%m%d-%H%M%S)

cd $MO_TARGET_CWD && $R/bin/mini-ork run --json refactor-audit "$S/kickoff-t1-collapse-floor-breaker.md"
```

The run being synthesized is `review-t1-collapse-floor-breaker-20261029-110750` (2026-10-29 11:07:50, cost $2.71). Artifacts: `lens-{glm,kimi,codex,opus,minimax}.md` → `panel-responses.md` → `synthesis.md` → `verifier-lens-completeness.log`.

### 7.2 Re-verify the receipts independently (no model, no network)

```bash
cd /Volumes/docker-ssd/ps/mini-ork-worktrees/review-t1-collapse-floor-breaker
PYTHONPATH=$PWD python3 -m pytest \
  tests/unit/test_circuit_breaker_floor.py \
  tests/unit/test_circuit_breaker_collapse.py \
  tests/unit/test_circuit_breaker_py.py
```

Note: the kickoff's receipt command uses `.venv/bin/python`, **which does not exist in this worktree** (E-1). Use the main checkout's interpreter with `PYTHONDONTWRITEBYTECODE=1` so no `__pycache__` is written into the review tree.

The W1/W2/W3 receipts can be reproduced directly against a scratch DB without touching the repo — `mig.init_db` a temp database, seed one `code_fix` task_class with two recipes, and call `check_liveness_breaker` with and without a raising detector. The shapes are in the panel responses (A's transcripts, C's `EXPLAIN QUERY PLAN`, D's scratch DB, E's probe).

### 7.3 What blocks self-dispatch — named explicitly

**No P1 in this audit blocks re-running the audit.** The `refactor-audit` dispatch path (planner → lenses → transform → synthesizer → verifier → publisher) never touches `check_liveness_breaker`; the floor signal only gates `mini-ork verify` and `mini-ork publish` through the liveness gate. The audit is re-runnable today, before and after any of the required adjustments.

**Two meta-conditions would have blocked *this* run**, and they are worth fixing before the next panel:

1. `MO_LANE_REQUIRE_VERIFICATION=1` — with it set, this run would have been **refused** at preflight, because `codex` and `opus` are unverified native lanes (`lane-preflight.json`, `unverified: [{lane: codex, …}, {lane: opus, …}]`). For a panel whose value is independent judgment, two unverifiable inputs are not a cosmetic problem.
2. The `learning_governed` routing policy silently overrode the pinned `reviewer: opus` with `nemotron_free` (`execute.log`). A synthesis whose entire product is cross-lens consensus should not be routed to a free advisory lane by a policy the operator did not know was in play. Pin the reviewer lane at the recipe level (`model_lane: opus`) rather than at the operator overlay, or make `learning_governed` respect the pin.

Neither blocks re-running the audit under the current configuration; both are cheap to fix and both are the difference between a panel whose consensus means something and a panel whose consensus is three models agreeing.

---

## Final verdict: ADJUST

**Consensus: 5 of 5 responses returned ADJUST.** (A: ADJUST · B: ADJUST · C: ADJUST · D: ADJUST · E: ADJUST.) The synthesis concurs: **ADJUST** — the commit is not ready to merge into a live RSI cycle as-is.

**Why ADJUST and not a rejection:** the commit is a faithful, byte-compatible transcription of its spec; no single defect makes the code unsafe to run; E-2 confirms no class breaches on live data today, so merging does not trip the live cycle on its first evaluation. What is wrong is the seam design — and every one of the required adjustments is a local, bounded edit (13.5 eng-days total).

**Why ADJUST and not APPROVE:** the commit's central claim — *"trips once per breach and then latches"* — is false under the real callers in three independent ways (scope, clearing, idempotency), and the trip's stated purpose (halting a collapsed policy) is not achieved by the wiring it chose. A merge that ships a safety signal which mis-scopes, re-arms on transient errors, and cannot reach the spend-stop is exactly the failure this review exists to catch.

### REQUIRED adjustments (ordered)

1. **W1 — the latch survives anything that is not an affirmative no-breach.** Tri-state `_eval_floor_signal`; clear the `collapse_floor` token only on an enabled, ran, `recommendation != "halt"` result. `mini_ork/recovery/circuit_breaker.py:412-418, 425-450, 565-569, 671-676`. *5/5 consensus.* — 1.0 d
2. **W2 — harden the floor→trip boundary.** Move `report.get("recommendation")` inside the `try` at `mini_ork/recovery/circuit_breaker.py:452`; honor `halt` only with `recent.lane_count >= 1` and `n >= 2*window`. *C-1 critical (verified); D-1, D-2, C-10.* — 1.5 d
3. **W3 — key the latch on the signal's scope (`task_class`), not `task_class::recipe`.** `mini_ork/recovery/circuit_breaker.py:522, 565-568, 671-696` vs `mini_ork/learning/collapse_floor.py:19-23, 88-97`. *4/5 consensus.* — 1.5 d
4. **W4 — report the breach.** Carry the floor's verdict into the `gate_run_all` result row and `verify.py`'s detail; fix `mini_ork/web/routes/learning.py:670,688` (`'open'` → `'OPEN'`); add `MO_CB_COLLAPSE_FLOOR=0` to the remediation text at `mini_ork/recovery/circuit_breaker.py:755-761`; ship A-12 (one canonical report shape) with it. *4/5 + 2/5 consensus.* — 2.0 d
5. **W5 — make the floor able to fire.** Stable `task_key` (not `kickoff_path`) at `mini_ork/learning/collapse_floor.py:81`; canonical terminal vocabulary at `:21`; clamp `MO_CB_FLOOR_WINDOW` at `:26-36` and make "insufficient history" distinguishable from "no breach". *4/5 consensus.* — 3.0 d
6. **R4 — keep the scope OPEN while latched; make the trip idempotent for the run that tripped.** `mini_ork/recovery/circuit_breaker.py:565-569, 600-613, 671-676`. *3/5 consensus.* — 1.5 d
7. **R7 — make the latch read-modify-write atomic.** `mini_ork/recovery/circuit_breaker.py:524-527` (read) → `:678-696` (write). *3/5 consensus.* — 1.0 d
8. **R8 — test the failure contracts against the production read path** (not the `floor_runs=` seam): two recipes in one class; latch preserved across fail-open and across a disabled run; same-run re-evaluation; `fired_count == 0` invariant on a floor-only trip; zero-lane-is-not-a-halt; a gate-level `fail`; `MO_CB_FLOOR_WINDOW` through the breaker. `tests/unit/test_circuit_breaker_floor.py`. *6/6 consensus.* — 2.0 d

**Plus one shipping constraint, not a patch:** while `narrowed` never fires on live data (C-4, `mini_ork/learning/collapse_detector.py:239-242`) and the false-trip rate is unmeasured (§5), **do not ship the floor armed** — land it in report/shadow mode (`MO_CB_FLOOR_MODE=report`, D-12) for one full RSI cycle, or fix the predicate against a class baseline first.

### Non-blocking nits (tracked; see Section 4 for the full list)

`A-7`/`B-11`/`C-19` env-var docs · `A-8` `MO_CB_COLLAPSE=0` widened scope · `A-12` two report shapes *(ship with W4)* · `A-14` dual-writer defaults · `A-15`/`C-24`/`D-4`/`D-6`/`E-10` import-time side effect and two import idioms · `A-17`/`A-18`/`C-26`/`D-9` `floor_runs` seam naming and `[]` semantics · `A-10`/`B-8`/`C-14` lane-family normalisation and union-across-half semantics · `B-5`/`B-6`/`C-11`/`C-12` floor-read cost and the missing composite index · `B-9` audit trail overwritten by the floor branch · `B-12` payload size · `B-15`/`C-21` frozen-kernel exception record · `C-9` `FLOOR_K` never exercised · `C-16`/`B-10`/`D-8` CSV-token latch register · `C-22`/`D-5` `out["floor"]` sibling key · `C-25` no gate-level test · `D-7` shared log-dedupe slot · `D-12` observe-first mode *(also a shipping constraint)* · `E-8`/`E-9` docstring and `docs/ARCHITECTURE.md:92` drift · `E-10` `register_breaker_signal` registry · `E-R9` sequential test / CUSUM for the statistical question.

### Also worth recording: the one thing the panel got wrong about itself

The framework flagged non-heterogeneous lane families for this panel before it dispatched, and routed the synthesizer off its pinned reviewer lane. Both are in §6. Neither blocks the merge decision. Both should be fixed before the next panel, because a 2/5 consensus marker on a five-model panel is not the same kind of evidence as it looks — and this document's ★ markers should be read with that discount applied.
