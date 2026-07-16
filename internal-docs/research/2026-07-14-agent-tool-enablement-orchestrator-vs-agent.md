# Who should control an agent's tools — the agent, or the orchestrator?
*2026-07-14. Evidence: Omnigent (cloned + read) + 14 papers from the libwit corpus. Written against mini-ork's current dispatch, which grants agents an unbounded, ambient tool surface.*

## Verdict

**The orchestrator owns the GRANT. The agent owns the CHOICE within it.**

And the reason this is easy: **it is not a security-vs-capability trade-off.** Narrowing the tool surface per node buys three things at once — smaller blast radius, *higher tool-selection accuracy*, and less context burned on tool schemas. Security and performance point the same way, which almost never happens. There is no tax to pay here.

---

## What mini-ork does today (the problem)

`lib/llm-dispatch.sh` invokes:
```
claude --print --permission-mode bypassPermissions --output-format … "$prompt"
```
No `--allowedTools`. No `--mcp-config`. No `--strict-mcp-config`.

Consequences:
1. **The tool surface is whatever the operator happens to have configured.** On this machine that's jina, perplexity, github, codegraph, arxiv-libwit, langfuse, **Gmail**, Google Drive. Two operators running the same recipe get *different* agents. A verifier-gated, reproducible-run pitch cannot survive that.
2. **Every node holds the full lethal trifecta.** An implementer node right now has (a) private repo data, (b) ingress of untrusted content (web/tool results), and (c) an exfiltration channel (Gmail `create_draft`, plus `Bash` + curl) — *with permissions bypassed*. Indirect prompt injection through a tool result (`2601.04795`) is sufficient to complete the chain.
3. `lib/harness_wrapper.sh` does the opposite and over-corrects: `--allowedTools "Read,Write,Edit,Bash"` → **zero MCP**, so agents on that path are silently blind (no docs, no search, no codegraph). Meanwhile `bin/_worker-launcher.sh:166` *instructs* the agent to "Use Context7 MCP" — which isn't even configured. A dead instruction.

So today the grant is simultaneously too wide (llm-dispatch), too narrow (harness), and unspecified (ambient). All three are the same root cause: **nobody owns the grant.**

---

## Evidence 1 — more tools makes agents *worse* (this is the surprise)

- **`2606.17519` — Scaling Enterprise Agent Routing.** Production catalog of 110 agents / 584 tools. Routing **F1 drops 16–23 percentage points** across three frontier models as the catalog grows from 10 → 110 agents. This is the money number: capability *falls* as the tool surface grows.
- **`2605.24660` — How Many Tools Should an LLM Agent See?** Show too many and the model can't choose; show too few and the right tool is absent. A *fixed* shortlist size is the wrong default — it should be adaptive.
- **`2510.20036` — ToolScope.** Redundant tools with overlapping names/descriptions introduce ambiguity and *reduce selection accuracy*. Merge + context-filter.
- **`2602.05366`, `2511.01854`** — retrieval over the tool catalog is required once it's large; coarse agent-level descriptions obscure fine-grained tool function.

**Implication:** dumping every configured MCP server into every agent isn't generosity, it's sabotage. The agent that can see 9 MCP servers picks worse than the one that can see the 3 it needs.

## Evidence 2 — the agent cannot be trusted to scope itself

- **`2603.28166` — Evaluating Privilege Usage with Real-World Tools:** "granting agents autonomy over tool use also **transfers the associated privileges to the agent and the underlying LLM**… improper privilege usage may lead to information leakage and infrastructure damage."
- **`2504.11703` — Progent** (68 cites): programmable, *orchestrator-side* privilege control is the answer to injection / memory-poisoning / malicious tools.
- **`2512.11147` — MiniScope:** a least-privilege authorization framework for tool-calling agents.
- **`2503.15547` — Prompt Flow Integrity** (42 cites): agent behaviour is set at *runtime* by prompts from the user **or from tool data** — hence privilege escalation.
- **`2601.04795`:** indirect prompt injection lands **via tool results**. Any tool returning untrusted content (jina, perplexity, web fetch) is an injection vector.
- **`2510.02554` — ToolTweak:** tool *providers* can adversarially manipulate selection to get themselves picked. An open MCP marketplace is itself an attack surface.
- **`2606.15008` — Security Engineering of OpenClaw:** attack-surface expansion and trust-boundary violations in exactly our shape — a self-hosted multi-agent system where LLM output executes commands.

