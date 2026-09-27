from aiohttp import web
import json, os
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError
from client_ai import ClientAI
from ai_router import AIRouter
import data_core
import data_core
