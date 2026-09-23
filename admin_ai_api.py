from __future__ import annotations
import json
import os
import re
import time
import uuid
import platform_db
from aiohttp import web
from platform_db import proposals, review_proposal, edit_proposal, add_clarification, potential_partners, update_potential, create_potential
from potential_partner_ai import PotentialPartnerAI
from research_provider import search_web
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError


def _admin(request):
    raw=request.headers.get('X-Telegram-Init-Data','').strip()
    token=request.app.get('stage3_bot_token','')
    admin_id=int(request.app.get('stage3_admin_id') or 0)
    if not raw: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':'telegram_init_data_required'}),content_type='application/json')
    try: uid=int(validate_telegram_webapp_init_data(raw,token)['id'])
    except (TelegramWebAppAuthError,ValueError,TypeError,KeyError) as e: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':str(e)}),content_type='application/json')
    if uid!=admin_id: raise web.HTTPForbidden(text=json.dumps({'ok':False,'error':'admin_access_required'}),content_type='application/json')
    return uid

async def catalog_list(request):
    _admin(request); return web.json_response({'ok':True,'items':proposals(request.query.get('status'))})

async def catalog_action(request):
    admin_id=_admin(request); pid=int(request.match_info['id']); data=await request.json(); action=request.match_info['action']
    if action=='clarify':
        comment=str(data.get('comment') or '').strip()
        p=review_proposal(pid,'clarification',admin_id,comment)
        if p:
            add_clarification(pid,p.get('partner_id'),admin_id,comment)
            if p.get('partner_id') and request.app.get('bot'):
                partner_user=__import__('platform_db').one('SELECT user_id FROM partners WHERE id=%s',(p['partner_id'],))
                if partner_user:
                    try:
                        await request.app['bot'].send_message(int(partner_user['user_id']), '📝 Ադմինիստրատորը խնդրում է ճշտել տվյալները։\n\n'+comment)
                    except Exception: pass
        return web.json_response({'ok':True,'proposal':p})
    if action=='edit':
        fields={k:data[k] for k in ('proposed_master_category','proposed_category','proposed_subcategory','proposed_service','description','reason') if k in data}
        return web.json_response({'ok':True,'proposal':edit_proposal(pid,admin_id,fields)})
    if action=='reject':
        return web.json_response({'ok':True,'proposal':review_proposal(pid,'rejected',admin_id,str(data.get('comment') or '').strip())})
    if action=='activate':
        from catalog_manager import activate_proposal
        result=activate_proposal(pid,admin_id,data)
        if result.get('proposal_id') and request.app.get('bot'):
            from platform_db import proposal as get_proposal, one as db_one
            pp=get_proposal(pid)
            if pp and pp.get('partner_id'):
                u=db_one('SELECT user_id FROM partners WHERE id=%s',(pp['partner_id'],))
                if u:
                    try:
                        await request.app['bot'].send_message(int(u['user_id']), '✅ Ձեր առաջարկված ուղղության կառուցվածքը հաստատվել է։\n\nՀաջորդ քայլը՝ խնդրում եմ ուղարկել հաստատման փաստաթուղթը այստեղ։')
                    except Exception: pass
        return web.json_response({'ok':True,'result':result})
    raise web.HTTPBadRequest(text=json.dumps({'ok':False,'error':'unknown_action'}),content_type='application/json')

async def potential_list(request):
    _admin(request); return web.json_response({'ok':True,'items':potential_partners(request.query.get('status'),request.query.get('q'))})

async def potential_structure(request):
    _admin(request); data=await request.json(); raw=str(data.get('raw_text') or '').strip()
    if len(raw)<10: raise web.HTTPBadRequest(text=json.dumps({'ok':False,'error':'raw_text_required'}),content_type='application/json')
    item=await PotentialPartnerAI(request.app['ai']).structure_candidate(raw,data.get('source','manual_research'))
    return web.json_response({'ok':True,'item':item})

async def potential_status(request):
    _admin(request); pid=int(request.match_info['id']); data=await request.json()
    allowed={'new','researched','ready_for_review','contacted','interested','invited','registered','approved','active','rejected','archived'}
    status=str(data.get('status') or '')
    if status not in allowed: raise web.HTTPBadRequest(text=json.dumps({'ok':False,'error':'invalid_status'}),content_type='application/json')
    return web.json_response({'ok':True,'item':update_potential(pid,status=status,admin_comment=str(data.get('comment') or '')[:2000])})

