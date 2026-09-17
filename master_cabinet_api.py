"""Partner cabinet API for the current PostgreSQL architecture.

The WebApp calls this module through main.py.  All routes are authenticated
with Telegram WebApp initData and operate on the current PostgreSQL schema.
"""
from __future__ import annotations

import json
from typing import Any

from aiohttp import web
from psycopg.rows import dict_row

from config import BOT_TOKEN
from database import _connect
from telegram_webapp_auth import validate_telegram_webapp_init_data


def _json(value: Any):
    """Make PostgreSQL values safe for aiohttp JSON responses."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def _auth_partner(request: web.Request) -> int:
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": "telegram_init_data_required"}),
            content_type="application/json",
        )
    try:
        data = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
        return int(data["id"])
    except Exception as exc:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}),
            content_type="application/json",
        )


def _partner_id(telegram_id: int) -> int | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM partners WHERE user_id=%s", (telegram_id,))
            row = cur.fetchone()
            return int(row["id"]) if row else None


def _require_partner(telegram_id: int) -> int:
    pid = _partner_id(telegram_id)
    if not pid:
        raise web.HTTPNotFound(
            text=json.dumps({"ok": False, "error": "partner_not_found"}),
            content_type="application/json",
        )
    return pid


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (table_name,))
        row = cur.fetchone()
        return bool(row and row["exists"])


async def api_dashboard(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM partners WHERE id=%s", (pid,))
            partner = cur.fetchone() or {}

            cur.execute("SELECT COUNT(*) AS n FROM services WHERE partner_id=%s", (pid,))
            services_count = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM partner_objects WHERE partner_id=%s", (pid,))
            objects_count = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM partner_locations WHERE partner_id=%s", (pid,))
            locations_count = int(cur.fetchone()["n"])

            bookings_count = 0
            active_bookings = 0
            if _table_exists(conn, "bookings"):
                cur.execute("SELECT COUNT(*) AS n FROM bookings WHERE partner_id=%s", (pid,))
                bookings_count = int(cur.fetchone()["n"])
                cur.execute(
                    """SELECT COUNT(*) AS n FROM bookings
                       WHERE partner_id=%s AND status IN ('pending','confirmed','paid','booked','in_progress')""",
                    (pid,),
                )
                active_bookings = int(cur.fetchone()["n"])

            cur.execute(
                """SELECT COALESCE(AVG(rating),0) AS avg_rating, COUNT(*) AS review_count
                   FROM reviews WHERE partner_id=%s AND status='published'""",
                (pid,),
            )
            rating = cur.fetchone() or {}

    return web.json_response({
        "ok": True,
        "partner": _json(partner),
        "metrics": {
            "services": services_count,
            "objects": objects_count,
            "locations": locations_count,
            "bookings": bookings_count,
            "active_bookings": active_bookings,
            "rating": float(rating.get("avg_rating") or 0),
            "reviews": int(rating.get("review_count") or 0),
        },
    })


async def api_settings(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.*, u.lang, u.username, u.full_name, u.phone
                   FROM partners p JOIN users u ON u.telegram_id=p.user_id
                   WHERE p.id=%s""",
                (pid,),
            )
            row = cur.fetchone() or {}
    return web.json_response({"ok": True, "settings": _json(row)})


