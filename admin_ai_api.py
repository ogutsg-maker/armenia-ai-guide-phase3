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
from ai_context_builder import build_ai_context
import ai_cost_center


def _norm(text):
    if text is None:
        return ""
    return " ".join(str(text).casefold().strip().split())



_ADMIN_LOCALES={"am":{"unknown":"Ես ամբողջությամբ չհասկացա հարցումը։ Կարող եք հարցնել բնական լեզվով՝ հայտերի, գործընկերների, ընկերությունների կամ կատալոգի մասին։","need_application":"Սկզբում բացեք հայտը կամ նշեք դրա համարը։","not_found":"Հայտ #{id} չի գտնվել։","last_item":"Սա ընթացիկ ցուցակի վերջին տարրն է։","safe_error":"Չհաջողվեց անվտանգ մշակել հարցումը։ Տվյալները չեն փոխվել։ Փորձեք կրկին։", "ai_unavailable":"⚠️ AI ծառայությունը ժամանակավորապես հասանելի չէ։ Groq-ը չի սպասարկում հարցումը, իսկ պահուստային AI ծառայություններն էլ հասանելի չեն։ Տվյալները չեն փոխվել։"},"ru":{"unknown":"Я не полностью понял запрос. Можно спрашивать обычным языком о заявках, партнёрах, компаниях или каталоге.","need_application":"Сначала откройте заявку или укажите её номер.","not_found":"Заявка #{id} не найдена.","last_item":"Это последний элемент в текущем списке.","safe_error":"Не удалось безопасно обработать запрос. Данные не изменены. Повторите запрос."},"en":{"unknown":"I didn't fully understand the request. You can ask naturally about applications, partners, businesses, or the catalog.","need_application":"Open an application first or specify its number.","not_found":"Application #{id} was not found.","last_item":"This is the last item in the current list.","safe_error":"I couldn't safely process the request. No data was changed. Please try again."}}

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

def _admin_tool_registry(role="admin"):
    """Compact machine-readable registry supplied to the planner."""
    tools=DataTools(role)
    return tools.available_tools()


def _admin_tool_schemas(role="admin"):
    """Planner-facing argument contract; business validation remains in DataTools."""
    common={
        "query":{"type":"string"},
        "city":{"type":"string"},
        "marz":{"type":"string"},
        "limit":{"type":"integer","minimum":1,"maximum":200},
    }
    return {
        "count":{"entity":{"type":"string","enum":["partners","applications","services","directions","subcategories","companies","addresses","documents"]}},
        "search_partners":common,
        "search_applications":{**common,"status":{"type":"string"}},
        "search_companies":common,
        "get_partner":{"partner_id":{"type":"integer"}},
        "get_company":{"company_id":{"type":"integer"}},
        "get_application":{"application_id":{"type":"integer"}},
        "get_application_full":{"application_id":{"type":"integer"}},
        "get_documents":{"partner_id":{"type":"integer"},"application_id":{"type":"integer"}},
        "get_addresses":common,
        "get_services":{**common,"partner_id":{"type":"integer"},"company_id":{"type":"integer"}},
        "get_orders":{**common,"partner_id":{"type":"integer"},"status":{"type":"string"}},
        "search_catalog":{**common,"master_category_id":{"type":"integer"}},
        "get_directions":{"limit":{"type":"integer","minimum":1,"maximum":200}},
        "catalog_overview":{},
        "ai_usage_summary":{"days":{"type":"integer","minimum":1,"maximum":365}},
        "check_application":{"application_id":{"type":"integer"}},
        "check_catalog_match":{"service_name":{"type":"string"},"category_id":{"type":"integer"}},
    }


def _admin_normalize_plan(data,message=""):
    """Normalize the single semantic ActionPlan contract used by the admin AI."""
    if not isinstance(data,dict): data={}
    registry={x["name"] for x in _admin_tool_registry("admin")}
    schemas=_admin_tool_schemas("admin")
    raw_tools=data.get("tool_requests")
    if not isinstance(raw_tools,list): raw_tools=[]
    normalized=[]
    for item in raw_tools[:6]:
        if not isinstance(item,dict): continue
        name=str(item.get("name") or "").strip()
        args=item.get("arguments") if isinstance(item.get("arguments"),dict) else {}
        if name not in registry: continue
        normalized.append({"name":name,"arguments":args})
    data["tool_requests"]=normalized
    data["tool_registry_valid"]=len(normalized)==len(raw_tools[:6])
    data["navigation"]=data.get("navigation")
    lang=_admin_detect_language(message)
    data["response_language"]=str(data.get("response_language") or lang).lower()
    if data["response_language"] not in {"am","ru","en"}:
        data["response_language"]=lang
    try:
        confidence=float(data.get("confidence",0.0) or 0.0)
    except (TypeError,ValueError):
        confidence=0.0
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
    if isinstance(data.get("active_context"),dict):
        ac=data["active_context"]
        ac_id=ac.get("entity_id")
        try: ac_id=int(ac_id) if ac_id not in (None,"") else None
        except (TypeError,ValueError): ac_id=None
        data["active_context"]={"scope":str(ac.get("scope") or "")[:80],"subject":str(ac.get("subject") or "")[:200],"intent":str(ac.get("intent") or "")[:80],"query":str(ac.get("query") or "")[:500],"filters":ac.get("filters") if isinstance(ac.get("filters"),dict) else {},"entity_type":str(ac.get("entity_type") or "")[:40],"entity_id":ac_id}
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

