"""Partner directions: 10 main directions -> subcategories -> documents -> approval."""
from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from datetime import date, datetime

import aiohttp
import psycopg
from aiohttp import web

from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data


def _db_url():
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def _safe(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe(x) for x in v]
    return v


def _fetchall(sql, params=()):
    with psycopg.connect(_db_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description] if cur.description else []
            return [_safe(dict(zip(cols, row))) for row in cur.fetchall()]


def _fetchone(sql, params=()):
    rows = _fetchall(sql, params)
    return rows[0] if rows else None


def _exec(sql, params=(), returning=False):
    # _exec_params_normalized
    if params is None:
        params = ()
    elif not isinstance(params, (tuple, list, dict)):
        params = (params,)
    with psycopg.connect(_db_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if returning:
                row = cur.fetchone()
                cols = [d.name for d in cur.description] if cur.description else []
                result = _safe(dict(zip(cols, row))) if row else None
            else:
                result = None
        conn.commit()
    return result


def ensure_partner_direction_schema():
    """Non-destructive migration. Existing partner/data rows are preserved."""
    _exec("""
    ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
    ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS name_en TEXT;
    ALTER TABLE categories ADD COLUMN IF NOT EXISTS name_en TEXT;
    CREATE TABLE IF NOT EXISTS partner_directions (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        master_category_id INT NOT NULL REFERENCES master_categories(id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('draft','pending','approved','rejected','frozen','deleted')),
        rejection_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(partner_id, master_category_id)
    );
    CREATE TABLE IF NOT EXISTS partner_direction_categories (
        id BIGSERIAL PRIMARY KEY,
        partner_direction_id BIGINT NOT NULL REFERENCES partner_directions(id) ON DELETE CASCADE,
        category_id INT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(partner_direction_id, category_id)
    );
    ALTER TABLE partner_verification_documents
        ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL;
    CREATE INDEX IF NOT EXISTS idx_partner_directions_partner
        ON partner_directions(partner_id, status);
    CREATE INDEX IF NOT EXISTS idx_partner_direction_categories_direction
        ON partner_direction_categories(partner_direction_id);
    CREATE INDEX IF NOT EXISTS idx_partner_verification_documents_direction
        ON partner_verification_documents(partner_direction_id, created_at DESC);

    CREATE TABLE IF NOT EXISTS service_direction_requests (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        requested_master_category_id INT NOT NULL REFERENCES master_categories(id) ON DELETE RESTRICT,
        requested_master_name TEXT,
        requested_service_name TEXT NOT NULL,
        proposed_subcategory_name TEXT,
        description TEXT,
        price NUMERIC,
        reason TEXT,
        status TEXT NOT NULL DEFAULT 'pending_admin',
        admin_note TEXT,
        partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL,
        document_id BIGINT REFERENCES partner_verification_documents(id) ON DELETE SET NULL,
        reviewed_by BIGINT,
        reviewed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS proposed_subcategory_name TEXT;
    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL;
    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS document_id BIGINT REFERENCES partner_verification_documents(id) ON DELETE SET NULL;
    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    CREATE INDEX IF NOT EXISTS idx_service_direction_requests_partner
        ON service_direction_requests(partner_id, status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_service_direction_requests_status
        ON service_direction_requests(status, created_at DESC);
    """)

    # Backfill the current legacy master_skills into partner directions.
    # For an already approved partner, existing selected directions become approved.
    _exec("""
    INSERT INTO partner_directions(partner_id, master_category_id, status)
    SELECT DISTINCT p.id, c.master_category_id,
           CASE WHEN p.status='approved' AND p.verification_status='approved'
                THEN 'approved' ELSE 'pending' END
    FROM partners p
    JOIN master_skills ms ON ms.user_id=p.user_id AND ms.is_active=TRUE
    JOIN categories c ON c.id=ms.category_id
    WHERE c.master_category_id IS NOT NULL
    ON CONFLICT(partner_id, master_category_id) DO NOTHING
    """)
    _exec("""
    INSERT INTO partner_direction_categories(partner_direction_id, category_id)
    SELECT pd.id, ms.category_id
    FROM partner_directions pd
    JOIN partners p ON p.id=pd.partner_id
    JOIN master_skills ms ON ms.user_id=p.user_id AND ms.is_active=TRUE
    JOIN categories c ON c.id=ms.category_id AND c.master_category_id=pd.master_category_id
    ON CONFLICT(partner_direction_id, category_id) DO NOTHING
    """)
    # The current project already has a single verified document for the initial direction.
    _exec("""
    UPDATE partner_verification_documents d
       SET partner_direction_id = x.direction_id
      FROM (
        SELECT d2.id AS doc_id, MIN(pd.id) AS direction_id
        FROM partner_verification_documents d2
        JOIN partner_directions pd ON pd.partner_id=d2.partner_id
        WHERE d2.partner_direction_id IS NULL
        GROUP BY d2.id
      ) x
     WHERE d.id=x.doc_id AND d.partner_direction_id IS NULL
    """)


def ensure_initial_partner_direction(partner_id: int, user_id: int):
    """Create the registration direction from the already-selected master_skills."""
    ensure_partner_direction_schema()
    rows = _fetchall("""
        SELECT DISTINCT c.master_category_id
        FROM master_skills ms
        JOIN categories c ON c.id=ms.category_id
        WHERE ms.user_id=%s AND ms.is_active=TRUE AND c.master_category_id IS NOT NULL
    """, (user_id,))
    for row in rows:
        _exec("""
            INSERT INTO partner_directions(partner_id, master_category_id, status)
            VALUES(%s,%s,'pending')
            ON CONFLICT(partner_id, master_category_id) DO NOTHING
        """, (partner_id, row["master_category_id"]))
        pd = _fetchone("SELECT id FROM partner_directions WHERE partner_id=%s AND master_category_id=%s", (partner_id, row["master_category_id"]))
        if pd:
            _exec("""
                INSERT INTO partner_direction_categories(partner_direction_id, category_id)
                SELECT %s, ms.category_id
                FROM master_skills ms
                JOIN categories c ON c.id=ms.category_id
                WHERE ms.user_id=%s AND ms.is_active=TRUE AND c.master_category_id=%s
                ON CONFLICT DO NOTHING
            """, (pd["id"], user_id, row["master_category_id"]))


def _partner(uid):
    return _fetchone("SELECT * FROM partners WHERE user_id=%s LIMIT 1", (uid,))


def _auth_admin(request):
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"telegram_init_data_required"}', content_type="application/json")
    try:
        user = validate_telegram_webapp_init_data(raw, request.app.get("stage3_bot_token") or os.getenv("BOT_TOKEN", ""))
        uid = int(user["id"])
    except (TelegramWebAppAuthError, KeyError, TypeError, ValueError) as exc:
        raise web.HTTPUnauthorized(text=json.dumps({"ok":False,"error":str(exc)}), content_type="application/json")
    admin_id = int(request.app.get("stage3_admin_id") or os.getenv("ADMIN_ID", "0") or 0)
    if uid != admin_id:
        raise web.HTTPForbidden(text='{"ok":false,"error":"admin_access_required"}', content_type="application/json")
    return uid


