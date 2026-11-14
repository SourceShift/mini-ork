# R1 Synthesis: How the LiveKit Agents framework actually works

**Run:** run-1794655682-58785 · **Date:** 2026-11-14 · **Cluster:** R1
**Audience:** a staff-level engineer preparing for a hiring-manager screen with the LiveKit Agents product lead. Agent loops in general are assumed; LiveKit-specific shape and tradeoffs are the target.

**Lenses read in full before writing:** `lens-glm.md` (21 primary public sources, breadth), `lens-kimi.md` (15 literature entries, rigor), `lens-codex.md` (15 code-pattern entries at pinned refs, practice), `lens-opus.md` (~2,260-word narrative, theory).

**Version frame.** The current stable line is `livekit-agents` 1.8.12, released 2026-11-13 (lens-glm S13; lens-codex pins commit `cbb5770c716c70009969cd7fa88e3c3662222ce9`, 2026-11-13). A 2.0.0b1 pre-release exists on PyPI dated 2026-10-12 (lens-glm S13). lens-opus read the tree at `a77605e` (2026-11-10), three days older. Every behavior below is pinned to 1.8.x unless stated otherwise. Anything about 2.0 is a gap, not a claim.

| Version | Date | What it anchors | Source |
|---|---|---|---|
| 1.0 GA | 2025-04 | `AgentSession` replaces `VoicePipelineAgent` and `MultimodalAgent` | lens-glm S1, S2 |
| 1.6.0 | 2026-06-11 | async tools: `ctx.update()`, `ToolFlag.CANCELLABLE` | lens-glm S11 |
| 1.6.1 | 2026 | turn detector v1-mini bundled in the Python SDK | lens-glm S6, S7 |
| 1.8.6 | 2026 | `max_tool_steps` enforced on the realtime tool loop; MCP cancellation notices | lens-glm S12 |
| 1.8.7 | 2026-10-14 | tools still running at interruption timeout continue asynchronously, deferred results delivered | lens-glm S12 |
| 1.8.9 | 2026-10-24 | interruption errors fall back to VAD without reconnecting | lens-glm S12 |
| 1.8.10 | 2026 | Silero VAD no longer auto-loaded | lens-glm S12 |
| 1.8.12 | 2026-11-13 | current stable; lens-codex pin | lens-glm S13, lens-codex header |
| 2.0.0b1 | 2026-10-12 | pre-release; text `MultilingualModel` removal promised | lens-glm S13, S7 |

---

## 1. TL;DR

- **1.8.12 is a pre-warmed process-pool runtime, not a spawn-per-job runtime.** A worker advertises availability over a websocket, reserves a slot, waits for a `JobAssignment`, and hands the job to an already-initialized job process; `AgentSession` is assembled inside that process. (lens-codex E1, E2; lens-glm S16) **Confidence: high for the shape, medium for exact constants.**
- **The turn loop is a synchronous commit pipeline.** VAD and STT feed a recognition state machine that owns endpointing, a separate end-of-turn decision, and a synchronous `on_end_of_turn` that spawns the reply; preemptive generation is speculative cascaded-LLM work cancelled on transcript drift. (lens-codex E4, E5, E7; lens-glm S7, S9) **Confidence: high.**
- **Tools are async-dispatched with progress updates, errors returned as `ToolError` outputs, per-session duplicate detection that fails open, and no documented timeout or retry policy.** (lens-codex E9; lens-glm S11; lens-opus §4) **Confidence: high.**
- **Session state is in-memory `ChatContext` plus `JobProcess.userdata`.** Nothing is written to durable storage and the job process is disposed after the session, so restart survival is an application problem. (lens-codex E10; lens-glm gaps; lens-opus §4) **Confidence: high.**
- **The three JD-named gaps are seams, not missing features.** Text modality has no runtime, durable workflows exist only as an experimental `livekit-durable` plugin not wired into core, and agent-to-agent communication stops at in-session handoff. (lens-glm gaps; lens-codex divergent 2; lens-kimi E14, E15; lens-opus §6) **Confidence: high for the absence, medium for the durability mechanism.**

**Framing line, attributed as interpretation:** LiveKit Agents is a floor-control system for realtime conversation. It owns who has the turn. Everything thin about it is a seam where floor control meets a channel or a duration it was not built for (lens-opus §3; synthesis judgment, not a sourced fact).

---

## 2. How to read the stars, and a warning about the panel

Star markers count lenses, not source families:

- **★** = 2 of 4 lenses converge
- **★★** = 3 of 4 lenses converge
- **★★★** = 4 of 4 lenses converge

Every finding states its lens count and names the lenses. Two caveats are load-bearing:

1. **The panel is not family-heterogeneous.** The run profile records that `lens-glm` and `lens-opus` resolve to the same zai family, while the `kimi` and `codex` lanes differ from their nameplates. Treat any agreement between lens-glm and lens-opus as one family's view unless lens-codex or lens-kimi corroborates it. For LiveKit internals, lens-codex is the load-bearing lens and lens-kimi is the independent check on the underlying problem, not on the code.
2. **A star is not a vote.** The recipe's discipline rule, citing Nasser 2026 (arxiv:2601.05114), is that voting between same-conviction agents amplifies bias rather than averaging it. Section 4 follows that rule: disputes are reported, not counted.

Lens anchors used inline: `lens-glm S<n>` = source number in `lens-glm.md`; `lens-kimi E<n>` = entry in `lens-kimi.md`; `lens-codex E<n>` = entry in `lens-codex.md`; `lens-opus §<n>` = section in `lens-opus.md`.

---

## 3. Findings by kickoff question

### Q1. The runtime path: worker, job, session, ownership, process boundaries

**Finding 1.1 ★ (2/4: lens-codex, lens-glm). Dispatch is a two-phase handshake, not a push.**
The LiveKit server sends `AvailabilityRequest` over the worker websocket. The worker increments `_reserved_slots` before calling the user's `request_fnc`, so concurrent availability checks see updated load. On accept it sends the availability response, registers a future in `_pending_assignments`, waits up to `ASSIGNMENT_TIMEOUT` for the matching `JobAssignment`, then calls `ProcPool.launch_job` (lens-codex E1, `worker.py:1391`, slot reservation `:1415`, assignment wait `:1452`, `launch_job` `:1477`, resolution `:1520`). Rejecting with `terminate=true` is a distinct verb from being unavailable (lens-codex E1, `worker.py:1409`). The operational view matches: "backend dispatch, LiveKit server routes the job to a worker, warm process or spawn/initialize a new one, entrypoint runs, then `connect()`" (lens-glm S16). lens-kimi confirms the literature does not cover this layer at all (lens-kimi §1, §5).

**Finding 1.2 ★ (2/4: lens-codex, lens-glm). The process model is a warm pool with a forkserver preload, not worker-per-job.**
`ProcPool` keeps `num_idle_processes` already-initialized job processes in `_warmed_proc_queue`; each spawn runs plugin prewarm once (lens-codex E2, `proc_pool.py:32`, warm queue `:71`, spawn-on-demand `:122-128`, `launch_job` `:168`). `_preload.py` imports `av`, builds the SSL context, and loads local inference weights; on Linux the worker calls `set_forkserver_preload(plugin_packages)` so children inherit the preload copy-on-write (lens-codex E2, `worker.py:786`, `ipc/_preload.py:1-17`). Child processes set SIGINT/SIGTERM to ignore, so the parent owns shutdown (lens-codex E2, `ipc/job_proc_lazy_main.py:8-11`). Operationally, the framework logs "no warmed process available for job, waiting for one to be created" when the pool is empty; `num_idle_processes` defaults from CPU count and `load_fnc`/`load_threshold` govern admission (lens-glm S16). This is the concrete answer to "why does `dev` spawn several processes."

**Finding 1.3 ★ (2/4: lens-codex, lens-glm). The failure modes are documented as symptoms, not internals.**
Slow joins come from cold starts on plans that scale to zero, blocking entrypoint code before `connect()` or `session.start()`, avatars whose `wait_for_join()` blocks first speech, burstable instances, and an `agent_name` mismatch with the registered worker that means the agent never joins (lens-glm S16). Tokens can 401 when join exceeds 60 seconds (lens-glm S16). lens-codex shows the reservation counter is in-process only, so load is per-process, not per-cluster (lens-codex E1). That last point is the one an operator is most likely to get wrong in a screen.