def _admin_safe_human_fallback(facts, question, plan, target="", entity_type=""):
    if isinstance(facts, dict) and isinstance(facts.get("tool_results"), list):
        parts=[]
        for item in facts.get("tool_results") or []:
            if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
                continue
            part=_admin_safe_human_fallback(item["data"], question, plan, target, entity_type)
            if part and part not in parts:
                parts.append(part)
        if parts:
            return "\n\n".join(parts)
    # Prefer the exact requested fact family before the generic application summary.
    # This keeps natural follow-ups focused even when a provider returns broad facts.
    requested=set(str(x).casefold() for x in (plan.get("data_needed") or []))
    if isinstance(facts,dict):
        if "documents" in requested and isinstance(facts.get("documents"),list):
            docs=facts.get("documents") or []
            if not docs: return "📄 Այս հայտի համար կապված փաստաթուղթ չի գտնվել։"
            lines=["📄 Փաստաթղթեր՝ "+str(len(docs))]
            for d in docs[:20]:
                if not isinstance(d,dict): continue
                did=d.get("id") or d.get("document_id") or "—"
                dtype=d.get("document_type") or d.get("type") or "Փաստաթուղթ"
                status=d.get("status") or d.get("verification_status") or "—"
                lines.append("• #"+str(did)+" · "+str(dtype)+" · "+str(status))
            return "\n".join(lines)
        if ("services" in requested or "application_services" in requested) and isinstance(facts.get("application_services"),list):
            services=facts.get("application_services") or []
            if not services: return "🛠 Այս հայտում ծառայություններ չեն նշված։"
            lines=["🛠 Ծառայություններ՝ "+str(len(services))]
            for n,s in enumerate(services[:30],1):
                if not isinstance(s,dict): continue
                name=s.get("name") or s.get("service_name") or "—"
                price=s.get("price")
                lines.append(str(n)+". "+str(name)+(" · "+str(price)+" ֏" if price not in (None,"") else ""))
            return "\n".join(lines)
        if {"category","subcategory","catalog"} & requested:
            audit=facts.get("category_audit")
            if isinstance(audit,list) and audit:
                item=audit[0]; verdict=item.get("verdict"); stored=item.get("category_name") or "—"; direction=item.get("direction") or "—"
                if verdict=="matched": return "📂 Կատեգորիան համապատասխանում է ակտիվ կատալոգին։\n🧭 "+str(direction)+"\n🏷 "+str(stored)
                if verdict=="review": return "📂 Կատեգորիան պահանջում է լրացուցիչ ստուգում։\n🧭 "+str(direction)+"\n🏷 "+str(stored)
                return "📂 Կատեգորիայի համապատասխանությունը հաստատելու համար բավարար տվյալ չկա։\n🏷 "+str(stored)
            cat=facts.get("category")
            if isinstance(cat,dict): return "📂 "+str(cat.get("name_am") or cat.get("name_ru") or cat.get("name_en") or "—")
    if isinstance(facts, dict) and isinstance(facts.get("application"), dict):
        app=facts["application"]; lines=[
            "📨 Հայտ #"+str(app.get("id") or "—"),
            "🏢 "+str(app.get("business_name") or app.get("partner_business_name") or "—"),
            "📌 Կարգավիճակ՝ "+str(app.get("status") or "—")
        ]
        if app.get("service_name"):
            p=app.get("price")
            ps=("{:,} ֏".format(int(float(p))) if isinstance(p,(int,float)) and float(p).is_integer() else (str(p)+" ֏" if p not in (None,"") else "—"))
            lines.append("🛠 Ծառայություն՝ "+str(app.get("service_name"))+(" · "+ps if ps!="—" else ""))
        if app.get("direction_name"): lines.append("🧭 Ուղղություն՝ "+str(app.get("direction_name")))
        if app.get("subcategory_name"): lines.append("📂 Ենթակատեգորիա՝ "+str(app.get("subcategory_name")))
        cat=facts.get("category")
        if isinstance(cat,dict): lines.append("🔎 Կատալոգ՝ "+str(cat.get("name_am") or cat.get("name_ru") or cat.get("name_en") or "—")+(" · ակտիվ" if cat.get("is_active") else " · ոչ ակտիվ"))
        docs=facts.get("documents")
        if isinstance(docs,list):
            ok=sum(1 for d in docs if str((d or {}).get("status") or (d or {}).get("verification_status") or "").casefold() in {"approved","verified","accepted"})
            lines.append("📄 Փաստաթղթեր՝ "+str(len(docs))+(" · հաստատված՝ "+str(ok) if docs else ""))
        truth=facts.get("truth")
        if isinstance(truth,dict):
            errs=truth.get("errors") or truth.get("error") or []
            lines.append("⚠️ Ստուգում՝ կան խնդիրներ" if errs else "✅ Ստուգում՝ հաստատված սխալներ չեն հայտնաբերվել")
        return "\n".join(lines)
    if isinstance(facts,dict) and facts.get("master_categories_count") is not None:
        if facts.get("subcategories_count") is not None:
            return "📚 Կատեգորիաների քանակը՝ "+str(facts["master_categories_count"])+" · ենթակատեգորիաների քանակը՝ "+str(facts["subcategories_count"])+"։"
        return "📚 Կատեգորիաների քանակը՝ "+str(facts["master_categories_count"])+"։"
    if isinstance(facts,dict) and facts.get("subcategories_count") is not None:
        return "📚 Ենթակատեգորիաների քանակը՝ "+str(facts["subcategories_count"])+"։"
    if isinstance(facts,dict) and "rows" in facts:
        return _admin_query_result_text(target or entity_type or "query_result",facts.get("rows") or [],plan.get("filters") or {},question)
    if isinstance(facts,dict) and isinstance(facts.get("candidates"), list):
        cands=facts.get("candidates") or []
        if cands:
            top=cands[0]
            name=top.get("name_am") or top.get("name_ru") or top.get("name_en") or "—"
            reason=top.get("match_reason")
            return "🔎 Կատալոգի համապատասխանություն՝ «"+str(name)+"»"+(" · օբյեկտի համընկնում" if reason=="object_match" else "")+"։"

    if isinstance(facts,dict) and facts.get("entity") and facts.get("count") is not None:
        labels={"directions":"կատեգորիա","subcategories":"ենթակատեգորիա","partners":"գործընկեր","applications":"հայտ","services":"ծառայություն"}
        return "📊 "+labels.get(str(facts.get("entity")),str(facts.get("entity")))+"՝ "+str(facts.get("count"))+"։"
    return _admin_localized(plan.get("response_language") or _admin_detect_language(question),"unknown")


def _admin_answer_is_internal_payload(answer):
    text = str(answer or "").strip()
    if not text:
        return True
    try:
        obj = json.loads(text)
        if isinstance(obj, (dict, list)):
            return True
    except Exception:
        pass
    low = text.casefold()
    if low.startswith("user safety:") or "ai попросит подтверждение" in low or "ai will ask for confirmation" in low:
        return True
    return any(x in low for x in ('"tool_results"', '"application": {', '"documents": [', '"truth": {', '"candidates": [', '"checks": ['))


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
        state={"last_focused_application_id":None,"last_focused_field":None,"last_focused_entity_type":None,"last_focused_entity_id":None,
               "active_context":{"scope":"","subject":"","intent":"","query":"","filters":{},"entity_type":"","entity_id":None},
               "response_language":None,"last_shown_applications":[],"current_list":[],"current_position":None,
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
        "active_context":state.get("active_context") or {},
        "ai_schema":__import__("ai_schema").inspector.get_snapshot(),
        "response_language":state.get("response_language"),
        "last_action":state.get("last_action"),"last_action_failed":state.get("last_action_failed",False),
        "last_error":state.get("last_error"),"last_error_context":state.get("last_error_context"),"retry_count":state.get("retry_count",0),
        "query_capabilities":{"targets":["applications","partners","businesses","catalog","master_categories","catalog_overview","services","ai_usage"],"catalog_behavior":"master_categories and catalog return the complete current catalog without an artificial row limit; catalog_overview returns live database counts","operators":["eq","neq","contains","gt","gte","lt","lte","in"]},
        "history":state.get("history",[])[-6:]})


def _admin_context(limit=30,include_catalog=False):
    applications=platform_db.rows("""SELECT a.*,p.user_id FROM partner_applications a JOIN partners p ON p.id=a.partner_id
        WHERE a.status NOT IN ('approved','pending_partner','deleted') ORDER BY a.created_at DESC LIMIT %s""",(int(limit),))
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
# Legacy phrase-to-SQL query engine removed. AI reads use DataTools.

