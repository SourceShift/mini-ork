//! Small Orca-styled building blocks shared by the new pages. No state, no
//! app context; the callers compose them with their own data.

use gpui::{AnyElement, ClickEvent, Div, SharedString, Window, div, px, App};
use serde_json::Value;
use ui::{Chip, prelude::*};

use crate::palette::{self as p, c, state_mark};

/// Frames per second for repeating mini-ork UI animations. GPUI re-renders an
/// animation on EVERY frame by default (`Animation::max_fps: None`) and any
/// running animation repaints the whole window each time — with the board's
/// full (not yet virtualized) tree that pinned a core (ram-sentinel killed the
/// app at 137% CPU). A single-digit frame rate keeps spinners legible at a
/// fraction of the cost.
pub(crate) const ANIM_FPS: f32 = 8.;

/// A repeating, phase-synced rotate animation throttled to [`ANIM_FPS`].
///
/// Drop-in for `ui`'s `with_rotate_animation` / `with_keyed_rotate_animation`,
/// which cannot be throttled (they hard-code `repeat_synced()` without a
/// `max_fps`).
pub(crate) trait ThrottledSpinExt:
    gpui::AnimationExt + ui::Transformable + Sized
{
    fn with_throttled_rotate(
        self,
        id: impl Into<gpui::ElementId>,
        period_s: u64,
    ) -> gpui::AnimationElement<Self> {
        self.with_animation(
            id,
            gpui::Animation::new(std::time::Duration::from_secs(period_s))
                .repeat_synced()
                .with_max_fps(ANIM_FPS),
            |el, delta| el.transform(gpui::Transformation::rotate(gpui::percentage(delta))),
        )
    }
}

impl<T: gpui::AnimationExt + ui::Transformable + Sized> ThrottledSpinExt for T {}

/// An Orca card: 1px border, CARD background, 12px inset. Square corners, like
/// Zed's own boxes — the radius is what made these read as foreign.
pub fn card() -> Div {
    div().bg(c(p::CARD)).border_1().border_color(c(p::BORDER)).p_3()
}

/// A state pill: the state word, uppercase at PILL size, in the state colour,
/// on that colour at 15% alpha. Orca's "needs you" / "done" / "working" tags,
/// drawn by `ui::Chip` so the pill's metrics and radius are Zed's.
pub fn state_pill(state: &str) -> AnyElement {
    let (_, colour) = state_mark(state);
    let tint = c(colour);
    Chip::new(state.to_ascii_uppercase())
        .label_size(LabelSize::XSmall)
        .label_color(Color::Custom(tint))
        .bg_color(tint.opacity(0.15))
        .border_color(tint.opacity(0.25))
        .height(px(18.))
        .into_any_element()
}

/// Orca's status glyph for a run state. Working nodes are a ring spinner
/// (delegated to `crate::sidebar::status_mark` so the animation id stays
/// unique); everything else is the static `state_mark` glyph.
pub fn status_mark(state: &str, size: f32, key: &str) -> AnyElement {
    if matches!(state, "working" | "running") {
        return crate::sidebar::status_mark(state, key);
    }
    let (glyph, colour) = state_mark(state);
    div()
        .flex_none()
        .text_size(rems_from_px(size))
        .text_color(c(colour))
        .child(SharedString::from(glyph))
        .into_any_element()
}

/// A section heading: the title in 11px semibold MUTED, with a count pill
/// (rendered by `state_pill` so it matches the rest of the design).
pub fn section_head(title: &str, count: Option<usize>) -> Div {
    h_flex()
        .flex_none()
        .gap_1p5()
        .items_center()
        .child(
            Label::new(title.to_string())
                .size(LabelSize::XSmall)
                .weight(gpui::FontWeight::SEMIBOLD)
                .color(Color::Muted),
        )
        .when_some(count, |this, n| this.child(state_pill(&n.to_string())))
}