**Finding 1.4 ★ (2/4, same family: lens-glm, lens-opus). Ownership is session-centric.**
`AgentSession` is the composition root; `Agent` carries persona and, in 1.8, optional `stt`/`llm`/`tts` overrides (lens-codex E3, `agent_session.py:409`, `agent.py:47`). lens-opus states the same at the conceptual level: "the unit of design is the session, not the agent" (lens-opus §2). Codex is the stronger anchor here; the opus agreement is a same-family echo, so weight it as one source.

**Ownership table (sourced from lens-codex E1-E4, E10):**

| Object | Lives in | Owns | Lifetime |
|---|---|---|---|
| `Worker` | worker process | server websocket, availability, assignment futures, proc pool | worker process |
| `ProcPool` | worker process | warmed job processes, spawn-on-demand, `launch_job` | worker process |
| `JobProcess` | job subprocess | plugin preload, `userdata` | one job |
| `AgentSession` | job subprocess | models, turn handling, tools, `ChatContext` | one session |
| `Agent` | inside session | persona/instructions, optional model overrides | until handoff |
| `AgentActivity` | inside session | turn loop, `AudioRecognition`, `SpeechHandle`s, preemption, delegation checks | until handoff |

---

### Q2. The turn loop: what the framework does and what the plugin does

**Finding 2.1 ★ (2/4: lens-codex, lens-glm). One recognition state machine owns VAD, STT, endpointing, and turn commit.**
`AudioRecognition` owns an `_STTPipeline` (audio in, events out) plus the endpointing and turn timers. It drives `RecognitionHooks` into `AgentActivity`: `on_start_of_speech`, `on_interim_transcript`, `on_final_transcript`, `on_end_of_turn`, `on_transcription_timeout`, `on_preemptive_generation`, `on_eot_prediction` (lens-codex E4, `audio_recognition.py:294`, hooks `:161-182`, STT pump `:241`). `AgentActivity.on_end_of_turn` is deliberately synchronous so it cannot be cancelled mid-commit, and it immediately spawns `_user_turn_completed_task` (lens-codex E4, `agent_activity.py:2834`, `:2929`). The configuration surface names five detection modes: the audio `TurnDetector`, `"stt"`, `"realtime_llm"`, `"vad"`, `"manual"` (lens-glm S9).

**Finding 2.2 ★ (2/4: lens-codex, lens-glm). Endpointing is learned per user; the ceiling is fixed.**
`DynamicEndpointing` runs an exponentially weighted estimator (`alpha=0.9`) over the user's end-of-speech pauses and moves `min_delay` toward observed pausing, clamped by a fixed `max_delay`. It distinguishes `between_utterance_delay`, `between_turn_delay`, and `immediate_interruption_delay`, and updates differently on agent speech start/end and on overlapping speech (lens-codex E5, `endpointing.py:49`, learning state `:74-88`, surfaces `:97`, `:106`, `:115`). `max_delay` cannot be learned because there is no natural upper bound to observe (lens-codex E5). lens-glm adds the config floor: with the audio detector the endpointing defaults move to `min_delay` 0.3 s and `max_delay` 2.5 s, and Silero's `min_silence_duration` must be at least 0.25 s (lens-glm S7). No peer in the codex survey learns the floor from the user's own pause distribution (lens-codex divergent 5).

**Finding 2.3 ★ (2/4: lens-codex, lens-glm). Interruption is a pausable speech handle, not a kill.**
Each agent utterance is a `SpeechHandle` with priority, `allow_interruptions`, and an `_interrupt_fut` (lens-codex E6, `speech_handle.py:33`, `:201`). Interruption by audio activity is gated by a minimum word count from the live transcript, AEC warmup, and whether a realtime model already does turn detection (lens-codex E6, `agent_activity.py:2493`). With `false_interruption_timeout` set, the framework pauses audio output and can resume it instead of killing the utterance (lens-codex E6, `agent_activity.py:2540-2554`). A separate `AdaptiveInterruptionDetector` streams overlapping speech to `inference/interruption.py`, which returns a probability and a backchannel verdict, with `max_retry=0` because a late verdict is worthless (lens-codex E6, `inference/interruption.py:268`, `:125`, `:60`). lens-glm's config list matches: `enabled`, `mode` (`"adaptive"` vs `"vad"`), `false_interruption_timeout`, `resume_false_interruption`, `backchannel_boundary`, `min_words`, `min_duration`, `discard_audio_if_uninterruptible`; VAD runs first on every frame, turn detection applies when the agent is silent, interruption handling when it is speaking (lens-glm S9). Resumable false-interruption handling is rare; Pipecat models interruption as an irreversible frame (lens-codex E6, E14).

**Finding 2.4 ★ (2/4: lens-codex, lens-glm). Preemptive generation is speculative cascaded-LLM work, cancelled on transcript drift.**
On an interim transcript the activity starts a scheduled-less `_generate_reply` for the cascaded LLM path only, recording the transcript, chat context, tools, and `tool_choice` in `_PreemptiveGeneration`. `on_preflight_stale` cancels it when a later interim transcript is not equivalent to the one it was built from (`_transcripts_equivalent`) (lens-codex E7, `agent_activity.py:2753`, staleness `:2810`, equivalence check `:2820`, retry cap `:2779`). Caps are `max_speech_duration` and `max_retries`. It is skipped for `RealtimeModel` and while another speech is live and uninterrupted (lens-codex E7). lens-glm's turn-handling guide states preemptive generation is "on by default" (lens-glm S9). The explicit equivalence check is the detail that prevents a reply to a half-heard sentence being committed as if it answered the full one (lens-codex E7).

**Finding 2.5 ★ (2/4, same family: lens-glm, lens-opus). The framework's turn loop is a turn-loop story, not a cold-start story.**
Preemptive generation buys interior latency, the time between end of speech and first audio; it cannot buy time before the process exists (lens-opus §4). lens-glm's operator note is the same point from the other side: cold starts dominate slow joins on scale-to-zero plans (lens-glm S16). This is a same-family pair; the underlying claim is safe because the mechanism is separately visible in lens-codex E2.

**Framework vs plugin, by stage (lens-codex E4-E7, E13; lens-glm S7, S9):**

| Stage | Core owns | Plugin/model owns |
|---|---|---|
| VAD | consumes the VAD stream, decides whether detection or interruption applies | Silero ONNX CPU VAD with a buffered `VADStream` |
| STT | `_STTPipeline` in `AudioRecognition`, flush at turn boundaries | `STT.stream()` / `RecognizeStream` |
| Endpointing | `DynamicEndpointing` learns `min_delay`, clamps at `max_delay` | none |
| End-of-turn | selects the detection strategy, fires `on_eot_prediction`, commits synchronously | `TurnDetector` ONNX `predict_end_of_turn` (architecture disputed, Section 4) |
| LLM | `_generate_reply`, preemptive generation, tool loop | `LLM.chat()` / `RealtimeModel.session()` |
| TTS | `SpeechHandle`, playout, pause/resume | `TTS.stream()` / `synthesize()` |
| Interruption | `_interrupt_by_audio_activity`, `min_words`, AEC warmup, pause/resume | `AdaptiveInterruptionDetector` via inference |

**What the literature says about the underlying problem.** Three findings matter for the screen and none of them are LiveKit-specific:

- Acoustic plus prosodic features beat a text-only end-of-turn signal decisively: F1 0.930 at 7.8% false alarms and 400 ms median latency for acoustic+prosodic, against F1 0.292 at 74.8% false alarms for text alone; adding text to the pair made premature detection significantly worse (lens-kimi E2, arXiv:2609.11066). This is a 2026 re-derivation of a result established in 2002 (lens-kimi E2 citation chain).
- A frozen multilingual encoder plus a small per-language head transfers cheaply: zero-shot gap 0.017 AUC, about a quarter of the penalty paid for causal encoding (lens-kimi E3, arXiv:2610.39755).
- A cheap local screen plus an expensive server adjudicator is the right shape for a realtime turn loop, and the remote call costs 98 to 140 ms round trip (lens-kimi E5, arXiv:2503.23439).

