# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx",
#     "rich",
# ]
# ///
from __future__ import annotations

import sys
from pathlib import Path

# Make the project importable when running as a script (sys.path[0] is `scripts/`).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from addteam.bootstrap_repo import run


if __name__ == "__main__":
    raise SystemExit(run())
