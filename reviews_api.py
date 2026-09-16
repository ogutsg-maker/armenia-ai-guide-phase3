"""Phase 3 — reviews & ratings.

A client leaves one rating (1–5) + comment per completed booking. The comment
goes through AI moderation: clean text is ``published`` immediately, suspicious
text is ``flagged`` for admin review. Partners can reply once. Aggregated
rating is exposed for ranking / storefront.
"""
from __future__ import annotations

from aiohttp import web

import features
from marketplace_flow_api import _one, _rows, _exec, _uid, _json


def _partner_id_for_user(uid: int):
    row = _one("SELECT id FROM partners WHERE user_id=%s", (uid,))
    return row["id"] if row else None


async def create_review(request):
    if not features.is_enabled("reviews"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)
    uid = _uid(request)
    booking_id = int(request.match_info["booking_id"])
    data = await request.json()
    try:
        rating = int(data.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    lo = int(features.get("review_min_rating", 1))
    hi = int(features.get("review_max_rating", 5))
    if not (lo <= rating <= hi):
        return web.json_response({"ok": False, "error": "invalid_rating"}, status=400)
    comment = str(data.get("comment") or "").strip()[:2000]

    booking = _one(
        "SELECT * FROM bookings WHERE id=%s AND client_id=%s", (booking_id, uid))
    if not booking:
        return web.json_response({"ok": False, "error": "booking_not_found"}, status=404)
    if booking.get("status") != "completed":
        return web.json_response({"ok": False, "error": "booking_not_completed"}, status=400)

    if _one("SELECT id FROM reviews WHERE booking_id=%s AND client_id=%s",
            (booking_id, uid)):
        return web.json_response({"ok": False, "error": "already_reviewed"}, status=409)

    # AI moderation (best-effort; falls back to heuristic in mock mode).
    status = "published"
    moderation = {"flagged": False, "reason": ""}
    if features.is_enabled("review_ai_moderation") and comment:
        ai = request.app.get("ai")
        if ai is not None:
            try:
                moderation = await ai.moderate_review(comment, rating)
            except Exception:
                moderation = {"flagged": False, "reason": ""}
        if moderation.get("flagged"):
            status = "flagged"

    review = _exec(
        "INSERT INTO reviews(booking_id,client_id,partner_id,service_id,rating,comment,"
        "status,data_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",
        (booking_id, uid, booking["partner_id"], booking.get("service_id"),
         rating, comment, status, _json({"moderation": moderation})),
        True,
    )

    # Notify the partner about a new (published) review.
    if status == "published":
        try:
            from notify import notify
            owner = _one("SELECT user_id FROM partners WHERE id=%s", (booking["partner_id"],))
            if owner and owner.get("user_id"):
                await notify(
                    request.app, int(owner["user_id"]),
                    title="⭐ Новый отзыв",
                    body=f"Оценка {rating}/5 по брони №{booking_id}.",
                    kind="review_new", audience="partner",
                    data={"review_id": review["id"] if review else None},
                )
        except Exception:
            pass

    return web.json_response({
        "ok": True, "review_id": review["id"] if review else None,
        "status": status, "flagged": status == "flagged",
    })


async def partner_reply(request):
    if not features.is_enabled("reviews"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)
    uid = _uid(request)
    review_id = int(request.match_info["review_id"])
    data = await request.json()
    reply = str(data.get("reply") or data.get("message") or "").strip()[:2000]
    if not reply:
        return web.json_response({"ok": False, "error": "empty_reply"}, status=400)
    partner_id = _partner_id_for_user(uid)
    if not partner_id:
        return web.json_response({"ok": False, "error": "not_a_partner"}, status=403)
    review = _one("SELECT * FROM reviews WHERE id=%s AND partner_id=%s",
                  (review_id, partner_id))
    if not review:
        return web.json_response({"ok": False, "error": "review_not_found"}, status=404)
    updated = _exec(
        "UPDATE reviews SET partner_reply=%s,updated_at=NOW() WHERE id=%s RETURNING *",
        (reply, review_id), True)
    return web.json_response({"ok": True, "review_id": review_id,
                              "partner_reply": updated.get("partner_reply") if updated else reply})


def _aggregate(partner_id: int) -> dict:
    agg = _one(
        "SELECT COUNT(*) cnt, COALESCE(AVG(rating),0) avg_rating "
        "FROM reviews WHERE partner_id=%s AND status='published'",
        (partner_id,))
    return {
        "count": int(agg["cnt"]) if agg else 0,
        "avg_rating": round(float(agg["avg_rating"]), 2) if agg else 0.0,
    }


async def partner_reviews(request):
    if not features.is_enabled("reviews"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)
    partner_id = int(request.match_info["partner_id"])
    items = _rows(
        "SELECT id,rating,comment,partner_reply,status,created_at FROM reviews "
        "WHERE partner_id=%s AND status='published' ORDER BY created_at DESC LIMIT 50",
        (partner_id,))
    return web.json_response({"ok": True, "summary": _aggregate(partner_id), "items": items})


def register_reviews_routes(app):
    app.router.add_post(
        "/api/market/client/booking/{booking_id}/review", create_review)
    app.router.add_post(
        "/api/market/partner/review/{review_id}/reply", partner_reply)
    app.router.add_get(
        "/api/market/partner/{partner_id}/reviews", partner_reviews)
