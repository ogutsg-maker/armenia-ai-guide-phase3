"""Phase 3 — support tickets with an AI first line + human escalation.

Flow: user opens a ticket -> AI answers (status ``ai_answered``) unless it is
unsure or the user wants a human, in which case the ticket is ``escalated``
and an admin is notified. Admins reply and resolve/close from the admin panel.
"""
from __future__ import annotations

from aiohttp import web

import features
from marketplace_flow_api import _one, _rows, _exec, _uid, _json
from admin_ai_api import _admin


def _messages(ticket_id: int):
    return _rows(
        "SELECT sender_role,sender_id,message,created_at FROM support_ticket_messages "
        "WHERE ticket_id=%s ORDER BY created_at", (ticket_id,))


async def _ai_answer(request, ticket_id: int, user_text: str, lang: str):
    """Generate + persist an AI reply. Returns (reply, escalate)."""
    if not features.is_enabled("support_ai"):
        return "", True  # AI off -> straight to human
    ai = request.app.get("ai")
    if ai is None:
        return "", True
    history = _messages(ticket_id)
    try:
        out = await ai.support_reply(user_text, history=history, lang=lang)
    except Exception:
        return "", True
    reply = (out or {}).get("reply", "")
    escalate = bool((out or {}).get("escalate"))
    if reply:
        _exec(
            "INSERT INTO support_ticket_messages(ticket_id,sender_role,message) "
            "VALUES(%s,'ai',%s)", (ticket_id, reply))
    return reply, escalate


async def _escalate(request, ticket_id: int):
    _exec("UPDATE support_tickets SET status='escalated',updated_at=NOW() WHERE id=%s",
          (ticket_id,))
    # Notify the admin (best-effort).
    try:
        from notify import notify
        admin_id = int(request.app.get("stage3_admin_id") or 0)
        if admin_id:
            await notify(
                request.app, admin_id,
                title="🆘 Эскалация тикета",
                body=f"Тикет №{ticket_id} требует внимания человека.",
                kind="support_escalated", audience="admin",
                data={"ticket_id": ticket_id},
            )
    except Exception:
        pass


async def create_ticket(request):
    if not features.is_enabled("support"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)
    uid = _uid(request)
    data = await request.json()
    subject = str(data.get("subject") or "").strip()[:300]
    message = str(data.get("message") or data.get("text") or "").strip()[:4000]
    audience = "partner" if str(data.get("audience")) == "partner" else "client"
    lang = str(data.get("language") or "ru")[:5]
    if not message:
        return web.json_response({"ok": False, "error": "empty_message"}, status=400)

    ticket = _exec(
        "INSERT INTO support_tickets(user_id,audience,subject,status,data_json) "
        "VALUES(%s,%s,%s,'open',%s::jsonb) RETURNING *",
        (uid, audience, subject or message[:60], _json({"language": lang})), True)
    tid = ticket["id"]
    _exec("INSERT INTO support_ticket_messages(ticket_id,sender_role,sender_id,message) "
          "VALUES(%s,'user',%s,%s)", (tid, uid, message))

    reply, escalate = await _ai_answer(request, tid, message, lang)
    if escalate or not reply:
        await _escalate(request, tid)
        status = "escalated"
    else:
        _exec("UPDATE support_tickets SET status='ai_answered',updated_at=NOW() WHERE id=%s",
              (tid,))
        status = "ai_answered"
    return web.json_response({"ok": True, "ticket_id": tid, "status": status,
                              "reply": reply, "escalated": status == "escalated"})


async def add_message(request):
    if not features.is_enabled("support"):
        return web.json_response({"ok": False, "error": "feature_disabled"}, status=403)
    uid = _uid(request)
    tid = int(request.match_info["ticket_id"])
    data = await request.json()
    message = str(data.get("message") or data.get("text") or "").strip()[:4000]
    if not message:
        return web.json_response({"ok": False, "error": "empty_message"}, status=400)
    ticket = _one("SELECT * FROM support_tickets WHERE id=%s AND user_id=%s", (tid, uid))
    if not ticket:
        return web.json_response({"ok": False, "error": "ticket_not_found"}, status=404)
    if ticket.get("status") in ("resolved", "closed"):
        return web.json_response({"ok": False, "error": "ticket_closed"}, status=409)
    _exec("INSERT INTO support_ticket_messages(ticket_id,sender_role,sender_id,message) "
          "VALUES(%s,'user',%s,%s)", (tid, uid, message))
    lang = (ticket.get("data_json") or {}).get("language", "ru") if isinstance(ticket.get("data_json"), dict) else "ru"

    # Don't re-run AI once a human has taken over.
    if ticket.get("status") == "escalated":
        return web.json_response({"ok": True, "ticket_id": tid, "status": "escalated", "reply": ""})
    reply, escalate = await _ai_answer(request, tid, message, lang)
    if escalate or not reply:
        await _escalate(request, tid)
        status = "escalated"
    else:
        _exec("UPDATE support_tickets SET status='ai_answered',updated_at=NOW() WHERE id=%s", (tid,))
        status = "ai_answered"
    return web.json_response({"ok": True, "ticket_id": tid, "status": status,
                              "reply": reply, "escalated": status == "escalated"})


