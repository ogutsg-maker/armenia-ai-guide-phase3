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
from config import BOT_TOKEN
from master_cabinet_api import _auth_partner as _cabinet_auth_partner


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

    CREATE INDEX IF NOT EXISTS idx_service_direction_requests_partner
        ON service_direction_requests(partner_id, status, created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_service_direction_requests_status
        ON service_direction_requests(status, created_at DESC);
    """)

    # Backfill legacy master_skills without relying on a missing composite
    # UNIQUE constraint. partner_directions may now contain multiple rows for
    # the same master across different businesses.
    # The current project already has a single verified document for the initial direction.
    def ensure_initial_partner_direction(partner_id: int, user_id: int):
    """Create the registration direction from the already-selected master_skills."""
    rows = _fetchall("""
        SELECT DISTINCT c.master_category_id
        FROM master_skills ms
        JOIN categories c ON c.id=ms.category_id
        WHERE ms.user_id=%s AND ms.is_active=TRUE AND c.master_category_id IS NOT NULL
    """, (user_id,))
    for row in rows:
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
    async def admin_category_settings(request):
        _admin_guard(request)
        category_id = int(request.match_info["id"])
        payload = await request.json()
        allowed = ("bank_commission_type","bank_commission_value","cancellation_policy","premium_contact_enabled","premium_contact_fee","premium_disclosure_scope","contact_reveal_after_booking")
        updates = {k: payload[k] for k in allowed if k in payload}
        if not updates:
            return web.json_response({"ok": False, "error": "no_changes"}, status=400)
        existing = _fetchone("SELECT id FROM category_settings WHERE category_id=%s", (category_id,))
        if existing:
            sets=", ".join(f"{k}=%s" for k in updates)
            _exec(f"UPDATE category_settings SET {sets}, updated_at=NOW() WHERE category_id=%s", [*updates.values(), category_id])
        else:
            fields=["category_id", *updates.keys()]
            values=[category_id, *updates.values()]
            marks=", ".join(["%s"]*len(values))
            _exec(f"INSERT INTO category_settings ({', '.join(fields)}) VALUES ({marks})", values)
        return web.json_response({"ok": True, "category_id": category_id})

    app.router.add_post("/api/admin/partner-services/{id}/action", admin_service_action)
    app.router.add_post("/api/admin/categories/{id}/settings", admin_category_settings)
