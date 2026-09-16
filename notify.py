"""Unified notification layer for the Armenia AI Guide platform.

Every user-facing event should go through :func:`notify`, which:
  1. persists a row in the ``notifications`` table (so the WebApp cabinets can
     render an inbox / unread badge), and
  2. best-effort pushes the same message to the recipient over Telegram.

The Telegram push is best-effort: if the bot is missing or the user has never
started the bot, the DB record is still created and the call never raises.
"""
from __future__ import annotations

import logging

import platform_db

logger = logging.getLogger(__name__)


def _resolve_bot(bot_or_app):
    """Accept either an aiohttp ``Application``, an aiogram ``Bot`` or ``None``."""
    if bot_or_app is None:
        return None
    # aiohttp Application stores the bot under app['bot']
    getter = getattr(bot_or_app, "get", None)
    if callable(getter):
        candidate = bot_or_app.get("bot")
        if candidate is not None:
            return candidate
    # already a bot instance
    if hasattr(bot_or_app, "send_message"):
        return bot_or_app
    return None


async def notify(bot_or_app, user_id, title="", body="", *, kind="info",
                 audience="user", data=None, telegram_text=None):
    """Persist a notification and best-effort push it over Telegram.

    Returns the created notification row (or ``None`` if persistence failed).
    """
    row = None
    try:
        row = platform_db.create_notification(
            user_id, title=title, body=body, kind=kind,
            audience=audience, data=data or {},
        )
    except Exception:
        logger.exception("Could not persist notification for %s", user_id)

    bot = _resolve_bot(bot_or_app)
    if bot is not None:
        text = telegram_text or (f"{title}\n\n{body}" if title and body else (title or body))
        if text:
            try:
                await bot.send_message(int(user_id), text)
                if row:
                    try:
                        platform_db.mark_notification_delivered(row["id"])
                    except Exception:
                        pass
            except Exception:
                # user never started the bot / blocked it / bot missing — fine
                logger.debug("Telegram push skipped for %s", user_id)
    return row
