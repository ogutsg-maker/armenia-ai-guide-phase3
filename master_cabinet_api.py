"""Partner cabinet API for the current PostgreSQL architecture.

The WebApp calls this module through main.py.  All routes are authenticated
with Telegram WebApp initData and operate on the current PostgreSQL schema.
"""
from __future__ import annotations

import json
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

from partner_registration_ai import _groq_json, _norm, _safe_int


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
            # Keep the partner service list independent from optional
            # category translation columns. The dashboard already proves that
            # services exist for this partner; the cabinet must display them
            # even if a legacy DB has a different category schema.
            cur.execute(
                """SELECT s.*
                   FROM services s
                   WHERE s.partner_id=%s
                     AND (s.status IS NULL OR s.status <> 'deleted')
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


async def _load_partner_service_catalog(pid: int):
    """Load only subcategories belonging to this partner's approved directions."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id AS category_id, c.master_category_id,
                       c.name_am AS category_am, c.name_ru AS category_ru,
                       c.name_en AS category_en, c.slug AS category_slug,
                       m.name_am AS master_am, m.name_ru AS master_ru,
                       m.name_en AS master_en, m.slug AS master_slug
                FROM categories c
                JOIN master_categories m ON m.id=c.master_category_id
                JOIN partner_directions pd
                  ON pd.partner_id=%s
                 AND pd.master_category_id=c.master_category_id
                 AND pd.status='approved'
                WHERE c.is_active=TRUE AND m.is_active=TRUE
                ORDER BY c.master_category_id, c.id
            """, (pid,))
            return [dict(row) for row in cur.fetchall()]


async def _ai_match_new_service(pid: int, name: str, description: str = ""):
    """Classify a partner service without exposing taxonomy to the partner.

    Services may be created only inside directions already approved for this
    partner. A service from another direction becomes a structured admin
    direction request instead of being placed under the wrong direction.
    """
    key = __import__("os").getenv("GROQ_API_KEY", "").strip()
    if not key or AsyncGroq is None:
        raise RuntimeError("groq_not_configured")

    catalog = await _load_partner_service_catalog(pid)
    if not catalog:
        return {"status": "no_catalog"}

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name_am, name_ru, name_en, slug
                FROM master_categories
                WHERE is_active=TRUE
                ORDER BY id
            """)
            all_masters = [dict(row) for row in cur.fetchall()]

    approved_master_ids = {_safe_int(x.get("master_category_id")) for x in catalog}
    approved_masters = []
    seen = set()
    for row in catalog:
        mid = _safe_int(row.get("master_category_id"))
        if mid in seen:
            continue
        seen.add(mid)
        approved_masters.append({
            "master_category_id": mid,
            "hy": _norm(row.get("master_am")),
            "ru": _norm(row.get("master_ru")),
            "en": _norm(row.get("master_en")),
        })

    def grams(value):
        value = __import__("re").sub(r"\\s+", "", _norm(value).lower())
        return {value[i:i+3] for i in range(max(0, len(value)-2))}

    def score(row):
        sg = grams(name + " " + description)
        rg = set()
        for key in ("category_am", "category_ru", "category_en", "master_am", "master_ru", "master_en"):
            rg |= grams(row.get(key))
        return len(sg & rg) / max(1, len(sg)) if sg and rg else 0

    candidates = sorted(catalog, key=score, reverse=True)[:80]

    schema = {
        "type": "object",
        "properties": {
            "matched_category_id": {"type": ["integer", "null"]},
            "master_category_id": {"type": ["integer", "null"]},
            "proposed_subcategory_name": {"type": ["string", "null"]},
            "out_of_scope_master_id": {"type": ["integer", "null"]},
            "reason": {"type": "string"},
        },
        "required": [
            "matched_category_id", "master_category_id",
            "proposed_subcategory_name", "out_of_scope_master_id", "reason"
        ],
        "additionalProperties": False,
    }

    system = """You are the service classifier for Armenia AI Guide.
Understand Armenian, Russian and English.

The partner NEVER chooses a direction or subcategory manually.
The partner only writes a service and price. You classify it.

STRICT RULES:
1. A partner may create services ONLY inside directions already approved for that partner.
2. If an existing subcategory genuinely fits, return its exact matched_category_id
   and its master_category_id. That master MUST be approved for this partner.
3. If no existing subcategory fits, but the service clearly belongs to an APPROVED
   partner direction, return matched_category_id=null, the approved master_category_id,
   and a concise proposed_subcategory_name.
4. If the service clearly belongs to a MASTER direction that is NOT approved for this
   partner, do NOT map it to any approved direction and do NOT propose a subcategory
   under an approved direction. Return out_of_scope_master_id with the real master ID.
5. If genuinely ambiguous, return all classification IDs null and no proposal.
6. Use ONLY real IDs supplied in the prompt. Never invent IDs.
Return JSON only."""

    prompt = (
        "SERVICE:\n" + json.dumps(
            {"name": _norm(name), "description": _norm(description)[:500]},
            ensure_ascii=False
        )
        + "\nAPPROVED PARTNER DIRECTIONS:\n"
        + json.dumps(approved_masters, ensure_ascii=False)
        + "\nALL PLATFORM DIRECTIONS (for detecting an unapproved direction):\n"
        + json.dumps([
            {"master_category_id": _safe_int(x.get("id")),
             "hy": _norm(x.get("name_am")),
             "ru": _norm(x.get("name_ru")),
             "en": _norm(x.get("name_en"))}
            for x in all_masters
        ], ensure_ascii=False)
        + "\nAPPROVED DIRECTION SUBCATEGORIES:\n"
        + json.dumps([
            {"category_id": _safe_int(x.get("category_id")),
             "master_category_id": _safe_int(x.get("master_category_id")),
             "hy": _norm(x.get("category_am")),
             "ru": _norm(x.get("category_ru")),
             "en": _norm(x.get("category_en")),
             "direction_hy": _norm(x.get("master_am")),
             "direction_ru": _norm(x.get("master_ru")),
             "direction_en": _norm(x.get("master_en"))}
            for x in candidates
        ], ensure_ascii=False)
    )

    client = AsyncGroq(api_key=key)
    result = await _groq_json(
        client,
        __import__("os").getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        system,
        prompt,
        "partner_service_classification",
        schema,
        260,
    )

    allowed = {_safe_int(x.get("category_id")) for x in catalog}
    cid = _safe_int(result.get("matched_category_id"))
    mid = _safe_int(result.get("master_category_id"))
    out_mid = _safe_int(result.get("out_of_scope_master_id"))

    if cid is not None and cid not in allowed:
        cid = None
    if mid is not None and mid not in approved_master_ids:
        mid = None
    all_master_ids = {_safe_int(x.get("id")) for x in all_masters}
    if out_mid is not None and out_mid not in all_master_ids:
        out_mid = None

    if cid is not None:
        row = next((x for x in catalog if _safe_int(x.get("category_id")) == cid), None)
        mid = _safe_int(row.get("master_category_id")) if row else mid
        out_mid = None

    if out_mid is not None and out_mid not in approved_master_ids:
        master = next((x for x in all_masters if _safe_int(x.get("id")) == out_mid), None)
        return {
            "status": "out_of_scope",
            "category_id": None,
            "master_category_id": None,
            "out_of_scope_master_id": out_mid,
            "out_of_scope_master_name": _norm(
                (master or {}).get("name_am")
                or (master or {}).get("name_ru")
                or (master or {}).get("name_en")
            ),
            "proposed_name": "",
            "reason": _norm(result.get("reason")),
        }

    proposed = _norm(result.get("proposed_subcategory_name"))
    return {
        "status": (
            "matched" if cid is not None
            else ("proposal" if mid is not None and proposed else "clarification")
        ),
        "category_id": cid,
        "master_category_id": mid,
        "proposed_name": proposed,
        "reason": _norm(result.get("reason")),
    }

