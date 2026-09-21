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
    bid = _business_id(request,pid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM partner_objects WHERE partner_id=%s AND business_id=%s ORDER BY id", (pid,bid))
            rows = cur.fetchall()
    return web.json_response({"ok": True, "objects": _json(rows)})


async def api_object_create(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request,pid)
    data = await request.json()
    name = str(data.get("object_name") or data.get("name") or "").strip()
    if not name:
        return web.json_response({"ok": False, "error": "object_name_required"}, status=400)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO partner_objects(partner_id,business_id,object_name,address,city,marz,data_json)
                   VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (pid,bid,name,data.get("address"),data.get("city"),data.get("marz"),json.dumps(data.get("data_json") or {})),
            )
            row = cur.fetchone()
        conn.commit()
    return web.json_response({"ok": True, "object": _json(row)})


async def api_object_delete(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request,pid)
    oid = int(request.match_info["object_id"])
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM partner_objects WHERE id=%s AND partner_id=%s AND business_id=%s RETURNING id", (oid,pid,bid))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return web.json_response({"ok": False, "error": "object_not_found"}, status=404)
    return web.json_response({"ok": True, "id": oid})


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
                """SELECT s.*
                   FROM services s
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


async def _load_partner_service_catalog(pid: int, business_id: int | None):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.id AS category_id,c.master_category_id,c.name_am AS category_am,c.name_ru AS category_ru,c.name_en AS category_en,c.slug AS category_slug,
                                  m.name_am AS master_am,m.name_ru AS master_ru,m.name_en AS master_en,m.slug AS master_slug
                           FROM categories c JOIN master_categories m ON m.id=c.master_category_id
                           JOIN partner_directions pd ON pd.partner_id=%s AND pd.master_category_id=c.master_category_id
                              AND pd.status='approved' AND pd.business_id=%s
                           WHERE c.is_active=TRUE AND m.is_active=TRUE ORDER BY c.master_category_id,c.id""",(pid,business_id))
            return [dict(x) for x in cur.fetchall()]


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


