"""Reliable per-admin Telegram WebApp menu button."""
from __future__ import annotations
import asyncio
import os
from aiohttp import web
from aiogram.types import MenuButtonWebApp, WebAppInfo

_original = web.Application.__init__

async def _set_admin_menu(bot):
    await asyncio.sleep(2)
    raw = (os.getenv("ADMIN_TELEGRAM_ID") or os.getenv("ADMIN_ID") or "").strip()
    base = (os.getenv("WEBAPP_BASE_URL") or "").strip().rstrip("/")
    if not raw or not base or not bot:
        return
    try:
        admin_id = int(raw)
        await bot.set_chat_menu_button(
            chat_id=admin_id,
            menu_button=MenuButtonWebApp(
                text="🔐 Ադմին-պանել",
                web_app=WebAppInfo(url=f"{base}/admin.html"),
            ),
        )
    except Exception:
        pass

def _init(self, *args, **kwargs):
    _original(self, *args, **kwargs)
    try:
        import __main__
        bot = getattr(__main__, "bot", None)
        if bot is not None:
            asyncio.get_running_loop().create_task(_set_admin_menu(bot))
    except Exception:
        pass

if not getattr(web.Application, "_armenia_admin_menu_wrapped", False):
    web.Application.__init__ = _init
    web.Application._armenia_admin_menu_wrapped = True
