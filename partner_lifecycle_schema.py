"""Partner lifecycle rules installed by versioned migrations."""
from __future__ import annotations


def ensure_partner_lifecycle_schema() -> None:
    """No database mutation is allowed during application startup."""
    return None