async def _ai_match_new_service(pid: int, name: str, description: str = "", business_id: int | None = None):
    """Classify a newly added partner service without making Groq a hard dependency.

    The partner cabinet must still work when Groq is rate-limited or temporarily
    unavailable. We first use the real active catalogue and deterministic
    multilingual matching; Groq is only an optional semantic fallback.
    """
    import os
    import re
    from difflib import SequenceMatcher

    # New services are classified against the COMPLETE active catalogue.
    # We deliberately do not start from the partner's approved directions:
    # otherwise an unrelated approved subcategory can win before AI ever sees
    # the real catalogue.
    catalog = await _load_full_service_catalog()

    def norm_match(value):
        return re.sub(r"[^a-zа-яёա-ֆ0-9]+", " ", str(value or "").lower(), flags=re.IGNORECASE).strip()

    query = norm_match(f"{name} {description}")
    name_norm = norm_match(name)

    # Multilingual semantic groups for services whose short name is often too
    # small for character similarity alone. They never create catalogue IDs.
    synonym_groups = [
        {"կտրում", "կտրել", "մազերի կտրում", "մազկտրում", "վարսավիր", "haircut", "barber", "парикмахер", "стрижка", "стрижку", "стрижки"},
        {"ներկում", "ներկել", "մազերի ներկում", "մազերի ներկել", "coloring", "hair coloring", "haircolor", "окрашивание", "окраска", "краска"},
        {"ոճավորում", "սանրվածք", "մազերի սանրվածք", "укладка", "уклад", "styling", "hairstyling"},
        {"մատնահարդարում", "маникюр", "manicure"},
        {"պեդիկյուր", "ոտնահարդարում", "педикюр", "pedicure"},
        {"դիմահարդարում", "макияж", "makeup"},
        {"տրանսֆեր", "transfer", "трансфер"},
        {"էքսկուրսիա", "экскурсия", "экскурсии", "tour", "excursion"},
        {"լուսանկար", "լուսանկարիչ", "ֆոտո", "фотограф", "фотография", "photographer", "photography"},
        {"տեսանկարահանում", "տեսագրում", "վիդեո", "видеограф", "видеосъемка", "video", "videography"},
        {"սանտեխնիկ", "սանտեխնիկա", "սանտեխնիկական", "սանտեխնիկական աշխատանքներ", "ջրամատակարարում", "ջրահեռացում", "водопровод", "сантехника", "сантехник", "сантехнические работы", "plumbing", "plumber"},
    ]

    def concepts(value):
        text = norm_match(value)
        found = set()
        for index, group in enumerate(synonym_groups):
            if any(norm_match(term) and norm_match(term) in text for term in group):
                found.add(index)
        return found

    q_tokens = set(query.split())
    q_concepts = concepts(name_norm)

    def label_score(labels):
        best = 0.0
        for label in labels:
            s = norm_match(label)
            if not s:
                continue
            label_concepts = concepts(s)
            concept_score = 0.0
            if q_concepts and label_concepts:
                concept_score = len(q_concepts & label_concepts) / max(len(q_concepts), len(label_concepts))
            label_tokens = set(s.split())
            token_score = len(q_tokens & label_tokens) / max(1, min(len(q_tokens), len(label_tokens)))
            ratio = SequenceMatcher(None, name_norm, s).ratio()
            containment = 1.0 if (name_norm and (name_norm in s or s in name_norm)) else 0.0
            best = max(best, concept_score * 0.70 + token_score * 0.15 + ratio * 0.10 + containment * 0.05)
        return best

    def master_score(master_rows):
        # Direction is selected FIRST. A direction gets the strongest signal
        # from its own translated names and also a bounded signal from the
        # services/subcategories that belong to it.
        direct = label_score([
            master_rows[0].get("master_am"),
            master_rows[0].get("master_ru"),
            master_rows[0].get("master_en"),
            master_rows[0].get("master_slug"),
        ])
        child = max((label_score([
            r.get("category_am"), r.get("category_ru"),
            r.get("category_en"), r.get("category_slug")
        ]) for r in master_rows), default=0.0)
        return max(direct, child * 0.90)

    grouped = {}
    for row in catalog:
        grouped.setdefault(_safe_int(row["master_category_id"]), []).append(row)

    ranked_masters = sorted(grouped.values(), key=master_score, reverse=True)
    selected_rows = ranked_masters[0] if ranked_masters else []
    selected_master_score = master_score(selected_rows) if selected_rows else 0.0
    ranked_categories = sorted(
        selected_rows,
        key=lambda r: label_score([
            r.get("category_am"), r.get("category_ru"),
            r.get("category_en"), r.get("category_slug")
        ]),
        reverse=True,
    )
    best = ranked_categories[0] if ranked_categories else None
    best_score = label_score([
        best.get("category_am"), best.get("category_ru"),
        best.get("category_en"), best.get("category_slug")
    ]) if best else 0.0

    # Never accept a weak hierarchical match. In particular, a word such as
    # "սանտեխնիկ" must not fall into an unrelated category such as apartment
    # cleaning merely because that direction happens to be approved.
    if best and selected_master_score >= 0.55 and best_score >= 0.55:
        master_id = _safe_int(best["master_category_id"])
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM partner_directions
                       WHERE partner_id=%s AND business_id=%s
                         AND master_category_id=%s AND status='approved'
                       LIMIT 1""",
                    (pid, business_id, master_id),
                )
                approved = bool(cur.fetchone())

        if approved:
            return {
                "status": "matched",
                "category_id": _safe_int(best["category_id"]),
                "master_category_id": master_id,
                "business_action": "same_business",
                "proposed_business_name": None,
                "reason": "Hierarchical multilingual catalogue match: direction first, subcategory second.",
            }

        return {
            "status": "proposal",
            "category_id": _safe_int(best["category_id"]),
            "master_category_id": master_id,
            "out_of_scope_master_id": master_id,
            "out_of_scope_master_name": best.get("master_am") or best.get("master_ru") or best.get("master_en"),
            "proposed_name": best.get("category_am") or best.get("category_ru") or best.get("category_en"),
            "business_action": "same_business",
            "proposed_business_name": None,
            "reason": "The service matches a real catalogue direction, but that direction is not yet approved for this partner.",
        }

    # Reuse the same category scorer for the optional provider fallback.
    def score(row):
        return label_score([
            row.get("category_am"), row.get("category_ru"),
            row.get("category_en"), row.get("category_slug")
        ])

    # Optional Groq semantic fallback. IMPORTANT: this classification receives
    # the COMPLETE active catalogue, not only directions already approved for
    # this partner. Otherwise a genuinely new direction can never be identified.
    key = os.getenv("GROQ_API_KEY", "").strip()
    if key and AsyncGroq is not None:
        try:
            all_catalog = await _load_full_service_catalog()
            all_ranked = sorted(all_catalog, key=score, reverse=True)
            candidates = [x for x in all_ranked[:80] if score(x) >= 0.10] or all_ranked[:80]
            schema = {
                "type": "object",
                "properties": {
                    "matched_category_id": {"type": ["integer", "null"]},
                    "master_category_id": {"type": ["integer", "null"]},
                    "proposed_subcategory_name": {"type": ["string", "null"]},
                    "out_of_scope_master_id": {"type": ["integer", "null"]},
                    "business_action": {"type": "string"},
                    "proposed_business_name": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": [
                    "matched_category_id", "master_category_id",
                    "proposed_subcategory_name", "out_of_scope_master_id",
                    "business_action", "proposed_business_name", "reason",
                ],
                "additionalProperties": False,
            }
            system = """Classify one partner service against the supplied real catalogue.
