# TraceOtter

TraceOtter is an OSS-oriented training pipeline for turning local coding-agent
trajectories into small-model training data.

It is built for this target loop:

```text
Codex JSONL + Claude JSONL + mini-ork runs
  -> ADP-inspired normalized episodes
  -> MemP-style procedural skill consolidation
  -> LLaMA-Factory SFT dataset/config
  -> later reward model + Agent-R1/verl RL
```

## Why It Exists

No existing open-source project currently does the whole job automatically:

- normalize local Codex/Claude/mini-ork transcripts;
- consolidate recurring behavior into procedural memory;
- export small-model training data;
- gate replacement with replay/eval.

TraceOtter is the missing glue. It uses:

- **Agent Data Protocol** as the normalization reference;
- **MemP** as the procedural-memory consolidation reference;
- **Open-SWE-Traces** and **SWE-Zero** as public bootstrap data sources for later training mixes;
- **LLaMA-Factory** as the first SFT/QLoRA training target;
- **Agent-R1/verl** only after the evaluator/reward model is stable.

## CLI

```bash
traceotter --json doctor
```

Normalize local traces:

```bash
traceotter --json ingest \
  --codex /Users/admin/.codex/sessions \
  --claude /Users/admin/.claude/projects \
  --mini-ork .mini-ork/runs \
  --out .traceotter/local \
  --limit-files 200
```

Consolidate procedural skills:

```bash
traceotter --json consolidate \
  --episodes .traceotter/local/episodes.jsonl \
  --out .traceotter/local/skills.json
```

Export LLaMA-Factory artifacts:

```bash
traceotter --json export-llamafactory \
  --episodes .traceotter/local/episodes.jsonl \
  --skills .traceotter/local/skills.json \
  --out .traceotter/local/llamafactory
```

Run the full local preparation pipeline:

```bash
traceotter --json pipeline \
  --codex /Users/admin/.codex/sessions \
  --claude /Users/admin/.claude/projects \
  --mini-ork .mini-ork/runs \
  --out .traceotter/local \
  --limit-files 500
```

## Outputs

```text
.traceotter/local/
  manifest.json
  episodes.jsonl
  skills.json
  report.json
  llamafactory/
    traceotter_sft.json
    dataset_info.json
    llamafactory_sft.yaml
```

The LLaMA-Factory dataset uses an Alpaca-style shape:

```json
{
  "instruction": "Given this coding-agent task, choose the route, key procedure, and verification plan.",
  "input": "...episode context...",
  "output": "...route/procedure/verification..."
}
```

## Training

Copy or symlink the generated `dataset_info.json` and dataset JSON into your
LLaMA-Factory `data/` directory, then run the generated config:

```bash
llamafactory-cli train .traceotter/local/llamafactory/llamafactory_sft.yaml
```

Default base model:

```text
Qwen/Qwen2.5-Coder-1.5B-Instruct
```

This is intentionally small enough for early local or modest GPU experiments.
Increase model size only after the replay evaluator shows the dataset is clean.

## Public Bootstrap Data

TraceOtter's first local implementation does not download public datasets
automatically. The intended next dataset mixer should combine:

- local `episodes.jsonl`;
- NVIDIA Open-SWE-Traces;
- NVIDIA SWE-Zero OpenHands trajectories.

The local data should receive higher sampling weight for researcher-specific
behavior, while public SWE data should improve general coding-agent priors.

## Safety

TraceOtter redacts obvious token/API-key patterns, but this is not enough for
public release of private traces. Treat all generated datasets as private until
reviewed.

Do not train directly on raw transcripts. Train on normalized, redacted,
deduplicated, and labeled episodes.

