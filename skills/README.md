# mini-ork Skills

8 generic skills ported from the source project. Each skill is a self-contained
Claude Code skill directory — copy `SKILL.md` + supporting files into your
project's `.claude/skills/` to install.

---

## Ported skills (generic — use as-is)

| Skill dir | What it does |
|-----------|-------------|
| `react-best-practices/` | React component quality rules: hooks discipline, prop-drilling avoidance, memoization patterns |
| `composition-patterns/` | UI composition patterns: compound components, render props, slot patterns |
| `data-testid-naming/` | Naming conventions for `data-testid` attributes; enforces consistent testid schemas |
| `web-design-guidelines/` | Cross-cutting UI design rules: spacing, color, typography, accessibility baseline |
| `theme-factory/` | Token-based theme generation: dark/light/sepia variants from a seed palette |
| `pm-product-spec/` | Product spec writing: user stories, acceptance criteria, edge cases |
| `markdown-to-epub/` | Converts a Markdown book (chapter files) to an EPUB archive |
| `docx/` | Reads/writes `.docx` files via pandoc; useful for report generation |

---

## Deferred skills (templatable — not ported in v0.1)

These 5 skills from the source project contain project-specific logic that
requires non-trivial templating. Ported versions are planned for v0.2:

- `lib-docs-context7` — Context7 doc fetch wired to a specific library registry
- `agentflow-bundle-delta` — Vite bundle delta analysis (references specific build paths)
- `agentflow-playwright-template` — Playwright test scaffolding (references specific testid conventions)
- `agentflow-scope-question` — Scope clarification prompt (references specific features list)
- `react-view-transitions` — React view transition patterns (references specific page structure)

---

## Installing a skill

Copy the skill directory into your project's skills folder:

```sh
cp -r ~/ps/mini-ork/skills/react-best-practices /your/project/.claude/skills/
```

Then reference it in your project's `CLAUDE.md` or invoke via `/react-best-practices`.

---

## Skipped skills (source-project-specific, not ported)

The following skills from the source project are tightly coupled to that
project's domain and are not suitable for generic use:

- `deep-research` — book-generation research pipeline
- `article-extractor` — arXiv article extraction workflow
- `recursive-research` — recursive book-research loop
- `youtube-transcript` — YouTube transcript to book chapter
- `agentflow-fix-arbitrary-tailwind` — project-specific Tailwind CSS fixer
