from __future__ import annotations
import asyncio,json,logging,os
from pathlib import Path
from aiohttp import web
from aiogram import Bot,Dispatcher,types
from config import BOT_TOKEN,WEBAPP_BASE_URL
import db
from routes import setup
from telegram import setup as telegram_setup
log=logging.getLogger("aig");logging.basicConfig(level=logging.INFO)
bot=Bot(BOT_TOKEN);dp=Dispatcher();telegram_setup(dp)
BASE=Path(__file__).parent/"web_apps"
async def html(request):
    name=request.match_info["name"]; p=BASE/name
    if not p.exists():raise web.HTTPNotFound()
    return web.Response(text=p.read_text(encoding="utf-8"),content_type="text/html",headers={"Cache-Control":"no-store"})
async def webhook(request):
    try:
        u=types.Update.model_validate(await request.json()); await dp.feed_update(bot,u); return web.json_response({"ok":True})
    except Exception: log.exception("telegram update"); return web.json_response({"ok":False},status=500)
async def health(request):return web.json_response({"ok":True})
async def start():
    db.init()
    app=web.Application()
    app.router.add_get("/health",health);app.router.add_post("/telegram/webhook",webhook)
    app.router.add_get("/",lambda r:html(type("R",(),{"match_info":{"name":"welcome.html"}})()))
    app.router.add_get("/{name:welcome.html|partner.html|master_cabinet.html|client.html|admin.html}",html)
    setup(app)
    app.router.add_static("/assets/",path=str(BASE))
    runner=web.AppRunner(app);await runner.setup()
    port=int(os.getenv("PORT","10000"));await web.TCPSite(runner,"0.0.0.0",port).start()
    log.info("Armenia AI Guide clean runtime on %s",port)
    await asyncio.Event().wait()
if __name__=="__main__":asyncio.run(start())