async def get_ticket(request):
    uid = _uid(request)
    tid = int(request.match_info["ticket_id"])
    ticket = _one("SELECT * FROM support_tickets WHERE id=%s AND user_id=%s", (tid, uid))
    if not ticket:
        return web.json_response({"ok": False, "error": "ticket_not_found"}, status=404)
    return web.json_response({"ok": True, "ticket": ticket, "messages": _messages(tid)})


async def list_tickets(request):
    uid = _uid(request)
    items = _rows(
        "SELECT id,subject,status,priority,updated_at FROM support_tickets "
        "WHERE user_id=%s ORDER BY updated_at DESC LIMIT 50", (uid,))
    return web.json_response({"ok": True, "items": items})


# --- admin side ------------------------------------------------------------
async def admin_list(request):
    _admin(request)
    status = request.query.get("status")
    if status:
        items = _rows("SELECT id,user_id,audience,subject,status,priority,updated_at "
                      "FROM support_tickets WHERE status=%s ORDER BY updated_at DESC LIMIT 100",
                      (status,))
    else:
        items = _rows("SELECT id,user_id,audience,subject,status,priority,updated_at "
                      "FROM support_tickets ORDER BY updated_at DESC LIMIT 100")
    return web.json_response({"ok": True, "items": items})


async def admin_reply(request):
    admin_id = _admin(request)
    tid = int(request.match_info["ticket_id"])
    data = await request.json()
    message = str(data.get("message") or data.get("reply") or "").strip()[:4000]
    if not message:
        return web.json_response({"ok": False, "error": "empty_message"}, status=400)
    ticket = _one("SELECT * FROM support_tickets WHERE id=%s", (tid,))
    if not ticket:
        return web.json_response({"ok": False, "error": "ticket_not_found"}, status=404)
    _exec("INSERT INTO support_ticket_messages(ticket_id,sender_role,sender_id,message) "
          "VALUES(%s,'admin',%s,%s)", (tid, admin_id, message))
    _exec("UPDATE support_tickets SET status='escalated',updated_at=NOW() WHERE id=%s", (tid,))
    # Push the admin answer to the user.
    try:
        from notify import notify
        await notify(
            request.app, int(ticket["user_id"]),
            title="💬 Ответ поддержки",
            body=message, kind="support_reply", audience=ticket.get("audience", "client"),
            data={"ticket_id": tid})
    except Exception:
        pass
    return web.json_response({"ok": True, "ticket_id": tid})


async def admin_resolve(request):
    _admin(request)
    tid = int(request.match_info["ticket_id"])
    data = await request.json() if request.can_read_body else {}
    new_status = "closed" if str(data.get("status")) == "closed" else "resolved"
    updated = _exec("UPDATE support_tickets SET status=%s,updated_at=NOW() WHERE id=%s RETURNING id",
                    (new_status, tid), True)
    if not updated:
        return web.json_response({"ok": False, "error": "ticket_not_found"}, status=404)
    return web.json_response({"ok": True, "ticket_id": tid, "status": new_status})


def register_support_routes(app):
    app.router.add_post("/api/support/ticket", create_ticket)
    app.router.add_post("/api/support/ticket/{ticket_id}/message", add_message)
    app.router.add_get("/api/support/ticket/{ticket_id}", get_ticket)
    app.router.add_get("/api/support/tickets", list_tickets)
    app.router.add_get("/api/admin/support/tickets", admin_list)
    app.router.add_post("/api/admin/support/ticket/{ticket_id}/reply", admin_reply)
    app.router.add_post("/api/admin/support/ticket/{ticket_id}/resolve", admin_resolve)
