//! Status bar items: the board toggle and the live mini-ork counters on the
//! left, the mini-ork version on the right.

use gpui::{App, Context, Entity, Render, SharedString, Subscription, WeakEntity, Window};
use ui::{Divider, DividerColor, Tooltip, prelude::*};
use workspace::{HideStatusItem, ItemHandle, StatusItemView, Workspace};

use crate::{
    board::Lane,
    page,
    palette::{self as p, c},
    sidebar::MiniOrkSidebar,
    store::BoardStore,
};

pub fn init(cx: &mut App) {
    cx.observe_new(|workspace: &mut Workspace, window, cx| {
        let Some(window) = window else {
            return;
        };
        let weak = cx.entity().downgrade();
        let Some(store) = BoardStore::for_workspace(&cx.entity(), cx) else {
            return;
        };
        let left = cx.new(|cx| MiniOrkStatus::new(weak, store.clone(), cx));
        let right = cx.new(|cx| MiniOrkVersion::new(store, cx));
        workspace.status_bar().update(cx, |status_bar, cx| {
            status_bar.add_left_item(left, window, cx);
            status_bar.add_right_item(right, window, cx);
        });
    })
    .detach();
}

pub struct MiniOrkStatus {
    workspace: WeakEntity<Workspace>,
    store: Entity<BoardStore>,
    _store: Subscription,
}

impl MiniOrkStatus {
    fn new(workspace: WeakEntity<Workspace>, store: Entity<BoardStore>, cx: &mut Context<Self>) -> Self {
        let sub = cx.observe(&store, |_, _, cx| cx.notify());
        Self {
            workspace,
            store,
            _store: sub,
        }
    }
}

fn toggle_board(workspace: &WeakEntity<Workspace>, cx: &mut App) {
    with_sidebar(workspace, cx, |sidebar, cx| sidebar.toggle_board(cx));
}

/// Open the Threads board, where working and failed runs are grouped.
fn show_board(workspace: &WeakEntity<Workspace>, cx: &mut App) {
    with_sidebar(workspace, cx, |sidebar, cx| sidebar.show_board(cx));
}

fn with_sidebar(
    workspace: &WeakEntity<Workspace>,
    cx: &mut App,
    f: impl FnOnce(&mut MiniOrkSidebar, &mut Context<MiniOrkSidebar>),
) {
    let Some(workspace) = workspace.upgrade() else {
        return;
    };
    let Some(mw) = workspace.read(cx).multi_workspace().and_then(|mw| mw.upgrade()) else {
        return;
    };
    let sidebar = mw
        .read(cx)
        .sidebar()
        .and_then(|s| s.to_any().downcast::<MiniOrkSidebar>().ok());
    if let Some(sidebar) = sidebar {
        sidebar.update(cx, f);
        mw.update(cx, |_, cx| cx.notify());
    }
}

