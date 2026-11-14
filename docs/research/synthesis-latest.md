# R3 Synthesis: Text Modality, Durable Execution, and the Channel-Agnostic Session

**Run:** run-1794657974-8423 · **Synthesized:** 2026-11-14 · **Cluster:** R3
**Inputs:** `lens-glm.md` (25 web sources), `lens-kimi.md` (15 core + 22 adjacent academic/standards sources), `lens-codex.md` (14 implementations read at pinned SHAs), `lens-opus.md` (6-section narrative, 17 cited sources)
**Consumer:** a staff engineer writing the design chapters and preparing an API-critique round against a real framework's author.

## Citation key

| Lens file | Prefix | What it is |
|---|---|---|
| `lens-glm.md` | `GLM-n` | breadth web sweep, vendor docs and CPaaS documentation |
| `lens-kimi.md` | `Kimi-n` (core), `Kimi-An` (adjacent) | peer-reviewed papers, preprints, standards drafts |
| `lens-codex.md` | `Codex-n` | code read at a pinned SHA, with file and symbol anchors |
| `lens-opus.md` | `Opus-n` | narrative analysis and its cited sources |

Consensus markers: ★ = 2 lenses, ★★ = 3 lenses, ★★★ = all 4. Markers count lenses that make the *claim*, not lenses that merely mention the topic. Where a lens supports a claim only indirectly, the note says so.

---

## 1. TL;DR

- **"Exactly-once" is four claims wearing one name, and the honest design target is at-least-once plus deduplication at an effect boundary you control.** Every lens arrives here independently: by definitional split (GLM, Kimi-7), by impossibility proof (Kimi-8), by code survey (Codex convergent-2: no implementation demonstrates end-to-end exactly-once), by narrative (Opus §2). Confidence: **high**.
- **Text is not the audio modality with a cheaper transport, and no audio-first framework in the survey has a production text-only path.** LiveKit's own runtime only reaches `_text_only` under a simulation mode (Codex-1, `agent_session.py:2427`), Pipecat silently drops `TextFrame` on telephony (Codex-2, `serializers/twilio.py:147`), and OpenAI Realtime admits text output while keeping VAD in the session contract (Codex-4). Confidence: **high**.
- **The text session is message-oriented, not stream-oriented: segment economics, a per-message status lifecycle, and no audio clock.** Buffer to turn boundaries, model the gateway's status enum as your state machine, and treat a "delivered" callback as ambiguous when a logical message spans segments (GLM-3, GLM-4, Opus §4). Confidence: **high** for the shape, **medium** for the specific status semantics (see D5).
- **Durable execution's load-bearing move is recording the model's nondeterminism as an effect rather than replaying it as control flow, and that move is correct but not free.** The cost is a determinism tax on control flow plus replay-compatibility versioning on the artifact an agent team changes most often (Kimi Finding B, Opus §3, Codex-8 `explicit_recovery`). Confidence: **high** that the move is correct, **medium** that replay-based durability is the right default for agent workloads.
- **No lens found a measured latency distribution for delivery receipts, a spec for inbound segment reassembly, a vendor fault-injection kit, or a cross-transport session contract.** These are the four holes the book cannot paper over with a citation (GLM §"What's NOT", Kimi §5, Codex §"could not establish", Opus §5). Confidence: **high** that the gap is real.

---

## 2. Consensus findings

| # | Finding | Mark | n | Lenses |
|---|---|---|---|---|
| C1 | "Exactly-once" names at least three separable guarantees; every vendor claim is scoped and most are about journal completion, not HTTP effects | ★★★ | 4 | GLM dissent-1 · Kimi-7, Kimi-8 · Opus §2 · Codex conv-2 |
| C2 | Text is a second-class modality in audio-first agent frameworks; disabling audio does not produce a correct text session | ★★★ | 4 | GLM-21, GLM-22 · Kimi Finding C · Opus §4 · Codex-1, Codex-2, Codex-4, conv-5 |
| C3 | Inbound delivery is at-least-once and unordered on CPaaS; dedupe on the gateway message id before any model call | ★★★ | 4 | GLM-5, GLM dissent-2 · Kimi-8, Kimi-A12 · Opus §2, §4 · Codex-3, conv-2 |
| C4 | Durable execution works by quarantining nondeterminism at one boundary; the boundary's location differs per product but its existence is universal | ★★★ | 4 | GLM-11, GLM-13 · Kimi Finding B · Opus §2 · Codex conv-3 |
| C5 | Idempotency keys are the deduplication mechanism, are supplied by the application, and are opt-in on every product examined | ★★★ | 4 | GLM-14 · Kimi-11 · Opus rec-3 · Codex-8 |
| C6 | Replay-based durability carries a real, specific cost for agent workloads, and checkpoint-and-rerun is a credible alternative | ★★★ | 4 | GLM-20 · Kimi-15, caveat-1 · Opus §3 · Codex-8 (divergent-1) |
| C7 | Multi-segment messaging makes "delivered" ambiguous and forces turn-level buffering | ★★ | 3 | GLM-3, GLM-4 · Opus §4 · Codex-3 |
| C8 | Session identity is an application-level key, never a connection | ★★ | 3 | Kimi-4 · Opus rec-7 · Codex conv-1 |
| C9 | Hour-scale waits must be durable state, not an occupied process, and channel policy can invalidate a wait that the runtime survives | ★★ | 3 | GLM-14, GLM-6 · Opus §4 · Codex conv-4 |
| C10 | Backpressure and throughput are policy the send API does not report | ★★ | 3 | GLM-8, GLM-9 · Opus rec-7 · Codex-3 |
| C11 | Resume semantics are where frameworks diverge most, and re-firing an already-executed effect is the common failure | ★ | 2 | Kimi-9 · Codex-6 |

### C1 ★★★ Exactly-once is a family, not a guarantee

Four independent routes reach the same conclusion.

Trofimov et al. separate **delivery, consistency and determinism** and show that "exactly-once" as used in stream processing is not formally defined; the bundled term hides three properties with different obligations (Kimi-7). Andreakis proves that when two independent systems must both record a fact and no transaction spans them, **source-only recovery cannot deliver both no-duplicates and no-loss**; any policy keyed on the source side must duplicate in one reachable state or drop in the other (Kimi-8). The code survey finds that "no implementation in this survey demonstrates true end-to-end exactly-once" and that every product ships at-least-once plus an explicit ack (Codex conv-2). The narrative lens states the same thing in the language an API reviewer will use: what vendors sell is at-least-once plus deduplication at a boundary the application controls (Opus §2).

The web lens supplies the concrete divergence that makes the point operational rather than philosophical. Restate says an invocation "runs exactly once regardless of failures," but duplicate requests are deduped **only if you add an idempotency key to the request headers** (GLM-14). Temporal and Azure both say out loud that activities execute at-least-once, which is why they must be idempotent: Microsoft's Durable Task Framework "guarantees that each called activity function will be executed at least once" (GLM-18), and Temporal's Python activities default to **unlimited** retry attempts with exponential backoff (GLM-12). DBOS is exactly-once only where the step's write and the checkpoint commit in the same Postgres transaction, and at-least-once everywhere else (GLM-16).

