"""Admin statistics for the current AI-first marketplace."""
from __future__ import annotations
import time
from aiohttp import web
import features
import ai_cost_center
from marketplace_flow_api import _one, _rows
from admin_ai_api import _admin

_STATS_CACHE={"at":0.0,"data":None}; _STATS_TTL=60.0

def _scalar(sql,params=(),key="v",default=0):
    row=_one(sql,params); return default if not row or row.get(key) is None else row.get(key)

def _status_counts(table,column="status"):
    try:
        return {str(r.get("k")):int(r.get("c") or 0) for r in _rows(f"SELECT {column} k, COUNT(*) c FROM {table} GROUP BY {column}")}
    except Exception:
        return {}

def _compute_overview():
    requests=_status_counts("service_requests"); negotiations=_status_counts("negotiations"); bookings=_status_counts("bookings"); partners=_status_counts("partners")
    gmv=float(_scalar("SELECT COALESCE(SUM(agreed_price),0) v FROM bookings WHERE status IN ('paid','booked','completed')"))
    commission=float(_scalar("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE payment_type='commission' AND status IN ('paid','settled')"))
    premium=float(_scalar("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE payment_type='premium_contact' AND status IN ('paid','settled')"))
    refunds=_one("SELECT COUNT(*) c, COALESCE(SUM(refund_amount),0) s FROM booking_cancellations") or {}
    reviews=_one("SELECT COUNT(*) c, COALESCE(AVG(rating),0) a FROM reviews WHERE status='published'") or {}
    flagged=int(_scalar("SELECT COUNT(*) v FROM reviews WHERE status='flagged'"))
    users_total=int(_scalar("SELECT COUNT(*) v FROM users"))
    return {"users_total":users_total,"requests":{"by_status":requests,"total":sum(requests.values())},"negotiations":{"by_status":negotiations,"total":sum(negotiations.values())},"bookings":{"by_status":bookings,"total":sum(bookings.values()),"gmv":gmv},"revenue":{"commission":commission,"premium_contact":premium},"partners":partners,"reviews":{"count":int(reviews.get("c") or 0),"avg_rating":round(float(reviews.get("a") or 0),2),"flagged":flagged},"support":_status_counts("support_tickets"),"refunds":{"count":int(refunds.get("c") or 0),"amount":float(refunds.get("s") or 0)}}

async def stats_overview(request):
    _admin(request); now=time.time()
    if not request.query.get("fresh") and _STATS_CACHE["data"] is not None and now-_STATS_CACHE["at"]<_STATS_TTL:return web.json_response({"ok":True,"cached":True,"stats":_STATS_CACHE["data"]})
    data=_compute_overview(); _STATS_CACHE.update(at=now,data=data); return web.json_response({"ok":True,"cached":False,"stats":data})

async def stats_top_partners(request):
    _admin(request)
    items=_rows("SELECT p.id,p.business_name,COUNT(b.id) FILTER (WHERE b.status IN ('paid','booked','completed')) bookings,COALESCE(SUM(b.agreed_price) FILTER (WHERE b.status IN ('paid','booked','completed')),0) gmv,COALESCE((SELECT AVG(rating) FROM reviews r WHERE r.partner_id=p.id AND r.status='published'),0) avg_rating FROM partners p LEFT JOIN bookings b ON b.partner_id=p.id GROUP BY p.id,p.business_name ORDER BY gmv DESC,bookings DESC LIMIT 20")
    return web.json_response({"ok":True,"items":items})

async def stats_timeseries(request):
    _admin(request); metric=request.query.get("metric","bookings")
    try: days=max(1,min(365,int(request.query.get("days",30))))
    except (TypeError,ValueError): days=30
    table={"bookings":"bookings","requests":"service_requests","reviews":"reviews","tickets":"support_tickets"}.get(metric,"bookings")
    rows=_rows(f"SELECT DATE(created_at) d,COUNT(*) c FROM {table} WHERE created_at>=NOW()-INTERVAL '{days} days' GROUP BY DATE(created_at) ORDER BY d")
    return web.json_response({"ok":True,"metric":metric,"days":days,"series":rows})

# NOTE: /api/admin/settings is owned by runtime_platform_bootstrap
# (_admin_settings_get/_admin_settings_save), which has the fuller validation.
# Registering it here as well raised aiohttp RuntimeError
# ("Added route will never be executed") on startup, so it was removed.

