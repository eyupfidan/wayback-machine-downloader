"""Backward-compatible entry point for source checkouts.

Prefer the installed ``wayback-tool`` command or ``python -m wayback_tool``.
"""

from __future__ import annotations

import sys
from pathlib import Path


src_dir = Path(__file__).resolve().parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from wayback_tool.cli import run  # noqa: E402


if __name__ == "__main__":
    run()