These claims do not contradict. They describe different boundaries. Restate's guarantee is about journal-mediated **invocation completion**; Microsoft's is about **activity execution**; DBOS's is about **effects that share a database transaction**. A synthesis that says "vendors claim exactly-once" without this split is wrong, and saying it in an API review will get you corrected by the person who wrote the SDK.

### C2 ★★★ No audio-first framework has a production text-only path

This is the finding with the most code behind it, and it is the book's thesis.

The documentation layer says text-only sessions are supported. LiveKit's multimodality page lists text as a capability that enables "text-only sessions or hybrid voice and text interactions," and places turn detection and interruptions in the **speech and audio** row, not the text row (GLM-21). That is a correct description of the capability.

The code says something narrower. `AgentSession` is one object owning STT, VAD, TTS, LLM, turn detection, tool calls and media I/O (`agent_session.py:391`); the text sink, `TextOutput`, has `capture_text` and `flush` and its contract is "text segment plus a segment boundary," not a message id (`io.py:515`, `:529`, `:533`) (Codex-1). `_text_only` returns true **only** when the job is a `SimulationMode.SIMULATION_MODE_TEXT` simulation (`agent_session.py:2427`), and that is the branch that nulls out `vad`, `stt` and `tts` (`agent_activity.py:5304`). `say()` raises `RuntimeError` when there is no TTS and audio output is enabled (`agent_activity.py:1744`, `:1760`). So the runtime's own text path is the transcript of a speech act, and the production path for text-only requires assembling behavior by hand: disable audio I/O, attach a custom `TextOutput`, and accept that the session's control surfaces still assume a speech act.

Pipecat's leak is different and sharper. `TextFrame`'s own docstring lists its consumers as "LLM services, context aggregators, TTS services and more," a list that does not include transports (`frames.py:278`); the output transport dispatches audio, image, mixer and timed frames and never text (`base_output.py:284`, `:296`); and `TwilioFrameSerializer.serialize` returns `None` for anything it cannot represent (`serializers/twilio.py:147`). Text on a phone call vanishes as `None`, not as an exception (Codex-2). The only serializer that maps `TextFrame` to a wire type is protobuf (`serializers/protobuf.py:47`).

OpenAI Realtime is the half-step: `output_modalities` accepts `["text"]` (`realtime_session_create_request.py:93`) while `turn_detection` remains a field of the audio input config (`realtime_audio_config_input_param.py:65`) (Codex-4). The modality is configurable; the turn model is not.

The academic lens explains why this is inevitable rather than lazy. Turn timing is a human-side universal that the audio path inherits for free: across ten languages, turn transitions cluster at short gaps (Kimi-13). And even systems **with** audio mishandle it, interrupting too aggressively and rarely backchannelling (Kimi-A16). The text modality removes the timing problem and substitutes delivery, ordering and duration problems (Kimi Finding C). Copying the audio session contract into text does not produce a cheaper voice product; it produces a bad text product.

### C3 ★★★ Inbound is at-least-once and unordered; dedupe before the model

Twilio documents per-request webhook overrides for timeout, retry count and retry policy (GLM-5), and Event Streams "guarantees at-least-once delivery," can deliver events out of order, and instructs receivers to deduplicate on event id (Opus-12). The default webhook policy is short (connect timeout 5s, read timeout 15s, retry count 1) and Event Streams retries for up to four hours (Opus §4, Opus-18). A slow handler plus a retry yields a duplicate inbound message. That is the channel's **default** behavior.

The code lens corroborates the shape without the numbers: Twilio's Python client has no session object at all, the unit is a `Message` resource, delivery is push-only through a callback, and "the caller must build its own correlation and dedup layer keyed on the message SID" (Codex-3, `message/__init__.py:105`, `:733`). The codex lens explicitly could not establish inbound ordering from the client library, which is itself the point: ordering is not expressible in the client's type surface.

The academic and standards support is the reason this is non-negotiable rather than merely advisable. The Standard Webhooks spec says the `webhook-id` header "is often used as an idempotency key, which lets a consumer ensure that they only process a specific event once," and requires producers to give consumers a way to list failed messages and manually replay them (Kimi-A12). Andreakis's theorem says the gateway's acceptance record and your session record are two independent systems with no transaction spanning them, so the dedup decision has to be anchored on an authoritative sink-side record and **the guarantee has a half-life you choose** (Kimi-8).

Note the asymmetry that matters for design: the inbound side can be made effect-safe with a dedup table. The outbound side cannot, because "did the SMS send" is recorded by the carrier, not by you.

### C4 ★★★ Durable execution is the discipline of putting nondeterminism in exactly one place

Temporal's rule is that workflow code must be deterministic because replay re-executes it, while activities "execute outside the replay path" and may be fully nondeterministic (GLM-11, Opus-6). The Python SDK enforces this with a workflow sandbox that restricts `datetime.now`, `random`, `uuid1/uuid4/uuid7`, threads and I/O at runtime (`_restrictions.py:619-623`, `:722-723`, `:796`) (Codex-7). Temporal's own agent rebuttal is that determinism "applies only to Workflow code at the orchestration layer" (Kimi-5), and its LLM integration page makes the pattern concrete: **one HTTP call per activity attempt**, so the activity retry policy is the only retry in play (GLM-13).

Restate draws the same line with a journal instead of an event history: every side-effecting step plus its result is recorded, replay skips forward rather than re-executing (GLM-14), and `ctx.run` is the wrapper that creates the journal entry (`context.py:482`) (Codex-8). DBOS draws it at `@step` (`_dbos.py:1586`), with the workflow body re-running on recovery (Codex-9, Kimi-3). LangGraph draws it at the checkpoint and resumes by re-executing the node from its start (`types.py:928`) (Codex-6).

Four products, four different lines, one shared requirement: the author must separate repeatable logic from side effects, and the model call belongs on the effect side. Kimi's Finding B states the boundary precisely: this only holds **if the nondeterministic call is an effect, not a decision**. The moment a model output determines a branch that was not recorded, replay diverges.

### C5 ★★★ Idempotency keys are the mechanism, and you supply them

Restate's dedup is opt-in via a request header (GLM-14); the SDK exposes `idempotency_key` on the call and send surface, not the transport (`context.py:292`, `:306`) (Codex-8). The IETF draft fixes the contract: the key MUST be unique, MUST NOT be reused with a different payload, a duplicate retry after completion returns the completed response, and a **concurrent** retry gets a conflict (Kimi-11). The normative status codes are the useful part: 400 when a required header is absent, 422 when a key is reused with a different payload, 409 when a retry arrives while the original is still in flight. The 409 case is the one agents get wrong, because the natural agent behavior on a timeout is to try again.

