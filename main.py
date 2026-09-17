"""Armenia AI Guide — current AI-first runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BOT_TOKEN, ADMIN_ID, WEBAPP_BASE_URL
from database import DatabaseManager
from ai_service import AIService
from states import PartnerAIStates
from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data
from stage3_partner_verification import register_stage3_routes
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


def webapp_url(path: str) -> str:
    return f"{WEBAPP_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


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
    return _keyboard("partner.html", "🏢 Բացել գործընկերոջ AI բաժինը")


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


async def api_webapp_role(request: web.Request):
    uid, tg_user = _telegram_user_from_request(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    role = str(payload.get("role") or "").strip().lower()
    lang = str(payload.get("lang") or "").strip().lower()
    if role not in {"client", "master"}:
        return web.json_response({"ok": False, "error": "invalid_role"}, status=400)
    if lang not in {"hy", "ru", "en"}:
        lang = "hy"
    db.register_user(uid, tg_user.get("username") or f"user_{uid}", tg_user.get("first_name") or tg_user.get("last_name") or "")
    db.update_user_field(uid, "role", role)
    db.update_user_field(uid, "lang", lang)
    return web.json_response({"ok": True, "telegram_id": uid, "role": role, "lang": lang})


async def _partner_auth(request: web.Request):
    uid, tg_user = _telegram_user_from_request(request)
    db.register_user(uid, tg_user.get("username") or f"user_{uid}", tg_user.get("first_name") or tg_user.get("last_name") or "")
    db.update_user_field(uid, "role", "master")
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
    history.append({"role": "user", "content": text})
    from partner_registration_ai import extract, missing_question
    from ai_first_partner_onboarding import persist_ready_application
    profile = await extract(text, history, db, previous_profile=previous, pending_field=pending)
    merged = dict(previous)
    for key, value in (profile or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    missing = [key for key in ("business_name", "city", "direction", "services") if not merged.get(key)]
    merged["missing"] = missing
    merged["ready"] = not missing
    await state.update_data(partner_onboarding_history=history, partner_profile=merged)
    if missing:
        question = missing_question(merged, lang)
        history.append({"role": "assistant", "content": question})
        await state.update_data(partner_onboarding_pending_field=missing[0], partner_onboarding_history=history)
        return {"message": t(lang, "🤖 Ես արդեն հավաքել եմ ձեր ասած տվյալները։ " + question, "🤖 Я уже собрал данные. " + question, "🤖 I have collected the information. " + question), "completed": False, "profile": merged}
    result = persist_ready_application(db, uid, merged)
    await state.clear()
    message = t(lang,
        "✅ Բիզնեսի տվյալները ճանաչեցի և պահպանեցի։ Ուղղությունը ստեղծված է որպես սպասող հայտ։ Հաջորդ քայլը՝ բեռնեք հաստատող փաստաթուղթը։",
        "✅ Данные бизнеса распознаны и сохранены. Направление создано как заявка на проверку. Следующий шаг — загрузите подтверждающий документ.",
        "✅ I recognized and saved the business. The direction is pending review. Next step: upload the verification document.")
    if result.get("proposal_created"):
        message = t(lang, "✅ Տվյալները պահպանված են։ Նոր ուղղության առաջարկը ուղարկվել է ադմինիստրատորին։", "✅ Данные сохранены. Предложение нового направления отправлено администратору.", "✅ Data saved. The new-direction proposal was sent to the administrator.")
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
    user = db.get_user(uid) or {}
    role = user.get("role")
    if role == "master":
        await message.answer("🏢 Armenia AI Guide\n\nԲացեք գործընկերոջ AI բաժինը՝ ձեր բիզնեսը կառավարելու կամ շարունակելու գրանցումը։", reply_markup=_partner_keyboard())
        return
    if role == "client":
        await message.answer("👤 Armenia AI Guide\n\nԲացեք AI օգնականը՝ ծառայություն գտնելու, ընտրելու և ամրագրելու համար։", reply_markup=_client_keyboard())
        return
    await state.clear()
    await message.answer("✦ <b>Armenia AI Guide</b>\n<i>ARMENIA · AI CONCIERGE</i>\n\nՁեր AI օգնականը ծառայություններ գտնելու, ընտրելու, բանակցելու և ամրագրման համար։", reply_markup=_welcome_keyboard(), parse_mode=ParseMode.HTML)


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
    if uid != route_uid:
        return web.json_response({"ok": False, "error": "telegram_user_mismatch"}, status=403)
    request["telegram_user_id"] = uid
    return await handler(request)


async def health(request: web.Request):
    return web.json_response({"ok": True})


async def serve_index(request: web.Request):
    path = WEB_APPS_DIR / "index.html"
    if not path.is_file():
        return web.json_response({"ok": False, "error": "index.html_not_found"}, status=500)
    return web.FileResponse(path)


async def main():
    logger.info("🚀 Запуск Armenia AI Guide — AI-first runtime")
    app = web.Application(middlewares=[telegram_partner_auth_middleware])
    app.router.add_get("/health", health)
    app.router.add_get("/", serve_index)
    app.router.add_post("/api/webapp/role", api_webapp_role)
    app.router.add_post("/api/webapp/partner/start", api_webapp_partner_start)
    app.router.add_post("/api/webapp/partner/message", api_webapp_partner_message)
    app.router.add_get("/api/master/{id}/registration-status", api_partner_registration_status)
    register_stage3_routes(app, bot_token=BOT_TOKEN, admin_id=ADMIN_ID)
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
