import hashlib,hmac,json,urllib.parse
from aiohttp import web
from config import BOT_TOKEN,ADMIN_ID
from data.users import ensure
def verify(raw):
    if not raw: raise web.HTTPUnauthorized(text="telegram_init_data_required")
    d=dict(urllib.parse.parse_qsl(raw,keep_blank_values=True)); h=d.pop("hash",None)
    if not h or "user" not in d: raise web.HTTPUnauthorized(text="invalid_init_data")
    check="\n".join(f"{k}={d[k]}" for k in sorted(d))
    secret=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret,check.encode(),hashlib.sha256).hexdigest(),h): raise web.HTTPUnauthorized(text="invalid_init_data")
    return json.loads(d["user"])
async def user(r): return ensure(verify(r.headers.get("X-Telegram-Init-Data","")))
def require_admin(uid):
    if uid!=ADMIN_ID: raise web.HTTPForbidden(text="admin_required")