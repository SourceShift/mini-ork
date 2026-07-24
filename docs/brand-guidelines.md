# mini-ork Brand Guidelines v1.0

> Last updated: 2026-07-22
> Status: Draft — codifies the shipping design system (`design/mini-ork/app/tokens.css`) and the voice already latent in README + `docs/positioning/`.
> Source of truth. Detail on tokens lives in `design/mini-ork/app/tokens.css`; messaging lives in `docs/positioning/why-mini-ork.md`.

## Quick Reference

| Element | Value |
|---------|-------|
| Primary signal color | Phosphor Green `#4FD1A0` |
| Brand anchor color | Ork Red `#A52828` |
| Surface | Near-black `#080A0B` |
| Primary font | JetBrains Mono |
| Secondary font | Inter |
| Voice | Direct · Evidence-first · Unhyped |
| One-liner | A task operating system for agents. |

---

## 0. Brand Essence

**What mini-ork is:** a task operating system for agents — classify → plan → execute → verify → reflect → improve.

**The core belief the brand exists to express:** *multi-agent review only counts if the reviewers can't all share the same blind spot.* Everything visual and verbal ladders back to heterogeneity-by-construction and executable verification over vendor consensus theater.

**Personality in one image:** a terminal-grade operator console — dense, mono, phosphor-on-black — commanded by a calm red master orc coordinating a fleet of green mini-orc agents. Serious instrument, not a toy; friendly crew, not a threat.

---

## 1. Color Palette

The palette is a **dark-first operator console**. Light-on-near-black is the default; there is no light theme in the shipping system. Colors are *signals*, not decoration — each hue carries a fixed operational meaning.

### Base / Surface

| Name | Hex | RGB | Usage |
|------|-----|-----|-------|
| BG | `#080A0B` | rgb(8,10,11) | App background, deepest layer |
| Panel | `#0D1012` | rgb(13,16,18) | Panels, cards |
| Panel 2 | `#11161A` | rgb(17,22,26) | Raised panel header gradient |
| Panel 3 | `#161D22` | rgb(22,29,34) | Controls, buttons |
| Raised | `#1B242A` | rgb(27,36,42) | Hover / active surface |
| Hairline | `#1A2228` | rgb(26,34,40) | Grid lines, panel borders |
| Edge | `#303C44` | rgb(48,60,68) | Strong borders, focus edges |

### Text

| Name | Hex | RGB | Usage |
|------|-----|-----|-------|
| Text | `#D6DDE2` | rgb(214,221,226) | Primary body / data |
| Text 2 | `#AAB4BB` | rgb(170,180,187) | Secondary |
| Text 3 (muted) | `#7C8890` | rgb(124,136,144) | Captions, muted |
| Text 4 (chrome) | `#56626A` | rgb(86,98,106) | Labels, faint chrome |
| Text 5 (ghost) | `#3C474E` | rgb(60,71,78) | Disabled / ghost |

### Signal — the four operational hues

| Name | Hex | RGB | Meaning (fixed) |
|------|-----|-----|-----------------|
| **Phosphor Green** | `#4FD1A0` | rgb(79,209,160) | OK · data · **heterogeneous** · primary brand signal |
| **Amber** | `#E6B04A` | rgb(230,176,74) | Warn · pending · budget caution |
| **Red** | `#F0584E` | rgb(240,88,78) | Alert · fail · destructive command |
| **Cyan** | `#58B9C9` | rgb(88,185,201) | Link · focus · info |
| Violet | `#9B8CDF` | rgb(155,140,223) | Secondary accent (rare) |

> Rule: never use a signal color decoratively. Green means *good/verified*, red means *failed/destructive*. Miscoloring a passing state red is a brand violation, not just a UI bug.

### Ork Brand Accents

The "creature" palette — desaturated, earthy, used for identity surfaces (logo, hero, marketing), **not** for operational UI signal.

| Name | Hex | RGB | Usage |
|------|-----|-----|-------|
| **Ork Red** | `#A52828` | rgb(165,40,40) | The master orc — primary brand mark color |
| Ork Red Deep | `#6D1A1A` | rgb(109,26,26) | Shadow / gradient partner |
| Ork Green | `#2F7A48` | rgb(47,122,72) | The mini-orc crew |
| Ork Amber | `#B07A2C` | rgb(176,122,44) | Warm accent |

