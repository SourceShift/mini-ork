# Granted-node probe — skill smoke

You are a researcher in the skill-smoke recipe. Your node was granted one
skill by name; the executor placed it in your checkout before you started.

Do exactly this, then write your lens:

1. Run this shell command and keep its verbatim output:

   ```
   ls .claude/skills/ && cat .claude/skills/skill-smoke-demo/SKILL.md
   ```

2. Write `lens-granted.md` (run directory) — a markdown doc whose FIRST line
   is a `#` header, with at least four non-blank lines, containing:
   - the marker line copied verbatim from the SKILL.md you just read:
     `SKILL-SMOKE-DEMO-MARKER-7F3A`
   - one line naming the skill you saw (`skill-smoke-demo`)
   - the `ls` output verbatim

No analysis, no recommendations — this is a probe, brevity is correct.
