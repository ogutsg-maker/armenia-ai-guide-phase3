from __future__ import annotations
import json,re,secrets
from datetime import datetime,timedelta,timezone
from aiohttp import web
import db
from auth import user
from ai import extract
from config import ADMIN_ID,COMMISSION_MODE,COMMISSION_RATE,IDRAM_PAYMENT_URL
def j(data,status=200): return web.json_response(data,status=status)
def authu(request): return request["tg_user"]
async def current(request):
    u=await user(request); uid=int(u["id"])
    db.exec("INSERT INTO aig_users(telegram_id,username,full_name) VALUES(%s,%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET username=EXCLUDED.username,full_name=EXCLUDED.full_name,updated_at=now()", (uid,u.get("username"),(u.get("first_name","")+" "+u.get("last_name","")).strip()))
    return uid
def notify(uid,kind,payload):
    db.exec("INSERT INTO aig_notifications(telegram_id,kind,payload) VALUES(%s,%s,%s)",(uid,kind,json.dumps(payload,ensure_ascii=False)))
def commission(price):
    if COMMISSION_MODE=="on_top": return round(price*COMMISSION_RATE/100,2)
    if COMMISSION_MODE=="inside": return round(price*COMMISSION_RATE/(100+COMMISSION_RATE),2)
    return round(COMMISSION_RATE,2)
async def session(request):
    uid=await current(request); u=db.one("SELECT * FROM aig_users WHERE telegram_id=%s",(uid,)); p=db.one("SELECT * FROM aig_partners WHERE telegram_id=%s",(uid,))
    return j({"ok":True,"user":u,"partner":p,"admin":uid==ADMIN_ID})
async def register_partner(request):
    uid=await current(request); body=await request.json(); name=str(body.get("name") or "").strip(); phone=str(body.get("phone") or "").strip()
    if not name:return j({"ok":False,"error":"business_name_required"},400)
    if not re.fullmatch(r"[+0-9() .-]{7,30}",phone):return j({"ok":False,"error":"invalid_phone"},400)
    u=authu(request)
    with db.conn() as c:
        c.execute("UPDATE aig_users SET role='partner',updated_at=now() WHERE telegram_id=%s",(uid,))
        p=c.execute("INSERT INTO aig_partners(telegram_id,phone) VALUES(%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET phone=EXCLUDED.phone RETURNING id",(uid,phone)).fetchone()[0]
        c.execute("INSERT INTO aig_companies(partner_id,name) SELECT %s,%s WHERE NOT EXISTS(SELECT 1 FROM aig_companies WHERE partner_id=%s)",(p,name,p))
    notify(uid,"partner_registered",{"company":name}); return j({"ok":True,"destination":"/master_cabinet.html"})
async def partner_profile(request):
    uid=await current(request); p=db.one("SELECT * FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    return j({"ok":True,"partner":p,"companies":db.all("SELECT * FROM aig_companies WHERE partner_id=%s AND NOT archived ORDER BY id",(p["id"],))})
async def partner_services(request):
    uid=await current(request); p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    return j({"ok":True,"items":db.all("SELECT s.*,c.name company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE c.partner_id=%s ORDER BY s.id DESC",(p["id"],))})
async def service_preview(request):
    uid=await current(request); p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    body=await request.json(); text=str(body.get("text") or "").strip()
    x,meta=await extract(text,"service creation")
    if not x.get("name") or x.get("price_amd") is None or x.get("price_type") not in ("fixed","from"):return j({"ok":False,"error":"service_data_incomplete"},400)
    pending={"kind":"service_create","company_id":body.get("company_id"),"data":x}
    db.exec("INSERT INTO aig_ai_sessions(telegram_id,context,pending) VALUES(%s,'PARTNER',%s) ON CONFLICT(telegram_id) DO UPDATE SET pending=EXCLUDED.pending,updated_at=now()",(uid,json.dumps(pending,ensure_ascii=False)))
    db.exec("INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,purpose,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s,%s)",(uid,meta["provider"],meta["model"],"extract","service_creation",meta["input_tokens"],meta["output_tokens"]))
    return j({"ok":True,"preview":x,"confirm_required":True})
async def service_confirm(request):
    uid=await current(request); p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,)); s=db.one("SELECT pending FROM aig_ai_sessions WHERE telegram_id=%s",(uid,))
    if not p or not s or not s["pending"]:return j({"ok":False,"error":"nothing_to_confirm"},400)
    pending=s["pending"]; data=pending["data"]; cid=pending.get("company_id")
    if cid is None:
        row=db.one("SELECT id FROM aig_companies WHERE partner_id=%s AND NOT archived ORDER BY id LIMIT 1",(p["id"],)); cid=row["id"] if row else None
    if cid is None or not db.one("SELECT id FROM aig_companies WHERE id=%s AND partner_id=%s AND NOT archived",(cid,p["id"])):return j({"ok":False,"error":"company_not_owned"},403)
    row=db.exec("INSERT INTO aig_services(company_id,name,price_type,price_amd,hours,at_client,territory,status) VALUES(%s,%s,%s,%s,%s,%s,%s,'CLASSIFICATION_PENDING') RETURNING id",(cid,data["name"],data["price_type"],float(data["price_amd"]),data.get("hours"),bool(data.get("at_client")),data.get("territory")),True)
    db.exec("INSERT INTO aig_service_applications(service_id) VALUES(%s)",(row["id"],)); db.exec("UPDATE aig_ai_sessions SET pending=NULL,updated_at=now() WHERE telegram_id=%s",(uid,))
    notify(ADMIN_ID,"service_application",{"service_id":row["id"]}); return j({"ok":True,"service_id":row["id"],"status":"CLASSIFICATION_PENDING"})
