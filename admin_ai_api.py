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


def _norm(text):
    if text is None:
        return ""
    return " ".join(str(text).casefold().strip().split())


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

def _admin_safe(value):
    from decimal import Decimal
    from datetime import date, datetime
    if value is None or isinstance(value,(str,int,float,bool)): return value
    if isinstance(value,Decimal): return float(value)
    if isinstance(value,(datetime,date)): return value.isoformat()
    if isinstance(value,dict): return {str(k):_admin_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple,set)): return [_admin_safe(v) for v in value]
    return str(value)


def _admin_session(admin_id):
    sid=int(admin_id); now=time.time(); state=_ADMIN_SESSIONS.get(sid)
    if not state or now-float(state.get("updated_at",0))>_ADMIN_SESSION_TTL:
        state={"last_focused_application_id":None,"last_focused_field":None,"last_shown_applications":[],
               "pending_action":None,"waiting_for_input":None,"history":[],"updated_at":now}
        _ADMIN_SESSIONS[sid]=state
    state["updated_at"]=now
    return state


def _admin_history(state,role,text):
    state.setdefault("history",[]).append({"role":str(role),"content":str(text)[:1200]})
    state["history"]=_admin_safe(state["history"][-10:]); state["updated_at"]=time.time()


def _admin_hydrate_application(aid):
    if not aid: return None
    try:
        return _admin_safe(platform_db.one("""SELECT id,business_name,status,service_name,price,direction_name,
            master_category_id,subcategory_name,category_id,location_marz,location_city,address,phone,description,created_at,updated_at
            FROM partner_applications WHERE id=%s""",(int(aid),)))
    except Exception: return None


def _admin_hydrate_context(state,limit=12):
    try:
        applications=platform_db.rows("""SELECT id,business_name,status,service_name,price,direction_name,
            master_category_id,subcategory_name,category_id,location_city,address,created_at
            FROM partner_applications WHERE status NOT IN ('approved','pending_partner')
            ORDER BY created_at DESC LIMIT %s""",(int(limit),))
    except Exception: applications=[]
    return _admin_safe({"focused_application":_admin_hydrate_application(state.get("last_focused_application_id")),
        "applications":applications,"waiting_for_input":state.get("waiting_for_input"),
        "last_focused_field":state.get("last_focused_field"),"history":state.get("history",[])[-6:]})


def _admin_context(limit=30,include_catalog=False):
    applications=platform_db.rows("""SELECT a.*,p.user_id FROM partner_applications a JOIN partners p ON p.id=a.partner_id
        WHERE a.status NOT IN ('approved','pending_partner') ORDER BY a.created_at DESC LIMIT %s""",(int(limit),))
    for a in applications:
        payload=a.get("payload_json") or {}
        if isinstance(payload,str):
            try: payload=json.loads(payload)
            except Exception: payload={}
        a["payload_json"]=payload
    result={"applications":applications,
            "partners":platform_db.rows("SELECT id,user_id,status,verification_status,business_name,business_description FROM partners ORDER BY id DESC LIMIT 50"),
            "businesses":platform_db.rows("SELECT id,partner_id,name,description,phone,status FROM partner_businesses WHERE status<>'archived' ORDER BY id DESC LIMIT 100")}
    if include_catalog: result["catalog"]=_admin_catalog()
    return _admin_safe(result)


async def _admin_execute(command):
    intent=str(command.get("intent") or ""); aid=command.get("application_id")
    try: aid=int(aid) if aid is not None else None
    except (TypeError,ValueError): aid=None
    if intent=="show_applications":
        rows=_admin_context(limit=30).get("applications",[])
        if not rows: return "📨 Заявок нет."
        return "📨 Заявки ("+str(len(rows))+"):\n"+"\n".join("#"+str(x["id"])+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("service_name") or "—")+" · "+str(x.get("price") if x.get("price") is not None else "—")+" ֏" for x in rows[:20])
    if intent=="show_full_application":
        if not aid: return "Сначала откройте заявку или укажите её номер."
        if not _admin_hydrate_application(aid): return "Заявка #"+str(aid)+" не найдена."
        state["last_focused_application_id"]=int(aid)
        state["last_focused_field"]=None
        reply=await _admin_execute({"intent":"show_full_application","application_id":int(aid)})
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    if intent=="show_application_count":
        row=platform_db.one("SELECT COUNT(*) AS count FROM partner_applications WHERE status NOT IN ('approved','pending_partner')")
        return "📨 Сейчас в работе: "+str(int(row.get("count") or 0))+" заявок."
    if intent=="open_application":
        if not aid: return "Укажите номер заявки."
        a=platform_db.one("SELECT a.*,p.user_id FROM partner_applications a JOIN partners p ON p.id=a.partner_id WHERE a.id=%s",(aid,))
        if not a: return "Заявка #"+str(aid)+" не найдена."
        loc=", ".join(str(x) for x in (a.get("location_marz"),a.get("location_city"),a.get("address")) if x)
        return ("📨 Заявка #"+str(aid)+" · "+str(a.get("business_name") or "—")+"\nСтатус: "+str(a.get("status") or "—")+"\nTelegram: "+str(a.get("user_id") or "—")+"\n📍 "+(loc or "—")+"\n☎ "+str(a.get("phone") or "—")+"\n🛠 "+str(a.get("service_name") or "—")+" · "+str(a.get("price") if a.get("price") is not None else "—")+" ֏\n🧭 "+str(a.get("direction_name") or "—")+" → "+str(a.get("subcategory_name") or "—"))
    if intent=="show_full_application":
        return _admin_full_application_text(aid)
    if intent=="show_partners":
        rows=_admin_context(limit=1).get("partners",[])
        return "🤝 Партнёров нет." if not rows else "🤝 Партнёры:\n"+"\n".join("#"+str(x["id"])+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("status") or "—") for x in rows[:30])
    if intent=="show_businesses":
        rows=_admin_context(limit=1).get("businesses",[])
        return "🏢 Компаний нет." if not rows else "🏢 Компании:\n"+"\n".join("#"+str(x["id"])+" · "+str(x.get("name") or "—")+" · "+str(x.get("status") or "—") for x in rows[:50])
    return "Неизвестный запрос."


