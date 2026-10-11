//! The mini-ork IDE design's palette, sizes and small building blocks.
//!
//! Views name design colours (`BG`, `TEXT`, `GREEN`, …); `c` / `ca` return
//! them. Orca's neutral dark values are the default. Setting
//! `MINI_ORK_PALETTE=theme` opts out and maps every key onto the ACTIVE Zed
//! theme (editor/panel backgrounds, borders, text levels, status and syntax
//! colours), so the IDE follows any theme, light or dark, and re-colours when
//! the theme changes. Pages name colours rather than sending hex — see
//! `mini_ork/ide_pages/spec.py`.

use std::{collections::HashMap, sync::RwLock};

use gpui::{AnyElement, App, ClickEvent, Div, Font, Hsla, Window, px, rgb};
use theme::ActiveTheme as _;
use ui::{TintColor, prelude::*};

pub const BG: u32 = 0x282c33;
pub const CHROME: u32 = 0x2f343e;
pub const BORDER: u32 = 0x363c46;
pub const BORDER2: u32 = 0x464b57;
// One step off BORDER so the theme map can tell hover from a border.
pub const HOVER: u32 = 0x363c47;
pub const SELECTED: u32 = 0x454a56;
pub const ROW_SELECTED: u32 = 0x3b414d;
pub const ROW_RULE: u32 = 0x30353f;
pub const NODE_BG: u32 = 0x353b45;

pub const TEXT: u32 = 0xdce0e5;
pub const BODY: u32 = 0xc8ccd4;
pub const MUTED: u32 = 0xa9afbc;
pub const SUB: u32 = 0x878a98;
pub const DIM: u32 = 0x5f6672;
pub const BLUE: u32 = 0x74ade8;
pub const GREEN: u32 = 0xa1c181;
pub const RED: u32 = 0xd07277;
pub const YELLOW: u32 = 0xdec184;
pub const PURPLE: u32 = 0xb477cf;
pub const CYAN: u32 = 0x6eb4bf;
pub const ORANGE: u32 = 0xc0966b;

/// Literal design greys that views use directly.
const STREAM_BOX: u32 = 0x262a31;
const ELEMENT: u32 = 0x353a44;
const CANVAS: u32 = 0x1f2227;
const SKELETON: u32 = 0x434a56;
const NODE_CARD: u32 = 0x323843;
const NODE_CARD_PENDING: u32 = 0x2a2e36;

/// New Orca surfaces: card / page / sidebar / sidebar-active; an amber unread
/// accent; and the three git-status colours the IDE shows in row marks.
pub const CARD: u32 = 0x171717;
pub const PAGE: u32 = 0x0a0a0a;
pub const SIDEBAR: u32 = 0x1c1c1c;
pub const SIDEBAR_ACTIVE: u32 = 0x2a2a2a;
pub const AMBER: u32 = 0xf59e0b;
pub const GIT_ADD: u32 = 0x81b88b;
pub const GIT_MOD: u32 = 0xe2c08d;
pub const GIT_DEL: u32 = 0xc74e39;

/// Type scale and corner radii the Orca design uses throughout. Text now goes
/// through `ui::Label` and its `LabelSize` scale, so only the sizes with no
/// Zed equivalent stay here.
pub const TITLE: f32 = 13.;
pub const BADGE: f32 = 10.;
pub const PILL: f32 = 9.;
pub const RADIUS_CARD: f32 = 10.;
pub const RADIUS_BTN: f32 = 6.;

/// Design colour → active-theme colour; `None` until `sync_theme` runs.
static THEMED: RwLock<Option<HashMap<u32, Hsla>>> = RwLock::new(None);

