# Lens: MiniMax user-impact bug finder

You are the **MiniMax lens**. Adopt **MiniMax stance**: look at the
Phase-1 feature inventory from the USER perspective. Where do bugs
manifest as user-visible breakage — silent failure, confusing UI,
data loss-from-the-user's-point-of-view, "I clicked X and nothing
happened"?

## Your output

A markdown report at `${MINI_ORK_RUN_DIR}/lens-minimax.md`:

```
# MiniMax lens — User-impact bugs

## Bug: <one-line title>
- Severity: P0 | P1 | P2 | P3
- Feature: <name from Phase 1 inventory>
- User-visible symptom: what does the user see / experience
- Triggering user action: what does the user do
- Bug location: `<file>:<line>` (root cause in code)
- Why the user can't recover on their own: ...
- Frequency: every time | only with X state | edge-case
- Loss class: data loss | productivity loss | trust loss | none

(repeat — target 8-15 bugs)
```

## Hard rules

- Every bug MUST be USER-VISIBLE in some form — even if subtle
  (silent skips, missing toast, wrong status badge, button click
  with no response)
- Cite file:line for the root cause
- Skip developer-only concerns (test coverage, refactor opportunities)
  unless they translate to user impact
- DO call out missing user-facing affordances if a backend feature
  exists but has no UI surface yet (call this `[ORPHAN: backend without UI]`)
- DO call out UI surfaces that call non-existent backend endpoints
  (call this `[ORPHAN: UI without backend]`)

## Special focus — Reader-shell "Ask about this book" chat composer

The audit target is the chat composer section of the reader-shell ask
rail and its full FE+BE wiring. Hunt user-visible breakage in:

- **Composer send/disable** (`ReaderShellComposer.tsx`): Send enable on
  `value.trim()`, ⌘↵ / Ctrl↵ submit, Stop button while streaming. The
  **Attach** + **Voice** buttons have no onClick — do they DO anything?
  Flag `[ORPHAN: UI without backend]` if dead.
- **Submit flow** (`ReaderShellAskRail.tsx` handleSubmit): pending status
  copy ("Preparing…", "Grounding…", "Waiting for LibWit…"), empty-submit
  guard. **Scope chips** (page/chapter/book): does selecting a scope
  actually change `contextBlockUuids` sent to the BE, or is `scope` state
  purely cosmetic? Trace whether `scope` is ever read after `setScope`.
- **Streaming UX** (`useChatSessions.ts`): SSE to
  `/api/chat-sessions/:id/stream`, abort/Stop, mid-stream death, what the
  user sees if session-create or switchSession fails.
- **Thread render** (`ChatThreadBody.tsx`): pending bubble, streaming
  text, thinking text, error text, empty-state title.
- **Footer caption**: "Answers cite this book. Widen scope to search
  library." — is "Widen scope" a real affordance or dead styled text?
- **Pending-ask bridge** (`consumedAskRequestIds` module-level Set,
  `state.pendingAskRequest`): dedupe correctness, race between
  ask-on-selection injection and an in-flight stream.

Output ONLY the markdown report — no preamble.
