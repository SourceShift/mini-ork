"""CLI entrypoint for the Python dispatch layer (Phase-0 wiring, ADR-001).

    printf '%s' "$prompt" | python3 -m mini_ork.dispatch <model> --out <file>

Reads the prompt from STDIN (never argv — E2BIG-proof), dispatches via
mini_ork.dispatch.dispatch_model, writes the assistant text to --out (or
stdout), persists an llm_calls row when MINI_ORK_DB is set, and exits with the
provider's exit code so a bash caller (lib/llm-dispatch.sh MO_DISPATCH_BACKEND)
sees a faithful rc.

Usable standalone today; it is also the bridge a future MO_DISPATCH_BACKEND=
python switch in lib/llm-dispatch.sh will delegate to (that live-path flip,
with its sidecar-contract integration, is a separate slice).
"""

from __future__ import annotations

import argparse
import os
import sys

from .models import DispatchRequest
from .providers import dispatch_model, provider_for_model
from .telemetry import persist_artifact, persist_call
from .transcripts import write_exec_transcript


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini_ork.dispatch", add_help=True)
    parser.add_argument("model", help="lane name, e.g. codex / opus / glm")
    parser.add_argument("--out", default="", help="write assistant text here (default: stdout)")
    parser.add_argument("--feature", default=os.environ.get("MO_LANE_FEATURE", "mini-ork:dispatch"))
    parser.add_argument("--tier", default=os.environ.get("MO_LANE_TIER", "default"))
    parser.add_argument("--actor", default=os.environ.get("MO_LANE_ACTOR", ""))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("MO_NODE_TIMEOUT_S", "1500")))
    args = parser.parse_args(argv)

    prompt = sys.stdin.read()
    # `model` may be a comma-separated fallback chain, e.g. "minimax,codex,sonnet":
    # a hung/flaky primary lane is abandoned and the next is tried, so one bad
    # lane can't stall the run (MO_DISPATCH_ATTEMPT_TIMEOUT_S bounds each try).
    lanes = [m.strip() for m in args.model.split(",") if m.strip()]
    request = DispatchRequest(model=lanes[0], prompt=prompt, timeout_s=args.timeout)
    if len(lanes) > 1:
        from .providers import dispatch_with_fallback
        _att = os.environ.get("MO_DISPATCH_ATTEMPT_TIMEOUT_S")
        result = dispatch_with_fallback(
            request, lanes,
            per_attempt_timeout_s=float(_att) if _att else None)
    else:
        result = dispatch_model(request)

    # Emit the assistant body to the caller (out-file or stdout).
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(result.text)
        except OSError as exc:
            sys.stderr.write(f"[mini_ork.dispatch] could not write {args.out}: {exc}\n")
            return 127
    else:
        sys.stdout.write(result.text)

    # Sidecar contract the bash executor + reward path depend on (parity with
    # lib/llm-dispatch.sh mo_llm_dispatch): <out>.cost per-call cost, <out>.err.log
    # on failure, and $MINI_ORK_RUN_DIR/.last-llm-cost + .last-llm-duration-ms
    # (read by mini_ork/cli/execute.py:_trace_write_node_rich and the D-022 cost
    # charger). Best-effort — never changes the exit code.
    if args.out:
        try:
            with open(args.out + ".cost", "w", encoding="utf-8") as fh:
                fh.write(f"{result.cost_usd:.6f}")
        except OSError:
            pass
        if not result.ok and result.error:
            try:
                with open(args.out + ".err.log", "w", encoding="utf-8") as fh:
                    fh.write(result.error.rstrip() + "\n")
            except OSError:
                pass
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if args.out:
        try:
            write_exec_transcript(args.out, args.model)
        except Exception as exc:  # transcript generation must never break dispatch
            sys.stderr.write(f"[mini_ork.dispatch] transcript write failed: {exc}\n")
    if run_dir and os.path.isdir(run_dir):
        try:
            with open(os.path.join(run_dir, ".last-llm-cost"), "w", encoding="utf-8") as fh:
                fh.write(f"{result.cost_usd:.6f}")
            with open(os.path.join(run_dir, ".last-llm-duration-ms"), "w", encoding="utf-8") as fh:
                fh.write(str(result.duration_ms))
        except OSError:
            pass

    # Persist telemetry (best-effort; never changes the exit code).
    db = os.environ.get("MINI_ORK_DB", "")
    call_id: int | None = None
    if db:
        try:
            call_id = persist_call(
                db,
                result,
                provider=provider_for_model(args.model),
                feature_name=args.feature,
                tier=args.tier,
                actor=args.actor or None,
                run_id=os.environ.get("MINI_ORK_RUN_ID") or None,
                traceparent=os.environ.get("MO_TRACEPARENT") or None,
            )
        except Exception as exc:  # telemetry must never break dispatch
            sys.stderr.write(f"[mini_ork.dispatch] telemetry write failed: {exc}\n")

    # Register run_artifacts rows for any agent-* file the bash layer already
    # wrote into MINI_ORK_RUN_DIR (agent-<node>.transcript.json +
    # agent-<node>.stream.jsonl). The Python path is the future
    # MO_DISPATCH_BACKEND=python switch; today the bash mirror at
    # _mo_llm_persist_agent_transcript also registers the same files, so the
    # UNIQUE(run_id, node_id, kind, rel_path) constraint keeps the row count
    # at 1 even if both paths fire.
    if db and run_dir and call_id is not None:
        run_id = os.environ.get("MINI_ORK_RUN_ID") or ""
        node_id = os.environ.get("MO_NODE_ID") or ""
        if run_id and node_id:
            safe_node = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in node_id
            )
            for kind, filename in (
                ("turn_jsonl", f"agent-{safe_node}.stream.jsonl"),
                ("transcript", f"agent-{safe_node}.transcript.json"),
            ):
                abs_path = os.path.join(run_dir, filename)
                if os.path.isfile(abs_path):
                    try:
                        persist_artifact(
                            db,
                            run_id=run_id,
                            node_id=node_id,
                            call_id=call_id,
                            kind=kind,
                            rel_path=filename,
                            abs_path=abs_path,
                        )
                    except Exception as exc:  # telemetry must never break dispatch
                        sys.stderr.write(
                            f"[mini_ork.dispatch] run_artifacts write failed "
                            f"for {kind}: {exc}\n"
                        )

    status = "ok" if result.ok else f"FAIL rc={result.rc}"
    sys.stderr.write(
        f"[mini_ork.dispatch] {args.model} {status} "
        f"in={result.usage.input_tokens} out={result.usage.output_tokens} "
        f"cost=${result.cost_usd:.6f} {result.duration_ms}ms\n"
    )
    if not result.ok and result.error:
        sys.stderr.write(result.error.rstrip() + "\n")
    return result.rc


if __name__ == "__main__":
    raise SystemExit(main())
