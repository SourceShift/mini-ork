# RSI Tier-1 #2: an anytime-valid promotion certificate and a per-class alpha ledger for the apply gate (default off)

## Goal

**How the gate decides today.** `mini_ork/cli/apply.py:evaluate_gate` promotes
a measured candidate when two things hold: its probe pass rate goes up
(`delta > MO_APPLY_NONREGRESSION_DELTA`), and no previously passing gate probe
now fails. The only significance check is `mcnemar_exact_p`. It runs only when
`MO_APPLY_SIG_ALPHA` is set, which is unset by default. It is a fixed-sample
p-value, applied at one constant level to every candidate, with no memory of
earlier tests. The apply loop is a stream of such tests. PACE (`2606.08106`)
measured greedy acceptance committing 30–42% false edits in exactly this
setting. TOP-20 calls the certificate "the single highest-leverage change".

**The live numbers.**
- There are 43 `apply_attempts` rows; the last is from 2026-10-13. Their 19
  "promotions" were scored by a mock and reverted.
- Only 2 attempts were probe-scored, and both had n=2.
- At n=2 the best McNemar p is 0.25. The best e-value below is
  1.75² = 3.0625, against a slot-1 bar of 40.
- So no candidate measured on today's probe sets can be certified. The
  certificate becomes reachable at 7 or more gate probes, which is what
  `rsi-t1-probe-harvest` builds.

**This kickoff adds two things.**
1. **A test-by-betting e-value over the paired gate-probe outcomes.**
   - `E = (1+λ)^gains · (1−λ)^losses`, with the bet λ = 0.75 by default.
   - **Why it is valid.** Under H0, each probe's `E[after − before] ≤ 0` given
     the past, and each factor `1 + λ·(after − before)` has expectation at most
     1. So E is a test supermartingale. By Ville's inequality,
     `P(sup E ≥ 1/α) ≤ α` under any stopping rule. The probe loop's budget and
     timeout truncation are such stopping rules.
   - Best-of-N control (`control_n > 1`) only makes `before` larger, which
     makes the test more conservative.
2. **A per-task-class alpha ledger.** It spends the total error budget α across
   the stream of certified tests:
   - the k-th test that has at least one discordant pair gets
     `α_k = α · 1/(k(k+1))`;
   - since `Σ 1/(k(k+1)) = 1`, a union bound gives
     `P(any false promotion in the class) ≤ α` over the class's whole history;
   - the bar for test k is `1/α_k = k(k+1)/α`.

   With α = 0.05 and λ = 0.75, the minimum number of gains (with zero losses)
   that certifies is:

   | Slot k | 1 | 2 | 3 | 4 | 5 |
   |---|---|---|---|---|---|
   | Bar 1/α_k | 40 | 120 | 240 | 400 | 600 |
   | Minimum gains | 7 | 9 | 10 | 11 | 12 |

   This is the "tenth faces a stricter bar than the first" property.

**Default off.**
- Everything sits behind `MO_APPLY_CERTIFICATE=1`.
- With the flag unset, `evaluate_gate` returns the same JSON string as today,
  and no table is touched.
- With the flag on, a would-be promote with no certificate, or whose
  certificate is unavailable or misconfigured, becomes `quarantined`. That
  fails closed.
- The certificate never turns a quarantine or rejection into a promote.

**Out of scope, and why.**
- **`mini_ork/gates/promotion_gate.py:promotion_evaluate`.** This is the
  improve, eval, promote path (chokepoint ②). There, `utility_delta = 0` and
  nothing reaches a promote, and McNemar is not used at all. Route it through
  apply, or certify it in a follow-up, once it produces a real candidate.
- **Pre-registration of the acceptance predicate.** That is the separate Tier-2
  item "Pre-registered falsifiable gates".
- **The anchor-health halt.** It already shipped, as the `collapse_history`
  writer (I4b) plus `mini_ork/recovery/circuit_breaker.py`.
- **Accumulating e-values across sweeps.** This needs the edit memory
  (`_previously_failed`) to stop blocking re-proposal. It is a follow-up.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `db/migrations/0086_promotion_certificates.sql` (new)

Use the next free number if 0086 is taken when you implement. The migration
is additive only:

```
CREATE TABLE IF NOT EXISTS promotion_certificates (
  id            INTEGER PRIMARY KEY,
  task_class    TEXT    NOT NULL,
  slot          INTEGER NOT NULL CHECK (slot >= 1),
  candidate_id  TEXT    NOT NULL,
  gains         INTEGER NOT NULL CHECK (gains >= 0),
  losses        INTEGER NOT NULL CHECK (losses >= 0),
  n             INTEGER NOT NULL CHECK (n >= 0),
  bet           REAL    NOT NULL,
  e_value       REAL    NOT NULL,
  ledger        TEXT    NOT NULL CHECK (ledger IN ('spend','none')),
  alpha_total   REAL    NOT NULL,
  alpha_k       REAL    NOT NULL,
  threshold     REAL    NOT NULL,
  certified     INTEGER NOT NULL CHECK (certified IN (0,1)),
  created_at    INTEGER NOT NULL,
  UNIQUE (task_class, slot)
);
CREATE INDEX IF NOT EXISTS idx_promotion_certificates_class ON promotion_certificates(task_class, slot);

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0086_promotion_certificates.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'promotion-certificates-v1');
```

### 2. `mini_ork/learning/promotion_certificate.py` (new)

The module is pure math plus one table, with no env reads outside
`resolve_config`.

- **Constants.**
  - `FLAG = "MO_APPLY_CERTIFICATE"`
  - `ALPHA_ENV = "MO_APPLY_CERT_ALPHA"`
  - `BET_ENV = "MO_APPLY_CERT_BET"`
  - `LEDGER_ENV = "MO_APPLY_CERT_LEDGER"`
  - `DEFAULT_ALPHA = 0.05`
  - `DEFAULT_BET = 0.75`
  - `LEDGER_MODES = ("spend", "none")`
- **`DDL`.** A string holding the `CREATE TABLE` and `CREATE INDEX` statements
  from section 1, byte-for-byte. It does not include the `schema_migrations`
  insert.
- **`ensure_table(con: sqlite3.Connection) -> None`.** It runs `DDL`. That makes
  it work on a DB that predates the migration.
- **`normalize_task_class(task_class: str) -> str`.** It returns
  `task_class.strip().replace("-", "_")`. An empty result raises `ValueError`.
- **`paired_counts(before: list, after: list) -> tuple[int, int]`.**
  - It returns `(gains, losses)`, where `gains` is the count of `i` with
    `after[i]` truthy and `before[i]` falsy, and `losses` is the reverse.
  - Unequal lengths raise `ValueError`.
- **`betting_e_value(gains: int, losses: int, bet: float) -> float`.**
  - It returns `(1.0 + bet) ** gains * (1.0 - bet) ** losses`.
  - It raises `ValueError` when `gains < 0`, when `losses < 0`, or unless
    `0.0 < bet < 1.0`.
- **`spend_fraction(k: int) -> float`.** It returns `1.0 / (k * (k + 1))`, and
  raises `ValueError` when `k < 1`.
- **`slot_alpha(alpha_total: float, k: int, ledger: str) -> float`.**
  - `"spend"` gives `alpha_total * spend_fraction(k)`.
  - `"none"` gives `alpha_total`; this is PACE's per-decision guarantee only.
  - Any other ledger value raises `ValueError`.
- **`resolve_config(environ: dict | None = None) -> dict`.**
  - It reads `os.environ` when `environ` is `None`.
  - It returns `{"enabled": bool, "alpha": float, "bet": float, "ledger": str, "errors": list[str]}`.
  - `enabled` is `environ.get(FLAG) == "1"`.
  - An unset or empty value takes its default.
  - An invalid value takes its default AND appends one error string that starts
    with the env var's name, for example
    `"MO_APPLY_CERT_ALPHA='2' must be a number in (0, 1)"`. The rules:
    - alpha must be a float with `0 < x < 1`;
    - bet must be a float with `0 < x < 1`;
    - ledger must be in `LEDGER_MODES`.