async def _admin_execute(command):
    intent=str(command.get("intent") or "").strip()
    aid=command.get("application_id")
    try:
        aid=int(aid) if aid is not None else None
    except (TypeError,ValueError):
        aid=None
    tools=DataTools("admin")
    if intent=="show_application_count":
        data=tools.execute("count",{"entity":"applications"}).get("data") or {}
        return "📨 Հայտերի քանակը՝ "+str(data.get("count") or 0)+"։"
    if intent=="show_partner_count":
        data=tools.execute("count",{"entity":"partners"}).get("data") or {}
        return "🤝 Գործընկերների քանակը՝ "+str(data.get("count") or 0)+"։"
    if intent=="show_applications":
        rows=(tools.execute("search_applications",{"limit":30}).get("data") or {}).get("items",[])
        if not rows: return "📨 Հայտեր չկան։"
        return "📨 Հայտեր ("+str(len(rows))+"):\n"+"\n".join(
            "#"+str(x.get("id"))+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("service_name") or "—")
            for x in rows[:20])
    if intent=="show_partners":
        rows=(tools.execute("search_partners",{"limit":50}).get("data") or {}).get("items",[])
        return "🤝 Գործընկերներ չկան։" if not rows else "🤝 Գործընկերներ:\n"+"\n".join(
            "#"+str(x.get("id"))+" · "+str(x.get("business_name") or "—")+" · "+str(x.get("status") or "—")
            for x in rows[:30])
    if intent=="show_businesses":
        rows=(tools.execute("search_companies",{"limit":100}).get("data") or {}).get("items",[])
        return "🏢 Ընկերություններ չկան։" if not rows else "🏢 Ընկերություններ:\n"+"\n".join(
            "#"+str(x.get("id"))+" · "+str(x.get("name") or "—")+" · "+str(x.get("status") or "—")
            for x in rows[:50])
    if intent in {"open_application","show_full_application"}:
        return _admin_full_application_text(aid)
    return "Չհաջողվեց որոշել հարցման տեսակը։"

def _admin_full_application_text(aid):
    if not aid:
        return "Укажите номер заявки."
    try:
        result=DataTools("admin").execute("get_application_full",{"application_id":int(aid)})
        payload=result.get("data") or {}
        app=payload.get("application")
        docs=payload.get("documents") or []
    except Exception:
        return "Не удалось открыть заявку #"+str(aid)+"."
    if not app:
        return "Заявка #"+str(aid)+" не найдена."
    def val(v):
        return "—" if v is None or str(v).strip()=="" else str(v)
    loc=", ".join(str(x) for x in (
        app.get("location_marz"),app.get("location_city"),
        app.get("location_village"),app.get("address")
    ) if x)
    lines=[
        "📨 Заявка #"+str(aid)+" · "+val(app.get("business_name")),
        "Статус: "+val(app.get("status")),
        "Telegram: "+val(app.get("user_id")),
        "📍 Место: "+(loc or "—"),
        "☎ Телефон: "+val(app.get("phone")),
        "🛠 Услуга: "+val(app.get("service_name")),
        "💰 Цена: "+(val(app.get("price"))+" ֏" if app.get("price") is not None else "—"),
        "🧭 Направление: "+val(app.get("direction_name")),
        "🏷 Подкатегория: "+val(app.get("subcategory_name")),
        "🆔 ID категории: "+val(app.get("category_id")),
        "📝 Описание: "+val(app.get("description")),
        "📄 Документы: "+str(len(docs)),
        "Создана: "+val(app.get("created_at")),
        "Обновлена: "+val(app.get("updated_at")),
    ]
    payload=app.get("payload_json") or {}
    if isinstance(payload,str):
        try:
            payload=json.loads(payload)
        except Exception:
            payload={}
    services=payload.get("services") if isinstance(payload,dict) else None
    if isinstance(services,list) and services:
        lines.append("🛠 Все услуги из заявки:")
        for svc in services:
            if isinstance(svc,dict):
                name=svc.get("name") or svc.get("service_name") or "—"
                price=svc.get("price")
                lines.append("• "+str(name)+((" · "+str(price)+" ֏") if price is not None else ""))
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
        # The application row may not carry a document_id. Documents are linked
        # through partner_verification_documents, so inspect the canonical relation.
        docs=_admin_semantic_documents(aid)
        if isinstance(docs,list) and docs:
            lines=["📄 Փաստաթղթեր՝ "+str(len(docs))]
            for d in docs[:20]:
                did=d.get("id") or d.get("document_id") or "—"
                dtype=d.get("document_type") or d.get("type") or "Փաստաթուղթ"
                status=d.get("status") or d.get("verification_status") or "—"
                lines.append("• #"+str(did)+" · "+str(dtype)+" · "+str(status))
            if len(docs)>20:
                lines.append("… և ևս "+str(len(docs)-20)+" փաստաթուղթ")
            return "\n".join(lines)
        doc_id=app.get("document_id")
        if doc_id:
            return "📄 Փաստաթուղթ\n🆔 ID: "+str(doc_id)+"\n📌 Հայտի փաստաթղթի ID-ն առկա է, բայց կապված փաստաթուղթ չգտնվեց։"
        return "📄 Փաստաթղթեր\nℹ️ Այս հայտի համար կապված փաստաթուղթ չի գտնվել。"
    return "Ուղղեք, թե հայտի որ դաշտն եք ուզում տեսնել."

def _admin_state_preview(action):
    if action.get("intent")=="delete_applications":
        ids=action.get("application_ids") or []
        existing=platform_db.rows("SELECT id,business_name,status FROM partner_applications WHERE id = ANY(%s) ORDER BY id DESC",(list(ids),))
        found={int(row["id"]):row for row in existing if str(row.get("id") or "").isdigit()}
        if not found:
            return "Ни одна из указанных заявок не найдена."
        parts=[]
        for value in ids:
            try: iv=int(value)
            except (TypeError,ValueError): continue
            row=found.get(iv)
            if row:
                parts.append("#"+str(iv)+" · "+str(row.get("business_name") or "—")+" · "+str(row.get("status") or "—"))
            else:
                parts.append("#"+str(iv)+" · не найдена")
        return "🗑 Удалить заявки:\n" + "\n".join(parts) + "\n⚠️ Заявки будут скрыты из активного списка."
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
    if action.get("intent")=="delete_applications":
        ids=action.get("application_ids") or []
        clean=[]
        for value in ids:
            try:
                iv=int(value)
                if iv>0 and iv not in clean:
                    clean.append(iv)
            except (TypeError,ValueError):
                pass
        if not clean:
            return "Չկա ջնջման ենթակա հայտ։"
        platform_db.execute(
            "UPDATE partner_applications SET status='deleted',updated_at=NOW() WHERE id = ANY(%s) AND status <> 'deleted'",
            (clean,)
        )
        return "✓ Ջնջված հայտեր՝ "+", ".join("#"+str(x) for x in clean)+"."
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


class AdminAIProviderError(RuntimeError):
    """All configured AI providers failed for this request."""
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


