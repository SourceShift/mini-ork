//! The left edge of the mini-ork IDE: the activity bar and the Threads board.
//!
//! It fills Zed's pluggable workspace sidebar slot (`workspace::Sidebar`), so
//! it sits left of the docks, full height. Its top strip continues the title
//! bar (project, branch) under the window's traffic lights.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::{Duration, Instant},
};

use gpui::{
    AnyElement, App, Context, Entity, EntityId, EventEmitter, FocusHandle, Focusable, Pixels,
    Render, ScrollHandle, SharedString, Subscription, Task, WeakEntity, Window, WindowControlArea, px,
};
use ui_input::{ERASED_EDITOR_FACTORY, ErasedEditor, ErasedEditorEvent};
use ui::{
    ContextMenu, DiffStat, Disclosure, Divider, Indicator, ListHeader, ListItem, ListItemSpacing,
    PopoverMenu, Tooltip, prelude::*,
    utils::{TRAFFIC_LIGHT_PADDING, platform_title_bar_height},
};
use workspace::{MultiWorkspace, MultiWorkspaceEvent, Sidebar, SidebarEvent, SidebarSide, Workspace};

use crate::{
    board::{self, Board, Run},
    components::ThrottledSpinExt as _,
    page::{self, MiniOrkPage},
    palette::{self as p, c},
    projects,
    store::{self, BoardStore},
    thumb,
};

pub const ACTIVITY_WIDTH: f32 = 42.;
const BOARD_WIDTH: f32 = 272.;
const BOARD_MIN: f32 = 200.;
/// How long a failed `board page dags` read waits before it is tried again.
/// Long enough that a page the engine does not know cannot spawn a subprocess
/// on every render, short enough that a transient failure repairs itself
/// within a poll or two.
const THUMBS_RETRY: Duration = Duration::from_secs(10);

/// The activity bar, top to bottom: key, glyph, tooltip.
const ACTIVITIES: [(&str, &str, &str); 12] = [
    ("threads", "▤", "Threads (task board)"),
    ("inbox", "✉", "Inbox — what needs you"),
    ("kanban", "▦", "Board"),
    ("orch", "✦", "Orchestrator & intake"),
    ("verify", "✓", "Verification & safety"),
    ("recipes", "▥", "Recipes, specs & epics"),
    ("autos", "◷", "Automations & scheduling"),
    ("lanes", "$", "Models, lanes & cost"),
    ("learn", "❖", "Learning & memory"),
    ("context", "◫", "Context (ContextNest)"),
    ("nodes", "⬡", "Nodes & sandboxes"),
    ("setup", "⚙", "Setup & health"),
];

/// Activate `workspace` in this window, then start a mini-ork thread in it.
///
/// The switch has to come first: `store::new_thread` dispatches
/// `NewExternalAgentThread`, which the window routes to the *active* workspace's
/// agent panel, so a thread aimed at another project would otherwise land in the
/// one already in front.
fn start_thread_in(
    multi_workspace: &WeakEntity<MultiWorkspace>,
    workspace: &Entity<Workspace>,
    window: &mut Window,
    cx: &mut App,
) {
    if let Some(multi) = multi_workspace.upgrade() {
        multi.update(cx, |multi, cx| multi.activate(workspace.clone(), None, window, cx));
    }
    store::new_thread(workspace, "", window, cx);
}

pub struct MiniOrkSidebar {
    multi_workspace: WeakEntity<MultiWorkspace>,
    focus_handle: FocusHandle,
    board_open: bool,
    board_width: f32,
    /// The run state the board is filtered to (`needs_you`, `working`, …), or
    /// `None` for every thread, newest first — the default, where a run's group
    /// is told from its own row instead of from a heading.
    board_filter: Option<String>,
    selected_run: Option<String>,
    bound: Option<(EntityId, EntityId)>,
    /// Full-text search over runs, and runs paged in beyond the board's first 50.
    search: Arc<dyn ErasedEditor>,
    query: String,
    results: Option<Vec<Run>>,
    extra: Vec<Run>,
    total: Option<u64>,
    has_more: bool,
    loading_more: bool,
    list_scroll: ScrollHandle,
    /// One mini-DAG per visible run, straight from `board page dags`.
    thumbs: HashMap<String, thumb::Thumb>,
    /// What the last thumbnail read was *for* — the visible run ids plus (only
    /// while something is working) the store generation. A working run keeps
    /// its id while it grows nodes, so id equality alone would freeze the DAG
    /// at its first frame.
    thumbs_sig: Option<(Vec<String>, u64)>,
    loading_thumbs: bool,
    /// Not before this instant, after a read that failed. A `board page dags`
    /// that is broken for the session (an engine too old to know the key) must
    /// not spawn a subprocess on every render, but a single hiccup must not
    /// leave the board without DAGs until the run set happens to change.
    thumbs_retry_at: Option<Instant>,
    /// Run cards whose node list the user folded.
    folded: HashSet<String>,
    _thumbs: Task<()>,
    _search: Task<()>,
    _search_sub: Subscription,
    _store: Option<Subscription>,
    _workspace: Option<Subscription>,
    _multi: Subscription,
}

