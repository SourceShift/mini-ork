# mini-ork security state — what ships, what is open

Snapshot written 2026-11-13 for the 2026 security-technique review. Reviewers
score every technique against THIS file. Keep it current: an entry that no
longer exists makes the review rank work that is already done.

## Already shipped

| Surface | What exists | Where |
|---|---|---|
| **Secret scanning** | `gitleaks` with a project config, two runs: `make secrets` on the working tree, and a pinned, checksum-verified `secret-scan` CI job that is a required dependency of `ci result`. The local target fails closed when gitleaks is absent. Findings print with `--redact`. | `docs/operator/secret-scan.md`, `.gitleaks.toml`, `.github/workflows/ci.yml` |
| **Merge risk tiers** | Path classes (`.github/workflows/**`, `.githooks/**`, `db/migrations/**`, vault and secrets modules, the risk gate itself) refuse to merge without an approval recorded against the head patch id. The policy is read from `origin/main`, so a branch cannot loosen its own check. | `config/risk-tiers.yaml`, `mini_ork/gates/merge_risk.py`, `mini_ork/gates/merge_approval.py`, `mini_ork/cli/approvals_cmd.py` |
| **Secrets vault, two tiers** | Simple tier: local file or env, failing closed on a missing value. Secure tier: a real secret store with per-node scoping, nothing in logs or shell history. One config chooses between them. | `mini_ork/dispatch/vault.py`, `mini_ork/dispatch/vault_write.py`, `mini_ork/dispatch/secrets.py`, `mini_ork/cli/vault_cmd.py`, `mini_ork/remote/secrets_scope.py`, `config/vault.example.yaml` |
| **Sandboxed placement** | A run may execute on the host, in a container, or on a remote node provider behind one node-agent protocol (`--placement remote --env <profile>`). Per-node sandboxes are documented. | `docs/architecture/cloud-sandbox-providers.md`, `docs/architecture/per-node-sandboxes.md` |
| **Write-scope claims** | A worktree registers the file surface it owns (CAID registry); a second worktree whose claim overlaps a live one is refused, so concurrent agents cannot race a file. A `reference-transaction` hook blocks direct feature-branch creation. | `scripts/mini_ork_worktree.py`, `.githooks/reference-transaction` |
| **Repo integrity guard** | Frozen kernel paths cannot be edited by a self-improvement run without the documented mechanism; the guard heals a rebase rather than silently allowing it. | `mini_ork/gates/` (`repo_integrity_guard`), `config/kernel-frozen.yaml` |
| **Gate-hackability scan** | `gaming_scan` looks for patches that satisfy a gate without doing the work. | `mini_ork/verify/gaming_scan.py` |
| **Verification before publish** | A code-fix run publishes only on a verifier verdict; an abstention withholds publish instead of greening it. Verdict trichotomy PROVEN / REFUTED / UNVERIFIED. | `mini_ork/cli/publisher.py`, `mini_ork/verify/levels.py`, `docs/architecture/verification-stack.md` |
| **Evidence ledger and traces** | Per-run transcripts plus an evidence ledger; node output is written to the run dir and cited by path. | `mini_ork/verify/evidence_ledger.py`, run dirs under `.mini-ork/runs/` |
| **Kickoff guard** | A kickoff must carry numbered Done-When criteria before a run starts. | `mini_ork/verify/kickoff_guard.py`, `mini_ork validate` |
| **Artifact contracts** | Declared ports and visibility limits between nodes; a node consumes only what its contract lists. | `schemas/artifact_contract.schema.json`, `docs/architecture/artifact-graph.md` |

## Open — no mechanism ships

| Gap | Evidence it is open |
|---|---|
| **No security check in the code-fix verifier set.** The verifiers are `test`, `typecheck`, `fallback_guard`, `metamorphic`. None looks for injection, authz defects, unsafe deserialization, path traversal, hardcoded credentials that gitleaks' patterns miss, or a dependency pulled from an attacker-chosen source. | `recipes/code-fix/verifiers/` |
| **Secret scanning is pattern-and-entropy only.** It does not find a secret that was already pushed in a form gitleaks' rules miss, and it says nothing about non-credential leakage (internal hostnames, customer data in fixtures). | `docs/operator/secret-scan.md` |
| **The agent's inputs are trusted.** A coding agent reads repository files, issue text, tool output, and web or MCP results with no injection probe between the content and the instruction. There is no red-team term that plants an instruction and checks whether the run follows it. | no verifier references an injection probe |
| **Correctness is measured; exploitability is not.** The oracle scores whether a fix works, never whether it opens a hole. Nothing replays an exploit against the patched tree. | `docs/architecture/verification-stack.md` |
| **Tool surface is broad.** Nodes get bash and file tools; there is no per-run measured allowlist, and no test asserts that a node could not reach outside the workspace. | `recipes/*/workflow.yaml`, sandbox docs (placement is configurable, not enforced per node) |
| **Supply chain within a run.** A run may install dependencies; nothing hashes or verifies what it resolved. | `recipes/`, `scripts/` |

## Doctrine that constrains every proposal

- Fail closed: a missing value is an error that names the key, never a silent empty string.
- A guard that cannot measure blocks; it never reports "fine" it did not observe.
- Abstention is never a pass.
- Deterministic checks decide; a model may propose or veto, never approve.
- No technique may be adopted that makes the pipeline depend on a security model's opinion as its accept/reject rule.
