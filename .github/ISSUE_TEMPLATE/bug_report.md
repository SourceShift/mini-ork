---
name: Bug report
about: Report a reproducible defect in mini-ork
title: "bug: <short description>"
labels: ["bug", "needs-triage"]
assignees: []
---

## Version

```
mini-ork --version
```

## Environment

- OS + version:
- bash version (`bash --version`):
- sqlite3 version (`sqlite3 --version`):
- claude CLI version (`claude --version`):

## Reproduction

Minimal `kickoff.md` that triggers the bug (remove anything not needed to reproduce):

```markdown
<!-- paste minimal kickoff.md here -->
```

Command run:

```bash
mini-ork deliver kickoff.md
```

Relevant lines from `.mini-ork/runs/<run-id>/run.log`:

```
<!-- paste log lines here -->
```

## Observed Behavior

<!-- What happened -->

## Expected Behavior

<!-- What should have happened -->

## Additional Context

<!-- screenshots, state.db query results, etc. -->