impl MiniOrkSidebar {
    pub fn new(
        multi_workspace: Entity<MultiWorkspace>,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Self {
        let factory = ERASED_EDITOR_FACTORY.get().expect("editor factory is set at start-up");
        let search = factory(window, cx);
        search.set_placeholder_text("Search runs — feature, kickoff, results…", window, cx);
        let search_sub = search.subscribe(
            Box::new({
                let this = cx.entity().downgrade();
                let editor = search.clone();
                move |event, _window, cx| {
                    if event == ErasedEditorEvent::BufferEdited {
                        let text = editor.text(cx);
                        this.update(cx, |this, cx| this.set_query(text, cx)).ok();
                    }
                }
            }),
            window,
            cx,
        );
        let multi = cx.subscribe(&multi_workspace, |this, _, event: &MultiWorkspaceEvent, cx| {
            if matches!(event, MultiWorkspaceEvent::ActiveWorkspaceChanged { .. }) {
                this.bound = None;
                // Drop the other workspace's DAGs, or they render over this
                // one's rows until the next read lands. Clearing the signature
                // is also what makes an in-flight read's answer get discarded
                // rather than written back — see `ensure_thumbs`.
                this.thumbs.clear();
                this.thumbs_sig = None;
                this.thumbs_retry_at = None;
                cx.notify();
            }
        });
        // The sidebar is the IDE's activity bar: keep it open.
        let handle = multi_workspace.downgrade();
        cx.defer(move |cx| {
            handle
                .update(cx, |multi_workspace, cx| {
                    if !multi_workspace.sidebar_open() {
                        multi_workspace.open_sidebar(cx);
                    }
                })
                .ok();
        });
        Self {
            multi_workspace: multi_workspace.downgrade(),
            focus_handle: cx.focus_handle(),
            board_open: true,
            board_width: BOARD_WIDTH,
            board_filter: None,
            selected_run: None,
            bound: None,
            search,
            query: String::new(),
            results: None,
            extra: Vec::new(),
            total: None,
            has_more: true,
            loading_more: false,
            list_scroll: ScrollHandle::new(),
            thumbs: HashMap::new(),
            thumbs_sig: None,
            loading_thumbs: false,
            thumbs_retry_at: None,
            folded: HashSet::new(),
            _thumbs: Task::ready(()),
            _search: Task::ready(()),
            _search_sub: search_sub,
            _store: None,
            _workspace: None,
            _multi: multi,
        }
    }

    fn target(&self, cx: &App) -> Option<(std::path::PathBuf, std::path::PathBuf)> {
        let workspace = self.workspace(cx)?;
        let store = BoardStore::for_workspace(&workspace, cx)?;
        let store = store.read(cx);
        Some((store.bin.clone(), store.home.clone()?))
    }

    /// A new search: debounce, then fetch the first page.
    fn set_query(&mut self, text: String, cx: &mut Context<Self>) {
        let query = text.trim().to_string();
        if query == self.query {
            return;
        }
        self.query = query.clone();
        self.extra.clear();
        self.has_more = true;
        self.total = None;
        if query.is_empty() {
            self.results = None;
            self._search = Task::ready(());
            cx.notify();
            return;
        }
        let Some((bin, home)) = self.target(cx) else {
            return;
        };
        self.loading_more = true;
        cx.notify();
        self._search = cx.spawn(async move |this, cx| {
            cx.background_executor().timer(Duration::from_millis(300)).await;
            let page = cx.background_spawn(board::fetch_runs(bin, home, query.clone(), 0, 50)).await;
            this.update(cx, |this, cx| {
                if this.query != query {
                    return;
                }
                this.loading_more = false;
                match page {
                    Ok(page) => {
                        this.has_more = page.has_more;
                        this.total = page.total;
                        this.results = Some(page.runs);
                    }
                    Err(error) => {
                        this.has_more = false;
                        this.results = Some(Vec::new());
                        log::warn!("mini-ork run search: {error}");
                    }
                }
                cx.notify();
            })
            .ok();
        });
    }

    /// The next 50 — of the search results, or of all runs past the board's first page.
    fn load_more(&mut self, cx: &mut Context<Self>) {
        if self.loading_more || !self.has_more {
            return;
        }
        let Some((bin, home)) = self.target(cx) else {
            return;
        };
        let base = BoardStore::for_workspace(&match self.workspace(cx) { Some(w) => w, None => return }, cx)
            .and_then(|s| s.read(cx).board.as_ref().map(|b| b.runs.len()))
            .unwrap_or(0);
        let offset = match &self.results {
            Some(results) => results.len(),
            None => base + self.extra.len(),
        };
        let query = self.query.clone();
        self.loading_more = true;
        cx.notify();
        self._search = cx.spawn(async move |this, cx| {
            let page = cx.background_spawn(board::fetch_runs(bin, home, query.clone(), offset, 50)).await;
            this.update(cx, |this, cx| {
                this.loading_more = false;
                if this.query != query {
                    return;
                }
                match page {
                    Ok(page) => {
                        this.has_more = page.has_more && !page.runs.is_empty();
                        this.total = page.total.or(this.total);
                        match &mut this.results {
                            Some(results) => results.extend(page.runs),
                            None => this.extra.extend(page.runs),
                        }
                    }
                    Err(error) => {
                        this.has_more = false;
                        log::warn!("mini-ork runs page: {error}");
                    }
                }
                cx.notify();
            })
            .ok();
        });
    }

    /// The runs the board is showing, in order: search results when a query is
    /// live, otherwise the board's first page plus whatever "load more" added.
    fn visible_runs(&self, board: Option<&Board>) -> Vec<Run> {
        match &self.results {
            Some(results) => results.clone(),
            None => {
                let mut runs = board.map(|b| b.runs.clone()).unwrap_or_default();
                let seen: HashSet<String> = runs.iter().map(|r| r.id.clone()).collect();
                runs.extend(self.extra.iter().filter(|r| !seen.contains(&r.id)).cloned());
                runs
            }
        }
    }

    fn store_generation(&self, cx: &App) -> u64 {
        self.workspace(cx)
            .and_then(|workspace| BoardStore::for_workspace(&workspace, cx))
            .map(|store| store.read(cx).generation)
            .unwrap_or(0)
    }

    /// Fetch the thumbnails for what the board is showing, if that has changed
    /// since the last read. Called from `render`, so the store's own notify is
    /// the trigger and the cadence is the store's (`MIN_GAP`, 1.5s while a run
    /// is live) — one extra subprocess per read, never one per row.
    fn ensure_thumbs(&mut self, board: Option<&Board>, cx: &mut Context<Self>) {
        // A read that failed recently is not retried on every render — see
        // `thumbs_retry_at`.
        if self.thumbs_retry_at.is_some_and(|at| Instant::now() < at) {
            return;
        }
        let runs = self.visible_runs(board);
        if runs.is_empty() {
            if !self.thumbs.is_empty() {
                self.thumbs.clear();
            }
            self.thumbs_sig = None;
            return;
        }
        let mut ids: Vec<String> = runs.iter().map(|run| run.id.clone()).collect();
        ids.sort_unstable();
        // A working run keeps its id while it adds nodes, so fold in the store
        // generation — but only then, or every cost tick would re-read the
        // whole board.
        let generation = if runs.iter().any(|run| run.state == "working") {
            self.store_generation(cx)
        } else {
            0
        };
        let signature = (ids.clone(), generation);
        if self.loading_thumbs || self.thumbs_sig.as_ref() == Some(&signature) {
            return;
        }
        let Some((bin, home)) = self.target(cx) else {
            // The store has no `home` until its first poll resolves. Leave the
            // signature unset so this render's work is not lost.
            return;
        };
        // Set before the spawn: a render while the fetch is in flight must not
        // start a second one.
        self.thumbs_sig = Some(signature.clone());
        self.loading_thumbs = true;
        self._thumbs = cx.spawn(async move |this, cx| {
            let fetched = cx.background_spawn(board::fetch_thumbs(bin, home, ids)).await;
            this.update(cx, |this, cx| {
                this.loading_thumbs = false;
                // The signature is the guard against an answer that is no
                // longer wanted: a workspace switch clears it, and a newer read
                // replaces it. Either way this result is for a board that is
                // not on screen, and writing it would put the wrong runs' DAGs
                // on the rows.
                if this.thumbs_sig.as_ref() != Some(&signature) {
                    return;
                }
                match fetched {
                    Ok(thumbs) => {
                        this.thumbs = thumbs;
                        this.thumbs_retry_at = None;
                    }
                    Err(error) => {
                        // Keep the previous map — a failed poll must never
                        // blank a board that already had its DAGs — but let a
                        // later read try again.
                        log::warn!("mini-ork dags page: {error}");
                        this.thumbs_sig = None;
                        this.thumbs_retry_at = Some(Instant::now() + THUMBS_RETRY);
                    }
                }
                cx.notify();
            })
            .ok();
        });
    }

    pub fn board_open(&self) -> bool {
        self.board_open
    }

    pub fn toggle_board(&mut self, cx: &mut Context<Self>) {
        self.board_open = !self.board_open;
        cx.emit(SidebarEvent::SerializeNeeded);
        cx.notify();
    }

    /// Open the Threads board (no-op when it is already open).
    pub fn show_board(&mut self, cx: &mut Context<Self>) {
        if !self.board_open {
            self.toggle_board(cx);
        }
    }

    fn workspace(&self, cx: &App) -> Option<Entity<Workspace>> {
        Some(self.multi_workspace.upgrade()?.read(cx).workspace().clone())
    }

    /// Re-render when the active workspace's board or active tab changes.
    fn bind(&mut self, cx: &mut Context<Self>) -> Option<(Entity<Workspace>, Entity<BoardStore>)> {
        let workspace = self.workspace(cx)?;
        let store = BoardStore::for_workspace(&workspace, cx)?;
        let ids = (workspace.entity_id(), store.entity_id());
        if self.bound != Some(ids) {
            self.bound = Some(ids);
            self._store = Some(cx.observe(&store, |_, _, cx| cx.notify()));
            self._workspace = Some(cx.subscribe(&workspace, |_, _, event: &workspace::Event, cx| {
                if matches!(event, workspace::Event::ActiveItemChanged) {
                    cx.notify();
                }
            }));
        }
        Some((workspace, store))
    }

    fn active_page_key(workspace: &Entity<Workspace>, cx: &App) -> Option<String> {
        let item = workspace.read(cx).active_item(cx)?;
        let page = item.to_any_view().downcast::<MiniOrkPage>().ok()?;
        Some(page.read(cx).key.clone())
    }

    fn render_strip(
        &self,
        board: Option<&Board>,
        workspace: Option<Entity<Workspace>>,
        window: &Window,
        cx: &App,
    ) -> AnyElement {
        let height = platform_title_bar_height(window);
        let fullscreen = window.is_fullscreen() || window.is_simple_fullscreen();
        let header = board.map(|b| b.header.clone()).unwrap_or_default();
        let project = if header.project.is_empty() {
            board.map(|b| b.project.clone()).unwrap_or_default()
        } else {
            header.project.clone()
        };
        h_flex()
            .id("mini-ork-sidebar-strip")
            .window_control_area(WindowControlArea::Drag)
            .on_click(|event, window, _| {
                if event.click_count() == 2 {
                    window.titlebar_double_click();
                }
            })
            .flex_none()
            .h(height)
            .w_full()
            .bg(c(p::CHROME))
            .border_b_1()
            .border_color(c(p::BORDER))
            .when(!fullscreen && cfg!(target_os = "macos"), |this| this.pl(px(TRAFFIC_LIGHT_PADDING + 2.)))
            .when(fullscreen || !cfg!(target_os = "macos"), |this| this.pl_3())
            .gap_2p5()
            .text_size(rems_from_px(12.5_f32))
            .whitespace_nowrap()
            .overflow_hidden()
            .when(self.board_open && !project.is_empty(), |this| {
                this.children(crate::title_bar::left_items(&header, &project, workspace, true, cx))
            })
            .into_any_element()
    }

    fn render_activity_bar(
        &self,
        workspace: &Option<Entity<Workspace>>,
        needs_you: u64,
        active_key: Option<String>,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let mono = p::mono_font(cx);
        let mut bar = v_flex()
            .flex_none()
            .w(rems_from_px(ACTIVITY_WIDTH))
            .h_full()
            .bg(c(p::CHROME))
            .border_r_1()
            .border_color(c(p::BORDER))
            .items_center()
            .py_1p5()
            .gap_0p5();
        for (i, (key, glyph, tooltip)) in ACTIVITIES.iter().enumerate() {
            if *key == "setup" {
                bar = bar.child(div().flex_1());
            }
            let on = if *key == "threads" {
                self.board_open
            } else {
                active_key.as_deref() == Some(*key)
            };
            let key = key.to_string();
            let workspace = workspace.clone();
            let button = div()
                .id(SharedString::from(format!("activity-{key}")))
                .relative()
                .size_8()
                .flex()
                .items_center()
                .justify_center()
                .when(on, |this| this.bg(c(p::SELECTED)))
                .text_size(rems_from_px(15_f32))
                .font(mono.clone())
                .text_color(c(if on { p::TEXT } else { p::SUB }))
                .cursor_pointer()
                .hover(|style| style.bg(c(p::HOVER)).text_color(c(p::TEXT)))
                .tooltip(Tooltip::text(*tooltip))
                .child(*glyph)
                .when((key == "threads" || key == "inbox") && needs_you > 0, |this| {
                    this.child(
                        div()
                            .absolute()
                            .top(rems_from_px(2_f32))
                            .right(rems_from_px(1_f32))
                            .min_w_3p5()
                            .h_3p5()
                            .px(rems_from_px(3_f32))
                            .rounded(rems_from_px(7_f32))
                            .bg(c(p::YELLOW))
                            .flex()
                            .items_center()
                            .justify_center()
                            .text_size(rems_from_px(9.5_f32))
                            .font_weight(gpui::FontWeight::SEMIBOLD)
                            .text_color(c(p::BG))
                            .child(needs_you.to_string()),
                    )
                })
                .on_click(cx.listener(move |this, _, window, cx| {
                    if key == "threads" {
                        this.toggle_board(cx);
                        return;
                    }
                    let (key, workspace) = (key.clone(), workspace.clone());
                    window.defer(cx, move |window, cx| {
                        if let Some(workspace) = workspace {
                            page::open_page(&workspace, key, None, Default::default(), window, cx);
                        }
                    });
                }));
            bar = bar.child(button);
            if i == 0 {
                bar = bar.child(Divider::horizontal().w_5().my_1());
            }
        }
        bar.into_any_element()
    }

    /// One row per known project — projects whose folder has moved stay listed,
    /// so they can still be found and removed — then "Add project…". Every row
    /// offers the same actions as the title bar's project menu.
    fn render_project_picker(workspace: &Entity<Workspace>, cx: &App) -> AnyElement {
        let row = |id: SharedString, title: String, subtitle: Option<String>| {
            v_flex()
                .id(id)
                .mx_0p5()
                .px_2()
                .py(rems_from_px(5_f32))
                .cursor_pointer()
                .hover(|s| s.bg(c(p::HOVER)))
                .child(div().text_size(rems_from_px(12.5_f32)).text_color(c(p::TEXT)).child(title))
                .when_some(subtitle, |this, sub| this.child(p::text(sub, 11., p::SUB)))
        };
        let mut picker = v_flex().gap_0p5().pt_1();
        for (i, entry) in projects::entries(workspace, cx).into_iter().enumerate() {
            let missing = entry.missing;
            let subtitle = if missing {
                format!("{} · folder gone", entry.path)
            } else {
                entry.path.clone()
            };
            let open_workspace = workspace.clone();
            let root = entry.root.clone();
            let (actions_workspace, actions_entry) = (workspace.clone(), entry.clone());
            picker = picker.child(
                h_flex()
                    .id(SharedString::from(format!("board-open-project-{i}")))
                    .w_full()
                    .gap_1()
                    .items_center()
                    .mx_0p5()
                    .px_2()
                    .py(rems_from_px(5_f32))
                    .hover(|s| s.bg(c(p::HOVER)))
                    .child(
                        v_flex()
                            .id(SharedString::from(format!("board-project-open-{i}")))
                            .flex_1()
                            .when(!missing, |this| {
                                this.cursor_pointer().on_click(move |_, window, cx| {
                                    projects::switch_to(&open_workspace, root.clone(), window, cx);
                                })
                            })
                            .child(
                                div()
                                    .text_size(rems_from_px(12.5_f32))
                                    .text_color(c(if missing { p::SUB } else { p::TEXT }))
                                    .child(entry.name.clone()),
                            )
                            .child(p::text(subtitle, 11., if missing { p::RED } else { p::SUB })),
                    )
                    .child(
                        PopoverMenu::new(SharedString::from(format!("board-project-menu-{i}")))
                            .trigger_with_tooltip(
                                IconButton::new(SharedString::from(format!("board-project-actions-{i}")), IconName::Ellipsis)
                                    .icon_size(IconSize::Small),
                                Tooltip::text("Project actions"),
                            )
                            .menu(move |window, cx| {
                                let (workspace, entry) = (actions_workspace.clone(), actions_entry.clone());
                                Some(ContextMenu::build(window, cx, move |menu, _, _| {
                                    projects::project_actions(menu, &workspace, entry.clone())
                                }))
                            }),
                    ),
            );
        }
        let workspace = workspace.clone();
        picker
            .child(
                row("board-open-project-pick".into(), "Add project…".into(), None)
                    .on_click(move |_, window, cx| projects::add(&workspace, window, cx)),
            )
            .into_any_element()
    }

    /// The board header's "+": start a mini-ork thread in this project. When the
    /// window holds more than one project, the same button asks which one first —
    /// stock Zed heads that menu "New Thread In…". This board shows one project
    /// at a time, so the menu is the only way to aim a new thread at a project
    /// that is not the active one.
    fn render_new_thread(&self, workspace: &Option<Entity<Workspace>>, cx: &App) -> AnyElement {
        let open: Vec<Entity<Workspace>> = self
            .multi_workspace
            .upgrade()
            .map(|multi| multi.read(cx).workspaces().cloned().collect())
            .unwrap_or_default();
        if open.len() < 2 {
            // A single project makes a one-row menu: a click to open it and a
            // click to pick the only answer.
            let workspace = workspace.clone();
            return IconButton::new("board-new-thread", IconName::Plus)
                .icon_size(IconSize::Small)
                .tooltip(Tooltip::text("New thread"))
                .on_click(move |_, window, cx| {
                    if let Some(workspace) = workspace.clone() {
                        store::new_thread(&workspace, "", window, cx);
                    }
                })
                .into_any_element();
        }
        let active = workspace.clone();
        let multi_workspace = self.multi_workspace.clone();
        let home = std::env::var("HOME").unwrap_or_default();
        PopoverMenu::new("board-new-thread-menu")
            .trigger_with_tooltip(
                IconButton::new("board-new-thread", IconName::Plus).icon_size(IconSize::Small),
                Tooltip::text("New thread in…"),
            )
            .menu(move |window, cx| {
                // `.menu` is called as `Fn`, so nothing captured here may be
                // moved out of it: each entry handler takes its own handle.
                let multi = multi_workspace.clone();
                let rows: Vec<(Entity<Workspace>, String, String, bool)> = open
                    .iter()
                    .map(|workspace| {
                        let root = projects::current_root(workspace, cx);
                        let name = root
                            .as_ref()
                            .and_then(|root| root.file_name())
                            .map(|name| name.to_string_lossy().to_string())
                            .unwrap_or_else(|| "mini-ork".to_string());
                        let path = root
                            .as_ref()
                            .map(|root| root.to_string_lossy().replace(&home, "~"))
                            .unwrap_or_default();
                        (workspace.clone(), name, path, active.as_ref() == Some(workspace))
                    })
                    .collect();
                Some(ContextMenu::build(window, cx, move |mut menu, _, _| {
                    menu = menu.header("New Thread In…");
                    for (workspace, name, path, is_active) in rows {
                        let multi = multi.clone();
                        menu = menu.custom_entry(
                            move |_, _| {
                                h_flex()
                                    .w_full()
                                    .gap_2()
                                    .child(
                                        div()
                                            .w_3()
                                            .text_color(c(p::GREEN))
                                            .child(if is_active { "✓" } else { "" }),
                                    )
                                    .child(
                                        v_flex()
                                            .child(
                                                div()
                                                    .text_size(rems_from_px(12.5_f32))
                                                    .text_color(c(p::TEXT))
                                                    .when(is_active, |this| {
                                                        this.font_weight(gpui::FontWeight::SEMIBOLD)
                                                    })
                                                    .child(name.clone()),
                                            )
                                            .child(
                                                div()
                                                    .text_size(rems_from_px(11_f32))
                                                    .text_color(c(p::SUB))
                                                    .child(path.clone()),
                                            ),
                                    )
                                    .into_any_element()
                            },
                            move |window, cx| {
                                start_thread_in(&multi, &workspace, window, cx)
                            },
                        );
                    }
                    menu
                }))
            })
            .into_any_element()
    }

    fn render_board(
        &self,
        board: Option<&Board>,
        error: Option<String>,
        workspace: &Option<Entity<Workspace>>,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let runs: Vec<Run> = self.visible_runs(board);
        let counts = board.map(|b| b.counts.clone()).unwrap_or_default();
        let header = {
            let workspace = workspace.clone();
            h_flex()
                .flex_none()
                .h_9()
                .pl_3()
                .pr_2()
                .gap_2()
                .border_b_1()
                .border_color(c(p::BORDER))
                .child(
                    div()
                        .text_size(rems_from_px(12.5_f32))
                        .font_weight(gpui::FontWeight::SEMIBOLD)
                        .text_color(c(p::TEXT))
                        .child("Threads"),
                )
                .child(p::text(
                    match (self.total, self.results.is_some()) {
                        (Some(total), _) => format!("{} of {total}", runs.len()),
                        _ => runs.len().to_string(),
                    },
                    12.,
                    p::SUB,
                ))
                .child(div().flex_1())
                .when_some(self.board_filter.clone(), |this, state| {
                    this.child(
                        div()
                            .id("board-filter-clear")
                            .px_1p5()
                            .py_0p5()
                            .text_size(rems_from_px(11_f32))
                            .text_color(c(p::MUTED))
                            .cursor_pointer()
                            .hover(|s| s.bg(c(p::HOVER)).text_color(c(p::TEXT)))
                            .tooltip(Tooltip::text("Show every thread"))
                            .child(format!("{} ✕", state_words(&state).0))
                            .on_click(cx.listener(|this, _, _, cx| {
                                this.board_filter = None;
                                cx.notify();
                            })),
                    )
                })
                .child(self.render_new_thread(&workspace, cx))
        };
        let search_row = h_flex()
            .flex_none()
            .h(rems_from_px(30_f32))
            .mx_2p5()
            .mt(px(8.))
            .px_2()
            .gap_1p5()
            .border_1()
            .border_color(c(p::BORDER2))
            .bg(c(p::BG))
            .child(p::text("⌕", 12., p::SUB))
            .child(div().flex_1().min_w_0().text_size(rems_from_px(12.5_f32)).child(self.search.render(window, cx)));
        let tiles = [
            ("needs_you", counts.needs_you, "need you", p::YELLOW),
            ("working", counts.working, "working", p::BLUE),
            ("failed", counts.failed, "failed", p::RED),
            ("done", counts.done, "done", p::GREEN),
        ];
        let count_grid = h_flex()
            .flex_none()
            .gap_1()
            .px_2p5()
            .py_2()
            .border_b_1()
            .border_color(c(p::BORDER))
            .children(tiles.into_iter().map(|(state, n, label, colour)| {
                let selected = self.board_filter.as_deref() == Some(state);
                v_flex()
                    .id(SharedString::from(format!("board-filter-{state}")))
                    .flex_1()
                    .items_center()
                    .py_1()
                    .bg(c(if selected { p::HOVER } else { p::BG }))
                    .border_1()
                    .border_color(c(if selected { colour } else { p::BG }))
                    .cursor_pointer()
                    .tooltip(Tooltip::text(format!(
                        "Only the threads that {label} — click again for all"
                    )))
                    .child(
                        div()
                            .text_size(rems_from_px(14_f32))
                            .font_weight(gpui::FontWeight::SEMIBOLD)
                            .text_color(c(colour))
                            .child(n.to_string()),
                    )
                    .child(p::text(label, 10.5, p::SUB))
                    .on_click(cx.listener(move |this, _, _, cx| {
                        this.board_filter =
                            (this.board_filter.as_deref() != Some(state)).then(|| state.to_string());
                        cx.notify();
                    }))
            }));

        let now = now_epoch();
        // Newest first, thread by thread: the board opens as one flat list, so
        // a run is found by when it started rather than by which group it fell
        // into. The tiles above narrow it to a single state.
        let filter = self.board_filter.as_deref();
        let mut shown: Vec<&Run> = runs
            .iter()
            .filter(|run| filter.is_none_or(|state| run.state == state))
            .collect();
        shown.sort_by_key(|run| std::cmp::Reverse(run.started_at.unwrap_or(0)));
        // The extra left padding is room for `ListItem`'s disclosure, which
        // hangs a rem outside the row's own padding.
        let mut list = v_flex().pl_4().pr_1p5().pt_1p5().pb_4();
        if let Some(error) = error.filter(|_| runs.is_empty()) {
            list = list.child(p::text(error, 12., p::SUB).p_2());
            // A window restored without a folder has nothing to read runs from:
            // offer the known projects right here instead of a dead end.
            if let Some(workspace) = workspace.as_ref().filter(|w| projects::current_root(w, cx).is_none()) {
                list = list.child(Self::render_project_picker(workspace, cx));
            }
        } else if board.is_none() {
            list = list.child(p::text("Loading…", 12., p::SUB).p_2());
        } else if runs.is_empty() && self.results.is_some() && !self.loading_more {
            list = list.child(p::text(format!("No runs match “{}”.", self.query), 12., p::SUB).p_2());
        } else if runs.is_empty() && self.results.is_none() {
            list = list.child(
                p::text("No threads yet. Press + and say what you want done.", 12., p::SUB).p_2(),
            );
        } else if let Some(state) = filter.filter(|_| shown.is_empty()) {
            list = list.child(
                p::text(format!("Nothing {}.", state_words(state).1), 12., p::SUB).p_2(),
            );
        } else if let Some(state) = filter {
            let (mark, colour) = p::state_mark(state);
            list = list.child(
                ListHeader::new(format!("{} · {}", state_words(state).0, shown.len()))
                    .start_slot(div().text_size(rems_from_px(11_f32)).text_color(c(colour)).child(mark)),
            );
        }
        for run in shown {
            list = list.child(self.render_run(run, &run.state, now, workspace, cx));
        }
        if self.loading_more {
            list = list.child(p::text("Loading…", 11.5, p::SUB).px_2().py_1p5());
        } else if self.has_more && !runs.is_empty() {
            list = list.child(
                div()
                    .id("board-load-more")
                    .mx_2()
                    .my_1p5()
                    .py(rems_from_px(5_f32))
                    .border_1()
                    .border_color(c(p::BORDER2))
                    .text_center()
                    .text_size(rems_from_px(11.5_f32))
                    .text_color(c(p::MUTED))
                    .cursor_pointer()
                    .hover(|s| s.bg(c(p::HOVER)).text_color(c(p::TEXT)))
                    .child("Load more")
                    .on_click(cx.listener(|this, _, _, cx| this.load_more(cx))),
            );
        }
        let scroll = self.list_scroll.clone();
        v_flex()
            .flex_none()
            .w(rems_from_px(self.board_width))
            .h_full()
            .bg(c(p::CHROME))
            .border_r_1()
            .border_color(c(p::BORDER))
            .child(header)
            .child(search_row)
            .child(count_grid)
            .child(
                div()
                    .id("board-list")
                    .flex_1()
                    .overflow_y_scroll()
                    .track_scroll(&self.list_scroll)
                    // Near the bottom, fetch the next page.
                    .on_scroll_wheel(cx.listener(move |this, _, _, cx| {
                        let (offset, max) = (scroll.offset().y, scroll.max_offset().y);
                        if f32::from(max) > 0. && f32::from(-offset) >= f32::from(max) - 240. {
                            this.load_more(cx);
                        }
                    }))
                    .child(list),
            )
            .into_any_element()
    }
}

impl MiniOrkSidebar {
    /// One run, drawn like Orca draws its sessions: a status mark (a spinner
    /// while working), the title and a dim subtitle. The selected run and the
    /// ones still in flight open into a card that lists their nodes — status,
    /// model mark, name and elapsed time — each a link into the run graph.
    #[allow(clippy::too_many_arguments)]
    fn render_run(
        &self,
        run: &Run,
        state: &str,
        now: i64,
        workspace: &Option<Entity<Workspace>>,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let selected = self.selected_run.as_deref() == Some(run.id.as_str());
        let sub_colour = match state {
            "failed" => Color::Error,
            "needs_you" => Color::Warning,
            _ => Color::Placeholder,
        };
        let mut sub = run.recipe.clone();
        if !run.step.is_empty() {
            sub = if sub.is_empty() { run.step.clone() } else { format!("{sub} · {}", run.step) };
        }
        // `+N −M` is a `DiffStat` of its own in `sub_line`, not text spliced into
        // the recipe line.
        let has_diff = run.added > 0 || run.removed > 0;
        let (id, title) = (run.id.clone(), run.display_title());
        let open = {
            let (id, title, workspace) = (id.clone(), title.clone(), workspace.clone());
            cx.listener(move |this, _, window, cx| {
                this.selected_run = Some(id.clone());
                cx.notify();
                let (id, title, workspace) = (id.clone(), title.clone(), workspace.clone());
                window.defer(cx, move |window, cx| {
                    if let Some(workspace) = workspace {
                        page::open_run(&workspace, id, title, None, window, cx);
                    }
                });
            })
        };
        let head = h_flex()
            .w_full()
            .gap_2()
            .child(
                div()
                    .w_3p5()
                    .flex_none()
                    .flex()
                    .justify_center()
                    .child(status_mark(state, &run.id)),
            )
            .child(
                Label::new(title.clone())
                    .flex_1()
                    .size(LabelSize::Small)
                    .color(Color::Default)
                    .when(selected, |this| this.weight(gpui::FontWeight::SEMIBOLD))
                    .truncate(),
            )
            .child(
                Label::new(age(run.started_at, now))
                    .flex_none()
                    .size(LabelSize::XSmall)
                    .color(Color::Placeholder)
                    .buffer_font(cx),
            );
        let sub_line = h_flex()
            .w_full()
            .pl(rems_from_px(22_f32))
            .gap_1p5()
            .child(
                Label::new(sub)
                    .flex_1()
                    .size(LabelSize::XSmall)
                    .color(sub_colour)
                    .truncate(),
            )
            .child(
                Label::new(format!("${:.2}", run.cost_usd))
                    .flex_none()
                    .size(LabelSize::XSmall)
                    .color(Color::Hidden)
                    .buffer_font(cx),
            )
            .when(has_diff, |this| {
                this.child(
                    DiffStat::new(
                        SharedString::from(format!("board-run-diff-{}", run.id)),
                        run.added as usize,
                        run.removed as usize,
                    )
                    .label_size(LabelSize::XSmall),
                )
            });
        let span = span_line(run.started_at, run.ended_at, state).map(|line| {
            div().pl(rems_from_px(22_f32)).child(
                Label::new(line)
                    .size(LabelSize::XSmall)
                    .color(Color::Hidden)
                    .buffer_font(cx),
            )
        });
        let tooltip = span_tooltip(run.started_at, run.ended_at);
        let lines = || {
            v_flex()
                .w_full()
                .gap_0p5()
                .child(head)
                .child(sub_line)
                .children(span)
        };
        // A run's form is its own property — never the selection: promoting the
        // selected row to a card resized it and shifted every row below.
        // Selection only colours the row, through `toggle_state` (Zed's
        // `ghost_element_selected`).
        //
        // Every run whose recipe the board resolved is a card that lists its
        // nodes, not just the ones in flight or waiting on you. A run with no
        // DAG — an unresolvable recipe, or a read that has not landed yet — has
        // nothing to list and stays a plain row.
        let Some(dag) = self.thumbs.get(&run.id).filter(|dag| !dag.nodes.is_empty()) else {
            return ListItem::new(SharedString::from(format!("board-run-{}", run.id)))
                .selectable(true)
                .toggle_state(selected)
                .when_some(tooltip, |this, tip| this.tooltip(Tooltip::text(tip)))
                .on_click(open)
                .child(lines())
                .into_any_element();
        };

        // The card: header, then "N nodes" that folds the node list away.
        let folded = self.folded.contains(&run.id);
        let seen = dag.nodes.iter().filter(|n| n.state != "never_seen").count();
        let total = dag.nodes.len();
        let fold_id = run.id.clone();
        // The fold is a `Disclosure` — Zed's own chevron — as a row of its own
        // rather than `ListItem::toggle`: the toggle hangs an absolutely
        // positioned chevron outside the row, and `outlined()` clips what leaves
        // the border.
        let fold_row = (total > 0).then(|| {
            h_flex()
                .id(SharedString::from(format!("board-fold-{}", run.id)))
                .pl(rems_from_px(22_f32))
                .gap_1p5()
                .cursor_pointer()
                .child(Disclosure::new(
                    SharedString::from(format!("board-fold-icon-{}", run.id)),
                    !folded,
                ))
                .child(
                    Label::new(format!("{seen} of {total} nodes"))
                        .size(LabelSize::XSmall)
                        .color(Color::Placeholder),
                )
                .on_click(cx.listener(move |this, _, _, cx| {
                    if !this.folded.remove(&fold_id) {
                        this.folded.insert(fold_id.clone());
                    }
                    cx.notify();
                }))
        });
        let body: Option<AnyElement> =
            (!folded).then(|| self.render_nodes(run, dag, now, workspace, cx));
        // Every row is a `ListItem`, so hover, selection and the card's border
        // come from the theme rather than from colours of our own. The header
        // keeps the click that opens the run: the fold row and the node rows
        // below it toggle and navigate, and a click on those must not also open
        // the run.
        //
        // `ListItem` lays its children out in a row (`h_flex`, clipped), so the
        // header, the fold row and the node list have to be ONE vertical child:
        // handed over separately, the fold chevron lands beside the header and
        // the node list is squeezed to nothing.
        ListItem::new(SharedString::from(format!("board-run-{}", run.id)))
            .spacing(ListItemSpacing::Sparse)
            .selectable(true)
            .outlined()
            .toggle_state(selected)
            .child(
                v_flex()
                    .w_full()
                    .gap_0p5()
                    .child(
                        v_flex()
                            .id(SharedString::from(format!("board-run-head-{}", run.id)))
                            .w_full()
                            .gap_0p5()
                            .cursor_pointer()
                            .child(lines())
                            .when_some(tooltip, |this, tip| this.tooltip(Tooltip::text(tip)))
                            .on_click(open),
                    )
                    .children(fold_row)
                    .children(body),
            )
            .into_any_element()
    }

