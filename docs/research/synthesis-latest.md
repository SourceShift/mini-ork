# R2 Synthesis: LiveKit, the company, the market, and the economics

**Cluster:** R2 · **Run:** run-1794656961-44232 · **Research as-of:** 2026-11-14
**Audience:** a staff engineer three days from a hiring-manager screen and a CTO conversation.
**Inputs:** `lens-glm` (breadth, 21 sources), `lens-kimi` (depth, 22 sources), `lens-codex` (code and pricing, 18 sources), `lens-opus` (narrative, 16 sources). All four read in full before this document was written.

**Consensus scale:** ★ = 2 of 4 lenses · ★★ = 3 of 4 · ★★★ = all 4.
A marker is a count of lenses, not a vote on truth. Where lenses disagree, Section 3 reports the disagreement and does not average it.

---

## 1. TL;DR

1. **LiveKit is a 2021-founded open-core realtime infrastructure company**, not an "AI company" in the model sense. Russ d'Sa (CEO) and David Zhao (CTO) launched the open source project in July 2021 and have raised roughly $180M across five rounds, ending in a $100M Series C on 2026-01-22 led by Index Ventures at a $1B valuation. What it sells is an SFU plus SDKs plus a managed operational envelope (telephony, inference, observability, compliance). (lens-glm [source-1], [source-5]; lens-kimi §2.16; lens-opus §1; confidence **high** on the round and the founders, **medium** on the total.)

2. **The free/paid line is drawn in code, not only on the pricing page.** Server and Agents framework are Apache-2.0, but the framework's default turn detector calls LiveKit's hosted inference (`agent_session.py:584`), and end-of-turn plus adaptive interruption are free for agents hosted on Cloud and metered for self-hosted ones. Published tiers are Build $0 (1,000 agent minutes, 5 concurrent), Ship from $50, Scale from $500, Enterprise custom; LiveKit's own worked example totals **$0.0479/min** all-in. (lens-codex §2 and §3; lens-glm [source-2]; lens-kimi §2.16(d); confidence **high as of 2026-11-14**, and the page carries no effective date.)

3. **OpenAI is simultaneously the flagship customer and the most credible absorber.** ChatGPT Advanced Voice runs on LiveKit Cloud, and OpenAI now ships its own WebRTC transport inside the Realtime API. The relationship is the strongest proof of technical quality and the clearest concentration risk in the same fact. (lens-glm [source-3], [source-4], [source-13]; lens-kimi §2.16(c); lens-opus §3; confidence **high** on the customer fact, **medium** on what it implies.)

4. **The absorption thesis is genuinely contested, and no source settles it.** A model vendor bought the framework-plus-transport layer on 2026-10-29 (Deepgram acquired Daily, the company behind Pipecat). Against that, the best voice-agent benchmark attributes 79 to 90 percent of failures to agent behaviour rather than to speech or transport, and a vendor-native realtime model posts a 13.5 percent interruption-avoidance rate against a perfect turn-take rate for a cascaded pipeline. (lens-glm [source-10]; lens-opus §3; lens-kimi §3.4; confidence **medium**.)

5. **Nothing about LiveKit's money is public.** No ARR, no revenue, no paying-customer count, no self-hosted-to-Cloud conversion rate, no market share. The three research houses that size the adjacent market disagree by 36 percent on the 2025 base year and by a factor of 1.63 on growth. The safe move in every conversation is "not public" plus the structure, never a number. (lens-kimi §3.1 and §3.3; lens-glm §D; lens-opus §5; confidence **high**.)

---

## 2. Findings by kickoff question (consensus marked)

### Q1. Founding, founders, funding through November 2026

**★★ (3/4) Founded 2021, open source project live July 2021, two founders: Russ d'Sa (CEO) and David Zhao (CTO).** TechCrunch's December 2021 piece has the company at five months old, 3 to 15 staff, ~2,000 GitHub stars, 100+ projects, zero revenue (lens-glm [source-5]; lens-opus §1). Bloomberg's Series C headline confirms founding in 2021 and names Zhao (lens-glm [source-7]). The Fortune press release names exactly the same two founders (lens-kimi §2.16(b)). **There is no third founder.** An early sweep hypothesis naming Weiwei Li was wrong, and lens-glm killed it explicitly (lens-glm §A1).

**Funding ladder, per lens:**

