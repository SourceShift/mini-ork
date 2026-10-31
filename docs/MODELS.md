# Model Routing Reference

mini-ork routes work through **lanes**. A workflow node declares a
`model_lane`, and `.mini-ork/config/agents.yaml` maps that lane to a provider
entry in the registry (`config/providers.yaml`). The entry's `kind` selects the
transport — an external agent CLI or a direct HTTP adapter — built by
`mini_ork/dispatch/providers.py`.

This keeps recipes portable: a recipe can say `reviewer` or `glm_lens` without
hard-coding a vendor model in every node.

---

## Committed Provider Lanes

Lanes are entries in `config/providers.yaml`; there is no per-lane wrapper. (The
bash wrappers `lib/providers/cl_*.sh` were removed in the 2026-07 bash removal,
`docs/plans/2026-07-26-bash-removal-plan.md`.) A
`$MINI_ORK_HOME/config/providers.yaml` replaces the committed file as a whole —
it is not merged — so copy every lane you still need into it.

| Lane | Kind | Transport | Typical use |
|---|---|---|---|
| `glm` | `anthropic-compat` | Claude CLI against an Anthropic-shaped gateway (`GLM_API_KEY`) | cheap tactical checks and structured analysis |
| `kimi` | `anthropic-compat` | Claude CLI against a gateway (`KIMI_API_KEY`) | long-context review and synthesis |
| `minimax` | `anthropic-compat` | Claude CLI against a gateway (`MINIMAX_API_KEY`) | additional heterogeneous lens family |
| `deepseek`, `deepseek_flash` | `anthropic-compat` | Claude CLI against a gateway (`DEEPSEEK_API_KEY`) | budget planning or implementation lanes |
| `opus`, `sonnet` | `anthropic-native` | Claude CLI on the ambient Anthropic login | high-reasoning review; general worker |
| `anthropic_api` | `anthropic-native` | Claude CLI with your own `ANTHROPIC_API_KEY` | the same, billed to your key |
| `codex` | `codex-native` | `codex` CLI on its ambient login | executable coding and repository-grounded work |
| `opencode` | `opencode-native` | `opencode` CLI, auth from its own store | additional coding lane |
| `openai_api` | `openai-compat` | direct HTTP to an OpenAI-shaped endpoint (`OPENAI_API_KEY`) | BYO-key chat endpoints (OpenAI, OpenRouter, vLLM) |
| `uhp_harnessrouter` | `uhp` | direct HTTP SSE to a local HarnessRouter | local-only harness routing |

An `executable` kind runs a command you name, for transports no built-in kind
covers. Every gateway lane needs its key in `config/secrets.local.sh`; a missing
key fails the dispatch at the lane preflight (`mini-ork doctor` reports it
first).

Exact model names and prices belong in the registry entry's `model:` pin or the
deployment environment, not in recipe docs. Verify live provider pricing before
large runs.

---

## Lane Binding

Project-local lane policy is normally stored at `.mini-ork/config/agents.yaml`:

```yaml
lanes:
  planner: opus
  researcher: codex
  implementer: codex
  worker: codex
  reviewer: opus
  verifier: glm
  reflector: codex
  publisher: codex
  rollback: codex

  glm_lens: glm
  kimi_lens: kimi
  codex_lens: codex
  opus_lens: opus
  minimax_lens: minimax
```

Recipe nodes then reference the lane:

```yaml
nodes:
  - name: reviewer
    type: reviewer
    model_lane: reviewer
    prompt_ref: prompts/reviewer.md
    dispatch_mode: serial
```

Resolution path:

```text
workflow.yaml node.model_lane
  -> .mini-ork/config/agents.yaml lane binding
  -> config/providers.yaml entry (its `kind` picks the transport)
  -> external CLI or direct HTTP endpoint configured on the host
```

---

## Heterogeneous Panels

The audit, research, migration, runbook, UI, blog, post-MVP, and recursive
self-improvement recipes use lens lanes such as `glm_lens`, `kimi_lens`,
`codex_lens`, `opus_lens`, and sometimes `minimax_lens`.

That is the load-bearing design choice: the recipe gets independent model
families by configuration rather than by prompt persona alone.

Example from a panel recipe:

```yaml
nodes:
  - name: lens_glm
    type: researcher
    model_lane: glm_lens
    prompt_ref: prompts/lens-glm.md
    dispatch_mode: parallel

  - name: lens_opus
    type: researcher
    model_lane: opus_lens
    prompt_ref: prompts/lens-opus.md
    dispatch_mode: parallel
```

---

## Harness Identity and Conformance