def _direction_payload(pd):
    return pd


def _catalog_for_partner(partner_id, business_id=None):
    masters = _fetchall("SELECT id,name_am,name_ru,slug,is_active FROM master_categories ORDER BY id")
    rows = _fetchall("""
        SELECT pd.id direction_id,pd.master_category_id,pd.status,pd.rejection_reason,
               m.name_am,m.name_ru,m.slug,
               COALESCE((SELECT COUNT(*) FROM partner_verification_documents d WHERE d.partner_direction_id=pd.id),0) document_count
        FROM partner_directions pd
        JOIN master_categories m ON m.id=pd.master_category_id
        WHERE pd.partner_id=%s AND pd.status<>'deleted' AND (%s IS NULL OR pd.business_id=%s)
        ORDER BY m.id
    """, (partner_id,business_id,business_id))
    selected = _fetchall("""
        SELECT pdc.partner_direction_id, c.id,c.master_category_id,c.name_am,c.name_ru,c.slug,c.is_active
        FROM partner_direction_categories pdc
        JOIN categories c ON c.id=pdc.category_id
        WHERE pdc.partner_direction_id IN (SELECT id FROM partner_directions WHERE partner_id=%s)
        ORDER BY c.id
    """, (partner_id,))
    by_direction = {}
    for c in selected:
        by_direction.setdefault(c["partner_direction_id"], []).append(c)
    own = {r["master_category_id"]: r for r in rows}
    result=[]
    for m in masters:
        r=dict(m)
        pd=own.get(m["id"])
        r.update({
            "direction_id": pd["direction_id"] if pd else None,
            "status": pd["status"] if pd else "not_added",
            "rejection_reason": pd["rejection_reason"] if pd else None,
            "subcategories": by_direction.get(pd["direction_id"], []) if pd else [],
            "catalog_subcategories": _fetchall("""
                SELECT id,master_category_id,name_am,name_ru,slug,is_active
                FROM categories WHERE master_category_id=%s AND is_active=TRUE ORDER BY id
            """, (m["id"],)),
        })
        result.append(r)
    return result


