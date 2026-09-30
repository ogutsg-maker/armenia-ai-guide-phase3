-- Versioned DB changes extracted from runtime bootstrap code.
-- Apply only through the migration process, never from application startup.

-- platform_schema.py: statements that were previously executed on every startup.

    -- ---------------------------------------------------------------------
    -- Core partner tables
    -- ---------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS partners (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL UNIQUE REFERENCES users(telegram_id) ON DELETE CASCADE,
        business_name TEXT NOT NULL DEFAULT '',
        business_description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','pending','under_review','approved','rejected','suspended','blocked')),
        verification_status TEXT NOT NULL DEFAULT 'not_submitted'
            CHECK (verification_status IN ('not_submitted','pending','approved','rejected')),
        rejection_reason TEXT,
        contact_share_policy TEXT NOT NULL DEFAULT 'after_booking'
            CHECK (contact_share_policy IN ('after_booking','premium','never')),
        contact_sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        premium_contact_sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );


    -- WebApp authentication can reach partner registration before the legacy
    -- bot registration path has inserted the user. Keep the FK strict, but
    -- automatically create the minimal users row first.
    CREATE OR REPLACE FUNCTION ensure_partner_user_exists()
    RETURNS TRIGGER AS $$
    BEGIN
        INSERT INTO users (telegram_id)
        VALUES (NEW.user_id)
        ON CONFLICT (telegram_id) DO NOTHING;

        RETURN NEW;

    END;

    $$ LANGUAGE plpgsql SET search_path = public, pg_temp;


    DROP TRIGGER IF EXISTS trg_ensure_partner_user_exists ON partners;

    CREATE TRIGGER trg_ensure_partner_user_exists
    BEFORE INSERT ON partners
    FOR EACH ROW
    EXECUTE FUNCTION ensure_partner_user_exists();


    -- Canonical object-level weekly schedule. Kept separate from data_json so
    -- partner edits cannot be overwritten by legacy registration metadata.
    ALTER TABLE partner_objects
        ADD COLUMN IF NOT EXISTS working_hours JSONB NOT NULL DEFAULT '{}'::jsonb;


    UPDATE partner_objects
       SET working_hours = COALESCE(data_json->'working_hours', '{}'::jsonb)
     WHERE working_hours = '{}'::jsonb
       AND COALESCE(data_json->'working_hours', '{}'::jsonb) <> '{}'::jsonb;

-- partner_directions_api.py: schema changes/backfills removed from runtime.
ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

    ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS name_en TEXT;

    ALTER TABLE categories ADD COLUMN IF NOT EXISTS name_en TEXT;

    ALTER TABLE partner_verification_documents
        ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL;

    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS proposed_subcategory_name TEXT;

    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL;

    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS document_id BIGINT REFERENCES partner_verification_documents(id) ON DELETE SET NULL;

    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- partner_directions_api.py: additional runtime _exec blocks removed from schema builder.
INSERT INTO partner_directions(partner_id, master_category_id, status)
    SELECT DISTINCT p.id, c.master_category_id,
           CASE WHEN p.status='approved' AND p.verification_status='approved'
                THEN 'approved' ELSE 'pending' END
    FROM partners p
    JOIN master_skills ms ON ms.user_id=p.user_id AND ms.is_active=TRUE
    JOIN categories c ON c.id=ms.category_id
    WHERE c.master_category_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM partner_directions pd
          WHERE pd.partner_id=p.id
            AND pd.master_category_id=c.master_category_id
      )
    
INSERT INTO partner_direction_categories(partner_direction_id, category_id)
    SELECT pd.id, ms.category_id
    FROM partner_directions pd
    JOIN partners p ON p.id=pd.partner_id
    JOIN master_skills ms ON ms.user_id=p.user_id AND ms.is_active=TRUE
    JOIN categories c ON c.id=ms.category_id AND c.master_category_id=pd.master_category_id
    ON CONFLICT(partner_direction_id, category_id) DO NOTHING
    
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
    # Runtime reconciliation: the cabinet must derive a firm's direction tree
    # from the same active services that it already displays. This also repairs
    # old records whose business_id was not populated during migration.
    if business_id is not None:
        _exec("""
        UPDATE partner_directions pd
           SET business_id=%s, updated_at=NOW()
        WHERE pd.partner_id=%s
          AND pd.business_id IS NULL
          AND EXISTS (
              SELECT 1
              FROM services s
              JOIN categories c ON c.id=s.category_id
              WHERE s.partner_id=pd.partner_id
                AND s.business_id=%s
                AND s.status IN ('active','approved')
                AND c.master_category_id=pd.master_category_id
          )
        """, (business_id,partner_id,business_id))
        _exec("""
        INSERT INTO partner_directions(partner_id,business_id,master_category_id,status)
        SELECT DISTINCT s.partner_id,s.business_id,c.master_category_id,'approved'
        FROM services s
        JOIN categories c ON c.id=s.category_id
        WHERE s.partner_id=%s AND s.business_id=%s
          AND s.status IN ('active','approved')
          AND c.master_category_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM partner_directions pd
              WHERE pd.partner_id=s.partner_id
                AND pd.business_id=s.business_id
                AND pd.master_category_id=c.master_category_id
          )
        """,(partner_id,business_id))
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
    # Canonical fallback: services already prove which catalogue directions
    # belong to this firm. Do not let a stale/missing partner_directions row
    # hide a real approved direction from the cabinet.
    if business_id is not None:
        service_masters = _fetchall("""
            SELECT DISTINCT c.master_category_id
            FROM services s
            JOIN categories c ON c.id=s.category_id
            WHERE s.partner_id=%s AND s.business_id=%s
              AND (s.status IS NULL OR s.status IN ('active','approved'))
              AND c.master_category_id IS NOT NULL
        """, (partner_id,business_id))
        existing_masters = {int(x["master_category_id"]) for x in rows if x.get("master_category_id") is not None}
        for x in service_masters:
            mid=int(x["master_category_id"])
            if mid in existing_masters:
                continue
            m=_fetchone("SELECT id,name_am,name_ru,slug,is_active FROM master_categories WHERE id=%s",(mid,))
            if m:
                rows.append({"direction_id":None,"master_category_id":mid,"status":"approved","rejection_reason":None,
                             "name_am":m["name_am"],"name_ru":m["name_ru"],"slug":m["slug"],"document_count":0})
        rows.sort(key=lambda x:int(x.get("master_category_id") or 0))

    selected = _fetchall("""
        SELECT pdc.partner_direction_id, c.id,c.master_category_id,c.name_am,c.name_ru,c.slug,c.is_active
        FROM partner_direction_categories pdc
        JOIN categories c ON c.id=pdc.category_id
        WHERE pdc.partner_direction_id IN (
            SELECT id FROM partner_directions
            WHERE partner_id=%s AND (%s IS NULL OR business_id=%s)
        )
        ORDER BY c.id
    """, (partner_id,business_id,business_id))
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


