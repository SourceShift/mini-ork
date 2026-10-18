# Lens: activation — install to first value

Model observed: GLM-5.3 (lane: activation). Reference: OpenClaw checkout at
`$REF` (read-only). Target: mini-ork
worktree at `$MO_TARGET_CWD`
(`$MO_TARGET_CWD`, read-only). All commands below were read-only (ls, sed,
grep, cat, git show/ls-tree); no file outside this run directory was created,
edited, moved or deleted.

## Evidence Read

Reference checkout (`REF=$REF`):

- `README.md:24-66` — install section: `curl -fsSL https://openclaw.ai/install.sh | bash`
  (macOS/Linux/WSL2) and `iwr -useb https://openclaw.ai/install.ps1 | iex`
  (Windows); "The installer supports macOS, Linux, and Windows. It provisions a
  supported Node.js runtime when needed." Quick start: "On a fresh install, the
  installer scripts start onboarding automatically", then `openclaw gateway
  status` and `openclaw dashboard`.
- `package.json:34-36` — `"bin": {"openclaw": "openclaw.mjs"}`; files list
  includes `scripts/install-cli.sh`, `scripts/install.ps1`,
  `scripts/postinstall-bundled-plugins.mjs` (npm package ships installers).
- `git -C $REF show HEAD:docs/start/getting-started.md` (not checked out;
  read via git):
  - line 28: `npx openclaw@latest` — "Try it in one command"; Quick start
    "finds an existing Claude Code or Codex CLI login or API key, verifies it
    with a real completion, saves the config, and opens the web dashboard"
    (lines 31-35).
  - lines 96-105: Quick setup steps — install → wizard → `openclaw gateway
    install` → `openclaw gateway status` ("You should see the Gateway
    listening on port 18789") → `openclaw dashboard` ("If it loads, everything
    is working") → send first message.
  - lines 158-172: "If setup does not work" → `openclaw triage`: "runs
    read-only health checks, writes a sanitized prompt describing what it
    found, and then offers to hand that prompt to a coding agent it detects on
    your machine… Nothing leaves your machine until you choose an agent."
- `git -C $REF show HEAD:docs/start/wizard.md`:
  - lines 115-126 — Quick start detection is read-only and broad: "Detect
    configured models, API-key environment variables, supported local AI CLIs,
    and already installed tool-capable models from reachable Ollama or LM
    Studio servers"; "Only that connection is tested with a real completion."
  - lines 26-28 — "Interrupted baseline setup resumes on the next run";
    "**Inference ready** appears only after the selected connection passes
    verification."
- `git -C $REF show HEAD:docs/start/onboarding-overview.md:11-12,36` — "Every
  path establishes inference first… verifies a real completion before
  configuring the remaining setup."
- `docs/cli/doctor.md:9-19,42-55` — `openclaw doctor`: "Health checks and
  quick fixes… Use it whenever something is not behaving as expected and you
  want one command to explain what is wrong." Documented across seven pages;
  `--fix` performs repairs; lint and post-upgrade modes exist.
- `docs/cli/triage.md:9-40` — `openclaw triage` detection order (codex, claude,
  pi, opencode, muse, grok, cursor-agent, kimi, qwen, agy); the prompt carries
  version, platform, "prioritized Doctor findings with repair hints"; archive
  "excludes secrets, tokens, raw chat payloads, and raw logs."
- `docs/install/index.md:221-233` — "Verify the install": `openclaw --version`
  → `openclaw doctor` → `openclaw gateway status`.
- `docs/install/installer.md:130-160` — install.sh flow: installs a supported
  Node.js runtime when needed, installs Git if missing; post-install: "For an
  unconfigured install, starts onboarding before doctor or gateway checks.
  With `--no-onboard` or no TTY, it prints the command to finish setup later."
- `docs/help/faq-first-run.md` + `docs/help/faq-first-run/quick-start.md` — a
  dedicated first-run FAQ; accordion "I am stuck, fastest way to get unstuck"
  says "Use a local AI agent that can see your machine."
- `docs/help/troubleshooting.md:11-40` — "## First 60 seconds": an ordered
  command ladder (`openclaw triage`, `openclaw status`, `gateway probe`,
  `doctor`, …) with "Good output, one line each."

Target tree (`$MO_TARGET_CWD=$MO_TARGET_CWD`):

- `README.md:210-212` — one-line installer exists:
  `curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh`.
- `README.md:235-241` — native Windows requires manually winget-installing five
  packages (`Python.Python.3.11`, `Git.Git`, `jqlang.jq`, `MikeFarah.yq`,
  `SQLite.SQLite`), then `py -3 .\scripts\full_install.py`.
- `README.md:256-281` — "Your first verifier-backed workflow": six manual steps
  (`mkdir -p ~/miniork-demo && cd` → `git init` → `mini-ork init` →
  `cp "$MINIORK_SOURCE/examples/01-hello-world/kickoff.md" ./kickoff.md` →
  `MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md` → `mini-ork validate`).
- `README.md:275-277` — "For a real run, review **.mini-ork/config/agents.yaml**,
  authenticate the CLI or configure the providers it names, then run the same
  command without `MINI_ORK_DRY_RUN=1`."
- `install.sh:92` + `scripts/full_install.py:148-149` — installer execs
  `full_install.py`, which ends with "✓ MiniOrk full runtime installed in
  <venv>" and one hint: "Configure provider lanes with: mini-ork providers
  status <lane>". No first-run/demo pointer, no auto-onboard.
- `examples/00-demo.sh` — an offline dry-run demo already exists as a bash
  script (temp dir, `mini-ork init`, `doctor`, synthetic kickoff, dry `run`,
  task_runs inspection); line 73: in dry-run mode there is **no task_runs row**
  ("the Python classify runtime early-exits before INSERT when
  MINI_ORK_DRY_RUN=1, by design").
- `examples/01-hello-world/README.md:11,21` — example quick start says
  `mini-ork version` → "mini-ork 0.1.0" and copies the kickoff via the author's
  personal absolute path `cp ~/ps/mini-ork/examples/01-hello-world/kickoff.md`.
  (`mini_ork/cli/main.py:2194-2197` `_version_handler` actually prints
  "mini-ork 0.9.0"; `_HELP` header says "(v0.1)".)
- `mini_ork/cli/main.py:48-55` + `_NATIVE_MODULE_SUBS` (~line 55-120) +
  `_build_default_registry()` (line ~2180-2200) — the subcommand registry:
  `run, doctor, install, providers, mcp, vault, version, board, validate,
  garden, resume, recover, repair, rollback, serve, …`. There is **no** `demo`,
  `onboard`, `triage`, `status`, or `dashboard` subcommand.
- `mini_ork/cli/main.py:2141-2189` — `_doctor_handler`: presence-only checks
  (sqlite3/git/curl/claude/codex/python3 on PATH; `GLM_API_KEY`/`KIMI_API_KEY`/
  `MINIMAX_API_KEY`/`DEEPSEEK_API_KEY` set/unset with "run: mini-ork providers
  configure <family>"); engine-vs-DB version drift warning suggesting
  `mini-ork migrate`; vault backend statuses. No `--fix`, no probe, no
  real-completion verification.
- `mini_ork/cli/validate.py:1-35` — pre-run static checks "with concrete
  `Fix:` hints"; Check 5 is a lane liveness probe: "one cached 1-token probe
  per configured lane, ten-minute TTL."
- `mini_ork/cli/lane_preflight.py:532-556` — blocked-run stderr includes
  per-lane reasons and a final `fix:` line naming `mini-ork recover <run_id>
  --lane …` and gate toggles.
- `mini_ork/cli/providers.py:29-32` — `mini-ork providers status|configure`
  exists (workflow-aware, never prints credentials, stores in
  `$MINI_ORK_HOME/config/secrets.local.sh` with owner-only permissions).
- `mini_ork/dispatch/secrets.py:60-132` — secret store fails closed (raises
  `SecretStoreError` on missing/loose-permission/symlink stores rather than
  expanding to an empty string).
- `mini_ork/dispatch/openai_chat_transport.py:1-10` — an `openai-chat`
  provider kind already exists for OpenAI-compatible `/chat/completions`
  endpoints, explicitly naming "OpenRouter, Groq, Together, local vLLM, etc."
- `docs/UI.md:1-20` — the mini-ork IDE is a Rust (GPUI) fork of Zed vendored as
  the `ui/` submodule; "The crate cannot be built from this repo." There is no
  web frontend ("the old React SPA … has been removed").
- `docs/operator/provider-triage.md` — triage guidance exists but is
  drift-panel-specific (Layer 2b README drift), not a first-run path.
- `mini_ork/cli/run_resume.py:131-181` — `run --resume <run_id>` exists;
  "The state.db is allowed to be missing on disk; an interrupted run …".
- `docs/PYTHON-SDK.md:1-30` — embedding path: `pip install -e '.[full]'`.

Commands run (all read-only): `ls` on both roots and docs trees; `git -C $REF
ls-tree --name-only HEAD docs/start/`; `git -C $REF show HEAD:docs/start/<file>`
for getting-started.md, wizard.md, onboarding-overview.md; `sed -n` / `grep -n`
over the files listed above; `wc -l` for size context.

### Cross-Candidate Observations

- OpenClaw's activation loop is a *sequence the product walks for you*: one
  command → installer auto-starts onboarding → detect what you already have
  (CLI logins, env keys, local Ollama/LM Studio) → verify with a **real
  completion** before claiming success → visible proof (`gateway status`,
  dashboard) → when anything breaks, an ordered ladder (`triage` → `status` →
  `doctor`) plus a dedicated first-run FAQ. mini-ork has strong primitives for
  nearly every stage — offline dry-run (`MINI_ORK_DRY_RUN`), `doctor`,
  `validate` with `Fix:` hints and a lane liveness probe, `providers
  configure`, fail-closed secrets, `run --resume` — but nothing sequences them.
  The newcomer must discover and order ~60 subcommands from a 514-line README.
- Both projects have a curl one-liner; parity ends the moment it finishes.
  OpenClaw's installer "starts onboarding before doctor" (installer.md:157);
  mini-ork's prints a single providers hint (full_install.py:149).
- mini-ork's dry run is genuinely key-free but records no task_runs row
  (00-demo.sh:73), so a newcomer inspecting state.db per README:270-272 cannot
  distinguish "worked" from "did nothing." OpenClaw's equivalent moment is a
  *verified real completion* — success is proved, not assumed.
- Several candidates share one root cause — no guided first-run path — so A1/A2
  /A7 compound: the demo creates the first success, onboarding converts it into
  a first *real* run, and the install tail starts both.

## Candidate Features

### A1 — `mini-ork demo`: one command to a completed, verified run, offline

- evidence: `git -C $REF show HEAD:docs/start/getting-started.md:28-35` ("Try
  it in one command: npx openclaw@latest … verifies it with a real completion,
  saves the config, and opens the web dashboard"); reference README.md:51 ("On
  a fresh install, the installer scripts start onboarding automatically").
- what_openclaw_does: the first command a visitor runs is also the demo — one
  command produces a working, verified result with no manual scaffolding; the
  product walks itself.
- why_it_drives_stars: the developer moment won is "I ran one command and the
  loop completed in front of me." A star is often bookmarked on that moment;
  today mini-ork's equivalent is six copy-paste steps (README:256-268) whose
  example doc contains a broken personal path (`examples/01-hello-world/README.md:21`
  `cp ~/ps/mini-ork/...`). The demo content already exists
  (`examples/00-demo.sh`) — it is just not a command.
- mini_ork_fit: high
- star_impact: high
- effort: S
- risk: a dry-run demo that writes no task_runs row can look like theatre
  ("nothing happened"); the demo must print what the loop proved and where the
  artifacts are, and say explicitly what a real run adds.
- first_step: register a `demo` handler in `mini_ork/cli/main.py`
  (`_build_default_registry`, alongside `doctor`) that wraps the existing
  `examples/00-demo.sh` flow in Python (temp dir, `mini-ork init`, synthetic
  kickoff, `MINI_ORK_DRY_RUN=1 run code-fix`, then print run-dir artifacts and
  the verdict line), ending with "next: mini-ork onboard".

### A2 — `mini-ork onboard`: credential detection + verified-probe wizard

- evidence: `git -C $REF show HEAD:docs/start/wizard.md:115-126` ("Detect
  configured models, API-key environment variables, supported local AI CLIs,
  and already installed tool-capable models from reachable Ollama or LM Studio
  servers… Only that connection is tested with a real completion");
  onboarding-overview.md:11-12 ("Every path establishes inference first");
  README.md:66 ("Onboarding verifies model access…").
- what_openclaw_does: onboarding reuses credentials the visitor *already has*
  (a Claude Code/Codex login on PATH, an env key, a local model server),
  verifies the chosen route with a real completion, and only then writes
  config — "Inference ready" is earned, never assumed (wizard.md:26-28).
- why_it_drives_stars: the cliff between mini-ork's dry run and first real run
  is the credential wall: README:275-277 tells the newcomer to "review
  agents.yaml, authenticate the CLI or configure the providers it names" —
  reading config before first value. Detection converts "go get an API key"
  into "press 1 to reuse the claude CLI you already have." That is the moment a
  curious visitor becomes a user.
- mini_ork_fit: high
- star_impact: high
- effort: M
- risk: a wizard that silently picks a dead lane destroys trust; every detected
  route must be probed (mini-ork already has the probe: validate.py Check 5,
  "one cached 1-token probe per configured lane") and a failed probe must fail
  closed with the exact `mini-ork providers configure <family>` line.
- first_step: add `mini_ork/cli/onboard.py` that (1) detects `claude`/`codex`
  CLIs via `shutil.which`, env keys and vault entries via
  `mini_ork.dispatch.providers.provider_environment()`, and reachable
  OpenAI-compatible locals via the existing `openai-chat` kind; (2) probes each
  candidate using validate.py's lane probe; (3) writes the winner via
  `mini-ork providers configure` / agents.yaml; register it in
  `_build_default_registry()`.

### A3 — `mini-ork triage`: sanitized diagnosis bundle handed to a coding agent

- evidence: `docs/cli/triage.md:9-40` (detection order codex→claude→pi→…;
  "The prompt includes the OpenClaw version, platform, Node.js version,
  prioritized Doctor findings with repair hints, and the diagnostics archive
  path… excludes secrets, tokens, raw chat payloads, and raw logs");
  getting-started.md:158-172 ("One command turns the current state of your
  install into a diagnosis you can act on").
- what_openclaw_does: when the install is broken, triage packages a sanitized,
  secret-free diagnosis and hands it to a coding agent already on the machine —
  the tool debugs itself with the visitor's own agent.
- why_it_drives_stars: mini-ork *is* an agent dispatcher; "mini-ork diagnosed
  its own broken setup by dispatching a repair prompt to my own claude CLI" is
  a shareable, on-brand moment. It also rescues the newcomer whose first real
  run died on a stale engine pointer or missing key — exactly the user who
  unstars and leaves. Today that user gets scattered stderr and
  `docs/operator/provider-triage.md`, which is about the drift panel, not them.
- mini_ork_fit: high
- star_impact: high
- effort: M
- risk: leaking secrets in the bundle would be catastrophic for trust; the
  sanitizer must be allowlist-based over the shapes mini-ork already emits
  (`lane_preflight` JSON, `verdict.json`, `execute_stderr`), and the handoff
  must ask before launching anything (reference triage asks; nothing leaves the
  machine without consent).
- first_step: new `mini_ork/cli/triage_cmd.py`: collect version + engine commit
  (`_engine_commit` in main.py), doctor output, last run's
  `lane_preflight`/`verdict.json`/stderr tail from `.mini-ork/runs/<id>/`, emit
  a ≤8 KiB prompt with secrets redacted (reuse the allowlist discipline of
  `mini_ork/dispatch/secrets.py`), then either print the handoff command or
  dispatch one bounded repair turn through mini-ork's own lanes.

### A4 — `mini-ork doctor --fix`: from presence report to guided repair

- evidence: `docs/cli/doctor.md:9-19` ("Health checks and quick fixes for the
  gateway, channels, plugins, skills… `--fix`"; seven doc pages incl.
  "Gateway and service recovery"); contrast mini-ork `_doctor_handler`
  (main.py:2141-2189) which only prints `[OK]`/`[MISSING]`/`[WARN]`.
- what_openclaw_does: doctor not only names what is wrong but, with `--fix`,
  performs the mechanical repairs it knows how to make, each repair a bounded,
  explained step.
- why_it_drives_stars: doctor is the command every stuck visitor runs first;
  a doctor that ends the report with "run `mini-ork doctor --fix` to repair
  these 3 findings" converts a dead end into progress. mini-ork's doctor
  already *knows* the fixes (it prints "run: mini-ork providers configure
  glm", "run `mini-ork migrate`") — it just makes the human retype them.
- mini_ork_fit: high
- star_impact: medium
- effort: S
- risk: repair theatre — a `--fix` that "fixes" a missing API key can only
  prompt for it; each fix must be a real action with a success line, and
  anything not fixable must say so plainly.
- first_step: extend `_doctor_handler` with `--fix`: for each WARN it already
  emits, run the remediation it already names (interactive
  `providers configure <family>` for unset keys; `mini-ork migrate` when
  `_db_engine_version` lags; `mini-ork install --force` when the managed
  command is stale), printing one line per repair.

### A5 — "First 60 seconds" ladder + first-run FAQ

- evidence: `docs/help/troubleshooting.md:11-40` — "## First 60 seconds": an
  ordered ladder (`openclaw triage` → `status` → `gateway probe` → `doctor` →
  …) with "Good output, one line each"; `docs/help/faq-first-run.md` — a
  dedicated first-run FAQ with anchors like "I am stuck" and "Installer
  stuck?".
- what_openclaw_does: failure states are pre-written as a symptom-first,
  ordered command ladder with expected-good output, so the stuck user climbs
  out without asking anyone.
- why_it_drives_stars: the questions mini-ork's first-run path generates are
  predictable and finite (venv/PATH confusion after install, dry run left no
  state.db row, `claude` CLI missing, key unset, empty `ui/` submodule,
  engine-pointer drift — all visible in the receipts above). Writing the ladder
  once turns the most common churn moment into a self-serve exit; error
  messages and the README can point at its anchors.
- mini_ork_fit: high
- star_impact: medium
- effort: S
- risk: docs nobody finds; each ladder entry must be linked from the exact
  error message that produces that symptom (mini-ork's `Fix:` hint convention
  in validate.py is the natural hook).
- first_step: add `docs/operator/first-run.md` with the ordered ladder for the
  Start-here path (`mini-ork doctor` → `mini-ork validate` → dry run →
  `providers status` → real run), each step with one line of expected-good
  output; link it from README "Start here" and from the lane_preflight
  `fix:` line in `mini_ork/cli/lane_preflight.py:547-556`.

### A6 — Interrupted-run resume as the default story

- evidence: wizard.md:26-27 ("Interrupted baseline setup resumes on the next
  run"); getting-started.md ("your config stays saved" across Ctrl+C);
  contrast: mini-ork `run --resume` exists (`mini_ork/cli/run_resume.py`,
  `--resume <run_id>` hard-errors on unknown ids, re-dispatches only the
  redispatch set) but the Start-here path (README:256-281) never tells a
  newcomer what happens after Ctrl+C on a real run.
- what_openclaw_does: interruption is a designed state, not an error — the next
  invocation picks up where the wizard stopped, and the docs say so at the
  moment you might panic.
- why_it_drives_stars: a first real run that dies at minute 3 of a 2-5 minute
  hello-world (examples/01-hello-world/README.md:24) is the classic
  rage-quit moment; one sentence — "interrupted runs continue with
  `mini-ork run --resume <id>`" — at the exact stall keeps the visitor in the
  funnel, and mini-ork's resume machinery is already built and strict.
- mini_ork_fit: medium
- star_impact: medium
- effort: S
- risk: promising resume for states the engine cannot actually resume; scope
  the claim to what `run_resume.py` supports today and say so.
- first_step: after any interrupted run, print a banner naming the run id and
  the exact `--resume` command; add the same sentence to README's first-workflow
  section. Code touchpoint: the run lifecycle exit path in
  `mini_ork/cli/main.py` / `run_resume.py` where the run id is already known.

### A7 — Verified install tail: version stamp + post-install self-check

- evidence: `docs/install/index.md:221-233` ("Verify the install":
  `openclaw --version` → `openclaw doctor` → `openclaw gateway status`);
  installer.md:157 (post-install "starts onboarding before doctor or gateway
  checks. With `--no-onboard` or no TTY, it prints the command to finish setup
  later"); `--verify` flag checks "the installed version and… gateway health".
- what_openclaw_does: the installer's last act is verification, and the version
  command is the first diagnostic — the visitor never wonders "did it work?"
- why_it_drives_stars: mini-ork's install currently ends with "✓ MiniOrk full
  runtime installed" plus one providers hint (full_install.py:148-149), and
  `mini-ork version` prints a static string (main.py:2194-2197) that disagrees
  with the help header ("v0.1") and the example doc ("0.1.0") — a small
  honesty crack on the very first command a newcomer runs. A version line that
  proves the install (engine commit, venv python, doctor summary) converts
  "did it work?" into "it works."
- mini_ork_fit: high
- star_impact: medium
- effort: S
- risk: a heavyweight version command that fails when offline or when providers
  are unset; keep it local-only and always succeed, with doctor gated behind a
  flag or printed as a next-step pointer.
- first_step: (1) make `_version_handler` print version + engine commit +
  venv python path (all local facts it already computes for doctor); (2) end
  `scripts/full_install.py` by running `mini-ork doctor` and printing
  "first success in 60 seconds: `mini-ork demo`" (A1) — mirroring the
  reference's `--no-onboard`-style opt-out for non-TTY.

### A8 — Key-free first real run: local OpenAI-compatible lane as onboard fallback

- evidence: wizard.md:117-118 (detection includes "already installed
  tool-capable models from reachable Ollama or LM Studio servers on the Gateway
  host. This read-only pass never downloads a model"); FAQ-first-run covers
  "Is a local model OK for casual chats?"; mini-ork already ships the
  plumbing — `mini_ork/dispatch/openai_chat_transport.py:1-10` explicitly
  supports "OpenRouter, Groq, Together, local vLLM, etc." via
  `/chat/completions`.
- what_openclaw_does: the visitor without any API key is still a first-class
  onboarding path — the wizard detects a local model server and verifies a
  real completion against it, so first value needs zero credentials.
- why_it_drives_stars: mini-ork's only key-free path today is the dry run,
  which records no task_runs row (examples/00-demo.sh:73) — so a visitor
  without a key can never see a *real* verified run, only a rehearsal. A
  "no key? use localhost:11434" fallback in onboarding (A2) lets the first
  real loop — plan, dispatch, verdict.json — complete with what the visitor
  already has. That is the "demo path that does not require the hardest
  prerequisite."
- mini_ork_fit: medium
- star_impact: high
- effort: M
- risk: small local models may fail mini-ork's verifier gates, turning the
  key-free path into a fail-heavy experience; onboard must probe the local
  server and set expectations (or route only the cheap lanes locally) rather
  than promising parity.
- first_step: in `mini_ork/cli/providers.py` (or the A2 onboard module), add a
  `local` lane kind that builds on the existing `openai-chat` provider kind
  with `base_url=http://localhost:11434/v1`, probed during onboarding; document
  it as the zero-credential default in README "Start here".