**Third-party numbers for LiveKit's own turn loop exist, and they are mixed.** FireRedChat's authors measured LiveKit at 140 ms T90 barge-in with a 33.4% false barge-in rate, 70.8% average Chinese end-of-turn accuracy (45.3% on "unfinished") and 86.2% English, and 3.598 s P50 end-to-first-response latency, against FireRedChat at 170 ms and 10.2%, 96.0% and 94.9%, and 2.341 s (lens-kimi E4, arXiv:2509.06502). These are third-party, unreviewed, and the latency sample is 25 utterances. Quote them with those qualifiers or not at all.

**Cascade versus speech-to-speech is measured per stack, not settled per architecture.** Full-Duplex-Bench v3 ran six configurations through the LiveKit framework with identical audio: GPT-Realtime led Pass@1 at 0.600 with 6.89 s task completion; Gemini Live 3.1 was fastest at 4.25 s but took the turn only 78.0% of the time and produced no speech on 22% of examples; the cascaded Whisper to GPT-4o to TTS baseline had a perfect 100% turn-take rate and the worst latency at 10.12 s (lens-kimi E1, arXiv:2604.04847). LiveKit's own post refuses a verdict and ships hybrids: realtime plus separate STT, or realtime plus separate TTS (lens-glm S10). No paper isolates architecture from model, ASR, and TTS (lens-kimi §4).

---

### Q3. Tools: schema, dispatch, timeout, error, retry

**Finding 3.1 ★ (2/4: lens-codex, lens-glm). Schema derivation is real and has a repair path.**
`function_arguments_to_pydantic_model` walks `inspect.signature`, resolves hints with `get_type_hints(include_extras=True)`, pulls parameter descriptions from `docstring_parser.parse_from_object`, skips context-typed parameters (`RunContext` and subclasses), and builds a model with `create_model` (lens-codex E8, `llm/utils.py:530`, context skip `:553`). The decorator collects the description from the docstring or an explicit `description=`; `raw_schema=` bypasses derivation entirely (lens-codex E8, `llm/tool_context.py:335`, `:420`, `:445`). Argument parsing has a three-step repair path tuned for small open-weight models: strict JSON, then `json_repair`, then stripping leaked chat-template tokens such as `<|...|>` and Gemma turn markers (lens-codex E8, `llm/utils.py:568-604`). lens-glm's migration note confirms `@function_tool` replaced `@llm.ai_callable` plus `FunctionContext`, with schemas from signatures and docstrings and a `RunContext` passed to tools (lens-glm S2).

**Finding 3.2 ★★ (3/4: lens-codex, lens-glm, lens-opus). Tool execution is async, and errors are model-visible strings.**
`_ToolExecutor.execute` builds a `RunContext`, attaches it to a `first_update_fut`, and runs the tool in its own task (lens-codex E9, `tool_executor.py:329`). A tool may call `ctx.update()` to stream progress; the first update answers dispatch, and the later return is coalesced into a synthetic final output so the model never waits on a result that will not come (lens-codex E9). Exceptions are captured and returned as `ToolError` outputs rather than propagated (lens-codex E9, `:396-420`). lens-glm's async-tools post says the same in product terms: the first `ctx.update(message)` returns control to the LLM immediately, the agent voices progress while the tool runs, deferred results surface when both agent and customer are idle, and the final `return` is spoken as the closing line; `ctx.with_filler(...)` plays audio bypassing the LLM (lens-glm S11). lens-opus draws the operational consequence: an outage looks like a model-visible string unless the app logs it (lens-opus §4).

**Finding 3.3 ★ (2/4: lens-codex, lens-glm). Duplicate detection is per-session and fails open.**
Duplicate calls are keyed per tool with an `on_duplicate` mode, `allow` by default, plus a confirm mode that injects an `lk_agents_confirm_duplicate` parameter into the schema; detection is by tool name only (lens-codex E9, `tool_executor.py:359`; lens-glm S11). Dedupe state is per session, so two sessions can double-book the same side effect (lens-codex E9). lens-opus's version: duplicate detection fails open by design and the docstring delegates exactly-once to the tool body; a booking tool cancelled after the charge and then retried by a resumed turn is a correctness failure the framework will not prevent (lens-opus §4). The two descriptions agree on behavior; opus adds the failure story.

**Finding 3.4 ★★ (3/4: lens-codex, lens-glm, lens-kimi). There is no documented tool timeout or retry policy.**
lens-glm's async-tools post states plainly that timeouts and retries are not covered (lens-glm S11). lens-codex shows error capture and per-call cancellation gated on `ToolFlag.CANCELLABLE`, but no timeout or retry path in `_ToolExecutor.execute` (lens-codex E9, `:513`). lens-kimi reports that the literature studies error text and concurrency but not timeout and retry policy; the one benchmark that could have tested it used zero-latency mock APIs by design (lens-kimi §5, E1). The nearest thing to a timeout is the 1.8.7 release note: tools still running at an interruption timeout now continue asynchronously and deliver deferred results (lens-glm S12). That is an interruption timeout, not a tool deadline. Do not conflate them.

**Finding 3.5 ★ (2/4: lens-codex, lens-glm). Cancellation is opt-in and separate from interruption.**
`ToolFlag.CANCELLABLE` opts a tool into auto-exposed `get_running_tasks()` / `cancel_task(call_id)`, which raises `asyncio.CancelledError`; interrupting a tool is not cancelling it (lens-glm S11). Cancellation is per-call and gated on the flag (lens-codex E9, `:513`). This is the sentence to get right in a screen: an interruption changes who has the floor; it does not by itself abort the work.

**Finding 3.6 ★ (2/4: lens-codex, lens-opus). A handoff after a progress update is treated as a bug.**
The handoff-after-`ctx.update()` guard converts returning an `Agent` after a progress update into a `ToolError` (lens-codex E9, `tool_executor.py:428-440`). lens-opus's session-scoped versus activity-scoped tool distinction is the same design pressure from the other side: activity tools are cancelled or awaited on swap, `AsyncToolset` tools survive (lens-opus §4). The codex claim is code-anchored; the opus claim is at `a77605e` and is the weaker anchor.

**Finding 3.7 ★ (2/4: lens-codex, lens-kimi). The framework ships a model-graded supervision layer over its own tool calls.**
After a user turn, if the model did not call a tool, `DelegationDetector` asks a decision model (default `google/gemma-4-31b-it`) a single probability question built from a long policy prompt: did the caller ask for something only the backend can do, and has no tool call done it yet. A `grace_period` of 0.5 s lets the model delegate on its own first; `max_retry=0` because a late verdict is useless (lens-codex E12, `delegation.py:1-45`, `:73`, `:82`, `:67`; `decisions.py:29-62`). No surveyed peer does this; Pipecat leaves it to app code (lens-codex E12). lens-kimi's tool-reliability literature explains why a framework might: single-turn function calling is close to solved while memory, dynamic decision-making, and long-horizon reasoning remain weak (lens-kimi E7, BFCL).

**What the literature adds that the framework cannot absorb.** The tool error string is part of the API surface. Adding a terminal command to an expired-credentials error drove recovery from 82% down to 45% across five models; naming the available tool instead restored it to 84%; deleting the step and keeping the cause restored it to 82% (lens-kimi E8, arXiv:2609.35381). On rate limits, "Wait before retrying." produced 6% recovery while "Wait a few seconds and call tool again" produced 88% (lens-kimi E8). A framework that wraps tool exceptions can choose this text on the developer's behalf (lens-kimi E8). Async dispatch is also a measured latency win: 1.6x to 5.4x lower end-to-end latency for about 20 extra tokens, about 90 ms added latency, and 27.5 MB of KV cache on a 3B model (lens-kimi E9, arXiv:2412.07017).

---

### Q4. State: chat context, userdata, what survives a restart