- **`certify(con, *, task_class: str, candidate_id: str, before: list, after: list, alpha_total: float, bet: float, ledger: str, now: int) -> dict`.**
  1. Let `tc = normalize_task_class(task_class)`, and compute `gains`, `losses`
     and `e = betting_e_value(gains, losses, bet)`.
  2. The `base` dict is
     `{"task_class": tc, "candidate_id": candidate_id, "gains": gains, "losses": losses, "n": len(before), "bet": bet, "ledger": ledger, "alpha_total": alpha_total, "e_value": e}`.
  3. When `gains + losses == 0`, return
     `{**base, "consumed": False, "slot": None, "alpha_k": None, "threshold": None, "certified": False}`
     and write NO row.
     - This is sound: E is exactly 1 in that case, so leaving these tests out
       of the ledger keeps `E[e · 1{counted}] ≤ P(counted)`.
     - Dropping any test whose `e < 1` would NOT be sound. So every test with
       at least one discordant pair consumes a slot, whatever branch decides it.
  4. Otherwise:
     - call `ensure_table(con)`;
     - inside one `BEGIN IMMEDIATE` transaction:
       - read `slot = COALESCE(MAX(slot), 0) + 1` for `tc`;
       - compute `alpha_k = slot_alpha(alpha_total, slot, ledger)`,
         `threshold = 1.0 / alpha_k` and `certified = e >= threshold`;
       - INSERT the row, with `created_at = now`;
       - COMMIT;
     - roll back and re-raise on any error.
  5. Return
     `{**base, "consumed": True, "slot": slot, "alpha_k": alpha_k, "threshold": threshold, "certified": certified}`.

### 3. `mini_ork/cli/apply.py`

- **The module docstring's env list.** Add three lines:
  - `MO_APPLY_CERTIFICATE=1  anytime-valid certificate + alpha ledger on the measured path (default OFF)`
  - `MO_APPLY_CERT_ALPHA  per-class error budget (default 0.05)`
  - `MO_APPLY_CERT_BET / MO_APPLY_CERT_LEDGER  bet λ (default 0.75) / spend|none (default spend)`
- **`evaluate_gate`'s signature** becomes
  `evaluate_gate(candidate_id, utility_before, utility_after, pertask_json="", control_n=1, *, task_class: str = "", db: str | None = None) -> str`.
  Delete the line `del candidate_id`, because the certificate records it.
- **The certificate overlay.** Insert it after the existing decision tree,
  where `decision` and `rationale` are final, and before `result = {...}` is
  built. Import `from mini_ork.learning import promotion_certificate as _pc`
  locally. Let `cfg = _pc.resolve_config()` and `cert_payload = None`. When
  `cfg["enabled"]`, take the first case that applies.

  The text `<orig>` below stands for the rationale before the overlay. Every
  case that turns a promote into `quarantined` replaces the rationale with the
  string shown.

  1. **The config is misconfigured** (`cfg["errors"]` is non-empty).
     - `cert_payload = {"error": "misconfigured: " + "; ".join(cfg["errors"])}`.
     - When `decision == "promoted"`, the decision becomes `"quarantined"` and
       the rationale becomes
       `f"certificate misconfigured: {'; '.join(cfg['errors'])}; point-estimate gate said: {<orig>}"`.
     - No slot is consumed.
  2. **There are no per-task vectors** (`not (measured and len(before) == len(after))`).
     - `cert_payload = {"error": "no per-task vectors"}`.
     - When `decision == "promoted"`, the decision becomes `"quarantined"` and
       the rationale becomes
       `f"no anytime-valid certificate: no per-task vectors to certify; point-estimate gate said: {<orig>}"`.
  3. **Otherwise, certify.**
     - Open `sqlite3.connect(_db_path(db))` and call
       `cert = _pc.certify(con, task_class=task_class, candidate_id=str(candidate_id), before=before, after=after, alpha_total=cfg["alpha"], bet=cfg["bet"], ledger=cfg["ledger"], now=int(time.time()))`.
       Close the connection in a `finally` block.
     - **On any exception `exc`** (this includes `_db_path` raising when `db` is
       `None` and `MINI_ORK_DB` is unset, and an empty `task_class`):
       - `cert_payload = {"error": f"{type(exc).__name__}: {exc}"}`;
       - when promoted, the decision becomes `"quarantined"` and the rationale
         becomes
         `f"certificate unavailable: {type(exc).__name__}: {exc}; point-estimate gate said: {<orig>}"`.
     - **Otherwise** `cert_payload = cert`, and when `decision == "promoted"`:
       - **Certified:** append
         `f"; certificate e={cert['e_value']:.4f} >= {cert['threshold']:.4f} (slot {cert['slot']})"`
         to the rationale.
       - **Not certified, with `cert["threshold"] is None`:** the decision
         becomes `"quarantined"` and the rationale becomes
         `f"no anytime-valid certificate: no discordant probe pairs (gains=0, losses=0); point-estimate gate said: {<orig>}"`.
       - **Not certified otherwise:** the decision becomes `"quarantined"` and
         the rationale becomes
         `f"no anytime-valid certificate: e={cert['e_value']:.4f} < threshold={cert['threshold']:.4f} (gains={cert['gains']}, losses={cert['losses']}, slot={cert['slot']}, alpha_k={cert['alpha_k']:.6g}, bet={cert['bet']:g}); point-estimate gate said: {<orig>}"`.

  When the flag is on, the overlay also adds `result["cert"] = cert_payload`
  after the existing `sig_*` keys. When the flag is off, the overlay adds
  nothing and opens no DB connection.
