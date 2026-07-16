---
title: 'The Self-Report Gap: I Audited 4,785 Agent Claims Against What Actually Ran'
description: 'An agent claimed a verification it never ran — 90% of the time. I measured it against the tool-call record, and the culprit turned out to be the trigger, not the model.'
pubDate: '2026-07-14'
draft: true
previewSlug: '61d2154b76c058b0'
tags: ['agents', 'memory', 'evaluation', 'contextnest', 'introspection']
authors: ['amir-khakshour', 'lukas-brandt']
---

Codex told me it had verified something. It hadn't. Not "it ran the wrong command" — it had run **no commands at all**, in the entire session, before writing `"verification": ["Compared both chapter-1 sandbox outputs"]` into its structured memory block.

Then I checked the other 4,784 blocks. What I found made me rewrite the conclusion twice — the second time because I'd blamed the wrong thing.

## Why there's a memory at all

Every coding session produces knowledge that dies with it — what broke, what I'd already tried, which file was the one that mattered. So I built [ContextNest](https://github.com/SourceShift/ContextNest): a local Rust service that stores what past sessions learned and hands it back to the next one.

Storage was never the hard part. **What to store** was. A raw transcript is 50,000 lines of tool spam; what the next agent needs is small — what did you decide, what broke, what did you verify, what did you ship.

## The idea that looked clever

Have the agent tell me. At the end of every turn, each agent emits a structured block that types the turn into fields:

```json
{
  "goal": "wire the retry loop into the dispatcher",
  "decisions": ["exponential backoff over fixed — the API rate-limits in bursts"],
  "failures": ["first attempt deadlocked; the lock was held across the await"],
  "verification": ["cargo test --lib dispatcher → 14 passed"],
  "delivered_features": [{"feature": "retry loop", "files": ["src/dispatch.rs"]}]
}
```

A hook types each field into a memory kind and ingests it. Zero human labeling. The agent knows what it just did better than any parser could — it has the intent, the reasoning, the discarded options.

That last sentence is the mistake. It took two months to see it, and I only saw it by going back and grading every block against what actually ran.

## The audit

I wrote a script the agent can't talk its way past: walk the transcript, record every `Edit`, `Write`, and shell call, then check each block's claims against them.

**96 sessions. 4,785 blocks. 46 Claude, 50 Codex. Same criteria for both.**

| Defect | Claude | Codex |
|---|---|---|
| Claimed shipping a feature, no edit anywhere in the session | 0.2% | **24.4%** |
| Edited files, then didn't record which files | **26.3%** | 13.6% |
| Claimed a verification, **no command ever ran** | 0.0% | **90.2%** |
| Read files, left `read_context` empty | 10.4% | 0.0% |

They fail in **opposite directions**. Claude under-records what it did. Codex over-claims what it didn't.

<figure>
  <img
    src="https://storage.googleapis.com/libwit-static-asset/blog-heroes/the-self-report-gap-fig-1.jpeg"
    alt="A mirrored bar chart around a horizontal axis labeled 'what actually ran'. Claude's bars extend below the line — under-records, with 'files not recorded 26%' marked. Codex's bars extend above it — over-claims, with 'fabricated verification 90%' and 'phantom features 24%' marked."
    loading="lazy"
  />
  <figcaption>The two agents miss the same ground truth from opposite sides: Claude leaves out work it did, Codex reports work it didn't do.</figcaption>
</figure>

The per-session distribution is the part that stops you: **26 of 50 Codex sessions are 100% defective on verification claims. Only 2 are clean.** On the Claude side, **44 of 44 sessions are clean** on that same axis — not one fabricated verification.

## I got this wrong three times first

The 90% looked so implausible I assumed it was my bug. It took three failed attempts to convince me otherwise, and every one of them was *my* script:

| Hypothesis | Test | Result |
|---|---|---|
| My parser missed Codex's commands | Codex wraps shell as `{"cmd":"..."}`, not `cmd: "..."` — fixed the regex | Number didn't move |
| One turn streamed as many blocks | Counted real turn boundaries | 0.8 blocks per turn — one per turn, by design |
| My "user prompt" detector was wrong | Counted them | I was treating **4,028 <span class="term" data-def="Transcript entries typed as 'user' that carry a tool's output back to the model — not anything a human typed. Every tool call produces one, so they vastly outnumber real prompts.">tool-return envelopes</span> as user prompts** |

