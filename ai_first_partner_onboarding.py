"""Clean AI-first partner onboarding persistence.

This module is the only persistence path used by the active partner AI chat.
It deliberately does not use the old city/category button registration flow.

Atomicity: persist_ready_application() writes partner_locations,
partner_directions, partner_direction_categories and services inside a
SINGLE database transaction (one connection). Either the whole catalogue
footprint of the application lands, or nothing does — we never leave a
partner with a direction but no services, or services whose category link
never got written.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from database import _connect

logger = logging.getLogger(__name__)


def _safe_int(value):
    """Return a real integer ID, never raise on AI/database-shaped values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        s = str(value).strip()
        if not s or not s.isdigit():
            return None
        return int(s)
    except (TypeError, ValueError):
        return None


def _profile_ready(profile: dict[str, Any]) -> bool:
    return bool(
        str(profile.get("business_name") or "").strip()
        and str(profile.get("city") or "").strip()
        and str(profile.get("direction") or "").strip()
        and profile.get("services")
    )


def _direction_match(db, profile: dict[str, Any]) -> tuple[int | None, list[int]]:
    from partner_registration_ai import match_catalog
    return match_catalog(db, profile)


# --------------------------------------------------------------------------
# Cursor-based helpers. Every write in persist_ready_application() shares ONE
# cursor/connection so the whole application is a single atomic transaction.
# --------------------------------------------------------------------------
def _cur_one(cur, query: str, params=()):
    cur.execute(query, params)
    row = cur.fetchone()
    if not row:
        return None
    return dict(zip([d.name for d in cur.description], row))


def _cur_all(cur, query: str, params=()):
    cur.execute(query, params)
    rows = cur.fetchall()
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def _cur_exec(cur, query: str, params=(), returning=False):
    cur.execute(query, params)
    if not returning:
        return None
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([d.name for d in cur.description], row))


def _ensure_partner(db, uid: int, profile: dict[str, Any]):
    """Upsert the partner row. Runs BEFORE the catalogue transaction because
    partner_locations/partner_directions FK it, and because create/update go
    through database.py which manages its own connection. It is idempotent, so
    a retry after a rolled-back catalogue transaction simply reuses the row.
    """
    partner = db.get_partner_by_user(uid)
    name = str(profile.get("business_name") or "").strip()[:200]
    description = str(profile.get("description") or "").strip()[:5000]
    profile_json = profile  # pass the dict; update_partner() serializes and casts to JSONB (%s::jsonb). A pre-serialized string hits the non-jsonb branch and fails on the JSONB column.
    if partner:
        pid = _safe_int(partner.get("id"))
        if not pid:
            raise ValueError("partner_id_invalid")
        # Never silently approve a new/changed AI application.
        if partner.get("status") not in ("approved", "suspended", "blocked"):
            db.update_partner(
                pid,
                business_name=name,
                business_description=description,
                status="pending",
                verification_status="not_submitted",
                profile_json=profile_json,
            )
        else:
            db.update_partner(pid, business_name=name, business_description=description, profile_json=profile_json)
        return pid

    pid = db.create_partner(uid)
    db.update_partner(
        pid,
        business_name=name,
        business_description=description,
        status="pending",
        verification_status="not_submitted",
        profile_json=profile_json,
    )
    return pid


def _save_location(cur, partner_id: int, profile: dict[str, Any]):
    city = str(profile.get("city") or "").strip()[:200]
    district = str(profile.get("district") or "").strip()[:200] or None
    if not city:
        return
    _cur_exec(
        cur,
        """
        DELETE FROM partner_locations
        WHERE partner_id=%s AND COALESCE(city,'')=%s AND COALESCE(data_json->>'district','')=COALESCE(%s,'')
        """,
        (partner_id, city, district),
    )
    _cur_exec(
        cur,
        """
        INSERT INTO partner_locations(partner_id,country,city,data_json)
        VALUES(%s,'Armenia',%s,%s::jsonb)
        """,
        (partner_id, city, json.dumps({"district": district}, ensure_ascii=False)),
    )


