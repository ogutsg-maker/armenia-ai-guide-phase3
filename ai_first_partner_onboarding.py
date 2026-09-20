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
        and str(profile.get("marz") or profile.get("region") or "").strip()
        and str(profile.get("city") or "").strip()
        and str(profile.get("address") or "").strip()
        and str(profile.get("phone") or "").strip()
        and profile.get("services")
    )


def _direction_match(db, profile: dict[str, Any]) -> tuple[int | None, list[int]]:
    from partner_registration_ai import match_catalog
    return match_catalog(db, profile)


# --------------------------------------------------------------------------
# Cursor-based helpers. Every write in persist_ready_application() shares ONE
# cursor/connection so the whole application is a single atomic transaction.
# --------------------------------------------------------------------------
def _row_to_dict(cur, row):
    # database._connect() uses row_factory=dict_row, so rows already arrive as
    # plain dicts. Older code did dict(zip(cur.description, row)); with a dict
    # row that zips over the dict KEYS and silently replaces every value with
    # its column name (e.g. id -> "id"), which broke _safe_int() downstream.
    # Handle both dict rows and legacy tuple rows.
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    cols = [d.name for d in cur.description] if cur.description else []
    return dict(zip(cols, row))


def _cur_one(cur, query: str, params=()):
    cur.execute(query, params)
    return _row_to_dict(cur, cur.fetchone())


def _cur_all(cur, query: str, params=()):
    cur.execute(query, params)
    return [_row_to_dict(cur, row) for row in cur.fetchall()]


def _cur_exec(cur, query: str, params=(), returning=False):
    cur.execute(query, params)
    if not returning:
        return None
    return _row_to_dict(cur, cur.fetchone())


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
    # IMPORTANT: never invent or silently choose a category here.
    # category_ids are the exact IDs selected by the AI and already validated
    # against the real catalogue in partner_registration_ai.match_catalog().
    # If AI cannot map a service, it remains unclassified instead of being put
    # into an arbitrary first category.
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

    # Only categories explicitly linked to this partner direction are allowed.
    linked_rows = _cur_all(
        cur,
        "SELECT category_id FROM partner_direction_categories WHERE partner_direction_id=%s",
        (direction_id,),
    )
    linked_category_ids = {
        cid for cid in (_safe_int(r.get("category_id")) for r in linked_rows)
        if cid is not None
    }

    count = 0
    for item in profile.get("services") or []:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or "").strip()[:300]
        if not name:
            continue

        # The AI returns the exact existing categories.id selected from the
        # catalogue. Validate it one more time against this partner direction.
        matched_category_id = _safe_int(item.get("matched_subcategory_id"))
        if matched_category_id not in linked_category_ids:
            matched_category_id = None

        price = item.get("price")
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None

        # 3-level catalogue: master_categories -> categories -> services.
        # category_id is the real catalogue subcategory link.
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
                "matched_subcategory_id": matched_category_id,
            },
            ensure_ascii=False,
        )

        if existing:
            _cur_exec(
                cur,
                "UPDATE services SET category_id=%s,subcategory_id=NULL,price=%s,status='pending',data_json=%s::jsonb,updated_at=NOW() WHERE id=%s",
                (matched_category_id, price, payload, existing["id"]),
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
                    matched_category_id,
                    name,
                    str(profile.get("description") or "")[:1000],
                    price,
                    payload,
                ),
            )
        count += 1
    return count

