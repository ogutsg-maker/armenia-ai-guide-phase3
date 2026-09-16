"""Phase 3 — premium (paid) contact disclosure.

A partner can set ``contact_share_policy``:
  * ``after_booking`` — contact is free once the client has a paid booking;
  * ``premium``       — contact is unlocked by paying ``premium_contact_fee``
                        (stored in ``partners.profile_json``) via Idram;
  * ``never``         — contact is never shared through the platform.

Disclosures are recorded in ``contact_disclosures`` and are idempotent: a
client who already unlocked a partner's contact never pays twice.
"""
from __future__ import annotations

import json

from aiohttp import web

import features
from idram import IdramProvider
# Reuse the marketplace DB/auth helpers so behaviour stays identical.
from marketplace_flow_api import _one, _exec, _uid, _json


def _profile(partner: dict) -> dict:
    p = partner.get("profile_json") or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = {}
    return p if isinstance(p, dict) else {}


def _scope_contact(profile: dict, scope: str) -> dict:
    """Return the contact fields allowed by *scope*."""
    keys = ("phone",) if scope == "limited" else ("phone", "website", "telegram")
    return {k: profile.get(k) for k in keys if profile.get(k)}


def _load_partner_for_service(service_id: int):
    return _one(
        "SELECT p.id,p.business_name,p.contact_share_policy,p.contact_sharing_enabled,"
        "p.premium_contact_sharing_enabled,p.profile_json,s.id service_id,s.name service_name "
        "FROM services s JOIN partners p ON p.id=s.partner_id "
        "WHERE s.id=%s AND s.status='approved' AND p.status='approved'",
        (service_id,),
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
    """Tell the client how (and for how much) they can get the contact."""
    if not features.is_enabled("premium_contact"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)
    _uid(request)
    service_id = int(request.match_info["service_id"])
    partner = _load_partner_for_service(service_id)
    if not partner:
        return web.json_response({"ok": False, "error": "service_not_available"}, status=404)
    profile = _profile(partner)
    policy = partner.get("contact_share_policy") or "after_booking"
    fee = _fee_for(partner, profile)
    currency = profile.get("currency") or "AMD"
    return web.json_response({
        "ok": True,
        "policy": policy,
        "fee": fee if policy == "premium" else 0,
        "currency": currency,
        "scope": profile.get("premium_disclosure_scope", "limited"),
        "premium_enabled": bool(partner.get("premium_contact_sharing_enabled", True)),
    })


async def contact_unlock(request):
    """Unlock the partner's contact for the calling client."""
    if not features.is_enabled("premium_contact"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)
    uid = _uid(request)
    service_id = int(request.match_info["service_id"])
    partner = _load_partner_for_service(service_id)
    if not partner:
        return web.json_response({"ok": False, "error": "service_not_available"}, status=404)
    partner_id = partner["id"]
    profile = _profile(partner)
    policy = partner.get("contact_share_policy") or "after_booking"
    scope = profile.get("premium_disclosure_scope", "limited")

    if policy == "never" or not partner.get("contact_sharing_enabled", True):
        return web.json_response({"ok": False, "error": "contact_not_shareable"}, status=403)

    # Already unlocked earlier? -> return the contact again, no charge.
    existing = _one(
        "SELECT * FROM contact_disclosures WHERE client_id=%s AND partner_id=%s "
        "AND status='disclosed' ORDER BY created_at DESC LIMIT 1",
        (uid, partner_id),
    )
    if existing:
        return web.json_response({
            "ok": True, "already_unlocked": True,
            "contact": _scope_contact(profile, scope),
            "disclosure_id": existing["id"],
        })

    if policy == "after_booking":
        paid = _one(
            "SELECT id FROM bookings WHERE client_id=%s AND partner_id=%s "
            "AND status IN ('paid','booked','completed') LIMIT 1",
            (uid, partner_id),
        )
        if not paid:
            return web.json_response(
                {"ok": False, "error": "booking_required"}, status=402)
        disclosure = _exec(
            "INSERT INTO contact_disclosures(client_id,partner_id,status,fee_amount,"
            "disclosure_scope,data_json) VALUES(%s,%s,'disclosed',0,%s,%s::jsonb) RETURNING *",
            (uid, partner_id, scope, _json({"reason": "after_booking", "service_id": service_id})),
            True,
        )
        return web.json_response({
            "ok": True, "paid": False,
            "contact": _scope_contact(profile, scope),
            "disclosure_id": disclosure["id"] if disclosure else None,
        })

    # policy == 'premium' -> charge the fee through Idram
    if not partner.get("premium_contact_sharing_enabled", True):
        return web.json_response({"ok": False, "error": "premium_disabled"}, status=403)
    fee = _fee_for(partner, profile)
    currency = profile.get("currency") or "AMD"
    if fee <= 0:
        # Nothing to charge -> disclose for free but still log it.
        disclosure = _exec(
            "INSERT INTO contact_disclosures(client_id,partner_id,status,fee_amount,"
            "disclosure_scope,data_json) VALUES(%s,%s,'disclosed',0,%s,%s::jsonb) RETURNING *",
            (uid, partner_id, scope, _json({"reason": "premium_zero_fee", "service_id": service_id})),
            True,
        )
        return web.json_response({
            "ok": True, "paid": False,
            "contact": _scope_contact(profile, scope),
            "disclosure_id": disclosure["id"] if disclosure else None,
        })

    idram = IdramProvider()
    intent = idram.create_invoice(
        amount=fee, currency=currency,
        description=f"Premium contact: {partner['business_name']}",
        order_id=f"contact-{partner_id}-{uid}",
        metadata={"partner_id": partner_id, "client_id": uid, "service_id": service_id},
    )
    payment = _exec(
        "INSERT INTO payments(client_id,partner_id,payment_type,status,amount,currency,"
        "provider,provider_payment_id,data_json) VALUES(%s,%s,'premium_contact',%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",
        (uid, partner_id, intent.status, fee, currency, intent.provider,
         intent.transaction_id, _json({"mode": intent.mode, "bill_no": intent.bill_no,
                                       "payment_url": intent.payment_url})),
        True,
    )
    # Live Idram is not settled yet -> hand back the redirect URL, no contact.
    if intent.status != "paid":
        _exec(
            "INSERT INTO contact_disclosures(client_id,partner_id,payment_id,status,fee_amount,"
            "disclosure_scope,data_json) VALUES(%s,%s,%s,'pending',%s,%s,%s::jsonb)",
            (uid, partner_id, payment["id"] if payment else None, fee, scope,
             _json({"payment_url": intent.payment_url})),
        )
        return web.json_response({
            "ok": True, "paid": False, "pending_payment": True,
            "payment_url": intent.payment_url,
            "payment_id": payment["id"] if payment else None,
        })

    # Settled (test mode or already-paid): disclose + credit the partner.
    disclosure = _exec(
        "INSERT INTO contact_disclosures(client_id,partner_id,payment_id,status,fee_amount,"
        "disclosure_scope,data_json) VALUES(%s,%s,%s,'disclosed',%s,%s,%s::jsonb) RETURNING *",
        (uid, partner_id, payment["id"] if payment else None, fee, scope,
         _json({"transaction": intent.transaction_id, "service_id": service_id})),
        True,
    )
    _exec(
        "INSERT INTO partner_financial_ledger(partner_id,entry_type,amount,currency,description) "
        "VALUES(%s,'premium_contact',%s,%s,%s)",
        (partner_id, fee, currency, "Premium contact disclosure fee"),
    )
    # Best-effort notify the partner.
    try:
        from notify import notify
        owner = _one("SELECT user_id FROM partners WHERE id=%s", (partner_id,))
        if owner and owner.get("user_id"):
            await notify(
                request.app, int(owner["user_id"]),
                title="🔓 Контакт разблокирован",
                body=f"Клиент оплатил доступ к вашим контактам ({fee:.0f} {currency}).",
                kind="premium_contact", audience="partner",
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
        "/api/market/client/service/{service_id}/contact/quote", contact_quote)
    app.router.add_post(
        "/api/market/client/service/{service_id}/contact/unlock", contact_unlock)