def _save_direction(cur, partner_id: int, profile: dict[str, Any], master_id, category_ids) -> tuple[int | None, bool, str | None]:
    if not master_id:
        # No catalogue match → raise a proposal for the admin. The FULL profile
        # (including services) is stored in payload_json so that approving the
        # proposal (catalog_manager.activate_proposal) can materialise the
        # services later — they are NOT lost.
        _cur_exec(
            cur,
            """
            INSERT INTO ai_catalog_proposals(
                source,partner_id,proposed_master_category,proposed_category,
                description,reason,payload_json,status
            ) VALUES('partner_ai',%s,%s,%s,%s,%s,%s::jsonb,'pending')
            """,
            (
                partner_id,
                str(profile.get("direction") or "").strip()[:300],
                ", ".join(str(x) for x in (profile.get("subcategory_names") or []))[:1000],
                str(profile.get("description") or "")[:5000],
                "AI could not map the partner's direction to an active catalogue direction.",
                json.dumps(profile, ensure_ascii=False),
            ),
        )
        return None, False, "proposal"

    pd = _cur_one(
        cur,
        """
        INSERT INTO partner_directions(partner_id,master_category_id,status)
        VALUES(%s,%s,'pending')
        ON CONFLICT(partner_id,master_category_id)
        DO UPDATE SET status=CASE WHEN partner_directions.status='rejected' THEN 'pending' ELSE partner_directions.status END,
                      rejection_reason=NULL,updated_at=NOW()
        RETURNING id
        """,
        (partner_id, master_id),
    )
    direction_id = _safe_int(pd.get("id")) if pd else None
    if not direction_id:
        raise ValueError("partner_direction_id_invalid")

    _cur_exec(cur, "DELETE FROM partner_direction_categories WHERE partner_direction_id=%s", (direction_id,))
    # Guarantee at least one subcategory link. Client search INNER JOINs
    # services -> partner_direction_categories on category_id, so a service
    # with a NULL/unlinked category is invisible. If the AI could not map a
    # concrete subcategory, fall back to the first active subcategory of the
    # matched direction so the partner's services stay discoverable.
    if not category_ids:
        fallback = _cur_one(
            cur,
            "SELECT id FROM categories WHERE master_category_id=%s AND is_active=TRUE ORDER BY id LIMIT 1",
            (master_id,),
        )
        fb_id = _safe_int(fallback.get("id")) if fallback else None
        if fb_id is not None:
            category_ids = [fb_id]
    for category_id in category_ids:
        safe_category_id = _safe_int(category_id)
        if safe_category_id is None:
            continue
        _cur_exec(
            cur,
            "INSERT INTO partner_direction_categories(partner_direction_id,category_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (direction_id, safe_category_id),
        )
    return direction_id, True, None


def _save_services(cur, partner_id: int, direction_id: int | None, profile: dict[str, Any]):
    if not direction_id:
        return 0
    raw_category_ids = [
        r.get("category_id")
        for r in _cur_all(
            cur,
            "SELECT category_id FROM partner_direction_categories WHERE partner_direction_id=%s",
            (direction_id,),
        )
    ]
    category_ids = [_safe_int(value) for value in raw_category_ids]
    category_ids = [value for value in category_ids if value is not None]
    default_category = category_ids[0] if category_ids else None
    count = 0
    for item in profile.get("services") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:300]
        if not name:
            continue
        price = item.get("price")
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None

        # 3-level catalogue: master_categories -> categories -> services.
        # A service is attached to a categories.id (subcategory_id stays NULL);
        # there is no separate catalog_subcategories level.
        existing = _cur_one(
            cur,
            "SELECT id FROM services WHERE partner_id=%s AND name=%s AND status<>'deleted' ORDER BY id DESC LIMIT 1",
            (partner_id, name),
        )
        payload = json.dumps(
            {
                "ai_source": True,
                "price_type": item.get("price_type") or "unknown",
                "direction_id": direction_id,
            },
            ensure_ascii=False,
        )
        if existing:
            _cur_exec(
                cur,
                "UPDATE services SET category_id=%s,subcategory_id=NULL,price=%s,status='pending',data_json=%s::jsonb,updated_at=NOW() WHERE id=%s",
                (default_category, price, payload, existing["id"]),
            )
        else:
            _cur_exec(
                cur,
                """
                INSERT INTO services(partner_id,category_id,subcategory_id,name,description,price,status,data_json)
                VALUES(%s,%s,NULL,%s,%s,%s,'pending',%s::jsonb)
                """,
                (
                    partner_id,
                    default_category,
                    name,
                    str(profile.get("description") or "")[:1000],
                    price,
                    payload,
                ),
            )
        count += 1
    return count


def persist_ready_application(db, uid: int, profile: dict[str, Any]) -> dict[str, Any]:
    """Persist a completed AI application without approving anything.

    The location/direction/services writes run in ONE transaction so a failure
    mid-way rolls back cleanly instead of leaving a half-written application.
    """
    if not _profile_ready(profile):
        raise ValueError("partner_profile_not_ready")

    # Partner row first (FK parent) — idempotent upsert, its own connection.
    partner_id = _ensure_partner(db, uid, profile)
    # Catalogue match is a read — do it outside the write transaction.
    master_id, category_ids = _direction_match(db, profile)

    with _connect() as conn:
        try:
            with conn.cursor() as cur:
                # City belongs to partner_locations in the current schema, not users.
                _save_location(cur, partner_id, profile)
                direction_id, mapped, proposal = _save_direction(cur, partner_id, profile, master_id, category_ids)
                service_count = _save_services(cur, partner_id, direction_id, profile)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "partner_id": partner_id,
        "direction_id": direction_id,
        "mapped_to_catalog": mapped,
        "proposal_created": proposal == "proposal",
        "service_count": service_count,
        "status": "pending",
    }
