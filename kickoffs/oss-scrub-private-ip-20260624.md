# OSS-scrub private LibWit IP/PII before public-main merge

## Goal
Make the tracked tree OSS-safe to merge to the public `main` by removing all
private LibWit infrastructure references, internal host/IP addresses, private
product source paths, and personal email. After this scrub the repo can be
squash-merged to public `main` without leaking private material.

## Root cause (verified against source)
An earlier scrub (commits cfaae17, 7306faf) was incomplete. `git ls-files | xargs grep`
confirms private content still lives at HEAD in six tracked files:
- private host `REDACTED-INTERNAL-HOST` + internal IP `REDACTED-INTERNAL-IP` (Tailscale/k3s/Loki/Prometheus/Grafana/Tempo)
- LibWit product infra paths (`infra/charts/REDACTED/...`, `src/components/REDACTED/...`, `REDACTED-PROJECT`)
- maintainer personal email (`redacted@example.com`)

## Scope Hint
Touch ONLY these six files. Do NOT search for or modify any other file. Do NOT
touch behavioral code, lib/*, bin/*, or recipe workflow/contract YAML.
- `CHANGELOG.md`
- `docs/refactor/synthesis-latest.md`
- `recipes/db-migration/example-kickoff.md`
- `recipes/ops-runbook/example-kickoff.md`
- `docs/RSP.md`
- `kickoffs/researcher-reader-ask-lang-envelope-audit-20260624.md`

## Expected Edit
1. **DELETE** `docs/refactor/synthesis-latest.md` — it is a LibWit-product
   refactor synthesis (Gemini billing-key re-seal, SealedSecret, libwit reader
   internals), not mini-ork material. `git rm` it.
2. **DELETE** `kickoffs/researcher-reader-ask-lang-envelope-audit-20260624.md` —
   LibWit Reader product audit referencing `src/components/REDACTED/...`. `git rm` it.
3. **REDACT** `recipes/db-migration/example-kickoff.md` — replace
   `prod (REDACTED-INTERNAL-HOST, accessed via Tailscale `REDACTED-INTERNAL-IP:5932`)` with a neutral
   placeholder, e.g. `prod (<prod-host>, accessed via VPN <prod-host>:5432)`.
4. **REDACT** `recipes/ops-runbook/example-kickoff.md` — replace every
   `REDACTED-INTERNAL-HOST` and `REDACTED-INTERNAL-IP` (k3s host, Loki/Prometheus/Grafana/Tempo URLs)
   with neutral placeholders, e.g. host `<prod-host>` and URLs
   `http://<prod-host>:13101` etc. Keep the example structurally valid.
5. **REDACT** `docs/RSP.md` — replace maintainer line email
   `redacted@example.com` with the existing project security alias
   `security@ork-ai.dev` (used in SECURITY.md). Keep the maintainer's name.
6. **REDACT** `CHANGELOG.md` — the changelog note that prints the literal
   leak set `(`the host application`, `REDACTED-INTERNAL-HOST`, `REDACTED-INTERNAL-IP`, etc.)` must
   not print the literal host/IP; reword to `(private hostnames, internal IPs,
   host-application refs, etc.)`.

## Requirements
- ≤ ~25 changed lines across the four redacted files plus two deletions.
- No new dependencies. No behavioral-code edits. Placeholders must keep the
  example kickoffs valid markdown.
- Do not invent new infra details; use generic placeholders only.

## Done When
- `git grep -nE 'REDACTED-INTERNAL-HOST|100\.74\.239\.22|REDACTED-PROJECT|infra/charts/REDACTED|src/components/REDACTED|khakshour\.amir@gmail'`
  over tracked files returns **zero** matches.
- The two deleted files are removed from the index (`git status` shows them deleted).
- `verdict.json` written with pass:true + the zero-match grep proof.
