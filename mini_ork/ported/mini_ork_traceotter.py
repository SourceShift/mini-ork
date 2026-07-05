"""Python port of bin/mini-ork-traceotter — distill this repo's own run
trajectories with TraceOtter and surface grounded values.

Strangler-fig parity port of the thin integration wrapper. Resolves the
TraceOtter venv, runs the distill pipeline (subprocess into TraceOtter's own
env — it is a separate package), then renders analytics / skills / dataset. The
render functions read the produced OUT dir and are transcribed verbatim from the
bash so they're testable against a fixture without a live distill.

    main(argv=None) -> int   # 0 ok / 1 distill-fail / 2 not-configured
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter


def _bar():
    return "─" * 60


def render_analytics(out: str, runs: str) -> str:
    eps = [json.loads(l) for l in open(f"{out}/episodes.jsonl")]
    rep = json.load(open(f"{out}/report.json"))
    n = len(eps)
    cost = sum(e["outcome"].get("costUsd", 0) for e in eps)
    tc = sum(e["outcome"].get("toolCalls", 0) for e in eps)
    te = sum(e["outcome"].get("toolErrors", 0) for e in eps)
    st = Counter(e["outcome"]["status"] for e in eps)
    tp = Counter(str(e["outcome"]["testsPassed"]) for e in eps)
    imit = sum(1 for e in eps if e["labels"].get("shouldImitate"))
    ps = [e["labels"]["processScore"] for e in eps]
    avg = (sum(ps) / len(ps)) if ps else 0
    bar = _bar()
    lines = [
        bar,
        f" mini-ork ⟶ TraceOtter   ({n} run trajectories distilled)",
        bar,
        f" real cost         ${cost:,.2f}   (mean ${cost/n if n else 0:.3f}/run)",
        f" tool reliability  {100*(1-te/tc) if tc else 0:.1f}%  ({tc:,} calls, {te:,} errors)",
        f" outcome           completed {st.get('completed',0)} · partial {st.get('partial',0)} · failed {st.get('failed',0)}",
        f" tests (parsed)    passed {tp.get('True',0)} · failed {tp.get('False',0)} · none {tp.get('None',0)}",
        f" process_score     mean {avg:.3f}  (grounded: tool-reliability + grounding + no-fail)",
        bar,
        f" distilled skills  {rep['skills']}",
        f" SFT-ready         {rep['llamafactory']['examples']} examples  (→ train a local lane)",
        f" clean-imitate     {imit}  (would-imitate: clean completions worth training on)",
        bar,
        " next:  mini-ork traceotter skills   |   mini-ork traceotter dataset",
        f" data:  {out}/  (episodes, skills, quality report, llamafactory/)",
    ]
    return "\n".join(lines) + "\n"


def render_skills(out: str) -> str:
    s = json.load(open(f"{out}/skills.json"))
    lines = [f"# {len(s)} distilled skills (recurring procedures mined from your runs)", ""]
    for x in sorted(s, key=lambda k: -(k.get("support") or len(k.get("sourceEpisodeIds", []))))[:20]:
        n = x.get("support") or len(x.get("sourceEpisodeIds", []))
        proc = " → ".join(x.get("procedure", [])[:2])
        lines.append(f"[{n:>4} eps] {x.get('skillId','?')}: {proc[:110]}")
    return "\n".join(lines) + "\n"


def render_dataset(out: str) -> str:
    lf = json.load(open(f"{out}/report.json"))["llamafactory"]
    return (f"SFT examples: {lf['examples']}\n"
            f"dataset:      {lf['dataset']}\n"
            f"train:        {lf['train_command']}\n")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    to = os.environ.get("TRACEOTTER_HOME", "/Volumes/docker-ssd/ps/TraceOtter")
    py = os.path.join(to, ".venv", "bin", "python")
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    runs = os.path.join(home, "runs")
    out = os.path.join(home, "traceotter")
    mode = argv[0] if argv else "analytics"

    if not (os.path.isfile(py) and os.access(py, os.X_OK)):
        sys.stderr.write(f"[traceotter] not found at {to} (.venv). Set TRACEOTTER_HOME.\n"); return 2
    if not os.path.isdir(runs):
        sys.stderr.write(f"[traceotter] no runs at {runs} — run some mini-ork tasks first.\n"); return 2

    src_args = ["--mini-ork", runs]
    if mode == "--all":
        src_args = ["--claude", os.path.expanduser("~/.claude/projects"), "--mini-ork", runs]
        codex = os.path.expanduser("~/.codex/sessions")
        if os.path.isdir(codex):
            src_args += ["--codex", codex]
        mode = "analytics"

    sys.stderr.write(f"[traceotter] distilling {runs} → {out} ...\n")
    if subprocess.run([py, "-m", "traceotter.cli", "--json", "pipeline", *src_args, "--out", out],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        sys.stderr.write("[traceotter] distill failed\n"); return 1

    if mode == "skills":
        sys.stdout.write(render_skills(out))
    elif mode == "dataset":
        sys.stdout.write(render_dataset(out))
    else:
        sys.stdout.write(render_analytics(out, runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
