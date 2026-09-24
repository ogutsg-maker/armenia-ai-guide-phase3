"""Universal AI assistant for the partner cabinet.

The assistant is intentionally an interpreter, not a database agent:
Groq returns a strict intent, Python validates ownership and performs the
actual database mutation. Destructive/change actions require confirmation.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

from aiohttp import web
from notify import notify

from config import BOT_TOKEN
from database import _connect
from telegram_webapp_auth import validate_telegram_webapp_init_data

_PENDING: dict[str, tuple[float, int, dict[str, Any]]] = {}
_PENDING_TTL = 10 * 60


def _json_safe(value: Any):
    """Convert DB values to JSON-safe primitives before aiohttp serializes them."""
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _json_response(payload, *args, **kwargs):
    return web.json_response(_json_safe(payload), *args, **kwargs)


def _auth(request: web.Request) -> int:
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": "telegram_init_data_required"}), content_type="application/json")
    try:
        data = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
        return int(data["id"])
    except Exception as exc:
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}), content_type="application/json")


def _partner(uid: int) -> int:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM partners WHERE user_id=%s", (uid,))
            row = cur.fetchone()
    if not row:
        raise web.HTTPNotFound(text=json.dumps({"ok": False, "error": "partner_not_found"}), content_type="application/json")
    return int(row["id"])


def _groq_client():
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    try:
        from groq import AsyncGroq
        return AsyncGroq(api_key=key)
    except Exception:
        return None


async def _ai_json(message: str, language: str, context: dict[str, Any]) -> dict[str, Any]:
    client = _groq_client()
    if client is None:
        return {"intent": "clarify", "reply": {"hy": "AI ծառայությունը ժամանակավորապես հասանելի չէ։", "ru": "AI сейчас недоступен.", "en": "AI is temporarily unavailable."}.get(language, "AI is temporarily unavailable.")}
    model = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-20b"
    system = """You are the universal right-hand assistant inside Armenia AI Guide partner cabinet.
