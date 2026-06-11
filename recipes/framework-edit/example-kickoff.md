# Example Kickoff — Framework-Edit Recipe

**Task:** Add a "Cost-saved-vs-Opus" badge to the Trajectory UI page header.

**Change description:**
In `ui/src/routes/AgentDetailPage.tsx`, add a small badge component next to
the page title that displays the estimated cost savings of the current run
compared to an Opus-only baseline. The badge should read "Saved $X.XX vs Opus"
and derive the value from `run.metadata.estimated_savings_usd`.

Also update `ui/src/components/AgentTranscript.tsx` to pass the
`estimated_savings_usd` field through the transcript prop so the badge can
access it.

**Glob hint:** `ui/src/{routes,components}/**`

**Expected outputs:**
- `framework-edit.diff` (unified diff, 2 files)
- `verdict.json`
