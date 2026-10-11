//! Switching the IDE window between mini-ork projects.
//!
//! Known projects are the homes listed in `~/.config/mini-ork/projects.json`
//! (the registry `mini-ork serve` and Setup › Projects share;
//! `MINI_ORK_PROJECTS_FILE` overrides the path). Switching opens the project's
//! folder as a workspace of this window and activates it, so each project keeps
//! its own Threads board and tabs.
//!
//! A project joins the list only when it is switched to, or added through the
//! menu — never as a side effect of opening a folder. Removing one edits the
//! registry alone; nothing on disk is touched. The list is one model
//! ([`entries`]) rendered by both the title-bar menu and the sidebar.

use std::path::{Path, PathBuf};

use gpui::{App, AppContext as _, Entity, PathPromptOptions, SharedString, TaskExt as _, Window};
use serde_json::{Value, json};
use ui::{ContextMenu, ContextMenuEntry};
use util::ResultExt as _;
use workspace::{MultiWorkspace, OpenMode, OpenOptions, Toast, Workspace, notifications::NotificationId};

use crate::board;

fn registry_file() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("MINI_ORK_PROJECTS_FILE").filter(|p| !p.is_empty()) {
        return Some(PathBuf::from(path));
    }
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config/mini-ork/projects.json"))
}

fn read_registry(file: &Path) -> Value {
    std::fs::read_to_string(file)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({ "projects": [] }))
}

/// The project folder a registry entry names: the parent of a `.mini-ork` home,
/// or the entry itself when it is already a folder.
fn project_root(entry: &Path) -> PathBuf {
    match entry.file_name() {
        Some(name) if name == ".mini-ork" => entry
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| entry.to_path_buf()),
        _ => entry.to_path_buf(),
    }
}

/// Whether a registry entry names `root` — accepting `<root>` and `<root>/.mini-ork`.
fn entry_names(entry: &str, root: &Path) -> bool {
    project_root(Path::new(entry)) == root
}

/// Project folders named by `registry`, in order, deduplicated.
fn roots_of(registry: &Value) -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = Vec::new();
    for entry in registry
        .get("projects")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(entry) = entry.as_str() else { continue };
        let root = project_root(Path::new(entry));
        if !roots.contains(&root) {
            roots.push(root);
        }
    }
    roots
}

/// Write `registry` to `file` through a sibling temp file and a rename. The
/// indirection matters: a torn write reads back as `{"projects": []}`, so an
/// interrupted plain write would erase every project, not merely corrupt one.
fn write_registry(file: &Path, registry: &Value) -> std::io::Result<()> {
    let dir = file.parent().unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(dir)?;
    let text = serde_json::to_string_pretty(registry).map_err(std::io::Error::other)?;
    let tmp = file.with_extension("json.tmp");
    std::fs::write(&tmp, text + "\n")?;
    std::fs::rename(&tmp, file)
}

/// Drop every entry that names `root`. Returns whether anything was removed.
fn registry_remove(registry: &mut Value, root: &Path) -> bool {
    let Some(list) = registry.get_mut("projects").and_then(Value::as_array_mut) else {
        return false;
    };
    let before = list.len();
    list.retain(|entry| entry.as_str().is_none_or(|entry| !entry_names(entry, root)));
    list.len() != before
}

/// Project folders (the parents of each `.mini-ork` home), registry order.
pub fn known() -> Vec<PathBuf> {
    registry_file()
        .map(|file| roots_of(&read_registry(&file)))
        .unwrap_or_default()
}

/// Add `home` (a `.mini-ork` directory) to the registry, once.
pub fn remember_home(home: &Path) {
    let Some(file) = registry_file() else { return };
    let mut registry = read_registry(&file);
    let entry = home.to_string_lossy().to_string();
    let Some(list) = registry
        .as_object_mut()
        .map(|object| object.entry("projects").or_insert_with(|| json!([])))
        .and_then(Value::as_array_mut)
    else {
        return;
    };
    if list.iter().any(|known| known.as_str() == Some(entry.as_str())) {
        return;
    }
    list.push(Value::String(entry));
    write_registry(&file, &registry).log_err();
}

/// Remove `root` from the registry. Nothing outside the registry is touched:
/// the folder, its `.mini-ork` and every run in it stay exactly as they are.
pub fn forget(root: &Path) {
    let Some(file) = registry_file() else { return };
    let mut registry = read_registry(&file);
    if registry_remove(&mut registry, root) {
        write_registry(&file, &registry).log_err();
    }
}

/// One row of the project list, as the title-bar menu and the sidebar both
/// render it.
#[derive(Clone)]
pub struct ProjectEntry {
    pub root: PathBuf,
    pub name: String,
    /// The root with `$HOME` shortened to `~`.
    pub path: String,
    /// No `.mini-ork` here, so it cannot be opened as a project.
    pub missing: bool,
    pub is_current: bool,
}