async def _admin_ai_completion(messages, *, max_tokens=700, json_mode=False):
    """Call AI providers in order: Groq, OpenAI, OpenRouter."""
    errors=[]
    providers=[]
    groq_key=os.getenv("GROQ_API_KEY","").strip()
    if groq_key:
        providers.append(("groq",groq_key,os.getenv("GROQ_MODEL","").strip() or "openai/gpt-oss-20b"))
    openai_key=os.getenv("OPENAI_API_KEY","").strip()
    if openai_key:
        providers.append(("openai",openai_key,os.getenv("OPENAI_MODEL","").strip() or "gpt-4o-mini"))
    openrouter_key=os.getenv("OPENROUTER_API_KEY","").strip()
    if openrouter_key:
        providers.append(("openrouter",openrouter_key,os.getenv("OPENROUTER_MODEL","").strip() or "nvidia/nemotron-3-super-120b-a12b:free"))
    if not providers:
        raise AdminAIProviderError("No AI provider is configured.")
    for provider,key,model in providers:
        try:
            if provider=="groq":
                from groq import AsyncGroq
                client=AsyncGroq(api_key=key, max_retries=0)
            else:
                from openai import AsyncOpenAI
                kwargs={"api_key":key}
                if provider=="openrouter":
                    kwargs["base_url"]="https://openrouter.ai/api/v1"
                    kwargs["default_headers"]={
                        "HTTP-Referer":os.getenv("OPENROUTER_SITE_URL","https://armenia-ai-guide-phase3.onrender.com"),
                        "X-Title":"Armenia AI Guide",
                    }
                kwargs["max_retries"]=0
                client=AsyncOpenAI(**kwargs)
            request_kwargs={"model":model,"messages":messages,"temperature":0,"max_tokens":max_tokens}
            # Keep reasoning-token usage low for the OpenRouter planner so the
            # completion budget is spent on the required ActionPlan JSON.
            if provider=="openrouter" and json_mode:
                request_kwargs["reasoning"]={"effort":"low","exclude":True}
            # Structured planner calls use provider-side JSON mode when supported.
            # This is a transport constraint, not phrase-specific semantic logic.
            if json_mode:
                request_kwargs["response_format"]={"type":"json_object"}
            try:
                resp=await client.chat.completions.create(**request_kwargs)
            except Exception as structured_exc:
                # Only retry structured JSON calls when the provider rejected the
                # JSON transport itself (typically HTTP 400). Never retry 429s:
                # doing so multiplies rate-limit pressure and delays the next provider.
                status=getattr(getattr(structured_exc,"response",None),"status_code",None)
                message_text=str(structured_exc).lower()
                is_rate_limited=(status==429 or "429" in message_text or "rate limit" in message_text or "too many requests" in message_text)
                if json_mode and not is_rate_limited and status in (400,422,None):
                    resp=await client.chat.completions.create(
                        model=model,messages=messages,temperature=0,max_tokens=max_tokens
                    )
                else:
                    raise structured_exc
            usage=getattr(resp,"usage",None)
            ai_cost_center.record_usage(
                provider=provider, model=model, chain="admin_secretary",
                stage="planner", operation="admin_ai_message",
                purpose="Admin natural-language assistant",
                input_tokens=int(getattr(usage,"prompt_tokens",0) or 0),
                output_tokens=int(getattr(usage,"completion_tokens",0) or 0),
                cached_tokens=int(getattr(getattr(usage,"prompt_tokens_details",None),"cached_tokens",0) or 0),
                reasoning_tokens=int(getattr(getattr(usage,"completion_tokens_details",None),"reasoning_tokens",0) or 0),
            )
            content=(resp.choices[0].message.content or "").strip()
            if not content:
                raise RuntimeError("empty AI response")
            return content,provider,model
        except Exception as exc:
            errors.append({"provider":provider,"model":model,"error":str(exc)[:500]})
    raise AdminAIProviderError("All configured AI providers failed.",errors)

def _admin_planner_context(ctx):
    """Build compact semantic context from business entities, not DB internals."""
    ctx=ctx if isinstance(ctx,dict) else {}
    active=ctx.get("active_context") if isinstance(ctx.get("active_context"),dict) else {}
    rows=ctx.get("last_shown_query_rows") or []
    compact_rows=[]
    for row in rows[:6]:
        if not isinstance(row,dict): continue
        compact_rows.append({k:row.get(k) for k in (
            "id","business_name","name","service_name","status","price","category_id",
            "category_name_am","category_name_ru","category_name_en","master_category_id",
            "master_name_am","master_name_ru","master_name_en","location_marz","location_city",
            "address","partner_name","verification_status"
        ) if k in row})
    history=ctx.get("history") or []
    compact_history=[]
    for item in history[-6:]:
        if isinstance(item,dict):
            compact_history.append({
                "role":str(item.get("role") or "")[:20],
                "content":str(item.get("content") or "")[:500]
            })

    focused_type=str(ctx.get("last_focused_entity_type") or active.get("entity_type") or "").strip()
    focused_id=ctx.get("last_focused_entity_id") or ctx.get("last_focused_application_id") or active.get("entity_id")
    ai_context=""
    try:
        ai_context=build_ai_context(
            focused_type or None,
            focused_id,
            include_platform_index=True,
        )
    except Exception:
        ai_context=""

    return {
        "active_context":{
            "scope":str(active.get("scope") or "")[:80],
            "subject":str(active.get("subject") or "")[:160],
            "intent":str(active.get("intent") or "")[:80],
            "query":str(active.get("query") or "")[:400],
            "entity_type":str(active.get("entity_type") or "")[:40],
            "entity_id":active.get("entity_id")
        },
        "focused_entity":{
            "type":focused_type[:40],
            "id":focused_id
        },
        "ai_context":ai_context[:14000],
        "last_query_target":str(ctx.get("last_query_target") or ctx.get("last_result_kind") or "")[:60],
        "last_rows":compact_rows,
        "history":compact_history,
        "preferred_response_language":ctx.get("preferred_response_language") or ctx.get("response_language")
    }

async def _admin_ai_json(message,ctx):
    """Compact universal semantic planner; no phrase-specific intent dictionaries."""
    is_replanning=bool(isinstance(ctx,dict) and ctx.get("replanning"))
    planner_ctx=_admin_planner_context(ctx)
    registry=_admin_tool_registry("admin")
    schemas=_admin_tool_schemas("admin")
    system="""You are the universal semantic planner for Armenia AI Guide admin.
Understand Armenian, Russian, English, mixed language, transliteration, typos and short follow-ups.
You are NOT a database client. You may only request tools from the supplied registry.
Never invent IDs, database fields, SQL, table names or results. Resolve entities from context or request a search tool.
Choose the minimum number of read-only tools needed to answer the user.
For a count use count. For live platform totals use catalog_overview. For AI usage/cost use ai_usage_summary.
For a complete application use get_application_full. For semantic catalog matching use check_catalog_match.
Mutations must use action_required=mutation and are handled separately; never execute a mutation merely because the user asks for it.
Return ONLY one JSON object with:
reasoning_summary,intent,target,entity_type,entity_id,entity_name,data_needed,tool_requests,
field,value_raw,navigation,filters,sort,limit,action_required,response_language,confidence,active_context.
Each tool_request must be {"name":"TOOL_NAME","arguments":{...}}.
Only use tools present in TOOL_REGISTRY and arguments matching TOOL_SCHEMAS.
active_context={scope,subject,intent,query,filters,entity_type,entity_id}.
reasoning_summary is at most one short sentence.

TOOL_REGISTRY:
""" + json.dumps(registry,ensure_ascii=False) + """

TOOL_SCHEMAS:
""" + json.dumps(schemas,ensure_ascii=False) + """
"""
    payload=json.dumps({
        "message":str(message or "")[:1500],
        "context":planner_ctx,
        "previous_tool_results":ctx.get("compact_tool_results",[]) if isinstance(ctx,dict) else [],
        "ai_context":planner_ctx.get("ai_context","")
    },ensure_ascii=False,default=str)
    raw,provider,model=await _admin_ai_completion(
        [{"role":"system","content":system},{"role":"user","content":payload}],
        max_tokens=350 if is_replanning else 500,
        json_mode=True,
    )
    raw=(raw or "").strip()
    try:
        data=json.loads(raw)
    except json.JSONDecodeError:
        start=raw.find("{")
        if start<0:
            raise RuntimeError(f"{provider} returned invalid planner JSON")
        try:
            data,_end=json.JSONDecoder().raw_decode(raw[start:])
        except json.JSONDecodeError:
            raise RuntimeError(f"{provider} returned invalid planner JSON")
    if not isinstance(data,dict):
        raise RuntimeError(f"{provider} returned non-object planner output")
    data["ai_provider"]=provider
    data["ai_model"]=model
    return _admin_normalize_plan(data,message)

