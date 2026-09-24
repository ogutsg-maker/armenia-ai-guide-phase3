from __future__ import annotations

"""AI Context Builder.

Turns live platform data into compact, business-readable context for the AI.
The database remains the source of truth. This module deliberately hides SQL
structure, foreign keys and implementation details from the model.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import platform_db


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


def _one(sql: str, params=()):
    try:
        row = platform_db.one(sql, params)
        return _safe(row) if row else None
    except Exception:
        return None


def _rows(sql: str, params=()):
    try:
        return [_safe(x) for x in (platform_db.rows(sql, params) or [])]
    except Exception:
        return []


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _price(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if number.is_integer():
            return f"{int(number):,} AMD"
        return f"{number:g} AMD"
    except Exception:
        return str(value)


def _application(aid: int):
    return _one(
        """SELECT a.id,a.partner_id,a.business_name,a.status,a.service_name,a.price,
                  a.direction_name,a.master_category_id,a.subcategory_name,a.category_id,
                  a.location_marz,a.location_city,a.location_village,a.address,a.phone,
                  a.description,a.object_name,a.created_at,a.updated_at,a.payload_json,
                  p.business_name AS partner_name
           FROM partner_applications a
           LEFT JOIN partners p ON p.id=a.partner_id
           WHERE a.id=%s""",
        (int(aid),),
    )


def _application_documents(partner_id):
    if not partner_id:
        return []
    return _rows(
        """SELECT id,application_id,document_type,status,verification_status,
                  file_name,admin_note,rejection_reason,created_at,updated_at
           FROM partner_verification_documents
           WHERE partner_id=%s ORDER BY id DESC LIMIT 30""",
        (int(partner_id),),
    )


def _category(category_id):
    if not category_id:
        return None
    return _one(
        """SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.is_active,
                  m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en
           FROM categories c
           LEFT JOIN master_categories m ON m.id=c.master_category_id
           WHERE c.id=%s""",
        (int(category_id),),
    )


def _service(service_id: int):
    return _one(
        """SELECT s.id,s.partner_id,s.business_id,s.name,s.category_id,s.price,s.status,
                  p.business_name AS partner_name,
                  b.name AS company_name,
                  c.master_category_id,c.name_am AS category_name_am,
                  c.name_ru AS category_name_ru,c.name_en AS category_name_en,
                  m.name_am AS direction_name_am,m.name_ru AS direction_name_ru,
                  m.name_en AS direction_name_en
           FROM services s
           LEFT JOIN partners p ON p.id=s.partner_id
           LEFT JOIN partner_businesses b ON b.id=s.business_id
           LEFT JOIN categories c ON c.id=s.category_id
           LEFT JOIN master_categories m ON m.id=c.master_category_id
           WHERE s.id=%s""",
        (int(service_id),),
    )


def _partner(partner_id: int):
    return _one(
        """SELECT id,user_id,business_name,business_description,status,
                  verification_status,contact_share_policy,created_at,updated_at
           FROM partners WHERE id=%s""",
        (int(partner_id),),
    )


def _business(business_id: int):
    return _one(
        """SELECT b.id,b.partner_id,b.name,b.description,b.phone,b.status,
                  p.business_name AS partner_name
           FROM partner_businesses b
           LEFT JOIN partners p ON p.id=b.partner_id
           WHERE b.id=%s""",
        (int(business_id),),
    )


def _catalog_category(category_id: int):
    return _category(category_id)


def _render_application(app: dict[str, Any], *, include_documents=True) -> str:
    lines = [
        f"[APPLICATION #{app.get('id')}]",
        f"business: {_first(app.get('business_name'), app.get('partner_name'), '—')}",
        f"partner: {app.get('partner_name') or '—'} [partner:{app.get('partner_id') or '—'}]",
        f"status: {app.get('status') or '—'}",
        f"service: {app.get('service_name') or '—'}",
        f"price: {_price(app.get('price')) or '—'}",
        f"direction: {app.get('direction_name') or '—'} [direction:{app.get('master_category_id') or '—'}]",
        f"category: {app.get('subcategory_name') or '—'} [category:{app.get('category_id') or '—'}]",
        f"location: {_first(app.get('location_marz'),'—')} / {_first(app.get('location_city'),'—')} / {_first(app.get('location_village'), '—')}",
        f"address: {app.get('address') or '—'}",
    ]
    if app.get("phone"):
        lines.append(f"phone: {app['phone']}")
    if app.get("description"):
        lines.append(f"description: {str(app['description'])[:700]}")

    category = _category(app.get("category_id"))
    if category:
        lines += [
            "catalog:",
            f"  direction: {_first(category.get('master_name_am'),category.get('master_name_ru'),category.get('master_name_en'),'—')} [direction:{category.get('master_category_id') or '—'}]",
            f"  category: {_first(category.get('name_am'),category.get('name_ru'),category.get('name_en'),'—')} [category:{category.get('id') or '—'}]",
            f"  active: {'yes' if category.get('is_active') else 'no'}",
        ]

    payload = app.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if isinstance(payload, dict) and isinstance(payload.get("services"), list):
        services = [x for x in payload["services"] if isinstance(x, dict)]
        if services:
            lines.append("application_services:")
            for item in services[:20]:
                name = _first(item.get("name"), item.get("service_name"), "—")
                price = _price(item.get("price"))
                lines.append(f"  - {name}" + (f" · {price}" if price else ""))

    if include_documents:
        docs = _application_documents(app.get("partner_id"))
        lines.append(f"documents: {len(docs)}")
        for doc in docs[:12]:
            status = _first(doc.get("status"), doc.get("verification_status"), "—")
            dtype = _first(doc.get("document_type"), "document")
            lines.append(f"  - #{doc.get('id')} · {dtype} · {status}")

    missing = []
    for field, label in (
        ("service_name", "service"),
        ("price", "price"),
        ("location_city", "city"),
        ("address", "address"),
    ):
        if app.get(field) in (None, ""):
            missing.append(label)
    if missing:
        lines.append("missing: " + ", ".join(missing))

    return "\n".join(lines)


def _render_service(service: dict[str, Any]) -> str:
    category = _category(service.get("category_id"))
    lines = [
        f"[SERVICE #{service.get('id')}]",
        f"name: {service.get('name') or '—'}",
        f"company: {service.get('company_name') or '—'} [company:{service.get('business_id') or '—'}]",
        f"partner: {service.get('partner_name') or '—'} [partner:{service.get('partner_id') or '—'}]",
        f"price: {_price(service.get('price')) or '—'}",
        f"status: {service.get('status') or '—'}",
    ]
    if category:
        lines += [
            "classification:",
            f"  direction: {_first(category.get('master_name_am'),category.get('master_name_ru'),category.get('master_name_en'),'—')} [direction:{category.get('master_category_id') or '—'}]",
            f"  category: {_first(category.get('name_am'),category.get('name_ru'),category.get('name_en'),'—')} [category:{category.get('id') or '—'}]",
        ]
    return "\n".join(lines)


def _render_partner(partner: dict[str, Any]) -> str:
    businesses = _rows(
        """SELECT id,name,description,phone,status
           FROM partner_businesses
           WHERE partner_id=%s AND status<>'archived'
           ORDER BY id DESC LIMIT 50""",
        (int(partner["id"]),),
    )
    services = _rows(
        """SELECT s.id,s.business_id,s.name,s.price,s.status,
                  c.name_am AS category_name_am,c.name_ru AS category_name_ru
           FROM services s
           LEFT JOIN categories c ON c.id=s.category_id
           WHERE s.partner_id=%s AND s.status<>'archived'
           ORDER BY s.id DESC LIMIT 100""",
        (int(partner["id"]),),
    )
    lines = [
        f"[PARTNER #{partner.get('id')}]",
        f"name: {partner.get('business_name') or '—'}",
        f"status: {partner.get('status') or '—'}",
        f"verification: {partner.get('verification_status') or '—'}",
        f"description: {str(partner.get('business_description') or '—')[:700]}",
        f"companies: {len(businesses)}",
    ]
    for b in businesses[:20]:
        lines.append(f"  - company #{b.get('id')}: {b.get('name') or '—'} · {b.get('status') or '—'}")
    lines.append(f"services: {len(services)}")
    for s in services[:30]:
        lines.append(f"  - service #{s.get('id')}: {s.get('name') or '—'} · {_price(s.get('price')) or '—'} · {_first(s.get('category_name_am'),s.get('category_name_ru'),'—')}")
    return "\n".join(lines)


def _render_business(business: dict[str, Any]) -> str:
    services = _rows(
        """SELECT s.id,s.name,s.price,s.status,
                  c.name_am AS category_name_am,c.name_ru AS category_name_ru
           FROM services s
           LEFT JOIN categories c ON c.id=s.category_id
           WHERE s.business_id=%s AND s.status<>'archived'
           ORDER BY s.id DESC LIMIT 50""",
        (int(business["id"]),),
    )
    lines = [
        f"[COMPANY #{business.get('id')}]",
        f"name: {business.get('name') or '—'}",
        f"partner: {business.get('partner_name') or '—'} [partner:{business.get('partner_id') or '—'}]",
        f"phone: {business.get('phone') or '—'}",
        f"status: {business.get('status') or '—'}",
        f"description: {str(business.get('description') or '—')[:600]}",
        f"services: {len(services)}",
    ]
    for s in services[:30]:
        lines.append(f"  - service #{s.get('id')}: {s.get('name') or '—'} · {_price(s.get('price')) or '—'} · {_first(s.get('category_name_am'),s.get('category_name_ru'),'—')}")
    return "\n".join(lines)


def _render_category(category: dict[str, Any]) -> str:
    return "\n".join([
        f"[CATALOG CATEGORY #{category.get('id')}]",
        f"name: {_first(category.get('name_am'),category.get('name_ru'),category.get('name_en'),'—')}",
        f"direction: {_first(category.get('master_name_am'),category.get('master_name_ru'),category.get('master_name_en'),'—')} [direction:{category.get('master_category_id') or '—'}]",
        f"active: {'yes' if category.get('is_active') else 'no'}",
    ])


def build_entity_context(entity_type: str, entity_id: Any) -> str:
    """Return a compact live business context for one entity."""
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return ""

    entity_type = str(entity_type or "").strip().lower()
    if entity_type == "application":
        row = _application(eid)
        return _render_application(row) if row else ""
    if entity_type == "service":
        row = _service(eid)
        return _render_service(row) if row else ""
    if entity_type == "partner":
        row = _partner(eid)
        return _render_partner(row) if row else ""
    if entity_type in {"business", "company"}:
        row = _business(eid)
        return _render_business(row) if row else ""
    if entity_type in {"category", "subcategory"}:
        row = _catalog_category(eid)
        return _render_category(row) if row else ""
    return ""


def build_platform_index() -> str:
    """Small global index used when no single entity is focused."""
    counts = {}
    for label, sql in (
        ("partners", "SELECT COUNT(*) AS n FROM partners"),
        ("companies", "SELECT COUNT(*) AS n FROM partner_businesses WHERE status<>'archived'"),
        ("services", "SELECT COUNT(*) AS n FROM services WHERE status<>'archived'"),
        ("applications", "SELECT COUNT(*) AS n FROM partner_applications"),
        ("directions", "SELECT COUNT(*) AS n FROM master_categories WHERE is_active=TRUE"),
        ("subcategories", "SELECT COUNT(*) AS n FROM categories WHERE is_active=TRUE"),
    ):
        row = _one(sql)
        counts[label] = int((row or {}).get("n") or 0)

    lines = ["[PLATFORM INDEX]"]
    for key, value in counts.items():
        lines.append(f"{key}: {value}")

    apps = _rows(
        """SELECT id,business_name,status,service_name,location_city
           FROM partner_applications ORDER BY created_at DESC LIMIT 20"""
    )
    if apps:
        lines.append("recent_applications:")
        for a in apps:
            lines.append(
                f"  - application #{a.get('id')} · {a.get('business_name') or '—'} · "
                f"{a.get('service_name') or '—'} · {a.get('location_city') or '—'} · {a.get('status') or '—'}"
            )

    return "\n".join(lines)


def build_ai_context(entity_type: str | None = None, entity_id: Any = None, *,
                     include_platform_index: bool = True) -> str:
    """Build the AI-facing context without exposing database internals."""
    parts = []
    if include_platform_index:
        parts.append(build_platform_index())
    if entity_type and entity_id not in (None, ""):
        entity = build_entity_context(entity_type, entity_id)
        if entity:
            parts.append(entity)
    return "\n\n".join(parts)


# Shared operational context facade.
# The legacy builders above remain available for compatibility; the public
# build_ai_context function is redirected to the new shared context layer.
def build_ai_context(entity_type: str | None = None, entity_id: Any = None, *,
                     include_platform_index: bool = True, role: str = "admin",
                     full: bool = False) -> str:
    from ai_context_layer import build_context, render_context
    return render_context(
        build_context(
            role=role,
            entity_type=entity_type if entity_type else None,
            entity_id=entity_id if entity_id not in (None, "") else None,
            full=full,
            focus_source="admin" if role == "admin" else "conversation",
        )
    )
