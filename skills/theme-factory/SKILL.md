---
name: theme-factory
description: Toolkit for styling exported book artifacts with professional themes. Each theme includes a cohesive color palette, complementary font pairings, and format-specific styling (EPUB CSS, DOCX reference template). Apply to EPUB, DOCX, and future PDF exports. Based on ComposioHQ/awesome-claude-skills theme-factory.
license: MIT
metadata:
  author: agentflow
  version: '1.0.0'
  based_on: ComposioHQ/awesome-claude-skills theme-factory
---

# Theme Factory Skill

Provides a curated collection of professional font and color themes for book exports.
Each theme includes color palettes, font pairings, and format-specific styling rules.

## Purpose

To apply consistent, professional styling to exported books (EPUB, DOCX). Each theme
includes:
- A cohesive color palette with hex codes
- Complementary font pairings for headers and body text
- Distinct visual identity suitable for different contexts and audiences
- EPUB CSS output
- DOCX reference-doc styling guidance

## Themes Available (3 shipped, 10 available upstream)

The following 3 themes are shipped for Track A (T4). The full 10-theme set is available
from upstream `ComposioHQ/awesome-claude-skills/theme-factory/themes/`.

1. **Modern Minimalist** — Clean and contemporary grayscale for tech books
2. **Ocean Depths** — Professional and calming maritime theme for academic books
3. **Tech Innovation** — Bold and modern high-contrast theme for AI/ML content

Additional upstream themes (v2): Sunset Boulevard, Forest Canopy, Golden Hour, Arctic
Frost, Desert Rose, Botanical Garden, Midnight Galaxy.

## Theme Details

Each theme is defined in `themes/<theme-name>.md` with:
- Color palette (primary, accent, background, text) with hex codes
- Font pairings (headers + body)
- Best-use guidance

## Application Process

### EPUB

Read the theme file, generate a `theme.css` for pandoc's `--css` flag:

```css
/* Generated from theme-factory preset */
body { font-family: 'DejaVu Sans', sans-serif; color: #1a2332; }
h1, h2, h3 { font-family: 'DejaVu Sans Bold', sans-serif; color: #2d8b8b; }
a { color: #0066ff; }
pre { background: #1e1e1e; color: #ffffff; }
blockquote { border-left: 4px solid #2d8b8b; }
```

### DOCX

Generate a `template.docx` (reference document) with theme fonts/colors set in Word
styles, then pass via `--reference-doc template.docx` during pandoc conversion.

### Theme injection in export pipeline

The `themeApplier` service reads the theme preset, generates format-specific styling,
and passes it to the builder (`epubBuilder` / `docxBuilder`). T4 implements this full
integration.

## Failure handling

No fallback. If a theme preset is missing or malformed, fail the export job. Do not
silently apply a default theme.
