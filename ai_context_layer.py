from __future__ import annotations

"""Shared AI operational context for Admin, Partner and Client assistants.

The database remains the source of truth. This layer exposes a compact business
view: live statistics, entity focus, relations and on-demand full profiles.
It never exposes SQL to the model and never performs mutations.
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


def _rows(sql: str, params=()):
    try:
        return [_safe(x) for x in (data_core.rows(sql, params) or [])]
    except Exception:
        return []


def _one(sql: str, params=()):
    try:
        row = data_core.one(sql, params)
        return _safe(row) if row else None
    except Exception:
        return None


def _table_exists(name: str) -> bool:
    row = _one(
        "SELECT 1 AS ok FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s LIMIT 1",
        (name,),
    )
    return bool(row)


def _count(table: str, where: str = "", params=()) -> int | None:
    if not _table_exists(table):
        return None
    sql = f'SELECT COUNT(*) AS n FROM "public"."{table}"'
    if where:
        sql += " WHERE " + where
    row = _one(sql, params)
    try:
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


def _stats() -> dict[str, Any]:
    """Build compact operational statistics using only known business facts."""
    stats: dict[str, Any] = {
        "platform": {},
        "catalog": {},
        "geography": {},
        "applications": {},
        "documents": {},
        "orders": {},
        "work_queue": {},
    }

    for key, table, where in (
        ("partners", "partners", ""),
        ("companies", "partner_businesses", "status <> 'archived'"),
        ("services", "services", "status IS NULL OR status <> 'deleted'"),
        ("clients", "users", "role = 'client'"),
        ("applications", "partner_applications", ""),
        ("directions", "master_categories", "is_active = TRUE"),
        ("subcategories", "categories", "is_active = TRUE"),
    ):
        value = _count(table, where)
        if value is not None:
            if key in {"directions", "subcategories"}:
                stats["catalog"][key] = value
            elif key == "clients":
                stats["platform"][key] = value
            elif key == "applications":
                stats["applications"]["total"] = value
            else:
                stats["platform"][key] = value

    for status in ("pending_admin", "approved", "rejected"):
        value = _count("partner_applications", "status=%s", (status,))
        if value is not None:
            stats["applications"][status] = value

    for key, table in (
        ("documents", "partner_verification_documents"),
    ):
        value = _count(table)
        if value is not None:
            stats["documents"]["total"] = value

    if _table_exists("partner_verification_documents"):
        for status in ("pending", "under_review", "approved", "rejected"):
            value = _count(
                "partner_verification_documents",
                "(status=%s OR verification_status=%s)",
                (status, status),
            )
            if value is not None:
                stats["documents"][status] = value

    # The project currently uses bookings for partner/client booking flow.
    if _table_exists("bookings"):
        stats["orders"]["total"] = _count("bookings") or 0
        for status in ("new", "pending", "confirmed", "active", "completed", "cancelled", "canceled"):
            value = _count("bookings", "status=%s", (status,))
            if value:
                stats["orders"][status] = value

    stats["work_queue"]["applications_to_review"] = stats["applications"].get("pending_admin", 0)
    stats["work_queue"]["documents_to_review"] = (
        stats["documents"].get("pending", 0) + stats["documents"].get("under_review", 0)
    )

    if _table_exists("services"):
        cities = _one(
            "SELECT COUNT(DISTINCT city) AS n FROM services "
            "WHERE city IS NOT NULL AND TRIM(city)<>''"
        )
        if cities:
            stats["geography"]["service_cities"] = int(cities.get("n") or 0)

    if _table_exists("partner_objects"):
        cities = _one(
            "SELECT COUNT(DISTINCT city) AS n FROM partner_objects "
            "WHERE COALESCE(is_active,TRUE)=TRUE AND city IS NOT NULL AND TRIM(city)<>''"
        )
        marzes = _one(
            "SELECT COUNT(DISTINCT marz) AS n FROM partner_objects "
            "WHERE COALESCE(is_active,TRUE)=TRUE AND marz IS NOT NULL AND TRIM(marz)<>''"
        )
        if cities:
            stats["geography"]["cities"] = int(cities.get("n") or 0)
        if marzes:
            stats["geography"]["marzes"] = int(marzes.get("n") or 0)

    # Finance is deliberately capability-aware: only expose metrics when the
    # corresponding business table exists in this deployment.
    for table in ("payments", "financial_transactions", "transactions", "partner_payouts", "commissions"):
        if _table_exists(table):
            stats.setdefault("finance", {})["records"] = _count(table) or 0
            break

    return stats


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
    for section in ("platform", "catalog", "geography", "applications", "documents", "orders", "work_queue", "finance"):
        data = stats.get(section)
        if not isinstance(data, dict) or not data:
            continue
        lines.append(section.upper())
        for key, value in data.items():
            lines.append(f"  {labels.get(key, key)}: {value}")
    return "\n".join(lines)


def _entity(entity_type: str, entity_id: Any, *, full: bool = False) -> dict[str, Any] | None:
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return None
    kind = str(entity_type or "").strip().lower()

    if kind == "application":
        app = _one(
            """SELECT a.id,a.partner_id,a.business_id,a.business_name,a.status,a.service_name,a.price,
                      a.direction_name,a.master_category_id,a.subcategory_name,a.category_id,
                      a.location_marz,a.location_city,a.location_village,a.address,a.phone,
                      a.description,a.object_name,a.object_id,a.created_at,a.updated_at,a.payload_json,
                      p.business_name AS partner_name
               FROM partner_applications a
               LEFT JOIN partners p ON p.id=a.partner_id
               WHERE a.id=%s""", (eid,))
        if not app:
            return None
        out = {"type": "application", "id": eid, "profile": app}
        out["documents"] = _rows(
            """SELECT id,application_id,partner_id,document_type,status,verification_status,
                      file_name,admin_note,rejection_reason,created_at,updated_at
               FROM partner_verification_documents
               WHERE application_id=%s OR partner_id=%s ORDER BY id DESC LIMIT 30""",
            (eid, app.get("partner_id")),
        )
        if app.get("business_id"):
            out["company"] = _entity("company", app["business_id"], full=full)
        if app.get("partner_id"):
            out["partner"] = _entity("partner", app["partner_id"], full=False)
        return out

    if kind == "partner":
        partner = _one(
            """SELECT id,user_id,business_name,business_description,status,
                      verification_status,contact_share_policy,created_at,updated_at
               FROM partners WHERE id=%s""", (eid,))
        if not partner:
            return None
        out = {"type": "partner", "id": eid, "profile": partner}
        out["companies"] = _rows(
            """SELECT id,partner_id,name,description,phone,status,created_at,updated_at
               FROM partner_businesses WHERE partner_id=%s AND status<>'archived'
               ORDER BY id DESC LIMIT 50""", (eid,))
        if full:
            out["services"] = _rows(
                """SELECT id,business_id,name,description,price,status,category_id,created_at,updated_at
                   FROM services WHERE partner_id=%s AND (status IS NULL OR status<>'deleted')
                   ORDER BY id DESC LIMIT 100""", (eid,))
            out["addresses"] = _rows(
                """SELECT id,business_id,object_name,address,city,marz,phone,is_active
                   FROM partner_objects WHERE partner_id=%s AND COALESCE(is_active,TRUE)=TRUE
                   ORDER BY business_id,id LIMIT 100""", (eid,))
        return out

    if kind in {"company", "business"}:
        business = _one(
            """SELECT b.id,b.partner_id,b.name,b.description,b.phone,b.status,
                      b.created_at,b.updated_at,p.business_name AS partner_name
               FROM partner_businesses b
               LEFT JOIN partners p ON p.id=b.partner_id
               WHERE b.id=%s""", (eid,))
        if not business:
            return None
        out = {"type": "company", "id": eid, "profile": business}
        out["addresses"] = _rows(
            """SELECT id,business_id,object_name,address,city,marz,phone,is_active
               FROM partner_objects WHERE business_id=%s AND COALESCE(is_active,TRUE)=TRUE
               ORDER BY id LIMIT 50""", (eid,))
        out["services"] = _rows(
            """SELECT id,business_id,name,description,price,status,category_id,created_at,updated_at
               FROM services WHERE business_id=%s AND (status IS NULL OR status<>'deleted')
               ORDER BY id DESC LIMIT 100""", (eid,))
        return out

    if kind == "service":
        return _one(
            """SELECT s.id,s.partner_id,s.business_id,s.name,s.description,s.price,s.status,
                      s.category_id,s.created_at,s.updated_at,b.name AS company_name,
                      p.business_name AS partner_name,
                      c.master_category_id,c.name_am AS category_name_am,
                      c.name_ru AS category_name_ru,c.name_en AS category_name_en,
                      m.name_am AS direction_name_am,m.name_ru AS direction_name_ru,
                      m.name_en AS direction_name_en
               FROM services s
               LEFT JOIN partner_businesses b ON b.id=s.business_id
               LEFT JOIN partners p ON p.id=s.partner_id
               LEFT JOIN categories c ON c.id=s.category_id
               LEFT JOIN master_categories m ON m.id=c.master_category_id
               WHERE s.id=%s""", (eid,))

    if kind in {"category", "subcategory"}:
        return _one(
            """SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.is_active,
                      m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en
               FROM categories c LEFT JOIN master_categories m ON m.id=c.master_category_id
               WHERE c.id=%s""", (eid,))

    if kind == "order":
        if _table_exists("bookings"):
            return _one("SELECT * FROM bookings WHERE id=%s", (eid,))
        return None
    return None


def build_context(*, role: str, actor_id: Any = None, entity_type: str | None = None,
                  entity_id: Any = None, full: bool = False,
                  focus_source: str = "conversation") -> dict[str, Any]:
    role = str(role or "").strip().lower()
    context = {
        "system": {
            "role": role,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "context_version": 1,
        },
        "statistics": _stats(),
        "focus": {
            "type": entity_type or None,
            "id": int(entity_id) if str(entity_id).isdigit() else entity_id,
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
        item = _entity(entity_type, entity_id, full=full)
        if item:
            key = f"{str(entity_type).lower()}:{int(entity_id)}"
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
            if entity_type == "application" and item.get("company"):
                context["relations"].append(
                    f"application:{int(entity_id)} -> company:{item['company'].get('id')}"
                )
    return context


def render_context(context: dict[str, Any], *, max_chars: int = 24000) -> str:
    """Human-readable context for the model; keeps IDs explicit and stable."""
    ctx = context if isinstance(context, dict) else {}
    lines = ["AI OPERATIONAL CONTEXT"]
    system = ctx.get("system") or {}
    lines.append(f"SYSTEM role={system.get('role','')} version={system.get('context_version','')}")
    stats = ctx.get("statistics") or {}
    lines.append(_render_stats(stats))

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

    permissions = ctx.get("permissions") or {}
    lines.append("PERMISSIONS " + json.dumps(permissions, ensure_ascii=False))

    text = "\n\n".join(lines)
    return text[:max_chars]


def build_partner_context(partner_id: int, *, entity_type: str | None = None,
                          entity_id: Any = None, full: bool = False) -> str:
    return render_context(
        build_context(
            role="partner",
            actor_id=partner_id,
            entity_type=entity_type,
            entity_id=entity_id,
            full=full,
            focus_source="partner_cabinet",
        )
    )


def build_admin_context(*, entity_type: str | None = None, entity_id: Any = None,
                        full: bool = False) -> str:
    return render_context(
        build_context(
            role="admin",
            entity_type=entity_type,
            entity_id=entity_id,
            full=full,
            focus_source="admin",
        )
    )
