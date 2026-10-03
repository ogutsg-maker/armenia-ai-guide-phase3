import re
from aiohttp import web

from auth import user, require_admin
from data import partners
from data.core import active_services
from ai.manager import turn, partner_service_preview
from ai.session import get as get_ai_session, clear as clear_ai_session
from lifecycle.services import create_after_confirmation
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
    try:
        b = await r.json()
    except Exception:
        return j({"ok": False, "error": "invalid_json"}, status=400)
    name = str(b.get("name", "")).strip()
    phone = str(b.get("phone", "")).strip()
    if not name or not re.fullmatch(r"[+0-9() .-]{7,30}", phone):
        return j({"ok": False, "error": "name_and_phone_required"}, status=400)
    p = partners.get(u["telegram_id"])
    if p:
        existing = partners.companies(p["id"])
        if existing:
            return j({"ok": True, "partner_id": p["id"], "company": existing[0], "destination": "/partner_cabinet.html", "existing": True})
    p, c = partners.register(u["telegram_id"], name, phone)
    return j({"ok": True, "partner_id": p["id"], "company": c, "destination": "/partner_cabinet.html", "existing": False})


async def partner_ai(r):
    u = await user(r)
    b = await r.json()
    text = str(b.get('text','')).strip()
    if not text: return j({'ok':False,'error':'text_required'}, status=400)
    confirmation = str(b.get('confirm','')).lower()
    state = get_ai_session(u['telegram_id'])
    if confirmation in ('yes','confirm','համաձայն եմ','подтверждаю','да'):
        pending = state.get('pending') if state else None
        if not pending or pending.get('action') != 'create_service': return j({'ok':False,'error':'nothing_to_confirm'}, status=400)
        service = pending['service']
        row,status = create_after_confirmation(u['telegram_id'],service['company_id'],service)
        clear_ai_session(u['telegram_id'])
        return j({'ok':True,'kind':'created','service':row,'status':status})
    try:
        result = await partner_service_preview(u['telegram_id'],text)
        return j({'ok':True,'result':result})
    except ValueError as exc:
        return j({'ok':False,'error':str(exc)}, status=400)

async def client_search(r):
    u = await user(r)
    b = await r.json()
    return j({"ok": True, "items": active_services(b.get("text"), b.get("city"))})


async def partner_profile(r):
    u = await user(r)
    partner = partners.get(u['telegram_id'])
    if not partner: return j({'ok':False,'error':'partner_registration_required'}, status=404)
    return j({'ok':True,'partner':partner,'companies':partners.companies(partner['id'])})

async def partner_services(r):
    u = await user(r)
    partner = partners.get(u['telegram_id'])
    if not partner: return j({'ok':False,'error':'partner_registration_required'}, status=404)
    items = run("SELECT s.*,c.name AS company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE c.partner_id=%s AND c.archived=false ORDER BY s.id DESC", (partner['id'],), True)
    return j({'ok':True,'items':items})

async def partner_service_confirm(r):
    u = await user(r)
    state = get_ai_session(u['telegram_id'])
    pending = state.get('pending') if state else None
    if not pending or pending.get('action') != 'create_service': return j({'ok':False,'error':'nothing_to_confirm'}, status=400)
    service = pending['service']
    row,status = create_after_confirmation(u['telegram_id'],service['company_id'],service)
    clear_ai_session(u['telegram_id'])
    return j({'ok':True,'kind':'created','service':row,'status':status})

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
    app.router.add_get("/api/partner/profile", partner_profile)
    app.router.add_get("/api/partner/services", partner_services)
    app.router.add_post("/api/partner/services/confirm", partner_service_confirm)
    app.router.add_post("/api/client/search", client_search)
    app.router.add_post("/api/admin/ai", admin_ai)
    app.router.add_get("/api/admin/applications", applications)
    app.router.add_get("/api/admin/services", admin_services)
    app.router.add_post("/api/admin/services/{id}/decision", admin_service_decision)
