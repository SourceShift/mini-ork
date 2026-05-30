# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 0.1.x (latest) | Yes |
| < 0.1.0 | No |

## Reporting a Vulnerability

Do not open a public GitHub issue for security vulnerabilities.

Email the maintainers at: **security@ork-ai.dev** (placeholder — replace with real address before publishing).

Include:
- Description of the vulnerability and potential impact
- Steps to reproduce
- Any suggested mitigations

You will receive an acknowledgment within 72 hours. We aim to release a fix within 14 days of a confirmed report.

## Security Guidelines for Users

**Do not put secrets in kickoff files.**
`kickoff.md` is passed verbatim to LLM APIs. Treat it as public. Never include API keys, tokens, passwords, or internal hostnames.

**`.env` and `config.env` are gitignored by default.**
`mini-ork init` writes a `.gitignore` that excludes `.mini-ork/secrets/`, `.env`, and `.env.local`. Verify this is in place before committing if you added mini-ork to an existing repo.

**state.db contains run history.**
`state.db` (default: `.mini-ork/state.db`) may contain snippets of source code, prompt text, and model responses from your runs. Do not commit it. It is gitignored by default.

**INBOX/ escalation files.**
`.mini-ork/INBOX/` contains structured summaries of failed epics including code diffs and reviewer feedback. Do not commit or publish this directory.

**Hook scripts run with your shell permissions.**
Hook scripts configured in `agents.yaml` are executed by mini-ork with the same permissions as your shell. Audit hook scripts before running them, especially if sourced from a shared template.

**Model API keys.**
Store keys in `.mini-ork/config.env` or `.mini-ork/secrets/`. Never pass them as positional arguments (they appear in `ps` output). Both directories are gitignored.
