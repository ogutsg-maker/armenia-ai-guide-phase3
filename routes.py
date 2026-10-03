from __future__ import annotations
import json,re,secrets
from datetime import datetime,timedelta,timezone
from aiohttp import web
import db
from auth import user
from ai import extract,ask
from config import ADMIN_ID,COMMISSION_MODE,COMMISSION_RATE,IDRAM_PAYMENT_URL
def j(data,status=200): return web.json_response(data,status=status)
def tr(lang,hy,ru,en): return {"hy":hy,"ru":ru,"en":en}.get(lang,ru)
def lang(u): return u.get("lang") or "hy"
async def session(request):
    u=await user(request); uid=int(u["id"])
    db.exec("INSERT INTO aig_users(telegram_id,username,full_name) VALUES(%s,%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET username=EXCLUDED.username,full_name=EXCLUDED.full_name,updated_at=now()", (uid,u.get("username"),(u.get("first_name","")+" "+u.get("last_name","")).strip()))
    x=db.one("SELECT * FROM aig_users WHERE telegram_id=%s",(uid,))
    p=db.one("SELECT * FROM aig_partners WHERE telegram_id=%s",(uid,))
    return j({"ok":True,"user":x,"partner":p,"admin":uid==ADMIN_ID})
async def register_partner(request):
    u=await user(request); uid=int(u["id"]); body=await request.json()
    name=str(body.get("name") or "").strip(); phone=str(body.get("phone") or "").strip()
    if not name:return j({"ok":False,"error":"business_name_required"},400)
    if not re.fullmatch(r"[+0-9() .-]{7,30}",phone):return j({"ok":False,"error":"invalid_phone"},400)
    with db.conn() as c:
        c.execute("INSERT INTO aig_users(telegram_id,username,full_name,role) VALUES(%s,%s,%s,'partner') ON CONFLICT(telegram_id) DO UPDATE SET role='partner',updated_at=now()", (uid,u.get("username"),(u.get("first_name","")+" "+u.get("last_name","")).strip()))
        p=c.execute("INSERT INTO aig_partners(telegram_id,phone) VALUES(%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET phone=EXCLUDED.phone RETURNING id",(uid,phone)).fetchone()[0]
        c.execute("INSERT INTO aig_companies(partner_id,name) SELECT %s,%s WHERE NOT EXISTS(SELECT 1 FROM aig_companies WHERE partner_id=%s)",(p,name,p))
    return j({"ok":True,"destination":"/master_cabinet.html"})
async def partner_profile(request):
    u=await user(request); uid=int(u["id"]); p=db.one("SELECT * FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    cs=db.all("SELECT * FROM aig_companies WHERE partner_id=%s AND NOT archived ORDER BY id",(p["id"],))
    return j({"ok":True,"partner":p,"companies":cs})
async def partner_services(request):
    u=await user(request); uid=int(u["id"]); p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    rows=db.all("SELECT s.*,c.name company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE c.partner_id=%s ORDER BY s.id DESC",(p["id"],))
    return j({"ok":True,"items":rows})
async def create_service_preview(request):
    u=await user(request); uid=int(u["id"]); body=await request.json(); text=str(body.get("text") or "").strip()
    p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    x,_=await extract(text,"service creation")
    if not x.get("name") or not x.get("price_amd") or x.get("price_type") not in ("fixed","from"):return j({"ok":False,"error":"service_data_incomplete"},400)
    pending={"kind":"service_create","company_id":body.get("company_id"),"data":x}
    db.exec("INSERT INTO aig_ai_sessions(telegram_id,context,pending) VALUES(%s,'PARTNER',%s) ON CONFLICT(telegram_id) DO UPDATE SET pending=EXCLUDED.pending,updated_at=now()", (uid,json.dumps(pending,ensure_ascii=False)))
    return j({"ok":True,"preview":x,"confirm_required":True})
async def confirm_service(request):
    u=await user(request); uid=int(u["id"]); p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,))
    s=db.one("SELECT pending FROM aig_ai_sessions WHERE telegram_id=%s",(uid,))
    if not p or not s or not s["pending"]:return j({"ok":False,"error":"nothing_to_confirm"},400)
    pending=s["pending"]; data=pending["data"]; company_id=pending.get("company_id")
    if not company_id: company_id=db.one("SELECT id FROM aig_companies WHERE partner_id=%s AND NOT archived ORDER BY id LIMIT 1",(p["id"],))["id"]
    owned=db.one("SELECT id FROM aig_companies WHERE id=%s AND partner_id=%s",(company_id,p["id"]))
    if not owned:return j({"ok":False,"error":"company_not_owned"},403)
    row=db.exec("INSERT INTO aig_services(company_id,name,price_type,price_amd,hours,at_client,territory,status) VALUES(%s,%s,%s,%s,%s,%s,%s,'PENDING_ADMIN') RETURNING id",(company_id,data["name"],data["price_type"],float(data["price_amd"]),data.get("hours"),bool(data.get("at_client")),data.get("territory")),True)
    db.exec("INSERT INTO aig_service_applications(service_id) VALUES(%s)",(row["id"],))
    db.exec("UPDATE aig_ai_sessions SET pending=NULL,updated_at=now() WHERE telegram_id=%s",(uid,))
    return j({"ok":True,"service_id":row["id"],"status":"PENDING_ADMIN"})
