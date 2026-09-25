"""End-to-end Armenia AI Guide marketplace flow."""
from __future__ import annotations
import json, os, secrets, logging
from decimal import Decimal
from datetime import datetime, date
import psycopg
from aiohttp import web
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError
from booking_schema import ensure_booking_schema
import qr_util
from idram import IdramProvider
from ai_negotiator import AINegotiator
import data_core

def _db_url():
    value=os.getenv('DATABASE_URL','').strip()
    if not value: raise RuntimeError('DATABASE_URL is not configured')
    return value

def _safe(v):
    if isinstance(v,(datetime,date)): return v.isoformat()
    if isinstance(v,Decimal): return float(v)
    if isinstance(v,dict): return {k:_safe(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)): return [_safe(x) for x in v]
    return v

def _rows(sql,params=()):
    with psycopg.connect(_db_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql,params); cols=[d.name for d in cur.description] if cur.description else []
            rows=[_safe(dict(zip(cols,row))) for row in cur.fetchall()]
    return rows

def _one(sql,params=()):
    rows=_rows(sql,params); return rows[0] if rows else None

def _exec(sql,params=(),returning=False):
    with psycopg.connect(_db_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql,params); result=None
            if returning:
                row=cur.fetchone(); cols=[d.name for d in cur.description] if cur.description else []
                result=_safe(dict(zip(cols,row))) if row else None
        conn.commit()
    return result

def _uid(request):
    raw=request.headers.get('X-Telegram-Init-Data','').strip()
    if not raw: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':'telegram_init_data_required'}),content_type='application/json')
    try:
        data=validate_telegram_webapp_init_data(raw,request.app.get('stage3_bot_token') or os.getenv('BOT_TOKEN','')); return int(data['id'])
    except (TelegramWebAppAuthError,ValueError,TypeError,KeyError) as exc:
        raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':str(exc)}),content_type='application/json')

def _json(value): return json.dumps(value or {},ensure_ascii=False)

def _commission(service):
    """Resolve the tariff with a 3-level cascade:
       1) per-service override (admin set it on the service),
       2) subcategory override (categories.commission_*),
       3) direction default (master_categories.commission_* = "initial settings").
    NULL at a level means "inherit the level below it".
    """
    # 1) per-service override
    stype=service.get('commission_type')
    if stype:
        try:sval=float(service.get('commission_value') or 0)
        except Exception:sval=0.0
        return str(stype),max(0.0,sval)
    # 2)+3) subcategory override, else direction default
    cat_id=service.get('category_id')
    if cat_id:
        row=_one("""
            SELECT COALESCE(c.commission_type, m.commission_type, 'on_top') AS ctype,
                   COALESCE(c.commission_value, m.commission_value, 10)      AS cvalue
            FROM categories c
            LEFT JOIN master_categories m ON m.id=c.master_category_id
            WHERE c.id=%s
        """,(cat_id,))
        if row:
            ctype=str(row.get('ctype') or 'on_top')
            try:value=float(row.get('cvalue') or 0)
            except Exception:value=0.0
            return ctype,max(0.0,value)
    data=service.get('data_json') or {}
    if isinstance(data,str):
        try:data=json.loads(data)
        except Exception:data={}
    try:rate=float(data.get('commission_percent',10))
    except Exception:rate=10.0
    return 'on_top',max(0.0,min(100.0,rate))

def _price_and_commission(final_price,service):
    ctype,value=_commission(service)
    if ctype=='fixed':
        commission=round(value,2)
        return commission,round(final_price,2),round(final_price-commission,2)
    if ctype=='inside':
        commission=round(final_price*value/100,2)
        return commission,round(final_price,2),round(final_price-commission,2)
    commission=round(final_price*value/100,2)
    return commission,round(final_price+commission,2),round(final_price,2)

