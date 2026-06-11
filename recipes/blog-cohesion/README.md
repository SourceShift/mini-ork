# Recipe: blog-cohesion

5-lens cohesion audit of an **existing** blog-post draft. Each lens
examines the post from a distinct structural angle routed to a
DIFFERENT model family (thesis=GLM / bridge=Codex / topic=Opus /
entity=MiniMax / rhythm=Kimi). An Opus arbiter reconciles overlapping
suggestions and emits a diffable `applied_post.md` + `apply_log.md`.

Companion to `blog-post` (which DRAFTS from a kickoff); this recipe
POLISHES an already-written draft.

## When to use

- A draft is in `draft: true` state and needs structural tightening
  before publish.
- The user said "polish", "tighten", "smooth", "restyle", "cohesion
  pass", "run cohesion", or "blog cohesion" on a specific `.md` file.
- You want reviewer-grade structural feedback delivered as a single
  applied diff, not a series of inline comments.

## When NOT to use

- Fresh drafting from scratch → use `blog-post` recipe.
- AI-tell removal on already-clean prose → use `humanizer`.
- Internal `.md` → public blog conversion → use `md-to-blog-post`.
- Quick voice-pass with no diff machinery → just edit manually.

## Dispatch

```bash
mini-ork run blog-cohesion path/to/kickoff.md
```

The kickoff names the absolute post path. See `example-kickoff.md`.

## Cost

Per `cost_model` in `task_class.yaml`:
- Min: $1.50
- Max: $4.50
- Per lens: $0.50 (5 lenses + 1 arbiter + 1 verifier)

Runtime: 8–25 min wall-clock depending on lens dispatch parallelism
and arbiter input size.

## Outputs

- `${MINI_ORK_RUN_DIR}/applied_post.md` — modified post (user `cp`s back)
- `${MINI_ORK_RUN_DIR}/apply_log.md` — applied / rejected / carve-outs log
- `${MINI_ORK_RUN_DIR}/context-{thesis,bridge,topic,entity,rhythm}.json` — lens reports
- `${MINI_ORK_RUN_DIR}/plan.json` — planner output

## Verifier gate

`verifiers/cohesion-completeness.sh` enforces:
1. All 5 lens JSONs parse + carry a top-level `verdict` field
2. `applied_post.md` present + non-empty + frontmatter preserved
3. `apply_log.md` present with all 4 required sections

## Architecture

```
                  ┌─────────┐
       kickoff ──▶│ planner │ (sonnet)
                  └────┬────┘
                       │   POST_PATH: <abs>
                       ├──────────┬──────────┬──────────┬──────────┐
                       ▼          ▼          ▼          ▼          ▼
                    thesis     bridge      topic      entity     rhythm
                    (GLM)     (Codex)     (Opus)    (MiniMax)   (Kimi)
                       └──────────┴──────────┴──────────┴──────────┘
                                              │
                                              ▼
                                       arbiter (Opus)
                                       writes applied_post.md
                                              │
                                              ▼
                                cohesion-completeness verifier
                                              │
                                       ┌──────┴───────┐
                                       ▼              ▼
                                   publisher      rollback
```

## Heterogeneity rationale

Per Rajan 2025 (arxiv:2511.16708) + Nasser 2026 (arxiv:2601.05114):
multi-lens reviews catch additive gaps only when pairwise correlation
between lenses stays low. Each of the 5 structural lenses routes to a
DISTINCT model family by construction. The names of the lens roles
(thesis/bridge/topic/entity/rhythm) are stable; the model mapping is
governed by `config/agents.yaml`.

The arbiter is Opus (reviewer lane). Opus also runs the `topic`
lens — different prompts, different jobs (analyzer vs reconciler).
The heterogeneity claim is about the panel of analyzers, not the
arbiter.

## Migrated from

This recipe ports the logic of the legacy
`scripts/blog-cohesion/cohesion-pipeline.sh` shell dispatcher (which
ran in the blog repo itself) into the mini-ork recipe framework. The
old dispatcher's 7 prompts mapped roughly 1:1 onto these 7 (planner
is new; figures was dropped — handled by the separate
`blog-post-enhance` skill).
