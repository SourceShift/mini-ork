# Session gaps — 4f781acd (orchestrator page redesign: engine page + fork renderer conformance)

Session: `4f781acd-be5b-4284-84e6-00f395cde3b3` (Claude Code; launched in the zed-mini-ork fork,
work lands in the mini-ork engine at `/Users/admin/ps/mini-ork`).
Reported: 2026-10-16. Repo heads at audit time: `60c00154` on `main` (mini-ork),
`9154ef4` on `main` (mini-ork-ui), `e88610c48e` on branch `mini-ork` (zed fork).

No trust was placed in recorded status. Every row below was re-checked by running the page,
the tests, or reading the crate — commands and receipts are quoted verbatim.

## (a) Task table

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| orch-P0/P1 | Wire P0 payloads; ship P1 dispatch | `[x]` | **shipped** | `6f15537d`; `tests/unit/test_ide_pages_actions.py` green (part of the 50-passed run below) |
| orch-P2 | Answer a parked run from the IDE | `[x]` | **shipped** | `5bb1ff70` |
| orch-P3 | Race-keep, arrivals run link, race panel | `[x]` | **shipped** | `4b48a84a`; `tests/unit/test_ide_pages_orch_races.py` green |
| orch-P4 | The attention strip | `[x]` | **not re-verified** | `5bf2ea5b` exists on `main`; the strip's own content was not re-measured this session |
| FR-023 | Planner tab: classifications table (J9 / AC-009) | `[x]` | **shipped** | `f7b46301`; `tests/unit/test_ide_pages_orch_classify.py` green |
| FR-033 | One table per race + Keep carrying a `confirm` | `[x]` | **shipped** | `4b48a84a`; races tests green |
| AC-012 | Fork renderer covers every emitted section type and action key | `[x]` | **durable half verified; the census claim is wrong** | see G1 + G2 |
| NFR | Warm-page budget on the default tab | `[x]` | **not re-measured** | the 677→721 ms numbers live in the spec; not re-run today, so treat as recorded-only |
| replay-CLI | Live replay, CLI half, all eight tabs | `[x]` | **verified today** | all 8 tabs built through `board page orch --tab <t>` → `errors={}` on every one |
| replay-GUI | Live replay, GUI half (the pixels) | `[~]` | **open — blocked on user** | needs the IDE window; standing instruction keeps FE launches off this session |
| full-unit-suite | Full `tests/unit`: is the ACP trio the only red? | `todo` (CN `cn-94ed276f`) / `completed` (z-insight) | **DONE — and the premise was false** | `11 failed, 9895 passed, 15 skipped, 8 errors in 3533.93s`; eight reds besides the trio |
| acp-trio | Decide whether to refresh the three ACP assertions | `todo` (CN `cn-dc146a0c`) | **open** | `pytest tests/unit/test_acp_agent_py.py -q --tb=no -rf` → `3 failed, 170 passed in 138.95s` |
| minimax-quota | minimax lane is out of quota | `todo` (CN `cn-6c3c96a1`) | **open, and real** | `pytest tests/unit/test_acp_lane_repair.py -q --tb=line` → `3 failed, 19 passed in 44.84s`, on `run-lane-1 stopped: minimax minimax is out of quota` |
| push-backlog | Land this session's spec commits | — | **done** | `02734e1a` on `main`; `git rev-list --left-right --count origin/main...HEAD` → `0 0` |

Test receipt for every orchestrator-page file at once, run this session:

```
pytest tests/unit/test_ide_pages_orch.py tests/unit/test_ide_pages_orch_classify.py \
       tests/unit/test_ide_pages_orch_races.py tests/unit/test_ide_pages_actions.py \
       tests/unit/test_native_dispatch_py.py -q --tb=no -rf
-> 50 passed in 134.50s (0:02:14)
```

## (b) Missing features & gaps

**G1 — the AC-012 payload census does not reproduce. Spec-vs-shipped mismatch.**
`specs/orchestrator-page-redesign.spec.md:1041` claims "**155 `{"set": …}` actions, 43
`cli`+`confirm`+`home` and 1 `cli`+`confirm`**", read as a property of the page. Re-measured
on the same primary home, all eight tabs, `MINI_ORK_IDE_SPEC` varied:

| level | set | cli | home | confirm | thread | path | page | run |
|---|---|---|---|---|---|---|---|---|
| unset | 155 | 50 | 22 | **2** | 36 | 51 | 25 | 13 |
| `1` | 155 | 50 | 22 | **2** | 36 | 51 | 25 | 13 |
| `2` | 155 | 110 | 82 | **51** | 48 | 52 | 25 | 13 |

