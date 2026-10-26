# feat(apply): matched-attempt control arm for probe scoring (G06-T03)

A candidate now gets credit for a probe task only if it solves one that
none of N blind retries of the unmutated recipe solved
(MO_APPLY_PROBE_CONTROL_N, default 3; 1 = previous single-baseline
behaviour). Budget exhaustion mid-control returns unscored (quarantine).
Evidence: blind resampling beats self-repair under placebo control
(arXiv 2607.26117). Built via mini-ork framework-edit (glm/minimax) +
one repair pass; verified manually (scoped tests + full suite).

Co-Authored-By: Claude <noreply@anthropic.com>

