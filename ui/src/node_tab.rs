//! A DAG node as its own editor tab: the run's DAG on top with this node
//! selected, then the node inspector (Overview, Stream, Changes, Artifacts, …)
//! below, full width. Opened from a run's story/flow/DAG, or a Threads card.
//! One tab per (run, node); opening it again brings it forward.
//!
//! The tab hosts a `MiniOrkGraph` in tab mode — the same canvas and inspector
//! the full-screen graph shows, so the two never drift — with the canvas
//! pinned to the top strip and the inspector filling the rest.

use std::collections::BTreeMap;

use gpui::{
    AnyElement, App, AppContext as _, Context, Entity, EventEmitter, FocusHandle, Focusable, Render,
    SharedString, Task, WeakEntity, Window,
};
use ui::prelude::*;
use workspace::{
    Workspace,
    item::{Item, TabContentParams},
};

use crate::{
    board,
    graph::{MiniOrkGraph, Model},
    page::MiniOrkPage,
    palette::{self as p, c},
    store::BoardStore,
};

pub struct MiniOrkNodeTab {
    pub run_id: String,
    pub node_id: String,
    title: String,
    graph: Option<Entity<MiniOrkGraph>>,
    error: Option<String>,
    focus_handle: FocusHandle,
    _load: Task<()>,
}

impl MiniOrkNodeTab {
    fn new(
        run_id: String,
        node_id: String,
        title: String,
        model: Option<Model>,
        workspace: WeakEntity<Workspace>,
        page: WeakEntity<MiniOrkPage>,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Self {
        let mut this = Self {
            run_id: run_id.clone(),
            node_id: node_id.clone(),
            title,
            graph: None,
            error: None,
            focus_handle: cx.focus_handle(),
            _load: Task::ready(()),
        };
        match model {
            Some(model) => this.attach(model, workspace, page, window, cx),
            None => this.load(workspace, window, cx),
        }
        this
    }

    fn attach(
        &mut self,
        model: Model,
        workspace: WeakEntity<Workspace>,
        page: WeakEntity<MiniOrkPage>,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let node = Some(self.node_id.clone());
        let graph = cx.new(|cx| {
            let mut graph = MiniOrkGraph::new(model, node, workspace, page, window, cx);
            graph.tab_mode = true;
            graph
        });
        cx.observe(&graph, |_, _, cx| cx.notify()).detach();
        self.graph = Some(graph);
        cx.notify();
    }

    /// Opened without the run page's data (from a Threads card): read the run
    /// page for its DAG first.
    fn load(&mut self, workspace: WeakEntity<Workspace>, window: &mut Window, cx: &mut Context<Self>) {
        let Some((bin, home)) = workspace.upgrade().and_then(|ws| {
            let store = BoardStore::for_workspace(&ws, cx)?;
            let store = store.read(cx);
            Some((store.bin.clone(), store.home.clone()?))
        }) else {
            self.error = Some("No mini-ork home for this project.".into());
            return;
        };
        let run = self.run_id.clone();
        let args = BTreeMap::from([("run".to_string(), run.clone())]);
        self._load = cx.spawn_in(window, async move |this, cx| {
            let page = cx
                .background_spawn(board::fetch_page(bin, home, "run".into(), None, args))
                .await;
            this.update_in(cx, |this, window, cx| match page {
                Ok(page) => match Model::from_page(&page, &run) {
                    Some(model) => this.attach(model, workspace, WeakEntity::new_invalid(), window, cx),
                    None => {
                        this.error = Some(format!("Run {run} has no DAG to show."));
                        cx.notify();
                    }
                },
                Err(error) => {
                    this.error = Some(error.to_string());
                    cx.notify();
                }
            })
            .ok();
        });
    }

    /// The node the tab shows now (↑/↓ in the inspector moves it).
    fn current_node(&self, cx: &App) -> String {
        self.graph
            .as_ref()
            .and_then(|g| g.read(cx).selected_node())
            .unwrap_or_else(|| self.node_id.clone())
    }
}

impl EventEmitter<()> for MiniOrkNodeTab {}

impl Focusable for MiniOrkNodeTab {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl Item for MiniOrkNodeTab {
    type Event = ();

    fn tab_content_text(&self, _detail: usize, cx: &App) -> SharedString {
        self.current_node(cx).into()
    }

    fn tab_content(&self, params: TabContentParams, _window: &Window, cx: &App) -> AnyElement {
        h_flex()
            .gap(rems_from_px(7_f32))
            .child(
                div()
                    .text_size(rems_from_px(11.5_f32))
                    .text_color(c(if params.selected { p::BLUE } else { p::SUB }))
                    .child("⬡"),
            )
            .child(Label::new(self.current_node(cx)).single_line().color(params.text_color()))
            .child(
                Label::new(short_run(&self.run_id))
                    .size(LabelSize::XSmall)
                    .single_line()
                    .color(Color::Muted),
            )
            .into_any_element()
    }

    fn tab_tooltip_text(&self, cx: &App) -> Option<SharedString> {
        let node = self.current_node(cx);
        Some(if self.title.is_empty() {
            format!("{node} · {}", self.run_id).into()
        } else {
            format!("{node} · {} ({})", self.title, self.run_id).into()
        })
    }
}

impl Render for MiniOrkNodeTab {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let body: AnyElement = match (&self.graph, &self.error) {
            (Some(graph), _) => graph.clone().into_any_element(),
            (None, Some(error)) => p::text(error.clone(), 12.5, p::RED).p_4().into_any_element(),
            (None, None) => div()
                .p_4()
                .child(crate::loading::loading_line(format!("board page run --arg run={}", self.run_id), cx))
                .into_any_element(),
        };
        div()
            .track_focus(&self.focus_handle)
            .size_full()
            .bg(c(p::BG))
            .child(body)
    }
}

/// `…160033` — the tail of a run id, enough to tell tabs apart.
fn short_run(run_id: &str) -> String {
    let tail: String = run_id.chars().rev().take(6).collect::<Vec<_>>().into_iter().rev().collect();
    format!("…{tail}")
}

/// Open (or bring forward) the tab for `node` of `run_id`. `model` is the run
/// page's DAG when the caller has it; otherwise the tab reads it.
pub fn open_node_tab(
    workspace: &Entity<Workspace>,
    run_id: String,
    node_id: String,
    title: String,
    model: Option<Model>,
    page: WeakEntity<MiniOrkPage>,
    window: &mut Window,
    cx: &mut App,
) {
    workspace.update(cx, |workspace, cx| {
        let existing = workspace
            .items_of_type::<MiniOrkNodeTab>(cx)
            .find(|tab| tab.read(cx).run_id == run_id && tab.read(cx).node_id == node_id);
        if let Some(existing) = existing {
            workspace.activate_item(&existing, true, true, window, cx);
            return;
        }
        let weak = cx.entity().downgrade();
        let tab = cx.new(|cx| MiniOrkNodeTab::new(run_id, node_id, title, model, weak, page, window, cx));
        workspace.add_item_to_active_pane(Box::new(tab), None, true, window, cx);
    });
}
