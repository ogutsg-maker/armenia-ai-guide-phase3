"""Phase 3 — premium (paid) contact disclosure.

All database access is routed through Data Core. This module owns only the
HTTP/API policy and Idram orchestration.
"""
from __future__ import annotations

import json

from aiohttp import web

import data_core
import features
from idram import IdramProvider


def _uid(request):
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": "telegram_init_data_required"}),
            content_type="application/json",
        )
    from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError
    import os
    try:
        data = validate_telegram_webapp_init_data(
            raw, request.app.get("stage3_bot_token") or os.getenv("BOT_TOKEN", "")
        )
        return int(data["id"])
    except (TelegramWebAppAuthError, ValueError, TypeError, KeyError) as exc:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": str(exc)}),
            content_type="application/json",
        )


def _profile(partner: dict) -> dict:
    p = partner.get("profile_json") or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = {}
    return p if isinstance(p, dict) else {}


def _scope_contact(profile: dict, scope: str) -> dict:
    keys = ("phone",) if scope == "limited" else ("phone", "website", "telegram")
    return {k: profile.get(k) for k in keys if profile.get(k)}


def _load_partner_for_service(service_id: int):
    return data_core.one(
        """SELECT p.id,p.business_name,p.contact_share_policy,p.contact_sharing_enabled,
                  p.premium_contact_sharing_enabled,p.profile_json,
                  s.id AS service_id,s.name AS service_name
           FROM services s
           JOIN partners p ON p.id=s.partner_id
           WHERE s.id=%s AND s.status='approved' AND p.status='approved'""",
        (int(service_id),),
    )


def _fee_for(partner: dict, profile: dict) -> float:
    try:
        fee = float(profile.get("premium_contact_fee") or 0)
    except (TypeError, ValueError):
        fee = 0.0
    cap = float(features.get("premium_contact_max_fee", 100000) or 0)
    if cap > 0:
        fee = min(fee, cap)
    return max(0.0, fee)


async def contact_quote(request):
    if not features.is_enabled("premium_contact"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)
    _uid(request)
    partner = _load_partner_for_service(int(request.match_info["service_id"]))
    if not partner:
        return web.json_response({"ok": False, "error": "service_not_available"}, status=404)
    profile = _profile(partner)
    policy = partner.get("contact_share_policy") or "after_booking"
    fee = _fee_for(partner, profile)
    return web.json_response({
        "ok": True,
        "policy": policy,
        "fee": fee if policy == "premium" else 0,
        "currency": profile.get("currency") or "AMD",
        "scope": profile.get("premium_disclosure_scope", "limited"),
        "premium_enabled": bool(partner.get("premium_contact_sharing_enabled", True)),
    })


