# Kickoff — UI audit: BookReader mobile + desktop

## Surfaces to audit

1. **BookReader page** (desktop + mobile)
   - Desktop entry: `https://libwit.com/library/<book-uuid>/read`
   - Mobile entry: same URL, viewport 390×844
   - Files: `src/pages/reader/BookReader.tsx`, `src/pages/reader/BookChapterArticle.tsx`,
     `src/components/libwit/reader/mobile/MobileReaderChrome.tsx`

2. **Highlighter toolbar** (selection-triggered, both viewports)
   - Files: `src/components/libwit/highlighter/HighlighterToolbar.tsx`,
     `src/components/libwit/reader/mobile/MobileReaderSelectionPopover.tsx`

## Target users

- Researchers on macOS desktop, Chrome / Safari
- Mobile readers on iOS Safari (primary) + Android Chrome (secondary)
- Screen-reader users (VoiceOver iOS + macOS, NVDA on Windows)

## Scope boundaries

- WILL NOT cover: server-side rendering perf (book-fetch route).
- WILL NOT cover: design-system primitives outside the reader (CitationsPage etc).
- WILL NOT cover: deep-link / sharing flows.

## Viewport matrix override

- desktop: 1440×900
- mobile: 390×844

## Severity rubric override

Default rubric is fine. Add: any keyboard-trap = P0 (we have screen-reader
users in the target profile).

## Distribution

Findings → `docs/ux-briefs/<date>-bookreader-audit.md`. Pin the P0 fixes
into the next sprint.