    /// The nodes of one run card: one to a line, every line starting at the
    /// same left edge, so the card reads as a roster rather than a layout.
    ///
    /// `dag.layers` decides only the *order* — a layer is listed before the
    /// layers that wait on it — so the roster reads in the order the recipe
    /// runs its nodes, with nodes a recipe runs in parallel adjacent on
    /// consecutive lines.
    fn render_nodes(
        &self,
        run: &Run,
        dag: &thumb::Thumb,
        now: i64,
        workspace: &Option<Entity<Workspace>>,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let mut rows = v_flex().w_full();
        for &index in dag.layers.iter().flatten() {
            let Some(node) = dag.nodes.get(index) else {
                continue;
            };
            rows = rows.child(self.render_node_row(run, node, now, workspace, cx));
        }
        rows.into_any_element()
    }

    /// One node in a run card: status, model mark, name, elapsed. Clicking it
    /// opens the run graph on that node.
    fn render_node_row(
        &self,
        run: &Run,
        node: &thumb::TNode,
        now: i64,
        workspace: &Option<Entity<Workspace>>,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let running = node.state == "running";
        let pending = node.state == "never_seen";
        let elapsed = match (node.started_at, node.duration_ms) {
            (_, Some(ms)) if !running => duration_label(ms / 1000),
            (Some(start), _) if running => duration_label(now - start),
            _ => String::new(),
        };
        let (run_id, title, node_id, workspace) =
            (run.id.clone(), run.display_title(), node.id.clone(), workspace.clone());
        ListItem::new(SharedString::from(format!("board-node-{}-{}", run.id, node.id)))
            .indent_level(1)
            .indent_step_size(px(18.))
            .selectable(true)
            .tooltip(Tooltip::text(
                [node.id.as_str(), node.kind.as_str(), node.lane.as_str(), &node.state.replace('_', " ")]
                    .iter()
                    .filter(|s| !s.is_empty())
                    .copied()
                    .collect::<Vec<_>>()
                    .join(" · "),
            ))
            .start_slot(
                h_flex()
                    .gap_1p5()
                    .child(node_mark(&node.state, &format!("{}-{}", run.id, node.id)))
                    .child(p::lane_mark(&node.lane, 12.)),
            )
            .end_slot(
                Label::new(elapsed)
                    .flex_none()
                    .size(LabelSize::XSmall)
                    .color(Color::Hidden)
                    .buffer_font(cx),
            )
            .child(
                Label::new(node.id.clone())
                    .flex_1()
                    .size(LabelSize::Small)
                    .color(if running {
                        Color::Default
                    } else if pending {
                        Color::Hidden
                    } else {
                        Color::Muted
                    })
                    .truncate(),
            )
            .on_click(cx.listener(move |_, _, window, cx| {
                let (run_id, title, node_id, workspace) =
                    (run_id.clone(), title.clone(), node_id.clone(), workspace.clone());
                window.defer(cx, move |window, cx| {
                    let Some(workspace) = workspace else {
                        return;
                    };
                    // The tab is the same one the run page's DAG opens, but the
                    // page handle it carries is not: the inspector's retry, gate
                    // and merge steps act through `Origin::Page`, and an invalid
                    // one swallows the action. Hand over the page for this run
                    // when one is already open — from a card there is no other
                    // source for it — so those steps work rather than silently
                    // going nowhere.
                    let page = workspace
                        .read(cx)
                        .items_of_type::<crate::page::MiniOrkPage>(cx)
                        .find(|page| {
                            page.read(cx).args.get("run").map(String::as_str) == Some(run_id.as_str())
                        })
                        .map(|page| page.downgrade())
                        .unwrap_or_else(WeakEntity::new_invalid);
                    crate::node_tab::open_node_tab(&workspace, run_id, node_id, title, None, page, window, cx);
                });
            }))
            .into_any_element()
    }
}

/// A run's status: a spinner while it works, otherwise a coloured dot.
pub(crate) fn status_mark(state: &str, key: &str) -> AnyElement {
    if matches!(state, "working" | "running") {
        return Icon::new(IconName::LoadCircle)
            .size(IconSize::XSmall)
            .color(Color::Custom(c(p::YELLOW)))
            .with_throttled_rotate(SharedString::from(format!("run-spin-{key}")), 2)
            .into_any_element();
    }
    let colour = match state {
        "done" | "published" => p::GREEN,
        "failed" | "error" => p::RED,
        "needs_you" | "needs" => p::YELLOW,
        _ => p::DIM,
    };
    Indicator::dot().color(Color::Custom(c(colour))).into_any_element()
}

/// A node's status: spinner, ✓ in a ring, ✕ in a ring, or an empty ring.
pub(crate) fn node_mark(state: &str, key: &str) -> AnyElement {
    let ring = |colour: u32| {
        div()
            .size_3()
            .flex_none()
            .rounded_full()
            .border_1()
            .border_color(c(colour))
            .flex()
            .items_center()
            .justify_center()
    };
    match state {
        "running" => Icon::new(IconName::LoadCircle)
            .size(IconSize::XSmall)
            .color(Color::Custom(c(p::YELLOW)))
            .with_throttled_rotate(SharedString::from(format!("node-spin-{key}")), 2)
            .into_any_element(),
        "done" => ring(p::GREEN)
            .child(gpui::svg().path(IconName::Check.path()).size_2().text_color(c(p::GREEN)))
            .into_any_element(),
        "failed" => ring(p::RED)
            .child(gpui::svg().path(IconName::Close.path()).size(rems_from_px(7_f32)).text_color(c(p::RED)))
            .into_any_element(),
        _ => ring(p::DIM).into_any_element(),
    }
}

/// `45s`, `12m`, `1h 05m`.
fn duration_label(secs: i64) -> String {
    match secs.max(0) {
        s @ 0..60 => format!("{s}s"),
        s @ 60..3600 => format!("{}m", s / 60),
        s => format!("{}h {:02}m", s / 3600, (s % 3600) / 60),
    }
}

fn local(epoch: i64) -> Option<chrono::DateTime<chrono::Local>> {
    use chrono::TimeZone as _;
    chrono::Local.timestamp_opt(epoch, 0).single()
}

/// `21:03 → 21:41`, `21:03 → running`, with the date when it is not today
/// (and the end's date only when it differs from the start's).
fn span_line(started: Option<i64>, ended: Option<i64>, state: &str) -> Option<String> {
    let start = local(started?)?;
    let today = chrono::Local::now().date_naive();
    let fmt = |t: &chrono::DateTime<chrono::Local>, same_day_as: chrono::NaiveDate| {
        if t.date_naive() == same_day_as {
            t.format("%H:%M").to_string()
        } else {
            t.format("%b %-d %H:%M").to_string()
        }
    };
    let head = fmt(&start, today);
    let tail = match ended.and_then(local) {
        Some(end) if state != "working" => fmt(&end, start.date_naive()),
        _ if state == "working" => "running".to_string(),
        _ => "—".to_string(),
    };
    Some(format!("{head} → {tail}"))
}

fn span_tooltip(started: Option<i64>, ended: Option<i64>) -> Option<String> {
    let start = local(started?)?;
    let mut text = format!("Started {}", start.format("%Y-%m-%d %H:%M:%S"));
    if let Some(end) = ended.and_then(local) {
        let secs = (end - start).num_seconds().max(0);
        let took = if secs >= 3600 {
            format!("{}h {:02}m", secs / 3600, (secs % 3600) / 60)
        } else {
            format!("{}m {:02}s", secs / 60, secs % 60)
        };
        text.push_str(&format!(" · Finished {} · {took}", end.format("%Y-%m-%d %H:%M:%S")));
    }
    Some(text)
}

/// A run state's board heading, and how the same state reads mid-sentence
/// (`Nothing waiting on you.`).
fn state_words(state: &str) -> (&'static str, &'static str) {
    match state {
        "needs_you" => ("Needs you", "waiting on you"),
        "working" => ("Working", "working"),
        "failed" => ("Failed", "failed"),
        "done" => ("Done", "done"),
        _ => ("Queued", "queued"),
    }
}

fn now_epoch() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or_default()
}

