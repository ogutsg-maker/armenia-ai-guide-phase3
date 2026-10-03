import re
import json
from aiohttp import web

from auth import user, require_admin
from data import partners
from data.core import active_services
from ai.manager import turn, partner_service_preview
from ai.session import get as get_ai_session, clear as clear_ai_session
from lifecycle.services import create_after_confirmation
from lifecycle.bookings import create_booking, partner_confirm_booking, confirm_commission_payment, checkin, complete, submit_review
from db import run, exec


def j(x, status=200):
    return web.json_response(x, status=status, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))


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


async def client_select_service(r):
    u = await user(r)
    try:
        service_id = int(r.match_info["service_id"])
    except (TypeError, ValueError):
        return j({"ok": False, "error": "invalid_service_id"}, status=400)

    service = run(
        """SELECT s.id,s.name,s.price_type,s.price_amd,s.territory,s.address_id,
                  c.name AS company_name,p.id AS partner_id,p.telegram_id AS partner_telegram_id,
                  a.city,a.district
           FROM aig_services s
           JOIN aig_companies c ON c.id=s.company_id
           JOIN aig_partners p ON p.id=c.partner_id
           LEFT JOIN aig_addresses a ON a.id=s.address_id
           WHERE s.id=%s AND s.status='ACTIVE' AND c.archived=false AND p.status='active'""",
        (service_id,),
    )
    if not service:
        return j({"ok": False, "error": "active_service_not_found"}, status=404)

    existing = run(
        """SELECT n.id,n.status
           FROM aig_negotiations n
           JOIN aig_client_requests r ON r.id=n.request_id
           WHERE r.client_telegram_id=%s AND n.service_id=%s
             AND n.status IN ('waiting_partner','active','agreed')
           ORDER BY n.id DESC LIMIT 1""",
        (u["telegram_id"], service_id),
    )
    if existing:
        return j({
            "ok": True,
            "request_id": run("SELECT request_id FROM aig_negotiations WHERE id=%s", (existing["id"],))["request_id"],
            "negotiation_id": existing["id"],
            "status": existing["status"],
            "existing": True,
        })

    city = service.get("city") or service.get("territory")
    district = service.get("district")
    request_row = run(
        """INSERT INTO aig_client_requests(client_telegram_id,service_text,city,district,status)
           VALUES(%s,%s,%s,%s,'searching') RETURNING *""",
        (u["telegram_id"], service["name"], city, district),
    )
    negotiation = run(
        """INSERT INTO aig_negotiations
             (request_id,service_id,partner_id,status,interest_deadline)
           VALUES(%s,%s,%s,'waiting_partner',now()+interval '3 minutes')
           RETURNING *""",
        (request_row["id"], service["id"], service["partner_id"]),
    )

    exec(
        """INSERT INTO aig_notifications(telegram_id,kind,payload)
           VALUES(%s,'new_client_request',%s)""",
        (
            service["partner_telegram_id"],
            json.dumps({
                "request_id": request_row["id"],
                "negotiation_id": negotiation["id"],
                "service_id": service["id"],
                "service_name": service["name"],
                "company_name": service["company_name"],
                "city": city,
                "district": district,
                "price_type": service["price_type"],
                "price_amd": service["price_amd"],
            }, ensure_ascii=False, default=str),
        ),
    )
    return j({
        "ok": True,
        "request_id": request_row["id"],
        "negotiation_id": negotiation["id"],
        "status": negotiation["status"],
        "interest_deadline": negotiation["interest_deadline"],
        "service": service,
        "existing": False,
    })


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

async def partner_notifications(r):
    u = await user(r)
    partner = partners.get(u["telegram_id"])
    if not partner:
        return j({"ok": False, "error": "partner_registration_required"}, status=404)
    items = run(
        """SELECT id,kind,payload,read_at,created_at
           FROM aig_notifications
           WHERE telegram_id=%s
           ORDER BY id DESC
           LIMIT 50""",
        (u["telegram_id"],),
        True,
    )
    return j({"ok": True, "items": items})


