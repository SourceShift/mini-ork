//! Selectable, copyable text: every reading surface in the IDE (page text,
//! cells, values, notes, code, the agent stream, errors) renders through Zed's
//! plain-text `Markdown` element, so it can be selected with the mouse and
//! copied with cmd-C or the right-click menu.
//!
//! One `Markdown` entity per text, kept in a per-view cache keyed by the
//! caller; texts not used in a render are dropped after it.

use std::{
    cell::RefCell,
    collections::{HashMap, HashSet},
};

use gpui::{
    AnyElement, App, AppContext as _, Context, Div, Entity, FontStyle, FontWeight, Hsla, InteractiveElement as _,
    IntoElement as _, ScrollHandle, SharedString, Stateful, StatefulInteractiveElement as _, TextOverflow, TextStyle,
    WhiteSpace, px, relative,
};
use markdown::{Markdown, MarkdownElement, MarkdownStyle};
use ui::rems_from_px;

use crate::palette as p;

#[derive(Default)]
pub struct Texts {
    map: RefCell<HashMap<String, (SharedString, Entity<Markdown>)>>,
    used: RefCell<HashSet<String>>,
    scrolls: RefCell<HashMap<String, ScrollHandle>>,
}

impl Texts {
    /// Call before building a render; `end` after it drops unused texts.
    pub fn begin(&self) {
        self.used.borrow_mut().clear();
    }

    pub fn end(&self) {
        let used = self.used.borrow();
        self.map.borrow_mut().retain(|key, _| used.contains(key));
        self.scrolls.borrow_mut().retain(|key, _| used.contains(key));
    }

    /// `el` as a scroll box inside another scroll area (the stream, a page):
    /// the wheel scrolls this box while it can move and reaches the outer area
    /// only at this box's ends or when its content fits.
    pub fn nested_scroll(&self, key: impl AsRef<str>, el: Stateful<Div>) -> Stateful<Div> {
        let key = format!("scroll:{}", key.as_ref());
        self.used.borrow_mut().insert(key.clone());
        let handle = self.scrolls.borrow_mut().entry(key).or_default().clone();
        let probe = handle.clone();
        el.track_scroll(&handle).on_scroll_wheel(move |_, _, cx| {
            // Runs after this box's own scroll listener added the delta and
            // before layout clamps it: still inside [-max, 0] means this box
            // took the scroll, so the outer area must not move too.
            let max = probe.max_offset().y;
            let y = probe.offset().y;
            if max > px(0.) && y <= px(0.) && y >= -max {
                cx.stop_propagation();
            }
        })
    }

    fn entity<T: 'static>(&self, key: &str, text: &str, cx: &mut Context<T>) -> Entity<Markdown> {
        self.entity_with(key, text, false, cx)
    }

    fn entity_with<T: 'static>(&self, key: &str, text: &str, rich: bool, cx: &mut Context<T>) -> Entity<Markdown> {
        self.used.borrow_mut().insert(key.to_string());
        let existing = self.map.borrow().get(key).cloned();
        match existing {
            Some((current, markdown)) if current.as_ref() == text => markdown,
            Some((_, markdown)) => {
                let text: SharedString = text.to_string().into();
                markdown.update(cx, |markdown, cx| markdown.reset(text.clone(), cx));
                self.map.borrow_mut().insert(key.to_string(), (text, markdown.clone()));
                markdown
            }
            None => {
                let text: SharedString = text.to_string().into();
                let markdown = if rich {
                    cx.new(|cx| Markdown::new(text.clone(), None, None, cx))
                } else {
                    cx.new(|cx| Markdown::new_text(text.clone(), cx))
                };
                self.map.borrow_mut().insert(key.to_string(), (text, markdown.clone()));
                markdown
            }
        }
    }

    /// `text` parsed as markdown (headings, lists, code), selectable.
    pub fn markdown<T: 'static>(&self, key: impl AsRef<str>, text: impl AsRef<str>, look: Look, cx: &mut Context<T>) -> AnyElement {
        let key = format!("md:{}", key.as_ref());
        let markdown = self.entity_with(&key, text.as_ref(), true, cx);
        element(markdown, look, cx)
    }

    /// `text` as a selectable element with this look.
    pub fn text<T: 'static>(&self, key: impl AsRef<str>, text: impl AsRef<str>, look: Look, cx: &mut Context<T>) -> AnyElement {
        let markdown = self.entity(key.as_ref(), text.as_ref(), cx);
        element(markdown, look, cx)
    }
}

#[derive(Clone, Copy)]
pub struct Look {
    pub size: f32,
    pub colour: Hsla,
    pub mono: bool,
    pub weight: FontWeight,
    pub nowrap: bool,
    pub italic: bool,
}

impl Look {
    pub fn new(size: f32, colour: Hsla) -> Self {
        Self { size, colour, mono: false, weight: FontWeight::NORMAL, nowrap: false, italic: false }
    }

    pub fn mono(mut self, on: bool) -> Self {
        self.mono = on;
        self
    }

    pub fn bold(mut self, on: bool) -> Self {
        if on {
            self.weight = FontWeight::SEMIBOLD;
        }
        self
    }

    /// One line, cut with an ellipsis.
    pub fn nowrap(mut self) -> Self {
        self.nowrap = true;
        self
    }

    pub fn italic(mut self, on: bool) -> Self {
        self.italic = on;
        self
    }
}

fn element(markdown: Entity<Markdown>, look: Look, cx: &App) -> AnyElement {
    let settings = theme::theme_settings(cx);
    let font = if look.mono { settings.buffer_font(cx).clone() } else { settings.ui_font(cx).clone() };
    let mut text = TextStyle {
        color: look.colour,
        font_family: font.family.clone(),
        font_features: font.features.clone(),
        font_fallbacks: font.fallbacks.clone(),
        font_size: rems_from_px(look.size).into(),
        font_weight: look.weight,
        line_height: relative(1.5),
        ..Default::default()
    };
    if look.nowrap {
        text.white_space = WhiteSpace::Nowrap;
        text.text_overflow = Some(TextOverflow::Truncate("…".into()));
    }
    if look.italic {
        text.font_style = FontStyle::Italic;
    }
    let mut code_block = gpui::StyleRefinement::default();
    code_block.background = Some(p::c(p::BG).into());
    code_block.padding.left = Some(rems_from_px(10_f32).into());
    code_block.padding.right = Some(rems_from_px(10_f32).into());
    code_block.padding.top = Some(rems_from_px(8_f32).into());
    code_block.padding.bottom = Some(rems_from_px(8_f32).into());
    let style = MarkdownStyle {
        base_text_style: text,
        selection_background_color: p::ca(0x74ade859),
        // `MarkdownStyle` wants pixels and this function is handed no window to
        // resolve rems with, so the reader's UI font size is read from the theme
        // instead — the same value `setup_ui_font` gives `Window::set_rem_size`.
        paragraph_spacing: rems_from_px(if look.nowrap { 0_f32 } else { 6_f32 })
            .to_pixels(theme::theme_settings(cx).ui_font_size(cx)),
        code_block,
        inline_code: gpui::TextStyleRefinement {
            background_color: Some(p::c(p::BG)),
            ..Default::default()
        },
        rule_color: p::c(p::BORDER),
        ..Default::default()
    };
    MarkdownElement::new(markdown, style).into_any_element()
}