### A9 — Windows one-line installer (install.ps1)

- evidence: README.md:33-35 (`iwr -useb https://openclaw.ai/install.ps1 | iex`);
  package.json ships `scripts/install.ps1`; installer.md:505-541 documents the
  PowerShell flow. Contrast: mini-ork README:235-241 — five manual winget
  commands then `py -3 .\scripts\full_install.py`.
- what_openclaw_does: the Windows path is the same shape as the Unix path —
  one pasted line that provisions prerequisites and the package.
- why_it_drives_stars: every Windows-native visitor hits the six-step manual
  prerequisite wall at minute one; that is a population-sized activation leak,
  and the fix is a script that wraps exactly the steps the README currently
  asks the human to do by hand.
- mini_ork_fit: medium
- star_impact: medium
- effort: M
- risk: maintaining PowerShell provisioning across Windows configurations is
  real surface; keep it to invoking the documented winget list + existing
  `full_install.py` and fail closed with the manual list when winget is
  unavailable.
- first_step: add `install.ps1` at repo root that runs the exact winget package
  list from README:236-238 (idempotent, skipping installed ones) and then
  `py -3 scripts\full_install.py`; replace the README winget block with the
  one-liner.

### A10 — Browser "success view" (`mini-ork dashboard`-equivalent)