def _admin_full_application_text(aid):
    """Return the complete current application state for read-only admin inspection."""
    if not aid:
        return "Укажите номер заявки."
    a=platform_db.one("""SELECT a.*,p.user_id
        FROM partner_applications a
        LEFT JOIN partners p ON p.id=a.partner_id
        WHERE a.id=%s""",(int(aid),))
    if not a:
        return "Заявка #"+str(aid)+" не найдена."

    def val(v):
        if v is None or str(v).strip()=="":
            return "—"
        return str(v)

    loc=", ".join(str(x) for x in (
        a.get("location_marz"),a.get("location_city"),
        a.get("location_village"),a.get("address")
    ) if x)

    lines=[
        "📨 Заявка #"+str(aid)+" · "+val(a.get("business_name")),
        "Статус: "+val(a.get("status")),
        "Telegram: "+val(a.get("user_id")),
        "📍 Место: "+(loc or "—"),
        "☎ Телефон: "+val(a.get("phone")),
        "",
        "🛠 Услуга: "+val(a.get("service_name")),
        "💰 Цена: "+(val(a.get("price"))+" ֏" if a.get("price") is not None else "—"),
        "🧭 Направление: "+val(a.get("direction_name")),
        "🏷 Подкатегория: "+val(a.get("subcategory_name")),
        "🆔 ID категории: "+val(a.get("category_id")),
        "",
        "📝 Описание: "+val(a.get("description")),
        "🏢 Объект: "+val(a.get("object_name")),
        "📄 Документ ID: "+val(a.get("document_id")),
        "Создана: "+val(a.get("created_at")),
        "Обновлена: "+val(a.get("updated_at")),
    ]

    payload=a.get("payload_json") or {}
    if isinstance(payload,str):
        try: payload=json.loads(payload)
        except Exception: payload={}
    if isinstance(payload,dict):
        services=payload.get("services")
        if isinstance(services,list) and services:
            lines += ["","🛠 Все услуги из заявки:"]
            for i,svc in enumerate(services,1):
                if not isinstance(svc,dict): continue
                name=svc.get("name") or svc.get("service_name") or "—"
                price=svc.get("price")
                direction=svc.get("direction_name") or "—"
                sub=svc.get("subcategory_name") or "—"
                lines.append(
                    str(i)+". "+str(name)+" · "+
                    ((str(price)+" ֏") if price is not None else "—")+" · "+
                    str(direction)+" → "+str(sub)
                )
    return "\n".join(lines)


def _admin_catalog():
    return platform_db.rows("""SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,
        m.name_am AS master_am,m.name_ru AS master_ru,m.name_en AS master_en
        FROM categories c JOIN master_categories m ON m.id=c.master_category_id
        WHERE c.is_active=TRUE AND m.is_active=TRUE ORDER BY c.id""")

# Small deterministic concept dictionary used only by the Python resolver.
_ADMIN_CONCEPT_MAP = {
    "брови": {"брови", "бровей", "бровями", "бровью", "бровь", "հոնքեր", "հոնք", "հոնքերի", "eyebrows", "eyebrow"},
    "маникюр": {"маникюр", "маникюра", "маникюрный", "մատնահարդարում", "manicure", "եղունգ"},
    "педикюр": {"педикюр", "պեդիկյուր", "pedicure"},
    "макияж": {"макияж", "դիմահարդարում", "makeup", "визаж"},
    "стрижка": {"стрижка", "стрижки", "стрижку", "վարսավիր", "սանրվածք", "haircut"},
    "волосы": {"волосы", "մազ", "մազեր", "hair"},
    "окрашивание": {"окрашивание", "ներկում", "ներկել", "coloring", "colouring"},
}