/// Rebuild the design → theme map from the active theme. Called at init and
/// whenever the theme changes. The Orca dark look is the default; setting
/// `MINI_ORK_PALETTE=theme` keeps the older behaviour of mapping every design
/// key onto the active Zed theme (an opt-out for users who don't want Orca).
pub fn sync_theme(cx: &App) {
    let theme = cx.theme();
    let colors = theme.colors();
    let status = theme.status();
    let syntax = |name: &str, fallback: u32| -> Hsla {
        theme
            .syntax()
            .style_for_name(name)
            .and_then(|style| style.color)
            .unwrap_or_else(|| rgb(fallback).into())
    };
    // A surface a little darker than `base`, in light and dark themes alike.
    let shade = |base: Hsla, amount: f32| Hsla { l: (base.l - amount).clamp(0., 1.), ..base };
    let orca = std::env::var("MINI_ORK_PALETTE").as_deref() != Ok("theme");
    let map = if orca {
        HashMap::from([
            (BG, rgb(0x0a0a0a).into()),
            (CHROME, rgb(0x171717).into()),
            (BORDER, rgb(0x232323).into()),
            (BORDER2, rgb(0x333333).into()),
            (HOVER, rgb(0x262626).into()),
            (SELECTED, rgb(0x353535).into()),
            (ROW_SELECTED, rgb(0x262626).into()),
            (ROW_RULE, rgb(0x1c1c1c).into()),
            (NODE_BG, rgb(0x1f1f1f).into()),
            (TEXT, rgb(0xfafafa).into()),
            (BODY, rgb(0xd4d4d4).into()),
            (MUTED, rgb(0xa1a1a1).into()),
            (SUB, rgb(0x8b8b8b).into()),
            (DIM, rgb(0x5e5e5e).into()),
            (BLUE, rgb(0x60a5fa).into()),
            (GREEN, rgb(0x34d399).into()),
            (RED, rgb(0xff6568).into()),
            (YELLOW, rgb(0xf0b100).into()),
            (PURPLE, rgb(0xa78bfa).into()),
            (CYAN, rgb(0x22d3ee).into()),
            (ORANGE, rgb(0xff6900).into()),
            (STREAM_BOX, rgb(0x111111).into()),
            (ELEMENT, rgb(0x262626).into()),
            (CANVAS, rgb(0x0a0a0a).into()),
            (SKELETON, rgb(0x262626).into()),
            (NODE_CARD, rgb(0x171717).into()),
            (NODE_CARD_PENDING, rgb(0x121212).into()),
            (CARD, rgb(0x171717).into()),
            (PAGE, rgb(0x0a0a0a).into()),
            (SIDEBAR, rgb(0x1c1c1c).into()),
            (SIDEBAR_ACTIVE, rgb(0x2a2a2a).into()),
            (AMBER, rgb(0xf59e0b).into()),
            (GIT_ADD, rgb(0x81b88b).into()),
            (GIT_MOD, rgb(0xe2c08d).into()),
            (GIT_DEL, rgb(0xc74e39).into()),
        ])
    } else {
        HashMap::from([
            (BG, colors.editor_background),
            (CHROME, colors.panel_background),
            (BORDER, colors.border_variant),
            (BORDER2, colors.border),
            (HOVER, colors.element_hover),
            (SELECTED, colors.element_selected),
            (ROW_SELECTED, colors.background),
            (ROW_RULE, colors.border_variant.opacity(0.6)),
            (NODE_BG, colors.element_background.blend(colors.element_hover.opacity(0.6))),
            (TEXT, colors.text),
            (BODY, colors.text.blend(colors.text_muted.opacity(0.45))),
            (MUTED, colors.text_muted),
            (SUB, colors.text_placeholder),
            (DIM, colors.text_disabled.opacity(0.75)),
            (BLUE, colors.text_accent),
            (GREEN, status.success),
            (RED, status.error),
            (YELLOW, status.warning),
            (PURPLE, syntax("keyword", PURPLE)),
            (CYAN, syntax("type", CYAN)),
            (ORANGE, syntax("number", ORANGE)),
            (STREAM_BOX, shade(colors.editor_background, 0.02)),
            (ELEMENT, colors.element_background),
            (CANVAS, shade(colors.editor_background, 0.04)),
            (SKELETON, colors.border),
            // Run-graph node cards: an element surface, a quieter one while pending.
            (NODE_CARD, colors.element_background.blend(colors.element_hover.opacity(0.4))),
            (NODE_CARD_PENDING, colors.editor_background.blend(colors.element_background.opacity(0.6))),
            (CARD, colors.element_background),
            (PAGE, colors.editor_background),
            (SIDEBAR, colors.panel_background),
            (SIDEBAR_ACTIVE, colors.element_selected),
            (AMBER, status.warning),
            (GIT_ADD, colors.version_control_added),
            (GIT_MOD, colors.version_control_modified),
            (GIT_DEL, colors.version_control_deleted),
        ])
    };
    if let Ok(mut themed) = THEMED.write() {
        *themed = Some(map);
    }
}

