#!/usr/bin/env python3
"""Catalog seed for the behavioral UI-contract verifier."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_ork.verify.behavioral import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