### Model-Family Palette (product-specific)

Heterogeneity-by-construction is the thesis, so **model families get named colors**. Use these consistently anywhere a family is attributed (lane maps, fingerprint views, trajectory).

| Family | Hex | Vendor |
|--------|-----|--------|
| Opus | `#A394E8` | Anthropic |
| Sonnet | `#E0975F` | Anthropic |
| GLM | `#46C0B6` | Zhipu |
| Kimi | `#5F97E6` | Moonshot |
| Codex | `#D4B24F` | OpenAI |
| Gemini | `#74C463` | Google |
| None / unassigned | `#5A666E` | — |

### Accessibility

All defaults are light-on-near-black and clear AAA comfortably:

| Pair | Approx. ratio | Level |
|------|---------------|-------|
| Text `#D6DDE2` on BG `#080A0B` | ~13:1 | AAA |
| Phosphor Green `#4FD1A0` on BG | ~10:1 | AAA |
| Amber `#E6B04A` on BG | ~10:1 | AAA |
| Cyan `#58B9C9` on BG | ~8:1 | AAA (large/AA normal) |

- Text 4/5 (chrome, ghost) are intentionally low-contrast **decorative** chrome — never place essential information there.
- Focus is a 2px cyan ring on a BG-colored inset (`:focus-visible`), never removed.
- Motion respects `prefers-reduced-motion` (CRT scanline, ping, shimmer all disable).

---

## 2. Typography

**Mono-first.** mini-ork is an operator instrument; the default typeface is monospace so data, IDs, costs, and code align on a grid. Inter is the *secondary* sans for prose surfaces (marketing, long-form docs).

### Font Stack

```css
--mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
--sans: 'Inter', system-ui, -apple-system, sans-serif;
```

Load: `JetBrains Mono` weights 400–800, `Inter` 400–600.
Feature settings on body: `'zero', 'ss02'` (slashed zero, disambiguated glyphs), tracking `-0.01em`.

### Type Scale (console)

| Token | Size | Typical use |
|-------|------|-------------|
| micro | 9.5px | Eyebrows, tags, family chips |
| xs | 10.5px | Labels, table headers |
| sm | 11.5px | Table cells, secondary |
| base | 12px | Body / default |
| md | 13px | Emphasis body |
| lg | 16px | Panel titles |
| xl | 21px | Section headers |
| 2xl | 30px | View headers |
| display | 40px | Hero numerals |

### Long-form / marketing scale (Inter)

| Element | Weight | Size (Desktop / Mobile) | Line height |
|---------|--------|-------------------------|-------------|
| H1 | 700 | 40px / 30px | 1.2 |
| H2 | 600 | 30px / 24px | 1.25 |
| H3 | 600 | 21px / 18px | 1.3 |
| Body | 400 | 16px / 16px | 1.55 |
| Small | 400 | 14px / 14px | 1.5 |

### Rules

- Numbers use tabular figures (`font-variant-numeric: tabular-nums`) so costs and counts don't jitter.
- Labels/eyebrows are **UPPERCASE, letter-spaced** (`0.13em`–`0.18em`), muted chrome color.
- Prose (Inter) drops the tracking (`letter-spacing: 0`).

---

## 3. Logo & Mark

### Concept

Two-part identity: the **wordmark** `mini-ork` (lowercase, mono) and the **mark** — a red master-orc glyph, optionally over a fleet of small green orc dots (the crew).

### Variants

| Variant | Use case |
|---------|----------|
| Wordmark | `mini-ork` lowercase, JetBrains Mono 700, Text color — headers, docs |
| Bracket wordmark | `[mini-ork]` — terminal/console contexts, favicons of type |
| Mark (master orc) | Ork Red `#A52828` glyph — app icon, avatar, small spaces |
| Crew lockup | Red master orc + green mini-orc dots — hero, marketing |
| Monochrome | Single Text-color version for limited-palette contexts |