Understand Armenian, Russian and English. The partner speaks naturally; never force menus.
Return ONLY JSON matching the schema.
Allowed intents:
show_businesses, show_services, show_orders, show_profile, show_addresses, show_documents,
add_business, update_business, delete_business,
add_address, update_address, delete_address,
add_service, update_service, delete_service,
clarify.
Never invent IDs. Use only IDs from CONTEXT. If an operation changes or deletes data,
set needs_confirmation=true. Read-only intents do not need confirmation.
For add_service extract name, price, description, business_id, object_id when clearly known.
For update/delete identify an existing entity by id only when the context supports it.
If ambiguous, use clarify and ask one concise question.
"""
    schema = {
        "type":"object",
        "properties":{
            "intent":{"type":"string","enum":["show_businesses","show_services","show_orders","show_profile","show_addresses","show_documents","add_business","update_business","delete_business","add_address","update_address","delete_address","add_service","update_service","delete_service","clarify"]},
            "reply":{"type":"string"},
            "needs_confirmation":{"type":"boolean"},
            "business_id":{"type":["integer","null"]},
            "object_id":{"type":["integer","null"]},
            "service_id":{"type":["integer","null"]},
            "name":{"type":["string","null"]},
            "description":{"type":["string","null"]},
            "phone":{"type":["string","null"]},
            "city":{"type":["string","null"]},
            "marz":{"type":["string","null"]},
            "address":{"type":["string","null"]},
            "price":{"type":["number","null"]},
            "contact_phone":{"type":["string","null"]},
            "reason":{"type":["string","null"]}
        },
        "required":["intent","reply","needs_confirmation","business_id","object_id","service_id","name","description","phone","city","marz","address","price","contact_phone","reason"],
        "additionalProperties":False
    }
    prompt = json.dumps({"message":message,"language":language,"context":context}, ensure_ascii=False, default=str)
    request_kwargs = dict(
        temperature=0,
        max_tokens=900,
        response_format={"type":"json_object"},
        messages=[
            {"role":"system","content":system},
            {"role":"user","content":prompt}
        ],
    )
    try:
        resp = await client.chat.completions.create(model=model, **request_kwargs)
    except Exception as first_exc:
        # Groq model IDs change over time. If Render still has a retired
        # GROQ_MODEL (for example llama-3.1-8b-instant), retry once with the
        # currently supported GPT-OSS 20B model.
        if model != "openai/gpt-oss-20b" and ("404" in str(first_exc) or "model" in str(first_exc).lower()):
            resp = await client.chat.completions.create(model="openai/gpt-oss-20b", **request_kwargs)
        else:
            raise
    raw = resp.choices[0].message.content or ""
    raw = raw.strip()
    if raw.startswith("\`\`\`"):
        raw = re.sub(r"^\`\`\`(?:json)?\\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\\s*\`\`\`$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                data = None
        else:
            data = None
    if not isinstance(data, dict):
        repair = await client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=700,
            response_format={"type":"json_object"},
            messages=[
                {"role":"system","content":"Return ONLY one valid JSON object. No markdown, no explanation."},
                {"role":"user","content":"Convert this failed output into a valid JSON object matching the requested assistant schema: " + raw[:5000]}
            ],
        )
        repaired = (repair.choices[0].message.content or "").strip()
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_ai_response") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid_ai_response")
    return data


def _context(pid: int) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,description,phone,status FROM partner_businesses WHERE partner_id=%s AND status='active' ORDER BY id", (pid,))
            businesses = [dict(x) for x in cur.fetchall()]
            cur.execute("""SELECT id,business_id,object_name,address,city,marz,phone
                           FROM partner_objects
                           WHERE partner_id=%s AND COALESCE(is_active,TRUE)=TRUE
                           ORDER BY business_id,id""", (pid,))
            objects = [dict(x) for x in cur.fetchall()]
            cur.execute("""SELECT id,business_id,name,description,price
                           FROM services
                           WHERE partner_id=%s AND (status IS NULL OR status <> 'deleted')
                           ORDER BY business_id,id DESC""", (pid,))
            services = [dict(x) for x in cur.fetchall()]
    return {"businesses":businesses,"addresses":objects,"services":services}


def _clean_num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _pending_add(pid: int, command: dict[str, Any]) -> str:
    token = uuid.uuid4().hex
    _PENDING[token] = (time.time(), pid, command)
    return token


def _get_pending(token: str, pid: int) -> dict[str, Any]:
    now=time.time()
    for k,(ts,_,_) in list(_PENDING.items()):
        if now-ts > _PENDING_TTL:
            _PENDING.pop(k,None)
    item=_PENDING.get(token)
    if not item or item[1] != pid:
        raise web.HTTPNotFound(text=json.dumps({"ok":False,"error":"confirmation_expired"}),content_type="application/json")
    return item[2]


def _entity_name(ctx, key, ident):
    if ident is None:
        return ""
    for x in ctx.get(key, []):
        if int(x["id"]) != int(ident):
            continue
        if key == "addresses":
            label = str(x.get("object_name") or "").strip()
            parts = [
                str(x.get("address") or "").strip(),
                str(x.get("city") or "").strip(),
                str(x.get("marz") or "").strip(),
            ]
            location = ", ".join(p for p in parts if p)
            if label and location:
                return f"{label} — {location}"
            return label or location
        return x.get("name") or x.get("object_name") or ""
    return ""


def _address_label(ctx, ident):
    """Human-readable address label; never expose the internal object ID."""
    label = _entity_name(ctx, "addresses", ident)
    if label:
        return label
    for x in ctx.get("addresses", []):
        if str(x.get("id")) == str(ident):
            parts = [
                str(x.get("address") or "").strip(),
                str(x.get("city") or "").strip(),
                str(x.get("marz") or "").strip(),
            ]
            return ", ".join(p for p in parts if p)
    return "Адрес не указан"


async def api_ai_command(request: web.Request):
    uid=_auth(request); pid=_partner(uid)
    data=await request.json()
    message=str(data.get("message") or "").strip()
    language=str(data.get("language") or "hy")
    if language not in {"hy","ru","en"}: language="hy"
    if not message:
        return web.json_response({"ok":False,"error":"message_required"},status=400)
    ctx=_context(pid)

    # Natural-language confirmation: if the partner answers "да / yes / այո"
    # after a pending action, treat it exactly like pressing the Confirm button.
    normalized = re.sub(r"[\s.!?,;:]+", " ", message.lower()).strip()
    if normalized in {
        "да", "да да", "da", "yes", "y", "ok", "okay", "confirm", "confirmed",
        "подтверждаю", "подтвердить", "согласен", "согласна",
        "այո", "հա", "հաստատում եմ", "հաստատել"
    }:
        pending = [
            (ts, token, cmd)
            for token, (ts, owner_pid, cmd) in _PENDING.items()
            if owner_pid == pid and time.time() - ts <= _PENDING_TTL
        ]
        if pending:
            _, token, command = max(pending, key=lambda item: item[0])
            _PENDING.pop(token, None)
            # Re-resolve the pending entity against the latest live context.
            # The original AI response may have omitted service_id/name.
            pending_intent = command.get("intent")
            if pending_intent in {"update_service", "delete_service"}:
                pending_name = re.sub(r"\s+", " ", str(command.get("name") or "").casefold()).strip()
                if not pending_name:
                    pending_name = re.sub(r"\s+", " ", str(command.get("message") or "").casefold()).strip()
                candidates = []
                for svc in ctx.get("services", []):
                    svc_name = re.sub(r"\s+", " ", str(svc.get("name") or "").casefold()).strip()
                    if svc_name and svc_name in pending_name:
                        candidates.append(svc)
                if len(candidates) == 1:
                    command["service_id"] = int(candidates[0]["id"])
                    command["business_id"] = int(candidates[0]["business_id"])
                    command["name"] = candidates[0].get("name")
            result = await _execute_mutation(pid, command, ctx)
            if isinstance(result, web.Response):
                # Rebuild the partner context after every confirmed mutation.
                # The next AI turn therefore always sees fresh business data.
                try:
                    fresh_ctx = _context(pid)
                    result.headers["X-AI-Context-Refreshed"] = "1"
                    result.headers["X-AI-Context-Version"] = "live"
                    result.headers["X-AI-Context-Entities"] = str(
                        len(fresh_ctx.get("businesses", []))
                        + len(fresh_ctx.get("services", []))
                        + len(fresh_ctx.get("addresses", []))
                    )
                except Exception:
                    pass
                return result

    # Natural-language cancellation of the last pending action.
    if normalized in {
        "нет", "no", "n", "cancel", "отмена", "отменить",
        "не надо", "не делай", "ոչ", "ոչ, պետք չէ", "չեղարկել"
    }:
        pending = [
            (ts, token)
            for token, (ts, owner_pid, _) in _PENDING.items()
            if owner_pid == pid and time.time() - ts <= _PENDING_TTL
        ]
        if pending:
            _, token = max(pending, key=lambda item: item[0])
            _PENDING.pop(token, None)
            return web.json_response({
                "ok": True,
                "reply": {"hy": "Գործողությունը չեղարկվեց։", "ru": "Действие отменено.", "en": "Action cancelled."}.get(language, "Action cancelled.")
            })

    try:
        command=await _ai_json(message,language,ctx)
    except Exception as exc:
        return web.json_response({"ok":False,"error":"ai_command_failed","message":str(exc)[:240]},status=503)
    command["language"]=language
    command["message"]=message
    intent=command.get("intent")

    # Resolve an existing service from the live partner context before mutation.
    # AI still decides the intent; Python only verifies the target entity.
    # The message itself is also used as a deterministic entity hint. This is
    # important for short requests such as "Удали педикюр": the model may return
    # delete_service without a name, while the live context contains exactly one
    # matching service.
    if intent in {"update_service", "delete_service"}:
        wanted = re.sub(r"\s+", " ", str(command.get("name") or "").casefold()).strip()
        if not wanted:
            msg_norm = re.sub(r"\s+", " ", message.casefold()).strip()
            matches_from_message = []
            for svc in ctx.get("services", []):
                service_name = re.sub(r"\s+", " ", str(svc.get("name") or "").casefold()).strip()
                if service_name and service_name in msg_norm:
                    matches_from_message.append(svc)
            if len(matches_from_message) == 1:
                wanted = re.sub(r"\s+", " ", str(matches_from_message[0].get("name") or "").casefold()).strip()
                command["name"] = matches_from_message[0].get("name")
        if wanted and not command.get("service_id"):
            candidates = []
            for svc in ctx.get("services", []):
                service_name = re.sub(r"\s+", " ", str(svc.get("name") or "").casefold()).strip()
                if service_name and (service_name == wanted or wanted in service_name or service_name in wanted):
                    candidates.append(svc)
            if len(candidates) == 1:
                command["service_id"] = int(candidates[0]["id"])
                command["business_id"] = int(candidates[0]["business_id"])
        if command.get("service_id"):
            svc = next((x for x in ctx.get("services", []) if int(x.get("id")) == int(command["service_id"])), None)
            if svc:
                command["business_id"] = int(svc["business_id"])
                command["name"] = svc.get("name") or command.get("name")

    # Resolve an explicitly named company deterministically. Do not depend on
    # Groq returning business_id when the partner has already named the company
    # in natural language (for example: "для моей компании BYUTI").
    if intent == "add_service" and not command.get("business_id"):
        msg_norm = re.sub(r"\s+", " ", message.casefold()).strip()
        businesses = ctx.get("businesses", [])
        exact = [
            b for b in businesses
            if str(b.get("name") or "").casefold().strip()
            and str(b.get("name") or "").casefold().strip() in msg_norm
        ]
        if len(exact) == 1:
            command["business_id"] = int(exact[0]["id"])
        elif len(businesses) == 1:
            command["business_id"] = int(businesses[0]["id"])
    if intent in {"show_businesses","show_services","show_orders","show_profile","show_addresses","show_documents"}:
        return await _execute_read(pid,command,ctx)
    if intent=="clarify" or not intent:
        return web.json_response({"ok":True,"reply":str(command.get("reply") or "Пожалуйста, уточните запрос."),"command":command})
    if intent not in {"show_businesses","show_services","show_orders","clarify"}:
        token=_pending_add(pid,command)
        return web.json_response({"ok":True,"reply":str(command.get("reply") or _preview(command,ctx,language)),"confirmation_id":token,"command":command})
    return await _execute_mutation(pid,command,ctx)


def _preview(c,ctx,lang):
    intent=c.get("intent","")
    names={
        "add_service":"Добавить услугу",
        "update_service":"Изменить услугу",
        "delete_service":"Удалить услугу",
        "add_business":"Добавить компанию",
        "update_business":"Изменить компанию",
        "delete_business":"Удалить компанию",
        "add_address":"Добавить адрес",
        "update_address":"Изменить адрес",
        "delete_address":"Удалить адрес",
    }
    title=names.get(intent,intent)
    details=[]
    if c.get("name"):
        details.append("🛠 "+str(c["name"]))
    if c.get("price") is not None:
        details.append("💰 "+str(c["price"])+" ֏")
    if c.get("business_id"):
        details.append("🏢 "+(_entity_name(ctx,"businesses",c.get("business_id")) or str(c["business_id"])))
    if c.get("object_id"):
        details.append("📍 "+_address_label(ctx,c.get("object_id")))
    return title + (":\n" + "\n".join(details) if details else "")


async def _execute_read(pid,c,ctx):
    intent=c.get("intent")
    if intent=="show_businesses":
        lines=["🏢 "+str(x.get("name") or "") for x in ctx["businesses"]]
        return web.json_response({"ok":True,"reply":"\n".join(lines) or "Компаний пока нет.","data":{"businesses":ctx["businesses"]}})
    if intent=="show_profile":
        return _json_response({"ok":True,"reply":"Պրոֆիլը հասանելի է։","data":{"context":ctx.get("operational_context",""),"businesses":ctx.get("businesses",[]),"addresses":ctx.get("addresses",[]),"services":ctx.get("services",[])}})
    if intent=="show_addresses":
        lines=["📍 "+str(x.get("object_name") or x.get("address") or "—") for x in ctx["addresses"]]
        return _json_response({"ok":True,"reply":"\n".join(lines) or "Հասցեներ դեռ չկան։","data":{"addresses":ctx["addresses"]}})
    if intent=="show_documents":
        # Document records are intentionally fetched only when requested.
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,document_type,status,verification_status,original_filename,created_at,updated_at
                               FROM partner_verification_documents WHERE partner_id=%s ORDER BY id DESC LIMIT 50""",(pid,))
                rows=[dict(x) for x in cur.fetchall()]
        return _json_response({"ok":True,"reply":"\n".join("📄 #%s — %s — %s" % (x.get("id"),x.get("document_type") or "document",x.get("status") or x.get("verification_status") or "—") for x in rows) or "Փաստաթղթեր դեռ չկան։","data":{"documents":rows}})
    if intent=="show_services":
        lines=["🛠 %s — %s ֏" % (x.get("name") or "", x.get("price") if x.get("price") is not None else "—") for x in ctx["services"]]
        return _json_response({"ok":True,"reply":"\n".join(lines) or "Услуг пока нет.","data":{"services":ctx["services"]}})
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,business_id,status,created_at FROM bookings WHERE partner_id=%s ORDER BY id DESC LIMIT 50", (pid,))
            rows=[dict(x) for x in cur.fetchall()]
    return web.json_response({"ok":True,"reply":"\n".join("📥 #%s — %s" % (x["id"],x.get("status") or "—") for x in rows) or "Заказов пока нет.","data":{"orders":rows}})



