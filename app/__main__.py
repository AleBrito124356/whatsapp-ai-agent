"""``python -m app`` entrypoint (see app/cli.py)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # emoji-safe on Windows consoles
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
