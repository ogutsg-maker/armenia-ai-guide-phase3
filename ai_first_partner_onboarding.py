"""Clean AI-first partner onboarding persistence.

This module is the only persistence path used by the active partner AI chat.
It deliberately does not use the old city/category button registration flow.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from database import _connect

logger = logging.getLogger(__name__)


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


def _exec(db, query: str, params=(), returning=False):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone() if returning else None
            result = None
            if row is not None:
                cols = [d.name for d in cur.description]
                result = dict(zip(cols, row))
            conn.commit()
            return result


def _fetchone(db, query: str, params=()):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                return None
            return dict(zip([d.name for d in cur.description], row))


def _fetchall(db, query: str, params=()):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in rows]


def _ensure_partner(db, uid: int, profile: dict[str, Any]):
    partner = db.get_partner_by_user(uid)
    name = str(profile.get("business_name") or "").strip()[:200]
    description = str(profile.get("description") or "").strip()[:5000]
    profile_json = json.dumps(profile, ensure_ascii=False)
    if partner:
        pid = int(partner["id"])
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


def _save_location(db, partner_id: int, profile: dict[str, Any]):
    city = str(profile.get("city") or "").strip()[:200]
    district = str(profile.get("district") or "").strip()[:200] or None
    if not city:
        return
    _exec(
        db,
        """
        DELETE FROM partner_locations
        WHERE partner_id=%s AND COALESCE(city,'')=%s AND COALESCE(data_json->>'district','')=COALESCE(%s,'')
        """,
        (partner_id, city, district),
    )
    _exec(
        db,
        """
        INSERT INTO partner_locations(partner_id,country,city,data_json)
        VALUES(%s,'Armenia',%s,%s::jsonb)
        """,
        (partner_id, city, json.dumps({"district": district}, ensure_ascii=False)),
    )


def _save_direction(db, partner_id: int, profile: dict[str, Any]) -> tuple[int | None, bool, str | None]:
    master_id, category_ids = _direction_match(db, profile)
    if not master_id:
        _exec(
            db,
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

    pd = _fetchone(
        db,
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
    direction_id = int(pd["id"])

    _exec(db, "DELETE FROM partner_direction_categories WHERE partner_direction_id=%s", (direction_id,))
    for category_id in category_ids:
        _exec(
            db,
            "INSERT INTO partner_direction_categories(partner_direction_id,category_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (direction_id, int(category_id)),
        )
    return direction_id, True, None


def _save_services(db, partner_id: int, direction_id: int | None, profile: dict[str, Any]):
    if not direction_id:
        return 0
    category_ids = [
        r["category_id"]
        for r in _fetchall(
            db,
            "SELECT category_id FROM partner_direction_categories WHERE partner_direction_id=%s",
            (direction_id,),
        )
    ]
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

        # The current catalogue uses the services table.  Do not put a
        # categories.id into catalog_subcategories.id; when a dedicated
        # catalog_subcategories row exists, it can be linked later by admin.
        existing = _fetchone(
            db,
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
            _exec(
                db,
                "UPDATE services SET category_id=%s,subcategory_id=NULL,price=%s,status='pending',data_json=%s::jsonb,updated_at=NOW() WHERE id=%s",
                (default_category, price, payload, existing["id"]),
            )
        else:
            _exec(
                db,
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
    """Persist a completed AI application without approving anything."""
    if not _profile_ready(profile):
        raise ValueError("partner_profile_not_ready")

    partner_id = _ensure_partner(db, uid, profile)
    db.update_user_field(uid, "city", str(profile.get("city") or "").strip()[:200])
    _save_location(db, partner_id, profile)
    direction_id, mapped, proposal = _save_direction(db, partner_id, profile)
    service_count = _save_services(db, partner_id, direction_id, profile)

    return {
        "partner_id": partner_id,
        "direction_id": direction_id,
        "mapped_to_catalog": mapped,
        "proposal_created": proposal == "proposal",
        "service_count": service_count,
        "status": "pending",
    }
