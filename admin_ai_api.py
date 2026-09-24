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
from ai_data_tools import DataTools, DataToolError


def _norm(text):
    if text is None:
        return ""
    return " ".join(str(text).casefold().strip().split())



_ADMIN_LOCALES={"am":{"unknown":"Ես ամբողջությամբ չհասկացա հարցումը։ Կարող եք հարցնել բնական լեզվով՝ հայտերի, գործընկերների, ընկերությունների կամ կատալոգի մասին։","need_application":"Սկզբում բացեք հայտը կամ նշեք դրա համարը։","not_found":"Հայտ #{id} չի գտնվել։","last_item":"Սա ընթացիկ ցուցակի վերջին տարրն է։","safe_error":"Չհաջողվեց անվտանգ մշակել հարցումը։ Տվյալները չեն փոխվել։ Փորձեք կրկին։"},"ru":{"unknown":"Я не полностью понял запрос. Можно спрашивать обычным языком о заявках, партнёрах, компаниях или каталоге.","need_application":"Сначала откройте заявку или укажите её номер.","not_found":"Заявка #{id} не найдена.","last_item":"Это последний элемент в текущем списке.","safe_error":"Не удалось безопасно обработать запрос. Данные не изменены. Повторите запрос."},"en":{"unknown":"I didn't fully understand the request. You can ask naturally about applications, partners, businesses, or the catalog.","need_application":"Open an application first or specify its number.","not_found":"Application #{id} was not found.","last_item":"This is the last item in the current list.","safe_error":"I couldn't safely process the request. No data was changed. Please try again."}}

def _admin_detect_language(text):
    t=str(text or "")
    am=sum(1 for ch in t if "\u0530"<=ch<="\u058f")
    ru=sum(1 for ch in t if "\u0400"<=ch<="\u04ff")
    en=sum(1 for ch in t if "a"<=ch.lower()<="z")
    if am>=max(1,ru,en): return "am"
    if ru>=max(1,am,en): return "ru"
    if en>0: return "en"
    return "ru"

def _admin_localized(lang,key,**kwargs):
    return _ADMIN_LOCALES.get(lang,_ADMIN_LOCALES["ru"]).get(key,key).format(**kwargs)

def _admin_normalize_plan(data,message=""):
    """Normalize the single semantic ActionPlan contract used by the admin AI."""
    if not isinstance(data,dict): data={}
    lang=str(data.get("response_language") or data.get("language") or "").lower()[:2]
    if lang not in {"am","ru","en"}: lang=_admin_detect_language(message)
    try: confidence=float(data.get("confidence",0) or 0)
    except Exception: confidence=0.0
    # Keep one canonical identity field internally while accepting legacy output.
    entity_id=data.get("entity_id", data.get("application_id"))
    if entity_id not in (None,""):
        try: entity_id=int(entity_id)
        except (TypeError,ValueError): entity_id=None
    data["entity_id"]=entity_id
    data["application_id"]=entity_id
    data["entity_type"]=str(data.get("entity_type") or data.get("target") or "").strip().lower()
    data["entity_name"]=str(data.get("entity_name") or data.get("entity_query") or "").strip()[:200]
    raw_needed=data.get("data_needed")
    if not isinstance(raw_needed,list): raw_needed=[]
    data["data_needed"]=[str(x).strip().lower() for x in raw_needed if str(x).strip()]
    raw_tools=data.get("tool_requests")
    if not isinstance(raw_tools,list): raw_tools=[]
    data["tool_requests"]=[
        {"name":str(x.get("name") or "").strip(),"arguments":x.get("arguments") if isinstance(x.get("arguments"),dict) else {}}
        for x in raw_tools if isinstance(x,dict) and str(x.get("name") or "").strip()
    ][:6]
    data["navigation"]=data.get("navigation")
    data["response_language"]=lang
    data["confidence"]=max(0.0,min(1.0,confidence))
    data["count_only"]=bool(data.get("count_only",False))
    data["reasoning_summary"]=str(data.get("reasoning_summary") or "")[:500]
    data["intent"]=str(data.get("intent") or "unknown").strip().lower()
    data["target"]=str(data.get("target") or "").strip().lower()
    data["field"]=None if data.get("field") in (None,"","none") else str(data.get("field")).strip().lower()
    data["value_raw"]=data.get("value_raw",data.get("value_text"))
    data.setdefault("filters",{})
    data.setdefault("sort",None)
    try: data["limit"]=max(1,int(data.get("limit",20) or 20))
    except Exception: data["limit"]=20
    data.setdefault("action_required","read_only")
    return data

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
        state={"last_focused_application_id":None,"last_focused_field":None,"last_focused_entity_type":None,"last_focused_entity_id":None,"last_shown_applications":[],"current_list":[],"current_position":None,
               "last_query":None,"last_query_target":None,"last_shown_query_rows":[],"last_result_kind":None,"last_result_facts":None,"pending_action":None,"waiting_for_input":None,"history":[],"last_action":None,"last_action_failed":False,"last_error":None,"last_error_context":None,"retry_count":0,"updated_at":now}
        _ADMIN_SESSIONS[sid]=state
    state["updated_at"]=now
    return state


def _admin_history(state,role,text):
    state.setdefault("history",[]).append({"role":str(role),"content":str(text)[:1200]})
    state["history"]=_admin_safe(state["history"][-10:]); state["updated_at"]=time.time()