async def api_ai_command_confirm(request: web.Request):
    uid=_auth(request); pid=_partner(uid)
    data=await request.json()
    token=str(data.get("confirmation_id") or "").strip()
    if not token:
        return web.json_response({"ok":False,"error":"confirmation_id_required"},status=400)
    command=_get_pending(token,pid)
    _PENDING.pop(token,None)
    ctx=_context(pid)
    return await _execute_mutation(pid,command,ctx)

async def _execute_mutation(pid,c,ctx):
    intent=c.get("intent")
    bid=int(c["business_id"]) if c.get("business_id") else None
    oid=int(c["object_id"]) if c.get("object_id") else None
    sid=int(c["service_id"]) if c.get("service_id") else None
    name=str(c.get("name") or "").strip()
    description=str(c.get("description") or "").strip()
    phone=str(c.get("phone") or "").strip() or None
    price=_clean_num(c.get("price"))
    if intent=="add_service":
        if not bid:
            return web.json_response({"ok":True,"reply":"Укажите, в какой компании добавить услугу."})
        candidates=[x for x in ctx.get("addresses",[]) if int(x.get("business_id") or 0)==bid]
        if oid:
            selected = next((x for x in candidates if int(x["id"]) == oid), None)
            if selected is None:
                # If Groq returned a stale/mismatched object id, use the only
                # active address of the selected company when there is exactly one.
                if len(candidates) == 1:
                    oid=int(candidates[0]["id"])
                else:
                    return web.json_response({"ok":True,"reply":"Укажите адрес, где должна быть эта услуга."})
        elif len(candidates)==1:
            oid=int(candidates[0]["id"])
        else:
            return web.json_response({"ok":True,"reply":"Укажите адрес, где должна быть эта услуга."})
        if not name:
            return web.json_response({"ok":True,"reply":"Как называется услуга?"})
        from master_cabinet_api import _ai_match_new_service
        match=await _ai_match_new_service(pid,name,description,bid)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,name FROM partner_businesses WHERE id=%s AND partner_id=%s AND status='active'",(bid,pid))
                b=cur.fetchone()
                cur.execute("SELECT id,object_name,address,city,marz,phone FROM partner_objects WHERE id=%s AND partner_id=%s AND business_id=%s AND COALESCE(is_active,TRUE)=TRUE",(oid,pid,bid))
                o=cur.fetchone()
                if not b or not o:
                    return web.json_response({"ok":False,"error":"business_or_address_not_found"},status=404)
                cur.execute("""INSERT INTO partner_applications(
                    partner_id,business_id,status,business_name,location_marz,location_city,address,object_name,object_id,phone,
                    direction_name,master_category_id,subcategory_name,category_id,service_name,price,description,ai_reason,payload_json)
                    VALUES(%s,%s,'pending_admin',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id""",
                    (pid,bid,b["name"],o.get("marz"),o.get("city"),o.get("address"),o.get("object_name"),oid,
                     c.get("contact_phone") or o.get("phone"),match.get("direction_name") or match.get("out_of_scope_master_name") or "",
                     match.get("master_category_id") or match.get("out_of_scope_master_id"),match.get("subcategory_name") or match.get("proposed_name") or "",
                     match.get("category_id"),name,price,description,match.get("reason") or "AI assistant request.",json.dumps({"source":"partner_ai_assistant","message":c.get("message"),"object_id":oid},ensure_ascii=False)))
                aid=int(cur.fetchone()["id"])
            conn.commit()
        try:
            admin_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or 0)
            if admin_id:
                location = ", ".join(x for x in [o.get("marz"), o.get("city"), o.get("address")] if x)
                body = "Поступила новая заявка #" + str(aid) + " от " + str(b.get("name") or "бизнес") + ".\\n\\n🛠 " + name + " · " + str(price if price is not None else "—") + " ֏"
                if location: body += "\\n📍 " + location
                await notify(request.app, admin_id, title="📨 Новая заявка", body=body, kind="partner_application", audience="admin", data={"application_id":aid}, telegram_text="🤖 AI-секретарь\\n\\n" + body + "\\n\\nНапишите, что сделать с заявкой.")
        except Exception:
            pass

        return web.json_response({"ok":True,"reply":"✓ Услуга «%s» подготовлена и отправлена администратору на подтверждение. Заявка #%s."%(name,aid),"data":{"application_id":aid}})

    with _connect() as conn:
        with conn.cursor() as cur:
            if intent=="add_business":
                if len(name)<2: return web.json_response({"ok":True,"reply":"Укажите название компании."})
                cur.execute("INSERT INTO partner_businesses(partner_id,name,description,phone,status) VALUES(%s,%s,%s,%s,'active') RETURNING id,name",(pid,name,description or None,phone))
                row=cur.fetchone()
                conn.commit()
                return web.json_response({"ok":True,"reply":"✓ Компания «%s» создана."%row["name"],"data":{"business":dict(row)}})
            if intent in {"update_business","delete_business"}:
                cur.execute("SELECT id,name FROM partner_businesses WHERE id=%s AND partner_id=%s AND status='active'",(bid,pid)); b=cur.fetchone()
                if not b: return web.json_response({"ok":False,"error":"business_not_found"},status=404)
                if intent=="delete_business":
                    cur.execute("UPDATE partner_businesses SET status='archived',updated_at=NOW(),is_default=FALSE WHERE id=%s AND partner_id=%s",(bid,pid))
                    conn.commit(); return web.json_response({"ok":True,"reply":"✓ Компания «%s» удалена из активного списка."%b["name"]})
                fields=[]; vals=[]
                for k,v in (("name",name),("description",description),("phone",phone)):
                    if v not in ("",None): fields.append(k+"=%s"); vals.append(v)
                if fields: cur.execute("UPDATE partner_businesses SET "+",".join(fields)+",updated_at=NOW() WHERE id=%s AND partner_id=%s",(*vals,bid,pid))
                conn.commit(); return web.json_response({"ok":True,"reply":"✓ Данные компании обновлены."})
            if intent in {"add_address","update_address","delete_address"}:
                if intent=="add_address":
                    if not bid or len(name)<2: return web.json_response({"ok":True,"reply":"Укажите компанию и название адреса."})
                    cur.execute("SELECT id FROM partner_businesses WHERE id=%s AND partner_id=%s AND status='active'",(bid,pid))
                    if not cur.fetchone(): return web.json_response({"ok":False,"error":"business_not_found"},status=404)
                    cur.execute("""INSERT INTO partner_objects(partner_id,business_id,object_name,address,city,marz,phone)
                                   VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,object_name""",(pid,bid,name,c.get("address") or None,c.get("city") or None,c.get("marz") or None,phone))
                    row=cur.fetchone(); conn.commit()
                    return web.json_response({"ok":True,"reply":"✓ Адрес «%s» добавлен."%row["object_name"],"data":{"address":dict(row)}})
                cur.execute("SELECT id,object_name FROM partner_objects WHERE id=%s AND partner_id=%s AND business_id=%s AND COALESCE(is_active,TRUE)=TRUE",(oid,pid,bid))
                o=cur.fetchone()
                if not o: return web.json_response({"ok":False,"error":"address_not_found"},status=404)
                if intent=="delete_address":
                    cur.execute("UPDATE partner_objects SET is_active=FALSE WHERE id=%s AND partner_id=%s AND business_id=%s",(oid,pid,bid))
                    conn.commit(); return web.json_response({"ok":True,"reply":"✓ Адрес «%s» удалён."%o["object_name"]})
                sets=[]; vals=[]
                for k,v in (("object_name",name),("address",c.get("address")),("city",c.get("city")),("marz",c.get("marz")),("phone",phone)):
                    if v not in ("",None): sets.append(k+"=%s"); vals.append(v)
                if sets: cur.execute("UPDATE partner_objects SET "+",".join(sets)+" WHERE id=%s AND partner_id=%s AND business_id=%s",(*vals,oid,pid,bid))
                conn.commit(); return web.json_response({"ok":True,"reply":"✓ Адрес обновлён."})
            if intent in {"add_service","update_service","delete_service"}:
                if not bid: return web.json_response({"ok":True,"reply":"Укажите, в какой компании находится услуга."})
                cur.execute("SELECT id,name FROM partner_businesses WHERE id=%s AND partner_id=%s AND status='active'",(bid,pid))
                if not cur.fetchone(): return web.json_response({"ok":False,"error":"business_not_found"},status=404)
                if intent=="update_service":
                    cur.execute("SELECT id,name FROM services WHERE id=%s AND partner_id=%s AND business_id=%s AND (status IS NULL OR status<>'deleted')",(sid,pid,bid)); s=cur.fetchone()
                    if not s:return web.json_response({"ok":False,"error":"service_not_found"},status=404)
                    sets=[];vals=[]
                    for k,v in (("name",name),("description",description),("price",price)):
                        if v not in ("",None):sets.append(k+"=%s");vals.append(v)
                    if sets:cur.execute("UPDATE services SET "+",".join(sets)+",updated_at=NOW() WHERE id=%s AND partner_id=%s AND business_id=%s",(*vals,sid,pid,bid))
                    conn.commit();return web.json_response({"ok":True,"reply":"✓ Услуга обновлена."})
                if intent=="delete_service":
                    cur.execute("UPDATE services SET status='deleted',updated_at=NOW() WHERE id=%s AND partner_id=%s AND business_id=%s RETURNING id",(sid,pid,bid))
                    if not cur.fetchone():return web.json_response({"ok":False,"error":"service_not_found"},status=404)
                    conn.commit();return web.json_response({"ok":True,"reply":"✓ Услуга удалена."})
    return web.json_response({"ok":True,"reply":"Запрос принят."})


