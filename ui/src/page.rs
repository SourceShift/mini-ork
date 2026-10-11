//! A mini-ork page as an editor tab: one generic renderer for every
//! `mini-ork board page <key>` document (see `mini_ork/ide_pages/spec.py`).
//! A run's tab is the `run` page with `args.run`.

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    sync::Arc,
    time::Duration,
};

use gpui::{
    AnyElement, App, ClickEvent, Context, Entity, EventEmitter, FocusHandle, Focusable, Render,
    SharedString, Task, WeakEntity, Window, px,
};
use serde_json::Value;
use gpui::{
    Animation, AnimationExt as _, AnchoredPositionMode, DismissEvent, ScrollHandle, Subscription, anchored,
    deferred, point,
};
use ui::{ScrollAxes, Scrollbars, WithScrollbar as _, prelude::*};
use ui_input::{ERASED_EDITOR_FACTORY, ErasedEditor, ErasedEditorEvent};
use workspace::{
    Workspace,
    item::{Item, TabContentParams},
};

use crate::{
    board, components,
    loading::{self, Shape},
    select::{Look, Texts},
    palette::{self as p, c},
    store::{self, BoardStore, Origin},
};

/// The most lines of a page's `desc` the header draws. Mirrors `_DESC_LINES`
/// in `mini_ork/ide_pages/run.py`, which wraps the description to this many
/// lines; a page is free to send none at all.
const MAX_DESC_LINES: usize = 5;

/// The glyph each page shows in its tab and in the activity bar.
pub fn glyph(key: &str) -> &'static str {
    match key {
        "run" => "◇",
        "orch" => "✦",
        "runs" => "≡",
        "changes" => "⑂",
        "verify" => "✓",
        "recipes" => "▥",
        "autos" => "◷",
        "lanes" => "$",
        "learn" => "❖",
        "context" => "◫",
        "nodes" => "⬡",
        "setup" => "⚙",
        "features" => "◈",
        _ => "◇",
    }
}

/// The tab label before the page has loaded.
pub fn default_label(key: &str) -> &'static str {
    match key {
        "orch" => "Orchestrator",
        "runs" => "Runs",
        "changes" => "Changes",
        "verify" => "Verify & safety",
        "recipes" => "Recipes & epics",
        "autos" => "Automations",
        "lanes" => "Lanes & cost",
        "learn" => "Learning & memory",
        "context" => "Context",
        "nodes" => "Nodes",
        "setup" => "Setup & health",
        "features" => "Features",
        _ => "mini-ork",
    }
}

/// `run-1791302645-a41c` → `run-a41c`; `run-le-1791314676-64879-1` →
/// `run-64879-1`: whatever follows the epoch seconds, else the last part.
fn run_label(run_id: &str) -> String {
    let parts: Vec<&str> = run_id.split('-').collect();
    let epoch = parts
        .iter()
        .position(|part| part.len() >= 9 && part.chars().all(|c| c.is_ascii_digit()));
    match epoch {
        Some(i) if i + 1 < parts.len() => format!("run-{}", parts[i + 1..].join("-")),
        _ => match run_id.rsplit_once('-') {
            Some((_, tail)) if !tail.is_empty() => format!("run-{tail}"),
            _ => run_id.to_string(),
        },
    }
}

/// The flow map's replay: a clock that walks the run's `flow.timeline` and
/// overrides the drawn steps' states while it plays (or is paused mid-way).
/// The clock is a wall-time remap — at 1× the whole timeline plays in about
/// 12 seconds whatever the run's real length — so `now` is in the timeline's
/// own epoch seconds, not wall seconds.
pub(crate) struct Replay {
    /// The run the replay belongs to; a run change stops it.
    pub(crate) run: String,
    /// The clock position, in epoch seconds, between `start` and `end`.
    pub(crate) now: f64,
    /// The timeline's first and last `t`.
    pub(crate) start: f64,
    pub(crate) end: f64,
    /// Whether the clock is advancing.
    pub(crate) playing: bool,
    /// The chosen speed multiplier (0.5, 1 or 2); kept when a replay stops so
    /// the controls keep showing the picked speed.
    pub(crate) speed: f32,
    /// True while `now` overrides the map's real states (playing, or paused
    /// mid-way). Cleared at the end and on Restart, so the map shows truth.
    pub(crate) active: bool,
    /// Bumped whenever the clock is (re)started or stopped, so a ticker left
    /// over from an earlier replay sees the change and exits.
    pub(crate) epoch: u64,
}

impl Default for Replay {
    fn default() -> Self {
        Self { run: String::new(), now: 0., start: 0., end: 0., playing: false, speed: 1., active: false, epoch: 0 }
    }
}

/// A `composer` section's editor and its buffer subscription. One per
/// composer section of the loaded page (see [`MiniOrkPage::composers`]); a
/// keystroke re-renders the page so the Send button's enabled state tracks
/// the buffer.
pub(crate) struct Composer {
    pub(crate) editor: Arc<dyn ErasedEditor>,
    _sub: Subscription,
}

pub struct MiniOrkPage {
    pub(crate) key: String,
    pub(crate) args: BTreeMap<String, String>,
    tab: Option<String>,
    title_hint: String,
    /// True when the page is hosted in the right-hand Run panel: it hides the
    /// page header and its outer padding, so the panel's own chrome shows.
    dock: bool,
    pub(crate) workspace: WeakEntity<Workspace>,
    focus_handle: FocusHandle,
    data: Option<Result<Value, String>>,
    loading: bool,
    /// A user switched tab or filter and the new content is on its way: the
    /// content area shows a loader instead of the previous content.
    switching: bool,
    /// Every (tab, args) result seen this session: a revisited tab shows at
    /// once and refreshes under the sweep bar.
    cache: HashMap<String, Value>,
    /// The section shapes each tab last had, for its skeleton.
    shapes: HashMap<String, Vec<Shape>>,
    /// Bumped whenever fresh content replaces a skeleton (its fade-in key).
    fade: u64,
    /// The DAG card scrolls sideways, as in the design.
    dag_scroll: ScrollHandle,
    /// The DAG canvas' pan-in-progress anchor: the pointer's last position and
    /// the scroll offset it was handed at the time. The canvas is the same one
    /// a node tab draws, so it drags the same way; the state has to live on the
    /// page because the page — not a `MiniOrkGraph` — is what renders it here.
    dag_drag: std::rc::Rc<std::cell::Cell<Option<(gpui::Point<gpui::Pixels>, gpui::Point<gpui::Pixels>)>>>,
    /// The run flow map scrolls sideways over its own track.
    flow_scroll: ScrollHandle,
    /// The Graph tab's Steps · Flow map toggle: true draws the flow map and
    /// hides the step graph (DAG). Default false = Steps. It lives on the
    /// page, so it survives reloads of the same run.
    show_flow_map: bool,
    /// The stage a flow-map chip just jumped to, with a sequence number the
    /// 1.5s highlight timer checks before clearing it (so a second click
    /// restarts the glow rather than letting the first timer end it).
    pub(crate) flow_focus: Option<(String, u64)>,
    /// The flow map's replay clock and the states it overlays while it runs.
    pub(crate) replay: Replay,
    /// Selectable text for everything this page shows.
    pub(crate) texts: Texts,
    /// The run graph, full screen, when open.
    graph: Option<Entity<crate::graph::MiniOrkGraph>>,
    /// Open the graph (on this node, if any) once the page has loaded.
    pub(crate) pending_graph: Option<Option<String>>,
    /// The DAG tab's run graph: the same view a node tab hosts, so a node
    /// clicked in the page's DAG is inspected in the page — its transcript,
    /// retry, gate and merge below the canvas — instead of in a second tab.
    /// Built on the frame after the DAG is on screen, because it needs a
    /// window (see `ensure_dag_graph`).
    dag_graph: Option<Entity<crate::graph::MiniOrkGraph>>,
    _graph_sub: Option<Subscription>,
    /// Board reads this page has followed (it re-reads when the board moves).
    seen_board: u64,
    last_reload: std::time::Instant,
    _store_sub: Option<Subscription>,
    generation: u64,
    /// Expansion state for `story` step rows and per-file/per-check cards,
    /// keyed `"<section>-<id>"` (e.g. `"story-planner"`,
    /// `"s0-files-0:crates/mini_ork_ui/src/sections.rs"`).
    pub(crate) expanded: HashSet<String>,
    /// Editors for the loaded page's `composer` sections, keyed by the
    /// section's index in `sections` (the index `render_section` is given).
    pub(crate) composers: HashMap<usize, Composer>,
    /// Story step ids whose `open` default has been applied for the current
    /// run. Each step is seeded once, the first time a load carries it: a
    /// step that appears later (a live run) still gets its default, and a step
    /// the user collapsed stays collapsed on refresh. Reset on run change.
    seeded: HashSet<String>,
    _load: Task<()>,
}

impl MiniOrkPage {
    fn new(
        key: String,
        tab: Option<String>,
        args: BTreeMap<String, String>,
        title_hint: String,
        workspace: WeakEntity<Workspace>,
        cx: &mut Context<Self>,
    ) -> Self {
        let mut this = Self {
            key,
            args,
            tab,
            title_hint,
            dock: false,
            workspace,
            focus_handle: cx.focus_handle(),
            data: None,
            loading: false,
            switching: false,
            cache: HashMap::new(),
            shapes: HashMap::new(),
            fade: 0,
            dag_scroll: ScrollHandle::new(),
            dag_drag: Default::default(),
            flow_scroll: ScrollHandle::new(),
            show_flow_map: false,
            flow_focus: None,
            replay: Replay::default(),
            texts: Texts::default(),
            graph: None,
            pending_graph: None,
            dag_graph: None,
            _graph_sub: None,
            seen_board: 0,
            last_reload: std::time::Instant::now(),
            _store_sub: None,
            generation: 0,
            expanded: HashSet::new(),
            composers: HashMap::new(),
            seeded: HashSet::new(),
            _load: Task::ready(()),
        };
        this.reload(cx);
        this
    }