async def potential_research(request):
    _admin(request); data=await request.json(); query=str(data.get('query') or '').strip()
    if len(query)<3: raise web.HTTPBadRequest(text=json.dumps({'ok':False,'error':'query_required'}),content_type='application/json')
    try: results=await search_web(query,str(data.get('location') or 'Armenia'),int(data.get('limit') or 10))
    except Exception as exc: return web.json_response({'ok':False,'error':str(exc),'message':'SEARCH_PROVIDER_NOT_CONFIGURED'},status=503)
    created=[]
    for r in results:
        raw=(r.get('title','')+'\n'+r.get('snippet','')+'\nSource: '+r.get('url','')).strip()
        try: item=await PotentialPartnerAI(request.app['ai']).structure_candidate(raw,'web_research')
        except Exception: continue
        if item:
            from platform_db import update_potential
            item=update_potential(item['id'],source_urls_json=[r.get('url')],status='researched')
            created.append(item)
    return web.json_response({'ok':True,'results':created,'source_count':len(results)})


# =====================================================================
# Admin AI/channel settings are served by /api/admin/settings
# (runtime_platform_bootstrap._admin_settings_get/_admin_settings_save,
# backed by the `features` module). The previous get_ai_settings/
# update_ai_settings handlers here referenced database.get_supabase_client
# and a `system_settings` table that do not exist in this psycopg-based
# project, so every call 500'd. They were removed to avoid a broken,
# duplicate settings surface.
# =====================================================================


_ADMIN_PENDING={}
_ADMIN_PENDING_TTL=15*60
_ADMIN_SESSIONS={}
_ADMIN_SESSION_TTL=30*60
_CONFIRM_YES={"да","да.","yes","yes.","ok","okay","подтверждаю","подтвердить","հա","այո","այո.","հաստատում եմ"}
_CONFIRM_NO={"нет","нет.","no","no.","cancel","отмена","отменить","ոչ","ոչ.","չեղարկել"}

def _admin_session(admin_id):
    sid=int(admin_id)
    now=time.time()
    state=_ADMIN_SESSIONS.get(sid)
    if not state or now-float(state.get("updated_at",0))>_ADMIN_SESSION_TTL:
        state={"last_focused_application_id":None,"pending_action":None,"history":[],"updated_at":now}
        _ADMIN_SESSIONS[sid]=state
    state["updated_at"]=now
    return state

def _admin_history(state,role,text):
    state.setdefault("history",[]).append({"role":role,"content":str(text)[:1000]})
    state["history"]=state["history"][-8:]
    state["updated_at"]=time.time()

def _admin_pending_add(command):
    token=uuid.uuid4().hex
    _ADMIN_PENDING[token]=(time.time(),command)
    return token

def _admin_catalog():
    return platform_db.rows("""SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,
        m.name_am AS master_am,m.name_ru AS master_ru,m.name_en AS master_en
        FROM categories c JOIN master_categories m ON m.id=c.master_category_id
        WHERE c.is_active=TRUE AND m.is_active=TRUE ORDER BY c.id""")

def _admin_category_by_text(value):
    target=str(value or "").strip().casefold()
    if not target: return None
    rows=_admin_catalog()
    exact=[x for x in rows if target in {str(x.get("name_am") or "").casefold(),
        str(x.get("name_ru") or "").casefold(),str(x.get("name_en") or "").casefold()}]
    if len(exact)==1: return exact[0]
    return None

def _admin_state_preview(action):
    aid=action.get("application_id")
    app=platform_db.one("SELECT * FROM partner_applications WHERE id=%s",(aid,))
    if not app: return "Заявка #"+str(aid)+" не найдена."
    if action.get("intent")=="edit_application":
        field=action.get("field")
        old=app.get({"subcategory":"subcategory_name","category":"subcategory_name",
                      "name":"service_name","service":"service_name","price":"price",
                      "description":"description","note":"admin_note"}.get(field))
        return "📨 Заявка #"+str(aid)+"\n🔧 "+str(field)+" : «"+str(old or "—")+"» → «"+str(action.get("new_value") or "—")+"»"
    if action.get("intent")=="approve_application":
        return "📨 Заявка #"+str(aid)+"\n✅ Перевести заявку на этап документа"
    if action.get("intent")=="reject_application":
        return "📨 Заявка #"+str(aid)+"\n❌ Отклонить\n📝 "+str(action.get("reason") or "Без причины")
    if action.get("intent")=="clarify_application":
        return "📨 Заявка #"+str(aid)+"\n📝 Отправить партнёру на уточнение\n"+str(action.get("admin_note") or "Требуется уточнение")
    return "Действие не определено."