def _admin_guard(request):
    raw = request.headers.get("X-Telegram-Init-Data", "").strip() or request.query.get("tgwad", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"telegram_init_data_required"}', content_type="application/json")
    try:
        user = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
    except TelegramWebAppAuthError as exc:
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"telegram_init_data_invalid"}', content_type="application/json")
    admin_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or os.getenv("ADMIN_ID", "0") or 0)
    if not admin_id or int(user["id"]) != admin_id:
        raise web.HTTPForbidden(text='{"ok":false,"error":"admin_access_required"}', content_type="application/json")
    return int(user["id"])


def register_partner_direction_routes(app, db=None, bot=None):
    app["partner_direction_bot"] = bot
    ensure_partner_direction_schema()

    async def directions(request):
        # The current cabinet uses route id=0 as a neutral placeholder.
        # Resolve the real Telegram user from validated Mini App initData,
        # exactly like the main cabinet API does.
        uid=int(request.match_info["id"])
        if uid == 0:
            # Use the exact same authentication implementation as the
            # working partner cabinet endpoints (/services, /objects).
            try:
                uid = _cabinet_auth_partner(request)
            except web.HTTPException as exc:
                return web.json_response({"ok":False,"error":"telegram_init_data_invalid"},status=exc.status)
        partner=_partner(uid)
        if not partner:
            return web.json_response({"ok":False,"error":"partner_registration_required"},status=404)
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
        
ALTER TABLE category_settings
          ADD COLUMN IF NOT EXISTS bank_commission_type TEXT NOT NULL DEFAULT 'none',
          ADD COLUMN IF NOT EXISTS bank_commission_value NUMERIC NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS cancellation_policy TEXT NOT NULL DEFAULT 'no_refund',
          ADD COLUMN IF NOT EXISTS premium_contact_enabled BOOLEAN NOT NULL DEFAULT FALSE,
          ADD COLUMN IF NOT EXISTS premium_contact_fee NUMERIC NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS premium_disclosure_scope TEXT NOT NULL DEFAULT 'none',
          ADD COLUMN IF NOT EXISTS contact_reveal_after_booking BOOLEAN NOT NULL DEFAULT TRUE
        

-- partner_lifecycle_schema.py: lifecycle trigger/function.
CREATE OR REPLACE FUNCTION sync_partner_after_direction_change()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'approved' THEN
        UPDATE partners
           SET status='approved', verification_status='approved', rejection_reason=NULL, updated_at=NOW()
         WHERE id=NEW.partner_id;
    ELSIF NEW.status = 'rejected' THEN
        UPDATE partners
           SET verification_status=CASE
                WHEN EXISTS (SELECT 1 FROM partner_directions WHERE partner_id=NEW.partner_id AND status='approved') THEN 'approved'
                ELSE 'rejected' END,
               status=CASE
                WHEN EXISTS (SELECT 1 FROM partner_directions WHERE partner_id=NEW.partner_id AND status='approved') THEN 'approved'
                ELSE 'pending' END,
               rejection_reason=CASE WHEN EXISTS (SELECT 1 FROM partner_directions WHERE partner_id=NEW.partner_id AND status='approved') THEN NULL ELSE NEW.rejection_reason END,
               updated_at=NOW()
         WHERE id=NEW.partner_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;

DROP TRIGGER IF EXISTS trg_sync_partner_after_direction_change ON partner_directions;
CREATE TRIGGER trg_sync_partner_after_direction_change
AFTER INSERT OR UPDATE OF status ON partner_directions
FOR EACH ROW EXECUTE FUNCTION sync_partner_after_direction_change();
