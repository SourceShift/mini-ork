# UI parity baseline

This suite records behavioral landmarks for the 10 TanStack Router routes before the OpenHands `ui/` fork. It deliberately does not compare pixels: each passing route writes a diagnostic PNG to `tests/ui/snapshots/`, while assertions cover accessible behavior only.

## Load-bearing behavior

These contracts must survive the fork:

- The application shell exposes a visible main region and navigation links.
- `/` exposes the Fleet heading and Fleet navigation entry.
- `/new` exposes the New Run heading and Catalog link.
- `/recipes` exposes the Capabilities heading and New Run link.
- `/trajectory` exposes the Trajectory heading and hypothesis-root selector.
- `/fingerprint` remains reachable through the Fingerprint navigation entry.
- `/terminal` exposes the Live shell heading and attach control.
- Persisted run, agent, input, and self-improve records expose their route heading, parent navigation, and primary tab/control.
- Tests only read existing records. They never create, alter, or terminate runs.

## Skipped when empty

The run detail, agent detail, run input, and self-improve detail routes need persisted records. When the API has no suitable record, the corresponding test adds a `skipped-when-empty` annotation, captures the source list route under the deep-route snapshot name, and makes no deep-route landmark assertion. This keeps the 10-route inventory explicit without treating a fabricated identifier or error page as parity evidence.

## Cosmetic behavior

These details are expected to change in SE-3 and later work and are not asserted:

- Colors, typography, spacing, borders, shadows, icon choices, and animations.
- Card, table, chart, graph, terminal, and sidebar geometry.
- Exact copy outside route-identifying headings and accessible control names.
- Row counts, metrics, costs, statuses, timestamps, and other live data.
- Screenshot pixels. PNGs are review artifacts, not golden-image tests.

## Baseline mode vs live mode

Baseline mode is the checked-in behavioral specification. Live mode regenerates the PNG evidence against a running local UI and API.

One-time browser setup:

```sh
cd ui
npx playwright install chromium
```

Start the application in another terminal with `make web-up`, then run:

```sh
make web-snapshot
```

The suite expects `http://127.0.0.1:7070`. A missing server or API is a hard failure; zero-test success is prevented by the explicit `parity.spec.ts` selection and 10 declared tests. No arbitrary sleeps are used: navigation waits for the network to settle, then captures immediately. Terminal and live panels may still contain time-varying content, which is acceptable because PNGs are diagnostic only.
