# Audit: is mini-ork ready to be public OSS?

## Goal
Produce an OSS-readiness verdict for the mini-ork repo before it is pushed to the
public remote `github.com:SourceShift/mini-ork`. The audit is READ-ONLY analysis;
it must not edit source. Output a single findings note with a clear
GO / NO-GO verdict and a list of any blockers.

## What to check (each must get a pass/fail with evidence)
1. **License**: `LICENSE` exists, is a recognized OSS license, and the README/headers
   don't contradict it.
2. **No committed secrets**: scan the tree and the diff `main...HEAD` for API keys,
   tokens, private keys, `.env`/secrets files, or credentials. `.mini-ork/config/secrets.local.sh`
   and any `*.local.*` must be gitignored, not committed.
3. **No proprietary / IP leak** (the `never_leak_libwit_ip_into_miniork_oss` rule):
   no libwit/researcher/aidolfin domain content, German-tax-law terms (EStG/UStG/GoBD),
   `filings.libwit.com`, or book-gen/chapter domain specifics in tracked files.
4. **No hardcoded personal absolute paths** (e.g. `/Users/<name>/…`,
   `/Volumes/docker-ssd/…`) baked into committed code that would break for others.
5. **README adequacy**: states what the project is, how to install/run, and license.
6. **.gitignore hygiene**: covers `.mini-ork/state.db`, secrets, run artifacts,
   caches.
7. **Dependency license sanity**: no obviously GPL-incompatible vendored code if the
   project license is permissive.

## Scope
- IN: read-only inspection of tracked files, `.gitignore`, `LICENSE`, `README.md`,
  and the `git diff main...HEAD` surface.
- OUT: no source edits, no pushing, no release. This audit only reports.

## Proof command
```
test -f .mini-ork/research-notes/oss-readiness-findings.md && \
  grep -qiE 'verdict:\s*(GO|NO-GO)' .mini-ork/research-notes/oss-readiness-findings.md && \
  grep -qiE 'license|secret|ip|leak' .mini-ork/research-notes/oss-readiness-findings.md
```

## Done when
`.mini-ork/research-notes/oss-readiness-findings.md` exists with a GO/NO-GO verdict,
a per-check pass/fail table, and an explicit blocker list (empty if GO).
