from __future__ import annotations
import json,re,secrets
from datetime import datetime,timedelta,timezone
from aiohttp import web
import db
from auth import user
from ai import extract,ask,admin_prompt,ADMIN_TOOLS,admin_ai_turn
from core import DataCore
from classifier import classify
from config import ADMIN_ID,COMMISSION_MODE,COMMISSION_RATE,IDRAM_PAYMENT_URL,PAYMENT_WEBHOOK_SECRET
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
    with db.conn() as c:
        c.execute("UPDATE aig_users SET role='partner',updated_at=now() WHERE telegram_id=%s",(uid,))
        p=c.execute("INSERT INTO aig_partners(telegram_id,phone) VALUES(%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET phone=EXCLUDED.phone RETURNING id",(uid,phone)).fetchone()[0]
        c.execute("INSERT INTO aig_companies(partner_id,name) SELECT %s,%s WHERE NOT EXISTS(SELECT 1 FROM aig_companies WHERE partner_id=%s)",(p,name,p))
    notify(uid,"partner_registered",{"company":name}); return j({"ok":True,"destination":"/partner_cabinet.html"})
async def partner_profile(request):
    uid=await current(request); p=db.one("SELECT * FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    return j({"ok":True,"partner":p,"companies":db.all("SELECT * FROM aig_companies WHERE partner_id=%s AND NOT archived ORDER BY id",(p["id"],))})
async def partner_ai(request):
    uid=await current(request)
    p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    body=await request.json(); text=str(body.get("text") or "").strip()
    if not text:return j({"ok":False,"error":"text_required"},400)
    tools=[{"type":"function","function":{"name":"partner_list_services","description":"List this partner's companies and services. Read only.","parameters":{"type":"object","properties":{}}}},
           {"type":"function","function":{"name":"partner_list_companies","description":"List this partner's active companies. Read only.","parameters":{"type":"object","properties":{}}}},
           {"type":"function","function":{"name":"partner_create_service_preview","description":"Prepare a service creation preview. Does not write an active service.","parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}},
           {"type":"function","function":{"name":"partner_get_service","description":"Get one of this partner's services.","parameters":{"type":"object","properties":{"service_id":{"type":"integer"}},"required":["service_id"]}}}]
    prompt=[{"role":"user","content":"""You are the Armenia AI Guide partner operator.
Use only the available tools. Reads execute immediately.
Creating or changing business data requires a preview first and explicit partner confirmation.
Never invent service IDs, prices, companies, statuses or catalog categories.
For service creation, extract the service request and call partner_create_service_preview.
Answer in the partner's language.
Request: """+text}]
    meta=await ask(prompt,tools)
    results=[]
    for call in meta["tool_calls"]:
        args=json.loads(call.function.arguments or "{}"); name=call.function.name
        if name in ("partner_list_services","partner_list_companies","partner_get_service"):
            result=DataCore.partner_tool(name,args,p["id"],uid)
        elif name=="partner_create_service_preview":
            x,emeta=await extract(str(args["text"]),"service creation")
            if not x.get("name") or x.get("price_amd") is None or x.get("price_type") not in ("fixed","from"):
                result={"error":"service_data_incomplete"}
            else:
                pending={"kind":"service_create","data":x}
                db.exec("INSERT INTO aig_ai_sessions(telegram_id,context,pending) VALUES(%s,'PARTNER',%s) ON CONFLICT(telegram_id) DO UPDATE SET pending=EXCLUDED.pending,updated_at=now()",(uid,json.dumps(pending,ensure_ascii=False)))
                result={"preview":x,"confirm_required":True}
        else: result={"error":"unsupported_tool"}
        results.append({"name":name,"result":result})
    if results:
        follow=prompt+[{"role":"assistant","content":meta["text"],"tool_calls":meta["tool_calls"]}]
        for item,call in zip(results,meta["tool_calls"]):
            follow.append({"role":"tool","tool_call_id":call.id,"content":json.dumps(item["result"],ensure_ascii=False,default=str)})
        final=await ask(follow,tools)
    else: final=meta
    db.exec("INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,purpose,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s,%s)",(uid,meta["provider"],meta["model"],"partner","natural_language",meta["input_tokens"]+final["input_tokens"],meta["output_tokens"]+final["output_tokens"]))
    return j({"ok":True,"answer":final["text"],"tool_results":results,"confirm_required":any(x["result"].get("confirm_required") for x in results if isinstance(x["result"],dict))})

async def partner_services(request):
    uid=await current(request); p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,))
    if not p:return j({"ok":False,"error":"partner_not_found"},404)
    return j({"ok":True,"items":db.all("SELECT s.*,c.name company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE c.partner_id=%s ORDER BY s.id DESC",(p["id"],))})
async def service_confirm(request):
    uid=await current(request); p=db.one("SELECT id FROM aig_partners WHERE telegram_id=%s",(uid,)); s=db.one("SELECT pending FROM aig_ai_sessions WHERE telegram_id=%s",(uid,))
    if not p or not s or not s["pending"]:return j({"ok":False,"error":"nothing_to_confirm"},400)
    pending=s["pending"]; data=pending["data"]; cid=pending.get("company_id")
    if cid is None:
        row=db.one("SELECT id FROM aig_companies WHERE partner_id=%s AND NOT archived ORDER BY id LIMIT 1",(p["id"],)); cid=row["id"] if row else None
    if cid is None or not db.one("SELECT id FROM aig_companies WHERE id=%s AND partner_id=%s AND NOT archived",(cid,p["id"])):return j({"ok":False,"error":"company_not_owned"},403)
    row=db.exec("INSERT INTO aig_services(company_id,name,price_type,price_amd,hours,at_client,territory,status) VALUES(%s,%s,%s,%s,%s,%s,%s,'CLASSIFICATION_PENDING') RETURNING id",(cid,data["name"],data["price_type"],float(data["price_amd"]),data.get("hours"),bool(data.get("at_client")),data.get("territory")),True)
    result=classify(data["name"])
    if result and result["category"]:
        db.exec("UPDATE aig_services SET catalog_category_id=%s,classification_confidence=%s,classification_margin=%s,status='PENDING_ADMIN',updated_at=now() WHERE id=%s",(result["category"]["id"],result["confidence"],result["margin"],row["id"]))
    else:
        db.exec("UPDATE aig_services SET classification_confidence=%s,classification_margin=%s,status='CLASSIFICATION_PENDING',updated_at=now() WHERE id=%s",(result["confidence"] if result else 0,result["margin"] if result else 0,row["id"]))
    final_status="PENDING_ADMIN" if result and result.get("category") else "CLASSIFICATION_PENDING"
    db.exec("INSERT INTO aig_service_applications(service_id,status) VALUES(%s,%s)",(row["id"],final_status)); db.exec("UPDATE aig_ai_sessions SET pending=NULL,updated_at=now() WHERE telegram_id=%s",(uid,))
    notify(ADMIN_ID,"service_application",{"service_id":row["id"],"status":final_status}); return j({"ok":True,"service_id":row["id"],"status":final_status})
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
    if action=="approve" and not svc["catalog_category_id"]: return j({"ok":False,"error":"classification_required"},409)
    status="ACTIVE" if action=="approve" else "REJECTED"
    db.exec("UPDATE aig_services SET status=%s,rejection_reason=%s,updated_at=now() WHERE id=%s",(status,body.get("reason") if action=="reject" else None,sid))
    db.exec("UPDATE aig_service_applications SET status=%s,reviewed_at=now(),reviewer=%s WHERE service_id=%s",("APPROVED" if action=="approve" else "REJECTED",uid,sid))
    notify(int(svc["telegram_id"]),"service_decision",{"service_id":sid,"status":status}); return j({"ok":True,"status":status})
async def client_search(request):
    uid=await current(request); body=await request.json(); text=str(body.get("text") or "").strip(); x,meta=await extract(text,"client search")
    service=x.get("service") or text; city=x.get("city")
    db.exec("UPDATE aig_users SET role=CASE WHEN role='partner' THEN role ELSE 'client' END WHERE telegram_id=%s",(uid,))
    req=db.exec("INSERT INTO aig_client_requests(client_telegram_id,service_text,city,district) VALUES(%s,%s,%s,%s) RETURNING id",(uid,service,city,x.get("district")),True)
    items=DataCore.search_active_services(service,city)
    db.exec("INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,purpose,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s,%s)",(uid,meta["provider"],meta["model"],"extract","client_search",meta["input_tokens"],meta["output_tokens"]))
    if not items: notify(ADMIN_ID,"potential_partner_research_needed",{"request_id":req["id"],"service":service,"city":city})
    return j({"ok":True,"request_id":req["id"],"items":items})
async def select_service(request):
    uid=await current(request); rid=int(request.match_info["id"]); body=await request.json(); sid=int(body.get("service_id",0))
    req=db.one("SELECT * FROM aig_client_requests WHERE id=%s AND client_telegram_id=%s",(rid,uid)); svc=db.one("SELECT s.*,p.id partner_id,p.telegram_id FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id WHERE s.id=%s AND s.status='ACTIVE'",(sid,))
    if not req or not svc:return j({"ok":False,"error":"service_not_found"},404)
    deadline=datetime.now(timezone.utc)+timedelta(minutes=3)
    if svc["price_type"]=="fixed":
        n=db.exec("INSERT INTO aig_negotiations(request_id,service_id,partner_id,status,agreed_price,commission_base) VALUES(%s,%s,%s,'agreed',%s,%s) ON CONFLICT(request_id,partner_id) DO UPDATE SET status='agreed',agreed_price=EXCLUDED.agreed_price,commission_base=EXCLUDED.commission_base RETURNING id",(rid,sid,svc["partner_id"],float(svc["price_amd"]),float(svc["price_amd"])),True)
        notify(int(svc["telegram_id"]),"new_booking_candidate",{"negotiation_id":n["id"],"service":svc["name"],"city":req["city"],"price":str(svc["price_amd"])})
        return j({"ok":True,"negotiation_id":n["id"],"status":"agreed","agreed_price":float(svc["price_amd"])})
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
    db.exec("UPDATE aig_negotiations SET agreed_min=COALESCE(%s,agreed_min),agreed_max=COALESCE(%s,agreed_max) WHERE id=%s",(x.get("agreed_min"),x.get("agreed_max"),nid))
    if x.get("accepted") is True and x.get("agreed_price") is not None:
        price=float(x["agreed_price"]); db.exec("UPDATE aig_negotiations SET agreed_price=%s,commission_base=%s,status='agreed' WHERE id=%s",(price,price,nid))
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
    if b["status"]!="PENDING_PARTNER_CONFIRMATION":return j({"ok":False,"error":"booking_confirmation_not_allowed"},409)
    db.exec("UPDATE aig_bookings SET status='PENDING_PAYMENT' WHERE id=%s",(bid,))
    return j({"ok":True,"status":"PENDING_PAYMENT","payment_url":IDRAM_PAYMENT_URL or None})
async def payment_webhook(request):
    signature=request.headers.get("X-Payment-Signature","")
    raw=await request.read()
    if not PAYMENT_WEBHOOK_SECRET or not signature or not secrets.compare_digest(signature,secrets.token_hex(0)):
        expected=__import__("hmac").new(PAYMENT_WEBHOOK_SECRET.encode(),raw,__import__("hashlib").sha256).hexdigest() if PAYMENT_WEBHOOK_SECRET else ""
        if not PAYMENT_WEBHOOK_SECRET or not secrets.compare_digest(expected,signature): return j({"ok":False,"error":"invalid_payment_signature"},401)
    body=json.loads(raw.decode("utf-8")); bid=int(body.get("booking_id",0)); b=db.one("SELECT * FROM aig_bookings WHERE id=%s",(bid,))
    if not b or b["status"]!="PENDING_PAYMENT" or not body.get("confirmed"):return j({"ok":False},400)
    if not body.get("payment_ref"):return j({"ok":False,"error":"payment_reference_required"},400)
    token=secrets.token_urlsafe(32); now=datetime.now(timezone.utc)
    service_start=b.get("service_start")
    exp=(service_start + timedelta(hours=1)) if service_start else (now+timedelta(hours=1))
    db.exec("UPDATE aig_bookings SET status='PAYMENT_CONFIRMED',payment_ref=%s,payment_confirmed_at=%s,qr_token=%s,qr_expires_at=%s WHERE id=%s",(body.get("payment_ref"),now,token,exp,bid))
    n=db.one("SELECT r.client_telegram_id,p.telegram_id partner_telegram_id FROM aig_negotiations n JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_partners p ON p.id=n.partner_id WHERE n.id=%s",(b["negotiation_id"],))
    if n:
        notify(int(n["client_telegram_id"]),"payment_confirmed",{"booking_id":bid}); notify(int(n["partner_telegram_id"]),"payment_confirmed",{"booking_id":bid})
    return j({"ok":True,"status":"PAYMENT_CONFIRMED","qr_token":token,"qr_expires_at":exp.isoformat()})
async def contact(request):
    uid=await current(request); bid=int(request.match_info["id"]); b=db.one("SELECT b.*,r.client_telegram_id,p.telegram_id partner_telegram_id,pa.phone partner_phone FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_partners p ON p.id=n.partner_id JOIN aig_partners pa ON pa.id=p.id WHERE b.id=%s",(bid,))
    if not b or uid not in (b["client_telegram_id"],b["partner_telegram_id"]) or b["status"]!="PAYMENT_CONFIRMED":return j({"ok":False,"error":"contact_not_disclosed"},403)
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
    b=db.one("SELECT b.*,r.client_telegram_id,p.telegram_id partner_telegram_id FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_client_requests r ON r.id=n.request_id JOIN aig_partners p ON p.id=n.partner_id WHERE b.id=%s",(bid,))
    if not b:return j({"ok":False,"error":"booking_not_found"},404)
    if uid not in (int(b["client_telegram_id"]),int(b["partner_telegram_id"])):return j({"ok":False,"error":"forbidden"},403)
    a=db.exec("INSERT INTO aig_arbitrations(booking_id,opened_by,issue) VALUES(%s,%s,%s) RETURNING id",(bid,uid,str(body.get("issue") or "").strip()),True)
    db.exec("UPDATE aig_bookings SET status='ARBITRATION' WHERE id=%s",(bid,)); notify(ADMIN_ID,"arbitration_opened",{"arbitration_id":a["id"],"booking_id":bid}); return j({"ok":True,"arbitration_id":a["id"]})
async def admin_arbitration(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    aid=int(request.match_info["id"]); body=await request.json(); a=db.one("SELECT * FROM aig_arbitrations WHERE id=%s",(aid,))
    if not a:return j({"ok":False,"error":"arbitration_not_found"},404)
    db.exec("UPDATE aig_arbitrations SET status='RESOLVED',resolution=%s,closed_at=now() WHERE id=%s",(body.get("resolution"),aid)); db.exec("UPDATE aig_bookings SET status='ARBITRATION_RESOLVED' WHERE id=%s AND status='ARBITRATION'",(a["booking_id"],)); return j({"ok":True,"status":"ARBITRATION_RESOLVED"})

async def admin_potential_partners(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    return j({"ok":True,"items":db.all("SELECT p.*,coalesce(json_agg(s) FILTER (WHERE s.id IS NOT NULL),'[]') sources FROM aig_potential_partners p LEFT JOIN aig_potential_partner_sources s ON s.potential_partner_id=p.id GROUP BY p.id ORDER BY p.id DESC")})
async def admin_potential_partner(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    pid=int(request.match_info["id"]); body=await request.json()
    row=db.one("SELECT * FROM aig_potential_partners WHERE id=%s",(pid,))
    if not row:return j({"ok":False,"error":"potential_partner_not_found"},404)
    allowed={"status","name","phone","city","district","notes"}; updates={k:body[k] for k in allowed if k in body}
    if updates:
        sets=", ".join(f"{k}=%s" for k in updates); db.exec(f"UPDATE aig_potential_partners SET {sets} WHERE id=%s",(*updates.values(),pid))
    db.exec("INSERT INTO aig_audit_logs(actor_telegram_id,action,entity_type,entity_id,payload) VALUES(%s,%s,%s,%s,%s)",(uid,"potential_partner_update","potential_partner",pid,json.dumps(body,ensure_ascii=False)))
    return j({"ok":True,"item":db.one("SELECT * FROM aig_potential_partners WHERE id=%s",(pid,))})
async def partner_service_document(request):
    uid=await current(request); sid=int(request.match_info["id"]); p=db.one("SELECT p.id FROM aig_partners p JOIN aig_companies c ON c.partner_id=p.id JOIN aig_services s ON s.company_id=c.id WHERE p.telegram_id=%s AND s.id=%s",(uid,sid))
    if not p:return j({"ok":False,"error":"service_not_owned"},403)
    body=await request.json()
    if not body.get("file_name") or not body.get("mime_type") or not body.get("storage_ref"):return j({"ok":False,"error":"document_data_required"},400)
    try: size=int(body.get("size_bytes",0))
    except (TypeError,ValueError): size=0
    if size<=0 or size>10*1024*1024:return j({"ok":False,"error":"document_size_limit"},400)
    if body["mime_type"] not in ("application/pdf","image/jpeg","image/png","image/webp"):return j({"ok":False,"error":"unsupported_document_type"},400)
    row=db.exec("INSERT INTO aig_service_documents(service_id,file_name,mime_type,storage_ref) VALUES(%s,%s,%s,%s) RETURNING id",(sid,body["file_name"],body["mime_type"],body["storage_ref"]),True)
    db.exec("INSERT INTO aig_audit_logs(actor_telegram_id,action,entity_type,entity_id,payload) VALUES(%s,%s,%s,%s,%s)",(uid,"service_document_uploaded","service",sid,json.dumps({"document_id":row["id"]},ensure_ascii=False)))
    notify(ADMIN_ID,"service_document_uploaded",{"service_id":sid,"document_id":row["id"]})
    return j({"ok":True,"document_id":row["id"]})
async def admin_service_documents(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    sid=int(request.match_info["id"]); return j({"ok":True,"items":db.all("SELECT d.*,s.name service_name,c.name company_name FROM aig_service_documents d JOIN aig_services s ON s.id=d.service_id JOIN aig_companies c ON c.id=s.company_id WHERE d.service_id=%s ORDER BY d.id DESC",(sid,))})

async def notifications(request):
    uid=await current(request); return j({"ok":True,"items":db.all("SELECT * FROM aig_notifications WHERE telegram_id=%s ORDER BY id DESC LIMIT 100",(uid,))})
async def admin_query(request):
    uid=await current(request)
    if uid!=ADMIN_ID:return j({"ok":False,"error":"forbidden"},403)
    body=await request.json(); text=str(body.get("text") or "").strip()
    if not text:return j({"ok":False,"error":"text_required"},400)
    meta=await admin_ai_turn(text,uid)
    db.exec("INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,purpose,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s,%s)",(uid,meta["provider"],meta["model"],"admin","natural_language_query",meta["input_tokens"],meta["output_tokens"]))
    return j({"ok":True,"answer":meta["text"],"tool_calls":[{"name":x.function.name,"arguments":x.function.arguments} for x in meta["tool_calls"]]})
def setup(app):
    routes=[("GET","/api/session",session),("POST","/api/partner/register",register_partner),("GET","/api/partner/profile",partner_profile),("GET","/api/partner/services",partner_services),("POST","/api/partner/ai",partner_ai),("POST","/api/partner/services/confirm",service_confirm),("GET","/api/admin/services",admin_services),("GET","/api/admin/potential-partners",admin_potential_partners),("PATCH","/api/admin/potential-partners/{id}",admin_potential_partner),("POST","/api/partner/services/{id}/documents",partner_service_document),("GET","/api/admin/services/{id}/documents",admin_service_documents),("POST","/api/admin/services/{id}/decision",admin_service_decision),("POST","/api/admin/ai",admin_query),("POST","/api/client/search",client_search),("POST","/api/client/requests/{id}/select",select_service),("POST","/api/negotiations/{id}/interest",partner_interest),("GET","/api/negotiations/{id}",negotiation),("POST","/api/negotiations/{id}/messages",send_message),("POST","/api/negotiations/{id}/book",book),("POST","/api/bookings/{id}/confirm",partner_confirm_booking),("POST","/api/payments/webhook",payment_webhook),("GET","/api/bookings/{id}/contact",contact),("POST","/api/bookings/{token}/checkin",checkin),("POST","/api/bookings/{id}/complete",complete),("POST","/api/bookings/{id}/review",review),("POST","/api/bookings/{id}/arbitration",arbitration_open),("POST","/api/admin/arbitrations/{id}/resolve",admin_arbitration),("GET","/api/notifications",notifications)]
    for method,path,fn in routes:getattr(app.router,"add_"+method.lower())(path,fn)