async def stats_structure(request):
    """Compact, drill-down statistics for the Admin Overview.
    This is read-only platform data; no AI/model is involved.
    """
    _admin(request)
    section = str(request.query.get("section") or "catalog").strip().lower()

    def safe_rows(sql, params=()):
        try:
            return _rows(sql, params)
        except Exception:
            return []

    def safe_scalar(sql, params=(), key="v"):
        try:
            return _scalar(sql, params, key=key, default=0)
        except Exception:
            return 0

    if section == "catalog":
        directions = safe_rows("""SELECT m.id,m.name_am,m.name_ru,m.name_en,
                    COUNT(c.id) AS subcategories
                    FROM master_categories m
                    LEFT JOIN categories c ON c.master_category_id=m.id AND c.is_active=TRUE
                    WHERE m.is_active=TRUE
                    GROUP BY m.id,m.name_am,m.name_ru,m.name_en
                    ORDER BY m.id""")
        services = safe_rows("""SELECT COALESCE(c.master_category_id,0) AS direction_id,
                    COALESCE(m.name_am,'Չդասակարգված') AS direction_name,
                    COUNT(s.id) AS services
                    FROM services s
                    LEFT JOIN categories c ON c.id=s.category_id
                    LEFT JOIN master_categories m ON m.id=c.master_category_id
                    WHERE s.status<>'deleted'
                    GROUP BY c.master_category_id,m.name_am
                    ORDER BY services DESC""")
        return web.json_response({"ok":True,"section":section,"summary":{
            "directions":int(safe_scalar("SELECT COUNT(*) v FROM master_categories WHERE is_active=TRUE")),
            "subcategories":int(safe_scalar("SELECT COUNT(*) v FROM categories WHERE is_active=TRUE")),
            "services":int(safe_scalar("SELECT COUNT(*) v FROM services WHERE status<>'deleted'"))
        },"directions":directions,"services_by_direction":services})

    if section == "partners":
        status = safe_rows("""SELECT status,COUNT(*) AS count FROM partners GROUP BY status ORDER BY status""")
        companies = safe_rows("""SELECT p.id,p.business_name,COUNT(b.id) AS companies
                    FROM partners p LEFT JOIN partner_businesses b
                    ON b.partner_id=p.id AND b.status<>'archived'
                    GROUP BY p.id,p.business_name ORDER BY companies DESC,p.id DESC""")
        return web.json_response({"ok":True,"section":section,"summary":{
            "partners":int(safe_scalar("SELECT COUNT(*) v FROM partners WHERE status<>'archived'")),
            "companies":int(safe_scalar("SELECT COUNT(*) v FROM partner_businesses WHERE status<>'archived'")),
            "addresses":int(safe_scalar("SELECT COUNT(*) v FROM partner_objects")),
            "documents":int(safe_scalar("SELECT COUNT(*) v FROM partner_verification_documents"))
        },"status":status,"companies_by_partner":companies})

    if section == "geography":
        marzes = safe_rows("""SELECT COALESCE(NULLIF(TRIM(marz),''),'—') AS marz,
                    COUNT(DISTINCT partner_id) AS partners,
                    COUNT(DISTINCT business_id) AS companies,
                    COUNT(*) AS addresses
                    FROM partner_objects
                    GROUP BY COALESCE(NULLIF(TRIM(marz),''),'—')
                    ORDER BY partners DESC,companies DESC,marz""")
        cities = safe_rows("""SELECT COALESCE(NULLIF(TRIM(city),''),'—') AS city,
                    COALESCE(NULLIF(TRIM(marz),''),'—') AS marz,
                    COUNT(DISTINCT partner_id) AS partners,
                    COUNT(DISTINCT business_id) AS companies,
                    COUNT(*) AS addresses
                    FROM partner_objects
                    GROUP BY COALESCE(NULLIF(TRIM(city),''),'—'),
                             COALESCE(NULLIF(TRIM(marz),''),'—')
                    ORDER BY partners DESC,companies DESC,city""")
        return web.json_response({"ok":True,"section":section,"summary":{
            "marzes":len(marzes),
            "cities":len(cities),
            "partners":int(safe_scalar("SELECT COUNT(DISTINCT partner_id) v FROM partner_objects")),
            "companies":int(safe_scalar("SELECT COUNT(DISTINCT business_id) v FROM partner_objects"))
        },"marzes":marzes,"cities":cities})

    if section == "services":
        rows = safe_rows("""SELECT s.id,s.name,s.price,s.status,
                    p.business_name AS partner_name,b.name AS company_name,
                    COALESCE(m.name_am,'Չդասակարգված') AS direction_name,
                    COALESCE(c.name_am,'Չդասակարգված') AS subcategory_name
                    FROM services s
                    LEFT JOIN partners p ON p.id=s.partner_id
                    LEFT JOIN partner_businesses b ON b.id=s.business_id
                    LEFT JOIN categories c ON c.id=s.category_id
                    LEFT JOIN master_categories m ON m.id=c.master_category_id
                    WHERE s.status<>'deleted'
                    ORDER BY s.id DESC LIMIT 200""")
        return web.json_response({"ok":True,"section":section,"summary":{
            "total":int(safe_scalar("SELECT COUNT(*) v FROM services WHERE status<>'deleted'")),
            "priced":int(safe_scalar("SELECT COUNT(*) v FROM services WHERE status<>'deleted' AND price IS NOT NULL")),
            "uncategorized":int(safe_scalar("SELECT COUNT(*) v FROM services WHERE status<>'deleted' AND category_id IS NULL"))
        },"items":rows})

    if section == "activity":
        items = safe_rows("""SELECT 'partner' AS type,id,business_name AS name,status,created_at
                    FROM partners
                    ORDER BY created_at DESC NULLS LAST,id DESC LIMIT 10""")
        companies = safe_rows("""SELECT 'company' AS type,id,name,status,created_at
                    FROM partner_businesses
                    ORDER BY created_at DESC NULLS LAST,id DESC LIMIT 10""")
        services = safe_rows("""SELECT 'service' AS type,id,name,status,created_at
                    FROM services
                    WHERE status<>'deleted'
                    ORDER BY created_at DESC NULLS LAST,id DESC LIMIT 10""")
        return web.json_response({"ok":True,"section":section,
            "partners":items,"companies":companies,"services":services})

    if section == "gaps":
        gaps = {
            "directions_without_companies": int(safe_scalar("""SELECT COUNT(*) v FROM master_categories m
                WHERE m.is_active=TRUE AND NOT EXISTS (
                  SELECT 1 FROM partner_direction_categories pdc
                  JOIN partner_directions pd ON pd.id=pdc.partner_direction_id
                  WHERE pd.status='approved' AND pd.master_category_id=m.id)""")),
            "subcategories_without_services": int(safe_scalar("""SELECT COUNT(*) v FROM categories c
                WHERE c.is_active=TRUE AND NOT EXISTS (
                  SELECT 1 FROM services s WHERE s.category_id=c.id AND s.status<>'deleted')""")),
            "services_without_category": int(safe_scalar("""SELECT COUNT(*) v FROM services
                WHERE status<>'deleted' AND category_id IS NULL""")),
            "companies_without_address": int(safe_scalar("""SELECT COUNT(*) v FROM partner_businesses b
                WHERE b.status<>'archived' AND NOT EXISTS (
                  SELECT 1 FROM partner_objects o WHERE o.business_id=b.id)""")),
            "companies_without_services": int(safe_scalar("""SELECT COUNT(*) v FROM partner_businesses b
                WHERE b.status<>'archived' AND NOT EXISTS (
                  SELECT 1 FROM services s WHERE s.business_id=b.id AND s.status<>'deleted')""")),
            "partners_without_companies": int(safe_scalar("""SELECT COUNT(*) v FROM partners p
                WHERE p.status<>'archived' AND NOT EXISTS (
                  SELECT 1 FROM partner_businesses b WHERE b.partner_id=p.id AND b.status<>'archived')"""))
        }
        return web.json_response({"ok":True,"section":section,"gaps":gaps})

    return web.json_response({"ok":False,"error":"unknown_statistics_section"},status=400)

async def stats_ai_cost(request):
    _admin(request)
    try: days=max(1,min(3650,int(request.query.get("days",30))))
    except (TypeError,ValueError): days=30
    try:
        data=ai_cost_center.admin_overview(days=days)
    except Exception as exc:
        return web.json_response({"ok":False,"error":"ai_cost_unavailable","details":str(exc)[:300]},status=503)
    return web.json_response({"ok":True,"days":days,"cost":data})

def register_admin_stats_routes(app):
    app.router.add_get("/api/admin/stats/overview",stats_overview)
    app.router.add_get("/api/admin/stats/top-partners",stats_top_partners)
    app.router.add_get("/api/admin/stats/timeseries",stats_timeseries)
    app.router.add_get("/api/admin/stats/ai-cost",stats_ai_cost)\n    app.router.add_get("/api/admin/stats/structure",stats_structure)
