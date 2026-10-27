# Third-party components

mini-ork depends on open-source work. We use it as an engine, keep it upstream, and
contribute fixes back rather than forking. This file records what we use and why.

---

## verifiers (Prime Intellect) — MIT

- **Upstream:** https://github.com/PrimeIntellect-ai/verifiers
- **License:** MIT
- **Version:** `>=0.2.0,<0.5` — the `verifiers.v1` namespace, which upstream ships as a
  *preview*. We pin deliberately and bump on purpose. The floor is the first release with
  the `v1.runtimes` shape Crucible calls; the ceiling was raised from `<0.3` to `<0.5` by
  dependabot (PR #230, 2026-10-27). Before accepting that, `mini_ork/runtime/engine.py`'s
  entire surface — `make_runtime`, the four `*Config` classes, and the Runtime protocol
  methods — was checked against **both ends**, `0.2.0` and `0.4.0`, and is unchanged. That
  check is now a test (`tests/unit/test_crucible_runtime.py::
  test_the_engine_api_surface_survives_the_pinned_range`) and a CI job (`verifiers-pin`),
  so the next automated bump is verified rather than merely merged.
- **Used by:** `mini_ork/runtime/` (Crucible — our verified-execution seam). This is the
  **only** import site; nothing else in mini-ork may import `verifiers`, so an upstream API
  change touches exactly one file.
- **What we use it for:** the `verifiers.v1.runtimes` **Runtime protocol** — one interface
  (`start`/`run`/`read`/`stop`/`teardown`) over four interchangeable backends: `docker` and
  `subprocess` locally, `prime` and `modal` in the cloud. That is genuinely better than
  anything we would hand-roll, and it makes cloud execution a config field rather than a
  rewrite. (Their v1 message-DAG trace and interception server are on our roadmap for the
  same reason; not wired yet.)
- **Where we deliberately do not use it: node providers.** `mini_ork/remote/providers/`
  (static, daytona, e2b, modal) is a different layer and does not route through
  `v1.runtimes`, even where the vendor is the same. A node provider answers "where does
  this run's machine come from", then boots a node-agent inside it; the protocol that
  matters — sync, run-dir mirror, reattach, secret scoping — is ours. It also needs verbs
  `Runtime` does not offer at any version through 0.4.0: `attach` by the cloud's handle
  (a later process must find the same node), and `reap(max_age_s)` — a TTL sweep over
  every labelled sandbox the account owns. `Runtime` has `start`/`stop`/`teardown` for a
  runtime you already hold, and no list-by-label API. Routing providers through it would
  trade two thin vendor adapters for losing the reaper. See
  `docs/architecture/cloud-sandbox-providers.md`, "Why this is not `verifiers.v1.runtimes`".
- **Why we depend rather than fork:** upstream ships daily and v1 is explicitly a preview.
  A fork would rot within a month. We depend, pin, and wrap behind our own seam — and when
  `verifiers` is absent, Crucible drives the `docker` CLI directly and behaves identically,
  so mini-ork stays zero-dependency by default.
- **Where we diverge — and where we do not.** It would be wrong to say `verifiers` "scores
  with an LLM judge." In v1 a task's `@vf.reward` is arbitrary Python, and their SWE
  tasksets score on test execution exactly as we do. They also defend against reward hacks,
  but at a **different boundary**. Their interception server sits between the harness and
  the model API, so it can cut egress or rewrite a tool or search result that would let a
  rollout cheat (`docs/v1/architecture.md`: "Intercepting and rewriting tool responses or
  server-side web search results to block reward hacks"). That is a *tool-boundary*
  defence, and it is real. mini-ork's is an *oracle-boundary* defence: a reward function
  returning `1.0` because the test went green still says nothing about whether the
  **passing** patch is **actually correct** — a patch can special-case the test, and a
  tool-boundary proxy cannot see that (~73.6% shortcut rate under compute pressure). The
  two are complementary, not competing. mini-ork adds two layers on top of theirs:
    - **Crucible** (`mini_ork/runtime/`) — execution-anchored outcome. Its `failed` vs
      `error` split is ours: an *error* is a broken environment, not a failed patch, and
      blaming the patch for it is a false-reject (PR #170). A judge may **veto**, never
      **approve** (PR #168, `reward_from_status`).
    - **Assay** — the solve-time oracle (PoC+ extraction, metamorphic amplification, delta
      gating, explicit abstention). This has no counterpart upstream and is the component we
      claim as differentiating.

  See `docs/architecture/verification-stack.md` for where each of these runs, and
  `mini_ork/certify/` for Assay's implementation.

```
MIT License

Copyright (c) Prime Intellect

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## GEPA — reflective prompt evolution

- **Upstream:** https://github.com/gepa-ai/gepa (paper: arXiv 2507.19457)
- **Used by:** `mini_ork/gepa/` (`MiniOrkGEPAAdapter`)
- **Where we diverge:** upstream GEPA integrations (including the one shipped inside
  `verifiers`) optimize a **single `system_prompt`** against a rubric score. mini-ork
  evolves a **multi-component candidate** — `{planner, implementer, reviewer}` — for an
  orchestrated delivery loop, scored on **real downstream execution outcomes**. Different
  optimization target, different fitness signal.
