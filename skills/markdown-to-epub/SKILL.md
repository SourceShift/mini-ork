---
name: markdown-to-epub
description: Convert assembled book markdown to EPUB3 format using pandoc. Handles LaTeX math (MathML), Mermaid diagrams (SVG pre-render via mmdc), syntax-highlighted code fences (skylighting), and CSS theme injection. Invoke when exporting a generated book to EPUB for e-reader distribution.
license: MIT
metadata:
  author: agentflow
  version: '1.0.0'
  based_on: ComposioHQ/awesome-claude-skills document-skills pattern
---

# Markdown to EPUB Converter

Converts markdown content (book chapters, math, diagrams, code) into a valid EPUB3 file
using pandoc. Designed for the Researcher book generation pipeline where books are stored
as atomic Logseq-style blocks in PostgreSQL.

## Prerequisites

- **pandoc** (v3.1+): `apt-get install pandoc`
- **mermaid-cli** (mmdc): `npm install -g @mermaid-js/mermaid-cli` (required for Mermaid → SVG pre-render)
- **epubcheck** (optional, for validation): `apt-get install epubcheck`

## Workflow

### 1. Receive assembled markdown

The markdown assembler (`markdownAssembler.ts`) produces a single `.md` file from the
book's atomic blocks. This is the input to the conversion.

### 2. Pre-render Mermaid diagrams

If the markdown contains ` ```mermaid ` fenced blocks, pre-render them to SVG:

```bash
# Extract all mermaid blocks and render to SVG files
# The pandoc Lua filter will reference these by hash
for f in *.mmd; do
  mmdc -i "$f" -o "${f%.mmd}.svg" -b transparent
done
```

Alternatively, use a pandoc Lua filter (`mermaid.lua`) that shells out to `mmdc` during
conversion.

### 3. Generate EPUB with pandoc

```bash
pandoc input.md \
  --from markdown+tex_math_dollars+raw_html \
  --to epub3 \
  --mathml \
  --highlight-style pygments \
  --css theme.css \
  --metadata title="Book Title" \
  --metadata author="Author Name" \
  --metadata lang="en" \
  --epub-cover-image=cover.png \
  --lua-filter=mermaid.lua \
  -o output.epub
```

Key flags:
- `--from markdown+tex_math_dollars`: recognizes `<math>` / `<displaymath>` as LaTeX math
- `--to epub3`: EPUB3 format (required for MathML support)
- `--mathml`: renders math as MathML (native in EPUB3 readers)
- `--highlight-style pygments`: syntax highlighting for code blocks
- `--css theme.css`: inject custom CSS (from Theme Factory)
- `--lua-filter=mermaid.lua`: pre-render Mermaid blocks to inline SVG
- `--epub-cover-image=cover.png`: book cover (resized to recommended 1600x2560)

### 4. Validate (optional)

```bash
epubcheck output.epub
```

## Content compatibility matrix

| Input feature | Pandoc handling | EPUB3 reader support |
|---------------|----------------|---------------------|
| `<math>` / `<displaymath>` | MathML via `--mathml` | Apple Books, Thorium, Calibre |
| ` ```mermaid ` | SVG via Lua filter + mmdc | All modern readers (SVG in XHTML) |
| ` ```python ` etc. | Syntax-highlighted HTML via skylighting | All readers (no JS required) |
| `| table |` | HTML `<table>` | All readers |
| `![alt](url)` | `<img>` with relative path | All readers |
| `> blockquote` | `<blockquote>` | All readers |
| `# Heading` | `<h1>` - `<h6>` | All readers, TOC auto-generated |
| `---` (horizontal rule) | `<hr>` | All readers |
| Admonitions (`> [!NOTE]`) | Custom Lua filter transforms to styled `<div>` | All readers |

## CSS theme injection

The Theme Factory skill provides font/color presets. During export, inject a
theme-specific CSS file via `--css theme.css`. The CSS targets pandoc's EPUB3 output
classes:
- `body`, `h1`-`h6`, `p`, `blockquote`, `pre`, `code`, `table`, `figure`, `figcaption`
- `div.note`, `div.warning`, `div.tip` (admonitions via custom filter)

## Failure handling

This project has a **no-fallback** rule. If pandoc exits non-zero or produces a
zero-byte file, fail the export job with the error message. Do not retry with epub-gen
or any other tool. Fix the root cause.

## Dependencies

Install in Daytona sandbox Dockerfile:

```dockerfile
RUN apt-get update && apt-get install -y pandoc && \
    npm install -g @mermaid-js/mermaid-cli
```