    /// The right-hand Run panel's page: the run's `view=dock` view, with the
    /// page header and outer padding hidden. `tab` picks the dock tab
    /// (`changes`, `checks`, `agents`, `cost`, `learned`), defaulting to
    /// `changes` at the server.
    pub fn new_dock(
        run: String,
        tab: Option<String>,
        workspace: WeakEntity<Workspace>,
        cx: &mut Context<Self>,
    ) -> Self {
        let mut args = BTreeMap::new();
        args.insert("run".to_string(), run);
        args.insert("view".to_string(), "dock".to_string());
        let mut this = Self::new("run".into(), tab, args, String::new(), workspace, cx);
        this.dock = true;
        this
    }

    /// Point a dock page at another run (the panel follows the active run).
    /// A no-op when it already shows that run.
    pub fn set_run(&mut self, run: String, cx: &mut Context<Self>) {
        if self.args.get("run").map(String::as_str) == Some(run.as_str()) {
            return;
        }
        let mut args = BTreeMap::new();
        args.insert("run".to_string(), run);
        args.insert("view".to_string(), "dock".to_string());
        self.set_args(args, cx);
    }

    fn label(&self) -> String {
        if self.key == "run" {
            return run_label(self.args.get("run").map(String::as_str).unwrap_or("run"));
        }
        self.data
            .as_ref()
            .and_then(|d| d.as_ref().ok())
            .and_then(|d| d.get("label").and_then(Value::as_str))
            .map(String::from)
            .unwrap_or_else(|| default_label(&self.key).to_string())
    }

    pub fn set_args(&mut self, args: BTreeMap<String, String>, cx: &mut Context<Self>) {
        let prev_run = self.args.get("run").cloned();
        for (k, v) in args {
            if k == "tab" {
                self.tab = Some(v);
            } else if v.is_empty() {
                self.args.remove(&k);
            } else {
                self.args.insert(k, v);
            }
        }
        // Expansion state is per-run: a different run gets a fresh `expanded`
        // set, and the next load will seed it from the new run's `step.open`
        // defaults (per the kickoff).
        if prev_run != self.args.get("run").cloned() {
            self.expanded.clear();
            self.seeded.clear();
            self.stop_replay();
        }
        self.begin_switch();
        self.reload(cx);
    }

    /// Stop the flow map's replay: drop the clock and the state override so the
    /// map shows the real states again. The chosen speed is kept.
    pub(crate) fn stop_replay(&mut self) {
        self.replay.active = false;
        self.replay.playing = false;
        // Bump the epoch so a ticker still in flight sees the change and exits.
        self.replay.epoch += 1;
    }

    fn cache_key(&self) -> String {
        format!("{}|{:?}", self.tab.clone().unwrap_or_default(), self.args)
    }

    /// A user asked for other content: show it from the cache when it has been
    /// seen, else a skeleton until it arrives.
    pub(crate) fn begin_switch(&mut self) {
        match self.cache.get(&self.cache_key()) {
            Some(cached) => {
                self.data = Some(Ok(cached.clone()));
                self.switching = false;
            }
            None => self.switching = true,
        }
    }

    fn source(&self) -> String {
        let mut source = format!("board page {}", self.key);
        if let Some(tab) = &self.tab {
            source.push_str(&format!(" --tab {tab}"));
        }
        for (k, v) in &self.args {
            source.push_str(&format!(" --arg {k}={v}"));
        }
        source
    }

    fn skeleton_shapes(&self) -> Vec<Shape> {
        let tab = self.tab.clone().unwrap_or_default();
        self.shapes
            .get(&tab)
            .cloned()
            .unwrap_or_else(|| loading::default_shapes(&self.key, &tab))
    }

    fn set_tab(&mut self, tab: String, cx: &mut Context<Self>) {
        // Leaving the Graph tab stops the replay (and it is only drawn there).
        self.stop_replay();
        self.tab = Some(tab);
        self.begin_switch();
        self.reload(cx);
    }

    /// Whether a fresh fetch is worth starting now: not mid-fetch, and not
    /// within two seconds of the last one. Board bumps can arrive in bursts
    /// during a live run, and each reload cancels the fetch in flight.
    pub(crate) fn may_reload(&self) -> bool {
        !self.loading && self.last_reload.elapsed() >= Duration::from_secs(2)
    }

    /// Fetch the page again; while it is live, keep refreshing it.
    pub fn reload(&mut self, cx: &mut Context<Self>) {
        self.last_reload = std::time::Instant::now();
        self.generation += 1;
        let generation = self.generation;
        self.loading = true;
        cx.notify();
        let (key, tab, args) = (self.key.clone(), self.tab.clone(), self.args.clone());
        let workspace = self.workspace.clone();
        self._load = cx.spawn(async move |this, cx| {
            loop {
                // The board poller knows the project's home; wait for its first read.
                let mut target = None;
                for _ in 0..100 {
                    target = cx.update(|cx| {
                        let workspace = workspace.upgrade()?;
                        let store = BoardStore::for_workspace(&workspace, cx)?;
                        let store = store.read(cx);
                        Some((store.bin.clone(), store.home.clone()?))
                    });
                    if target.is_some() {
                        break;
                    }
                    cx.background_executor().timer(Duration::from_millis(300)).await;
                }
                let Some((bin, home)) = target else {
                    this.update(cx, |this, cx| {
                        this.loading = false;
                        this.switching = false;
                        this.data = Some(Err("This project has no .mini-ork — run `mini-ork init` in it.".into()));
                        cx.notify();
                    })
                    .ok();
                    return;
                };
                // Show the previous answer for this page at once, so the click
                // that opened it lands on content rather than a skeleton; the
                // read below replaces it moments later.
                this.update(cx, |this, cx| {
                    if this.data.is_none() {
                        if let Some(cached) = crate::store::cached_page(&home, &key, tab.as_deref(), &args, cx) {
                            this.data = Some(Ok(cached));
                            cx.notify();
                        }
                    }
                })
                .ok();
                let result = cx
                    .background_spawn(board::fetch_page(bin, home.clone(), key.clone(), tab.clone(), args.clone()))
                    .await;
                let refresh = this
                    .update(cx, |this, cx| {
                        if this.generation != generation {
                            return None;
                        }
                        this.loading = false;
                        if this.switching || this.data.is_none() {
                            this.fade += 1;
                        }
                        this.switching = false;
                        let refresh = match &result {
                            Ok(value) => value.get("refresh").and_then(Value::as_u64),
                            Err(_) => None,
                        };
                        if let (Ok(value), Some(graph)) = (&result, this.graph.clone()) {
                            if let Some(model) = this.args.get("run").and_then(|run| crate::graph::Model::from_page(value, run)) {
                                graph.update(cx, |graph, cx| graph.set_model(model, cx));
                            }
                        }
                        // The DAG tab's graph follows the page the same way a
                        // live run's nodes appear in it without a reload.
                        if let (Ok(value), Some(graph)) = (&result, this.dag_graph.clone()) {
                            if let Some(model) = this.args.get("run").and_then(|run| crate::graph::Model::from_page(value, run)) {
                                graph.update(cx, |graph, cx| graph.set_model(model, cx));
                            }
                        }
                        if let Ok(value) = &result {
                            this.cache.insert(this.cache_key(), value.clone());
                            crate::store::remember_page(&home, &key, tab.as_deref(), &args, value.clone(), cx);
                            let tab = this.tab.clone().unwrap_or_else(|| str_of(value, "tab"));
                            let shapes: Vec<Shape> = arr(value, "sections")
                                .iter()
                                .map(|s| Shape {
                                    kind: str_of(s, "type"),
                                    full: s.get("full").and_then(Value::as_bool).unwrap_or(false),
                                    titled: !str_of(s, "title").is_empty(),
                                })
                                .collect();
                            if !shapes.is_empty() {
                                this.shapes.insert(tab, shapes);
                            }
                            // Seed expansion from `step.open` for steps not
                            // seen before in this run; a run change resets it.
                            this.seed_step_expansion(value);
                        }
                        this.data = Some(result.map_err(|e| e.to_string()));
                        cx.notify();
                        refresh
                    })
                    .ok()
                    .flatten();
                // Pages follow the board (see `follow_board`); only a page that
                // asks for it refreshes on a timer.
                let seconds = refresh.unwrap_or(0);
                if seconds == 0 {
                    return;
                }
                cx.background_executor().timer(Duration::from_secs(seconds)).await;
            }
        });
    }