async def admin_services(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    return j({"ok":True,"items":db.all("SELECT s.*,c.name company_name,p.telegram_id FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id WHERE s.status IN('CLASSIFICATION_PENDING','PENDING_ADMIN') ORDER BY s.id")})
async def admin_service_decision(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    sid=int(request.match_info["id"]); body=await request.json(); action=body.get("action")
    if action not in ("approve","reject"):return j({"ok":False,"error":"invalid_action"},400)
    svc=db.one("SELECT s.*,p.telegram_id FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id WHERE s.id=%s",(sid,))
    if not svc:return j({"ok":False,"error":"service_not_found"},404)
    status="ACTIVE" if action=="approve" else "REJECTED"
    db.exec("UPDATE aig_services SET status=%s,rejection_reason=%s,updated_at=now() WHERE id=%s",(status,body.get("reason") if action=="reject" else None,sid))
    db.exec("UPDATE aig_service_applications SET status=%s,reviewed_at=now(),reviewer=%s WHERE service_id=%s",("APPROVED" if action=="approve" else "REJECTED",uid,sid))
    notify(int(svc["telegram_id"]),"service_decision",{"service_id":sid,"status":status}); return j({"ok":True,"status":status})
async def client_search(request):
    uid=await current(request); body=await request.json(); text=str(body.get("text") or "").strip(); x,meta=await extract(text,"client search")
    service=x.get("service") or text; city=x.get("city")
    db.exec("UPDATE aig_users SET role=CASE WHEN role='partner' THEN role ELSE 'client' END WHERE telegram_id=%s",(uid,))
    req=db.exec("INSERT INTO aig_client_requests(client_telegram_id,service_text,city,district) VALUES(%s,%s,%s,%s) RETURNING id",(uid,service,city,x.get("district")),True)
    items=db.all("SELECT s.id service_id,s.name service_name,s.price_type,s.price_amd,c.name partner_name,p.id partner_id,a.city FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id LEFT JOIN aig_addresses a ON a.id=s.address_id WHERE s.status='ACTIVE' AND s.name ILIKE %s AND (%s IS NULL OR a.city IS NULL OR a.city ILIKE %s) ORDER BY s.id LIMIT 3",(f"%{service}%",city,f"%{city}%"))
    db.exec("INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,purpose,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s,%s)",(uid,meta["provider"],meta["model"],"extract","client_search",meta["input_tokens"],meta["output_tokens"]))
    if not items: notify(ADMIN_ID,"potential_partner_research_needed",{"request_id":req["id"],"service":service,"city":city})
    return j({"ok":True,"request_id":req["id"],"items":items})
async def select_service(request):
    uid=await current(request); rid=int(request.match_info["id"]); body=await request.json(); sid=int(body.get("service_id",0))
    req=db.one("SELECT * FROM aig_client_requests WHERE id=%s AND client_telegram_id=%s",(rid,uid)); svc=db.one("SELECT s.*,p.id partner_id,p.telegram_id FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id WHERE s.id=%s AND s.status='ACTIVE'",(sid,))
    if not req or not svc:return j({"ok":False,"error":"service_not_found"},404)
    deadline=datetime.now(timezone.utc)+timedelta(minutes=3)
    n=db.exec("INSERT INTO aig_negotiations(request_id,service_id,partner_id,interest_deadline,status) VALUES(%s,%s,%s,%s,'waiting_partner') ON CONFLICT(request_id,partner_id) DO UPDATE SET service_id=EXCLUDED.service_id,interest_deadline=EXCLUDED.interest_deadline,status='waiting_partner' RETURNING id",(rid,sid,svc["partner_id"],deadline),True)
    notify(int(svc["telegram_id"]),"new_client",{"negotiation_id":n["id"],"service":svc["name"],"city":req["city"],"price":str(svc["price_amd"])})
    return j({"ok":True,"negotiation_id":n["id"],"interest_deadline":deadline.isoformat()})
async def partner_interest(request):
    uid=await current(request); nid=int(request.match_info["id"]); n=db.one("SELECT n.*,p.telegram_id FROM aig_negotiations n JOIN aig_partners p ON p.id=n.partner_id WHERE n.id=%s",(nid,))
    if not n or int(n["telegram_id"])!=uid:return j({"ok":False,"error":"forbidden"},403)
    if n["status"]!="waiting_partner" or (n["interest_deadline"] and n["interest_deadline"]<datetime.now(timezone.utc)):return j({"ok":False,"error":"interest_window_closed"},409)
    db.exec("UPDATE aig_negotiations SET status='active',partner_interest_at=now() WHERE id=%s",(nid,))
    return j({"ok":True,"status":"active"})
async def expire_interests():
    db.exec("UPDATE aig_negotiations SET status='expired' WHERE status='waiting_partner' AND interest_deadline<now()")
async def negotiation(request):
    uid=await current(request); nid=int(request.match_info["id"])
    n=db.one("SELECT n.*,s.name service_name,c.name company_name,p.telegram_id partner_telegram_id,r.client_telegram_id FROM aig_negotiations n JOIN aig_services s ON s.id=n.service_id JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=n.partner_id JOIN aig_client_requests r ON r.id=n.request_id WHERE n.id=%s",(nid,))
    if not n or uid not in (n["client_telegram_id"],n["partner_telegram_id"]):return j({"ok":False,"error":"forbidden"},403)
    return j({"ok":True,"negotiation":n,"messages":db.all("SELECT * FROM aig_messages WHERE negotiation_id=%s ORDER BY id",(nid,)),"booking":db.one("SELECT * FROM aig_bookings WHERE negotiation_id=%s",(nid,))})
async def send_message(request):
    uid=await current(request); nid=int(request.match_info["id"]); body=await request.json(); msg=str(body.get("message") or "").strip()
    if not msg:return j({"ok":False,"error":"message_required"},400)
    n=db.one("SELECT n.*,r.client_telegram_id,p.telegram_id partner_telegram_id FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_partners p ON p.id=n.partner_id WHERE n.id=%s",(nid,))
    if not n or uid not in (n["client_telegram_id"],n["partner_telegram_id"]) or n["status"]!="active":return j({"ok":False,"error":"negotiation_not_active"},403)
    role="client" if uid==n["client_telegram_id"] else "partner"; db.exec("INSERT INTO aig_messages(negotiation_id,sender_role,sender_telegram_id,message) VALUES(%s,%s,%s,%s)",(nid,role,uid,msg))
    x,meta=await extract(msg,"negotiation terms")
    if x.get("agreed_price") is not None:
        price=float(x["agreed_price"]); db.exec("UPDATE aig_negotiations SET agreed_price=%s,commission_base=%s,status='agreed' WHERE id=%s",(price,price,nid))
    else:
        db.exec("UPDATE aig_negotiations SET agreed_min=COALESCE(%s,agreed_min),agreed_max=COALESCE(%s,agreed_max) WHERE id=%s",(x.get("agreed_min"),x.get("agreed_max"),nid))
    db.exec("INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,purpose,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s,%s)",(uid,meta["provider"],meta["model"],"extract","negotiation",meta["input_tokens"],meta["output_tokens"]))
    return await negotiation(request)
async def book(request):
    uid=await current(request); nid=int(request.match_info["id"]); n=db.one("SELECT * FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id WHERE n.id=%s AND r.client_telegram_id=%s",(nid,uid))
    if not n or n["status"]!="agreed" or n["agreed_price"] is None:return j({"ok":False,"error":"booking_requires_exact_agreement"},400)
    price=float(n["agreed_price"]); b=db.exec("INSERT INTO aig_bookings(negotiation_id,status,agreed_price,commission_amd) VALUES(%s,'PENDING_PARTNER_CONFIRMATION',%s,%s) ON CONFLICT(negotiation_id) DO UPDATE SET agreed_price=EXCLUDED.agreed_price RETURNING id",(nid,price,commission(price)),True)
    return j({"ok":True,"booking_id":b["id"],"status":"PENDING_PARTNER_CONFIRMATION"})
async def partner_confirm_booking(request):
    uid=await current(request); bid=int(request.match_info["id"]); b=db.one("SELECT b.*,p.telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_partners p ON p.id=n.partner_id WHERE b.id=%s",(bid,))
    if not b or int(b["telegram_id"])!=uid:return j({"ok":False,"error":"forbidden"},403)
    db.exec("UPDATE aig_bookings SET status='PENDING_PAYMENT' WHERE id=%s AND status='PENDING_PARTNER_CONFIRMATION'",(bid,))
    return j({"ok":True,"status":"PENDING_PAYMENT","payment_url":IDRAM_PAYMENT_URL or None})
async def payment_webhook(request):
    body=await request.json(); bid=int(body.get("booking_id",0)); b=db.one("SELECT * FROM aig_bookings WHERE id=%s",(bid,))
    if not b or b["status"]!="PENDING_PAYMENT" or not body.get("confirmed"):return j({"ok":False},400)
    token=secrets.token_urlsafe(32); now=datetime.now(timezone.utc); exp=now+timedelta(hours=1)
    db.exec("UPDATE aig_bookings SET status='PAYMENT_CONFIRMED',payment_ref=%s,payment_confirmed_at=%s,qr_token=%s,qr_expires_at=%s WHERE id=%s",(body.get("payment_ref"),now,token,exp,bid))
    n=db.one("SELECT r.client_telegram_id,p.telegram_id partner_telegram_id FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_partners p ON p.id=n.partner_id WHERE n.id=%s",(b["negotiation_id"],))
    if n:
        notify(int(n["client_telegram_id"]),"payment_confirmed",{"booking_id":bid}); notify(int(n["partner_telegram_id"]),"payment_confirmed",{"booking_id":bid})
    return j({"ok":True,"status":"PAYMENT_CONFIRMED","qr_token":token,"qr_expires_at":exp.isoformat()})
async def contact(request):
    uid=await current(request); bid=int(request.match_info["id"]); b=db.one("SELECT b.*,r.client_telegram_id,p.telegram_id partner_telegram_id,pa.phone partner_phone FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_partners p ON p.id=n.partner_id JOIN aig_partners pa ON pa.id=p.id WHERE b.id=%s",(bid,))
    if not b or uid not in (b["client_telegram_id"],b["partner_telegram_id"]) or b["status"]!="PAYMENT_CONFIRMED":return j({"ok":False,"error":"contact_not_disclosed"},403)
    client=db.one("SELECT telegram_id FROM aig_users WHERE telegram_id=%s",(b["client_telegram_id"],))
    db.exec("INSERT INTO aig_booking_contacts(booking_id,client_telegram,partner_telegram,partner_phone) VALUES(%s,%s,%s,%s) ON CONFLICT(booking_id) DO NOTHING",(bid,b["client_telegram_id"],b["partner_telegram_id"],b["partner_phone"]))
    return j({"ok":True,"partner_phone":b["partner_phone"]})
async def checkin(request):
    uid=await current(request); token=request.match_info["token"]; b=db.one("SELECT b.*,p.telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_partners p ON p.id=n.partner_id WHERE b.qr_token=%s",(token,))
    if not b or int(b["telegram_id"])!=uid:return j({"ok":False,"error":"invalid_qr"},400)
    now=datetime.now(timezone.utc)
    if b["status"]!="PAYMENT_CONFIRMED" or (b["qr_expires_at"] and b["qr_expires_at"]<now):return j({"ok":False,"error":"qr_expired"},400)
    db.exec("INSERT INTO aig_booking_checkins(booking_id,partner_telegram_id) VALUES(%s,%s) ON CONFLICT(booking_id) DO NOTHING",(b["id"],uid)); db.exec("UPDATE aig_bookings SET status='IN_PROGRESS',checked_in_at=%s WHERE id=%s",(now,b["id"]))
    return j({"ok":True,"status":"IN_PROGRESS"})
async def complete(request):
    uid=await current(request); bid=int(request.match_info["id"]); b=db.one("SELECT b.*,p.telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_partners p ON p.id=n.partner_id WHERE b.id=%s",(bid,))
    if not b or int(b["telegram_id"])!=uid or b["status"]!="IN_PROGRESS":return j({"ok":False,"error":"completion_not_allowed"},403)
    db.exec("UPDATE aig_bookings SET status='SERVICE_COMPLETED',completed_at=now() WHERE id=%s",(bid,)); return j({"ok":True,"status":"SERVICE_COMPLETED"})
async def review(request):
    uid=await current(request); bid=int(request.match_info["id"]); body=await request.json(); b=db.one("SELECT b.*,r.client_telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_client_requests r ON r.id=n.request_id WHERE b.id=%s",(bid,))
    if not b or int(b["client_telegram_id"])!=uid or b["status"]!="SERVICE_COMPLETED":return j({"ok":False,"error":"review_not_allowed"},400)
    rating=int(body.get("rating",0))
    if not 1<=rating<=5:return j({"ok":False,"error":"invalid_rating"},400)
    db.exec("INSERT INTO aig_reviews(booking_id,client_telegram_id,rating,text) VALUES(%s,%s,%s,%s) ON CONFLICT(booking_id) DO NOTHING",(bid,uid,rating,body.get("text"))); return j({"ok":True})
async def arbitration_open(request):
    uid=await current(request); bid=int(request.match_info["id"]); body=await request.json()
    b=db.one("SELECT * FROM aig_bookings WHERE id=%s",(bid,))
    if not b:return j({"ok":False,"error":"booking_not_found"},404)
    a=db.exec("INSERT INTO aig_arbitrations(booking_id,opened_by,issue) VALUES(%s,%s,%s) RETURNING id",(bid,uid,str(body.get("issue") or "").strip()),True)
    db.exec("UPDATE aig_bookings SET status='ARBITRATION' WHERE id=%s",(bid,)); notify(ADMIN_ID,"arbitration_opened",{"arbitration_id":a["id"],"booking_id":bid}); return j({"ok":True,"arbitration_id":a["id"]})
async def admin_arbitration(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    aid=int(request.match_info["id"]); body=await request.json(); a=db.one("SELECT * FROM aig_arbitrations WHERE id=%s",(aid,))
    if not a:return j({"ok":False,"error":"arbitration_not_found"},404)
    db.exec("UPDATE aig_arbitrations SET status='RESOLVED',resolution=%s,closed_at=now() WHERE id=%s",(body.get("resolution"),aid)); db.exec("UPDATE aig_bookings SET status='SERVICE_COMPLETED' WHERE id=%s AND status='ARBITRATION'",(a["booking_id"],)); return j({"ok":True})
async def notifications(request):
    uid=await current(request); return j({"ok":True,"items":db.all("SELECT * FROM aig_notifications WHERE telegram_id=%s ORDER BY id DESC LIMIT 100",(uid,))})
async def admin_query(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    body=await request.json(); q=str(body.get("text") or "").lower()
    if any(x in q for x in ("заяв","application","հայտ")): return await admin_services(request)
    if "ai" in q or "ծախս" in q:
        row=db.one("SELECT count(*) n,coalesce(sum(input_tokens+output_tokens),0) tokens,coalesce(sum(usd),0) usd FROM aig_ai_costs WHERE created_at::date=current_date")
        return j({"ok":True,"answer":f"AI operations today: {row['n']}; tokens: {row['tokens']}; USD: {row['usd']}"})
    if "arbitr" in q or "արբիտ" in q:return j({"ok":True,"items":db.all("SELECT * FROM aig_arbitrations WHERE status='OPEN' ORDER BY id")})
    return j({"ok":True,"answer":"Data Core query accepted.","available":["applications","AI costs","arbitrations"]})
def setup(app):
    routes=[("GET","/api/session",session),("POST","/api/partner/register",register_partner),("GET","/api/partner/profile",partner_profile),("GET","/api/partner/services",partner_services),("POST","/api/partner/services/preview",service_preview),("POST","/api/partner/services/confirm",service_confirm),("GET","/api/admin/services",admin_services),("POST","/api/admin/services/{id}/decision",admin_service_decision),("POST","/api/admin/ai",admin_query),("POST","/api/client/search",client_search),("POST","/api/client/requests/{id}/select",select_service),("POST","/api/negotiations/{id}/interest",partner_interest),("GET","/api/negotiations/{id}",negotiation),("POST","/api/negotiations/{id}/messages",send_message),("POST","/api/negotiations/{id}/book",book),("POST","/api/bookings/{id}/confirm",partner_confirm_booking),("POST","/api/payments/webhook",payment_webhook),("GET","/api/bookings/{id}/contact",contact),("POST","/api/bookings/{token}/checkin",checkin),("POST","/api/bookings/{id}/complete",complete),("POST","/api/bookings/{id}/review",review),("POST","/api/bookings/{id}/arbitration",arbitration_open),("POST","/api/admin/arbitrations/{id}/resolve",admin_arbitration),("GET","/api/notifications",notifications)]
    for method,path,fn in routes:getattr(app.router,"add_"+method.lower())(path,fn)
