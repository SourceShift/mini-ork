# Reader-Shell Composer Bug Audit Plan

1. **Lock the bug definition.** Count only concrete defects on the reader-shell “Ask about this book” composer FE->BE path: observable broken behavior, contract mismatch, race/stale state, security or fail-open exposure, missing error handling, observability gap that hides failure, or dead UI/code after cutover. Exclude wishlist UX, speculative rewrites, style preferences, and unproven “could be better” claims.

2. **Apply strict evidence thresholds.** Every bug in lens output must include file:line evidence and a short reproduction or trigger sketch. Kimi contract bugs need two anchors: the declared/expected contract and the implementation that violates it. Minimax user-impact bugs need one root-cause anchor plus the user-visible symptom.

3. **Cover every kickoff feature once.** Require at least one bug-find pass over each named surface: `ReaderShellComposer.tsx`, `ReaderShellAskRail.tsx`, `ChatThreadBody.tsx`, `useChatSessions.ts`, `/api/chat-sessions/:id/stream`, `chatGateway`, `chatSessionService`, `langgraph-sse`, and citation/auxiliary answer services where they affect citations or answer generation.

4. **Target in-scope bug classes.** Ask lenses to explicitly check correctness, FE/BE contract drift, SSE event-shape drift, request-body key drift, stale closure/dependency bugs, races around pending asks and streaming aborts, fail-open or silent failure paths, dead Attach/Voice/Widen-scope affordances, DB/type mismatches, and missing failure observability.

5. **Synthesize with false-positive filtering.** Deduplicate by underlying root cause, not by symptom. Drop any item without a concrete trigger, impact, and source anchor. Mark `CONSENSUS 2/2` only when both lens reports identify the same defect or same root cause; otherwise mark `SINGLE-LENS`.

6. **Grade severity consistently.** Use P0 for data loss/security/cross-user leakage or unusable core ask flow; P1 for broken submit/stream/session/citation behavior affecting normal use; P2 for recoverable broken affordances, misleading state, or important silent failure; P3 for edge-case polish bugs with low blast radius. Report severity counts in the synthesis.

7. **Emit report-only artifacts.** Write the unified ranked list to `${MINI_ORK_RUN_DIR}/synthesis.md`, cross-reference `lens-kimi.md` and `lens-minimax.md`, include file:line anchors, reproduction sketch, and impact for each bug. Do not include patches, implementation tasks, PR language, or fix proposals. Success is `verifiers/lens-completeness.sh` returning `pass=true`.
