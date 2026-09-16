"""Partner cabinet API for the clean Armenia AI Guide architecture.

Uses the new platform tables and does not depend on legacy orders/bids.
"""
from __future__ import annotations

import json
import os
import secrets
from decimal import Decimal
from aiohttp import web

from platform_db import (
    get_partner_by_user,
    update_partner,
    rows,
    one,
    execute,
    approved_partner_categories,
    list_employees,
    create_employee,
    update_employee,
    delete_employee,
)
from telegram_webapp_auth import (
    validate_telegram_webapp_init_data,
    TelegramWebAppAuthError,
)
from booking_schema import ensure_booking_schema
from notify import notify


def _json_response(data, status=200):
    return web.json_response(data, status=status)


def _error(code, status=400):
    return _json_response({"ok": False, "error": code}, status=status)


def _uid(request):
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": "telegram_init_data_required"}),
            content_type="application/json",
        )
    try:
        data = validate_telegram_webapp_init_data(
            raw,
            request.app.get("stage3_bot_token") or os.getenv("BOT_TOKEN", ""),
        )
        return int(data["id"])
    except (TelegramWebAppAuthError, ValueError, TypeError, KeyError) as exc:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": str(exc)}),
            content_type="application/json",
        )


def _partner(request):
    uid = _uid(request)
    partner = get_partner_by_user(uid)
    if not partner:
        raise web.HTTPForbidden(
            text=json.dumps({"ok": False, "error": "partner_registration_required"}),
            content_type="application/json",
        )
    if partner.get("status") in ("blocked", "suspended", "deleted"):
        raise web.HTTPForbidden(
            text=json.dumps({"ok": False, "error": "partner_blocked"}),
            content_type="application/json",
        )
    return uid, partner


def _json_value(value):
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            return json.dumps({"value": value}, ensure_ascii=False)
    return json.dumps(value or {}, ensure_ascii=False)