async def client_search(request):
    uid=_uid(request); data=await request.json(); q=str(data.get('query') or data.get('text') or '').strip(); city=str(data.get('city') or '').strip(); category_id=int(data.get('category_id') or 0)
    params=[]; where=["s.status='approved'","p.status='approved'","pd.status='approved'"]
    if q:
        like=f'%{q}%'; params += [like]*5
        where.append("(LOWER(s.name) LIKE LOWER(%s) OR LOWER(s.description) LIKE LOWER(%s) OR LOWER(p.business_name) LIKE LOWER(%s) OR LOWER(c.name_am) LIKE LOWER(%s) OR LOWER(c.name_ru) LIKE LOWER(%s))")
    if city: params.append(city); where.append("EXISTS(SELECT 1 FROM partner_locations pl WHERE pl.partner_id=p.id AND LOWER(COALESCE(pl.city,''))=LOWER(%s))")
    if category_id: params.append(category_id); where.append('s.category_id=%s')
    sql=f"""SELECT s.id service_id,s.partner_id,s.category_id,s.name service_name,s.description,s.price,s.currency,s.duration_minutes,s.data_json,p.business_name,p.business_description,p.contact_share_policy,c.name_am category_name_am,c.name_ru category_name_ru,COALESCE((SELECT pl.city FROM partner_locations pl WHERE pl.partner_id=p.id ORDER BY pl.id LIMIT 1),'') city,COALESCE((SELECT pl.marz FROM partner_locations pl WHERE pl.partner_id=p.id ORDER BY pl.id LIMIT 1),'') marz FROM services s JOIN partners p ON p.id=s.partner_id JOIN partner_direction_categories pdc ON pdc.category_id=s.category_id JOIN partner_directions pd ON pd.id=pdc.partner_direction_id AND pd.partner_id=p.id LEFT JOIN categories c ON c.id=s.category_id WHERE {' AND '.join(where)} GROUP BY s.id,p.id,c.id,p.business_name,p.business_description,p.contact_share_policy ORDER BY s.created_at DESC LIMIT 20"""
    items=_rows(sql,tuple(params))
    for item in items: item.pop('data_json',None); item.pop('business_description',None)
    return web.json_response({'ok':True,'items':items,'query':q,'client_id':uid})

async def create_request(request):
    uid=_uid(request); data=await request.json(); summary=str(data.get('summary') or data.get('query') or '').strip(); city=str(data.get('city') or '').strip() or None; lang=str(data.get('language') or 'hy')[:5]; profile=data.get('preferences') or {}
    item=_exec("INSERT INTO service_requests(client_id,status,language,city,summary,preferences_json) VALUES(%s,'searching',%s,%s,%s,%s::jsonb) RETURNING *",(uid,lang,city,summary,_json(profile)),True)
    return web.json_response({'ok':True,'request':item})

async def select_candidate(request):
    uid=_uid(request); rid=int(request.match_info['request_id']); data=await request.json(); service_id=int(data.get('service_id') or 0)
    item=_one("SELECT sr.id request_id,sr.status,s.id service_id,s.partner_id,s.name service_name,s.price,s.currency,s.duration_minutes,p.business_name,p.contact_share_policy FROM service_requests sr JOIN services s ON s.id=%s JOIN partners p ON p.id=s.partner_id WHERE sr.id=%s AND sr.client_id=%s AND s.status='approved' AND p.status='approved'",(service_id,rid,uid))
    if not item:return web.json_response({'ok':False,'error':'candidate_not_found'},status=404)
    _exec("INSERT INTO request_candidates(request_id,partner_id,service_id,rank_score,status) VALUES(%s,%s,%s,100,'selected') ON CONFLICT(request_id,partner_id,service_id) DO UPDATE SET status='selected'",(rid,item['partner_id'],service_id))
    negotiation=_exec("INSERT INTO negotiations(request_id,client_id,partner_id,state_json) VALUES(%s,%s,%s,%s::jsonb) RETURNING *",(rid,uid,item['partner_id'],_json({'service_id':service_id,'service_name':item['service_name'],'price':float(item['price'] or 0),'currency':item['currency'],'client_agreed':False,'partner_agreed':False})),True)
    _exec("UPDATE service_requests SET status='negotiating',updated_at=NOW() WHERE id=%s",(rid,))
    _exec("INSERT INTO negotiation_messages(negotiation_id,sender_role,message,data_json) VALUES(%s,'ai',%s,%s::jsonb)",(negotiation['id'],f"Ընտրված ծառայությունն է՝ {item['service_name']}։ Գինը՝ {item['price']} {item['currency']}։ Կարող եք գրել ձեր ցանկությունները։",_json({'type':'selection'})))
    return web.json_response({'ok':True,'negotiation':negotiation,'candidate':item})

