"""Phase 3 — admin statistics + feature settings.

* ``/api/admin/stats/*`` — aggregated platform metrics (requests, deals,
  bookings/GMV, partners, reviews, support, refunds).
* ``/api/admin/settings`` — read/write the Phase 3 feature flags & tunables
  (enable/disable modules, cancellation windows, fee caps) WITHOUT a redeploy.

All endpoints require admin auth. Stats are cached briefly since the aggregate
queries are heavier than hot-path reads.
"""
from __future__ import annotations

import time

from aiohttp import web

import features
from marketplace_flow_api import _one, _rows
from admin_ai_api import _admin

_STATS_CACHE = {"at": 0.0, "data": None}
_STATS_TTL = 60.0


def _scalar(sql, params=(), key="v", default=0):
    row = _one(sql, params)
    if not row:
        return default
    val = row.get(key)
    return val if val is not None else default


def _status_counts(table, column="status"):
    out = {}
    for r in _rows(f"SELECT {column} k, COUNT(*) c FROM {table} GROUP BY {column}"):
        out[str(r.get("k"))] = int(r.get("c") or 0)
    return out


def _compute_overview() -> dict:
    requests_by_status = _status_counts("service_requests")
    negotiations_by_status = _status_counts("negotiations")
    bookings_by_status = _status_counts("bookings")

    gmv = float(_scalar(
        "SELECT COALESCE(SUM(agreed_price),0) v FROM bookings "
        "WHERE status IN ('paid','booked','completed')"))
    commission = float(_scalar(
        "SELECT COALESCE(SUM(amount),0) v FROM payments "
        "WHERE payment_type='commission' AND status IN ('paid','settled')"))
    premium_revenue = float(_scalar(
        "SELECT COALESCE(SUM(amount),0) v FROM payments "
        "WHERE payment_type='premium_contact' AND status IN ('paid','settled')"))
    refunds_row = _one(
        "SELECT COUNT(*) c, COALESCE(SUM(refund_amount),0) s FROM booking_cancellations")
    reviews_row = _one(
        "SELECT COUNT(*) c, COALESCE(AVG(rating),0) a FROM reviews WHERE status='published'")
    flagged_reviews = int(_scalar(
        "SELECT COUNT(*) v FROM reviews WHERE status='flagged'"))

    return {
        "requests": {"by_status": requests_by_status,
                     "total": sum(requests_by_status.values())},
        "negotiations": {"by_status": negotiations_by_status,
                         "total": sum(negotiations_by_status.values())},
        "bookings": {"by_status": bookings_by_status,
                     "total": sum(bookings_by_status.values()),
                     "gmv": gmv},
        "revenue": {"commission": commission, "premium_contact": premium_revenue},
        "partners": _status_counts("partners"),
        "reviews": {
            "count": int((reviews_row or {}).get("c") or 0),
            "avg_rating": round(float((reviews_row or {}).get("a") or 0), 2),
            "flagged": flagged_reviews,
        },
        "support": _status_counts("support_tickets"),
        "refunds": {
            "count": int((refunds_row or {}).get("c") or 0),
            "amount": float((refunds_row or {}).get("s") or 0),
        },
    }


async def stats_overview(request):
    _admin(request)
    now = time.time()
    if not request.query.get("fresh") and _STATS_CACHE["data"] is not None \
            and (now - _STATS_CACHE["at"]) < _STATS_TTL:
        return web.json_response({"ok": True, "cached": True, "stats": _STATS_CACHE["data"]})
    data = _compute_overview()
    _STATS_CACHE["data"] = data
    _STATS_CACHE["at"] = now
    return web.json_response({"ok": True, "cached": False, "stats": data})


async def stats_top_partners(request):
    _admin(request)
    items = _rows(
        "SELECT p.id, p.business_name, "
        "COUNT(b.id) FILTER (WHERE b.status IN ('paid','booked','completed')) bookings, "
        "COALESCE(SUM(b.agreed_price) FILTER (WHERE b.status IN ('paid','booked','completed')),0) gmv, "
        "COALESCE((SELECT AVG(rating) FROM reviews r WHERE r.partner_id=p.id AND r.status='published'),0) avg_rating "
        "FROM partners p LEFT JOIN bookings b ON b.partner_id=p.id "
        "GROUP BY p.id, p.business_name ORDER BY gmv DESC, bookings DESC LIMIT 20")
    return web.json_response({"ok": True, "items": items})


async def stats_timeseries(request):
    _admin(request)
    metric = request.query.get("metric", "bookings")
    try:
        days = max(1, min(365, int(request.query.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    table = {
        "bookings": "bookings",
        "requests": "service_requests",
        "reviews": "reviews",
        "tickets": "support_tickets",
    }.get(metric, "bookings")
    rows = _rows(
        f"SELECT DATE(created_at) d, COUNT(*) c FROM {table} "
        f"WHERE created_at >= NOW() - INTERVAL '{days} days' "
        "GROUP BY DATE(created_at) ORDER BY d")
    return web.json_response({"ok": True, "metric": metric, "days": days, "series": rows})


# --- feature settings ------------------------------------------------------
async def get_settings(request):
    _admin(request)
    return web.json_response({
        "ok": True,
        "settings": features.all_settings(),
        "defaults": features.DEFAULTS,
    })


async def update_settings(request):
    _admin(request)
    data = await request.json()
    overrides = data.get("settings") if isinstance(data.get("settings"), dict) else data
    if not isinstance(overrides, dict):
        return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)
    # Only accept known keys to avoid junk in the store.
    allowed = set(features.DEFAULTS.keys())
    clean = {k: v for k, v in overrides.items() if k in allowed}
    if not clean:
        return web.json_response({"ok": False, "error": "no_known_keys"}, status=400)
    merged = features.save(clean)
    return web.json_response({"ok": True, "settings": merged})


def register_admin_stats_routes(app):
    app.router.add_get("/api/admin/stats/overview", stats_overview)
    app.router.add_get("/api/admin/stats/top-partners", stats_top_partners)
    app.router.add_get("/api/admin/stats/timeseries", stats_timeseries)
    app.router.add_get("/api/admin/settings", get_settings)
    app.router.add_post("/api/admin/settings", update_settings)
