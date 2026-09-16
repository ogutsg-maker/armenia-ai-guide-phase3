"""
marketplace_bot — Гибридный маркетплейс локальных услуг в Армении.
AI-диспетчер (Groq) + Idram-платежи + анонимный чат с модерацией + торги.

Точка входа: совмещённый aiohttp-сервер (API + статика Web Apps)
             + aiogram long-polling бот.
"""
import os
import json
import asyncio
import logging
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import CommandStart, Command, CommandObject, Filter
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import WebAppInfo, MenuButtonWebApp

from config import (
    BOT_TOKEN, ADMIN_ID, WEBAPP_BASE_URL,
)
from database import DatabaseManager
from ai_service import GroqAI
from partner_registration_ai import extract as extract_partner_profile, match_subcategories, missing_question
from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data
import stage3_partner_verification as stage3_partner_verification
from stage3_partner_verification import register_stage3_routes

try:
    from master_cabinet_api import register_master_cabinet_routes
except ImportError:
    register_master_cabinet_routes = None
from states import RegistrationStates
from keyboards import (
    get_role_keyboard,
    get_master_categories_keyboard,
    get_categories_keyboard,
)

# ─── Инициализация ──────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

db = DatabaseManager()
ai = GroqAI()

BASE_DIR = Path(__file__).resolve().parent
WEB_APPS_DIR = BASE_DIR / "web_apps"


def webapp_url(path: str) -> str:
    return f"{WEBAPP_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def get_app_keyboard(path: str, text: str):
    builder = InlineKeyboardBuilder()
    builder.button(text=text, web_app=WebAppInfo(url=webapp_url(path)))
    builder.adjust(1)
    return builder.as_markup()


def get_welcome_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Բացել Armenia AI Guide / Открыть Armenia AI Guide",
        web_app=WebAppInfo(url=webapp_url("welcome.html")),
    )
    builder.adjust(1)
    return builder.as_markup()


def get_client_app_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👤 Բացել հաճախորդի բաժինը / Открыть раздел клиента",
        web_app=WebAppInfo(url=webapp_url("client.html")),
    )
    builder.adjust(1)
    return builder.as_markup()


def get_partner_app_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🏢 Բացել գործընկերոջ բաժինը / Открыть раздел партнёра",
        web_app=WebAppInfo(url=webapp_url("partner.html")),
    )
    builder.adjust(1)
    return builder.as_markup()


async def api_webapp_set_role(request: web.Request):
    """Set the selected role from the Welcome WebApp using Telegram initData."""
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        return web.json_response({"ok": False, "error": "telegram_init_data_required"}, status=401)
    try:
        telegram_user = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
        uid = int(telegram_user["id"])
    except (TelegramWebAppAuthError, KeyError, TypeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    role = str(data.get("role") or "").strip().lower()
    if role not in {"client", "master"}:
        return web.json_response({"ok": False, "error": "invalid_role"}, status=400)

    user = db.get_user(uid)
    if not user:
        db.register_user(uid, telegram_user.get("username") or f"user_{uid}", telegram_user.get("first_name") or "")

    db.update_user_field(uid, "role", role)
    lang = str(data.get("lang") or "").strip().lower()
    if lang in {"hy", "ru", "en"}:
        db.update_user_field(uid, "lang", lang)
    return web.json_response({"ok": True, "role": role, "telegram_id": uid})


async def _webapp_partner_auth(request: web.Request):
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": "telegram_init_data_required"}), content_type="application/json")
    try:
        telegram_user = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
        uid = int(telegram_user["id"])
    except (TelegramWebAppAuthError, KeyError, TypeError, ValueError) as exc:
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}), content_type="application/json")
    user = db.get_user(uid)
    if not user:
        db.register_user(uid, telegram_user.get("username") or f"user_{uid}", telegram_user.get("first_name") or "")
        user = db.get_user(uid) or {}
    db.update_user_field(uid, "role", "master")
    return uid, user


def _partner_state(uid: int):
    key = StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid)
    return FSMContext(storage=storage, key=key)


async def api_webapp_partner_start(request: web.Request):
    """Start partner onboarding and keep the conversation inside the WebApp."""
    uid, user = await _webapp_partner_auth(request)
    state = _partner_state(uid)
    current = await state.get_state()
    if current is None:
        await state.set_state(RegistrationStates.choosing_city)
        await state.update_data(partner_onboarding_history=[], partner_profile={}, partner_onboarding_pending_field=None)
    lang = (user or {}).get("lang") or "hy"
    return web.json_response({
        "ok": True,
        "started": True,
        "telegram_id": uid,
        "message": t(lang,
            "🏢 <b>Գրանցենք ձեր բիզնեսը</b>\n\nՊատմեք ազատ ձևով՝ ինչպես է կոչվում բիզնեսը, որտեղ է գտնվում, ինչ ծառայություններ եք մատուցում և ինչ գներով։ Ես կճանաչեմ ուղղությունը, ենթաուղղությունները և ծառայությունները։",
            "🏢 <b>Зарегистрируем ваш бизнес</b>\n\nРасскажите свободно: как называется бизнес, где находится, какие услуги вы оказываете и какие у них цены. Я сам определю направление, подкатегории и услуги.",
            "🏢 <b>Let’s register your business</b>\n\nTell me naturally what the business is called, where it is located, what services you offer and their prices. I will determine the direction, subcategories and services."),
        "completed": False,
    })