Understand Armenian, Russian and English semantically.
Return an existing category ID only when it is a clear semantic match.
Never invent an ID. If there is no clear match, return null.
Return the result as valid JSON only."""
            prompt = json.dumps({
                "service": {"name": name, "description": description[:800]},
                "subcategories": [
                    {
                        "id": _safe_int(x["category_id"]),
                        "master_id": _safe_int(x["master_category_id"]),
                        "hy": x.get("category_am"),
                        "ru": x.get("category_ru"),
                        "en": x.get("category_en"),
                    }
                    for x in candidates
                ],
            }, ensure_ascii=False)
            result = await _groq_json(
                AsyncGroq(api_key=key),
                os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
                system,
                prompt,
                "partner_service_classification",
                schema,
                280,
            )
            cid = _safe_int(result.get("matched_category_id"))
            valid = {_safe_int(x["category_id"]) for x in all_catalog}
            if cid in valid:
                row = next(x for x in all_catalog if _safe_int(x["category_id"]) == cid)
                groq_mid = _safe_int(row["master_category_id"])
                with _connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """SELECT 1 FROM partner_directions
                               WHERE partner_id=%s AND business_id=%s
                                 AND master_category_id=%s AND status='approved'
                               LIMIT 1""",
                            (pid, business_id, groq_mid),
                        )
                        groq_approved = bool(cur.fetchone())
                if groq_approved:
                    return {
                        "status": "matched",
                        "category_id": cid,
                        "master_category_id": groq_mid,
                        "business_action": "same_business",
                        "proposed_business_name": None,
                        "reason": _norm(result.get("reason")) or "AI semantic catalogue match.",
                    }
                return {
                    "status": "proposal",
                    "category_id": cid,
                    "master_category_id": groq_mid,
                    "out_of_scope_master_id": groq_mid,
                    "out_of_scope_master_name": row.get("master_am") or row.get("master_ru") or row.get("master_en"),
                    "proposed_name": row.get("category_am") or row.get("category_ru") or row.get("category_en"),
                    "business_action": "same_business",
                    "proposed_business_name": None,
                    "reason": _norm(result.get("reason")) or "The matched catalogue direction is not yet approved for this partner.",
                }
        except Exception as exc:
            # Rate limits and provider failures are non-fatal here.
            # Log only a short diagnostic and continue to the safe fallback.
            logging.getLogger(__name__).warning(
                "partner_service_ai_fallback: %s", str(exc)[:240]
            )

    # Existing platform categories that are not approved for this business
    # should become an admin proposal rather than a technical failure.
    try:
        all_catalog = await _load_full_service_catalog()
        all_ranked = sorted(all_catalog, key=score, reverse=True)
        candidate = all_ranked[0] if all_ranked else None
        if candidate and score(candidate) >= 0.58:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT master_category_id FROM partner_directions
                           WHERE partner_id=%s AND business_id=%s AND status='approved'""",
                        (pid, business_id),
                    )
                    approved_masters = {_safe_int(x["master_category_id"]) for x in cur.fetchall()}
            mid = _safe_int(candidate["master_category_id"])
            if mid not in approved_masters:
                return {
                    "status": "out_of_scope",
                    "category_id": None,
                    "master_category_id": None,
                    "out_of_scope_master_id": mid,
                    "out_of_scope_master_name": _norm(
                        candidate.get("master_am")
                        or candidate.get("master_ru")
                        or candidate.get("master_en")
                    ),
                    "proposed_name": _norm(
                        candidate.get("category_am")
                        or candidate.get("category_ru")
                        or candidate.get("category_en")
                    ),
                    "business_action": "same_business",
                    "proposed_business_name": None,
                    "reason": "Existing catalogue category is not yet approved for this business.",
                }
    except Exception:
        logging.getLogger(__name__).exception(
            "partner_service_catalog_fallback_failed"
        )

    # A new service must never fail just because the catalogue match is
    # uncertain. The product contract says unmatched services become an
    # administrator proposal. Keep the partner's service data intact and let
    # the admin classify it.
    return {
        "status": "proposal",
        "category_id": None,
        "master_category_id": None,
        "business_action": "same_business",
        "proposed_business_name": None,
        "proposed_name": name,
        "reason": "No sufficiently confident approved catalogue match was found; sent to administrator for classification.",
    }

