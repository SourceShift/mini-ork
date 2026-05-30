---
name: data-testid-naming
description: Pick a semantic data-testid that follows the project convention `feature-component-element`. Invoke whenever you're about to write a `data-testid="..."` attribute on a JSX element that humans will target in tests.
---

# data-testid naming

Project rule (CLAUDE.md): every interactive React element has
`data-testid="feature-component-element"`. The dashboard's babel plugin
auto-injects ordinal ids (e.g. `TicketsPanel-div-4`) for elements you forget,
but **anything tests target should be hand-named** so test failures are
self-documenting.

## Format

```
<feature>-<component>-<element>[-<modifier>]
```

- **feature** — kebab-case area name. e.g. `comms`, `bps`, `tickets`,
  `reader`, `library`.
- **component** — the React component or sub-component. e.g. `drawer`,
  `card`, `filter`, `pill`, `controls`.
- **element** — the role inside the component. e.g. `close`, `refresh`,
  `submit`, `search`, `status-active`.
- **modifier** — optional. The dynamic part that distinguishes one row from
  another: an id, a status string, a stable key. Use sparingly.

## Examples

| ✅ Good | ❌ Bad |
|---|---|
| `comms-drawer-close` | `close-btn` (no feature/component context) |
| `comms-drawer-refresh` | `comms-refresh-button` (skipped component) |
| `bps-filter-status-active` | `filter-active` (where? which filter?) |
| `bps-filter-priority-1` | `priority-button-1` (auto-id-style) |
| `ticket-card-${ticket_id}` | `card-7` (ordinal — fragile) |
| `tab-comms` | `nav-button-2` (which tab?) |

## When dynamic values are involved

Use a stable identifier (uuid, slug, status name) — not array index:

```tsx
{/* ✅ stable — survives reorder */}
<div data-testid={`ticket-card-${t.ticket_id}`} />

{/* ❌ fragile — adding a sibling shifts everything */}
<div data-testid={`ticket-card-${idx}`} />
```

## When the auto-injection plugin is enough

If the element is purely decorative or laid out once and tests will never
assert on it (a wrapper `<div>`, a spacer, a header `<h2>`), let the
`babel-plugin-auto-testid` plugin handle it. Manual ids should appear on:

- Buttons / inputs / selects / textareas
- Anchors that act like buttons
- Cards or rows tests will iterate over (use the row's stable id)
- Elements asserting state (e.g. status badge, validation chip)

## Verification

Before committing:

```bash
# All your new interactive elements should be findable by their hand-name
grep -nE 'data-testid="[a-z]+-[a-z]+-[a-z]+' frontend/src/path/to/your/file.tsx
```
