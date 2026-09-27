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
from ai_service import AIService
import data_core

from config import BOT_TOKEN
from telegram_webapp_auth import validate_telegram_webapp_init_data

_PENDING: dict[str, tuple[float, int, dict[str, Any]]] = {}
_PENDING_TTL = 10 * 60

# Ephemeral conversational state only; the database remains the source of truth.
_ACTIVE_BUSINESS: dict[int, tuple[float, int, str]] = {}
_ACTIVE_BUSINESS_TTL = 30 * 60

def _set_active_business(pid: int, business_id: int, name: str) -> None:
    _ACTIVE_BUSINESS[int(pid)] = (time.time(), int(business_id), str(name or ""))

def _get_active_business(pid: int, ctx: dict[str, Any]):
    item = _ACTIVE_BUSINESS.get(int(pid))
    if not item:
        return None
    ts, bid, _name = item
    if time.time() - ts > _ACTIVE_BUSINESS_TTL:
        _ACTIVE_BUSINESS.pop(int(pid), None)
        return None
    for b in ctx.get("businesses", []):
        if int(b.get("id") or 0) == bid and str(b.get("status") or "active") != "archived":
            return b
    _ACTIVE_BUSINESS.pop(int(pid), None)
    return None


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
    row = data_core.get_partner_by_user(int(uid))
    if not row:
        raise web.HTTPNotFound(text=json.dumps({"ok": False, "error": "partner_not_found"}), content_type="application/json")
    return int(row["id"])


def _shared_ai_service():
    """Use the application-wide provider gateway instead of a partner-only Groq client."""
    return AIService()