    /// A button/row/chip action: `set` and tab switches stay in this page;
    /// everything else goes through `store::perform` after this update ends.
    pub(crate) fn action_handler(
        &self,
        action: Option<&Value>,
        cx: &mut Context<Self>,
    ) -> Option<Box<dyn Fn(&ClickEvent, &mut Window, &mut App) + 'static>> {
        let action = action?.clone();
        if action.is_null() {
            return None;
        }
        let page = cx.entity().downgrade();
        let workspace = self.workspace.clone();
        Some(Box::new(move |_, window, cx| {
            let (action, page, workspace) = (action.clone(), page.clone(), workspace.clone());
            window.defer(cx, move |window, cx| {
                if let Some(question) = action.get("confirm").and_then(Value::as_str) {
                    let answer = window.prompt(
                        gpui::PromptLevel::Warning,
                        question,
                        None,
                        &["Yes", "Cancel"],
                        cx,
                    );
                    let mut action = action.clone();
                    if let Some(map) = action.as_object_mut() {
                        map.remove("confirm");
                    }
                    let window_handle = window.window_handle();
                    cx.spawn(async move |cx| {
                        if answer.await.ok() == Some(0) {
                            window_handle
                                .update(cx, |_, window, cx| {
                                    if let Some(workspace) = workspace.upgrade() {
                                        store::perform(&action, &workspace, Origin::Page(page), window, cx);
                                    }
                                })
                                .ok();
                        }
                    })
                    .detach();
                    return;
                }
                if let Some(workspace) = workspace.upgrade() {
                    store::perform(&action, &workspace, Origin::Page(page), window, cx);
                }
            });
        }))
    }

    pub(crate) fn button(&self, id: impl Into<ElementId>, b: &Value, height: f32, size: f32, cx: &mut Context<Self>) -> AnyElement {
        let label = str_of(b, "label");
        let kind = b.get("kind").and_then(Value::as_str).unwrap_or("default").to_string();
        let handler = self.action_handler(b.get("do"), cx);
        p::button(id, label, &kind, height, size, handler)
    }

    /// A page's `desc`: the page's own description, under its title, one row
    /// per line and at most [`MAX_DESC_LINES`] of them.
    ///
    /// The page builder wraps it to that width already (`_DESC_LINES` in
    /// `mini_ork/ide_pages/run.py`), so a row here is a line there; the cap is
    /// repeated on this side because a page that ignores the contract must not
    /// be able to push the tab bar off the bottom of its own header.
    fn render_desc(&self, desc: &str, cx: &mut Context<Self>) -> AnyElement {
        v_flex()
            .gap(rems_from_px(2_f32))
            .children(
                desc.lines()
                    .filter(|line| !line.trim().is_empty())
                    .take(MAX_DESC_LINES)
                    .enumerate()
                    .map(|(i, line)| {
                        self.texts.text(format!("page-desc-{i}"), line, Look::new(13., c(p::MUTED)), cx)
                    }),
            )
            .into_any_element()
    }

    fn render_header(&self, page: &Value, cx: &mut Context<Self>) -> AnyElement {
        let title = str_of(page, "title");
        let sub = str_of(page, "sub");
        let desc = str_of(page, "desc");
        let chips = arr(page, "chips");
        let actions = arr(page, "actions");
        let mono = p::mono_font(cx);
        h_flex()
            .items_start()
            .gap_3()
            .flex_wrap()
            .child(
                v_flex()
                    .flex_1()
                    .min_w(rems_from_px(240_f32))
                    .gap(rems_from_px(3_f32))
                    .child(
                        h_flex()
                            .gap_2p5()
                            .flex_wrap()
                            .items_center()
                            .child(self.texts.text("page-title", &title, Look::new(17., c(p::TEXT)).bold(true), cx))
                            .children(chips.iter().map(|chip| {
                                div()
                                    .px(rems_from_px(7_f32))
                                    .border_1()
                                    .border_color(c(p::BORDER2))
                                    .text_size(rems_from_px(11.5_f32))
                                    .font(mono.clone())
                                    .text_color(p::named(&str_of(chip, "c")))
                                    .child(str_of(chip, "t"))
                            })),
                    )
                    .when(!desc.is_empty(), |this| this.child(self.render_desc(&desc, cx)))
                    .when(!sub.is_empty(), |this| {
                        this.child(self.texts.text("page-sub", &sub, Look::new(12.5, c(p::SUB)), cx))
                    }),
            )
            .child(
                h_flex().gap_1p5().flex_wrap().children(
                    actions
                        .iter()
                        .enumerate()
                        .map(|(i, b)| self.button(("page-action", i), b, 26., 12.5, cx)),
                ),
            )
            .into_any_element()
    }

    fn render_tabs(&self, page: &Value, cx: &mut Context<Self>) -> Option<AnyElement> {
        let tabs = arr(page, "tabs");
        if tabs.is_empty() {
            return None;
        }
        let current = self.tab.clone().unwrap_or_else(|| str_of(page, "tab"));
        Some(
            h_flex()
                .gap_0p5()
                .border_b_1()
                .border_color(c(p::BORDER))
                .children(tabs.iter().enumerate().map(|(i, t)| {
                    let key = str_of(t, "key");
                    let on = key == current;
                    div()
                        .id(("page-tab", i))
                        .px_2p5()
                        .py_1p5()
                        .text_size(rems_from_px(12.5_f32))
                        .text_color(c(if on { p::TEXT } else { p::SUB }))
                        .border_b_2()
                        .border_color(if on { c(p::BLUE) } else { p::ca(0x00000000) })
                        .cursor_pointer()
                        .hover(|s| s.text_color(c(p::TEXT)))
                        .child(str_of(t, "label"))
                        .on_click(cx.listener(move |this, _, _, cx| this.set_tab(key.clone(), cx)))
                }))
                .into_any_element(),
        )
    }

    /// Sections in the design's two-column grid: `full` spans both; `col`
    /// pins a column (list/detail layouts); the rest flow two to a row.
    fn render_sections(
        &self,
        page: &Value,
        hide_dag: bool,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let sections = arr(page, "sections");
        let mut rows: Vec<AnyElement> = Vec::new();
        let mut group: Vec<(usize, &Value)> = Vec::new();
        let flush = |group: &mut Vec<(usize, &Value)>, rows: &mut Vec<AnyElement>, this: &Self, window: &mut Window, cx: &mut Context<Self>| {
            if group.is_empty() {
                return;
            }
            let pinned = group.iter().any(|(_, s)| s.get("col").is_some());
            if pinned {
                let (left, right): (Vec<_>, Vec<_>) =
                    group.iter().partition(|(_, s)| s.get("col").and_then(Value::as_u64) != Some(2));
                rows.push(
                    h_flex()
                        .items_start()
                        .gap_3()
                        .child(v_flex().flex_1().min_w_0().gap_3().children(
                            left.iter().map(|(i, s)| this.render_section_card(*i, s, window, cx)),
                        ))
                        .child(v_flex().flex_1().min_w_0().gap_3().children(
                            right.iter().map(|(i, s)| this.render_section_card(*i, s, window, cx)),
                        ))
                        .into_any_element(),
                );
            } else {
                for pair in group.chunks(2) {
                    let mut row = h_flex().gap_3();
                    for (i, s) in pair {
                        row = row.child(div().flex_1().min_w_0().flex().child(this.render_section_card(*i, s, window, cx)));
                    }
                    if pair.len() == 1 {
                        row = row.child(div().flex_1().min_w_0());
                    }
                    rows.push(row.into_any_element());
                }
            }
            group.clear();
        };
        for (i, s) in sections.iter().enumerate() {
            // With the flow map drawn the older column DAG below it is
            // redundant; the flow card carries its own full-screen button.
            if hide_dag && str_of(s, "type") == "dag" {
                continue;
            }
            // The embedded run graph is the inspector: it holds the clicked
            // node's transcript and its retry / gate / merge steps. The
            // section the engine sends beside the DAG is a shallower copy of
            // one node and stays empty until a node is named as a page arg.
            // Only on the DAG tab — the graph outlives a tab switch, and
            // another tab's inspector is the only one it would get.
            if self.dag_graph.is_some()
                && matches!(str_of(page, "tab").as_str(), "dag" | "graph")
                && str_of(s, "type") == "inspector"
            {
                continue;
            }
            if s.get("full").and_then(Value::as_bool).unwrap_or(false) {
                flush(&mut group, &mut rows, self, window, cx);
                // A full-width section is not drawn through `render_section_card`
                // (it spans the grid alone, so there is no card to wrap), but it
                // still needs the id: without one the inspector resolves a copy
                // of any full-width section — a callout, a hero, a list — to the
                // page element, so the copied reference cannot say which section
                // was meant. The JSON copy button is deliberately not added here:
                // these sections were never laid out to keep a top-right corner
                // clear (a hero's pill right-aligns into it).
                rows.push(
                    div()
                        .id(Self::section_id(i, s))
                        .w_full()
                        .min_w_0()
                        .child(self.render_section(i, s, window, cx))
                        .into_any_element(),
                );
            } else {
                group.push((i, s));
            }
        }
        flush(&mut group, &mut rows, self, window, cx);
        v_flex().gap_3().children(rows).into_any_element()
    }

    /// The element id that names a section's payload (`s3-dag`), shared by the
    /// carded and full-width paths so the two can never disagree.
    fn section_id(index: usize, s: &Value) -> SharedString {
        SharedString::from(format!("s{index}-{}", str_of(s, "type")))
    }

    /// A section's card, wrapped in an element whose id names the payload
    /// section (`s3-dag`). The inspector's element reference reads
    /// `<source location> · <id path of the nearest ancestor with an id>`, so
    /// without an id here every section of a page would resolve to the page
    /// element itself and a copied reference could not say which one was meant.
    fn render_section_card(&self, index: usize, s: &Value, window: &mut Window, cx: &mut Context<Self>) -> AnyElement {
        div()
            .id(Self::section_id(index, s))
            .w_full()
            .min_w_0()
            .relative()
            .child(self.render_section(index, s, window, cx))
            .child(self.section_json_copy(index, s))
            .into_any_element()
    }

    /// `{ }` — this section's payload: the JSON the engine sent and the card
    /// beside it is drawn from. Floated into the card's top-right corner, where
    /// a code block already puts its own copy button. The heads that
    /// right-align their own controls pad clear of it.
    fn section_json_copy(&self, index: usize, s: &Value) -> AnyElement {
        div()
            .absolute()
            .top(rems_from_px(6_f32))
            .right(rems_from_px(8_f32))
            .child(
                ui::CopyButton::new(
                    SharedString::from(format!("s{index}-json-copy")),
                    SharedString::from(format!("{s:#}")),
                )
                .tooltip_label("Copy this section as JSON"),
            )
            .into_any_element()
    }

    fn render_section(&self, index: usize, s: &Value, window: &mut Window, cx: &mut Context<Self>) -> AnyElement {
        let kind = str_of(s, "type");
        match kind.as_str() {
            "dag" => return self.render_dag(index, s, window, cx),
            "inspector" => return self.render_inspector(index, s, cx),
            "triage" => return self.render_triage(index, s, cx),
            "callout" => return self.render_callout(index, s, cx),
            "hero" => return self.render_hero(index, s, cx),
            "columns" => return self.render_columns(index, s, cx),
            "composer" => return self.render_composer(index, s, window, cx),
            // `story` draws its own rows full-width without a card header.
            "story" => return self.render_story(index, s, cx),
            // `table` / `kv` / `list` draw their own Orca card, with a
            // `components::section_head` and the section's own actions in
            // place of the generic title row.
            "table" => return self.render_table(index, s, cx),
            "kv" => return self.render_kv(index, s, cx),
            "list" => return self.render_list(index, s, cx),
            _ => {}
        }
        let title = str_of(s, "title");
        let note = str_of(s, "note");
        let mut actions = arr(s, "actions");
        if kind == "markdown" && !str_of(s, "path").is_empty() {
            actions.push(serde_json::json!({ "label": "Open", "kind": "ghost", "do": { "path": str_of(s, "path") } }));
        }
        let body = match kind.as_str() {
            "bars" => self.render_bars(s, cx),
            "flow" => self.render_flow(index, s, cx),
            "code" => self.render_code(index, s, cx),
            "pills" => self.render_pills(s, cx),
            "chips" => self.render_chips(index, s, cx),
            // Section renderers share their body code with their block
            // renderers (used inside a story step's body).
            "files" => self.render_files(index, s, cx),
            "findings" => self.render_findings(index, s, cx),
            "checks" => self.render_checks(index, s, cx),
            "agents" => self.render_agents(index, s, cx),
            // Full document, capped in height and scrolled inside.
            "markdown" => self
                .texts
                .nested_scroll(
                    format!("s{index}-md-box"),
                    div()
                        .id(SharedString::from(format!("s{index}-md-box")))
                        .max_h(rems_from_px(640_f32))
                        .overflow_y_scroll()
                        .pr_1p5()
                        .child(self.texts.markdown(format!("s{index}-md"), str_of(s, "text"), Look::new(13., c(p::BODY)), cx)),
                )
                .into_any_element(),
            _ => self.texts.text(
                format!("s{index}-unknown"),
                &format!("This section ({kind}) needs a newer mini-ork IDE."),
                Look::new(12., c(p::MUTED)),
                cx,
            ),
        };
        let has_head = !title.is_empty() || !actions.is_empty();
        card()
            .flex_1()
            .px_3p5()
            .py_3()
            .gap_2p5()
            .when(has_head, |this| {
                this.child(
                    h_flex()
                        .gap_2p5()
                        .flex_wrap()
                        .items_center()
                        // Clear of the copy button floated into the card's corner.
                        .pr(rems_from_px(24_f32))
                        .child(div().flex_1().min_w(rems_from_px(120_f32)).child(self.texts.text(
                            format!("s{index}-title"),
                            &title,
                            Look::new(13., c(p::TEXT)).bold(true),
                            cx,
                        )))
                        .children(actions.iter().enumerate().map(|(i, b)| {
                            self.button(SharedString::from(format!("s{index}-a{i}")), b, 24., 12., cx)
                        })),
                )
            })
            .when(!note.is_empty(), |this| {
                this.child(div().mt(rems_from_px(-4_f32)).child(self.texts.text(format!("s{index}-note"), &note, Look::new(12., c(p::MUTED)), cx)))
            })
            .child(body)
            .into_any_element()
    }

    fn render_table(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let cols = arr(s, "cols");
        let head = arr(s, "head");
        let rows = arr(s, "rows");
        let mono = p::mono_font(cx);
        let sized = |el: Div, i: usize| -> Div {
            match cols.get(i) {
                Some(col) => match col.get("w").and_then(Value::as_f64) {
                    Some(w) => el.w(px(w as f32)).flex_none(),
                    None => el
                        .flex_grow(col.get("fr").and_then(Value::as_f64).unwrap_or(1.0) as f32)
                        .flex_basis(px(0.))
                        .min_w(px(col.get("min").and_then(Value::as_f64).unwrap_or(0.0) as f32)),
                },
                None => el.flex_1().min_w_0(),
            }
        };
        let header = h_flex()
            .gap_2p5()
            .px_1p5()
            .pt_0p5()
            .pb_1p5()
            .border_b_1()
            .border_color(c(p::BORDER))
            .children(head.iter().enumerate().map(|(i, h)| {
                sized(div(), i)
                    .text_size(rems_from_px(10.5_f32))
                    .text_color(c(p::SUB))
                    .whitespace_nowrap()
                    .overflow_hidden()
                    .child(h.as_str().unwrap_or_default().to_ascii_uppercase())
            }));
        let row_count = rows.len();
        let body: Vec<AnyElement> = rows.iter().enumerate().map(|(r, row)| {
            let handler = self.action_handler(row.get("do"), cx);
            let selected = row.get("sel").and_then(Value::as_bool).unwrap_or(false);
            let acts = arr(row, "acts");
            let cells = arr(row, "cells");
            let clickable = handler.is_some();
            let row_text = cells.iter().map(|cell| str_of(cell, "t")).collect::<Vec<_>>().join("\t");
            // The cells carry the row's click; a row's own buttons are drawn
            // beside them, so Remove is never read as "open this project".
            let cells_line = h_flex()
                .flex_1()
                .min_w_0()
                .gap_2p5()
                .children(cells.iter().enumerate().map(|(i, cell)| {
                    let colour = p::named(&str_of(cell, "c"));
                    let text = str_of(cell, "t");
                    let mono_cell = bool_of(cell, "mono");
                    let size = if mono_cell { 11.5 } else { 12.5 };
                    if clickable {
                        // A row that opens something keeps plain text, so a
                        // click is a click; right-click copies it.
                        let mut el = sized(div(), i).text_size(rems_from_px(size)).text_color(colour).truncate().child(text);
                        if mono_cell {
                            el = el.font(mono.clone());
                        }
                        if bool_of(cell, "b") {
                            el = el.font_weight(gpui::FontWeight::SEMIBOLD);
                        }
                        el.into_any_element()
                    } else {
                        sized(div(), i)
                            .overflow_hidden()
                            .child(self.texts.text(
                                format!("s{index}-r{r}-c{i}"),
                                &text,
                                Look::new(size, colour).mono(mono_cell).bold(bool_of(cell, "b")).nowrap(),
                                cx,
                            ))
                            .into_any_element()
                    }
                }));
            // `on_click` is on `StatefulInteractiveElement`, so the clickable
            // line needs an id of its own before it can take the handler.
            let cells_line = match handler {
                Some(handler) => cells_line
                    .id(SharedString::from(format!("s{index}-r{r}-cells")))
                    .cursor_pointer()
                    .on_click(handler)
                    .into_any_element(),
                None => cells_line.into_any_element(),
            };
            // A single 1px rule between rows only; hover is a clickable row's
            // affordance, so a plain row does not light up under the pointer.
            h_flex()
                .id(SharedString::from(format!("s{index}-r{r}")))
                .h(rems_from_px(30_f32))
                .gap_2p5()
                .px_1p5()
                .when(r + 1 < row_count, |this| this.border_b_1().border_color(c(p::BORDER)))
                .when(selected, |this| this.bg(c(p::ROW_SELECTED)))
                .when(clickable, |this| this.hover(|style| style.bg(c(p::HOVER))))
                // Right-click copies the row, wherever it is.
                .on_mouse_down(gpui::MouseButton::Right, move |_, _, cx| {
                    cx.write_to_clipboard(gpui::ClipboardItem::new_string(row_text.clone()));
                })
                .child(cells_line)
                .when(!acts.is_empty(), |this| {
                    this.child(h_flex().flex_none().gap(rems_from_px(5_f32)).children(
                        acts.iter().enumerate().map(|(a, b)| {
                            self.button(SharedString::from(format!("s{index}-r{r}-a{a}")), b, 22., 11.5, cx)
                        }),
                    ))
                })
                .into_any_element()
        }).collect();
        self.orca_section(index, s, Some(row_count), cx, v_flex().child(header).children(body).into_any_element())
    }

    fn render_kv(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let items = arr(s, "items");
        let body = v_flex()
            .gap_1p5()
            .children(items.iter().enumerate().map(|(i, item)| {
                let sub = str_of(item, "sub");
                let mono = bool_of(item, "mono");
                h_flex()
                    .items_start()
                    .gap_3()
                    .child(
                        // Right-aligned keys, so the values form a column.
                        h_flex()
                            .w(rems_from_px(120_f32))
                            .flex_none()
                            .justify_end()
                            .text_size(rems_from_px(11.5_f32))
                            .text_color(c(p::SUB))
                            .child(str_of(item, "k")),
                    )
                    .child(
                        v_flex()
                            .flex_1()
                            .min_w_0()
                            .gap(rems_from_px(1_f32))
                            .child(self.texts.text(
                                format!("s{index}-v{i}"),
                                str_of(item, "v"),
                                Look::new(12.5, p::named(&str_of(item, "c"))).mono(mono),
                                cx,
                            ))
                            .when(!sub.is_empty(), |this| {
                                this.child(self.texts.text(
                                    format!("s{index}-ks{i}"),
                                    &sub,
                                    Look::new(11., c(p::SUB)).mono(true),
                                    cx,
                                ))
                            }),
                    )
            }))
            .into_any_element();
        self.orca_section(index, s, None, cx, body)
    }

    fn render_list(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let items = arr(s, "items");
        let body = v_flex()
            .gap_2()
            .children(items.iter().enumerate().map(|(i, item)| {
                let sub = str_of(item, "sub");
                let acts = arr(item, "acts");
                let title = self.texts.text(
                    format!("s{index}-i{i}"),
                    str_of(item, "t"),
                    Look::new(12.5, p::named(&str_of(item, "tc"))).mono(bool_of(item, "mono")),
                    cx,
                );
                h_flex()
                    .items_start()
                    .gap(rems_from_px(9_f32))
                    .child(
                        div()
                            .flex_none()
                            .w_3p5()
                            .mt(px(1.))
                            .text_size(rems_from_px(12_f32))
                            .text_color(p::named(&str_of(item, "mc")))
                            .child(str_of(item, "m")),
                    )
                    .child(
                        v_flex()
                            .flex_1()
                            .min_w_0()
                            .gap(rems_from_px(1_f32))
                            .child(title)
                            .when(!sub.is_empty(), |this| {
                                this.child(self.texts.text(format!("s{index}-is{i}"), &sub, Look::new(11., c(p::SUB)), cx))
                            }),
                    )
                    .when(!acts.is_empty(), |this| {
                        // The row's actions are all ghost buttons: the Orca
                        // list keeps its weight in the title, not the chrome.
                        this.child(h_flex().flex_none().gap(rems_from_px(5_f32)).children(acts.iter().enumerate().map(
                            |(a, b)| {
                                let handler = self.action_handler(b.get("do"), cx);
                                p::button(
                                    SharedString::from(format!("s{index}-i{i}-a{a}")),
                                    str_of(b, "label"),
                                    "ghost",
                                    22.,
                                    11.5,
                                    handler,
                                )
                            },
                        )))
                    })
            }))
            .into_any_element();
        self.orca_section(index, s, Some(items.len()), cx, body)
    }

    /// The Orca card the restyled `table` / `kv` / `list` sections share:
    /// `components::card()`, headed by a `components::section_head` carrying
    /// the section's own actions, the note under the head, then the body.
    fn orca_section(
        &self,
        index: usize,
        s: &Value,
        count: Option<usize>,
        cx: &mut Context<Self>,
        body: AnyElement,
    ) -> AnyElement {
        let title = str_of(s, "title");
        let note = str_of(s, "note");
        let actions = arr(s, "actions");
        components::card()
            .flex_1()
            .min_w_0()
            .gap_2p5()
            .child(
                h_flex()
                    .gap_2p5()
                    .flex_wrap()
                    .items_center()
                    // Clear of the copy button floated into the card's corner.
                    .pr(rems_from_px(24_f32))
                    .child(div().flex_1().min_w(rems_from_px(120_f32)).child(components::section_head(&title, count)))
                    .children(actions.iter().enumerate().map(|(i, b)| {
                        self.button(SharedString::from(format!("s{index}-a{i}")), b, 24., 12., cx)
                    })),
            )
            .when(!note.is_empty(), |this| {
                this.child(self.texts.text(
                    format!("s{index}-note"),
                    &note,
                    Look::new(11., c(p::SUB)),
                    cx,
                ))
            })
            .child(body)
            .into_any_element()
    }

    fn render_bars(&self, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let mono = p::mono_font(cx);
        v_flex()
            .gap_1p5()
            .children(arr(s, "items").iter().map(|item| {
                let pct = item.get("pct").and_then(Value::as_f64).unwrap_or(1.0).clamp(1.0, 100.0);
                h_flex()
                    .gap_2p5()
                    .items_center()
                    .child(p::text(str_of(item, "label"), 12., p::MUTED).w_40().flex_none().truncate())
                    .child(
                        div()
                            .flex_1()
                            .h_2()
                            .bg(c(p::BG))
                            .overflow_hidden()
                            .child(
                                div()
                                    .h_full()
                                    .w(gpui::relative(pct as f32 / 100.))
                                    .bg(p::named(&str_of(item, "c"))),
                            ),
                    )
                    .child(
                        p::text(str_of(item, "val"), 11.5, p::TEXT)
                            .w(rems_from_px(72_f32))
                            .flex_none()
                            .text_right()
                            .font(mono.clone()),
                    )
            }))
            .into_any_element()
    }

    fn render_flow(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let mut row = h_flex().flex_wrap().items_center().gap_1p5();
        for (i, item) in arr(s, "items").iter().enumerate() {
            if i > 0 {
                row = row.child(p::text("→", 13., p::DIM));
            }
            let sub = str_of(item, "sub");
            row = row.child(
                v_flex()
                    .px(rems_from_px(9_f32))
                    .py(rems_from_px(5_f32))
                    .border_1()
                    .border_color(c(p::BORDER2))
                    .bg(c(p::BG))
                    .child(self.texts.text(
                        format!("s{index}-f{i}"),
                        str_of(item, "label"),
                        Look::new(12.5, p::named(&str_of(item, "c"))),
                        cx,
                    ))
                    .when(!sub.is_empty(), |this| {
                        this.child(self.texts.text(format!("s{index}-fs{i}"), &sub, Look::new(10.5, c(p::SUB)), cx))
                    }),
            );
        }
        row.into_any_element()
    }

    fn render_code(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        code_box(&self.texts, &format!("s{index}-code"), arr(s, "lines"), cx).into_any_element()
    }

    fn render_pills(&self, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let mono = p::mono_font(cx);
        h_flex()
            .flex_wrap()
            .gap_1p5()
            .children(arr(s, "items").iter().map(|item| {
                div()
                    .px(rems_from_px(7_f32))
                    .py(rems_from_px(1_f32))
                    .border_1()
                    .border_color(c(p::BORDER2))
                    .text_size(rems_from_px(11.5_f32))
                    .font(mono.clone())
                    .text_color(p::named(&str_of(item, "c")))
                    .child(str_of(item, "t"))
            }))
            .into_any_element()
    }

    fn render_chips(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        h_flex()
            .flex_wrap()
            .gap_1p5()
            .children(arr(s, "items").iter().enumerate().map(|(i, item)| {
                let on = bool_of(item, "on");
                let handler = self.action_handler(item.get("do"), cx);
                let chip = h_flex()
                    .id(SharedString::from(format!("s{index}-chip{i}")))
                    .h_6()
                    .px_2p5()
                    .items_center()
                    .border_1()
                    .border_color(c(if on { p::DIM } else { p::BORDER2 }))
                    .when(on, |this| this.bg(c(p::SELECTED)))
                    .text_size(rems_from_px(12_f32))
                    .text_color(c(if on { p::TEXT } else { p::MUTED }))
                    .child(str_of(item, "t"));
                match handler {
                    Some(handler) => chip.cursor_pointer().on_click(handler).into_any_element(),
                    None => chip.into_any_element(),
                }
            }))
            .into_any_element()
    }

    fn render_dag(&self, index: usize, s: &Value, window: &mut Window, cx: &mut Context<Self>) -> AnyElement {
        let mono = p::mono_font(cx);
        // The canvas a node tab leads with, drawn here from the same model, so
        // the run page's DAG and the DAG tab's DAG cannot drift into two
        // pictures of one run. The payload is already in memory (`self.data`),
        // so unlike a node tab this needs no fetch — only the model's columns.
        let model = self.args.get("run").and_then(|run| {
            self.data
                .as_ref()
                .and_then(|data| data.as_ref().ok())
                .and_then(|page| crate::graph::Model::from_page(page, run))
        });
        // The legacy path: an engine that predates the page-level `graph`
        // object still describes the DAG as a section of columns. Built only
        // when there is no model to draw, so the canvas path pays nothing.
        let fallback: Option<AnyElement> = model.is_none().then(|| {
            // Columns keep their width so a wide DAG overflows and scrolls.
            let mut row = h_flex().flex_none().items_center().gap_2p5();
            for (ci, column) in arr(s, "cols").iter().enumerate() {
                if ci > 0 {
                    row = row.child(p::text("→", 14., p::DIM).flex_none());
                }
                let nodes = column.as_array().cloned().unwrap_or_default();
                row = row.child(v_flex().flex_none().gap_2().children(nodes.iter().enumerate().map(|(ni, node)| {
                    let state = str_of(node, "state");
                    let selected = bool_of(node, "sel");
                    let (mark, colour) = match state.as_str() {
                        "done" => ("✓", p::GREEN),
                        "running" => ("●", p::BLUE),
                        "failed" => ("✗", p::RED),
                        "skipped" => ("–", p::DIM),
                        _ => ("○", p::DIM),
                    };
                    let status = match state.as_str() {
                        "running" => "working".to_string(),
                        other => other.to_string(),
                    };
                    let cost = str_of(node, "cost");
                    let status = if cost.is_empty() { status } else { format!("{status} · {cost}") };
                    let lane = str_of(node, "lane");
                    let role = str_of(node, "role");
                    let border = if selected {
                        c(p::BLUE)
                    } else if state == "running" {
                        p::ca(0x74ade880)
                    } else {
                        c(p::BORDER2)
                    };
                    // Any node opens the run graph full screen on that node's stream.
                    let node_id = str_of(node, "id");
                    let el = v_flex()
                        .id(SharedString::from(format!("s{index}-n{ci}-{ni}")))
                        .w_32()
                        .px(rems_from_px(9_f32))
                        .py(rems_from_px(7_f32))
                        .gap_0p5()
                        .border_1()
                        .border_color(border)
                        .bg(c(if state == "pending" { p::BG } else { p::NODE_BG }))
                        .child(
                            h_flex()
                                .gap_1p5()
                                .min_w_0()
                                .child(p::lane_mark(&lane, 18.))
                                .child(
                                    div()
                                        .min_w_0()
                                        .truncate()
                                        .text_size(rems_from_px(12.5_f32))
                                        .font_weight(gpui::FontWeight::SEMIBOLD)
                                        .text_color(c(p::TEXT))
                                        .child(str_of(node, "label")),
                                ),
                        )
                        .child(
                            h_flex()
                                .gap(rems_from_px(5_f32))
                                .min_w_0()
                                .child(p::text(p::lane_label(&lane), 11., p::TEXT).truncate())
                                .when(!role.is_empty(), |this| {
                                    this.child(p::text(format!("· {role}"), 11., p::SUB).truncate())
                                }),
                        )
                        .child(
                            h_flex()
                                .gap_1p5()
                                .child(p::text(mark, 10., colour))
                                .child(p::text(status, 11., colour).font(mono.clone())),
                        );
                    el.cursor_pointer()
                        .hover(|s| s.border_color(c(p::BLUE)))
                        .on_click(cx.listener(move |this, _, window, cx| {
                            this.open_node_tab(node_id.clone(), window, cx)
                        }))
                        .into_any_element()
                })));
            }
            row.into_any_element()
        });
        let legend = str_of(s, "legend");
        // The DAG is a two-axis viewport: a wide run scrolls sideways, a deep
        // one scrolls down. Bounding the height keeps a tall DAG from growing
        // past the window and pushing the sections below out of reach.
        //
        // The canvas brings its own scroll container and pan handlers (see
        // `graph::render_canvas`), and exactly one element may track a handle — a
        // second container would apply the same offset again and move the
        // content at double speed. So the canvas path hands it a bounded box
        // and nothing else; only the legacy column row needs a scroller here.
        let dag_height = (f32::from(window.viewport_size().height) - 200.).clamp(240., 760.);
        let scroller = match model {
            Some(model) => {
                // Fit against the box the canvas actually gets — the container's
                // bounds from the last frame — and not against the window. This
                // section sits inside the page's side padding and the card's, so
                // a window-based guess would fit the run into a box wider than
                // this one and clip the last column. A resized window re-fits on
                // the next frame for the same reason. The first frame has no
                // bounds yet; fitting to 1:1 covers it and the next frame
                // corrects.
                let measured = f32::from(self.dag_scroll.bounds().size.width);
                let zoom = crate::graph::fit_zoom(measured, &model, 0.);
                let page = cx.entity().downgrade();
                // Clicking a node here opens the node's own tab, the same
                // detail a click in the run graph's DAG opens. The page handle
                // travels with it so the inspector's retry/gate/merge steps can
                // act through `Origin::Page` rather than going nowhere.
                let on_node = std::rc::Rc::new(move |node: &str, window: &mut Window, cx: &mut App| {
                    page.update(cx, |this, cx| this.open_node_tab(node.to_string(), window, cx)).ok();
                });
                div()
                    .id(("dag-scroll", index))
                    .relative()
                    .w_full()
                    .min_w_0()
                    .h(px(dag_height))
                    .child(crate::graph::render_canvas(
                        &model,
                        zoom,
                        None,
                        "",
                        &self.dag_scroll,
                        &self.dag_drag,
                        on_node,
                        cx,
                    ))
            }
            None => div()
                .id(("dag-scroll", index))
                .relative()
                .w_full()
                .min_w_0()
                .max_h(px(dag_height))
                .pt_9()
                .pb_3()
                .child(fallback.unwrap_or_else(|| div().into_any_element()))
                .custom_scrollbars(
                    Scrollbars::always_visible(ScrollAxes::Both).tracked_scroll_handle(&self.dag_scroll),
                    window,
                    cx,
                ),
        };
        // Once the run graph exists it leads: the same strip a node tab shows,
        // with the clicked node's inspector under it, so the page can be read
        // and acted on without opening a second tab. The canvas above is the
        // first frame's stand-in — the graph is built a frame later, because
        // building it needs a window.
        let body: AnyElement = match self.dag_graph.clone() {
            Some(graph) => {
                // Tall enough for the strip plus a transcript, short enough
                // that the run's own panels below it stay in reach.
                let graph_height = (f32::from(window.viewport_size().height) - 320.).clamp(420., 720.);
                div().w_full().min_w_0().h(px(graph_height)).child(graph).into_any_element()
            }
            None => scroller.into_any_element(),
        };
        // An icon, not a label: the canvas it acts on is the whole card, so a
        // text button either sat in the padding above it or, as a later child
        // of the card, was painted over by the canvas itself. Nested in a
        // wrapper with the canvas and added after it, so it draws on top and
        // stays over the graph however tall the card grows. Top-left, where
        // the flow map keeps its own copy of this control and where the
        // embedded canvas has no chrome of its own to collide with.
        let full_screen = div()
            .id(("dag-full", index))
            .absolute()
            .top(rems_from_px(8_f32))
            .left(rems_from_px(8_f32))
            .size(rems_from_px(26_f32))
            .flex()
            .items_center()
            .justify_center()
            .rounded(rems_from_px(6_f32))
            .border_1()
            .border_color(c(p::BORDER2))
            .bg(c(p::BG))
            .cursor_pointer()
            .hover(|s| s.bg(c(p::HOVER)))
            .tooltip(ui::Tooltip::text("Full screen · inspect every node"))
            .child(
                Icon::new(IconName::MaximizeAlt)
                    .size(IconSize::Small)
                    .color(c(p::MUTED).into()),
            )
            .on_click(cx.listener(|this, _, window, cx| this.open_graph(None, window, cx)));
        card()
            .relative()
            .p_4()
            .child(
                div()
                    .relative()
                    .w_full()
                    .min_w_0()
                    .child(body)
                    .child(full_screen),
            )
            .when(!legend.is_empty(), |this| this.child(p::text(legend, 11.5, p::SUB).mt(px(12.))))
            .into_any_element()
    }

    /// The Graph tab's `Steps · Flow map` toggle: which of the two run views
    /// leads. The choice is `show_flow_map` on the page, so it outlives a
    /// reload of the same run.
    fn render_graph_toggle(&self, cx: &mut Context<Self>) -> AnyElement {
        let mono = p::mono_font(cx);
        let steps_on = !self.show_flow_map;
        h_flex()
            .gap_2()
            .items_center()
            .font(mono)
            .text_size(rems_from_px(11_f32))
            .child(
                div()
                    .id("graph-toggle-steps")
                    .text_color(c(if steps_on { p::TEXT } else { p::SUB }))
                    .cursor_pointer()
                    .hover(|s| s.text_color(c(p::TEXT)))
                    .child("Steps")
                    .on_click(cx.listener(|this, _, _, cx| {
                        this.show_flow_map = false;
                        cx.notify();
                    })),
            )
            .child(p::text("·", 11., p::DIM))
            .child(
                div()
                    .id("graph-toggle-flow")
                    .text_color(c(if steps_on { p::SUB } else { p::TEXT }))
                    .cursor_pointer()
                    .hover(|s| s.text_color(c(p::TEXT)))
                    .child("Flow map")
                    .on_click(cx.listener(|this, _, _, cx| {
                        this.show_flow_map = true;
                        cx.notify();
                    })),
            )
            .into_any_element()
    }

    fn render_inspector(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let state = str_of(s, "state");
        let (mark, colour) = match state.as_str() {
            "done" => ("✓", p::GREEN),
            "running" => ("●", p::BLUE),
            "failed" => ("✗", p::RED),
            _ => ("○", p::DIM),
        };
        let lines = arr(s, "lines");
        let _ = p::mono_font(cx);
        let note = str_of(s, "steer_note");
        let left = card()
            .flex_1()
            .min_w_0()
            .px_3p5()
            .py_3()
            .gap_2p5()
            .child(
                h_flex()
                    .gap_2()
                    .child(p::text(mark, 13., colour))
                    .child(
                        div()
                            .text_size(rems_from_px(13_f32))
                            .font_weight(gpui::FontWeight::SEMIBOLD)
                            .text_color(c(p::TEXT))
                            .child(str_of(s, "label")),
                    )
                    .child(p::text(format!("{} · {}", str_of(s, "role"), state), 12., p::SUB)),
            )
            .child(
                div()
                    .id(("inspector-out", index))
                    .min_h(rems_from_px(90_f32))
                    .max_h(rems_from_px(220_f32))
                    .overflow_y_scroll()
                    .px_2p5()
                    .py_2()
                    .bg(c(p::BG))
                    .border_1()
                    .border_color(c(p::BORDER))
                    .when(lines.is_empty(), |this| this.child(p::text("No output yet.", 11.5, p::DIM)))
                    .children(lines.iter().enumerate().map(|(i, l)| {
                        self.texts.text(
                            format!("insp-{index}-{i}"),
                            str_of(l, "t"),
                            Look::new(11.5, p::named(&str_of(l, "c"))).mono(true),
                            cx,
                        )
                    })),
            )
            .when(!note.is_empty(), |this| {
                this.child(
                    h_flex()
                        .gap_2()
                        .child(
                            div()
                                .text_size(rems_from_px(12_f32))
                                .font_weight(gpui::FontWeight::SEMIBOLD)
                                .text_color(c(p::TEXT))
                                .child("Steer this worker"),
                        )
                        .child(p::text(note, 12., p::MUTED)),
                )
            });
        let right = card()
            .w(rems_from_px(260_f32))
            .flex_none()
            .px_3p5()
            .py_3()
            .gap_2()
            .children(arr(s, "items").iter().enumerate().map(|(i, item)| {
                v_flex()
                    .child(p::text(str_of(item, "k"), 11., p::SUB))
                    .child(self.texts.text(
                        format!("insp-{index}-v{i}"),
                        str_of(item, "v"),
                        Look::new(12.5, p::named(&str_of(item, "c"))).mono(true),
                        cx,
                    ))
            }));
        h_flex().items_start().gap_3().child(left).child(right).into_any_element()
    }
}