/// The known projects plus the current one when the registry does not hold it,
/// current first.
pub fn entries(workspace: &Entity<Workspace>, cx: &App) -> Vec<ProjectEntry> {
    let current = current_root(workspace, cx);
    let mut roots = known();
    if let Some(current) = current.clone().filter(|root| !roots.contains(root)) {
        roots.insert(0, current);
    }
    let home = std::env::var("HOME").unwrap_or_default();
    roots
        .into_iter()
        .map(|root| {
            let name = root
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_else(|| root.to_string_lossy().to_string());
            let full = root.to_string_lossy();
            // An empty `HOME` would make `replace` insert `~` between every
            // character, so only shorten when there is something to shorten.
            let path = if home.is_empty() {
                full.to_string()
            } else {
                full.replace(&home, "~")
            };
            ProjectEntry {
                missing: !root.join(".mini-ork").is_dir(),
                is_current: current.as_deref() == Some(root.as_path()),
                root,
                name,
                path,
            }
        })
        .collect()
}

/// The commands every surface offers for one project, in one place, so the
/// title-bar submenu and the sidebar row cannot drift apart.
pub fn project_actions(
    menu: ContextMenu,
    workspace: &Entity<Workspace>,
    entry: ProjectEntry,
) -> ContextMenu {
    let mut menu = menu.header(entry.root.display().to_string());
    if !entry.missing {
        let (workspace, root) = (workspace.clone(), entry.root.clone());
        menu = menu.entry("Switch to project", None, move |window, cx| {
            switch_to(&workspace, root.clone(), window, cx);
        });
        let root = entry.root.clone();
        menu = menu.entry("Reveal in Finder", None, move |_, cx| {
            cx.reveal_path(&root);
        });
    }
    let path = entry.root.to_string_lossy().to_string();
    menu = menu.entry("Copy path", None, move |_, cx| {
        cx.write_to_clipboard(gpui::ClipboardItem::new_string(path.clone()));
    });
    // Removing the project this window is showing would be undone the moment it
    // is opened again, so it is offered for every row but that one.
    menu.item(
        ContextMenuEntry::new("Remove from list")
            .disabled(entry.is_current)
            .handler({
                let root = entry.root;
                move |_, _| forget(&root)
            }),
    )
}

/// The git worktree this window is in, labelled the way stock Zed's title bar
/// labels it: the main checkout reads `main`, a linked worktree the short name
/// that tells it apart from the main one. `None` outside a repository, or when
/// the worktree has no repository yet (a folder that was just opened).
pub fn worktree_label(workspace: &Entity<Workspace>, cx: &App) -> Option<SharedString> {
    let project = workspace.read(cx).project().read(cx);
    let path = project.visible_worktrees(cx).next()?.read(cx).abs_path();
    // The repository whose working directory contains this worktree; the
    // longest match wins, so a worktree nested in another one resolves to
    // itself.
    let repository = project
        .repositories(cx)
        .values()
        .filter(|repository| {
            let root = &repository.read(cx).work_directory_abs_path;
            path.as_ref() == root.as_ref() || path.starts_with(root.as_ref())
        })
        .max_by_key(|repository| repository.read(cx).work_directory_abs_path.as_os_str().len())?
        .read(cx);
    if repository.is_main_worktree() {
        return Some("main".into());
    }
    project::git_store::linked_worktree_short_name(
        repository.main_worktree_abs_path()?,
        &repository.work_directory_abs_path,
    )
}

/// The folder of this workspace's first visible worktree.
pub fn current_root(workspace: &Entity<Workspace>, cx: &App) -> Option<PathBuf> {
    let project = workspace.read(cx).project().read(cx);
    let worktree = project.visible_worktrees(cx).next()?;
    Some(worktree.read(cx).abs_path().to_path_buf())
}

/// Switch this window to the project at `root`: open its folder as a
/// workspace of this window (or activate it when it is already open). This is
/// the one gesture that adds a project to the registry.
pub fn switch_to(workspace: &Entity<Workspace>, root: PathBuf, window: &mut Window, cx: &mut App) {
    if current_root(workspace, cx).as_deref() == Some(root.as_path()) {
        return;
    }
    let home = root.join(".mini-ork");
    if home.is_dir() {
        remember_home(&home);
    }
    let app_state = workspace.read(cx).app_state().clone();
    let options = OpenOptions {
        requesting_window: window.window_handle().downcast::<MultiWorkspace>(),
        open_mode: OpenMode::Activate,
        ..OpenOptions::default()
    };
    workspace::open_paths(&[root], app_state, options, cx).detach_and_log_err(cx);
}