/// A design colour, as the active theme draws it (the literal hex when the
/// colour is not part of the design palette).
pub fn c(hex: u32) -> Hsla {
    THEMED
        .read()
        .ok()
        .and_then(|themed| themed.as_ref().and_then(|map| map.get(&hex).copied()))
        .unwrap_or_else(|| rgb(hex).into())
}

/// A design colour with an alpha byte (`0xRRGGBBAA`): the themed colour of
/// `RRGGBB` at that alpha, so tints follow the theme too.
pub fn ca(hex_with_alpha: u32) -> Hsla {
    let alpha = (hex_with_alpha & 0xff) as f32 / 255.;
    let base = c(hex_with_alpha >> 8);
    Hsla { a: base.a * alpha, ..base }
}

/// A colour name from a page (`"green"`, `"fam:sonnet"`, …).
pub fn named(name: &str) -> Hsla {
    if let Some(lane) = name.strip_prefix("fam:") {
        return c(family(lane));
    }
    c(match name {
        "text" => TEXT,
        "body" => BODY,
        "muted" => MUTED,
        "sub" => SUB,
        "dim" => DIM,
        "amber" => AMBER,
        "blue" => BLUE,
        "green" => GREEN,
        "red" => RED,
        "yellow" => YELLOW,
        "purple" => PURPLE,
        "cyan" => CYAN,
        "orange" => ORANGE,
        _ => BODY,
    })
}

/// The colour of a model family, matched loosely on the lane name.
pub fn family(lane: &str) -> u32 {
    let lane = lane.to_ascii_lowercase();
    let pick = [
        ("opus", PURPLE),
        ("sonnet", ORANGE),
        ("haiku", ORANGE),
        ("claude", ORANGE),
        ("glm", CYAN),
        ("zhipu", CYAN),
        ("kimi", BLUE),
        ("moonshot", BLUE),
        ("codex", YELLOW),
        ("gpt", YELLOW),
        ("openai", YELLOW),
        ("deepseek", 0x8f9cf0),
        ("minimax", 0xe08fb0),
        ("gemini", GREEN),
        ("qwen", 0x9fd4a3),
    ];
    pick.iter()
        .find(|(needle, _)| lane.contains(needle))
        .map(|(_, colour)| *colour)
        .unwrap_or(SUB)
}

/// The logo of a model family, matched on the lane name the same loose way
/// `family` matches its colour — so a lane and its mark are decided together.
/// `None` means "no mark ships for this family yet"; the caller draws the
/// coloured dot instead, so an unmapped lane degrades rather than vanishes.
///
/// Only the icons the tree already has. glm/zhipu is the one family with a
/// `family` colour but no mark yet — it falls back to the dot until an SVG
/// lands.
pub fn model_icon(lane: &str) -> Option<IconName> {
    let lane = lane.to_ascii_lowercase();
    let pick = [
        ("opus", IconName::AiClaude),
        ("sonnet", IconName::AiClaude),
        ("haiku", IconName::AiClaude),
        ("claude", IconName::AiClaude),
        ("anthropic", IconName::AiClaude),
        ("codex", IconName::AiOpenAi),
        ("gpt", IconName::AiOpenAi),
        ("openai", IconName::AiOpenAi),
        ("deepseek", IconName::AiDeepSeek),
        ("minimax", IconName::AiMiniMax),
        ("kimi", IconName::AiKimi),
        ("moonshot", IconName::AiKimi),
        ("qwen", IconName::AiQwen),
        ("gemini", IconName::AiGemini),
        ("google", IconName::AiGoogle),
        ("mistral", IconName::AiMistral),
        ("llama", IconName::AiLlamaCpp),
        ("ollama", IconName::AiOllama),
        ("xai", IconName::AiXAi),
        ("grok", IconName::AiXAi),
        ("router", IconName::AiOpenRouter),
    ];
    pick.iter().find(|(needle, _)| lane.contains(needle)).map(|(_, icon)| *icon)
}

