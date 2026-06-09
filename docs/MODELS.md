# Model Routing Reference

mini-ork routes work through **lanes**. A workflow node declares a
`model_lane`, and `.mini-ork/config/agents.yaml` maps that lane to a provider
family. The provider wrapper then invokes the external CLI or API adapter.

This keeps recipes portable: a recipe can say `reviewer` or `glm_lens` without
hard-coding a vendor model in every node.

---

## Shipped Provider Families

Provider wrappers live in `lib/providers/`:

| Provider key | Wrapper | Typical use |
|---|---|---|
| `glm` | `lib/providers/cl_glm.sh` | cheap tactical checks and structured analysis |
| `kimi` | `lib/providers/cl_kimi.sh` | long-context review and synthesis |
| `codex` | `lib/providers/cl_codex.sh` | executable coding and repository-grounded work |
| `deepseek` | `lib/providers/cl_deepseek.sh` | budget planning or implementation lanes |
| `opus` | `lib/providers/cl_opus.sh` | high-reasoning review, synthesis, architecture lens |
| `sonnet` | `lib/providers/cl_sonnet.sh` | general Anthropic worker lane |
| `minimax` | `lib/providers/cl_minimax.sh` | additional heterogeneous lens family |

Exact model names and prices belong in the provider wrapper or deployment
environment, not in recipe docs. Verify live provider pricing before large
runs.

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
  -> lib/providers/cl_<provider>.sh
  -> external CLI/API configured on the host
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

1. Add a wrapper at `lib/providers/cl_<provider>.sh`.
2. Make it read prompt input, call the external model, write the response to
   stdout, and return non-zero on failure.
3. Add a lane binding in `.mini-ork/config/agents.yaml`.
4. Add a smoke probe to the relevant provider-doctor or integration script.

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