async def api_settings_update(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    data = await request.json()
    allowed = {
        "business_name", "business_description", "contact_share_policy",
        "contact_sharing_enabled", "premium_contact_sharing_enabled", "profile_json",
    }
    fields = {k: data[k] for k in allowed if k in data}
    with _connect() as conn:
        with conn.cursor() as cur:
            if fields:
                sets = ", ".join(f"{k}=%s" for k in fields)
                cur.execute(f"UPDATE partners SET {sets}, updated_at=NOW() WHERE id=%s", (*fields.values(), pid))
            if data.get("lang") in {"hy", "ru", "en"}:
                cur.execute("UPDATE users SET lang=%s WHERE telegram_id=%s", (data["lang"], uid))
        conn.commit()
    return web.json_response({"ok": True})


async def api_objects(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM partner_objects WHERE partner_id=%s ORDER BY id", (pid,))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "objects": _json(rows)})


async def api_object_create(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    data = await request.json()
    name = str(data.get("object_name") or data.get("name") or "").strip()
    if not name:
        return web.json_response({"ok": False, "error": "object_name_required"}, status=400)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO partner_objects(partner_id,object_name,address,city,marz,data_json)
                   VALUES(%s,%s,%s,%s,%s,%s) RETURNING *""",
                (pid, name, data.get("address"), data.get("city"), data.get("marz"), json.dumps(data.get("data_json") or {})),
            )
            row = cur.fetchone()
        conn.commit()
    return web.json_response({"ok": True, "object": _json(row)})


async def api_object_delete(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    oid = int(request.match_info["object_id"])
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM partner_objects WHERE id=%s AND partner_id=%s RETURNING id", (oid, pid))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return web.json_response({"ok": False, "error": "object_not_found"}, status=404)
    return web.json_response({"ok": True, "id": oid})


async def api_services(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.*, c.name_am AS category_name_am, c.name_ru AS category_name_ru,
                          c.name_en AS category_name_en
                   FROM services s
                   LEFT JOIN categories c ON c.id=s.category_id
                   WHERE s.partner_id=%s AND s.status <> 'deleted'
                   ORDER BY s.id DESC""",
                (pid,),
            )
            rows = cur.fetchall()
            # Some older partner records may still live in partner_services.
            if not rows and _table_exists(conn, "partner_services"):
                cur.execute(
                    """SELECT id, partner_id, category_id, object_id,
                              service_name AS name, description, base_price AS price,
                              duration_minutes, is_active
                       FROM partner_services WHERE partner_id=%s ORDER BY id DESC""",
                    (pid,),
                )
                rows = cur.fetchall()
    return web.json_response({"ok": True, "services": _json(rows)})


async def api_service_create(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    data = await request.json()
    name = str(data.get("name") or data.get("service_name") or "").strip()
    if not name:
        return web.json_response({"ok": False, "error": "service_name_required"}, status=400)
    category_id = data.get("category_id")
    category_id = int(category_id) if category_id not in (None, "") else None
    price = data.get("price")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO services(partner_id,category_id,subcategory_id,name,description,price,duration_minutes,status,data_json)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,'draft',%s) RETURNING *""",
                (pid, category_id, data.get("subcategory_id") or None, name,
                 str(data.get("description") or ""), price, data.get("duration_minutes") or None,
                 json.dumps(data.get("data_json") or {})),
            )
            row = cur.fetchone()
        conn.commit()
    return web.json_response({"ok": True, "service": _json(row)})


async def api_service_update(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    sid = int(request.match_info["service_id"])
    data = await request.json()
    allowed = {"category_id", "subcategory_id", "name", "description", "price", "duration_minutes", "status", "data_json"}
    fields = {k: data[k] for k in allowed if k in data}
    if not fields:
        return web.json_response({"ok": True})
    if "data_json" in fields and not isinstance(fields["data_json"], str):
        fields["data_json"] = json.dumps(fields["data_json"])
    with _connect() as conn:
        with conn.cursor() as cur:
            sets = ", ".join(f"{k}=%s" for k in fields)
            cur.execute(f"UPDATE services SET {sets}, updated_at=NOW() WHERE id=%s AND partner_id=%s RETURNING *", (*fields.values(), sid, pid))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return web.json_response({"ok": False, "error": "service_not_found"}, status=404)
    return web.json_response({"ok": True, "service": _json(row)})


async def api_service_delete(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    sid = int(request.match_info["service_id"])
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE services SET status='deleted', updated_at=NOW() WHERE id=%s AND partner_id=%s RETURNING id", (sid, pid))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return web.json_response({"ok": False, "error": "service_not_found"}, status=404)
    return web.json_response({"ok": True, "id": sid})


async def api_notifications(request: web.Request):
    uid = _auth_partner(request)
    unread_only = str(request.query.get("unread_only", "0")).lower() in {"1", "true", "yes"}
    with _connect() as conn:
        with conn.cursor() as cur:
            if unread_only:
                cur.execute("SELECT * FROM notifications WHERE user_id=%s AND is_read=FALSE ORDER BY created_at DESC LIMIT 100", (uid,))
            else:
                cur.execute("SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (uid,))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "notifications": _json(rows), "unread_count": sum(1 for r in rows if not r.get("is_read"))})


async def api_notifications_read(request: web.Request):
    uid = _auth_partner(request)
    nid = int(request.match_info["notification_id"])
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET is_read=TRUE WHERE id=%s AND user_id=%s", (nid, uid))
        conn.commit()
    return web.json_response({"ok": True})


async def api_notifications_read_compat(request: web.Request):
    """Compatibility endpoint used by the current WebApp notification inbox.

    POST /notifications/read accepts either {"ids":[...]} or {}.
    Empty ids means mark all notifications for the authenticated partner read.
    """
    uid = _auth_partner(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    ids = data.get("ids") if isinstance(data, dict) else None
    with _connect() as conn:
        with conn.cursor() as cur:
            if ids:
                clean_ids = []
                for value in ids:
                    try:
                        clean_ids.append(int(value))
                    except (TypeError, ValueError):
                        continue
                if clean_ids:
                    cur.execute(
                        "UPDATE notifications SET is_read=TRUE WHERE user_id=%s AND id=ANY(%s)",
                        (uid, clean_ids),
                    )
            else:
                cur.execute("UPDATE notifications SET is_read=TRUE WHERE user_id=%s", (uid,))
            cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s AND is_read=FALSE", (uid,))
            unread = int(cur.fetchone()["n"] or 0)
        conn.commit()
    return web.json_response({"ok": True, "unread_count": unread})


async def api_notifications_read_all(request: web.Request):
    uid = _auth_partner(request)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET is_read=TRUE WHERE user_id=%s", (uid,))
        conn.commit()
    return web.json_response({"ok": True})


async def api_bookings(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    with _connect() as conn:
        if not _table_exists(conn, "bookings"):
            return web.json_response({"ok": True, "bookings": []})
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bookings WHERE partner_id=%s ORDER BY id DESC LIMIT 200", (pid,))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "bookings": _json(rows)})


async def api_locations(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM partner_locations WHERE partner_id=%s ORDER BY id", (pid,))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "locations": _json(rows)})


async def api_reviews(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM reviews WHERE partner_id=%s ORDER BY created_at DESC LIMIT 200", (pid,))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "reviews": _json(rows)})


async def api_documents(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,partner_id,document_type,original_filename,mime_type,file_size,status,
                                  rejection_reason,partner_direction_id,created_at,reviewed_at
                           FROM partner_verification_documents WHERE partner_id=%s ORDER BY id DESC""", (pid,))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "documents": _json(rows)})


