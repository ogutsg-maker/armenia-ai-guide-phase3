import re
from aiohttp import web
from auth import user,require_admin
from data import partners
from data.core import active_services
from ai.manager import turn
from db import run
def j(x): return web.json_response(x)
async def session(r):
 u=await user(r); return j({"ok":True,"user":u,"partner":partners.get(u["telegram_id"]),"admin":u["telegram_id"]==__import__("config").ADMIN_ID})
async def register(r):
 u=await user(r); b=await r.json(); name=str(b.get("name","")).strip(); phone=str(b.get("phone","")).strip()
 if not name or not re.fullmatch(r"[+0-9() .-]{7,30}",phone): return j({"ok":False,"error":"name_and_phone_required"},status=400)
 p,c=partners.register(u["telegram_id"],name,phone); return j({"ok":True,"partner_id":p["id"],"company":c,"destination":"/partner_cabinet.html"})
async def partner_ai(r):
 u=await user(r); b=await r.json(); return j({"ok":True,"result":await turn(u["telegram_id"],"PARTNER",str(b.get("text","")))})
async def client_search(r):
 u=await user(r); b=await r.json(); return j({"ok":True,"items":active_services(b.get("text"),b.get("city"))})
async def admin_ai(r):
 u=await user(r); require_admin(u["telegram_id"]); b=await r.json(); return j({"ok":True,"result":await turn(u["telegram_id"],"ADMIN",str(b.get("text","")))})
async def applications(r):
 u=await user(r); require_admin(u["telegram_id"])
 return j({"ok":True,"items":run("SELECT a.*,s.name service_name,c.name company_name FROM aig_service_applications a JOIN aig_services s ON s.id=a.service_id JOIN aig_companies c ON c.id=s.company_id ORDER BY a.id DESC",many=True)})
def setup_routes(app):
 app.router.add_get("/api/session",session); app.router.add_post("/api/partner/register",register); app.router.add_post("/api/partner/ai",partner_ai); app.router.add_post("/api/client/search",client_search); app.router.add_post("/api/admin/ai",admin_ai); app.router.add_get("/api/admin/applications",applications)