async def contact_unlock(request):
    if not features.is_enabled("premium_contact"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)

    uid = _uid(request)
    service_id = int(request.match_info["service_id"])
    partner = _load_partner_for_service(service_id)
    if not partner:
        return web.json_response({"ok": False, "error": "service_not_available"}, status=404)

    partner_id = int(partner["id"])
    profile = _profile(partner)
    policy = partner.get("contact_share_policy") or "after_booking"
    scope = profile.get("premium_disclosure_scope", "limited")

    if policy == "never" or not partner.get("contact_sharing_enabled", True):
        return web.json_response({"ok": False, "error": "contact_not_shareable"}, status=403)

    existing = data_core.one(
        """SELECT * FROM contact_disclosures
           WHERE client_id=%s AND partner_id=%s AND status='disclosed'
           ORDER BY created_at DESC LIMIT 1""",
        (uid, partner_id),
    )
    if existing:
        return web.json_response({
            "ok": True,
            "already_unlocked": True,
            "contact": _scope_contact(profile, scope),
            "disclosure_id": existing["id"],
        })

    if policy == "after_booking":
        paid = data_core.one(
            """SELECT id FROM bookings
               WHERE client_id=%s AND partner_id=%s
                 AND status IN ('paid','booked','completed')
               LIMIT 1""",
            (uid, partner_id),
        )
        if not paid:
            return web.json_response({"ok": False, "error": "booking_required"}, status=402)

        disclosure = data_core.execute(
            """INSERT INTO contact_disclosures
               (client_id,partner_id,status,fee_amount,disclosure_scope,data_json)
               VALUES(%s,%s,'disclosed',0,%s,%s::jsonb) RETURNING *""",
            (uid, partner_id, scope,
             data_core.json_dump({"reason": "after_booking", "service_id": service_id})),
            True,
        )
        return web.json_response({
            "ok": True, "paid": False,
            "contact": _scope_contact(profile, scope),
            "disclosure_id": disclosure["id"] if disclosure else None,
        })

    if not partner.get("premium_contact_sharing_enabled", True):
        return web.json_response({"ok": False, "error": "premium_disabled"}, status=403)

    fee = _fee_for(partner, profile)
    currency = profile.get("currency") or "AMD"
    if fee <= 0:
        disclosure = data_core.execute(
            """INSERT INTO contact_disclosures
               (client_id,partner_id,status,fee_amount,disclosure_scope,data_json)
               VALUES(%s,%s,'disclosed',0,%s,%s::jsonb) RETURNING *""",
            (uid, partner_id, scope,
             data_core.json_dump({"reason": "premium_zero_fee", "service_id": service_id})),
            True,
        )
        return web.json_response({
            "ok": True, "paid": False,
            "contact": _scope_contact(profile, scope),
            "disclosure_id": disclosure["id"] if disclosure else None,
        })

    idram = IdramProvider()
    intent = idram.create_invoice(
        amount=fee,
        currency=currency,
        description=f"Premium contact: {partner['business_name']}",
        order_id=f"contact-{partner_id}-{uid}",
        metadata={"partner_id": partner_id, "client_id": uid, "service_id": service_id},
    )

    payment = data_core.execute(
        """INSERT INTO payments
           (client_id,partner_id,payment_type,status,amount,currency,
            provider,provider_payment_id,data_json)
           VALUES(%s,%s,'premium_contact',%s,%s,%s,%s,%s,%s::jsonb)
           RETURNING *""",
        (
            uid, partner_id, intent.status, fee, currency, intent.provider,
            intent.transaction_id,
            data_core.json_dump({
                "mode": intent.mode,
                "bill_no": intent.bill_no,
                "payment_url": intent.payment_url,
            }),
        ),
        True,
    )

    if intent.status != "paid":
        data_core.execute(
            """INSERT INTO contact_disclosures
               (client_id,partner_id,payment_id,status,fee_amount,disclosure_scope,data_json)
               VALUES(%s,%s,%s,'pending',%s,%s,%s::jsonb)""",
            (
                uid, partner_id, payment["id"] if payment else None, fee, scope,
                data_core.json_dump({"payment_url": intent.payment_url}),
            ),
        )
        return web.json_response({
            "ok": True, "paid": False, "pending_payment": True,
            "payment_url": intent.payment_url,
            "payment_id": payment["id"] if payment else None,
        })

    disclosure = data_core.execute(
        """INSERT INTO contact_disclosures
           (client_id,partner_id,payment_id,status,fee_amount,disclosure_scope,data_json)
           VALUES(%s,%s,%s,'disclosed',%s,%s,%s::jsonb) RETURNING *""",
        (
            uid, partner_id, payment["id"] if payment else None, fee, scope,
            data_core.json_dump({
                "transaction": intent.transaction_id,
                "service_id": service_id,
            }),
        ),
        True,
    )
    data_core.execute(
        """INSERT INTO partner_financial_ledger
           (partner_id,entry_type,amount,currency,description)
           VALUES(%s,'premium_contact',%s,%s,%s)""",
        (partner_id, fee, currency, "Premium contact disclosure fee"),
    )

    try:
        from notify import notify
        owner = data_core.get_partner(partner_id)
        if owner and owner.get("user_id"):
            await notify(
                request.app,
                int(owner["user_id"]),
                title="🔓 Контакт разблокирован",
                body=f"Клиент оплатил доступ к вашим контактам ({fee:.0f} {currency}).",
                kind="premium_contact",
                audience="partner",
                data={"client_id": uid, "service_id": service_id},
            )
    except Exception:
        pass

    return web.json_response({
        "ok": True, "paid": True,
        "contact": _scope_contact(profile, scope),
        "disclosure_id": disclosure["id"] if disclosure else None,
        "transaction": intent.transaction_id,
    })


def register_premium_contact_routes(app):
    app.router.add_get(
        "/api/market/client/service/{service_id}/contact/quote", contact_quote
    )
    app.router.add_post(
        "/api/market/client/service/{service_id}/contact/unlock", contact_unlock
    )