**Finding 4.1 ★★ (3/4: lens-codex, lens-glm, lens-opus). `ChatContext` is a typed, in-memory item log.**
`ChatContext` holds discriminated items: `message`, `function_call`, `function_call_output`, `agent_handoff`, `agent_config_update`. It can be copied with pruning (`exclude_image`, `exclude_audio`, `exclude_function_call`), serialized to a dict with the same exclusions, or converted to protobuf (lens-codex E10, `chat_context.py:407`, `:456`, `:494`, `:529`, `to_dict` `:773`, `from_dict` `:1056`, `to_proto` `:1061`). Nothing writes it to durable storage: it lives in memory on the `AgentSession`, and the process pool disposes the process after the job (lens-codex E10). lens-glm's secondary sources point the same way: the only public material on memory pushes persistence into Supabase or MongoDB, and reconnect behavior is a separate feature (lens-glm secondary list). lens-opus states it as "session state lives in memory" and "tasks do not persist" (lens-opus §2, §4). lens-kimi has no framework-level source, but its state-degradation work explains why this matters: long conversations fail mostly through unreliability, not incapacity (lens-kimi E10, E11).

**Finding 4.2 ★ (2/4: lens-codex, lens-opus). `userdata` is application state, deliberately outside the model's context.**
`userdata` is typed generically and lives on `JobProcess` (`job.py:1033`). It is not the chat history and is not in `ChatContext` (lens-codex E3, E10). lens-opus treats the same split as a design principle: the session owns conversation state, the app owns application state (lens-opus §2). The codex anchor is decisive; opus is a same-family echo here only in framing, not in code.

**Finding 4.3 ★★ (3/4: lens-codex, lens-glm, lens-opus). Nothing survives process death.**
The process pool disposes the job process after the job, and no lens found a framework path that serializes `ChatContext` or `userdata` to durable storage (lens-codex E10; lens-glm gap 5; lens-opus §4). lens-glm is explicit that "what survives a restart is stated nowhere" and that the in-memory conclusion is inference from the negative space, not documentation (lens-glm gap 5). That inference is now code-corroborated by lens-codex, which is why the finding is stated as high confidence rather than as documentation. The honest formulation: as of 1.8.12, the framework has no durable session store; restart survival is the application's job.

**Finding 4.4 ★ (2/4: lens-kimi, lens-codex). State representation is a framework lever, not only a model property.**
On an identical 60-item state-tracking set, changing the output protocol moved accuracy from 31.7% to 88.0%; pooled core accuracy was 31.7% against 84.9% for single-step arithmetic (lens-kimi E11, arXiv:2603.27343). Multi-turn degradation is mostly unreliability: average performance fell 39% across six tasks, but the aptitude drop was about 16% and statistically insignificant while unreliability more than doubled (lens-kimi E10, arXiv:2505.06120). lens-codex's `ChatContext` design is the corresponding lever: typed items, pruning, and protobuf serialization are choices about what the model sees (lens-codex E10). The link between the two is synthesis judgment, not a claim either lens makes.

**State table (lens-codex E10, E3; lens-opus §4; lens-glm gap 5):**

| State | Where it lives | In model context? | Survives process death? |
|---|---|---|---|
| `ChatContext` | `AgentSession` memory | yes | no |
| `userdata` | `JobProcess` | no | no |
| tool dedupe state | session | no | no |
| tasks | activity/session | as chat items | no |
| external memory | app's store | via retrieval | yes, app-owned |

---

### Q5. Handoffs and multi-agent composition

**Finding 5.1 ★★ (3/4: lens-codex, lens-glm, lens-opus). A handoff is an in-session activity swap, not a new process.**
`AgentSession.update_agent` rejects an agent already bound to another activity, sets `_new_turns_blocked` on the current activity immediately, then creates `_update_activity_task`, which drains the old activity, transfers reusable resources (STT/TTS streams where possible), and starts the new `AgentActivity` (lens-codex E11, `agent_session.py:1813`, `:1901`, `:1824`). `AgentTask` subclasses `Agent`, is awaitable, and can be completed or cancelled (lens-codex E11, `agent.py:890`). Resource reuse across activities avoids a second STT/TTS connect (lens-codex E11, `agent_activity.py:1111`). lens-glm's handoff guide describes the user-facing form: a triage agent's `@function_tool` returns a new `Agent` instance, which triggers automatic handoff, with context passed via `self.chat_ctx.copy(exclude_instructions=True)` (lens-glm S15). lens-opus states the same at the design level: a handoff drains the old activity and swaps the agent inside the live session rather than restarting anything (lens-opus §2).

**Finding 5.2 ★ (2/4: lens-glm, lens-codex). Tasks and task groups are the in-agent composition primitive, and they live under `beta`.**
One supervisor `Agent` holds long-lived session control; Tasks are short-lived, focused units that run inside the agent, temporarily take the conversation, and return typed results (`AgentTask[TriageResult]`, `self.complete()`). There is no agent swap, no routing LLM hop, and no latency penalty; context flows via `chat_ctx` (lens-glm S14). `TaskGroup` (from `livekit.agents.beta.workflows`) chains sequential tasks with shared context: `task_group.add(lambda: TriageTask(...), id="triage")`, results via `results.task_results["triage"]` (lens-glm S14). The beta module path is the load-bearing detail: this surface is not stable API. lens-codex shows the same primitive from the inside as `AgentTask`, an awaitable `Agent` subclass (lens-codex E11).

**Finding 5.3 ★ (2/4: lens-glm, lens-codex). Warm human transfer is a task, not a handoff.**
`WarmTransferTask` (from `livekit.agents.beta.workflows`) holds the caller, opens a private consultation room where the AI briefs the human, then hands over and disconnects the AIs (lens-glm S15). This is a task-shaped workflow rather than an `update_agent` swap, which is why it can hold a third party in a separate room while the original session stays alive. The codex survey does not cover `WarmTransferTask` specifically, so the star count is 2 but the second lens is only corroborating the task primitive, not this workflow.

**Finding 5.4 ★ (2/4: lens-codex, lens-opus). Handoffs are asynchronous, with a drain window.**
A handoff is asynchronous and leaves a window where the old activity is muted but still draining; code that assumes the swap is immediate will race (lens-codex E11). lens-opus names the operational consequence: tools attached to an `Agent` are activity-scoped and are cancelled or awaited during a handoff, while tools in an `AsyncToolset` are session-scoped and survive the swap (lens-opus §4). Put work that must survive a handoff in a session-scoped toolset (lens-opus §6 rec 5). The codex anchor is `_update_activity_task`; the opus claim is at `a77605e` and should be re-checked at `cbb5770` before quoting in the screen.

**Finding 5.5 ★ (2/4: lens-kimi, lens-glm). The governance literature says a handoff guard needs its own evaluation.**
Maat, a deterministic contract layer between agents, caught some handoff defects but produced a 37% false-alarm rate on governed halts, and when unattributed halts count as failed work the governed arm underperforms the ungoverned arm in four of six workflows; the paper withdrew its own v1 claim that gains held in all six (lens-kimi E12, arXiv:2609.34017). A deterministic, non-LLM validator is the only reproducible kind, and it still needs evaluation (lens-kimi E12). lens-glm's docs show LiveKit's own handoff machinery is exactly this kind of layer, with context copied and instructions excluded (lens-glm S15). The implication: if LiveKit adds stricter handoff validation, the false-alarm cost is a real design risk, not a hypothetical.

**Finding 5.6 ★ (2/4: lens-kimi, lens-opus). Communication, not task decomposition, is the organising problem.**
The multi-agent survey separates system-level communication (architecture, goals, protocols) from internal communication (strategies, paradigms, objects, content) and concludes the field's benchmarks do not measure communication (lens-kimi E13, arXiv:2502.14321). lens-opus maps that onto the framework: task groups are system-level orchestration of concurrent tasks, transfers are internal communication between agents, and agent-to-agent communication is a floor-authority problem (lens-opus §6 rec 7). The vocabulary is the reusable artifact; it is a survey, one source, not a body of evidence (lens-kimi E13).

---

### Q6. Where the framework is thin: the three JD-named gaps