async def partner_negotiation_decision(r):
    u = await user(r)
    partner = partners.get(u["telegram_id"])
    if not partner:
        return j({"ok": False, "error": "partner_registration_required"}, status=404)
    try:
        negotiation_id = int(r.match_info["id"])
    except (TypeError, ValueError):
        return j({"ok": False, "error": "invalid_negotiation_id"}, status=400)
    body = await r.json()
    action = str(body.get("action", "")).strip().lower()
    if action not in ("interest", "decline"):
        return j({"ok": False, "error": "invalid_action"}, status=400)

    negotiation = run(
        """SELECT n.*,r.client_telegram_id,s.name AS service_name,c.name AS company_name
           FROM aig_negotiations n
           JOIN aig_client_requests r ON r.id=n.request_id
           JOIN aig_services s ON s.id=n.service_id
           JOIN aig_companies c ON c.id=s.company_id
           WHERE n.id=%s AND n.partner_id=%s
           FOR UPDATE""",
        (negotiation_id, partner["id"]),
    )
    if not negotiation:
        return j({"ok": False, "error": "negotiation_not_found"}, status=404)

    if negotiation["status"] != "waiting_partner":
        return j({"ok": True, "status": negotiation["status"], "already_decided": True})

    if negotiation.get("interest_deadline") and negotiation["interest_deadline"] < __import__("datetime").datetime.now(__import__("datetime").timezone.utc):
        exec("UPDATE aig_negotiations SET status='expired' WHERE id=%s", (negotiation_id,))
        exec("UPDATE aig_client_requests SET status='searching' WHERE id=%s", (negotiation["request_id"],))
        return j({"ok": True, "status": "expired", "expired": True})

    if action == "interest":
        exec(
            "UPDATE aig_negotiations SET status='active',partner_interest_at=now() WHERE id=%s",
            (negotiation_id,),
        )
        exec(
            "UPDATE aig_client_requests SET status='negotiating' WHERE id=%s",
            (negotiation["request_id"],),
        )
        exec(
            "INSERT INTO aig_notifications(telegram_id,kind,payload) VALUES(%s,'partner_interested',%s)",
            (
                negotiation["client_telegram_id"],
                json.dumps({
                    "request_id": negotiation["request_id"],
                    "negotiation_id": negotiation_id,
                    "service_name": negotiation["service_name"],
                    "company_name": negotiation["company_name"],
                }, ensure_ascii=False, default=str),
            ),
        )
        return j({"ok": True, "status": "active", "negotiation_id": negotiation_id})

    exec("UPDATE aig_negotiations SET status='declined' WHERE id=%s", (negotiation_id,))
    exec("UPDATE aig_client_requests SET status='searching' WHERE id=%s", (negotiation["request_id"],))
    exec(
        "INSERT INTO aig_notifications(telegram_id,kind,payload) VALUES(%s,'partner_declined',%s)",
        (
            negotiation["client_telegram_id"],
            json.dumps({
                "request_id": negotiation["request_id"],
                "negotiation_id": negotiation_id,
                "service_name": negotiation["service_name"],
                "company_name": negotiation["company_name"],
            }, ensure_ascii=False, default=str),
        ),
    )
    return j({"ok": True, "status": "declined", "negotiation_id": negotiation_id})



async def negotiation_get(r):
    u = await user(r)
    try:
        negotiation_id = int(r.match_info["id"])
    except (TypeError, ValueError):
        return j({"ok": False, "error": "invalid_negotiation_id"}, status=400)
    n = run("""SELECT n.id,n.status,n.request_id,n.service_id,n.partner_id,
                      n.agreed_min,n.agreed_max,n.agreed_price,n.commission_base,n.agreed_payload,
                      n.partner_interest_at,n.created_at,n.interest_deadline,
                      s.name AS service_name,c.name AS company_name,
                      r.client_telegram_id,r.city,r.district
               FROM aig_negotiations n
               JOIN aig_client_requests r ON r.id=n.request_id
               JOIN aig_services s ON s.id=n.service_id
               JOIN aig_companies c ON c.id=s.company_id
               WHERE n.id=%s""",(negotiation_id,))
    if not n:
        return j({"ok": False, "error": "negotiation_not_found"}, status=404)
    requested_role = str(r.query.get("role", "")).strip().lower()
    partner = partners.get(u["telegram_id"])
    can_be_partner = bool(partner and int(n["partner_id"]) == int(partner["id"]))
    can_be_client = int(n["client_telegram_id"]) == int(u["telegram_id"])
    if requested_role == "client":
        if not can_be_client:
            return j({"ok": False, "error": "forbidden"}, status=403)
        role = "client"
    elif requested_role == "partner":
        if not can_be_partner:
            return j({"ok": False, "error": "forbidden"}, status=403)
        role = "partner"
    elif can_be_client:
        role = "client"
    elif can_be_partner:
        role = "partner"
    else:
        return j({"ok": False, "error": "forbidden"}, status=403)
    messages = run("""SELECT id,sender_role,sender_id,message,data_json,created_at
                      FROM negotiation_messages
                      WHERE negotiation_id=%s ORDER BY id ASC""",(negotiation_id,),True)
    return j({"ok": True, "negotiation": n, "messages": messages, "role": role})