impl MiniOrkPage {
    /// Flip the expansion state for a section's item (e.g. `"story-planner"`).
    pub(crate) fn toggle_step_expanded(&mut self, section: &str, step_id: &str, cx: &mut Context<Self>) {
        let key = format!("{section}-{step_id}");
        if !self.expanded.remove(&key) {
            self.expanded.insert(key);
        }
        cx.notify();
    }

    /// Apply each story step's `open` default the first time a load carries
    /// that step. A load without a story section (another tab) seeds nothing,
    /// so opening Story later still gets its defaults. `set_args` clears
    /// `seeded` when the run changes.
    pub(crate) fn seed_step_expansion(&mut self, value: &Value) {
        for section in arr(value, "sections") {
            if str_of(&section, "type") != "story" {
                continue;
            }
            for step in arr(&section, "steps") {
                let id = str_of(&step, "id");
                if id.is_empty() || !self.seeded.insert(id.to_string()) {
                    continue;
                }
                if bool_of(&step, "open") {
                    self.expanded.insert(format!("story-{id}"));
                }
            }
        }
    }

    /// Create one editor per `composer` section of `page`: reuse the editor of
    /// a section that is unchanged and drop those whose section is gone. Run
    /// from `render` before the sections are drawn.
    fn ensure_composers(&mut self, page: &Value, window: &mut Window, cx: &mut Context<Self>) {
        let wanted: Vec<(usize, Value)> = arr(page, "sections")
            .into_iter()
            .enumerate()
            .filter(|(_, section)| str_of(section, "type") == "composer")
            .collect();
        let indices: Vec<usize> = wanted.iter().map(|(index, _)| *index).collect();
        self.composers.retain(|index, _| indices.contains(index));
        for (index, section) in wanted {
            if self.composers.contains_key(&index) {
                continue;
            }
            let factory = ERASED_EDITOR_FACTORY.get().expect("editor factory is set at start-up");
            let editor = factory(window, cx);
            let placeholder = str_of(&section, "placeholder");
            if !placeholder.is_empty() {
                editor.set_placeholder_text(&placeholder, window, cx);
            }
            let _sub = editor.subscribe(
                Box::new({
                    let this = cx.entity().downgrade();
                    move |event, _window, cx| {
                        if event == ErasedEditorEvent::BufferEdited {
                            this.update(cx, |_, cx| cx.notify()).ok();
                        }
                    }
                }),
                window,
                cx,
            );
            self.composers.insert(index, Composer { editor, _sub });
        }
    }

