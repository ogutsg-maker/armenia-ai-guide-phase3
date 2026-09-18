"""Public partner storefront (витрина) API.

Read-only, unauthenticated endpoints that expose an APPROVED partner and its
APPROVED services so a client can browse them like a shop page. Only public,
non-sensitive fields are returned — no contact details, no owner user_id, no
ledger. Contact sharing still follows contact_share_policy and happens later in
the booking flow, never here.
"""
from __future__ import annotations

import json

from aiohttp import web

from platform_db import one, rows


def _json_response(data, status=200):
    return web.json_response(data, status=status)


def _error(code, status=400):
    return _json_response({"ok": False, "error": code}, status=status)


def _num(value):
    if value is None:
        return None
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def _profile(partner):
    """Return only whitelisted, display-safe fields from profile_json."""
    raw = partner.get("profile_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    allowed = ("tagline", "about", "logo_url", "cover_url", "working_hours", "languages")
    return {key: raw[key] for key in allowed if key in raw}


def _service_public(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description") or "",
        "price": _num(row.get("price")),
        "currency": row.get("currency") or "AMD",
        "duration_minutes": row.get("duration_minutes"),
        "category_name_am": row.get("category_name_am"),
        "category_name_ru": row.get("category_name_ru"),
        "packages": [],
        "options": [],
    }


def _partner_public(partner):
    return {
        "id": partner["id"],
        "business_name": partner.get("business_name") or "",
        "business_description": partner.get("business_description") or "",
        "contact_share_policy": partner.get("contact_share_policy") or "after_booking",
        "profile": _profile(partner),
    }


async def storefront(request):
    """GET /api/storefront/{partner_id} — public partner page with services."""
    try:
        partner_id = int(request.match_info["partner_id"])
    except (TypeError, ValueError):
        return _error("invalid_partner_id")

    partner = one(
        "SELECT * FROM partners WHERE id=%s AND status='approved'",
        (partner_id,),
    )
    if not partner:
        # Do not leak whether the partner exists but is unapproved.
        return _error("storefront_not_found", 404)

    service_rows = rows(
        """
        SELECT s.id, s.name, s.description, s.price, s.currency, s.duration_minutes,
               c.name_am AS category_name_am,
               c.name_ru AS category_name_ru
        FROM services s
        LEFT JOIN categories c ON c.id = s.category_id
        JOIN partner_direction_categories pdc ON pdc.category_id=s.category_id
        JOIN partner_directions pd ON pd.id=pdc.partner_direction_id
             AND pd.partner_id=s.partner_id AND pd.status='approved'
        WHERE s.partner_id = %s AND s.status = 'approved'
        ORDER BY s.created_at DESC
        """,
        (partner_id,),
    )

    services = {row["id"]: _service_public(row) for row in service_rows}

    if services:
        service_ids = list(services.keys())
        package_rows = rows(
            """
            SELECT id, service_id, name, price, currency, description
            FROM service_packages
            WHERE service_id = ANY(%s) AND is_active = TRUE
            ORDER BY service_id, id
            """,
            (service_ids,),
        )
        for pkg in package_rows:
            services[pkg["service_id"]]["packages"].append({
                "id": pkg["id"],
                "name": pkg["name"],
                "price": _num(pkg.get("price")),
                "currency": pkg.get("currency") or "AMD",
                "description": pkg.get("description") or "",
            })

        option_rows = rows(
            """
            SELECT id, service_id, name, price_delta, currency
            FROM service_options
            WHERE service_id = ANY(%s) AND is_active = TRUE
            ORDER BY service_id, id
            """,
            (service_ids,),
        )
        for opt in option_rows:
            services[opt["service_id"]]["options"].append({
                "id": opt["id"],
                "name": opt["name"],
                "price_delta": _num(opt.get("price_delta")),
                "currency": opt.get("currency") or "AMD",
            })

    locations = rows(
        """
        SELECT marz, city, village, address, location_type
        FROM partner_locations
        WHERE partner_id = %s
        ORDER BY id
        """,
        (partner_id,),
    )

    return _json_response({
        "ok": True,
        "partner": _partner_public(partner),
        "locations": locations,
        "services": list(services.values()),
        "service_count": len(services),
    })


def register_storefront_routes(app):
    app.router.add_get("/api/storefront/{partner_id}", storefront)