A lane name does not say what ran: the same model behind a different harness,
or a different release of the same CLI, is a different system, and a harness
swap alone moves cost and token counts by multiples. Every `llm_calls` row
therefore records the harness that served it in `metadata_json.harness`, and
the dispatch row in the audit chain carries the same object:

```json
{"harness": "claude-code", "kind": "anthropic-native", "binary": "claude", "version": "2.1.3 (Claude Code)"}
```

`harness` is the vendor CLI (`claude-code`, `codex`, `opencode`), an
`executable` script (version `sha256:<12 hex>` of the script), or one of
mini-ork's own transports (`mini-ork/openai-compat`, `mini-ork/uhp`, …; version
`mini-ork <version>`). CLI versions are read once per release of the binary and
cached in `$MINI_ORK_HOME/harness-versions.json`.

The lane preflight only asks a gateway whether it accepts a key. A
**conformance probe** asks whether the lane, through its real harness, keeps
the dispatch contract. It runs one real dispatch — same argv, env, cwd pin and
default tool grants a node gets — on a task with an unguessable answer:

```bash
mini-ork providers conform codex sonnet glm      # PASS/FAIL per lane; exit 1 if any fail
mini-ork providers conform --json --timeout 120 codex
```

| check | passes when |
|---|---|
| `answered` | the dispatch returned rc 0 with text |
| `cwd_and_tools` | the lane read a nonce file in the pinned cwd and returned it |
| `json_contract` | the reply is a JSON object carrying the nonce (the planner shape rule) |
| `usage_reported` | input + output tokens > 0, so cost accounting is real |
| `read_only` | the scratch directory holds only the probe file afterwards |

Each probe costs one small call, is written to `llm_calls` like any other
dispatch, and lands in the audit chain as `conformance_probe` with the harness
identity it ran under. With `MO_REQUIRE_CONFORMANCE=1` the lane preflight
refuses a lane that has no passing probe **for the harness release it runs
now** — upgrading the CLI re-opens the question until the lane is probed again.

Every run also writes a **harness card** to `runs/<run_id>/harness-card.md`
(and `.json`): per lane, the harness and release that served it, its calls,
tokens and cost, and that release's conformance status; per node, the lane it
requested and the lane that served it. A fallback is listed as a warning, and
so is a *family collapse* — nodes that asked for different model families but
were all served by one, which turns a heterogeneous panel into same-family
consensus without failing anything:

```bash
mini-ork harness-card <run_id>            # markdown
mini-ork harness-card <run_id> --json     # the card as JSON
mini-ork harness-card <run_id> --write    # (re)write it into the run dir
```

---

## Changing Providers

To change all nodes on a lane, edit `.mini-ork/config/agents.yaml`:

```yaml
lanes:
  reviewer: kimi
```

To change one recipe without changing global policy, add a recipe-specific lane
policy and stage it before running. `bin/mini-ork-self-improve` does this for
`config/agents.recursive-self-improve.yaml`.

---

## Adding a Provider

### Option 1: providers.yaml registry (no code, BYO keys)

For any Anthropic or OpenAI-compatible endpoint, add an entry to
`config/providers.yaml` with `kind`, `model`, `base_url`, and `api_key_env`
(the NAME of an env var you export in
`$MINI_ORK_HOME/config/secrets.local.sh` — template at
`config/secrets.example.sh`). Then bind a lane to it in
`.mini-ork/config/agents.yaml`. See `docs/CONFIG.md` →
"Bring-your-own providers" and `tests/test_provider_registry.sh`.

### Option 2: `executable` kind (custom CLI or env logic)

1. Write an executable script that reads the prompt, calls the external model,
   writes the response to stdout, and exits non-zero on failure.
2. Declare it in `config/providers.yaml` with `kind: executable` and
   `script: <path>` (relative paths resolve against the mini-ork root; the
   file must be executable).
3. Add a lane binding in `.mini-ork/config/agents.yaml`.
4. Run `mini-ork doctor` to confirm the lane passes its preflight, then
   `mini-ork providers conform <lane>` to confirm its harness keeps the
   dispatch contract.

The top-level recipes should not need to change when a provider is added; they
should keep referring to lanes.

---

## Safety Notes

- Do not silently remap failed providers to another family. A fallback can turn
  a heterogeneous panel into same-family consensus without the user noticing.
- Keep Opus available for recipes that explicitly depend on the Opus
  architectural-shape lens.
- For temporary provider freezes, change lane policy explicitly and record the
  run context. Do not delete recipe lanes just because one provider is disabled
  for a validation window.