async def negotiation_message(r):
    u = await user(r)
    try:
        negotiation_id = int(r.match_info["id"])
    except (TypeError, ValueError):
        return j({"ok": False, "error": "invalid_negotiation_id"}, status=400)
    try:
        body = await r.json()
    except Exception:
        return j({"ok": False, "error": "invalid_json"}, status=400)

    message = str(body.get("message", "")).strip()
    if not message or len(message) > 4000:
        return j({"ok": False, "error": "message_required"}, status=400)

    n = run(
        """SELECT n.id,n.status,n.partner_id,r.client_telegram_id
           FROM aig_negotiations n
           JOIN aig_client_requests r ON r.id=n.request_id
           WHERE n.id=%s""",
        (negotiation_id,),
    )
    if not n:
        return j({"ok": False, "error": "negotiation_not_found"}, status=404)

    requested_role = str(r.query.get("role", "")).strip().lower()
    partner = partners.get(u["telegram_id"])
    can_be_partner = bool(partner and int(n["partner_id"]) == int(partner["id"]))
    can_be_client = int(n["client_telegram_id"]) == int(u["telegram_id"])

    if requested_role == "client":
        if not can_be_client:
            return j({"ok": False, "error": "forbidden"}, status=403)
        role = "client"
    elif requested_role == "partner":
        if not can_be_partner:
            return j({"ok": False, "error": "forbidden"}, status=403)
        role = "partner"
    elif can_be_client:
        role = "client"
    elif can_be_partner:
        role = "partner"
    else:
        return j({"ok": False, "error": "forbidden"}, status=403)

    if n["status"] != "active":
        return j({"ok": False, "error": "negotiation_not_active"}, status=409)

    row = run(
        """INSERT INTO negotiation_messages
             (negotiation_id,sender_role,sender_id,message,data_json)
           VALUES(%s,%s,%s,%s,%s)
           RETURNING id,sender_role,sender_id,message,data_json,created_at""",
        (negotiation_id, role, u["telegram_id"], message, json.dumps({}, ensure_ascii=False)),
    )

    # Natural-chat agreement fast path.
    # We do not turn ordinary messages into forms/cards. We only finalize when
    # explicit agreement language exists on both sides and a concrete price
    # has appeared in the conversation.
    history = run(
        """SELECT sender_role,message
           FROM negotiation_messages
           WHERE negotiation_id=%s
           ORDER BY id ASC""",
        (negotiation_id,),
        True,
    )

    def _price_from_text(value):
        text = str(value or "").replace(",", ".")
        matches = re.findall(r"(?<!\\d)(\\d{3,7})(?:\\.\\d+)?", text)
        if not matches:
            return None
        return float(matches[-1])

    def _normalize_chat_text(value):
        return " ".join(str(value or "").lower().replace("։", " ").replace("՝", " ").split())

    def _has_partner_acceptance(value):
        t = _normalize_chat_text(value)
        phrases = (
            "да", "yes", "այո", "согласен", "согласна", "согласны",
            "сможем", "договорились", "подходит", "ок", "ok", "լավ",
            "կգամ", "կգանք", "ուրեմն կգամ", "ուրեմն կգանք",
            "буду", "приеду", "приедем", "хорошо",
        )
        return any(t == p or t.startswith(p + " ") or t.endswith(" " + p) for p in phrases)

    def _has_client_acceptance(value):
        t = _normalize_chat_text(value)
        phrases = (
            "договорились", "согласовано", "согласен", "согласна",
            "беру", "подходит", "да", "yes", "այո", "ок", "ok",
            "լավ", "համաձայն եմ", "համաձայն",
        )
        return any(t == p or t.startswith(p + " ") or t.endswith(" " + p) for p in phrases)

    prices = [_price_from_text(x.get("message")) for x in history]
    price = next((p for p in reversed(prices) if p is not None), None)
    partner_agreed = any(x.get("sender_role") == "partner" and _has_partner_acceptance(x.get("message")) for x in history)
    client_agreed = any(x.get("sender_role") == "client" and _has_client_acceptance(x.get("message")) for x in history)

    if price is not None and (partner_agreed or client_agreed):
        payload = n.get("agreed_payload") or {}
        payload.update({
            "agreed_price": price,
            "client_agreed": bool(payload.get("client_agreed") or client_agreed),
            "partner_agreed": bool(payload.get("partner_agreed") or partner_agreed),
        })
        if payload["client_agreed"] and payload["partner_agreed"]:
            exec(
                """UPDATE aig_negotiations
                   SET agreed_price=%s,agreed_min=%s,agreed_max=%s,
                       agreed_payload=%s,agreed_at=now(),
                       commission_base=%s,status='agreed'
                   WHERE id=%s""",
                (price, price, price, json.dumps(payload, ensure_ascii=False), price, negotiation_id),
            )
            return j({"ok": True, "message": row, "status": "agreed", "agreed_price": price})
        exec(
            """UPDATE aig_negotiations
               SET agreed_price=%s,agreed_min=%s,agreed_max=%s,agreed_payload=%s
               WHERE id=%s""",
            (price, price, price, json.dumps(payload, ensure_ascii=False), negotiation_id),
        )

    return j({"ok": True, "message": row, "status": "active"})