**Finding 6.1 ★★ (3/4: lens-glm, lens-kimi, lens-opus; lens-codex silent). Text modality has no runtime.**
v1 moved chat onto the `lk.chat` topic and transcriptions to `lk.transcription` text streams (lens-glm S2). The JD's "channels such as SMS" phrasing implies a multi-channel abstraction that no fetched docs page describes (lens-glm S17, gap 3). A text channel has no VAD, no audio clock, and minutes or hours between turns; tasks do not persist and session state is in memory, so an SMS thread is a different runtime, not a different transport, and process-per-call is a poor fit for a conversation that sleeps for an hour (lens-opus §4). The academic literature treats voice and text as separate modalities with separate evaluation cultures and barely compares them (lens-kimi §5). lens-codex did not survey a text path, which is itself consistent with the absence.

**Finding 6.2 ★★★ (4/4: lens-glm, lens-codex, lens-kimi, lens-opus). Durable workflows have no shipped session lifecycle; one experimental plugin exists.**
No docs page, blog post, or GitHub issue found in the breadth sweep describes how LiveKit Agents would persist or resume agent execution across process death; the only adjacent public material pushes persistence outside the framework (lens-glm gap 1). lens-codex found `livekit-durable`, a plugin that serializes a live generator or coroutine by reading its frame instruction pointer, stack, and block stack through a native extension, and validates a code hash on deserialize; nothing else in the survey does this, and it is unused by core (only its own tests import it), so it is an experiment, not a shipped answer (lens-codex divergent 2, `livekit-durable/livekit/durable/function.py:80-175`). lens-opus found the same package on PyPI at 0.1.0, published 2025-12-19, with integration depth undocumented (lens-opus §5). The systems literature supplies the two candidate mechanisms and says neither has been evaluated on an agent harness: event-log replay with deterministic execution (lens-kimi E14, arXiv:2103.00033) versus speculative execution with distributed rollback, which removes the determinism requirement at the cost of more aborted work and slower recovery (lens-kimi E15, arXiv:2412.13314). An LLM turn loop is hostile to deterministic replay unless every model call is pinned to a recorded result (lens-kimi E14, E15; lens-opus §3).

**Finding 6.3 ★★★ (4/4: lens-glm, lens-codex, lens-kimi, lens-opus). Agent-to-agent communication stops at in-session handoff.**
Everything public describes one session, one process, with handoffs and beta task groups inside it; no protocol for cross-room or cross-agent-system messaging appears in the fetched docs (lens-glm gap 2). lens-codex's `DelegationDetector` is in-session supervision, not inter-agent messaging, and the handoff primitive is an activity swap inside one session (lens-codex E12, E11). The literature is thin: one primary multi-agent study that retracts its headline claim, plus one survey (lens-kimi E12, E13, §5). lens-opus frames the gap as floor authority: the hard question is which agent holds the floor and how that authority transfers, not how bytes move between processes (lens-opus §6 rec 7).

**Finding 6.4 ★ (2/4: lens-glm, lens-opus). The three gaps are posting-scoped, not a company-wide admission.**
The Enlistar mirror of the posting quotes "Help define the text modality of the framework, enabling agents to interact with users across channels such as SMS", "Design abstractions for stateful and durable agentic workflows", and "Shape agent-to-agent communication and delegation patterns" (lens-glm S17). The currently-live senior variant on Ashby contains none of those phrases (lens-glm S18). Treat the three gaps as scoped to the posting being interviewed for, and verify which posting that is before leading with them (lens-glm dissent 4). lens-opus treats them as seams rather than missing features, which is compatible with either posting (lens-opus §6 rec 7).

---

## 4. Disputed findings

No vote rule. Each dispute is reported with both sides, a judgment about why the lenses disagree, and the evidence that would settle it.

### D1. What the shipped turn detector actually is

**The disputed claim:** whether LiveKit's production end-of-turn model works from transcript text or from audio plus semantics.

- **Side A (audio plus semantics, vendor primary).** TurnDetector v1 "abandons transcript-based detection": an audio encoder feeds a learned adapter and a fine-tuned LLM semantic branch, while a second encoder feeds a recurrent acoustic branch; the two are fused into one end-of-turn prediction, using only the current user turn and no chat context (lens-glm S6, 2026-06-17).
- **Side B (text side).** lens-kimi's entry on text-side end-of-turn says "LiveKit's shipped turn detector is a text-side model", and lens-opus says "LiveKit's shipped model works on the transcript side", anchoring the claim in the December 2024 135M-parameter SmolLM v2 transformer over a sliding four-turn window (lens-kimi E2; lens-opus §1, §2).
- **Side C (the code is ambiguous).** The plugin downloads a q8 ONNX model and runs `predict_end_of_turn` inside the shared inference subprocess (lens-codex E13). The starter template comment says the detector "listens to the user's audio directly, combining semantic understanding with acoustic cues", while core still carries a TODO for multimodal end-of-utterance at `agent_activity.py:2838` (`feat/AGT-2520-multimodal-EOU`); lens-codex says to treat the comment as a product claim, not an implementation fact (lens-codex E15).

**Judgment: anachronism plus a real migration question, in that order.** There are at least three generations in play. The December 2024 model was text (SmolLM v2, 135M, four-turn window). A later text `MultilingualModel` (Qwen2.5-0.5B, 396 MB, 50 to 160 ms per turn, needs STT transcripts) is deprecated for removal in 2.0 (lens-glm S7). The June 2026 v1 is audio plus semantics (lens-glm S6). lens-kimi and lens-opus describe generation one or two; lens-glm describes generation three. That is a time-scale disagreement, not a factual one. What remains genuinely open is Side C: lens-codex's TODO is evidence that the core feature gate for multimodal EOU still exists at 1.8.12, so either the TODO is stale or the shipped ONNX is not the full described v1. No lens opened the model graph. **Do not assert either architecture in the screen without naming the version and the artifact you read.**

**What would resolve it:** read `livekit-plugins-turn-detector` at `cbb5770`, inspect whether the ONNX inputs are audio features or token ids, and compare with the `livekit/turn-detector` model card; then ask the product lead which model 1.8.12 ships by default and whether the multimodal-EOU TODO is live.

### D2. Endpointing defaults: 0.3/2.5 versus 0.5/3.0

- **Side A:** with the audio detector, endpointing defaults move to `min_delay` 0.3 s and `max_delay` 2.5 s (lens-glm S7, docs rendered 2026-11-14).
- **Side B:** the tuning docs recommend `inference.TurnDetector()` with fixed endpointing at 0.5 s minimum and 3.0 s maximum (lens-opus §2, page undated).

**Judgment: different objects and possible page drift.** Side A describes the detector's own defaults; Side B describes a recommended starting configuration. The two can both be true. lens-glm's is the more specific and more recent claim, so use 0.3/2.5 for "what the framework does" and 0.5/3.0 only if the tuning page labels it as a recommendation. There is a second, quieter tension inside lens-glm: a February 2026 FAQ recommends STT endpointing as the default for most production agents, while the current docs make the audio TurnDetector the session default (lens-glm S8 vs S7). Both are LiveKit positions four months apart.

**What would resolve it:** fetch the tuning page on a date and check whether 0.5/3.0 is labeled "default" or "recommended", and read the `DynamicEndpointing` constants at `cbb5770`.

### D3. Cold start: spawn per dispatch versus pre-warmed pool

- **Side A:** the agent server "boots a job subprocess which joins the room" when a dispatch request arrives (lens-opus §4, quoting the agents overview).
- **Side B:** the process pool keeps `num_idle_processes` already-initialized processes; a job is handed to a warm process, and a new one is spawned only when none is available, with the log line "no warmed process available for job, waiting for one to be created" (lens-codex E2; lens-glm S16).

**Judgment: a granularity difference, not a contradiction, and codex wins on internals.** The docs compress "assign a pre-warmed process" into "boots a subprocess", which is true of the process but misleading about the latency. The operational conclusion in Side A survives and is actually reinforced by Side B: cold start is real when the pool is empty or the plan scales to zero, and preemptive generation cannot help before a process exists. In the screen, use the codex mechanism and keep the opus warning.

**What would resolve it:** trace `ProcPool.launch_job` at `cbb5770` with an empty warm queue and time the spawn, and check whether LiveKit Cloud's default plan pre-warms.