/// The provider mark (as Orca draws it) for a model lane, as an asset path.
pub fn model_image(lane: &str) -> Option<&'static str> {
    let lane = lane.to_ascii_lowercase();
    let pick = [
        ("opus", "images/llm/claude.svg"),
        ("sonnet", "images/llm/claude.svg"),
        ("haiku", "images/llm/claude.svg"),
        ("claude", "images/llm/claude.svg"),
        ("anthropic", "images/llm/claude.svg"),
        ("deepseek", "images/llm/deepseek.png"),
        ("glm", "images/llm/zai.png"),
        ("zhipu", "images/llm/zai.png"),
        ("zai", "images/llm/zai.png"),
        ("z-ai", "images/llm/zai.png"),
        ("minimax", "images/llm/minimax.svg"),
        ("kimi", "images/llm/kimi.png"),
        ("moonshot", "images/llm/kimi.png"),
        ("qwen", "images/llm/qwen.png"),
        ("gemini", "images/llm/gemini.png"),
        ("mistral", "images/llm/mistral.png"),
        ("grok", "images/llm/grok.png"),
        ("xai", "images/llm/grok.png"),
    ];
    pick.iter().find(|(needle, _)| lane.contains(needle)).map(|(_, path)| *path)
}

/// A lane's mark at `size` px: Zed's terminal icon for a command lane, else
/// the provider's logo (Orca's set), else Zed's tinted model icon (OpenAI),
/// else a dot in the family colour.
pub fn lane_mark(lane: &str, size: f32) -> AnyElement {
    if matches!(lane.to_ascii_lowercase().as_str(), "shell" | "command" | "cmd" | "") {
        return gpui::svg()
            .path(IconName::Terminal.path())
            .size(rems_from_px(size))
            .flex_none()
            .text_color(c(SUB))
            .into_any_element();
    }
    if let Some(path) = model_image(lane) {
        return gpui::img(path).size(rems_from_px(size)).flex_none().into_any_element();
    }
    if let Some(icon) = model_icon(lane) {
        return gpui::svg()
            .path(icon.path())
            .size(rems_from_px(size))
            .flex_none()
            .text_color(c(family(lane)))
            .into_any_element();
    }
    let dot = (size * 0.5).max(5.);
    div()
        .size(rems_from_px(dot))
        .mx(rems_from_px((size - dot) / 2.))
        .flex_none()
        .rounded_full()
        .bg(c(family(lane)))
        .into_any_element()
}

/// A human name for the worker a step's `lane` names — the label the step
/// cards show beside the mark. Matched loosely (case-insensitive substring),
/// the same way `model_image` matches, so a lane and its label agree.
pub fn lane_label(lane: &str) -> String {
    let lower = lane.to_ascii_lowercase();
    let pick = [
        ("opus", "Claude Opus"),
        ("sonnet", "Claude Sonnet"),
        ("haiku", "Claude Haiku"),
        ("claude", "Claude"),
        ("deepseek", "DeepSeek"),
        ("glm", "GLM (z.ai)"),
        ("zai", "GLM (z.ai)"),
        ("zhipu", "GLM (z.ai)"),
        ("minimax", "MiniMax"),
        ("codex", "OpenAI Codex"),
        ("gpt", "OpenAI Codex"),
        ("openai", "OpenAI Codex"),
        ("kimi", "Kimi"),
        ("moonshot", "Kimi"),
        ("qwen", "Qwen"),
        ("gemini", "Gemini"),
        ("mistral", "Mistral"),
        ("grok", "Grok"),
        ("xai", "Grok"),
        ("shell", "command"),
        ("command", "command"),
        ("cmd", "command"),
    ];
    pick.iter()
        .find(|(needle, _)| lower.contains(needle))
        .map(|(_, label)| (*label).to_string())
        .unwrap_or_else(|| lane.to_string())
}