async def api_admin_partner_document_open(request: web.Request):
    """Reliable admin document opener.

    The deployed Stage-3 module may be an older revision where the public
    /documents/{doc_id}/url route is missing.  Keep the existing verification
    system untouched and expose a compatibility endpoint from main.py.
    """
    try:
        admin_id = stage3_partner_verification._admin_telegram_id(
            request,
            BOT_TOKEN,
            ADMIN_ID,
        )
        pid = int(request.match_info["id"])
        doc_id = int(request.match_info["doc_id"])

        row = stage3_partner_verification._db_fetchone(
            """
            SELECT id, partner_id, storage_path, file_data
            FROM partner_verification_documents
            WHERE id=%s AND partner_id=%s
            """,
            (doc_id, pid),
        )
        if not row:
            return web.json_response(
                {"ok": False, "error": "document_not_found"}, status=404
            )

        if row.get("storage_path"):
            try:
                url = await stage3_partner_verification._storage_signed_url(
                    row["storage_path"], 900
                )
                return web.json_response({
                    "ok": True,
                    "admin_id": admin_id,
                    "url": url,
                    "source": "storage",
                    "expires_in": 900,
                })
            except Exception as exc:
                logger.warning(
                    "Document signed URL failed, trying database fallback: %s",
                    exc,
                )

        if row.get("file_data") is not None:
            # Return a one-time-ish same-auth API URL. The admin page will
            # fetch it with Telegram initData and open the resulting Blob.
            return web.json_response({
                "ok": True,
                "admin_id": admin_id,
                "url": f"/api/admin/partner-applications/{pid}/documents/{doc_id}/open-file",
                "source": "database",
            })

        return web.json_response(
            {"ok": False, "error": "document_file_not_available"}, status=404
        )
    except web.HTTPException:
        raise
    except Exception as exc:
        logger.exception("Admin document open failed")
        return web.json_response(
            {"ok": False, "error": "document_open_failed", "details": str(exc)[:500]},
            status=500,
        )


