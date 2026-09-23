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

from config import BOT_TOKEN
from database import _connect
from telegram_webapp_auth import validate_telegram_webapp_init_data

_PENDING: dict[str, tuple[float, int, dict[str, Any]]] = {}
_PENDING_TTL = 10 * 60


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
show_businesses, show_services, show_orders,
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
            "intent":{"type":"string","enum":["show_businesses","show_services","show_orders","add_business","update_business","delete_business","add_address","update_address","delete_address","add_service","update_service","delete_service","clarify"]},
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
    prompt = json.dumps({"message":message,"language":language,"context":context}, ensure_ascii=False)
    resp = await client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=900,
        response_format={"type":"json_object"},
        messages=[
            {"role":"system","content":system},
            {"role":"user","content":prompt}
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
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
    if ident is None: return ""
    for x in ctx.get(key,[]):
        if int(x["id"]) == int(ident):
            return x.get("name") or x.get("object_name") or ""
    return ""


async def api_ai_command(request: web.Request):
    uid=_auth(request); pid=_partner(uid)
    data=await request.json()
    message=str(data.get("message") or "").strip()
    language=str(data.get("language") or "hy")
    if language not in {"hy","ru","en"}: language="hy"
    if not message:
        return web.json_response({"ok":False,"error":"message_required"},status=400)
    ctx=_context(pid)
    try:
        command=await _ai_json(message,language,ctx)
    except Exception as exc:
        return web.json_response({"ok":False,"error":"ai_command_failed","message":str(exc)[:240]},status=503)
    command["language"]=language
    command["message"]=message
    intent=command.get("intent")
    if intent in {"show_businesses","show_services","show_orders"}:
        return await _execute_read(pid,command,ctx)
    if intent=="clarify" or not intent:
        return web.json_response({"ok":True,"reply":str(command.get("reply") or "Пожалуйста, уточните запрос."),"command":command})
    if command.get("needs_confirmation",True):
        token=_pending_add(pid,command)
        return web.json_response({"ok":True,"reply":str(command.get("reply") or _preview(command,ctx,language)),"confirmation_id":token,"command":command})
    return await _execute_mutation(pid,command,ctx)


def _preview(c,ctx,lang):
    intent=c.get("intent","")
    names={ "add_service":"Добавить услугу", "update_service":"Изменить услугу", "delete_service":"Удалить услугу", "add_business":"Добавить компанию", "update_business":"Изменить компанию", "delete_business":"Удалить компанию", "add_address":"Добавить адрес", "update_address":"Изменить адрес", "delete_address":"Удалить адрес" }
    return names.get(intent,intent)


async def _execute_read(pid,c,ctx):
    intent=c.get("intent")
    if intent=="show_businesses":
        lines=["🏢 "+str(x.get("name") or "") for x in ctx["businesses"]]
        return web.json_response({"ok":True,"reply":"\n".join(lines) or "Компаний пока нет.","data":{"businesses":ctx["businesses"]}})
    if intent=="show_services":
        lines=["🛠 %s — %s ֏" % (x.get("name") or "", x.get("price") if x.get("price") is not None else "—") for x in ctx["services"]]
        return web.json_response({"ok":True,"reply":"\n".join(lines) or "Услуг пока нет.","data":{"services":ctx["services"]}})
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,business_id,status,created_at FROM bookings WHERE partner_id=%s ORDER BY id DESC LIMIT 50", (pid,))
            rows=[dict(x) for x in cur.fetchall()]
    return web.json_response({"ok":True,"reply":"\n".join("📥 #%s — %s" % (x["id"],x.get("status") or "—") for x in rows) or "Заказов пока нет.","data":{"orders":rows}})


async def _execute_mutation(pid,c,ctx):
    intent=c.get("intent")
    bid=int(c["business_id"]) if c.get("business_id") else None
    oid=int(c["object_id"]) if c.get("object_id") else None
    sid=int(c["service_id"]) if c.get("service_id") else None
    name=str(c.get("name") or "").strip()
    description=str(c.get("description") or "").strip()
    phone=str(c.get("phone") or "").strip() or None
    price=_clean_num(c.get("price"))
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
