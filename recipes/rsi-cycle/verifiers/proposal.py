#!/usr/bin/env python3
"""Normalise the synthesis into `mini_ork.rsi.select.Proposal` and check the
structural facts a model can get wrong.

Policy stays in `mini_ork.rsi.select` — frozen paths, allowlisted prefixes,
blast radius, whether the success command is registered. This node enforces only
what is *structurally* true or false, and fails early with a message the operator
can act on: the proposal must answer the one candidate it was asked about, and
every path it names must exist in the repository it names.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (RUN_DIR, as_list, brief_path, fail, first, input_file, read_json)

STAGE = "proposal"
MAX_FILES = 5


def _clean_paths(raw) -> list[str]:
    out: list[str] = []
    for item in as_list(raw):
        path = item.strip().strip("`").replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path and path not in out:
            out.append(path)
    return out


def main() -> None:
    suggestion = read_json(input_file("suggestion", "suggestion.json"), STAGE)
    brief = read_json(brief_path(), STAGE)
    candidate = brief.get("candidate") or {}
    constraints = brief.get("constraints") or {}

    candidate_id = str(first(suggestion, "candidate_id", default="")).strip()
    wanted = str(candidate.get("candidate_id") or "").strip()
    if candidate_id and candidate_id != wanted:
        fail(STAGE, f"the synthesis answers candidate {candidate_id!r} but this run was "
                    f"dispatched for {wanted!r}")

    files = _clean_paths(first(suggestion, "files", "files_to_touch", "proposed_files"))
    if not files:
        fail(STAGE, "the synthesis named no files; there is nothing to scope")
    limit = int(constraints.get("max_files") or MAX_FILES)
    if len(files) > limit:
        fail(STAGE, f"{len(files)} files proposed; the limit is {limit} (a wider diff is a "
                     f"review request, not a fix)")
    repo_root = str(constraints.get("repo_root") or "")
    for path in files:
        if os.path.isabs(path) or ".." in path.split("/"):
            fail(STAGE, f"{path!r} is not a repo-relative path")
        if not os.path.exists(os.path.join(repo_root, path)):
            fail(STAGE, f"{path!r} does not exist under {repo_root}; an invented path would "
                        f"be rejected before dispatch anyway, so it fails here instead")

    verification = str(first(suggestion, "verification_command", "success_command", default="")
                       or candidate.get("success_command") or "").strip()
    if not verification:
        fail(STAGE, "no verification command: neither the synthesis nor the candidate has one")

    proposal = {
        "candidate_id": wanted,
        "files": files,
        "summary": str(first(suggestion, "summary", "change_summary", default="")).strip()
                   or str(candidate.get("title") or ""),
        "root_cause": str(first(suggestion, "root_cause", "cause", default="")).strip(),
        "ac_ids": as_list(first(suggestion, "ac_ids", "acceptance_criteria_ids")),
        "do_not_touch": _clean_paths(first(suggestion, "do_not_touch", "forbidden_paths")),
        "verification_command": verification,
    }

    path = os.path.join(RUN_DIR, "proposal.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(proposal, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"verifier": "rsi-cycle-proposal", "pass": True,
                      "candidate_id": wanted, "files": len(files)}))


if __name__ == "__main__":
    main()
