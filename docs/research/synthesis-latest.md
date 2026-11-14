# R4 Synthesis: LiveKit people, engineering culture, and the API design review round

Run `run-1794659005-72818`, composed 2026-11-14 from four lens reports:
`lens-glm.md`, `lens-kimi.md`, `lens-codex.md`, `lens-opus.md`.

## How to read this, and one structural caveat

The lens set is heterogeneous by design, and that shapes what "consensus" can mean here.

- `lens-glm` is the only lens that makes person-level claims. `lens-codex` explicitly refuses to (its closing section: "Anything about Theo Moneng or the CTO's identity... must be sourced from LiveKit-owned pages"), and `lens-opus` reports a null search result and makes exactly one quoted person claim (opus §6, final note).
- `lens-kimi` deliberately makes zero LiveKit claims (kimi §7: "Nothing in this lens establishes anything about LiveKit").

Consequence: for **any LiveKit-specific fact**, a 4-lens consensus marker is unreachable by construction, because one lens abstains on principle. The ceiling for company facts is ★★. ★★★ appears in this document only for industry-practice claims that all four lenses can legitimately speak to. This is not a weakness in the findings; it is the lens design, and it should not be hidden behind a confident-looking star count.

Requested-section mapping: the recipe asked for TL;DR, consensus, dispute, gaps, recommendations, manifest. Because the plan's verifier also requires a per-person verified/inferred split, a rubric, and an ask-list, those sit in Sections 4, 5 and 6, between dispute and gaps.

**Lessons applied.** Every input was read in full before this file was written (lesson `t:th-8c78c5d797d5`), and this is the single auditable artifact reading all four lens outputs (lesson `t:th-1e42cc51c68c`). No prior-run artifacts were consulted.

---

## Section 1: TL;DR

1. **The two interviewers resolve to Théo Monnom (agents and WebRTC) and David Zhao (co-founder and CTO), and the identity evidence is strong but rests on one lens.** `theomonnom` and `davidzhao` both appear in the livekit GitHub org People section and both self-describe the affiliation; the CTO match is first-name-plus-title inference, which is exactly the ambiguity the plan flagged. (glm §0; corroborated only for the GitHub accounts by codex, source index note) Confidence: **high on the accounts, medium on "the David you are meeting is this David"**.

2. **LiveKit's observable engineering instinct is additive evolution with a migration surface, and they separate "breaking" from "behavior change" in their own release tooling.** The agents repo's release-note generator defines the two categories operationally, deprecations carry a `target_version`, and one server break was staged across releases with a cross-reference back to the warning. (codex units 1, 5, 9, 8; opus §2 and §4; glm §4) Confidence: **high** as a description of repository practice, **medium** as a prediction about the interview.

3. **Nothing public describes LiveKit's API design review, so the round's rubric is unknowable from outside and must be asked about, not assumed.** No design-doc template, no RFC description, no API-shape linting policy, and the one checked-in review rubric (`REVIEW.md`) governs PII in logs, not API shape. (glm §7.1; codex unit 12 and its "could not establish" section) Confidence: **high** (this is an absence claim, well bounded by all four lenses).

4. **The rubric that scores well in an API review is small, well documented, and industry-standard, not company-specific:** naming and consistency, resource versus verb fit, evolvability, versioning plus a deprecation clock, error model, pagination, idempotency, auth defaults, DX. (kimi §2; opus §2; codex "what this means for the round"; glm sources 25 and 26) Confidence: **medium-high**: it is normative canon, and the field's own reviewers say it is thin (kimi caveats 1, 8).

5. **The differentiator in the round is not flaw-finding but cost-of-change reasoning with a migration path attached.** Review outcomes are dominated by alignment and improvement, not defect spotting: defect comments were 14% of 570 sampled comments against 29% for code improvement. (kimi §2 citing Bacchelli and Bird 2013; opus §3 and recommendations 1 and 4; codex inference block) Confidence: **medium-high**.

---

## Section 2: Consensus findings

Markers: ★ = 2 lenses, ★★ = 3 lenses, ★★★ = 4 lenses. Lens IDs are cited inline.

### C1 ★★★ The review rubric is a small, shared, named set of axes

All four lenses converge on essentially the same list, from four different directions: kimi from 15 analyzed writeups across Google, Microsoft, Stripe, Kubernetes, GitHub, GitLab, Go, Rust and one peer-reviewed field study; opus from the historical Twilio/Stripe/Google lineage; glm from the Google API Design Guide and Zalando's guidelines; codex by measuring LiveKit's practice against those axes. The axes that recur: **resource modeling, naming and consistency, evolvability, versioning and deprecation, error model, pagination, idempotency and retry safety, auth defaults, latency/async contract, DX and docs** (Kimi-2 table; Opus §2; GLM-25, GLM-26; Codex convergent patterns 1 to 3).

What this licenses: Amir can open a critique by naming the axis, and it will land. What it does not license: claiming LiveKit adopts any of these. See R2.

### C2 ★★ LiveKit prefers an additive escape hatch to a forced migration

Three lenses, three kinds of evidence. Codex measures it: `Room` split into `CoreRoom` plus extensions and shipped as a **minor** with the old entry point intact and the new one labeled experimental (codex unit 8); protocol fields are added rather than repurposed (unit 7); TURN config compatibility is staged rather than cut (unit 1); four migration guides exist for the 1.0 break, each stating the old version's Cloud status (unit 10). Opus reads the same instinct as the conventional model working as advertised, and separately notes the 1.0 rebuild was the correct exception (opus §2, §4 edge case 2). Glm records the migration guides and the breaking-change handling from the docs side (glm §4).

Ceiling note: glm is partly corroborating, kimi abstains by design, so this is ★★, not ★★★.

### C3 ★★ The house vocabulary distinguishes "breaking" from "behavior change", and a deprecation carries a removal target