fn age(started_at: Option<i64>, now: i64) -> String {
    let Some(started) = started_at else {
        return String::new();
    };
    match (now - started).max(0) {
        0..60 => "now".into(),
        secs @ 60..3600 => format!("{}m", secs / 60),
        secs @ 3600..86400 => format!("{}h", secs / 3600),
        secs => format!("{}d", secs / 86400),
    }
}

impl EventEmitter<SidebarEvent> for MiniOrkSidebar {}

impl Focusable for MiniOrkSidebar {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl Sidebar for MiniOrkSidebar {
    /// The workspace lays the sidebar out at exactly this width (`.w(sidebar_width)`
    /// in `multi_workspace`), so the rail has to answer in real pixels — and the
    /// rows inside it are sized in rems, which `rem_size` turns into pixels. If
    /// this stayed a fixed pixel count the rows would outgrow the rail as soon as
    /// the reader raised their UI font size and be clipped by it.
    ///
    /// `board_width` is kept in design pixels — the units the constants above are
    /// written in, and what gets persisted — and scaled here instead. The factor
    /// is `ui_font_size`, which is exactly what `setup_ui_font` hands to
    /// `Window::set_rem_size`, so the two agree on what a rem is.
    fn width(&self, cx: &App) -> Pixels {
        let rem = theme::theme_settings(cx).ui_font_size(cx);
        let design = if self.board_open { ACTIVITY_WIDTH + self.board_width } else { ACTIVITY_WIDTH };
        rems_from_px(design).to_pixels(rem)
    }

