# Session gaps — da0b8c69 (run page IA: one page always, the reason promoted, raw output folded)

Session: `da0b8c69-720a-4bb1-8f6f-ce2d80683760` (Claude Code; launched in the zed-mini-ork fork,
work lands in the mini-ork engine at `/Users/admin/ps/mini-ork`).
Reported: 2026-10-16. Repo heads at audit time: `93ec6448` on `main` (mini-ork — a peer's
docs(audit) commit, not mine), `9154ef4` on `main` (crates/mini_ork_ui), `e88610c48e` on
branch `mini-ork` (zed fork).

**Everything in this session is UNCOMMITTED.** Both trees are dirty and nothing is pushed. The
table below therefore records verified-in-working-tree, not verified-in-git.

No trust was placed in my own recorded status. Each row was re-checked by running the page,
the tests, or reading the source; commands and receipts are quoted verbatim.

## (a) Task table

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| plan | Run-page IA redesign, Parts 0–3 | approved | **implemented, uncommitted** | plan `/Users/admin/.claude/plans/sequential-plotting-blossom.md`; 6 engine files + 2 crate files dirty |
| P0-route | `/ide/page` honours a per-request `spec` | done | **verified through the real route** | `TestClient` on `/ide/page`: `spec=None`/`'abc'`/`'0'` → v1 tabs; `'2'`/`'99'` → v2; the `'abc'`-after-`'2'` request proves the ContextVar resets |
| P0-crate | One `IDE_SPEC`, sent per request | done | **compiles** | `./script/clippy -p mini_ork_ui` → exit 0 |
| P0-live | The plan's decisive curl at `:7090` | — | **NOT RUN** | the daemon has held old code for 1d 5h (`serve --port 7090 --home /private/tmp/mo-smoke50/.mini-ork`); restarting a server I did not start is the user's call — see G1 |
| P1 | `Why — evidence` promoted to section 2; the `?` label gone | done | **verified on live data** | story page dumps `s0 triage / s1 list 'Why — evidence' / s2 hero / …`; acceptance 4 reads `Failed at gates_materialize`, kind `code` |
| P2 | One collapsed `Diagnostics` home for raw output | done | **verified** | `s5 story 'Diagnostics'`; renders a `files` block on `child-1789795349-93634`; needs no renderer change (`render_story` keys expansion off `self.expanded`) |
| P3 | Empties vanish; counts on tab badges | done | **verified** | badges `Agents · 15`, `Artifacts · 89`; `Questions` count 0 → plain label; a run with atoms keeps `Decisions and assumptions` in its new slot |
| v1-frozen | `_overview_tab` output unchanged | — | **verified byte-identical** | section dump matches pre-change exactly (`s0 list 'Retry'` … `s7 list 'Files changed'`) |
| retry-hint | Verifier payload reader + hint version | done | **66 passed** | `pytest tests/unit/test_retry_hint.py tests/unit/test_verify_levels.py -q` → `66 passed in 82.66s` |
| run-page-tests | The run page's own suites | — | **2 failed, 38 passed** | `pytest tests/unit/test_ide_pages_run_story.py tests/unit/test_ide_pages_run.py -q` → `2 failed, 38 passed in 29.69s` |
| broader-sweep | Earlier sweep incl. the CN run-memory file | — | **5 failed, 89 passed** | `5 failed, 89 passed in 63.01s`; all five are the ordering/placeholder changes below, none a regression |
| spec-rule | Re-read the locked architecture rule | — | **done, unchanged** | `specs/orchestrator-page-redesign.spec.md:31-35` still: no new section type, no renderer change |
| build | `script/mini-ork-build` | — | **running at audit time** | background task `bm964hxf0`; not finished, so "the app carries P0" is not yet a fact |
| gui-smoke | GUI smoke test | — | **open — blocked on user** | launching the app is the user's; the standing instruction keeps FE launches off this session |

## (b) Findings

**G1 — the plan's headline acceptance criterion was never executed as written.**
The plan's decisive receipt is a `curl` against the live `:7090` daemon. It did not run: that
daemon predates the change and answers level 1 no matter what the query says. What ran instead is
the same claim in-process — the real FastAPI route, real engine code, via `TestClient` — plus the
subprocess path directly. That is *equivalent evidence*, not *the same evidence*: it does not prove
the query string survives a real socket, a real client, and a daemon whose own env default is
level 1. Restarting the daemon closes it. I did not kill a server another session started.

