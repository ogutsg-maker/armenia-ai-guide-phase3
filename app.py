from __future__ import annotations
import asyncio,logging,os
from pathlib import Path
from aiohttp import web
from aiogram import Bot,Dispatcher,types
import db
from routes import setup,expire_interests
from telegram import setup as telegram_setup
log=logging.getLogger("aig");logging.basicConfig(level=logging.INFO)
bot=Bot(os.environ["TELEGRAM_BOT_TOKEN"]);dp=Dispatcher();telegram_setup(dp)
BASE=Path(__file__).parent/"web_apps"
async def html(request):
    name=request.match_info["name"];p=BASE/name
    if not p.is_file():raise web.HTTPNotFound()
    return web.Response(text=p.read_text(encoding="utf-8"),content_type="text/html",headers={"Cache-Control":"no-store"})
async def root(request): request.match_info["name"]="welcome.html";return await html(request)
async def webhook(request):
    try:
        update=types.Update.model_validate(await request.json());await dp.feed_update(bot,update);return web.json_response({"ok":True})
    except Exception:log.exception("telegram update failed");return web.json_response({"ok":False},status=500)
async def health(request):return web.json_response({"ok":True})
async def lifecycle_worker():
    while True:
        try: await asyncio.to_thread(expire_interests)
        except Exception: log.exception("lifecycle worker")
        await asyncio.sleep(10)
async def start():
    app=web.Application()
    app.router.add_get("/health",health);app.router.add_post("/telegram/webhook",webhook);app.router.add_get("/",root)
    app.router.add_get("/{name:welcome.html|partner.html|partner_cabinet.html|client.html|admin.html}",html);setup(app)
    runner=web.AppRunner(app);await runner.setup();port=int(os.getenv("PORT","10000"));await web.TCPSite(runner,"0.0.0.0",port).start()
    try:
        await asyncio.to_thread(db.init)
    except Exception:
        log.exception("database initialization failed")
        raise
    log.info("Armenia AI Guide clean runtime on %s",port)
    worker=asyncio.create_task(lifecycle_worker())
    try: await asyncio.Event().wait()
    finally: worker.cancel()
if __name__=="__main__":asyncio.run(start())
