# Agent-S GUI smoke gate: verify human-visible outcomes with a computer-use agent

## Goal

mini-ork's verification stack verifies code artifacts (diffs, tests, relations,
differential suites). When the deliverable is a human-visible state — the
mini-ork IDE (Zed fork), a dashboard, a document — verification today is a
human eyeballing a screenshot. [Agent-S](https://github.com/simular-ai/Agent-S)
(S3, >72.6% on OSWorld) is a computer-use agent: it takes a natural-language
task, reads the screen, and drives real apps by clicking/typing. Wire it in as
an **advisory evidence-producing gate** that verifies observables without ever
seeing the patch — it cannot be talked into passing a bad diff.

## Files in scope

- `mini_ork/gates/agent_s_smoke.py` — NEW. The evaluator, task-spec loader,
  Agent-S subprocess wrapper, consensus logic, evidence capture.
- `tests/unit/test_agent_s_smoke_py.py` — NEW. Unit tests (Agent-S subprocess
  fully mocked; no GUI, no network in tests).

Read-only references (do NOT edit):
- `mini_ork/gates/gate_registry.py` — the gate registry; `register_gate_evaluator`
  (in-process evaluator registration) and the executable gate contract:
  rc 0 = pass, rc 2 = defer, else = fail.
- `mini_ork/gates/native_gates.py` — how a native gate evaluates in-process.
- `config/secrets.local.sh` — where Agent-S provider keys come from (see Env).
- `docs/architecture/verification-stack.md` — gate semantics this must fit.

No other file may change. In particular: NO new CLI subcommand (the exact-set
guard + rubric ritual is out of scope), NO recipe edits, NO scheduler wiring —
the gate is invocable through the existing gate registry only.

## Task spec (input contract)

A run that wants GUI verification writes
`.mini-ork/runs/<run_id>/agent_s_task.yaml`:

```yaml
app: /path/to/target.app          # launched fresh by the evaluator
app_args: ["/path/to/project"]    # optional
launch_cmd: null                  # optional override (e.g. a script); if set, `app` is ignored
steps: >                          # natural language, what to do
  Open the Threads board, click a working run, open its run graph.
expect: >                         # natural language, what must be observable
  The run graph shows nodes with lane marks; selecting a running node
  shows the Stream tab with a spinner.
attempts: 3                       # k-of-n consensus, default 3, pass = strict majority
timeout_s: 300                    # per attempt, default 300
max_usd: 2.0                      # advisory cap; evaluator aborts further attempts when exceeded
```

YAML parsing must tolerate missing optional keys; a missing `steps` or `expect`
or `app`/`launch_cmd` is a config error → DEFER (never fail the run for a
malformed task spec).

## Required behaviour of mini_ork/gates/agent_s_smoke.py

1. **Registration.** Importable as
   `from mini_ork.gates.agent_s_smoke import register; register()` which calls
   `gate_registry.register_gate_evaluator("agent_s_gui", evaluate)` (verify the
   exact signature in `gate_registry.py` first; if in-process registration
   requires a different shape, adapt to it, do not edit other files to make it
   fit). Also expose `python -m mini_ork.gates.agent_s_smoke <task.yaml>` with
   the executable-contract exit codes: 0 = pass, 2 = defer, 1 = fail
   (a `if __name__ == "__main__":` guard is REQUIRED — known trap).
2. **Evaluate(context)**: locate the task spec from the run dir in context;
   absent spec → DEFER (this gate never fires unless a run asks for it).
3. **Env gating — the safety rule.** The agent executes real input events. It
   MUST NOT run unless ALL of:
   - `MO_AGENT_S_ALLOW=1` (explicit operator opt-in, per invocation),
   - a main-model provider key present (`AGENT_S_PROVIDER` ∈ {openai,
     openrouter, anthropic, vllm} with the matching key var, see Env),
   - a grounding endpoint present (`AGENT_S_GROUND_URL`).
   Any missing → DEFER with a one-line reason in the result JSON. Never FAIL a
   run because the GUI verifier could not run.
4. **Launch isolation.** Before attempts: quit existing instances of the target
   app (`osascript -e 'quit app "<name>"'` best-effort), launch fresh with a
   scratch state dir (`TMPDIR`-based `HOME` override where the app honors it;
   for the Zed fork pass `--foreground` and a temp user-data dir). After
   attempts: quit the app. All launch/quit failures → DEFER.
5. **Attempt loop.** `attempts` invocations of the Agent-S CLI:
   `agent_s --provider <p> --model <m> --ground_provider huggingface
   --ground_url <url> --ground_model ui-tars-1.5-7b` plus the task's
   `steps`+`expect` composed per Agent-S's documented task input (read the
   repo README at run time; if the CLI shape differs, follow the README —
   pin nothing this kickoff hasn't verified). Each attempt: `timeout_s` cap,
   stdout/stderr captured, screenshots/transcript copied from Agent-S's output
   dir (or screen-captured per attempt when Agent-S doesn't persist them).
6. **Pass decision.** An attempt passes when Agent-S's own final report says
   the task succeeded (parse its JSON/stdout; if only free text, a strict
   marker grep for `success`/`completed` — be conservative, ambiguous = not
   passed). Gate passes on strict majority of attempts (2 of 3 by default).
   Zero attempts ran → DEFER.
7. **Evidence.** Write `.mini-ork/runs/<run_id>/artifacts/agent_s/`:
   `verdict.json` `{pass: bool|null, state: pass|fail|defer, attempts: [{rc,
   duration_s, passed, log_tail}], reason: str, evidence: [screenshot paths]}`
   plus the screenshots themselves. `pass: null` for defer. Nothing is written
   outside the run dir.
8. **Cost guard.** Stop launching further attempts once `max_usd` is exceeded
   (estimate: attempts × per-attempt estimate from Agent-S's reported usage
   when available; otherwise a flat `AGENT_S_USD_PER_ATTEMPT` estimate,
   default 0.5). Result state stays what the completed attempts support.
9. **Verdict semantics.** This gate is ADVISORY: on `fail` it returns `defer`
   to the gate registry is WRONG — return the honest `fail` to the registry
   but the module docstring + `verdict.json` carry `"advisory": true` so
   merge-time consumers can treat it as evidence, not a hard gate. Registry
   consumers decide weighting; this module only reports.

## Env (documented in the module docstring; do NOT edit secrets files)

- `AGENT_S_PROVIDER` (default `openrouter`), `AGENT_S_MODEL` (default
  `openrouter/<a cheap vision-capable chat model>`), plus the provider key var:
  `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` /
  `AGENT_S_BASE_URL` (vllm-style openai-compatible).
- `AGENT_S_GROUND_URL` + `HUGGINGFACE_API_KEY` (UI-TARS-1.5-7B via HF
  Inference Providers; document the endpoint setup in the docstring).
- `MO_AGENT_S_ALLOW=1`, `MO_AGENT_S_USD_PER_ATTEMPT`.
- Keys are read from the environment mini-ork already loads
  (`config/secrets.local.sh`); the kickoff does not add them.

## Tests (tests/unit/test_agent_s_smoke_py.py)

No GUI, no network, no real Agent-S: monkeypatch `subprocess.run` /
`osascript` / file writes into tmp dirs. Cover at least:
- absent task spec → defer; malformed spec → defer.
- `MO_AGENT_S_ALLOW` unset or key missing → defer, no subprocess spawned.
- 2-of-3 majority pass → pass; 1-of-3 → fail; zero attempts ran → defer.
- attempt timeout counts as not-passed, loop continues.
- rc contract of `__main__`: 0/1/2 mapped correctly (invoke the module's main
  via `runpy` with sys.argv patched).
- cost guard stops attempts once cap exceeded.
- evidence dir + verdict.json shape (advisory flag present).

## Verification

```
python3.11 -m pytest -q -p no:asyncio tests/unit/test_agent_s_smoke_py.py
mini-ork validate
make lint
```

A live smoke (real Agent-S run against the built IDE) happens after merge,
manually, with `MO_AGENT_S_ALLOW=1` on a dedicated session — NOT in this
kickoff's scope.

## Out of scope (later kickoffs if this proves out)

- VM isolation (colima Linux VM with VNC display) so the agent never shares
  the operator's GUI session.
- Scheduler/nightly wiring; IDE-post-build automation.
- Differential adjudication mode (Agent-S as heterogeneous verifier on
  held-out probes).
