import asyncio,logging,os
from pathlib import Path
from aiohttp import web
from aiogram import Bot,Dispatcher,types
from config import BOT_TOKEN
from telegram import setup
from routes import setup_routes
logging.basicConfig(level=logging.INFO)
BASE=Path(__file__).parent/"web_apps"
bot=Bot(BOT_TOKEN); dp=Dispatcher(); setup(dp)
async def health(r): return web.json_response({"ok":True})
async def webhook(r):
    try: await dp.feed_update(bot,types.Update.model_validate(await r.json())); return web.json_response({"ok":True})
    except Exception: logging.exception("webhook"); return web.json_response({"ok":False},status=500)
async def page(r):
    p=BASE/r.match_info["name"]
    if not p.is_file(): raise web.HTTPNotFound()
    return web.Response(text=p.read_text("utf-8"),content_type="text/html")
async def main():
    app=web.Application(); app.router.add_get("/health",health); app.router.add_post("/telegram/webhook",webhook)
    for n in ("welcome.html","partner.html","partner_cabinet.html","client.html","admin.html"): app.router.add_get("/"+n,page)
    setup_routes(app); runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",int(os.getenv("PORT","10000"))).start(); logging.info("clean runtime listening")
    while True: await asyncio.sleep(3600)
if __name__=="__main__": asyncio.run(main())