The narrative lens makes this a design rule rather than a library feature: an idempotency key on every tool call and outbound send, committed with the checkpoint (Opus rec-3). Nothing in any client library invents an application-level key for you (Codex conv-6).

### C6 ★★★ Replay is not free, and checkpoint-and-rerun is a real alternative

The dissent has three forms and they are all documented.

**Cost shape.** Durable engines were tuned for high concurrency and low compute per task, where each step takes milliseconds. An agent spends 45 seconds on a single LLM call, makes 40 calls at $0.05 to $5 each, and runs ten of them rather than a hundred thousand (Opus-13). The bottleneck is token spend, not workflow throughput.

**Determinism tax.** Every clock read, random value, ad hoc HTTP call and environment lookup in orchestration code must route through a durable-step API, and a missed one produces an error that is hard to debug and impossible to search for (Opus-14). Temporal's SDK makes this visible rather than hidden: `patched(id)` exists purely so authors can change workflow code safely against old histories (`_context.py:810`), and `wait_condition`'s docstring warns that changing `timeout=None` later "constitutes a nondeterministic change to workflow code" (`:1028`) (Codex-7). Most products hide this problem; Temporal makes the versioning primitive public.

**Operational limits.** Temporal's default history limit is 50MB with a 2MB per-payload limit, and each activity transition costs roughly 50 to 100ms (Opus-15). A 100-step agent emitting 500KB per step hits the ceiling.

**The vendor's own admission.** Restate ships `explicit_recovery` (preview), which runs the handler again from the start and tells the author to supply stable idempotency keys instead of replaying the journal (`object.py:205`) (Codex-8). That is a durable execution system offering an opt-out from durability's central mechanism, which is the strongest available evidence that replay and nondeterministic handlers are an awkward fit.

