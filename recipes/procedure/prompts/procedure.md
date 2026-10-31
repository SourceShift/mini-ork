# Run the procedure

The plan's objective names the kickoff file. Read that file in full: it holds
the procedure. Then carry out every step yourself, in order, in this one
session — do not split the work, skip ahead, or stop to ask.

Rules:
- Do exactly what each step says, in the working directory you were started in.
- If a step cannot be done, stop there and say why; do not invent a workaround
  that changes what the procedure asked for.
- Do not commit, push, or rewrite git history.

When you finish (or stop), write `${MINI_ORK_RUN_DIR}/procedure-report.md`:

```
## Steps
- [x] 1. <step, in the procedure's own words> — <what you did; the command or file that shows it>
- [ ] 4. <a step you could not do> — <why>
```

One line per step of the procedure, numbered as in the kickoff. Check a box
only for a step you actually completed. Nothing else in the file is graded.
