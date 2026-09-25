"""Armenia AI Guide — current AI-first runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonDefault, MenuButtonWebApp, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BOT_TOKEN, ADMIN_ID, WEBAPP_BASE_URL
from database import DatabaseManager
from ai_service import AIService
from states import PartnerAIStates
from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data
from stage3_partner_verification import register_stage3_routes
from partner_business_application_api import register_business_application_routes
from partner_directions_api import register_partner_direction_routes
from admin_ai_api import admin_ai_message
import runtime_platform_bootstrap  # noqa: F401

try:
    from master_cabinet_api import register_master_cabinet_routes
except ImportError:
    register_master_cabinet_routes = None

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
db = DatabaseManager()
ai = AIService()
BASE_DIR = Path(__file__).resolve().parent
WEB_APPS_DIR = BASE_DIR / "web_apps"


# Telegram WebView can retain HTML aggressively. Change this value when a
# frontend deployment must invalidate an already opened Mini App URL.
WEBAPP_VERSION = os.getenv("WEBAPP_VERSION", "20260925-4").strip() or "20260925-4"


def webapp_url(path: str) -> str:
    base = f"{WEBAPP_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}v={WEBAPP_VERSION}"


def t(lang: str, hy: str, ru: str, en: str) -> str:
    return {"hy": hy, "ru": ru, "en": en}.get(lang, ru)


def _keyboard(url_path: str, text: str):
    b = InlineKeyboardBuilder()
    b.button(text=text, web_app=WebAppInfo(url=webapp_url(url_path)))
    return b.as_markup()


def _welcome_keyboard():
    return _keyboard("welcome.html", "✦ Armenia AI Guide")


def _client_keyboard():
    return _keyboard("client.html", "👤 Բացել AI օգնականը / Открыть AI")


def _partner_keyboard():
    return _keyboard("partner.html?entry=welcome", "🏢 Բացել գործընկերոջ AI բաժինը")


def _partner_state(uid: int) -> FSMContext:
    from aiogram.fsm.storage.base import StorageKey
    return FSMContext(storage=storage, key=StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid))


def _telegram_user_from_request(request: web.Request) -> tuple[int, dict]:
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": "telegram_init_data_required"}), content_type="application/json")
    try:
        user = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
        return int(user["id"]), user
    except (TelegramWebAppAuthError, KeyError, TypeError, ValueError) as exc:
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}), content_type="application/json")


async def api_webapp_session(request: web.Request):
    """Return the user's current app role/session destination."""
    uid, tg_user = _telegram_user_from_request(request)
    db.register_user(
        uid,
        tg_user.get("username") or f"user_{uid}",
        tg_user.get("first_name") or tg_user.get("last_name") or "",
    )
    partner = db.get_partner_by_user(uid)
    if partner and str(partner.get("status") or "").lower() == "approved":
        return web.json_response({
            "ok": True,
            "role": "partner",
            "partner_status": "approved",
            "destination": "master_cabinet.html",
            "business_name": partner.get("business_name") or "",
        })
    return web.json_response({
        "ok": True,
        "role": "client",
        "partner_status": str(partner.get("status") or "") if partner else None,
        "destination": "welcome.html",
    })