async def _ai_json(message: str, language: str, context: dict[str, Any]) -> dict[str, Any]:
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
For add_business, when the partner explicitly says "new company/new business/նոր ֆիրմա/նոր ընկերություն/новая фирма/новая компания", create add_business and extract the new company name from the same message when present.
For update/delete identify an existing entity by id only when the context supports it.
If ambiguous, use clarify and ask one concise question.
IMPORTANT LANGUAGE RULE: reply MUST be written entirely in the requested language. If language is "hy", use Armenian only (company/service names may remain as supplied). Never answer in English or Russian when language is "hy".
Return a JSON object with exactly these keys:
intent, reply, needs_confirmation, business_id, object_id, service_id, name, description, phone, city, marz, address, price, contact_phone, reason.
Use null for unknown scalar values. The current partner context is supplied in the user message.
"""
    prompt = json.dumps({"message":message,"language":language,"context":context}, ensure_ascii=False, default=str)
    try:
        return await _shared_ai_service().chat_json(system, prompt, max_tokens=900)
    except Exception:
        return {"intent": "clarify", "reply": {"hy": "AI ծառայությունը ժամանակավորապես հասանելի չէ։", "ru": "AI сейчас временно недоступен. Попробуйте ещё раз позже.", "en": "AI is temporarily unavailable. Please try again later."}.get(language, "AI is temporarily unavailable. Please try again later.")}

def _context(pid: int) -> dict[str, Any]:
    """Read the partner's live data through Data Core only."""
    businesses = data_core.list_companies(partner_id=int(pid), include_archived=False)
    addresses = data_core.get_partner_addresses(int(pid))
    services = data_core.list_services(partner_id=int(pid), limit=200)
    return {
        "businesses": businesses,
        "addresses": addresses,
        "services": services,
    }

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
    # Prefer the language of the actual message over a stale frontend selector.
    if re.search(r"[\u0531-\u058F]", message):
        language = "hy"
    elif re.search(r"[А-Яа-яЁё]", message):
        language = "ru"
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

            # Keep a confirmed add-service action alive when an address is still missing.
            if command.get("intent") == "add_service" and not command.get("object_id"):
                if command.get("_address_confirmation"):
                    address_text = str(command.get("_address_message") or "").strip()
                    bid = int(command.get("business_id") or 0)
                    actor_uid = int(command.get("actor_user_id") or uid)
                    parts = [p.strip() for p in address_text.split(",") if p.strip()]
                    city = parts[0] if len(parts) >= 2 else None
                    address = ", ".join(parts[1:]) if len(parts) >= 2 else address_text
                    created = data_core.create_partner_address(
                        partner_id=pid,
                        actor_user_id=actor_uid,
                        company_id=bid,
                        address=address,
                        city=city,
                        object_name=address_text,
                    )
                    command["object_id"] = int(created["id"])
                    command.pop("_address_confirmation", None)
                    command.pop("_address_message", None)
                else:
                    candidates = [
                        x for x in ctx.get("addresses", [])
                        if int(x.get("business_id") or 0) == int(command.get("business_id") or 0)
                    ]
                    if len(candidates) == 1:
                        command["object_id"] = int(candidates[0]["id"])
                    else:
                        reply = {
                            "hy": "Նշեք ծառայության հասցեն։ Օրինակ՝ «Գյումրի, Կենտրոնական 22»։",
                            "ru": "Укажите адрес услуги. Например: «Гюмրի, Центральная 22».",
                            "en": "Please provide the service address, for example: “Gyumri, Kentronakan 22”.",
                        }.get(language, "Նշեք ծառայության հասցեն։")
                        return web.json_response({
                            "ok": True,
                            "reply": reply,
                            "confirmation_id": token,
                            "command": command,
                        })

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
            result = await _execute_mutation(pid, command, ctx, uid)
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

    # Continue the same confirmed add-service action when the partner
    # supplies the missing address in the next message.
    if normalized not in {
        "да", "да да", "da", "yes", "y", "ok", "okay", "confirm", "confirmed",
        "подтверждаю", "подтвердить", "согласен", "согласна",
        "այո", "հա", "հաստատում եմ", "հաստատել",
        "нет", "no", "n", "cancel", "отмена", "отменить",
        "не надо", "не делай", "ոչ", "ոչ, պետք չէ", "չեղարկել"
    }:
        waiting = [
            (ts, token, cmd)
            for token, (ts, owner_pid, cmd) in _PENDING.items()
            if owner_pid == pid
            and time.time() - ts <= _PENDING_TTL
            and cmd.get("intent") == "add_service"
            and not cmd.get("object_id")
        ]
        if waiting:
            _, token, command = max(waiting, key=lambda item: item[0])
            command["actor_user_id"] = uid
            command["_address_message"] = message
            command["_address_confirmation"] = True
            company = _entity_name(ctx, "businesses", command.get("business_id")) or "ընկերությունը"
            reply = {
                "hy": f'Ավելացնել «{message}» հասցեն «{company}» ընկերությանը և օգտագործել այն ծառայության համար։ Հաստատո՞ւմ եք։',
                "ru": f'Добавить адрес «{message}» в компанию «{company}» и использовать его для услуги. Подтверждаете?',
                "en": f'Add the address “{message}” to “{company}” and use it for the service. Confirm?',
            }.get(language, f'Ավելացնել «{message}» հասցեն և օգտագործել այն ծառայության համար։ Հաստատո՞ւմ եք։')
            return web.json_response({
                "ok": True,
                "reply": reply,
                "confirmation_id": token,
                "command": command,
            })

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
    command["actor_user_id"]=uid
    intent=command.get("intent")

    # Recover an explicit new-company name from mixed-language user text.
    if intent == "add_business" and not str(command.get("name") or "").strip():
        m = re.search(
            r"(?:նոր\s+(?:ֆիրմա|ընկերություն)|new\s+(?:company|business)|нов(?:ая|ую)\s+(?:фирма|компан(?:ия|ию)))\s*[,;:\-]?\s*(.+)$",
            message,
            re.IGNORECASE,
        )
        if m:
            command["name"] = m.group(1).strip(" .,!?:;-")

    # Confirmation text is generated by Python so Groq cannot switch languages.
    if intent in {
        "add_business","update_business","delete_business",
        "add_service","update_service","delete_service",
        "add_address","update_address","delete_address",
    } and command.get("needs_confirmation"):
        command["reply"] = _local_preview_reply(command, ctx, language)

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

    # Resolve the company deterministically. Explicit names win; phrases such
    # as "այդ ընկերությունում" use the last company selected/created in chat.
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
            _set_active_business(pid, int(exact[0]["id"]), str(exact[0].get("name") or ""))
        elif re.search(r"(այդ|նոր|ընտրված)\s+(?:կոմպանիայում|ընկերությունում|ֆիրմայում)|\b(?:эта|этой|новой)\s+(?:компании|фирме)\b|\b(?:that|this|new)\s+(?:company|business)\b", message, re.IGNORECASE):
            active = _get_active_business(pid, ctx)
            if active:
                command["business_id"] = int(active["id"])
        elif len(businesses) == 1:
            command["business_id"] = int(businesses[0]["id"])
    if intent in {"show_businesses","show_services","show_orders","show_profile","show_addresses","show_documents"}:
        return await _execute_read(pid,command,ctx)
    if intent=="clarify" or not intent:
        return web.json_response({"ok":True,"reply":str(command.get("reply") or "Пожалуйста, уточните запрос."),"command":command})
    if intent not in {"show_businesses","show_services","show_orders","clarify"}:
        token=_pending_add(pid,command)
        return web.json_response({"ok":True,"reply":str(command.get("reply") or _preview(command,ctx,language)),"confirmation_id":token,"command":command})
    return await _execute_mutation(pid,command,ctx,uid)