async def api_service_create(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    data = await request.json()
    name = str(data.get("name") or data.get("service_name") or "").strip()
    description = str(data.get("description") or "").strip()
    if not name:
        return web.json_response({"ok": False, "error": "service_name_required"}, status=400)
    price = data.get("price")
    try:
        price = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid_price"}, status=400)

    try:
        match = await _ai_match_new_service(pid, name, description)
    except Exception as exc:
        return web.json_response({"ok": False, "error": "service_ai_failed", "detail": str(exc)[:300]}, status=503)

    if match["status"] == "no_catalog":
        return web.json_response({
            "ok": False,
            "error": "no_approved_catalog",
            "message": "Նախ պետք է հաստատված ուղղություն ունենաք։"
        }, status=409)

    if match["status"] == "out_of_scope":
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS service_direction_requests (
                        id BIGSERIAL PRIMARY KEY,
                        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
                        requested_master_category_id INT NOT NULL REFERENCES master_categories(id) ON DELETE RESTRICT,
                        requested_master_name TEXT,
                        requested_service_name TEXT NOT NULL,
                        description TEXT,
                        price NUMERIC,
                        proposed_subcategory_name TEXT,
                        reason TEXT,
                        status TEXT NOT NULL DEFAULT 'pending_admin',
                        admin_note TEXT,
                        partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL,
                        document_id BIGINT REFERENCES partner_verification_documents(id) ON DELETE SET NULL,
                        reviewed_by BIGINT,
                        reviewed_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    SELECT id FROM service_direction_requests
                    WHERE partner_id=%s
                      AND requested_master_category_id=%s
                      AND lower(trim(requested_service_name))=lower(trim(%s))
                      AND status IN ('pending','pending_admin','document_pending','document_under_review')
                    LIMIT 1
                """, (pid, match["out_of_scope_master_id"], name))
                duplicate = cur.fetchone()
                if duplicate:
                    request_id = int(duplicate["id"])
                else:
                    cur.execute("""
                        INSERT INTO service_direction_requests
                            (partner_id, requested_master_category_id, requested_master_name,
                             requested_service_name, proposed_subcategory_name,
                             description, price, reason, status)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending_admin')
                        RETURNING id
                    """, (
                        pid,
                        match["out_of_scope_master_id"],
                        match.get("out_of_scope_master_name") or None,
                        name,
                        match.get("proposed_name") or "",
                        description or None,
                        price,
                        match.get("reason") or None,
                    ))
                    request_id = int(cur.fetchone()["id"])
            conn.commit()
        return web.json_response({
            "ok": True,
            "direction_request_created": True,
            "request_id": request_id,
            "message": "Այս ծառայությունը ձեր հաստատված ուղղությունների մեջ չէ։ AI-ն կառուցվածքային հարցումն ուղարկել է ադմինիստրատորին՝ համապատասխան ուղղության հաստատման համար։"
        })

    if match["status"] == "clarification":
        return web.json_response({
            "ok": False,
            "error": "service_needs_clarification",
            "message": "Չկարողացա վստահ որոշել ծառայության կատալոգային համապատասխանությունը։ Գրեք ծառայության մասին մի փոքր ավելի մանրամասն։"
        }, status=422)

    if match["status"] == "proposal":
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS subcategory_proposals (
                        id BIGSERIAL PRIMARY KEY,
                        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
                        master_category_id INT NOT NULL REFERENCES master_categories(id) ON DELETE RESTRICT,
                        proposed_name TEXT NOT NULL,
                        requested_service_name TEXT,
                        description TEXT,
                        price NUMERIC,
                        status TEXT NOT NULL DEFAULT 'pending',
                        admin_note TEXT,
                        reviewed_by BIGINT,
                        reviewed_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    SELECT id FROM subcategory_proposals
                    WHERE partner_id=%s AND master_category_id=%s
                      AND lower(trim(proposed_name))=lower(trim(%s))
                      AND status='pending'
                    LIMIT 1
                """, (pid, match["master_category_id"], match["proposed_name"]))
                duplicate = cur.fetchone()
                if duplicate:
                    return web.json_response({
                        "ok": True, "proposal_created": True,
                        "proposal_id": int(duplicate["id"]),
                        "message": "Առաջարկը արդեն ուղարկված է ադմինիստրատորին։"
                    })
                cur.execute("""
                    INSERT INTO subcategory_proposals
                        (partner_id,master_category_id,proposed_name,requested_service_name,description,price,status)
                    VALUES(%s,%s,%s,%s,%s,%s,'pending')
                    RETURNING id
                """, (pid, match["master_category_id"], match["proposed_name"], name,
                      description or None, price))
                proposal_id = int(cur.fetchone()["id"])
            conn.commit()
        return web.json_response({
            "ok": True, "proposal_created": True, "proposal_id": proposal_id,
            "message": "AI-ն չի գտել համապատասխան գործող ենթակատեգորիա։ Առաջարկը ուղարկվել է ադմինիստրատորին։"
        })

    with _connect() as conn:
        with conn.cursor() as cur:
            payload = json.dumps({
                "ai_source": True,
                "matched_subcategory_id": match["category_id"],
                "master_category_id": match["master_category_id"],
            }, ensure_ascii=False)
            existing = None
            cur.execute(
                "SELECT id FROM services WHERE partner_id=%s AND lower(trim(name))=lower(trim(%s)) AND status<>'deleted' ORDER BY id DESC LIMIT 1",
                (pid, name),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """UPDATE services SET category_id=%s,subcategory_id=NULL,price=%s,
                       description=%s,status='pending',data_json=%s::jsonb,updated_at=NOW()
                       WHERE id=%s AND partner_id=%s RETURNING *""",
                    (match["category_id"], price, description, payload, existing["id"], pid),
                )
            else:
                cur.execute(
                    """INSERT INTO services(partner_id,category_id,subcategory_id,name,description,price,status,data_json)
                       VALUES(%s,%s,NULL,%s,%s,%s,'pending',%s::jsonb) RETURNING *""",
                    (pid, match["category_id"], name, description, price, payload),
                )
            row = cur.fetchone()
        conn.commit()
    return web.json_response({
        "ok": True, "service": _json(row),
        "matched_category_id": match["category_id"],
        "master_category_id": match["master_category_id"],
        "message": "Ծառայությունը դասակարգվեց AI-ի կողմից և ուղարկվեց ստուգման։"
    })


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


async def api_service_catalog(request: web.Request):
    """Return every active subcategory under the partner's approved directions."""
    uid = _auth_partner(request)
    pid = _require_partner(uid)
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
                 AND pd.master_category_id=c.master_category_id
                 AND pd.status='approved'
                WHERE c.is_active=TRUE AND m.is_active=TRUE
                ORDER BY c.master_category_id, c.id
            """, (pid,))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "categories": _json(rows)})


async def api_subcategory_proposal(request: web.Request):
    """Automatically create a pending admin proposal when no catalog match exists."""
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    data = await request.json()
    proposed_name = str(data.get("proposed_name") or "").strip()
    master_id = int(data.get("master_category_id") or 0)
    if not proposed_name:
        return web.json_response({"ok": False, "error": "proposed_name_required"}, status=400)
    if not master_id:
        return web.json_response({"ok": False, "error": "master_category_required"}, status=400)

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM partner_directions
                WHERE partner_id=%s AND master_category_id=%s AND status='approved'
            """, (pid, master_id))
            if not cur.fetchone():
                return web.json_response({"ok": False, "error": "direction_not_approved"}, status=403)

            cur.execute("""
                SELECT id,name_am,name_ru,name_en FROM categories
                WHERE master_category_id=%s AND is_active=TRUE
            """, (master_id,))
            rows = cur.fetchall()

            import re
            def norm(v):
                return re.sub(r"[^a-z0-9\u0531-\u0587]+", "", str(v or "").lower())
            wanted = norm(proposed_name)
            for row in rows:
                names = {norm(row["name_am"]), norm(row["name_ru"]), norm(row["name_en"])}
                if wanted and wanted in names:
                    return web.json_response({
                        "ok": False, "error": "subcategory_already_exists", "category": dict(row)
                    }, status=409)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS subcategory_proposals (
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
                    master_category_id INT NOT NULL REFERENCES master_categories(id) ON DELETE RESTRICT,
                    proposed_name TEXT NOT NULL,
                    requested_service_name TEXT,
                    description TEXT,
                    price NUMERIC,
                    status TEXT NOT NULL DEFAULT 'pending',
                    admin_note TEXT,
                    reviewed_by BIGINT,
                    reviewed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                SELECT id FROM subcategory_proposals
                WHERE partner_id=%s AND master_category_id=%s
                  AND lower(trim(proposed_name))=lower(trim(%s))
                  AND status='pending'
                LIMIT 1
            """, (pid, master_id, proposed_name))
            duplicate = cur.fetchone()
            if duplicate:
                return web.json_response({
                    "ok": True,
                    "proposal_id": int(duplicate["id"]),
                    "message": "Այս ենթակատեգորիայի առաջարկն արդեն ուղարկված է ադմինիստրատորին։"
                })

            cur.execute("""
                INSERT INTO subcategory_proposals
                    (partner_id,master_category_id,proposed_name,requested_service_name,description,price,status)
                VALUES(%s,%s,%s,%s,%s,%s,'pending')
                RETURNING id
            """, (pid, master_id, proposed_name,
                  str(data.get("requested_service_name") or "").strip() or None,
                  str(data.get("description") or "").strip() or None,
                  data.get("price")))
            proposal_id = int(cur.fetchone()["id"])
        conn.commit()
    return web.json_response({
        "ok": True,
        "proposal_id": proposal_id,
        "message": "Առաջարկը ավտոմատ ուղարկվեց ադմինիստրատորին։"
    })


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
    app.router.add_get("/api/master/{id}/service-catalog", api_service_catalog)
    app.router.add_post("/api/master/{id}/subcategory-proposals", api_subcategory_proposal)
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
    # NOTE: GET /api/master/{id}/documents is already registered by
    # register_stage3_routes (api_partner_documents), which is called earlier
    # in main.py on the same app. Registering it here too raised aiohttp
    # RuntimeError ("Added route will never be executed") on startup, so this
    # duplicate was removed. The stage3 handler returns a superset
    # ({ok, partner, documents}); clients reading `.documents` are unaffected.
    app.router.add_post("/api/partner/cabinet/update-by-image", update_profile_by_image)
    app.router.add_post("/api/partner/cabinet/update-by-voice", update_profile_by_voice)