def persist_ready_application(db, uid: int, profile: dict[str, Any]) -> dict[str, Any]:
    """Create the single editable universal partner application draft.

    IMPORTANT: completion of the AI conversation no longer writes directions,
    subcategories or services directly into the live catalogue. The partner
    row may be created/updated as a pending shell, while the full business,
    location, contact, AI direction/subcategory and service proposal are kept
    in partner_applications for admin review.
    """
    # The form, not the chat, is responsible for collecting missing fields.
    # Create a draft even when the initial free-form message is incomplete.
    partner_id = _ensure_partner(db, uid, profile)
    services = [x for x in (profile.get("services") or []) if isinstance(x, dict)]
    first = services[0] if services else {}
    from partner_business_application_api import default_business, ensure_business_application_schema
    ensure_business_application_schema()

    existing_business = default_business(partner_id)
    business_action = str(profile.get("business_action") or "same_business").strip().lower()
    is_new_business = business_action in {"new_business","new business","new-business"}
    business_id = None if is_new_business else (existing_business["id"] if existing_business and existing_business.get("status") == "active" else None)

    # AI classification is informational at application time. Admin can edit
    # master/category IDs before activation; do not materialise them yet.
    master_id = None
    category_id = None
    try:
        master_id, category_ids = _direction_match(db, profile)
        category_id = _safe_int(category_ids[0]) if category_ids else None
    except Exception:
        logger.exception("Application catalogue classification failed; keeping proposal editable")

    payload = dict(profile)
    payload["services"] = services
    payload["ai_master_category_id"] = master_id
    payload["ai_category_id"] = category_id
    payload["application_version"] = 1

    proposed_business_name = str(profile.get("proposed_business_name") or "").strip()[:200] or None
    direction_name = str(
        profile.get("direction")
        or profile.get("master_category_name")
        or ""
    ).strip()[:300] or None
    subcategory_name = str(
        profile.get("subcategory")
        or profile.get("subcategory_name")
        or (first.get("subcategory_name") or "")
    ).strip()[:300] or None
    service_name = str(first.get("name") or "").strip()[:300] or None
    price = first.get("price")
    try:
        price = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        price = None

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO partner_applications(
                    partner_id,business_id,status,business_name,
                    location_marz,location_city,location_village,address,phone,
                    direction_name,master_category_id,subcategory_name,category_id,
                    service_name,price,description,object_name,ai_reason,payload_json
                )
                VALUES(
                    %s,%s,'pending_partner',%s,
                    %s,%s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s::jsonb
                )
                RETURNING id
                """,
                (
                    partner_id,
                    business_id,
                    (proposed_business_name if is_new_business else str(profile.get("business_name") or "").strip()[:200]),
                    str(profile.get("marz") or profile.get("region") or "").strip()[:200] or None,
                    str(profile.get("city") or "").strip()[:200] or None,
                    str(profile.get("village") or "").strip()[:200] or None,
                    str(profile.get("address") or "").strip()[:500] or None,
                    str(profile.get("phone") or "").strip()[:80] or None,
                    direction_name,
                    master_id,
                    subcategory_name,
                    category_id,
                    service_name,
                    price,
                    str(profile.get("description") or "").strip()[:5000] or None,
                    str(profile.get("object_name") or profile.get("object") or "").strip()[:300] or None,
                    "AI classified the partner message; admin can edit all catalogue fields before activation.",
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            row = cur.fetchone()
        conn.commit()

    return {
        "partner_id": partner_id,
        "application_id": row["id"] if row else None,
        "business_id": business_id,
        "new_business": is_new_business,
        "direction_id": None,
        "mapped_to_catalog": bool(master_id),
        "proposal_created": True,
        "service_count": len(services),
        "status": "pending_partner",
        "open_form": True,
    }



def create_partner_application_draft(db, uid: int, profile: dict[str, Any]) -> dict[str, Any]:
    """Create/update the one initial partner application draft."""
    partner_id = _ensure_partner(db, uid, profile)
    services = [dict(x) for x in (profile.get("services") or []) if isinstance(x, dict)]
    master_id = None
    category_id = None
    try:
        master_id, category_ids = _direction_match(db, profile)
        # Keep the AI-selected real master direction even when a particular
        # service has no subcategory match yet; the partner can correct it in
        # the full form.
        if not master_id:
            master_id = _safe_int(profile.get("master_category_id"))
        category_id = _safe_int(category_ids[0]) if category_ids else None
    except Exception:
        master_id = _safe_int(profile.get("master_category_id"))
        category_id = None
        logger.exception("Draft catalogue classification failed")
    payload = dict(profile)
    payload["services"] = services
    payload["ai_master_category_id"] = master_id
    payload["ai_category_id"] = category_id
    payload["application_version"] = 1
    business_action = str(profile.get("business_action") or "same_business").strip().lower()
    existing = None
    if business_action not in {"new_business","new business","new-business"}:
        from partner_business_application_api import default_business
        existing = default_business(partner_id)
    business_id = existing["id"] if existing and existing.get("status") == "active" else None
    first = services[0] if services else {}
    bname = str(profile.get("proposed_business_name") or "").strip()[:200] if business_action in {"new_business","new business","new-business"} else str(profile.get("business_name") or "").strip()[:200]
    values = (
        business_id,bname,
        str(profile.get("marz") or profile.get("region") or "").strip()[:200] or None,
        str(profile.get("city") or "").strip()[:200] or None,
        str(profile.get("village") or "").strip()[:200] or None,
        str(profile.get("address") or "").strip()[:500] or None,
        str(profile.get("phone") or "").strip()[:80] or None,
        str(profile.get("direction") or "").strip()[:300] or None,master_id,
        str(first.get("subcategory_name") or "").strip()[:300] or None,category_id,
        str(first.get("name") or "").strip()[:300] or None,first.get("price"),
        str(profile.get("description") or "").strip()[:5000] or None,
        str(profile.get("object_name") or profile.get("object") or "").strip()[:300] or None,
        json.dumps(payload,ensure_ascii=False)
    )
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM partner_applications WHERE partner_id=%s AND status IN ('pending_partner','sent_back') ORDER BY id DESC LIMIT 1",(partner_id,))
            old=cur.fetchone()
            if old:
                cur.execute("""UPDATE partner_applications SET business_id=%s,business_name=%s,
                    location_marz=%s,location_city=%s,location_village=%s,address=%s,phone=%s,
                    direction_name=%s,master_category_id=%s,subcategory_name=%s,category_id=%s,
                    service_name=%s,price=%s,description=%s,object_name=%s,payload_json=%s::jsonb,
                    updated_at=NOW() WHERE id=%s RETURNING id""",(*values,old["id"]))
                aid=cur.fetchone()["id"]
            else:
                cur.execute("""INSERT INTO partner_applications(
                    partner_id,business_id,status,business_name,location_marz,location_city,
                    location_village,address,phone,direction_name,master_category_id,
                    subcategory_name,category_id,service_name,price,description,object_name,
                    ai_reason,payload_json)
                    VALUES(%s,%s,'pending_partner',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    RETURNING id""",(partner_id,*values,
                    "AI draft; partner must complete and submit the full application."))
                aid=cur.fetchone()["id"]
        conn.commit()
    return {"partner_id":partner_id,"application_id":aid,"status":"pending_partner","open_form":True,"profile":payload}