# Shared AI Context integration.
# Keep the existing partner-specific mutation/read contract, but source the
# conversational context from the central operational context layer.
def _context(pid: int) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id,name,description,phone,status FROM partner_businesses "
                "WHERE partner_id=%s AND status='active' ORDER BY id",
                (pid,),
            )
            businesses = [dict(x) for x in cur.fetchall()]
            cur.execute(
                """SELECT id,business_id,object_name,address,city,marz,phone
                   FROM partner_objects
                   WHERE partner_id=%s AND COALESCE(is_active,TRUE)=TRUE
                   ORDER BY business_id,id""",
                (pid,),
            )
            objects = [dict(x) for x in cur.fetchall()]
            cur.execute(
                """SELECT id,business_id,name,description,price,status,category_id
                   FROM services
                   WHERE partner_id=%s AND (status IS NULL OR status <> 'deleted')
                   ORDER BY business_id,id DESC""",
                (pid,),
            )
            services = [dict(x) for x in cur.fetchall()]

    from ai_context_layer import build_partner_context
    try:
        operational = build_partner_context(pid)
    except Exception:
        operational = ""

    return {
        "businesses": businesses,
        "addresses": objects,
        "services": services,
        "operational_context": operational,
        "permissions": {
            "role": "partner",
            "can_read_own_data": True,
            "can_modify_own_data": True,
            "confirmation_required_for_mutations": True,
        },
    }