**G2 — five tests are red and were left red on purpose.**
The project rule is not to touch tests before the user confirms the behaviour. The five:
`test_v2_story_tab_sections` and `test_story_tab_is_guarded_when_the_section_raises` (ordering —
`Diagnostics` is now last, so `kinds[-1] == "story"`, not `"list"`); `test_disabled_cn_decisions_section_renders_one_line_state`
and `test_decisions_section_empty_when_no_node_has_a_session` (`"No decision atoms recorded"` is
gone by design; the blob is now `'null'`); `test_run_page_build_with_run_memory_remains_ok_true`
(`"Decisions and assumptions"` is absent when there are no atoms). The plan predicted three of
these five. **Until they are updated the suite cannot distinguish this change from a regression**,
which is the real cost of leaving them.

**G3 — the `?` had a different cause than the plan recorded, and the real defect is broader than
the page.** The plan blamed the fallback chain at `retry_hint.py:1878-1881`. The actual cause:
`mini_ork/verify/levels.py::read_verifier_payload` requires a `verifier` field to name-match, and
**62 of 123** verifier scripts under `recipes/*/verifiers/` never write one (recounted this
session, comments excluded; a peer's independent glob agrees at 62/123). For those, the sidecar
reads as "no verdict at all", `_first_failed_node` comes back empty, `_case_unknown` fires, and the
page reports `Failed at ?; the cause was not classified` over a verdict sitting on disk. Fixed at
the reader with a `read_single_verifier_payload` fallback plus `HINT_VERSION 4 → 5` (the version
field is the *only* thing that retires a stale `retry-hint.json` — mtimes cannot date one). The
root remains: a name-matched read is the wrong primary for a file whose name already says which
verifier wrote it.

**G4 — my own docstring carried a number I had not verified.** `read_single_verifier_payload`'s
docstring said "57 of the recipes' 117 verifier scripts". The verified figure is 62 of 123.
Corrected this session. Worth recording because it is the same failure mode as G3: a plausible
ratio, asserted, never counted.

**G5 — a peer's hypothesis about `read_verifier_payload` was checked and refuted.**
The peer read the docstring ("the LAST JSON object whose `verifier` equals …") against the loop
(returns on the **first** match) and concluded the loop was the bug. It is not:
`_iter_json_objects` walks the file **bottom-up** and yields the trailing blob before anything
nested in it, so first-match *is* last-in-file. `levels.py:206` says "scan bottom-up like
`read_verifier_payload`", confirming the intent. The docstring was made explicit about the
mechanism; no behaviour changed.

**G6 — two checkouts of the crate make a push race.** `crates/mini_ork_ui` (the submodule,
`origin` = SourceShift/mini-ork-ui, HEAD `9154ef4`, my two files dirty) and
`/Volumes/docker-ssd/ps/mini-ork-ui` (different inodes, same `origin`, same HEAD, clean). P0 edits
`board.rs` and `ide_http.rs`; any peer landing either file from the second checkout before this
lands conflicts on push. This is the standing reason `board.rs` changes need serialising.

**G7 — the engine tree is shared and four of its dirty files are not mine.**
Alongside my six, `/Users/admin/ps/mini-ork` holds `docs/architecture/artifact-transport.md`,
`docs/architecture/per-node-sandboxes.md`, `mini_ork/gates/rubric_scoring.py`, and
`tests/unit/test_rubric_prescreen_py.py`. A `git add -A` there would sweep a peer's mid-edit work
into my commit. Not touched.

## (c) What this session did NOT establish

- That the app renders the new run page. The build was still running at audit time and the GUI
  pass is the user's.
- That the level-2 page is what a *user* sees end to end. In-process route proof is not a socket.
- That the `Diagnostics` disclosure behaves when clicked. `open=False` seeding is read from the
  source, not exercised.
- Render cost is measured (~54 ms added: `why_section` 0.5, `diagnostics_sections` 47.9,
  `_v2_tabs` 5.2, against pre-existing `_attach_flow` at 2.2 s) but not against a budget.
