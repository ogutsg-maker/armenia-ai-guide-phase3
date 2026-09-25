from __future__ import annotations

"""Compatibility facade for AI-facing context builders.

All live data comes from Data Core. This module contains presentation helpers
only; it does not construct SQL or access the database primitives directly.
"""

import json
from typing import Any

import data_core


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v) for v in value]
    return str(value)


def _price(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        return f"{int(number):,} AMD" if number.is_integer() else f"{number:g} AMD"
    except Exception:
        return str(value)


def build_entity_context(entity_type: str, entity_id: Any) -> str:
    try:
        entity = data_core.get_ai_entity(str(entity_type or ""), int(entity_id), full=True)
    except (TypeError, ValueError):
        return ""
    except Exception:
        return ""
    if not entity:
        return ""
    return json.dumps(_safe(entity), ensure_ascii=False, default=str)


def build_platform_index() -> str:
    stats = data_core.operational_stats()
    lines = ["[PLATFORM INDEX]"]
    for section, values in stats.items():
        if isinstance(values, dict):
            for key, value in values.items():
                lines.append(f"{section}.{key}: {value}")
    apps = data_core.search_applications(limit=20)
    if apps:
        lines.append("recent_applications:")
        for app in apps:
            lines.append(
                f"  - application #{app.get('id')} · "
                f"{app.get('business_name') or app.get('partner_business_name') or '—'} · "
                f"{app.get('service_name') or '—'} · "
                f"{app.get('location_city') or '—'} · {app.get('status') or '—'}"
            )
    return "\n".join(lines)


def build_ai_context(entity_type: str | None = None, entity_id: Any = None, *,
                     include_platform_index: bool = True, role: str = "admin",
                     full: bool = False) -> str:
    from ai_context_layer import build_context, render_context
    return render_context(build_context(
        role=role,
        entity_type=entity_type if entity_type else None,
        entity_id=entity_id if entity_id not in (None, "") else None,
        full=full,
        focus_source="admin" if role == "admin" else "conversation",
    ))


def build_admin_context(*, entity_type: str | None = None,
                        entity_id: Any = None, full: bool = False) -> str:
    return build_ai_context(
        entity_type=entity_type, entity_id=entity_id,
        include_platform_index=True, role="admin", full=full,
    )
