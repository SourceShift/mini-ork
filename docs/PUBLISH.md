# Publishing a run

`mini-ork publish` turns a finished run into a public, redacted page at
`https://console.miniork.dev/r/<slug>`. The link *is* the authorization: there
is no login, no expiry, and the slug is 22 random characters — so treat the URL
like the run's content. Revoke from the console when a run should stop being
readable.

```bash
mini-ork publish run-1795604701-80144
#   published: https://console.miniork.dev/r/7Q2f…

mini-ork publish run-1795604701-80144 --dry-run --out /tmp/bundle.json
mini-ork publish run-1795604701-80144 --no-transcripts      # summaries only
mini-ork publish ./some/run/dir --api-url http://127.0.0.1:7823/api/v1
```

The API key comes from `ATTESTIA_API_KEY` (or `--api-key-env VAR`) or stdin
(`--api-key-stdin`). There is deliberately no `--api-key` flag: an argument
shows up in shell history and in `ps`.

## What is published

| Section | Source | Notes |
|---|---|---|
| `run` | `run_profile.json`, `plan.json`, `run-verdict.json` | title, objective, verdict, levels, repo base sha, cost |
| `kickoff` | `kickoff.md` | excerpt only (3 000 chars) |
| `dag` | `plan.json` decomposition | nodes, `depends_on` edges, per-node status and artifacts |
| `verification` | `verification_manifest.json`, `verifier_*.json` | instruments + per-check pass/fail |
| `agents` | `agent-*.live.jsonl` | per-agent summary: model, turns, tool calls, cost |
| transcripts | the same `agent-*.live.jsonl` files | full conversation, fetched per agent |

Transcripts travel *beside* the bundle (`{bundle, transcripts}`) and are served
one agent at a time, so the page stays light however long the run was. Caps:
4 000 chars per message, 2 MB per agent, 6 MB per run — truncation is marked,
never silent.

## Redaction — two passes, on purpose

`mini_ork/publish/redact.py` replaces what it recognises (provider keys,
GitHub/Slack/AWS/Google tokens, JWTs, private-key blocks, `Authorization:`
headers, credentials in URLs, emails) and rewrites absolute paths to `~/…` /
`<volume>/…`. Dict keys that name a credential (`api_key`, `password`,
`authorization`, …) keep the key and lose the value.

Then the *same* rules run again as a **scan** over every string in the finished
bundle. A match means the scrubber missed something, and the command refuses:

```
publish: refused — residual secrets after redaction in: env_secret
exit 4
```

The control plane re-runs the scan server-side (`server/publicruns/guard.ts`,
same rule names) and rejects the upload too. The producer is not trusted to
grade its own homework.

The bundle carries `redaction.counts` so a reader can see the page was filtered,
not spotless.

## Exit codes

| code | meaning |
|---:|---|
| 0 | published, or a clean `--dry-run` |
| 2 | usage error / run directory not found |
| 3 | the bundle could not be built |
| 4 | residual secret after redaction — upload refused |
| 5 | the control plane rejected the upload (auth, size, digest, guard) |

## Two tiers

- **Simple (default)** — the command posts straight to
  `https://console.miniork.dev/api/public/runs` with an API key in the
  environment. Nothing to stand up; works from a laptop.
- **Secure** — point `--api-url` (or `MINI_ORK_PUBLISH_URL`) at your own
  control plane and hand the key to the process from a secret store
  (`--api-key-stdin` keeps it out of the environment and off the disk). The
  key is required either way: a missing key is an error, never an empty string
  sent anonymously.

## Bundle contract

`SCHEMA = "mini-ork.public-run/v1"`. `mini_ork/publish/bundle.py` owns it; the
consumer's validator is `server/publicruns/schema.ts` on the attestia side. A
change to one is a change to the other — bump the schema name when a field is
removed or changes meaning.