def _tokens(text):
    return [t for t in re.findall(r"[a-zа-яёևա-ֆ0-9-]+", _norm(text)) if len(t) > 1]

def _concept_tokens(text):
    raw=set(_tokens(text))
    expanded=set(raw)
    for key, aliases in _ADMIN_CONCEPT_MAP.items():
        if raw.intersection(aliases) or key in raw:
            expanded.update(aliases)
            expanded.add(key)
    return expanded

def _filter_catalog_master(rows, master_category_id):
    if master_category_id is None:
        return rows
    try:
        mid=int(master_category_id)
    except (ValueError,TypeError):
        return rows
    return [x for x in rows if x.get("master_category_id") is not None and int(x.get("master_category_id"))==mid]

def _admin_category_candidates(value, master_category_id=None, limit=8):
    """Deterministic live-catalog resolver. IDs always come from DB."""
    target=_norm(value)
    if not target:
        return []
    try:
        rows=_filter_catalog_master(_admin_catalog(), master_category_id)
    except Exception:
        return []
    if not rows:
        return []

    target_tokens=set(_tokens(target))
    target_concepts=_concept_tokens(target)
    scored=[]

    for row in rows:
        names={k:_norm(row.get(k) or "") for k in ("name_am","name_ru","name_en")}
        names={k:v for k,v in names.items() if v}
        if not names:
            continue
        best=0
        reasons=[]

        if target in names.values():
            best=max(best,1000)
            reasons.append("exact")

        if any(target in name or name in target for name in names.values()):
            best=max(best,700)
            reasons.append("phrase")

        catalog_text=" ".join(names.values())
        catalog_tokens=set(_tokens(catalog_text))
        catalog_concepts=_concept_tokens(catalog_text)

        concept_overlap=target_concepts.intersection(catalog_concepts)
        if concept_overlap:
            specific={"брови","հոնքեր","հոնք","eyebrows","eyebrow",
                      "маникюр","մատնահարդարում","manicure",
                      "պեդիկյուր","pedicure","макияж","դիմահարդարում","makeup","визаж"}
            specific_hits=concept_overlap.intersection(specific)
            best=max(best,500 + min(len(concept_overlap),5)*20 + len(specific_hits)*80)
            reasons.append("concept")

        overlap=target_tokens.intersection(catalog_tokens)
        if overlap:
            best=max(best,300 + min(len(overlap),5)*20)
            reasons.append("token")

        root_hits=0
        for token in target_tokens:
            if len(token) < 4:
                continue
            if any(token in ct or ct in token for ct in catalog_tokens if len(ct) >= 4):
                root_hits += 1
        if root_hits:
            best=max(best,180 + min(root_hits,5)*15)
            reasons.append("root")

        if best:
            label=str(row.get("name_am") or row.get("name_ru") or row.get("name_en") or "")
            scored.append((best,len(reasons),label.casefold(),row,reasons))

    scored.sort(key=lambda item:(item[0],item[1],item[2]),reverse=True)
    return [{"row":x[3],"score":x[0],"reasons":x[4]} for x in scored[:max(1,int(limit))]]

def find_best_subcategory(service_name, master_category_id=None, requested_value=None):
    query=str(requested_value or "").strip() or str(service_name or "").strip()
    candidates=_admin_category_candidates(query, master_category_id, limit=8)
    if not candidates:
        return None
    top=candidates[0]
    if requested_value:
        return top["row"] if top["score"] >= 700 else None
    return top["row"] if top["score"] >= 500 else None

def _admin_category_suggestions(service_name, master_category_id=None):
    candidates=_admin_category_candidates(service_name, master_category_id, limit=5)
    result=[]
    for item in candidates:
        row=item["row"]
        label=str(row.get("name_am") or row.get("name_ru") or row.get("name_en") or "").strip()
        if label and label not in result:
            result.append(label)
    return result

def _admin_category_by_text(value, master_category_id=None):
    return find_best_subcategory("", master_category_id, requested_value=value)

def _application_review(aid):
    app=platform_db.one("SELECT * FROM partner_applications WHERE id=%s",(int(aid),))
    if not app: return "Заявка не найдена."
    checks=[("Компания",app.get("business_name")),("Услуга",app.get("service_name")),("Цена",app.get("price")),("Направление",app.get("direction_name")),("Подкатегория",app.get("subcategory_name")),("Город",app.get("location_city")),("Адрес",app.get("address")),("Телефон",app.get("phone"))]
    missing=[name for name,value in checks if value is None or str(value).strip()==""]
    return "Основные поля заполнены. Явно пустых обязательных данных не вижу." if not missing else "Пустые поля: "+", ".join(missing)

