"""Partner cabinet API for the current PostgreSQL architecture.

The WebApp calls this module through main.py.  All routes are authenticated
with Telegram WebApp initData and operate on the current PostgreSQL schema.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from aiohttp import web
from psycopg.rows import dict_row

from config import BOT_TOKEN
from database import _connect
from telegram_webapp_auth import validate_telegram_webapp_init_data

try:
    from groq import AsyncGroq
except Exception:
    AsyncGroq = None

from partner_ai_assistant_api import api_ai_command, api_ai_command_confirm


def _json(value: Any):
    """Make PostgreSQL values safe for aiohttp JSON responses."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
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
    """Resolve the canonical partner from the authenticated Telegram user.

    The cabinet must never lose the partner identity just because an older
    registration path did not materialize the partners row yet. The user is
    already authenticated by Telegram; in that case the canonical Data Core
    helper is used to materialize the partner record once.
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM partners WHERE user_id=%s", (int(telegram_id),))
            row = cur.fetchone()
            if row:
                return int(row["id"])
    try:
        import data_core
        partner = data_core.ensure_partner(int(telegram_id))
        return int(partner["id"]) if partner else None
    except Exception:
        logging.getLogger(__name__).exception(
            "partner identity materialization failed telegram_id=%s", telegram_id
        )
        return None


def _business_id(request: web.Request, pid: int) -> int | None:
    raw=str(request.headers.get("X-Business-Id") or "").strip()
    with _connect() as conn:
        with conn.cursor() as cur:
            if raw.isdigit():
                cur.execute("SELECT id FROM partner_businesses WHERE id=%s AND partner_id=%s AND status='active'",(int(raw),pid))
                row=cur.fetchone()
                if row: return int(row["id"])
            cur.execute("SELECT id FROM partner_businesses WHERE partner_id=%s AND status='active' ORDER BY is_default DESC,id LIMIT 1",(pid,))
            row=cur.fetchone()
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
    bid = _business_id(request,pid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM partners WHERE id=%s", (pid,))
            partner = cur.fetchone() or {}

            cur.execute("SELECT COUNT(*) AS n FROM services WHERE partner_id=%s AND business_id=%s", (pid,bid))
            services_count = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM partner_objects WHERE partner_id=%s AND business_id=%s", (pid,bid))
            objects_count = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM partner_locations WHERE partner_id=%s AND business_id=%s", (pid,bid))
            locations_count = int(cur.fetchone()["n"])

            bookings_count = 0
            active_bookings = 0
            if _table_exists(conn, "bookings"):
                cur.execute("SELECT COUNT(*) AS n FROM bookings WHERE partner_id=%s AND business_id=%s", (pid,bid))
                bookings_count = int(cur.fetchone()["n"])
                cur.execute(
                    """SELECT COUNT(*) AS n FROM bookings
                       WHERE partner_id=%s AND business_id=%s AND status IN ('pending','confirmed','paid','booked','in_progress')""",
                    (pid,bid),
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



async def api_objects(request: web.Request):
    await _ensure_partner_object_active_column()
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request,pid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM partner_objects
                   WHERE partner_id=%s AND business_id=%s
                     AND COALESCE(is_active, TRUE)=TRUE
                   ORDER BY id""",
                (pid,bid),
            )
            rows = cur.fetchall()
    # Object working_hours is canonical data. Do not reconstruct or overwrite
    # it from the registration application when the object already has a
    # schedule; otherwise a later cabinet reload can undo a partner's edit.
    for row in rows:
        data_json = row.get("data_json") if isinstance(row,dict) else None
        if isinstance(data_json,str):
            try:
                data_json=json.loads(data_json or "{}")
            except Exception:
                data_json={}
        if not isinstance(data_json,dict):
            data_json={}
        row["data_json"]=data_json
    return web.json_response({"ok": True, "objects": _json(rows)})


async def _ensure_partner_object_active_column():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE partner_objects ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
        conn.commit()





