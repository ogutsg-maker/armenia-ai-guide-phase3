"""Business and universal partner-application layer for Armenia AI Guide.

Partner -> Business -> Direction -> Subcategory -> Service.
Applications are reviewed before catalogue records become active.
"""
from __future__ import annotations
import json, os, logging
from datetime import date, datetime
from decimal import Decimal
import psycopg
from psycopg.rows import dict_row
from aiohttp import web
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError

logger = logging.getLogger(__name__)

def _db_url():
    v=os.getenv("DATABASE_URL","").strip()
    if not v: raise RuntimeError("DATABASE_URL is not configured")
    return v

def _connect():
    return psycopg.connect(_db_url(), prepare_threshold=None, row_factory=dict_row)

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

class _CatalogDB:
    """Minimal catalog adapter for Admin Classification AI.
    It intentionally exposes read-only catalog methods only.
    """
    def get_all_master_categories(self):
        return _all("""SELECT id,name_am,name_ru,name_en,slug FROM master_categories
                       WHERE is_active=TRUE ORDER BY id""")
    def get_subcategories_by_master(self, master_id):
        return _all("""SELECT id,master_category_id,name_am,name_ru,name_en,slug
                       FROM categories WHERE master_category_id=%s AND is_active=TRUE
                       ORDER BY id""",(master_id,))

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
    ALTER TABLE bookings ADD COLUMN IF NOT EXISTS business_id BIGINT REFERENCES partner_businesses(id) ON DELETE SET NULL;
    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS business_id BIGINT REFERENCES partner_businesses(id) ON DELETE CASCADE;
    CREATE INDEX IF NOT EXISTS idx_partner_businesses_partner ON partner_businesses(partner_id,status);
    ALTER TABLE partner_directions DROP CONSTRAINT IF EXISTS partner_directions_partner_id_master_category_id_key;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_direction_business_master ON partner_directions(business_id,master_category_id);
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
    WHERE p.status IN ('approved','suspended','blocked')
      AND NOT EXISTS(
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
        rows=_all("""SELECT a.*,b.name AS business_name_db,m.name_am AS master_name_am,m.name_ru AS master_name_ru,
                            c.name_am AS category_name_am,c.name_ru AS category_name_ru
                     FROM partner_applications a
                     LEFT JOIN partner_businesses b ON b.id=a.business_id
                     LEFT JOIN master_categories m ON m.id=a.master_category_id
                     LEFT JOIN categories c ON c.id=a.category_id
                     WHERE a.partner_id=%s ORDER BY a.created_at DESC""",(p["id"],))
        # The editable form is driven by the full JSON profile, not only the
        # legacy one-service columns. Normalize every draft here too, because
        # the WebApp may open a saved draft directly from this endpoint.
        normalized=[]
        for row in rows:
            payload=row.get("payload_json") or {}
            if isinstance(payload,str):
                try: payload=json.loads(payload)
                except Exception: payload={}
            if not isinstance(payload,dict): payload={}
            row["payload_json"]=payload
            row["services"]=payload.get("services") if isinstance(payload.get("services"),list) else []
            for key,alts in {
                "business_name":["business_name"],
                "location_marz":["marz","region"],
                "location_city":["city"],
                "location_village":["village"],
                "address":["address"],
                "phone":["phone"],
                "direction_name":["direction","master_category_name"],
                "master_category_id":["master_category_id","ai_master_category_id"],
                "category_id":["category_id","ai_category_id"],
                "description":["description"],
            }.items():
                if row.get(key) in (None,""):
                    for alt in alts:
                        if payload.get(alt) not in (None,""):
                            row[key]=payload.get(alt)
                            break
            normalized.append(row)
        return web.json_response({"ok":True,"applications":normalized})

    async def admin_applications(request):
        _admin(request)
        rows=_all("""SELECT a.*,p.business_name AS partner_legacy_name,p.user_id,
                            b.name AS business_name_db,
                            m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en,
                            c.name_am AS category_name_am,c.name_ru AS category_name_ru,c.name_en AS category_name_en,
                            d.original_filename AS document_filename,d.status AS document_status
                     FROM partner_applications a
                     JOIN partners p ON p.id=a.partner_id
                     LEFT JOIN partner_businesses b ON b.id=a.business_id
                     LEFT JOIN master_categories m ON m.id=a.master_category_id
                     LEFT JOIN categories c ON c.id=a.category_id
                     LEFT JOIN partner_verification_documents d ON d.id=a.document_id
                     WHERE a.status NOT IN ('approved','pending_partner')
                     ORDER BY a.created_at DESC""")

        # The application payload is the authoritative multi-service list.
        # Build a catalogue lookup once and enrich every service with the
        # REAL direction/subdirection names selected by Admin Classification AI.
        catalog_rows=_all("""SELECT c.id AS category_id,c.master_category_id,
                                    m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en,
                                    c.name_am AS category_name_am,c.name_ru AS category_name_ru,c.name_en AS category_name_en
                             FROM categories c
                             JOIN master_categories m ON m.id=c.master_category_id""")
        catalog_by_id={int(x["category_id"]):x for x in catalog_rows if x.get("category_id") is not None}

        for row in rows:
            payload=row.get("payload_json") or {}
            if isinstance(payload,str):
                try: payload=json.loads(payload)
                except Exception: payload={}
            if not isinstance(payload,dict): payload={}
            raw_services=payload.get("services") if isinstance(payload.get("services"),list) else []
            enriched=[]
            for svc in raw_services:
                if not isinstance(svc,dict) or not str(svc.get("name") or svc.get("service_name") or "").strip():
                    continue
                item=dict(svc)
                cid=_safe_int(item.get("matched_subcategory_id") or item.get("subcategory_id") or item.get("category_id"))
                cat=catalog_by_id.get(cid) if cid is not None else None
                item["matched_subcategory_id"]=cid
                item["direction_id"]=_safe_int((cat or {}).get("master_category_id")) or _safe_int(row.get("master_category_id"))
                item["direction_name"]=(cat or {}).get("master_name_am") or (cat or {}).get("master_name_ru") or row.get("master_name_am") or row.get("master_name_ru")
                item["subcategory_name"]=(cat or {}).get("category_name_am") or (cat or {}).get("category_name_ru") or ("" if cid is None else None)
                item["needs_admin_classification"]=cid is None
                enriched.append(item)
            row["payload_json"]=payload
            row["services"]=enriched
            row["catalog_services"]=enriched

        return web.json_response({"ok":True,"applications":rows})

    async def application_update(request):
        uid=_auth(request); p=_partner(uid)
        if not p: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        aid=int(request.match_info["application_id"]); data=await request.json()
        row=_one("SELECT * FROM partner_applications WHERE id=%s AND partner_id=%s",(aid,p["id"]))
        if not row: return web.json_response({"ok":False,"error":"application_not_found"},status=404)

        allowed=("business_name","location_marz","location_city","location_village","address","phone",
                 "direction_name","master_category_id","subcategory_name","category_id",
                 "service_name","price","description","object_name")
        fields={k:data[k] for k in allowed if k in data}

        payload=data.get("payload")
        merged=None
        if isinstance(payload,dict):
            current=row.get("payload_json") or {}
            if isinstance(current,str):
                try: current=json.loads(current)
                except Exception: current={}
            if not isinstance(current,dict): current={}
            merged=dict(current)
            merged.update(payload)

            # Keep the full multi-service list exactly as edited by the partner.
            services=merged.get("services")
            if isinstance(services,list):
                clean=[]
                for svc in services:
                    if not isinstance(svc,dict): continue
                    name=str(svc.get("name") or svc.get("service_name") or "").strip()
                    if not name: continue
                    item=dict(svc)
                    item["name"]=name
                    item["price_type"]=str(item.get("price_type") or "fixed")
                    cid=_safe_int(item.get("matched_subcategory_id") or item.get("subcategory_id") or item.get("category_id"))
                    item["matched_subcategory_id"]=cid
                    clean.append(item)
                merged["services"]=clean
                # The partner UI does not expose catalogue fields, so never
                # erase the AI's internal classification just because the form
                # submitted visible fields with null catalogue IDs.
                if clean:
                    first=clean[0]
                    first_cid=_safe_int(first.get("matched_subcategory_id"))
                    if not fields.get("category_id") and first_cid is not None:
                        fields["category_id"]=first_cid
                    if not fields.get("subcategory_name") and first.get("subcategory_name"):
                        fields["subcategory_name"]=first.get("subcategory_name")
                    if not fields.get("service_name"):
                        fields["service_name"]=first["name"]
                    if fields.get("price") in (None,"") and first.get("price") not in (None,""):
                        fields["price"]=first.get("price")

            # If the partner changed a service name in the visible form,
            # re-run internal catalogue classification before saving. The
            # partner still never sees catalogue IDs or choices.
            if isinstance(merged.get("services"),list):
                needs_reclass=any(
                    isinstance(s,dict) and _safe_int(s.get("matched_subcategory_id")) is None
                    for s in merged.get("services")
                )
                if needs_reclass:
                    try:
                        from partner_registration_ai import classify_profile_catalog
                        classification=await classify_profile_catalog(_CatalogDB(), {
                            "business_name": merged.get("business_name") or row.get("business_name") or "",
                            "description": merged.get("description") or row.get("description") or "",
                            "marz": merged.get("marz") or row.get("location_marz") or "",
                            "city": merged.get("city") or row.get("location_city") or "",
                            "address": merged.get("address") or row.get("address") or "",
                            "services": merged.get("services") or [],
                        })
                        classified=classification.get("services") or []
                        if classified:
                            merged["services"]=classified
                            merged["master_category_id"]=classification.get("master_category_id") or merged.get("master_category_id")
                            merged["ai_master_category_id"]=merged.get("master_category_id")
                            merged["classification_confidence"]=classification.get("confidence",0)
                            merged["classification_ambiguities"]=classification.get("ambiguities") or []
                            merged["classification_needs_review"]=bool(classification.get("needs_review"))
                            fields["payload_json"]=json.dumps(merged,ensure_ascii=False)
                    except Exception:
                        logger.exception("Application service reclassification failed")

            # Preserve the internal master classification from the AI profile.
            internal_mid=_safe_int(merged.get("master_category_id") or merged.get("ai_master_category_id"))
            if internal_mid is not None and not fields.get("master_category_id"):
                fields["master_category_id"]=internal_mid
            if not fields.get("direction_name"):
                fields["direction_name"]=str(merged.get("direction") or merged.get("master_category_name") or "").strip() or None

            fields["payload_json"]=json.dumps(merged,ensure_ascii=False)

        if not fields:
            return web.json_response({"ok":True,"application":row})

        sets=", ".join(f"{k}=%s" for k in fields)
        sets+=", updated_at=NOW()"
        vals=list(fields.values())+[aid]
        if "payload_json" in fields:
            sets=sets.replace("payload_json=%s","payload_json=%s::jsonb")
        try:
            updated=_exec(f"UPDATE partner_applications SET {sets} WHERE id=%s RETURNING *",vals,True)
            return web.json_response({"ok":True,"application":updated})
        except Exception as exc:
            logger.exception("Partner application update failed")
            return web.json_response({"ok":False,"error":"application_update_failed","detail":str(exc)[:500]},status=500)

    async def application_get(request):
        uid=_auth(request); p=_partner(uid)
        if not p: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        aid=int(request.match_info["application_id"])
        row=_one("SELECT * FROM partner_applications WHERE id=%s AND partner_id=%s",(aid,p["id"]))
        if not row: return web.json_response({"ok":False,"error":"application_not_found"},status=404)

        # Always expose one normalized application object. The AI draft keeps
        # the complete profile in payload_json; the legacy one-service columns
        # contain only the first service. The WebApp must not have to guess
        # which representation is authoritative.
        payload=row.get("payload_json") or {}
        if isinstance(payload,str):
            try: payload=json.loads(payload)
            except Exception: payload={}
        if not isinstance(payload,dict): payload={}
        services=payload.get("services") if isinstance(payload.get("services"),list) else []
        normalized=dict(row)
        normalized["payload_json"]=payload
        normalized["services"]=services
        for key,payload_keys in {
            "business_name":["business_name"],
            "location_marz":["marz","region"],
            "location_city":["city"],
            "location_village":["village"],
            "address":["address"],
            "phone":["phone"],
            "direction_name":["direction","master_category_name"],
            "master_category_id":["master_category_id","ai_master_category_id"],
            "description":["description"],
        }.items():
            if normalized.get(key) in (None,""):
                for pk in payload_keys:
                    if payload.get(pk) not in (None,""):
                        normalized[key]=payload.get(pk)
                        break
        return web.json_response({"ok":True,"application":normalized})

    async def application_catalog(request):
        uid=_auth(request); p=_partner(uid)
        if not p: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        rows=_all("""SELECT m.id,m.name_am,m.name_ru,m.name_en,
                            COALESCE(json_agg(json_build_object(
                              'id',c.id,'name_am',c.name_am,'name_ru',c.name_ru,'name_en',c.name_en,'slug',c.slug)
                              ORDER BY c.id) FILTER (WHERE c.id IS NOT NULL),'[]'::json) AS subcategories
                     FROM master_categories m
                     LEFT JOIN categories c ON c.master_category_id=m.id AND c.is_active=TRUE
                     WHERE m.is_active=TRUE
                     GROUP BY m.id,m.name_am,m.name_ru,m.name_en
                     ORDER BY m.id""")
        return web.json_response({"ok":True,"directions":rows})

    async def application_submit(request):
        try:
            uid = _auth(request)
            p = _partner(uid)
            if not p:
                return web.json_response({"ok":False,"error":"partner_not_found"}, status=404)

            aid = int(request.match_info["application_id"])
            a = _one(
                "SELECT * FROM partner_applications WHERE id=%s AND partner_id=%s",
                (aid, p["id"])
            )
            if not a:
                return web.json_response({"ok":False,"error":"application_not_found"}, status=404)

            payload = a.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}

            services = payload.get("services") if isinstance(payload.get("services"), list) else []

            # Partner-facing fields. Catalog classification remains an internal
            # admin task and must never block partner submission.
            required = ("business_name","location_marz","location_city","address","phone")
            missing = [k for k in required if a.get(k) in (None, "")]
            if not services and not a.get("service_name"):
                missing.append("services")
            if missing:
                return web.json_response(
                    {"ok":False,"error":"application_incomplete","fields":missing},
                    status=422
                )

            internal_mid = _safe_int(
                a.get("master_category_id")
                or payload.get("master_category_id")
                or payload.get("ai_master_category_id")
            )

            if not a.get("document_id"):
                return web.json_response({"ok":False,"error":"document_required"}, status=409)

            doc = _one(
                "SELECT id,status FROM partner_verification_documents WHERE id=%s AND partner_id=%s",
                (a["document_id"], p["id"])
            )
            if not doc:
                return web.json_response({"ok":False,"error":"document_not_found"}, status=404)
            if doc["status"] not in ("pending","approved"):
                return web.json_response({"ok":False,"error":"document_not_ready"}, status=409)

            # Keep legacy first-service columns synchronized while payload_json
            # remains the authoritative multi-service record.
            first = next(
                (
                    x for x in services
                    if isinstance(x, dict)
                    and str(x.get("name") or x.get("service_name") or "").strip()
                ),
                None
            )
            if first:
                cid = _safe_int(
                    first.get("matched_subcategory_id")
                    or first.get("subcategory_id")
                    or first.get("category_id")
                )
                _exec(
                    """UPDATE partner_applications
                       SET master_category_id=%s, category_id=%s,
                           subcategory_name=%s, service_name=%s, price=%s,
                           direction_name=COALESCE(direction_name,%s),
                           updated_at=NOW()
                       WHERE id=%s""",
                    (
                        internal_mid,
                        cid,
                        str(first.get("subcategory_name") or "").strip() or None,
                        str(first.get("name") or first.get("service_name") or "").strip()[:300],
                        first.get("price"),
                        str(payload.get("direction") or "").strip() or None,
                        aid,
                    )
                )

            row = _exec(
                """UPDATE partner_applications
                   SET status='pending_admin', updated_at=NOW()
                   WHERE id=%s RETURNING *""",
                (aid,),
                True
            )
            return web.json_response({"ok":True,"application":row})

        except web.HTTPException:
            raise
        except Exception as exc:
            logger.exception("Partner application submit failed")
            return web.json_response(
                {
                    "ok":False,
                    "error":"application_submit_failed",
                    "detail":str(exc)[:500]
                },
                status=500
            )

    async def application_document_upload(request):
        uid=_auth(request); p=_partner(uid)
        if not p: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        aid=int(request.match_info["application_id"])
        a=_one("SELECT * FROM partner_applications WHERE id=%s AND partner_id=%s",(aid,p["id"]))
        if not a: return web.json_response({"ok":False,"error":"application_not_found"},status=404)
        reader=await request.multipart(); file_part=None; document_type="business_document"
        async for part in reader:
            if part.name=="document_type": document_type=(await part.text()).strip()[:80] or document_type
            elif part.name=="file": file_part=part; break
        if file_part is None: return web.json_response({"ok":False,"error":"file_required"},status=400)
        allowed={"image/jpeg":".jpg","image/png":".png","image/webp":".webp","application/pdf":".pdf"}
        mime=(file_part.headers.get("Content-Type") or "").lower()
        if mime not in allowed: return web.json_response({"ok":False,"error":"unsupported_file_type"},status=400)
        data=bytearray()
        while True:
            chunk=await file_part.read_chunk(1024*1024)
            if not chunk: break
            data.extend(chunk)
            if len(data)>10*1024*1024: return web.json_response({"ok":False,"error":"file_too_large"},status=413)
        from stage3_partner_verification import _storage_upload
        import uuid
        original=os.path.basename(file_part.filename or "document")[:180]
        path=f"partners/{p['id']}/applications/{aid}/{uuid.uuid4().hex}{allowed[mime]}"
        try:
            await _storage_upload(path,bytes(data),mime)
            storage_path=path; blob=None
        except Exception:
            storage_path=None; blob=bytes(data)
        doc=_exec("""INSERT INTO partner_verification_documents(
                     partner_id,business_id,document_type,original_filename,storage_path,file_data,mime_type,file_size,status)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING id""",
                  (p["id"],a.get("business_id"),document_type,original,storage_path,blob,mime,len(data)),True)
        # Uploading the document does not submit the application. The partner
        # must explicitly press the final submit button after reviewing the form.
        row=_exec("""UPDATE partner_applications
                     SET document_id=%s,updated_at=NOW()
                     WHERE id=%s RETURNING *""",(doc["id"],aid),True)
        return web.json_response({"ok":True,"application":row,"document_id":doc["id"]})

    async def admin_application_action(request):
        _admin(request); aid=int(request.match_info["application_id"]); data=await request.json()
        action=str(data.get("action") or "").strip()
        a=_one("SELECT * FROM partner_applications WHERE id=%s",(aid,))
        if not a: return web.json_response({"ok":False,"error":"application_not_found"},status=404)
        allowed={"edit","send_to_partner","reject","approve","approve_document","activate"}
        if action not in allowed: return web.json_response({"ok":False,"error":"invalid_action"},status=400)
        fields={}
        for k in ("business_name","location_marz","location_city","location_village","address","phone",
                  "direction_name","master_category_id","subcategory_name","category_id","service_name",
                  "price","description","object_name","admin_note"):
            if k in data: fields[k]=data[k]

        if action=="edit":
            payload=data.get("payload")
            if isinstance(payload,dict):
                current=a.get("payload_json") or {}
                if isinstance(current,str):
                    try: current=json.loads(current)
                    except Exception: current={}
                if not isinstance(current,dict): current={}
                merged=dict(current)
                merged.update(payload)
                services=merged.get("services")
                if isinstance(services,list):
                    clean=[]
                    for svc in services:
                        if not isinstance(svc,dict): continue
                        name=str(svc.get("name") or svc.get("service_name") or "").strip()
                        if not name: continue
                        item=dict(svc)
                        item["name"]=name
                        item["matched_subcategory_id"]=_safe_int(item.get("matched_subcategory_id") or item.get("subcategory_id") or item.get("category_id"))
                        clean.append(item)
                    merged["services"]=clean
                    if clean:
                        first=clean[0]
                        fields.setdefault("master_category_id",_safe_int(first.get("direction_id") or merged.get("master_category_id") or merged.get("ai_master_category_id")))
                        fields.setdefault("category_id",_safe_int(first.get("matched_subcategory_id")))
                        fields.setdefault("direction_name",str(first.get("direction_name") or merged.get("direction") or "").strip() or None)
                        fields.setdefault("subcategory_name",str(first.get("subcategory_name") or "").strip() or None)
                        fields.setdefault("service_name",first.get("name"))
                        fields.setdefault("price",first.get("price"))
                fields["payload_json"]=json.dumps(merged,ensure_ascii=False)

            if not fields: return web.json_response({"ok":True,"application":a})
            sets=", ".join(f"{k}=%s" for k in fields)
            sets+=",updated_at=NOW()"
            vals=list(fields.values())+[aid]
            if "payload_json" in fields:
                sets=sets.replace("payload_json=%s","payload_json=%s::jsonb")
            row=_exec(f"UPDATE partner_applications SET {sets} WHERE id=%s RETURNING *",
                      (*vals,),True)
            return web.json_response({"ok":True,"application":row})
        if action=="send_to_partner":
            note=str(data.get("admin_note") or "").strip()[:3000] or "Խնդրում ենք ուղղել նշված տվյալները և կրկին ուղարկել հայտը."
            row=_exec("""UPDATE partner_applications SET status='pending_partner',
                         admin_note=%s, reviewed_by=%s, reviewed_at=NOW(), updated_at=NOW()
                         WHERE id=%s RETURNING *""",(note,_auth(request),aid),True)
            return web.json_response({"ok":True,"application":row})
        if action=="reject":
            row=_exec("""UPDATE partner_applications SET status='rejected',admin_note=%s,
                         reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW()
                         WHERE id=%s RETURNING *""",(data.get("admin_note"),_auth(request),aid),True)
            return web.json_response({"ok":True,"application":row})
        if action=="approve":
            row=_exec("""UPDATE partner_applications SET status='document_pending',
                         updated_at=NOW() WHERE id=%s RETURNING *""",(aid,),True)
            return web.json_response({"ok":True,"application":row})

        if action=="approve_document":
            if not a.get("document_id"):
                return web.json_response({"ok":False,"error":"document_required"},status=409)
            doc=_one("SELECT id,status FROM partner_verification_documents WHERE id=%s AND partner_id=%s",(a["document_id"],a["partner_id"]))
            if not doc: return web.json_response({"ok":False,"error":"document_not_found"},status=404)
            if doc["status"]!="pending": return web.json_response({"ok":False,"error":"document_not_pending"},status=409)
            admin_id=_admin(request)
            _exec("UPDATE partner_verification_documents SET status='approved',reviewed_by=%s,reviewed_at=NOW(),rejection_reason=NULL WHERE id=%s",(admin_id,doc["id"]))
            row=_exec("""UPDATE partner_applications SET status='document_under_review',reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW()
                         WHERE id=%s RETURNING *""",(admin_id,aid),True)
            return web.json_response({"ok":True,"application":row})

        if not a.get("document_id"):
            return web.json_response({"ok":False,"error":"document_required"},status=409)
        doc=_one("SELECT * FROM partner_verification_documents WHERE id=%s AND partner_id=%s",(a["document_id"],a["partner_id"]))
        if not doc or doc.get("status")!="approved":
            return web.json_response({"ok":False,"error":"document_not_approved"},status=409)

        payload=a.get("payload_json") or {}
        if isinstance(payload,str):
            try: payload=json.loads(payload)
            except Exception: payload={}
        if not isinstance(payload,dict): payload={}
        app_services=payload.get("services") if isinstance(payload.get("services"),list) else []
        app_services=[x for x in app_services if isinstance(x,dict) and str(x.get("name") or x.get("service_name") or "").strip()]
        if not app_services and a.get("service_name"):
            app_services=[{"name":a.get("service_name"),"price":a.get("price"),"matched_subcategory_id":a.get("category_id")}]

        # Activation is the final safety gate: every service must have a real
        # catalogue subcategory selected by AI or corrected by the admin.
        unresolved=[]
        category_ids=[]
        for svc in app_services:
            cid=_safe_int(svc.get("matched_subcategory_id") or svc.get("subcategory_id") or svc.get("category_id"))
            if cid is None:
                unresolved.append(str(svc.get("name") or svc.get("service_name") or "").strip())
            else:
                category_ids.append(cid)
        if unresolved:
            return web.json_response({"ok":False,"error":"services_need_classification","services":unresolved},status=409)

        mid=_safe_int(a.get("master_category_id") or payload.get("master_category_id") or payload.get("ai_master_category_id"))
        if mid is None:
            return web.json_response({"ok":False,"error":"direction_required"},status=409)

        # Verify that every selected subcategory really belongs to the chosen
        # direction. Never create a fake/new catalogue category at activation.
        valid_rows=_all("""SELECT id FROM categories WHERE master_category_id=%s AND id=ANY(%s::int[])""",(mid,list(set(category_ids))))
        valid_ids={_safe_int(x.get("id")) for x in valid_rows}
        invalid=[cid for cid in category_ids if cid not in valid_ids]
        if invalid:
            return web.json_response({"ok":False,"error":"invalid_subcategory_for_direction","category_ids":invalid},status=409)

        if a.get("business_id"):
            bid=a["business_id"]
        else:
            b=_exec("""INSERT INTO partner_businesses(partner_id,name,description,is_default)
                       VALUES(%s,%s,%s,NOT EXISTS(SELECT 1 FROM partner_businesses WHERE partner_id=%s))
                       RETURNING id""",
                    (a["partner_id"],a.get("business_name") or "Նոր բիզնես",a.get("description"),a["partner_id"]),True)
            bid=b["id"]

        pd=_one("""SELECT id FROM partner_directions
                    WHERE partner_id=%s AND business_id=%s AND master_category_id=%s
                    ORDER BY id LIMIT 1""",(a["partner_id"],bid,mid))
        if pd:
            _exec("UPDATE partner_directions SET status='approved',rejection_reason=NULL,updated_at=NOW() WHERE id=%s",(pd["id"],))
            direction_id=pd["id"]
        else:
            pd=_exec("""INSERT INTO partner_directions(partner_id,business_id,master_category_id,status)
                        VALUES(%s,%s,%s,'approved') RETURNING id""",(a["partner_id"],bid,mid),True)
            direction_id=pd["id"]

        for cid in sorted(set(category_ids)):
            _exec("""INSERT INTO partner_direction_categories(partner_direction_id,category_id)
                     VALUES(%s,%s) ON CONFLICT DO NOTHING""",(direction_id,cid))

        _exec("UPDATE partner_verification_documents SET business_id=%s,partner_direction_id=%s,status='approved' WHERE id=%s",(bid,direction_id,a["document_id"]))

        for svc in app_services:
            name=str(svc.get("name") or svc.get("service_name") or "").strip()[:300]
            cid=_safe_int(svc.get("matched_subcategory_id") or svc.get("subcategory_id") or svc.get("category_id"))
            price=svc.get("price")
            try: price=float(price) if price not in (None,"") else None
            except (TypeError,ValueError): price=None
            existing=_one("""SELECT id FROM services
                             WHERE partner_id=%s AND business_id=%s AND name=%s AND status<>'deleted'
                             ORDER BY id DESC LIMIT 1""",(a["partner_id"],bid,name))
            data_json=json.dumps({
                "application_id":aid,"ai_source":True,
                "price_type":svc.get("price_type") or "fixed",
                "matched_subcategory_id":cid,
                "direction_id":direction_id
            },ensure_ascii=False)
            if existing:
                _exec("""UPDATE services SET category_id=%s,name=%s,description=%s,price=%s,status='pending',
                         data_json=%s::jsonb,updated_at=NOW() WHERE id=%s""",
                      (cid,name,a.get("description"),price,data_json,existing["id"]))
            else:
                _exec("""INSERT INTO services(partner_id,business_id,category_id,subcategory_id,name,description,price,status,data_json)
                         VALUES(%s,%s,%s,NULL,%s,%s,%s,'pending',%s::jsonb)""",
                      (a["partner_id"],bid,cid,name,a.get("description"),price,data_json))

        _exec("""UPDATE partner_applications SET business_id=%s,status='approved',reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW()
                 WHERE id=%s""",(bid,_auth(request),aid))
        _exec("UPDATE partners SET status='approved',verification_status='approved',updated_at=NOW() WHERE id=%s",(a["partner_id"],))
        return web.json_response({"ok":True,"application":_one("SELECT * FROM partner_applications WHERE id=%s",(aid,))})


    app.router.add_get("/api/master/{id}/businesses",businesses)
    app.router.add_post("/api/master/{id}/businesses",create_business)
    app.router.add_post("/api/master/{id}/applications/{application_id}",application_update)
    app.router.add_get("/api/master/{id}/application-catalog",application_catalog)
    app.router.add_post("/api/master/{id}/applications/{application_id}/submit",application_submit)
    app.router.add_post("/api/master/{id}/applications/{application_id}/document",application_document_upload)
    app.router.add_get("/api/master/{id}/applications",applications)
    app.router.add_get("/api/master/{id}/applications/{application_id}",application_get)
    app.router.add_get("/api/admin/universal-applications",admin_applications)
    app.router.add_post("/api/admin/partner-applications/{application_id}/action",admin_application_action)