def _application_field_answer(aid,field):
    app=platform_db.one("SELECT * FROM partner_applications WHERE id=%s",(int(aid),))
    if not app: return "Заявка не найдена."
    if field=="category":
        master_id=app.get("master_category_id")
        master=None
        if master_id:
            master=platform_db.one("SELECT name_am,name_ru,name_en FROM master_categories WHERE id=%s",(int(master_id),))
        master_name=(master.get("name_am") or master.get("name_ru") or master.get("name_en")) if master else (app.get("direction_name") or "—")
        return "🧭 Направություն: «"+str(master_name or "—")+"»\n📂 Ենթակատեգորիա: «"+str(app.get("subcategory_name") or "—")+"»"
    if field=="subcategory":
        return "🏷 Подкатегория: «"+str(app.get("subcategory_name") or "—")+"»"
    if field=="service":
        return "🛠 Услуга: «"+str(app.get("service_name") or "—")+"»"
    return "Укажите поле заявки."

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
    system="""You are the structural Intent Extractor for the Armenia AI Guide admin panel.
Your ONLY job is to parse the administrator's natural language into strict JSON.
You do not talk to the user. You do not advise. Output ONLY valid JSON.

STRICT RULES:
1. NEVER invent, hallucinate, predict, or resolve database IDs. Python resolves all IDs.
2. Use the supplied focused application and conversation history. Short follow-ups continue the current task.
3. If the administrator says "это", "эта", "здесь", "այս", "այստեղ", "ուղղիր", "исправь", or only names a field, use the focused application and previous field when available.
4. If a correction is requested without a new value, set value_raw=null and action_required=suggest_alternatives.
5. If a new value is explicitly provided, put ONLY the new/target value in value_raw and use action_required=execute. For replacement language A to B, ignore old A and extract only new B into value_raw. This applies to Russian ("измени A на B"), Armenian ("A-ն փոխիր B-ով"), and English ("change A to B"). Never include verbs or particles such as "на", "ով", or "to" in value_raw.
6. Questions and inspection requests are READ ONLY. Do not turn a question into a mutation.
7. Understand Armenian, Russian and English, including mixed-language messages.

Return exactly:
{"intent":"show_applications|show_application_count|show_application|show_application_field|inspect_application|suggest_application_correction|edit_application|approve_application|reject_application|clarify_application|show_partners|show_businesses|unknown","target":"application|partner|business|service|category|document|order","application_id":null,"field":"subcategory|price|service_name|location_city|description|null","action_required":"suggest_alternatives|request_value|execute|read_only","value_raw":null,"reason":null,"confidence":0.0}

INTENT RULES:
- "сколько заявок", "քանի հայտ", "how many applications" -> show_application_count.
- "покажи/проверь заявки" -> show_applications.
- "открой/покажи заявку" -> show_application.
- "проверь ... и исправь ошибки", "նայիր հայտերը և ուղղիր սխալները" -> suggest_application_correction. This means inspect first and prepare a proposal; NEVER mutate directly.
- Questions about a field are read_only and use show_application_field or inspect_application.
- A correction without a value is edit_application with value_raw=null and action_required=suggest_alternatives.
- An explicit replacement value is edit_application with action_required=execute.
- Never invent IDs or catalog values. Python resolves all IDs and catalog values.

FIELD RULES:
- ենթակատեգորիա / подкатегория / subcategory -> subcategory
- կատեգորիա / категория -> subcategory when changing the application's catalog category
- գին / цена / price -> price
- անուն / название услуги / service name -> service_name
- քաղաք / город / city -> location_city
- նկարագրություն / описание / description -> description

CONTEXT EXAMPLES:
Focused application #36, last field=subcategory:
"Ուղղիր" -> edit_application, field=subcategory, application_id=36, value_raw=null, action_required=suggest_alternatives.
"исправь" -> same.
"подкатегория" -> edit_application, field=subcategory, application_id=36, value_raw=null, action_required=suggest_alternatives.
"նայիր ենթակատեգորիան և ուղղիր" -> edit_application, field=subcategory, application_id=36, value_raw=null, action_required=suggest_alternatives.
"проверь какая подкатегория" -> show_application_field, field=subcategory.
"открой заявку #36" -> show_application, application_id=36.
"поменяй подкатегорию на Брови" -> edit_application, field=subcategory, value_raw="Брови", action_required=execute.

Never return a catalog ID. Never invent a value not present in the administrator's message or supplied context.
"""
    payload=json.dumps({"message":message,"context":ctx},ensure_ascii=False,default=str)
    messages=[{"role":"system","content":system},{"role":"user","content":payload}]
    try:
        resp=await client.chat.completions.create(model=model,messages=messages,temperature=0,max_tokens=260,)
    except Exception as first:
        if model!="openai/gpt-oss-20b" and ("404" in str(first) or "model" in str(first).lower()):
            resp=await client.chat.completions.create(model="openai/gpt-oss-20b",messages=messages,temperature=0,max_tokens=260)
        else: raise
    raw=(resp.choices[0].message.content or "").strip()
    raw=re.sub(r"^\s*\`\`\`(?:json)?\s*|\s*\`\`\`\s*$","",raw,flags=re.I|re.S).strip()
    try:
        data=json.loads(raw)
    except json.JSONDecodeError:
        start=raw.find("{")
        end=raw.rfind("}")
        if start<0 or end<=start:
            raise RuntimeError("Groq returned invalid JSON")
        data=json.loads(raw[start:end+1])
    if not isinstance(data,dict):
        raise RuntimeError("Groq returned a non-object intent")
    return data