async def admin_services(request):
    u=await user(request)
    if int(u["id"])!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    rows=db.all("SELECT s.*,c.name company_name,p.telegram_id FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id WHERE s.status='PENDING_ADMIN' ORDER BY s.id")
    return j({"ok":True,"items":rows})
async def admin_service_decision(request):
    u=await user(request)
    if int(u["id"])!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    sid=int(request.match_info["id"]); body=await request.json(); action=body.get("action")
    if action not in ("approve","reject"):return j({"ok":False,"error":"invalid_action"},400)
    status="ACTIVE" if action=="approve" else "REJECTED"
    db.exec("UPDATE aig_services SET status=%s,rejection_reason=%s,updated_at=now() WHERE id=%s",(status,body.get("reason") if action=="reject" else None,sid))
    db.exec("UPDATE aig_service_applications SET status=%s,reviewed_at=now(),reviewer=%s WHERE service_id=%s",("APPROVED" if action=="approve" else "REJECTED",int(u["id"]),sid))
    return j({"ok":True,"status":status})
def classify(name):
    n=name.lower()
    if any(x in n for x in ("холодиль","սառնար","стираль","լվացքի")): return None
    return None
async def client_search(request):
    u=await user(request); uid=int(u["id"]); body=await request.json(); text=str(body.get("text") or "")
    x,_=await extract(text,"client search")
    service=x.get("service") or text; city=x.get("city")
    db.exec("INSERT INTO aig_users(telegram_id,username,full_name,role) VALUES(%s,%s,%s,'client') ON CONFLICT(telegram_id) DO UPDATE SET role=COALESCE(aig_users.role,'client')",(uid,u.get("username"),(u.get("first_name","")+" "+u.get("last_name","")).strip()))
    req=db.exec("INSERT INTO aig_client_requests(client_telegram_id,service_text,city,district) VALUES(%s,%s,%s,%s) RETURNING id",(uid,service,city,x.get("district")),True)
    q="SELECT s.id service_id,s.name service_name,s.price_type,s.price_amd,c.name partner_name,p.id partner_id,a.city FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id LEFT JOIN aig_addresses a ON a.id=s.address_id WHERE s.status='ACTIVE' AND s.name ILIKE %s"
    items=db.all(q,(f"%{service}%",))
    return j({"ok":True,"request_id":req["id"],"reply":tr(lang(db.one("SELECT * FROM aig_users WHERE telegram_id=%s",(uid,))),"Գտածս հաստատված ակտիվ ծառայություններն են։","Нашёл подтверждённые активные услуги.","Here are confirmed active services."),"items":items[:3]})
async def select_service(request):
    u=await user(request); uid=int(u["id"]); rid=int(request.match_info["id"]); body=await request.json(); sid=int(body["service_id"])
    req=db.one("SELECT * FROM aig_client_requests WHERE id=%s AND client_telegram_id=%s",(rid,uid))
    svc=db.one("SELECT s.*,p.id partner_id FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id WHERE s.id=%s AND s.status='ACTIVE'",(sid,))
    if not req or not svc:return j({"ok":False,"error":"service_not_found"},404)
    n=db.exec("INSERT INTO aig_negotiations(request_id,service_id,partner_id,partner_interest_at) VALUES(%s,%s,%s,now()) ON CONFLICT(request_id,partner_id) DO UPDATE SET service_id=EXCLUDED.service_id RETURNING id",(rid,sid,svc["partner_id"]),True)
    return j({"ok":True,"negotiation_id":n["id"]})
