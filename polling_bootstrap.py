"""Ensure Telegram webhook mode is cleared before aiogram long polling starts.

This is intentionally isolated from main.py so the polling startup path stays
unchanged. aiogram imports this module through config.py before Dispatcher is
created, allowing us to wrap Dispatcher.start_polling safely.
"""
from __future__ import annotations

import logging

from aiogram import Dispatcher

logger = logging.getLogger(__name__)


if not getattr(Dispatcher, "_armenia_webhook_bootstrap", False):
    _original_start_polling = Dispatcher.start_polling

    async def _start_polling_without_webhook(self, *bots, **kwargs):
        for bot in bots:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                logger.info("✅ Telegram webhook deleted before polling")
            except Exception:
                logger.exception("❌ Could not delete Telegram webhook before polling")
                raise

        return await _original_start_polling(self, *bots, **kwargs)

    Dispatcher.start_polling = _start_polling_without_webhook
    Dispatcher._armenia_webhook_bootstrap = True