def _admin_hydrate_application(aid):
    if not aid: return None
    try:
        return _admin_safe(platform_db.one("""SELECT id,partner_id,business_name,status,service_name,price,direction_name,
            master_category_id,subcategory_name,category_id,location_marz,location_city,location_village,address,
            phone,description,object_name,document_id,payload_json,created_at,updated_at
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
        "last_focused_field":state.get("last_focused_field"),
        "last_query":state.get("last_query"),
        "last_query_target":state.get("last_query_target"),
        "last_shown_query_rows":state.get("last_shown_query_rows",[]),
        "last_result_kind":state.get("last_result_kind"),
        "last_result_facts":state.get("last_result_facts"),
        "last_action":state.get("last_action"),"last_action_failed":state.get("last_action_failed",False),
        "last_error":state.get("last_error"),"last_error_context":state.get("last_error_context"),"retry_count":state.get("retry_count",0),
        "query_capabilities":{"targets":["applications","partners","businesses","catalog","master_categories","catalog_overview","services"],"catalog_behavior":"master_categories and catalog return the complete current catalog without an artificial row limit; catalog_overview returns live database counts","operators":["eq","neq","contains","gt","gte","lt","lte","in"]},
        "history":state.get("history",[])[-6:]})


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



# =====================================================================
# Dynamic Admin Query / Skills layer
# =====================================================================
_ADMIN_QUERY_TARGETS={
 "applications":{"table":"partner_applications a","select":"a.id,a.business_name,a.status,a.service_name,a.price,a.direction_name,a.master_category_id,a.subcategory_name,a.category_id,a.location_marz,a.location_city,a.location_village,a.address,a.phone,a.description,a.created_at,a.updated_at","order":"a.created_at DESC","limit":50,"aliases":{"application","applications","requests","заявки","հայտեր"},"fields":{"status":"a.status","business_name":"a.business_name","service_name":"a.service_name","price":"a.price","direction_name":"a.direction_name","subcategory_name":"a.subcategory_name","location_marz":"a.location_marz","location_city":"a.location_city","location_village":"a.location_village","address":"a.address","phone":"a.phone","description":"a.description","category_id":"a.category_id","master_category_id":"a.master_category_id"}},
 "partners":{"table":"partners p","select":"p.id,p.user_id,p.business_name,p.business_description,p.status,p.verification_status,p.contact_share_policy,p.created_at,p.updated_at","order":"p.created_at DESC","limit":50,"aliases":{"partner","partners","партнеры","գործընկերներ"},"fields":{"status":"p.status","verification_status":"p.verification_status","business_name":"p.business_name","business_description":"p.business_description","location_marz":"__PARTNER_LOCATION_MARZ__","location_city":"__PARTNER_LOCATION_CITY__"}},
 "businesses":{"table":"partner_businesses b JOIN partners p ON p.id=b.partner_id","select":"b.id,b.partner_id,b.name,b.description,b.phone,b.status,p.business_name AS partner_business_name,b.created_at","order":"b.created_at DESC","limit":50,"aliases":{"business","businesses","companies","компании","ընկերություններ"},"fields":{"status":"b.status","name":"b.name","description":"b.description","phone":"b.phone","partner_name":"p.business_name"}},
 "master_categories":{"table":"master_categories m","select":"m.id,m.name_am,m.name_ru,m.name_en,m.slug,m.is_active","order":"m.id ASC","limit":0,"aliases":{"master_categories","master category","master categories","directions","direction","главные категории","направления","ուղղություններ","ուղղություն","գլխավոր կատեգորիաներ","գլխավոր կատեգորիա"},"fields":{"name":"m.name_am","name_am":"m.name_am","name_ru":"m.name_ru","name_en":"m.name_en","slug":"m.slug","is_active":"m.is_active"}},
 "catalog":{"table":"categories c JOIN master_categories m ON m.id=c.master_category_id","select":"c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en","order":"c.id ASC","limit":0,"aliases":{"catalog","category","categories","subcategory","подкатегории","կատալոգ"},"fields":{"name":"c.name_am","name_am":"c.name_am","name_ru":"c.name_ru","name_en":"c.name_en","slug":"c.slug","master_category_id":"c.master_category_id","master_name":"m.name_am","master_name_am":"m.name_am","master_name_ru":"m.name_ru","master_name_en":"m.name_en"},"base_where":"c.is_active=TRUE AND m.is_active=TRUE"}, "catalog_overview":{"aliases":{"catalog overview","catalog_overview","կատալոգի ընդհանուր","ընդհանուր կատալոգ","catalog stats","catalog count","direction","directions","master categories","subcategory","subcategories","ուղղություն","ուղղություններ","ենթաուղղություն","ենթաուղղություններ","направления","поднаправления"},"limit":1},
 "services":{"table":"services s LEFT JOIN categories c ON c.id=s.category_id LEFT JOIN master_categories m ON m.id=c.master_category_id LEFT JOIN partners p ON p.id=s.partner_id","select":"s.id,s.partner_id,s.business_id,s.name,s.category_id,s.price,s.status,p.business_name AS partner_name,c.name_am AS category_name_am,c.name_ru AS category_name_ru,c.name_en AS category_name_en,c.master_category_id,m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en,s.created_at","order":"s.id DESC","limit":50,"aliases":{"service","services","услуги","услуга","ծառայություններ","ծառայություն","uslugi"},"fields":{"name":"s.name","category_id":"s.category_id","category_name":"c.name_am","master_category_id":"c.master_category_id"}}}
_ADMIN_QUERY_FIELD_ALIASES={"city":"location_city","город":"location_city","քաղաք":"location_city","marz":"location_marz","region":"location_marz","область":"location_marz","մարզ":"location_marz","village":"location_village","село":"location_village","գյուղ":"location_village","address":"address","адрес":"address","հասցե":"address","price":"price","цена":"price","գին":"price","status":"status","статус":"status","կարգավիճակ":"status","name":"business_name","название":"business_name","անուն":"business_name","service":"service_name","service_name":"service_name","услуга":"service_name","подкатегория":"subcategory_name","subcategory":"subcategory_name","ենթակատեգորիա":"subcategory_name","verification_status":"verification_status"}
_ADMIN_STATUS_ALIASES={"applications":{"pending":["pending_admin","pending_partner","document_pending"],"moderation":["pending_admin"]},"partners":{"pending":["pending"],"moderation":["pending","pending_verification"]},"businesses":{}}
def _admin_normalize_location(field,value):
    text=str(value or "").strip()
    key=_norm(text)
    aliases={
        "location_marz":{"котайк":"Kotayk","կոտայք":"Kotayk","kotayk":"Kotayk"},
        "location_city":{"раздан":"Հրազդան","հրազդան":"Հրազդան","hrazdan":"Հրազդան"}
    }
    return aliases.get(field,{}).get(key,text)

def _admin_query_target(value):
 text=_norm(value)
 if text in _ADMIN_QUERY_TARGETS:return text
 for key,spec in _ADMIN_QUERY_TARGETS.items():
  if text in {_norm(x) for x in spec.get("aliases",set())}:return key
 return None
def _admin_query_filter_items(filters,target=None):
 if not isinstance(filters,dict):return []
 items=[]
 for raw_field,raw_value in filters.items():
  field=_ADMIN_QUERY_FIELD_ALIASES.get(_norm(raw_field),_norm(raw_field))
  if target=="catalog" and _norm(raw_field) in {"subcategory_name","subcategory","ենթակատեգորիա","подкатегория","category_name","category"}: field="name"
  elif target=="catalog" and _norm(raw_field) in {"master_name","master_category_name","direction_name","ուղղություն","գլխավոր կատեգորիա","направление"}: field="master_name"
  if _norm(raw_field)=="name" and target=="catalog": field="name"
  elif _norm(raw_field)=="name" and target=="businesses": field="name"
  if field in {"location_marz","location_city"}: raw_value=_admin_normalize_location(field,raw_value)
  if isinstance(raw_value,dict):
   for op,value in raw_value.items():items.append((field,_norm(op),value))
  else:items.append((field,"eq",raw_value))
 return items
def _admin_query_build(target,filters,limit=20,sort=None):
 target=_admin_query_target(target)
 if not target:return None,"Неизвестный объект данных."
 if target=="catalog_overview":
  return ("SELECT (SELECT COUNT(*) FROM master_categories) AS master_categories_count, (SELECT COUNT(*) FROM categories) AS subcategories_count, (SELECT COUNT(*) FROM master_categories WHERE is_active=TRUE) AS master_categories_active, (SELECT COUNT(*) FROM categories WHERE is_active=TRUE) AS subcategories_active", (), target), None
 spec=_ADMIN_QUERY_TARGETS[target];clauses=[];params=[]
 if spec.get("base_where"):clauses.append(spec["base_where"])
 for field,op,value in _admin_query_filter_items(filters,target):
  column=spec["fields"].get(field)
  if not column:return None,"Фильтр «"+str(field)+"» недоступен для объекта «"+target+"»."
  op=str(op or "eq").casefold().strip()
  if target=="partners" and field in {"location_marz","location_city"}:
   pa_field="location_marz" if field=="location_marz" else "location_city"
   clauses.append("EXISTS (SELECT 1 FROM partner_applications pa WHERE pa.partner_id=p.id AND pa."+pa_field+" = %s)")
   params.append(value)
   continue
  if op in {"eq","equals","="}:
   aliases=_ADMIN_STATUS_ALIASES.get(target,{}).get(_norm(value))
   if field=="status" and aliases:clauses.append(column+" = ANY(%s)");params.append(list(aliases))
   else:clauses.append(column+" = %s");params.append(value)
  elif op in {"neq","not_equals","!="}:clauses.append(column+" <> %s");params.append(value)
  elif op in {"contains","like","ilike"}:clauses.append("COALESCE("+column+",'') ILIKE %s");params.append("%"+str(value)+"%")
  elif op in {"gt","greater_than","price_gt"}:clauses.append(column+" > %s");params.append(value)
  elif op in {"gte","greater_or_equal","at_least","price_gte"}:clauses.append(column+" >= %s");params.append(value)
  elif op in {"lt","less_than","price_lt"}:clauses.append(column+" < %s");params.append(value)
  elif op in {"lte","less_or_equal","at_most","price_lte"}:clauses.append(column+" <= %s");params.append(value)
  elif op=="in":
   if not isinstance(value,list) or not value or len(value)>20:return None,"Оператор in требует список до 20 значений."
   clauses.append(column+" = ANY(%s)");params.append(value)
  else:return None,"Оператор «"+op+"» не разрешён."
 try:limit=max(1,int(limit or 20))
 except Exception:limit=20
 where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
 order_sql=spec["order"]
 if isinstance(sort,dict):
  sf=str(sort.get("field") or "").casefold();sd=str(sort.get("direction") or "desc").casefold()
  if sf in {"price","created_at","business_name","service_name","name"} and sd in {"asc","desc"}:
   sort_col=sf
   if target=="applications":sort_col="a."+sort_col
   elif target=="partners":sort_col="p."+sort_col
   elif target=="businesses":sort_col="b."+sort_col
   elif target=="catalog":sort_col="c."+sort_col
   order_sql=sort_col+" "+sd.upper()
 sql="SELECT "+spec["select"]+" FROM "+spec["table"]+where+" ORDER BY "+order_sql
 if target not in {"master_categories","catalog"}:
  sql += " LIMIT %s";params.append(limit)
 return (sql,tuple(params),target),None
def _admin_service_price_map(service_rows):
    """Use the actual partner-specific service price stored on services.price."""
    result={}
    for row in service_rows or []:
        try: sid=int(row.get("id"))
        except (TypeError,ValueError): continue
        price=row.get("price")
        if price not in (None,""):
            result[sid]=[price]
    return result

def _admin_query_rows(target,filters,limit=20,sort=None):
 built,error=_admin_query_build(target,filters,limit,sort)
 if error:return None,error
 sql,params,target=built
 try:
  rows=platform_db.rows(sql,params)
  if target=="services" and rows:
   price_map=_admin_service_price_map(rows)
   for x in rows:
    vals=price_map.get(int(x["id"])) if x.get("id") is not None else None
    x["prices_amd"]=vals or []
    x["price_amd"]=vals[0] if vals and len(vals)==1 else None
    x["currency"]="AMD" if vals else None
  return rows,None
 except Exception as exc:return None,"Չհաջողվեց կատարել որոնումը՝ "+str(exc)[:180]

async def _admin_query_answer(question,target,filters,limit=20,sort=None):
 rows,error=_admin_query_rows(target,filters,limit,sort)
 if error:return "⚠️ "+error
 answer_facts={"target":target,"rows":rows or []}
 if target=="services":
  answer_facts["category_audit"]=_admin_service_category_audit(rows or [])
 fallback=_admin_query_result_text(target,rows,filters,question)
 if not rows and target=="catalog" and filters:
  # Natural-language catalog searches often arrive as an exact filter. Retry as a live name search.
  retry={}
  for k,v in (filters or {}).items():
   if _norm(k) in {"name","subcategory_name","subcategory","category_name","ենթակատեգորիա","подкатегория","category"}:
    retry["name"]={"contains":v.get("contains") if isinstance(v,dict) and v.get("contains") is not None else v}
   else: retry[k]=v
  rows,error=_admin_query_rows(target,retry,limit,sort)
  if error:return "⚠️ "+error
 if not rows:return fallback
 try:
  from groq import AsyncGroq
  key=os.getenv("GROQ_API_KEY","").strip()
  if not key:return fallback
  model=os.getenv("GROQ_MODEL","").strip() or "openai/gpt-oss-20b"
  client=AsyncGroq(api_key=key)
  payload=json.dumps({"question":question,"target":target,"filters":filters,"rows":rows,"category_audit":answer_facts.get("category_audit") if target=="services" else None},ensure_ascii=False,default=str)
  resp=await client.chat.completions.create(model=model,messages=[
   {"role":"system","content":"Answer the Armenia AI Guide administrator in the same language as the question. Use ONLY the supplied database facts. Be concise and factual. Mention the count when relevant. Never invent facts. Prices in service rows are AMD (֏). If category_audit is supplied and the question asks whether services are incorrectly categorized, use its verdicts: matched=no verified mismatch, review=possible mismatch requiring review, insufficient_data=cannot determine. Do not answer an audit question by merely dumping the service list. For catalog_overview, master_categories_count and subcategories_count are the real active catalog counts and short follow-ups remain about that catalog context. Read-only answer."},
   {"role":"user","content":payload}],temperature=0,max_tokens=500)
  answer=(resp.choices[0].message.content or "").strip()
  return answer or fallback
 except Exception:return fallback

def _admin_query_result_text(target,rows,filters,question):
 if not rows:return "🔎 Ничего не найдено."
 labels={"applications":"📨 Заявки","partners":"🤝 Партнёры","businesses":"🏢 Компании","catalog":"📚 Каталог","master_categories":"📂 Ուղղություններ","catalog_overview":"📚 Катալոգ","services":"🛠 Услуги"}
 if target=="catalog_overview":
  x=rows[0]
  return ("📚 Կատալոգ՝ "+str(x.get("master_categories_count") or 0)+" ուղղություն, "
          +str(x.get("subcategories_count") or 0)+" ենթաուղղություն։")
 lines=[labels.get(target,"🔎 Результат")+" ("+str(len(rows))+"):"]
 for x in rows:
  if target=="applications":
   loc=", ".join(str(v) for v in (x.get("location_marz"),x.get("location_city"),x.get("address")) if v)
   lines.append("#"+str(x.get("id"))+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("service_name") or "—")+" · "+str(x.get("price") if x.get("price") is not None else "—")+" ֏"+(" · "+loc if loc else ""))
  elif target=="partners":lines.append("#"+str(x.get("id"))+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("status") or "—")+" · verification="+str(x.get("verification_status") or "—"))
  elif target=="businesses":lines.append("#"+str(x.get("id"))+" · "+str(x.get("name") or "—")+" · "+str(x.get("status") or "—")+" · "+str(x.get("partner_business_name") or "—"))
  elif target=="master_categories":lines.append("#"+str(x.get("id"))+" · "+str(x.get("name_am") or x.get("name_ru") or x.get("name_en") or "—"))
  elif target=="services":
   prices=x.get("prices_amd") or []
   price_text=(" · գին="+", ".join(str(p)+" ֏" for p in prices if p is not None)) if prices else " · գին=—"
   lines.append("#"+str(x.get("id"))+" · "+str(x.get("name") or "—")+" · category="+str(x.get("category_id") or "—")+" · "+str(x.get("category_name_am") or x.get("category_name_ru") or x.get("category_name_en") or "—")+price_text+(" · "+str(x.get("partner_name")) if x.get("partner_name") else ""))
  else:lines.append("#"+str(x.get("id"))+" · "+str(x.get("name_am") or x.get("name_ru") or x.get("name_en") or "—")+" · "+str(x.get("master_name_am") or x.get("master_name_ru") or "—"))
 return "\n".join(lines)

async def _admin_execute(command):
    intent=str(command.get("intent") or ""); aid=command.get("application_id")
    try: aid=int(aid) if aid is not None else None
    except (TypeError,ValueError): aid=None
    if intent=="show_applications":
        rows=_admin_context(limit=30).get("applications",[])
        if not rows: return "📨 Заявок нет."
        return "📨 Заявки ("+str(len(rows))+"):\n"+"\n".join("#"+str(x["id"])+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("service_name") or "—")+" · "+str(x.get("price") if x.get("price") is not None else "—")+" ֏" for x in rows[:20])
    # Full application is handled by the canonical read-only branch below.
    if intent=="show_partner_count":
        row=platform_db.one("SELECT COUNT(*) AS count FROM partners")
        reply="🤝 Գործընկերների քանակը՝ "+str(int(row.get("count") or 0))+"։"
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
    if intent=="query_database":
        return await _admin_query_answer(str(command.get("question") or ""),command.get("target") or "applications",command.get("filters") or {},command.get("limit") or 20,command.get("sort"))
    if intent=="show_full_application":
        return _admin_full_application_text(aid)
    if intent=="show_partners":
        rows=_admin_context(limit=50).get("partners",[])
        return "🤝 Партнёров нет." if not rows else "🤝 Партнёры:\n"+"\n".join("#"+str(x["id"])+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("status") or "—") for x in rows[:30])
    if intent=="show_businesses":
        rows=_admin_context(limit=100).get("businesses",[])
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
        "💰 Գին: "+(val(a.get("price"))+" ֏" if a.get("price") is not None else "—"),
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
    if not app: return "Հայտը չի գտնվել։"
    docs=_admin_semantic_documents(aid)
    category=None
    if app.get("category_id"):
        category=platform_db.one("""SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.is_active
            FROM categories c WHERE c.id=%s""",(int(app["category_id"]),))
    truth=_admin_application_truth(app,docs,category)
    errors=[x for x in truth.get("checks",[]) if x.get("severity")=="error"]
    warnings=[x for x in truth.get("checks",[]) if x.get("severity")=="warning"]
    parts=[]
    if errors: parts.append("Ստուգված խնդիրներ՝ "+", ".join(str(x.get("message") or x.get("code")) for x in errors))
    else: parts.append("Ստուգված կոշտ սխալ չի հայտնաբերվել։")
    if warnings: parts.append("Լրացուցիչ ստուգման/տեղեկության կարիք կա՝ "+", ".join(str(x.get("message") or x.get("code")) for x in warnings))
    return " ".join(parts)

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
    if field=="documents":
        doc_id=app.get("document_id")
        if doc_id:
            return "📄 Փաստաթուղթ\n🆔 ID: "+str(doc_id)+"\n📌 Հայտի փաստաթղթի ID-ն առկա է։ Եթե պետք է, կարող եմ ստուգել դրա ընթացիկ հաստատման կարգավիճակը։"
        return "📄 Փաստաթուղթ\n⚠️ Հայտում փաստաթղթի ID նշված չէ։"
    return "Ուղղեք, թե հայտի որ դաշտն եք ուզում տեսնել."

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
    """Universal semantic planner: meaning first, Python validates and executes."""
    from groq import AsyncGroq
    key=os.getenv("GROQ_API_KEY","").strip()
    if not key: raise RuntimeError("GROQ_API_KEY is not configured")
    model=os.getenv("GROQ_MODEL","").strip() or "openai/gpt-oss-20b"
    client=AsyncGroq(api_key=key)
    system="""You are the universal semantic planner for the Armenia AI Guide administrator.
Understand what the administrator means, not predefined command phrases. Input can be Armenian,
Russian, English, mixed language, transliteration, typos, colloquial wording, elliptical follow-ups
or broad natural questions. Use the supplied conversation context. A short follow-up may refer to the immediately previous
database result by position, ID, field, name, or property (for example asking for "names" after a list
of IDs). Resolve that reference from last_shown_query_rows, last_query_target, last_result_kind and last_result_facts before choosing a
new target. If the previous result is an aggregate/catalog overview, preserve that subject for short
follow-ups unless the administrator clearly introduces a new subject. This is semantic context resolution, not a predefined command list.

Decide the goal, subject/entity, context reference, factual data needed, and whether the request is
read-only or a mutation. Python is the source of truth: it resolves IDs, permissions and executes
only whitelisted database operations. Never invent IDs or write SQL. Do not expose hidden
chain-of-thought; reasoning_summary is one short sentence.

Use generic intents when appropriate:
information_request, inspect_entity, query_database, show_applications, show_application_count,
show_application, show_application_field, show_documents, edit_application, approve_application,
reject_application, clarify_application, suggest_application_correction, show_partners,
show_businesses, show_partner_count, unknown.

For lists/searches/counts/filters use query_database. For ordinary factual questions use
information_request or inspect_entity.
For catalog questions, distinguish the catalog itself from services: master_categories means top-level directions, catalog means subcategories, and catalog_overview means aggregate catalog counts. When the administrator asks for all directions/categories or asks for the names after a catalog list, query the corresponding catalog target and use the complete current catalog. Never assume a fixed catalog size or use 20, 22, or 320 as a hard limit. For questions asking how many directions and subcategories exist, use catalog_overview so counts come from the database. For explicit mutations use the appropriate write intent.

Examples of meaning:
- "քանի գործընկեր ունենք", "сколько партнёров", "how many partners" => show_partner_count.
- "ունենք ծանր տեխնիկայի վարձույթ ենթաուղղություններում?", "есть ли ... в подкатегориях?" => query_database on catalog with a name contains search; do not use a services filter and do not invent a subcategory.
- If the administrator asks whether a phrase/category exists in the catalog, use catalog and filters.name with contains, preserving the user phrase.
- If a query says "subcategory_name" but the target is catalog, treat that as the catalog category name field, not an applications-only field.
- Count questions must return database counts, not a truncated list.
entity_type can be application, partner, business, service, catalog, document, order, booking,
or unknown. entity_id is only an ID explicitly present or safely supplied by context; otherwise
leave it null. entity_name is the natural name to search. data_needed is a concise list of factual
datasets/fields needed, such as application, partner, documents, services, categories, verification,
status, location, prices, orders, bookings. navigation describes first/next/previous/ordinal
navigation; Python resolves it. filters/sort/limit apply to database queries. action_required is
read_only unless a real mutation is explicitly requested. response_language follows the user.
confidence is an honest estimate.

You also have safe business-data tools. Prefer tool_requests for questions that require entity data
or checks. Choose only tools appropriate to the administrator role. Never invent tool names or SQL.
For a focused entity, Python resolves the entity and injects its ID where appropriate. You may request
several tools when the answer needs several independent facts. Available tools:
- search_partners: find partners by name/service/city/status
- get_partner: get one partner's allowed profile
- get_application: get one application
- get_documents: get verification documents/status
- get_addresses: get partner business objects/addresses
- get_directions: get active top-level directions
- search_catalog: search active categories/subcategories
- get_services: get services/prices/catalog links
- get_orders: get visible orders (may report schema pending)
- check_application: factual application completeness/status checks
- check_catalog_match: search catalog candidates for a service
- count: count partners/applications/services/directions/subcategories

Return ONLY JSON with:
reasoning_summary, intent, target, entity_type, entity_id, entity_name, data_needed, tool_requests,
field, value_raw, navigation, filters, sort, limit, action_required, response_language, confidence.
"""
    payload=json.dumps({"message":message,"context":ctx},ensure_ascii=False,default=str)
    messages=[{"role":"system","content":system},{"role":"user","content":payload}]
    try:
        try:
            resp=await client.chat.completions.create(model=model,messages=messages,temperature=0,
                max_tokens=420,response_format={"type":"json_object"})
        except Exception as json_mode_error:
            if "response_format" not in str(json_mode_error).lower() and "json_object" not in str(json_mode_error).lower():
                raise
            resp=await client.chat.completions.create(model=model,messages=messages,temperature=0,max_tokens=420)
    except Exception as first:
        if model!="openai/gpt-oss-20b" and ("404" in str(first) or "model" in str(first).lower()):
            resp=await client.chat.completions.create(model="openai/gpt-oss-20b",messages=messages,temperature=0,max_tokens=420)
        else: raise
    raw=(resp.choices[0].message.content or "").strip()
    try: data=json.loads(raw)
    except json.JSONDecodeError:
        start=raw.find("{"); end=raw.rfind("}")
        if start<0 or end<=start: raise RuntimeError("Groq returned invalid JSON")
        data=json.loads(raw[start:end+1])
    if not isinstance(data,dict): raise RuntimeError("Groq returned a non-object intent")
    return _admin_normalize_plan(data,message)

def _admin_fallback_intent(message,focused_id=None):
    text=_norm(message)
    has_application=bool(re.search(r"(հայտ|դիմում|заявк|request|application)",text,re.I|re.U))
    asks_count=bool(re.search(r"(քանի|сколько|how many|count|количеств)",text,re.I|re.U))
    asks_list=bool(re.search(r"(ինչ|որ|какие|какая|что|what|which|ցույց|show|list|ցուցակ)",text,re.I|re.U))
    asks_category=bool(re.search(r"(կատեգոր|ենթակատեգոր|category|subcategory|подкатегор)",text,re.I|re.U))
    asks_documents=bool(re.search(r"(փաստաթուղ|փաստաթուղթ|документ|документы|document|documents)",text,re.I|re.U))
    asks_services=bool(re.search(r"(ծառայ|услуг|service|services|uslugi)",text,re.I|re.U))
    asks_inspect=bool(re.search(r"(ստուգ|провер|check|ճիշտ|правильно|correct|ошибк|սխալ|верн)",text,re.I|re.U))
    if focused_id and asks_documents:
        return _admin_normalize_plan({"intent":"show_documents","target":"application","entity_id":focused_id,"field":"documents","reasoning_summary":"Փաստաթղթերի մասին հարց՝ ընթացիկ հայտի համատեքստում։","confidence":0.93},message)
    if focused_id and asks_category and asks_inspect:
        return _admin_normalize_plan({"intent":"inspect_application","target":"application","entity_id":focused_id,"field":"subcategory","reasoning_summary":"Ընթացիկ հայտի կատեգորիայի ճիշտ լինելը պետք է ստուգել՝ առանց փոփոխության։","confidence":0.93},message)
    if asks_count and re.search(r"(պառտն|գործընկեր|partner|партнер)",text,re.I|re.U):
        return _admin_normalize_plan({"intent":"show_partner_count","target":"partners","reasoning_summary":"Հարց գործընկերների ընդհանուր քանակի մասին։","confidence":0.94},message)
    if re.search(r"(կատալոգ|ենթաուղղ|ենթակատեգոր|подкатегор|subcategory|category)",text,re.I|re.U) and re.search(r"(ծանր\s+տեխնիկ|высок[а-яё]*\s+техник|тяж[а-яё]*\s+техник|heavy\s+equipment|equipment\s+rental|վարձույթ|аренд)",text,re.I|re.U):
        phrase=""
        m=re.search(r"(ծանր\s+տեխնիկ[այիա]?\s+վարձույթ|тяж[а-яё]*\s+техник[аиы]?\s+(?:в\s+аренду|аренды)|heavy\s+equipment\s+rental|equipment\s+rental)",message,re.I|re.U)
        phrase=(m.group(1) if m else "heavy equipment rental").strip()
        return _admin_normalize_plan({"intent":"query_database","target":"catalog","filters":{"name":{"contains":phrase}},"reasoning_summary":"Կատալոգում ենթաուղղության բնական լեզվով որոնում։","confidence":0.94},message)
    if asks_services:
        return _admin_normalize_plan({"intent":"query_database","target":"services","reasoning_summary":"Ծառայությունների ցանկի հարցում։","confidence":0.82},message)
    if has_application and asks_count:
        return _admin_normalize_plan({"intent":"show_application_count","target":"applications","reasoning_summary":"Вопрос о количестве заявок.","confidence":0.88},message)
    if has_application and asks_category and (asks_inspect or "ինչ" in text or "какая" in text):
        return _admin_normalize_plan({"intent":"show_application_field","target":"application","field":"subcategory","application_id":focused_id,"reasoning_summary":"Запрос о категории или подкатегории текущей заявки.","confidence":0.82},message)
    if has_application and asks_inspect and re.search(r"(ուղղ|исправ|fix|շտկ)",text,re.I|re.U):
        return _admin_normalize_plan({"intent":"suggest_application_correction","target":"application","application_id":focused_id,"reasoning_summary":"Нужно проверить заявку и предложить исправление.","confidence":0.86},message)
    if has_application and (asks_list or re.search(r"(կան|ունենք|есть|имеем|have)",text,re.I|re.U)):
        return _admin_normalize_plan({"intent":"show_applications","target":"applications","reasoning_summary":"Запрос о наличии или списке заявок.","confidence":0.84},message)
    if focused_id and re.search(r"(ստուգ|провер|check)",text,re.I|re.U):
        return _admin_normalize_plan({"intent":"inspect_application","target":"application","application_id":focused_id,"reasoning_summary":"Проверка текущей заявки без изменения данных.","confidence":0.8},message)
    if focused_id and re.search(r"(ուղղ|исправ|շտկ|fix)",text,re.I|re.U):
        return _admin_normalize_plan({"intent":"edit_application","target":"application","application_id":focused_id,"field":"subcategory","value_raw":None,"action_required":"suggest_alternatives","reasoning_summary":"Исправление текущего поля требует предложения вариантов.","confidence":0.72},message)
    m=re.search(r"(?:заявк[ауеи]?|հայտ(?:ը|ի)?|application)\s*#?\s*(\d+)",text)
    aid=int(m.group(1)) if m else focused_id
    if aid and re.search(r"(открой|բաց|open|покаж|ցույց)",text,re.I|re.U):
        return _admin_normalize_plan({"intent":"show_application","target":"application","application_id":aid,"reasoning_summary":"Запрошено открытие конкретной заявки.","confidence":0.86},message)
    return _admin_normalize_plan({"intent":"unknown","reasoning_summary":"Недостаточно уверенности для безопасного действия.","confidence":0.0},message)


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


def _admin_resolve_semantic_entity(plan,state):
    entity_type=_norm(plan.get("entity_type") or plan.get("target"))
    entity_name=str(plan.get("entity_name") or "").strip()
    entity_id=plan.get("entity_id")
    if entity_id not in (None,""):
        try: return entity_type,int(entity_id)
        except (TypeError,ValueError): pass
    focused_type=state.get("last_focused_entity_type")
    focused_id=state.get("last_focused_entity_id") or state.get("last_focused_application_id")
    if not entity_name and focused_id:
        return focused_type or "application",int(focused_id)
    if not entity_name: return None,None
    q="%"+entity_name+"%"; candidates=[]
    try:
        candidates += platform_db.rows("""SELECT id,business_name,'application' AS entity_type FROM partner_applications
            WHERE business_name ILIKE %s OR service_name ILIKE %s ORDER BY updated_at DESC NULLS LAST,id DESC LIMIT 10""",(q,q))
    except Exception: pass
    try:
        candidates += platform_db.rows("""SELECT id,business_name,'partner' AS entity_type FROM partners
            WHERE business_name ILIKE %s ORDER BY updated_at DESC NULLS LAST,id DESC LIMIT 10""",(q,))
    except Exception: pass
    try:
        candidates += platform_db.rows("""SELECT id,name,'business' AS entity_type FROM partner_businesses
            WHERE name ILIKE %s ORDER BY id DESC LIMIT 10""",(q,))
    except Exception: pass
    if not candidates: return None,None
    typed=[x for x in candidates if entity_type and _norm(x.get("entity_type"))==entity_type]
    pool=typed or candidates
    exact=[x for x in pool if _norm(x.get("business_name") or x.get("name"))==_norm(entity_name)]
    chosen=(exact or pool)[0]
    return str(chosen.get("entity_type") or entity_type or "unknown"),int(chosen["id"])


def _admin_semantic_documents(application_id):
    if not application_id: return []
    try:
        exists=platform_db.one("""SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_name='partner_verification_documents'""")
        if not exists: return []
        cols=platform_db.rows("""SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='partner_verification_documents'
            ORDER BY ordinal_position""")
        names={str(x.get("column_name")) for x in cols}
        app_col=next((x for x in ("application_id","partner_application_id","partner_id") if x in names),None)
        if not app_col: return []
        select_cols=[x for x in ("id","application_id","partner_application_id","partner_id","document_type",
            "file_name","file_url","status","verification_status","admin_note","rejection_reason",
            "created_at","updated_at") if x in names]
        if not select_cols: return []
        value=int(application_id)
        if app_col=="partner_id":
            app=platform_db.one("SELECT partner_id FROM partner_applications WHERE id=%s",(value,))
            if not app or app.get("partner_id") is None: return []
            value=app["partner_id"]
        return _admin_safe(platform_db.rows("SELECT "+",".join(select_cols)+" FROM partner_verification_documents WHERE "+app_col+"=%s ORDER BY id DESC LIMIT 50",(value,)))
    except Exception: return []


def _admin_service_category_audit(rows):
    """Fact-based service -> catalog category audit."""
    result=[]
    for row in rows or []:
        service_name=str(row.get("name") or "").strip()
        category_id=row.get("category_id")
        stored_category=str(row.get("category_name_am") or row.get("category_name_ru") or row.get("category_name_en") or "").strip()
        candidates=[]
        try:
            candidates=_admin_category_candidates(service_name,row.get("master_category_id"),limit=8) or []
        except Exception:
            candidates=[]
        candidate_rows=[]
        for item in candidates:
            x=item.get("row") if isinstance(item,dict) and "row" in item else item
            if isinstance(x,dict): candidate_rows.append(x)
        exact=[x for x in candidate_rows if str(x.get("id"))==str(category_id)]
        result.append({"service_id":row.get("id"),"service_name":service_name,"category_id":category_id,
            "category_name":stored_category,"direction":row.get("master_name_am") or row.get("master_name_ru") or row.get("master_name_en"),
            "catalog_candidates":candidate_rows[:8],
            "stored_category_is_top_candidate":bool(exact and candidate_rows and str(candidate_rows[0].get("id"))==str(category_id)),
            "verdict":"matched" if exact and candidate_rows and str(candidate_rows[0].get("id"))==str(category_id)
                     else ("review" if candidate_rows else "insufficient_data")})
    return result

def _admin_application_truth(app, documents=None, category=None):
    """Return only checks that are provable from current platform data.
    This layer deliberately does not treat missing phone/description as errors
    unless an explicit backend rule exists for them.
    """
    if not app:
        return {"checks": [], "currency": "AMD"}
    checks=[]
    status=str(app.get("status") or "").strip()
    if status:
        checks.append({"code":"status","severity":"info","value":status,
                       "message":"Հայտի ընթացիկ կարգավիճակը՝ "+status+"։"})
    else:
        checks.append({"code":"status_missing","severity":"warning","message":"Հայտի կարգավիճակը լրացված չէ։"})

    service=str(app.get("service_name") or "").strip()
    if service:
        checks.append({"code":"service_present","severity":"ok","value":service})
    else:
        checks.append({"code":"service_missing","severity":"warning","message":"Ծառայության անունը նշված չէ։"})

    price=app.get("price")
    if price in (None,""):
        checks.append({"code":"price_missing","severity":"warning","message":"Ծառայության գինը նշված չէ։"})
    else:
        try:
            numeric=float(price)
            checks.append({"code":"price_valid","severity":"ok","value":numeric,"currency":"AMD"})
            if numeric < 0:
                checks.append({"code":"price_negative","severity":"error","message":"Գինը բացասական է։"})
        except (TypeError,ValueError):
            checks.append({"code":"price_invalid","severity":"error","value":str(price),
                           "message":"Գնի արժեքը թվային չէ։"})

    if category:
        active=category.get("is_active")
        checks.append({"code":"category_exists","severity":"ok","value":category.get("id"),
                       "name_am":category.get("name_am"),"name_ru":category.get("name_ru"),
                       "name_en":category.get("name_en"),"is_active":active})
        if active is False:
            checks.append({"code":"category_inactive","severity":"error",
                           "message":"Ընտրված կատեգորիան ակտիվ չէ։"})
        if app.get("master_category_id") is not None and category.get("master_category_id") is not None:
            try:
                if int(app["master_category_id"]) != int(category["master_category_id"]):
                    checks.append({"code":"category_direction_mismatch","severity":"error",
                                   "message":"Կատեգորիան չի պատկանում հայտում նշված ուղղությանը։"})
                else:
                    checks.append({"code":"category_direction_match","severity":"ok"})
            except (TypeError,ValueError):
                pass

    docs=list(documents or [])
    if docs:
        statuses=[str(d.get("status") or d.get("verification_status") or "").strip().lower() for d in docs]
        approved=sum(1 for s in statuses if s=="approved")
        checks.append({"code":"documents_present","severity":"ok","count":len(docs),"approved":approved})
        if statuses and approved==len(statuses):
            checks.append({"code":"documents_all_approved","severity":"ok"})
        elif any(s in {"rejected","declined"} for s in statuses):
            checks.append({"code":"documents_rejected","severity":"error"})
        else:
            checks.append({"code":"documents_pending","severity":"warning"})
    else:
        checks.append({"code":"documents_missing","severity":"warning","message":"Կապված հաստատման փաստաթուղթ չի գտնվել։"})

    # Phone and description are factual fields, not approval errors here.
    for field in ("phone","description"):
        value=app.get(field)
        checks.append({"code":field+"_present" if str(value or "").strip() else field+"_missing",
                       "severity":"info","value":bool(str(value or "").strip())})

    return {"checks":checks,"currency":"AMD",
            "approval_rule_note":"Չլրացված phone/description դաշտերը ինքնին սխալ չեն համարվում, քանի դեռ backend-ում դրանց պարտադիր լինելու կանոն չկա։"}

def _admin_semantic_entity_data(entity_type,entity_id,data_needed,state):
    """Build factual context through the role-aware DataTools facade.
    Existing specialized checks remain available as a compatibility fallback.
    """
    result={}
    tools=DataTools("admin")
    if entity_type=="application" and entity_id:
        try:
            result["application"]=tools.execute("get_application",{"application_id":int(entity_id)}).get("data",{}).get("application")
            result["documents"]=tools.execute("get_documents",{"application_id":int(entity_id)}).get("data",{}).get("documents",[])
        except DataToolError:
            pass
        app=result.get("application") or _admin_hydrate_application(entity_id)
        if not app: return {}
        result["application"]=app
        needed=set(data_needed or [])
        if not needed: needed={"application","documents","partner","categories","services","verification"}
        if not result.get("documents"):
            result["documents"]=_admin_semantic_documents(entity_id)
        if app.get("partner_id") and ("partner" in needed or "verification" in needed):
            try:
                result["partner"]=tools.execute("get_partner",{"partner_id":int(app["partner_id"])}).get("data",{}).get("partner")
            except DataToolError:
                result["partner"]=_admin_safe(platform_db.one("""SELECT id,user_id,status,verification_status,
                    business_name,business_description,contact_share_policy,created_at,updated_at
                    FROM partners WHERE id=%s""",(int(app["partner_id"]),)))
            except Exception: result["partner"]=None
        if app.get("category_id") and ("categories" in needed or "category" in needed or "subcategory" in needed):
            try:
                result["category"]=_admin_safe(platform_db.one("""SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.is_active,
                    m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en
                    FROM categories c JOIN master_categories m ON m.id=c.master_category_id WHERE c.id=%s""",(int(app["category_id"]),)))
            except Exception: result["category"]=None
            try:
                result["catalog_candidates"]=_admin_safe([x["row"] for x in _admin_category_candidates(
                    str(app.get("service_name") or ""),app.get("master_category_id"),limit=8)])
            except Exception: result["catalog_candidates"]=[]
        result["truth"]=_admin_application_truth(app,result.get("documents"),result.get("category"))
        if app.get("category_id") and ("services" in needed or "service" in needed):
            try:
                result["services"]=tools.execute("get_services",{"category_id":int(app["category_id"])}).get("data",{}).get("items",[])
            except DataToolError:
                try:
                    result["services"]=_admin_safe(platform_db.rows(
                        "SELECT id,name,category_id,created_at FROM services WHERE category_id=%s ORDER BY id DESC LIMIT 50",
                        (int(app["category_id"]),)))
                except Exception: result["services"]=[]
        return result
    if entity_type=="partner" and entity_id:
        try:
            result["partner"]=_admin_safe(platform_db.one("""SELECT id,user_id,status,verification_status,business_name,
                business_description,contact_share_policy,created_at,updated_at FROM partners WHERE id=%s""",(int(entity_id),)))
            result["businesses"]=_admin_safe(platform_db.rows(
                "SELECT id,partner_id,name,description,phone,status,created_at FROM partner_businesses WHERE partner_id=%s AND status<>'archived' ORDER BY id DESC LIMIT 50",
                (int(entity_id),)))
        except Exception: pass
        return result
    if entity_type=="business" and entity_id:
        try:
            result["business"]=_admin_safe(platform_db.one("""SELECT b.id,b.partner_id,b.name,b.description,b.phone,b.status,
                b.created_at,p.business_name AS partner_name,p.verification_status FROM partner_businesses b
                JOIN partners p ON p.id=b.partner_id WHERE b.id=%s""",(int(entity_id),)))
        except Exception: pass
        return result
    return result


def _admin_tool_context(plan, entity_type, entity_id):
    """Execute only model-selected, role-allowed data tools and return factual results."""
    requests=plan.get("tool_requests") or []
    if not requests:
        return {}
    tools=DataTools("admin")
    results=[]
    for req in requests:
        name=str(req.get("name") or "").strip()
        args=dict(req.get("arguments") or {})
        # Python owns identity resolution; the model never supplies raw SQL or permissions.
        if entity_id:
            if entity_type=="application" and name in {"get_application","get_documents","check_application"}:
                args.setdefault("application_id",int(entity_id))
            elif entity_type=="partner" and name in {"get_partner","get_documents","get_addresses","get_services"}:
                args.setdefault("partner_id",int(entity_id))
            elif entity_type=="business" and name=="get_addresses":
                args.setdefault("business_id",int(entity_id))
        try:
            results.append(tools.execute(name,args))
        except DataToolError as exc:
            results.append({"tool":name,"error":str(exc)})
        except Exception as exc:
            results.append({"tool":name,"error":"tool_execution_failed","detail":str(exc)[:180]})
    return {"tool_results":results}


async def _admin_refine_tool_context(question, plan, entity_type, entity_id, facts):
    """Second semantic planning pass after real data arrives."""
    key=os.getenv("GROQ_API_KEY","").strip()
    if not key:
        return facts
    try:
        from groq import AsyncGroq
        client=AsyncGroq(api_key=key)
        model=os.getenv("GROQ_MODEL","").strip() or "openai/gpt-oss-20b"
        available=DataTools("admin").available_tools()
        payload=json.dumps({
            "question":question,"entity_type":entity_type,"entity_id":entity_id,
            "plan":plan,"facts":facts,"available_tools":available
        },ensure_ascii=False,default=str)
        resp=await client.chat.completions.create(
            model=model,
            messages=[
                {"role":"system","content":"You are the second planning pass for the Armenia AI Guide admin assistant. You already received real database results. Decide whether they are sufficient. If not, request only additional safe business-data tools needed to complete the answer. Never request SQL, never invent tools, never invent IDs. Prefer the smallest number of additional calls. Return ONLY JSON with tool_requests and done."},
                {"role":"user","content":payload}
            ],
            temperature=0,max_tokens=500
        )
        raw=(resp.choices[0].message.content or "").strip()
        data=json.loads(raw)
        requests=data.get("tool_requests") if isinstance(data,dict) else []
        if not isinstance(requests,list) or not requests:
            return facts
        extra=_admin_tool_context({"tool_requests":requests[:4]},entity_type,entity_id)
        if not extra:
            return facts
        merged=dict(facts) if isinstance(facts,dict) else {"initial":facts}
        merged["additional_tool_results"]=extra.get("tool_results",[])
        return merged
    except Exception:
        return facts


async def _admin_semantic_answer(question,plan,state):
    entity_type,entity_id=_admin_resolve_semantic_entity(plan,state)
    # A follow-up can naturally refer to the immediately previous query result.
    previous_rows=state.get("last_shown_query_rows") or []
    previous_target=state.get("last_query_target")
    if str(plan.get("intent") or "")=="query_database" and not plan.get("target") and previous_target:
        plan["target"]=previous_target
    if entity_id:
        state["last_focused_entity_type"]=entity_type
        state["last_focused_entity_id"]=entity_id
        if entity_type=="application": state["last_focused_application_id"]=entity_id
    needed=plan.get("data_needed") or []
    target=_admin_query_target(plan.get("target"))
    facts=_admin_tool_context(plan,entity_type,entity_id)
    if facts:
        facts=await _admin_refine_tool_context(question,plan,entity_type,entity_id,facts)
    if not facts and entity_id:
        facts=_admin_semantic_entity_data(entity_type,entity_id,needed,state)
    if not facts and target:
        # If the user is asking a follow-up about the previous result, reuse those exact
        # rows instead of issuing a broader unrelated query.
        followup_rows=previous_rows if previous_target==target and previous_rows else None
        if followup_rows is not None:
            rows=followup_rows
            error=None
        else:
            rows,error=_admin_query_rows(target,plan.get("filters") or {},plan.get("limit") or 20,plan.get("sort"))
        facts={"target":target,"rows":rows or []}
        if error: facts={"error":error}
    if not facts and state.get("last_result_facts"):
        facts=_admin_safe(state.get("last_result_facts"))
        if isinstance(facts,dict):
            facts.setdefault("target",previous_target or target or "query_result")
        state["last_query_target"]=previous_target
        state["last_query"]=question[:500]
    if not facts and previous_rows:
        facts={"target":previous_target or target or "query_result","rows":previous_rows}
        state["last_query_target"]=previous_target
        state["last_shown_query_rows"]=_admin_safe(previous_rows[:20])
        state["last_query"]=question[:500]
    if not facts:
        return _admin_localized(plan.get("response_language","ru"),"unknown")

    # Persist the factual result that generated this answer. It becomes semantic context
    # for the next natural-language follow-up, including aggregate results with no row list.
    if isinstance(facts,dict):
        state["last_query_target"]=target or plan.get("target")
        state["last_query"]=question[:500]
        if isinstance(facts.get("rows"),list):
            state["last_shown_query_rows"]=_admin_safe(facts.get("rows")[:20])
            state["last_result_kind"]="rows"
            state["last_result_facts"]=None
        else:
            state["last_result_kind"]=target or plan.get("target") or "facts"
            state["last_result_facts"]=_admin_safe(facts)
    fallback=json.dumps(facts,ensure_ascii=False,default=str)
    if isinstance(facts,dict) and "rows" in facts:
        if (target or entity_type)=="services":
            facts["category_audit"]=_admin_service_category_audit(facts.get("rows") or [])
        fallback=_admin_query_result_text(target or entity_type,facts.get("rows") or [],plan.get("filters") or {},question)
    key=os.getenv("GROQ_API_KEY","").strip()
    if not key: return fallback
    try:
        from groq import AsyncGroq
        client=AsyncGroq(api_key=key); model=os.getenv("GROQ_MODEL","").strip() or "openai/gpt-oss-20b"
        payload=json.dumps({"question":question,"goal":plan.get("intent"),"entity_type":entity_type,
            "entity_id":entity_id,"data_needed":needed,"facts":facts},ensure_ascii=False,default=str)
        resp=await client.chat.completions.create(model=model,messages=[
            {"role":"system","content":"""You are the final answer layer for the Armenia AI Guide administrator.
Answer naturally, directly and humanly in the same language as the question. The question may
be a short follow-up to the previous result. In that case, answer from the supplied rows and identify
the requested property (such as names, IDs, categories, prices, statuses) from those rows. If the user
gives a numeric ID that appears in the previous rows, resolve it against those rows; do not ask the
user to restate the request.
Use ONLY the supplied database facts and the supplied truth/check results. Service prices are supplied on the actual partner-specific `services` rows as `prices_amd` / `price_amd`; these are stored prices in AMD (֏). Do not say that prices are unavailable when a value is present. For catalog overview facts, `master_categories_count` is the real number of active directions and `subcategories_count` is the real number of active subcategories; preserve that subject for short follow-ups. The truth/check results are authoritative for
whether something is actually wrong. Do NOT turn an empty field into an error, mandatory field, or
approval problem unless the supplied facts explicitly prove that rule. Do not invent business rules,
approval consequences, currency, prices, categories, or document status.
All application/service prices in these facts are in AMD (֏) unless the facts explicitly state another
currency. Never call an AMD amount dollars, euros, or another currency.
For a "show/open" request, prefer a compact human-readable summary with the important fields; do not
dump a Markdown table or raw database structure. If the user asks whether something is "normal", first
state the factual status, then verified problems, then missing information that is merely informational. If `category_audit` is supplied, use its verdicts: `matched` means no verified category mismatch in the supplied catalog evidence, `review` means a possible mismatch that needs review, and `insufficient_data` means the system cannot determine it. Do not replace an audit question with a generic service list.
If no verified error is present, say that clearly. Do not mention AI, prompts, SQL, internal tools or
chain-of-thought. Simple question = simple answer; broad inspection = compact structured summary."""},
            {"role":"user","content":payload}],temperature=0,max_tokens=700)
        answer=(resp.choices[0].message.content or "").strip()
        return answer or fallback
    except Exception: return fallback

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

    # A bare affirmative/acknowledgement is not an approval command.
    # Only an existing pending_action may consume confirmation. This prevents
    # phrases like "այո ճիշտ է" / "да, правильно" from becoming mutations.
    if not pending and re.fullmatch(
        r"(?:да|да,?\s*(?:правильно|верно|ок)|yes|yes,?\s*(?:correct|right|ok)|"
        r"այո|այո,?\s*(?:ճիշտ|լավ|հաստատ)|հա|հա,?\s*(?:ճիշտ|լավ))",
        normalized, re.IGNORECASE|re.UNICODE):
        reply="Հասկացա։ Տվյալները չեմ փոխել։" if _admin_detect_language(message)=="am" else "Понял. Данные не изменял."
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply)
        return reply

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
    # Universal conversational resolver. Identity/navigation is deterministic;
    # semantic intent can still be delegated to Groq afterwards.
    focused_id=state.get("last_focused_application_id")
    current_list=state.get("current_list") or []
    current_pos=state.get("current_position")
    current_type=state.get("last_focused_entity_type")
    current_id=state.get("last_focused_entity_id")
    nav_intent=None
    if current_list:
        ordinal_map=[
            (r"(?:\b(?:первая|первую|первый|первое|1-я|1ю)\b|\b(?:առաջին|առաջինը|առաջինին)\b)",0),
            (r"(?:\b(?:вторая|вторую|второй|второе|2-я|2ю)\b|\b(?:երկրորդ|երկրորդը|երկրորդին)\b)",1),
            (r"(?:\b(?:третья|третью|третий|третье|3-я|3ю)\b|\b(?:երրորդ|երրորդը|երրորդին)\b)",2)
        ]
        for pattern,idx in ordinal_map:
            if re.search(pattern,local_text,re.I|re.U) and idx < len(current_list):
                item=current_list[idx]
                state["current_position"]=idx
                state["last_focused_entity_type"]=item.get("type")
                state["last_focused_entity_id"]=int(item["id"])
                if item.get("type")=="application":
                    focused_id=int(item["id"])
                    state["last_focused_application_id"]=focused_id
                nav_intent={"intent":"show_application","target":"application","application_id":int(item["id"]),
                            "action_required":"read_only","confidence":1.0}
                break
    if current_list and re.search(r"(?:\b(?:следующая|следующую|следующий|следующее|дальше|next)\b|\b(?:հաջորդը|հաջորդ)\b)",local_text,re.I|re.U):
        pos=int(current_pos) if isinstance(current_pos,int) else -1
        next_pos=pos+1
        if next_pos >= len(current_list):
            return "Это последний элемент в текущем списке."
        item=current_list[next_pos]
        state["current_position"]=next_pos
        state["last_focused_entity_type"]=item.get("type")
        state["last_focused_entity_id"]=int(item["id"])
        if item.get("type")=="application":
            focused_id=int(item["id"])
            state["last_focused_application_id"]=focused_id
        nav_intent={"intent":"show_application","target":"application","application_id":int(item["id"]),
                    "action_required":"read_only","confidence":1.0}
    if current_id and re.search(r"(?:\b(?:этот|эта|эту|его|ему|этого|этой)\b|\b(?:այս|սա|նրան|նրա)\b)",local_text,re.I|re.U):
        if current_type=="application":
            focused_id=int(current_id)
            state["last_focused_application_id"]=focused_id
    if focused_id and re.search(r"(?:покаж|открой|show|open|ցույց|բաց).*(?:полн|целик|всю|ամբողջ|լիարժեք|complete|full)",local_text,re.I|re.U):
        nav_intent={"intent":"show_full_application","target":"application","application_id":int(focused_id),
                    "action_required":"read_only","confidence":1.0}

    full_app_match=bool(re.search(
        r"(?:ամբողջական|ամբողջությամբ|ամբողջ|լիարժեք|ուղղված|полностью|полную|полное|всю|исправленную|целиком|full|complete).*(?:հայտ|заявк|application)|(?:հայտ|заявк|application).*(?:ամբողջական|ամբողջությամբ|ամբողջ|լիարժեք|ուղղված|полностью|полную|полное|всю|исправленную|целиком|full|complete)",
        local_text,re.IGNORECASE
    ))
    if nav_intent:
        c=nav_intent
    elif full_app_match and re.search(r"(?:ցույց|покаж|открой|show|open)",local_text,re.IGNORECASE):
        id_match=re.search(r"(?:#|№)\s*(\d+)",local_text)
        requested_aid=int(id_match.group(1)) if id_match else focused_id
        if not requested_aid and len(state.get("last_shown_applications") or [])==1:
            try:
                requested_aid=int(state["last_shown_applications"][0]["id"])
            except Exception:
                requested_aid=None
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
    c=_admin_normalize_plan(c,message)


    state["last_action"]={"intent":c.get("intent"),"target":c.get("target"),"reasoning_summary":c.get("reasoning_summary"),"confidence":c.get("confidence")}
    state["last_action_failed"]=False; state["last_error"]=None; state["last_error_context"]=None

    intent=str(c.get("intent") or "unknown").lower()

    # Natural-language inspection shortcut: when an application is already focused,
    # category correctness is a factual inspection request, never a mutation.
    category_question=bool(re.search(
        r"(?:категор|подкатегор|category|subcategory|կատեգոր|ենթակատեգոր|ենթաուղղ).*(?:правиль|верн|correct|ճիշտ|սխալ)|"
        r"(?:правиль|верн|correct|ճիշտ|սխալ).*(?:категор|подкатегор|category|subcategory|կատեգոր|ենթակատեգոր|ենթաուղղ)",
        message, re.IGNORECASE|re.UNICODE))
    if focused_id and category_question and "#" not in message and "№" not in message:
        c["intent"]="information_request"
        c["target"]="application"
        c["entity_type"]="application"
        c["entity_id"]=int(focused_id)
        c["data_needed"]=["application","categories","services","verification"]
        c["tool_requests"]=[
            {"name":"get_application","arguments":{"application_id":int(focused_id)}},
            {"name":"check_application","arguments":{"application_id":int(focused_id)}},
            {"name":"check_catalog_match","arguments":{"service_name":str((_admin_hydrate_application(focused_id) or {}).get("service_name") or "")}}
        ]
        c=_admin_normalize_plan(c,message)
        intent="information_request"

    target=str(c.get("target") or "").lower()
    aid=c.get("entity_id") or c.get("application_id") or focused_id
    # The semantic layer owns meaning; Python only resolves identity/navigation and validates execution.
    navigation=c.get("navigation")
    if isinstance(navigation,dict):
        nav_type=_norm(navigation.get("type"))
        if nav_type in {"first","1","առաջին","առաջինը"} and current_list:
            state["current_position"]=0
            aid=int(current_list[0].get("id")) if current_list[0].get("id") else aid
        elif nav_type in {"next","հաջորդ","հաջորդը"} and current_list:
            pos=int(state.get("current_position") if isinstance(state.get("current_position"),int) else -1)+1
            if pos < len(current_list):
                state["current_position"]=pos
                aid=int(current_list[pos].get("id")) if current_list[pos].get("id") else aid
    field=str(c.get("field") or "").lower()
    intent={"open_application":"show_application","count_applications":"show_application_count","count":"show_application_count","inspect":"inspect_application","documents":"show_documents","show_document":"show_documents","show_documents":"show_documents"}.get(intent,intent)

    if intent in {"information_request","inspect_entity","semantic_query","research_entity"}:
        reply=await _admin_semantic_answer(message,c,state)
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply
    if field in {"category","subcategory","price","service_name","location_city","description","documents"}: state["last_focused_field"]=field
    # Generic reference resolution: once an entity is focused, pronouns inherit that identity.
    # The AI receives the focused entity in context; this fallback only protects short ambiguous turns.
    if not aid and state.get("last_focused_entity_type")=="application":
        aid=state.get("last_focused_entity_id") or state.get("last_focused_application_id")
    elif not field or field=="none": field=state.get("last_focused_field") or ""
    if aid:
        try:
            aid=int(aid)
            state["last_focused_application_id"]=aid
            state["last_focused_entity_type"]="application"
            state["last_focused_entity_id"]=aid
        except (TypeError,ValueError): aid=None

    is_question=("?" in message or "՞" in message or bool(re.search(r"\b(как|какая|какие|какое|почему|зачем|что|где|сколько|what|which|how|why|where|how many|ինչ|ինչպես|որ|որտեղ|արդյոք|քանի)\b",message.casefold())))
    if is_question and intent in {"edit_application","approve_application","reject_application","clarify_application"}: intent="show_application_field" if field else "inspect_application"

    if intent=="query_database":
        qtarget=_admin_query_target(target or c.get("target"))
        qfilters=c.get("filters") or {}
        if qtarget=="catalog" and not qfilters and c.get("entity_name"):
            qfilters={"name":{"contains":c.get("entity_name")}}
        if not qtarget:
            reply=_admin_localized(c.get("response_language","ru"),"unknown")
        else:
            qlimit=c.get("limit") or 20; qsort=c.get("sort")
            rows,error=_admin_query_rows(qtarget,qfilters,qlimit,qsort)
            if error: reply="⚠️ "+error
            else:
                state["last_query"]={"target":qtarget,"filters":_admin_safe(qfilters),"sort":_admin_safe(qsort),"limit":int(qlimit or 20)}
                state["last_shown_query_rows"]=[_admin_safe(x) for x in (rows or [])]
                state["last_result_kind"]=qtarget
                state["last_result_facts"]=_admin_safe({"target":qtarget,"rows":rows or []})
                reply=await _admin_query_answer(message,qtarget,qfilters,qlimit,qsort)
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    if intent=="show_application_count":
        rows=_admin_context(limit=30).get("applications",[])
        if len(rows)==1:
            try: state["last_focused_application_id"]=int(rows[0]["id"])
            except Exception: pass
        reply=await _admin_execute({"intent":"show_application_count"})
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    if intent=="show_applications":
        try: rows=_admin_context(limit=30).get("applications",[])
        except Exception: rows=[]
        state["last_shown_applications"]=[{"id":int(x["id"]),"business_name":x.get("business_name"),"service_name":x.get("service_name")} for x in rows[:20]]
        state["current_list"]=[{"type":"application","id":int(x["id"]),"business_name":x.get("business_name")} for x in rows[:20]]
        state["current_position"]=0 if rows else None
        if rows:
            state["last_focused_application_id"]=int(rows[0]["id"])
            state["last_focused_entity_type"]="application"
            state["last_focused_entity_id"]=int(rows[0]["id"])
        reply=await _admin_execute({"intent":"show_applications"})
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    if intent in {"show_application","inspect_application","show_application_field","show_full_application","show_documents"}:
        if not aid: return _admin_localized(c.get("response_language","ru"),"need_application")
        if not _admin_hydrate_application(aid): return _admin_localized(c.get("response_language","ru"),"not_found",id=aid)
        state["last_focused_application_id"]=int(aid)
        if intent=="show_full_application":
            reply=_admin_full_application_text(int(aid))
        elif intent=="show_documents":
            reply=_application_field_answer(int(aid),"documents")
        else:
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
    except Exception as exc:
        try:
            state=_admin_session(int(request.app.get("stage3_admin_id") or 0))
            state["pending_action"]=None; state["waiting_for_input"]=None
            state["last_action_failed"]=True; state["last_error"]=str(exc)[:1000]
            state["last_error_context"]=message[:1000]; state["retry_count"]=int(state.get("retry_count",0) or 0)+1
        except Exception: pass
        import logging
        logging.getLogger(__name__).exception("admin_ai_turn_failed")
        return web.json_response({"ok":True,"reply":_admin_localized(_admin_detect_language(message),"safe_error")})



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

