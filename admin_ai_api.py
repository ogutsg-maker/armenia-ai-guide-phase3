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

def _admin_context(limit=30):
    applications=platform_db.rows("""SELECT a.*,p.user_id FROM partner_applications a
        JOIN partners p ON p.id=a.partner_id
        WHERE a.status NOT IN ('approved','pending_partner')
        ORDER BY a.created_at DESC LIMIT %s""",(limit,))
    for a in applications:
        payload=a.get("payload_json") or {}
        if isinstance(payload,str):
            try: payload=json.loads(payload)
            except Exception: payload={}
        a["payload_json"]=payload
        a["services"]=payload.get("services") if isinstance(payload,dict) and isinstance(payload.get("services"),list) else []
    catalog=platform_db.rows("""SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,m.name_am AS master_am,m.name_ru AS master_ru,m.name_en AS master_en
        FROM categories c JOIN master_categories m ON m.id=c.master_category_id
        WHERE c.is_active=TRUE AND m.is_active=TRUE ORDER BY c.master_category_id,c.id""")
    return {"applications":applications,"catalog":catalog,
            "partners":platform_db.rows("SELECT id,user_id,status,verification_status,business_name,business_description FROM partners ORDER BY id DESC LIMIT 50"),
            "businesses":platform_db.rows("SELECT id,partner_id,name,description,phone,status FROM partner_businesses WHERE status<>'archived' ORDER BY id DESC LIMIT 100")}

def _admin_pending_add(command):
    token=uuid.uuid4().hex
    _ADMIN_PENDING[token]=(time.time(),command)
    return token

async def _admin_ai_json(message,ctx):
    from groq import AsyncGroq
    key=os.getenv("GROQ_API_KEY","").strip()
    if not key: raise RuntimeError("GROQ_API_KEY is not configured")
    model=os.getenv("GROQ_MODEL","").strip() or "openai/gpt-oss-20b"
    client=AsyncGroq(api_key=key)
    system="""You are the AI secretary of Armenia AI Guide.
Understand Armenian, Russian and English. Return ONLY JSON.
The administrator speaks naturally and expects real admin actions.
Allowed intents: show_applications, open_application, edit_application,
approve_application, reject_application, clarify_application, show_partners,
show_businesses, clarify.
Never invent IDs. Use only IDs from context.
For mutations extract application_id and exact requested fields.
When the administrator names a catalogue category/subcategory, use the matching IDs from context.catalog.
JSON fields: intent, application_id, partner_id, master_category_id,
category_id, service_name, price, direction_name, subcategory_name,
admin_note, reason, reply."""
    payload=json.dumps({"message":message,"context":ctx},ensure_ascii=False,default=str)
    messages=[{"role":"system","content":system},{"role":"user","content":payload}]
    try:
        resp=await client.chat.completions.create(model=model,messages=messages,temperature=0.1,max_tokens=900)
    except Exception as first:
        if model!="openai/gpt-oss-20b" and ("404" in str(first) or "model" in str(first).lower()):
            resp=await client.chat.completions.create(model="openai/gpt-oss-20b",messages=messages,temperature=0.1,max_tokens=900)
        else: raise
    raw=(resp.choices[0].message.content or "").strip().replace("```json","").replace("```","").strip()
    data=json.loads(raw)
    return data if isinstance(data,dict) else {}

def _admin_ai_preview(c,ctx):
    aid=c.get("application_id")
    a=next((x for x in ctx["applications"] if aid and int(x["id"])==int(aid)),None)
    if not a: return str(c.get("reply") or "Укажите номер заявки.")
    price=c.get("price") if c.get("price") is not None else a.get("price")
    lines=["📨 Заявка #"+str(a["id"])+" · "+str(a.get("business_name") or "—"),
           "🛠 "+str(c.get("service_name") or a.get("service_name") or "—")+" · "+str(price if price is not None else "—")+" ֏"]
    loc=", ".join(x for x in [a.get("location_marz"),a.get("location_city"),a.get("address")] if x)
    if loc: lines.append("📍 "+loc)
    if c.get("direction_name") or c.get("master_category_id"): lines.append("💇 "+str(c.get("direction_name") or a.get("direction_name") or "Новое направление"))
    if c.get("subcategory_name") or c.get("category_id"): lines.append("🏷 "+str(c.get("subcategory_name") or a.get("subcategory_name") or "Новая подкатегория"))
    if c.get("admin_note") or c.get("reason"): lines.append("📝 "+str(c.get("admin_note") or c.get("reason")))
    return "\n".join(lines)

