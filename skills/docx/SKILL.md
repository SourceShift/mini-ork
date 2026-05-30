---
name: docx
description: Create and edit professional Word documents (.docx). Use when exporting book content to DOCX format for editors, publishers, or offline reading. Wraps pandoc for markdown-to-docx conversion with style reference injection and theme application.
license: MIT
metadata:
  author: agentflow
  version: '1.0.0'
  based_on: ComposioHQ/awesome-claude-skills document-skills/docx
---

# DOCX Export for Book Content

Converts assembled book markdown to .docx format using pandoc. Supports style reference
documents, font/color theme injection, and table-of-contents generation.

## Prerequisites

- **pandoc** (v3.1+): `apt-get install pandoc`

## Workflow

### 1. Receive assembled markdown

Same markdown input as the EPUB path (assembled from atomic blocks by
`markdownAssembler.ts`).

### 2. Generate DOCX with pandoc

```bash
pandoc input.md \
  --from markdown+tex_math_dollars+raw_html \
  --to docx \
  --reference-doc=template.docx \
  --metadata title="Book Title" \
  --metadata author="Author Name" \
  --toc \
  --toc-depth=3 \
  --highlight-style pygments \
  -o output.docx
```

Key flags:
- `--reference-doc=template.docx`: injects styles (fonts, colors, margins) from a
  reference document. The Theme Factory skill generates these per-theme.
- `--toc`: auto-generates table of contents from headings.
- `--toc-depth=3`: include H1-H3 in TOC.
- `--highlight-style pygments`: syntax highlight code blocks.

### 3. Math handling in DOCX

Pandoc converts `<math>` / `<displaymath>` to Office Math Markup Language (OMML)
equations, which Word renders natively. No pre-rendering required.

### 4. Mermaid diagrams in DOCX

Pre-render Mermaid blocks to PNG/SVG before conversion (same as EPUB path). Pandoc
embeds images in the docx.

### 5. Theme application

The Theme Factory skill produces a theme-specific `template.docx` (reference document)
with:
- Custom heading fonts and sizes
- Body text font and spacing
- Color palette for headings, links, table borders
- Page margins and layout

Generate `template.docx` once per theme, then reference it via `--reference-doc`
during conversion.

## Failure handling

No fallback. If pandoc fails, the export job goes to `failed`. Bubble the error message.

## Dependencies

```dockerfile
RUN apt-get update && apt-get install -y pandoc
```