def _admin_contextual_fallback_plan(message,state):
    """Last-resort identity fallback only; semantic topic resolution belongs to Groq."""
    focused_id=state.get("last_focused_application_id") or state.get("last_focused_entity_id")
    if not focused_id:
        return None
    return _admin_normalize_plan({
        "intent":"information_request",
        "target":"application",
        "entity_type":state.get("last_focused_entity_type") or "application",
        "entity_id":focused_id,
        "data_needed":["entity"],
        "action_required":"read_only",
        "confidence":0.20,
    },message)

def _admin_fallback_intent(message,focused_id=None,state=None):
    '''Safe context-first fallback when every AI provider is unavailable.
    This is not a command dictionary: it scores the user's words against the
    business vocabulary exposed by the current AI Context and uses only
    deterministic database facts. Normal operation still goes through AI.
    '''
    state=state or {}
    text=_norm(str(message or ""))
    if focused_id:
        return _admin_normalize_plan({
            "intent":"information_request",
            "target":"application",
            "entity_type":"application",
            "entity_id":focused_id,
            "data_needed":["entity"],
            "action_required":"read_only",
            "reasoning_summary":"AI providers unavailable; preserved the current focused entity.",
            "confidence":0.05,
        },message)

    # Use the live platform index vocabulary rather than hard-coded user phrases.
    # This gives the UI a useful factual response during provider outages.
    vocab={
        "partners": {"partner","partners","գործընկեր","գործընկերներ","партнер","партнёры"},
        "applications": {"application","applications","հայտ","հայտեր","заявка","заявки"},
        "services": {"service","services","ծառայություն","ծառայություններ","услуга","услуги"},
        "directions": {"direction","directions","ուղղություն","ուղղություններ","направление","направления"},
        "subcategories": {"subcategory","subcategories","ենթակատեգորիա","ենթակատեգորիաներ","подкатегория","подкатегории"},
        "ai_usage": {"ai","AI","ai operation","ai operations","AI operations","ai usage","ai cost","AI costs","AI ծախս","AI ծախսեր","AI գործողություն","AI գործողություններ","операции ai","операция ai","ai операции","расходы ai"},
    }
    scores={k:sum(1 for token in vals if token in text) for k,vals in vocab.items()}
    target=max(scores,key=scores.get) if scores else None
    if target and scores[target]>0:
        intent=("count_ai_usage" if target=="ai_usage" and any(x in text for x in {"քանի","сколько","how many","count","количество"}) else ("count" if any(x in text for x in {"քանի","сколько","how many","count","количество"}) else "query_database"))
        return _admin_normalize_plan({
            "intent":intent,
            "target":target,
            "entity_type":target.rstrip("s"),
            "data_needed":[target],
            "tool_requests":[{"name":"count","arguments":{"entity":target}}],
            "action_required":"read_only",
            "reasoning_summary":"AI providers unavailable; used the live platform vocabulary for a safe factual query.",
            "confidence":0.10,
        },message)
    return _admin_normalize_plan({
        "intent":"unknown",
        "action_required":"read_only",
        "reasoning_summary":"All configured AI providers were unavailable; no safe semantic interpretation was possible.",
        "confidence":0.0,
    },message)

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
        focused_type_norm=str(focused_type or "").lower()
        requested={str(x).casefold() for x in (plan.get("data_needed") or [])}
        # Keep the immediately focused application for natural follow-ups such as
        # documents/services/category checks. The model decides the semantic field;
        # Python only preserves the existing entity identity.
        if focused_type_norm=="application" and (
            not entity_type
            or entity_type=="application"
            or requested & {"documents","services","application_services","category","subcategory","catalog","status"}
        ):
            return "application",int(focused_id)
        if not entity_type or entity_type in {focused_type_norm,"application"}:
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
        # Application payload may contain the complete set of services supplied
        # during onboarding. This is distinct from the public catalog/service table.
        payload=app.get("payload_json") or {}
        if isinstance(payload,str):
            try: payload=json.loads(payload)
            except Exception: payload={}
        payload_services=payload.get("services") if isinstance(payload,dict) else None
        if isinstance(payload_services,list):
            result["application_services"]=_admin_safe(payload_services)

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
    requests=plan.get("tool_requests") or []
    if not requests: return {}
    tools=DataTools("admin")
    results=[]
    for req in requests[:6]:
        if not isinstance(req,dict): continue
        name=str(req.get("name") or "").strip()
        args=dict(req.get("arguments") or {})
        if entity_id:
            if entity_type=="application" and name in {"get_application","get_documents","check_application"}:
                args.setdefault("application_id",int(entity_id))
            elif entity_type=="partner" and name in {"get_partner","get_documents","get_addresses","get_services"}:
                args.setdefault("partner_id",int(entity_id))
            elif entity_type in {"business","company"} and name=="get_company":
                args.setdefault("company_id",int(entity_id))
        try:
            results.append(tools.execute(name,args))
        except DataToolError as exc:
            results.append({"tool":name,"data":{},"error":str(exc)})
        except Exception as exc:
            results.append({"tool":name,"data":{},"error":"tool_execution_failed:"+str(exc)[:250]})
    return {"tool_results":results}

def _admin_compact_tool_result_for_replanning(result, question=""):
    """Keep re-planning context factual but small.

    The full Data Contract remains available to the final answer layer. Re-planning
    only needs identifiers, names, statuses, prices, category/location fields and
    a bounded set of other fields to decide the next tool.
    """
    if not isinstance(result,dict):
        return result
    out={
        "status":result.get("status"),
        "tool_executed":result.get("tool_executed"),
        "extracted_records_count":result.get("extracted_records_count"),
        "system_notice":result.get("system_notice"),
    }
    data=result.get("data")
    if isinstance(data,list):
        compact=[]
        priority=("id","row_index","name","name_am","name_ru","name_en","business_name",
                  "service_name","status","verification_status","price","price_amd","prices_amd",
                  "category_id","category_name","category_name_am","category_name_ru","subcategory_name",
                  "master_category_id","master_name_am","master_name_ru","city","location_city",
                  "marz","location_marz","address","document_type")
        for row in data[:20]:
            if not isinstance(row,dict):
                compact.append(row); continue
            fields=row.get("fields") if isinstance(row.get("fields"),dict) else row
            picked={}
            for key in priority:
                if key in fields and fields[key] not in (None,""):
                    picked[key]=fields[key]
            if not picked:
                for key,value in list(fields.items())[:10]:
                    picked[str(key)]=value
            compact.append({"row_index":row.get("row_index"),"table":row.get("table"),
                            "fields":_admin_safe(picked)})
        out["data"]=compact
    elif isinstance(data,dict):
        out["data"]=_admin_safe({str(k):v for k,v in list(data.items())[:16]})
    else:
        out["data"]=_admin_safe(data)
    return out


