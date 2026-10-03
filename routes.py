import re
import json
from aiohttp import web

from auth import user, require_admin
from data import partners
from data.core import active_services
from ai.manager import turn, partner_service_preview, partner_registration_preview
from ai.session import get as get_ai_session, save as save_ai_session, clear as clear_ai_session
from lifecycle.services import create_after_confirmation, activate_services_after_document
from lifecycle.bookings import create_booking, partner_confirm_booking, confirm_commission_payment, checkin, complete, submit_review
from db import run, exec
from data.core import audit, notify


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


async def partner_registration_ai(r):
    u = await user(r)
    try:
        body = await r.json()
    except Exception:
        return j({"ok": False, "error": "invalid_json"}, status=400)
    text = str(body.get("text", "")).strip()
    if not text:
        return j({"ok": False, "error": "text_required"}, status=400)

    normalized = " ".join(text.lower().replace("ё", "е").replace("։", " ").replace("՝", " ").split())
    state = get_ai_session(u["telegram_id"])
    pending = state.get("pending") if state else None
    confirmations = {"yes","confirm","confirmed","համաձայն եմ","համաձայն","այո","հաստատում եմ","հաստատել","подтверждаю","подтвердить","да","согласен","согласна"}
    if normalized in confirmations:
        if not isinstance(pending, dict) or pending.get("action") != "register_partner":
            return j({"ok": False, "error": "nothing_to_confirm"}, status=400)
        draft = pending.get("draft") or {}
        if pending.get("missing"):
            return j({"ok": True, "result": {"kind": "registration_missing", "draft": draft, "missing": pending["missing"]}})
        name = str(draft.get("name") or "").strip()
        phone = str(draft.get("phone") or "").strip()
        if not name or not re.fullmatch(r"[+0-9() .-]{7,30}", phone):
            return j({"ok": False, "error": "registration_data_incomplete"}, status=400)
        if partners.get(u["telegram_id"]):
            return j({"ok": True, "result": {"kind": "message", "answer": "Ваш бизнес уже зарегистрирован.", "destination": "/partner_cabinet.html"}})
        p, company = partners.register(u["telegram_id"], name, phone)
        if draft.get("marz") or draft.get("city") or draft.get("village") or draft.get("address"):
            exec(
                "INSERT INTO aig_addresses(company_id,marz,city,district,address,is_base) VALUES(%s,%s,%s,%s,%s,true)",
                (company["id"], draft.get("marz") or None, draft.get("city") or draft.get("village") or None, None, draft.get("address") or None),
            )
        clear_ai_session(u["telegram_id"])
        return j({"ok": True, "result": {"kind": "registered", "company": company, "destination": "/partner_cabinet.html"}})

    try:
        result = await partner_registration_preview(u["telegram_id"], text)
        return j({"ok": True, "result": result})
    except Exception:
        __import__("logging").exception("partner registration ai")
        return j({"ok": False, "error": "ai_request_failed"}, status=500)


