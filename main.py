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
import data_core
from ai_service import AIService
from ai_manager import AIManager, AIContext
from states import PartnerAIStates
from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data
from stage3_partner_verification import register_stage3_routes
from partner_business_application_api import register_business_application_routes
from partner_directions_api import register_partner_direction_routes
from admin_ai_api import admin_ai_message, register_admin_ai_routes
from admin_stats_api import register_admin_stats_routes
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
ai_manager = AIManager()
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
    """Unified registration conversation.

    Registration now goes through the same AIManager/Groq pipeline as client,
    partner and admin conversations. The model owns semantic understanding;
    ToolRegistry owns the authenticated save operation.
    """
    user = db.get_user(uid) or {}
    lang = user.get("lang") or "hy"
    try:
        result = await ai_manager.handle_message(
            int(uid),
            text,
            AIContext.REGISTRATION,
            language=lang,
            extra_context={"registration": True},
        )
    except Exception:
        logger.exception("Unified registration AI turn failed")
        return {
            "message": t(
                lang,
                "⚠️ Ներողություն, AI ծառայությունը ժամանակավորապես անհասանելի է։ Փորձեք կրկին։",
                "⚠️ Извините, AI временно недоступен. Попробуйте ещё раз.",
                "⚠️ Sorry, the AI service is temporarily unavailable. Please try again.",
            ),
            "completed": False,
        }

    tool_result = result.get("tool_result") or {}
    profile = tool_result.get("profile") if isinstance(tool_result, dict) else None
    application_id = tool_result.get("application_id") if isinstance(tool_result, dict) else None
    completed = bool(tool_result.get("ok") and application_id)

    # Keep the legacy FSM state only as a compatibility bridge for the existing
    # WebApp UI. Conversation history/state is now owned by AIManager.
    if completed:
        await state.update_data(
            partner_profile=profile or {},
            partner_application_id=application_id,
            partner_onboarding_pending_field=None,
        )
        await state.set_state(PartnerAIStates.onboarding)
    else:
        await state.update_data(partner_profile=profile or {})

    response = {
        "message": result.get("reply") or "",
        "completed": completed,
        "profile": profile or {},
    }
    if application_id:
        response["application_id"] = application_id
        # Once the AI has prepared the draft, the WebApp must immediately open
        # the editable application form. The verification document is uploaded
        # there; the AI chat itself is not the document-upload surface.
        response["open_form"] = bool(completed)
    if result.get("confirmation_required"):
        response["confirmation_required"] = True
    if result.get("error"):
        response["error"] = result.get("error")
    return response


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
        result = await ai_manager.handle_message(
            message.from_user.id,
            text,
            AIContext.ADMIN,
        )
        await message.answer(result.get("reply") or "⚠️ No response.")
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

async def serve_master_cabinet(request: web.Request):
    return await _serve_html_file(request, "master_cabinet.html")

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
    app["ai_manager"] = ai_manager
    app.router.add_get("/health", health)
    app.router.add_post("/telegram/webhook", telegram_webhook)
    app.router.add_get("/", serve_index)
    app.router.add_get("/welcome.html", serve_welcome)
    app.router.add_get("/partner.html", serve_partner)
    app.router.add_get("/master_cabinet.html", serve_master_cabinet)
    app.router.add_get("/api/webapp/session", api_webapp_session)
    app.router.add_post("/api/webapp/role", api_webapp_role)
    app.router.add_post("/api/webapp/partner/start", api_webapp_partner_start)
    app.router.add_post("/api/webapp/partner/message", api_webapp_partner_message)
    app.router.add_get("/api/master/{id}/registration-status", api_partner_registration_status)
    register_stage3_routes(app, bot_token=BOT_TOKEN, admin_id=ADMIN_ID)
    register_business_application_routes(app, bot_token=BOT_TOKEN, admin_id=ADMIN_ID)
    register_partner_direction_routes(app, db, bot=bot)
    register_admin_stats_routes(app)
    register_admin_ai_routes(app, ai=ai, bot=bot)
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