async def _upload_direction_document(request, partner, direction_id):
    from stage3_partner_verification import _storage_upload
    pd = _fetchone("SELECT * FROM partner_directions WHERE id=%s AND partner_id=%s", (direction_id, partner["id"]))
    if not pd:
        return web.json_response({"ok":False,"error":"partner_direction_not_found"}, status=404)
    if pd["status"] in ("approved", "frozen"):
        return web.json_response({"ok":False,"error":"direction_already_approved"}, status=400)
    reader = await request.multipart()
    document_type="business_document"
    file_part=None
    async for part in reader:
        if part.name=="document_type": document_type=(await part.text()).strip()[:80] or document_type
        elif part.name=="file": file_part=part; break
    if file_part is None:
        return web.json_response({"ok":False,"error":"file_required"},status=400)
    allowed={"image/jpeg":".jpg","image/png":".png","image/webp":".webp","application/pdf":".pdf"}
    mime=(file_part.headers.get("Content-Type") or "application/octet-stream").lower()
    if mime not in allowed:
        return web.json_response({"ok":False,"error":"unsupported_file_type"},status=400)
    data=bytearray()
    while True:
        chunk=await file_part.read_chunk(1024*1024)
        if not chunk: break
        data.extend(chunk)
        if len(data)>10*1024*1024:
            return web.json_response({"ok":False,"error":"file_too_large"},status=413)
    if not data:
        return web.json_response({"ok":False,"error":"empty_file"},status=400)
    original=os.path.basename(file_part.filename or "document")[:180]
    path=f"partners/{partner['id']}/directions/{direction_id}/{uuid.uuid4().hex}{allowed[mime]}"
    storage_ok=True
    try:
        await _storage_upload(path, bytes(data), mime)
    except Exception:
        storage_ok=False
    if storage_ok:
        doc=_exec("""
          INSERT INTO partner_verification_documents(partner_id,partner_direction_id,document_type,original_filename,storage_path,file_data,mime_type,file_size,status)
          VALUES(%s,%s,%s,%s,%s,NULL,%s,%s,'pending') RETURNING id,partner_direction_id,document_type,original_filename,mime_type,file_size,status,created_at
        """,(partner["id"],direction_id,document_type,original,path,mime,len(data)),True)
    else:
        doc=_exec("""
          INSERT INTO partner_verification_documents(partner_id,partner_direction_id,document_type,original_filename,storage_path,file_data,mime_type,file_size,status)
          VALUES(%s,%s,%s,%s,NULL,%s,%s,%s,'pending') RETURNING id,partner_direction_id,document_type,original_filename,mime_type,file_size,status,created_at
        """,(partner["id"],direction_id,document_type,original,bytes(data),mime,len(data)),True)
    _exec("UPDATE partner_directions SET status='pending', rejection_reason=NULL, updated_at=NOW() WHERE id=%s",(direction_id,))
    _exec("""UPDATE service_direction_requests
              SET status='document_under_review', document_id=%s, updated_at=NOW()
              WHERE partner_direction_id=%s AND status='document_pending'""",
          (doc["id"], direction_id))
    return web.json_response({"ok":True,"document":doc,"direction_id":direction_id,"status":"pending"})


