import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types

from config import BOT_TOKEN
from telegram import setup
from routes import setup_routes

logging.basicConfig(level=logging.INFO)

BASE = Path(__file__).parent / "web_apps"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
setup(dp)


async def health(request):
    return web.json_response({"ok": True})


async def root(request):
    raise web.HTTPFound("/welcome.html")


async def webhook(request):
    try:
        update = types.Update.model_validate(await request.json())
        await dp.feed_update(bot, update)
        return web.json_response({"ok": True})
    except Exception:
        logging.exception("telegram webhook")
        return web.json_response({"ok": False}, status=500)


async def page(request):
    name = request.path.lstrip("/")
    allowed = {
        "welcome.html",
        "partner.html",
        "partner_cabinet.html",
        "client.html",
        "admin.html",
    }
    if name not in allowed:
        raise web.HTTPNotFound()

    path = BASE / name
    if not path.is_file():
        raise web.HTTPNotFound()

    return web.Response(
        text=path.read_text("utf-8"),
        content_type="text/html",
        charset="utf-8",
    )

async def main():
    app = web.Application()

    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_post("/telegram/webhook", webhook)

    for name in (
        "welcome.html",
        "partner.html",
        "partner_cabinet.html",
        "client.html",
        "admin.html",
    ):
        app.router.add_get("/" + name, page)

    setup_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()

    logging.info("Armenia AI Guide clean runtime listening on %s", port)

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