async def api_webapp_role(request: web.Request):
    uid, tg_user = _telegram_user_from_request(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    role = str(payload.get("role") or "").strip().lower()
    lang = str(payload.get("lang") or "").strip().lower()
    if role not in {"client", "partner", "master"}:
        return web.json_response({"ok": False, "error": "invalid_role"}, status=400)
    if lang not in {"hy", "ru", "en"}:
        lang = "hy"
    db.register_user(uid, tg_user.get("username") or f"user_{uid}", tg_user.get("first_name") or tg_user.get("last_name") or "")
    db.update_user_field(uid, "role", "partner" if role == "master" else role)
    db.update_user_field(uid, "lang", lang)
    return web.json_response({"ok": True, "telegram_id": uid, "role": role, "lang": lang})


async def _partner_auth(request: web.Request):
    uid, tg_user = _telegram_user_from_request(request)
    db.register_user(uid, tg_user.get("username") or f"user_{uid}", tg_user.get("first_name") or tg_user.get("last_name") or "")
    db.update_user_field(uid, "role", "partner")
    return uid, db.get_user(uid) or {}


async def _start_partner_ai_state(uid: int) -> FSMContext:
    state = _partner_state(uid)
    current = await state.get_state()
    if current != PartnerAIStates.onboarding.state:
        await state.clear()
        await state.set_state(PartnerAIStates.onboarding)
        await state.update_data(partner_onboarding_history=[], partner_profile={}, partner_onboarding_pending_field=None)
    return state


async def api_webapp_partner_start(request: web.Request):
    try:
        uid, user = await _partner_auth(request)
        await _start_partner_ai_state(uid)
        lang = user.get("lang") or "hy"
        return web.json_response({"ok": True, "started": True, "telegram_id": uid, "message": t(lang,
            "🏢 <b>Գրանցենք ձեր բիզնեսը</b>\n\nՊատմեք ազատ ձևով՝ ինչպես է կոչվում բիզնեսը, որտեղ է գտնվում, ինչ ծառայություններ եք մատուցում և ինչ գներով։ Կարող եք ավելացնել նաև օբյեկտների, տարածքի և աշխատանքի ժամերի մասին տեղեկություններ։ Ես ինքնուրույն կկառուցեմ հայտը։",
            "🏢 <b>Зарегистрируем ваш бизнес</b>\n\nРасскажите свободно: как называется бизнес, где находится, какие услуги вы оказываете и по каким ценам. Можно добавить объекты, зону работы и график. Я сам соберу заявку.",
            "🏢 <b>Let’s register your business</b>\n\nTell me naturally what your business is called, where it is located, what services you offer and their prices. You can also describe objects, coverage and schedule. I will build the application for you.")})
    except web.HTTPException:
        raise
    except Exception as exc:
        logger.exception("Partner AI start failed for Telegram user")
        return web.json_response({"ok": False, "error": "partner_start_failed", "detail": str(exc)[:500]}, status=500)


async def api_webapp_partner_message(request: web.Request):
    try:
        uid, _ = await _partner_auth(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        text = str(payload.get("text") or "").strip()
        if len(text) < 2:
            return web.json_response({"ok": False, "error": "message_too_short"}, status=400)
        state = await _start_partner_ai_state(uid)
        result = await _process_partner_onboarding_text(uid, text, state)
        return web.json_response({"ok": True, **result})
    except web.HTTPException:
        raise
    except Exception as exc:
        logger.exception("Partner AI message failed for Telegram user")
        return web.json_response({"ok": False, "error": "partner_message_failed", "detail": str(exc)[:500]}, status=500)


async def _process_partner_onboarding_text(uid: int, text: str, state: FSMContext) -> dict:
    user = db.get_user(uid) or {}
    lang = user.get("lang") or "hy"
    data = await state.get_data()
    history = list(data.get("partner_onboarding_history") or [])
    pending = data.get("partner_onboarding_pending_field")
    previous = data.get("partner_profile") or {}
    # Give the onboarding classifier the current organization context so it can
    # distinguish "new direction in this business" from "another business".
    try:
        from partner_business_application_api import default_business
        partner_row=db.get_partner_by_user(uid) or {}
        current_business=default_business(int(partner_row["id"])) if partner_row.get("id") else None
        if current_business:
            previous=dict(previous)
            previous["current_business_name"]=current_business.get("name")
            previous["current_business_description"]=current_business.get("description") or ""
    except Exception:
        pass
    history.append({"role": "user", "content": text})
    from partner_registration_ai import extract, missing_question
    from ai_first_partner_onboarding import persist_ready_application, create_partner_application_draft
    profile = await extract(text, history, db, previous_profile=previous, pending_field=pending)

    # Final deterministic safety net for the WebApp. The partner's original
    # message is authoritative for obvious facts and explicitly priced
    # services. This runs even when Groq returns 400/429 or malformed JSON.
    try:
        from partner_registration_ai import _recover_obvious_facts, _recover_services_from_history
        source_history = history + [{"role": "user", "content": text}]
        profile = _recover_obvious_facts(
            " ".join(str(x.get("content") or "") for x in source_history),
            dict(profile or {}),
        )
        recovered_services = _recover_services_from_history(source_history)
        if recovered_services:
            profile["services"] = recovered_services
    except Exception:
        logger.exception("Partner deterministic extraction fallback failed")

    merged = dict(previous)

    for key, value in (profile or {}).items():
        if key in ("services", "missing", "ready"):
            continue
        if value not in (None, "", [], {}):
            merged[key] = value

    previous_services = [dict(x) for x in (previous.get("services") or []) if isinstance(x, dict)]
    new_services = [dict(x) for x in (profile.get("services") or []) if isinstance(x, dict)] if isinstance(profile, dict) else []
    numeric_answer = pending == "services" and bool(re.fullmatch(r"[0-9][0-9\\s.,]*", text.strip()))

    if numeric_answer and previous_services:
        digits = re.sub(r"[^0-9]", "", text)
        if digits:
            previous_services[-1]["price"] = float(digits)
        merged["services"] = previous_services
    else:
        combined = [dict(x) for x in previous_services]
        by_name = {str(x.get("name") or "").strip().lower(): x for x in combined}
        for item in new_services:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key_name = name.lower()
            if key_name in by_name:
                old_item = by_name[key_name]
                if item.get("price") not in (None, ""):
                    old_item["price"] = item.get("price")
                if item.get("price_type") not in (None, "", "unknown"):
                    old_item["price_type"] = item.get("price_type")
            else:
                combined.append(item)
                by_name[key_name] = item
        if combined:
            merged["services"] = combined

    # Final normalization before the profile reaches the WebApp.
    # Keep one canonical shape regardless of which AI/recovery path produced
    # the values. The partner form consumes this exact shape.
    if not merged.get("city"):
        merged["city"] = merged.get("location_city") or merged.get("settlement") or ""
    if not merged.get("marz"):
        merged["marz"] = merged.get("location_marz") or merged.get("region") or ""
    if not merged.get("phone"):
        merged["phone"] = merged.get("phone_number") or ""
    normalized_services=[]
    for svc in (merged.get("services") or []):
        if not isinstance(svc,dict):
            continue
        name=str(svc.get("name") or svc.get("service_name") or svc.get("service") or "").strip()
        if not name:
            continue
        item=dict(svc)
        item["name"]=name
        if item.get("price") in ("",None):
            item["price"]=None
        try:
            if item.get("price") is not None:
                item["price"]=float(item["price"])
        except (TypeError,ValueError):
            item["price"]=None
        item["price_type"]=str(item.get("price_type") or "fixed").strip().lower()
        if item["price_type"] in {"starting","starting_from","from_price"}:
            item["price_type"]="from"
        normalized_services.append(item)
    if normalized_services:
        merged["services"]=normalized_services

    # The partner never needs to provide an internal catalogue direction.
    # AI matching/proposal handles that automatically.
    # Business name is a required partner-facing registration field.
    # Direction/subcategory remain admin-side classification fields.
    missing = [key for key in ("business_name", "marz", "city", "phone", "services") if not merged.get(key)]
    merged["missing"] = missing
    merged["ready"] = not missing

    # Catalogue classification is NOT part of partner registration.
    # Partner Intake only extracts business facts and services. The admin-side
    # classification happens later, after the partner has reviewed/submitted
    # the form. This keeps the onboarding request small and prevents the full
    # catalogue from being sent to Groq during every registration message.
    merged["master_category_id"] = None
    merged["classification_confidence"] = 0
    merged["classification_ambiguities"] = []
    merged["classification_needs_review"] = True

    await state.update_data(partner_onboarding_history=history, partner_profile=merged)
    if missing:
        # IMPORTANT: the AI result is shown immediately in the universal form.
        # Missing fields remain editable/empty; the partner does not have to
        # answer a questionnaire before seeing what AI understood.
        draft = create_partner_application_draft(db, uid, merged)
        question = missing_question(merged, lang)
        history.append({"role": "assistant", "content": question})
        await state.update_data(
            partner_onboarding_pending_field=missing[0],
            partner_onboarding_history=history,
            partner_profile=merged,
            partner_application_id=draft.get("application_id"),
        )
        return {
            "message": t(
                lang,
                "🤖 Ես կազմեցի հայտի նախնական տարբերակը։ Ստուգեք լրացված տվյալները և լրացրեք միայն բաց դաշտերը։ " + question,
                "🤖 Я собрал предварительную заявку. Проверьте заполненные данные и заполните только пустые поля. " + question,
                "🤖 I prepared the application draft. Check the extracted data and fill only the missing fields. " + question,
            ),
            "completed": False,
            "open_form": True,
            "application_id": draft.get("application_id"),
            "profile": merged,
        }
    try:
        result = persist_ready_application(db, uid, merged)
    except ValueError as exc:
        # The persistence layer may reject an incomplete profile. This is a
        # normal conversational state, not an error for the partner.
        if str(exc) == "partner_profile_not_ready":
            required = ("marz", "city", "phone", "services")
            missing = [key for key in required if not merged.get(key)]
            merged["missing"] = missing
            merged["ready"] = not missing
            if missing:
                question = missing_question(merged, lang)
                history.append({"role": "assistant", "content": question})
                await state.update_data(
                    partner_onboarding_pending_field=missing[0],
                    partner_onboarding_history=history,
                    partner_profile=merged,
                )
                return {
                    "message": t(
                        lang,
                        "🤖 " + question,
                        "🤖 " + question,
                        "🤖 " + question,
                    ),
                    "completed": False,
                    "profile": merged,
                }
        raise
    if result.get("error") or result.get("ok") is False:
        # Never expose an internal persistence error as a generic profile error.
        await state.update_data(
            partner_onboarding_pending_field="services",
            partner_onboarding_history=history,
            partner_profile=merged,
        )
        return {
            "message": t(
                lang,
                "⚠️ Չհաջողվեց պահպանել հայտը։ Խնդրում եմ նշեք ծառայության անունը և գինը։",
                "⚠️ Не удалось сохранить заявку. Укажите услугу и цену.",
                "⚠️ I could not save the application. Please provide the service and price.",
            ),
            "completed": False,
            "profile": merged,
        }
    if not result.get("proposal_created") and int(result.get("service_count") or 0) < 1:
        # A malformed AI response must never produce a "completed" application
        # with zero persisted services.
        await state.update_data(
            partner_onboarding_pending_field="services",
            partner_onboarding_history=history,
            partner_profile=merged,
        )
        return {
            "message": t(
                lang,
                "🤖 Ծառայությունը չկարողացա պահպանել։ Գրեք ծառայության անունը և գինը։",
                "🤖 Я не смог сохранить услугу. Напишите название услуги и цену.",
                "🤖 I could not save the service. Please provide the service name and price.",
            ),
            "completed": False,
            "profile": merged,
        }
    await state.clear()
    message = t(lang,
        "✅ Հայտը կազմված է և ուղարկված է ադմինիստրատորին։ Նա կստուգի բիզնեսը, ուղղությունը, ենթաուղղությունը, ծառայությունը և կուղարկի ձեզ լրացման/փաստաթղթի պահանջը։",
        "✅ Заявка полностью сформирована. Следующий шаг — загрузите подтверждающий документ. После загрузки вся анкета вместе с документом будет отправлена администратору одним заявлением.",
        "✅ The application is fully prepared. Next, upload the verification document. After upload, the complete application and document will be sent to the administrator together.")
    if result.get("proposal_created"):
        message = t(lang, "✅ Ամբողջական հայտը կազմված է։ Բեռնեք փաստաթուղթը, և ամբողջ հայտը միասին կուղարկվի ադմինիստրատորին։", "✅ Полная заявка сформирована. Загрузите документ — после этого вся заявка будет отправлена администратору вместе.", "✅ The full application is prepared. Upload the document and the complete application will be sent to the administrator together.")
    return {"message": message, "completed": True, "profile": merged, **result}


async def api_partner_registration_status(request: web.Request):
    uid, _ = _telegram_user_from_request(request)
    partner = db.get_partner_by_user(uid)
    if not partner:
        return web.json_response({"ok": True, "registered": False, "status": "not_registered"})
    return web.json_response({"ok": True, "registered": True, "partner_id": partner["id"], "status": partner.get("status"), "verification_status": partner.get("verification_status"), "business_name": partner.get("business_name") or "", "business_description": partner.get("business_description") or "", "rejection_reason": partner.get("rejection_reason") or ""})


@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    db.register_user(uid, message.from_user.username or f"user_{uid}", message.from_user.full_name or "")
    await state.clear()
    # Reset any per-chat Telegram menu button that may still point to an old
    # partner WebApp URL. The current menu is always the welcome screen.
    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonDefault(),
        )
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(text="Armenia AI Guide", web_app=WebAppInfo(url=webapp_url("welcome.html")))
        )
    except Exception:
        logger.exception("Could not reset Telegram menu button for chat %s", message.chat.id)
    await message.answer(
        "✦ <b>Armenia AI Guide</b>\n<i>ARMENIA · AI CONCIERGE</i>\n\nՁեր AI օգնականը ծառայություններ գտնելու, ընտրելու, բանակցելու և ամրագրման համար։",
        reply_markup=_welcome_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("👑 Admin", reply_markup=_keyboard("admin.html", "Բացել Admin Cabinet"))


@router.message(Command("admin_panel"))
async def cmd_admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("👑 Admin Cabinet", reply_markup=_keyboard("admin.html", "Բացել Admin Cabinet"))


@router.message(lambda message: message.from_user.id == ADMIN_ID and bool((message.text or message.caption or "").strip()))
async def telegram_admin_ai_secretary(message: types.Message):
    """The admin can operate the platform directly from the Telegram chat."""
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return
    try:
        reply = await admin_ai_message(message.from_user.id, text)
        await message.answer(reply)
    except Exception:
        logger.exception("Telegram admin AI secretary failed")
        await message.answer("⚠️ AI-секретарь временно недоступен. Попробуйте ещё раз.")


@router.message(PartnerAIStates.onboarding)
async def telegram_partner_ai_message(message: types.Message, state: FSMContext):
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("🤖 Գրեք ձեր բիզնեսի մասին տեքստով։ / Опишите бизнес текстом.")
        return
    try:
        result = await _process_partner_onboarding_text(message.from_user.id, text, state)
        await message.answer(result["message"], parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Telegram partner AI message failed")
        await message.answer("⚠️ Произошла техническая ошибка. Попробуйте ещё раз.")


@web.middleware
async def telegram_partner_auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/master/") or request.method == "OPTIONS":
        return await handler(request)
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        return web.json_response({"ok": False, "error": "telegram_init_data_required"}, status=401)
    try:
        user = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
        uid = int(user["id"])
        route_uid = int(request.path.split("/")[3])
    except (TelegramWebAppAuthError, KeyError, TypeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}, status=401)
    # Current partner cabinet uses /api/master/0/... as a user-scoped
    # route. The authenticated Telegram user is resolved from initData and
    # the business/partner API performs the real ownership checks. Keep the
    # old /api/master/<telegram_id>/... form compatible as well.
    if route_uid != 0 and uid != route_uid:
        return web.json_response({"ok": False, "error": "telegram_user_mismatch"}, status=403)
    request["telegram_user_id"] = uid
    return await handler(request)


async def health(request: web.Request):
    return web.json_response({"ok": True})


async def serve_index(request: web.Request):
    path = WEB_APPS_DIR / "welcome.html"
    if not path.is_file():
        return web.json_response({"ok": False, "error": "welcome.html_not_found"}, status=500)
    return web.FileResponse(path)

async def _serve_html_file(request: web.Request, filename: str):
    path = WEB_APPS_DIR / filename
    if not path.is_file():
        logger.error("❌ WebApp file missing for %s: %s", request.path, path)
        return web.json_response({"ok": False, "error": f"{filename}_not_found"}, status=500)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.exception("❌ Cannot read WebApp file %s", path)
        return web.json_response({"ok": False, "error": "webapp_read_failed", "detail": str(exc)}, status=500)
    logger.info("📤 WebApp %s -> %s bytes for %s", filename, len(content.encode("utf-8")), request.path)
    return web.Response(
        text=content,
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

async def serve_welcome(request: web.Request):
    return await _serve_html_file(request, "welcome.html")

async def serve_partner(request: web.Request):
    return await _serve_html_file(request, "partner.html")

def log_webapp_files():
    for name in ("welcome.html", "partner.html"):
        path = WEB_APPS_DIR / name
        try:
            logger.info("📄 WebApp %s: %s bytes (%s)", name, path.stat().st_size, path)
        except OSError:
            logger.error("❌ WebApp file missing: %s", path)


async def telegram_webhook(request: web.Request):
    try:
        payload = await request.json()
        update = types.Update.model_validate(payload)
        await dp.feed_update(bot, update)
        return web.json_response({"ok": True})
    except Exception:
        logger.exception("Telegram webhook update failed")
        return web.json_response({"ok": False}, status=500)


@web.middleware
async def _webapp_cache_middleware(request: web.Request, handler):
    response = await handler(request)
    # Never let Telegram's embedded WebView keep HTML entry points stale.
    # Static assets can remain cacheable; HTML always revalidates.
    if request.path.endswith(".html") or request.path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


async def main():
    logger.info("🚀 Запуск Armenia AI Guide — AI-first runtime")
    log_webapp_files()
    app = web.Application(middlewares=[telegram_partner_auth_middleware, _webapp_cache_middleware])
    app.router.add_get("/health", health)
    app.router.add_post("/telegram/webhook", telegram_webhook)
    app.router.add_get("/", serve_index)
    app.router.add_get("/welcome.html", serve_welcome)
    app.router.add_get("/partner.html", serve_partner)
    app.router.add_get("/api/webapp/session", api_webapp_session)
    app.router.add_post("/api/webapp/role", api_webapp_role)
    app.router.add_post("/api/webapp/partner/start", api_webapp_partner_start)
    app.router.add_post("/api/webapp/partner/message", api_webapp_partner_message)
    app.router.add_get("/api/master/{id}/registration-status", api_partner_registration_status)
    register_stage3_routes(app, bot_token=BOT_TOKEN, admin_id=ADMIN_ID)
    register_business_application_routes(app, bot_token=BOT_TOKEN, admin_id=ADMIN_ID)
    register_partner_direction_routes(app, db, bot=bot)
    logger.info("✅ Business/application layer registered")
    logger.info("✅ Partner direction routes registered")
    logger.info("✅ Stage 3 verification routes registered")
    if register_master_cabinet_routes is not None:
        register_master_cabinet_routes(app, db, bot=bot)
        logger.info("✅ Partner cabinet API registered")
    app.router.add_static("/", path=str(WEB_APPS_DIR), name="web_apps")
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("🌐 HTTP-сервер запущен на порту %s", port)
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Armenia AI Guide", web_app=WebAppInfo(url=webapp_url("welcome.html"))))
        logger.info("✅ Telegram bottom menu button configured")
    except Exception:
        logger.exception("Could not configure Telegram menu button")

    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        if render_url:
            webhook_url = f"{render_url}/telegram/webhook"
    if webhook_url:
        try:
            # drop_pending_updates=True + delete_webhook first clears any stale
            # getUpdates session so a previous poller stops conflicting.
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
            logger.info("✅ Telegram webhook configured: %s", webhook_url)
        except Exception:
            logger.exception("Could not configure Telegram webhook")
            raise
        # In webhook mode nothing blocks the event loop, so we must keep the
        # process (and the aiohttp server) alive explicitly. Without this the
        # coroutine returns, asyncio.run() exits and the server dies.
        logger.info("📡 Webhook mode active; serving updates via /telegram/webhook")
        await asyncio.Event().wait()
    else:
        logger.info("ℹ️ TELEGRAM_WEBHOOK_URL/RENDER_EXTERNAL_URL not set; using polling")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())