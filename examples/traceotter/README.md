# TraceOtter Example

Create a tiny local dataset from your agent sessions:

```bash
traceotter --json pipeline \
  --codex /Users/admin/.codex/sessions \
  --claude /Users/admin/.claude/projects \
  --mini-ork ../../.mini-ork/runs \
  --out /tmp/traceotter-demo \
  --limit-files 20 \
  --max-events-per-file 200
```

Inspect outputs:

```bash
jq . /tmp/traceotter-demo/report.json
head -1 /tmp/traceotter-demo/episodes.jsonl | jq .
jq '.[0]' /tmp/traceotter-demo/skills.json
```

Train with LLaMA-Factory after reviewing/redacting the dataset:

```bash
llamafactory-cli train /tmp/traceotter-demo/llamafactory/llamafactory_sft.yaml
```

