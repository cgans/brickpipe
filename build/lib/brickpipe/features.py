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
#
# prices: to be flipped on once a BrickLink seller account exists (one paid
# order from any BrickLink store earns the feedback it needs) and an API
# consumer plus a token for the home IP address have been registered. The
# first live run against the real API is the acceptance test; when it passes,
# remove the entry below and the feature is on for everyone.
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