def _admin_fallback_intent(message,focused_id=None):
    text=_norm(message)
    # Deterministic fallback is deliberately semantic, not a list of UI commands.
    if re.search(r"(сколько|քանի)\s+.*(заяв|հայտ)|how many applications",text):
        return {"intent":"show_application_count","target":"application","confidence":0.9}
    if any(x in text for x in ("նայիր հայտերը և ուղղիր սխալները","проверь заявки и исправь ошибки","проверь заявки и исправь","ստուգիր հայտերը և ուղղիր","check applications and fix")):
        return {"intent":"suggest_application_correction","target":"application","confidence":0.9}
    if any(x in text for x in ("ստուգիր հայտերը","ցույց տուր հայտերը","проверь заявки","покажи заявки","show applications")):
        return {"intent":"show_applications","target":"application","confidence":0.8}
    if any(x in text for x in ("ինչ կատեգոր","ինչ ենթակատեգոր","какая категория","какая подкатегория","под какой категор","what category","which category")):
        return {"intent":"show_application_field","target":"application","field":"subcategory","application_id":focused_id,"confidence":0.8}
    if any(x in text for x in ("հայտը ճիշտ է լրացված","հայտը ճիշտ է լրացված՞","проверь заявку","заявка заполнена правильно")):
        return {"intent":"inspect_application","target":"application","application_id":focused_id,"confidence":0.8}
    if any(x in text for x in ("ուղղիր","исправь","շտկիր")) and focused_id:
        return {"intent":"edit_application","target":"application","application_id":focused_id,
                "field":"subcategory","value_raw":None,"action_required":"suggest_alternatives","confidence":0.7}
    m=re.search(r"(?:заявк[ауеи]?|հայտ(?:ը|ի)?|application)\s*#?\s*(\d+)",text)
    aid=int(m.group(1)) if m else focused_id
    if any(x in text for x in ("открой","բացիր","open","покажи")) and aid:
        return {"intent":"show_application","target":"application","application_id":aid,"confidence":0.8}
    return {"intent":"unknown","confidence":0.0}



def _admin_parse_replacement(message):
    """Parse only high-confidence A->B replacement syntax. Never resolves catalog IDs."""
    text=str(message or "").strip()
    if not text:
        return None
    patterns=[
        (r"^\s*(.+?)\s*(?:→|->|=>)\s*(.+?)\s*$", 2),
        (r"^\s*(?:измени|поменяй|замени)\s+.+?\s+(?:на)\s+(.+?)\s*$", 1),
        (r"^\s*(?:change|replace)\s+.+?\s+(?:to|with)\s+(.+?)\s*$", 1),
        (r"^\s*.+?\s*(?:փոխիր|փոխարինիր|դարձրու)\s+(.+?)\s*$", 1),
    ]
    for pattern,index in patterns:
        m=re.match(pattern,text,flags=re.I|re.U)
        if not m:
            continue
        target=m.group(index).strip()
        target=re.sub(r"^[\"'«]+|[\"'».,!?]+$","",target).strip()
        if target and len(target)<=120:
            return target
    return None


def _admin_audit_application_catalog(app):
    """Audit current subcategory against the most specific live catalog concept."""
    if not app:
        return None
    service=str(app.get("service_name") or "").strip()
    if not service:
        return None
    candidates=_admin_category_candidates(service,app.get("master_category_id"),limit=20)
    if not candidates:
        return None

    specific_groups={
        "брови":{"брови","հոնքեր","հոնք","eyebrows","eyebrow"},
        "маникюр":{"маникюр","մատնահարդարում","manicure","եղունգ"},
        "педикюр":{"педикюр","պեդիկյուր","pedicure"},
        "макияж":{"макияж","դիմահարդարում","makeup","визаж"},
        "стрижка":{"стрижка","վարսավիր","սանրվածք","haircut"},
    }
    generic_groups={
        "окрашивание":{"окрашивание","ներկում","ներկել","coloring","colouring"},
        "волосы":{"волосы","մազ","մազեր","hair"},
    }
    service_concepts=_concept_tokens(service)
    specific_present={k for k,v in specific_groups.items() if service_concepts.intersection(v)}
    generic_present={k for k,v in generic_groups.items() if service_concepts.intersection(v)}

    ranked=[]
    for item in candidates:
        row=item["row"]
        cat_text=_norm(" ".join(str(row.get(k) or "") for k in ("name_am","name_ru","name_en")))
        cat_concepts=_concept_tokens(cat_text)
        spec_hits=sum(1 for k in specific_present if cat_concepts.intersection(specific_groups[k]))
        generic_hits=sum(1 for k in generic_present if cat_concepts.intersection(generic_groups[k]))
        score=item["score"] + spec_hits*1200 + generic_hits*80
        ranked.append((score,spec_hits,generic_hits,item))
    ranked.sort(key=lambda x:(x[0],x[1],-x[2]),reverse=True)

    best=ranked[0][3]["row"]
    current=_norm(str(app.get("subcategory_name") or ""))
    label=_norm(str(best.get("name_am") or best.get("name_ru") or best.get("name_en") or ""))
    if not label or current==label:
        return None
    if ranked[0][1] <= 0 and ranked[0][0] < 700:
        return None
    return best