### D4. Whether the three gaps are LiveKit's own admission

- **Side A:** the Enlistar mirror names text modality, durable workflows, and agent-to-agent communication as the role's frontier (lens-glm S17).
- **Side B:** the currently-live senior variant on Ashby names none of them (lens-glm S18).

**Judgment: posting-scoped, not company-wide.** Both are LiveKit postings; only one names the gaps. Any sentence of the form "LiveKit admits X is missing" must be scoped to the specific posting. This is a qualification, not a contradiction, and it is the kind of thing a product lead will notice if you get it wrong.

**What would resolve it:** confirm which posting the screen is for, and read that posting's exact text.

### D5. Turn-detection benchmark superiority

- **Side A:** LiveKit's v1 detector reaches 9.9% false-cutoff at a 300 ms budget, against Deepgram Flux at 12.9% and ultraVAD at 27.7%, and 543 ms mean latency at a 5% false-cutoff target (lens-glm S6, vendor benchmark on LiveKit's own `eot-bench`).
- **Side B:** a WebRTC consultancy says the frameworks are "closer in practice than marketing implies" and advises prototyping both on your own audio (lens-glm S20); a practitioner thread reports open-source turn-taking models failing outside clean short sentences (lens-glm S19); FireRedChat's third-party measurement puts LiveKit's false barge-in at 33.4% against its own 10.2% and LiveKit's Chinese "unfinished" accuracy at 45.3% (lens-kimi E4).

**Judgment: different metrics on different test sets, plus vendor-versus-third-party instrumentation.** Side A measures false cutoff at a latency budget on LiveKit's own benchmark. Side B measures false barge-in and per-language end-of-turn accuracy on other test sets. These are not one league table. The vendor benchmark is also unreplicated. The dispute is partly definitional and partly real: definitional because the metrics differ, real because the third-party numbers are worse than the marketing implies.

**What would resolve it:** run both detectors on a common test set with a common protocol, and publish per-language "unfinished" accuracy next to false barge-in. Until then, quote each number with its test set and sample size.

### D6. Cascade versus speech-to-speech

- **Measured trade-off:** the cascade had the best turn-take rate (100%) and the worst latency (10.12 s); GPT-Realtime led Pass@1 at 0.600 with 6.89 s; Gemini Live 3.1 was fastest at 4.25 s but spoke on only 78% of turns (lens-kimi E1).
- **Vendor position:** no universal winner; realtime has a structural latency edge and better voice quality, cascade wins control, modularity, debugging, per-layer cost, 8 kHz telephony, and EU residency; LiveKit supports hybrids (lens-glm S10).
- **Practitioner dissent:** "STT to LLM to TTS is a dead end", countered in the same thread by auditability needs (lens-glm S19).
- **Narrative reading:** the orchestration layer may be transitional, but a realtime model still needs a room, a job, a process boundary, a tool runtime, and a state model (lens-opus §3).

**Judgment: genuinely unsettled, and the lenses agree on the unsettledness rather than on an outcome.** No paper isolates architecture from model, ASR, and TTS (lens-kimi §4). The honest screen answer is that the trade-off is measured per stack, not settled per architecture, and that LiveKit's own position is to support both.

**What would resolve it:** a production turn-level ledger stratified by modality, as lens-opus proposes; or a controlled study that holds model, ASR, and TTS constant while varying only the architecture.

### D7. Is the framework layer transitional?

- **Dissent:** if the model owns turn-taking, VAD, endpointing, ASR, and TTS orchestration become compatibility shims, and durable value sits in the model vendor's weights (lens-opus §3, steel-manning Moshi and the Realtime API).
- **Response:** control, observability, cost, vendor lock, tool execution, and transport do not come from the model, and value migrates from orchestration to lifecycle and state (lens-opus §3).

**Judgment: a live hypothesis, not a finding.** No lens asserts the framework dies; lens-glm's vendor post explicitly refuses a verdict and ships both paths (lens-glm S10). Treat this as an open question with a clean falsifier: if realtime share passes 50% and keeps rising, the orchestration layer is transitional; if cascaded stays above 70% after two years of speech-to-speech models, the control argument wins (lens-opus §5).

---

## 5. Cross-lens gaps

Items here are either absent from every lens or covered by only one lens and therefore fragile. The first four are genuine field gaps; the rest are coverage gaps that a screen could expose.

1. **Durable workflows for a nondeterministic agent loop are unsolved in public.** No docs page, no architecture post, no GitHub issue describes how LiveKit Agents would persist or resume a session across process death (lens-glm gap 1). The only artifact is an experimental plugin not wired into core (lens-codex divergent 2; lens-opus §5). The systems literature offers replay and speculative rollback, and no published work evaluates either on an agent harness (lens-kimi §4). This is the single largest gap between what the JD says matters and what the public record documents.
2. **Text modality as an architecture is absent.** No fetched docs page describes a multi-channel abstraction; the literature barely compares voice and text; lens-codex did not survey a text path (lens-glm gap 3; lens-kimi §5; lens-opus §4). The JD's SMS phrasing is the only signal (lens-glm S17).
3. **Cross-process agent-to-agent communication has no protocol.** Everything public is one session, one process, with handoffs and beta task groups (lens-glm gap 2; lens-codex E11, E12). The literature is one retracting study plus one survey (lens-kimi E12, E13).
4. **Restart survival semantics are stated nowhere in the docs.** The in-memory conclusion is inference from the negative space in lens-glm and code-corroborated by lens-codex, not documented behavior (lens-glm gap 5; lens-codex E10).
5. **The worker registration handshake and process model have no prose source.** lens-glm S5 defers to sub-pages and S16 documents symptoms; lens-kimi says the literature does not cover it; only lens-codex answers it (lens-codex E1, E2; lens-glm gap 4; lens-kimi §5).
6. **SDK 2.0 is undocumented at breadth level.** 2.0.0b1 exists on PyPI (2026-10-12) and the docs promise text-detector removal in 2.0, but no 2.0 announcement, migration guide, or changelog summary surfaced (lens-glm S13, S7, gap 6). No lens read 2.0 code.
7. **Tool timeout and retry policy is unserved by both the framework docs and the literature.** lens-glm S11 says timeouts and retries are not covered; lens-kimi §5 says the same for the literature; lens-codex found error capture and cancellation but no timeout path (lens-codex E9).
8. **No independent production postmortems of LiveKit Agents.** Case studies are vendor-published; the only independent operational signal is an HN thread about a system built without LiveKit (lens-glm gap 7, S19).
9. **No third-party measurement of the framework's own overhead.** "Tens of ms" is vendor-supplied and echoed by a consultancy (lens-glm gap 8, S20).
10. **The turn-taking literature is telephone-domain and systematically excludes overlap.** Entry 3 excludes active overlaps (40% English/Mandarin to 55% Japanese), entry 2 trains on segments with under 10% opposing-channel activity, and entry 5's automatic labels reach only 76.1% agreement on gaps (lens-kimi §4). WebRTC agent traffic is a different channel, and no paper in the lens evaluates on it.
11. **Cross-session memory is an external-store pattern, not a framework primitive.** The only public material is blog posts about Supabase and MongoDB (lens-glm secondary list). No lens found a framework API for it.
12. **Multi-tenant isolation is a consequence of process-per-job, not a designed feature.** One process per job gives good isolation but no shared cache and a cold start per conversation (lens-opus §4). No lens found a tenancy model beyond that.

---

## 6. Unverified: load-bearing claims no lens could source

Everything below is stated as unverified. Do not present any of it as fact in the screen. Each item names what would settle it.

