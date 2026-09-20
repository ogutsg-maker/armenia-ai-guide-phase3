"""Business and universal partner-application layer for Armenia AI Guide.

Partner -> Business -> Direction -> Subcategory -> Service.
Applications are reviewed before catalogue records become active.
"""
from __future__ import annotations
import json, os
from datetime import date, datetime
from decimal import Decimal
import psycopg
from aiohttp import web
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError

def _db_url():
    v=os.getenv("DATABASE_URL","").strip()
    if not v: raise RuntimeError("DATABASE_URL is not configured")
    return v

def _connect():
    return psycopg.connect(_db_url(), prepare_threshold=None, row_factory=psycopg.rows.dict_row)

def _safe(v):
    if isinstance(v,(datetime,date)): return v.isoformat()
    if isinstance(v,Decimal): return float(v)
    if isinstance(v,dict): return {k:_safe(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)): return [_safe(x) for x in v]
    return v

def _one(sql,p=()):
    with _connect() as c:
        with c.cursor() as cur:
            cur.execute(sql,p); return _safe(cur.fetchone())

def _all(sql,p=()):
    with _connect() as c:
        with c.cursor() as cur:
            cur.execute(sql,p); return _safe(cur.fetchall())

def _exec(sql,p=(),ret=False):
    with _connect() as c:
        with c.cursor() as cur:
            cur.execute(sql,p); row=cur.fetchone() if ret else None
        c.commit(); return _safe(row)

def ensure_business_application_schema():
    _exec("""
    CREATE TABLE IF NOT EXISTS partner_businesses(
      id BIGSERIAL PRIMARY KEY,
      partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      description TEXT,
      status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','pending','suspended','archived')),
      is_default BOOLEAN NOT NULL DEFAULT FALSE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    ALTER TABLE partner_directions ADD COLUMN IF NOT EXISTS business_id BIGINT REFERENCES partner_businesses(id) ON DELETE CASCADE;
    ALTER TABLE services ADD COLUMN IF NOT EXISTS business_id BIGINT REFERENCES partner_businesses(id) ON DELETE CASCADE;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS business_id BIGINT REFERENCES partner_businesses(id) ON DELETE CASCADE;
    ALTER TABLE partner_objects ADD COLUMN IF NOT EXISTS business_id BIGINT REFERENCES partner_businesses(id) ON DELETE CASCADE;
    ALTER TABLE partner_locations ADD COLUMN IF NOT EXISTS business_id BIGINT REFERENCES partner_businesses(id) ON DELETE CASCADE;
    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS business_id BIGINT REFERENCES partner_businesses(id) ON DELETE CASCADE;
    CREATE INDEX IF NOT EXISTS idx_partner_businesses_partner ON partner_businesses(partner_id,status);
    CREATE INDEX IF NOT EXISTS idx_partner_directions_business ON partner_directions(business_id,status);
    CREATE INDEX IF NOT EXISTS idx_services_business ON services(business_id,status);
    CREATE INDEX IF NOT EXISTS idx_partner_documents_business ON partner_verification_documents(business_id,created_at DESC);

    CREATE TABLE IF NOT EXISTS partner_applications(
      id BIGSERIAL PRIMARY KEY,
      partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
      business_id BIGINT REFERENCES partner_businesses(id) ON DELETE SET NULL,
      status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','pending_partner','pending_admin','document_pending','document_under_review','approved','rejected','needs_clarification')),
      business_name TEXT,
      location_marz TEXT,
      location_city TEXT,
      location_village TEXT,
      address TEXT,
      phone TEXT,
      direction_name TEXT,
      master_category_id INT REFERENCES master_categories(id) ON DELETE SET NULL,
      subcategory_name TEXT,
      category_id INT REFERENCES categories(id) ON DELETE SET NULL,
      service_name TEXT,
      price NUMERIC,
      description TEXT,
      object_name TEXT,
      object_id BIGINT,
      document_id BIGINT REFERENCES partner_verification_documents(id) ON DELETE SET NULL,
      ai_reason TEXT,
      admin_note TEXT,
      payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
      reviewed_by BIGINT,
      reviewed_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_partner_applications_admin ON partner_applications(status,created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_partner_applications_partner ON partner_applications(partner_id,status,created_at DESC);
    """)

    # One default business for every existing partner. Existing records remain untouched.
    _exec("""
    INSERT INTO partner_businesses(partner_id,name,description,is_default)
    SELECT p.id, COALESCE(NULLIF(p.business_name,''),'Իմ բիզնեսը'),
           p.business_description, TRUE
    FROM partners p
    WHERE NOT EXISTS(
      SELECT 1 FROM partner_businesses b WHERE b.partner_id=p.id
    )
    """)
    _exec("""
    UPDATE partner_businesses b SET is_default=TRUE, updated_at=NOW()
    WHERE b.id IN (
      SELECT DISTINCT ON(partner_id) id
      FROM partner_businesses
      ORDER BY partner_id, is_default DESC, id
    ) AND NOT EXISTS(
      SELECT 1 FROM partner_businesses x
      WHERE x.partner_id=b.partner_id AND x.is_default=TRUE AND x.id<>b.id
    )
    """)
    # Attach legacy records to the existing/default business.
    _exec("""UPDATE partner_directions pd SET business_id=b.id
             FROM partner_businesses b
             WHERE b.partner_id=pd.partner_id AND b.is_default=TRUE
               AND pd.business_id IS NULL""")
    _exec("""UPDATE services s SET business_id=b.id
             FROM partner_businesses b
             WHERE b.partner_id=s.partner_id AND b.is_default=TRUE
               AND s.business_id IS NULL""")
    _exec("""UPDATE partner_verification_documents d SET business_id=b.id
             FROM partner_businesses b
             WHERE b.partner_id=d.partner_id AND b.is_default=TRUE
               AND d.business_id IS NULL""")
    _exec("""UPDATE partner_objects o SET business_id=b.id
             FROM partner_businesses b
             WHERE b.partner_id=o.partner_id AND b.is_default=TRUE
               AND o.business_id IS NULL""")
    _exec("""UPDATE partner_locations l SET business_id=b.id
             FROM partner_businesses b
             WHERE b.partner_id=l.partner_id AND b.is_default=TRUE
               AND l.business_id IS NULL""")
    _exec("""UPDATE service_direction_requests r SET business_id=b.id
             FROM partner_businesses b
             WHERE b.partner_id=r.partner_id AND b.is_default=TRUE
               AND r.business_id IS NULL""")