**The killer argument:** if the agent decides its own privileges, then whoever controls a tool result controls the privilege decision. An LLM self-restricting is not a security boundary, it's a suggestion. This is the textbook confused deputy. **The grant must live outside the model's influence.**

## Evidence 3 — what Omnigent actually does (cloned, read)

Omnigent wraps the same native harnesses we do (Claude, Cursor, Hermes, opencode) — and makes the opposite choice at every point:

- `omnigent/tools/manager.py` + `base.py` + `mcp.py` — an orchestrator-side tool **registry**, not agent discovery.
- `omnigent/tools/builtins/` vs `omnigent/tools/client_specified/` — tools are **declared by the caller**, not found by the agent.
- `_build_tools(config)` (`claude_native_bridge.py`) — the toolset is **constructed from config**, and the execution environment is provisioned with an explicit `OSEnvSpec(cwd=…, sandbox=OSEnvSandboxSpec(...))`. Environment *and* toolset are orchestrator-provisioned.
- `omnigent/stores/permission_store/` — a **persisted permission store** with `check_access()` and per-session / per-user grants. Permission is a first-class, durable, orchestrator-side object.
- `hermes_native_permissions.py`, `cursor_native_permissions.py`, `opencode_native_client.list_permissions()/reply_permission()` — they **intercept the harness's permission elicitation and answer it from policy**.

That last one is the sharpest contrast. Where mini-ork says `--permission-mode bypassPermissions` (throw the gate away), Omnigent *keeps the gate and answers it programmatically*. Same autonomy, but every decision is policy-driven and logged instead of blanket-waived.

---

## The design for mini-ork

Recipes already declare an **artifact contract** and a **verifier contract** per node. Add a third: a **capability contract**.

```yaml
nodes:
  - id: plan
    tools: [read, codegraph, arxiv, jina]        # research; no write, no bash, no comms
  - id: implement
    tools: [read, write, edit, bash, codegraph, ctx7]   # NO web ingress, NO comms
  - id: research
    tools: [jina, perplexity, arxiv]             # read-only; no write, no bash
  - id: verify
    tools: [read, bash:test]                     # no write, no network
```

**The structural property to aim for: no single node holds all three legs of the lethal trifecta.**
- `implement` has private data + an exfil channel, but **no untrusted-content ingress**.
- `research` has untrusted-content ingress, but **no private data and no exfil channel**.
- Injection in `research` cannot reach the repo; a compromised `implement` has nothing to be injected *by*.

That is the same philosophy as the cross-family review gate — a **structural** guarantee, not a hopeful one — applied to tools instead of reviewers.

### Enforcement mechanics (small change, big payoff)
1. Pass `--mcp-config <node-scoped.json>` **plus `--strict-mcp-config`** so the operator's ambient MCP cannot leak in. This is what makes a run **hermetic and reproducible** — which the verifier-gated pitch *requires* and does not currently have.
2. Pass `--allowedTools` naming both native tools and MCP grants (`mcp__jina` grants a whole server).
3. Persist the granted tool set per node in `state.db`. The run becomes auditable: "this node had exactly these capabilities."
4. Keep `bypassPermissions` **only** inside a narrowed grant — bypassing permission on an unbounded surface is the actual bug, not the bypass itself.
5. Fix the dead line in `bin/_worker-launcher.sh:166` (Context7 MCP isn't configured; the repo uses the `ctx7` CLI).

### What we deliberately do NOT do
Do not have the orchestrator pick *which* tool to call. That's what the model is for, and micromanaging the call is where rigid frameworks (LangGraph-style hardcoded graphs) lose to agents. The orchestrator draws the fence; the agent moves freely inside it.

### If the catalog gets large
Once the tool catalog outgrows a node's contract, build the shortlist by **retrieval** rather than dumping it (`2602.05366`, `2511.01854`), with an **adaptive** shortlist size (`2605.24660`) rather than a fixed k.

---

## One-line answer
**The orchestrator grants; the agent chooses.** Not because safety demands a sacrifice, but because a narrower tool surface is *simultaneously* safer, cheaper, and measurably more accurate (−16–23 F1 pts is what the alternative costs) — and because an agent that can widen its own privileges isn't a boundary an attacker has to cross.

## Sources
Omnigent (github.com/omnigent-ai/omnigent, cloned 2026-07-14) · `2606.17519` · `2605.24660` · `2510.20036` · `2602.05366` · `2511.01854` · `2603.22862` · `2603.28166` · `2504.11703` (Progent) · `2512.11147` (MiniScope) · `2503.15547` · `2601.04795` · `2510.02554` (ToolTweak) · `2606.15008` · `2509.25926`