- evidence: README.md:62-66 ("openclaw dashboard … opens the Control UI in
  your browser. If it loads, everything is working"); getting-started.md
  step "Open the dashboard". Contrast: mini-ork docs/UI.md:1-20 — the IDE is a
  Rust/Zed fork in a submodule that "cannot be built from this repo", and the
  web frontend was deliberately removed ("the old React SPA … has been
  removed").
- what_openclaw_does: the final onboarding step is a visual, undeniable proof
  of life — a page in the browser that is the assistant working.
- why_it_drives_stars: visual proof is the strongest share trigger
  (screenshot-to-social); terminal verdict lines do not screenshot as well.
- mini_ork_fit: low
- star_impact: medium
- effort: L
- risk: this recommendation collides with a deliberate architecture decision
  (web UI removed in favor of the native IDE); rebuilding a dashboard would be
  theatre against the project's own direction. Included for completeness — the
  synthesizer should likely reject it on fit grounds, noting `mini-ork serve`
  (observability API, mini_ork/cli/serve.py) already exposes the read surface
  a future view could use.
- first_step: if pursued at all, a static `mini-ork board --web` page over the
  existing `serve.py` read API that renders the last run's verdict — not a
  general dashboard.

## Ranked Shortlist

Ranked by star impact per unit of effort:

1. **A1** — smallest effort, widest funnel: every clone becomes a first success; the demo content already exists, it just isn't a command.
2. **A2** — removes the credential cliff between dry run and first real run; the probe it needs already ships in validate.py.
3. **A3** — on-brand self-diagnosis handed to the visitor's own coding agent; rescues the exact user who would otherwise churn silently.
4. **A7** — S-effort polish of the two commands every visitor runs first (install tail, version); fixes a visible version-honesty crack.
5. **A8** — first *real* verified run without owning any API key; plumbing (openai-chat transport) already exists.
6. **A4** — doctor already names every fix; `--fix` just runs them — small delta on an existing handler.
7. **A5** — cheap, predictable failure ladder with expected-good output; compounds with A1-A4 via error-message links.
8. **A9** — one-line Windows parity; real population, bounded script work.
9. **A6** — mostly messaging around resume machinery that already exists and is strict.
10. **A10** — honest low fit: contradicts the deliberate removal of the web UI; listed last for completeness.

## mini-ork Gap Analysis

First-run path walked as a newcomer would (paths from the target worktree):

1. **Install** — `curl … | sh` (README:212) → `install.sh:92` execs
   `scripts/full_install.py`, which ends "✓ MiniOrk full runtime installed" +
   "Configure provider lanes with: mini-ork providers status <lane>"
   (full_install.py:148-149). **Gap (A7):** no verification, no auto-onboard,
   no pointer to any first-success command — the reference installer "starts
   onboarding before doctor" (installer.md:157).
2. **First command** — `mini-ork version` prints a static "0.9.0" string
   (main.py:2194-2197) that contradicts `_HELP` ("v0.1") and
   `examples/01-hello-world/README.md:11` ("0.1.0"). **Gap (A7):** version is
   not a diagnostic; the drift is a first-impression honesty crack.
3. **First workflow** — README:256-268: six manual steps. **First stall
   (A1):** the newcomer must invent the scaffolding; and if they follow the
   example's own README instead, `examples/01-hello-world/README.md:21`
   (`cp ~/ps/mini-ork/...`) fails on any machine but the author's. The
   alternative demo, `examples/00-demo.sh`, exists but is a bash script the
   README's Start-here section never mentions.
4. **After the dry run** — README:270-272 says to inspect `.mini-ork/runs/`
   and `.mini-ork/state.db`; but dry-run mode writes no task_runs row
   (00-demo.sh:73), so the DB looks empty and "did it work?" is unanswered.
   **Gap (A1/A8):** no verified-success moment; the reference's is a real
   completion (wizard.md:124, onboarding-overview.md:36).
5. **First real run** — README:275-277: "review .mini-ork/config/agents.yaml,
   authenticate the CLI or configure the providers it names." **Second stall
   (A2):** the credential cliff. mini-ork today: `doctor` shows presence-only
   WARNs with a configure hint (main.py:2141-2189); `providers status/configure`
   exists (providers.py:29-32); `validate` Check 5 probes configured lanes
   (validate.py:30-35). **Missing:** detection of what the visitor already has
   (CLIs on PATH, env, vault, local servers) and a guided, probe-verified
   sequence choosing it — OpenClaw's whole quick-start (wizard.md:115-126).
6. **When it breaks** — lane_preflight prints per-lane reasons and a `fix:`
   line (lane_preflight.py:532-556) — good. But there is no bundle, no
   doctor-repair, no agent handoff, and `docs/operator/provider-triage.md` is
   about the drift panel, not the first-run user. **Gap (A3/A4/A5).**
7. **When it is interrupted** — `run --resume` exists and is strict
   (run_resume.py:131-181), but nothing in the first-run path tells the
   newcomer it exists. **Gap (A6).**
8. **Wanting to see it** — no visual proof of life: the IDE is a Rust fork
   that cannot be built from this repo (docs/UI.md:1-20); `serve.py` is an
   observability API without a UI. **Gap (A10, low fit by design).**

Per-candidate summary (today / missing):

- **A1** — today: `examples/00-demo.sh` (offline, complete, undocumented in
  Start-here). Missing: a `demo` subcommand registered in
  `_build_default_registry()` and surfaced at the install tail.
- **A2** — today: doctor's provider preflight, `providers configure`,
  validate's lane probe, fail-closed secrets (secrets.py). Missing: detection
  wizard + real-completion verification ordering them.
- **A3** — today: doctor, lane_preflight JSON payloads, per-run stderr
  artifacts. Missing: sanitized bundling + agent handoff (`triage`).
- **A4** — today: `_doctor_handler` names fixes inline. Missing: `--fix` that
  executes them.
- **A5** — today: `Fix:` hint convention (validate.py), operator docs.
  Missing: symptom-first first-run ladder with expected-good output, linked
  from errors.
- **A6** — today: `run --resume`, `resume`, `recover`, `rollback`. Missing:
  interruption as a documented, banner-level state in the first-run path.
- **A7** — today: version string + install success line. Missing: verified
  install tail (engine commit, doctor summary, demo pointer) and version
  consistency.
- **A8** — today: `openai-chat` provider kind supporting local vLLM-style
  endpoints (openai_chat_transport.py:1-10). Missing: a key-free default lane
  offered at onboarding so the first real run needs no credential.
- **A9** — today: documented manual winget list (README:235-241). Missing:
  `install.ps1` one-liner.
- **A10** — today: `serve.py` read API, `board` for the IDE. Missing: any
  human-visible success view — and the project deliberately removed the web
  UI, so this is a direction question, not a gap to fill blindly.

## Open Questions

- Would the maintainers accept an interactive wizard in the CLI (A2), given
  mini-ork's operator-first, no-daemon minimalism — or should onboarding be a
  non-interactive `mini-ork onboard --check` that prints a plan? A negative
  answer demotes A2 and promotes A4/A5.
- Is a PyPI publish planned? `pipx run mini-ork demo` would be the true npx
  equivalent and would change A1's shape (no clone at all before first value).
- Will the dry-run's no-task_runs-row behavior change? If dry runs record
  state, A1's "verified success" story strengthens and A8's urgency drops.
- What share of the target audience is on native Windows vs WSL (drives A9's
  rank), and is there CI capacity to keep a PowerShell installer honest?
- For A8, can mini-ork's verifier gates pass on typical local models, or does
  the key-free lane need a special lightweight recipe so the first real run
  doesn't fail its own gates?
- Does the panel's cost-governance story (a README pillar) interact with A8 —
  i.e., is "first run free because it's local" a message the project wants to
  send?