This is the single most actionable consensus in the document, because it is a vocabulary match, not a fact match.

- Codex unit 5 quotes the operational definitions in `.github/release_notes.py`: `## Breaking changes` is "code or config that worked on the previous release fails or silently does something else"; `## Behavior changes` is "raised dependency floors, new defaults and corrected behavior". Codex unit 9 finds `target_version` support in the deprecation decorator, so a warning can name the removal version.
- Kimi's canon says the same thing in standards language: Kubernetes gives each stability level a **clock** (beta: 9 months or 3 minor releases, whichever is longer) and a machine-readable signal; Azure forbids keeping a preview in preview for more than a year; GitHub publishes a hard dated retirement and returns `410 Gone` (Kimi-9, Kimi-4, Kimi-10).
- Opus recommendation 4: never propose a breaking change without a migration story that has a number in it.

The convergence is on the *structure* (classify, then date the removal, then publish the migration), not on the numbers. LiveKit's numbers are different from every canon source: the TURN break gave self-hosters 23 days (codex unit 1). Do not quote Azure's five minutes or Kubernetes' nine months as if they were LiveKit's.

### C4 ★★ The round rewards obligation-and-cost reasoning over style taste

Kimi's empirical anchor: review comments are dominated by improvement and alignment, not defects (defect comments 78 of 570, 14%; improvement 165 of 570, 29%; Kimi-2 citing Bacchelli and Bird 2013). Opus's theoretical frame reaches the same place from Hyrum's Law: the contract you ship is the union of everything users rely on, so the reviewer's real question is "what are we promising, and can we afford it" (opus §3). Codex's inference from LiveKit's own process is that a critique which names the category and proposes the additive path matches their observed instinct (codex, "What this means for the API review round").

Practical shape: "Rename this" is a comment. "Renaming this breaks N clients, here is the deprecation path, and here is why the rename is or is not worth that cost" is the staff answer (opus §3).

### C5 ★★ Prefer "a versioning scheme, chosen and documented" over "the correct versioning scheme"

Kimi caveat 5 is the hard evidence: Serbout and Pautasso analyzed roughly 603,000 OpenAPI specifications from 2015 to 2023 and found more than 50 distinct version identifier formats in active use, across metadata-based, URL-based, header-based and dynamic schemes. There is no settled right answer.

Codex shows LiveKit itself running at least three schemes at once, and deviating from semver on purpose: semver declared in the Go server changelog (unit 2); a fixed cross-language changeset group pinning the Go module and npm package together (unit 7); patch-first with human-gated minor and major in agents (unit 4); and a wire-field removal (`FunctionCall.extra`) shipped as a **patch** for both packages because ecosystem coordination beat semver purity (unit 7).

So the defensible move in the room is to ask what the scheme is, what the support window is, and what the removal signal is. Not to argue for date-based versioning because Stripe does it.

### C6 ★ Measurement and methodology get published, not just features

Glm finds the Agent Simulations post (repeatable simulation-based testing for voice agents, with a named customer case study, 2026-10-08) and the built-in agent test framework with LLM judges in the agents README (glm sources 21 and 10). Codex finds `eot-bench`, an open benchmark for end-of-turn detection with a public leaderboard and a public Hugging Face dataset in 14 languages, "the open benchmark for end-of-turn detection" (codex unit 14).

Two lenses, independent surfaces, same observation: they publish the ruler, not only the score. This is the strongest culture signal available, and it is also the best rapport material, because it is specific and checkable rather than flattering.

### C7 ★ Machine-checkable conventions should be automated so human review stays on judgment

Kimi's field study: 20 of 24 API designers used linters (FxCop, Clang-Tidy, custom) explicitly so that human review could focus on "intent, customer workflows, usable naming decisions" rather than "the annoying stuff" (Kimi-14, Murphy et al. 2018). Codex finds the same split in LiveKit's actual pipeline: an automated reviewer (`devin-ai-integration[bot]`) comments on every sampled PR before human approval, Renovate handles dependencies, and a release-gate workflow enforces two bot-authored approvals mechanically (codex units 12 and 13).

Note what this does *not* say: automation is not a substitute for review here, it is a first pass. The human approval still came, about six hours later on the sampled PR (codex unit 13).

---

## Section 3: Disputed findings

Five genuine disagreements. No vote-counting: three lenses agreeing does not make a disputed item settled, and per the recipe's citation of Nasser 2026, voting between same-conviction agents amplifies bias rather than averaging it.

### D1. Does LiveKit have strong compatibility discipline, or a compressed semver signal that hides breaks?

- **Side A (discipline):** Glm reads the migration guides and staged TURN removal as "breaking-change handling exists" (glm §4). Codex units 8 and 10 read `CoreRoom` and the four migration guides as careful, consumer-first handling. Opus §2 treats the semver discipline as "exactly what the mainstream model predicts a serious framework team does".
- **Side B (compressed signal):** Codex units 4 and 7 and divergent patterns 1 and 2 argue the opposite from the same repo: "almost every change ships as a patch, including changes users experience as behavioral breaks", and a wire-field removal was marked `patch`. Codex's own divergent-patterns list explicitly sets the server against the framework: the server will remove compatibility after one release, the framework optimizes for never surprising a deployed agent.

- **Why they disagree (my judgment):** three separate causes, all visible in the evidence. (i) *Different repos*: the server and the Python framework have different consumer populations with different upgrade models. A self-hoster can be told "change your TURN config in 23 days"; a deployed production agent cannot be surprised. (ii) *Different risk budgets*: codex unit 1 states the trade-off, security won over migration comfort. (iii) *Different instruments*: side A measured migration artifacts (guides, staged notes), side B measured version numbers. Those two instruments are not comparable, and neither lens said so.

