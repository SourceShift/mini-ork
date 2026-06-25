# Kickoff — runbook: BullMQ queue backlog (the host application dev_book-generation)

## Incident class

`bullmq-book-gen-queue-backlog` — the BullMQ `dev_book-generation` queue
on the host application's Redis instance accumulates > 100 waiting jobs and chapter
throughput drops to < 1 chapter/minute, blocking user-visible book
generation.

## Affected services

- the host application BE pods (consume the queue)
- Redis at `<prod-host>:6380` (queue substrate)
- Hatchet (alternative dispatcher for the same workflow)
- Downstream Daytona sandboxes (per-chapter agent execution)

## Blast radius

When this happens:
- All in-progress book generations stall.
- New `POST /confirm` requests succeed but jobs never start processing.
- User sees "Generating chapter 1 of N" indefinitely.
- Cost: chapters that DO eventually run with hours of stale context produce
  off-topic content.

## Expected severity

P1 — user-visible feature degradation, no data loss, recovers within
30 min of correct mitigation.

## Audience

Mixed — incident may be picked up by on-call who hasn't touched book-gen
code; assume basic kubectl + redis-cli familiarity; spell out specific
queue commands.

## Runtime environment

- platform: k3s on <prod-host> (<prod-host>)
- log: Loki at `http://<prod-host>:13101`
- metrics: Prometheus via Grafana at `http://<prod-host>:13000`
- tracing: Tempo at `http://<prod-host>:3200`
- Redis: `<prod-host>:6380` password `<redis-password>` queue prefix `dev_`

## External dependencies

- Anthropic API (for chapter generation calls)
- Daytona (sandbox provisioning)
- Hatchet (workflow dispatch)

## Scope boundaries

- WILL NOT cover: cold-start of fresh Redis instance (assume Redis is up).
- WILL NOT cover: Anthropic outage (separate runbook).
- WILL NOT cover: Daytona quota exhaustion (separate runbook).

## Why this runbook now

Observed pattern: queue backlogs over the past month have taken 45-90
minutes from detection to recovery because the on-call follows ad-hoc
diagnosis. Heterogeneous-family lens panel can write a tight runbook
covering detection / containment / diagnosis / recovery / prevention in
one pass.
