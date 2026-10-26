# fix(trace): implementer code_region from target-repo diff, not .mini-ork (#166)

The implementer node traced files_written=[impl.log] (a .mini-ork/runs path).
infer_trace_code_region relativized that to code_region='.mini-ork' whenever
MINI_ORK_RUN_DIR was unset at trace time, so every implementer trace landed in a
phantom '.mini-ork' bucket and collapsed the per-region GRPO advantage slice
(lane_region_advantage) the flywheel learns from.

Seed files_written from the TARGET repo's git-visible changes for implementer
nodes only (git diff --name-only + untracked via ls-files --exclude-standard, so
.mini-ork/runs artifacts stay out), falling back to impl.log when there are none.
The region now resolves from the edited source dir. Bash + python parity, plus a
regression test that reproduces the exact scenario (impl.log under
.mini-ork/runs, MINI_ORK_RUN_DIR unset) and asserts region=='app'.

Produced by mini-ork dogfooding itself: framework-edit run-1783839606-83585.
