//! The design's loading state: a 2 px sweeping bar under the tabs, a spinner +
//! "Loading <source>" line, and shimmering skeleton cards shaped like the
//! sections that are coming.

use std::time::Duration;

use gpui::{
    Animation, AnimationExt as _, AnyElement, App, Div, Hsla, ease_in_out, linear_color_stop, linear_gradient,
    px, relative,
};
use ui::{CommonAnimationExt as _, prelude::*};

use crate::components::ThrottledSpinExt as _;
use crate::palette::{self as p, c};

/// A section shape for a skeleton card: its type and whether it spans both columns.
#[derive(Clone, Debug)]
pub struct Shape {
    pub kind: String,
    pub full: bool,
    pub titled: bool,
}

impl Shape {
    pub fn new(kind: &str, full: bool) -> Self {
        Self { kind: kind.into(), full, titled: true }
    }
}

/// The skeleton for a tab nobody has loaded yet in this session.
pub fn default_shapes(key: &str, tab: &str) -> Vec<Shape> {
    match (key, tab) {
        ("run", "" | "dag") => vec![Shape::new("dag", true), Shape::new("list", false), Shape::new("kv", false)],
        ("run", "agents") | ("runs", "" | "runs") => vec![Shape::new("chips", true), Shape::new("table", true)],
        _ => vec![Shape::new("table", true), Shape::new("list", false), Shape::new("kv", false)],
    }
}

/// The 2 px indeterminate bar (`moIndet`): a 35 % blue glow sweeping left to right.
pub fn sweep_bar(id: impl Into<ElementId>) -> AnyElement {
    let glow = |from: Hsla, to: Hsla| {
        div()
            .h_full()
            .w(relative(0.5))
            .bg(linear_gradient(90., linear_color_stop(from, 0.), linear_color_stop(to, 1.)))
    };
    let blue = c(p::BLUE);
    let clear = p::ca(0x74ade800);
    div()
        .relative()
        .w_full()
        .h_0p5()
        .overflow_hidden()
        .child(
            h_flex()
                .absolute()
                .top_0()
                .h_0p5()
                .w(relative(0.35))
                .child(glow(clear, blue))
                .child(glow(blue, clear))
                .with_animation(
                    id,
                    Animation::new(Duration::from_millis(1000))
                        .repeat()
                        .with_easing(ease_in_out)
                        // The sweep only shows while a tab loads, but it still
                        // repaints the whole window per frame — cap it.
                        .with_max_fps(24.),
                    |el, delta| el.left(relative(-0.35 + 1.35 * delta)),
                ),
        )
        .into_any_element()
}

/// Spinner + "Loading" + the source being read, in dim mono.
pub fn loading_line(source: String, cx: &App) -> AnyElement {
    h_flex()
        .gap_2()
        .min_w_0()
        .text_size(rems_from_px(11.5_f32))
        .text_color(c(p::SUB))
        .child(
            Icon::new(IconName::LoadCircle)
                .size(IconSize::Custom(rems(11. / 16.)))
                .color(Color::Custom(c(p::BLUE)))
                .with_throttled_rotate("loading-icon", 1),
        )
        .child(div().flex_none().child("Loading"))
        .child(p::text(source, 11.5, p::DIM).font(p::mono_font(cx)).truncate().min_w_0())
        .into_any_element()
}

/// One shimmering bar (`SHIM`): a pulse between the two skeleton greys.
fn bar(id: SharedString, width: gpui::DefiniteLength, height: f32, radius: f32, delay: f32) -> AnyElement {
    let low = c(p::BORDER);
    let high = c(0x434a56);
    div()
        .flex_none()
        .w(width)
        .h(rems_from_px(height))
        .rounded(rems_from_px(radius))
        .bg(low)
        .with_animation(
            id,
            Animation::new(Duration::from_millis(1300))
                .repeat()
                // A skeleton shows dozens of shimmering bars; a slow pulse
                // reads the same at a fraction of the repaint cost.
                .with_max_fps(6.),
            move |el, delta| {
                let phase = (delta + delay).fract();
                let t = 0.5 - 0.5 * (phase * std::f32::consts::TAU).cos();
                el.bg(mix(low, high, t))
            },
        )
        .into_any_element()
}

fn mix(a: Hsla, b: Hsla, t: f32) -> Hsla {
    Hsla {
        h: a.h + (b.h - a.h) * t,
        s: a.s + (b.s - a.s) * t,
        l: a.l + (b.l - a.l) * t,
        a: a.a + (b.a - a.a) * t,
    }
}

const WIDTHS: [f32; 6] = [0.92, 0.74, 0.86, 0.61, 0.80, 0.68];

