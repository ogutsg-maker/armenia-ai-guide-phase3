"""
Глобальная конфигурация проекта.
Все настройки загружаются из .env с fallback-значениями.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load the platform runtime bridge before main creates aiohttp.Application.
import runtime_platform_bootstrap  # noqa: F401

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Telegram polling must never compete with an active webhook.
# The bridge wraps Dispatcher.start_polling and removes any stale webhook
# immediately before polling starts.
import polling_bootstrap  # noqa: F401,E402
import admin_access  # noqa: F401,E402

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")

ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Default Groq model used by the unified GroqAI service. Override per-deploy
# via the GROQ_MODEL env var. Per-contour models can still be overridden
# separately (e.g. PARTNER_ONBOARDING_MODEL for the onboarding extractor).
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан в .env")

IDRAM_MERCHANT_ID = os.getenv("IDRAM_MERCHANT_ID", "test")
IDRAM_SECRET_KEY = os.getenv("IDRAM_SECRET_KEY", "test")
IDRAM_SUCCESS_URL = os.getenv("IDRAM_SUCCESS_URL", "")
IDRAM_FAIL_URL = os.getenv("IDRAM_FAIL_URL", "")
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "https://your-domain.com")
SUPPORTED_LANGS = {"hy", "ru", "en"}
DEFAULT_LANG = "hy"
CITY_SYNONYMS = {
    "ереван": ["ереван", "երևան", "yerevan", "erevan"],
    "гюмри": ["гюмри", "գյումրի", "gyumri", "gumri"],
    "ванадзор": ["ванадзор", "վանաձոր", "vanadzor"],
    "дилижан": ["дилижан", "դիլիջան", "dilijan"],
    "севан": ["севан", "սևան", "sevan"],
    "тбилиси": ["тбилиси", "թբիլիսի", "tbilisi"],
}
COMMISSION_INSIDE = "inside"
COMMISSION_ON_TOP = "on_top"
COMMISSION_FIXED = "fixed"
COMMISSION_TYPES = [COMMISSION_INSIDE, COMMISSION_ON_TOP, COMMISSION_FIXED]
