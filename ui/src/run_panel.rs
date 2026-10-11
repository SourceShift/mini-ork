//! The right-hand Run panel: the active run's dock view — Changes · Checks ·
//! Agents · Cost · Learned — one column beside the session, the way Orca keeps
//! a worktree's Source Control and Checks next to the session.
//!
//! The panel is a thin host: it follows the workspace's active run and hosts a
//! [`MiniOrkPage`] built with `view=dock`. The page's own tab bar switches the
//! dock's tabs; it reloads with the board store, but only while the panel is
//! the visible panel of its dock, so a closed panel never polls.

use gpui::{
    Action, AnyElement, App, Context, Entity, EventEmitter, FocusHandle, Focusable, Pixels, Render,
    Subscription, WeakEntity, Window, actions, px,
};
use ui::{IconName, prelude::*};
use workspace::{
    Workspace,
    dock::{DockPosition, Panel, PanelEvent},
};

use crate::{
    page::MiniOrkPage,
    palette::{self as p, c},
    store::BoardStore,
};

actions!(
    mini_ork_run_panel,
    [
        /// Toggles focus on the mini-ork Run panel.
        ToggleFocus
    ]
);

const RUN_PANEL_KEY: &str = "MiniOrkRunPanel";

/// Hook every new workspace: put the Run panel in its right dock. A no-op when
/// there is no window.
pub fn init(cx: &mut App) {
    cx.observe_new(|workspace: &mut Workspace, window, cx| {
        let Some(window) = window else {
            return;
        };
        workspace.register_action(|workspace, _: &ToggleFocus, window, cx| {
            workspace.toggle_panel_focus::<RunPanel>(window, cx);
        });
        // The workspace is leased while `observe_new` runs, so the initial run
        // is read from the borrowed workspace rather than through its handle.
        let initial_run = active_run(workspace, cx);
        let workspace_entity = cx.entity();
        let panel = cx.new(|cx| RunPanel::new(workspace_entity, initial_run, cx));
        workspace.add_panel(panel, window, cx);
    })
    .detach();
}

pub struct RunPanel {
    workspace: WeakEntity<Workspace>,
    focus_handle: FocusHandle,
    /// The dock page, once a run is known.
    page: Option<Entity<MiniOrkPage>>,
    /// The run the panel follows: the workspace's active run, if any.
    run: Option<String>,
    /// The board read the panel last saw, so it reloads once per read.
    seen_board: u64,
    /// Whether the panel is the visible panel of its dock: the page reloads
    /// with the board only while it is, so a closed panel never polls.
    active: bool,
    _store: Option<Subscription>,
    _active_item: Subscription,
}

impl RunPanel {
    fn new(workspace: Entity<Workspace>, initial_run: Option<String>, cx: &mut Context<Self>) -> Self {
        let active_item = cx.subscribe(&workspace, |this, _, event: &workspace::Event, cx| {
            if matches!(event, workspace::Event::ActiveItemChanged) {
                this.sync_run(cx);
            }
        });
        let store = BoardStore::for_workspace(&workspace, cx);
        let mut seen_board = 0;
        let mut store_sub = None;
        if let Some(store) = &store {
            seen_board = store.read(cx).generation;
            store_sub = Some(cx.observe(store, |this, store, cx| {
                let generation = store.read(cx).generation;
                if generation == this.seen_board {
                    return;
                }
                this.seen_board = generation;
                if this.active {
                    if let Some(page) = this.page.clone() {
                        if page.read(cx).may_reload() {
                            page.update(cx, |page, cx| page.reload(cx));
                        }
                    }
                }
            }));
        }
        let mut this = Self {
            workspace: workspace.downgrade(),
            focus_handle: cx.focus_handle(),
            page: None,
            run: None,
            seen_board,
            active: false,
            _store: store_sub,
            _active_item: active_item,
        };
        if let Some(run) = initial_run {
            this.show_run(run, cx);
        }
        this
    }

    /// Follow the workspace's active run. An active item that is not a run
    /// page leaves the panel showing the run it already has; before any run is
    /// opened the panel shows its empty state.
    fn sync_run(&mut self, cx: &mut Context<Self>) {
        let Some(workspace) = self.workspace.upgrade() else {
            return;
        };
        let run = active_run(workspace.read(cx), cx);
        if let Some(run) = run {
            self.show_run(run, cx);
        }
    }

    /// Point the dock page at `run`, creating it on first use.
    fn show_run(&mut self, run: String, cx: &mut Context<Self>) {
        if self.run.as_deref() == Some(run.as_str()) {
            return;
        }
        self.run = Some(run.clone());
        match self.page.clone() {
            Some(page) => page.update(cx, |page, cx| page.set_run(run, cx)),
            None => {
                let workspace = self.workspace.clone();
                self.page = Some(cx.new(|cx| MiniOrkPage::new_dock(run, None, workspace, cx)));
            }
        }
        cx.notify();
    }
}

/// The run id carried by the workspace's active item, when it is a run page.
fn active_run(workspace: &Workspace, cx: &App) -> Option<String> {
    workspace
        .active_item(cx)
        .and_then(|item| item.to_any_view().downcast::<MiniOrkPage>().ok())
        .and_then(|page| page.read(cx).args.get("run").cloned())
}

impl EventEmitter<PanelEvent> for RunPanel {}

impl Focusable for RunPanel {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl Panel for RunPanel {
    fn persistent_name() -> &'static str {
        "mini-ork Run"
    }

    fn panel_key() -> &'static str {
        RUN_PANEL_KEY
    }

    fn position(&self, _window: &Window, _cx: &App) -> DockPosition {
        DockPosition::Right
    }

    fn position_is_valid(&self, position: DockPosition) -> bool {
        position == DockPosition::Right
    }

    fn set_position(&mut self, _position: DockPosition, _window: &mut Window, _cx: &mut Context<Self>) {}

    fn default_size(&self, _window: &Window, _cx: &App) -> Pixels {
        px(380.)
    }

    fn icon(&self, _window: &Window, _cx: &App) -> Option<IconName> {
        Some(IconName::ListTodo)
    }

    fn icon_tooltip(&self, _window: &Window, _cx: &App) -> Option<&'static str> {
        Some("mini-ork Run")
    }

    fn toggle_action(&self) -> Box<dyn Action> {
        Box::new(ToggleFocus)
    }

    fn activation_priority(&self) -> u32 {
        50
    }

    fn set_active(&mut self, active: bool, _window: &mut Window, cx: &mut Context<Self>) {
        self.active = active;
        if active {
            // Fresh content the moment the panel is shown.
            if let Some(page) = &self.page {
                page.update(cx, |page, cx| page.reload(cx));
            }
            cx.notify();
        }
    }
}

impl Render for RunPanel {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        let content: AnyElement = match self.page.clone() {
            Some(page) => page.into_any_element(),
            None => empty_state(),
        };
        v_flex()
            .size_full()
            .bg(c(p::BG))
            .text_color(c(p::TEXT))
            .track_focus(&self.focus_handle)
            .child(content)
    }
}

/// What the panel shows before a run is in front.
fn empty_state() -> AnyElement {
    v_flex()
        .size_full()
        .items_center()
        .justify_center()
        .p_4()
        .child(p::text(
            "Open a run to see its changes and checks",
            12.5,
            p::SUB,
        ))
        .into_any_element()
}