async def _admin_refine_tool_context(question, plan, entity_type, entity_id, facts):
    """Bounded semantic Re-planning loop.

    The first planner has the live schema. Re-planners receive compact Data Contracts
    only, so the same database schema and large result payload are not resent.
    """
    if not isinstance(facts,dict):
        return facts

    accumulated=list(facts.get("tool_results") or [])
    seen=set()
    for item in accumulated:
        if isinstance(item,dict):
            seen.add(json.dumps({
                "tool":item.get("tool_executed"),
                "data":item.get("data"),
            },ensure_ascii=False,default=str,sort_keys=True)[:1200])

    # Two re-planning passes + the initial planner + final answer = at most four
    # model calls for one semantic request. Increase only after measuring usage.
    max_steps=2
    for _step in range(max_steps):
        compact_results=[_admin_compact_tool_result_for_replanning(x,question) for x in accumulated]
        try:
            replanned=await _admin_ai_json(
                question,
                {
                    "replanning": True,
                    "active_context": facts.get("active_context") or {},
                    "tool_results": compact_results,
                    "plan": plan,
                }
            )
        except Exception:
            break

        requests=replanned.get("tool_requests") or []
        if not requests:
            break

        fresh=_admin_tool_context(
            {"tool_requests":requests},
            entity_type,
            entity_id,
        )
        new_results=fresh.get("tool_results") or []
        if not new_results:
            break

        added=0
        for result in new_results:
            signature=json.dumps({
                "tool":result.get("tool_executed"),
                "data":result.get("data"),
            },ensure_ascii=False,default=str,sort_keys=True)[:2000]
            if signature not in seen:
                seen.add(signature)
                accumulated.append(result)
                added+=1
        plan=replanned
        if not added:
            break

    if accumulated:
        merged=dict(facts)
        merged["tool_results"]=accumulated
        merged["replanning_steps"]=len(accumulated)
        return merged
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
    # Convert the planner semantic data request into authoritative reads. No phrase matching.
    if entity_type=="application" and entity_id and needed:
        plan.setdefault("tool_requests", [])
        requested={str(x).casefold() for x in needed}
        names={str(r.get("name") or "") for r in plan["tool_requests"] if isinstance(r,dict)}
        if "documents" in requested and "get_documents" not in names:
            plan["tool_requests"].append({"name":"get_documents","arguments":{"application_id":int(entity_id)}})
        if {"services","application_services"} & requested and not ({"get_services","get_application"} & names):
            plan["tool_requests"].append({"name":"get_application","arguments":{"application_id":int(entity_id)}})
        if {"category","subcategory","catalog"} & requested and not ({"check_application","check_catalog_match"} & names):
            plan["tool_requests"].append({"name":"check_application","arguments":{"application_id":int(entity_id)}})
    # Semantic hydration: once the planner identifies what information is needed,
    # Python guarantees the corresponding safe read even if the model omitted a tool call.
    # This is business-semantic, not phrase-specific routing.
    if entity_id and entity_type in {"partner","business","company"} and needed:
        plan.setdefault("tool_requests", [])
        requested={str(x).casefold() for x in needed}
        names={str(r.get("name") or "") for r in plan["tool_requests"] if isinstance(r,dict)}
        if "services" in requested and "get_services" not in names:
            plan["tool_requests"].append({"name":"get_services","arguments":{"partner_id":int(entity_id)} if entity_type=="partner" else {"business_id":int(entity_id)}})
        if "documents" in requested and "get_documents" not in names:
            plan["tool_requests"].append({"name":"get_documents","arguments":{"partner_id":int(entity_id)} if entity_type=="partner" else {}})
        if {"category","subcategory","catalog"} & requested and "get_services" not in names and entity_type=="partner":
            plan["tool_requests"].append({"name":"get_services","arguments":{"partner_id":int(entity_id)}})
    target=str(plan.get("target") or "").strip().lower()
    simple_counts={"partners":"partners","applications":"applications","services":"services",
                   "directions":"directions","subcategories":"subcategories","companies":"companies",
                   "addresses":"addresses","documents":"documents"}
    if target in simple_counts and str(plan.get("intent") or "").casefold() in {"count","count_entities","count_partners","count_applications","count_services","count_directions","count_subcategories","count_ai_usage"}:
        try:
            entity=simple_counts[target]
            raw=DataTools("admin").execute("count",{"entity":entity})
            facts=raw.get("data") if isinstance(raw,dict) else {}
        except Exception as exc:
            facts={"error":str(exc)[:180]}
    elif target=="catalog_overview":
        try:
            facts=DataTools("admin").execute("catalog_overview",{}).get("data",{})
        except Exception as exc:
            facts={"error":str(exc)[:180]}
    elif target=="ai_usage":
        try:
            days=int(plan.get("days") or 1)
            facts=DataTools("admin").execute("ai_usage_summary",{"days":days}).get("data",{})
        except Exception as exc:
            facts={"error":str(exc)[:180]}
    else:
        facts=_admin_tool_context(plan,entity_type,entity_id)
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
    fallback=_admin_safe_human_fallback(facts, question, plan, target, entity_type)
    if isinstance(facts,dict) and "rows" in facts and (target or entity_type)=="services":
        facts["category_audit"]=_admin_service_category_audit(facts.get("rows") or [])
        fallback=_admin_safe_human_fallback(facts, question, plan, target, entity_type)

    # Keep the semantic request to a single AI planner pass.
    # The planner chooses the safe read tools; Python/database facts are then
    # rendered by the deterministic human fallback. This prevents a second
    # provider call merely to rewrite an already verified DB result.
    return fallback