async def api_service_create(request: web.Request):
    uid=_auth_partner(request); pid=_require_partner(uid); bid=_business_id(request,pid)
    data=await request.json(); name=str(data.get("name") or data.get("service_name") or "").strip(); description=str(data.get("description") or "").strip()
    if not name: return web.json_response({"ok":False,"error":"service_name_required"},status=400)
    try: price=float(data.get("price")) if data.get("price") not in (None,"") else None
    except (TypeError,ValueError): return web.json_response({"ok":False,"error":"invalid_price"},status=400)
    try: match=await _ai_match_new_service(pid,name,description,bid)
    except Exception as exc: return web.json_response({"ok":False,"error":"service_ai_failed","detail":str(exc)[:300]},status=503)
    if match["status"] in {"out_of_scope","proposal"} or match.get("business_action")=="new_business":
        from partner_business_application_api import ensure_business_application_schema
        ensure_business_application_schema()
        app_bid=None if match.get("business_action")=="new_business" else bid
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT name FROM partner_businesses WHERE id=%s AND partner_id=%s""",(bid,pid))
                business_row=cur.fetchone() or {}
                cur.execute("""SELECT u.phone FROM users u WHERE u.telegram_id=%s""",(uid,))
                user_row=cur.fetchone() or {}
                cur.execute("""INSERT INTO partner_applications(
                                  partner_id,business_id,status,business_name,phone,direction_name,
                                  master_category_id,subcategory_name,service_name,price,description,ai_reason,payload_json)
                               VALUES(%s,%s,'pending_admin',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                               RETURNING id""",
                            (pid,app_bid,business_row.get("name") if app_bid is not None else match.get("proposed_business_name"),
                             user_row.get("phone"),match.get("out_of_scope_master_name") or "",
                             match.get("out_of_scope_master_id") or match.get("master_category_id"),
                             match.get("proposed_name") or None,name,price,description,match.get("reason") or "",
                             json.dumps({"source":"partner_service","current_business_id":bid,"new_business":app_bid is None,
                                         "services":[{"name":name,"price":price,"description":description,
                                                      "matched_subcategory_id":match.get("category_id"),
                                                      "direction_id":match.get("master_category_id")}]},ensure_ascii=False)))
                aid=int(cur.fetchone()["id"])
            conn.commit()
        return web.json_response({"ok":True,"proposal_created":True,"application_id":aid,"new_business":app_bid is None,
                                  "message":"AI-ն կազմեց ամբողջական հայտ և ուղարկեց ադմինիստրատորին։"})
    if match["status"]=="no_catalog":
        # Kept only for defensive compatibility. The matcher now converts an
        # empty approved catalogue into an admin proposal instead of blocking
        # the partner with a technical 409.
        return web.json_response({"ok":True,"proposal_created":True,
                                  "message":"Ծառայությունն ուղարկվել է ադմինիստրատորին դասակարգման համար։"})
    if match["status"]=="clarification": return web.json_response({"ok":False,"error":"service_needs_clarification","message":"Գրեք ծառայության մասին մի փոքր ավելի մանրամասն։"},status=422)
    payload=json.dumps({"ai_source":True,"matched_subcategory_id":match["category_id"],"master_category_id":match["master_category_id"],"business_id":bid},ensure_ascii=False)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM services WHERE partner_id=%s AND business_id=%s AND lower(trim(name))=lower(trim(%s)) AND status<>'deleted' ORDER BY id DESC LIMIT 1",(pid,bid,name))
            old=cur.fetchone()
            if old:
                cur.execute("UPDATE services SET category_id=%s,subcategory_id=NULL,price=%s,description=%s,status='pending',data_json=%s::jsonb,updated_at=NOW() WHERE id=%s AND partner_id=%s AND business_id=%s RETURNING *",(match["category_id"],price,description,payload,old["id"],pid,bid))
            else:
                cur.execute("INSERT INTO services(partner_id,business_id,category_id,subcategory_id,name,description,price,status,data_json) VALUES(%s,%s,%s,NULL,%s,%s,%s,'pending',%s::jsonb) RETURNING *",(pid,bid,match["category_id"],name,description,price,payload))
            row=cur.fetchone()
        conn.commit()
    return web.json_response({"ok":True,"service":_json(row),"matched_category_id":match["category_id"],"master_category_id":match["master_category_id"],"message":"Ծառայությունը դասակարգվեց AI-ի կողմից և ուղարկվեց ստուգման։"})

async def api_service_update(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request,pid)
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
            cur.execute(f"UPDATE services SET {sets}, updated_at=NOW() WHERE id=%s AND partner_id=%s AND business_id=%s RETURNING *", (*fields.values(), sid, pid,bid))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return web.json_response({"ok": False, "error": "service_not_found"}, status=404)
    return web.json_response({"ok": True, "service": _json(row)})


async def api_service_delete(request: web.Request):
    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request,pid)
    sid = int(request.match_info["service_id"])
    bid = _business_id(request, pid)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE services SET status='deleted', updated_at=NOW() WHERE id=%s AND partner_id=%s AND business_id=%s RETURNING id", (sid, pid,bid))
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
