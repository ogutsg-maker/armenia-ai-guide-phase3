from __future__ import annotations
import hashlib,hmac,json,time,urllib.parse
from aiohttp import web
from config import BOT_TOKEN
def telegram_user(raw:str)->dict:
    if not raw: raise web.HTTPUnauthorized(text='telegram_init_data_required')
    data=dict(urllib.parse.parse_qsl(raw,keep_blank_values=True))
    received=data.pop("hash",None)
    if not received: raise web.HTTPUnauthorized(text='telegram_hash_missing')
    check="\\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,received): raise web.HTTPUnauthorized(text='invalid_telegram_init_data')
    auth_date=int(data.get("auth_date","0"))
    if time.time()-auth_date>86400: raise web.HTTPUnauthorized(text='telegram_init_data_expired')
    return json.loads(data["user"])
async def user(request):
    u=telegram_user(request.headers.get("X-Telegram-Init-Data",""))
    request["tg_user"]=u
    return u