| Round | Amount | Date | Lead | Anchor quality | Lenses |
|---|---|---|---|---|---|
| Seed | $7M announced ($6.74M per Zhao's own table) | Dec 2021 | Redpoint Ventures | TechCrunch, fetched | ★ (lens-glm [source-5]; lens-opus §1) |
| Seed 2 | $7.3M (one lens says $8M) | Sep 2022 | not established | secondary listing | ★, disputed amount |
| Series A | $22M ($22.5M in one lens) | Jun 5, 2024 (one database says Mar 2024) | **not established** | Zhao round-table mirror | ★, disputed |
| Series B | $45M | Apr 10-11, 2025 | Altimeter Capital | founder post + FinSMEs + TechCrunch; $345M post-money, ~$83M raised to date | ★ (lens-glm [source-6]; lens-opus §1) |
| Series C | $100M | 2026-01-22 | Index Ventures | LiveKit's own post + Fortune press release + Bloomberg headline; $1B valuation; Salesforce Ventures new, Hanabi/Altimeter/Redpoint returning | ★★ (lens-glm [source-1]; lens-kimi §2.16(a)(b); lens-opus §1) |

**★ (2/4) Total raised ≈ $180M across five rounds.** lens-glm sums $7M + $7.3M + $22M + $45M + $100M to ~$182M; lens-opus states ~$183M; lens-kimi's search index carries Tech in Asia's "$181M+ across 5 rounds" but could not fetch it (lens-kimi §8.21). The three figures are the same arithmetic with different seed assumptions. Say "about $180M" and move on.

**★★ (3/4) The Series C is the best-documented event in the company's history** and the one number that is safe to lead with, because three independent anchors exist: the company's own post, a Fortune press release, and a Bloomberg headline (lens-glm [source-1] [source-7]; lens-kimi §2.16; lens-opus §1).

### Q2. Product line: what is free, what is paid

**★★★ (4/4) Open core: the data plane is open, the managed control plane is closed.** lens-codex read the licences and the repos directly: `livekit/livekit` is Apache-2.0 Go, 22,204 stars, v1.13.11 released 2026-10-25; `livekit/agents` is Apache-2.0 Python with a parallel TypeScript package, 15,218 stars, shipping roughly weekly (1.8.12 on 2026-11-13). No cloud control-plane repo exists in the org listing (100 public repos inspected). lens-glm, lens-kimi and lens-opus all describe the same split from the vendor pages. (lens-codex §1, §2, §3; lens-glm §A3; lens-kimi §2.16; lens-opus §2.)

**★★★ (4/4) The tier structure.** Build $0/mo (1,000 agent session minutes, 5 concurrent agent sessions, no credit card), Ship from $50/mo (5,000 included, then $0.01/min), Scale from $500/mo (50,000 included, up to 600 concurrent), Enterprise custom (SSO, SLA, HIPAA, RBAC, region pinning, private links, on-prem through sales). All four lenses carry the same four tiers; the three that fetched the page independently reproduce the usage allowances line for line, which is what a same-day fetch of one page should look like. (lens-glm [source-2]; lens-kimi §2.16(d); lens-codex §3; lens-opus §2 and §4.)

**★★ (3/4) Transport is priced as an afterthought: $0.0005/min on Ship, $0.0004/min on Scale.** All three pricing literacies agree, and lens-kimi draws the consequence: WebRTC media is roughly 1 percent of the customer's per-minute bill. (lens-glm [source-2]; lens-kimi §3.2; lens-codex §3.)

**★★ (3/4) The worked example is $0.0479/min, and it is quotable only with the date.** Breakdown: agent session $0.0100, telephony $0.0100, observability $0.0100, TTS $0.0090, STT $0.0075, LLM $0.0014. Note that the platform take per minute ($0.01) is smaller than the models combined ($0.0179). (lens-glm [source-2]; lens-kimi §3.2; lens-codex §3.)

**★★ (3/4) Turn detection is the visible open-core seam.** End-of-turn detection is $0.50 per 1,000 requests and adaptive interruption $1.20 per 1,000, free for agents hosted on LiveKit Cloud and metered after a development allowance for self-hosted agents (lens-glm [source-2]; lens-kimi §2.16(d); lens-codex §3).

**Single-lens but code-anchored, and the sharpest finding in the corpus: the OSS framework's default turn detector is a Cloud product.** `livekit-agents/livekit/agents/voice/agent_session.py:584-586` reads `raw_turn_detection = turn_handling.get("turn_detection", inference.TurnDetector())`. Out-of-the-box conversation quality therefore depends on LiveKit Inference. That is the open-core boundary expressed as a Python default argument, and it is a better thing to say in an interview than any pricing table. (lens-codex §2.)

**Single-lens: the Startup Program is the quantified conversion subsidy.** $10,000 in Cloud credits, one year on Scale (normally $500/mo), $7,000 in partner inference credits, "about $23,000 in total value", for companies under 50 employees that have raised under $5M, with no conversion commitment and credits that "roll over to standard rates". Nobody else publishes a number like this. (lens-kimi §2.16(e), §3.3.)

**★ (2/4) The enterprise surface is operational, not algorithmic:** SSO, SLA, dedicated solutions engineer, volume pricing, on-prem/private deployment via sales, HIPAA reports, region pinning, private links (lens-glm §A3; lens-kimi §2.16(d)). lens-codex adds that 99.99 percent uptime is advertised on every paid tier, which makes uptime table stakes rather than a differentiator.

### Q3. Named customers and what each logo proves

**★★ (3/4) OpenAI is the anchor customer: ChatGPT Advanced Voice runs on LiveKit Cloud.** The October 2024 partnership post says developers get "the same end-to-end technology powering ChatGPT's new Advanced Voice feature", with WebRTC on the client leg, WebSocket to the model, and a ~300ms conversational latency target (lens-glm [source-4]). The homepage says OpenAI "built ChatGPT's Advanced Voice on LiveKit Cloud, serving millions of users every day" (lens-kimi §2.16(c)). lens-opus draws the strategic conclusion: OpenAI bought the client-side media path rather than building it. (Confidence high on the fact.)

**★ (2/4) The wider roster, and it is a logo wall with different provenance per logo.** SAP (Joule assistant) is on the customers page (lens-glm [source-3]). Salesforce Agentforce (customer support voice) and Tesla (sales, support, insurance, roadside voice) are named in the Series C post (lens-glm [source-1]; lens-kimi §2.16(a)). The Fortune press release names OpenAI, xAI, Salesforce, Coursera and Spotify (lens-kimi §2.16(b)). Verkada ("30,000+ organizations"), Skydio (drone teleoperation), Polymath Robotics, Nvidia, Oracle, Decagon, Cresta, Podium, Headspace, Deutsche Telekom and others fill out the wall (lens-glm [source-3]; lens-kimi §2.16(c)).

**★ (2/4) Retell AI appears on LiveKit's customer wall and its CTO is quoted saying "We recently moved from a homegrown WebSocket stack to LiveKit Cloud."** A direct competitor in voice agents is a paying customer of the transport layer. This is the cleanest question the research surfaced. (lens-glm [source-3]; lens-kimi §2.16(c).)

**★ (2/4) What a logo proves is that someone paid for something, and no more.** lens-glm makes the point sharply (a competitor and an open voice-model company both sit on the wall; one logo's alt text is unreadable "SpaceXAI"), and lens-opus calls the logo wall "a claim to attribute rather than a fact to assert". No lens found a single customer with a quantified contract, a duration, or a revenue share. (lens-glm §A4; lens-opus §2.)

**Note the two scale figures that are not customer counts:** "300,000+ developers" (homepage, November 2026) and "more than 200,000 developers and customers" (January 2026 press release). Different metrics, different dates, neither a paying-customer figure (lens-kimi §3.6).

### Q4. The competitive set, placed precisely

**★★★ (4/4) The market decomposes into three layers, and everyone is converging on the middle one:** transport/SFU (LiveKit server, mediasoup, Janus, Jitsi Videobridge, Agora RTC, Daily, Twilio), orchestration framework/runtime (LiveKit Agents, Pipecat, Pipecat Cloud, Vapi, Retell, Bland), and models/speech (OpenAI Realtime, Deepgram, ElevenLabs, AssemblyAI, Cartesia, open models). lens-kimi's AGI House taxonomy uses eight layers and reaches the same structural conclusion; lens-codex's pricing survey and lens-opus's history both land on the same split. (lens-kimi §2.5; lens-codex §3 and "Convergent patterns"; lens-opus §2; lens-glm §A5.)

| Player | What it is | Overlap with LiveKit | The number to quote (as-of 2026-11-14) |
|---|---|---|---|
| **Pipecat** | BSD-2 Python frame-pipeline framework, maintained by Daily and the community, 17,253 stars, created 2023-12-27 | Direct framework competitor, **and lists LiveKit as a supported transport** | No platform fee of its own; the runtime is Pipecat Cloud |
| **Pipecat Cloud (Daily)** | Closed hosted runtime, per-vCPU-minute pricing, unlimited concurrency | Same layer as LiveKit Cloud | $0.01 to $0.03/min active (agent-1x to 3x); Daily WebRTC $0.004/participant-min |
| **Vapi** | Closed orchestration over borrowed transports (Daily WebRTC free); 66 public repos, none the server | Managed agent platform, one layer above LiveKit | Hosting $0.05/min, models at pass-through; Pro tier 10% of hosting with a $999/mo floor |
| **Retell AI** | Closed voice-agent platform publishing a model-by-speed price matrix; 11 public repos, no server | Managed agent platform **and** a LiveKit Cloud customer | Infrastructure $0.055/min, their example $0.11/min total; $8/concurrency/month after 20 |
| **OpenAI Realtime** | Hosted speech-to-speech, WebRTC recommended for browser and mobile; closed, no self-host | Model vendor that now ships its own transport | gpt-realtime-2.1 $32/1M audio in, $64/1M audio out, cached input $0.40/1M |
| **Deepgram** | STT incumbent ($0.0048/min Nova-3 streaming) that climbed into agents ($0.075/min Voice Agent) | Supplier to LiveKit, Vapi and Retell **and** their competitor; bought Daily/Pipecat 2026-10-29 | 15x markup from STT component to assembled agent |
| **ElevenLabs** | Voice vendor with an agent product | TTS line item inside everyone's bill, plus its own agent | Agents $0.08/min additional, burst $0.16/min |
| **Agora** | CPaaS incumbent with a decade of edge build-out | Direct transport competitor with an installed base and a conversational-AI upsell | RTC $0.59 per 1,000 min; conversational AI from $0.10/min |
| **Twilio** | Telephony incumbent | Number supplier underneath LiveKit, Vapi, Retell and Pipecat | ConversationRelay $0.07/min (page states "current as of October 2026") |
| **Bland** | All-in bundled platform, telephony excluded | Same buyer, opposite pricing philosophy | $0.12 to $0.14/min |
| **HF speech-to-speech** | Apache-2.0 fully local pipeline, 13,693 stars | The "you need none of this" existence proof | No commercial runtime |

All price and repo figures above are lens-codex §1 to §16, fetched 2026-11-14, except the Vapi Series B, which is ★ (lens-glm [source-8] [source-9]; lens-opus §4).

**Single-lens, code-anchored, and strategically load-bearing: Pipecat supports LiveKit as a transport.** Choosing Pipecat does not mean choosing against LiveKit Cloud. The competition is at the framework and runtime layer, not the SFU. (lens-codex §5.)

**★ (2/4) Platform take is converging while model cost is not.** The platform fee band is roughly $0.01 to $0.06/min (LiveKit $0.0100, Pipecat Cloud $0.01 to $0.03, Vapi $0.05, Retell $0.055); the model stack is $0.04 to $0.10/min. The margin is in bundling and inference, not in transport. (lens-codex §3 and "Convergent patterns" 6; lens-kimi §3.2 corroborates the direction from LiveKit's own cost stack.)

**★ (2/4) Stars say the framework race is close, not won.** Pipecat at 17,253 stars is ahead of `livekit/agents` at 15,218; `livekit/livekit` at 22,204 is the largest asset and its `sip` service has 497. Development attention in the ecosystem is on the agent-facing surface, not the media plumbing. (lens-codex §4 and "Stack-rank"; lens-glm [source-3] publishes the same 22.2K/15.2K from the customers page.)

### Q5. The open-source-plus-hosted-cloud tension against model vendors

**★★★ (4/4) The structure is real and the split is not public.** Every lens independently establishes that the media server and framework are free and the managed control plane is the paid product, and every lens independently reports that no revenue split, conversion rate, or self-hosted fleet size is published. lens-codex proves the closed control plane by absence in the org listing. This is the most solid block of the research. (lens-glm §D; lens-kimi §3.3; lens-codex §3; lens-opus §2.)

**★★★ (4/4) The absorption thesis is contested, and presenting it as settled is the single most likely way to sound naive.** The question, in lens-kimi's citation of AGI House: "whether this layer stays independent or is absorbed into model vendors' realtime APIs", in a document that also says "Model vendors are moving up-stack" and "Independent orchestrators remain model-neutral". lens-opus steel-mans the dissent and concludes it is "right about direction and wrong about speed". lens-glm found the raw events but no settled interpretation. lens-codex supplies the structural detail: OpenAI "owns the transport inside its own product and still does not own the phone network". (lens-kimi §3.4; lens-opus §3; lens-glm §C7; lens-codex §9.)

**Evidence on each side, both sourced:**

| Absorption side | Counter side |
|---|---|
| OpenAI added WebRTC to the Realtime API (lens-glm [source-13] [source-14], dated 2024-12-17) | OpenAI bought the client leg rather than building it; it ran on LiveKit's SDK and Cloud first (lens-glm [source-4]) |
| Deepgram acquired Daily/Pipecat on 2026-10-29, folding model, transport and framework into one company (lens-glm [source-10] [source-11]; lens-opus §3) | Pipecat stays BSD-2 and vendor-neutral after the deal (lens-glm [source-10]) |
| A speech-to-speech model removes the STT/LLM/TTS loop the framework exists to manage; cascaded pipelines are roughly 1.6s or more before audio against 800 to 2,000ms end-to-end (lens-kimi §3.4, from AssemblyAI's own figures) | 79 to 90 percent of voice-agent failures are agent behaviour, not speech or transport; voice agents retain 30 to 45 percent of text-mode performance (lens-kimi §2.13, arXiv:2603.13686) |
| Model vendors appear in the orchestration layer of the taxonomy, not only the model layers (lens-kimi §3.4) | GPT-Realtime's interruption-avoidance rate is 13.5 percent; a cascaded Whisper to GPT-4o to TTS pipeline has a perfect turn-take rate (lens-kimi §2.14, arXiv:2604.04847) |
| The Deepgram deal shows a framework vendor accepting acquisition (lens-glm [source-10]) | A competitor's framework (Pipecat) lists LiveKit's Cloud as a supported transport, and an STT vendor (AssemblyAI) publishes a guide saying the loop stays with a framework (lens-codex §5; lens-kimi §2.15) |

**★★ (3/4) Transport is the commoditising layer; orchestration and observability are where the margin is.** lens-kimi's evidence is the pricing sheet (WebRTC at $0.0005/min against $0.01 for the session and $0.01 for observability) plus the SFU scaling literature: stream count grows as N squared, mediasoup hit 100 percent CPU at roughly 80 participants on a 40-core Xeon, and the media data plane is being pushed into programmable hardware. lens-codex reaches the same conclusion from competitor pricing. lens-opus reaches it from the historical argument that infra gets absorbed and the operational envelope does not. (lens-kimi §3.5; lens-codex §3; lens-opus §2.)

**★ (2/4) The open-core precedent has a named failure mode, and it predates the model-vendor threat by six years.** a16z in 2019: cloud hosting creates the risk that "public clouds will choose to take your open source code and compete", and over-proprietarising invites a fork. Sentry is the closest observable analogue, with 90 percent of users self-hosting on the free plan as of May 2024 while revenue "more than tripled in just over two years". (lens-kimi §3.3, §2.7, §2.10; lens-opus §2 describes the same design logic without the precedent.)

### Q6. What this means for what an engineer says in a CTO conversation

This question is not a fact to verify; it is the payload. The lenses converge on a shape rather than a script, and the shape is:

**Lead with structure, not with numbers.** The four things that are actually verifiable are the funding ladder with dates and leads, the open/paid boundary, the per-minute cost stack, and the customer roster with its provenance. Everything else is either undisclosed (revenue, conversion, concentration) or contested (market size, competitor traction, the absorption thesis). (Synthesis judgment across all four; anchors throughout Sections 2 and 3.)

**The two questions only an insider can answer are also the two questions that prove you did the work:** the speech-to-speech versus cascaded minute mix on LiveKit Cloud, and the OSS-to-Cloud conversion cohort with net revenue retention. Neither is public, and asking for them is a demonstration of judgement rather than a gap. (lens-opus §5; lens-kimi §3.6 reaches the same opening from the developer-count funnel.)

**The defensible sentence about strategy is modest and hard to disprove:** LiveKit is the neutral, deployable, multi-party media and operational layer that survives model churn, and the orchestration seam above it is closing. That is a better sentence than "we are the AI infrastructure layer", which is a market-share claim nobody can support. (lens-opus §6; lens-kimi §7.4.)

---

## 3. Disputed findings

Per Nasser 2026 (arXiv:2601.05114), same-conviction agents that vote amplify bias rather than averaging it. These are reported, not resolved by majority.

### D1. Does OpenAI's Realtime API support SIP telephony? (direct contradiction, 2 lenses against 1)

- **For SIP:** lens-opus states OpenAI added "native SIP telephony to the Realtime API at GA in August 2025", citing 36Kr and iThome (both August 2025 secondary coverage). lens-glm's OpenAI docs entry says WebRTC "plus WebSocket and SIP per Azure's mirror" (lens-glm [source-13]).
- **Against:** lens-codex fetched `developers.openai.com/api/docs/guides/realtime-webrtc` on 2026-11-14 and reports the page "documents WebRTC and WebSocket only; it does not document SIP or telephony", and builds a strategic conclusion on it ("OpenAI owns the transport inside its own product and still does not own the phone network").
- **Why they disagree:** almost certainly scope, not fact. lens-codex read one guide page (the WebRTC transport guide) and generalised to "the docs". lens-opus read August 2025 trade coverage of a GA announcement. Both can be true: SIP can be supported and documented on a different page from the WebRTC guide. lens-glm's "per Azure's mirror" hedge suggests it did not confirm on OpenAI's own docs either.
- **What would resolve it:** open `developers.openai.com/api/docs/guides/realtime` (the main guide, not the WebRTC sub-page) and search for SIP; check the OpenAI changelog for an August 2025 entry. Until then, assert neither. This is a live trap because the strategic argument on each side depends on it.

### D2. Retell AI's traction: $100M run rate or ~$60M ARR?

- **lens-glm:** "Retell AI announced a $100M annualized run rate with only $4.6M new funding (~$5.1M total raised)", November 2026, company-claimed, days old, no independent grounding.
- **lens-opus:** "Retell reportedly reached ~$60M ARR on a very small team, both figures low confidence and disputed across sources."
- **Why they disagree:** run rate and ARR are different metrics, and the two figures are weeks apart in time. A $100M annualised run rate can coexist with $60M trailing ARR during fast growth. Neither figure has a primary filing.
- **What would resolve it:** a Retell disclosure with a definition, or a funding announcement with an investor-confirmed number. Do not quote either.

### D3. Series A: who led it, how much, and when?

- **lens-opus:** "$22.5M Series A led by Altimeter in June 2024", sourced to Wikipedia.
- **lens-glm:** $22M, June 5 2024 per a mirror of David Zhao's own round table, with a second database saying March 2024, and **"the Series A lead investor is not named in anything fetched this run. Do not assert one."**
- **Why they disagree, and my judgment:** lens-opus's Altimeter attribution looks like a conflation with the Series B, where Altimeter is confirmed by three sources. Wikipedia is tertiary and its Series A detail is weak. The Zhao-table mirror is founder-adjacent and is the better source for amount and date. Treat the lead as unknown.
- **What would resolve it:** the original Series A announcement or the investor's own portfolio post.

### D4. The second seed round: $7.3M or $8M?

lens-glm has "$7.3M, September 2022" from a funding listing; lens-opus has "an $8M additional seed in 2022" from Wikipedia. Both are secondary, one round apart in precision, and neither is a primary announcement. It matters only because it changes the "total raised" arithmetic by about $1M. Report the ladder and skip the reconciliation.

### D5. Paying-customer count: 500+ or not public?

lens-opus cites TechCrunch (2025-04-10) reporting "500+ paying customers". lens-glm (§D1) and lens-kimi (§3.6) both state that **no paying-customer count is published**, and neither found the TechCrunch figure. This is not a logical contradiction (a journalist can report a number the company does not publish), but it is a single-source, 19-month-stale figure that the other three lenses did not corroborate. Treat as single-lens, not as fact.

### D6. Is xAI a named customer?

lens-kimi's Fortune press release read names "OpenAI, xAI, Salesforce, Coursera, Spotify". lens-glm, working from the logo wall, found one logo whose alt text reads "SpaceXAI" with the file `x-ai.svg` and explicitly declined to quote either name. The press release is the stronger source, so "xAI is named in the Series C press release" is defensible; "xAI is on the customer wall" is not something lens-glm could confirm.

### D7. Market size: three houses, three answers

Not a lens-versus-lens dispute but the most consequential external disagreement in the corpus, and it belongs here because a reader will otherwise average it:

| House | 2025 base | 2026 | Forecast | CAGR | Horizon |
|---|---|---|---|---|---|
| Grand View Research (Jun 2026) | $2.5B | $3.5B | $35.2B (2033) | 39.0% | 2026-2033 |
| SNS Insider (upd. 2026-09-29) | $2.75B | $3.70B | $55.55B (2035) | 35.13% | 2026-2035 |
| Meticulous Research (Oct 2026) | $3.40B | $5.10B | $43.83B (2036) | 24.0% | 2026-2036 |

36 percent spread on the base year, 1.63x on the CAGR, deliberately staggered horizons, no disclosed method in any of the three, and Meticulous's later horizon lands *below* SNS's earlier one. The dispersion is the finding. (lens-kimi §3.1.)

### D8. Series B date: September 2024 or April 2025?

lens-opus flags that "several widely copied comparison posts date the Series B to September 2024 at roughly a $300M valuation" and treats it as an error against FinSMEs and TechCrunch's April 2025 coverage. lens-glm independently has April 10-11, 2025 with a $345M post-money. Two lenses against the copied posts. Treat April 2025 as correct, and be aware the wrong date is common enough that saying it would be an unforced error.

### D9. Headquarters

One funding-database snippet says San Jose, CA; LinkedIn and a podcast place d'Sa in San Francisco (lens-glm §C4). Low stakes and unresolved. Say "Bay Area" if it comes up.

### Single-lens findings with no cross-check (explicitly no consensus)

These are not disputes. They are findings only one lens produced, reported so the reader can see where the cross-checking stopped:

- **OpenAI's traffic or revenue share of LiveKit.** No lens quantifies it. It is the single most load-bearing unknown for Q5 (lens-glm §D2).
- **The 79 to 90 percent agent-behaviour failure attribution and the 30 to 45 percent retention of text-mode performance.** Only lens-kimi, from arXiv:2603.13686. Peer-reviewed but a single benchmark suite with a simulator decoupled from wall-clock time.
- **The interruption-avoidance 13.5 percent and 4.25 second latency figures.** Only lens-kimi, from arXiv:2604.04847, which is labelled "work in progress" on arXiv.
- **"90 percent of Sentry users self-host on the free plan" and the a16z 5 percent conversion heuristic.** Only lens-kimi; both are weak instruments, described in Section 5.
- **The Startup Program's $23,000 figure.** Only lens-kimi.
- **Every competitor price in the Q4 table except Vapi's round.** Only lens-codex, all fetched same day from vendor pages with no effective dates.
- **The `agent_session.py:584` default turn-detector finding.** Only lens-codex, but it is a file and line, which is the strongest form of single-lens evidence in this run.
- **The French government Visio deployment.** Only lens-opus, sourced to Wikipedia (tertiary). Useful as an illustration of sovereignty, not as a fact to lean on.
- **"500+ paying customers."** Only lens-opus (see D5).

---

## 4. Cross-lens gaps: what no source covers

Aggregated from lens-glm §D (9 items), lens-kimi §3.3 and §6 (17 methodological items), lens-codex ("Do not assert"), and lens-opus §5. Deduplicated, ordered by how much they matter for the two conversations.

1. **LiveKit revenue, ARR, gross margin, or any financial disclosure.** Zero primary sources. The only circulating numbers come from secondary-market listings (Hiive, share price around $3.96, one snippet estimating ~$3.7M for 2025) and aggregators. All four lenses converge on the absence, which makes the absence itself the finding.
2. **OpenAI's share of LiveKit's traffic or revenue.** The lede of every funding story and the flagship logo, with no quantification anywhere. This is the load-bearing unknown for the concentration-risk argument.
3. **The speech-to-speech versus cascaded minute mix on LiveKit Cloud.** Suggested by lens-opus as the single measurement that would settle the absorption thesis. Not published, and not estimable from outside.
4. **OSS-to-Cloud conversion rate, by deployment size, with net revenue retention.** Nobody publishes it, including LiveKit. Every public analogue is either stale (Sentry, May 2024), unsourced (a16z, 2019), or vendor-interested (Logz.io, 2022).
5. **Self-hosted fleet size.** Lens-codex's repo activity suggests a large self-hosted population but cannot size it. 22,204 stars on the server and 497 on the SIP service is an interest signal, not a deployment count.
6. **Current headcount.** The last grounded figure is 15 people in December 2021 (lens-glm [source-5]). Five years and four rounds later, nothing. This matters for the "staff engineer's span of influence" question that Amir should ask about.
7. **Cloud infrastructure unit economics.** Edge network cost, gross margin per minute, egress cost. Nothing public. The $0.0479/min customer-facing example is the only cost anchor, and it is a seller's calculator, not a measured average.
8. **The Series A primary announcement and its lead investor.** Every funding database papers over this differently (see D3).
9. **Any independent sizing of "realtime AI infrastructure" as a category.** No analyst taxonomy isolates transport, SFU, orchestration or telephony. Grand View explicitly has no separately quantified infrastructure segment. The size of LiveKit's actual addressable market is not in any public report.
10. **Whether a platform competitor renting LiveKit's transport (Retell) is durable or transitional.** The cleanest strategic question the research surfaced, and nothing examines it.
11. **Churn, retention, or contract structure for OpenAI, SAP, Tesla-sized accounts.** Zero coverage.
12. **The precise licence and terms of service for each LiveKit component.** Partly closed by lens-codex (Apache-2.0 for server and agents, separate "LiveKit Model License" for turn-detection models), but the Cloud terms, the trademark policy, and the open-core boundary in writing are unexamined.
13. **Peer-reviewed literature on this market.** Lens-kimi found zero papers studying realtime AI infrastructure as a market and zero on open-core conversion in realtime media. Everything peer-reviewed in the corpus is adjacent and labelled as extrapolation.
14. **Late-2026 entrants and announcements.** The sweep covers Deepgram/Daily (Oct 2026) and a Retell claim from days before the research date, but there is no systematic entrant watch. Treat "and any newer entrant" from the kickoff as partially answered at best.
15. **Board composition, governance, and what Index Ventures' lead implies about the next 18 months.** All four lenses silent.

---

## 5. Claims an engineer should not make

Numbered, with the reason the support is weak. These are the sentences that would survive a friendly conversation and die in a CTO's follow-up question.

1. **Any LiveKit ARR or revenue figure**, including "around $10M" or "$3.7M for 2025". *Why weak:* not disclosed in the Series C post, the Fortune press release, the homepage, or the pricing page. The only numbers are a secondary-market listing and aggregator estimates with no primary backing.
2. **"The voice AI market is worth $X"** or **"growing 39 percent a year".** *Why weak:* three research houses give $2.5B, $2.75B and $3.40B for 2025 and CAGRs of 39.0, 35.13 and 24.0, none discloses a method, and the spread on the base year (36 percent) is larger than any year-over-year growth worth quoting.
3. **"LiveKit's market is the voice AI agent market."** *Why weak:* the published category is applications plus platforms. No analyst taxonomy isolates the infrastructure layer. Grand View explicitly has no separately quantified infrastructure or platform segment, so the number is measuring someone else's business.
4. **"LiveKit has X percent market share."** *Why weak:* no report in the corpus names LiveKit in a share table at all. There is no denominator in public form.
5. **"Open source converts about 5 percent to paid."** *Why weak:* an a16z rule of thumb from 2019 with no source, never independently validated, and routinely confused with Sentry's 90 percent self-host *occupancy* figure, which is a different metric measured 19 months ago on an undefined denominator.
6. **"300,000 developers, so roughly 15,000 paying customers."** *Why weak:* a chain of three unverified links. 300,000 is a developer figure from the homepage; the January 2026 press release says "more than 200,000 developers and customers", a different metric; no conversion rate is published. This is the exact arithmetic an interviewer enjoys taking apart.
7. **"500+ paying customers" as a current fact.** *Why weak:* single lens (lens-opus citing TechCrunch, April 2025), 19 months stale, and explicitly contradicted on availability by the two lenses that looked for a customer count and found none.
8. **"Model vendors will absorb the framework layer", stated as settled.** *Why weak:* it is the single most likely way to sound naive. The best available brief frames it as an open question, the benchmark evidence points the other way (79 to 90 percent of failures are agent behaviour; 13.5 percent interruption avoidance for a vendor-native model), and the counter-evidence is structural (sovereignty, multi-party, telephony, physical AI). Present it as a debate with named evidence on both sides.
9. **"OpenAI uses LiveKit, so LiveKit is the standard"** or **"OpenAI endorses the Agents framework".** *Why weak:* OpenAI uses LiveKit Cloud as transport for ChatGPT Advanced Voice. That is a statement about infrastructure and about substitutability, not about the framework and not about model neutrality.
10. **"SFU transport is LiveKit's moat."** *Why weak:* transport is $0.0005/min, about 1 percent of LiveKit's own worked example. SFU work scales as N squared streams, mediasoup saturates a 40-core Xeon near 80 participants, and the media data plane is moving into programmable hardware. The moat claim belongs one layer up, in orchestration, observability and compliance.
11. **"LiveKit is Apache-2.0" as a blanket statement.** *Why weak:* Apache-2.0 is confirmed for `livekit/livekit` and `livekit/agents`, but the agents README states the turn-detection models carry a separate "LiveKit Model License", the Cloud control plane is proprietary, and the Cloud terms of service were not read by any lens.
12. **Vendor traction claims repeated as fact:** Vapi's "1 billion calls" and "a million developers", Retell's "$100M run rate", Deepgram's "first complete, production-ready, open stack". *Why weak:* all company self-claims. Retell's figure is days old, uncorroborated, and conflicts with the other lens's ~$60M ARR. Attribute or omit.
13. **Any per-minute price without an as-of date**, including LiveKit's own $0.0479/min. *Why weak:* the pricing page carries no effective date; the only internal dates are model identifiers (Cartesia Sonic 3.6, 2026-08-27), which bound its currency from below. Say "as of mid-November 2026, check the page."
14. **"OpenAI's Realtime API has no SIP support"** or **"has SIP support".** *Why weak:* the lenses directly contradict each other (D1) and the disagreement is scoped to which page each read. Do not assert either direction until you have opened the current main Realtime guide.
15. **"The Series A was led by Altimeter."** *Why weak:* one lens says so via Wikipedia; the lens that tried hardest to ground it says the lead is not established in anything fetched, and Altimeter is confirmed for the Series B, which is the likely source of the conflation.
16. **"LiveKit was founded by three people", or naming Weiwei Li as a founder.** *Why weak:* TechCrunch, the Fortune press release, Bloomberg and LinkedIn all name exactly two founders. It is an error that was already killed once in this research and could easily come back.
17. **"Total raised was exactly $182M"** (or $183M, or $181M). *Why weak:* it is an arithmetic sum over sources that disagree about the seed structure. Say "about $180M across five rounds".

---

## 6. Recommendations

Each one names the lens that supports it and the condition under which it would be wrong.

1. **Lead with the funding ladder as a ladder, never a single number.** $7M seed (Redpoint, Dec 2021), $22M Series A (Jun 2024), $45M Series B (Altimeter, Apr 2025, $345M post), $100M Series C (Index Ventures, 2026-01-22, $1B). *Supported by:* lens-glm [source-1] [source-5] [source-6], lens-opus §1. *Wrong if:* a new round, secondary sale or tender offer lands before the interview. Re-check the week of.
2. **Say "not public" for revenue and never repeat a marketplace number.** *Supported by:* lens-glm §D1, lens-kimi §3.3, lens-opus §5 (three lenses independently found no disclosure). *Wrong if:* LiveKit or a filing discloses ARR, in which case the disclosure supersedes everything here.
3. **Describe the business as an open data plane plus a paid operational envelope**, and name the envelope: telephony, inference routing, observability, compliance, concurrency, region pinning. *Supported by:* all four lenses, and by the `agent_session.py:584` default. *Wrong if:* the Cloud control plane is open-sourced, or the current pitch has re-centred on Agents-as-the-product. Check the most recent keynote before saying it.
4. **Quote the $0.0479/min worked example instead of any competitor's headline rate**, and never put a platform fee next to an all-in rate. Vapi's $0.05 excludes models; Bland's $0.14 excludes telephony; they are not comparable. *Supported by:* lens-codex §3 and "Do not assert", lens-kimi §3.2. *Wrong if:* the pricing page changes, which it does without notice.
5. **Present the absorption thesis as a live debate with named evidence on both sides.** *Supported by:* lens-kimi §3.4, lens-opus §3, lens-glm §C7, lens-codex §9 (four lenses, no settled interpretation). *Wrong if:* the CTO concedes the layer and describes a pivot. Then the useful conversation is about the pivot and you should drop the debate frame immediately.
6. **Ask for the speech-to-speech versus cascaded minute mix, and the OSS-to-Cloud conversion cohort with NRR.** *Supported by:* lens-opus §5 (the measurement argument), lens-kimi §3.6 (the funnel opening), lens-glm §D (the absence). *Wrong if:* they say the metric is not tracked. That is itself an answer about how the business is run, and the correct response is curiosity, not a follow-up lecture.
7. **Say "validation and concentration risk" about OpenAI in the same breath, with no percentage.** *Supported by:* lens-glm §D2, lens-kimi §4.7, lens-opus §3. *Wrong if:* they volunteer a diversified revenue mix. Then lead with diversification and let the concentration point go.
8. **Give market size as a range with the disagreement named, or skip it.** "Three research houses put the 2025 AI voice agent market between $2.5B and $3.4B and disagree by 36 percent; none of them isolates the infrastructure layer" is a stronger sentence than any single figure. *Supported by:* lens-kimi §3.1 (single lens, but methodologically the most careful passage in the corpus). *Wrong if:* a bottom-up, methodologically disclosed sizing appears, which is the only kind that would beat this framing.
9. **Place competitors by layer, not by name.** Pipecat is a framework that supports LiveKit as a transport; Vapi and Retell are managed agent platforms; Deepgram and ElevenLabs are model suppliers who also sell agents; Twilio and Agora own telephony and transport distribution. *Supported by:* lens-codex §5 and §13 to §15, lens-kimi §2.5. *Wrong if:* a vendor's current product page shows materially different bundling, which for this market means re-check within weeks.
10. **Treat GitHub stars, "1M downloads/month" and "3B calls/year" as activity and marketing signals, not adoption.** Point at release cadence instead (LiveKit server and agents ship roughly weekly; Pipecat ships weekly) if you want a real health signal. *Supported by:* lens-codex "Stack-rank" caveat, lens-glm §C5, lens-kimi §7.10. *Wrong under:* essentially no condition; this one is a discipline, not a forecast.
11. **If you must cite one competitive fact that lands, use the Pipecat-supports-LiveKit-transport one.** It reframes the competition from "framework war" to "layer ownership", and it is checkable in a public repo. *Supported by:* lens-codex §5 (single lens, code-anchored). *Wrong if:* Pipecat drops the LiveKit transport, which is a one-line check on the day.

---

## 7. Source manifest

Deduplicated across the four lenses. Everything below was fetched or read by the lens that cites it on 2026-11-14 unless marked otherwise. `[lookup: ...]` entries are lens-declared gaps where no canonical URL was confirmed.

### LiveKit primary documents
- LiveKit, "LiveKit's Series C: towards the voice-driven era of computing", 2026-01-22. https://livekit.com/blog/livekit-series-c
- LiveKit Series C press release via Fortune Press Releases, 2026-01-22. https://fortune.com/press-releases/livekit-raises-100m-1b-valuation-voice-first-computing-2026-01-22/
- LiveKit homepage, fetched 2026-11-14. https://livekit.com/
- LiveKit pricing page, fetched 2026-11-14. https://livekit.com/pricing (livekit.io/pricing 308-redirects here)
- LiveKit customers page, fetched 2026-11-14. https://livekit.com/customers
- LiveKit, "OpenAI and LiveKit partner to turn Advanced Voice into an API", 2024-10-03. https://livekit.com/blog/openai-livekit-partnership-advanced-voice-realtime-api
- LiveKit (Brian Blumenfeld), "Introducing the LiveKit Startup Program", 2026-09-16. https://livekit.com/blog/introducing-livekit-startups
- LiveKit docs: agents, cloud, quotas and limits. https://docs.livekit.io/agents/ · https://docs.livekit.io/home/cloud/ · https://docs.livekit.io/deploy/admin/quotas-and-limits

### Founding and funding coverage
- TechCrunch (Christine Hall), "LiveKit co-founder believes the metaverse needs open infrastructure", 2021-12-13. https://techcrunch.com/2021/12/13/livekit-metaverse-open-infrastructure
- Bloomberg, "LiveKit, Seller of Voice Tools to OpenAI, Raises $100 Million", 2026-01-22 (headline-level; body paywalled). https://www.bloomberg.com/news/articles/2026-01-22/livekit-seller-of-voice-tools-to-openai-raises-100-million
- Russ d'Sa, Series B and Agents 1.0 announcement, 2025-04-10/11. https://x.com/dsa/status/1910381256543789288
- TechCrunch, "LiveKit's tools help power real-time communications", 2025-04-10. https://techcrunch.com/2025/04/10/livekits-tools-help-power-real-time-communications/
- FinSMEs, "LiveKit Raises $45M in Series B at $345M Valuation", 2025-04. https://www.finsmes.com/2025/04/livekit-raises-45m-in-series-b-at-a-345m-valuation.html
- FinSMEs, "LiveKit Raises $100M in Series C Funding at a $1 Billion Valuation", 2026-01-23. https://www.finsmes.com/2026/01/livekit-raises-100m-in-series-c-funding-at-a-1-billion-valuation.html
- Tech in Asia, "Voice AI platform LiveKit nets $100m series C at $1b valuation" (not fetched). https://www.techinasia.com/news/voice-ai-platform-livekit-nets-100m-series-c-at-1b-valuation
- David Zhao, co-founder and CTO. https://www.linkedin.com/in/davidzhao
- SFELC podcast with Russ d'Sa. https://sfelc.com/podcasts/the-product-paradigm-shift-how-livekit-navigated-high-stakes-scaling-challenges-to-build-the-future-of-voice-first-ai-interfaces-russ-dsa-livekit
- Index Ventures, "See, Hear, Speak: Investing in LiveKit". https://www.indexventures.com/perspectives/see-hear-speak-investing-in-livekit-and-the-future-of-realtime-applications/
- `[lookup: LiveKit funding history every round investor valuation 2026]` (Zhao round-table mirror; funding databases)
- `[lookup: Hiive LiveKit stock share price]` (secondary-market listing; low confidence, cited only to explain why a revenue figure exists in circulation)

### Market sizing and structure
- Grand View Research, "AI Voice Agents Market Size, Share & Trends Analysis, 2026-2033", June 2026. https://www.grandviewresearch.com/industry-analysis/ai-voice-agents-market-report
- Meticulous Research, "Voice AI Agents Market (2026-2036)", October 2026. https://www.meticulousresearch.com/product/voice-ai-agents-market-6938
- SNS Insider, "AI Voice Agents Market Size, Share & Trends Analysis", updated 2026-09-29. https://www.snsinsider.com/reports/ai-voice-agent-market-11242
- Menlo Ventures, "2025: The State of Generative AI in the Enterprise", 2025-12-09. https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/
- AGI House (Jessica Chen), "Voice Agents Research Brief: The Technical Stack Behind Realtime Conversation", 2026-07-30. https://blog.agihouse.org/posts/voice-agents-research-brief
- AssemblyAI (Kelsey Foster), "The voice AI stack for building agents in 2026", 2026-10-28. https://www.assemblyai.com/blog/the-voice-ai-stack-for-building-agents

### Peer-reviewed and preprint
- Michel, Sengupta, Kim, Netravali, Rexford, "Scalable Video Conferencing Using SDN Principles", arXiv:2503.11649, 2025-03-14. https://arxiv.org/abs/2503.11649 · https://arxiv.org/html/2503.11649v1
- Ray, Dhandhania, Barres, Narasimhan, "τ-Voice: Benchmarking Full-Duplex Voice Agents on Real-World Domains", arXiv:2603.13686, 2026-03-14. https://arxiv.org/abs/2603.13686
- Lin, Chen, Chen, Lee, "Full-Duplex-Bench-v3", arXiv:2604.04847, 2026-04-06 (arXiv comment: "Work in progress"). https://arxiv.org/abs/2604.04847
- Duparc, Möller, Jussen, Stachon, Algac, Otto, "Archetypes of open-source business models", *Electronic Markets*, 2022, DOI 10.1007/s12525-022-00557-9. https://link.springer.com/article/10.1007/s12525-022-00557-9
- Kshetri, "Economics of Open Source Software and AI Models", *Computer* 58(1):134-141, 2025-01, DOI 10.1109/MC.2024.3488974 (**not readable in this run; cited as a pointer only**). https://www.computer.org/csdl/magazine/co/2025/01/10834152/23lk3Uc21eE · https://dl.acm.org/doi/abs/10.1109/MC.2024.3488974
- Ashby Institute (Sidney Scott), "Via Negativa: The AI Economy by Elimination", v4.1, 2026-10-02, working paper TAI-WP-2026-02 (**no arXiv ID assigned**; author discloses financial conflicts). https://theashbyinstitute.org/publications/via-negativa · https://theashbyinstitute.org/manus-storage/ViaNegativaarXivv3_58458eb4.pdf
- Notably absent: any peer-reviewed work on realtime AI infrastructure as a market. lens-kimi found none.

### Open-core and business-model economics
- a16z (Peter Levine, Jennifer Li), "Open Source: From Community to Commercialization", 2019-10-04. https://a16z.com/open-source-from-community-to-commercialization/
- Contrary Research (Vardan Sawhney), "Sentry: Business Breakdown & Founding Story", updated 2026-10-22. https://research.contrary.com/company/sentry
- Logz.io (Charlie Klein), "The Open Source Observability Adoption and Migration Curve", 2022-10-19. https://logz.io/blog/open-source-observability-adoption-migration-curve/

### Repositories (metadata via GitHub REST API, 2026-11-14)
- https://github.com/livekit/livekit (Apache-2.0, Go; 22,204 stars; v1.13.11, 2026-10-25)
- https://github.com/livekit/agents (Apache-2.0 Python + agents-js; 15,218 stars; livekit-agents@1.8.12, 2026-11-13; turn-detection models under a separate "LiveKit Model License")
- https://github.com/livekit/egress · https://github.com/livekit/sip · https://github.com/livekit/ingress
- https://github.com/pipecat-ai/pipecat (BSD-2-Clause, Python; 17,253 stars; created 2023-12-27)
- https://github.com/versatica/mediasoup (ISC; 7,419 stars) · https://github.com/jitsi/jitsi-videobridge (Apache-2.0; 3,109 stars) · https://github.com/meetecho/janus-gateway (GPL-3.0; 9,203 stars)
- https://github.com/huggingface/speech-to-speech (Apache-2.0; 13,693 stars)
- https://github.com/fixie-ai/ultravox (4,589 stars; last push 2025-12-12) · https://github.com/kyutai-labs/moshi (last push 2026-09-09)
- Org listings: https://api.github.com/orgs/livekit/repos · https://api.github.com/orgs/VapiAI/repos · https://api.github.com/orgs/RetellAI/repos · https://api.github.com/repos/{owner}/{repo}

### Competitor pricing and product pages (all fetched 2026-11-14; none carries a stable effective date)
- https://www.daily.co/pricing/pipecat-cloud/
- https://vapi.ai/pricing · Vapi Series B post: https://vapi.ai/blog/series-b · https://finance.yahoo.com/sectors/technology/articles/vapi-raises-50m-series-b-130000518.html
- https://www.retellai.com/pricing · https://www.retellai.com · https://www.ycombinator.com/companies/retell-ai
- https://developers.openai.com/api/docs/pricing · https://developers.openai.com/api/docs/guides/realtime · https://developers.openai.com/api/docs/guides/realtime-webrtc
- https://deepgram.com/pricing · https://deepgram.com/blog/daily-pipecat-deepgram · https://techcrunch.com/2026/10/29/deepgram-acquires-pipecat-maker-daily-to-bolster-its-voice-ai-infrastructure · https://dealroom.co/news/168036-deepgram-buys-pipecat-maker-daily-to-own-more-of-the-voice-ai-stack/ · https://github.com/orgs/deepgram/discussions/1710
- https://elevenlabs.io/pricing · https://elevenlabs.io/pricing/agents
- https://www.bland.ai/pricing · https://www.agora.io/en/pricing/ · https://www.twilio.com/en-us/voice/pricing
- `[lookup: OpenAI Realtime API WebRTC update December 17 2024 announcement]` (original openai.com URL migrated; date multi-confirmed, canonical URL not pinned)

### History and WebRTC background
- Alvestrand, "Google release of WebRTC source code", W3C public-webrtc list, 2011-06-01. https://lists.w3.org/Archives/Public/public-webrtc/2011May/0022.html
- Wikipedia, "WebRTC", accessed 2026-11-14. https://en.wikipedia.org/wiki/WebRTC
- Wikipedia, "LiveKit", accessed 2026-11-14. https://en.wikipedia.org/wiki/LiveKit (tertiary; the source of the French Visio claim and one of the disputed Series A details)
- InfoQ, "OpenAI Launches Public Beta of Realtime API", 2024-10. https://www.infoq.com/news/2024/10/realtime-api-openai/
- 36Kr, "OpenAI enters voice model battle with GPT-Realtime", 2025-08. https://eu.36kr.com/en/p/3443307829794176
- iThome, "OpenAI gpt-realtime and Realtime API SIP support", 2025-08. https://www.ithome.com.tw/news/170913
- Fora Soft, "WebRTC architecture guide: P2P, SFU, MCU", 2026. https://www.forasoft.com/blog/article/webrtc-architecture-guide-for-business-2026

### Method and citation-chain references
- Nasser, attribution-bias argument for not vote-ruling disputed findings, arXiv:2601.05114 (2026), as specified in the synthesis recipe.
- METR, "Measuring the Impact of Early-2025 AI on Experienced Open-Source Developer Productivity", arXiv:2507.09089 · METR, "Measuring AI Ability to Complete Long Software Tasks", arXiv:2503.14499v4 · Xie et al., OSWorld, arXiv:2404.07972 · Xu et al., TheAgentCompany, arXiv:2412.14161 · Villalobos et al., "Will We Run Out of Data?", arXiv:2211.04325 · Acemoglu, "The Simple Macroeconomics of AI", NBER WP 32487 (2024) · Shumailov et al., *Nature* 631 (2024) (all cited within the Via Negativa working paper's backtest spine, not read independently here)

---

## 8. Method note and limits

- **What this document is.** A composition of four lens reports written in the same run. It adds cross-checking, consensus counting and dispute adjudication. It adds no new sources, and no fact appears here that is not in at least one lens.
- **What it is not.** Independent verification. Where a lens says it fetched a page, this document trusts that; nobody re-fetched anything for the synthesis. The two places that would most benefit from a re-fetch are the OpenAI Realtime guide (D1) and the LiveKit pricing page (which has no effective date).
- **Stale-by-construction risks.** Prices and product tiers move monthly. The $0.0479/min example and every competitor rate are good for 2026-11-14 only. Funding rounds move quarterly. The absorption debate moves with each model release.
- **The single strongest and single weakest inputs.** Strongest: lens-codex's file-and-line evidence (`agent_session.py:584`, the `Router` interface, the org-listing absence of a control plane), because it is checkable. Weakest: the market-sizing triads, because none of the three houses discloses a method and their spread exceeds any growth rate worth quoting.
- **What would change the picture most.** One number: LiveKit's speech-to-speech versus cascaded minute mix. A second: OSS-to-Cloud conversion with NRR. Neither is public, and both are named in the gaps section rather than guessed at.