def _num(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _partner_owns_category(partner_id, category_id):
    """True if category_id belongs to one of the partner's APPROVED directions."""
    return bool(one(
        """
        SELECT c.id
        FROM categories c
        JOIN partner_direction_categories pdc ON pdc.category_id=c.id
        JOIN partner_directions pd ON pd.id=pdc.partner_direction_id
         AND pd.status='approved'
        WHERE c.id=%s AND pd.partner_id=%s
        LIMIT 1
        """,
        (category_id, partner_id),
    ))


async def dashboard(request):
    uid, partner = _partner(request)
    directions = rows(
        """
        SELECT pd.id, pd.master_category_id, pd.status, pd.rejection_reason,
               m.name_am, m.name_ru,
               COUNT(DISTINCT pdc.category_id) AS category_count,
               COUNT(DISTINCT d.id) AS document_count
        FROM partner_directions pd
        JOIN master_categories m ON m.id = pd.master_category_id
        LEFT JOIN partner_direction_categories pdc
               ON pdc.partner_direction_id = pd.id
        LEFT JOIN partner_verification_documents d
               ON d.partner_direction_id = pd.id
        WHERE pd.partner_id = %s AND pd.status <> 'deleted'
        GROUP BY pd.id, m.id
        ORDER BY pd.id
        """,
        (partner["id"],),
    )

    service_count = one(
        "SELECT COUNT(*) AS n FROM services WHERE partner_id=%s AND status<>'deleted'",
        (partner["id"],),
    )["n"]

    object_count = one(
        "SELECT COUNT(*) AS n FROM partner_objects WHERE partner_id=%s",
        (partner["id"],),
    )["n"]

    booking_count = one(
        """
        SELECT COUNT(*) AS n
        FROM bookings
        WHERE partner_id=%s AND status NOT IN ('cancelled','expired')
        """,
        (partner["id"],),
    )["n"]

    return _json_response({
        "ok": True,
        "partner": partner,
        "directions": directions,
        "service_count": service_count,
        "object_count": object_count,
        "booking_count": booking_count,
    })


async def categories(request):
    uid, partner = _partner(request)
    return _json_response({
        "ok": True,
        "items": approved_partner_categories(partner["id"]),
    })


async def service_categories(request):
    uid, partner = _partner(request)
    return _json_response({
        "ok": True,
        "items": rows(
            """
            SELECT id, master_category_id, name_am, name_ru, slug
            FROM categories
            WHERE is_active=TRUE
            ORDER BY master_category_id, id
            """
        ),
    })


async def services(request):
    uid, partner = _partner(request)
    return _json_response({
        "ok": True,
        "items": rows(
            """
            SELECT s.*,
                   c.name_am AS category_name_am,
                   c.name_ru AS category_name_ru
            FROM services s
            LEFT JOIN categories c ON c.id=s.category_id
            WHERE s.partner_id=%s AND s.status<>'deleted'
            ORDER BY s.created_at DESC
            """,
            (partner["id"],),
        ),
    })


async def create_service(request):
    uid, partner = _partner(request)
    data = await request.json()
    category_id = int(data.get("category_id") or 0)
    subcategory_id = data.get("subcategory_id")
    if not category_id:
        return _error("category_id_required")

    allowed = one(
        """
        SELECT c.id
        FROM categories c
        JOIN partner_direction_categories pdc
          ON pdc.category_id=c.id
        JOIN partner_directions pd
          ON pd.id=pdc.partner_direction_id
         AND pd.status='approved'
        WHERE c.id=%s AND pd.partner_id=%s
        LIMIT 1
        """,
        (category_id, partner["id"]),
    )
    if not allowed:
        return _error("direction_not_approved", 403)

    service = execute(
        """
        INSERT INTO services(
            partner_id, category_id, subcategory_id, name, description,
            price, currency, duration_minutes, status, data_json
        )
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s::jsonb)
        RETURNING *
        """,
        (
            partner["id"],
            category_id,
            int(subcategory_id) if subcategory_id else None,
            str(data.get("name") or "Նոր ծառայություն"),
            str(data.get("description") or ""),
            data.get("price"),
            str(data.get("currency") or "AMD"),
            data.get("duration_minutes"),
            _json_value(data.get("data_json")),
        ),
        True,
    )
    return _json_response({"ok": True, "service": service})


async def service_details(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    service = one(
        "SELECT * FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    )
    if not service:
        return _error("service_not_found", 404)

    return _json_response({
        "ok": True,
        "service": service,
        "packages": rows(
            "SELECT * FROM service_packages WHERE service_id=%s ORDER BY id",
            (service_id,),
        ),
        "options": rows(
            "SELECT * FROM service_options WHERE service_id=%s ORDER BY id",
            (service_id,),
        ),
        "schedule": rows(
            "SELECT * FROM service_schedule WHERE service_id=%s ORDER BY weekday",
            (service_id,),
        ),
    })


async def add_package(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    data = await request.json()
    if not one(
        "SELECT id FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    ):
        return _error("service_not_found", 404)

    item = execute(
        """
        INSERT INTO service_packages(service_id,name,price,currency,description)
        VALUES(%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (
            service_id,
            str(data.get("name") or ""),
            data.get("price", 0),
            data.get("currency", "AMD"),
            str(data.get("description") or ""),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def add_option(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    data = await request.json()
    if not one(
        "SELECT id FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    ):
        return _error("service_not_found", 404)

    item = execute(
        """
        INSERT INTO service_options(service_id,name,price_delta,currency)
        VALUES(%s,%s,%s,%s)
        RETURNING *
        """,
        (
            service_id,
            str(data.get("name") or ""),
            data.get("price_delta", 0),
            data.get("currency", "AMD"),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def schedule(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    data = await request.json()
    if not one(
        "SELECT id FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    ):
        return _error("service_not_found", 404)

    item = execute(
        """
        INSERT INTO service_schedule(
            service_id,weekday,start_time,end_time,is_available
        )
        VALUES(%s,%s,%s,%s,%s)
        ON CONFLICT(service_id,weekday)
        DO UPDATE SET
            start_time=EXCLUDED.start_time,
            end_time=EXCLUDED.end_time,
            is_available=EXCLUDED.is_available
        RETURNING *
        """,
        (
            service_id,
            int(data.get("weekday", 0)),
            data.get("start_time") or None,
            data.get("end_time") or None,
            bool(data.get("is_available", True)),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def update_service(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    data = await request.json()
    current = one(
        "SELECT * FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    )
    if not current:
        return _error("service_not_found", 404)
    if current["status"] == "deleted":
        return _error("service_deleted")

    # Resolve new field values (fall back to current values).
    new_category = current["category_id"]
    if "category_id" in data and data.get("category_id"):
        new_category = int(data["category_id"])
        if new_category != current["category_id"] and not _partner_owns_category(
            partner["id"], new_category
        ):
            return _error("direction_not_approved", 403)

    new_name = str(data["name"]) if "name" in data else current["name"]
    new_desc = str(data["description"]) if "description" in data else current["description"]
    new_price = data["price"] if "price" in data else current["price"]
    new_currency = str(data["currency"]) if "currency" in data else current["currency"]
    new_duration = (
        data["duration_minutes"] if "duration_minutes" in data else current["duration_minutes"]
    )
    new_subcat = (
        int(data["subcategory_id"]) if data.get("subcategory_id") else current["subcategory_id"]
    )
    new_data_json = (
        _json_value(data["data_json"]) if "data_json" in data else None
    )

    # A material change to an already-approved service sends it back to
    # moderation (marketplace re-review rule).
    core_changed = (
        new_name != current["name"]
        or new_desc != current["description"]
        or _num(new_price) != _num(current["price"])
        or new_category != current["category_id"]
    )
    new_status = current["status"]
    if current["status"] == "approved" and core_changed:
        new_status = "pending"

    item = execute(
        """
        UPDATE services
        SET category_id=%s, subcategory_id=%s, name=%s, description=%s,
            price=%s, currency=%s, duration_minutes=%s, status=%s,
            data_json=COALESCE(%s::jsonb, data_json), updated_at=NOW()
        WHERE id=%s AND partner_id=%s
        RETURNING *
        """,
        (
            new_category, new_subcat, new_name, new_desc, new_price,
            new_currency, new_duration, new_status, new_data_json,
            service_id, partner["id"],
        ),
        True,
    )
    return _json_response({"ok": True, "service": item})


_SERVICE_STATUS_TRANSITIONS = {
    "pending": {"draft", "rejected"},
    "frozen": {"approved"},
    "approved": {"frozen"},
    "deleted": {"draft", "pending", "approved", "rejected", "frozen"},
}


async def service_status(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    data = await request.json()
    target = str(data.get("status") or "").strip()
    if target not in _SERVICE_STATUS_TRANSITIONS:
        return _error("invalid_status")
    current = one(
        "SELECT status FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    )
    if not current:
        return _error("service_not_found", 404)
    if current["status"] not in _SERVICE_STATUS_TRANSITIONS[target]:
        return _error("invalid_status_transition")
    item = execute(
        """
        UPDATE services SET status=%s, updated_at=NOW()
        WHERE id=%s AND partner_id=%s RETURNING *
        """,
        (target, service_id, partner["id"]),
        True,
    )
    return _json_response({"ok": True, "service": item})


async def service_delete(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    item = execute(
        """
        UPDATE services SET status='deleted', updated_at=NOW()
        WHERE id=%s AND partner_id=%s AND status<>'deleted' RETURNING id
        """,
        (service_id, partner["id"]),
        True,
    )
    if not item:
        return _error("service_not_found", 404)
    return _json_response({"ok": True, "deleted_id": item["id"]})


def _owns_service(partner_id, service_id):
    return bool(one(
        "SELECT id FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner_id),
    ))


async def package_delete(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    package_id = int(request.match_info["package_id"])
    if not _owns_service(partner["id"], service_id):
        return _error("service_not_found", 404)
    item = execute(
        """
        UPDATE service_packages SET is_active=FALSE
        WHERE id=%s AND service_id=%s RETURNING id
        """,
        (package_id, service_id),
        True,
    )
    if not item:
        return _error("package_not_found", 404)
    return _json_response({"ok": True, "deleted_id": item["id"]})


async def option_delete(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    option_id = int(request.match_info["option_id"])
    if not _owns_service(partner["id"], service_id):
        return _error("service_not_found", 404)
    item = execute(
        """
        UPDATE service_options SET is_active=FALSE
        WHERE id=%s AND service_id=%s RETURNING id
        """,
        (option_id, service_id),
        True,
    )
    if not item:
        return _error("option_not_found", 404)
    return _json_response({"ok": True, "deleted_id": item["id"]})


async def settings(request):
    uid, partner = _partner(request)
    if request.method == "GET":
        return _json_response({"ok": True, "partner": partner})

    data = await request.json()
    updated = update_partner(
        partner["id"],
        contact_share_policy=str(
            data.get("contact_share_policy")
            or partner.get("contact_share_policy")
            or "after_booking"
        ),
        profile_json=data.get(
            "profile_json",
            partner.get("profile_json") or {},
        ),
    )
    return _json_response({"ok": True, "partner": updated})


async def locations(request):
    uid, partner = _partner(request)
    if request.method == "GET":
        return _json_response({
            "ok": True,
            "items": rows(
                "SELECT * FROM partner_locations WHERE partner_id=%s ORDER BY id",
                (partner["id"],),
            ),
        })

    data = await request.json()
    item = execute(
        """
        INSERT INTO partner_locations(
            partner_id,marz,city,village,address,location_type,data_json
        )
        VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING *
        """,
        (
            partner["id"],
            data.get("marz"),
            data.get("city"),
            data.get("village"),
            data.get("address"),
            data.get("location_type", "fixed"),
            _json_value(data.get("data_json")),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def objects(request):
    uid, partner = _partner(request)

    if request.method == "GET":
        return _json_response({
            "ok": True,
            "items": rows(
                """
                SELECT *
                FROM partner_objects
                WHERE partner_id=%s
                ORDER BY id
                """,
                (partner["id"],),
            ),
        })

    data = await request.json()
    name = str(data.get("object_name") or data.get("name") or "").strip()
    if not name:
        return _error("object_name_required")

    item = execute(
        """
        INSERT INTO partner_objects(
            partner_id,object_name,address,city,marz,data_json
        )
        VALUES(%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING *
        """,
        (
            partner["id"],
            name,
            data.get("address"),
            data.get("city"),
            data.get("marz"),
            _json_value(data.get("data_json")),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def object_delete(request):
    uid, partner = _partner(request)
    object_id = int(request.match_info["object_id"])
    deleted = execute(
        """
        DELETE FROM partner_objects
        WHERE id=%s AND partner_id=%s
        RETURNING id
        """,
        (object_id, partner["id"]),
        True,
    )
    if not deleted:
        return _error("object_not_found", 404)
    return _json_response({"ok": True, "deleted_id": deleted["id"]})


async def object_update(request):
    uid, partner = _partner(request)
    object_id = int(request.match_info["object_id"])
    data = await request.json()
    current = one(
        "SELECT * FROM partner_objects WHERE id=%s AND partner_id=%s",
        (object_id, partner["id"]),
    )
    if not current:
        return _error("object_not_found", 404)

    name = current["object_name"]
    if "object_name" in data or "name" in data:
        name = str(data.get("object_name") or data.get("name") or "").strip()
        if not name:
            return _error("object_name_required")
    address = data["address"] if "address" in data else current["address"]
    city = data["city"] if "city" in data else current["city"]
    marz = data["marz"] if "marz" in data else current["marz"]
    new_data_json = _json_value(data["data_json"]) if "data_json" in data else None

    item = execute(
        """
        UPDATE partner_objects
        SET object_name=%s, address=%s, city=%s, marz=%s,
            data_json=COALESCE(%s::jsonb, data_json)
        WHERE id=%s AND partner_id=%s
        RETURNING *
        """,
        (name, address, city, marz, new_data_json, object_id, partner["id"]),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def employees(request):
    uid, partner = _partner(request)

    if request.method == "GET":
        return _json_response({
            "ok": True,
            "items": list_employees(partner["id"]),
        })

    data = await request.json()
    name = str(data.get("full_name") or data.get("name") or "").strip()
    if not name:
        return _error("full_name_required")
    item = create_employee(
        partner["id"],
        name,
        role_title=str(data.get("role_title") or "").strip(),
        phone=data.get("phone"),
        object_id=data.get("object_id"),
        skills=data.get("skills") or data.get("skills_json"),
    )
    return _json_response({"ok": True, "item": item})


async def employee_update(request):
    uid, partner = _partner(request)
    employee_id = int(request.match_info["employee_id"])
    data = await request.json()
    fields = {}
    for key in ("full_name", "role_title", "phone", "object_id", "is_active"):
        if key in data:
            fields[key] = data[key]
    if "skills" in data or "skills_json" in data:
        fields["skills_json"] = data.get("skills") or data.get("skills_json")
    item = update_employee(employee_id, partner["id"], **fields)
    if not item:
        return _error("employee_not_found", 404)
    return _json_response({"ok": True, "item": item})


async def employee_delete(request):
    uid, partner = _partner(request)
    employee_id = int(request.match_info["employee_id"])
    deleted = delete_employee(employee_id, partner["id"])
    if not deleted:
        return _error("employee_not_found", 404)
    return _json_response({"ok": True, "deleted_id": deleted["id"]})


async def bookings(request):
    uid, partner = _partner(request)
    items = rows(
        """
        SELECT b.*,
               u.full_name AS client_name,
               u.username AS client_username
        FROM bookings b
        LEFT JOIN users u ON u.telegram_id=b.client_id
        WHERE b.partner_id=%s
        ORDER BY b.updated_at DESC
        """,
        (partner["id"],),
    )
    return _json_response({"ok": True, "items": items})


async def booking_details(request):
    uid, partner = _partner(request)
    booking_id = int(request.match_info["booking_id"])
    booking = one(
        """
        SELECT b.*,
               u.full_name AS client_name,
               u.username AS client_username
        FROM bookings b
        LEFT JOIN users u ON u.telegram_id=b.client_id
        WHERE b.id=%s AND b.partner_id=%s
        """,
        (booking_id, partner["id"]),
    )
    if not booking:
        return _error("booking_not_found", 404)

    return _json_response({
        "ok": True,
        "booking": booking,
        "payments": rows(
            "SELECT * FROM payments WHERE booking_id=%s ORDER BY id DESC",
            (booking_id,),
        ),
        "checkin": one(
            "SELECT * FROM booking_checkins WHERE booking_id=%s",
            (booking_id,),
        ),
    })


async def booking_status(request):
    uid, partner = _partner(request)
    booking_id = int(request.match_info["booking_id"])
    data = await request.json()
    status = str(data.get("status") or "").strip()

    allowed = {
        "confirmed": {"pending", "paid"},
        "completed": {"confirmed", "checked_in"},
        "cancelled": {
            "pending", "awaiting_payment", "paid", "confirmed"
        },
    }
    current = one(
        "SELECT status FROM bookings WHERE id=%s AND partner_id=%s",
        (booking_id, partner["id"]),
    )
    if not current:
        return _error("booking_not_found", 404)

    if status not in allowed or current["status"] not in allowed[status]:
        return _error("invalid_status_transition")

    item = execute(
        """
        UPDATE bookings
        SET status=%s,updated_at=NOW()
        WHERE id=%s AND partner_id=%s
        RETURNING *
        """,
        (status, booking_id, partner["id"]),
        True,
    )

    if status == "cancelled":
        reason = str(data.get("reason") or "Отменено партнёром")
        execute(
            """
            INSERT INTO booking_cancellations(
                booking_id,cancelled_by,reason
            )
            VALUES(%s,%s,%s)
            """,
            (booking_id, "partner", reason),
        )

    return _json_response({"ok": True, "booking": item})


async def checkin(request):
    uid, partner = _partner(request)
    data = await request.json()
    booking_id = data.get("booking_id")
    token = str(data.get("token") or data.get("code") or "").strip()

    if booking_id:
        booking_id = int(booking_id)
        booking = one(
            "SELECT * FROM bookings WHERE id=%s AND partner_id=%s",
            (booking_id, partner["id"]),
        )
        if not booking:
            return _error("booking_not_found", 404)

        if booking["status"] not in ("paid", "confirmed", "checked_in"):
            return _error("booking_not_ready_for_checkin")

        existing = one(
            "SELECT * FROM booking_checkins WHERE booking_id=%s",
            (booking_id,),
        )
        if existing:
            return _json_response({"ok": True, "checkin": existing})

        new_token = secrets.token_urlsafe(32)
        item = execute(
            """
            INSERT INTO booking_checkins(booking_id,token)
            VALUES(%s,%s)
            RETURNING *
            """,
            (booking_id, new_token),
            True,
        )
        return _json_response({"ok": True, "checkin": item})

    if token:
        check = one(
            """
            SELECT bc.*,b.partner_id,b.status AS booking_status
            FROM booking_checkins bc
            JOIN bookings b ON b.id=bc.booking_id
            WHERE bc.token=%s AND b.partner_id=%s
            """,
            (token, partner["id"]),
        )
        if not check:
            return _error("checkin_token_not_found", 404)
        if check["status"] != "active":
            return _error("checkin_token_not_active")

        execute(
            """
            UPDATE booking_checkins
            SET status='used',checked_in_at=NOW(),checked_in_by=%s
            WHERE id=%s
            """,
            (uid, check["id"]),
        )
        booking = execute(
            """
            UPDATE bookings
            SET status='checked_in',updated_at=NOW()
            WHERE id=%s AND partner_id=%s
            RETURNING *
            """,
            (check["booking_id"], partner["id"]),
            True,
        )
        # Notify admin that the service was delivered, and the client too.
        biz = partner.get("business_name") or "partner"
        svc = (booking or {}).get("service_name") or "ործայություն"
        admin_id = request.app.get("stage3_admin_id") or 0
        if admin_id:
            await notify(
                request.app, int(admin_id),
                title="✅ Նշևահակում / Check-in",
                body=f"Գործընկեր «{biz}» հաստատեց ծառայության կատարումը (№{booking['id']}, {svc}).",
                kind="checkin", audience="admin",
                data={"booking_id": booking["id"], "partner_id": partner["id"]},
            )
        if booking and booking.get("client_id"):
            await notify(
                request.app, int(booking["client_id"]),
                title="✅ Ծառայությունը հաստատվեց",
                body=f"«{biz}»-ը հաստատեց ծառայության կատարումը (№{booking['id']}).",
                kind="checkin", audience="client",
                data={"booking_id": booking["id"]},
            )
        return _json_response({"ok": True, "booking": booking})

    return _error("booking_id_or_token_required")


async def history(request):
    uid, partner = _partner(request)
    return _json_response({
        "ok": True,
        "items": rows(
            """
            SELECT b.id,b.status,b.agreed_price,b.currency,
                   b.service_name,b.scheduled_at,b.updated_at
            FROM bookings b
            WHERE b.partner_id=%s
            ORDER BY b.updated_at DESC
            LIMIT 100
            """,
            (partner["id"],),
        ),
    })


async def finance(request):
    uid, partner = _partner(request)
    result = one(
        """
        SELECT
            COALESCE(SUM(agreed_price),0) AS gross,
            COALESCE(SUM(commission_amount),0) AS commission,
            COALESCE(SUM(partner_amount),0) AS partner_amount
        FROM bookings
        WHERE partner_id=%s AND status IN ('paid','confirmed','checked_in','completed')
        """,
        (partner["id"],),
    )
    # Payout aggregates: money already paid out and money awaiting processing.
    payout_stats = one(
        """
        SELECT
            COALESCE(SUM(amount) FILTER (WHERE status='paid'),0) AS paid_out,
            COALESCE(SUM(amount) FILTER (WHERE status IN ('requested','processing')),0)
                AS pending
        FROM partner_payouts
        WHERE partner_id=%s
        """,
        (partner["id"],),
    )
    earned = _num(result["partner_amount"])
    paid_out = _num(payout_stats["paid_out"])
    pending = _num(payout_stats["pending"])
    # Available for withdrawal = what the partner earned, minus what has
    # already been paid out and what is currently reserved by open requests.
    balance = round(earned - paid_out - pending, 2)

    ledger = rows(
        """
        SELECT *
        FROM partner_financial_ledger
        WHERE partner_id=%s
        ORDER BY created_at DESC
        LIMIT 100
        """,
        (partner["id"],),
    )
    payouts = rows(
        """
        SELECT *
        FROM partner_payouts
        WHERE partner_id=%s
        ORDER BY requested_at DESC
        LIMIT 100
        """,
        (partner["id"],),
    )
    return _json_response({
        "ok": True,
        "gross": _num(result["gross"]),
        "commission": _num(result["commission"]),
        "partner_amount": earned,
        "paid_out": paid_out,
        "pending": pending,
        "balance": balance,
        "currency": partner.get("currency") or "AMD",
        "ledger": ledger,
        "payouts": payouts,
    })


async def request_payout(request):
    uid, partner = _partner(request)
    data = await request.json()

    # Recompute the available balance server-side (never trust the client).
    result = one(
        """
        SELECT COALESCE(SUM(partner_amount),0) AS partner_amount
        FROM bookings
        WHERE partner_id=%s AND status IN ('paid','confirmed','checked_in','completed')
        """,
        (partner["id"],),
    )
    payout_stats = one(
        """
        SELECT COALESCE(SUM(amount) FILTER (WHERE status IN ('requested','processing','paid')),0)
               AS committed
        FROM partner_payouts
        WHERE partner_id=%s
        """,
        (partner["id"],),
    )
    balance = round(_num(result["partner_amount"]) - _num(payout_stats["committed"]), 2)

    requested = data.get("amount")
    amount = _num(requested) if requested is not None else balance
    if amount <= 0:
        return _error("invalid_amount")
    if amount > balance + 1e-6:
        return _error("insufficient_balance")

    currency = str(data.get("currency") or partner.get("currency") or "AMD")
    payout = execute(
        """
        INSERT INTO partner_payouts(
            partner_id, amount, currency, status, method, destination, note, data_json
        )
        VALUES(%s,%s,%s,'requested',%s,%s,%s,%s::jsonb)
        RETURNING *
        """,
        (
            partner["id"], amount, currency,
            str(data.get("method") or "idram"),
            str(data.get("destination") or ""),
            str(data.get("note") or ""),
            _json_value(data.get("data_json")),
        ),
        True,
    )
    # Mirror the request in the ledger for a full audit trail.
    execute(
        """
        INSERT INTO partner_financial_ledger(
            partner_id, entry_type, amount, currency, description
        )
        VALUES(%s,'payout_request',%s,%s,%s)
        """,
        (partner["id"], -amount, currency, f"Запрос вывода №{payout['id']}"),
    )
    admin_id = request.app.get("stage3_admin_id") or 0
    if admin_id:
        biz = partner.get("business_name") or "partner"
        await notify(
            request.app, int(admin_id),
            title="💸 Запрос на вывод средств",
            body=f"Партнёр «{biz}» запросил вывод {amount:.0f} {currency} (№{payout['id']}).",
            kind="payout", audience="admin",
            data={"payout_id": payout["id"], "partner_id": partner["id"]},
        )
    return _json_response({"ok": True, "payout": payout})


# --- Partner notification inbox --------------------------------------------
# Notifications are keyed by the recipient's Telegram id (users.telegram_id),
# which for a partner equals partners.user_id == the authenticated uid. We scope
# the cabinet inbox to partner-facing audiences so a user's client-side
# notifications never leak into the partner inbox.
_PARTNER_AUDIENCES = ("partner", "user")


def _unread_count(uid):
    row = one(
        "SELECT COUNT(*) AS c FROM notifications "
        "WHERE user_id=%s AND audience=ANY(%s) AND is_read=FALSE",
        (uid, list(_PARTNER_AUDIENCES)),
    )
    return int((row or {}).get("c") or 0)


async def notifications_list(request):
    uid, partner = _partner(request)
    unread_only = str(request.query.get("unread_only", "")).lower() in (
        "1", "true", "yes", "on",
    )
    if unread_only:
        items = rows(
            "SELECT * FROM notifications "
            "WHERE user_id=%s AND audience=ANY(%s) AND is_read=FALSE "
            "ORDER BY created_at DESC LIMIT 50",
            (uid, list(_PARTNER_AUDIENCES)),
        )
    else:
        items = rows(
            "SELECT * FROM notifications "
            "WHERE user_id=%s AND audience=ANY(%s) "
            "ORDER BY created_at DESC LIMIT 50",
            (uid, list(_PARTNER_AUDIENCES)),
        )
    return _json_response({
        "ok": True,
        "unread_count": _unread_count(uid),
        "notifications": items,
    })


async def notifications_read(request):
    uid, partner = _partner(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids") if isinstance(body, dict) else None
    if isinstance(ids, list):
        ids = [int(x) for x in ids if str(x).strip().lstrip("-").isdigit()]
        if ids:
            execute(
                "UPDATE notifications SET is_read=TRUE "
                "WHERE user_id=%s AND id=ANY(%s)",
                (uid, ids),
            )
    else:
        # Mark every partner-facing notification as read.
        execute(
            "UPDATE notifications SET is_read=TRUE "
            "WHERE user_id=%s AND audience=ANY(%s)",
            (uid, list(_PARTNER_AUDIENCES)),
        )
    return _json_response({"ok": True, "unread_count": _unread_count(uid)})


def register_master_cabinet_routes(app, db, bot=None):
    # Extend the clean schema once at startup. Safe because every statement
    # uses IF NOT EXISTS.
    ensure_booking_schema()
    if bot is not None and app.get("bot") is None:
        app["bot"] = bot

    app.router.add_get("/api/master/{id}/dashboard", dashboard)
    app.router.add_get("/api/master/{id}/categories", categories)
    app.router.add_get("/api/master/{id}/service-categories", service_categories)

    app.router.add_get("/api/master/{id}/services", services)
    app.router.add_post("/api/master/{id}/services", create_service)
    app.router.add_get(
        "/api/master/{id}/services/{service_id}/details",
        service_details,
    )
    app.router.add_post(
        "/api/master/{id}/services/{service_id}/packages",
        add_package,
    )
    app.router.add_post(
        "/api/master/{id}/services/{service_id}/options",
        add_option,
    )
    app.router.add_post(
        "/api/master/{id}/services/{service_id}/schedule",
        schedule,
    )
    app.router.add_patch(
        "/api/master/{id}/services/{service_id}",
        update_service,
    )
    app.router.add_post(
        "/api/master/{id}/services/{service_id}",
        update_service,
    )
    app.router.add_post(
        "/api/master/{id}/services/{service_id}/status",
        service_status,
    )
    app.router.add_delete(
        "/api/master/{id}/services/{service_id}",
        service_delete,
    )
    app.router.add_delete(
        "/api/master/{id}/services/{service_id}/packages/{package_id}",
        package_delete,
    )
    app.router.add_delete(
        "/api/master/{id}/services/{service_id}/options/{option_id}",
        option_delete,
    )

    app.router.add_route("*", "/api/master/{id}/settings", settings)
    app.router.add_route("*", "/api/master/{id}/locations", locations)

    app.router.add_get("/api/master/{id}/objects", objects)
    app.router.add_post("/api/master/{id}/objects", objects)
    app.router.add_delete(
        "/api/master/{id}/objects/{object_id}",
        object_delete,
    )
    app.router.add_patch(
        "/api/master/{id}/objects/{object_id}",
        object_update,
    )
    app.router.add_post(
        "/api/master/{id}/objects/{object_id}",
        object_update,
    )

    app.router.add_get("/api/master/{id}/employees", employees)
    app.router.add_post("/api/master/{id}/employees", employees)
    app.router.add_patch(
        "/api/master/{id}/employees/{employee_id}",
        employee_update,
    )
    app.router.add_post(
        "/api/master/{id}/employees/{employee_id}",
        employee_update,
    )
    app.router.add_delete(
        "/api/master/{id}/employees/{employee_id}",
        employee_delete,
    )

    app.router.add_get("/api/master/{id}/bookings", bookings)
    app.router.add_get(
        "/api/master/{id}/bookings/{booking_id}",
        booking_details,
    )
    app.router.add_post(
        "/api/master/{id}/bookings/{booking_id}/status",
        booking_status,
    )
    app.router.add_post("/api/master/{id}/checkin", checkin)

    app.router.add_get("/api/master/{id}/history", history)
    app.router.add_get("/api/master/{id}/finance", finance)
    app.router.add_post("/api/master/{id}/finance/payout", request_payout)

    app.router.add_get("/api/master/{id}/notifications", notifications_list)
    app.router.add_post("/api/master/{id}/notifications/read", notifications_read)
