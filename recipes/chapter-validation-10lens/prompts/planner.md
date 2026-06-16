# Planner — chapter-validation-10lens

You receive a kickoff describing the chapter under review.

Emit `${MINI_ORK_RUN_DIR}/plan.json` with this exact shape:

```json
{
  "task_class": "chapter_validation_10lens",
  "chapter_artifact_path": "<absolute path to the chapter markdown>",
  "chapter_context": {
    "title": "<chapter title>",
    "genre": "<technical|academic|trade|textbook|practical|literary|other>",
    "target_word_count": <integer>,
    "assigned_source_ids": ["<id1>", "<id2>"],
    "publisher_style": "<style slug or null>",
    "language": "<en|fa|es|...>"
  },
  "lens_partitions": [
    {"lens_id": "01", "lens_name": "structure"},
    {"lens_id": "02", "lens_name": "factuality"},
    {"lens_id": "03", "lens_name": "voice_tone"},
    {"lens_id": "04", "lens_name": "length_density"},
    {"lens_id": "05", "lens_name": "forbidden_constructs"},
    {"lens_id": "06", "lens_name": "markdown_format"},
    {"lens_id": "07", "lens_name": "coverage"},
    {"lens_id": "08", "lens_name": "coherence"},
    {"lens_id": "09", "lens_name": "reader_contract"},
    {"lens_id": "10", "lens_name": "synthesis_originality"}
  ],
  "verifier_contract": {
    "checks": [
      {"id": "lens_outputs_complete", "kind": "verifier_ref", "ref": "verifiers/lens_outputs_complete.sh"}
    ]
  }
}
```

Parse the kickoff to fill `chapter_context`. If a field is unknown,
emit explicit `null` rather than guessing. The `lens_partitions` array
is fixed — emit verbatim. Write the file and nothing else.

## Kickoff content

{{KICKOFF_CONTENT}}