def default_business(partner_id:int):
    return _one("""SELECT * FROM partner_businesses
                   WHERE partner_id=%s AND status='active'
                   ORDER BY is_default DESC,id LIMIT 1""",(partner_id,))

def _auth(request):
    raw=request.headers.get("X-Telegram-Init-Data","").strip()
    if not raw: raise web.HTTPUnauthorized(text='{"ok":false,"error":"telegram_init_data_required"}',content_type="application/json")
    try:
        u=validate_telegram_webapp_init_data(raw,request.app.get("business_bot_token") or os.getenv("BOT_TOKEN",""))
        return int(u["id"])
    except (TelegramWebAppAuthError,KeyError,TypeError,ValueError):
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"invalid_telegram_init_data"}',content_type="application/json")

def _admin(request):
    uid=_auth(request); admin=int(request.app.get("business_admin_id") or os.getenv("ADMIN_ID","0") or 0)
    if uid!=admin: raise web.HTTPForbidden(text='{"ok":false,"error":"admin_access_required"}',content_type="application/json")
    return uid

def _partner(uid):
    return _one("SELECT * FROM partners WHERE user_id=%s LIMIT 1",(uid,))

def register_business_application_routes(app, bot_token=None, admin_id=None):
    app["business_bot_token"]=bot_token
    app["business_admin_id"]=admin_id
    ensure_business_application_schema()

    async def businesses(request):
        uid=_auth(request); p=_partner(uid)
        if not p: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        rows=_all("""SELECT * FROM partner_businesses WHERE partner_id=%s
                     ORDER BY is_default DESC,id""",(p["id"],))
        return web.json_response({"ok":True,"businesses":rows,"current":default_business(p["id"])})

    async def create_business(request):
        uid=_auth(request); p=_partner(uid)
        if not p: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        data=await request.json()
        name=str(data.get("name") or "").strip()
        if len(name)<2: return web.json_response({"ok":False,"error":"business_name_required"},status=400)
        row=_exec("""INSERT INTO partner_businesses(partner_id,name,description,is_default)
                     VALUES(%s,%s,%s,FALSE)
                     RETURNING *""",(p["id"],name,str(data.get("description") or "").strip() or None),True)
        return web.json_response({"ok":True,"business":row})

    async def applications(request):
        uid=_auth(request); p=_partner(uid)
        if not p: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        rows=_all("""SELECT a.*,b.name AS business_name,m.name_am AS master_name_am,m.name_ru AS master_name_ru,
                            c.name_am AS category_name_am,c.name_ru AS category_name_ru
                     FROM partner_applications a
                     LEFT JOIN partner_businesses b ON b.id=a.business_id
                     LEFT JOIN master_categories m ON m.id=a.master_category_id
                     LEFT JOIN categories c ON c.id=a.category_id
                     WHERE a.partner_id=%s ORDER BY a.created_at DESC""",(p["id"],))
        return web.json_response({"ok":True,"applications":rows})

    async def admin_applications(request):
        _admin(request)
        rows=_all("""SELECT a.*,p.business_name AS partner_legacy_name,p.user_id,
                            b.name AS business_name_db,
                            m.name_am AS master_name_am,m.name_ru AS master_name_ru,
                            c.name_am AS category_name_am,c.name_ru AS category_name_ru
                     FROM partner_applications a
                     JOIN partners p ON p.id=a.partner_id
                     LEFT JOIN partner_businesses b ON b.id=a.business_id
                     LEFT JOIN master_categories m ON m.id=a.master_category_id
                     LEFT JOIN categories c ON c.id=a.category_id
                     WHERE a.status<>'approved'
                     ORDER BY a.created_at DESC""")
        return web.json_response({"ok":True,"applications":rows})

    async def admin_application_action(request):
        _admin(request); aid=int(request.match_info["application_id"]); data=await request.json()
        action=str(data.get("action") or "").strip()
        a=_one("SELECT * FROM partner_applications WHERE id=%s",(aid,))
        if not a: return web.json_response({"ok":False,"error":"application_not_found"},status=404)
        allowed={"edit","send_to_partner","reject","approve"}
        if action not in allowed: return web.json_response({"ok":False,"error":"invalid_action"},status=400)
        fields={}
        for k in ("business_name","location_marz","location_city","location_village","address","phone",
                  "direction_name","master_category_id","subcategory_name","category_id","service_name",
                  "price","description","object_name","admin_note"):
            if k in data: fields[k]=data[k]
        if action=="edit":
            if not fields: return web.json_response({"ok":True,"application":a})
            sets=", ".join(f"{k}=%s" for k in fields)
            row=_exec(f"UPDATE partner_applications SET {sets},updated_at=NOW() WHERE id=%s RETURNING *",
                      (*fields.values(),aid),True)
            return web.json_response({"ok":True,"application":row})
        if action=="send_to_partner":
            row=_exec("""UPDATE partner_applications SET status='pending_partner',
                         updated_at=NOW() WHERE id=%s RETURNING *""",(aid,),True)
            return web.json_response({"ok":True,"application":row})
        if action=="reject":
            row=_exec("""UPDATE partner_applications SET status='rejected',admin_note=%s,
                         reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW()
                         WHERE id=%s RETURNING *""",(data.get("admin_note"),_auth(request),aid),True)
            return web.json_response({"ok":True,"application":row})
        # approve = materialize the business shell only; catalogue direction/service
        # stays pending until required document review is complete.
        if not a.get("business_id"):
            b=_exec("""INSERT INTO partner_businesses(partner_id,name,description,is_default)
                       VALUES(%s,%s,%s,FALSE) RETURNING id""",
                    (a["partner_id"],a.get("business_name") or "Նոր բիզնես",a.get("description")),True)
            bid=b["id"]
        else: bid=a["business_id"]
        row=_exec("""UPDATE partner_applications SET business_id=%s,status='document_pending',
                     updated_at=NOW() WHERE id=%s RETURNING *""",(bid,aid),True)
        return web.json_response({"ok":True,"application":row})

    app.router.add_get("/api/master/{id}/businesses",businesses)
    app.router.add_post("/api/master/{id}/businesses",create_business)
    app.router.add_get("/api/master/{id}/applications",applications)
    app.router.add_get("/api/admin/partner-applications",admin_applications)
    app.router.add_post("/api/admin/partner-applications/{application_id}/action",admin_application_action)