async def negotiation(request):
    u=await user(request); uid=int(u["id"]); nid=int(request.match_info["id"])
    n=db.one("SELECT n.*,s.name service_name,c.name company_name,p.telegram_id partner_telegram_id,r.client_telegram_id FROM aig_negotiations n JOIN aig_services s ON s.id=n.service_id JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=n.partner_id JOIN aig_client_requests r ON r.id=n.request_id WHERE n.id=%s",(nid,))
    if not n or uid not in (n["client_telegram_id"],n["partner_telegram_id"]):return j({"ok":False,"error":"forbidden"},403)
    msgs=db.all("SELECT * FROM aig_messages WHERE negotiation_id=%s ORDER BY id",(nid,))
    b=db.one("SELECT * FROM aig_bookings WHERE negotiation_id=%s",(nid,))
    return j({"ok":True,"negotiation":n,"messages":msgs,"booking":b})
async def send_message(request):
    u=await user(request); uid=int(u["id"]); nid=int(request.match_info["id"]); body=await request.json(); msg=str(body.get("message") or "").strip()
    n=db.one("SELECT n.*,r.client_telegram_id,p.telegram_id partner_telegram_id FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_partners p ON p.id=n.partner_id WHERE n.id=%s",(nid,))
    if not n or uid not in (n["client_telegram_id"],n["partner_telegram_id"]):return j({"ok":False,"error":"forbidden"},403)
    role="client" if uid==n["client_telegram_id"] else "partner"
    db.exec("INSERT INTO aig_messages(negotiation_id,sender_role,sender_telegram_id,message) VALUES(%s,%s,%s,%s)",(nid,role,uid,msg))
    x,_=await extract(msg,"negotiation terms")
    if x.get("agreed_price") or x.get("agreed_min") or x.get("agreed_max"):
        db.exec("UPDATE aig_negotiations SET agreed_min=COALESCE(%s,agreed_min),agreed_max=COALESCE(%s,agreed_max),agreed_price=COALESCE(%s,agreed_price),commission_base=COALESCE(%s,commission_base),status=CASE WHEN %s IS NOT NULL THEN 'agreed' ELSE status END WHERE id=%s",(x.get("agreed_min"),x.get("agreed_max"),x.get("agreed_price"),x.get("agreed_price"),x.get("agreed_price"),nid))
    return await negotiation(request)
async def book(request):
    u=await user(request); uid=int(u["id"]); nid=int(request.match_info["id"])
    n=db.one("SELECT n.*,r.client_telegram_id,p.telegram_id FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_partners p ON p.id=n.partner_id WHERE n.id=%s",(nid,))
    if not n or uid!=n["client_telegram_id"] or n["status"]!="agreed":return j({"ok":False,"error":"booking_not_allowed"},400)
    price=float(n["agreed_price"] or ((float(n["agreed_min"])+float(n["agreed_max"]))/2 if n["agreed_min"] and n["agreed_max"] else 0))
    commission=price*COMMISSION_RATE/100 if COMMISSION_MODE=="on_top" else price*COMMISSION_RATE/(100+COMMISSION_RATE) if COMMISSION_MODE=="inside" else 0
    b=db.exec("INSERT INTO aig_bookings(negotiation_id,status,agreed_price,commission_amd) VALUES(%s,'PENDING_PARTNER_CONFIRMATION',%s,%s) ON CONFLICT(negotiation_id) DO UPDATE SET agreed_price=EXCLUDED.agreed_price RETURNING id",(nid,price,commission),True)
    return j({"ok":True,"booking_id":b["id"],"status":"PENDING_PARTNER_CONFIRMATION"})
async def partner_confirm_booking(request):
    u=await user(request); uid=int(u["id"]); bid=int(request.match_info["id"])
    b=db.one("SELECT b.*,p.telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_partners p ON p.id=n.partner_id WHERE b.id=%s",(bid,))
    if not b or uid!=b["telegram_id"]:return j({"ok":False,"error":"forbidden"},403)
    db.exec("UPDATE aig_bookings SET status='PENDING_PAYMENT' WHERE id=%s",(bid,))
    return j({"ok":True,"status":"PENDING_PAYMENT","payment_url":IDRAM_PAYMENT_URL or None})