- **`apply_run`.** Its only change is to the single `evaluate_gate(...)` call,
  which gains `task_class=task_class, db=db`.
- **Nothing else changes.** That includes the McNemar branch (which runs first
  and can already quarantine), the per-task no-regression rule, control_n, the
  harness-cost gate, `FABRICATING_SCORERS` and `record_promotion`.

## Files in scope (touch ONLY these)

- `db/migrations/0086_promotion_certificates.sql` (new)
- `mini_ork/learning/promotion_certificate.py` (new)
- `mini_ork/cli/apply.py`
- `tests/unit/test_promotion_certificate.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_promotion_certificate.py`: closed-form math, temp sqlite, no network, no probe runs)

**Setup.**
- An autouse fixture deletes `MO_APPLY_CERTIFICATE`, `MO_APPLY_CERT_ALPHA`,
  `MO_APPLY_CERT_BET`, `MO_APPLY_CERT_LEDGER`, `MO_APPLY_SIG_ALPHA`,
  `MINI_ORK_DB` and `MO_APPLY_REGRESSION_TOLERANCE` with `monkeypatch.delenv`.
- Use `pytest.approx(x, rel=1e-12)` for floats.
- `V(g, l)` below means the pertask JSON
  `json.dumps({"before": [0] * 12, "after": [1] * g + [0] * (12 - g)})` when
  `l == 0`.

**The math.** Every expected value below is exact.

- **`betting_e_value` table.**

  | gains | losses | bet | Expected |
  |---|---|---|---|
  | 0 | 0 | 0.75 | 1.0 |
  | 7 | 0 | 0.75 | 50.26507568359375 |
  | 6 | 0 | 0.75 | 28.722900390625 |
  | 5 | 0 | 0.75 | 16.4130859375 |
  | 8 | 0 | 0.75 | 87.96388244628906 |
  | 9 | 0 | 0.75 | 153.93679428100586 |
  | 10 | 0 | 0.75 | 269.38938999176025 |
  | 9 | 1 | 0.75 | 38.484198570251465 |
  | 10 | 1 | 0.75 | 67.34734749794006 |
  | 1 | 1 | 0.75 | 0.4375 |
  | 0 | 3 | 0.75 | 0.015625 |
  | 2 | 1 | 0.5 | 1.125 |
  | 3 | 1 | 0.5 | 1.6875 |

  These raise `ValueError`: bet values `0.0`, `1.0`, `-0.1` and `1.5`, and
  `gains=-1`.
- **`spend_fraction`.** It returns 0.5 for 1, `1/6` for 2 and `1/12` for 3.
  `sum(spend_fraction(k) for k in range(1, 100)) == pytest.approx(0.99)`.
  `spend_fraction(0)` raises `ValueError`.
- **`slot_alpha`.**
  - `(0.05, 1, "spend")` gives 0.025, `(0.05, 2, "spend")` gives `0.05/6`, and
    `(0.05, 3, "spend")` gives `0.05/12`.
  - `(0.05, 7, "none")` gives 0.05.
  - Ledger `"fdr"` raises `ValueError`.
  - `1/slot_alpha` is approximately 40, 120 and 240 for k = 1, 2, 3.