async def partner_ai(r):
    u = await user(r)
    try:
        b = await r.json()
    except Exception:
        return j({"ok": False, "error": "invalid_json"}, status=400)
    text = str(b.get("text", "")).strip()
    if not text:
        return j({"ok": False, "error": "text_required"}, status=400)
    confirmation = str(b.get("confirm", "")).strip().lower()
    normalized_text = " ".join(text.lower().replace("ё", "е").replace("։", " ").replace("՝", " ").split())
    # Confirmation is a natural-language action. The partner must be able to
    # type "подтверждаю" / "подтверждаю заявку" / "да" instead of pressing
    # the UI button. Keep this as a deterministic fast path before Groq.
    confirmation_phrases = {
        "yes", "confirm", "confirmed", "համաձայն եմ", "համաձայն",
        "այո", "հաստատում եմ", "հաստատել", "подтверждаю",
        "подтверждаю заявку", "подтвердить", "подтверждаю отправку",
        "да", "согласен", "согласна",
    }
    is_text_confirmation = normalized_text in confirmation_phrases
    state = get_ai_session(u["telegram_id"])
    if confirmation in confirmation_phrases or is_text_confirmation:
        pending = state.get("pending") if state else None
        if not pending or pending.get("action") not in ("create_service", "create_services"):
            return j({"ok": False, "error": "nothing_to_confirm"}, status=400)
        if pending.get("missing_documents"):
            return j({"ok": False, "error": "direction_document_required", "missing_documents": pending["missing_documents"]}, status=409)
        if pending.get("services") and any(not s.get("classification") for s in pending["services"]):
            return j({"ok": False, "error": "service_classification_pending"}, status=409)
        result = create_after_confirmation(
            u["telegram_id"],
            pending["services"][0]["company_id"] if pending.get("services") else pending["service"]["company_id"],
            pending,
        )
        clear_ai_session(u["telegram_id"])
        return j({"ok": True, "kind": "created", **result})
    try:
        result = await partner_service_preview(u["telegram_id"], text)
        return j({"ok": True, "result": result})
    except ValueError as exc:
        return j({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        __import__("logging").exception("partner ai")
        return j({"ok": False, "error": "ai_request_failed"}, status=500)


async def partner_upload_direction_document(r):
    u = await user(r)
    partner = partners.get(u["telegram_id"])
    if not partner:
        return j({"ok": False, "error": "partner_registration_required"}, status=403)
    try:
        company_id = int(r.query.get("company_id"))
        direction_id = int(r.query.get("direction_category_id"))
    except (TypeError, ValueError):
        return j({"ok": False, "error": "company_and_direction_required"}, status=400)
    company = run(
        "SELECT id FROM aig_companies WHERE id=%s AND partner_id=%s AND archived=false",
        (company_id, partner["id"]),
    )
    if not company:
        return j({"ok": False, "error": "company_not_found"}, status=404)
    direction = run("SELECT id FROM aig_catalog_categories WHERE id=%s AND active=true", (direction_id,))
    if not direction:
        return j({"ok": False, "error": "direction_not_found"}, status=404)

    reader = await r.multipart()
    field = await reader.next()
    if not field or field.name != "file":
        return j({"ok": False, "error": "file_required"}, status=400)
    filename = field.filename or "document"
    mime = (field.headers.get("Content-Type") or "").lower()
    allowed = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if mime not in allowed or ext not in ("pdf", "jpg", "jpeg", "png", "webp"):
        return j({"ok": False, "error": "unsupported_document_format"}, status=400)
    data = bytearray()
    while True:
        chunk = await field.read_chunk(1024 * 256)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 10 * 1024 * 1024:
            return j({"ok": False, "error": "document_too_large"}, status=400)
    if not data:
        return j({"ok": False, "error": "empty_document"}, status=400)

    row = run(
        "INSERT INTO aig_direction_documents(company_id,catalog_category_id,file_name,mime_type,size_bytes,content,status,uploaded_by) "
        "VALUES(%s,%s,%s,%s,%s,%s,'ACTIVE',%s) RETURNING id,file_name,size_bytes,status",
        (company_id, direction_id, filename, mime, len(data), bytes(data), u["telegram_id"]),
    )
    activate_services_after_document(company_id, direction_id, u["telegram_id"])
    # The preview session may still contain the old missing-document gate. Remove only this direction so confirmation can continue.
    state = get_ai_session(u["telegram_id"])
    pending = state.get("pending") if state else None
    if isinstance(pending, dict):
        missing = [d for d in (pending.get("missing_documents") or []) if int(d.get("id", -1)) != direction_id]
        pending["missing_documents"] = missing
        pending["action"] = "create_services"
        save_ai_session(u["telegram_id"], state.get("context") or "PARTNER", pending)
    audit(u["telegram_id"], "direction_document_uploaded", "direction", direction_id, {"document_id": row["id"], "company_id": company_id})
    # Return the saved AI preview so the partner can continue with the same
    # request after uploading the document; no re-entry of the service text.
    pending_preview = None
    if isinstance(pending, dict):
        pending_preview = {
            "kind": "preview",
            "services": pending.get("services") or [],
            "missing_documents": pending.get("missing_documents") or [],
            "can_submit_to_admin": not (pending.get("missing_documents") or [])
                and all(s.get("classification") for s in (pending.get("services") or [])),
        }
    return j({"ok": True, "document": row, "direction_id": direction_id, "result": pending_preview})


async def admin_direction_document(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    doc_id = int(r.match_info["id"])
    row = run("SELECT file_name,mime_type,content FROM aig_direction_documents WHERE id=%s", (doc_id,))
    if not row:
        raise web.HTTPNotFound()
    return web.Response(body=bytes(row["content"]), content_type=row["mime_type"], headers={
        "Content-Disposition": f'inline; filename="{row["file_name"].replace(chr(34), "")}"'
    })
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
    state = get_ai_session(u["telegram_id"])
    pending = state.get("pending") if state else None
    if not pending or pending.get("action") not in ("create_service", "create_services"):
        return j({"ok": False, "error": "nothing_to_confirm"}, status=400)
    services = pending.get("services") or [pending.get("service")]
    result = create_after_confirmation(u["telegram_id"], services[0]["company_id"], pending)
    clear_ai_session(u["telegram_id"])
    return j({"ok": True, "kind": "created", **result})

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
        """SELECT s.id,s.name,s.description,s.price_type,s.price_amd,s.hours,s.at_client,s.territory,
                  s.marzes,s.cities,s.districts,s.status,s.catalog_category_id,s.direction_category_id,
                  c.name AS company_name,p.phone,
                  a.marz,a.city,a.district,a.address,
                  app.id AS application_id,app.status AS application_status,app.reason,
                  d.id AS document_id,d.file_name AS document_name,d.status AS document_status
           FROM aig_services s
           JOIN aig_companies c ON c.id=s.company_id
           JOIN aig_partners p ON p.id=c.partner_id
           LEFT JOIN aig_addresses a ON a.id=s.address_id
           LEFT JOIN aig_service_applications app ON app.service_id=s.id
           LEFT JOIN LATERAL (
             SELECT id,file_name,status FROM aig_direction_documents
             WHERE company_id=s.company_id AND catalog_category_id=s.direction_category_id
             ORDER BY id DESC LIMIT 1
           ) d ON true
           WHERE s.status IN ('PENDING_ADMIN','NEEDS_CORRECTION')
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
    service = run(
        """SELECT s.*,c.partner_id,p.telegram_id AS partner_telegram_id
           FROM aig_services s JOIN aig_companies c ON c.id=s.company_id
           JOIN aig_partners p ON p.id=c.partner_id WHERE s.id=%s""",
        (service_id,),
    )
    if not service:
        return j({"ok": False, "error": "service_not_found"}, status=404)

    if action == "approve":
        if service.get("direction_category_id") and not run(
            "SELECT id FROM aig_direction_documents WHERE company_id=%s AND catalog_category_id=%s AND status='ACTIVE' LIMIT 1",
            (service["company_id"], service["direction_category_id"]),
        ):
            return j({"ok": False, "error": "direction_document_required"}, status=409)
        exec("UPDATE aig_services SET status='ACTIVE',rejection_reason=NULL,updated_at=now() WHERE id=%s", (service_id,))
        exec(
            "UPDATE aig_service_applications SET status='APPROVED',reviewed_at=now(),reviewer=%s,reason=NULL "
            "WHERE service_id=%s",
            (u["telegram_id"], service_id),
        )
        audit(u["telegram_id"], "service_activated", "service", service_id, {})
        return j({"ok": True, "service_id": service_id, "status": "ACTIVE"})

    if action in ("delete", "archive"):
        exec("UPDATE aig_services SET status='ARCHIVED',updated_at=now() WHERE id=%s", (service_id,))
        exec("UPDATE aig_service_applications SET status='DELETED',reviewed_at=now(),reviewer=%s WHERE service_id=%s", (u["telegram_id"], service_id))
        audit(u["telegram_id"], "service_deleted", "service", service_id, {})
        return j({"ok": True, "service_id": service_id, "status": "ARCHIVED"})

    if action in ("correction", "send_back", "return"):
        reason = str(body.get("reason", "")).strip()
        if not reason:
            return j({"ok": False, "error": "correction_reason_required"}, status=400)
        exec("UPDATE aig_services SET status='NEEDS_CORRECTION',rejection_reason=%s,updated_at=now() WHERE id=%s", (reason, service_id))
        exec(
            "UPDATE aig_service_applications SET status='NEEDS_CORRECTION',reviewed_at=now(),reviewer=%s,reason=%s WHERE service_id=%s",
            (u["telegram_id"], reason, service_id),
        )
        notify(
            service["partner_telegram_id"],
            "service_correction_required",
            {"service_id": service_id, "reason": reason},
        )
        audit(u["telegram_id"], "service_sent_for_correction", "service", service_id, {"reason": reason})
        return j({"ok": True, "service_id": service_id, "status": "NEEDS_CORRECTION", "reason": reason})

    if action == "edit":
        fields = {
            "name": body.get("name"),
            "description": body.get("description"),
            "price_type": body.get("price_type"),
            "price_amd": body.get("price_amd"),
            "hours": body.get("hours"),
            "at_client": body.get("at_client"),
            "territory": body.get("territory"),
        }
        updates, values = [], []
        for key, value in fields.items():
            if value is not None:
                updates.append(f"{key}=%s")
                values.append(value)
        if not updates:
            return j({"ok": False, "error": "no_edit_fields"}, status=400)
        values.append(service_id)
        exec("UPDATE aig_services SET " + ",".join(updates) + ",updated_at=now() WHERE id=%s", tuple(values))
        audit(u["telegram_id"], "service_edited", "service", service_id, fields)
        return j({"ok": True, "service_id": service_id, "status": "PENDING_ADMIN"})

    return j({"ok": False, "error": "invalid_action"}, status=400)


async def applications(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    return j({
        "ok": True,
        "items": run(
            """SELECT a.*,s.name AS service_name,s.description,s.price_type,s.price_amd,s.hours,s.at_client,
                      s.territory,s.marzes,s.cities,s.districts,s.status AS service_status,
                      s.catalog_category_id,s.direction_category_id,
                      c.name AS company_name,p.phone,
                      ad.marz,ad.city,ad.district,ad.address,
                      d.id AS document_id,d.file_name AS document_name,d.status AS document_status
               FROM aig_service_applications a
               JOIN aig_services s ON s.id=a.service_id
               JOIN aig_companies c ON c.id=s.company_id
               JOIN aig_partners p ON p.id=c.partner_id
               LEFT JOIN aig_addresses ad ON ad.id=s.address_id
               LEFT JOIN LATERAL (
                 SELECT id,file_name,status FROM aig_direction_documents
                 WHERE company_id=s.company_id AND catalog_category_id=s.direction_category_id
                 ORDER BY id DESC LIMIT 1
               ) d ON true
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

async def admin_catalog(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    rows = run(
        """SELECT m.id,m.name_am,m.name_ru,m.name_en,m.slug,m.is_active,
                  m.commission_type,m.commission_value,m.verification_required,
                  m.verification_document_types,
                  root.id AS catalog_id
           FROM master_categories m
           LEFT JOIN aig_catalog_categories root
             ON root.slug=m.slug AND root.parent_id IS NULL
           ORDER BY m.id""",
        many=True,
    )
    children = run(
        """SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,
                  c.is_active,c.commission_type,c.commission_value,
                  cat.id AS catalog_id
           FROM categories c
           LEFT JOIN aig_catalog_categories cat ON cat.slug=c.slug
           ORDER BY c.master_category_id,c.id""",
        many=True,
    )
    by_master = {}
    for child in children:
        by_master.setdefault(child["master_category_id"], []).append(child)
    for direction in rows:
        direction["subdirections"] = by_master.get(direction["id"], [])
    return j({"ok": True, "directions": rows, "direction_count": len(rows),
              "subdirection_count": len(children)})


async def admin_catalog_direction_update(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    direction_id = int(r.match_info["id"])
    body = await r.json()
    current = run("SELECT * FROM master_categories WHERE id=%s", (direction_id,))
    if not current:
        return j({"ok": False, "error": "direction_not_found"}, status=404)

    allowed = {
        "is_active": body.get("is_active"),
        "commission_type": body.get("commission_type"),
        "commission_value": body.get("commission_value"),
        "verification_required": body.get("verification_required"),
        "verification_document_types": body.get("verification_document_types"),
    }
    updates, values = [], []
    for key, value in allowed.items():
        if value is not None:
            updates.append(f"{key}=%s")
            values.append(json.dumps(value, ensure_ascii=False) if key == "verification_document_types" else value)
    if not updates:
        return j({"ok": False, "error": "no_edit_fields"}, status=400)
    values.append(direction_id)
    exec("UPDATE master_categories SET " + ",".join(updates) + " WHERE id=%s", tuple(values))

    if "is_active" in allowed and allowed["is_active"] is not None:
        exec(
            "UPDATE aig_catalog_categories SET active=%s WHERE parent_id IS NULL "
            "AND slug=(SELECT slug FROM master_categories WHERE id=%s)",
            (bool(allowed["is_active"]), direction_id),
        )
    audit(u["telegram_id"], "catalog_direction_updated", "master_category", direction_id, body)
    return j({"ok": True, "direction_id": direction_id})


async def admin_catalog_subdirection_update(r):
    u = await user(r)
    require_admin(u["telegram_id"])
    sub_id = int(r.match_info["id"])
    body = await r.json()
    current = run("SELECT * FROM categories WHERE id=%s", (sub_id,))
    if not current:
        return j({"ok": False, "error": "subdirection_not_found"}, status=404)
    allowed = {
        "is_active": body.get("is_active"),
        "commission_type": body.get("commission_type"),
        "commission_value": body.get("commission_value"),
    }
    updates, values = [], []
    for key, value in allowed.items():
        if value is not None:
            updates.append(f"{key}=%s")
            values.append(value)
    if not updates:
        return j({"ok": False, "error": "no_edit_fields"}, status=400)
    values.append(sub_id)
    exec("UPDATE categories SET " + ",".join(updates) + " WHERE id=%s", tuple(values))
    if "is_active" in allowed and allowed["is_active"] is not None:
        exec(
            "UPDATE aig_catalog_categories SET active=%s WHERE slug=(SELECT slug FROM categories WHERE id=%s)",
            (bool(allowed["is_active"]), sub_id),
        )
    audit(u["telegram_id"], "catalog_subdirection_updated", "category", sub_id, body)
    return j({"ok": True, "subdirection_id": sub_id})


def setup_routes(app):
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/partner/register", register)
    app.router.add_post("/api/partner/ai", partner_ai)
    app.router.add_post("/api/partner/registration-ai", partner_registration_ai)
    app.router.add_post("/api/partner/direction-document", partner_upload_direction_document)
    app.router.add_get("/api/admin/direction-documents/{id}", admin_direction_document)
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
    app.router.add_get("/api/admin/catalog", admin_catalog)
    app.router.add_post("/api/admin/catalog/directions/{id}", admin_catalog_direction_update)
    app.router.add_post("/api/admin/catalog/subdirections/{id}", admin_catalog_subdirection_update)
    app.router.add_get("/api/admin/services", admin_services)
    app.router.add_post("/api/admin/services/{id}/decision", admin_service_decision)