async def admin_ai_message(admin_id,message):
    message=str(message or "").strip()
    if not message: return "Գրեք, թե ինչ պետք է ստուգեմ կամ փոխեմ։"
    state=_admin_session(admin_id); normalized=_norm(message)

    # A language-only turn changes presentation language, not the conversation topic.
    if re.fullmatch(r"(?:հայերեն|հայերենով|հայերեն պատասխանիր|պատասխանիր հայերեն|по[- ]русски|на русском|ответь по[- ]русски|in english|answer in english|english please)",normalized,re.I|re.U):
        state["response_language"]="am" if re.search(r"հայերեն",normalized,re.I|re.U) else ("en" if "english" in normalized else "ru")
        prior=state.get("last_query") or (state.get("active_context") or {}).get("query")
        if prior:
            ac=state.get("active_context") or {}
            follow_plan=_admin_normalize_plan({"intent":ac.get("intent") or "information_request","target":ac.get("scope") or state.get("last_query_target") or "","entity_type":ac.get("entity_type") or "","entity_id":ac.get("entity_id"),"entity_name":ac.get("subject") or "","filters":ac.get("filters") or {},"response_language":state["response_language"],"action_required":"read_only","confidence":1.0},prior)
            try: reply=await _admin_semantic_answer(prior,follow_plan,state)
            except Exception: reply="Հասկացա։ Այսուհետ կպատասխանեմ հայերեն։" if state["response_language"]=="am" else ("Понял. Дальше отвечу по-русски." if state["response_language"]=="ru" else "Understood. I’ll answer in English.")
        else:
            reply="Հասկացա։ Այսուհետ կպատասխանեմ հայերեն։" if state["response_language"]=="am" else ("Понял. Дальше отвечу по-русски." if state["response_language"]=="ru" else "Understood. I’ll answer in English.")
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

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

    # High-confidence application navigation/deletion is handled locally.
    # This prevents provider rate limits from breaking basic admin operations.
    open_match=re.search(r"(?i)\b(?:открой|открыть|open|բացիր|բացել|ցույց\s+տուր)\s*(?:заявку|заявка|application|հայտ)?\s*#?\s*(\d+)\b", message)
    if open_match:
        try:
            open_id=int(open_match.group(1))
            if _admin_hydrate_application(open_id):
                state["last_focused_application_id"]=open_id
                state["last_focused_entity_type"]="application"
                state["last_focused_entity_id"]=open_id
                reply=await _admin_execute({"intent":"open_application","application_id":open_id})
            else:
                reply="Заявка #"+str(open_id)+" не найдена."
        except Exception:
            reply="Не удалось открыть заявку."
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    delete_match=re.search(r"(?i)\b(?:удали|удалить|удалите|delete|remove|հեռացրու|հեռացնել|ջնջիր|ջնջել)\b", message)
    if delete_match:
        ids=[]
        explicit=[int(x) for x in re.findall(r"#?(\d+)",message)]
        if explicit:
            ids=explicit
        else:
            rows=state.get("last_shown_query_rows") or []
            if rows:
                ids=[int(row["id"]) for row in rows if isinstance(row,dict) and str(row.get("id") or "").isdigit()]
            if not ids:
                ids=[int(row["id"]) for row in (state.get("current_list") or []) if isinstance(row,dict) and str(row.get("id") or "").isdigit()]
        ids=list(dict.fromkeys(ids))
        if not ids:
            reply="Укажите номер заявки или сначала покажите заявки."
            _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply
        action={"intent":"delete_applications","application_ids":ids}
        state["pending_action"]=_admin_safe(action)
        reply="🤖 Подготовил действие:\n\n"+_admin_state_preview(action)+"\n\nПодтвердить удаление? «да» / «нет»"
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    # Route ordinary read-only conversation through the semantic planner before legacy command handlers.
    try:
        plan_raw=await _admin_ai_json(message,state)
        plan=_admin_normalize_plan(plan_raw,message)
        if str(plan.get("action_required") or "read_only").lower() not in {"mutation","write","confirm"}:
            reply=await _admin_semantic_answer(message,plan,state)
            _admin_history(state,"admin",message); _admin_history(state,"assistant",reply)
            return reply
    except Exception:
        pass

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
    if nav_intent:
        c=nav_intent
    else:
        c=None
    ctx=_admin_hydrate_context(state)
    if state.get("response_language"):
        ctx["preferred_response_language"]=state.get("response_language")
    if c is None:
        try:
            c=await _admin_ai_json(message,ctx)
        except AdminAIProviderError:
            # Provider outage must not destroy the conversation state. Use the
            # safe semantic-context fallback; the data layer remains authoritative.
            c=_admin_fallback_intent(message,focused_id,state)
        except Exception: c=_admin_fallback_intent(message,focused_id,state)
    c=_admin_normalize_plan(c,message)

    # Generic conversational identity resolution: when the semantic planner identifies the
    # previous result as an application but omits its ID, safely inherit the ID only when
    # exactly one application row is present in the immediately previous result.
    if not c.get("entity_id") and str(c.get("entity_type") or c.get("target") or "").lower() in {"application","applications"}:
        previous_rows=state.get("last_shown_query_rows") or []
        if len(previous_rows)==1 and isinstance(previous_rows[0],dict) and previous_rows[0].get("id") is not None:
            try:
                c["entity_id"]=int(previous_rows[0]["id"])
                c["application_id"]=int(previous_rows[0]["id"])
            except (TypeError,ValueError):
                pass

    # One generic context pass handles low-confidence/unknown turns. This is
    # deliberately subject-based (entity + field + semantic goal), not a list
    # of special phrases, so new natural-language variants reuse the same path.
    if str(c.get("intent") or "unknown") in {"unknown",""} or float(c.get("confidence") or 0) < 0.35:
        contextual=_admin_contextual_fallback_plan(message,state)
        if contextual:
            c=contextual

    ac=c.get("active_context")
    if isinstance(ac,dict):
        state["active_context"]=_admin_safe({"scope":ac.get("scope") or c.get("target") or "","subject":ac.get("subject") or c.get("entity_name") or "","intent":ac.get("intent") or c.get("intent") or "","query":ac.get("query") or message[:500],"filters":ac.get("filters") if isinstance(ac.get("filters"),dict) else (c.get("filters") or {}),"entity_type":ac.get("entity_type") or c.get("entity_type") or "","entity_id":ac.get("entity_id") if ac.get("entity_id") not in (None,"") else c.get("entity_id")})
    else:
        target_hint=str(c.get("target") or "").lower(); entity_hint=str(c.get("entity_type") or "").lower()
        if target_hint in {"catalog","master_categories","catalog_overview","partners","businesses","services"} or entity_hint in {"catalog","partner","business","service"}:
            state["active_context"]=_admin_safe({"scope":target_hint or entity_hint,"subject":str(c.get("entity_name") or "")[:200],"intent":str(c.get("intent") or ""),"query":message[:500],"filters":c.get("filters") if isinstance(c.get("filters"),dict) else {},"entity_type":entity_hint,"entity_id":c.get("entity_id")})
    state["last_action"]={"intent":c.get("intent"),"target":c.get("target"),"reasoning_summary":c.get("reasoning_summary"),"confidence":c.get("confidence")}
    state["last_action_failed"]=False; state["last_error"]=None; state["last_error_context"]=None

    intent=str(c.get("intent") or "unknown").lower()
    if intent=="catalog_counts":
        c["intent"]="information_request"
        c["target"]="catalog_overview"
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

    if intent=="query_database" and c.get("tool_requests"):
        reply=await _admin_semantic_answer(message,c,state)
        _admin_history(state,"admin",message); _admin_history(state,"assistant",reply); return reply

    if intent=="query_database":
        reply=await _admin_semantic_answer(message,c,state)
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

    if intent not in {"edit_application","approve_application","reject_application","clarify_application","delete_applications"}:
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



async def api_admin_ai_costs(request):
    _admin(request)
    try:
        days=max(1,min(int(request.query.get("days","30") or 30),3650))
    except (TypeError,ValueError):
        days=30
    partner_raw=request.query.get("partner_id")
    partner_id=None
    if partner_raw not in (None,""):
        try: partner_id=int(partner_raw)
        except (TypeError,ValueError): partner_id=None
    from ai_cost_center import usage_summary
    return web.json_response({"ok":True,"days":days,"partner_id":partner_id,**usage_summary(partner_id=partner_id,days=days)})


async def api_admin_ai_costs_providers(request):
    _admin(request)
    try:
        days=max(1,min(int(request.query.get("days","30") or 30),3650))
    except (TypeError,ValueError):
        days=30
    rows=platform_db.rows(
        """SELECT provider,model,COUNT(*) operations,
                  COALESCE(SUM(input_tokens),0) input_tokens,
                  COALESCE(SUM(output_tokens),0) output_tokens,
                  COALESCE(SUM(cached_input_tokens),0) cached_input_tokens,
                  COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,
                  COALESCE(SUM(total_tokens),0) total_tokens,
                  COALESCE(SUM(total_cost_usd),0) total_cost_usd,
                  COALESCE(SUM(total_cost_amd),0) total_cost_amd
           FROM ai_usage_ledger
           WHERE created_at >= NOW() - (%s || ' days')::interval
           GROUP BY provider,model ORDER BY total_cost_usd DESC, operations DESC""",
        [days],
    )
    return web.json_response({"ok":True,"days":days,"items":rows})