async def update_profile_by_image(request):
    return web.json_response({"ok": False, "error": "image_processing_requires_openai_api"}, status=503)


async def update_profile_by_voice(request):
    return web.json_response({"ok": False, "error": "voice_processing_requires_openai_api"}, status=503)


def register_master_cabinet_routes(app, db=None, bot=None):
    """Register the complete current partner cabinet API.

    Signature intentionally matches main.py: (app, db, bot=bot).
    """
    app["partner_db"] = db
    app.router.add_get("/api/master/{id}/dashboard", api_dashboard)
    app.router.add_get("/api/master/{id}/settings", api_settings)
    app.router.add_post("/api/master/{id}/settings", api_settings_update)
    app.router.add_get("/api/master/{id}/objects", api_objects)
    app.router.add_post("/api/master/{id}/objects", api_object_create)
    app.router.add_delete("/api/master/{id}/objects/{object_id}", api_object_delete)
    app.router.add_get("/api/master/{id}/services", api_services)
    app.router.add_post("/api/master/{id}/services", api_service_create)
    app.router.add_post("/api/master/{id}/services/{service_id}", api_service_update)
    app.router.add_delete("/api/master/{id}/services/{service_id}", api_service_delete)
    app.router.add_get("/api/master/{id}/notifications", api_notifications)
    app.router.add_post("/api/master/{id}/notifications/{notification_id}/read", api_notifications_read)
    app.router.add_post("/api/master/{id}/notifications/read", api_notifications_read_compat)
    app.router.add_post("/api/master/{id}/notifications/read-all", api_notifications_read_all)
    app.router.add_get("/api/master/{id}/bookings", api_bookings)
    app.router.add_get("/api/master/{id}/locations", api_locations)
    app.router.add_get("/api/master/{id}/reviews", api_reviews)
    app.router.add_get("/api/master/{id}/documents", api_documents)
    app.router.add_post("/api/partner/cabinet/update-by-image", update_profile_by_image)
    app.router.add_post("/api/partner/cabinet/update-by-voice", update_profile_by_voice)
