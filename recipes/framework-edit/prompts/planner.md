# Planner Prompt

You are planning a routine mini-ork framework edit.

Inputs:
- Natural-language change request.
- Optional file-glob hint that narrows the affected subtree.
- Any explicit `scope_allow` override for high-blast-radius files.

Kickoff content:
```text
{{KICKOFF_CONTENT}}
```

Produce a concise plan with:
1. Requested outcome in one sentence.
2. Candidate files or globs to inspect.
3. Files explicitly out of scope.
4. Expected verifier commands.
5. Binding artifact manifest:
   - `${MINI_ORK_RUN_DIR}/framework-edit.diff`
   - `${MINI_ORK_RUN_DIR}/verdict.json`
   - verdict schema: `{ "files_changed": number, "tests_pass": boolean, "static_pass": boolean, "pass": boolean }`

Do not add recipe-authoring nodes. This is a code-edit recipe, not a drafter
panel.