The counter-case is also real and should not be dismissed: replay is semantically fine for dynamic agents, since determinism does not forbid dynamic control flow (Kimi-5), and OpenAI Codex and Replit Agent 3 are cited as production users (though that claim is Temporal's own marketing and was not independently verified, per Kimi caveat-5). The disagreement is about fit, not feasibility.

### C7 ★★ Segments make "delivered" ambiguous

GSM-7 gives 160 characters per segment and UCS-2 gives 70; concatenated messages carry a UDH so segments hold 153 or 67 characters, and billing is per segment (GLM-3). Concatenation is supported up to 1,600 characters, ten segments (GLM-4). Twilio reports status **per Message resource, not per segment**, so "delivered" does not mean the handset reassembled every segment (Opus §4, Opus-16). A single emoji drops a 140-character message to UCS-2 and doubles segment count and cost.

The code lens shows the same fact as a billing field: `num_segments` is `0` until a sender is assigned for Messaging Service sends, long bodies are segmented and charged per segment, and `validity_period` (1 to 36,000 seconds, default 36,000) sits on the same flat create call as `content_sid` (Codex-3, `message/__init__.py:74`, `:105`, `:738`, `:761`). SMS-only and WhatsApp/template-only options share one namespace, and the caller is left to know which apply.

Consequence: a transport that streams tokens into SMS produces dozens of billed segments and a reply that can arrive interleaved with its own predecessor. Buffering to a turn is a correctness requirement, not an optimization.

### C8 ★★ The session key is an application-level string, not a connection

LangGraph keys a run by `thread_id` in config (`checkpoint/base/__init__.py:213`), Stream by `cid` as `type:id` (`channel.py:25`), OpenAI Agents by `session_id` (`session.py:77`), Restate by virtual-object key (`context.py:812`), LiveKit by room participant plus the `lk.chat` topic (`room_io.py:99`) (Codex conv-1). The transport connection is disposable; the key is not. Restate's virtual object is the clearest statement of the shape the kickoff asks about: keyed, stateful, serialized per key, transport-agnostic, with transport-specific capabilities handled by separate typed side channels (durable promises, timers, queues) rather than by widening the core object (Kimi-4).

Opus rec-7 draws the design consequence: encode channel capability as **data the session consults**, not branches scattered through it.

### C9 ★★ Long waits are durable state, and channel policy can still kill them

Every durable product treats a long wait as a timer rather than an occupied process: Temporal `sleep` and `wait_condition` (`_context.py:1003`, `:1028`), Restate `ctx.sleep` (`context.py:645`), DBOS recovery of pending workflows (`_recovery.py:14`), LangGraph `interrupt` (`types.py:913`) (Codex conv-4). Restate adds the FaaS-specific version: handlers can suspend while waiting for an external event, for example a user approving an order, without paying for wait time (GLM-14).

The channel adds a constraint the runtime cannot see. WhatsApp opens a 24-hour customer service window from the last inbound user message; inside it free-form messages are allowed, outside it only pre-approved templates (GLM-6, GLM-7, Opus-17). A workflow that sleeps twelve hours and replies is fine. One that sleeps twenty-five fails **at send time**, not at design time. The runtime survived; the channel did not.

### C10 ★★ Throughput is policy the API does not report

Throughput is a function of number class and account standing: short codes around 100 MPS, toll-free and long codes far lower (GLM-8), and US 10DLC throughput scales with Brand Trust Score and campaign registration from a few messages per day to thousands (GLM-9). No send-API response exposes remaining capacity (GLM §"What's NOT", item 6). Vapi's SMS product makes 10DLC approval an onboarding step before the first message (GLM-25), which confirms the policy shapes the agent before any code runs.

Design consequence, stated in Opus rec-7: the capability object must carry throughput and free-form window alongside segment length and encoding, because the framework has to model backpressure the channel refuses to report. Codex-3 shows what happens when it does not: channel-specific fields leak into the common create call and the caller is left to know which apply.

### C11 ★ Resume is where effects re-fire

Khan's conformance probe measures this directly: LangGraph 1.2.9 is "exactly-once across interrupts and at-least-once across crashes on one API," and **consume-once fails under concurrent delivery**, with k processes resuming one parked interrupt firing the gated effect k times at saturation 1.0 in 36 of 40 cells, crossing hosts (Kimi-9). The code lens documents the mechanism that makes re-execution possible: LangGraph restores the checkpoint and re-runs the node body from the start (`types.py:928`), and multiple interrupts are matched to stored resume values **by order** (`:930`), so inserting an `interrupt()` call silently changes the meaning of payloads already stored (Codex-6).

The same shape appears in a different form in LiveKit: `room_io.py:545` spawns `asyncio.create_task(_read_text())` per inbound stream with no ordering barrier, so two near-simultaneous sends are two tasks with no sequencer (Codex-1). The codex lens did not run a race to confirm observable behavior, so treat the ordering consequence as untested and the concurrency exposure as structural.

---

## 3. Disputed findings

Per Nasser 2026 (arxiv:2601.05114), voting between same-conviction agents amplifies bias rather than averaging it. These are reported, not resolved by count.

### D1. Is durable execution the right primitive for agents?

**Pro (feasible and in use).** Temporal's determinism requirement applies only to orchestration code; activities are free to be nondeterministic; dynamic agents are supported (Kimi-5, GLM-11, GLM-13). Restate's journal makes replay skip-forward (GLM-14). DBOS makes durability a library over Postgres (Kimi-3). Khan's probe finds DBOS 2.27.0 conformant in all probed cells and OpenAI Agents SDK 0.18.2 holding effect exactly-once across session restore (Kimi-9). The narrative lens's conventional-wisdom section states the mainstream position fairly: an agent is a workflow whose steps call a model (Opus §2).

**Con (wrong primitive).** The cost model is inverted for agents (Opus-13), the determinism tax lands on the artifact that changes most often (Opus-14), history and payload limits bite at realistic agent sizes (Opus-15), and replay is not the same as a safe retry of an external provider call embedded in the workflow (GLM-20). The strongest structural evidence is Restate shipping `explicit_recovery` to skip replay entirely (`object.py:205`, Codex-8).

**Why they disagree.** Three separate axes are being conflated. (1) **Definition of fit:** the pro side answers "is replay semantically possible for agents," which is yes; the con side answers "is replay the cheapest way to get crash recovery for this workload shape," which is often no. (2) **Workload assumption:** microservice-shaped steps of milliseconds versus agent-shaped steps of tens of seconds and large payloads. (3) **Change rate:** a payment workflow is stable for years; an agent graph changes weekly, and replay compatibility turns the graph into a versioned API surface. No lens is factually wrong here. Opus §3 is explicit that Temporal's rebuttal is correct on its own terms and that "feasibility is not fit."

**What would resolve it.** Opus §5 specifies the experiment: a 40-step research agent with 500KB tool outputs and three human approval gates, implemented twice (Temporal workflow with activities; top-level rerun caching literal step returns in an artifact store), crashed at step 20 and again during an approval wait. Measure tokens re-spent per crash, p50/p99 end-to-end latency, engineering hours to add one step and to change the prompt mid-run, count of nondeterminism errors, and recovery success rate. Opus predicts token re-spend collapses toward zero for **both** because both journal the expensive result, and the difference shows up in operational surface and change cost. Two supporting numbers: the fraction of real agent steps producing payloads above 2MB (decides whether history limits are theoretical), and the rate of status-callback inversions and duplicate inbound events per 10,000 messages (decides how much of the session layer must be order-tolerant by construction).

### D2. What does Restate's "runs exactly once" actually cover?

**Restate's side.** An invocation "runs exactly once regardless of failures"; every side-effecting step and its result is journalled; replay skips forward (GLM-14, Kimi-4).

**The academic side.** Source-only recovery cannot deliver both no-duplicates and no-loss across a dual write, and the outbox pattern does not save you because relay delivery and checkpointing remain separate durable operations (Kimi-8). "Exactly-once" is not formally defined in the stream-processing literature and bundles delivery, consistency and determinism (Kimi-7).

**The code side.** No implementation in the survey demonstrates true end-to-end exactly-once (Codex conv-2), and Restate's journal has a finite configured retention window (`journal_retention`, `idempotency_retention` at `object.py:62-66`) (Codex-8).

**Why they disagree.** They are not measuring the same thing. Restate's guarantee is about **invocation completion inside the journal**: the handler's control flow observes each recorded step once. It is not a claim that your outbound HTTP call to Twilio landed once, and it cannot be, because Twilio's acceptance record is in another system (Kimi-8). The vendor's own fine print says dedup requires an opt-in idempotency key (GLM-14). Both statements are true; the marketing sentence omits the scope.

**What would resolve it.** Two checks a staff engineer can run before the API review: (a) read the journal-mismatch and retention documentation for the version under review, and (b) kill the process between the external effect inside `ctx.run` and the journal write, then restart and observe whether the effect repeats. If it repeats, the guarantee is invocation-scoped, as the theory predicts. Also worth noting: Huang and Garcia-Molina defined a **relaxed** exactly-once tier for limited-capability endpoints 25 years ago (Kimi-10), which is the academic precedent for advertising a weaker guarantee on a constrained channel rather than one guarantee everywhere.

### D3. Does LiveKit support text-only sessions in production, or only in simulation?

**Docs side.** The multimodality page says text enables "text-only sessions or hybrid voice and text interactions," and assigns turn detection and interruption to the speech and audio row (GLM-21). Taken alone, this reads as a supported production modality.

**Code side.** `_text_only` returns true only under `SimulationMode.SIMULATION_MODE_TEXT` (`agent_session.py:2427`), that branch is what nulls VAD/STT/TTS (`agent_activity.py:5304`), and `say()` raises without TTS when audio output is enabled (`agent_activity.py:1760`) (Codex-1). Text output's contract is a segment plus a boundary, with no delivery receipt, no retry and no per-message acknowledgement, "because audio has none of those" (Codex-1, `io.py:515`).

**Why they disagree.** They describe different layers, and both are accurate at their layer. The docs describe what the session **can be configured to do**; the code describes what the runtime's own text path **is**. The gap is not a documentation bug. It is the exact place where an audio-first design shows through: the text path exists as a sink for speech, not as a first-class message channel, and the supported way to build text-only is to disable audio I/O and supply your own `TextOutput` (`room_io/types.py:113-117`).

**What would resolve it.** Construct a text-only `AgentSession` in a non-simulation job, attach a `TextOutput`, and drive two inbound `lk.chat` streams concurrently. Observe (a) whether `say()` is reachable, (b) whether the two streams interleave, given the per-stream `asyncio.create_task` with no sequencer (`room_io.py:545`), and (c) what the framework reports as delivery status. This is also the highest-value question for the API-critique round, because it is answerable by the API's author and it is not an accusation.

### D4. What does "resume" mean? Three answers, one word

**Docs impression.** `interrupt()` pauses graph execution and `Command(resume=...)` continues "from the last checkpoint" (GLM-23), which reads as continuation.

**Code fact.** "The graph resumes from the start of the node, re-executing all logic" (`types.py:928`), and interrupts are matched by order (`:930`) (Codex-6).

**Measurement.** LangGraph 1.2.9 is exactly-once across interrupts and at-least-once across crashes on the same API (Kimi-9).

**Why they disagree.** "Checkpoint" is a superstep boundary, not an instruction pointer. Restoring the checkpoint and re-running the node body is continuation **of the graph** and re-execution **of the node**. The three sources are compatible once that distinction is made, and the disagreement is really about whether a user reading "resume" will infer the second meaning. Khan's framing is the one to carry: a framework that persists execution state must decide what resume means **for effects that already happened**, and six named properties (prefix continuation, effect exactly-once, fork determinism, checkpoint validity, consume-once, recovery determinism) are the vocabulary for that decision (Kimi-9).

**What would resolve it.** Put a side effect before the `interrupt()` in one node, resume, and count the effects. The paper's own probe is the template; a five-line test settles it per framework and per version.

### D5. How reliable is a "delivered" status callback, and how late can it be?

**What is documented.** The status lifecycle (`queued`, `sent`, `delivered`, `undelivered`, `failed`) and the callback payload (GLM-2); a `read` status after `delivered` on channels that support it (Opus-16); platform-specific caveats, including Vonage documenting status callbacks up to 30 days after send on its Messages API (GLM-10).

**What is not.** No source states how late a `delivered` or `undelivered` callback may arrive before it should be treated as lost, and Twilio reports status per Message resource rather than per segment (Opus-16, Opus §5). The web lens flags this as a gap, the narrative lens flags it as a gap, and the code lens could not extract ordering or retry semantics from the generated resource model at all (Codex §"could not establish").

**Why this is a dispute rather than a gap.** The lenses disagree on whether the status stream is a usable state machine or a hint. GLM treats the enum as "the text-modality replacement for the audio clock" (GLM-2). Opus treats the callback as unreliable by default and recommends a second verification path. Kimi supplies the precedent for the second path: a real-time webhook plus a scheduled polling fallback with idempotent upserts (Kimi-A14), and the Standard Webhooks requirement that producers let consumers list failed messages and replay manually (Kimi-A12).

**What would resolve it.** A measured distribution: for a large traffic sample, the p50/p95/p99 delay from send to `delivered`, and the rate of status inversions (a `delivered` followed by `undelivered`) per 10,000 messages. No lens found this published. Until it exists, design for the second path.

---

## 4. Cross-lens gaps

Aggregated from GLM §"What's NOT in the recent literature," Kimi §5, Codex §"What this lens could not establish from code," and Opus §5. These are candidates for the book's "what nobody has measured" passages, and for out-of-distribution questions.

**G1. No measured delivery-receipt latency or inversion rate.** Twilio enumerates the states but publishes no latency budget; no CPaaS source promises ordered or exactly-once inbound delivery (GLM gap-1, Opus §5, Codex gap). Every design either over-provisions or under-provisions the status layer without this number.

**G2. Inbound segment reassembly is undocumented.** Outbound concatenation is specified precisely (160/153, 70/67, 1,600-character ceiling). How inbound multi-segment messages arrive at webhooks (how many posts, in what order, what a partial failure looks like) appears in no CPaaS doc found (GLM gap-2), and there is no paper (Kimi gap-3).

**G3. No cross-transport session abstraction has been studied.** LiveKit names text as a capability, Pipecat documents transports one carrier at a time, Vapi ships SMS as a product feature, and none documents one session abstraction spanning chat SDK, telephony and MQ client (GLM gap-3). The academic version: the question has been asked in the transactional-dataflow world (Apiary, Stateful Entities, Styx) and **not** in the agent world (Kimi gap-6). The code lens found only per-library evidence, never a shared contract (Codex, all entries).

**G4. No vendor publishes a fault-injection or conformance kit, and testing guidance is missing everywhere.** No durable-execution vendor publishes a reproducible suite (kill worker mid-activity, duplicate webhook delivery, partition during journal write); no CPaaS vendor publishes a webhook conformance kit (GLM gap-4). The evidence that this matters is ArrivalBench: single-execution grading certifies 86 to 100 percent of agent-generated pipelines that are **7.0 to 79.2 percent silently wrong** under late, duplicated, out-of-order and retried delivery (Kimi-A1). That is a replication failure of an entire evaluation methodology, not a bug.

**G5. Typing indicators and read receipts, text's analog of barge-in, appear in no agent framework doc and no paper.** GLM looked and found the likely primary outside its sweep (GLM gap-5); Kimi found nothing treating "the status callback arrived 40 minutes late and out of order" as a research problem (Kimi gap-2); Opus argues the mapping is wrong anyway, because cancelling an in-flight turn cancels a reply the user never saw (Opus §4). The design primitive (turn boundary plus supersede rule) is asserted, not measured.

**G6. Hour-spanning human-in-the-loop waits are unmeasured.** The workflow literature has durable timers and signals; the agent literature has interrupts and HITL. Nothing measures the combination, and nothing measures what a session looks like after four hours of dormancy (Kimi gap-4). Restate's suspend-on-FaaS claim is the only first-party statement and it is vendor-authored (GLM-14).

**G7. Concurrency anomalies in agent runtimes have one formal source and one measurement.** Four anomalies formalized in TLA+ (stale-generation, phantom-tool, causal-cascade, tool-effect reordering) with a reproduction of a lost update in ByteDance's deer-flow and a reordering issue in LangGraph's `ToolNode` (Kimi-A3), plus consume-once saturation of 1.0 in 36 of 40 cells (Kimi-9). One source is not a literature (Kimi gap-5), and the code lens found the structural exposure (per-stream tasks with no sequencer, order-based interrupt matching) without running a race (Codex-1, Codex-6).

**G8. The replay-versus-checkpoint-and-rerun bake-off does not exist.** Opus §5 specifies it and predicts the outcome; nobody has run it. Until someone does, "durable execution fits agents" and "durable execution is the wrong primitive" are both positions without a decisive measurement.

**G9. Twilio's create-time idempotency is unresolved.** The IETF draft defines the header and its 409/422/400 semantics (Kimi-11); no such parameter appears in `MessageList.create` (`message/__init__.py:810-905`), which the codex lens correctly labels "weak evidence of absence, not proof" (Codex-3). If Twilio does not accept one, outbound dedup is local-only, which is precisely Andreakis's dual-write hole (Kimi-8). This is a one-question resolution: ask Twilio, or send the same create twice and count.

**Also unmeasured, lower priority:** the fraction of agent steps producing payloads above 2MB (Opus §5); Ably's subscriber-side delivery guarantee (Codex-13); Stream's offline delivery and unread semantics (Codex-14); LiveKit text ordering across concurrent `lk.chat` streams (Codex-1); and the two classic papers the academic lens could not obtain in primary form (Henderson and Terlevich 1994; a Restate arXiv paper, which Kimi's arXiv API search confirms does not exist).

---

## 5. Recommendations

Each is phrased as an action, with its support and the condition under which it would be wrong.

**R1. Model the text session as message-oriented, not stream-oriented. Buffer to a turn boundary before any send.**
Support: GLM-3, GLM-4, Opus rec-1, Codex-3 (`num_segments` as a billing field). Wrong if the transport is a true streaming chat channel where partial output is meaningful to the reader, as in LiveKit text streams where `interim_stream` and `final_stream` share a `segment_id` and partial output is the product (Opus §4, Opus-19).

**R2. Dedupe inbound on the gateway message id, and persist that id in the same transaction as the session event it produces.**
Support: GLM-5, Opus-12, Opus rec-2, Codex-3, Kimi-8, Kimi-A12. Wrong only if the channel provides exactly-once inbound delivery, which no CPaaS vendor in the sweep does.

**R3. Put exactly-once at the effect boundary: an idempotency key on every tool call and every outbound send, committed with the checkpoint. Expose 409 on concurrent retry, 422 on payload-changing reuse, 400 on missing required key.**
Support: Kimi-11 (normative), Opus rec-3, Codex-8 (`idempotency_key` on the call surface), GLM-14. Wrong if the downstream API exposes a stable transactional id that genuinely deduplicates across sessions; then you can delegate rather than duplicate the mechanism.

**R4. Put the durable boundary around orchestration and record the model result as the step output. Never replay the model call.**
Support: GLM-11, GLM-13, Kimi Finding B, Opus rec-4, Codex conv-3. Wrong if the agent is a single model call with no orchestration, where there is nothing durable to protect.

**R5. Treat "exactly-once" in any API review as four questions: which of delivery, consistency and determinism; over what boundary; with what retention; and what happens on a concurrent retry.**
Support: Kimi-7 (the three properties), Kimi-8 (retention as a chosen half-life), Codex-8 (`journal_retention`, `idempotency_retention`), GLM dissent-1. Wrong never, but it is unhelpful if used as a rhetorical weapon rather than a clarifying question. Ask it of your own design first.

**R6. Do not ship an audio-first session with audio disabled as your text product. Build a session whose primary object is the message, and consult a capability object for everything transport-specific.**
Support: GLM-21, Codex-1 (`_text_only` only in simulation), Codex-2 (`TextFrame` dropped as `None`), Codex-4 (VAD in the session contract), Kimi Finding C, Opus rec-7. Wrong if you will support exactly one channel forever and never add a second, in which case the capability object is pure indirection.

**R7. The capability object should carry, at minimum: maximum segment length, encoding, read-receipt support, typing support, free-form window, delivery-order guarantee, and throughput class.**
Support: GLM-3 (length/encoding), Opus-16 (read receipts), GLM-6/GLM-7 (24-hour window), GLM-8/GLM-9 (throughput), Opus rec-7, Codex-3 (the leak that happens without it). Wrong if the values are static per deployment and never vary at runtime; then constants are simpler and the object is ceremony.

**R8. Choose replay-based durability for sub-second resume, high fan-out or strict audit; choose checkpoint-and-rerun for expensive, low-concurrency, frequently changing agent graphs. Set the dedup retention deliberately, as a product decision, not a default.**
Support: Opus rec-5, Opus-13, Opus-15, Kimi-15, Codex-8 (`explicit_recovery`), Kimi-8 (bounded dedup state limits the guarantee's lifetime). Wrong for payment or ledger workloads, where millisecond steps and strict ordering invert the economics.

**R9. Externalize large step payloads and journal a reference, not the blob.**
Support: Opus-15 (2MB per payload, 50MB history), Opus rec-6, Kimi-15 (model requests as the cut that cannot be replayed). Wrong if steps are bounded and small, where the extra indirection buys nothing.

**R10. Test the failure matrix deterministically, not with integration luck: duplicate inbound, out-of-order and inverted status, crash between effect and checkpoint, two workers resuming one parked interrupt, the 24-hour window boundary, and inbound segment reassembly.**
Support: Opus rec-8, Opus-12, Kimi-9 (consume-once fails under concurrent delivery at saturation 1.0 in 36 of 40 cells), Kimi-A1 (single-execution grading misses 7 to 79 percent silent wrongness), GLM gap-4. Wrong if you own both ends of a single in-order WebSocket, in which case most of the matrix is inapplicable.

**R11. Before publishing a durability claim, state its boundary and its retention in the same sentence, and put the retention value in the config the user can see.**
Support: Kimi-8, Codex-8, GLM-14, Opus rec-3. Wrong if the guarantee is genuinely unbounded, which no source in this synthesis demonstrates.

---

## 6. Mapping to the whiteboard and the API-critique round

The kickoff's success criterion is that a staff engineer could defend these decisions on a whiteboard and in an API review with the API's author. This section is the bridge.

### 6.1 The whiteboard scenario

Design a channel-agnostic agent session that serves SMS, WhatsApp and a websocket chat client. Draw it in five boxes and defend each.

1. **Ingress dedupe, before anything else.** Key on the gateway's message id (`MessageSid` on Twilio, `webhook-id` on the Standard Webhooks pattern). Write the dedupe record and the session event in one transaction. Defend with C3 and R2. The reviewer's likely objection is "that is a distributed transaction"; the answer is that it is not, because both records are yours, and the only cross-system record is the carrier's acceptance, which is why the dedupe key is the carrier's id rather than yours (Kimi-8).
2. **Session envelope versus per-transport capability object.** The envelope holds session id, conversation key, turn boundaries, durable state reference. The capability object holds segment length, encoding, read-receipt and typing support, free-form window, ordering guarantee, throughput class. Defend with C8, C10, R6, R7. The reviewer's objection is "why not just branch on channel type"; the answer is that the branch is the same information stored in the wrong place, and Codex-3 shows what happens when channel-specific fields share one flat namespace.
3. **Turn assembler.** Inbound segments reassemble to a turn; outbound turns buffer before send. Defend with C7, R1. Acknowledge the gap: inbound reassembly has no spec (G2), so the design is defensive, not derived.
4. **Durable orchestration boundary.** The model call is an activity or a journalled step; its result is recorded; the control flow that consumes it is deterministic. Defend with C4, R4. Name the retained decision: replay versus checkpoint-and-rerun (D1, R8), and say which workload shape you are optimizing for rather than claiming one is universally right.
5. **Effect boundary with idempotency keys.** Every tool call and every outbound send carries a key; concurrent retries get 409. Defend with C5, R3.

Then draw the failure matrix as a second diagram: six injected faults, each pointing at the box that absorbs it. If a fault has no owner, that is the honest finding, and it goes in the design doc rather than being hidden.

### 6.2 The API-critique round

The goal is not to win. It is to demonstrate that you read the code, understand the design's trade-offs, and can name the leak without insulting the person who chose it. Use questions, and let the author confirm.

**Ask these, in this order.**

1. "`_text_only` returns true only under a simulation mode, and that branch is what nulls VAD, STT and TTS. What is the supported production path for a text-only session: disable audio I/O and supply a `TextOutput`, or is there a runtime path I missed?" (Codex-1, D3). This is the single highest-value question in the round, and it is answerable.
2. "`TextOutput`'s contract is a segment plus a boundary, with no message id and no delivery receipt. Is that because the audio modality has no such concept, or because text delivery is considered out of scope for the session?" (Codex-1, `io.py:515`). The answer tells you whether text is a modality or a transcript.
3. "`room_io.py:545` spawns a task per inbound stream with no sequencer. Are two near-simultaneous `lk.chat` sends ordered anywhere above that, or is ordering left to the application?" (Codex-1, C11). Volunteer that you did not run the race.
4. "If I resume a parked interrupt on two workers, how many times does the gated effect fire?" (Kimi-9, C11). Ask it of any framework, not just LiveKit. The measured answer in the literature is k times.
5. "When the docs say text-only sessions are supported and the audio row owns turn detection, is the text session's turn boundary supplied by the application or inferred by the runtime?" (GLM-21, C2).

**Assert these, with the citation in your pocket.**

- "Exactly-once here is invocation-scoped, not effect-scoped." Then state the boundary you think it covers and ask for correction. Do not assert it as a gotcha (D2, C1, R5).
- "A 1,600-character logical message is ten billed segments and one status callback, so `delivered` is ambiguous at the segment level." (GLM-3, GLM-4, C7).
- "WhatsApp's 24-hour window means a durable wait can outlive the channel's permission to send." (GLM-6, C9). This one is usually new to the room and lands well because it is a runtime-plus-policy interaction, not a runtime bug.

**Do not assert these.**

- That Temporal, Restate or DBOS "guarantee exactly-once" without the scope (C1, R5).
- That OpenAI Codex and Replit Agent 3 run on Temporal. That is Temporal's own marketing, unverified in this run (Kimi caveat-5).
- That replay is unsafe for LLM calls. The defensible claim is narrower: replay is safe when the model call is an effect and its result is recorded; it diverges when a model output drives an unrecorded branch (Kimi Finding B, D1).
- Any rate, latency or inversion figure for delivery receipts. None exists in any lens (G1).

**Close with the constructive version.** "The design I would defend is: message-oriented text session, capability object for transport differences, dedupe on the carrier's id before the model, idempotency keys at the effect boundary, durable orchestration with the model result recorded rather than replayed, and a deterministic fault matrix in CI. The parts I cannot cite are the receipt latency distribution and inbound segment reassembly, and I would instrument both rather than assume them."

That closing is the whole synthesis in one paragraph, and every clause has a lens behind it.

---

## 7. Source manifest

Every URL, arXiv id and repository cited across the four lenses and this synthesis, grouped by source lens. Canonical publisher URLs as the lens recorded them.

### 7.1 From `lens-glm.md` (25 sources)

- Twilio, Track the Message Status of Outbound Messages: https://www.twilio.com/docs/messaging/guides/track-outbound-message-status
- Twilio, Outbound Message Status in Status Callbacks: https://www.twilio.com/docs/messaging/guides/outbound-message-status-in-status-callbacks
- Twilio, What SMS Character Limit: https://www.twilio.com/docs/glossary/what-sms-character-limit
- Twilio Help, Concatenated SMS over 160 characters: https://help.twilio.com/articles/223181508-Does-Twilio-support-concatenated-SMS-messages-or-messages-over-160-characters-
- Twilio, Webhook connection overrides: https://www.twilio.com/docs/usage/webhooks/webhooks-connection-overrides
- Twilio, WhatsApp Key Concepts: https://www.twilio.com/docs/whatsapp/key-concepts
- Meta for Developers, WhatsApp send messages (service messages): https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
- Twilio, Account Based Throughput Overview: https://www.twilio.com/docs/messaging/guides/account-based-throughput-overview
- Twilio Help, Message throughput (MPS) and Trust Scores for A2P 10DLC: https://help.twilio.com/articles/1260803225669-Message-throughput-MPS-and-Trust-Scores-for-A2P-10DLC-in-the-US
- Vonage, SMS Delivery Receipts API Guide: https://developer.vonage.com/en/messaging/sms/guides/delivery-receipts
- Temporal, Workflow: https://docs.temporal.io/workflows
- Temporal, Error handling, Python SDK best practices: https://docs.temporal.io/develop/python/best-practices/error-handling
- Temporal, OpenRouter integration, Python SDK: https://docs.temporal.io/develop/python/integrations/openrouter
- Restate, Key Concepts: https://docs.restate.dev/foundations/key-concepts
- Restate, What is Durable Execution: https://restate.dev/what-is-durable-execution
- DBOS, Why You Should Build Durable Workflows With Postgres: https://www.dbos.dev/blog/why-postgres-durable-execution
- Microsoft Learn, Durable Functions overview: https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-overview
- Microsoft Q&A, Azure durable function at least once execution: https://learn.microsoft.com/en-us/answers/questions/1351212/azure-durable-function-at-least-once-execution-and
- Trigger.dev, Trigger.dev vs Inngest: https://trigger.dev/vs/inngest
- SwytchCode, Best Durable Execution Platforms for AI Agents: https://www.swytchcode.com/content/best-durable-execution-platforms
- LiveKit, Multimodality overview: https://docs.livekit.io/agents/multimodality
- Pipecat, Twilio WebSocket Integration: https://docs.pipecat.ai/pipecat/telephony/twilio-websockets
- LangChain, Interrupts (LangGraph): https://docs.langchain.com/oss/python/langgraph/interrupts
- LangChain, Human-in-the-loop (middleware): https://docs.langchain.com/oss/python/langchain/human-in-the-loop
- Vapi, SMS chat: https://docs.vapi.ai/chat/sms-chat

### 7.2 From `lens-kimi.md` (15 core, plus adjacent used here)

Core:
- Garcia-Molina and Salem (1987), Sagas, DOI https://doi.org/10.1145/38713.38742 · https://www.cs.princeton.edu/techreports/1987/070.pdf
- Fischer, Lynch and Paterson (1985), FLP, DOI https://doi.org/10.1145/3149.214121
- Li et al. (2022), DBOS progress report, CIDR: https://www.cidrdb.org/cidr2022/papers/p26-li.pdf
- Restate, Building a Modern Durable Execution Engine: https://restate.dev/blog/building-a-modern-durable-execution-engine-from-first-principles · https://restate.dev/what-is-durable-execution · https://docs.restate.dev/foundations/key-concepts
- Temporal (2025), Of course you can build dynamic AI agents with Temporal: https://temporal.io/blog/of-course-you-can-build-dynamic-ai-agents-with-temporal
- Carbone et al. (2015), Lightweight Asynchronous Snapshots: https://arxiv.org/abs/1506.08603
- Trofimov et al. (2019), Delivery, consistency, and determinism: https://arxiv.org/abs/1907.06250
- Andreakis (2026), Machine-Checked Dual-Write Recovery from a Commit Log: https://arxiv.org/abs/2608.00501 · https://doi.org/10.5281/zenodo.22700396
- Khan (2026), Resume Means Resume: https://arxiv.org/abs/2608.03836
- Huang and Garcia-Molina (2001), Exactly-once semantics in a replicated messaging system: https://doi.org/10.1109/icde.2001.914808 · abstract via https://api.openalex.org/works/doi:10.1109/icde.2001.914808
- Jena, Dalal and Tishkin (2026), The Idempotency-Key HTTP Header Field: https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
- Cho et al. (2022), CheckDST: https://arxiv.org/abs/2112.08321
- Stivers et al. (2009), Turn-taking: https://doi.org/10.1073/pnas.0903616106
- Overeem et al. (2021), Event sourced systems and schema evolution: https://arxiv.org/abs/2104.01146 · https://doi.org/10.1016/j.jss.2021.110970
- Nakajima (2026), Replay Is a Family of Assertions: https://arxiv.org/abs/2611.06924 · https://github.com/yoheinakajima/agent-runtime-laws

Adjacent, cited above:
- ArrivalBench: https://arxiv.org/abs/2610.02363
- Deterministic Replay for AI Agent Systems: https://arxiv.org/abs/2607.16200
- Concurrency Anomalies in Multi-Agent LLM Systems: https://arxiv.org/abs/2606.17182
- Agent Mesh, Reliability Primitives for Non-Idempotent Agent Delegation: https://arxiv.org/abs/2608.26225
- Alvaro, Rosen and Hellerstein (2015), Lineage-driven Fault Injection: https://doi.org/10.1145/2723372.2723711
- CheckMate: https://arxiv.org/abs/2403.13629
- LogPlayer: https://arxiv.org/abs/1911.11286
- Nakajima (2026), The Log is the Agent: https://arxiv.org/abs/2605.21997
- ESAA: https://arxiv.org/abs/2602.23193
- Stateless Decision Memory for Enterprise AI Agents: https://arxiv.org/abs/2604.20158
- Fowler, Event Sourcing: https://martinfowler.com/eaaDev/EventSourcing.html · CQRS: https://martinfowler.com/bliki/CQRS.html
- Standard Webhooks specification: https://raw.githubusercontent.com/standard-webhooks/standard-webhooks/main/spec/standard-webhooks.md
- Kafka design documentation: https://kafka.apache.org/documentation/#design · KIP-98: https://cwiki.apache.org/confluence/display/KIP/KIP-98%3A+Exactly+once+delivery+and+transactional+messaging
- Gupta (2026), Two-Path Status Verification: https://arxiv.org/abs/2607.15529
- CloudEvents: https://cloudevents.io/
- Arora et al. (2025), Talking Turns: https://arxiv.org/abs/2503.01174
- Feng et al. (2023), LLM-driven DST: https://arxiv.org/abs/2310.14970
- Mitra et al. (2026), Adaptive Turn-Taking: https://arxiv.org/abs/2606.13544
- MultiWOZ: https://arxiv.org/abs/1810.00278
- Styx: https://arxiv.org/abs/2312.06893 · https://doi.org/10.1145/3725363
- Stateful Entities: https://arxiv.org/abs/2112.00710 · https://doi.org/10.48786/edbt.2024.02
- Apiary: https://arxiv.org/abs/2208.13068
- DBOS architecture: https://docs.dbos.dev/architecture

### 7.3 From `lens-codex.md` (14 implementations, pinned)

- livekit/agents: https://github.com/livekit/agents (HEAD `cbb5770c716c`; local snapshot)
- pipecat-ai/pipecat: https://github.com/pipecat-ai/pipecat (local clone `6eed6ff7793c`, upstream `0602125e1d81`)
- twilio/twilio-python: https://github.com/twilio/twilio-python (`7e3ca781178d`)
- openai/openai-python: https://github.com/openai/openai-python (`3dae6090cc31`)
- openai/openai-agents-python: https://github.com/openai/openai-agents-python (`64fbde5d02b4`)
- langchain-ai/langgraph: https://github.com/langchain-ai/langgraph (`08a201695378`)
- temporalio/sdk-python: https://github.com/temporalio/sdk-python (`968c3c9dcf50`)
- restatedev/sdk-python: https://github.com/restatedev/sdk-python (`ee674cc00a53`)
- dbos-inc/dbos-transact-py: https://github.com/dbos-inc/dbos-transact-py (`81aa7eea4e5b`)
- nats-io/nats.py: https://github.com/nats-io/nats.py (`f42260fad14c`)
- aio-libs/aiokafka: https://github.com/aio-libs/aiokafka (`8f7357470cbb`)
- pika/pika: https://github.com/pika/pika (`06b5a03926ac`)
- ably/ably-pubsub-python: https://github.com/ably/ably-pubsub-python (`d4e3e5dd6190`)
- GetStream/stream-py: https://github.com/GetStream/stream-py (`7140b6911e2b`)

### 7.4 From `lens-opus.md` (plus sources cited by this synthesis)

- Twilio, How long can a message be: https://www.twilio.com/docs/glossary/what-sms-character-limit
- Twilio, Outbound Message Status in Status Callbacks: https://www.twilio.com/docs/messaging/guides/outbound-message-status-in-status-callbacks
- Twilio, Event delivery retries and duplication: https://www.twilio.com/docs/events/event-delivery-and-duplication
- Twilio, Webhooks connection overrides: https://www.twilio.com/docs/usage/webhooks/webhooks-connection-overrides
- Meta, WhatsApp send messages: https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
- Meta, WhatsApp messaging limits: https://developers.facebook.com/docs/whatsapp/messaging-limits/
- Temporal, Workflow Definition: https://docs.temporal.io/workflow-definition
- Temporal, Activities: https://docs.temporal.io/activities
- Temporal, Human-in-the-loop AI agent: https://docs.temporal.io/ai/cookbook/human-in-the-loop-python
- Temporal, Of course you can build dynamic AI agents with Temporal: https://temporal.io/blog/of-course-you-can-build-dynamic-ai-agents-with-temporal
- Restate, Key Concepts: https://docs.restate.dev/foundations/key-concepts
- Restate, Architecture: https://docs.restate.dev/references/architecture
- DBOS: https://www.dbos.dev/
- AWS, What is Step Functions: https://docs.aws.amazon.com/step-functions/latest/dg/welcome.html
- Microsoft Learn, Durable orchestrator code constraints: https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-code-constraints
- Apache Kafka, KIP-98: https://cwiki.apache.org/confluence/pages/viewpage.action?pageId=67641670
- LiveKit, Text and transcriptions: https://docs.livekit.io/agents/multimodality/text
- Martin Fowler, Event Sourcing: https://martinfowler.com/eaaDev/EventSourcing.html
- Garcia-Molina and Salem, Sagas: https://www.cs.princeton.edu/research/techreps/598
- ZenML, Your Agents Are Not Microservices: https://www.zenml.io/blog/agents-are-not-microservices
- ZenML, Why Kitaru Doesn't Use Journal Replay: https://www.zenml.io/blog/no-journal-replay
- befailproof, Why Temporal Breaks When You Put an AI Agent Inside It: https://befailproof.ai/blog/why-temporal-breaks-with-ai-agents/
- GSMA, IR.75 SMS Hubbing Architecture: https://www.gsma.com/newsroom/wp-content/uploads//IR.75-v2.0.pdf
- Aspnes, Distributed Systems, Chapter 8 (coordinated attack): https://ac.informatik.uni-freiburg.de/teaching/ss_23/ds_lectures/pdf/Aspnes_chapter8.pdf

### 7.5 Cited by this synthesis only

- Nasser (2026), on voting between same-conviction agents: https://arxiv.org/abs/2601.05114 (referenced in the recipe prompt; not independently verified in this run)

---

*Composed from the four lens reports in this run directory. No fact appears here that is absent from a lens; where lenses disagree on a fact, it is reported under Section 3 rather than resolved by count. Where a claim rests on a vendor's own statement about its own product, the entry says so.*