/// Open the project at `root`, initializing it first when it is not one yet.
/// A folder without `.mini-ork` gets a confirmation before anything is written
/// into it — nothing is created without an explicit yes.
pub fn open_or_init(workspace: &Entity<Workspace>, root: PathBuf, window: &mut Window, cx: &mut App) {
    if root.join(".mini-ork").is_dir() {
        switch_to(workspace, root, window, cx);
        return;
    }
    let answer = window.prompt(
        gpui::PromptLevel::Warning,
        &format!("Set up mini-ork in {}?", root.display()),
        Some("mini-ork will create a .mini-ork directory in this folder."),
        &["Initialize", "Cancel"],
        cx,
    );
    let workspace = workspace.downgrade();
    let window_handle = window.window_handle();
    cx.spawn(async move |cx| {
        if answer.await.ok() != Some(0) {
            return;
        }
        // `mini-ork init` learns the folder from `PWD`, not from `--home`, so
        // the run is handed the folder by `run_action`'s working directory.
        let home = root.join(".mini-ork");
        let outcome = cx
            .background_spawn(board::run_action(
                board::mini_ork_bin(),
                home,
                vec!["init".to_string()],
                false,
            ))
            .await;
        cx.update(|cx| {
            let Some(workspace) = workspace.upgrade() else { return };
            let message = match &outcome {
                Ok(done) => format!("mini-ork init in {}: {done}", root.display()),
                Err(error) => format!("mini-ork init in {} failed: {error}", root.display()),
            };
            let toast = Toast::new(NotificationId::named(SharedString::from("mini-ork-init")), message)
                .autohide();
            workspace.update(cx, |workspace, cx| workspace.show_toast(toast, cx));
            if outcome.is_err() {
                return;
            }
            window_handle
                .update(cx, |_, window, cx| switch_to(&workspace, root, window, cx))
                .log_err();
        });
    })
    .detach();
}

/// Ask for a folder, then open it as a project, initializing it when needed.
/// One path for every "add a project" affordance in the IDE.
pub fn add(workspace: &Entity<Workspace>, window: &mut Window, cx: &mut App) {
    let paths = cx.prompt_for_paths(PathPromptOptions {
        files: false,
        directories: true,
        multiple: false,
        prompt: Some("Add mini-ork project".into()),
    });
    let workspace = workspace.downgrade();
    window
        .spawn(cx, async move |cx| {
            let Ok(Ok(Some(paths))) = paths.await else { return };
            let Some(root) = paths.into_iter().next() else { return };
            cx.update(|window, cx| {
                if let Some(workspace) = workspace.upgrade() {
                    open_or_init(&workspace, root, window, cx);
                }
            })
            .log_err();
        })
        .detach();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn registry(entries: &[&str]) -> Value {
        json!({ "projects": entries })
    }

    #[test]
    fn a_home_entry_and_a_plain_entry_name_the_same_root() {
        assert!(entry_names("/ps/mini-ork/.mini-ork", Path::new("/ps/mini-ork")));
        assert!(entry_names("/ps/mini-ork", Path::new("/ps/mini-ork")));
        assert!(!entry_names("/ps/other/.mini-ork", Path::new("/ps/mini-ork")));
    }

    #[test]
    fn roots_are_the_parents_of_homes_deduped_and_in_order() {
        let roots = roots_of(&registry(&[
            "/ps/b/.mini-ork",
            "/ps/a/.mini-ork",
            "/ps/b",
            "/ps/c",
        ]));
        assert_eq!(
            roots,
            vec![
                PathBuf::from("/ps/b"),
                PathBuf::from("/ps/a"),
                PathBuf::from("/ps/c"),
            ]
        );
    }

    #[test]
    fn removing_drops_every_spelling_of_the_root_and_keeps_siblings() {
        let mut value = json!({
            "projects": ["/ps/a/.mini-ork", "/ps/b", "/ps/a"],
            "other": true,
        });
        assert!(registry_remove(&mut value, Path::new("/ps/a")));
        assert_eq!(value["projects"], json!(["/ps/b"]));
        assert_eq!(value["other"], json!(true));
    }

    #[test]
    fn removing_something_absent_says_so_and_a_missing_list_is_not_an_error() {
        let mut value = registry(&["/ps/a"]);
        assert!(!registry_remove(&mut value, Path::new("/ps/z")));
        let mut empty = json!({});
        assert!(!registry_remove(&mut empty, Path::new("/ps/a")));
    }

    #[test]
    fn write_then_read_round_trips_and_leaves_no_temp_file() {
        let file = std::env::temp_dir().join(format!(
            "mini-ork-projects-test-{}.json",
            std::process::id(),
        ));
        let value = registry(&["/ps/a/.mini-ork"]);
        write_registry(&file, &value).expect("write registry");
        assert_eq!(read_registry(&file), value);
        assert!(!file.with_extension("json.tmp").exists());
        std::fs::remove_file(&file).ok();
    }
}
