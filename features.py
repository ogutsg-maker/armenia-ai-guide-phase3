"""Phase 3 feature flags & tunable settings, stored in admin_settings.

The admin panel toggles modules on/off and edits knobs (commission, premium
fee caps, cancellation windows) without a redeploy. Everything lives under a
single ``features`` key in ``admin_settings`` as JSON. Reads are cached for a
few seconds so hot paths don't hammer the DB.

Defaults are permissive (every module ON) so a fresh install behaves exactly
like a build without the toggle layer.
"""
from __future__ import annotations

import time

# key in admin_settings
_SETTINGS_KEY = "features"

# Default flags + tunables. Admins override any subset; missing keys fall back
# here, so adding a new flag never breaks an existing deployment.
DEFAULTS = {
    # module on/off switches
    "premium_contact": True,
    "cancellations": True,
    "reviews": True,
    "support": True,
    "support_ai": True,          # let AI answer the first line of support
    "review_ai_moderation": True,  # auto-flag toxic reviews via AI
    # tunables
    "review_min_rating": 1,
    "review_max_rating": 5,
    "cancellation_windows": {
        # policy -> list of [hours_before_start, refund_percent], best match wins
        "flexible": [[0, 100]],
        "moderate": [[24, 100], [0, 50]],
        "strict": [[48, 100], [24, 50], [0, 0]],
    },
    "premium_contact_max_fee": 100000,  # AMD safety cap
}

_CACHE: dict = {"at": 0.0, "data": None}
_TTL = 5.0  # seconds


def _raw() -> dict:
    """Load the raw stored overrides (cached, best-effort)."""
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["at"]) < _TTL:
        return _CACHE["data"]
    data = {}
    try:
        from platform_db import get_setting
        stored = get_setting(_SETTINGS_KEY, {})
        if isinstance(stored, dict):
            data = stored
    except Exception:
        data = {}
    _CACHE["data"] = data
    _CACHE["at"] = now
    return data


def invalidate() -> None:
    """Drop the cache (call right after an admin writes new settings)."""
    _CACHE["data"] = None
    _CACHE["at"] = 0.0


def all_settings() -> dict:
    """Merged view: defaults overlaid with admin overrides."""
    merged = dict(DEFAULTS)
    merged.update(_raw() or {})
    return merged


def get(key: str, default=None):
    """Read one setting (override > default > caller default)."""
    data = _raw()
    if key in data:
        return data[key]
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default


def is_enabled(flag: str) -> bool:
    """True unless an admin explicitly turned the flag off."""
    return bool(get(flag, True))


def save(overrides: dict) -> dict:
    """Persist a partial override dict (merged onto current), return merged view."""
    current = dict(_raw() or {})
    for k, v in (overrides or {}).items():
        current[k] = v
    try:
        from platform_db import set_setting
        set_setting(_SETTINGS_KEY, current)
    finally:
        invalidate()
    return all_settings()