async def _admin_execute(c):
    intent=c.get("intent")
    aid=int(c["application_id"]) if c.get("application_id") else None
    if intent=="show_applications":
        rows=_admin_context()["applications"]
        if not rows: return "📨 Новых заявок нет."
        return "📨 Заявки:\n"+"\n".join("#"+str(x["id"])+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("service_name") or "—")+" · "+str(x.get("price") if x.get("price") is not None else "—")+" ֏" for x in rows[:15])
    if intent=="open_application":
        if not aid: return "Укажите номер заявки."
        a=platform_db.one("SELECT a.*,p.user_id FROM partner_applications a JOIN partners p ON p.id=a.partner_id WHERE a.id=%s",(aid,))
        if not a: return "Заявка #"+str(aid)+" не найдена."
        loc=", ".join(x for x in [a.get("location_marz"),a.get("location_city"),a.get("address")] if x)
        return ("📨 Заявка #"+str(aid)+" · "+str(a.get("business_name") or "—")+"\nСтатус: "+str(a.get("status") or "—")+
                "\nTelegram: "+str(a.get("user_id") or "—")+"\n📍 "+(loc or "—")+"\n☎ "+str(a.get("phone") or "—")+
                "\n🛠 "+str(a.get("service_name") or "—")+" · "+str(a.get("price") if a.get("price") is not None else "—")+" ֏"+
                "\n🏷 "+str(a.get("direction_name") or "—")+" → "+str(a.get("subcategory_name") or "—"))
    if intent=="show_partners":
        rows=_admin_context()["partners"]
        return "🤝 Партнёров нет." if not rows else "🤝 Партнёры:\n"+"\n".join("#"+str(x["id"])+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("status") or "—") for x in rows[:20])
    if intent=="show_businesses":
        rows=_admin_context()["businesses"]
        return "🏢 Компаний нет." if not rows else "🏢 Компании:\n"+"\n".join("#"+str(x["id"])+" · "+str(x.get("name") or "—")+" · "+str(x.get("status") or "—") for x in rows[:30])
    if not aid: return "Укажите номер заявки."
    if not platform_db.one("SELECT id FROM partner_applications WHERE id=%s",(aid,)): return "Заявка #"+str(aid)+" не найдена."
    if intent=="edit_application":
        fields={}
        sub=str(c.get("subcategory_name") or "").strip().casefold()
        if sub and c.get("category_id") is None:
            matches=[]
            for cat in _admin_context().get("catalog",[]):
                names=[cat.get("name_am"),cat.get("name_ru"),cat.get("name_en")]
                if any(str(n or "").strip().casefold()==sub for n in names): matches.append(cat)
            if len(matches)==1:
                c["category_id"]=int(matches[0]["id"])
                c["master_category_id"]=int(matches[0]["master_category_id"])
                c["subcategory_name"]=matches[0].get("name_am") or matches[0].get("name_ru") or matches[0].get("name_en")
                c["direction_name"]=matches[0].get("master_am") or matches[0].get("master_ru") or matches[0].get("master_en")
        for k in ("direction_name","subcategory_name","service_name","admin_note"):
            if c.get(k) not in (None,""): fields[k]=str(c[k]).strip()
        for k in ("master_category_id","category_id"):
            if c.get(k) is not None:
                try: fields[k]=int(c[k])
                except: pass
        if c.get("price") is not None:
            try: fields["price"]=float(c["price"])
            except: pass
        if not fields: return "Уточните, что именно изменить."
        sets=", ".join(k+"=%s" for k in fields)
        platform_db.execute("UPDATE partner_applications SET "+sets+",updated_at=NOW() WHERE id=%s",(*fields.values(),aid))
        return "✓ Заявка #"+str(aid)+" обновлена."
    if intent=="approve_application":
        platform_db.execute("UPDATE partner_applications SET status='document_pending',reviewed_at=NOW(),updated_at=NOW() WHERE id=%s AND status NOT IN ('approved','pending_partner')",(aid,))
        return "✓ Заявка #"+str(aid)+" переведена на этап документа."
    if intent=="reject_application":
        reason=str(c.get("reason") or c.get("admin_note") or "Отклонено администратором.")[:3000]
        platform_db.execute("UPDATE partner_applications SET status='rejected',admin_note=%s,reviewed_at=NOW(),updated_at=NOW()",(reason,aid))
        return "✓ Заявка #"+str(aid)+" отклонена. Причина: "+reason
    if intent=="clarify_application":
        note=str(c.get("admin_note") or c.get("reason") or "Требуется уточнение данных.")[:3000]
        platform_db.execute("UPDATE partner_applications SET status='pending_partner',admin_note=%s,reviewed_at=NOW(),updated_at=NOW()",(note,aid))
        return "✓ Заявка #"+str(aid)+" отправлена партнёру на уточнение."
    return str(c.get("reply") or "Уточните команду.")

async def admin_ai_message(admin_id,message):
    normalized=re.sub(r"[\s.!?,;:]+"," ",message.lower()).strip()
    if normalized in {"да","da","yes","y","ok","okay","подтверждаю","подтвердить","այո","հա","հաստատել","հաստատում եմ"}:
        pending=[(ts,tok,c) for tok,(ts,c) in _ADMIN_PENDING.items() if time.time()-ts<=_ADMIN_PENDING_TTL]
        if pending:
            _,tok,c=max(pending,key=lambda x:x[0]);_ADMIN_PENDING.pop(tok,None)
            return await _admin_execute(c)
    if normalized in {"нет","no","n","cancel","отмена","отменить","ոչ","չեղարկել"}:
        pending=[(ts,tok) for tok,(ts,c) in _ADMIN_PENDING.items() if time.time()-ts<=_ADMIN_PENDING_TTL]
        if pending:
            _,tok=max(pending,key=lambda x:x[0]);_ADMIN_PENDING.pop(tok,None)
            return "Операция отменена."
    ctx=_admin_context()
    c=await _admin_ai_json(message,ctx)
    if c.get("intent") in {"show_applications","open_application","show_partners","show_businesses","clarify"}:
        return await _admin_execute(c)
    _admin_pending_add(c)
    return "🤖 Подготовил действие:\n\n"+_admin_ai_preview(c,ctx)+"\n\nПодтвердить? Напишите «да» или «нет»."

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