### Clear space & minimum size

- Clear space = height of the mark on all sides.
- Digital wordmark: 96px min width. Mark: 24px min.

### Don'ts

- Don't render the wordmark in Title Case or ALL CAPS — it is always lowercase `mini-ork`.
- Don't recolor the master orc outside Ork Red / monochrome.
- Don't map the ork "creature" reds/greens onto operational UI signals (green stays phosphor `#4FD1A0` in-app).
- Don't add shadows, bevels, or gradients to the wordmark.
- Don't hyphen-break or space the name (`mini ork`, `MiniOrk`, `Mini-Ork` are all wrong).

---

## 4. Voice & Tone

mini-ork's voice is the one already in the README and positioning docs: **an expert operator who leads with receipts and refuses to oversell.** It sounds like an engineer you trust because they tell you what *doesn't* work yet.

### Personality

| Trait | Meaning |
|-------|---------|
| **Direct** | Lead with the claim. Short sentences. Fragments allowed in-product. |
| **Evidence-first** | Every strong claim carries a receipt — a command, a ρ value, a paper, an exit code. "The harshness table is the receipts." |
| **Unhyped** | State limits plainly. A "Where we're honest about what it isn't (yet)" section is a feature, not a weakness. |
| **Precise** | Use the exact term (`verifier`, `gradient`, `lane`, `task class`). Don't fuzz the mechanism. |
| **Dry** | Wit is fine; exclamation and marketing gloss are not. |

### Voice Chart

| Trait | We Are | We Are Not |
|-------|--------|------------|
| Direct | "Pass/fail is an exit code, not an opinion." | "We help streamline your agentic workflows." |
| Evidence-first | "ρ = 0.05–0.25 across 1–4 agents (Rajan 2025)." | "Studies show diversity improves results." |
| Unhyped | "Self-evolution is class-restricted — don't oversell." | "Fully autonomous self-improving AI." |
| Precise | "The coalition gate hard-blocks same-family degeneration." | "Smart routing keeps quality high." |
| Confident | "This is the test we built mini-ork to pass." | "We think this might help, maybe." |

### Tone by Context

| Context | Tone | Example |
|---------|------|---------|
| README / marketing | Confident, benefit + receipt | "Every run starts smarter — and cheaper — than the last." |
| Docs | Instructional, exact | "Run `mini-ork validate` before any real run." |
| CLI / status output | Terse, mono, signal-colored | `verify → PASS (rc=0)` / `verify → vacuous` |
| Error messages | Calm, actionable, no blame | "No verifiers declared — marked `vacuous`, not `success`. Add `success_verifiers` in `artifact_contract.yaml`." |
| Success | Brief, factual | "Promoted candidate v0.3.1 — beat benchmark under budget." |
| Limitations | Plain, owning the gap | "Krippendorff α gate — not built yet. v0.3 candidate." |

### Prohibited Terms

| Avoid | Reason |
|-------|--------|
| Revolutionary / game-changing | Hype; we lead with receipts |
| Seamless / effortless | Vague; the product is an *instrument*, it rewards operators |
| Leverage (verb) | Say "use" |
| Synergy / holistic | Corporate jargon |
| Best-in-class / world-class | Unprovable claim |
| "AI-powered" as a value prop | Table stakes; describe the *mechanism* instead |
| "Fully autonomous" (unqualified) | Contradicts the class-restricted self-evolution honesty |

### Naming discipline

Always lowercase **mini-ork**, even at the start of a sentence in body copy where possible; if a sentence must start with it, prefer restructuring. Never "Mini-Ork," "MiniOrk," or "MINI-ORK."

---

## 5. Imagery & Illustration

### The creature world

- **Master orc:** friendly, modern, calm — a *coordinator*, not a warrior. Ork Red `#A52828`. Commands, doesn't fight.
- **Mini-orcs (crew):** small, green `#2F7A48`, collaborative — each is an agent/lane. Shown working in connected bubble-workspaces.
- **Setting:** clean, sci-fi operator environment (spaceship / command deck), never grimdark. The tone is competent and peaceful, matching "serious instrument, friendly crew."