async def payment_webhook(request):
    body=await request.json(); bid=int(body["booking_id"]); ok=bool(body.get("confirmed"))
    if not ok:return j({"ok":True})
    token=secrets.token_urlsafe(24); now=datetime.now(timezone.utc); exp=now+timedelta(hours=1)
    db.exec("UPDATE aig_bookings SET status='PAYMENT_CONFIRMED',payment_ref=%s,payment_confirmed_at=now(),qr_token=%s,qr_expires_at=%s WHERE id=%s",(body.get("payment_ref"),token,exp,bid))
    return j({"ok":True})
async def checkin(request):
    u=await user(request); token=request.match_info["token"]; b=db.one("SELECT b.*,p.telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_partners p ON p.id=n.partner_id WHERE b.qr_token=%s",(token,))
    if not b or int(u["id"])!=int(b["telegram_id"]):return j({"ok":False,"error":"invalid_qr"},400)
    now=datetime.now(timezone.utc)
    if b["qr_expires_at"] and b["qr_expires_at"]<now and b["status"]!="ARBITRATION":return j({"ok":False,"error":"qr_expired"},400)
    db.exec("UPDATE aig_bookings SET status='IN_PROGRESS',checked_in_at=now() WHERE id=%s",(b["id"],))
    return j({"ok":True,"status":"IN_PROGRESS"})
async def complete(request):
    u=await user(request); bid=int(request.match_info["id"])
    b=db.one("SELECT b.*,p.telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_partners p ON p.id=n.partner_id WHERE b.id=%s",(bid,))
    if not b or int(u["id"])!=int(b["telegram_id"]):return j({"ok":False,"error":"forbidden"},403)
    db.exec("UPDATE aig_bookings SET status='SERVICE_COMPLETED',completed_at=now() WHERE id=%s",(bid,))
    return j({"ok":True,"status":"SERVICE_COMPLETED"})
async def review(request):
    u=await user(request); bid=int(request.match_info["id"]); body=await request.json()
    b=db.one("SELECT b.*,r.client_telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_client_requests r ON r.id=n.request_id WHERE b.id=%s",(bid,))
    if not b or int(u["id"])!=int(b["client_telegram_id"]) or b["status"]!="SERVICE_COMPLETED":return j({"ok":False,"error":"review_not_allowed"},400)
    db.exec("INSERT INTO aig_reviews(booking_id,client_telegram_id,rating,text) VALUES(%s,%s,%s,%s)",(bid,int(u["id"]),int(body["rating"]),body.get("text")))
    return j({"ok":True})
async def admin_query(request):
    u=await user(request)
    if int(u["id"])!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    body=await request.json(); q=str(body.get("text") or "").lower()
    if "заяв" in q or "pending" in q: return await admin_services(request)
    if "ai" in q and "опера" in q:
        row=db.one("SELECT count(*) n,coalesce(sum(input_tokens+output_tokens),0) tokens FROM aig_ai_costs WHERE created_at::date=current_date")
        return j({"ok":True,"answer":f"Сегодня AI операций: {row['n']}; токенов: {row['tokens']}."})
    return j({"ok":True,"answer":"Я работаю через данные платформы. Уточните, что показать: заявки, сервисы без классификации, заказы, переговоры или AI расходы."})
def setup(app):
    app.router.add_get("/api/session",session)
    app.router.add_post("/api/partner/register",register_partner)
    app.router.add_get("/api/partner/profile",partner_profile)
    app.router.add_get("/api/partner/services",partner_services)
    app.router.add_post("/api/partner/services/preview",create_service_preview)
    app.router.add_post("/api/partner/services/confirm",confirm_service)
    app.router.add_get("/api/admin/services",admin_services)
    app.router.add_post("/api/admin/services/{id}/decision",admin_service_decision)
    app.router.add_post("/api/admin/ai",admin_query)
    app.router.add_post("/api/client/search",client_search)
    app.router.add_post("/api/client/requests/{id}/select",select_service)
    app.router.add_get("/api/negotiations/{id}",negotiation)
    app.router.add_post("/api/negotiations/{id}/messages",send_message)
    app.router.add_post("/api/negotiations/{id}/book",book)
    app.router.add_post("/api/bookings/{id}/confirm",partner_confirm_booking)
    app.router.add_post("/api/payments/webhook",payment_webhook)
    app.router.add_post("/api/bookings/{token}/checkin",checkin)
    app.router.add_post("/api/bookings/{id}/complete",complete)
    app.router.add_post("/api/bookings/{id}/review",review)