- **Ville check by exact enumeration.** It needs no randomness.
  1. For m in (10, 12), enumerate all 2^m sequences of gain or loss.
  2. For each sequence, compute the prefix e-values
     `betting_e_value(g_t, l_t, 0.75)` for t = 1..m.
  3. Count the sequences whose prefix maximum is at least the threshold.

  The counts are:
  - m=10, threshold 20: 28 of 1024;
  - m=10, threshold 40: 8 of 1024;
  - m=12, threshold 20: 112 of 4096;
  - m=12, threshold 40: 46 of 4096.

  Each count over 2^m is at most 1/threshold.
- **`paired_counts`.** `([0, 0, 1, 1], [1, 0, 0, 1])` gives `(1, 1)`. Unequal
  lengths raise `ValueError`.
- **`normalize_task_class`.** `" code-fix "` gives `"code_fix"`; `""` raises
  `ValueError`.
- **`resolve_config`.**
  - `{}` gives
    `{"enabled": False, "alpha": 0.05, "bet": 0.75, "ledger": "spend", "errors": []}`.
  - `{"MO_APPLY_CERTIFICATE": "1"}` gives `enabled` True.
  - `{"MO_APPLY_CERT_ALPHA": "2"}` gives alpha 0.05 and one error starting with
    `"MO_APPLY_CERT_ALPHA"`.
  - The same holds for `MO_APPLY_CERT_BET` set to `"1"`, and for
    `MO_APPLY_CERT_LEDGER` set to `"fdr"`.

**The ledger** (`con = sqlite3.connect(tmp_path / "c.db")`, an empty DB). Every
call uses `alpha_total=0.05`, `bet=0.75`, `ledger="spend"` and `now=1`, unless
the case says otherwise.

| # | Call | Expected |
|---|---|---|
| 1 | gains 7, class `"code_fix"` | slot 1, `alpha_k` 0.025, threshold ≈ 40, `e_value` 50.26507568359375, certified True, consumed True. One row, with `certified=1`. |
| 2 | gains 7 again, same class | slot 2, threshold ≈ 120, certified False |
| 3 | gains 7, class `"framework_edit"` | slot 1, certified True |
| 4 | gains 7, task class `"code-fix"` | slot 3 for `code_fix` |
| 5 | `before == after == [1, 0, 1]` | consumed False, slot None; the row count is unchanged |
| 6 | Class `"x"` with 4 prior rows, `ledger="none"`, gains 6 | slot 5, `alpha_k` 0.05, threshold ≈ 20, certified True |
| 7 | `task_class=""` | raises `ValueError` |
| 8 | Manually insert a second row with an existing `(task_class, slot)` | raises `sqlite3.IntegrityError` |

**The gate** (`p = str(tmp_path / "g.db")`, a path that does not exist yet,
with `db=p` and `task_class="code_fix"`):