async def api_admin_ai_costs_partners(request):
    _admin(request)
    try:
        days=max(1,min(int(request.query.get("days","30") or 30),3650))
    except (TypeError,ValueError):
        days=30
    rows=platform_db.rows(
        """SELECT u.partner_id, COALESCE(p.business_name,'') business_name, COUNT(*) operations,
                  COALESCE(SUM(input_tokens),0) input_tokens,
                  COALESCE(SUM(output_tokens),0) output_tokens,
                  COALESCE(SUM(total_tokens),0) total_tokens,
                  COALESCE(SUM(total_cost_usd),0) total_cost_usd
           FROM ai_usage_ledger u
           LEFT JOIN partners p ON p.id=u.partner_id
           WHERE u.partner_id IS NOT NULL
             AND u.created_at >= NOW() - (%s || ' days')::interval
           GROUP BY u.partner_id,p.business_name ORDER BY total_cost_usd DESC, operations DESC""",
        [days],
    )
    return web.json_response({"ok":True,"days":days,"items":rows})




async def api_admin_ai_company_economics(request):
    _admin(request)
    try: days=max(1,min(int(request.query.get("days") or 30),3650))
    except (TypeError,ValueError): days=30
    raw=request.query.get("partner_id")
    try: partner_id=int(raw) if raw not in (None,"") else None
    except (TypeError,ValueError): return web.json_response({"ok":False,"error":"invalid_partner_id"},status=400)
    return web.json_response({"ok":True,"period_days":days,
                              "items":ai_cost_center.company_economics(days=days,partner_id=partner_id)})


async def api_admin_ai_partner_economics(request):
    _admin(request)
    try: days=max(1,min(int(request.query.get("days") or 30),3650))
    except (TypeError,ValueError): days=30
    return web.json_response({"ok":True,"period_days":days,
                              "items":ai_cost_center.partner_economics(days=days)})


async def api_admin_ai_financial_summary(request):
    _admin(request)
    try:
        days=max(1,min(int(request.query.get("days") or 30),3650))
    except (TypeError,ValueError):
        days=30
    partner_raw=request.query.get("partner_id")
    try:
        partner_id=int(partner_raw) if partner_raw not in (None,"") else None
    except (TypeError,ValueError):
        return web.json_response({"ok":False,"error":"invalid_partner_id"},status=400)
    data=ai_cost_center.project_economics(days=days,partner_id=partner_id)
    return web.json_response({"ok":True,"summary":data})


async def api_admin_ai_project_economics(request):
    _admin(request)
    try:
        days=max(1,min(int(request.query.get("days") or 30),3650))
    except (TypeError,ValueError):
        days=30
    partner_raw=request.query.get("partner_id")
    try:
        partner_id=int(partner_raw) if partner_raw not in (None,"") else None
    except (TypeError,ValueError):
        return web.json_response({"ok":False,"error":"invalid_partner_id"},status=400)
    data=ai_cost_center.project_economics(days=days,partner_id=partner_id)
    return web.json_response({"ok":True,**data})



async def api_admin_ai_add_expense(request):
    _admin(request)
    try:
        body=await request.json()
        booking_id=int(body.get("booking_id")) if body.get("booking_id") not in (None,"") else None
        amount=float(body.get("amount_amd") or 0)
        expense_type=str(body.get("expense_type") or "other")
        partner_id=int(body.get("partner_id")) if body.get("partner_id") not in (None,"") else None
    except Exception:
        return web.json_response({"ok":False,"error":"invalid_payload"},status=400)
    if amount < 0:
        return web.json_response({"ok":False,"error":"amount_must_be_nonnegative"},status=400)
    try:
        from ai_cost_center import record_project_expense
        row=record_project_expense(booking_id=booking_id,partner_id=partner_id,
                                   expense_type=expense_type,amount_amd=amount,
                                   description=str(body.get("description") or ""),
                                   source=str(body.get("source") or "manual"))
    except ValueError as e:
        return web.json_response({"ok":False,"error":str(e)},status=400)
    return web.json_response({"ok":True,"expense":row})


async def api_admin_ai_order_economics(request):
    _admin(request)
    try:
        order_id=int(request.match_info["order_id"])
    except (TypeError,ValueError):
        return web.json_response({"ok":False,"error":"invalid_order_id"},status=400)
    from ai_cost_center import order_economics
    data=order_economics(order_id)
    if not data:
        return web.json_response({"ok":False,"error":"order_not_found"},status=404)
    return web.json_response({"ok":True,**data})



async def api_admin_ai_company_orders(request):
    _admin(request)
    try:
        company_id=int(request.match_info["company_id"])
    except (TypeError,ValueError):
        return web.json_response({"ok":False,"error":"invalid_company_id"},status=400)
    try:
        days=max(1,min(int(request.query.get("days") or 3650),3650))
    except (TypeError,ValueError):
        days=3650
    from ai_cost_center import company_orders_economics
    return web.json_response({"ok":True,"company_id":company_id,"period_days":days,
                              "items":company_orders_economics(company_id=company_id,days=days)})


async def api_admin_ai_order_trace(request):
    _admin(request)
    try:
        order_id=int(request.match_info["order_id"])
    except (TypeError,ValueError):
        return web.json_response({"ok":False,"error":"invalid_order_id"},status=400)
    from ai_cost_center import order_financial_trace
    data=order_financial_trace(order_id)
    if not data:
        return web.json_response({"ok":False,"error":"order_not_found"},status=404)
    return web.json_response({"ok":True,**data})


async def api_admin_ai_negotiation_economics(request):
    _admin(request)
    try:
        negotiation_id=int(request.match_info["negotiation_id"])
    except (TypeError,ValueError):
        return web.json_response({"ok":False,"error":"invalid_negotiation_id"},status=400)
    from ai_cost_center import negotiation_economics
    data=negotiation_economics(negotiation_id)
    if not data:
        return web.json_response({"ok":False,"error":"negotiation_not_found"},status=404)
    return web.json_response({"ok":True,**data})


def register_admin_ai_routes(app, ai, bot=None):
    app['ai']=ai
    app['bot']=bot
    app.router.add_post('/api/admin/assistant',api_admin_assistant)
    app.router.add_get('/api/admin/ai/costs',api_admin_ai_costs)
    app.router.add_get('/api/admin/ai/costs/partners',api_admin_ai_costs_partners)
    app.router.add_get('/api/admin/ai/costs/providers',api_admin_ai_costs_providers)
    app.router.add_get('/api/admin/ai/economics/order/{order_id}',api_admin_ai_order_economics)
    app.router.add_get('/api/admin/ai/economics/project',api_admin_ai_project_economics)
    app.router.add_get('/api/admin/ai/financial-summary',api_admin_ai_financial_summary)
    app.router.add_get('/api/admin/ai/partner-economics',api_admin_ai_partner_economics)
    app.router.add_get('/api/admin/ai/company-economics',api_admin_ai_company_economics)
    app.router.add_post('/api/admin/ai/economics/expense',api_admin_ai_add_expense)
    app.router.add_get('/api/admin/ai/economics/negotiation/{negotiation_id}',api_admin_ai_negotiation_economics)
    app.router.add_get('/api/admin/ai/economics/company/{company_id}/orders',api_admin_ai_company_orders)
    app.router.add_get('/api/admin/ai/economics/order/{order_id}/trace',api_admin_ai_order_trace)
    app.router.add_get('/api/admin/ai/catalog-proposals',catalog_list)
    app.router.add_post('/api/admin/ai/catalog-proposals/{id}/{action}',catalog_action)
    app.router.add_get('/api/admin/potential-partners',potential_list)
    app.router.add_post('/api/admin/potential-partners/structure',potential_structure)
    app.router.add_post('/api/admin/potential-partners/research',potential_research)
    app.router.add_post('/api/admin/potential-partners/{id}/status',potential_status)



