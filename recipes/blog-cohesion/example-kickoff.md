# Kickoff — blog-cohesion: polish an existing draft

## Post under audit

`/Volumes/docker-ssd/ps/blog/my_amir_blog/src/content/blog/2026-06-11-fable-token-tax.md`

(The planner copies this absolute path into `objective` on a line
beginning with `POST_PATH:`. Lens nodes grep for that marker to find
the post.)

## Audit goals

Five lenses examine the post and emit JSON findings. The arbiter
reconciles overlapping suggestions and produces:

- `${MINI_ORK_RUN_DIR}/applied_post.md` — the modified draft (the
  user `cp`s this back over the original to accept)
- `${MINI_ORK_RUN_DIR}/apply_log.md` — what was applied, rejected,
  or left as an open carve-out

## Scope

- Polish-only. NO new claims, citations, or arxiv IDs introduced.
- Preserve frontmatter verbatim: `title`, `description`, `pubDate`,
  `draft`, `tags`, `authors`, any `previewSlug`.
- Preserve code fences, mermaid blocks, `<figure>` HTML, and
  `<span class="term" ...>` tooltips through edits.

## Out of scope

- AI-tell removal (use `humanizer` recipe for that).
- Fresh drafting (use `blog-post` recipe with a kickoff).
- Image generation / figure manifesting (out-of-band).

## Definition of Done

1. All 5 lens JSON outputs (`context-{thesis,bridge,topic,entity,
   rhythm}.json`) exist in `${MINI_ORK_RUN_DIR}` and parse with a
   top-level `verdict` field.
2. `applied_post.md` exists, retains frontmatter, and is in 80–130%
   of the source post's word count (excluding `<figure>` blocks).
3. `apply_log.md` exists with all four required sections (Inputs
   read, Applied, Rejected, Open carve-outs).
4. `verifiers/cohesion-completeness.sh` returns `pass: true`.

## Voice constraints (carried into arbiter)

- Em-dash density ≤ 1.5 per 100 visible words.
- Word count: 600–1500 (unless the source is already outside).
- Frontmatter field set must be intact post-edit.

## Branch

The user reviews `${MINI_ORK_RUN_DIR}/applied_post.md` against the
original and decides whether to `cp` over. No auto-publish.