    /// Open the run graph full screen, optionally on one node's stream.
    /// A DAG node opens in its own tab (the full-screen graph is the
    /// "⤢ Full screen" button).
    pub(crate) fn open_node_tab(&mut self, node: String, window: &mut Window, cx: &mut Context<Self>) {
        let Some(run) = self.args.get("run").cloned() else {
            return;
        };
        let Some(Ok(page)) = self.data.clone() else {
            return;
        };
        let Some(workspace) = self.workspace.upgrade() else {
            return;
        };
        let model = crate::graph::Model::from_page(&page, &run);
        let (title, handle) = (self.title_hint.clone(), cx.entity().downgrade());
        window.defer(cx, move |window, cx| {
            crate::node_tab::open_node_tab(&workspace, run, node, title, model, handle, window, cx);
        });
    }

    pub(crate) fn open_graph(&mut self, node: Option<String>, window: &mut Window, cx: &mut Context<Self>) {
        let Some(run) = self.args.get("run").cloned() else {
            return;
        };
        let Some(Ok(page)) = self.data.clone() else {
            return;
        };
        let Some(model) = crate::graph::Model::from_page(&page, &run) else {
            return;
        };
        if let Some(graph) = self.graph.clone() {
            if let Some(node) = node {
                graph.update(cx, |graph, cx| graph.open_node(node, cx));
            }
            return;
        }
        let (workspace, page_handle) = (self.workspace.clone(), cx.entity().downgrade());
        let graph = cx.new(|cx| crate::graph::MiniOrkGraph::new(model, node, workspace, page_handle, window, cx));
        self._graph_sub = Some(cx.subscribe(&graph, |this, _, _: &DismissEvent, cx| {
            this.graph = None;
            this._graph_sub = None;
            cx.notify();
        }));
        graph.update(cx, |graph, cx| graph.focus(window, cx));
        self.graph = Some(graph);
        cx.notify();
    }