async def _admin_execute_state_action(action):
    aid=int(action.get("application_id") or 0)
    if action.get("intent")=="edit_application":
        field=action.get("field")
        if field=="subcategory":
            cat=platform_db.one("""SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en
                FROM categories c WHERE c.id=%s AND c.is_active=TRUE""",(int(action["category_id"]),))
            app=platform_db.one("SELECT master_category_id FROM partner_applications WHERE id=%s",(aid,))
            if not cat: return "Подкатегория отсутствует в активном каталоге."
            if app and app.get("master_category_id") is not None and int(cat["master_category_id"])!=int(app["master_category_id"]):
                return "Подкатегория относится к другому направлению."
            label=str(cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"))
            platform_db.execute("UPDATE partner_applications SET category_id=%s,subcategory_name=%s,updated_at=NOW() WHERE id=%s",
                (int(cat["id"]),label,aid))
            return "✓ Заявка #"+str(aid)+" : подкатегория изменена на «"+label+"»."
        columns={"name":"service_name","service":"service_name","price":"price","description":"description","note":"admin_note"}
        column=columns.get(field)
        if not column: return "Уточните поле для изменения."
        value=action.get("new_value")
        if field=="price":
            try: value=float(str(value).replace(" ","").replace(",","."))
            except Exception: return "Цена должна быть числом."
        else:
            value=str(value or "").strip()
            if not value: return "Новое значение не указано."
        platform_db.execute("UPDATE partner_applications SET "+column+"=%s,updated_at=NOW() WHERE id=%s",(value,aid))
        return "✓ Заявка #"+str(aid)+" : "+str(field)+" изменено."
    if action.get("intent")=="approve_application":
        platform_db.execute("UPDATE partner_applications SET status='document_pending',reviewed_at=NOW(),updated_at=NOW() WHERE id=%s AND status NOT IN ('approved','pending_partner')",(aid,))
        return "✓ Заявка #"+str(aid)+" переведена на этап документа."
    if action.get("intent")=="reject_application":
        reason=str(action.get("reason") or "Отклонено администратором.")[:3000]
        platform_db.execute("UPDATE partner_applications SET status='rejected',admin_note=%s,reviewed_at=NOW(),updated_at=NOW() WHERE id=%s",(reason,aid))
        return "✓ Заявка #"+str(aid)+" отклонена."
    if action.get("intent")=="clarify_application":
        note=str(action.get("admin_note") or "Требуется уточнение данных.")[:3000]
        platform_db.execute("UPDATE partner_applications SET status='pending_partner',admin_note=%s,reviewed_at=NOW(),updated_at=NOW() WHERE id=%s",(note,aid))
        return "✓ Заявка #"+str(aid)+" отправлена партнёру на уточнение."
    return "Действие не определено."

async def _admin_ai_json(message,ctx):
    from groq import AsyncGroq
    key=os.getenv("GROQ_API_KEY","").strip()
    if not key: raise RuntimeError("GROQ_API_KEY is not configured")
    model=os.getenv("GROQ_MODEL","").strip() or "openai/gpt-oss-20b"
    client=AsyncGroq(api_key=key)
    system="""You are the intent extractor for the Armenia AI Guide admin secretary.
Understand Armenian, Russian and English natural language.
Return ONLY JSON and never invent IDs.
Do not execute SQL and do not write the final response.
Fields:
intent = inspect | edit | approve | reject | clarify | show
target = application | partner | business | service | category | document | order
application_id = integer or null
field = name | price | category | subcategory | direction | city | address | phone | description | status | document | note | null
value_text = requested value or null
reason = short reason
confidence = number from 0 to 1
Examples:
ենթակատեգորիան ճիշտ չէ -> edit/application/subcategory with null value
այստեղ պետք է Հոնքեր լինի -> edit/application/subcategory/value Հոնքեր
цена неправильная, поставь 2500 -> edit/application/price/value 2500
это вообще не та категория -> edit/application/category with null value
заявка заполнена неправильно -> clarify/application
одобри заявку 36 -> approve/application/36
открой заявку 36 -> inspect/application/36
Use the focused application for references such as this, here, it, the application."""
    payload=json.dumps({"message":message,"context":ctx},ensure_ascii=False,default=str)
    messages=[{"role":"system","content":system},{"role":"user","content":payload}]
    try:
        resp=await client.chat.completions.create(model=model,messages=messages,temperature=0,max_tokens=300)
    except Exception as first:
        if model!="openai/gpt-oss-20b" and ("404" in str(first) or "model" in str(first).lower()):
            resp=await client.chat.completions.create(model="openai/gpt-oss-20b",messages=messages,temperature=0,max_tokens=300)
        else:
            raise
    raw=(resp.choices[0].message.content or "").strip()
    data=json.loads(raw)
    return data if isinstance(data,dict) else {}

async def admin_ai_message(admin_id,message):
    message=str(message or "").strip()
    if not message: return "Գրեք, թե ինչ պետք է ստուգեմ կամ փոխեմ։"
    state=_admin_session(admin_id)
    normalized=message.lower().strip()

    pending=state.get("pending_action")
    if pending and normalized in _CONFIRM_YES:
        reply=await _admin_execute_state_action(pending)
        state["pending_action"]=None
        _admin_history(state,"assistant",reply)
        return reply
    if pending and normalized in _CONFIRM_NO:
        state["pending_action"]=None
        reply="Отменено. Никаких изменений не внесено."
        _admin_history(state,"assistant",reply)
        return reply

    focused_id=state.get("last_focused_application_id")
    focused=None
    if focused_id:
        focused=platform_db.one("""SELECT id,business_name,status,service_name,price,direction_name,
            master_category_id,subcategory_name,category_id,location_marz,location_city,address
            FROM partner_applications WHERE id=%s""",(focused_id,))

    ctx={"focused_application":focused,
         "history":state.get("history",[])[-6:],
         "applications":_admin_context(limit=12,include_catalog=False).get("applications",[])}
    c=await _admin_ai_json(message,ctx)
    intent=str(c.get("intent") or "").lower()
    target=str(c.get("target") or "").lower()
    aid=c.get("application_id") or focused_id

    if intent in {"inspect","show"}:
        if target=="partner": return await _admin_execute({"intent":"show_partners"})
        if target=="business": return await _admin_execute({"intent":"show_businesses"})
        if not aid: return await _admin_execute({"intent":"show_applications"})
        state["last_focused_application_id"]=int(aid)
        reply=await _admin_execute({"intent":"open_application","application_id":int(aid)})
        _admin_history(state,"admin",message)
        _admin_history(state,"assistant",reply)
        return reply

    if intent not in {"edit","approve","reject","clarify"}:
        reply=str(c.get("reply") or "Уточните, что именно нужно сделать.")
        _admin_history(state,"admin",message)
        _admin_history(state,"assistant",reply)
        return reply

    if not aid: return "Укажите номер заявки или сначала откройте заявку."
    app=platform_db.one("SELECT * FROM partner_applications WHERE id=%s",(int(aid),))
    if not app: return "Заявка #"+str(aid)+" не найдена."

    action={"intent":intent,"application_id":int(aid)}
    if intent=="edit":
        field=str(c.get("field") or "").lower()
        value=str(c.get("value_text") or "").strip()
        if field in {"category","subcategory"}:
            if not value:
                return "Текущая подкатегория «"+str(app.get("subcategory_name") or "—")+"». Укажите новую, например: «այստեղ պետք է Հոնքեր լինի»."
            cat=_admin_category_by_text(value)
            if not cat:
                return "Не нашёл однозначную подкатегорию «"+value+"» в активном каталоге. Уточните точное название."
            if app.get("master_category_id") is not None and int(cat["master_category_id"])!=int(app["master_category_id"]):
                return "Подкатегория относится к другому направлению. Укажите подкатегорию из текущего направления."
            action["field"]="subcategory"
            action["category_id"]=int(cat["id"])
            action["new_value"]=str(cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"))
        else:
            if field not in {"name","service","price","description","note"} or not value:
                return "Уточните поле и новое значение: цена, название услуги, описание или подкатегория."
            action["field"]=field
            action["new_value"]=value
    else:
        action["new_value"]=c.get("value_text")
        if intent=="reject": action["reason"]=c.get("reason") or c.get("value_text")
        if intent=="clarify": action["admin_note"]=c.get("reason") or c.get("value_text")

    state["pending_action"]=action
    token=_admin_pending_add(action)
    preview=_admin_state_preview(action)
    reply="🤖 Подготовил действие:\n\n"+preview+"\n\nПодтвердить? Напишите «да» или «нет».\n\nPENDING:"+token
    _admin_history(state,"admin",message)
    _admin_history(state,"assistant",reply)
    return reply

async def api_admin_assistant(request):
    _admin(request)
    data=await request.json()
    message=str(data.get("message") or "").strip()
    if not message: return web.json_response({"ok":False,"error":"message_required"},status=400)
    try: return web.json_response({"ok":True,"reply":await admin_ai_message(int(request.app.get("stage3_admin_id") or 0),message)})
    except Exception as exc: return web.json_response({"ok":False,"error":"admin_ai_failed","message":str(exc)[:500]},status=503)


def register_admin_ai_routes(app, ai, bot=None):
    app['ai']=ai
    app['bot']=bot
    app.router.add_post('/api/admin/assistant',api_admin_assistant)
    app.router.add_get('/api/admin/ai/catalog-proposals',catalog_list)
    app.router.add_post('/api/admin/ai/catalog-proposals/{id}/{action}',catalog_action)
    app.router.add_get('/api/admin/potential-partners',potential_list)
    app.router.add_post('/api/admin/potential-partners/structure',potential_structure)
    app.router.add_post('/api/admin/potential-partners/research',potential_research)
    app.router.add_post('/api/admin/potential-partners/{id}/status',potential_status)

