#!/usr/bin/env python3
"""Catalog seed: the behavioral journey-contract verifier.

Top-level ``verifiers/`` is a native resolution base for the verifier
dispatcher (``mini_ork/cli/verify.py`` — it searches
``recipes/<recipe>/verifiers``, ``$MINI_ORK_HOME/verifiers``, then
``<root>/verifiers``). Reference it from an artifact_contract as::

    success_verifiers:
      - journey_contract

and supply the observable via ``MO_OBSERVABLE_SPEC`` (a yaml/json descriptor —
see ``verifiers/journey_contract.observable.example.yaml``) or the
``MO_BEHAV_*`` environment variables. With no observable declared it abstains
(UNVERIFIED), which is the opt-in no-op: nothing runs a behavioral check
unless a recipe configures one.

Delegates to :func:`mini_ork.verify.behavioral.main`, which prints the verdict
JSON to stdout and exits 0 on PROVEN, 1 on REFUTED, and ``MO_BEHAV_ABSTAIN_EXIT``
(default 1) on UNVERIFIED.
"""
import os
import sys

# Make the repo root importable when the dispatcher runs this file as a bare
# subprocess (it does not necessarily inherit the parent's sys.path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_ork.verify.behavioral import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())