async def client_notifications(r):
    u = await user(r)
    items = run("SELECT id,kind,payload,read_at,created_at FROM aig_notifications WHERE telegram_id=%s ORDER BY id DESC LIMIT 50",(u["telegram_id"],),True)
    return j({"ok":True,"items":items})

async def client_negotiations(r):
    u = await user(r)
    items = run("SELECT n.id,n.status,n.request_id,n.service_id,n.agreed_min,n.agreed_max,n.agreed_price,n.created_at,n.partner_interest_at,s.name AS service_name,c.name AS company_name,r.city,r.district FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_services s ON s.id=n.service_id JOIN aig_companies c ON c.id=s.company_id WHERE r.client_telegram_id=%s ORDER BY n.id DESC LIMIT 50",(u["telegram_id"],),True)
    return j({"ok":True,"items":items})

async def client_mark_notification_read(r):
    u = await user(r)
    notification_id = int(r.match_info["id"])
    exec("UPDATE aig_notifications SET read_at=now() WHERE id=%s AND telegram_id=%s",(notification_id,u["telegram_id"]))
    return j({"ok":True})


async def partner_negotiations(r):
    u=await user(r)
    partner=partners.get(u["telegram_id"])
    if not partner:
        return j({"ok":False,"error":"partner_registration_required"},404)
    items=run("""SELECT n.id,n.status,n.request_id,n.service_id,n.agreed_price,n.created_at,n.partner_interest_at,
                        s.name AS service_name,r.client_telegram_id,r.city,r.district,
                        (SELECT message FROM negotiation_messages m WHERE m.negotiation_id=n.id ORDER BY m.id DESC LIMIT 1) AS last_message
                 FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id
                 JOIN aig_services s ON s.id=n.service_id
                 WHERE n.partner_id=%s ORDER BY COALESCE(n.partner_interest_at,n.created_at) DESC LIMIT 50""",(partner["id"],),True)
    return j({"ok":True,"items":items})

async def partner_mark_notification_read(r):
    u = await user(r)
    notification_id = int(r.match_info["id"])
    exec(
        "UPDATE aig_notifications SET read_at=now() WHERE id=%s AND telegram_id=%s",
        (notification_id, u["telegram_id"]),
    )
    return j({"ok": True})


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