/// The mark and colour of a run state.
pub fn state_mark(state: &str) -> (&'static str, u32) {
    match state {
        "working" | "running" => ("●", YELLOW),
        "needs_you" | "needs" => ("?", ORANGE),
        "failed" | "error" => ("✗", RED),
        "done" | "published" => ("✓", GREEN),
        _ => ("○", SUB),
    }
}

/// The mark for a callout `tone`, drawn when the section carries no `icon`.
///
/// The engine's `outcome.resolve` computes a real icon (`✋` needs you, `✗`
/// failed, `✓` published, `●` working) and a callout built from it passes that
/// through. This is the fallback for the callouts built without one: before it
/// existed every callout rendered a literal `"?"`, so a red failure and a green
/// note carried the same glyph and the mark told the reader nothing.
pub fn tone_mark(tone: &str) -> &'static str {
    match tone {
        "green" => "✓",
        "red" => "✗",
        "orange" | "yellow" | "amber" => "?",
        "blue" | "cyan" => "●",
        _ => "•",
    }
}

/// RGB-composite blend of two colours, for Orca's "foreground 10% over
/// background" active surfaces. `t = 0` returns `a`; `t = 1` returns `b`.
pub fn mix(a: Hsla, b: Hsla, t: f32) -> Hsla {
    let t = t.clamp(0., 1.);
    a.blend(b.opacity(t))
}

pub fn mono_font(cx: &App) -> Font {
    theme::theme_settings(cx).buffer_font(cx).clone()
}

/// Text at a design size and colour.
///
/// `size` is still a design pixel value — every call site reads as it did —
/// but it is converted to rems, so the type scales with the reader's
/// `ui_font_size` instead of pinning to the pixel grid. `1rem` is 16px at the
/// default size, so nothing moves for a reader who never changed the setting.
pub fn text(content: impl Into<SharedString>, size: f32, colour: u32) -> Div {
    div()
        .text_size(rems_from_px(size))
        .text_color(c(colour))
        .child(content.into())
}

/// Text at a fixed pixel size, for the canvases only.
///
/// [`text`] scales with the reader's UI font size, which is what every view
/// built out of rems wants. The run DAG and the flow map are not built out of
/// rems: they place every box, edge and label at a pixel the layout computed,
/// and scale as a whole through the zoom gesture. Type inside them has to stay
/// on that pixel grid — a label that grew with the font size would sit outside
/// the box that was measured to hold it.
pub fn text_px(content: impl Into<SharedString>, size: f32, colour: u32) -> Div {
    div().text_size(px(size)).text_color(c(colour)).child(content.into())
}

/// A design button, drawn by Zed's own [`Button`]: hover, active, disabled and
/// focus states, padding and radius all come from the theme instead of being
/// re-drawn here. The Orca `kind` only picks the style.
///
/// `_font_size` is kept because callers still pass the design's type size; the
/// button's `ButtonSize` owns the label size now. `height` only chooses the
/// closest size, so the buttons snap to Zed's five heights.
pub fn button(
    id: impl Into<ElementId>,
    label: impl Into<SharedString>,
    kind: &str,
    height: f32,
    _font_size: f32,
    on_click: Option<Box<dyn Fn(&ClickEvent, &mut Window, &mut App) + 'static>>,
) -> AnyElement {
    let style = match kind {
        "primary" => ButtonStyle::Tinted(TintColor::Accent),
        "danger" => ButtonStyle::Tinted(TintColor::Error),
        "warn" => ButtonStyle::Tinted(TintColor::Warning),
        "ghost" => ButtonStyle::Transparent,
        _ => ButtonStyle::Outlined,
    };
    let size = if height <= 20. {
        ButtonSize::Compact
    } else if height <= 24. {
        ButtonSize::Default
    } else {
        ButtonSize::Medium
    };
    let disabled = on_click.is_none();
    let mut button = Button::new(id, label).style(style).size(size).disabled(disabled);
    if let Some(handler) = on_click {
        button = button.on_click(move |event, window, cx| handler(event, window, cx));
    }
    button.into_any_element()
}
