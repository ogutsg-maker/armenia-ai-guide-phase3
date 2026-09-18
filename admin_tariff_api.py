"""Admin tariff editing API.

The platform commission cascades: service override -> subcategory
(categories) override -> direction (master_categories) default. The direction
tariff is the mandatory "initial setting" (NOT NULL), while subcategory and
service tariffs are optional overrides (NULL => inherit from the level above).

These endpoints let the admin edit each level:
  * direction default  : POST /api/admin/tariffs/direction/{id}
  * subcategory override: POST /api/admin/tariffs/category/{id}
  * service override    : POST /api/admin/tariffs/service/{id}
  * overview           : GET  /api/admin/tariffs

Auth is enforced by the platform-wide `/api/admin/` auth middleware in
runtime_platform_bootstrap, so no per-handler token check is needed.
"""
from __future__ import annotations
import json
from aiohttp import web
from platform_db import one, execute

_TYPES = {"inside", "on_top", "fixed"}


def _parse_tariff(data, *, allow_inherit):
    """Return (commission_type, commission_value) or raise web.HTTPBadRequest.

    allow_inherit=True lets both fields be null/empty which means "inherit
    from the parent level" (only valid for subcategory/service overrides).
    """
    ctype = data.get("commission_type")
    cval = data.get("commission_value")
    if allow_inherit and (ctype in (None, "") and cval in (None, "")):
        return None, None
    if ctype not in _TYPES:
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": "invalid_commission_type", "allowed": sorted(_TYPES)}),
            content_type="application/json",
        )
    try:
        cval = float(cval)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": "invalid_commission_value"}),
            content_type="application/json",
        )
    if cval < 0:
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": "commission_value_negative"}),
            content_type="application/json",
        )
    if ctype in ("inside", "on_top") and cval > 100:
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": "percent_over_100"}),
            content_type="application/json",
        )
    return ctype, cval


async def set_direction_tariff(request):
    """Direction (master_categories) default tariff. Cannot be cleared: it is
    the initial/default setting the whole cascade falls back to."""
    mid = int(request.match_info["id"])
    data = await request.json()
    ctype, cval = _parse_tariff(data, allow_inherit=False)
    row = execute(
        "UPDATE master_categories SET commission_type=%s,commission_value=%s WHERE id=%s RETURNING id,name_am,name_ru,commission_type,commission_value",
        (ctype, cval, mid), True,
    )
    if not row:
        return web.json_response({"ok": False, "error": "direction_not_found"}, status=404)
    return web.json_response({"ok": True, "direction": row})


async def set_category_tariff(request):
    """Subcategory (categories) override. NULL/empty => inherit the direction default."""
    cid = int(request.match_info["id"])
    data = await request.json()
    ctype, cval = _parse_tariff(data, allow_inherit=True)
    row = execute(
        "UPDATE categories SET commission_type=%s,commission_value=%s WHERE id=%s RETURNING id,master_category_id,name_am,name_ru,commission_type,commission_value",
        (ctype, cval, cid), True,
    )
    if not row:
        return web.json_response({"ok": False, "error": "category_not_found"}, status=404)
    return web.json_response({"ok": True, "category": row, "inherits": ctype is None})


async def set_service_tariff(request):
    """Per-service override. NULL/empty => inherit subcategory/direction."""
    sid = int(request.match_info["id"])
    data = await request.json()
    ctype, cval = _parse_tariff(data, allow_inherit=True)
    row = execute(
        "UPDATE services SET commission_type=%s,commission_value=%s,updated_at=NOW() WHERE id=%s RETURNING id,partner_id,category_id,name,commission_type,commission_value",
        (ctype, cval, sid), True,
    )
    if not row:
        return web.json_response({"ok": False, "error": "service_not_found"}, status=404)
    return web.json_response({"ok": True, "service": row, "inherits": ctype is None})


async def tariffs_overview(request):
    """List directions with their default tariff and each subcategory's
    effective tariff (own override or inherited from the direction)."""
    from platform_db import rows as db_rows
    directions = db_rows(
        "SELECT id,name_am,name_ru,commission_type,commission_value FROM master_categories WHERE is_active=TRUE ORDER BY id"
    )
    cats = db_rows(
        """SELECT id,master_category_id,name_am,name_ru,commission_type,commission_value
           FROM categories WHERE is_active=TRUE ORDER BY master_category_id,id"""
    )
    by_dir = {}
    for c in cats:
        c["inherits"] = c.get("commission_type") is None
        by_dir.setdefault(c["master_category_id"], []).append(c)
    out = []
    for d in directions:
        out.append({**d, "subcategories": by_dir.get(d["id"], [])})
    return web.json_response({"ok": True, "directions": out})


def register_admin_tariff_routes(app):
    app.router.add_get("/api/admin/tariffs", tariffs_overview)
    app.router.add_post("/api/admin/tariffs/direction/{id}", set_direction_tariff)
    app.router.add_post("/api/admin/tariffs/category/{id}", set_category_tariff)
    app.router.add_post("/api/admin/tariffs/service/{id}", set_service_tariff)