def register_partner_direction_routes(app, db=None, bot=None):
    app["partner_direction_bot"] = bot
    ensure_partner_direction_schema()

    async def directions(request):
        uid=int(request.match_info["id"]); partner=_partner(uid)
        if not partner: return web.json_response({"ok":False,"error":"partner_registration_required"},status=404)
        raw=str(request.headers.get("X-Business-Id") or "").strip()
        business_id=int(raw) if raw.isdigit() else None
        if business_id and not _fetchone("SELECT id FROM partner_businesses WHERE id=%s AND partner_id=%s AND status='active'",(business_id,partner["id"])):
            business_id=None
        if business_id is None:
            b=_fetchone("SELECT id FROM partner_businesses WHERE partner_id=%s AND status='active' ORDER BY is_default DESC,id LIMIT 1",(partner["id"],))
            business_id=b["id"] if b else None
        return web.json_response({"ok":True,"directions":_catalog_for_partner(partner["id"],business_id)})

    async def add_direction(request):
        uid=int(request.match_info["id"]); partner=_partner(uid)
        if not partner: return web.json_response({"ok":False,"error":"partner_registration_required"},status=404)
        if partner.get("status") not in ("approved",): return web.json_response({"ok":False,"error":"partner_not_approved"},status=403)
        data=await request.json(); mid=int(data.get("master_category_id") or 0)
        if not mid: return web.json_response({"ok":False,"error":"master_category_required"},status=400)
        if not _fetchone("SELECT id FROM master_categories WHERE id=%s",(mid,)): return web.json_response({"ok":False,"error":"master_category_not_found"},status=404)
        pd=_fetchone("""INSERT INTO partner_directions(partner_id,master_category_id,status) VALUES(%s,%s,'pending') ON CONFLICT(partner_id,master_category_id) DO UPDATE SET status=CASE WHEN partner_directions.status='rejected' THEN 'pending' ELSE partner_directions.status END, updated_at=NOW() RETURNING *""",(partner["id"],mid))
        ids=data.get("category_ids") or []
        valid=_fetchall("SELECT id FROM categories WHERE master_category_id=%s AND is_active=TRUE AND id=ANY(%s)",(mid,[int(x) for x in ids])) if ids else []
        if ids and len(valid)!=len(set(int(x) for x in ids)):
            return web.json_response({"ok":False,"error":"invalid_subcategory_for_direction"},status=400)
        if ids:
            for row in valid:
                _exec("INSERT INTO partner_direction_categories(partner_direction_id,category_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(pd["id"],row["id"]))
        return web.json_response({"ok":True,"direction":_fetchone("SELECT * FROM partner_directions WHERE id=%s",(pd["id"],))})

    async def direction_document(request):
        uid=int(request.match_info["id"]); partner=_partner(uid)
        if not partner: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        return await _upload_direction_document(request,partner,int(request.match_info["direction_id"]))

    async def service_direction_requests(request):
        uid=int(request.match_info["id"]); partner=_partner(uid)
        if not partner: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        rows=_fetchall("""SELECT r.id,r.requested_master_category_id,r.requested_master_name,
                   r.requested_service_name,r.proposed_subcategory_name,r.description,r.price,r.reason,
                   r.status,r.admin_note,r.partner_direction_id,r.document_id,r.created_at,r.updated_at,
                   m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en,
                   pd.status AS direction_status
            FROM service_direction_requests r
            JOIN master_categories m ON m.id=r.requested_master_category_id
            LEFT JOIN partner_directions pd ON pd.id=r.partner_direction_id
            WHERE r.partner_id=%s AND r.status<>'approved' ORDER BY r.created_at DESC""",(partner["id"],))
        return web.json_response({"ok":True,"requests":rows})

    async def service_direction_request_upload(request):
        uid=int(request.match_info["id"]); partner=_partner(uid)
        if not partner: return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        rid=int(request.match_info["request_id"])
        row=_fetchone("""SELECT r.*,pd.id AS direction_id,pd.status AS direction_status
                         FROM service_direction_requests r
                         LEFT JOIN partner_directions pd ON pd.id=r.partner_direction_id
                         WHERE r.id=%s AND r.partner_id=%s""",(rid,partner["id"]))
        if not row: return web.json_response({"ok":False,"error":"direction_request_not_found"},status=404)
        if row["status"]!="document_pending" or not row.get("direction_id"):
            return web.json_response({"ok":False,"error":"document_upload_not_requested"},status=400)
        return await _upload_direction_document(request,partner,int(row["direction_id"]))

    async def admin_partner_directions(request):
        _auth_admin(request); pid=int(request.match_info["id"])
        directions=_fetchall("""
          SELECT pd.*,m.name_am,m.name_ru,m.slug,
                 COALESCE(json_agg(DISTINCT jsonb_build_object('id',c.id,'name_am',c.name_am,'name_ru',c.name_ru,'slug',c.slug)) FILTER (WHERE c.id IS NOT NULL),'[]'::json) subcategories,
                 COALESCE((SELECT COUNT(*) FROM partner_verification_documents d WHERE d.partner_direction_id=pd.id),0) document_count
          FROM partner_directions pd JOIN master_categories m ON m.id=pd.master_category_id
          LEFT JOIN partner_direction_categories pdc ON pdc.partner_direction_id=pd.id
          LEFT JOIN categories c ON c.id=pdc.category_id
          WHERE pd.partner_id=%s GROUP BY pd.id,m.id ORDER BY m.id
        """,(pid,))
        services=_fetchall("""
          SELECT s.*,c.name_am category_name_am,c.name_ru category_name_ru,m.name_am master_name_am,m.name_ru master_name_ru
          FROM services s LEFT JOIN categories c ON c.id=s.category_id LEFT JOIN master_categories m ON m.id=c.master_category_id
          WHERE s.partner_id=%s AND s.status<>'deleted' ORDER BY s.id DESC
        """,(pid,))
        docs=_fetchall("SELECT id,partner_direction_id,document_type,original_filename,mime_type,file_size,status,rejection_reason,created_at,reviewed_at FROM partner_verification_documents WHERE partner_id=%s ORDER BY created_at DESC",(pid,))
        return web.json_response({"ok":True,"directions":directions,"services":services,"documents":docs})

    async def admin_direction_action(request):
        admin_id=_auth_admin(request); did=int(request.match_info["id"]); data=await request.json() if request.can_read_body else {}; action=str(data.get("action") or "").lower()
        pd=_fetchone("SELECT * FROM partner_directions WHERE id=%s",(did,))
        if not pd: return web.json_response({"ok":False,"error":"direction_not_found"},status=404)
        if action=="approve":
            pending=_fetchone("SELECT id FROM partner_verification_documents WHERE partner_direction_id=%s AND status='pending' ORDER BY created_at DESC LIMIT 1",(did,))
            if not pending: return web.json_response({"ok":False,"error":"direction_document_required"},status=400)
            _exec("UPDATE partner_directions SET status='approved',rejection_reason=NULL,updated_at=NOW() WHERE id=%s",(did))
            _exec("UPDATE partner_verification_documents SET status='approved',reviewed_by=%s,reviewed_at=NOW(),rejection_reason=NULL WHERE partner_direction_id=%s AND status='pending'",(admin_id,did))
            # Services created by the AI onboarding stay pending until their direction and document are approved.
            # Once the direction is approved, publish only services belonging to this approved direction.
            _exec("""UPDATE services SET status='approved',updated_at=NOW()
                     WHERE partner_id=%s AND category_id IN
                       (SELECT category_id FROM partner_direction_categories WHERE partner_direction_id=%s)
                       AND status='pending'""",(pd["partner_id"],did))
        elif action=="reject":
            reason=str(data.get("reason") or "Մերժվել է ադմինիստրատորի կողմից")[:1000]
            _exec("UPDATE partner_directions SET status='rejected',rejection_reason=%s,updated_at=NOW() WHERE id=%s",(reason,did))
            _exec("UPDATE partner_verification_documents SET status='rejected',rejection_reason=%s,reviewed_by=%s,reviewed_at=NOW() WHERE partner_direction_id=%s AND status='pending'",(reason,admin_id,did))
        elif action=="freeze":
            _exec("UPDATE partner_directions SET status='frozen',updated_at=NOW() WHERE id=%s",(did))
        elif action=="activate":
            _exec("UPDATE partner_directions SET status='approved',updated_at=NOW() WHERE id=%s",(did))
        elif action=="delete":
            count=_fetchone("SELECT COUNT(*) n FROM services s JOIN categories c ON c.id=s.category_id WHERE s.partner_id=%s AND c.master_category_id=%s AND s.status<>'deleted'",(pd["partner_id"],pd["master_category_id"]))
            if int(count["n"] or 0)>0: return web.json_response({"ok":False,"error":"direction_has_services"},status=400)
            _exec("DELETE FROM partner_directions WHERE id=%s",(did))
        else: return web.json_response({"ok":False,"error":"unknown_action"},status=400)
        current=_fetchone("SELECT pd.*,p.user_id,m.name_am,m.name_ru FROM partner_directions pd JOIN partners p ON p.id=pd.partner_id JOIN master_categories m ON m.id=pd.master_category_id WHERE pd.id=%s",(did,)) if action!='delete' else None
        if current and request.app.get('partner_direction_bot') and action in ('approve','reject'):
            try:
                if action=='approve': text=f"✅ Ձեր ուղղությունը հաստատված է։\n\n{current.get('name_am') or current.get('name_ru')}"
                else: text=f"📝 Ձեր ուղղությունը մերժվել է։\n\nՊատճառ՝ {current.get('rejection_reason') or 'Ճշտման անհրաժեշտություն'}"
                await request.app['partner_direction_bot'].send_message(int(current['user_id']),text)
            except Exception:
                pass
        return web.json_response({"ok":True,"direction":current})

    async def admin_directions_tree(request):
        _auth_admin(request)
        masters=_fetchall("SELECT id,name_am,name_ru,name_en,slug,is_active,commission_type,commission_value FROM master_categories ORDER BY id")
        subs=_fetchall("SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,c.is_active,c.commission_type,c.commission_value,COALESCE(cs.bank_commission_type,'none') bank_commission_type,COALESCE(cs.bank_commission_value,0) bank_commission_value,COALESCE(cs.cancellation_policy,'no_refund') cancellation_policy,COALESCE(cs.premium_contact_enabled,FALSE) premium_contact_enabled,COALESCE(cs.premium_contact_fee,0) premium_contact_fee,COALESCE(cs.premium_disclosure_scope,'none') premium_disclosure_scope,COALESCE(cs.contact_reveal_after_booking,TRUE) contact_reveal_after_booking FROM categories c LEFT JOIN category_settings cs ON cs.category_id=c.id ORDER BY c.master_category_id,c.id")
        by={}
        for c in subs: by.setdefault(c['master_category_id'],[]).append(c)
        for m in masters: m['subcategories']=by.get(m['id'],[])
        return web.json_response({'ok':True,'directions':masters})

    async def admin_category_settings_action(request):
        _auth_admin(request)
        cid=int(request.match_info["id"])
        data=await request.json() if request.can_read_body else {}
        category=_fetchone("SELECT id,master_category_id,name_am,name_ru FROM categories WHERE id=%s",(cid,))
        if not category:
            return web.json_response({"ok":False,"error":"category_not_found"},status=404)

        # Keep tariff settings in the dedicated category_settings table.
        _exec("""
        CREATE TABLE IF NOT EXISTS category_settings (
            category_id INT PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
            bank_commission_type TEXT NOT NULL DEFAULT 'none',
            bank_commission_value NUMERIC NOT NULL DEFAULT 0,
            cancellation_policy TEXT NOT NULL DEFAULT 'no_refund',
            premium_contact_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            premium_contact_fee NUMERIC NOT NULL DEFAULT 0,
            premium_disclosure_scope TEXT NOT NULL DEFAULT 'none',
            contact_reveal_after_booking BOOLEAN NOT NULL DEFAULT TRUE
        )
        """)
        _exec("""
        ALTER TABLE category_settings
          ADD COLUMN IF NOT EXISTS bank_commission_type TEXT NOT NULL DEFAULT 'none',
          ADD COLUMN IF NOT EXISTS bank_commission_value NUMERIC NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS cancellation_policy TEXT NOT NULL DEFAULT 'no_refund',
          ADD COLUMN IF NOT EXISTS premium_contact_enabled BOOLEAN NOT NULL DEFAULT FALSE,
          ADD COLUMN IF NOT EXISTS premium_contact_fee NUMERIC NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS premium_disclosure_scope TEXT NOT NULL DEFAULT 'none',
          ADD COLUMN IF NOT EXISTS contact_reveal_after_booking BOOLEAN NOT NULL DEFAULT TRUE
        """)

        fields={}
        ct=str(data.get("commission_type") or "").strip()
        if ct in ("inside","on_top","fixed"): fields["commission_type"]=ct
        if "commission_value" in data:
            try: fields["commission_value"]=max(0.0,float(data["commission_value"]))
            except (TypeError,ValueError): pass

        bct=str(data.get("bank_commission_type") or "").strip()
        if bct in ("none","percent","fixed"): fields["bank_commission_type"]=bct
        if "bank_commission_value" in data:
            try: fields["bank_commission_value"]=max(0.0,float(data["bank_commission_value"]))
            except (TypeError,ValueError): pass

        cp=str(data.get("cancellation_policy") or "").strip()
        if cp in ("full_refund","half_refund","no_refund"): fields["cancellation_policy"]=cp

        if "premium_contact_enabled" in data:
            fields["premium_contact_enabled"]=bool(data["premium_contact_enabled"])
        if "premium_contact_fee" in data:
            try: fields["premium_contact_fee"]=max(0.0,float(data["premium_contact_fee"]))
            except (TypeError,ValueError): pass

        ds=str(data.get("premium_disclosure_scope") or "").strip()
        if ds in ("none","name_district_type","name_district","limited"): fields["premium_disclosure_scope"]=ds
        if "contact_reveal_after_booking" in data:
            fields["contact_reveal_after_booking"]=bool(data["contact_reveal_after_booking"])

        if not fields:
            return web.json_response({"ok":False,"error":"no_fields"},status=400)

        tariff_fields={"commission_type","commission_value"}
        category_fields={k:v for k,v in fields.items() if k in tariff_fields}
        settings_fields={k:v for k,v in fields.items() if k not in tariff_fields}

        if category_fields:
            sets=", ".join(f"{k}=%s" for k in category_fields)
            _exec(f"UPDATE categories SET {sets} WHERE id=%s",(*category_fields.values(),cid))

        if settings_fields:
            existing=_fetchone("SELECT category_id FROM category_settings WHERE category_id=%s",(cid,))
            if existing:
                sets=", ".join(f"{k}=%s" for k in settings_fields)
                _exec(f"UPDATE category_settings SET {sets} WHERE category_id=%s",(*settings_fields.values(),cid))
            else:
                cols=["category_id",*settings_fields.keys()]
                vals=[cid,*settings_fields.values()]
                marks=",".join(["%s"]*len(vals))
                _exec(f"INSERT INTO category_settings ({','.join(cols)}) VALUES ({marks})",tuple(vals))

        result=_fetchone("""
            SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,c.is_active,
                   c.commission_type,c.commission_value,
                   COALESCE(cs.bank_commission_type,'none') bank_commission_type,
                   COALESCE(cs.bank_commission_value,0) bank_commission_value,
                   COALESCE(cs.cancellation_policy,'no_refund') cancellation_policy,
                   COALESCE(cs.premium_contact_enabled,FALSE) premium_contact_enabled,
                   COALESCE(cs.premium_contact_fee,0) premium_contact_fee,
                   COALESCE(cs.premium_disclosure_scope,'none') premium_disclosure_scope,
                   COALESCE(cs.contact_reveal_after_booking,TRUE) contact_reveal_after_booking
            FROM categories c LEFT JOIN category_settings cs ON cs.category_id=c.id
            WHERE c.id=%s
        """,(cid,))
        return web.json_response({"ok":True,"category":result})

    async def admin_master_direction_action(request):
        admin_id=_auth_admin(request); mid=int(request.match_info['id']); data=await request.json() if request.can_read_body else {}; action=str(data.get('action') or '').lower()
        m=_fetchone('SELECT * FROM master_categories WHERE id=%s',(mid,))
        if not m:return web.json_response({'ok':False,'error':'master_direction_not_found'},status=404)
        if action=='freeze': _exec("UPDATE master_categories SET is_active=FALSE WHERE id=%s",(mid,))
        elif action=='activate': _exec("UPDATE master_categories SET is_active=TRUE WHERE id=%s",(mid,))
        elif action=='edit':
            fields={k:data[k] for k in ('name_am','name_ru','name_en','slug') if k in data and str(data[k]).strip()}
            # Direction-level DEFAULT tariff ("initial settings"). Applied to
            # every service under this direction unless overridden lower down.
            if str(data.get('commission_type') or '').strip() in ('inside','on_top','fixed'):
                fields['commission_type']=str(data['commission_type']).strip()
            if 'commission_value' in data:
                try:fields['commission_value']=max(0.0,float(data['commission_value']))
                except (TypeError,ValueError):pass
            if not fields:return web.json_response({'ok':False,'error':'no_fields'},status=400)
            sets=', '.join(f'{k}=%s' for k in fields); _exec(f'UPDATE master_categories SET {sets} WHERE id=%s',(*fields.values(),mid))
        elif action=='delete':
            used=_fetchone('SELECT COUNT(*) n FROM categories WHERE master_category_id=%s',(mid,))
            if int(used['n'] or 0)>0:return web.json_response({'ok':False,'error':'direction_has_subcategories','message':'Сначала удалите или перенесите подкатегории.'},status=400)
            _exec('DELETE FROM master_categories WHERE id=%s',(mid,))
        else:return web.json_response({'ok':False,'error':'unknown_action'},status=400)
        return web.json_response({'ok':True,'admin_id':admin_id,'direction':_fetchone('SELECT * FROM master_categories WHERE id=%s',(mid,)) if action!='delete' else None})

    async def admin_partner_settings(request):
        _auth_admin(request); pid=int(request.match_info["id"]); data=await request.json()
        partner=_fetchone("SELECT id FROM partners WHERE id=%s",(pid,))
        if not partner:return web.json_response({"ok":False,"error":"partner_not_found"},status=404)
        fields={k:data[k] for k in ("business_name","business_description","contact_sharing_enabled","premium_contact_sharing_enabled") if k in data}
        if not fields:return web.json_response({"ok":False,"error":"no_fields"},status=400)
        sets=', '.join(f'{k}=%s' for k in fields);_exec(f"UPDATE partners SET {sets},updated_at=NOW() WHERE id=%s",(*fields.values(),pid))
        return web.json_response({"ok":True,"partner":_fetchone("SELECT * FROM partners WHERE id=%s",(pid,))})

    async def admin_service_action(request):
        _auth_admin(request); sid=int(request.match_info["id"]); data=await request.json() if request.can_read_body else {}; action=str(data.get("action") or "").lower()
        service=_fetchone("SELECT * FROM services WHERE id=%s",(sid,))
        if not service:return web.json_response({"ok":False,"error":"service_not_found"},status=404)
        if action in ("freeze","activate"):
            _exec("UPDATE services SET status=%s,updated_at=NOW() WHERE id=%s",('frozen' if action=='freeze' else 'approved',sid))
        elif action=="delete":
            _exec("UPDATE services SET status='deleted',updated_at=NOW() WHERE id=%s",(sid,))
        elif action=="edit":
            allowed={}
            if "name" in data or "service_name" in data: allowed["name"]=data.get("name",data.get("service_name"))
            if "description" in data: allowed["description"]=data.get("description")
            if "price" in data or "base_price" in data: allowed["price"]=data.get("price",data.get("base_price"))
            if "duration_minutes" in data: allowed["duration_minutes"]=data.get("duration_minutes")
            # Per-service tariff OVERRIDE. Send commission_type='' (or null) to
            # clear the override and fall back to subcategory/direction default.
            if "commission_type" in data:
                ct=str(data.get("commission_type") or "").strip()
                allowed["commission_type"]=ct if ct in ("inside","on_top","fixed") else None
            if "commission_value" in data:
                cv=data.get("commission_value")
                if cv in (None,""):
                    allowed["commission_value"]=None
                else:
                    try:allowed["commission_value"]=max(0.0,float(cv))
                    except (TypeError,ValueError):pass
            if not allowed:return web.json_response({"ok":False,"error":"no_fields"},status=400)
            sets=', '.join(f"{k}=%s" for k in allowed); _exec(f"UPDATE services SET {sets},updated_at=NOW() WHERE id=%s",(*allowed.values(),sid))
        else:return web.json_response({"ok":False,"error":"unknown_action"},status=400)
        return web.json_response({"ok":True,"service":_fetchone("SELECT * FROM services WHERE id=%s",(sid,)) if action!='delete' else None})

    async def admin_subcategory_proposals(request):
        _auth_admin(request)
        rows=_fetchall("""
            SELECT sp.id,sp.partner_id,sp.master_category_id,sp.proposed_name,
                   sp.requested_service_name,sp.description,sp.price,sp.status,
                   sp.admin_note,sp.created_at,
                   p.business_name,m.name_am master_name_am,m.name_ru master_name_ru
            FROM subcategory_proposals sp
            JOIN partners p ON p.id=sp.partner_id
            JOIN master_categories m ON m.id=sp.master_category_id
            WHERE sp.status='pending'
            ORDER BY sp.created_at DESC
        """)
        return web.json_response({"ok":True,"proposals":rows})

    async def admin_subcategory_proposal_action(request):
        admin_id=_auth_admin(request)
        proposal_id=int(request.match_info["id"])
        data=await request.json() if request.can_read_body else {}
        action=str(data.get("action") or "").lower()
        proposal=_fetchone("SELECT * FROM subcategory_proposals WHERE id=%s",(proposal_id,))
        if not proposal:return web.json_response({"ok":False,"error":"proposal_not_found"},status=404)
        if proposal["status"]!="pending":return web.json_response({"ok":False,"error":"proposal_already_reviewed"},status=400)
        if action=="reject":
            reason=str(data.get("reason") or "Մերժվել է ադմինիստրատորի կողմից")[:1000]
            _exec("UPDATE subcategory_proposals SET status='rejected',admin_note=%s,reviewed_by=%s,reviewed_at=NOW() WHERE id=%s",(reason,admin_id,proposal_id))
            return web.json_response({"ok":True,"status":"rejected"})

        if action=="edit":
            # Admin can fully correct the partner's proposal before approval.
            name=str(data.get("name") or proposal.get("proposed_name") or "").strip()[:200]
            service_name=str(data.get("service_name") or proposal.get("requested_service_name") or "").strip()[:200]
            description=str(data.get("description") if data.get("description") is not None else (proposal.get("description") or ""))[:3000]
            price=data.get("price", proposal.get("price"))
            try:
                price=float(price) if price not in (None,"") else None
            except (TypeError,ValueError):
                return web.json_response({"ok":False,"error":"invalid_price"},status=400)
            if not name:
                return web.json_response({"ok":False,"error":"category_name_required"},status=400)
            _exec(
                """UPDATE subcategory_proposals
                   SET proposed_name=%s, requested_service_name=%s, description=%s,
                       price=%s, admin_note=%s
                   WHERE id=%s AND status='pending'""",
                (name,service_name,description,price,str(data.get("admin_note") or "")[:2000],proposal_id),
            )
            return web.json_response({
                "ok":True,
                "status":"pending",
                "proposal":_fetchone("SELECT * FROM subcategory_proposals WHERE id=%s",(proposal_id,))
            })

        if action!="approve":return web.json_response({"ok":False,"error":"unknown_action"},status=400)

        import re
        name=str(data.get("name") or proposal["proposed_name"] or "").strip()
        if not name:return web.json_response({"ok":False,"error":"category_name_required"},status=400)
        slug=re.sub(r"[^a-z0-9\u0531-\u0587]+","-",name.lower()).strip("-") or ("category-"+str(proposal_id))
        existing=_fetchone("SELECT id FROM categories WHERE master_category_id=%s AND (lower(trim(name_am))=lower(trim(%s)) OR lower(trim(name_ru))=lower(trim(%s)))",(proposal["master_category_id"],name,name))
        category_id = int(existing["id"]) if existing else None
        if not category_id:
            category=_exec("""
                INSERT INTO categories(master_category_id,name_am,name_ru,name_en,slug,is_active,commission_type,commission_value)
                VALUES(%s,%s,%s,%s,%s,TRUE,'inside',0)
                RETURNING id,master_category_id,name_am,name_ru,name_en,slug
            """,(proposal["master_category_id"],name,name,name,slug),True)
            category_id=int(category["id"])
        else:
            category=_fetchone("SELECT id,master_category_id,name_am,name_ru,name_en,slug FROM categories WHERE id=%s",(category_id,))

        # The proposal also contains the service the partner wanted to add.
        # Approving the new subcategory must not lose that service: create it
        # under the approved category so it immediately appears in the
        # partner cabinet. Keep it as draft because the category approval is
        # not itself a separate service-content approval.
        service_name=str(proposal.get("requested_service_name") or "").strip()
        service_price=proposal.get("price")
        if service_name:
            duplicate=_fetchone(
                """SELECT id FROM services
                   WHERE partner_id=%s AND category_id=%s
                     AND lower(trim(name))=lower(trim(%s))
                     AND (status IS NULL OR status <> 'deleted')
                   LIMIT 1""",
                (proposal["partner_id"],category_id,service_name),
            )
            if not duplicate:
                _exec(
                    """INSERT INTO services(
                           partner_id,category_id,subcategory_id,name,description,
                           price,status,data_json
                       )
                       VALUES(%s,%s,%s,%s,%s,%s,'draft',%s)""",
                    (
                        proposal["partner_id"],category_id,category_id,service_name,
                        str(proposal.get("description") or ""),
                        service_price,
                        json.dumps({
                            "subcategory_proposal_id": proposal_id,
                            "source": "partner_subcategory_proposal",
                        }),
                    ),
                )

        _exec("UPDATE subcategory_proposals SET status='approved',admin_note=NULL,reviewed_by=%s,reviewed_at=NOW() WHERE id=%s",(admin_id,proposal_id))
        return web.json_response({"ok":True,"status":"approved","category_id":category_id,"category":category})


    app.router.add_get("/api/admin/subcategory-proposals", admin_subcategory_proposals)
    app.router.add_post("/api/admin/subcategory-proposals/{id}/action", admin_subcategory_proposal_action)
    app.router.add_get("/api/master/{id}/service-direction-requests", service_direction_requests)
    app.router.add_post("/api/master/{id}/service-direction-requests/{request_id}/document", service_direction_request_upload)
    app.router.add_get("/api/master/{id}/partner-directions", directions)
    app.router.add_post("/api/master/{id}/partner-directions", add_direction)
    app.router.add_post("/api/master/{id}/partner-directions/{direction_id}/documents/upload", direction_document)
    app.router.add_get("/api/admin/directions-tree", admin_directions_tree)
    app.router.add_post("/api/admin/master-directions/{id}/action", admin_master_direction_action)
    app.router.add_get("/api/admin/partner-applications/{id}/directions", admin_partner_directions)
    app.router.add_post("/api/admin/partner-directions/{id}/action", admin_direction_action)
    app.router.add_post("/api/admin/partner/{id}/settings", admin_partner_settings)
    app.router.add_post("/api/admin/partner-services/{id}/action", admin_service_action)
