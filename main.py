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
from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data
from stage3_partner_verification import register_stage3_routes
from partner_business_application_api import register_business_application_routes
from partner_directions_api import register_partner_direction_routes
from admin_ai_api import admin_ai_message, register_admin_ai_routes
from admin_stats_api import register_admin_stats_routes
from marketplace_flow_api import register_marketplace_flow_routes
from client_api import register_client_routes
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
WEBAPP_VERSION = "20261002-5"


def _ensure_runtime_schema() -> None:
    """Run blocking schema migrations only after the HTTP listener is bound."""
    from stage3_partner_verification import ensure_stage3_schema
    from partner_business_application_api import ensure_business_application_schema
    from partner_directions_api import ensure_partner_direction_schema
    from booking_schema import ensure_booking_schema

    ensure_stage3_schema()
    ensure_business_application_schema()
    ensure_partner_direction_schema()
    ensure_booking_schema()


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


def _telegram_user_from_request(request: web.Request) -> tuple[int, dict]:
    """Validate Telegram Mini App initData and return (telegram_id, user)."""
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise TelegramWebAppAuthError("telegram_init_data_required")
    tg_user = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
    uid = int(tg_user["id"])
    db.register_user(
        uid,
        str(tg_user.get("username") or f"user_{uid}"),
        str(tg_user.get("first_name") or "") + (
            f" {tg_user.get('last_name')}" if tg_user.get("last_name") else ""
        ),
    )
    user = db.get_user(uid) or {"telegram_id": uid, "role": None, "lang": "hy"}
    return uid, user


async def _partner_auth(request: web.Request) -> tuple[int, dict]:
    """Authenticate a partner WebApp request and return its local user."""
    try:
        uid, user = _telegram_user_from_request(request)
    except TelegramWebAppAuthError as exc:
        return_error = str(exc) or "invalid_telegram_init_data"
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": return_error}),
            content_type="application/json",
        )
    partner = db.get_partner_by_user(uid)
    if partner:
        return uid, user
    # Registration is allowed to proceed for a new authenticated Telegram user.
    return uid, user


async def api_webapp_session(request: web.Request):
    """Return the authenticated Mini App session without exposing auth errors to UI."""
    try:
        uid, user = _telegram_user_from_request(request)
    except TelegramWebAppAuthError as exc:
        return web.json_response(
            {"ok": False, "authenticated": False, "error": str(exc) or "invalid_telegram_init_data"},
            status=401,
        )
    role = user.get("role")
    destination = "master_cabinet.html" if role == "partner" else "client.html"
    return web.json_response({
        "ok": True,
        "authenticated": True,
        "telegram_id": uid,
        "role": role,
        "destination": destination,
        "lang": user.get("lang") or "hy",
    })


async def api_webapp_role(request: web.Request):
    """Persist only the two public WebApp roles selected by the user."""
    try:
        uid, _ = _telegram_user_from_request(request)
    except TelegramWebAppAuthError as exc:
        return web.json_response(
            {"ok": False, "error": str(exc) or "invalid_telegram_init_data"},
            status=401,
        )
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    role = str(payload.get("role") or "").strip().lower()
    if role not in {"client", "partner"}:
        return web.json_response({"ok": False, "error": "invalid_role"}, status=400)
    db.update_user_field(uid, "role", role)
    return web.json_response({"ok": True, "role": role})


def _partner_registration_error(lang: str, code: str) -> str:
    messages = {
        "business_name_required": t(lang, "Գրեք բիզնեսի անունը։", "Укажите название бизнеса.", "Enter the business name."),
        "invalid_phone": t(lang, "Մուտքագրեք վավեր հեռախոսահամար։", "Укажите корректный номер телефона.", "Enter a valid phone number."),
    }
    return messages.get(code, t(lang, "Տեխնիկական սխալ։", "Техническая ошибка.", "Technical error."))


