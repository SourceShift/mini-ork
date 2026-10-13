#!/usr/bin/env python3
"""Live smoke for the Daytona node provider — ``smoke_sandbox_node.py --provider daytona``."""
import os
import sys

sys.argv[1:1] = ["--provider", "daytona"]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_sandbox_node import main  # noqa: E402

raise SystemExit(main())
