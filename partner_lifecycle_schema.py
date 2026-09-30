"""Partner lifecycle bootstrap placeholder.

Database lifecycle function/trigger is installed by a versioned migration,
not mutated during application startup.
"""
from __future__ import annotations


def ensure_partner_lifecycle_schema() -> None:
    """Keep startup side-effect free; lifecycle trigger is migration-owned."""
    return None
