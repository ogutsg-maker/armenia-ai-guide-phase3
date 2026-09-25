from __future__ import annotations

"""Shared AI operational context.

This layer deliberately contains no SQL. All schema knowledge and database
access live in Data Core; this module only shapes verified data for AI.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import data_core


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v) for v in value]
    return str(value)


def _render_stats(stats: dict[str, Any]) -> str:
    labels = {
        "partners": "partners", "companies": "companies", "services": "services",
        "clients": "clients", "applications": "applications",
        "directions": "directions", "subcategories": "subcategories",
        "pending_admin": "pending_admin", "approved": "approved", "rejected": "rejected",
        "total": "total", "pending": "pending", "under_review": "under_review",
        "confirmed": "confirmed", "completed": "completed",
    }
    lines = ["LIVE STATISTICS"]
    for section in ("platform", "catalog", "geography", "applications",
                    "documents", "orders", "work_queue", "finance"):
        data = stats.get(section)
        if not isinstance(data, dict) or not data:
            continue
        lines.append(section.upper())
        lines.extend(f"  {labels.get(k, k)}: {v}" for k, v in data.items())
    return "\n".join(lines)


def _entity(entity_type: str, entity_id: Any, *, full: bool = False,
            role: str = "admin", actor_id: Any = None) -> dict[str, Any] | None:
    try:
        return _safe(data_core.get_ai_entity(
            entity_type, int(entity_id), full=full,
            role=role, actor_id=int(actor_id) if actor_id is not None else None,
        ))
    except (TypeError, ValueError, LookupError):
        return None
    except Exception:
        return None


def build_context(*, role: str, actor_id: Any = None,
                  entity_type: str | None = None, entity_id: Any = None,
                  full: bool = False,
                  focus_source: str = "conversation") -> dict[str, Any]:
    role = str(role or "").strip().lower()
    normalized_id = int(entity_id) if str(entity_id).isdigit() else entity_id
    context = {
        "system": {
            "role": role,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "context_version": 2,
        },
        "statistics": _safe(data_core.operational_stats()),
        "focus": {
            "type": entity_type or None,
            "id": normalized_id,
            "source": focus_source if entity_id not in (None, "") else None,
        },
        "entities": {},
        "relations": [],
        "permissions": {
            "read": True,
            "search": True,
            "check": role in {"admin", "partner"},
            "modify": role in {"admin", "partner"},
        },
    }

    if entity_type and entity_id not in (None, ""):
        item = _entity(entity_type, entity_id, full=full, role=role, actor_id=actor_id)
        if item:
            key = f"{str(entity_type).lower()}:{normalized_id}"
            context["entities"][key] = item
            profile = item.get("profile") if isinstance(item, dict) else None
            if isinstance(profile, dict):
                if profile.get("partner_id"):
                    context["relations"].append(
                        f"{key} -> partner:{profile['partner_id']}"
                    )
                if profile.get("business_id"):
                    context["relations"].append(
                        f"{key} -> company:{profile['business_id']}"
                    )
            if str(entity_type).lower() == "application" and item.get("company"):
                company = item["company"]
                if isinstance(company, dict) and company.get("id") is not None:
                    context["relations"].append(
                        f"application:{normalized_id} -> company:{company['id']}"
                    )
    return context


def render_context(context: dict[str, Any], *, max_chars: int = 24000) -> str:
    ctx = context if isinstance(context, dict) else {}
    lines = ["AI OPERATIONAL CONTEXT"]
    system = ctx.get("system") or {}
    lines.append(
        f"SYSTEM role={system.get('role', '')} "
        f"version={system.get('context_version', '')}"
    )
    lines.append(_render_stats(ctx.get("statistics") or {}))

    focus = ctx.get("focus") or {}
    if focus.get("id") not in (None, ""):
        lines.append(
            "CURRENT FOCUS\n"
            f"  type: {focus.get('type')}\n"
            f"  id: {focus.get('id')}\n"
            f"  source: {focus.get('source') or 'unknown'}"
        )

    relations = ctx.get("relations") or []
    if relations:
        lines.append("RELATIONS")
        lines.extend("  " + str(x) for x in relations[:50])

    entities = ctx.get("entities") or {}
    if entities:
        lines.append("RELEVANT DATA")
        lines.append(json.dumps(_safe(entities), ensure_ascii=False, default=str))

    lines.append(
        "PERMISSIONS " +
        json.dumps(ctx.get("permissions") or {}, ensure_ascii=False)
    )
    return "\n\n".join(lines)[:max_chars]


def build_partner_context(partner_id: int, *, entity_type: str | None = None,
                          entity_id: Any = None, full: bool = False) -> str:
    return render_context(build_context(
        role="partner", actor_id=partner_id,
        entity_type=entity_type, entity_id=entity_id, full=full,
        focus_source="partner_cabinet",
    ))


def build_admin_context(*, entity_type: str | None = None,
                        entity_id: Any = None, full: bool = False) -> str:
    return render_context(build_context(
        role="admin", entity_type=entity_type, entity_id=entity_id, full=full,
        focus_source="admin",
    ))
