import re
from aiohttp import web

from auth import user, require_admin
from data import partners
from data.core import active_services
from ai.manager import turn
from db import run, exec


def j(x, status=200):
    return web.json_response(x, status=status)


async def session(r):
    u = await user(r)
    return j({
        "ok": True,
        "user": u,
        "partner": partners.get(u["telegram_id"]),
        "admin": u["telegram_id"] == __import__("config").ADMIN_ID,
    })


async def register(r):
    u = await user(r)
    b = await r.json()
    name = str(b.get("name", "")).strip()
    phone = str(b.get("phone", "")).strip()
    if not name or not re.fullmatch(r"[+0-9() .-]{7,30}", phone):
        return j({"ok": False, "error": "name_and_phone_required"}, status=400)
    p, c = partners.register(u["telegram_id"], name, phone)
    return j({"ok": True, "partner_id": p["id"], "company": c, "destination": "/partner_cabinet.html"})


async def partner_ai(r):
    u = await user(r)
    b = await r.json()
    return j({"ok": True, "result": await turn(u["telegram_id"], "PARTNER", str(b.get("text", "")))})


async def client_search(r):
    u = await user(r)
    b = await r.json()
    return j({"ok": True, "items": active_services(b.get("text"), b.get("city"))})


async def admin_ai(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    b = await r.json()
    result = await turn(u["telegram_id"], "ADMIN", str(b.get("text", "")))
    return j({"ok": True, "result": result, "answer": result.get("text", "")})


async def admin_services(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    items = run(
        """SELECT s.id,s.name,s.price_type,s.price_amd,s.status,
                  c.name AS company_name
           FROM aig_services s
           JOIN aig_companies c ON c.id=s.company_id
           WHERE s.status IN ('PENDING_ADMIN','CLASSIFICATION_PENDING')
           ORDER BY s.id DESC""",
        many=True,
    )
    return j({"ok": True, "items": items})


async def admin_service_decision(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    service_id = int(r.match_info["id"])
    body = await r.json()
    action = str(body.get("action", "")).lower()
    if action not in ("approve", "reject"):
        return j({"ok": False, "error": "invalid_action"}, status=400)

    service = run("SELECT id,status FROM aig_services WHERE id=%s", (service_id,))
    if not service:
        return j({"ok": False, "error": "service_not_found"}, status=404)

    if action == "approve":
        exec("UPDATE aig_services SET status='ACTIVE',updated_at=now() WHERE id=%s", (service_id,))
        exec(
            "UPDATE aig_service_applications SET status='APPROVED',reviewed_at=now(),reviewer=%s "
            "WHERE service_id=%s AND status IN ('PENDING_ADMIN','CLASSIFICATION_PENDING')",
            (u["telegram_id"], service_id),
        )
    else:
        exec("UPDATE aig_services SET status='REJECTED',updated_at=now() WHERE id=%s", (service_id,))
        exec(
            "UPDATE aig_service_applications SET status='REJECTED',reviewed_at=now(),reviewer=%s "
            "WHERE service_id=%s AND status IN ('PENDING_ADMIN','CLASSIFICATION_PENDING')",
            (u["telegram_id"], service_id),
        )

    return j({"ok": True, "service_id": service_id, "status": "ACTIVE" if action == "approve" else "REJECTED"})


async def applications(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    return j({
        "ok": True,
        "items": run(
            """SELECT a.*,s.name AS service_name,c.name AS company_name
               FROM aig_service_applications a
               JOIN aig_services s ON s.id=a.service_id
               JOIN aig_companies c ON c.id=s.company_id
               ORDER BY a.id DESC""",
            many=True,
        ),
    })


def setup_routes(app):
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/partner/register", register)
    app.router.add_post("/api/partner/ai", partner_ai)
    app.router.add_post("/api/client/search", client_search)
    app.router.add_post("/api/admin/ai", admin_ai)
    app.router.add_get("/api/admin/applications", applications)
    app.router.add_get("/api/admin/services", admin_services)
    app.router.add_post("/api/admin/services/{id}/decision", admin_service_decision)
