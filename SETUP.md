# SETUP — for the coding agent, not the human

You are setting up mini-ork on someone's machine. This file is the whole
protocol. Follow it rather than improvising: every rule below exists because the
alternative cost somebody an hour.

**Read this file and [`docs/WHY.md`](docs/WHY.md). Nothing else, yet.** Not the
README, not `docs/`. They are written for a person browsing a project, and
reading them first is how a setup turns into a research project.

## Your attention budget

Setup may cost the operator **at most six questions and about five minutes.**

| Rule | Limit |
|---|---|
| Questions | ≤ 6, **one per message** |
| Length per question | ≤ 300 words, including your recommended answer |
| Plans | exactly 1, after all answers are collected |
| Confirmations | exactly 1, before you execute |
| Secrets in chat | **zero.** Never. See below |

Before asking anything, run the two commands that answer most of it for you:

```bash
mini-ork onboard --dry-run   # what is already installed and what it would do
mini-ork doctor --json       # verdict: ready / needs_person / restartable
```

`mini-ork doctor` exits `0` ready, `2` a person is needed, `1` a restart would
fix it, and always ends with one `Next: <command>` line. **If `doctor --json` or
`mini-ork onboard --dry-run` already answers a question, do not ask it.** Ask
only what neither can know.

## The questions

Ask in this order. Stop as soon as the remaining ones are already answered.

1. **Where should the workspace live?** — recommended: the current directory,
   or the project they just mentioned. `mini-ork onboard --workspace <dir>`.
2. **Which model provider do you have an account with?** — recommended: the one
   whose credential they already have on this machine. Do not ask which "lane"
   they want; lane names are our vocabulary, not theirs. Map their answer to one
   of the configured lanes.
3. **Simple tier or secure tier?** — recommended: simple (a file under
   `.mini-ork/`, zero setup) if this is a laptop and a trial; secure (a real
   secret store, `config/vault.yaml`) if the machine is shared, audited, or will
   run unattended. Whichever they say, `mini-ork onboard --secure` is the only
   difference in what you type.
4. **Install the two optional satellites?** — ContextNest (local memory, Rust
   build) and Zed integration (editor wiring). Recommended: no for a first run.
   `mini-ork onboard --with contextnest,zed` adds them later at any time, so
   "later" is a complete answer.

Never ask a fifth question about a preference this file already recommends. If
the operator is silent on something, take the recommendation and say so in the
plan.

## Secrets: the one rule you cannot break

**Never ask for an API key, token, or password in this conversation, and never
put one in a command you run.** It lands in a transcript that is stored, and
that is the end of the secret.

Instead, when a credential is missing, send **one message** containing the exact
commands for the operator to paste into **their own terminal**. It is the only
thing in this protocol you hand over rather than execute:

```bash
mini-ork providers configure <lane>    # hidden prompt; the key never touches argv or history
mini-ork doctor                        # confirm: verdict should read `ready`
```

`mini-ork providers configure` reads from a terminal; it will not work from a
tool call, which is exactly why the operator runs it. It also accepts
`--from-stdin`, and that is not an escape hatch: a key piped through you lands in
the transcript too. On a shared machine, the secure tier has the same command
write into the configured vault backend instead of
`.mini-ork/config/secrets.local.sh` — point at `mini-ork vault status` to show
which backend will receive it.

## Plan, confirm, execute

Once the answers are in, send **one** message: what you will install, where the
workspace goes, which lane will serve which role, roughly how long it takes, and
what it will cost (setup itself costs **nothing** — `onboard` never calls a
model). Ask for confirmation once. Then execute:

```bash
mini-ork onboard --workspace <dir> [--secure] [--with contextnest,zed]
mini-ork doctor
```

`onboard` is idempotent and never spends money. If it reports a missing
prerequisite, it prints the command that fixes it — run that, not something
invented. `-y` accepts every default and is the right choice for CI.

Then prove the wiring **before** spending anything on a model:

```bash
# The example kickoff ships with the install (installer: ~/.local/share/mini-ork)
# or the clone (./examples). Use whichever exists.
cp <mini-ork>/examples/01-hello-world/kickoff.md ./kickoff.md
mini-ork validate --recipe code-fix          # static checks, each with a Fix: hint
MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md   # no model call, no spend
```

Only when the dry run is clean should the operator start a real run, and that is
their decision, not yours: `mini-ork run code-fix ./kickoff.md`.

## Handover

Finish with three rules and one command.

1. **State lives in `.mini-ork/`** — `state.db` (run history and what the system
   learned never leaves this machine), `runs/` (every run's evidence), and
   `config/`. Back up `state.db`; it is not reproducible from git.
2. **`mini-ork doctor` is the first thing to run when anything looks wrong.** It
   ends with the one next command, and its exit code is a real verdict.
3. **`mini-ork inbox` answers "what needs me?"** — one line per decision waiting
   on a person, already ordered by urgency. If it is empty, nothing needs them.

Your first command: `mini-ork doctor`