1. **The internal architecture of the shipped turn-detector ONNX at 1.8.12.** lens-glm S6 describes the June 2026 v1 as audio plus semantic plus acoustic fused. lens-codex E13 shows the plugin runs `predict_end_of_turn` on a q8 ONNX. No lens inspected the model graph or confirmed which inputs the shipped artifact takes. **Unverified: whether the shipped ONNX is the full v1 architecture or a partial/text variant.**
2. **The value and behavior of `ASSIGNMENT_TIMEOUT`.** lens-codex cites the constant and the wait path but not its value or what happens to the reservation when it expires. **Unverified: timeout duration and the decline/retry path.**
3. **The exact `num_idle_processes` default formula.** lens-glm S16 says it is derived from CPU count; lens-codex does not state the formula. **Unverified: the exact expression and whether it is capped.**
4. **What happens to an in-flight tool call when the user interrupts.** lens-glm S12 (1.8.7) says tools still running at an interruption timeout continue asynchronously and deliver deferred results; lens-codex E9 shows cancellation gated on `ToolFlag.CANCELLABLE` and errors returned as outputs. No lens traced the combined state machine for "interrupt during tool, tool raises, turn resumes". **Unverified: the exact ordering and whether a resumed turn can adopt a deferred result.**
5. **Whether the framework ever serializes session state, for example on reconnect.** lens-codex E10 says nothing writes `ChatContext` to durable storage; lens-glm's reconnect post is title-only. No lens read the reconnect code path. **Unverified: whether any reconnect path persists or replays state.**
6. **`livekit-durable` integration depth.** lens-codex says only its own tests import it; lens-opus says integration depth is undocumented. **Unverified: whether it works end to end with `AgentSession`, and whether it is supported.**
7. **What 2.0.0b1 changes.** No lens read the 2.0 code, changelog, or migration guide. **Unverified: turn-detector architecture, tool API, and state model in 2.0.**
8. **The framework's own latency and cost overhead.** No third-party measurement exists; the "tens of ms" figure is vendor-supplied and echoed (lens-glm gap 8). **Unverified: the framework's contribution to end-to-end latency.**
9. **The `agent_name` mismatch failure path.** lens-glm S16 documents the symptom (agent never joins); lens-codex does not trace the registration and name-matching code. **Unverified: where the mismatch is detected and what the worker logs.**
10. **`max_tool_steps` failure behavior.** lens-codex E3 cites `max_tool_steps=3`; lens-glm S12 says 1.8.6 enforced it on the realtime tool loop. Neither states what happens when the cap is hit (raise, stop, or a model-visible error). **Unverified: the cap's failure semantics.**
11. **Whether the turn detector's `eot-bench` numbers replicate.** The benchmark is LiveKit's own, the numbers are unreplicated, and the vendor is the interested party (lens-glm S6). **Unverified: independent replication on the same test set.**
12. **Whether the three JD gaps reflect the team's current roadmap or one posting's framing.** The two postings disagree (lens-glm S17, S18). **Unverified: which posting is current for the screen, and what the team actually plans to ship.**

---

## 7. Numbered falsifiable recommendations for the screen

Each item is an action, the lens support, and the condition under which it would be wrong. "Falsifiable" here means a future reader can check it against a repo, a doc, or an experiment.

1. **Say the version out loud before the first internals claim: "This is `livekit-agents` 1.8.12, commit `cbb5770`, unless we are talking about 2.0."**
   Support: lens-codex header; lens-glm S13. Wrong if: the interview is explicitly about 2.0.0b1 or `main`, in which case the entire 1.8.x frame is the wrong frame.

2. **Describe dispatch as a two-phase availability-then-assignment handshake over the worker websocket, with slot reservation before the user callback.**
   Support: lens-codex E1; lens-glm S16. Wrong if: a later release replaces the websocket handshake, or the deployment uses a dispatch mode that bypasses the worker (check `request_fnc` and dispatch rules at the pinned ref).

3. **Describe the process model as a pre-warmed pool with a forkserver preload, not as spawn-per-job.**
   Support: lens-codex E2; lens-glm S16. Wrong if: the deployment runs with `num_idle_processes=0` or on a scale-to-zero plan, in which case the observable behavior is effectively spawn-per-job and cold start dominates.

4. **Say "the framework commits the turn synchronously, and preemptive generation is speculative cascaded-LLM work cancelled on transcript drift" rather than "the framework predicts what the user will say".**
   Support: lens-codex E4, E7; lens-glm S9. Wrong if: the deployment is realtime-only, where preemptive generation is skipped because the provider already streams (lens-codex E7).

5. **Say "interruption changes who has the floor; it does not cancel the tool" and name `ToolFlag.CANCELLABLE` as the opt-in.**
   Support: lens-glm S11; lens-codex E9. Wrong if: a later release makes cancellation the default on interruption (check releases after 1.8.12).

6. **Say "errors are returned as `ToolError` outputs, duplicate detection is per-session and fails open, and there is no documented timeout or retry policy".**
   Support: lens-codex E9; lens-glm S11; lens-opus §4. Wrong if: 1.8.x ships an idempotency ledger keyed by tool call id or a documented tool deadline (check `tool_executor.py` at a later ref).

7. **Say "session state is in-memory `ChatContext` plus `JobProcess.userdata`; neither is durable; external stores are the documented pattern".**
   Support: lens-codex E10, E3; lens-glm gap 5; lens-opus §4. Wrong if: `livekit-durable` is wired into core or a reconnect path serializes state (both currently unverified, Section 6 items 5 and 6).

8. **Describe handoffs as an in-session activity swap that blocks new turns, drains, reuses STT/TTS, and swaps the activity, with tasks under `beta.workflows`.**
   Support: lens-codex E11; lens-glm S14, S15; lens-opus §2. Wrong if: a future release makes agents independently addressable across processes or promotes `TaskGroup` out of `beta`.

9. **Treat the three JD gaps as seams: text is a channel-and-durability problem, durable workflows are an effect-boundary problem, agent-to-agent communication is a floor-authority problem.**
   Support: lens-opus §6 rec 7; lens-glm gaps 1 to 3; lens-kimi E14, E15. Wrong if: the product lead says any of the three is already solved; then ask for the design doc and the version it shipped in.

10. **Quote the third-party turn-loop numbers with their test set and sample size, and never put eot-bench, TEN_Turn_Detection, and Full-Duplex-Bench in one table.**
    Support: lens-kimi E4, §4; lens-glm S6, S20. Wrong if: you have re-run the detectors on a common test set with a common protocol (then you can compare, and you should say so).

11. **Do not claim the detector is text-only or audio-only without naming the generation and the artifact you read.**
    Support: lens-glm S6 versus lens-kimi E2 and lens-opus §2; lens-codex E13, E15. Wrong if: you have read the 1.8.12 ONNX inputs and the model card, in which case state what you found and the version.

12. **Use the consensus-on-unsettledness framing for cascade versus speech-to-speech: the trade-off is measured per stack, not settled per architecture.**
    Support: lens-kimi E1, §4; lens-glm S10; lens-opus §3. Wrong if: you assert a winner without a controlled study that holds model, ASR, and TTS constant.

13. **Ask the product lead the version-frame question early: "which of these behaviors is 1.8.12, which is 2.0 beta, and which is only on main?"**
    Support: lens-glm S13 (2.0.0b1 exists), S7 (2.0 removal promised); lens-codex pin. Wrong if: the role is scoped to one shipping line and 2.0 is out of scope; then the question is noise.

14. **Do not claim exactly-once tool effects. Say the framework fails open and the tool body owns idempotency.**
    Support: lens-codex E9; lens-opus §4. Wrong if: you can point at an idempotency key in the framework that survives session loss (none found).

15. **When asked "what would you fix first", answer with the effect boundary, not the model: a durable session store keyed by session id plus an idempotency ledger keyed by tool call id.**
    Support: synthesis judgment across lens-codex E10, lens-glm gap 1, lens-kimi E14/E15, lens-opus §6. Wrong if: the team's roadmap already has both, in which case ask what blocks them (likely model nondeterminism, per lens-kimi E14).

---

## 8. Source manifest

Every URL cited across the four lenses and this synthesis, grouped by lens. Marked `[read]` where the lens reports fetching and reading the content, `[title]` where only the title and date were verified.

### lens-glm (breadth, 21 primary entries plus secondary leads)