async def admin_ai_message(admin_id,message):
    message=str(message or "").strip()
    if not message: return "Գրեք, թե ինչ պետք է ստուգեմ կամ փոխեմ։"
    state=_admin_session(admin_id); normalized=_norm(message)

    # Local state machine: confirmation and slot filling never call Groq.
    pending=state.get("pending_action")
    if pending and normalized in _CONFIRM_YES:
        try: reply=await _admin_execute_state_action(_admin_safe(pending))
        except Exception: reply="Не удалось сохранить изменение. Изменений не внесено."
        state["pending_action"]=None; state["waiting_for_input"]=None; state["last_focused_field"]=None
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply
    if pending and normalized in _CONFIRM_NO:
        state["pending_action"]=None; state["waiting_for_input"]=None; state["last_focused_field"]=None
        reply="Отменено. Никаких изменений не внесено."
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    waiting=state.get("waiting_for_input")
    if waiting:
        aid=waiting.get("application_id"); field=str(waiting.get("field") or ""); app=_admin_hydrate_application(aid)
        if app:
            raw=message
            if field=="price":
                try:
                    value=float(re.sub(r"[^0-9.,-]","",raw).replace(",","."))
                    if value<0: raise ValueError
                except Exception: return "Укажите цену числом, например: 2500."
            elif field in {"subcategory","service_name","location_city","description"}: value=raw
            else:
                state["waiting_for_input"]=None; return "Неизвестное поле для ввода."
            state["waiting_for_input"]=None; state["last_focused_application_id"]=int(aid); state["last_focused_field"]=field
            if field=="subcategory":
                cat=find_best_subcategory(str(app.get("service_name") or ""),app.get("master_category_id"),requested_value=value)
                if not cat: return "Не нашёл подкатегорию «"+value+"» в активном каталоге."
                action={"intent":"edit_application","application_id":int(aid),"field":"subcategory","category_id":int(cat["id"]),
                    "old_value_name":str(app.get("subcategory_name") or "—"),"new_value":str(cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"))}
            else: action={"intent":"edit_application","application_id":int(aid),"field":field,"new_value":value}
            state["pending_action"]=_admin_safe(action)
            reply="🤖 Подготовил действие:\n\n"+_admin_state_preview(action)+"\n\nПодтвердить? «да» / «нет»"
            _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply
        state["waiting_for_input"]=None


    # High-confidence replacement syntax is handled locally so old A cannot become the new value.
    focused_id=state.get("last_focused_application_id")
    replacement=_admin_parse_replacement(message)
    if replacement and focused_id:
        app=_admin_hydrate_application(focused_id)
        if app:
            field=state.get("last_focused_field") or "subcategory"
            if field in {"category","subcategory"}:
                cat=find_best_subcategory("",app.get("master_category_id"),requested_value=replacement)
                if not cat:
                    suggestions=_admin_category_suggestions(replacement,app.get("master_category_id"))
                    state["waiting_for_input"]={"field":"subcategory","application_id":int(focused_id)}
                    reply=("🔍 Заявка #"+str(focused_id)+"\n"
                           "Я понял, что нужно изменить подкатегорию на «"+replacement+"», "
                           "но не нашёл точного соответствия в активном каталоге.")
                    if suggestions:
                        reply+="\nВозможные варианты: "+", ".join("«"+x+"»" for x in suggestions)
                    reply+="\n\nВведите точное название подкатегории."
                    _admin_history(state,"admin",message)
                    _admin_history(state,"assistant",reply)
                    return reply
                action={"intent":"edit_application","application_id":int(focused_id),"field":"subcategory",
                        "category_id":int(cat["id"]),
                        "old_value_name":str(app.get("subcategory_name") or "—"),
                        "new_value":str(cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"))}
                state["pending_action"]=_admin_safe(action)
                state["waiting_for_input"]=None
                state["last_focused_field"]="subcategory"
                reply="🤖 Подготовил действие:\n\n"+_admin_state_preview(action)+"\n\nПодтвердить? «да» / «нет»"
                _admin_history(state,"admin",message)
                _admin_history(state,"assistant",reply)
                return reply

    # Context hydration: focused application + history + DB values, normalized for JSON.
    focused_id=state.get("last_focused_application_id")
    if not focused_id and len(state.get("last_shown_applications") or [])==1:
        try: focused_id=int(state["last_shown_applications"][0]["id"]); state["last_focused_application_id"]=focused_id
        except Exception: focused_id=None
    # Deterministic high-confidence intents for short admin commands.
    local_text=_norm(message)
    full_app_match=bool(re.search(
        r"(?:ամբողջական|ամբողջությամբ|ամբողջ|լիարժեք|ուղղված|полностью|полную|полное|всю|исправленную|целиком|full|complete).*(?:հայտ|заявк|application)|(?:հայտ|заявк|application).*(?:ամբողջական|ամբողջությամբ|ամբողջ|լիարժեք|ուղղված|полностью|полную|полное|всю|исправленную|целиком|full|complete)",
        local_text,re.IGNORECASE
    ))
    if full_app_match and re.search(r"(?:ցույց|покаж|открой|show|open)",local_text,re.IGNORECASE):
        id_match=re.search(r"(?:#|№)\s*(\d+)",local_text)
        requested_aid=int(id_match.group(1)) if id_match else None
        c={"intent":"show_full_application","target":"application",
           "application_id":requested_aid,"action_required":"read_only","confidence":1.0}
    elif re.search(r"(?:ստուգիր|проверь|check).*(?:հայտ|заявк|application).*(?:ուղղիր|исправ|fix|շտկ)",local_text):
        c={"intent":"suggest_application_correction","target":"application","application_id":None,"action_required":"suggest_alternatives","confidence":1.0}
    elif re.search(r"(?:ստուգիր|проверь|check).*(?:ենթակատեգոր|подкатегор|subcategory)",local_text):
        c={"intent":"show_application_field","target":"application","field":"subcategory","application_id":None,"action_required":"read_only","confidence":1.0}
    elif re.fullmatch(r"(?:ուղղիր|исправь|շտկիր)(?:\s+(?:սխալները|ошибки|ошибка|errors))?",local_text):
        c={"intent":"suggest_application_correction","target":"application","application_id":None,"action_required":"suggest_alternatives","confidence":1.0}
    else:
        c=None
    ctx=_admin_hydrate_context(state)
    if c is None:
        try: c=await _admin_ai_json(message,ctx)
        except Exception: c=_admin_fallback_intent(message,focused_id)

    intent=str(c.get("intent") or "unknown").lower()
    target=str(c.get("target") or "").lower()
    aid=c.get("application_id") or focused_id
    field=str(c.get("field") or "").lower()
    intent={"open_application":"show_application","count_applications":"show_application_count","count":"show_application_count","inspect":"inspect_application"}.get(intent,intent)

    if field in {"category","subcategory","price","service_name","location_city","description"}: state["last_focused_field"]=field
    elif not field or field=="none": field=state.get("last_focused_field") or ""
    if aid:
        try: aid=int(aid); state["last_focused_application_id"]=aid
        except (TypeError,ValueError): aid=None

    is_question=("?" in message or "՞" in message or bool(re.search(r"\b(как|какая|какие|какое|почему|зачем|что|где|сколько|what|which|how|why|where|how many|ինչ|ինչպես|որ|որտեղ|արդյոք|քանի)\b",message.casefold())))
    if is_question and intent in {"edit_application","approve_application","reject_application","clarify_application"}: intent="show_application_field" if field else "inspect_application"

    if intent=="show_application_count":
        reply=await _admin_execute({"intent":"show_application_count"})
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    if intent=="show_applications":
        try: rows=_admin_context(limit=30).get("applications",[])
        except Exception: rows=[]
        state["last_shown_applications"]=[{"id":int(x["id"]),"business_name":x.get("business_name"),"service_name":x.get("service_name")} for x in rows[:20]]
        if len(state["last_shown_applications"])==1: state["last_focused_application_id"]=state["last_shown_applications"][0]["id"]
        reply=await _admin_execute({"intent":"show_applications"})
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    if intent in {"show_application","inspect_application","show_application_field"}:
        if not aid: return "Сначала откройте заявку или укажите её номер."
        if not _admin_hydrate_application(aid): return "Заявка #"+str(aid)+" не найдена."
        state["last_focused_application_id"]=int(aid)
        reply=await _admin_execute({"intent":"open_application","application_id":int(aid)})
        if intent=="inspect_application": reply+="\n\n🔎 Проверка заполнения:\n"+_application_review(int(aid))
        elif intent=="show_application_field": reply+="\n\n"+_application_field_answer(int(aid),field)
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    # Compound command: inspect and propose, never mutate.
    if intent=="suggest_application_correction":
        rows=_admin_context(limit=20).get("applications",[]); proposals=[]
        for row in rows[:10]:
            app=_admin_hydrate_application(row["id"])
            if not app: continue
            cat=_admin_audit_application_catalog(app)
            if cat:
                proposals.append((app,cat))
        if not proposals: return "🔎 Проверил заявки: явных расхождений подкатегорий с активным каталогом не обнаружено."
        if len(proposals)>1:
            reply="🔎 Найдены возможные исправления:\n"+"\n".join("#"+str(a["id"])+" · "+str(a.get("service_name") or "—")+" · «"+str(a.get("subcategory_name") or "—")+"» → «"+str(c.get("name_am") or c.get("name_ru") or c.get("name_en"))+"»" for a,c in proposals)+"\n\nУкажите заявку для подготовки изменения."
            _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply
        app,cat=proposals[0]; aid=int(app["id"]); state["last_focused_application_id"]=aid; state["last_focused_field"]="subcategory"
        action={"intent":"edit_application","application_id":aid,"field":"subcategory","category_id":int(cat["id"]),"old_value_name":str(app.get("subcategory_name") or "—"),"new_value":str(cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"))}
        state["pending_action"]=_admin_safe(action)
        reply="🔎 Нашёл исправление для заявки #"+str(aid)+":\n\n"+_admin_state_preview(action)+"\n\nПодтвердить? «да» / «нет»"
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    if intent not in {"edit_application","approve_application","reject_application","clarify_application"}:
        reply="Я понял запрос не полностью. Скажите: «покажи заявки», «сколько заявок?», «открой #36», «проверь подкатегорию» или «цена неправильная»."
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    # Resolver -> ActionPlan. Only the ActionPlan can reach the executor.
    if not aid: return "Сначала откройте заявку или укажите её номер."
    app=_admin_hydrate_application(aid)
    if not app: return "Заявка #"+str(aid)+" не найдена."
    state["last_focused_application_id"]=int(aid)
    action={"intent":intent,"application_id":int(aid)}
    if intent=="edit_application":
        if field=="category": field="subcategory"
        if not field: return "Уточните поле: цена, название услуги, описание или подкатегория."
        value=str(c.get("value_raw") or c.get("value_text") or "").strip()
        if not value:
            state["last_focused_field"]=field; state["waiting_for_input"]={"field":field,"application_id":int(aid)}
            return "Какое значение установить для поля «"+field+"»?"
        if field=="subcategory":
            cat=find_best_subcategory(str(app.get("service_name") or ""),app.get("master_category_id"),requested_value=value)
            if not cat:
                suggestions=_admin_category_suggestions(value,app.get("master_category_id"))
                state["waiting_for_input"]={"field":"subcategory","application_id":int(aid)}
                reply=("🔍 Заявка #"+str(aid)+"\n"
                       "Я понял, что нужно установить подкатегорию «"+value+"», "
                       "но не нашёл точного соответствия в активном каталоге.")
                if suggestions:
                    reply+="\nВозможные варианты: "+", ".join("«"+x+"»" for x in suggestions)
                reply+="\n\nВведите точное название подкатегории."
                _admin_history(state,"admin",message)
                _admin_history(state,"assistant",reply)
                return reply
            if app.get("master_category_id") is not None and int(cat["master_category_id"])!=int(app["master_category_id"]): return "Подкатегория относится к другому направлению."
            action.update({"field":"subcategory","category_id":int(cat["id"]),"old_value_name":str(app.get("subcategory_name") or "—"),"new_value":str(cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"))})
        else:
            if field not in {"price","service_name","location_city","description"}: return "Это поле пока не поддерживается для изменения."
            if field=="price":
                try: value=float(re.sub(r"[^0-9.,-]","",value).replace(",","."))
                except Exception: return "Цена должна быть числом, например: 2500."
            action.update({"field":field,"new_value":value})
    else:
        action["new_value"]=c.get("value_text")
        if intent=="reject_application": action["reason"]=c.get("reason") or c.get("value_text")
        if intent=="clarify_application": action["admin_note"]=c.get("reason") or c.get("value_text")
    action=_admin_safe(action); state["pending_action"]=action; state["waiting_for_input"]=None
    reply="🤖 Подготовил действие:\n\n"+_admin_state_preview(action)+"\n\nПодтвердить? «да» или «нет»."
    _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply


async def api_admin_assistant(request):
    _admin(request)
    data=await request.json(); message=str(data.get("message") or "").strip()
    if not message: return web.json_response({"ok":False,"error":"message_required"},status=400)
    try:
        return web.json_response({"ok":True,"reply":await admin_ai_message(int(request.app.get("stage3_admin_id") or 0),message)})
    except Exception:
        try:
            state=_admin_session(int(request.app.get("stage3_admin_id") or 0))
            state["pending_action"]=None; state["waiting_for_input"]=None
        except Exception: pass
        import logging
        logging.getLogger(__name__).exception("admin_ai_turn_failed")
        return web.json_response({"ok":True,"reply":"Не удалось безопасно обработать команду. Изменений не внесено. Повторите команду."})



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

