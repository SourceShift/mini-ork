//! The mini-ork IDE title bar: project and branch on the left (when
//! the Threads board is closed — otherwise the sidebar's top strip shows them),
//! and on the right what needs you, today's spend, the scheduler, nodes and
//! sign-in. It replaces Zed's title bar item; Zed's `PlatformTitleBar` still
//! does window dragging and the traffic-light space.

use gpui::{
    AnyElement, App, Context, Entity, Render, Subscription, WeakEntity, Window,
};
use platform_title_bar::PlatformTitleBar;
use ui::{ButtonLike, ContextMenu, PopoverMenu, Tooltip, prelude::*, utils::TRAFFIC_LIGHT_PADDING};
use workspace::{MultiWorkspace, Workspace};

use crate::{
    board::Header,
    page,
    palette::{self as p, c},
    projects,
    sidebar::{ACTIVITY_WIDTH, MiniOrkSidebar},
    store::BoardStore,
};

pub fn init(cx: &mut App) {
    cx.observe_new(|workspace: &mut Workspace, window, cx| {
        let Some(window) = window else {
            return;
        };
        let weak = cx.entity().downgrade();
        let multi_workspace = workspace.multi_workspace().cloned();
        let item = cx.new(|cx| MiniOrkTitleBar::new(weak, multi_workspace, cx));
        workspace.set_titlebar_item(item.into(), window, cx);
    })
    .detach();
}

pub struct MiniOrkTitleBar {
    platform: Entity<PlatformTitleBar>,
    workspace: WeakEntity<Workspace>,
    multi_workspace: Option<WeakEntity<MultiWorkspace>>,
    _store: Option<Subscription>,
}

impl MiniOrkTitleBar {
    fn new(
        workspace: WeakEntity<Workspace>,
        multi_workspace: Option<WeakEntity<MultiWorkspace>>,
        cx: &mut Context<Self>,
    ) -> Self {
        let platform = cx.new(|cx| {
            let bar = PlatformTitleBar::new("mini-ork-title-bar", cx);
            match multi_workspace.clone() {
                Some(mw) => bar.with_multi_workspace(mw),
                None => bar,
            }
        });
        Self {
            platform,
            workspace,
            multi_workspace,
            _store: None,
        }
    }

    /// Width the sidebar takes and whether its Threads board is open.
    fn sidebar_state(&self, cx: &App) -> (f32, bool) {
        let Some(mw) = self.multi_workspace.as_ref().and_then(|mw| mw.upgrade()) else {
            return (0., false);
        };
        let mw = mw.read(cx);
        if !mw.sidebar_open() || !mw.multi_workspace_enabled(cx) {
            return (0., false);
        }
        let Some(sidebar) = mw.sidebar() else {
            return (0., false);
        };
        let board_open = sidebar
            .to_any()
            .downcast::<MiniOrkSidebar>()
            .map(|s| s.read(cx).board_open())
            .unwrap_or(true);
        (f32::from(sidebar.width(cx)), board_open)
    }
}

/// Project ⌄ · ⑂ worktree · ⎇ branch — shared with the sidebar strip. The
/// worktree and branch come from the workspace's own repository, so they are
/// right even before the board's first read lands.
pub fn left_items(
    header: &Header,
    project: &str,
    workspace: Option<Entity<Workspace>>,
    with_project: bool,
    cx: &App,
) -> Vec<AnyElement> {
    let mut items = Vec::new();
    if with_project {
        items.push(project_switcher(project, workspace.clone()).into_any_element());
        if let Some(worktree) = workspace
            .as_ref()
            .and_then(|workspace| projects::worktree_label(workspace, cx))
        {
            items.push(p::text(format!("⑂ {worktree}"), 12.5, p::SUB).into_any_element());
        }
        if !header.branch.is_empty() {
            items.push(p::text(format!("⎇ {}", header.branch), 12.5, p::SUB).into_any_element());
        }
    }
    items
}

/// `project ⌄`: a menu of the known mini-ork projects (the current one
/// checked) that switches this window to the one picked, plus "Open project
/// folder…" and "Manage projects…".
fn project_switcher(project: &str, workspace: Option<Entity<Workspace>>) -> impl IntoElement {
    let trigger = ButtonLike::new("tb-project").child(
        div()
            .px_0p5()
            .text_size(rems_from_px(12.5_f32))
            .font_weight(gpui::FontWeight::SEMIBOLD)
            .text_color(c(p::TEXT))
            .child(format!("{project} ⌄")),
    );
    PopoverMenu::new("tb-project-menu")
        .trigger_with_tooltip(trigger, Tooltip::text("Switch project"))
        .menu(move |window, cx| {
            let workspace = workspace.clone()?;
            // Read the list here: the inner builder is `move`, so it may only
            // capture owned values, never this `cx`.
            let entries = projects::entries(&workspace, cx);
            Some(ContextMenu::build(window, cx, move |mut menu, _, _| {
                menu = menu.header("mini-ork projects");
                for entry in entries {
                    // The submenu trigger is the project; its body is everything
                    // that can be done to it (see `projects::project_actions`).
                    let mark = if entry.is_current { "✓" } else { " " };
                    let note = if entry.missing { "  · folder gone" } else { "" };
                    let label = format!("{mark} {}  {}{note}", entry.name, entry.path);
                    let (workspace, entry) = (workspace.clone(), entry);
                    menu = menu.submenu(label, move |menu, _, _| {
                        projects::project_actions(menu, &workspace, entry.clone())
                    });
                }
                let add_workspace = workspace.clone();
                // Last use of `workspace` here, so it moves rather than clones.
                let manage_workspace = workspace;
                menu.separator()
                    .entry("Add project…", None, move |window, cx| {
                        projects::add(&add_workspace, window, cx)
                    })
                    .entry("Manage projects…", None, move |window, cx| {
                        let workspace = manage_workspace.clone();
                        window.defer(cx, move |window, cx| {
                            page::open_page(&workspace, "setup".into(), Some("projects".into()), Default::default(), window, cx);
                        });
                    })
            }))
        })
}

