#!/usr/bin/env python3
"""Compatibility launcher for legacy ``mini-ork-<subcommand>`` executables."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(subcommand: str) -> int:
    launcher = Path(__file__).with_name("mini-ork")
    os.execv(str(launcher), [str(launcher), subcommand, *sys.argv[1:]])
    return 127
