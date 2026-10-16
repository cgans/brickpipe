"""Feature flags.

Features that are built but not yet proven live stay behind a flag, so the
page and the command line do not offer something that cannot be used yet.
Turn one on with the BRICKPIPE_FEATURES environment variable, a
comma-separated list, for example ``BRICKPIPE_FEATURES=prices``.
"""
from __future__ import annotations

import os

ENV = "BRICKPIPE_FEATURES"

# name -> why it is off by default
FLAGGED = {
    "prices": "BrickLink only issues API keys to seller accounts, and the live "
              "call has not been exercised yet",
}


def enabled(name: str) -> bool:
    if name not in FLAGGED:
        return True
    wanted = {part.strip().lower() for part in os.environ.get(ENV, "").split(",")}
    return name in wanted or "all" in wanted


def all_flags() -> dict[str, bool]:
    return {name: enabled(name) for name in FLAGGED}