- **What would resolve it:** take one concrete change and trace whether a downstream consumer actually broke. The `FunctionCall.extra` removal (codex unit 7) is ideal because the changeset names the exact affected versions, agents 1.7.0 to 1.8.8. Check those release notes for user reports. If nobody broke, side B's "semver is compressed" is a stylistic complaint, not a reliability problem.

### D2. Resource modeling or verbs, for a realtime session API?

This is the disagreement most likely to decide the interview, because LiveKit Agents is verb-shaped.

- **Side A (resources):** Kimi's canon is explicit. AIP-121: model the API as a hierarchy of named resources with standard methods, keep the resource model independent of storage, relationships acyclic (Kimi-2). Murphy et al. treat usable naming and domain-accurate structure as the human-review core (Kimi-14).
- **Side B (verbs):** Opus dissent one: "a realtime agent control plane is verbs all the way down: start a session, interrupt, hand off, call a tool, cancel a generation. Forcing those into nouns produces custom methods that lie about what they do." Opus edge case one gives the concrete failure: a `sessions/{id}/replies` model implies the reply is durable and addressable, when in a voice agent it is a timing-sensitive event that can be preempted mid-flight, so the resource model hides cancellation, which is the property that matters.

- **Why they disagree:** artifact class, and kimi's own sources contain the reconciliation that kimi's summary under-weights. AIP-121 permits custom methods precisely for behavior that does not fit standard semantics, and states that custom methods typically use POST with the action in the URI (Kimi-2). So the purist reading of side A is a slightly stronger claim than its own primary source makes. The real axis is not "nouns versus verbs" but "is the thing you are naming durable and addressable, or is it an event on a live connection".

- **What would resolve it:** the identity of the artifact under review. If the round is on the HTTP Cloud API (the Cloud API was announced 2026-11-12, glm source 22), side A applies almost fully. If it is on the Python or JS SDK session surface, side B applies and a resource-model critique would make the API worse. Ask which. See A3.

### D3. Does the round measure a rubric, or measure taste and call it a rubric?

- **Side A (it measures a rubric):** Kimi §2 states the public record "converges on a specific, testable review object": whether the API's shape can survive its own success. Twelve named axes, each grounded in a source.
- **Side B (nobody has shown it measures anything):** Opus §5: "The field asserts that review produces better APIs, and almost nobody measures it." Opus edge case four adds the mechanism: a rubric scored on taste selects for mirroring the interviewer, because two reviewers from different schools will disagree about the same API.