async def api_services(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request,pid)
    with _connect() as conn:
        with conn.cursor() as cur:
            # Keep the partner service list independent from optional
            # category translation columns. The dashboard already proves that
            # services exist for this partner; the cabinet must display them
            # even if a legacy DB has a different category schema.
            cur.execute(
                """SELECT s.*,
                          (s.data_json->>'object_id') AS service_object_id,
                          (s.data_json->>'contact_phone') AS service_contact_phone,
                          c.master_category_id AS direction_id,
                          c.name_am AS subcategory_name_am,
                          c.name_ru AS subcategory_name_ru,
                          c.name_en AS subcategory_name_en,
                          m.name_am AS direction_name_am,
                          m.name_ru AS direction_name_ru,
                          m.name_en AS direction_name_en,
                          c.name_am AS category_name_am,
                          c.name_ru AS category_name_ru,
                          c.name_en AS category_name_en
                   FROM services s
                   LEFT JOIN categories c ON c.id=s.category_id
                   LEFT JOIN master_categories m ON m.id=c.master_category_id
                   WHERE s.partner_id=%s AND s.business_id=%s
                     AND (s.status IS NULL OR s.status <> 'deleted')
                   ORDER BY s.id DESC""",
                (pid,bid),
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



async def _load_full_service_catalog():
    """Return the complete active catalogue for new-service classification.

    Unlike the partner-facing service catalog, this intentionally includes
    directions that are not yet approved for the current partner. The AI must
    first determine what the service actually is; only after that do we decide
    whether the partner already has that direction approved.
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.id AS category_id,c.master_category_id,
                                  c.name_am AS category_am,c.name_ru AS category_ru,c.name_en AS category_en,c.slug AS category_slug,
                                  m.name_am AS master_am,m.name_ru AS master_ru,m.name_en AS master_en,m.slug AS master_slug
                           FROM categories c
                           JOIN master_categories m ON m.id=c.master_category_id
                           WHERE c.is_active=TRUE AND m.is_active=TRUE
                           ORDER BY c.master_category_id,c.id""")
            return [dict(x) for x in cur.fetchall()]






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
    bid = _business_id(request, pid)
    with _connect() as conn:
        if not _table_exists(conn, "bookings"):
            return web.json_response({"ok": True, "bookings": []})
        with conn.cursor() as cur:
            if bid is None:
                return web.json_response({"ok": True, "bookings": []})
            cur.execute(
                "SELECT * FROM bookings WHERE partner_id=%s AND business_id=%s ORDER BY id DESC LIMIT 200",
                (pid, bid),
            )
            rows = cur.fetchall()
    return web.json_response({"ok": True, "bookings": _json(rows)})