/// A primary action button next to a `▾` caret. Today the secondary actions
/// render as ghost buttons next to the caret; a future iteration will group
/// them under a real popover/dropdown menu (TODO) so the row stays a single
/// chip.
pub fn split_button(
    id: impl Into<SharedString>,
    primary: (impl Into<SharedString>, &str),
    on_primary: Option<Box<dyn Fn(&ClickEvent, &mut Window, &mut App) + 'static>>,
    more: Vec<(SharedString, Box<dyn Fn(&ClickEvent, &mut Window, &mut App) + 'static>)>,
) -> AnyElement {
    let id = id.into();
    let (label, kind) = primary;
    let primary_button = crate::palette::button(
        SharedString::from(format!("{id}-primary")),
        label,
        kind,
        24.,
        12.,
        on_primary,
    );
    if more.is_empty() {
        return primary_button;
    }
    let caret = crate::palette::button(
        SharedString::from(format!("{id}-caret")),
        SharedString::from("▾"),
        "ghost",
        24.,
        12.,
        None,
    );
    // TODO: replace these ghost buttons with a real `▾` menu (`popover` or
    // `dropdown_menu`) so the row stays a single chip. Until that lands the
    // secondary actions appear inline next to the caret.
    let extras = more.into_iter().enumerate().map(|(i, (label, handler))| {
        crate::palette::button(
            SharedString::from(format!("{id}-more-{i}")),
            label,
            "ghost",
            24.,
            12.,
            Some(handler),
        )
    });
    h_flex()
        .flex_none()
        .gap_1()
        .items_center()
        .child(primary_button)
        .child(caret)
        .children(extras)
        .into_any_element()
}

// ── diff helpers (moved from graph.rs) ────────────────────────────────────
//
// A run's diff is one big text blob; both the run-page `files`/`story` blocks
// (`sections.rs`) and the run-graph's file rows (`graph.rs`) need to read it.
// Splitting it per file and colouring the lines used to live on the graph,
// duplicated would be tempting; moved here once so both callers import.

/// A unified diff split per file: a section starts at `diff --git`, or at a
/// `--- ` header that no `diff --git` line introduced (plain patches). A bare
/// `--- ` only counts as a header when the next line is `+++ `: inside a hunk,
/// a removed line that began with `-- ` (an SQL or Lua comment) also reads
/// `--- …` and must not split the file.
pub(crate) fn diff_sections(diff: &str) -> Vec<String> {
    let mut sections = Vec::new();
    let mut current = String::new();
    let mut in_git_header = false;
    let mut lines = diff.lines().peekable();
    while let Some(line) = lines.next() {
        let git = line.starts_with("diff --git ");
        let plain_header = line.starts_with("--- ")
            && !in_git_header
            && lines.peek().is_some_and(|next| next.starts_with("+++ "));
        let starts = git || plain_header;
        if starts && !current.is_empty() {
            sections.push(std::mem::take(&mut current));
        }
        in_git_header = git
            || (in_git_header
                && ["index ", "new file", "deleted file", "similarity", "rename ", "old mode", "new mode"]
                    .iter()
                    .any(|p| line.starts_with(p)));
        current.push_str(line);
        current.push('\n');
    }
    if !current.is_empty() {
        sections.push(current);
    }
    sections
}

/// The section of `path` (a repo-relative path or a bare file name).
pub(crate) fn section_for(sections: &[String], path: &str) -> Option<String> {
    if path.is_empty() {
        return None;
    }
    let needle = format!("/{}", path.trim_start_matches('/'));
    sections
        .iter()
        .find(|section| {
            section.lines().take(4).any(|line| {
                (line.starts_with("diff --git ") || line.starts_with("--- ") || line.starts_with("+++ "))
                    && (line.contains(&needle) || line.ends_with(&format!(" {path}")))
            })
        })
        .cloned()
}

/// A diff as coloured lines for `code_box` (`+`/add green, `-`/del red,
/// `@@` cyan, `+++`/`---` text, the rest muted).
pub(crate) fn diff_lines(diff: &str, limit: usize) -> Vec<Value> {
    diff.lines()
        .take(limit)
        .map(|l| {
            let colour = if l.starts_with("+++") || l.starts_with("---") {
                "text"
            } else if l.starts_with('+') {
                "green"
            } else if l.starts_with('-') {
                "red"
            } else if l.starts_with("@@") {
                "blue"
            } else {
                "muted"
            };
            serde_json::json!({ "t": l, "c": colour })
        })
        .collect()
}
