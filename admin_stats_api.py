"""Admin statistics for the current AI-first marketplace."""
from __future__ import annotations
import time
from aiohttp import web
import features
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

async def get_settings(request):
    _admin(request); return web.json_response({"ok":True,"settings":features.all_settings(),"defaults":features.DEFAULTS})
async def update_settings(request):
    _admin(request); data=await request.json(); overrides=data.get("settings") if isinstance(data.get("settings"),dict) else data
    if not isinstance(overrides,dict): return web.json_response({"ok":False,"error":"invalid_payload"},status=400)
    clean={k:v for k,v in overrides.items() if k in features.DEFAULTS}
    if not clean:return web.json_response({"ok":False,"error":"no_known_keys"},status=400)
    return web.json_response({"ok":True,"settings":features.save(clean)})

def register_admin_stats_routes(app):
    app.router.add_get("/api/admin/stats/overview",stats_overview)
    app.router.add_get("/api/admin/stats/top-partners",stats_top_partners)
    app.router.add_get("/api/admin/stats/timeseries",stats_timeseries)
    app.router.add_get("/api/admin/settings",get_settings)
    app.router.add_post("/api/admin/settings",update_settings)