fn right_items(header: &Header, workspace: Option<Entity<Workspace>>, cx: &App) -> Vec<AnyElement> {
    let open = |key: &'static str, tab: Option<&'static str>| {
        let workspace = workspace.clone();
        move |_: &gpui::ClickEvent, window: &mut Window, cx: &mut App| {
            if let Some(workspace) = workspace.clone() {
                window.defer(cx, move |window, cx| {
                    page::open_page(&workspace, key.into(), tab.map(String::from), Default::default(), window, cx);
                });
            }
        }
    };
    let plain = |id: &'static str, label: String, colour: u32, tip: &'static str| {
        div()
            .id(id)
            .px_1p5()
            .py(rems_from_px(3_f32))
            .text_size(rems_from_px(12_f32))
            .text_color(c(colour))
            .cursor_pointer()
            .hover(|s| s.bg(c(p::HOVER)))
            .tooltip(Tooltip::text(tip))
            .child(label)
    };
    let nodes = if header.nodes.configured > 0 {
        format!("⬡ {} nodes", header.nodes.configured)
    } else {
        "⬡ local".to_string()
    };
    vec![
        div()
            .id("tb-needs")
            .px(rems_from_px(9_f32))
            .py_0p5()
            .border_1()
            .border_color(p::ca(0xdec18466))
            .bg(p::ca(0xdec18414))
            .text_size(rems_from_px(12_f32))
            .text_color(c(p::YELLOW))
            .cursor_pointer()
            .tooltip(Tooltip::text("Threads and gates waiting for you"))
            .child(format!("✋ {} need you", header.needs_you))
            .on_click(open("verify", Some("inbox")))
            .into_any_element(),
        plain(
            "tb-cost",
            format!("{} of ${:.0} today", money(header.today_usd), header.cap_usd),
            p::MUTED,
            "Spend today vs the daily cap",
        )
        .font(p::mono_font(cx))
        .on_click(open("lanes", Some("cost")))
        .into_any_element(),
        plain(
            "tb-scheduler",
            format!("◷ scheduler {}", if header.scheduler_on { "on" } else { "off" }),
            if header.scheduler_on { p::MUTED } else { p::YELLOW },
            "Project scheduler",
        )
        .on_click(open("autos", Some("schedulers")))
        .into_any_element(),
        plain("tb-nodes", nodes, p::MUTED, "Remote nodes")
            .on_click(open("nodes", None))
            .into_any_element(),
        div()
            .id("tb-sign-in")
            .ml_1p5()
            .text_size(rems_from_px(12.5_f32))
            .text_color(c(p::MUTED))
            .cursor_pointer()
            .hover(|s| s.text_color(c(p::TEXT)))
            .child("Sign In")
            .on_click(|_, window, cx| {
                if let Ok(action) = cx.build_action("client::SignIn", None) {
                    window.dispatch_action(action, cx);
                }
            })
            .into_any_element(),
    ]
}

impl Render for MiniOrkTitleBar {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let workspace = self.workspace.upgrade();
        let store = workspace.as_ref().and_then(|w| BoardStore::for_workspace(w, cx));
        if self._store.is_none()
            && let Some(store) = store.clone()
        {
            self._store = Some(cx.observe(&store, |_, _, cx| cx.notify()));
        }
        let board = store.as_ref().and_then(|s| s.read(cx).board.clone());
        let header = board.as_ref().map(|b| b.header.clone()).unwrap_or_default();
        let project = if header.project.is_empty() {
            board.as_ref().map(|b| b.project.clone()).unwrap_or_default()
        } else {
            header.project.clone()
        };
        let (sidebar_width, board_open) = self.sidebar_state(cx);
        let fullscreen = window.is_fullscreen() || window.is_simple_fullscreen();
        // With only the activity bar showing, the traffic lights reach past
        // the sidebar into the title bar.
        let traffic_gap = if sidebar_width > 0. && !fullscreen && cfg!(target_os = "macos") {
            (TRAFFIC_LIGHT_PADDING + 2. - sidebar_width).max(0.)
        } else {
            0.
        };
        // With the Threads board open, the sidebar strip shows the project and
        // branch instead.
        let strip_has_project = sidebar_width > ACTIVITY_WIDTH + 1. && board_open;
        let left = h_flex()
            .gap_2p5()
            .when(traffic_gap > 0., |this| this.pl(rems_from_px(traffic_gap)))
            .when(strip_has_project, |this| this.pl_1())
            .when(!project.is_empty(), |this| {
                this.children(left_items(&header, &project, workspace.clone(), !strip_has_project, cx))
            });
        let right = h_flex()
            .gap_2p5()
            .pr_2p5()
            .when(board.is_some(), |this| this.children(right_items(&header, workspace.clone(), cx)));
        let row = h_flex()
            .w_full()
            .h_full()
            .items_center()
            .whitespace_nowrap()
            .child(left)
            .child(div().flex_1())
            .child(right)
            .into_any_element();
        self.platform.update(cx, |platform, _| platform.set_children([row]));
        div()
            .w_full()
            .bg(c(p::CHROME))
            .border_b_1()
            .border_color(c(p::BORDER))
            .child(self.platform.clone())
            .into_any_element()
    }
}

fn money(value: Option<f64>) -> String {
    value.map(|v| format!("${v:.2}")).unwrap_or_else(|| "—".to_string())
}