Two of my defect rates came back as *exactly* 0% and *exactly* 100%. That is not a finding. **A defect rate that lands on a round number across every bucket is a tell for a broken measurement**, and both times it was. Hold onto that — it turns out to be the whole story.

## Then I stopped scripting and read one transcript

Four minutes of reading beat three script iterations, and it produced a different diagnosis.

Here is the actual session timeline. `V` is a block claiming a verification. `E` is a shell command. `D` is a coordination call.

```
zVVVVVVVVVVVzVVVVVzEEDEEEEEDEDEEEDEEDEDDEDEEDEDEDEDEDDEEEDDV
└──── 17 verification claims ────┘└──── all 24 shell commands ────┘
```

**Every claim comes before every command.** Sixteen of the seventeen had zero commands before them.

The work *does* happen — 24 shell calls in that session. But the agent **writes the report before doing the work**. The `verification` field describes what it is *about to* do. It isn't inventing tools it never had; it's **reporting in the future tense**, and my extractor stores it as past tense.

No script found that. It took reading the timeline in order.

## It wasn't dishonesty — it was the trigger

My first draft said the divergence was in the models: Codex the fabricator, Claude the amnesiac. Then I tested a duller hypothesis — that I'd configured the trigger wrong — and it held. Across 20 recent Codex sessions:

- **93% of its insight-turns did no verifying work at all.**
- **83% of those no-work turns still emitted a verification claim.**
- **Every turn that did real work claimed verification correctly — 21 of 21.**

The bug is the trigger. I built a form that demands a verification report on turns where the honest answer is "nothing to report," and then I was surprised the form got filled in. Read those numbers together and the mechanism is right there: when Codex actually runs a command, it reports honestly (21 of 21); the fabrication is concentrated entirely on the turns where it did *nothing*, because the instruction I gave both agents says *emit this block every turn, including short ones*. A generative model handed a mandatory `verification` field on an empty turn will fill it. That is a model doing exactly what it was told, not a model lying.

## I'm not the first to hit this

Once I had the name for the shape, the literature had it too — and at a scale my 96 sessions can't touch.

