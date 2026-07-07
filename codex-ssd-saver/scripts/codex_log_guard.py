#!/usr/bin/env python3
"""Deprecated shim: this script was renamed to codex_ssd_saver.py.

Kept so existing scheduled tasks and docs keep working. It forwards all
arguments to the new script.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

target = Path(__file__).resolve().with_name("codex_ssd_saver.py")
print(f"note: codex_log_guard.py is deprecated; forwarding to {target.name}", file=sys.stderr)
sys.argv[0] = str(target)
runpy.run_path(str(target), run_name="__main__")