async def api_webapp_partner_start(request: web.Request):
    """Start the single AI-first partner registration flow."""
    uid, user = await _partner_auth(request)
    lang = user.get("lang") or "hy"
    # Materialize the partner account at the start of the partner flow.
    # Company/application data is still created only by the confirmed AI action.
    db.update_user_field(uid, "role", "partner")
    partner = db.get_partner_by_user(uid)
    if not partner:
        partner = data_core.ensure_partner(uid)
    if partner:
        companies = data_core.list_companies(partner_id=int(partner["id"]))
        if companies:
            return web.json_response({
                "ok": True,
                "registered": True,
                "partner_id": int(partner["id"]),
                "company_id": int(companies[0]["id"]),
                "business_name": companies[0].get("name") or partner.get("business_name") or "",
                "destination": "master_cabinet.html",
                "message": t(
                    lang,
                    "Բարի գալուստ գործընկերոջ AI բաժին։ Գրեք, թե ինչ եք ուզում անել։",
                    "Добро пожаловать в AI-раздел партнёра. Напишите, что хотите сделать.",
                    "Welcome to the partner AI workspace. Tell me what you want to do.",
                ),
            })
    return web.json_response({
        "ok": True,
        "registered": False,
        "ai_first": True,
        "message": t(
            lang,
            "Գրանցումը սկսված է։ Պատմեք ձեր բիզնեսի մասին ազատ ձևով՝ անունը, ծառայությունները, գները և գտնվելու վայրը։ AI-ը կհավաքի տվյալները, ցույց կտա մեկ ընդհանուր նախադիտում և միայն ձեր հաստատումից հետո կստեղծի հայտը։",
            "Регистрация начата. Расскажите о бизнесе свободным текстом: название, услуги, цены и местоположение. AI соберёт данные, покажет одно общее предварительное представление и создаст заявку только после вашего подтверждения.",
            "Registration started. Describe your business freely: name, services, prices and location. AI will collect the data, show one combined preview, and create the application only after your confirmation.",
        ),
    })


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
        route_segment = request.path.split("/")[3]
    except (TelegramWebAppAuthError, KeyError, TypeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}, status=401)
    # The current partner cabinet intentionally uses /api/master/current/*
    # routes. "current" is an alias resolved from verified Telegram initData;
    # numeric legacy routes remain bound to the explicit Telegram user id.
    if route_segment != "current":
        try:
            route_uid = int(route_segment)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid_master_route"}, status=400)
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
    app.router.add_get("/api/master/{id}/registration-status", api_partner_registration_status)
    # Register routes without running blocking PostgreSQL migrations. Render must
    # see the HTTP listener before startup migrations begin.
    register_stage3_routes(app, bot_token=BOT_TOKEN, admin_id=ADMIN_ID, ensure_schema=False)
    register_business_application_routes(app, bot_token=BOT_TOKEN, admin_id=ADMIN_ID, ensure_schema=False)
    register_partner_direction_routes(app, db, bot=bot, ensure_schema=False)
    register_admin_stats_routes(app)
    register_marketplace_flow_routes(app, ensure_schema=False)
    register_client_routes(app, ai)
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
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("🌐 HTTP-сервер запущен на порту %s", port)
    logger.info("🛠️ Выполняем отложенные startup-мigration после bind порта")
    await asyncio.to_thread(_ensure_runtime_schema)
    logger.info("✅ Startup schema migration завершена")
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
    if not webhook_url:
        raise RuntimeError("TELEGRAM_WEBHOOK_URL or RENDER_EXTERNAL_URL is required; polling runtime has been removed")
    try:
        # Webhook is the single Render runtime mode. Clear any stale Telegram
        # getUpdates/poller session before registering the webhook.
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        logger.info("✅ Telegram webhook configured: %s", webhook_url)
    except Exception:
        logger.exception("Could not configure Telegram webhook")
        raise
    logger.info("📡 Webhook mode active; serving updates via /telegram/webhook")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())