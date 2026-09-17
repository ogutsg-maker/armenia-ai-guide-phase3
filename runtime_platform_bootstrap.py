"""Runtime compatibility/bootstrap layer for Armenia AI Guide."""
from __future__ import annotations
import base64, hashlib, hmac, importlib, json, logging, os, time
from functools import wraps
import aiohttp, psycopg
from aiohttp import web
from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data
_original_application_init = web.Application.__init__

def _database_url():
    value=os.getenv("DATABASE_URL","").strip()
    if not value: raise RuntimeError("DATABASE_URL is not configured")
    return value

def _document_access_secret():
    return (os.getenv("TELEGRAM_BOT_TOKEN","").strip() or os.getenv("BOT_TOKEN","").strip() or os.getenv("SUPABASE_SERVICE_ROLE_KEY","").strip()).encode()

def _make_document_access_token(pid, doc_id, ttl=900):
    raw=json.dumps({"pid":int(pid),"doc":int(doc_id),"exp":int(time.time())+ttl},separators=(",",":"),sort_keys=True).encode()
    body=base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig=base64.urlsafe_b64encode(hmac.new(_document_access_secret(),body.encode(),hashlib.sha256).digest()).decode().rstrip("=")
    return body+"."+sig

def _verify_document_access_token(token,pid,doc_id):
    try:
        body,supplied=token.split(".",1)
        expected=base64.urlsafe_b64encode(hmac.new(_document_access_secret(),body.encode(),hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(supplied,expected): return False
        p=json.loads(base64.urlsafe_b64decode(body+"="*(-len(body)%4)).decode())
        return int(p["pid"])==int(pid) and int(p["doc"])==int(doc_id) and int(p["exp"])>=int(time.time())
    except Exception: return False

def _db_fetchone(sql,params=()):
    with psycopg.connect(_database_url(),prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql,params); row=cur.fetchone()
            return dict(zip([d.name for d in cur.description],row)) if row else None

async def _storage_direct_download(path):
    base=os.getenv("SUPABASE_URL","").strip().rstrip("/"); key=os.getenv("SUPABASE_SERVICE_ROLE_KEY","").strip() or os.getenv("SUPABASE_KEY","").strip(); bucket=os.getenv("SUPABASE_STORAGE_BUCKET","partner-verification-documents").strip()
    if not base or not key: raise RuntimeError("Supabase Storage configuration is missing")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.get(f"{base}/storage/v1/object/{bucket}/{path}",headers={"Authorization":f"Bearer {key}","apikey":key}) as r:
            data=await r.read()
            if r.status!=200: raise RuntimeError(f"Storage download failed ({r.status})")
            return data

async def _document_bytes(row):
    if row.get("file_data") is not None: return bytes(row["file_data"])
    if row.get("storage_path"):
        try:
            from stage3_partner_verification import _storage_signed_url
            signed=await _storage_signed_url(str(row["storage_path"]),300)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.get(signed) as r:
                    data=await r.read()
                    if r.status==200: return data
        except Exception as exc: logging.warning("Signed document download failed: %s",exc)
        return await _storage_direct_download(str(row["storage_path"]))
    raise RuntimeError("document_file_not_available")

async def _admin_document_proxy(request):
    pid,doc_id=int(request.match_info["id"]),int(request.match_info["doc_id"])
    from stage3_partner_verification import _admin_telegram_id
    init=request.headers.get("X-Telegram-Init-Data","").strip()
    if init: _admin_telegram_id(request,request.app.get("stage3_bot_token"),request.app.get("stage3_admin_id"))
    elif not _verify_document_access_token(request.query.get("access",""),pid,doc_id):
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"document_access_required"}',content_type="application/json")
    row=_db_fetchone("SELECT original_filename,mime_type,storage_path,file_data FROM partner_verification_documents WHERE id=%s AND partner_id=%s",(doc_id,pid))
    if not row: return web.json_response({"ok":False,"error":"document_not_found"},status=404)
    try:
        data=await _document_bytes(row); mime=str(row.get("mime_type") or "application/octet-stream"); filename=str(row.get("original_filename") or "document").replace('"','')
        return web.Response(body=data,content_type=mime,headers={"Content-Disposition":f'inline; filename="{filename}"',"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})
    except Exception as exc:
        logging.exception("Verification document proxy failed")
        return web.json_response({"ok":False,"error":"document_open_failed","details":str(exc)[:500]},status=502)

async def _admin_document_viewer(request):
    pid,doc_id=int(request.match_info["id"]),int(request.match_info["doc_id"])
    token=request.query.get("access","").strip()
    if not _verify_document_access_token(token,pid,doc_id):
        from stage3_partner_verification import _admin_telegram_id
        _admin_telegram_id(request,request.app.get("stage3_bot_token"),request.app.get("stage3_admin_id"))
    row=_db_fetchone("SELECT original_filename,mime_type,storage_path,file_data FROM partner_verification_documents WHERE id=%s AND partner_id=%s",(doc_id,pid))
    if not row: return web.Response(text="Документ не найден",status=404)
    try:
        data=await _document_bytes(row); mime=str(row.get("mime_type") or "application/octet-stream"); filename=str(row.get("original_filename") or "document").replace('"','')
        return web.Response(body=data,content_type=mime,headers={"Content-Disposition":f'inline; filename="{filename}"',"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})
    except Exception as exc:
        logging.exception("Verification document viewer failed")
        return web.Response(text=f"Не удалось открыть документ: {str(exc)[:500]}",status=502,content_type="text/plain")

def _admin_configured_id():
    raw=os.getenv("ADMIN_TELEGRAM_ID","").strip() or os.getenv("ADMIN_ID","").strip()
    if raw:
        try: return int(raw)
        except (TypeError,ValueError): pass
    try: return int(getattr(importlib.import_module("__main__"),"ADMIN_ID",0) or 0)
    except Exception: return 0

def _validate_admin_request(request):
    raw=request.headers.get("X-Telegram-Init-Data","").strip()
    if not raw: raise web.HTTPUnauthorized(text='{"ok":false,"error":"telegram_init_data_required"}',content_type="application/json")
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip() or os.getenv("BOT_TOKEN","").strip()
    try:
        user=validate_telegram_webapp_init_data(raw,token); uid=int(user["id"])
    except (TelegramWebAppAuthError,KeyError,TypeError,ValueError) as exc:
        raise web.HTTPUnauthorized(text=json.dumps({"ok":False,"error":str(exc) or "invalid_telegram_init_data"}),content_type="application/json")
    admin_id=_admin_configured_id()
    if not admin_id: raise web.HTTPForbidden(text=json.dumps({"ok":False,"error":"admin_id_not_configured","your_telegram_id":uid}),content_type="application/json")
    if uid!=admin_id: raise web.HTTPForbidden(text=json.dumps({"ok":False,"error":"admin_access_required","your_telegram_id":uid}),content_type="application/json")
    request["admin_telegram_id"]=uid
    return uid

@web.middleware
async def _admin_auth_middleware(request,handler):
    path=request.path
    if path.startswith("/api/admin/") and not (path.endswith("/viewer") or path.endswith("/proxy") or path.endswith("/open-file")):
        _validate_admin_request(request)
    return await handler(request)

async def _admin_auth_probe(request):
    uid=_validate_admin_request(request)
    return web.json_response({"ok":True,"telegram_id":uid})

def _legacy_document_open(request):
    from stage3_partner_verification import _admin_telegram_id
    admin_id=_admin_telegram_id(request,request.app.get("stage3_bot_token"),request.app.get("stage3_admin_id"))
    pid,doc_id=int(request.match_info["id"]),int(request.match_info["doc_id"])
    if not _db_fetchone("SELECT id FROM partner_verification_documents WHERE id=%s AND partner_id=%s",(doc_id,pid)): return web.json_response({"ok":False,"error":"document_not_found"},status=404)
    token=_make_document_access_token(pid,doc_id); viewer=f"/api/admin/partner-applications/{pid}/documents/{doc_id}/viewer?access={token}"
    return web.json_response({"ok":True,"admin_id":admin_id,"url":viewer,"viewer_url":viewer,"source":"database"})

def _install_ai_first_partner_flow(main, db):
    from ai_first_partner_onboarding import persist_ready_application
    async def _new_process(uid: int, text: str, state):
        user=db.get_user(uid) or {}; lang=user.get("lang","hy"); data=await state.get_data(); history=list(data.get("partner_onboarding_history") or []); pending=data.get("partner_onboarding_pending_field"); previous=data.get("partner_profile") or {}; history.append({"role":"user","content":text})
        from partner_registration_ai import extract, missing_question
        profile=await extract(text,history,db,previous_profile=previous,pending_field=pending)
        merged=dict(previous)
        for k,v in (profile or {}).items():
            if v not in (None,"",[],{}): merged[k]=v
        profile=merged; required=[k for k in ("business_name","city","direction","services") if not profile.get(k)]; profile["missing"]=required; profile["ready"]=not required
        await state.update_data(partner_onboarding_history=history,partner_profile=profile)
        if required:
            question=missing_question(profile,lang); next_field=required[0]; history.append({"role":"assistant","content":question}); await state.update_data(partner_onboarding_pending_field=next_field,partner_onboarding_history=history)
            return {"message":({"hy":"🤖 Ես արդեն հավաքել եմ ձեր ասած տվյալները։ ","ru":"🤖 Я уже собрал данные. ","en":"🤖 I have collected the information. "}.get(lang,"🤖 ")+question),"completed":False,"profile":profile}
        result=persist_ready_application(db,uid,profile); await state.update_data(partner_onboarding_pending_field=None,partner_profile=profile)
        if result["proposal_created"]:
            message={"hy":"✅ Տվյալները պահպանված են։ Ձեր ուղղությունը նոր է կամ դեռ չկա կատալոգում։ Ես այն ուղարկել եմ ադմինիստրատորի հաստատմանը։","ru":"✅ Данные сохранены. Ваше направление новое или пока отсутствует в каталоге. Я отправил его администратору на рассмотрение.","en":"✅ Your data is saved. The direction is new or not yet in the catalogue, so I sent it to the administrator for review."}.get(lang,"Данные сохранены и отправлены администратору.")
        else:
            message={"hy":"✅ Բիզնեսի տվյալները ճանաչեցի և պահպանեցի։ Ուղղությունը ստեղծված է որպես սպասող հայտ։ Հաջորդ քայլը՝ հաստատող փաստաթուղթը բեռնել։","ru":"✅ Данные бизнеса распознаны и сохранены. Направление создано как заявка на проверку. Следующий шаг — загрузить подтверждающий документ.","en":"✅ I recognized and saved the business. The direction is pending review. Next step: upload the verification document."}.get(lang,"Данные сохранены. Загрузите подтверждающий документ.")
        return {"message":message,"completed":True,"profile":profile,**result}
    main._process_partner_onboarding_text=_new_process; main._armenia_ai_first_partner_flow=True

def _bootstrap(app):
    main=importlib.import_module("__main__"); db=getattr(main,"db",None); ai=getattr(main,"ai",None); bot=getattr(main,"bot",None)
    if db is None or ai is None: return
    from platform_schema import ensure_platform_schema; ensure_platform_schema()
    from partner_lifecycle_schema import ensure_partner_lifecycle_schema; ensure_partner_lifecycle_schema()
    if not getattr(main,"_armenia_ai_first_partner_flow",False): _install_ai_first_partner_flow(main,db)
    if not getattr(db,"_armenia_direction_bridge",False):
        original_set=db.set_master_categories
        @wraps(original_set)
        def bridged_set_master_categories(user_id,category_ids):
            result=original_set(user_id,category_ids)
            try:
                from partner_directions_api import ensure_initial_partner_direction
                partner=db.get_partner_by_user(user_id)
                if partner: ensure_initial_partner_direction(partner["id"],user_id)
            except Exception: logging.exception("Partner direction bridge failed")
            return result
        db.set_master_categories=bridged_set_master_categories; db._armenia_direction_bridge=True
    main.api_admin_partner_document_open=_legacy_document_open; main.api_admin_partner_document_open_file=_admin_document_proxy
    from partner_directions_api import register_partner_direction_routes; register_partner_direction_routes(app,db=db,bot=bot)
    from client_api import register_client_routes; register_client_routes(app,ai)
    from admin_ai_api import register_admin_ai_routes; register_admin_ai_routes(app,ai,bot=bot)
    from marketplace_flow_api import register_marketplace_flow_routes; register_marketplace_flow_routes(app)
    if not getattr(app,"_armenia_phase3_registered",False):
        from premium_contact_api import register_premium_contact_routes; register_premium_contact_routes(app)
        from reviews_api import register_reviews_routes; register_reviews_routes(app)
        from support_api import register_support_routes; register_support_routes(app)
        from admin_stats_api import register_admin_stats_routes; register_admin_stats_routes(app); app._armenia_phase3_registered=True
    if not getattr(app,"_armenia_storefront_registered",False):
        from storefront_api import register_storefront_routes; register_storefront_routes(app); app._armenia_storefront_registered=True
    if not getattr(app,"_armenia_admin_auth_registered",False):
        app.middlewares.append(_admin_auth_middleware); app.router.add_get("/api/admin/auth",_admin_auth_probe); app._armenia_admin_auth_registered=True
    if not getattr(app,"_armenia_document_proxy_registered",False):
        app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/proxy",_admin_document_proxy); app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/viewer",_admin_document_viewer); app._armenia_document_proxy_registered=True
    app["platform_bootstrap_ready"]=True

def _application_init(self,*args,**kwargs): _original_application_init(self,*args,**kwargs); _bootstrap(self)
if not getattr(web.Application,"_armenia_platform_bootstrapped",False): web.Application.__init__=_application_init; web.Application._armenia_platform_bootstrapped=True