async def api_admin_partner_document_open_file(request: web.Request):
    """Stream a database-fallback document to an authenticated admin."""
    try:
        stage3_partner_verification._admin_telegram_id(request, BOT_TOKEN, ADMIN_ID)
        pid = int(request.match_info["id"])
        doc_id = int(request.match_info["doc_id"])
        row = stage3_partner_verification._db_fetchone(
            """
            SELECT original_filename, mime_type, file_data
            FROM partner_verification_documents
            WHERE id=%s AND partner_id=%s
            """,
            (doc_id, pid),
        )
        if not row or row.get("file_data") is None:
            return web.json_response(
                {"ok": False, "error": "document_file_not_available"}, status=404
            )
        filename = str(row.get("original_filename") or "document").replace('"', "")
        return web.Response(
            body=bytes(row["file_data"]),
            content_type=row.get("mime_type") or "application/octet-stream",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except web.HTTPException:
        raise
    except Exception as exc:
        logger.exception("Admin document download failed")
        return web.json_response(
            {"ok": False, "error": "document_download_failed", "details": str(exc)[:500]},
            status=500,
        )


async def api_webapp_partner_message(request: web.Request):
    """Process one partner onboarding message without leaving the WebApp."""
    uid, user = await _webapp_partner_auth(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    text = str(payload.get("text") or "").strip()
    if len(text) < 2:
        return web.json_response({"ok": False, "error": "message_too_short"}, status=400)
    state = _partner_state(uid)
    current = await state.get_state()
    if current is None:
        await state.set_state(RegistrationStates.choosing_city)
        await state.update_data(partner_onboarding_history=[], partner_profile={}, partner_onboarding_pending_field=None)
    result = await _process_partner_onboarding_text(uid, text, state)
    return web.json_response({"ok": True, **result})


# ─── TELEGRAM WEB APP AUTH / PARTNER GATING ─────────────────────

@web.middleware
async def telegram_webapp_auth_middleware(request: web.Request, handler):
    """Authenticate Telegram WebApp requests and gate partner cabinet by approval status."""
    path = request.path

    if not path.startswith("/api/master/"):
        return await handler(request)

    if request.method == "OPTIONS":
        return await handler(request)

    raw_init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw_init_data:
        return web.json_response(
            {"ok": False, "error": "telegram_init_data_required"},
            status=401,
        )

    try:
        telegram_user = validate_telegram_webapp_init_data(
            raw_init_data,
            BOT_TOKEN,
        )
        telegram_id = int(telegram_user["id"])
        route_user_id = int(path.split("/")[3])
    except (
        TelegramWebAppAuthError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        return web.json_response(
            {
                "ok": False,
                "error": str(exc) or "invalid_telegram_init_data",
            },
            status=401,
        )

    if telegram_id != route_user_id:
        return web.json_response(
            {
                "ok": False,
                "error": "telegram_user_mismatch",
            },
            status=403,
        )

    request["telegram_user"] = telegram_user
    request["telegram_user_id"] = telegram_id

    parts = path.split("/", 4)
    tail = parts[4] if len(parts) > 4 else ""

    # Эти endpoints доступны до одобрения партнёра.
    if tail in (
        "registration-status",
        "register",
        "documents",
        "documents/upload",
    ):
        return await handler(request)

    try:
        partner = db.get_partner_by_user(telegram_id)
    except Exception:
        logger.exception("Partner status lookup failed for %s", telegram_id)
        partner = None

    if not partner:
        return web.json_response(
            {
                "ok": False,
                "error": "partner_registration_required",
                "status": "not_registered",
            },
            status=403,
        )

    status = str(partner.get("status") or "pending")

    if status != "approved":
        return web.json_response(
            {
                "ok": False,
                "error": "partner_not_approved",
                "status": status,
                "verification_status": partner.get("verification_status"),
            },
            status=403,
        )

    return await handler(request)


async def api_partner_registration_status(request: web.Request):
    uid = int(request.match_info["id"])
    partner = db.get_partner_by_user(uid)

    if not partner:
        return web.json_response(
            {"ok": True, "registered": False, "status": "not_registered"}
        )

    return web.json_response({
        "ok": True,
        "registered": True,
        "partner_id": partner.get("id"),
        "status": partner.get("status"),
        "verification_status": partner.get("verification_status"),
        "business_name": partner.get("business_name") or "",
        "business_description": partner.get("business_description") or "",
        "rejection_reason": partner.get("rejection_reason") or "",
    })


async def api_partner_register(request: web.Request):
    uid = int(request.match_info["id"])

    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "invalid_json"},
            status=400,
        )

    business_name = str(data.get("business_name") or "").strip()[:200]
    business_description = str(
        data.get("business_description") or ""
    ).strip()[:3000]

    if len(business_name) < 2:
        return web.json_response(
            {"ok": False, "error": "business_name_required"},
            status=400,
        )

    partner = db.get_partner_by_user(uid)

    if partner:
        status = str(partner.get("status") or "pending")

        if status == "approved":
            return web.json_response({
                "ok": True,
                "registered": True,
                "status": status,
                "partner_id": partner.get("id"),
            })

        if status in ("pending", "under_review"):
            return web.json_response({
                "ok": True,
                "registered": True,
                "status": status,
                "partner_id": partner.get("id"),
            })

        partner_id = partner.get("id")
        db.update_partner(
            partner_id,
            business_name=business_name,
            business_description=business_description,
            status="pending",
            verification_status="not_submitted",
        )
    else:
        partner_id = db.create_partner(uid)
        db.update_partner(
            partner_id,
            business_name=business_name,
            business_description=business_description,
            status="pending",
            verification_status="not_submitted",
        )

    return web.json_response({
        "ok": True,
        "registered": True,
        "status": "pending",
        "partner_id": partner_id,
        "message": "Դիմումը ուղարկված է ադմինիստրատորի ստուգմանը։",
    })


async def serve_index(request: web.Request):
    """Публичная главная страница Web App."""
    index_file = WEB_APPS_DIR / "index.html"

    if not index_file.is_file():
        logger.error("Главная страница не найдена: %s", index_file)
        return web.json_response(
            {"ok": False, "error": "index.html_not_found"},
            status=500,
        )

    return web.FileResponse(index_file)


async def health(request: web.Request):
    return web.json_response({"ok": True})


# ─── Фильтры ────────────────────────────────────────────────────

class IsAdmin(Filter):
    async def __call__(self, message: types.Message) -> bool:
        return message.from_user.id == ADMIN_ID


# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────

def t(lang: str, hy: str, ru: str, en: str) -> str:
    """Мультиязычный текст по ключу lang."""
    return {"hy": hy, "ru": ru, "en": en}.get(lang, ru)


async def save_voice_temp(voice: types.Voice) -> str:
    """Скачивает голосовое сообщение во временный файл."""
    file = await bot.get_file(voice.file_id)
    suffix = ".ogg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        await bot.download_file(file.file_path, tmp.name)
        return tmp.name


@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    stats = db.get_admin_stats()
    await message.answer(
        f"👑 **АДМИН-ПАНЕЛЬ**\n\n"
        f"👥 Пользователей: {stats.get('total_users', 0)}\n"
        f"🛠 Мастеров: {stats.get('total_masters', 0)} (✅ {stats.get('verified_masters', 0)})\n"
        f"📦 Заказов: {stats.get('total_orders', 0)}\n"
        f"✅ Выполнено: {stats.get('completed_orders', 0)}\n"
        f"💰 Комиссия: {float(stats.get('total_commission', 0)):,.0f} ֏\n"
        f"⚖️ Споров: {stats.get('open_disputes', 0)}",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(Command("admin_panel"))
async def cmd_admin_panel(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👑 Открыть панель",
        web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/admin.html"),
    )
    await message.answer("Админ-панель:", reply_markup=builder.as_markup())


@router.message(Command("reset"))
async def cmd_reset(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    db.update_user_field(uid, "role", None)
    db.update_user_field(uid, "city", None)
    await state.clear()
    await message.answer("🔄 Профиль сброшен. Напишите /start для повторной настройки.")
# ═══════════════════════════════════════════════════════════════
# 1. РЕГИСТРАЦИЯ И /START
# ═══════════════════════════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    username = message.from_user.username or f"user_{uid}"
    full_name = message.from_user.full_name or ""

    # --- Регистрация ---
    db.register_user(uid, username, full_name)
    user = db.get_user(uid)

    if user and user.get("role") is not None:
        role = user["role"]
        if role == "master":
            await message.answer(
                "🏢 Armenia AI Guide — Ձեր գործընկերոջ բաժինը\n\nԲացեք ձեր գործընկերոջ էջը՝ շարունակելու աշխատանքը:",
                reply_markup=get_partner_app_keyboard(),
            )
        else:
            await message.answer(
                "👤 Armenia AI Guide — Ձեր AI օգնականը\n\nԲացեք հաճախորդի էջը՝ ծառայություն գտնելու և ամրագրելու համար:",
                reply_markup=get_client_app_keyboard(),
            )
        return

    # --- Первый вход: всегда красивая Welcome WebApp, без запуска AI-чата. ---
    await state.clear()
    await message.answer(
        "✦ <b>Armenia AI Guide</b>\n"
        "<i>ARMENIA · AI CONCIERGE</i>\n\n"
        "Ձեր AI օգնականը ծառայություններ գտնելու, ընտրելու և ամրագրման հարցերում։\n\n"
        "Սկսելու համար բացեք Armenia AI Guide-ը։ / Откройте Armenia AI Guide, чтобы начать.",
        reply_markup=get_welcome_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("lang_"))
async def process_lang(callback: types.CallbackQuery, state: FSMContext):
    lang = callback.data.replace("lang_", "")
    db.update_user_field(callback.from_user.id, "lang", lang)
    await state.set_state(RegistrationStates.choosing_role)
    await callback.message.edit_text(
        t(lang,
          "Բարև Ձեզ! Ողջունում ենք ԻԻ-Մարկետփլեյսում: 🇦🇲\nԸնտրեք Ձեր դերը:",
          "Здравствуйте! Добро пожаловать на ИИ-Маркетплейс. 🇦🇲\nВыберите вашу роль:",
          "Hello! Welcome to AI Marketplace. 🇦🇲\nChoose your role:"),
        reply_markup=get_role_keyboard(),
    )
    await callback.answer()


@router.callback_query(RegistrationStates.choosing_role, F.data.startswith("role_"))
async def process_role(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "hy")
    chosen = callback.data.replace("role_", "")

    if chosen == "client":
        db.update_user_field(uid, "role", "client")
        await state.clear()
        await callback.message.edit_text(
            t(lang,
              "🎉 Դուք հաճախորդ եք: Նկարագրեք ձեր խնդիրը, և ԻԻ-ն կգտնի վարպետին:",
              "🎉 Вы — Клиент. Опишите задачу, и ИИ найдёт мастера!",
              "🎉 You are a Client. Describe your task and AI will find a master!"),
        )
    elif chosen == "master":
        db.update_user_field(uid, "role", "master")
        await state.set_state(RegistrationStates.choosing_city)
        await state.update_data(partner_onboarding_history=[], partner_profile={}, partner_onboarding_pending_field=None)
        await callback.message.edit_text(
            t(lang,
              "🏢 <b>Գրանցենք ձեր բիզնեսը</b>\n\nՊատմեք ազատ ձևով՝ ինչպես է կոչվում բիզնեսը, որտեղ է գտնվում, ինչ ծառայություններ եք մատուցում և ինչ գներով։ Ես կճանաչեմ ուղղությունը, ենթաուղղությունները և ծառայությունները։",
              "🏢 <b>Зарегистрируем ваш бизнес</b>\n\nРасскажите свободно: как называется бизнес, где находится, какие услуги вы оказываете и какие у них цены. Я сам определю направление, подкатегории и услуги.",
              "🏢 <b>Let’s register your business</b>\n\nTell me naturally what the business is called, where it is located, what services you offer and their prices. I will determine the direction, subcategories and services."),
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.message(F.web_app_data)
async def handle_webapp_data(message: types.Message, state: FSMContext):
    """Commands sent by role-specific WebApps back to the Telegram bot."""
    try:
        payload = json.loads(message.web_app_data.data or "{}")
    except Exception:
        payload = {"action": message.web_app_data.data}

    action = str(payload.get("action") or "").strip().lower()
    uid = message.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "hy")

    if action == "partner_register":
        db.update_user_field(uid, "role", "master")
        await state.clear()
        await state.set_state(RegistrationStates.choosing_city)
        await state.update_data(partner_onboarding_history=[], partner_profile={}, partner_onboarding_pending_field=None)
        await message.answer(
            t(lang,
              "🏢 <b>Գրանցենք ձեր բիզնեսը</b>\n\nՊատմեք ազատ ձևով՝ ինչպես է կոչվում բիզնեսը, որտեղ է գտնվում, ինչ ծառայություններ եք մատուցում և ինչ գներով։ Ես կճանաչեմ ուղղությունը, ենթաուղղությունները և ծառայությունները։",
              "🏢 <b>Зарегистрируем ваш бизнес</b>\n\nРасскажите свободно: как называется бизнес, где находится, какие услуги вы оказываете и какие у них цены. Я сам определю направление, подкатегории и услуги.",
              "🏢 <b>Let’s register your business</b>\n\nTell me naturally what the business is called, where it is located, what services you offer and their prices. I will determine the direction, subcategories and services."),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "client_open":
        db.update_user_field(uid, "role", "client")
        await state.clear()
        await message.answer(
            t(lang,
              "👤 Բարի գալուստ։ Բացեք հաճախորդի բաժինը։",
              "👤 Добро пожаловать. Откройте раздел клиента.",
              "👤 Welcome. Open the client section."),
            reply_markup=get_client_app_keyboard(),
        )


async def _process_partner_onboarding_text(uid: int, text: str, state: FSMContext) -> dict:
    user = db.get_user(uid) or {}
    lang = user.get("lang", "hy")
    data = await state.get_data()
    history = data.get("partner_onboarding_history") or []
    pending_field = data.get("partner_onboarding_pending_field")
    previous_profile = data.get("partner_profile") or {}
    history.append({"role": "user", "content": text})

    profile = await extract_partner_profile(text, history, db, previous_profile=previous_profile, pending_field=pending_field)
    merged = dict(previous_profile)
    for key, value in (profile or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    profile = merged
    if pending_field in {"business_name", "city", "district", "direction"} and text:
        profile[pending_field] = text.strip()
    elif pending_field == "services" and text and not profile.get("services"):
        profile["services"] = [{"name": text.strip(), "price": None, "price_type": "unknown"}]
    city_hint = data.get("partner_city_hint")
    if city_hint and not profile.get("city"):
        profile["city"] = city_hint
    required_missing = [k for k in ("business_name", "city", "direction", "services") if not profile.get(k)]
    profile["missing"] = required_missing
    profile["ready"] = not required_missing
    await state.update_data(partner_onboarding_history=history, partner_profile=profile)

    if not profile.get("ready"):
        question = missing_question(profile, lang)
        next_field = (profile.get("missing") or [None])[0]
        await state.update_data(partner_onboarding_pending_field=next_field)
        history.append({"role": "assistant", "content": question})
        await state.update_data(partner_onboarding_history=history)
        return {"message": t(lang, f"🤖 Ես արդեն հավաքել եմ ձեր ասած տվյալները։ {question}", f"🤖 Я уже собрал то, что вы рассказали. {question}", f"🤖 I have collected the information you gave me. {question}"), "completed": False, "profile": profile}

    name = str(profile.get("business_name") or "").strip()[:200]
    city = str(profile.get("city") or "").strip()[:200]
    direction = str(profile.get("direction") or "").strip()[:200]
    description = str(profile.get("description") or "").strip()
    services = profile.get("services") or []
    partner = db.get_partner_by_user(uid)
    if partner:
        partner_id = partner.get("id")
        db.update_partner(partner_id, business_name=name, business_description=description, status="pending", verification_status="not_submitted")
    else:
        partner_id = db.create_partner(uid)
        db.update_partner(partner_id, business_name=name, business_description=description, status="pending", verification_status="not_submitted")
    db.update_user_field(uid, "city", city)
    category_ids = match_subcategories(db, profile.get("subcategory_names") or [])
    if category_ids:
        try:
            db.set_master_categories(uid, category_ids)
        except Exception:
            logger.exception("Could not save AI-selected partner categories for %s", uid)
    service_lines = []
    for item in services:
        if not isinstance(item, dict):
            continue
        n = str(item.get("name") or "").strip()
        if not n:
            continue
        price = item.get("price")
        service_lines.append(f"• {n} — {price} ֏" if price not in (None, "") else f"• {n}")
    summary = [f"🏢 {name}", f"📍 {city}", f"🧭 {direction}"]
    if profile.get("district"):
        summary.append(f"📌 {profile['district']}")
    if service_lines:
        summary.append("\n🛠 Ծառայություններ / Услуги:\n" + "\n".join(service_lines[:20]))
    await state.update_data(partner_onboarding_pending_field=None, partner_profile=profile)
    return {"message": t(lang,
        "✅ Բիզնեսի տվյալները ճանաչեցի և պահպանեցի։\n\n" + "\n".join(summary) + "\n\n📄 Հաջորդ քայլը՝ բիզնեսը հաստատելու փաստաթուղթը բեռնեք հենց այս էջում։",
        "✅ Я распознал и сохранил данные бизнеса.\n\n" + "\n".join(summary) + "\n\n📄 Следующий шаг — загрузите подтверждающий документ прямо на этой странице.",
        "✅ I recognized and saved your business information.\n\n" + "\n".join(summary) + "\n\n📄 Next step: upload the verification document directly on this page."), "completed": True, "partner_id": partner_id, "profile": profile}


async def _partner_onboarding_message(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 2:
        return
    uid = message.from_user.id
    await bot.send_chat_action(uid, "typing")
    result = await _process_partner_onboarding_text(uid, text, state)
    await message.answer(result["message"], parse_mode=ParseMode.HTML)
    if result.get("completed"):
        await state.clear()


# --- Город (быстрый выбор или текстовый ввод) ---

@router.callback_query(RegistrationStates.choosing_city, F.data.startswith("city_"))
async def process_city_quick(callback: types.CallbackQuery, state: FSMContext):
    city = callback.data.replace("city_", "")
    uid = callback.from_user.id
    db.update_user_field(uid, "city", city)
    await _show_categories_selection(uid, state, callback.message, city)
    await callback.answer()


@router.message(RegistrationStates.choosing_city)
async def process_city_text(message: types.Message, state: FSMContext):
    await _partner_onboarding_message(message, state)


async def _show_categories_selection(uid: int, state: FSMContext, msg, city: str):
    """Показывает мастеру сферы деятельности для выбора специализации."""
    await state.update_data(selected_cats=[])
    await state.set_state(RegistrationStates.choosing_categories)
    
    master_cats = db.get_all_master_categories()
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "ru")
    
    await msg.answer(
        f"📍 Քաղաքը գրանցված է / Город сохранен: **{city}**\n\n"
        f"Ընտրեք ուղղությունը / Выберите сферу деятельности:",
        reply_markup=get_master_categories_keyboard(master_cats, lang),
        parse_mode=ParseMode.MARKDOWN,
    )


# --- Обработчики двухуровневых категорий ---

@router.callback_query(RegistrationStates.choosing_categories, F.data.startswith("select_mcat_"))
async def process_select_master_category(callback: types.CallbackQuery, state: FSMContext):
    """ЭТАП 2: Мастер нажал на главную сферу. Показываем подкатегории этой сферы."""
    master_category_id = int(callback.data.replace("select_mcat_", ""))
    await state.update_data(current_master_category_id=master_category_id)
    data = await state.get_data()
    selected = data.get("selected_cats", [])
    
    uid = callback.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "ru")
    
    subcategories = db.get_subcategories_by_master(master_category_id)
    if not subcategories:
        await callback.answer("⚠️ В этой сфере пока нет подкатегорий!", show_alert=True)
        return
        
    await callback.message.edit_text(
        t(lang,
          "Ընտրեք կոնկրետ ուղղությունները (կարող եք ընտրել մի քանիսը):",
          "Выберите конкретные подкатегории (можно выбрать несколько):",
          "Select specific subcategories (you can choose multiple):"),
        reply_markup=get_categories_keyboard(subcategories, selected, lang)
    )
    await callback.answer()


@router.callback_query(RegistrationStates.choosing_categories, F.data.startswith("mcat_"))
async def toggle_category(callback: types.CallbackQuery, state: FSMContext):
    """ЭТАП 3: Мастер нажимает на конкретную подкатегорию (включение/выключение галочки)."""
    cat_id = int(callback.data.replace("mcat_", ""))
    data = await state.get_data()
    selected = data.get("selected_cats", [])
    current_mcat_id = data.get("current_master_category_id", 1)
    
    if cat_id in selected:
        selected.remove(cat_id)
    else:
        selected.append(cat_id)
        
    await state.update_data(selected_cats=selected)
    
    uid = callback.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "ru")
    
    cats = db.get_subcategories_by_master(current_mcat_id)
    await callback.message.edit_reply_markup(
        reply_markup=get_categories_keyboard(cats, selected, lang)
    )
    await callback.answer("✅" if cat_id in selected else "❌")


@router.callback_query(RegistrationStates.choosing_categories, F.data == "back_to_mcat")
async def process_back_to_master_categories(callback: types.CallbackQuery, state: FSMContext):
    """Кнопка 'Назад': возвращает мастера от списка услуг к главным сферам."""
    master_cats = db.get_all_master_categories()
    uid = callback.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "ru")
    
    await callback.message.edit_text(
        f"Ընտրեք ուղղությունը / Выберите сферу деятельности:",
        reply_markup=get_master_categories_keyboard(master_cats, lang)
    )
    await callback.answer()


@router.callback_query(RegistrationStates.choosing_categories, F.data == "cats_done")
async def finish_registration(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    data = await state.get_data()
    selected = data.get("selected_cats", [])
    if not selected:
        await callback.answer("⚠️ Выберите хотя бы одну категорию!", show_alert=True)
        return
    db.set_master_categories(uid, selected)
    await state.clear()
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "hy")
    await callback.message.edit_text(
        t(lang,
          "🎉 Գրանցումն ավարտված է: Սպասեք հայտեր:",
          "🎉 Регистрация завершена! Ожидайте заявки.",
          "🎉 Registration complete! Await orders."),
    )
    await callback.answer()
# ═══════════════════════════════════════════════════════════════
# 2. ХЕНДЛЕРЫ КНОПОК МЕНЮ (Reply клавиатура)
# ═══════════════════════════════════════════════════════════════





# ═══════════════════════════════════════════════════════════════
# 3. ОБРАБОТКА ЗАПРОСОВ КЛИЕНТА (ГОЛОС И ТЕКСТ)
# ═══════════════════════════════════════════════════════════════





# ═══════════════════════════════════════════════════════════════
# 4. МАСТЕР ОТКЛИКАЕТСЯ → ТОРГИ
# ═══════════════════════════════════════════════════════════════







# ═══════════════════════════════════════════════════════════════
# 5. КЛИЕНТ: ПРИНЯТЬ / ПОТОРГОВАТЬСЯ / ОТКЛОНИТЬ
# ═══════════════════════════════════════════════════════════════







# ═══════════════════════════════════════════════════════════════
# 6. ОПЛАТА ЧЕРЕЗ IDRAM → ВАУЧЕР И QR-КОДЫ
# ═══════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════
# 7. ЗАКРЫТИЕ СДЕЛКИ ВРУЧНУЮ
# ═══════════════════════════════════════════════════════════════









# ═══════════════════════════════════════════════════════════════
# 8. ОЦЕНКА МАСТЕРА И АНОНИМНЫЙ ЧАТ
# ═══════════════════════════════════════════════════════════════





@router.message(F.text == "📂 Ուղղություններ / Направления")
async def open_master_cabinet(message: types.Message):
    """Вход в кабинет Web App мастера."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📂 Բացել սենյակը / Открыть кабинет", web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/master_cabinet.html"))
    await message.answer("Нажмите для входа в кабинет:", reply_markup=builder.as_markup())


# ═══════════════════════════════════════════════════════════════
# 9. АРБИТРАЖ / СПОРЫ И СИМУЛЯЦИЯ
# ═══════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════
# 10. API ЭНДПОИНТЫ (REST API ДЛЯ WEB APPS И АДМИН-ПАНЕЛИ)
# ═══════════════════════════════════════════════════════════════









# --- API ДЛЯ АДМИН-ПАНЕЛИ (СТАТИСТИКА, МОДЕРАЦИЯ И CRUD КАТАЛОГА) ---

async def api_admin_stats(request):
    """GET /api/admin/stats — Безопасная отдача общей операционной и финансовой статистики."""
    try:
        stats = db.get_admin_stats() or {}
        return web.json_response({
            "total_users": int(stats.get("total_users") or 0),
            "total_masters": int(stats.get("total_masters") or 0),
            "verified_masters": int(stats.get("verified_masters") or 0),
            "total_orders": int(stats.get("total_orders") or 0),
            "completed_orders": int(stats.get("completed_orders") or 0),
            "total_commission": float(stats.get("total_commission") or 0),
            "open_disputes": int(stats.get("open_disputes") or 0)
        })
    except Exception as e:
        return web.json_response({"total_users":0,"total_masters":0,"verified_masters":0,"total_orders":0,"completed_orders":0,"total_commission":0,"open_disputes":0})

async def api_admin_users(request):
    """GET /api/admin/users — Список всех пользователей с приведением типов для фронтенда."""
    try:
        users = db.get_all_users()
        formatted = []
        for u in users:
            created_str = str(u.get("created_at", ""))[:19] if u.get("created_at") else ""
            formatted.append({
                "telegram_id": u.get("telegram_id"), "username": u.get("username", "N/A"),
                "full_name": u.get("full_name", ""), "role": u.get("role"), "lang": u.get("lang", "hy"),
                "city": u.get("city"), "phone": u.get("phone"), "is_verified": bool(u.get("is_verified", False)),
                "is_frozen": bool(u.get("is_frozen", False)), "balance": float(u.get("balance", 0) or 0), "created_at": created_str
            })
        return web.json_response(formatted)
    except Exception as e:
        return web.json_response([], status=500)

async def api_admin_categories(request):
    """GET /api/admin/categories — Древовидная отдача структуры каталога с именами родителей."""
    try:
        categories = db.get_all_categories()
        formatted = []
        for cat in categories:
            formatted.append({
                "id": cat.get("id"), "master_category_id": cat.get("master_category_id"),
                "name_hy": cat.get("name_hy", ""), "name_ru": cat.get("name_ru", ""),
                "name_en": cat.get("name_en", ""), "commission_type": cat.get("commission_type", "on_top"),
                "commission_value": float(cat.get("commission_value", 10) or 0), "is_active": cat.get("is_active", True),
                "master_name_ru": cat.get("master_name_ru", ""), "master_name_am": cat.get("master_name_am", "")
            })
        return web.json_response(formatted)
    except Exception as e:
        return web.json_response([], status=500)

async def api_admin_category_update(request):
    """POST /api/admin/category/{id} — Изменение параметров (комиссии, активности) подкатегории."""
    cat_id = int(request.match_info["id"])
    data = await request.json()
    db.update_category(cat_id, **data)
    return web.json_response({"ok": True})

async def api_admin_master_category_create(request):
    """POST /api/admin/master_category — Создание новой родительской сферы."""
    data = await request.json()
    new_id = db.create_master_category(data['name_ru'], data['name_am'], data['slug'])
    return web.json_response({"ok": True, "id": new_id})

async def api_admin_master_category_update(request):
    """POST /api/admin/master_category/{id} — Редактирование существующей родительской сферы."""
    mcat_id = int(request.match_info["id"])
    data = await request.json()
    db.update_master_category(mcat_id, **data)
    return web.json_response({"ok": True})

async def api_admin_master_category_delete(request):
    """DELETE /api/admin/master_category/{id} — Удаление родительской сферы (каскадное)."""
    mcat_id = int(request.match_info["id"])
    db.delete_master_category(mcat_id)
    return web.json_response({"ok": True})

async def api_admin_subcategory_create(request):
    """POST /api/admin/subcategory — Создание новой подкатегории услуг."""
    data = await request.json()
    new_id = db.create_subcategory(
        master_category_id=int(data['master_category_id']), name_ru=data['name_ru'],
        name_am=data['name_am'], slug=data['slug'], commission_type=data.get('commission_type', 'on_top'),
        commission_value=float(data.get('commission_value', 10.0))
    )
    return web.json_response({"ok": True, "id": new_id})

async def api_admin_subcategory_delete(request):
    """DELETE /api/admin/subcategory/{id} — Удаление конкретной услуги мастера."""
    cat_id = int(request.match_info["id"])
    db.delete_subcategory(cat_id)
    return web.json_response({"ok": True})


async def api_admin_user_action(request):
    """POST /api/admin/user/{id}/{action} — Действия модератора над пользователем."""
    uid = int(request.match_info["id"])
    action = request.match_info["action"]
    if action == "verify":
        db.update_user_field(uid, "is_verified", True)
    elif action == "freeze":
        current = db.get_user(uid)
        db.update_user_field(uid, "is_frozen", not (current or {}).get("is_frozen", False))
    elif action == "delete":
        db.delete_user(uid)
    return web.json_response({"ok": True})



# ═══════════════════════════════════════════════════════════════
# ЗАПУСК HTTP-СЕРВЕРА И TG БОТА (ОБЩИЙ ЦИКЛ)
# ═══════════════════════════════════════════════════════════════

async def main():
    logger.info("🚀 Запуск ИИ-Маркетплейса Армении...")

    app = web.Application(
        middlewares=[telegram_webapp_auth_middleware]
    )

    # Health / public endpoints
    app.router.add_get("/health", health)

    # Partner registration — доступно до одобрения партнёра.
    app.router.add_get(
        "/api/master/{id}/registration-status",
        api_partner_registration_status,
    )
    app.router.add_post(
        "/api/master/{id}/register",
        api_partner_register,
    )

    # Legacy master endpoints removed. Partner cabinet endpoints are owned by
    # register_master_cabinet_routes (catalogue, services, objects, employees,
    # bookings, check-in, finance, history).

    # Admin base endpoints.
    app.router.add_get("/api/admin/stats", api_admin_stats)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_get("/api/admin/categories", api_admin_categories)
    app.router.add_post("/api/admin/category/{id}", api_admin_category_update)
    app.router.add_post("/api/admin/user/{id}/{action}", api_admin_user_action)

    # Admin catalogue CRUD.
    app.router.add_post("/api/admin/master_category", api_admin_master_category_create)
    app.router.add_post("/api/admin/master_category/{id}", api_admin_master_category_update)
    app.router.add_delete("/api/admin/master_category/{id}", api_admin_master_category_delete)
    app.router.add_post("/api/admin/subcategory", api_admin_subcategory_create)
    app.router.add_delete("/api/admin/subcategory/{id}", api_admin_subcategory_delete)

    # Compatibility document-open routes (for older Stage-3 deployments).
    # NB: the .../documents/{doc_id}/url route is owned by register_stage3_routes
    # (api_admin_partner_document_url); do not re-register it here or it will
    # shadow the Stage-3 handler. The frontend uses .../open.
    app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/open", api_admin_partner_document_open)
    app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/open-file", api_admin_partner_document_open_file)

    # Stage 3 — partner verification / admin moderation.
    register_stage3_routes(
        app,
        bot_token=BOT_TOKEN,
        admin_id=ADMIN_ID,
    )
    logger.info("✅ Stage 3 verification routes registered")

    # New partner cabinet.
    if register_master_cabinet_routes is not None:
        try:
            register_master_cabinet_routes(app, db, bot=bot)
            logger.info("✅ Partner cabinet API registered")
        except Exception:
            logger.exception("Не удалось зарегистрировать Partner cabinet API")

    # Welcome WebApp role selection. This endpoint validates Telegram initData.
    app.router.add_post("/api/webapp/role", api_webapp_set_role)
    app.router.add_post("/api/webapp/partner/start", api_webapp_partner_start)
    app.router.add_post("/api/webapp/partner/message", api_webapp_partner_message)

    # Public home page — explicitly serve index.html.
    app.router.add_get("/", serve_index)

    # Static Web App assets.
    app.router.add_static(
        "/",
        path=str(WEB_APPS_DIR),
        name="web_apps",
    )

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("🌐 HTTP-сервер запущен на порту %s", port)

    # Telegram bottom menu: one-tap entry into the beautiful Welcome WebApp.
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Присоединиться",
                web_app=WebAppInfo(url=webapp_url("welcome.html")),
            )
        )
        logger.info("✅ Telegram bottom menu button configured")
    except Exception:
        logger.exception("Не удалось настроить Telegram bottom menu button")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
