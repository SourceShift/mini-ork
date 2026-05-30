## 0.1.0 — 2026-05-30 — initial recipe (Phase A redesign)

- First release of the `code-fix` reference recipe.
- Implements the universal task loop: Classify → Plan → Execute → Verify → Reflect.
- Ships `workflow.yaml`, `task_class.yaml`, `artifact_contract.yaml`.
- Ships generic planner, implementer, and reviewer prompts (no project-specific language).
- Ships `verifiers/typecheck.sh` and `verifiers/test.sh` with auto-detect fallback chains
  for TypeScript (`tsc`), Python (`mypy`/`pytest`), Rust (`cargo`), Go (`go test`), Ruby.
- Ships `example-kickoff.md` and `example-output.md` as end-to-end documentation.