async def api_locations(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request,pid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM partner_locations WHERE partner_id=%s AND business_id=%s ORDER BY id", (pid,bid))
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
    bid = _business_id(request, pid)
    if not bid:
        return web.json_response({"ok": True, "documents": []})
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,partner_id,business_id,document_type,original_filename,mime_type,file_size,status,
                                  rejection_reason,partner_direction_id,created_at,reviewed_at
                           FROM partner_verification_documents
                           WHERE partner_id=%s AND business_id=%s ORDER BY id DESC""", (pid,bid))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "documents": _json(rows)})



async def api_service_catalog(request: web.Request):
    """Return every active subcategory under the selected company's approved directions."""
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request, pid)
    if not bid:
        return web.json_response({"ok": True, "categories": []})
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.master_category_id,
                       c.name_am, c.name_ru, c.name_en, c.slug,
                       m.name_am AS master_name_am,
                       m.name_ru AS master_name_ru,
                       m.name_en AS master_name_en
                FROM categories c
                JOIN master_categories m ON m.id=c.master_category_id
                JOIN partner_directions pd
                  ON pd.partner_id=%s
                 AND pd.business_id=%s
                 AND pd.master_category_id=c.master_category_id
                 AND pd.status='approved'
                WHERE c.is_active=TRUE AND m.is_active=TRUE
                ORDER BY c.master_category_id, c.id
            """, (pid,bid))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "categories": _json(rows)})



async def api_businesses(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    from data_core import list_companies, create_partner_company, archive_partner_company
    return web.json_response({"ok": True, "businesses": _json(list_companies(pid))})


async def api_ai_document_upload(request: web.Request):
    """Upload a direction-verification document from the partner AI assistant."""
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request, pid)
    if not bid:
        return web.json_response({"ok":False,"error":"business_required"},status=400)

    reader = await request.multipart()
    file_part = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            file_part = part
            break
    if file_part is None:
        return web.json_response({"ok":False,"error":"file_required"},status=400)

    filename = file_part.filename or "document"
    allowed = {".pdf":"application/pdf",".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".webp":"image/webp"}
    suffix = "." + filename.rsplit(".",1)[-1].lower() if "." in filename else ""
    if suffix not in allowed:
        return web.json_response({"ok":False,"error":"unsupported_file_type"},status=400)
    data = await file_part.read()
    if not data:
        return web.json_response({"ok":False,"error":"empty_file"},status=400)
    if len(data) > 10 * 1024 * 1024:
        return web.json_response({"ok":False,"error":"file_too_large"},status=400)

    with _connect() as conn:
        with conn.cursor() as cur:
            pending_application_id = None
            cur.execute(
                """SELECT id,partner_id,business_id,partner_direction_id,master_category_id
                   FROM partner_direction_verification_cases
                   WHERE partner_id=%s AND business_id=%s
                     AND status IN ('awaiting_document','pending_review','rejected')
                   ORDER BY updated_at DESC,id DESC LIMIT 1""",
                (pid,bid),
            )
            case = cur.fetchone()

            # A unified register_business submission can create the application
            # before deterministic catalogue classification fills the direction.
            # In that flow the UI already correctly tells the partner that a
            # direction document is required, but the verification case may not
            # have existed at the exact moment the upload endpoint was called.
            # Recover the direction from the latest pending application and
            # materialize the canonical direction/case instead of returning the
            # misleading "direction_verification_not_found" error.
            if not case:
                from data_core import get_application_full, ensure_direction_verification_case

                with _connect() as fallback_conn:
                    with fallback_conn.cursor() as fallback_cur:
                        fallback_cur.execute(
                            """SELECT id
                               FROM partner_applications
                               WHERE partner_id=%s
                                 AND business_id=%s
                                 AND status IN ('pending_partner','pending_admin')
                               ORDER BY id DESC
                               LIMIT 1""",
                            (pid, bid),
                        )
                        application_row = fallback_cur.fetchone()

                if application_row:
                    pending_application_id = int(application_row["id"])
                    application = get_application_full(
                        int(application_row["id"]), partner_id=pid
                    )
                    payload = (application or {}).get("payload_json") or {}
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            payload = {}

                    master_id = (application or {}).get("master_category_id")
                    if master_id is None and isinstance(payload, dict):
                        master_id = payload.get("master_category_id") or payload.get("ai_master_category_id")
                        if master_id is None:
                            for service_item in payload.get("services") or []:
                                if not isinstance(service_item, dict):
                                    continue
                                master_id = service_item.get("master_category_id")
                                if master_id is None:
                                    category_id = (
                                        service_item.get("matched_subcategory_id")
                                        or service_item.get("subcategory_id")
                                        or service_item.get("category_id")
                                    )
                                    if category_id not in (None, ""):
                                        from data_core import get_catalog_category
                                        category = get_catalog_category(int(category_id))
                                        master_id = category.get("master_category_id") if category else None
                                if master_id is not None:
                                    break

                    # Registration stores the complete service names first.
                    # If the application header has no direction yet, resolve
                    # the same live catalogue now so document upload does not
                    # depend on classification timing.
                    if master_id is None and isinstance(payload, dict):
                        from data_core import resolve_catalog_services
                        service_items = payload.get("services") or []
                        try:
                            resolved = resolve_catalog_services(
                                [{"name": str(x.get("name") or x.get("service_name") or "")}
                                 for x in service_items if isinstance(x, dict)],
                                limit=500,
                            )
                            for resolved_item in resolved:
                                candidate = resolved_item.get("master_category_id")
                                if candidate not in (None, "") and resolved_item.get("catalog_match_status") == "matched":
                                    master_id = int(candidate)
                                    break
                        except Exception:
                            logging.exception("document upload catalog resolution failed")

                    if master_id is not None:
                        gate = ensure_direction_verification_case(
                            partner_id=pid,
                            business_id=bid,
                            master_category_id=int(master_id),
                            actor_user_id=uid,
                        )
                        case = gate.get("case")
                        if case:
                            direction_id = int(case["partner_direction_id"])
                        else:
                            direction_id = None

            if not case and pending_application_id:
                # The application is the authoritative document target.
                # Direction classification may still be unresolved, so a
                # document must never be blocked by direction verification.
                cur.execute(
                    """UPDATE partner_verification_documents
                       SET is_current=FALSE,
                           status=CASE WHEN status='pending' THEN 'replaced' ELSE status END
                       WHERE partner_id=%s AND business_id=%s AND application_id=%s
                         AND is_current=TRUE AND status='pending'""",
                    (pid, bid, pending_application_id),
                )
                cur.execute(
                    """INSERT INTO partner_verification_documents
                       (partner_id,business_id,partner_direction_id,application_id,
                        document_type,original_filename,file_data,mime_type,file_size,status,is_current)
                       VALUES(%s,%s,NULL,%s,'business_document',%s,%s,%s,%s,'pending',TRUE)
                       RETURNING id,status,original_filename""",
                    (pid,bid,pending_application_id,filename,data,allowed[suffix],len(data)),
                )
                doc = cur.fetchone()
                cur.execute(
                    """UPDATE partner_applications
                       SET status='pending_admin',document_id=%s,updated_at=NOW()
                       WHERE id=%s AND partner_id=%s AND business_id=%s
                         AND status IN ('pending_partner','pending_admin','document_pending','document_under_review')""",
                    (int(doc["id"]),pending_application_id,pid,bid),
                )
                conn.commit()
                return web.json_response({
                    "ok": True,
                    "document_id": int(doc["id"]),
                    "status": doc["status"],
                    "filename": doc["original_filename"],
                    "application_id": pending_application_id,
                    "direction_id": None,
                    "verification_case_id": None,
                })

            if not case:
                # Last deterministic fallback: the direction may already exist
                # even when its verification case was not materialized yet.
                # Never force the partner through a second registration step.
                existing_direction = cur.execute(
                    """SELECT id,master_category_id,status
                       FROM partner_directions
                       WHERE partner_id=%s AND business_id=%s
                         AND status NOT IN ('approved','frozen','archived')
                       ORDER BY id DESC LIMIT 1""",
                    (pid,bid),
                )
                existing_direction = cur.fetchone()
                if existing_direction:
                    from data_core import ensure_direction_verification_case
                    gate = ensure_direction_verification_case(
                        partner_id=pid,
                        business_id=bid,
                        master_category_id=int(existing_direction["master_category_id"]),
                        actor_user_id=uid,
                    )
                    case = gate.get("case")
                if not case:
                    return web.json_response({"ok":False,"error":"direction_verification_not_found"},status=409)

            direction_id = int(case["partner_direction_id"])

            # A retry/replacement must not create two current documents for the
            # same direction. The newest upload is the current pending document.
            cur.execute(
                """UPDATE partner_verification_documents
                   SET is_current=FALSE,status=CASE WHEN status='pending' THEN 'replaced' ELSE status END
                   WHERE partner_direction_id=%s AND is_current=TRUE AND status='pending'""",
                (direction_id,),
            )
            cur.execute(
                """INSERT INTO partner_verification_documents
                   (partner_id,business_id,partner_direction_id,document_type,original_filename,
                    file_data,mime_type,file_size,status,is_current)
                   VALUES(%s,%s,%s,'direction_document',%s,%s,%s,%s,'pending',TRUE)
                   RETURNING id,status,original_filename""",
                (pid,bid,direction_id,filename,data,allowed[suffix],len(data)),
            )
            doc = cur.fetchone()
            cur.execute(
                """UPDATE partner_direction_verification_cases
                   SET status='pending_review',submitted_at=NOW(),updated_at=NOW()
                   WHERE id=%s""",
                (int(case["id"]),),
            )
            cur.execute(
                """UPDATE partner_directions SET status='pending',rejection_reason=NULL,updated_at=NOW()
                   WHERE id=%s AND partner_id=%s AND business_id=%s""",
                (direction_id,pid,bid),
            )
            # The document belongs to the direction. Every service application
            # for that same direction that was waiting for the partner document
            # can now enter the admin verification queue.
            cur.execute(
                """UPDATE partner_applications
                   SET status='pending_admin',document_id=%s,updated_at=NOW()
                   WHERE partner_id=%s
                     AND business_id=%s
                     AND master_category_id=%s
                     AND status='pending_partner'""",
                (int(doc["id"]),pid,bid,int(case["master_category_id"])),
            )
        conn.commit()
    return web.json_response({"ok":True,"document_id":int(doc["id"]),"status":doc["status"],
                              "filename":doc["original_filename"],"direction_id":direction_id,
                              "verification_case_id":int(case["id"])})


async def api_applications(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    from data_core import search_applications
    rows = search_applications(partner_id=pid, limit=100)
    from data_core import get_application_direction_verification
    for row in rows:
        try:
            row["direction_verification"] = get_application_direction_verification(
                application_id=int(row["id"]), partner_id=pid
            )
        except Exception:
            row["direction_verification"] = None
    return web.json_response({"ok": True, "applications": _json(rows)})


async def api_application_get(request: web.Request):
    uid = _auth_partner(request); pid = _require_partner(uid)
    application_id = int(request.match_info["application_id"])
    from data_core import get_application_full
    row = get_application_full(application_id, partner_id=pid)
    if not row:
        raise web.HTTPNotFound(text=json.dumps({"ok": False, "error": "application_not_found"}), content_type="application/json")
    return web.json_response({"ok": True, "application": _json(row)})


async def api_negotiations(request: web.Request):
    uid = _auth_partner(request)
    from marketplace_flow_api import partner_negotiations
    return await partner_negotiations(request)


async def api_negotiation_messages(request: web.Request):
    uid = _auth_partner(request)
    from marketplace_flow_api import partner_negotiation_messages
    return await partner_negotiation_messages(request)



def register_master_cabinet_routes(app, db=None, bot=None):
    """Register the complete current partner cabinet API.

    Signature intentionally matches main.py: (app, db, bot=bot).
    """
    app["partner_db"] = db
    app.router.add_get("/api/master/{id}/dashboard", api_dashboard)
    app.router.add_get("/api/master/{id}/businesses", api_businesses)
    app.router.add_post("/api/master/{id}/ai-command", api_ai_command)
    app.router.add_post("/api/master/{id}/ai-command/document", api_ai_document_upload)
    app.router.add_post("/api/master/{id}/documents/upload", api_ai_document_upload)
    app.router.add_post("/api/master/{id}/ai-command/confirm", api_ai_command_confirm)
    app.router.add_get("/api/master/{id}/settings", api_settings)
    app.router.add_get("/api/master/{id}/objects", api_objects)
    app.router.add_get("/api/master/{id}/services", api_services)
    app.router.add_get("/api/master/{id}/service-catalog", api_service_catalog)
    app.router.add_get("/api/master/{id}/notifications", api_notifications)
    app.router.add_post("/api/master/{id}/notifications/{notification_id}/read", api_notifications_read)
    app.router.add_post("/api/master/{id}/notifications/read", api_notifications_read_compat)
    app.router.add_post("/api/master/{id}/notifications/read-all", api_notifications_read_all)
    app.router.add_get("/api/master/{id}/bookings", api_bookings)
    app.router.add_get("/api/master/{id}/negotiations", api_negotiations)
    app.router.add_get("/api/master/{id}/negotiations/{negotiation_id}", api_negotiation_messages)
    app.router.add_get("/api/master/{id}/applications", api_applications)
    # GET /documents is owned by stage3_partner_verification.api_partner_documents.
    # Keep a single route so document reads cannot be shadowed by a legacy handler.
    app.router.add_get("/api/master/{id}/applications/{application_id}", api_application_get)
    app.router.add_get("/api/master/{id}/locations", api_locations)
    app.router.add_get("/api/master/{id}/reviews", api_reviews)
    # NOTE: GET /api/master/{id}/documents is already registered by
    # register_stage3_routes (api_partner_documents), which is called earlier
    # in main.py on the same app. Registering it here too raised aiohttp
    # RuntimeError ("Added route will never be executed") on startup, so this
    # duplicate was removed. The stage3 handler returns a superset
    # ({ok, partner, documents}); clients reading `.documents` are unaffected.