    /// Build the DAG tab's run graph, or move the existing one onto the run's
    /// current model. The graph needs a window to build its filter and steer
    /// editors, so this runs from a deferred action on the frame after the DAG
    /// is on screen — the same reason `open_graph` is called that way.
    fn ensure_dag_graph(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let Some(run) = self.args.get("run").cloned() else {
            return;
        };
        let Some(Ok(page)) = self.data.clone() else {
            return;
        };
        let Some(model) = crate::graph::Model::from_page(&page, &run) else {
            return;
        };
        if let Some(graph) = self.dag_graph.clone() {
            graph.update(cx, |graph, cx| graph.set_model(model, cx));
            return;
        }
        // Open on the node the run reached last, with its detail loaded, the
        // same view a click on it gives. Only at construction: `set_model`
        // keeps whatever is selected, so a live run does not yank the reader
        // off the node they are looking at every time the board moves.
        let focus = model.last_ran().map(|node| node.id.clone());
        let (workspace, page_handle) = (self.workspace.clone(), cx.entity().downgrade());
        let graph = cx.new(|cx| {
            let mut graph = crate::graph::MiniOrkGraph::new(model, focus, workspace, page_handle, window, cx);
            graph.tab_mode = true;
            graph
        });
        self.dag_graph = Some(graph);
        cx.notify();
    }