async def negotiation_terms(r):
    u=await user(r)
    try: nid=int(r.match_info["id"]); b=await r.json()
    except: return j({"ok":False,"error":"invalid_request"},400)
    n=run("""SELECT n.*,r.client_telegram_id FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id WHERE n.id=%s""",(nid,))
    if not n: return j({"ok":False,"error":"negotiation_not_found"},404)
    partner=partners.get(u["telegram_id"])
    role="client" if int(n["client_telegram_id"])==int(u["telegram_id"]) else ("partner" if partner and int(n["partner_id"])==int(partner["id"]) else None)
    if not role: return j({"ok":False,"error":"forbidden"},403)
    if n["status"]!="active": return j({"ok":False,"error":"negotiation_not_active"},409)
    try:
        price=float(b.get("agreed_price"))
        if price<=0: raise ValueError()
    except: return j({"ok":False,"error":"valid_agreed_price_required"},400)
    payload=n.get("agreed_payload") or {}
    payload.update({"agreed_price":price,role+"_agreed":True})
    exec("UPDATE aig_negotiations SET agreed_price=%s,agreed_min=%s,agreed_max=%s,agreed_payload=%s,agreed_at=CASE WHEN %s THEN now() ELSE agreed_at END,status=CASE WHEN COALESCE((%s)::jsonb->>'client_agreed','false')::boolean AND COALESCE((%s)::jsonb->>'partner_agreed','false')::boolean THEN 'agreed' ELSE 'active' END WHERE id=%s",
         (price,price,price,json.dumps(payload),payload.get("client_agreed") and payload.get("partner_agreed"),json.dumps(payload),json.dumps(payload),nid))
    return j({"ok":True,"status":"agreed" if payload.get("client_agreed") and payload.get("partner_agreed") else "active","agreed_price":price})


async def booking_by_negotiation(r):
    u=await user(r)
    try: nid=int(r.match_info["id"])
    except: return j({"ok":False,"error":"invalid_negotiation_id"},400)
    row=run("""SELECT b.*,n.partner_id,r.client_telegram_id,p.telegram_id AS partner_telegram_id
               FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id
               JOIN aig_client_requests r ON r.id=n.request_id
               JOIN aig_partners p ON p.id=n.partner_id WHERE b.negotiation_id=%s""",(nid,))
    if not row: return j({"ok":True,"booking":None})
    partner=partners.get(u["telegram_id"])
    if int(row["client_telegram_id"])!=int(u["telegram_id"]) and not (partner and int(row["partner_id"])==int(partner["id"])):
        return j({"ok":False,"error":"forbidden"},403)
    return j({"ok":True,"booking":row})

async def client_agree(r):
    u=await user(r)
    try: nid=int(r.match_info["id"])
    except: return j({"ok":False,"error":"invalid_negotiation_id"},400)
    n=run("SELECT n.*,r.client_telegram_id FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id WHERE n.id=%s",(nid,))
    if not n or int(n["client_telegram_id"])!=int(u["telegram_id"]): return j({"ok":False,"error":"forbidden"},403)
    if n["status"]!="active": return j({"ok":False,"error":"negotiation_not_active"},409)
    if not n.get("agreed_price"): return j({"ok":False,"error":"agreed_price_required"},409)
    exec("UPDATE aig_negotiations SET status='agreed',agreed_at=now(),commission_base=agreed_price WHERE id=%s",(nid,))
    return j({"ok":True,"status":"agreed"})

async def client_booking(r):
    u=await user(r)
    try: nid=int(r.match_info["id"])
    except: return j({"ok":False,"error":"invalid_negotiation_id"},400)
    try: b=await r.json()
    except: b={}
    try: row=create_booking(u["telegram_id"],nid,b)
    except ValueError as e: return j({"ok":False,"error":str(e)},409)
    return j({"ok":True,"booking":row})

async def partner_booking_confirm(r):
    u=await user(r)
    try: bid=int(r.match_info["id"])
    except: return j({"ok":False,"error":"invalid_booking_id"},400)
    try: row=partner_confirm_booking(u["telegram_id"],bid)
    except ValueError as e: return j({"ok":False,"error":str(e)},404)
    return j({"ok":True,"booking":row})