| Case | Expected |
|---|---|
| **Flag off is byte-identical.** `ap.evaluate_gate("c1", 0.0, 7/12, V(7,0), task_class="code_fix", db=p)` | Equals `ap.evaluate_gate("c1", 0.0, 7/12, V(7,0))` as a string, and `os.path.exists(p)` is `False`: no connection was opened. |
| **On, under-certified.** `MO_APPLY_CERTIFICATE=1`, `(0.0, 0.5, V(6,0))` | `decision == "quarantined"`. The rationale starts with `"no anytime-valid certificate: e=28.7229 < threshold=40.0000 (gains=6, losses=0, slot=1, alpha_k=0.025, bet=0.75); point-estimate gate said: non-regression cleared"`. `out["cert"]["certified"] is False`. |
| **On, certified.** Fresh DB, `(0.0, 7/12, V(7,0))` | `decision == "promoted"`, and the rationale contains `"; certificate e=50.2651 >= 40.0000 (slot 1)"`. |
| **The ledger tightens.** The same call twice on one DB | First `"promoted"`, then `"quarantined"`. The second rationale contains `"slot=2"` and `"threshold=120.0000"`. |
| **Regression still wins, but consumes a slot.** `pertask = {"before": [1, 0] + [0]*10, "after": [0, 1] + [0]*10}`, `(1/12, 1/12)` | The rationale starts with `"per-task no-regression gate"`. `out["cert"]` has consumed True, gains 1, losses 1 and `e_value` 0.4375, and one row exists. |
| **The scalar path is refused.** `ap.evaluate_gate("c", 0.5, 0.7, "", task_class="code_fix", db=p)` | `"quarantined"`. The rationale starts with `"no anytime-valid certificate: no per-task vectors"`. `out["cert"] == {"error": "no per-task vectors"}`. |
| **Unavailable fails closed.** `db=None`, `MINI_ORK_DB` unset, `V(7,0)` | `"quarantined"`, and the rationale starts with `"certificate unavailable:"`. |
| **An empty `task_class`** | `"quarantined"`, and the rationale starts with `"certificate unavailable:"`. |
| **Misconfigured fails closed.** `MO_APPLY_CERT_ALPHA=2`, `V(7,0)` | `"quarantined"`. The rationale starts with `"certificate misconfigured: MO_APPLY_CERT_ALPHA"`. `os.path.exists(p)` is `False`: no slot was consumed. |
| **It composes with McNemar.** `MO_APPLY_SIG_ALPHA=0.05`, `V(7,0)` | `"promoted"`. The rationale contains `"McNemar p=0.0078"` and `"certificate e=50.2651"`. |

**Through `apply_run`.** Copy these from `tests/unit/test_cli_apply_py.py`
verbatim: `FIXTURE_DDL`, `_APPLY_ENV`, the `envscrub` and `db` fixtures, `_rows`
and `_seed_gradient` (`:822`). Follow
`test_apply_run_probe_vetted_promote_without_unvetted` (`:857`): monkeypatch
`mini_ork.learning.probe_scorer.probe_score` to return a dict with `before` 0.0,
`after` `7/12`, `n` 12, `pertask_json` `V(7,0)` plus `ids`, `runs` `[]` and
`cost_usd` 0.0. Set `MO_APPLY_SCORER=probe`, `MO_APPLY_ENABLED=1` and
`MO_APPLY_CERTIFICATE=1`.

- **A certified candidate promotes.**
  - The summary decision is `"promoted"`.
  - `_rows(db, "promotion_certificates")` has one row, with
    `task_class == "reviewer"`, `candidate_id` equal to the summary's
    `candidate_id`, and `certified == 1`.
- **An under-certified candidate is quarantined.** With `after` `0.5` and
  `V(6,0)`:
  - the decision is `"quarantined"`;
  - the target file is unchanged;
  - the `apply_attempts` rationale contains `"no anytime-valid certificate"`.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_promotion_certificate.py`
- `pytest -q -p no:asyncio tests/unit/test_apply_significance.py tests/unit/test_cli_apply_py.py tests/unit/test_apply_harness_targets.py`
- `pytest -q -p no:asyncio tests/unit/test_collapse_history_writer.py tests/unit/test_eco6_k7_apply_hygiene.py tests/unit/test_probe_scorer_control_arm.py`
- `pytest -q -p no:asyncio tests/unit/test_migrate_py.py tests/unit/test_review_findings.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_promotion_certificate.py --junitxml="${MINI_ORK_RUN_DIR}/certificate-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/certificate-junit.xml`

## Success criteria

- All four verification commands pass, and every pre-existing test in them is
  unchanged. The pre-existing tests that must stay green are:
  - `test_apply_significance.py`
  - `test_cli_apply_py.py`
  - `test_apply_harness_targets.py`
  - `test_collapse_history_writer.py`
  - `test_eco6_k7_apply_hygiene.py`
  - `test_probe_scorer_control_arm.py`
  - `test_migrate_py.py`
  - `test_review_findings.py`, which applies every migration via `init_db`
- With `MO_APPLY_CERTIFICATE` unset, `evaluate_gate` output is byte-identical to
  before, and no `promotion_certificates` row is ever written.
- `ruff check --select F,E9 mini_ork/learning/promotion_certificate.py mini_ork/cli/apply.py tests/unit/test_promotion_certificate.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