def _local_preview_reply(c, ctx, lang):
    intent = str(c.get("intent") or "")
    name = str(c.get("name") or "").strip()
    price = c.get("price")
    company = _entity_name(ctx, "businesses", c.get("business_id")) if c.get("business_id") else ""

    if lang == "hy":
        if intent == "add_business":
            return f'Ստեղծել նոր ընկերություն «{name}»։ Հաստատո՞ւմ եք։'
        if intent == "add_service":
            details = f' «{name}»'
            if price not in (None, ""):
                details += f'՝ {price} դրամ'
            return f'{company or "Ձեր ընկերությունում"} ավելացնել ծառայությունը{details}։ Հաստատո՞ւմ եք։'
        if intent == "update_business":
            return f'Փոփոխել «{name or company}» ընկերության տվյալները։ Հաստատո՞ւմ եք։'
        if intent == "delete_business":
            return f'Ջնջել «{name or company}» ընկերությունը։ Հաստատո՞ւմ եք։'
        if intent == "add_address":
            return 'Ավելացնել նոր հասցե։ Հաստատո՞ւմ եք։'
        if intent == "update_address":
            return 'Փոփոխել հասցեն։ Հաստատո՞ւմ եք։'
        if intent == "delete_address":
            return 'Ջնջել հասցեն։ Հաստատո՞ւմ եք։'
        if intent == "update_service":
            return f'Փոփոխել «{name}» ծառայությունը։ Հաստատո՞ւմ եք։'
        if intent == "delete_service":
            return f'Ջնջել «{name}» ծառայությունը։ Հաստատո՞ւմ եք։'

    if lang == "ru":
        if intent == "add_business":
            return f'Создать новую компанию «{name}». Подтверждаете?'
        if intent == "add_service":
            details = f' «{name}»'
            if price not in (None, ""):
                details += f': {price} драм'
            return f'Добавить услугу{details} в «{company or "вашу компанию"}». Подтверждаете?'
        if intent == "update_business":
            return f'Изменить данные компании «{name or company}». Подтверждаете?'
        if intent == "delete_business":
            return f'Удалить компанию «{name or company}». Подтверждаете?'
        if intent == "add_address":
            return 'Добавить новый адрес. Подтверждаете?'
        if intent == "update_address":
            return 'Изменить адрес. Подтверждаете?'
        if intent == "delete_address":
            return 'Удалить адрес. Подтверждаете?'
        if intent == "update_service":
            return f'Изменить услугу «{name}». Подтверждаете?'
        if intent == "delete_service":
            return f'Удалить услугу «{name}». Подтверждаете?'

    return str(c.get("reply") or "Please confirm this change.")

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
        rows = data_core.get_partner_documents(partner_id=pid, actor_user_id=uid, limit=50)
        reply = "\n".join("📄 #%s — %s — %s" % (x.get("id"), x.get("document_type") or "document", x.get("status") or x.get("verification_status") or "—") for x in rows) or "Փաստաթղթեր դեռ չկան։"
        return _json_response({"ok":True,"reply":reply,"data":{"documents":rows}})
    if intent=="show_services":
        lines=["🛠 %s — %s ֏" % (x.get("name") or "", x.get("price") if x.get("price") is not None else "—") for x in ctx["services"]]
        return _json_response({"ok":True,"reply":"\n".join(lines) or "Услуг пока нет.","data":{"services":ctx["services"]}})
    rows = data_core.get_partner_bookings(partner_id=pid, actor_user_id=uid, limit=50)

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

