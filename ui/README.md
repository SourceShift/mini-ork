# `mini_ork_ui` — the mini-ork IDE crate

This is the source of the **mini-ork IDE**, the native (GPUI) user interface for
mini-ork. It is a library crate drawn inside a fork of Zed; the fork lives in the
separate **`zed-mini-ork`** repository, which vendors this directory as
`crates/mini_ork_ui`.

**This directory is the canonical home of the source.** Edit it here. The
`zed-mini-ork` fork copies it into `crates/mini_ork_ui/` before every cargo
command (its `script/mini-ork-sync-ui`), and git-ignores the copy — so the
source of truth is always this directory, never the copy in the fork.

## It does not build here

`mini_ork_ui` cannot compile in this repository. Fourteen of its dependencies are
Zed-internal path crates (`gpui`, `ui`, `workspace`, `agent_ui`, `theme`,
`project`, `settings`, `git_ui`, `platform_title_bar`, `markdown`,
`http_client`, `fs`, `util`, `ui_input`) and its manifest is a Zed-workspace
member (`edition.workspace = true`, all `dep.workspace = true`). To build or
type-check it, work in a `zed-mini-ork` checkout with this repo beside it — see
that repo's `MINI-ORK.md`.

## How it reaches mini-ork

Through the `mini-ork` CLI (`board …`, `automations …`, `init`, `serve`) as a
subprocess, and — when a server is already running — the read-only `/ide/*` HTTP
surface. See [`../docs/UI.md`](../docs/UI.md).