/// One skeleton card shaped like a section (`skelEl`).
pub fn skeleton_card(index: usize, shape: &Shape) -> Div {
    let delay = index as f32 * 0.09 / 1.3;
    let id = |part: &str| SharedString::from(format!("skel-{index}-{part}"));
    let pct = |v: f32| relative(v);
    let body: AnyElement = match shape.kind.as_str() {
        "table" => v_flex()
            .gap(rems_from_px(11_f32))
            .child(
                h_flex()
                    .gap_4()
                    .child(bar(id("h0"), pct(0.18), 7., 4., delay))
                    .child(bar(id("h1"), pct(0.30), 7., 4., delay))
                    .child(bar(id("h2"), pct(0.14), 7., 4., delay)),
            )
            .children((0..4).map(|j| {
                h_flex()
                    .gap_4()
                    .child(bar(id(&format!("r{j}a")), pct(0.14), 9., 4., delay))
                    .child(bar(id(&format!("r{j}b")), pct(WIDTHS[j % 6] * 0.5), 9., 4., delay))
                    .child(bar(id(&format!("r{j}c")), pct(0.12), 9., 4., delay))
            }))
            .into_any_element(),
        "kv" => h_flex()
            .flex_wrap()
            .gap_2()
            .children((0..4).map(|j| {
                v_flex()
                    .w(rems_from_px(150_f32))
                    .flex_grow(1.)
                    .max_w(rems_from_px(260_f32))
                    .gap(rems_from_px(7_f32))
                    .px_2p5()
                    .py_2()
                    .border_1()
                    .border_color(c(p::BORDER))
                    .child(bar(id(&format!("k{j}a")), pct(0.5), 7., 4., delay))
                    .child(bar(id(&format!("k{j}b")), pct(0.7), 12., 4., delay))
            }))
            .into_any_element(),
        "list" | "inspector" => v_flex()
            .gap_3()
            .children((0..3).map(|j| {
                h_flex()
                    .gap_2p5()
                    .child(bar(id(&format!("l{j}m")), px(12.).into(), 12., 6., delay))
                    .child(
                        v_flex()
                            .flex_1()
                            .gap_1p5()
                            .child(bar(id(&format!("l{j}a")), pct(WIDTHS[j % 6]), 9., 4., delay))
                            .child(bar(id(&format!("l{j}b")), pct(0.4), 7., 4., delay)),
                    )
            }))
            .into_any_element(),
        "bars" => v_flex()
            .gap(rems_from_px(9_f32))
            .children((0..3).map(|j| {
                h_flex()
                    .gap_2p5()
                    .child(bar(id(&format!("b{j}a")), pct(0.22), 8., 4., delay))
                    .child(bar(id(&format!("b{j}b")), pct(WIDTHS[(j + 2) % 6] * 0.6), 8., 4., delay))
                    .child(bar(id(&format!("b{j}c")), pct(0.10), 8., 4., delay))
            }))
            .into_any_element(),
        "code" | "dag" => v_flex()
            .gap_2()
            .children((0..4).map(|j| bar(id(&format!("c{j}")), pct(WIDTHS[j]), 8., 4., delay)))
            .into_any_element(),
        _ => h_flex()
            .gap_1p5()
            .children((0..3).map(|j| bar(id(&format!("p{j}")), px(72.).into(), 20., 10., delay)))
            .into_any_element(),
    };
    v_flex()
        .flex_1()
        .min_w_0()
        .px_3p5()
        .py_3()
        .gap_3()
        .border_1()
        .border_color(c(p::BORDER))
        .bg(c(p::CHROME))
        .when(shape.titled, |this| this.child(bar(id("title"), pct(0.34), 11., 4., delay)))
        .child(body)
}

/// The skeleton grid: full cards span the row; the rest pair up.
pub fn skeleton(shapes: &[Shape]) -> AnyElement {
    let mut rows: Vec<AnyElement> = Vec::new();
    let mut pending: Vec<(usize, &Shape)> = Vec::new();
    let flush = |pending: &mut Vec<(usize, &Shape)>, rows: &mut Vec<AnyElement>| {
        for pair in pending.chunks(2) {
            let mut row = h_flex().items_start().gap_3();
            for (i, shape) in pair {
                row = row.child(skeleton_card(*i, shape));
            }
            if pair.len() == 1 {
                row = row.child(div().flex_1());
            }
            rows.push(row.into_any_element());
        }
        pending.clear();
    };
    for (i, shape) in shapes.iter().enumerate() {
        if shape.full {
            flush(&mut pending, &mut rows);
            rows.push(skeleton_card(i, shape).into_any_element());
        } else {
            pending.push((i, shape));
        }
    }
    flush(&mut pending, &mut rows);
    v_flex().gap_3().children(rows).into_any_element()
}
