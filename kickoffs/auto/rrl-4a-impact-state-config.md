# RRL-4a: impact state (sealed secret, epochs, looks, canary) + `research:` / `impact:` config

## Goal

The research-driven RSI loop (`docs/plans/2026-10-29-research-driven-rsi-loop.md`)
adds gate G12, which tests a candidate on a **sealed** benchmark split. Two pieces of
plumbing must exist before the gate can be wired (RRL-4b):

1. `mini_ork/rsi/impact_state.py` — the persistent state of the sealed acceptor: the
   secret that keys the dev/sealed split, the epoch, how many sealed looks were spent
   (each look spends error budget), and main's canary score history.
2. Two new sections in the RSI cycle config (`mini_ork/rsi/config.py`): `research:`
   and `impact:`, parsed with the same fail-closed discipline as every other section.

Nothing calls the new module yet. Existing behaviour must not change: both sections are
optional and default to values that leave today's cycle untouched
(`research.enabled: false`).

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/rsi/impact_state.py` (new)

Standard library plus a lazy import of `mini_ork.dispatch.vault.vault_environment`
(only in the secure-tier branch). No network, no model calls.

- `class ImpactStateError(RuntimeError)`.
- `STATE_VERSION = 1`; `DEFAULT_SECRET_NAME = "MO_RSI_IMPACT_SECRET"`.
- `impact_dir(home) -> Path` → `Path(home) / "rsi" / "impact"`.

**`load_secret(home, *, secure_store: str = "", secret_name: str = DEFAULT_SECRET_NAME, environ=None) -> bytes`**

- **Secure tier** (`secure_store.strip()` non-empty): `value =
  vault_environment(environ).get(secret_name)`. A `None`/empty/whitespace value, or
  any exception from the vault, raises `ImpactStateError(f"secure tier: secret
  {secret_name} is not available from {secure_store}")` — never a fallback to the
  file. Return `value.strip().encode()`. The message must not contain the value.
- **Simple tier:** `path = impact_dir(home) / "secret"`.
  - Missing: create `impact_dir` (`mkdir(parents=True, exist_ok=True)`, then
    `os.chmod(dir, 0o700)`), write `secrets.token_hex(32)` through
    `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)`. If `O_EXCL` fails
    because another process created it first, fall through to reading.
  - Existing: `path.stat().st_mode & 0o077` non-zero →
    `ImpactStateError(f"{path}: permissions too open (expected 0600)")`. Content
    stripped empty → `ImpactStateError(f"{path}: empty secret")`.
  - Return the stripped content as bytes.

**`corpus_hash(items: Mapping[str, str]) -> str`** — first 16 hex chars of
`sha256(json.dumps(sorted(items.items()), separators=(",", ":")).encode())`.

**State file** — `state_path(home) = impact_dir(home) / "state.json"`.

- `new_state() -> dict` → `{"version": STATE_VERSION, "benchmarks": {}}`.
- `load_state(home) -> dict`: missing file → `new_state()`. Unparseable JSON, a
  non-dict, a `version` other than `STATE_VERSION`, or a non-dict `benchmarks` →
  `ImpactStateError` naming the path (fail closed — a corrupt look counter must never
  reset the error budget).
- `save_state(home, state) -> Path`: create the dir; write
  `json.dumps(state, indent=2, sort_keys=True) + "\n"` to a temp file in the same dir
  opened with mode `0o600`, then `os.replace` onto `state_path`. Return the path.

**Per-benchmark entry** — a dict with keys `epoch` (int), `corpus` (str), `looks`
(int), `accepted` (int), `rejected` (int), `canary` (list).

- `bench_state(state, bench: str, corpus: str) -> dict`:
  - absent → create `{"epoch": 1, "corpus": corpus, "looks": 0, "accepted": 0,
    "rejected": 0, "canary": []}` under `state["benchmarks"][bench]`;
  - present with a different `corpus` → `epoch += 1`, `corpus = corpus`, and
    `looks`, `accepted`, `rejected` reset to 0 (`canary` is kept);
  - return the (mutated, stored) entry.
- `rotate(state, bench) -> dict`: absent → `ImpactStateError(f"no impact state for
  {bench}")`; else `epoch += 1` and reset `looks`, `accepted`, `rejected` to 0.
- `record_look(state, bench, *, accepted: bool) -> int`: absent → `ImpactStateError`;
  `looks += 1`, then `accepted += 1` or `rejected += 1`; return the new `looks`.
- `record_canary(state, bench, *, sha: str, score: float, at: int, keep: int = 200) -> None`:
  absent → `ImpactStateError`. When the last canary entry has the same `sha`, replace
  it; otherwise append `{"sha": sha, "score": float(score), "at": int(at)}`. Keep only
  the last `keep` entries.
- `canary_scores(state, bench) -> list[float]`: the `score` values in order (`[]` when
  the benchmark is absent).

`__all__` lists every public name above, sorted.

### 2. `mini_ork/rsi/config.py`

**`ResearchConfig`** (`@dataclass(frozen=True, slots=True)`) — fields and defaults:
`enabled: bool = False`, `domains: tuple[str, ...] = ("verification",)`,
`verdicts: tuple[str, ...] = ("implement-now", "next")`,
`skip_disagreement: bool = True`, `skip_single_rater: bool = True`,
`max_candidates: int = 200`, `refresh_days: int = 30`,
`review_globs: Mapping[str, str] = field(default_factory=dict)`.
`from_mapping(raw)` classmethod:
- `_only("research", raw, {"enabled", "domains", "verdicts", "skip_disagreement",
  "skip_single_rater", "max_candidates", "refresh_days", "review_globs"})`;
- flags through `_flag`, positives through `_positive`, tuples through `_str_tuple`;
- every domain must be in `mini_ork.rsi.candidates.DOMAINS`, else
  `ConfigError(f"research.domains: unknown domain {d!r}")`;
- `domains` and `verdicts` must be non-empty, else `ConfigError("research.domains: …")`
  / `ConfigError("research.verdicts: …")`;
- `review_globs` must be a mapping of str → non-empty str whose keys are in `domains`,
  else `ConfigError("research.review_globs: …")`.

**`ImpactConfig`** — fields and defaults:
`alpha: float = 0.05`, `spending: str = "horizon_free"`, `lam: float = 1.0`,
`max_regression_rate: float = 0.0`, `dev_fraction: float = 0.5`,
`base_repeats: int = 2`, `max_looks: int = 30`, `workers: int = 4`,
`timeout_s: int = 900`,
`guard_tolerances: Mapping[str, float] = field(default_factory=lambda: {"false_reject": 0.0, "no_verdict": 0.0})`,
`secret_name: str = "MO_RSI_IMPACT_SECRET"`.
`from_mapping(raw)` classmethod:
- `_only("impact", raw, {<the eleven field names>})`;
- `0 < alpha < 1` else `ConfigError("impact.alpha: …")`;
- `spending in ("horizon_free", "per_decision")` else `ConfigError("impact.spending: …")`;
- `lam > 0` else `ConfigError("impact.lam: …")`;
- `0 <= max_regression_rate <= 1` else `ConfigError("impact.max_regression_rate: …")`;
- `0 < dev_fraction < 1` else `ConfigError("impact.dev_fraction: …")`;
- `base_repeats`, `max_looks`, `workers`, `timeout_s` through `_positive`;
- `guard_tolerances`: a mapping of str → number ≥ 0 (bool refused), else
  `ConfigError("impact.guard_tolerances: …")`; values stored as `float`;
- `secret_name`: non-empty str, else `ConfigError("impact.secret_name: …")`.
- A float field given as an `int` (e.g. `alpha: 1` is still refused by the range
  check, but `lam: 2`) is accepted and stored as `float`; a `bool` is refused.

**Wiring:**
- `_TOP_KEYS` gains `"research"` and `"impact"`.
- `CycleConfig` gains `research: ResearchConfig = field(default_factory=ResearchConfig)`
  and `impact: ImpactConfig = field(default_factory=ImpactConfig)` (after `secure`).
- `from_mapping` passes `research=ResearchConfig.from_mapping(_section(raw, "research"))`
  and `impact=ImpactConfig.from_mapping(_section(raw, "impact"))`.
- `CycleConfig.to_dict()` gains
  `"research": {"enabled": …, "domains": list(…)}` and
  `"impact": {"alpha": …, "spending": …, "max_looks": …}`.

### 3. `config/rsi-cycle.example.yaml`

Append two commented sections at the end of the file (after `secure:`), with every
key at its default value and a one-line comment each:

```yaml
# Research-driven adoption (docs/plans/2026-10-29-research-driven-rsi-loop.md).
# Off by default: when on, the harvest reads the newest technique review per domain
# and queues its ranked techniques as `technique_unadopted` candidates.
research:
  enabled: false
  domains: [verification]
  verdicts: [implement-now, next]
  skip_disagreement: true
  skip_single_rater: true
  max_candidates: 200
  refresh_days: 30
  review_globs: {}

# G12 — the sealed, paired impact acceptor for `bench:` candidates.
impact:
  alpha: 0.05                 # per-epoch false-accept budget
  spending: horizon_free      # | per_decision
  lam: 1.0                    # 1.0 = exact McNemar
  max_regression_rate: 0.0    # 0 = no previously-correct item may break
  dev_fraction: 0.5
  base_repeats: 2             # base re-runs; items that flip are excluded as flaky
  max_looks: 30               # sealed looks per epoch before `sealed_budget_exhausted`
  workers: 4
  timeout_s: 900
  guard_tolerances: {false_reject: 0, no_verdict: 0}
  secret_name: MO_RSI_IMPACT_SECRET   # secure tier: read from the vault
```

## Files in scope (touch ONLY these)

- `mini_ork/rsi/impact_state.py` (new)
- `mini_ork/rsi/config.py`
- `config/rsi-cycle.example.yaml`
- `tests/unit/test_rsi_impact_state_py.py` (new)
- `tests/unit/test_rsi_cycle_config_py.py` — add `"research", "impact"` to the
  `test_an_unknown_key_in_every_section_is_refused` parametrize list, and append new
  tests; change nothing else in it.

Do NOT modify any other file (not `config/rsi-cycle.yaml`, which is an operator's
untracked local copy).

## Tests

**`tests/unit/test_rsi_impact_state_py.py`** (hermetic, `tmp_path`):

- **load_secret (simple tier):** first call creates `rsi/impact/secret` with mode
  `0o600` and 64 hex chars; a second call returns the same bytes; `chmod 0o644` →
  `ImpactStateError` matching `permissions too open`; an empty file (0600) →
  `ImpactStateError` matching `empty secret`.
- **load_secret (secure tier):** monkeypatch `mini_ork.dispatch.vault.vault_environment`
  to return `{"MO_RSI_IMPACT_SECRET": " s3cret "}` → `b"s3cret"`, and no file is
  created; returning `{}` → `ImpactStateError` whose message names the store and does
  not contain `s3cret`; a vault that raises → `ImpactStateError`.
- **corpus_hash:** order-independent for the same mapping; changes when a stratum
  changes; 16 chars.
- **state round trip:** `load_state` on a fresh home equals `new_state()`;
  `save_state` then `load_state` round-trips; the file mode is `0o600`; garbage JSON,
  `{"version": 2, "benchmarks": {}}` and `{"version": 1, "benchmarks": []}` each raise.
- **bench_state:** first call → epoch 1; same corpus → unchanged; after two
  `record_look` calls a new corpus → epoch 2, looks 0, canary kept.
- **rotate / record_look:** `record_look(..., accepted=True)` returns 1 and bumps
  `accepted`; `rotate` bumps the epoch and zeroes the counters; both raise on an
  unknown benchmark.
- **record_canary:** same sha replaces; a new sha appends; `keep=3` trims to 3;
  `canary_scores` returns the scores; unknown benchmark → `[]` for `canary_scores`.

**Additions to `tests/unit/test_rsi_cycle_config_py.py`:**

- defaults: `cfg().research.enabled is False`, `cfg().impact.alpha == 0.05`,
  `cfg().impact.guard_tolerances == {"false_reject": 0.0, "no_verdict": 0.0}`;
- each `impact` range error raises `ConfigError` with the field name
  (`alpha: 0`, `alpha: 1`, `spending: "x"`, `lam: 0`, `max_regression_rate: 1.5`,
  `dev_fraction: 1`, `max_looks: 0`, `guard_tolerances: {"x": -1}`,
  `guard_tolerances: {"x": True}`, `secret_name: ""`);
- `research.domains: ["nope"]` → `research.domains`; `research.domains: []` →
  `research.domains`; `review_globs: {"routing": "x"}` with default domains →
  `research.review_globs`;
- `lam: 2` is stored as `2.0`;
- the shipped example still parses strict, and its `research.enabled is False`.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_impact_state_py.py tests/unit/test_rsi_cycle_config_py.py`
- `pytest -q -p no:asyncio tests/unit/test_rsi_cycle_py.py tests/unit/test_rsi_cycle_cli_py.py`
- `ruff check --select F,E9 mini_ork/rsi/impact_state.py mini_ork/rsi/config.py tests/unit/test_rsi_impact_state_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_impact_state_py.py tests/unit/test_rsi_cycle_config_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-4a-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-4a-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged (apart from
  the one parametrize list named above).
- No secret value is ever printed, logged or placed in an exception message.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
