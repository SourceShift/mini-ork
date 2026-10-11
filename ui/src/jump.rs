//! The ⌘J jump palette: reach any run or IDE page from the keyboard.
//!
//! ⌘J is installed with [`App::intercept_keystrokes`] rather than a key binding:
//! `reload_keymaps` clears registered bindings, so an interceptor is the only
//! place a crate-owned global shortcut survives a keymap reload. While the
//! palette is up it captures keystrokes the same way — see
//! `keymap_editor::ui_components::keystroke_input`.

use std::{cmp::Reverse, collections::BTreeMap};

use gpui::{
    AnyWindowHandle, App, Context, DismissEvent, Entity, EventEmitter, FocusHandle, Focusable,
    Keystroke, KeystrokeEvent, Render, Subscription, WeakEntity, Window,
};
use ui::{Divider, prelude::*};
use workspace::{ModalView, MultiWorkspace, Workspace};

use crate::{
    board::Run,
    page,
    palette::{self as p, c},
    store::BoardStore,
};

/// The pages the palette can open, matching the activity bar's order.
const PAGES: [(&str, &str, &str); 11] = [
    ("inbox", "✉", "Inbox — what needs you"),
    ("kanban", "▦", "Board"),
    ("runs", "≡", "Runs"),
    ("verify", "✓", "Verification & safety"),
    ("recipes", "▥", "Recipes, specs & epics"),
    ("autos", "◷", "Automations & scheduling"),
    ("lanes", "$", "Models, lanes & cost"),
    ("learn", "❖", "Learning & memory"),
    ("context", "◫", "Context (ContextNest)"),
    ("nodes", "⬡", "Nodes & sandboxes"),
    ("setup", "⚙", "Setup & health"),
];

/// How many rows the list draws at most, however many match.
const MAX_SHOWN: usize = 60;

pub fn init(cx: &mut App) {
    cx.intercept_keystrokes(|event, window, cx| {
        let keystroke = &event.keystroke;
        if keystroke.key != "j" || !keystroke.modifiers.platform {
            return;
        }
        if keystroke.modifiers.alt || keystroke.modifiers.shift || keystroke.modifiers.control {
            return;
        }
        let Some(multi_workspace) = window.root::<MultiWorkspace>().flatten() else {
            return;
        };
        let workspace = multi_workspace.read(cx).workspace().clone();
        open(&workspace, window, cx);
        cx.stop_propagation();
    })
    .detach();
}

/// Toggle the palette on this workspace (`toggle_modal` closes it on a second ⌘J).
fn open(workspace: &Entity<Workspace>, window: &mut Window, cx: &mut App) {
    let weak = workspace.downgrade();
    workspace.update(cx, |workspace, cx| {
        let store = BoardStore::for_workspace(&cx.entity(), cx);
        workspace.toggle_modal(window, cx, move |window, cx| {
            JumpPalette::new(weak, store, window, cx)
        });
    });
}