The failure mode has a term now: <span class="term" data-def="An agent asserts a task is complete when the environment state shows it isn't — a confident 'done' with no work behind it. Named and measured in Kabra et al., 2026.">**false success**</span>. [Characterizing False Success in LLM Agents](https://arxiv.org/abs/2606.09863) measures it across **9,876 + 1,879 trajectories** with text-independent ground truth — agents *"asserting task completion when the environment state shows otherwise"* — and finds it accounts for 45–48% of failures in some settings. My 90% is one corner of a documented, corpus-scale problem.

The mechanism I read off that one timeline has a name too. [When Agents Commit Too Soon](https://arxiv.org/abs/2606.22936) calls it <span class="term" data-def="An agent settles on one reading of the task early, then spends the rest of the run defending it. Final-answer scoring can't see it because the process has already collapsed to a fixed path.">premature commitment</span>: *"they settle on one reading of the evidence early, then spend the rest of the run defending it… final-answer scoring misses the failure mode."* Writing the report before the work is the same collapse, one turn wide.

Underneath all of it sits the reason a sterner prompt can't fix this: **models don't have privileged access to their own execution.** [Can LLMs Introspect? A Reality Check](https://arxiv.org/abs/2605.26242) argues apparent introspection isn't distinguishable from surface pattern-matching; [Song et al.](https://arxiv.org/abs/2503.07513) found 21 models fail to introspect even about their own knowledge; and last month, [Can LLMs Reliably Self-Report Adversarial Prefills?](https://arxiv.org/abs/2606.23671) found *no* model reliably recognizes when its own prior response was manipulated. When an agent tells you what it did, it is not reading a log — it is **generating text that sounds like what it probably did.** I wasn't collecting a report. I was collecting a second inference pass, from the same distribution as the first, with worse information.

The <span class="term" data-def="Generating a plausible account of what you should have done, after the fact, rather than reporting what you actually did — the model's stated reasoning need not reflect the computation behind it. Shown to happen on ordinary prompts, no adversarial setup needed.">post-hoc rationalisation</span> framing ([2508.19827](https://arxiv.org/abs/2508.19827), [2503.08679](https://arxiv.org/abs/2503.08679)) wasn't wrong; it explains *why* a self-report is unreliable. It just doesn't tell you to stop asking for one.

## The fix isn't a better narrator — it's a receipt

The obvious next move — the one I nearly built — is a worker that reads the turns and produces the insights instead of the agent. That's the same mistake wearing a hat: a summarizer is still an LLM writing a self-report, one with *less* context than the agent had.

The transcript already holds the truth, free and impossible to hallucinate. [From Agent Traces to Trust](https://arxiv.org/abs/2606.04990) puts the principle plainly: *"final-answer accuracy alone cannot explain how an output was produced, which evidence supported each claim."* So the extractor stops asking and starts deriving:

- **Which files were edited?** Every `Edit`/`Write` call, with its path — not the agent's memory of them.
- **Did it ship anything?** Whether an edit happened at all.
- **Did it verify?** Whether a command ran — and *when*, relative to the claim.

A verification whose command runs *after* the block is tagged <span class="term" data-def="ContextNest's provenance tiers: observed (a matching command ran and passed), claimed (self-report, no command cited), absent (cited a command that never ran), contradicted (the command ran and failed). Retrieval down-weights everything below observed.">`claimed`</span>, not `observed`, and ranks below things that actually happened. A feature claiming files that were never touched grades `absent`. None of it needs the model's cooperation.

<figure>
  <img
    src="https://storage.googleapis.com/libwit-static-asset/blog-heroes/the-self-report-gap-fig-2.jpeg"
    alt="A four-tier vertical ladder with a trust arrow pointing up its left side. From top: OBSERVED — command ran and passed, with a receipt icon; CLAIMED — self-report, no command cited, dashed outline; ABSENT — cited a command that never ran, faded; CONTRADICTED — command ran and failed, marked with a warning triangle."
    loading="lazy"
  />
  <figcaption>The trust ladder: a claim's rank depends on whether a receipt exists in the transcript, not on how confident the claim sounds.</figcaption>
</figure>

One caveat the newest work insists on, and I'll pass it along: a receipt is *stronger* evidence than a self-report, not *incorruptible*. [Trust No Tool](https://arxiv.org/abs/2605.17453) shows tool feedback itself can be adversarial. The tool-call record is the best ground truth in the transcript — it is not an oracle. So this is a trust ladder, not a lie detector.

## What shipping it actually taught me

Two things I didn't expect.

First, when I went to enforce provenance in my own store, I found the grader had been **blind the whole time**. Claude nests tool calls inside a message; Codex emits each as its own top-level event. My index only understood the Claude shape — so for *every* Codex session, the edit set came back empty and every claim entered ungraded. The grader wasn't scoring Codex badly. It couldn't see Codex at all. A grading system that silently can't see its evidence looks exactly like one that finds nothing wrong.

Second, I wanted to know whether "it's the trigger, not the model" survives a model swap. So I ran the same fix task through four models under one harness — Opus, [Kimi K2.7](https://github.com/SourceShift/mini-ork), MiniMax-M3, GLM-5.1 — with a contract that *doesn't* demand a verification field every turn. Then the sandbox denied their verify command: the exact condition that used to manufacture a fabricated claim.

Not one fabricated. GLM wrote *"verification command denied twice by permission layer — user must run the confirm command manually."* MiniMax filed a structured request telling me which command to run. Three vendors, the same temptation, and every model declined to claim what it hadn't done. It's one task each — a portability check, not a statistic. But the receipt-gating carried, and the fabrication didn't return once the trigger stopped forcing it.

## The line worth keeping

Split what you ask an agent to report by whether a receipt exists.

**Objective** — which files, did it ship, did it verify. A receipt is in the transcript. Derive it. Never ask.

**Subjective** — what was I trying to do, what did I decide, what am I assuming. No receipt exists; the agent is the only witness.

And here is the part that surprised me: on the subjective fields, both agents are **flawless**. `goal` and `progress` were correct in **all 4,785 blocks**, both models, zero misses. They are excellent at telling you what they were trying to do, and unreliable about what they mechanically did — which is precisely the part you never needed them for.

The detection fingerprint is the same one that caught my own scripts landing on 0% and 100%: **a blind instrument's silence is indistinguishable from a verdict.** An agent asked to verify on an empty turn, a grader that can't parse a whole class of session, a review gate with no diff to read — each returns something confident and wrong, and none of them raises an error. Before you trust any reading, prove the instrument could have seen the thing it's reporting on. If a field in your schema could have been proven from the transcript and you're still asking the model for it, that field is fiction waiting to happen.