    /// Re-read when the board moved (mini-ork wrote to the project) and this
    /// page is the one in front — so what you look at is never stale.
    fn follow_board(&mut self, cx: &mut Context<Self>) {
        if self._store_sub.is_some() {
            return;
        }
        let Some(workspace) = self.workspace.upgrade() else {
            return;
        };
        let Some(store) = BoardStore::for_workspace(&workspace, cx) else {
            return;
        };
        self.seen_board = store.read(cx).generation;
        self._store_sub = Some(cx.observe(&store, |this, store, cx| {
            let generation = store.read(cx).generation;
            if generation == this.seen_board {
                return;
            }
            this.seen_board = generation;
            let in_front = this
                .workspace
                .upgrade()
                .and_then(|w| w.read(cx).active_item(cx))
                .is_some_and(|item| item.item_id() == cx.entity_id());
            if (in_front || this.graph.is_some()) && this.may_reload() {
                this.reload(cx);
            }
        }));
    }
}

fn card() -> Div {
    v_flex()
        .bg(c(p::CHROME))
        .border_1()
        .border_color(c(p::BORDER))
        .min_w_0()
}

/// A monospace block; every line selectable, and a Copy button for all of it.
pub(crate) fn code_box<T: 'static>(texts: &Texts, key: &str, lines: Vec<Value>, cx: &mut Context<T>) -> gpui::Stateful<Div> {
    let all = lines.iter().map(|l| str_of(l, "t")).collect::<Vec<_>>().join("\n");
    let block = v_flex()
        .id(SharedString::from(format!("{key}-box")))
        .max_h(rems_from_px(480_f32))
        .overflow_y_scroll()
        .relative()
        .px_2p5()
        .py_2()
        .bg(c(p::BG))
        .border_1()
        .border_color(c(p::BORDER))
        // Clip sideways only: a plain overflow_hidden would also undo the
        // vertical scroll above.
        .overflow_x_hidden()
        .children(lines.iter().enumerate().map(|(i, l)| {
            texts.text(
                format!("{key}-{i}"),
                str_of(l, "t"),
                Look::new(11.5, p::named(&str_of(l, "c"))).mono(true).nowrap(),
                cx,
            )
        }))
        .when(!lines.is_empty(), |this| {
            this.child(
                div()
                    .absolute()
                    .top(rems_from_px(4_f32))
                    .right(rems_from_px(6_f32))
                    .child(ui::CopyButton::new(
                        SharedString::from(format!("{key}-copy")),
                        all.clone(),
                    )),
            )
        });
    texts.nested_scroll(format!("{key}-box"), block)
}