- **Why they disagree:** they are answering different questions and neither lens says so. Kimi describes what reviewers are *instructed to check* (a descriptive claim about process documents, and kimi's own caveat 1 concedes 12 of 15 sources are normative rather than empirical). Opus asks whether checking *predicts outcomes* (a predictive-validity claim). Both can be true: the rubric is a real shared instrument and an unvalidated one.

- **What would resolve it:** opus §5's proposed study, reviewers scoring the same six planted-flaw fragments, measuring inter-rater reliability and then predictive validity against the APIs those reviewers actually approve over twelve months. It does not exist in public. Until it does, treat the rubric as a shared language for arguing, not a scoring function (opus §3).

### D4. Théo Monnom's tenure and rank

- **Side A:** A LinkedIn profile snippet reads "Founding Engineer, Feb 2022 to present" (glm source 2). Glm grades this "plausible inference from login-walled snippet", medium confidence.
- **Side B:** Glm's own dissent section notes the public output footprint (GitHub, X bio, zero talks found) "reads quieter than a typical founding engineer at a $1B company" (glm §6). Opus ran its own searches on 2026-11-14 and found "no page matching the product lead's name", so it makes no factual claim at all (opus method note).

- **Why they disagree:** provenance and channel bias, not facts. Side A rests on a login-walled snippet that was not read on the page. Side B is an argument from absence, and absence is exactly what a builder who does not give talks produces. Both are consistent with the same person.
- **What would resolve it:** public, checkable repo data that neither lens collected: his earliest commits to `livekit/livekit` or `livekit/agents`, and his org join date. That would date the affiliation without LinkedIn. Alternatively, ask him. Nine months of commit history is a better tenure signal than a self-written headline anyway.

### D5. What is David Zhao's current public position?

- **Side A (publicly active):** Glm source 6 records X posts about LiveKit's WebRTC stack, turn detection and interruption handling, i.e. current technical obsessions in his own words.
- **Side B (publicly quiet on strategy):** Glm §7.5: his long-form public record "stops around 2022" and the 2025 to 2026 narrative voice is CEO Russ d'Sa. Opus adds one investor characterization from Index Ventures but no first-person recent material.

- **Why they disagree:** surface and role division. Short-form posts are current; long-form strategy is not his channel; the CEO is the company's narrative voice. These are compatible. The dispute is really about what a candidate may legitimately *source*, and the honest answer is: the abstraction-and-rigor characterization, attributed to Index Ventures, plus a 2022-era technical position on end-to-end WebRTC.
- **What would resolve it:** nothing accessible without X login. Ask him what he thinks the hard API problem is right now. That is a better use of the data anyway.

---

## Section 4: People dossier (verified versus inferred)

Hard constraint c:0 applies: no statement about a named individual that is not publicly sourced. Everything below is either a quoted or directly observed public artifact, or explicitly labeled inference. Where evidence is thin, the entry says so rather than rounding up.

**Standing caveat on both people:** this dossier is single-lens. It comes almost entirely from `lens-glm`, with one quoted characterization from `lens-opus` and one account-level corroboration from `lens-codex`. Under the consensus rules in Section 2 that is ★, not consensus. Two lenses independently searched and one (opus) found nothing for the product lead. Treat this whole section as one careful reading of public records, not as triangulated fact.

### 4.1 Théo Monnom (the kickoff's "Theo Moneng")

Name resolution: glm §0 resolved the kickoff spelling to **Théo Monnom**, accented, double n, final m, on the strength of the GitHub handle `theomonnom`, org membership, and the X handle `@theomonnom`. No public person named "Theo Moneng" surfaced at LiveKit or anywhere else. Confidence high on the spelling.

#### VERIFIED (public record, observable 2026-11-14)

| Claim | Source | Trace |
|---|---|---|
| GitHub account `theomonnom` exists and its bio reads "Building @livekit" | github.com/theomonnom, observed 2026-11-14 | glm source 1 |
| Listed in the livekit GitHub org People section | github.com/livekit, observed 2026-11-14 | glm source 9 |
| X account `@theomonnom`, bio "voice ai @ livekit - interested in CG & ML" | x.com/theomonnom, public bio snippet only | glm source 3 |

That is the whole verified set. Note how small it is: three self-descriptions and one org membership. Everything a candidate would actually want to know is inference.

#### INFERRED (labeled, with the reason it is not verified)

| Inference | Basis | Why it stays inferred |
|---|---|---|
| Founding engineer at LiveKit since Feb 2022 | LinkedIn snippet "Founding Engineer, Feb 2022 to present" | Login-walled; snippet only; section D4 disputes the reading |
| Prior role: software engineer at MEDIAWIND, 2020 to 2021 | Same LinkedIn snippet | Same |
| Core contributor to `livekit/agents` | Org membership plus public activity on the repo | Contribution rank never measured by any lens |
| Sole contributor to `livekit/mediadevices` | GitHub analytics snippet surfaced in search | Not confirmed on the page |
| Involved in MCP support on the agents roadmap | An open MCP-support issue exists in `livekit/agents` | Glm links it to him at medium confidence; the lens does not show he authored it. Weakest inference in the table |

#### NOT FOUND (absence of evidence, stated as such)

No talks, conference appearances, podcast episodes, blog posts, or authored bylines. Glm searched; opus searched and reported a null result. This is not evidence he has never spoken publicly, and it must not be presented as such. It does mean there is no public long-form material to study for rapport or for predicting his review style.

The one thing that *is* usable in the room is his self-described interest in computer graphics and ML (glm source 3), because it is his own words and it is the kind of detail a genuine question can be built on.

### 4.2 David Zhao (the CTO)

Name resolution: glm §0 matches "David, the CTO" to **David Zhao**, the only David publicly holding the CTO title at LiveKit. Confidence high, but explicitly an inference from first name plus title, which is the exact ambiguity the plan's risk note flagged. It appears in the ask-list (A1) for that reason.

#### VERIFIED (multiple independent public sources)

| Claim | Source | Trace |
|---|---|---|
| Co-founder and CTO of LiveKit | LinkedIn headline snippet; livekit org People; WebRTC Live #72 billing; funding coverage naming him co-founder alongside CEO Russ d'Sa | glm sources 5, 9, 7, 18, 19 |
| Founded LiveKit in 2021 with Russ d'Sa (CEO); YC alum | Funding coverage plus episode billing | glm sources 7, 18 |
| GitHub account `davidzhao` is in the livekit org | github.com/livekit People, observed 2026-11-14 | glm source 9 |
| The account `davidzhao` approves pull requests in `livekit/agents` | Approving reviewer on PR #8544 | codex, closing section, cited as an **account observation, not an identity claim**. This is the one place codex and glm touch the same object |
| Index Ventures characterizes him as "a thoughtful, rigorous technical architect with a particular knack for designing the right abstractions that developers intuitively love" | Index Ventures, "See, hear, speak: investing in LiveKit" | opus §6 final note; URL returned HTTP 200 when re-checked 2026-11-14 for this synthesis |

The Index quote is the most useful single sentence in this dossier, and it is also the most dangerous one to overuse. It is an **investor's** characterization in a fund's own investment announcement. It is attributable, but it is marketing-adjacent, and it describes him as of the round, not necessarily today. Use it as a hypothesis about what he probes for, never as a fact about how he will behave.

#### INFERRED

| Inference | Basis | Why it stays inferred |
|---|---|---|
| Previously Senior Venture Partner at Pioneer Fund | LinkedIn snippet | Login-walled, snippet only |
| Argued publicly for end-to-end WebRTC architectures (WebRTC Live #72, 2022-10-19) | Episode confirmed by two independent search results | Glm could not resolve a canonical URL: both plausible slugs 404'd, so it left a `[lookup:]` rather than a guessed link. Episode existence medium-high; content medium |
| Writes technical posts on WebRTC topics | Post titles surfaced in search on the byline-less blog and webrtc.ventures | Authorship attribution is weak: the LiveKit blog carries **no bylines at all** (glm source 13), so no post can be attributed to him |
| Currently posts about turn detection and interruption handling | X snippet | Profile snippet only; feed not readable without login |
| An abstraction-focused reviewer is more likely to probe whether a design can be defended under pressure than whether a style guide can be recited | Opus interpretation of the Index quote, explicitly labeled interpretation | This is inference on inference. Useful as a plan, not as a fact |

#### NOT FOUND

No first-person long-form strategy statements from 2025 or 2026. Glm §7.5: the recent narrative voice is the CEO. No confirmed authorship of any LiveKit blog post, because the blog has no bylines. No public description of how he runs a design review.

---

## Section 5: API review rubric (industry practice, not LiveKit fact)

**Read this label first.** Nothing in this rubric is a LiveKit fact. It is assembled from Google AIPs, the Azure guidelines, Stripe's engineering posts, the Kubernetes deprecation policy, GitHub's versioning docs, GitLab's style guide, the Rust API guidelines, and one peer-reviewed field study (Kimi-1 through Kimi-14). LiveKit's internal rubric is not public (glm §7.1, codex "could not establish"). Walk in with the rubric as a shared language, and with A2 as the question that would confirm or replace it.

### 5.1 The axes, and what a reviewer is actually checking

| Axis | What the reviewer is checking | Anchor |
|---|---|---|
| **Resource modeling** | Are the nouns the domain's nouns or the database's nouns? Are relationships acyclic? Does the surface look like the domain or the storage layer? | Kimi-2 (AIP-121); Kimi-14; kimi §2 |
| **Method fit** | Does each operation map to a standard method? Where a custom method is used, is the action in the URI rather than in a new verb? | Kimi-2 (AIP-121, AIP-130 to 136) |
| **Naming and consistency** | Does a term mean the same thing in the field name, the docs and the SDK? Is the vocabulary consistent across the whole corpus? | Kimi-1 (AIP-100 corpus consistency); Kimi-12 (GitLab entity and tag rules); Kimi-14 |
| **Evolvability** | Can this be extended without breaking clients? Can a rename be additive? Can a required field ever be added? | Kimi-3 (AIP-180); Kimi-12; Kimi-13 (Rust C-SEALED) |
| **Versioning and deprecation** | What is the scheme, the support window, the removal signal, the migration path? | Kimi-5, Kimi-10 (24 months, `Sunset`, 410); Kimi-9 (clocks and telemetry); Kimi-4 (api-version rules) |
| **Error model** | Is the failure contract specified, stable, machine-readable, and actionable? Are error codes contractual? | Kimi-4 (`x-ms-error-code` is contractual); RFC 9457 |
| **Pagination** | Designed in from the start, explicit page size, opaque continuation token? | AIP-158; Kimi-14 (pagination was the recurring concrete worry) |
| **Idempotency and retry safety** | Can every mutating operation be retried? Is the key or repeatability contract explicit, with a stated window? | Kimi-6 (Stripe idempotency); Kimi-4 (5-minute minimum window) |
| **Auth defaults** | Are the safe defaults default? Is the dangerous flow opt-in? | RFC 9700 (PKCE always, exact redirect match); Kimi-4 |
| **Latency and async contract** | Which operations are slow, and do they have an explicit async or LRO contract with `retry-after` and a retention window? | Kimi-4 (p99 over one second triggers LRO; monitor kept 24 hours) |
| **DX and documentation** | Can a new consumer get to a working call without asking a human? Is the error text actionable? | Kimi-1 (user-perspective review); Kimi-14 |
| **Reviewability** | Is the surface small enough to review, and does the design admit a mechanical check where one exists? | Kimi-4; Harmon 2023 |

### 5.2 What good critique sounds like

Assembled from the sources, not invented (kimi §2 "what good critique sounds like", plus opus §3):

1. **Name the axis, then the instance.** "This breaks the compatibility rule in AIP-180, specifically because the rename is a removal plus an addition and the old field is not kept" beats "this name is confusing" (Kimi-1, Kimi-3).
2. **State the obligation, not the preference.** "Retries of this endpoint cannot be made safe without an idempotency key" beats "you should probably add one" (Kimi-6).
3. **Propose the concrete alternative with the number attached.** "Return a `problem+json` body with a stable `type` URI" or "an opaque cursor with a page size of 50" beats "improve the error handling" (RFC 9457; AIP-158).
4. **Split must-change from improvement from question, out loud.** Haufe-Lexware's published process classifies every review comment into exactly these three (Kimi-15). Google's reviewer guide does the same with the "Nit:" convention and the rule not to block a change over personal preference (Kimi-8).
5. **Say what you did not check.** Google's guide asks reviewers to disclose the scope of their coverage. "I focused on the compatibility surface and did not look at the auth flow" reads as senior, not as hedging (Kimi-8).
6. **Attach the migration cost to every proposed break.** "Renaming this breaks N clients, here is the deprecation path, and here is why it is or is not worth that cost" (opus §3).
7. **Prefer the additive escape hatch where the design allows it.** This is the one place where the rubric meets LiveKit's observed instinct (codex convergent pattern 4). It is still inference from the repo, not a stated policy.

### 5.3 Two calibration notes

- **Do not argue for a specific versioning scheme.** With more than 50 formats in active use across ~603,000 specs, "date-based is correct" is not defensible. Argue that *a* scheme is chosen, documented, and paired with a support window and removal signal (Kimi caveat 5).
- **Do not present the rubric as validated.** Twelve of kimi's 15 sources are normative documents or retrospectives, not studies. The field's own systematic review reports 236 of 369 primary studies (63.9%) evaluate only Java systems, with stale datasets (Kimi caveat 8). "Google says" and "a study found" are different registers and should not share a sentence (kimi caveat 1).

---

## Section 6: Ask, don't assume

Twelve questions, each tied to the gap that makes it unanswerable from public sources. Sections A1 to A3 are the verifier's required minimum.

**A1. CTO identity confirmation (required).** "Just to make sure I have the right person: you're David Zhao, co-founder and CTO?" The match is high confidence but rests on first name plus title (glm §0, plan risk note). Ask it as a light confirmation in the first minute, not as a challenge.

**A2. The review round's format (required).** What is the artifact: an existing API, a new one, a PR diff, a written design? Is it reviewed live in front of the panel, or prepared and presented? How long? Nothing public describes this. The only third-party loop account is an aggregator claiming four stages over about 14 days with a take-home and Ashby as ATS, methodology unknown and not engineering-specific (glm source 23). Treat it as a single weak datum.

**A3. Which surface, specifically (required, and it decides D2).** HTTP Cloud API, the Python agents framework, the JS client SDK, or the wire protocol? The three have different consumer models and different correct answers on resource modeling. Note the timing: a new Cloud API shipped 2026-11-12, two days before the research (glm source 22), so it is a natural and current object.

**A4. Team structure and headcount (required).** How many engineers, how are they split, who owns the SDKs versus the server versus agents? Entirely unpublic (glm §7.6). This one matters more than usual for Amir: his differentiator is cross-team influence, and a team too small to have cross-team decisions is a structural mismatch, which is the preference signal already learned from the paretos withdrawal.

**A5. Who reviews API design, as distinct from code.** Is there a named reviewer or group, or is it whoever is in the PR? Codex's `CODEOWNERS` is one line, `* @livekit/agent-devs`, and `REVIEW.md` governs PII in logs rather than API shape (codex unit 12). Azure runs a Stewardship Board and Google runs mandatory beta review (Kimi-4, Kimi-1); whether LiveKit has an equivalent is unknown.

**A6. The deprecation clock.** What is the minimum support window, and is there a written policy? Kubernetes publishes clocks per stability level, GitHub publishes 24 months and a dated end, Azure caps preview at a year (Kimi-9, Kimi-10, Kimi-4). LiveKit's observed behaviour is 23 days for a TURN config break (codex unit 1). Ask what the rule is, because the observed instance is short and it would be wrong to infer a policy from it.

**A7. The automated reviewer.** Who is `devin-ai-integration[bot]` for, and what is it gating? It comments on every sampled PR before human approval, which is a genuine outlier versus the usual dependency-bot setup (codex unit 13, divergent pattern 4). This is also excellent rapport: it is specific, checkable, and shows you read the repo.

**A8. The patch-first policy.** `AGENTS.md` says patch by default and minor or major only on explicit human confirmation, and the policy lives in the file coding agents read (codex unit 4). What problem was that solving? The answer will tell you whether they are managing an automated contributor population, a release-cadence problem, or consumer trust.

**A9. The `FunctionCall.extra` call.** A wire-field removal shipped as a `patch` for two fixed-group packages, with the changeset naming the downstream versions that break (codex unit 7). Was that deliberate ecosystem coordination, and would they do it again? This is the single best question in the list for an API-design conversation, because it asks them to defend a real deviation from the discipline they otherwise follow.

**A10. The 1.13.0 release-object drift.** The changelog entry exists and the tag exists, but the GitHub release object does not (codex unit 3). Is that a known tagging gap, or a one-off? Glm's lesson applies: a careful reviewer notices this kind of thing, and asking about it is a low-cost way to demonstrate you actually read the repo rather than the marketing.

**A11. What the last API they broke was, and what it cost** (opus recommendation 8). Frame it as genuine curiosity about a case every framework eventually faces, not as a gotcha.

**A12. The open-core boundary as a design constraint.** LiveKit describes itself as the stack between foundation models and applications (Series C post, glm source 19) while the README promises the whole stack can run on your own servers (opus §6 recommendation 7). Which capabilities must stay self-hostable, and how does that constrain the API? This is the best product-lead question available, because it is a real tension in their own public material.

---

## Section 7: Cross-lens gaps (what no source answers)

Aggregated from the gaps each lens reported, plus one composition gap visible only at synthesis level.

1. **LiveKit's internal API review process.** No design-doc template, no RFC process description, no API-shape linter, no review rubric for API design (glm §7.1; codex unit 12 and closing section). The strongest single gap in the dossier.
2. **Any first-person SWE interview account.** No engineering blog post, no Reddit thread, no HN comment describing a LiveKit engineering loop. The take-home claim in an aggregator page (glm source 23) is the only loop-shaped datum and it is low confidence.
3. **Person-level bylines.** The LiveKit blog carries no author names, so no post can be attributed to either interviewer, and neither person's writing style is studyable through the company's own publications (glm §7.3, source 13).
4. **Théo Monnom's voice at length.** No talks, podcasts, or posts found by either of the two lenses that searched (glm §7.4; opus method note, null result).
5. **David Zhao's current strategy positions.** His long-form public record stops around 2022; the 2025 to 2026 narrative belongs to the CEO (glm §7.5).
6. **Engineering headcount and team structure** (glm §7.6). Unpublic anywhere.
7. **Whether API design review predicts anything.** No inter-rater reliability study, no predictive-validity study. Opus §5 designs the experiment that would settle it; it does not exist.
8. **The field's evidence base is thin and skewed.** 236 of 369 primary studies (63.9%) evaluate only Java systems; 13 papers use datasets older than 2015; three named open challenges remain (Kimi caveat 8, citing Lamothe et al. 2021).
9. **"No breaking changes" is aspirational, not descriptive.** Lercher et al. 2023 interviewed 17 practitioners across 11 companies and found the two central problems are tight organizational coupling and consumer lock-in, with consumer reliance on outdated versions and ineffective change communication as recurring failures (Kimi caveat 6). Any claim that a compatibility discipline is easy should be checked against this.
10. **Review depth per PR.** GitHub does not expose review comment counts anonymously, so "they review carefully" cannot be measured, only inferred from artifacts like `REVIEW.md` and the release gate (codex closing section).
11. **Whether the 1.13.0 release-object drift is systematic.** One instance found; a systematic count was not run (codex dead ends).
12. **Composition gap, synthesis judgment.** The entire API review canon kimi assembled is about HTTP/REST resource APIs, plus one Rust library checklist (Kimi-13). Not one of the 15 writeups covers reviewing a **realtime, duplex, session-oriented SDK** or a decorator-based agent framework. That is precisely the artifact class Amir is most likely to be handed. The rubric in Section 5 transfers, but the two axes that matter most for a realtime surface, cancellation and interruption semantics, and the latency budget, have no canon entry behind them. Kimi-4's LRO and `retry-after` material is the nearest thing and it is about polling, not preemption. This is a genuine hole in the field, and it is also Amir's opportunity: he has run money-handling and IoT systems where cancellation and partial failure were the whole problem.

---

## Section 8: Numbered recommendations

Each is falsifiable: the "wrong when" clause names the observation that would make it bad advice.

1. **Confirm A1, A2 and A3 before writing a single line of prep content beyond this dossier.**
   Supported by: all four lenses (glm §5, codex closing section, kimi §7, opus method note all independently say the round's format is unpublic).
   Wrong when: the recruiter has already sent a format description that contradicts this synthesis. Then amend, do not redo.

2. **Never call the Section 5 rubric "LiveKit's rubric" in the room.** Say "the way I'd review this" and let them supply their own frame.
   Supported by: kimi caveat 10, glm §5, plan risk note.
   Wrong when: LiveKit publishes or states a rubric during the round. Then adopt theirs immediately and drop yours; mirroring a stated rubric is not a loss of independence.

3. **Prepare at least three critiques in LiveKit's own vocabulary: which category (breaking versus behavior change), whether there is an additive escape hatch, and what the removal target and migration path are.**
   Supported by: codex units 1, 5, 8, 9 and convergent pattern 4; glm §4.
   Wrong when: the interview is on a surface where LiveKit has no observed pattern, for example a brand-new greenfield API with no consumers. Then "nobody depends on it yet" is the correct migration story and the vocabulary is overhead.

4. **Name your school before you critique.** State whether you are reviewing contract-first or DX-first, then give the verdict.
   Supported by: opus recommendation 3 (Twilio hide-the-complexity lineage against Google's resource model, both publicly documented).
   Wrong when: the interviewer wants one verdict and reads meta-commentary as evasion. In that case verdict first, school in one trailing sentence.

5. **Attach a number to every proposed break.** Not "this should be renamed" but "this rename touches N call sites, here is the path, here is why it is worth it".
   Supported by: opus recommendations 1 and 4; kimi §2 critique style; AIP-180's remove-and-add rule (Kimi-3).
   Wrong when: the API is unreleased. Then "no consumers yet" is the honest number and padding it is worse than saying so.

6. **Open with the must-change / improvement / question split out loud, and label advisory comments as advisory.**
   Supported by: Kimi-15 (Haufe-Lexware's three-way classification) and Kimi-8 (Google's "Nit:" convention, do not block over preference). Single lens, so this is a recommendation, not a consensus finding.
   Wrong when: the round is explicitly a rapid-fire critique exercise where structure costs time. Keep the labels, drop the preamble.

7. **Ask A7, A9 and A10.** The bot reviewer, the `FunctionCall.extra` patch, and the 1.13.0 release-object drift. These are the three questions that prove you read the repository rather than the blog, and A9 is a genuine design trade-off they had to make.
   Supported by: codex units 3, 7, 13 (divergent patterns 2 and 4).
   Wrong when: either person is not close to the agents or server repos. Then these land as trivia; pivot to A12 instead.

8. **Treat the open-core boundary as a design constraint in the product conversation, not a business footnote.**
   Supported by: opus recommendation 7; glm source 19 (Series C framing) against the README's self-hosting promise.
   Wrong when: the product-lead conversation is explicitly about metrics and roadmap. Then the constraint conversation is a detour; ask it of the CTO instead.

9. **Do not argue that date-based versioning is correct.** Argue that a scheme exists, is documented, and has a window and a removal signal.
   Supported by: kimi caveat 5 (Serbout and Pautasso, ~603,000 specs, more than 50 formats); codex units 4 and 7 show LiveKit deliberately deviating from semver for ecosystem reasons.
   Wrong when: they ask directly what you would choose. Then choose, state the conditions, and name what would make you choose differently. A defended choice is fine; a universal claim is not.

10. **Volunteer the operational surface the design does not cover: timeout, retry, duplicate delivery, partial failure, and for a realtime session, cancellation and preemption.**
    Supported by: opus recommendation 6; kimi §1 entry on RFC 9457; and the composition gap in Section 7 item 12, which is where a realtime candidate can add something the canon does not have.
    Wrong when: the interviewer parks operations to keep the round on surface design. Note it in one sentence and move on.

11. **Do not lean on the Index Ventures quote as if it were a fact about how David Zhao reviews.** Use it as a hypothesis and let the first ten minutes correct it.
    Supported by: opus §6 final note, which is explicit that the implication is interpretation; the quote itself is an investor's characterization in an investment announcement.
    Wrong when: he opens by describing his own review priorities. Then discard the hypothesis immediately; his self-description outranks an investor's.

---

## Section 9: Source manifest

Every URL, arxiv ID and repo cited across the four lenses. Grouped by the lens that introduced it. Marked `[synthesis-checked 200]` where this synthesis re-verified reachability on 2026-11-14. Items marked `[lookup]` are deliberate non-guesses by a lens: the source was not confirmed and no URL was invented.

### lens-glm (breadth)

1. https://github.com/theomonnom
2. https://www.linkedin.com/in/theomonnom (login-walled; cited from search snippet)
3. https://x.com/theomonnom (profile snippet only)
4. https://github.com/livekit/agents/issues/1865
5. https://www.linkedin.com/in/davidzhao (login-walled; snippet)
6. https://x.com/davidzh (snippet only)
7. `[lookup: webrtc.ventures "WebRTC Live #72" canonical URL]`. Both plausible slugs returned 404; episode confirmed by two independent search results. No URL fabricated.
8. https://open.spotify.com/episode/7AgLAB67OjilBN60snQZoa
9. https://github.com/livekit
10. https://github.com/livekit/agents
11. https://github.com/livekit/agents/releases
12. https://docs.livekit.io/agents/
13. https://livekit.com/blog
14. https://livekit.com/blog/openai-livekit-partnership-advanced-voice-realtime-api
15. https://openai.com/index/introducing-the-realtime-api
16. https://news.ycombinator.com/item?id=41743327
17. https://news.ycombinator.com/item?id=31447046
18. https://livekit.com/blog/livekits-series-b
19. https://livekit.com/blog/livekit-series-c
20. https://livekit.com/blog/announcing-agent-builder-a-pathway-to-code
21. https://livekit.com/blog/testing-voice-agents-with-agent-simulations
22. https://livekit.com/blog/introducing-livekit-cloud-api
23. https://www.theantijobboard.com/interview/livekit-ashby (third-party aggregator, low confidence)
24. https://www.glassdoor.com/Interview/LiveKit-Interview-Questions-E10388730.htm (design/marketing track only; do not project onto the SWE loop)
25. https://docs.cloud.google.com/apis/design
26. https://github.com/zalando/restful-api-guidelines
27. https://app.dealroom.co/news/feed/livekit-raises-45m-in-series-b

### lens-kimi (canon and research)

28. https://google.aip.dev/100
29. https://google.aip.dev/121
30. https://google.aip.dev/180 `[synthesis-checked 200]`
31. https://github.com/microsoft/api-guidelines/blob/vNext/azure/Guidelines.md
32. https://stripe.com/blog/api-versioning
33. https://stripe.com/blog/idempotency
34. https://stripe.dev/blog/payment-api-design
35. https://google.github.io/eng-practices/review/reviewer/looking-for.html
36. https://kubernetes.io/docs/reference/using-api/deprecation-policy/
37. https://docs.github.com/rest/overview/api-versions
38. https://go.googlesource.com/proposal/+/master/README.md
39. https://go.dev/doc/go1compat
40. https://docs.gitlab.com/development/api_styleguide/
41. https://rust-lang.github.io/api-guidelines/checklist.html
42. https://rust-lang.github.io/api-guidelines/future-proofing.html
43. https://www.cs.cmu.edu/~NatProg/papers/p249-murphy.pdf (Murphy et al., VL/HCC 2018; DOI `[lookup]`)
44. https://github.com/Haufe-Lexware/api-style-guide/blob/master/api-design-review-process/api-design-review-process.md
45. https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/ICSE202013-codereview.pdf (Bacchelli and Bird 2013)
46. https://arxiv.org/abs/2311.08175 (Lercher et al. 2023)
47. https://souhaila-serbout.me/pdfs/2023_ICWE_Versioning_JWE-4.pdf (Serbout and Pautasso 2023/2024)
48. https://dl.acm.org/doi/10.1145/3470133 (Lamothe et al. 2021)
49. https://www.infoq.com/articles/api-design-review/ (Harmon 2023)
50. https://chuniversiteit.nl/blog/science/api-governance-at-scale (Ahmad et al. 2024, summarized)
51. https://github.com/zalando/restful-api-guidelines/blob/main/chapters/design-principles.adoc
52. Standards annex (cited by ID, not URL): RFC 9110, RFC 9457, RFC 8594, RFC 9700, RFC 2119/8174, RFC 3339, RFC 4122, semver 2.0.0, AIP-125/130/131-136/158/160/193, AIP-1/AIP-2/AIP-200.

### lens-codex (repository survey)

53. https://github.com/livekit/livekit
54. https://github.com/livekit/livekit/releases/tag/v1.12.0
55. https://github.com/livekit/livekit/releases/tag/v1.13.1
56. https://github.com/livekit/livekit/blob/master/CHANGELOG.md
57. https://github.com/livekit/agents/blob/main/AGENTS.md
58. https://github.com/livekit/agents/blob/main/REVIEW.md
59. https://github.com/livekit/agents/blob/main/CONTRIBUTING.md
60. https://github.com/livekit/agents/blob/main/.github/release_notes.py
61. https://github.com/livekit/agents/blob/main/.github/workflows/release-gate.yml
62. https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/utils/deprecation.py
63. https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/metrics/usage_collector.py
64. https://github.com/livekit/agents/releases/tag/livekit-agents@1.8.0
65. https://github.com/livekit/agents/pull/8539
66. https://github.com/livekit/agents/pull/8544
67. https://github.com/livekit/agents/pull/6340
68. https://github.com/livekit/agents/issues/8464
69. https://github.com/livekit/protocol
70. https://github.com/livekit/protocol/blob/main/.changeset/config.json
71. https://github.com/livekit/protocol/blob/main/.changeset/remove-function-call-extra.md
72. https://github.com/livekit/client-sdk-js
73. https://github.com/livekit/client-sdk-js/blob/main/CHANGELOG.md
74. https://github.com/livekit/client-sdk-js/blob/main/.changeset/config.json
75. https://github.com/livekit/python-sdks
76. https://github.com/livekit/eot-bench
77. https://github.com/livekit/agent-skills
78. https://docs.livekit.io/llms.txt
79. https://docs.livekit.io/llms-full.txt
80. https://docs.livekit.io/sitemap.xml
81. https://docs.livekit.io/reference/llms.txt
82. https://docs.livekit.io/reference/migration-guides/v0-migration/python.md

### lens-opus (narrative)

83. https://www.twilio.com/en-us/blog/developers/new-era-for-twilio-documentation
84. https://stripe.com/blog/api-versioning (same object as 32; cited independently by both lenses)
85. https://google.aip.dev/180 (same object as 30)
86. https://www.indexventures.com/perspectives/see-hear-speak-investing-in-livekit-and-the-future-of-realtime-applications/ `[synthesis-checked 200]`
87. https://livekit.com/blog/livekits-series-b (same object as 18)
88. https://livekit.com/blog/livekit-series-c (same object as 19)
89. https://docs.livekit.io/reference/migration-guides/v0-migration/python/ (same object as 82)
90. https://docs.stripe.com/api/versioning
91. https://www.hyrumslaw.com/ `[synthesis-checked 200]`
92. https://www.rfc-editor.org/rfc/rfc9457.html
93. https://github.com/livekit/agents (same object as 10)

**Cross-lens overlap:** items 10/93, 18/87, 19/88, 30/85, 32/84, 82/89 are the same objects cited independently by two lenses. That overlap is itself mild corroboration, and it is the only place two lenses touched the same source. It does not extend to any person claim, which is why Section 4 stays single-lens.