    fn set_width(&mut self, width: Option<Pixels>, cx: &mut Context<Self>) {
        // A drag arrives in real pixels and is stored in design pixels, for the
        // same reason `width` above scales on the way out.
        let rem = f32::from(theme::theme_settings(cx).ui_font_size(cx));
        let design = |width: Pixels| f32::from(width) * 16. / rem;
        self.board_width = match width {
            Some(width) => (design(width) - ACTIVITY_WIDTH).max(BOARD_MIN),
            None => BOARD_WIDTH,
        };
        self.board_open = true;
        cx.notify();
    }

    fn has_notifications(&self, cx: &App) -> bool {
        self.workspace(cx)
            .and_then(|workspace| BoardStore::for_workspace(&workspace, cx))
            .and_then(|store| store.read(cx).board.as_ref().map(|b| b.counts.needs_you > 0))
            .unwrap_or(false)
    }

    fn side(&self, _cx: &App) -> SidebarSide {
        SidebarSide::Left
    }

    fn serialized_state(&self, _cx: &App) -> Option<String> {
        Some(serde_json::json!({ "board_open": self.board_open, "board_width": self.board_width }).to_string())
    }

    fn restore_serialized_state(&mut self, state: &str, _window: &mut Window, cx: &mut Context<Self>) {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(state) {
            if let Some(open) = value.get("board_open").and_then(|v| v.as_bool()) {
                self.board_open = open;
            }
            if let Some(width) = value.get("board_width").and_then(|v| v.as_f64()) {
                self.board_width = (width as f32).max(BOARD_MIN);
            }
            cx.notify();
        }
    }
}

impl Render for MiniOrkSidebar {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let bound = self.bind(cx);
        let (workspace, board, error) = match &bound {
            Some((workspace, store)) => {
                let store = store.read(cx);
                (Some(workspace.clone()), store.board.clone(), store.error.clone())
            }
            None => (None, None, None),
        };
        // Render is the trigger the store's notify reaches: the board re-reads
        // on a filesystem change, so the DAGs follow it at the same cadence.
        self.ensure_thumbs(board.as_ref(), cx);
        let active_key = workspace.as_ref().and_then(|w| Self::active_page_key(w, cx));
        let needs_you = board.as_ref().map_or(0, |b| b.counts.needs_you);
        let strip = self.render_strip(board.as_ref(), workspace.clone(), window, cx);
        let activity = self.render_activity_bar(&workspace, needs_you, active_key, cx);
        let board_column = self
            .board_open
            .then(|| self.render_board(board.as_ref(), error, &workspace, window, cx));
        v_flex()
            .id("mini-ork-sidebar")
            .track_focus(&self.focus_handle)
            .size_full()
            .bg(c(p::CHROME))
            .text_color(c(p::TEXT))
            .child(strip)
            .child(
                h_flex()
                    .flex_1()
                    .min_h_0()
                    .items_stretch()
                    .child(activity)
                    .children(board_column),
            )
    }
}
