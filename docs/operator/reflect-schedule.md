# Scheduled reflect

`mini-ork reflect` normally runs **post-run** (after every run) and as an
in-workflow node. Nothing runs it on a schedule, so two kinds of traces are
never reflected:

- a post-run pass that was killed (crash, SIGKILL, deadline), and
- traces skipped while the cost circuit was open.

The scheduled pass is the catch-up cadence that revisits them. It is the same
shape as `mini-ork supervise`: one config file, one lease, and two delivery
tiers — a `--once`-style simple tier and an `--interval` service tier.

## Both tiers

**Simple tier** — no server, no daemon, no cloud account. One line in cron or
a launchd job against the local SQLite home:

```cron
# every hour at :17
17 * * * * cd /path/to/project && mini-ork reflect --scheduled
```

launchd (`StartInterval` is seconds; see `config/reflect-schedule.example.yaml`
for a full plist fragment):

```xml
<key>ProgramArguments</key>
<array>
  <string>/usr/local/bin/mini-ork</string>
  <string>reflect</string>
  <string>--scheduled</string>
</array>
<key>StartInterval</key>
<integer>3600</integer>
```

**Service tier** — a long-running process against the shared DB (a fleet of
nodes, one owner):

```bash
mini-ork reflect --scheduled --interval
```

It runs one pass, sleeps `interval_s`, and repeats. Ctrl-C exits 0.

Config is optional. With no config named, `--scheduled` reads
`<home>/config/reflect-schedule.yaml`; if that file is absent the built-in
defaults apply, and `enabled: false` makes the command a no-op. A `--config
PATH` that is missing or unparseable is a hard error (exit 2) — never a silent
fallback to a paid schedule.

## Turning it on

```bash
cp config/reflect-schedule.example.yaml <home>/config/reflect-schedule.yaml
# edit <home>/config/reflect-schedule.yaml and set:
#   reflect_schedule:
#     enabled: true
```

`<home>` is `MINI_ORK_HOME` (default `./.mini-ork`). Then either install the
cron/launchd line or run the service tier. Off by default because every pass
spends LLM budget; the cost still lands under the existing
`MO_DAILY_BUDGET_USD` cap — the schedule adds no budget of its own.

## How a pass works

1. **Disabled** → prints `reflect schedule: disabled`, returns 0. No lease, no
   spend.
2. **Lease.** The pass holds the `__reflect_schedule__` lease
   (`run_leases`, the same table `mini-ork supervise` uses). A live lease held
   elsewhere prints `reflect schedule: another scheduled pass holds the lease`
   and returns 0, so a cron overlap is a no-op rather than a double spend. The
   lease TTL is `max(900, deadline_s + 60)`, so it always outlives one
   full-length pass.
3. **Window.** `since = max(last_ok_until, now - max_window_s)` — with no state
   file, `since = now - max_window_s` (7 days by default). The upper bound is
   `until = now - settle_s` (30 minutes by default). When the window is
   inverted (`until < since`) the pass prints
   `reflect schedule: nothing settled yet` and returns 0; a zero-width window
   (`until == since`) is still attempted and selects at most the traces on the
   boundary.
4. **Reflect.** One in-process `mini_ork.cli.reflect.main(["--since", ...])`
   pass with `MO_REFLECT_DEADLINE_S=<deadline_s>` and
   `MO_REFLECTION_UNTIL_TS=<until>` set for the duration. The until bound adds
   `created_at <= until` to the trace selection, so the pass stays off traces
   a fresh run's own post-run pass may still be reflecting.
5. **State.** Exit 0 → `<home>/state/reflect-schedule.json` is atomically
   replaced with `{"last_ok_until": until, "at": now}`. Any non-zero exit
   (including the deadline's 124) leaves the old state, so the window is
   retried next tick. The lease is released in `finally` either way.

## The state file

`<home>/state/reflect-schedule.json` is the only persistent state:

```json
{"last_ok_until": 1770000000, "at": 1770001800}
```

- `last_ok_until` — the exclusive lower bound of the next window; the upper
  bound of the last window that exited 0.
- `at` — wall-clock time of that pass, for operators (`cat` the file to see
  when the schedule last made progress).

A missing or corrupt file reads as "no state" — the pass falls back to the
`max_window_s` look-back. It is safe to delete to force a full-window catch-up
(the gradient watermark still prevents re-paying for already-reflected traces).

## Known limits

- **The lease serialises scheduled passes only.** A run's own post-run pass
  runs outside `__reflect_schedule__`, and `gradient_extractor.has_watermark`
  is a non-atomic check-then-call. The settle window removes the common race
  (fresh traces), but a post-run pass for an old or revived run can still
  overlap a scheduled pass and pay twice for the same trace. Set `settle_s` at
  or above the post-run reflect bound for long runs.
- **A cost-circuit abort still advances the window.** If the cost circuit
  opens mid-pass, `reflect` returns 0 and the state advances past the
  unreflected traces. Batch truncation (`MO_REFLECTION_BATCH`, default 500) has
  the same effect: only the first 500 rows in the window are processed, and the
  state advances to `until`. Both are recorded as follow-ups; a narrow
  `max_window_s` keeps the exposure small.
- **Lane changes need a restart.** The first pass of a service pins
  `MINI_ORK_GRADIENT_MODEL` in the process environment, so editing
  `agents.yaml` takes effect only after the service is restarted.
- **`mini-ork reflect --help` does not list the scheduled flags.** The help
  text is a byte-pinned golden. This page and
  `config/reflect-schedule.example.yaml` are the documentation of record.

## See also

- `config/reflect-schedule.example.yaml` — the annotated config and both tier
  snippets.
- `docs/operator/self-driving.md` — the `supervise` command this is modelled
  on.