#[derive(Clone)]
enum Target {
    Run { id: String, title: String },
    Page { key: &'static str },
}

#[derive(Clone)]
struct Item {
    target: Target,
    /// Text the query fuzzy-matches against.
    haystack: String,
    label: String,
    detail: String,
    mark: String,
    colour: u32,
}

/// The modal: a query line, the ranked rows, and a footer hint. One row per
/// board run (newest first) plus one per page.
pub struct JumpPalette {
    workspace: WeakEntity<Workspace>,
    window_handle: AnyWindowHandle,
    store: Option<Entity<BoardStore>>,
    focus_handle: FocusHandle,
    query: String,
    selected: usize,
    /// Keystrokes are captured only while the palette is alive.
    _keys: Subscription,
    _store: Option<Subscription>,
}

impl JumpPalette {
    fn new(
        workspace: WeakEntity<Workspace>,
        store: Option<Entity<BoardStore>>,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Self {
        let focus_handle = cx.focus_handle();
        let window_handle = window.window_handle();
        let listener = cx.listener(|this, event: &KeystrokeEvent, window, cx| {
            if window.window_handle() != this.window_handle {
                return;
            }
            if this.on_key(&event.keystroke, window, cx) {
                cx.stop_propagation();
            }
        });
        let keys = cx.intercept_keystrokes(listener);
        let store_subscription = store.as_ref().map(|store| cx.observe(store, |_, _, cx| cx.notify()));
        Self {
            workspace,
            window_handle,
            store,
            focus_handle,
            query: String::new(),
            selected: 0,
            _keys: keys,
            _store: store_subscription,
        }
    }

    /// Handle one captured keystroke. Returns whether it was consumed.
    fn on_key(&mut self, keystroke: &Keystroke, window: &mut Window, cx: &mut Context<Self>) -> bool {
        match keystroke.key.as_str() {
            "escape" => {
                cx.emit(DismissEvent);
                true
            }
            "up" => {
                self.move_selection(-1, cx);
                true
            }
            "down" => {
                self.move_selection(1, cx);
                true
            }
            "enter" => {
                self.confirm(window, cx);
                true
            }
            "backspace" => {
                self.query.pop();
                self.selected = 0;
                cx.notify();
                true
            }
            _ => {
                let modifiers = keystroke.modifiers;
                if modifiers.platform || modifiers.control || modifiers.alt {
                    return false;
                }
                match keystroke.key_char.as_deref() {
                    Some(text) if !text.is_empty() && text.chars().all(|c| !c.is_control()) => {
                        self.query.push_str(text);
                        self.selected = 0;
                        cx.notify();
                        true
                    }
                    _ => false,
                }
            }
        }
    }

    fn move_selection(&mut self, delta: i32, cx: &mut Context<Self>) {
        let count = self.matches(cx).len();
        if count == 0 {
            self.selected = 0;
        } else {
            let current = self.selected.min(count - 1) as i32;
            self.selected = (current + delta).rem_euclid(count as i32) as usize;
        }
        cx.notify();
    }

    /// Open the selected row: its run tab, or its page.
    fn confirm(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let matches = self.matches(cx);
        let Some(item) = matches.get(self.selected).cloned() else {
            return;
        };
        let Some(workspace) = self.workspace.upgrade() else {
            return;
        };
        cx.emit(DismissEvent);
        window.defer(cx, move |window, cx| match item.target {
            Target::Run { id, title } => page::open_run(&workspace, id, title, None, window, cx),
            Target::Page { key } => {
                page::open_page(&workspace, key.to_string(), None, BTreeMap::new(), window, cx)
            }
        });
    }

    /// Every row, before filtering: the board's runs (newest first), then pages.
    fn all_items(&self, cx: &App) -> Vec<Item> {
        let mut items = Vec::new();
        if let Some(board) = self.store.as_ref().and_then(|store| store.read(cx).board.clone()) {
            let mut runs: Vec<&Run> = board.runs.iter().collect();
            runs.sort_by_key(|run| Reverse(run.started_at.unwrap_or(0)));
            for run in runs {
                let (mark, colour) = p::state_mark(&run.state);
                let title = run.display_title();
                let detail = if run.recipe.is_empty() {
                    run.id.clone()
                } else {
                    format!("{} · {}", run.recipe, run.id)
                };
                items.push(Item {
                    haystack: format!("{title} {} {}", run.recipe, run.id),
                    target: Target::Run {
                        id: run.id.clone(),
                        title: title.clone(),
                    },
                    label: title,
                    detail,
                    mark: mark.to_string(),
                    colour,
                });
            }
        }
        for (key, glyph, label) in PAGES {
            items.push(Item {
                haystack: format!("{label} {key}"),
                target: Target::Page { key },
                label: label.to_string(),
                detail: format!("page · {key}"),
                mark: glyph.to_string(),
                colour: p::MUTED,
            });
        }
        items
    }

    /// `all_items` filtered by the query and ranked best match first.
    fn matches(&self, cx: &App) -> Vec<Item> {
        let all = self.all_items(cx);
        let query = self.query.trim();
        if query.is_empty() {
            return all;
        }
        let mut scored: Vec<(i32, usize)> = all
            .iter()
            .enumerate()
            .filter_map(|(index, item)| fuzzy_score(&item.haystack, query).map(|score| (score, index)))
            .collect();
        scored.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        scored.into_iter().map(|(_, index)| all[index].clone()).collect()
    }
}

impl Focusable for JumpPalette {
    fn focus_handle(&self, _cx: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl EventEmitter<DismissEvent> for JumpPalette {}

impl ModalView for JumpPalette {
    fn fade_out_background(&self) -> bool {
        true
    }
}

impl Render for JumpPalette {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let matches = self.matches(cx);
        if self.selected >= matches.len() {
            self.selected = matches.len().saturating_sub(1);
        }
        let mono = p::mono_font(cx);
        let mut list = v_flex().p_1().gap(rems_from_px(1_f32));
        for (index, item) in matches.iter().take(MAX_SHOWN).enumerate() {
            let selected = index == self.selected;
            list = list.child(
                h_flex()
                    .id(("mini-ork-jump-item", index))
                    .h(rems_from_px(26_f32))
                    .px_2()
                    .gap_2()
                    .items_center()
                    .cursor_pointer()
                    .when(selected, |this| this.bg(c(p::ROW_SELECTED)))
                    .hover(|this| this.bg(c(p::HOVER)))
                    .child(div().w_3().flex_none().text_color(c(item.colour)).child(item.mark.clone()))
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .truncate()
                            .text_size(rems_from_px(12.5_f32))
                            .text_color(c(p::TEXT))
                            .child(item.label.clone()),
                    )
                    .child(
                        div()
                            .flex_none()
                            .font(mono.clone())
                            .text_size(rems_from_px(10.5_f32))
                            .text_color(c(p::SUB))
                            .child(item.detail.clone()),
                    )
                    .on_click(cx.listener(move |this, _, window, cx| {
                        this.selected = index;
                        this.confirm(window, cx);
                    })),
            );
        }
        let query = if self.query.is_empty() {
            div()
                .text_size(rems_from_px(13_f32))
                .text_color(c(p::SUB))
                .child("Jump to a run or page…")
                .into_any_element()
        } else {
            div()
                .text_size(rems_from_px(13_f32))
                .text_color(c(p::TEXT))
                .child(self.query.clone())
                .into_any_element()
        };
        let footer = h_flex()
            .px_2p5()
            .py_1p5()
            .gap_2p5()
            .border_t_1()
            .border_color(c(p::BORDER))
            .font(mono)
            .text_size(rems_from_px(11_f32))
            .text_color(c(p::SUB))
            .child("↵ open")
            .child("esc close")
            .child("↑↓ move");
        v_flex()
            .id("mini-ork-jump")
            .track_focus(&self.focus_handle)
            .key_context("MiniOrkJump")
            .w(rems_from_px(560_f32))
            .border_1()
            .border_color(c(p::BORDER2))
            .bg(c(p::CHROME))
            .text_color(c(p::TEXT))
            .overflow_hidden()
            .child(
                h_flex()
                    .px_2p5()
                    .py_2()
                    .gap_2()
                    .items_center()
                    .child(div().flex_none().text_size(rems_from_px(12.5_f32)).text_color(c(p::BLUE)).child("⌘J"))
                    .child(div().flex_1().min_w_0().truncate().child(query)),
            )
            .child(Divider::horizontal().flex_none())
            .child(list.id("mini-ork-jump-list").max_h(rems_from_px(340_f32)).overflow_y_scroll())
            .child(footer)
    }
}

/// A crude subsequence match: every char of `needle` appears in `haystack` in
/// order. Higher scores are better; `None` means no match at all.
fn fuzzy_score(haystack: &str, needle: &str) -> Option<i32> {
    let hay: Vec<char> = haystack.to_lowercase().chars().collect();
    let needle: Vec<char> = needle.to_lowercase().chars().collect();
    if needle.is_empty() {
        return Some(0);
    }
    let mut score = 0i32;
    let mut start = 0usize;
    let mut previous: Option<usize> = None;
    for character in needle {
        let position = hay[start..].iter().position(|c| *c == character)? + start;
        if position > 0 && previous == Some(position - 1) {
            score += 8;
        }
        if position == 0 || !hay[position - 1].is_alphanumeric() {
            score += 6;
        }
        score -= (position - start) as i32;
        previous = Some(position);
        start = position + 1;
    }
    Some(score - (hay.len() as i32) / 4)
}
