# mini-ork Cloud — full architecture for SaaS deployment on the customer's AWS account

`/mini-ork research-synthesis` — heterogeneous 4-lens research + synthesis.

## Goal

Design the complete architecture for **mini-ork Cloud**: a product where a
customer stands mini-ork up in *their own* AWS account, the cloud bill lands on
their account, and mini-ork earns a commission. Starting point is static
node-tier pricing; the design must leave room for metered billing later.

The document must answer, explicitly and with diagrams:

1. **What "mini-ork on AWS" is** — the reference topology (VPC layout, compute
   for the control plane, where run sandboxes execute, storage, egress, secret
   custody), and why that shape rather than EKS-by-default or EC2-by-default.
2. **Who runs what** — the split between mini-ork's hosted control plane and
   the customer-plane that lives in their account, the API contract between
   them, and what the hosted side can and cannot see.
3. **Node pricing** — a static tier table (vCPU/memory/disk per tier, price to
   the customer, commission to mini-ork), how a node registers, how usage is
   metered, and what breaks when tiers become metered.
4. **Lanes and credentials** — whether the customer configures their own model
   lanes, and the safe way to do it: BYO keys vs brokered keys, where keys are
   stored, how they reach a dispatch, rotation, blast radius, and what happens
   on a leaked key. Answer "should the user set its lanes available?" directly.
5. **Trust boundaries and threat model** — at least five concrete threats with
   the control that answers each (customer-supplied code touching mini-ork
   credentials; a compromised node agent; a tenant escaping into another
   tenant; a model provider reading prompts; billing tampering).
6. **Delivery shape** — how this ships in the repo's two tiers (simple local
   trial vs secure cloud deployment), what is built first, and what is
   deliberately deferred.

## Scope

Read these before writing; cite them rather than restating them:

- `docs/architecture/cloud-sandbox-providers.md` — the node-provider protocol
  (static VMs, Daytona, … behind one node-agent contract) and `--placement
  remote --env <profile>`.
- `docs/architecture/artifact-graph.md` — declared ports, run-local manifests,
  what a node may read.
- `docs/architecture/verification-stack.md` — what a run must prove, since the
  cloud product's promise is verified delivery.
- `config/vault.example.yaml` and `mini-ork vault status` — the reference
  two-tier secret store (fail-closed local file vs secret manager).
- `mini_ork/context.py` — the `RunContext` env contract every node sees.
- `mini_ork/cost/budget.py` — the per-run ceiling pattern to reuse for metered
  node spend.
- `mini_ork/dispatch/providers.py` and `config/agents.yaml` — how lanes resolve
  to model providers and why an alias must resolve from the tracked map.
- The hosted console's account model (`users`, `tenant_users`,
  `tenant_subscriptions`, plan tiers, API-key hashing) if visible, as prior art
  for sign-up, plans and quota.

Out of scope: writing code, terraform modules, or pricing pages. This is an
architecture design document with diagrams and decisions.

## Constraints

- **Assume AWS first**, but keep the customer-plane contract cloud-neutral so a
  second provider does not require a redesign.
- **Fail closed.** A missing credential or an unusable price ceiling is a clear
  error, never a silent empty string or "unlimited".
- **The customer's bill is theirs.** Nothing mini-ork runs may charge the
  customer's account outside the agreed tiers without a visible ledger entry.
- **Secrets never sit in plaintext** in a database, a log, or a node's env more
  than the dispatch needs.
- Prefer decisions with a stated alternative that was rejected and why.

## Done When

1. The synthesis is a single markdown document with a one-paragraph executive
   summary followed by numbered sections matching the six Goal items.
2. At least four mermaid diagrams: reference AWS topology, control-plane ↔
   customer-plane sequence, credential flow for a dispatch, and trust
   boundaries.
3. The node pricing section contains a tier table with at least three tiers,
   each with vCPU, memory, disk, customer price, and mini-ork commission, plus
   one paragraph on how metered billing would replace it.
4. The lanes section answers "should the customer set their lanes?" with a
   recommendation, and specifies the credential path from store to dispatch
   including rotation and revocation.
5. The threat model lists at least five threats, each with a named control and
   the residual risk if that control fails.
6. Every claim grounded in this repository cites the file path it came from;
   externally sourced claims name their source.
7. The document ends with an ordered build plan: what ships first, what is
   deferred, and what would falsify the design.
