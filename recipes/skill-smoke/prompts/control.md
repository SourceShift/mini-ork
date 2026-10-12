# Control-node probe — skill smoke

You are a researcher in the skill-smoke recipe. Your node was granted NO
skills.

Do exactly this, then write your lens:

1. Run this shell command and keep its verbatim output (an error or an
   empty listing is the expected, correct answer — report it verbatim):

   ```
   ls .claude/skills/ || echo "no .claude/skills directory"
   ```

2. Write `lens-control.md` (run directory) — a markdown doc whose FIRST line
   is a `#` header, with at least four non-blank lines, containing:
   - the `ls` output verbatim
   - one line stating whether any skill directory was visible to you

Do NOT invent skills you did not see. No analysis — this is a probe,
brevity is correct.
