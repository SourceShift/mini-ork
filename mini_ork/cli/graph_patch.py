"""``mini-ork graph-patch`` — inspect and apply typed run-graph patches.

Verbs::

    graph-patch list <run_id> [--json]
    graph-patch propose <run_id> --kind K --from-node N
                        [--node X] [--lane L] [--new-node-json J]
                        [--children-json J]
                        [--source orchestrator|operator|supervisor] --reason R [--json]
    graph-patch validate <run_id> <n> [--json]      # materialize only
    graph-patch apply <run_id> <n> [--dry-run] [--json]
    graph-patch credit [--json]

Every verb supports ``--json``. A :class:`GraphPatchError` prints its message
and exits ``2`` — the same hard-error convention as ``mini-ork supervise``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mini_ork.recovery import graph_patch


def _home() -> str:
    return os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")


def _engine_root() -> str:
    return os.environ.get("MINI_ORK_ROOT") or str(
        Path(__file__).resolve().parents[2])


def _emit(payload, as_json: bool, text_lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in text_lines:
            print(line)


def _propose_patch(args: argparse.Namespace) -> dict:
    patch: dict = {
        "kind": args.kind,
        "from_node": args.from_node,
        "reason": args.reason,
        "source": args.source,
    }
    if args.node:
        patch["node"] = args.node
    if args.lane:
        patch["lane"] = args.lane
    if args.new_node_json:
        try:
            patch["new_node"] = json.loads(args.new_node_json)
        except json.JSONDecodeError as exc:
            raise graph_patch.GraphPatchError(
                f"--new-node-json is not valid JSON: {exc}") from exc
    children_json = getattr(args, "children_json", None)
    if children_json:
        try:
            children = json.loads(children_json)
        except json.JSONDecodeError as exc:
            raise graph_patch.GraphPatchError(
                f"--children-json is not valid JSON: {exc}") from exc
        if not isinstance(children, list):
            raise graph_patch.GraphPatchError(
                "--children-json must be a JSON list of child scopes")
        patch["children"] = children
    return patch


def _run(args: argparse.Namespace) -> int:
    home = _home()
    as_json = bool(getattr(args, "json", False))

    if args.verb == "list":
        entries = graph_patch.list_patches(home, args.run_id)
        _emit(entries, as_json, [
            f"{entry['number']:03d} {entry['status']:<9} "
            f"{str(entry['kind']):<13} {entry['from_node']} — {entry['reason']}"
            for entry in entries
        ] or ["no graph patches"])
        return 0

    if args.verb == "propose":
        run_dir = Path(home) / "runs" / str(args.run_id)
        number = graph_patch.propose(run_dir, _propose_patch(args))
        path = run_dir / graph_patch.PATCH_DIRNAME / f"{number:03d}.yaml"
        _emit({"number": number, "path": str(path)}, as_json,
              [f"proposed graph patch {number:03d} ({path})"])
        return 0

    if args.verb == "validate":
        workflow = graph_patch.materialize(home, args.run_id, args.n)
        _emit({"workflow": workflow}, as_json,
              [f"graph patch {args.n:03d} materializes: {workflow}"])
        return 0

    if args.verb == "apply":
        result = graph_patch.apply(
            home, args.run_id, args.n,
            engine_root=_engine_root(), dry_run=args.dry_run)
        _emit(result, as_json, [
            f"{'would apply' if args.dry_run else 'applied'} graph patch "
            f"{result['number']:03d}; workflow {result['workflow']}"
            + ("" if args.dry_run else f"; pid {result['pid']}")
        ])
        return 0

    if args.verb == "credit":
        report = graph_patch.credit(home)
        _emit(report, as_json, [
            f"{kind:<13} n={entry['n']:<3} passed={entry['passed']:<3} "
            f"rate={entry['rate']:.2f} credited={entry['credited']}"
            for kind, entry in sorted(report.items())
        ] or ["no graph-patch outcomes"])
        return 0

    raise graph_patch.GraphPatchError(f"unknown verb: {args.verb!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mini-ork graph-patch",
        description="Typed, immutable run-graph edits applied through recover --workflow.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_list = sub.add_parser("list", help="list a run's patches and their status")
    p_list.add_argument("run_id")
    p_list.add_argument("--json", action="store_true")

    p_propose = sub.add_parser("propose", help="write a new immutable patch")
    p_propose.add_argument("run_id")
    p_propose.add_argument("--kind", required=True)
    p_propose.add_argument("--from-node", required=True)
    p_propose.add_argument("--node")
    p_propose.add_argument("--lane")
    p_propose.add_argument("--new-node-json")
    p_propose.add_argument("--children-json")
    p_propose.add_argument("--reason", required=True)
    p_propose.add_argument("--source", default="operator")
    p_propose.add_argument("--json", action="store_true")

    p_validate = sub.add_parser("validate", help="materialize a patch without applying")
    p_validate.add_argument("run_id")
    p_validate.add_argument("n", type=int)
    p_validate.add_argument("--json", action="store_true")

    p_apply = sub.add_parser("apply", help="materialize and revive the run from it")
    p_apply.add_argument("run_id")
    p_apply.add_argument("n", type=int)
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--json", action="store_true")

    p_credit = sub.add_parser("credit", help="per-kind outcome credit vs the rerun control")
    p_credit.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        return _run(args)
    except graph_patch.GraphPatchError as exc:
        print(f"graph-patch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