`set=155` matches the claim exactly. `confirm` does not: it is **2** at level 1 and **51** at
level 2, and the claim's "43 + 1" sits between the two. Worse, the same command at the same
level returned different totals minutes apart (`cli` 50 → 110, `home` 22 → 82), because
several tabs render live machine state (parked runs, concord sessions, automations), so the
count moves as that state moves. The census is therefore **both level-dependent and
state-dependent**, and the spec quotes one snapshot as though it were invariant. Its purpose
— evidence that no affordance draws and does nothing — survives (G2 verifies it
structurally), but the numbers should be corrected or replaced with the structural claim.

**G2 — AC-012's durable claim verified (recorded here so the aggregator is not left with
only the G1 negation).**

- All 8 tabs build with `errors={}`: `session dispatch decide kickoffs races planner coord intake`.
- 9 section types are emitted: `chips, pills, table, triage, composer, list, kv, code, checks`.
  Each has a dispatch arm in `crates/mini_ork_ui/src/page.rs` — `table :896`, `kv :897`,
  `list :898`, `triage :886`, `composer :890`, `code`/`chips`/`pills`/`checks` likewise.
- 6 action verbs are emitted: `cli, page, set, run, path, thread`. Each has a branch in
  `crates/mini_ork_ui/src/store.rs::perform` (starts `:476`; `cli :483`, `page :487`,
  `set :513`, `run :516`, `path :537`), and `confirm` is honoured at `page.rs:564`.
- The fork actually asks for the v2 contract: `crates/mini_ork_ui/src/board.rs:194` and
  `:351` both set `MINI_ORK_IDE_SPEC=2`. Without that, `spec.py::ide_level()` returns 1 and
  the redesigned sections would never be emitted — this is the seam that makes the
  redesign visible at all.
- No engine↔fork drift: `ui/` (engine submodule) and `crates/mini_ork_ui` (fork) are
  byte-identical for `page.rs`, `sections.rs`, `store.rs`, `board.rs`.

**G3 — the GUI half of the live replay is unrun.** Both spec items that are `[~]` fail on the
same thing: nobody has looked at the pixels. The CLI half is clean, so this is the only
remaining check on the redesign's *presentation*.

**G4 — three ACP assertions are red and unfixed, by agreement.** They are not this work's
(`fae2a7a5`), and two of the three are stale arithmetic over now-correct accounting rather
than defects. No test has been touched pending the user's word.

**G5 — the minimax lane is out of quota, for real.** Not a code defect: three *unit* tests
(`test_acp_lane_repair.py`) drive a real `mini-ork run` and cannot reach their asserted end
state while the lane is exhausted (`assert ['in_progress','failed'] == ['in_progress','completed']`).
Any run routed to minimax stops. Two consequences worth escalating: the account needs topping
up, and those three tests belong to the unit suite while depending on a live funded provider,
so their verdict reports on the account rather than the commit.

**G6 — the full unit suite is red by 11, of which the redesign owns none.** Classified by
re-running each red file in isolation (a red that survives isolation is code-or-machine; one
that vanishes is run-order): 3 the ACP trio (G4), 3 the quota (G5),
1 `test_serve_down_returns_command` (the shared serve holds `127.0.0.1:7090` — `lsof` confirms
a listener), 1 `test_real_container_session` (docker: `mkdir /Volumes/docker-ssd: file exists`
— Docker Desktop cannot bind-mount under the external volume), 1
`test_fresh_process_sandbox_gc_all_sweeps_lazy_providers` (wants `SDK_ABSENT`, machine reports
`SDK_LOADED`), 1 `test_coalition_gate_defaults_to_the_root_config` (resolved the checkout's
generated `.mini-ork/config/.agents.effective.yaml` instead of its tmp `agents.yaml`), and 1
`test_valid_rows_void_after_edit` (green in isolation — order-dependent). Plus 8 fixture
errors in `test_workspace_conformance[docker]`, the same bind-mount family. This also explains
why a local run is redder than CI: that `.agents.effective.yaml` is untracked, so no CI
checkout has one.

**G7 — the ContextNest inbox is stale for this session.** `cn-94ed276f` ("Full tests/unit run:
is the ACP trio the only red?") is still recorded `todo` though it completed this session; a
reader trusting the inbox would re-run a 59-minute suite to learn what is already known.

## (c) Blocked on

- **The user, for the GUI replay** — launch the IDE and look at the Orchestrator tabs. Nothing
  in this session can substitute: it is a front-end launch plus a judgement about pixels.
- **The user, for the ACP trio** — whether to refresh the two cost-arithmetic assertions (the
  accounting is right, the expectations are stale) and whether `/run` should stay quiet, which
  is a product call the spec records as by-design.
- **The user, for minimax quota** — a top-up or a re-point; three tests and any run routed
  there are stuck until then.
- **Nobody, for G1** — the spec's census line should be corrected; it is a documentation fix
  with no code behind it.