pub(crate) fn str_of(v: &Value, key: &str) -> String {
    match v.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

pub(crate) fn bool_of(v: &Value, key: &str) -> bool {
    v.get(key).and_then(Value::as_bool).unwrap_or(false)
}

pub(crate) fn arr(v: &Value, key: &str) -> Vec<Value> {
    v.get(key).and_then(Value::as_array).cloned().unwrap_or_default()
}

impl EventEmitter<()> for MiniOrkPage {}

impl Focusable for MiniOrkPage {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl Item for MiniOrkPage {
    type Event = ();

    fn tab_content_text(&self, _detail: usize, _cx: &App) -> SharedString {
        self.label().into()
    }

    fn tab_content(&self, params: TabContentParams, _window: &Window, cx: &App) -> AnyElement {
        let mono = p::mono_font(cx);
        h_flex()
            .gap(rems_from_px(7_f32))
            .child(
                div()
                    .text_size(rems_from_px(11.5_f32))
                    .font(mono)
                    .text_color(c(if params.selected { p::BLUE } else { p::SUB }))
                    .child(glyph(&self.key)),
            )
            .child(Label::new(self.label()).single_line().color(params.text_color()))
            .into_any_element()
    }

    fn tab_tooltip_text(&self, _: &App) -> Option<SharedString> {
        match self.args.get("run") {
            Some(run) if !self.title_hint.is_empty() => Some(format!("{} ({run})", self.title_hint).into()),
            Some(run) => Some(run.clone().into()),
            None => None,
        }
    }
}

impl Render for MiniOrkPage {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        self.texts.begin();
        self.follow_board(cx);
        if matches!(self.data, Some(Ok(_))) && !self.switching {
            if let Some(node) = self.pending_graph.take() {
                let page = cx.entity().downgrade();
                window.defer(cx, move |window, cx| {
                    page.update(cx, |page, cx| page.open_graph(node, window, cx)).ok();
                });
            }
            // The DAG tab leads with the run graph, so the page carries one
            // rather than drawing a canvas of its own that cannot inspect a
            // node. Deferred for the window it needs to build.
            if self.dag_graph.is_none()
                && !self.show_flow_map
                && self
                    .data
                    .as_ref()
                    .and_then(|data| data.as_ref().ok())
                    .is_some_and(|page| matches!(str_of(page, "tab").as_str(), "dag" | "graph"))
            {
                let page = cx.entity().downgrade();
                window.defer(cx, move |window, cx| {
                    page.update(cx, |page, cx| page.ensure_dag_graph(window, cx)).ok();
                });
            }
        }
        let source = self.source();
        let bar = self.loading.then(|| loading::sweep_bar(("page-sweep", self.generation as usize)));
        let content = match self.data.clone() {
            None => v_flex()
                .gap_3p5()
                .when(!self.dock, |this| {
                    this.child(
                        div()
                            .text_size(rems_from_px(17_f32))
                            .font_weight(gpui::FontWeight::SEMIBOLD)
                            .text_color(c(p::TEXT))
                            .child(self.label()),
                    )
                })
                .children(bar)
                .child(loading::loading_line(source, cx))
                .child(loading::skeleton(&self.skeleton_shapes()))
                .into_any_element(),
            Some(Ok(page)) if self.switching => v_flex()
                .gap_3p5()
                .when(!self.dock, |this| this.child(self.render_header(&page, cx)))
                .child(v_flex().children(self.render_tabs(&page, cx)).children(bar.map(|b| div().mt(rems_from_px(-2_f32)).child(b))))
                .child(loading::loading_line(source, cx))
                .child(loading::skeleton(&self.skeleton_shapes()))
                .into_any_element(),
            Some(Err(error)) => v_flex()
                .gap_1p5()
                .child(p::text("This page could not be read", 15., p::TEXT))
                .child(self.texts.text("page-error", &error, Look::new(12.5, c(p::RED)).mono(true), cx))
                .into_any_element(),
            Some(Ok(page)) => {
                // The composer sections' editors are keyed by section index;
                // sync them before the sections are drawn.
                self.ensure_composers(&page, window, cx);
                // The Graph tab leads with the step graph (the column DAG);
                // the flow map sits behind the Steps · Flow map toggle. With
                // Flow map chosen the map leads and the DAG section below it
                // is retired, since the map carries its own full-screen button.
                let on_graph = str_of(&page, "tab") == "graph";
                let show_flow = on_graph && self.show_flow_map;
                let flow_map = show_flow
                    .then(|| page.get("flow"))
                    .flatten()
                    .filter(|flow| flow.is_object())
                    .map(|flow| crate::flow::render_flow_part_a(self, flow, &self.flow_scroll, window, cx));
                let hide_dag = show_flow;
                let toggle = on_graph.then(|| self.render_graph_toggle(cx));
                v_flex()
                    .gap_3p5()
                    .when(!self.dock, |this| this.child(self.render_header(&page, cx)))
                    .child(v_flex().children(self.render_tabs(&page, cx)).children(bar.map(|b| div().mt(rems_from_px(-2_f32)).child(b))))
                    .children(toggle)
                    .children(flow_map)
                    .child(
                        div().child(self.render_sections(&page, hide_dag, window, cx)).with_animation(
                            ("page-fade", self.fade as usize),
                            Animation::new(Duration::from_millis(280)),
                            |el, delta| el.opacity(delta),
                        ),
                    )
                    .into_any_element()
            }
        };
        self.texts.end();
        div()
            .id("mini-ork-page")
            .track_focus(&self.focus_handle)
            .size_full()
            .bg(c(p::BG))
            .text_color(c(p::TEXT))
            .overflow_y_scroll()
            .child(if self.dock {
                // The Run panel draws its own chrome: a small inset instead of
                // the page's wide padding.
                div().px_2p5().py_2().child(content)
            } else {
                // One column for every tab: full width, left aligned. The old
                // centred 1080px column made the content jump sideways the
                // moment you left the DAG tab — a canvas that had already
                // opted out of it — so the same page read at two widths.
                //
                // Spacing helpers rather than raw pixels, as Zed's own panels
                // do: these are `rems`, so the gutter tracks the reader's UI
                // font size instead of pinning a 20px that only reads right at
                // the default size.
                div().px_5().pt_4().pb_12().child(content)
            })
            .when_some(self.graph.clone(), |this, graph| {
                this.child(
                    deferred(
                        anchored()
                            .position_mode(AnchoredPositionMode::Window)
                            .position(point(px(0.), px(0.)))
                            .child(graph),
                    )
                    .with_priority(100),
                )
            })
    }
}

/// Open a page (or bring its tab forward and switch it to `tab`).
pub fn open_page(
    workspace: &Entity<Workspace>,
    key: String,
    tab: Option<String>,
    args: BTreeMap<String, String>,
    window: &mut Window,
    cx: &mut App,
) {
    if key == "run" {
        if let Some(run) = args.get("run").cloned() {
            open_run(workspace, run, String::new(), tab, window, cx);
        }
        return;
    }
    workspace.update(cx, |workspace, cx| {
        let existing = workspace
            .items_of_type::<MiniOrkPage>(cx)
            .find(|page| page.read(cx).key == key);
        if let Some(existing) = existing {
            workspace.activate_item(&existing, true, true, window, cx);
            if tab.is_some() || !args.is_empty() {
                existing.update(cx, |page, cx| {
                    if tab.is_some() {
                        page.tab = tab;
                    }
                    page.args.extend(args);
                    page.begin_switch();
                    page.reload(cx);
                });
            }
            return;
        }
        let weak = cx.entity().downgrade();
        let page = cx.new(|cx| MiniOrkPage::new(key, tab, args, String::new(), weak, cx));
        workspace.add_item_to_active_pane(Box::new(page), None, true, window, cx);
    });
}

/// Open a run's tab (or bring it forward).
pub fn open_run(
    workspace: &Entity<Workspace>,
    run_id: String,
    title: String,
    tab: Option<String>,
    window: &mut Window,
    cx: &mut App,
) {
    workspace.update(cx, |workspace, cx| {
        let existing = workspace
            .items_of_type::<MiniOrkPage>(cx)
            .find(|page| page.read(cx).key == "run" && page.read(cx).args.get("run") == Some(&run_id));
        if let Some(existing) = existing {
            workspace.activate_item(&existing, true, true, window, cx);
            if tab.is_some() {
                existing.update(cx, |page, cx| {
                    page.tab = tab;
                    page.begin_switch();
                    page.reload(cx);
                });
            }
            return;
        }
        let weak = cx.entity().downgrade();
        let mut args = BTreeMap::new();
        args.insert("run".to_string(), run_id);
        let page = cx.new(|cx| MiniOrkPage::new("run".into(), tab, args, title, weak, cx));
        workspace.add_item_to_active_pane(Box::new(page), None, true, window, cx);
    });
}
