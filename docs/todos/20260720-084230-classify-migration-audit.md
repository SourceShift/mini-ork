# Classify migration — requirements audit gaps

Status: completed

Last worked on: 2026-07-20 09:00 Europe/Berlin

Source task: `docs/migration/remaining-migration-handoff.md`

Run under audit: `run-1784528328-42404`

## Subtasks

1. Make the BDD-first verifier use its isolated project consistently
   - Current status: completed. The test now exports the same explicit engine,
     project-home, and target-repository contract as the code-fix E2E harness;
     the exact command passes 18 assertions.
   - Last time worked on: 2026-07-20 08:47 Europe/Berlin.
   - Remaining parts: none.

## First requirements audit

- Re-read the migration handoff, self-migrate manifest, run integration map,
  feature ledger, recipe prompt, artifact contract, and verifier contract.
- Confirmed the native classify and trace-store ports contain no subprocess,
  Popen, system, or print calls.
- Repointed the mapped runtime, CLI, validation, integration, E2E, security,
  script, sandbox, UI, demo, and comment seams to the Python runtime.
- Converted the live Bash parity unit suite into standalone golden contracts
  after confirming the durable pre-retirement report passed 5 tests.
- Found one verifier-harness gap after the classify assertions passed: the
  BDD-first test initializes from the engine directory instead of its isolated
  project directory.

## Second requirements audit

- Re-read the migration handoff, feature manifest, migrator prompt, artifact
  contract, integration map, 29-row ledger, and all five verifier reports.
- Programmatically matched every integration-map inbound path to a changed file.
- Reconfirmed both `mini_ork_classify.py` and the imported native
  `trace_store.py` contain no subprocess/Popen/system nodes and no `print()`
  calls; classify stdout remains limited to its documented contract.
- Confirmed `bin/mini-ork-classify` is absent, the top-level Bash and Python CLI
  direct-subcommand paths invoke the module, and closure scans find no literal
  retired path or dynamic `_bin(..., "classify")` caller.
- Replayed the standalone 9-test classifier contract, 6-test Python CLI suite,
  9-assertion classify integration test, seven security suites, three E2Es,
  post-MVP integration, focused Pyright, Bash/Python syntax checks, and all five
  migration gates. All pass after the BDD harness correction.

All classify technical and product requirements are satisfied in the isolated
proposal. The reviewed proposal was then applied to the source checkout without
touching the user-owned `.mini-ork/config/agents.yaml`.

## Source promotion audit

- Confirmed `git apply --check` succeeded before promotion and the generated
  proposal has SHA-256
  `cba03d84c3165732ec98a5b7fd055893d2542823a0c7d4adf6a0fd4cb484775f`.
- Replayed 15 classify/CLI unit tests, 9 classify integration assertions, 16
  post-MVP integration assertions, 43 security assertions, and 53 E2E
  assertions; all pass.
- Re-ran classify feature acceptance, focused Pyright, post-retirement parity,
  feature acceptance, ledger shape, fork closure, and diff hygiene; all pass.
- Diagnosed the outer `__gates__` failure as an orchestration-context defect:
  `mini_ork_verify.py` omits keys required by globally registered oracle gates,
  and its aggregate treats `defer` as failure. The self-migrate reports and
  implementation are green, so no paid retry was performed.

## Second completion audit

- Re-read the canonical handoff, classify kickoff, run integration map,
  detailed verdict, reviewer report, and all five verifier reports after source
  promotion.
- Confirmed every migration-owned code and test path in the source checkout is
  byte-identical to the independently verified isolated target, and confirmed
  `bin/mini-ork-classify` remains absent.
- Re-ran the complete focused source proof: 15 classify/CLI unit tests, 25
  integration assertions, 43 security assertions, 53 E2E assertions, classify
  feature acceptance, focused Pyright, durable pre-retirement evidence, all
  four post-retirement gates, and diff hygiene. All pass.
- Reconfirmed that the global oracle-gate context defect is outside the
  classify fork: no classify verifier or reviewer failed, and no paid retry is
  justified.

All classify technical and product requirements are satisfied in the source
checkout. The next migration fork is `plan`; its paid run still requires
separate explicit approval.