async def negotiation_get(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id'])
    n=data_core.get_negotiation(nid, actor_role='client', actor_id=uid)
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_found'},status=404)
    return web.json_response({'ok':True,'negotiation':n,'messages':data_core.get_negotiation_messages(nid, actor_role='client', actor_id=uid)})

def _state(n):
    s=n.get('state_json') or {}
    if isinstance(s,str):
        try:s=json.loads(s)
        except Exception:s={}
    return s

def _get_ai(request):
    """Return the shared GroqAI instance (or build a keyless fallback)."""
    ai=request.app.get('ai')
    if ai is None:
        from ai_service import GroqAI
        ai=GroqAI(); request.app['ai']=ai
    return ai

def _negotiator_hooks(actor_role, actor_id):
    """DB hooks bound to the current negotiation actor."""
    def insert_msg(nid,role,sender_id,text):
        return data_core.append_negotiation_message(nid,role,sender_id,text)
    def update_neg(nid,state,status):
        return data_core.update_negotiation(
            nid,state,status,actor_role=actor_role,actor_id=actor_id
        )
    def update_request(rid,status):
        # Request status is changed only through an already authorized
        # negotiation actor; do not expose a free-form request write to AI.
        n=data_core.get_negotiation(
            next((int(rid) for _ in [0]), 0),
            actor_role=actor_role,actor_id=actor_id
        )
        return data_core.update_request_status(rid,status) if n else None
    def insert_ai_msg(nid,role,text,data):
        return data_core.append_negotiation_message(nid,role,None,text,data)
    return dict(insert_msg=insert_msg,update_neg=update_neg,update_request=update_request,insert_ai_msg=insert_ai_msg)

async def negotiation_client_message(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); data=await request.json(); text=str(data.get('message') or '').strip()
    if not text:return web.json_response({'ok':False,'error':'message_required'},status=400)
    n=data_core.get_negotiation(nid, actor_role='client', actor_id=uid)
    if not n or n.get('status') != 'active':return web.json_response({'ok':False,'error':'negotiation_not_active'},status=400)
    negotiator=AINegotiator(_get_ai(request))
    await negotiator.handle(n,'client',uid,text,**_negotiator_hooks())
    return await negotiation_get(request)

async def partner_negotiations(request):
    uid=_uid(request); partner=_one('SELECT * FROM partners WHERE user_id=%s',(uid,))
    if not partner:return web.json_response({'ok':False,'error':'partner_not_found'},status=404)
    items=_rows("SELECT n.id,n.request_id,n.status,n.state_json,n.updated_at,sr.summary,sr.city,s.name service_name,p.business_name FROM negotiations n JOIN service_requests sr ON sr.id=n.request_id LEFT JOIN services s ON s.id=(n.state_json->>'service_id')::bigint JOIN partners p ON p.id=n.partner_id WHERE n.partner_id=%s AND n.status='active' ORDER BY n.updated_at DESC",(partner['id'],))
    return web.json_response({'ok':True,'items':items})

async def partner_negotiation_messages(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); n=data_core.get_negotiation(nid, actor_role='partner', actor_id=uid)
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_found'},status=404)
    return web.json_response({'ok':True,'negotiation':n,'messages':data_core.get_negotiation_messages(nid, actor_role='partner', actor_id=uid)})

async def partner_reply(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id']); data=await request.json(); text=str(data.get('message') or '').strip()
    if not text:return web.json_response({'ok':False,'error':'message_required'},status=400)
    n=data_core.get_negotiation(nid, actor_role='partner', actor_id=uid)
    if not n:return web.json_response({'ok':False,'error':'negotiation_not_active'},status=400)
    negotiator=AINegotiator(_get_ai(request))
    await negotiator.handle(n,'partner',uid,text,**_negotiator_hooks())
    return web.json_response({'ok':True,'messages':_rows('SELECT * FROM negotiation_messages WHERE negotiation_id=%s ORDER BY id',(nid,))})

async def partner_agree(request):
    uid=_uid(request); nid=int(request.match_info['negotiation_id'])
    n=data_core.get_negotiation(nid, actor_role='partner', actor_id=uid)
    if not n or n.get('status') != 'active':
        return web.json_response({'ok':False,'error':'negotiation_not_active'},status=400)

    st=_state(n)
    st['partner_agreed']=True
    if st.get('client_agreed'):
        updated=data_core.update_negotiation(
            nid,st,'agreed',actor_role='partner',actor_id=uid
        )
        if not updated:
            return web.json_response({'ok':False,'error':'agreement_requires_final_price'},status=400)
        data_core.update_request_status(
            n['request_id'],'confirmed',actor_role='partner',actor_id=uid
        )
        message='Համաձայն եմ։ Երկու կողմն էլ համաձայն են։ Կարող ենք ամրագրել։'
        status='agreed'
    else:
        updated=data_core.update_negotiation(
            nid,st,None,actor_role='partner',actor_id=uid
        )
        if not updated:
            return web.json_response({'ok':False,'error':'negotiation_update_failed'},status=400)
        message='Համաձայն եմ պայմաններին։ Սպասում ենք հաճախորդի վերջնական համաձայնությանը։'
        status='waiting_client'

    data_core.append_negotiation_message(nid,'partner',uid,message)
    return web.json_response({'ok':True,'status':status})

async def test_payment(request):
    # Test payment is strictly based on the final mutually agreed negotiation price.
    uid=_uid(request); nid=int(request.match_info['negotiation_id'])
    n=data_core.get_negotiation(nid, actor_role='client', actor_id=uid)
    if not n or n.get('status') != 'agreed':
        return web.json_response({'ok':False,'error':'negotiation_not_agreed'},status=400)

    # Idempotency: repeated taps return the existing booking/payment/QR.
    existing=_one("SELECT * FROM bookings WHERE negotiation_id=%s ORDER BY id DESC LIMIT 1",(nid,))
    if existing:
        payment=_one("SELECT * FROM payments WHERE booking_id=%s ORDER BY id DESC LIMIT 1",(existing['id'],))
        check=_one("SELECT * FROM booking_checkins WHERE booking_id=%s",(existing['id'],))
        partner=_one("SELECT id,business_name,business_description,contact_share_policy,contact_sharing_enabled,profile_json FROM partners WHERE id=%s",(existing['partner_id'],))
        locations=_rows("SELECT marz,city,village,address,location_type FROM partner_locations WHERE partner_id=%s ORDER BY id LIMIT 5",(existing['partner_id'],))
        return web.json_response({'ok':True,'payment':payment,'booking':existing,'checkin':check,'qr':qr_util.qr_data_uri(check['token']) if check else None,'partner':{'business_name':partner['business_name'],'locations':locations,'contact':{}}})

    st=_state(n)
    final_price = st.get('final_price', st.get('agreed_price'))
    if final_price is None:
        return web.json_response({'ok':False,'error':'negotiation_final_price_missing'},status=400)
    try:
        final_price=float(final_price)
    except (TypeError,ValueError):
        return web.json_response({'ok':False,'error':'negotiation_final_price_invalid'},status=400)
    if final_price <= 0:
        return web.json_response({'ok':False,'error':'negotiation_final_price_invalid'},status=400)
    service_id=int(st.get('service_id') or 0)
    service=_one("SELECT * FROM services WHERE id=%s AND partner_id=%s AND status='approved'",(service_id,n['partner_id']))
    if not service:
        return web.json_response({'ok':False,'error':'service_not_available'},status=404)
    raw_price=final_price
    try:
        price=float(raw_price)
    except (TypeError,ValueError):
        price=0.0
    if price<=0:
        return web.json_response({'ok':False,'error':'agreed_price_invalid'},status=400)

    commission, customer_total, partner_amount=_price_and_commission(price,service)
    idram=IdramProvider()
    intent=idram.create_invoice(
        amount=commission,currency=service['currency'],
        description=f"Platform commission: {service['name']}",
        order_id=nid,metadata={'service_id':service_id,'negotiation_id':nid},
    )
    txn=intent.transaction_id
    booking_status='paid' if intent.status=='paid' else 'pending_payment'
    booking=_exec(
        """INSERT INTO bookings(
               request_id,negotiation_id,client_id,partner_id,service_id,status,
               service_name,agreed_price,currency,commission_amount,partner_amount,data_json
           )
           SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
           WHERE pg_advisory_xact_lock(%s) IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM bookings WHERE negotiation_id=%s
             )
           RETURNING *""",
        (n['request_id'],nid,uid,n['partner_id'],service_id,booking_status,service['name'],
         price,service['currency'],commission,partner_amount,
         _json({'payment_mode':intent.provider,'payment_status':intent.status,'test_transaction':txn,
                'service_price':price,'commission_tariff':{'type':_commission(service)[0],'value':_commission(service)[1]}}),
         nid,nid),True)
    if not booking:
        booking=_one("SELECT * FROM bookings WHERE negotiation_id=%s ORDER BY id DESC LIMIT 1",(nid,))
        payment=_one("SELECT * FROM payments WHERE booking_id=%s ORDER BY id DESC LIMIT 1",(booking['id'],)) if booking else None
        check=_one("SELECT * FROM booking_checkins WHERE booking_id=%s",(booking['id'],)) if booking else None
        return web.json_response({'ok':True,'payment':payment,'booking':booking,'checkin':check,
                                  'qr':qr_util.qr_data_uri(check['token']) if check else None})
    payment=_exec(
        "INSERT INTO payments(booking_id,client_id,partner_id,payment_type,status,amount,currency,provider,provider_payment_id,data_json) VALUES(%s,%s,%s,'commission',%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",
        (booking['id'],uid,n['partner_id'],intent.status,commission,service['currency'],intent.provider,txn,
         _json({'mode':intent.mode,'bill_no':intent.bill_no,'payment_url':intent.payment_url})),True)
    _exec("UPDATE service_requests SET status='booked',updated_at=NOW() WHERE id=%s",(n['request_id'],))
    _exec("INSERT INTO partner_financial_ledger(partner_id,booking_id,entry_type,amount,currency,description) VALUES(%s,%s,'commission',%s,%s,%s)",(n['partner_id'],booking['id'],commission,service['currency'],'Test Idram platform commission'))
    _exec("INSERT INTO partner_financial_ledger(partner_id,booking_id,entry_type,amount,currency,description) VALUES(%s,%s,'partner_due',%s,%s,%s)",(n['partner_id'],booking['id'],partner_amount,service['currency'],'Partner amount after platform commission'))
    check=_exec("INSERT INTO booking_checkins(booking_id,token) VALUES(%s,%s) RETURNING *",(booking['id'],secrets.token_urlsafe(24)),True)

    partner=_one("SELECT id,business_name,business_description,contact_share_policy,contact_sharing_enabled,profile_json FROM partners WHERE id=%s",(n['partner_id'],))
    locations=_rows("SELECT marz,city,village,address,location_type FROM partner_locations WHERE partner_id=%s ORDER BY id LIMIT 5",(n['partner_id'],))
    profile=partner.get('profile_json') or {}
    if isinstance(profile,str):
        try: profile=json.loads(profile)
        except Exception: profile={}
    contact={}
    if partner.get('contact_sharing_enabled'):
        contact={k:profile.get(k) for k in ('phone','website','telegram') if profile.get(k)}
    details={'business_name':partner['business_name'],'locations':locations,'contact':contact,'service':service['name'],'price':price,'currency':service['currency'],'booking_id':booking['id']}

    try:
        from notify import notify
        owner=_one("SELECT user_id FROM partners WHERE id=%s",(n['partner_id'],))
        if owner and owner.get('user_id'):
            await notify(request.app,int(owner['user_id']),title='🛒 Новая бронь',body=f"«{service['name']}» — {price:.0f} {service['currency']} (№{booking['id']}).",kind='booking_new',audience='partner',data={'booking_id':booking['id'],'service_id':service_id})
    except Exception:
        pass
    try:
        qr_uri=qr_util.qr_data_uri(check['token'])
    except Exception:
        qr_uri=None
    return web.json_response({'ok':True,'payment':payment,'booking':booking,'checkin':check,'qr':qr_uri,'partner':details})

def _parse_scheduled_at(value):
    """Accept an ISO-8601 string; return it normalised or None on any problem."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).isoformat()
    except (ValueError, TypeError):
        return None


async def direct_booking(request):
    """Book a service straight from the storefront at its listed price.

    Skips the AI negotiation entirely: the client picks a service (and
    optionally one package + several add-on options), the price is computed
    server-side from APPROVED, ACTIVE catalogue rows, the platform commission
    is charged through the Idram provider (test mode = fictitious settlement),
    and a paid booking with a check-in QR is issued.
    """
    uid = _uid(request)
    service_id = int(request.match_info['service_id'])
    data = await request.json()

    service = _one(
        """
        SELECT s.* FROM services s
        JOIN partners p ON p.id=s.partner_id
        JOIN partner_direction_categories pdc ON pdc.category_id=s.category_id
        JOIN partner_directions pd ON pd.id=pdc.partner_direction_id AND pd.partner_id=p.id
        WHERE s.id=%s AND s.status='approved' AND p.status='approved' AND pd.status='approved'
        """,
        (service_id,),
    )
    if not service:
        return web.json_response({'ok': False, 'error': 'service_not_available'}, status=404)
    partner_id = service['partner_id']

    # --- Resolve the base price (service price or a chosen package) ---
    package = None
    package_id = data.get('package_id')
    if package_id:
        package = _one(
            "SELECT * FROM service_packages WHERE id=%s AND service_id=%s AND is_active=TRUE",
            (int(package_id), service_id),
        )
        if not package:
            return web.json_response({'ok': False, 'error': 'package_not_available'}, status=404)

    currency = (package or service).get('currency') or service.get('currency') or 'AMD'
    base = package['price'] if package else service.get('price')
    if base is None:
        # Negotiable / by-request services have no fixed price and cannot be
        # booked directly; the client should use the AI concierge instead.
        return web.json_response({'ok': False, 'error': 'price_on_request'}, status=400)
    base = float(base)

    # --- Add-on options ---
    option_ids = data.get('option_ids') or []
    if not isinstance(option_ids, list):
        option_ids = []
    option_ids = [int(x) for x in option_ids if str(x).strip().isdigit()]
    chosen_options = []
    options_total = 0.0
    if option_ids:
        chosen_options = _rows(
            "SELECT * FROM service_options WHERE id=ANY(%s) AND service_id=%s AND is_active=TRUE",
            (option_ids, service_id),
        )
        if len(chosen_options) != len(set(option_ids)):
            return web.json_response({'ok': False, 'error': 'option_not_available'}, status=404)
        options_total = sum(float(o.get('price_delta') or 0) for o in chosen_options)

    price = round(base + options_total, 2)
    if price <= 0:
        return web.json_response({'ok': False, 'error': 'invalid_total_price'}, status=400)

    commission, customer_total, partner_amount = _price_and_commission(price, service)
    scheduled_at = _parse_scheduled_at(data.get('scheduled_at'))
    client_note = str(data.get('client_note') or data.get('note') or '').strip()[:1000]

    # --- Request row (keeps analytics/history consistent with the AI path) ---
    summary = f"Direct booking: {service['name']}"
    req = data_core.create_direct_booking_request(
        uid, summary, {'direct': True, 'service_id': service_id}
    )

    # --- Charge the platform commission via the Idram provider layer ---
    idram = IdramProvider()
    intent = idram.create_invoice(
        amount=commission, currency=currency,
        description=f"Platform commission: {service['name']}",
        order_id=req['id'],
        metadata={'service_id': service_id, 'request_id': req['id'], 'direct': True},
    )
    txn = intent.transaction_id

    persisted = data_core.persist_direct_booking(
        client_id=uid, service=service, request_row=req,
        package_id=int(package_id) if package_id else None,
        status=('paid' if intent.status=='paid' else 'pending_payment'),
        price=price, currency=currency, commission=commission,
        partner_amount=partner_amount, scheduled_at=scheduled_at,
        client_note=client_note,
        intent=intent,
        metadata={
            'booking_channel':'storefront_direct',
            'payment_mode':intent.provider,
            'payment_status':intent.status,
            'test_transaction':txn,
            'service_price':price,
            'commission_tariff':{'type':_commission(service)[0],'value':_commission(service)[1]},
            'package':({'id':package['id'],'name':package['name'],'price':float(package['price'] or 0)} if package else None),
            'options':[{'id':o['id'],'name':o['name'],'price_delta':float(o.get('price_delta') or 0)} for o in chosen_options],
        },
    )
    if not persisted:
        return web.json_response({'ok':False,'error':'booking_creation_conflict'},status=409)
    booking=persisted['booking']
    payment=persisted['payment']
    data_core.add_booking_financial_entries(partner_id,booking['id'],commission,partner_amount,currency)
    check=data_core.create_booking_checkin(booking['id'],secrets.token_urlsafe(24))

    partner = _one(
        "SELECT id,business_name,business_description,contact_share_policy,contact_sharing_enabled,profile_json "
        "FROM partners WHERE id=%s",
        (partner_id,),
    )
    locations = _rows(
        "SELECT marz,city,village,address,location_type FROM partner_locations WHERE partner_id=%s ORDER BY id LIMIT 5",
        (partner_id,),
    )
    profile = partner.get('profile_json') or {}
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except Exception:
            profile = {}
    contact = {}
    if partner.get('contact_sharing_enabled'):
        contact = {k: profile.get(k) for k in ('phone', 'website', 'telegram') if profile.get(k)}
    details = {
        'business_name': partner['business_name'], 'locations': locations, 'contact': contact,
        'service': service['name'], 'price': customer_total, 'base_price': price, 'currency': currency, 'booking_id': booking['id'],
    }

    # Best-effort partner notification (never fail the booking on notify errors).
    try:
        from notify import notify
        owner = _one("SELECT user_id FROM partners WHERE id=%s", (partner_id,))
        if owner and owner.get('user_id'):
            await notify(
                request.app, int(owner['user_id']),
                title='🛒 Новое бронирование с витрины',
                body=f"«{service['name']}» — {price:.0f} {currency} (№{booking['id']}).",
                kind='booking_new', audience='partner',
                data={'booking_id': booking['id'], 'service_id': service_id},
            )
    except Exception:
        pass

    try:
        qr_uri = qr_util.qr_data_uri(check['token'])
    except Exception:
        qr_uri = None
    return web.json_response({
        'ok': True, 'payment': payment, 'booking': booking, 'checkin': check,
        'qr': qr_uri, 'partner': details,
    })


# --- Phase 3: cancellations ------------------------------------------------
def _partner_profile(partner):
    p=partner.get('profile_json') or {}
    if isinstance(p,str):
        try:p=json.loads(p)
        except Exception:p={}
    return p if isinstance(p,dict) else {}

def _hours_until(scheduled_at):
    """Hours from now until *scheduled_at* (None -> large number = free cancel)."""
    if not scheduled_at:
        return 1e9
    try:
        from datetime import datetime, timezone
        if isinstance(scheduled_at,str):
            dt=datetime.fromisoformat(scheduled_at.replace('Z','+00:00'))
        else:
            dt=scheduled_at
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        return (dt-datetime.now(timezone.utc)).total_seconds()/3600.0
    except Exception:
        return 1e9

def _refund_percent(policy, scheduled_at):
    """Refund % per cancellation policy and time left before the appointment."""
    import features
    windows=features.get('cancellation_windows',{}) or {}
    rules=windows.get(policy) or windows.get('moderate') or [[0,50]]
    hours=_hours_until(scheduled_at)
    best=None
    for entry in sorted(rules,key=lambda e:float(e[0]),reverse=True):
        try:h,pct=float(entry[0]),float(entry[1])
        except Exception:continue
        if hours>=h:
            best=pct;break
    if best is None and rules:
        # fewer hours left than the smallest window -> take the lowest tier
        try:best=float(sorted(rules,key=lambda e:float(e[0]))[0][1])
        except Exception:best=0.0
    return max(0.0,min(100.0,best if best is not None else 0.0))

async def _cancel_booking(request, actor):
    import features
    if not features.is_enabled('cancellations'):
        return web.json_response({'ok':False,'error':'feature_disabled'},status=403)
    uid=_uid(request); booking_id=int(request.match_info['booking_id'])
    try:data=await request.json()
    except Exception:data={}
    reason=str(data.get('reason') or '').strip()[:500]
    booking=data_core.get_booking(booking_id, actor_role=actor, actor_id=uid)
    if not booking:
        return web.json_response({'ok':False,'error':'booking_not_found'},status=404)
    if booking.get('status') in ('cancelled','refunded','completed'):
        return web.json_response({'ok':False,'error':'booking_not_cancellable','status':booking.get('status')},status=409)
    partner=_one("SELECT profile_json,user_id FROM partners WHERE id=%s",(booking['partner_id'],)) or {}
    profile=_partner_profile(partner)
    policy=profile.get('cancellation_policy','moderate')
    # Partner-initiated cancellations always fully refund the client.
    pct=100.0 if actor=='partner' else _refund_percent(policy,booking.get('scheduled_at'))
    price=float(booking.get('agreed_price') or 0)
    refund_amount=round(price*pct/100.0,2)
    currency=booking.get('currency') or 'AMD'
    new_status='refunded' if refund_amount>0 else 'cancelled'
    updated=data_core.cancel_booking(
        booking_id, actor_role=actor, actor_id=uid, new_status=new_status,
        reason=reason, refund_amount=refund_amount
    )
    if not updated:
        return web.json_response({'ok':False,'error':'booking_cancelled_or_state_conflict'},status=409)
    if refund_amount>0:
        data_core.update_payment_status_for_booking(
            booking_id, 'refunded' if pct>=100 else 'partial_refund'
        )
        _exec("INSERT INTO partner_financial_ledger(partner_id,booking_id,entry_type,amount,currency,description) VALUES(%s,%s,'refund',%s,%s,%s)",(booking['partner_id'],booking_id,-refund_amount,currency,f'Refund on cancellation ({actor}, {pct:.0f}%)'))
    _exec("INSERT INTO booking_cancellations(booking_id,cancelled_by,reason,refund_amount) VALUES(%s,%s,%s,%s)",(booking_id,actor,reason,refund_amount))
    # Notify the counterparty (best-effort).
    try:
        from notify import notify
        if actor=='client' and partner.get('user_id'):
            await notify(request.app,int(partner['user_id']),title='❌ Отмена брони',body=f"Клиент отменил бронь №{booking_id}.",kind='booking_cancelled',audience='partner',data={'booking_id':booking_id})
        elif actor=='partner':
            await notify(request.app,int(booking['client_id']),title='❌ Бронь отменена',body=f"Партнёр отменил бронь №{booking_id}. Возврат: {refund_amount:.0f} {currency}.",kind='booking_cancelled',audience='client',data={'booking_id':booking_id})
    except Exception:
        pass
    return web.json_response({'ok':True,'booking_id':booking_id,'status':new_status,'refund_percent':pct,'refund_amount':refund_amount,'currency':currency})

async def partner_checkin(request):
    uid=_uid(request)
    data=await request.json()
    token=str(data.get('token') or '').strip()
    if not token:return web.json_response({'ok':False,'error':'token_required'},status=400)
    # Data Core verifies token + partner ownership + booking state.
    result=data_core.checkin_booking(int(data.get('booking_id') or 0), uid, token) if data.get('booking_id') else None
    if not result:
        return web.json_response({'ok':False,'error':'checkin_token_invalid'},status=404)
    if result.get('error'):
        return web.json_response({'ok':False,'error':result['error'],'booking_status':result.get('booking_status')},status=409)
    check=result.get('checkin')
    booking=result.get('booking')
    return web.json_response({
        'ok':True,
        'already_checked_in':result.get('already_checked_in',False),
        'checkin':check,
        'booking':booking,
        'service_name':result.get('service_name'),
        'agreed_price':result.get('agreed_price'),
        'currency':result.get('currency'),
        'business_name':result.get('business_name'),
    })

async def cancel_booking_client(request):
    return await _cancel_booking(request,'client')

async def cancel_booking_partner(request):
    return await _cancel_booking(request,'partner')


# ─── Idram live payment callback / return ──────────────────────────────
# Idram (live mode) works in two steps against the merchant "result URL":
#   1. pre-check  (EDP_PRECHECK=YES)  → we must answer plain "OK"
#   2. settlement (with EDP_CHECKSUM) → we verify the MD5 signature, mark the
#      matching payment + booking as paid, then answer "OK".
# The result URL is configured inside the Idram merchant account; success/fail
# GET routes below are the browser redirects (IDRAM_SUCCESS_URL / IDRAM_FAIL_URL).
async def idram_result(request):
    try:
        post=await request.post()
        payload={k:str(v) for k,v in post.items()}
    except Exception:
        payload={}
    if not payload:
        payload={k:str(v) for k,v in request.query.items()}
    idram=IdramProvider()
    result=idram.verify_callback(payload)
    # Idram pre-check ping: acknowledge so it proceeds to settlement.
    if result.status=='precheck':
        return web.Response(text='OK')
    if not result.ok or result.status!='paid':
        logging.warning('Idram callback rejected: %s (bill=%s)',result.reason,result.bill_no)
        return web.Response(text='ERR')
    bill_no=str(result.bill_no or '')
    payment=_one("SELECT * FROM payments WHERE data_json->>'bill_no'=%s ORDER BY id DESC LIMIT 1",(bill_no,))
    if not payment:
        # Nothing to reconcile, but still ack so Idram does not retry forever.
        logging.warning('Idram callback: no payment for bill %s',bill_no)
        return web.Response(text='OK')
    # Idempotent reconciliation is centralized in Data Core.
    reconciled=data_core.reconcile_paid_payment(
        int(payment['id']), result.transaction_id or payment.get('provider_payment_id')
    )
    if reconciled and not reconciled.get('already_paid'):
        booking=reconciled.get('booking')
        booking_id=payment.get('booking_id')
        # Best-effort notify the partner about the confirmed payment.
            try:
                from notify import notify
                owner=_one("SELECT user_id FROM partners WHERE id=%s",(payment.get('partner_id'),))
                if owner and owner.get('user_id'):
                    await notify(request.app,int(owner['user_id']),title='\U0001f4b3 \u041e\u043f\u043b\u0430\u0442\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430',body=f"\u0411\u0440\u043e\u043d\u044c \u2116{booking_id} \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0430.",kind='payment_confirmed',audience='partner',data={'booking_id':booking_id})
            except Exception:
                pass
    return web.Response(text='OK')


def _idram_return_page(title,body):
    from html import escape
    base=(os.getenv('WEBAPP_BASE_URL','') or '').rstrip('/')
    link=f'<p><a href="{escape(base)}/">\u041d\u0430 \u0433\u043b\u0430\u0432\u043d\u0443\u044e</a></p>' if base else ''
    html=(f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
          f'<meta name="viewport" content="width=device-width, initial-scale=1">'
          f'<title>{escape(title)}</title></head>'
          f'<body style="font-family:system-ui,sans-serif;text-align:center;padding:40px">'
          f'<h2>{escape(title)}</h2><p>{escape(body)}</p>{link}</body></html>')
    return web.Response(text=html,content_type='text/html')

async def idram_success(request):
    return _idram_return_page('\u041e\u043f\u043b\u0430\u0442\u0430 \u043f\u0440\u043e\u0448\u043b\u0430','\u0421\u043f\u0430\u0441\u0438\u0431\u043e! \u041f\u043b\u0430\u0442\u0451\u0436 \u043f\u0440\u0438\u043d\u044f\u0442. \u041c\u043e\u0436\u043d\u043e \u0432\u0435\u0440\u043d\u0443\u0442\u044c\u0441\u044f \u0432 \u0431\u043e\u0442\u0430.')

async def idram_fail(request):
    return _idram_return_page('\u041f\u043b\u0430\u0442\u0451\u0436 \u043d\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0451\u043d','\u041e\u043f\u043b\u0430\u0442\u0430 \u043d\u0435 \u043f\u0440\u043e\u0448\u043b\u0430 \u0438\u043b\u0438 \u0431\u044b\u043b\u0430 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u0430. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0451 \u0440\u0430\u0437.')

def register_marketplace_flow_routes(app):
    ensure_booking_schema()
    app.router.add_post('/api/market/client/search',client_search)
    app.router.add_post('/api/market/client/request',create_request)
    app.router.add_post('/api/market/client/request/{request_id}/select',select_candidate)
    app.router.add_get('/api/market/client/negotiation/{negotiation_id}',negotiation_get)
    app.router.add_post('/api/market/client/negotiation/{negotiation_id}/message',negotiation_client_message)
    app.router.add_post('/api/market/client/negotiation/{negotiation_id}/pay-test',test_payment)
    app.router.add_post('/api/market/client/service/{service_id}/book',direct_booking)
    app.router.add_get('/api/market/partner/negotiations',partner_negotiations)
    app.router.add_get('/api/market/partner/negotiation/{negotiation_id}',partner_negotiation_messages)
    app.router.add_post('/api/market/partner/negotiation/{negotiation_id}/reply',partner_reply)
    app.router.add_post('/api/market/partner/negotiation/{negotiation_id}/agree',partner_agree)
    app.router.add_post('/api/market/client/booking/{booking_id}/cancel',cancel_booking_client)
    app.router.add_post('/api/market/partner/booking/{booking_id}/cancel',cancel_booking_partner)
    app.router.add_post('/api/market/partner/checkin',partner_checkin)
    # Idram live payment callback (server-to-server) + browser return pages.
    app.router.add_post('/api/idram/result',idram_result)
    app.router.add_get('/api/idram/result',idram_result)
    app.router.add_get('/api/idram/success',idram_success)
    app.router.add_get('/api/idram/fail',idram_fail)