impl Render for MiniOrkStatus {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let header = self
            .store
            .read(cx)
            .board
            .as_ref()
            .map(|b| b.header.clone())
            .unwrap_or_default();
        let item = |id: &'static str, label: String, colour: u32, tip: SharedString, key: &'static str, tab: Option<&'static str>, workspace: WeakEntity<Workspace>| {
            div()
                .id(id)
                .h_5()
                .px(rems_from_px(7_f32))
                .flex()
                .items_center()
                .text_size(rems_from_px(12_f32))
                .text_color(c(colour))
                .cursor_pointer()
                .hover(|s| s.bg(c(p::HOVER)))
                .tooltip(Tooltip::text(tip))
                .child(label)
                .on_click(move |_, window, cx| {
                    let workspace = workspace.clone();
                    if key == "threads" {
                        show_board(&workspace, cx);
                        return;
                    }
                    window.defer(cx, move |window, cx| {
                        if let Some(workspace) = workspace.upgrade() {
                            page::open_page(&workspace, key.into(), tab.map(String::from), Default::default(), window, cx);
                        }
                    });
                })
        };
        let w = self.workspace.clone();
        // Spend is muted until it nears the cap: the colour is the only thing
        // here that should make you look.
        let spent = header.today_usd.unwrap_or(0.);
        let budget_colour = if header.cap_usd > 0. && spent >= header.cap_usd {
            p::RED
        } else if header.cap_usd > 0. && spent >= header.cap_usd * 0.8 {
            p::YELLOW
        } else {
            p::MUTED
        };
        h_flex()
            .gap_0p5()
            .child(
                div()
                    .id("status-board")
                    .h_5()
                    .px_1p5()
                    .flex()
                    .items_center()
                    .text_size(rems_from_px(12_f32))
                    .text_color(c(p::MUTED))
                    .cursor_pointer()
                    .hover(|s| s.bg(c(p::HOVER)))
                    .tooltip(Tooltip::text("Toggle task board"))
                    .child("▤")
                    .on_click({
                        let w = w.clone();
                        move |_, _, cx| toggle_board(&w, cx)
                    }),
            )
            .child(Divider::vertical().h_3p5().mx_1().color(DividerColor::Border))
            .child(item(
                "status-needs",
                format!("✋ {} need you", header.needs_you),
                if header.needs_you > 0 { p::ORANGE } else { p::SUB },
                "Threads and gates waiting on you — opens the Inbox".into(),
                "inbox",
                None,
                w.clone(),
            ))
            .child(item(
                "status-working",
                format!("● {} working", header.working),
                if header.working > 0 { p::BLUE } else { p::SUB },
                "Runs in flight — on the Threads board".into(),
                "threads",
                None,
                w.clone(),
            ))
            .child(item(
                "status-failed",
                format!("✗ {} failed", header.failed),
                if header.failed > 0 { p::RED } else { p::SUB },
                "Failed runs — on the Threads board".into(),
                "threads",
                None,
                w.clone(),
            ))
            .child(item(
                "status-budget",
                format!("{} / ${:.0}", money(header.today_usd), header.cap_usd),
                budget_colour,
                "Spend today against the daily cap".into(),
                "lanes",
                Some("cost"),
                w.clone(),
            ))
            .child(item(
                "status-scheduler",
                format!("◷ {}", if header.scheduler_on { "on" } else { "off" }),
                if header.scheduler_on { p::MUTED } else { p::YELLOW },
                "Project scheduler".into(),
                "autos",
                Some("schedulers"),
                w.clone(),
            ))
            // Lanes are per-model detail: they earn a place in the bar only
            // when one is failing, and then as one alert rather than five bars.
            .when_some(lane_alert(&header.lanes), |this, (label, tip)| {
                this.child(Divider::vertical().h_3p5().mx_1().color(DividerColor::Border))
                    .child(item("status-lane-alert", label, p::RED, tip, "lanes", Some("cost"), w.clone()))
            })
    }
}

/// The lanes failing half their calls (over at least three), as one alert for
/// the status bar: the count to read, the names and last error in the tooltip.
/// `None` when every lane is healthy, so a working project shows no lane noise.
fn lane_alert(lanes: &[Lane]) -> Option<(String, SharedString)> {
    let failing: Vec<&Lane> = lanes
        .iter()
        .filter(|lane| lane.calls >= 3 && lane.failed * 2 >= lane.calls)
        .collect();
    let first = failing.first()?;
    let label = match failing.len() {
        1 => "⚠ 1 lane failing".to_string(),
        n => format!("⚠ {n} lanes failing"),
    };
    let mut tip = failing
        .iter()
        .map(|lane| format!("{}: {}/{} failed · ${:.2}", lane.lane, lane.failed, lane.calls, lane.usd))
        .collect::<Vec<_>>()
        .join(" · ");
    if !first.last_error.is_empty() {
        tip.push_str(&format!(" · {}", first.last_error));
    }
    Some((label, tip.into()))
}

impl StatusItemView for MiniOrkStatus {
    fn set_active_pane_item(&mut self, _: Option<&dyn ItemHandle>, _: &mut Window, _: &mut Context<Self>) {}

    fn hide_setting(&self, _cx: &App) -> Option<HideStatusItem> {
        None
    }
}

pub struct MiniOrkVersion {
    store: Entity<BoardStore>,
    _store: Subscription,
}

impl MiniOrkVersion {
    fn new(store: Entity<BoardStore>, cx: &mut Context<Self>) -> Self {
        let sub = cx.observe(&store, |_, _, cx| cx.notify());
        Self { store, _store: sub }
    }
}

impl Render for MiniOrkVersion {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let version = self
            .store
            .read(cx)
            .board
            .as_ref()
            .map(|b| b.header.version.clone())
            .unwrap_or_default();
        let label: SharedString = if version.is_empty() {
            "mini-ork".into()
        } else {
            format!("mini-ork {version}").into()
        };
        div().px(rems_from_px(7_f32)).text_size(rems_from_px(12_f32)).text_color(c(p::MUTED)).child(label)
    }
}

impl StatusItemView for MiniOrkVersion {
    fn set_active_pane_item(&mut self, _: Option<&dyn ItemHandle>, _: &mut Window, _: &mut Context<Self>) {}

    fn hide_setting(&self, _cx: &App) -> Option<HideStatusItem> {
        None
    }
}

fn money(value: Option<f64>) -> String {
    value.map(|v| format!("${v:.2}")).unwrap_or_else(|| "—".to_string())
}