Primary:
- [read] https://livekit.com/blog/livekits-series-b
- [read] https://docs.livekit.io/reference/migration-guides/v0-migration/python
- [read] https://docs.livekit.io/agents
- [read] https://docs.livekit.io/agents/logic
- [read] https://docs.livekit.io/agents/server
- [read] https://livekit.com/blog/solving-end-of-turn-detection
- [read] https://docs.livekit.io/agents/logic/turns/turn-detector
- [read] https://livekit.com/blog/turn-detection-voice-agents-vad-endpointing-model-based-detection
- [read] https://livekit.com/blog/turn-detection-and-interruption-handling
- [read] https://livekit.com/blog/realtime-vs-cascade
- [read] https://livekit.com/blog/async-tools-voice-agents
- [read] https://github.com/livekit/agents/releases
- [read] https://pypi.org/project/livekit-agents/
- [read] https://livekit.com/blog/supervisor-pattern-voice-agents
- [read] https://livekit.com/blog/handoff-pattern-voice-agents
- [read] https://livekit.com/blog/agent-join-latency
- [read] https://app.enlistar.com/job-openings/6ad62f69f66c64482fc9b9f2
- [read] https://jobs.ashbyhq.com/livekit/1757f49e-7e19-4c45-85f7-e4637dff66fb
- [read] https://news.ycombinator.com/item?id=47224295
- [read] https://rtcleague.com/blogs/livekit-vs-pipecat-ai-voice-agents
- [read] https://livekit.com/blog/best-open-source-voice-and-video-ai-agent-frameworks

Secondary leads (title and date verified, content not read):
- [title] https://livekit.com/blog/supabase-voice-agent-memory
- [title] https://livekit.com/blog/mongodb-voice-agent-memory
- [title] https://livekit.com/blog/keep-your-agent-in-the-room-on-reconnect
- [title] https://livekit.com/blog/diagnosing-blocked-event-loops
- [title] https://livekit.com/blog/configuring-livekit-agents-at-dispatch-with-metadata
- [title] https://livekit.com/blog/short-utterances
- [title] https://livekit.com/blog/understand-and-improve-agent-latency
- [title] https://livekit.com/blog/how-to-build-a-production-ready-ai-voice-agent
- [title] https://livekit.com/blog/how-clerk-ai-scales-enterprise-voice-agents-with-livekit
- [title] https://livekit.com/blog/retell-vs-vapi-vs-livekit
- [title] https://livekit.com/blog?page=2
- [title] https://livekit.com/blog?page=3

### lens-kimi (literature, 15 entries)

- [read] https://arxiv.org/abs/2503.04721 (Full-Duplex-Bench, ASRU 2025)
- [read] https://arxiv.org/abs/2510.07838 (Full-Duplex-Bench v2, ACL 2026)
- [read] https://arxiv.org/abs/2604.04847 (Full-Duplex-Bench v3)
- [read] https://arxiv.org/abs/2609.11066 (Less can be More)
- [read] https://arxiv.org/abs/2610.39755 (Multilingual EOT, frozen encoders)
- [read] https://arxiv.org/abs/2509.06502 (FireRedChat)
- [read] https://arxiv.org/abs/2503.23439 (SpeculativeETD, ACL 2026)
- [read] https://arxiv.org/abs/2410.00037 (Moshi)
- [read] https://arxiv.org/abs/2609.35381 (MCP error messages)
- [read] https://arxiv.org/abs/2412.07017 (AsyncLM)
- [read] https://arxiv.org/abs/2505.06120 (LLMs Get Lost in Multi-Turn Conversation)
- [read] https://arxiv.org/abs/2603.27343 (WMIP)
- [read] https://arxiv.org/abs/2609.34017 (Maat)
- [read] https://arxiv.org/abs/2502.14321 (Beyond Self-Talk, FCS, review)
- [read] https://arxiv.org/abs/2103.00033 (Netherite)
- [read] https://arxiv.org/abs/2412.13314 (libDSE)
- [read] https://arxiv.org/abs/2507.23159 (FDB v1.5, overlap handling)
- [read] https://arxiv.org/abs/2602.06053 (PersonaPlex)
- [read] https://arxiv.org/abs/2603.11394 (multi-turn reliability, clinical replication)
- [read] https://arxiv.org/abs/2605.15077 (async function calling, execution layer)
- [read] https://arxiv.org/abs/2512.14865 (Audio MultiChallenge)
- [read] https://proceedings.mlr.press/v267/patil25a.html (BFCL, ICML 2025)
- [read] https://daniellin94144.github.io/FDB-v3-demo/
- [read] https://github.com/Lorelys/maat-benchmarks
- [read] https://github.com/livekit/eot-bench
- [read] https://huggingface.co/livekit/turn-detector
- [title] https://openreview.net/forum?id=5vKnBgKmOV (browser verification wall)
- [title] https://openreview.net/forum?id=2GmDdhBdDk (BFCL OpenReview forum)

### lens-codex (code patterns, pinned at `cbb5770`, `0602125`, `228095c`)

- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/worker.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/ipc/proc_pool.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/ipc/_preload.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/voice/agent_session.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/voice/audio_recognition.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/voice/agent_activity.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/voice/endpointing.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/voice/speech_handle.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/inference/interruption.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/llm/utils.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/llm/tool_context.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/voice/tool_executor.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/llm/chat_context.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/voice/agent.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/llm/delegation.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-agents/livekit/agents/llm/_realtime/openai.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-plugins/livekit-plugins-openai/livekit/plugins/openai/realtime/realtime_model.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-plugins/livekit-plugins-silero/livekit/plugins/silero/vad.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-plugins/livekit-plugins-turn-detector/livekit/plugins/turn_detector/base.py
- [read] https://github.com/livekit/agents/blob/cbb5770c716c70009969cd7fa88e3c3662222ce9/livekit-plugins/livekit-durable/livekit/durable/function.py
- [read] https://github.com/pipecat-ai/pipecat/blob/0602125e1d814ae49a7c4e6dc3f6f1b8b3d5ed0c/src/pipecat/frames/frames.py
- [read] https://github.com/pipecat-ai/pipecat/blob/0602125e1d814ae49a7c4e6dc3f6f1b8b3d5ed0c/src/pipecat/turns/speculation_gate.py
- [read] https://github.com/pipecat-ai/pipecat/blob/0602125e1d814ae49a7c4e6dc3f6f1b8b3d5ed0c/src/pipecat/turns/user_turn_controller.py
- [read] https://github.com/livekit-examples/agent-starter-python/blob/228095c442fcb259c80e9786fc0d7c5534e6af46/src/agent.py
- [read] https://api.github.com/repos/livekit/agents

### lens-opus (narrative)

- [read] https://livekit.com/blog/using-a-transformer-to-improve-end-of-turn-detection
- [read] https://livekit.com/blog/livekits-series-b
- [read] https://livekit.com/blog/turn-detection-and-interruption-handling
- [read] https://docs.livekit.io/agents/logic/turns/tuning/
- [read] https://docs.livekit.io/agents/
- [read] https://docs.livekit.io/agents/logic/workflows/
- [read] https://docs.livekit.io/reference/migration-guides/v0-migration/python/
- [read] https://github.com/livekit/agents
- [read] https://github.com/livekit/agents/releases
- [read] https://arxiv.org/abs/2401.04868
- [read] https://arxiv.org/abs/2410.00037
- [read] https://techcrunch.com/2024/10/01/openais-devday-brings-realtime-api-and-other-treats-for-ai-app-developers/
- [read] https://docs.pipecat.ai/overview/introduction
- [read] https://temporal.io/how-it-works
- [read] https://www.piwheels.org/project/livekit-durable/

### Added by this synthesis

No new LiveKit source and no new LiveKit internals claim. Every LiveKit assertion is anchored to a lens entry above, and every unsourced internals claim is quarantined in Section 6.

One methodological citation is supplied by the recipe prompt rather than by a lens, and is flagged as such rather than treated as independently verified:

- [recipe-supplied] https://arxiv.org/abs/2601.05114 (Nasser 2026, on vote-rule bias between same-conviction agents)

---

*End of synthesis. Verifier note: the load-bearing consensus findings are 3.2, 3.4, 4.1, 4.3, 5.1, and 6.2. The load-bearing disputes are D1 (detector architecture), D3 (cold start mechanism), and D5 (benchmark superiority). If the verifier finds any of those stated as fact without a lens anchor, that section failed the discipline rule.*