async def _execute_mutation(pid, c, ctx, actor_user_id):
    intent = str(c.get("intent") or "")
    bid = int(c["business_id"]) if c.get("business_id") else None
    oid = int(c["object_id"]) if c.get("object_id") else None
    sid = int(c["service_id"]) if c.get("service_id") else None
    name = str(c.get("name") or "").strip()
    description = str(c.get("description") or "").strip()
    phone = str(c.get("phone") or "").strip() or None
    price = _clean_num(c.get("price"))
    lang = str(c.get("language") or "hy")

    if intent == "add_service":
        if not bid:
            return web.json_response({"ok": True, "reply": {"hy":"Նշեք, թե որ ընկերությունում ավելացնել ծառայությունը։","ru":"Укажите, в какую компанию добавить услугу.","en":"Please specify which company should receive the service."}.get(lang, "Նշեք ընկերությունը։")})
        candidates = [x for x in ctx.get("addresses", []) if int(x.get("business_id") or 0) == bid]
        if oid:
            selected = next((x for x in candidates if int(x.get("id") or 0) == oid), None)
            if selected is None:
                oid = int(candidates[0]["id"]) if len(candidates) == 1 else None
        elif len(candidates) == 1:
            oid = int(candidates[0]["id"])
        if not oid:
            return web.json_response({"ok": True, "reply": {"hy":"Նշեք հասցեն, որտեղ պետք է մատուցվի այս ծառայությունը։","ru":"Укажите адрес, где должна быть эта услуга.","en":"Please provide the address where this service is offered."}.get(lang, "Նշեք հասցեն։")})
        if not name:
            return web.json_response({"ok": True, "reply": {"hy":"Գրեք ծառայության անունը։","ru":"Укажите название услуги.","en":"Please provide the service name."}.get(lang, "Укажите название услуги.")})
        from master_cabinet_api import _ai_match_new_service
        match = await _ai_match_new_service(pid, name, description, bid)
        try:
            row = data_core.create_partner_service_application(
                partner_id=pid, actor_user_id=int(actor_user_id), company_id=bid, address_id=oid,
                name=name, price=price, description=description,
                phone=c.get("contact_phone") or phone, match=match,
                message=str(c.get("message") or ""),
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc) or "application_create_failed"}, status=400)
        aid = int(row["id"])
        return web.json_response({"ok":True,"reply":f"✓ Услуга «{name}» подготовлена и отправлена администратору на подтверждение. Заявка #{aid}.","data":{"application_id":aid}})

    if intent == "add_business":
        if len(name) < 2:
            return web.json_response({"ok": True, "reply": "Укажите название компании."})
        row = data_core.create_partner_company(partner_id=pid, actor_user_id=int(actor_user_id), name=name, description=description or None, phone=phone)
        _set_active_business(pid, int(row["id"]), str(row.get("name") or ""))
        return web.json_response({"ok":True,"reply":{"hy":f'✓ «{row["name"]}» ընկերությունը ստեղծվել է։',"ru":f'✓ Компания «{row["name"]}» создана.',"en":f'✓ Company “{row["name"]}” was created.'}.get(lang, f'✓ «{row["name"]}» ընկերությունը ստեղծվել է։'),"data":{"business":row}})

    if intent in {"update_business","delete_business"}:
        if not bid:
            return web.json_response({"ok":False,"error":"business_id_required"},status=400)
        try:
            if intent == "delete_business":
                row = data_core.archive_partner_company(company_id=bid, actor_user_id=int(actor_user_id))
                return web.json_response({"ok":True,"reply":"✓ Компания «%s» удалена из активного списка." % (row.get("name") or "")})
            row = data_core.update_partner_company(company_id=bid, actor_user_id=int(actor_user_id), name=name or None, description=description or None, phone=phone)
            return web.json_response({"ok":True,"reply":"✓ Данные компании обновлены.","data":{"business":row}})
        except Exception as exc:
            return web.json_response({"ok":False,"error":str(exc) or "business_not_found"},status=400)

    if intent in {"add_address","update_address","delete_address"}:
        try:
            if intent == "add_address":
                if not bid or len(name) < 2:
                    return web.json_response({"ok":True,"reply":"Укажите компанию и название адреса."})
                row = data_core.create_partner_address(
                    partner_id=pid, actor_user_id=int(actor_user_id), company_id=bid,
                    address=str(c.get("address") or name).strip(), city=c.get("city"),
                    marz=c.get("marz"), phone=phone, object_name=name,
                )
                return web.json_response({"ok":True,"reply":"✓ Адрес «%s» добавлен." % (row.get("object_name") or row.get("address") or ""), "data":{"address":row}})
            if not oid or not bid:
                return web.json_response({"ok":False,"error":"address_id_required"},status=400)
            if intent == "delete_address":
                row = data_core.archive_partner_address(address_id=oid, actor_user_id=int(actor_user_id))
                return web.json_response({"ok":True,"reply":"✓ Адрес «%s» удалён." % (row.get("object_name") or "")})
            row = data_core.update_partner_address(
                address_id=oid, actor_user_id=int(actor_user_id), address=c.get("address"),
                city=c.get("city"), marz=c.get("marz"), phone=phone, object_name=name or None,
            )
            return web.json_response({"ok":True,"reply":"✓ Адрес обновлён.","data":{"address":row}})
        except Exception as exc:
            return web.json_response({"ok":False,"error":str(exc) or "address_not_found"},status=400)

    if intent in {"update_service","delete_service"}:
        if not bid or not sid:
            return web.json_response({"ok":True,"reply":"Укажите компанию и услугу."})
        try:
            if intent == "update_service":
                service = data_core.get_service(sid)
                if not service or int(service.get("partner_id") or 0) != int(pid) or int(service.get("business_id") or 0) != int(bid):
                    raise ValueError("service_not_found")
                row = data_core.update_service_safe(
                    service_id=sid, actor_user_id=int(actor_user_id),
                    name=name or None, description=description if description else None, price=price,
                )
                return web.json_response({"ok":True,"reply":"✓ Услуга обновлена.","data":{"service":row}})
            row = data_core.archive_partner_service(service_id=sid, actor_user_id=int(actor_user_id))
            return web.json_response({"ok":True,"reply":"✓ Услуга удалена.","data":{"service":row}})
        except Exception as exc:
            return web.json_response({"ok":False,"error":str(exc) or "service_not_found"},status=400)

    return web.json_response({"ok":True,"reply":"Запрос принят."})