async def booking_payment(r):
    u=await user(r)
    try: bid=int(r.match_info["id"])
    except: return j({"ok":False,"error":"invalid_booking_id"},400)
    row=run("SELECT b.*,n.request_id,r.client_telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_client_requests r ON r.id=n.request_id WHERE b.id=%s",(bid,))
    if not row or int(row["client_telegram_id"])!=int(u["telegram_id"]): return j({"ok":False,"error":"booking_not_found"},404)
    if row["status"]!="PENDING_PAYMENT": return j({"ok":False,"error":"booking_not_payable"},409)
    return j({"ok":False,"error":"payment_provider_not_configured","amount":row["commission_amd"],"message":"Payment provider credentials are required before charging a real client."},503)

async def admin_test_payment(r):
    u=await user(r); require_admin(u["telegram_id"])
    try: bid=int(r.match_info["id"])
    except: return j({"ok":False,"error":"invalid_booking_id"},400)
    try: row=confirm_commission_payment(bid,"admin-test")
    except ValueError as e: return j({"ok":False,"error":str(e)},409)
    return j({"ok":True,"booking":row})

async def partner_checkin(r):
    u=await user(r)
    try: b=await r.json(); token=str(b.get("token","")).strip()
    except: token=""
    try: row=checkin(u["telegram_id"],token)
    except ValueError as e: return j({"ok":False,"error":str(e)},409)
    return j({"ok":True,"booking":row})

async def partner_complete(r):
    u=await user(r)
    try: bid=int(r.match_info["id"])
    except: return j({"ok":False,"error":"invalid_booking_id"},400)
    try: row=complete(u["telegram_id"],bid)
    except ValueError as e: return j({"ok":False,"error":str(e)},409)
    return j({"ok":True,"booking":row})

async def client_review(r):
    u=await user(r)
    try: bid=int(r.match_info["id"]); b=await r.json()
    except: return j({"ok":False,"error":"invalid_request"},400)
    try: rating=int(b.get("rating")); txt=str(b.get("text","")).strip()
    except: return j({"ok":False,"error":"invalid_rating"},400)
    if rating<1 or rating>5: return j({"ok":False,"error":"invalid_rating"},400)
    try: row=submit_review(u["telegram_id"],bid,rating,txt)
    except ValueError as e: return j({"ok":False,"error":str(e)},409)
    return j({"ok":True,"review":row})

def setup_routes(app):
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/partner/register", register)
    app.router.add_post("/api/partner/ai", partner_ai)
    app.router.add_get("/api/partner/profile", partner_profile)
    app.router.add_get("/api/partner/services", partner_services)
    app.router.add_get("/api/partner/notifications", partner_notifications)
    app.router.add_post("/api/partner/notifications/{id}/read", partner_mark_notification_read)
    app.router.add_get("/api/client/notifications", client_notifications)
    app.router.add_get("/api/client/negotiations", client_negotiations)
    app.router.add_post("/api/client/notifications/{id}/read", client_mark_notification_read)
    app.router.add_post("/api/partner/negotiations/{id}/decision", partner_negotiation_decision)
    app.router.add_get("/api/partner/negotiations", partner_negotiations)
    app.router.add_get("/api/negotiations/{id}", negotiation_get)
    app.router.add_post("/api/negotiations/{id}/messages", negotiation_message)
    app.router.add_post("/api/partner/services/confirm", partner_service_confirm)
    app.router.add_post("/api/client/search", client_search)
    app.router.add_post("/api/client/services/{service_id}/select", client_select_service)
    app.router.add_post("/api/client/negotiations/{id}/agree", client_agree)
    app.router.add_post("/api/negotiations/{id}/terms", negotiation_terms)
    app.router.add_get("/api/negotiations/{id}/booking", booking_by_negotiation)
    app.router.add_post("/api/client/negotiations/{id}/booking", client_booking)
    app.router.add_post("/api/client/bookings/{id}/pay", booking_payment)
    app.router.add_post("/api/client/bookings/{id}/review", client_review)
    app.router.add_post("/api/partner/bookings/{id}/confirm", partner_booking_confirm)
    app.router.add_post("/api/partner/bookings/{id}/complete", partner_complete)
    app.router.add_post("/api/partner/bookings/checkin", partner_checkin)
    app.router.add_post("/api/admin/bookings/{id}/test-payment", admin_test_payment)
    app.router.add_post("/api/admin/ai", admin_ai)
    app.router.add_get("/api/admin/applications", applications)
    app.router.add_get("/api/admin/services", admin_services)
    app.router.add_post("/api/admin/services/{id}/decision", admin_service_decision)
