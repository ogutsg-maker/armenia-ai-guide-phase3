from aiohttp import web
import json, os
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError
from client_ai import ClientAI
from ai_router import AIRouter

def uid(request):
    raw = request.headers.get('X-Telegram-Init-Data','').strip()
    if not raw: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':'telegram_init_data_required'}), content_type='application/json')
    try: return int(validate_telegram_webapp_init_data(raw, request.app.get('stage3_bot_token') or os.getenv('BOT_TOKEN',''))['id'])
    except (TelegramWebAppAuthError,ValueError,TypeError,KeyError) as e: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':str(e)}), content_type='application/json')

async def chat(request):
    user_id = uid(request)
    data = await request.json()
    text = str(data.get('text') or '').strip()
    if not text: return web.json_response({'ok':False,'error':'text_required'}, status=400)
    from database import DatabaseManager
    lang = (DatabaseManager().get_user(user_id) or {}).get('lang','hy')
    reply = await request.app['client_ai'].process(user_id, text, lang)
    return web.json_response({'ok':True,'reply':reply})

async def route(request):
    """Phase 2 orchestrator entry: let the AI core decide which module handles
    the message (client search / partner onboarding / negotiation / smalltalk)."""
    user_id = uid(request)
    data = await request.json()
    text = str(data.get('text') or '').strip()
    if not text: return web.json_response({'ok':False,'error':'text_required'}, status=400)
    role = str(data.get('role') or 'client').strip().lower()
    if role not in ('client','partner'): role='client'
    from database import DatabaseManager
    lang = str(data.get('language') or '').strip() or (DatabaseManager().get_user(user_id) or {}).get('lang','hy')
    result = await request.app['ai_router'].dispatch(user_id, role, text, lang)
    return web.json_response({'ok':True, **result})

def register_client_routes(app, ai):
    app['client_ai'] = ClientAI(ai)
    app['ai_router'] = AIRouter(ai)
    app.router.add_post('/api/client/ai/chat', chat)
    app.router.add_post('/api/client/ai/route', route)