### Console / UI imagery

- Hairline grids, phosphor-on-black, subtle CRT scanline + vignette (opacity ~0.5, `mix-blend-mode: overlay`) — always subtle, never at the cost of legibility.
- Data over ornament: real tables, real trajectories, tabular numerals.

### Illustration style

- Flat with restrained gradients; 2px consistent stroke.
- Palette-locked (creature palette for characters, signal palette for data).
- Corners 3px (matches `--rad`).

### Icons

- Outlined, ~24px grid, ~1.5px stroke, minimal fill.
- Sharp, small, mono-adjacent — they live in a dense console.

---

## 6. Design Components

Radii are **sharp** — this is an instrument, not a consumer app.

| Element | Radius |
|---------|--------|
| Base (`--rad`) | 3px |
| Slightly raised (`--rad-2`) | 5px |
| Tags | 2px |
| Pills / family chips | 2px |

### Buttons

| Type | Background | Text | Border |
|------|------------|------|--------|
| Default | Panel 3 `#161D22` | Text 2 | Hairline 2 |
| Ghost | transparent | Text 3 | none |
| Command (destructive) | Red wash `rgba(240,88,78,.11)` | Red `#F0584E` | Red `rgba(240,88,78,.3)` |

Buttons are UPPERCASE, 24px tall, mono, letter-spaced `0.02em`.

### Tags / status

`tag-ok` (green wash), `tag-warn` (amber), `tag-err` (red), `tag-info` (cyan), `tag-mut` (neutral). Meaning is fixed to the signal palette.

### Spacing scale

| Token | Value |
|-------|-------|
| g1 | 4px |
| g2 | 7px |
| g3 | 11px |
| g4 | 16px |
| g5 | 22px |
| g6 | 30px |

---

## 7. Messaging Architecture

### Mission

We give teams a task operating system for agents — classifying, planning, verifying, and remembering work across model families — so every run ships durable, verified artifacts instead of same-vendor consensus theater.

### Vision

A world where "multi-agent" means low-correlation evidence and executable checks — not one model family grading its own homework.

### Value proposition

For engineering teams running agentic work who need results they can trust, mini-ork is a task operating system that dispatches specialized agents across *distinct model families*, gates output through deterministic verifiers, and remembers every run. Unlike single-vendor agent frameworks, review independence is a structural property, not a hopeful prompt.

### Positioning statement

mini-ork is the operating system you build on top of Claude Code (or any single-vendor agent framework) when you want to pass the detection-fingerprint test — not just draw an agent graph.

### Primary message

**Stop letting one model family grade its own homework.**

### Supporting messages

| Message | Need it addresses | Proof point |
|---------|-------------------|-------------|
| Heterogeneous-family by construction | Independent review | `config/agents.yaml` maps lanes to GLM/Kimi/Codex/Opus/DeepSeek/MiniMax; coalition gate hard-blocks same-family panels |
| Executable verification before opinion | Trustworthy pass/fail | Every recipe ships `verifiers/*.sh`; pass/fail is an exit code; empty verification is `vacuous`, never silent success |
| Persistent trajectory memory | Runs that compound | `state.db` persists runs, gradients, lineage, cost; the planner sees the last N same-class runs |
| Cost governance | Predictable spend | Budget gates halt the queue; cost is a first-class column, not an afterthought |
| Honest about limits | Buyer trust | A published "what it isn't yet" section tied to the roadmap |

### Elevator pitches

- **10-second:** "mini-ork is a task OS for agents — it runs your work across different model families and verifies the output with real tests, not another AI's opinion."
- **30-second:** Add the problem: single-vendor agent frameworks let one model family review its own work, so you get consensus theater. mini-ork enforces family diversity, gates every artifact through executable verifiers, and remembers every run so the next one is smarter and cheaper.
- **60-second:** Add the receipts — Rajan 2025's ρ=0.05–0.25, the coalition gate, `state.db` trajectory metrics, and the class-restricted self-evolution honesty.

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-07-22 | Initial guidelines — codifies `design/mini-ork/app/tokens.css` + README/positioning voice into a single source of truth |
