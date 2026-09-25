"""End-to-end Armenia AI Guide marketplace flow."""
from __future__ import annotations
import json, os, logging
from decimal import Decimal
from datetime import datetime, date
from aiohttp import web
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError
from booking_schema import ensure_booking_schema
import qr_util
from idram import IdramProvider
from ai_negotiator import AINegotiator
import data_core

def _uid(request):
    raw=request.headers.get('X-Telegram-Init-Data','').strip()
    if not raw: raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':'telegram_init_data_required'}),content_type='application/json')
    try:
        data=validate_telegram_webapp_init_data(raw,request.app.get('stage3_bot_token') or os.getenv('BOT_TOKEN','')); return int(data['id'])
    except (TelegramWebAppAuthError,ValueError,TypeError,KeyError) as exc:
        raise web.HTTPUnauthorized(text=json.dumps({'ok':False,'error':str(exc)}),content_type='application/json')

def _json(value): return json.dumps(value or {},ensure_ascii=False)

# Backward-compatible DB helper aliases for legacy route modules.
# The actual database gateway is Data Core; these aliases keep old modules
# from importing removed SQL helpers from this orchestration module.
_one = data_core.one
_rows = data_core.rows
_exec = data_core.execute

def _commission(service):
    return data_core.resolve_service_commission(service)

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
    uid=_uid(request); data=await request.json()
    q=str(data.get('query') or data.get('text') or '').strip()
    city=str(data.get('city') or '').strip()
    category_id=int(data.get('category_id') or 0)
    items=data_core.marketplace_client_search(q,city,category_id)
    return web.json_response({'ok':True,'items':items,'query':q,'client_id':uid})

async def create_request(request):
    uid=_uid(request); data=await request.json()
    summary=str(data.get('summary') or data.get('query') or '').strip()
    city=str(data.get('city') or '').strip() or None
    lang=str(data.get('language') or 'hy')[:5]
    profile=data.get('preferences') or {}
    item=data_core.create_service_request(uid,None,'searching',lang,city,summary,profile)
    return web.json_response({'ok':True,'request':item})

async def select_candidate(request):
    uid=_uid(request); rid=int(request.match_info['request_id']); data=await request.json()
    service_id=int(data.get('service_id') or 0)
    result=data_core.marketplace_create_negotiation_selection(rid,uid,service_id)
    if not result:return web.json_response({'ok':False,'error':'candidate_not_found'},status=404)
    return web.json_response({'ok':True,**result})

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
    uid=_uid(request); items=data_core.marketplace_partner_negotiations(uid)
    if items is None:return web.json_response({'ok':False,'error':'partner_not_found'},status=404)
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
    return web.json_response({'ok':True,'messages':data_core.get_negotiation_messages(nid, actor_role='partner', actor_id=uid)})

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
    uid=_uid(request); nid=int(request.match_info['negotiation_id'])
    n=data_core.get_negotiation(nid, actor_role='client', actor_id=uid)
    if not n or n.get('status') != 'agreed':
        return web.json_response({'ok':False,'error':'negotiation_not_agreed'},status=400)
    existing=data_core.marketplace_existing_payment(nid)
    if existing:
        check=existing.get('checkin')
        display=data_core.get_partner_booking_display(existing['booking']['partner_id'])
        partner=display['partner'] if display else {}
        locations=display['locations'] if display else []
        return web.json_response({'ok':True,'payment':existing.get('payment'),'booking':existing['booking'],
                                  'checkin':check,'qr':qr_util.qr_data_uri(check['token']) if check else None,
                                  'partner':{'business_name':partner.get('business_name'),'locations':locations,'contact':{}}})
    st=_state(n)
    try: final_price=float(st.get('final_price',st.get('agreed_price')))
    except (TypeError,ValueError): return web.json_response({'ok':False,'error':'negotiation_final_price_invalid'},status=400)
    if final_price<=0:return web.json_response({'ok':False,'error':'negotiation_final_price_invalid'},status=400)
    service_id=int(st.get('service_id') or 0)
    service=data_core.marketplace_service_for_partner(service_id,int(n['partner_id']))
    if not service:return web.json_response({'ok':False,'error':'service_not_available'},status=404)
    commission,customer_total,partner_amount=_price_and_commission(final_price,service)
    idram=IdramProvider()
    intent=idram.create_invoice(amount=commission,currency=service['currency'],
        description=f"Platform commission: {service['name']}",order_id=nid,
        metadata={'service_id':service_id,'negotiation_id':nid})
    persisted=data_core.marketplace_persist_negotiation_booking(
        request_id=int(n['request_id']),negotiation_id=nid,client_id=uid,partner_id=int(n['partner_id']),
        service=service,status=('paid' if intent.status=='paid' else 'pending_payment'),
        price=final_price,currency=service['currency'],commission=commission,
        partner_amount=partner_amount,intent=intent)
    if not persisted:return web.json_response({'ok':False,'error':'booking_creation_conflict'},status=409)
    booking,payment,check=persisted['booking'],persisted['payment'],persisted['checkin']
    display=data_core.get_partner_booking_display(int(n['partner_id']))
    if not display:return web.json_response({'ok':False,'error':'partner_not_available'},status=404)
    partner,locations=display['partner'],display['locations']
    profile=partner.get('profile_json') or {}
    if isinstance(profile,str):
        try: profile=json.loads(profile)
        except Exception: profile={}
    contact={k:profile.get(k) for k in ('phone','website','telegram') if partner.get('contact_sharing_enabled') and profile.get(k)}
    try:
        from notify import notify
        owner=data_core.marketplace_partner_owner(int(n['partner_id']))
        if owner and owner.get('user_id'):
            await notify(request.app,int(owner['user_id']),title='🛒 Новая бронь',
                         body=f"«{service['name']}» — {final_price:.0f} {service['currency']} (№{booking['id']}).",
                         kind='booking_new',audience='partner',data={'booking_id':booking['id'],'service_id':service_id})
    except Exception: pass
    try: qr_uri=qr_util.qr_data_uri(check['token'])
    except Exception: qr_uri=None
    return web.json_response({'ok':True,'payment':payment,'booking':booking,'checkin':check,'qr':qr_uri,
                              'partner':{'business_name':partner['business_name'],'locations':locations,
                                         'contact':contact,'service':service['name'],'price':customer_total,
                                         'currency':service['currency'],'booking_id':booking['id']}})

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

    service = data_core.get_approved_service_for_booking(service_id)
    if not service:
        return web.json_response({'ok': False, 'error': 'service_not_available'}, status=404)
    partner_id = service['partner_id']

    # --- Resolve the base price (service price or a chosen package) ---
    package = None
    package_id = data.get('package_id')
    if package_id:
        package = data_core.get_active_package(service_id, int(package_id))
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
        chosen_options = data_core.get_approved_service_options(service_id, option_ids)
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
    if not req or not req.get('id'):
        return web.json_response({'ok':False,'error':'booking_request_conflict'},status=409)

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
    check=persisted.get('checkin')
    if not check:
        return web.json_response({'ok':False,'error':'booking_checkin_not_created'},status=500)

    display = data_core.get_partner_booking_display(partner_id)
    if not display:
        return web.json_response({'ok':False,'error':'partner_not_available'},status=404)
    partner = display['partner']
    locations = display['locations']
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
        owner = data_core.get_partner(partner_id)
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
    partner=data_core.marketplace_partner_profile(int(booking['partner_id'])) or {}
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
    data_core.marketplace_cancel_side_effects(
        int(booking['partner_id']),booking_id,actor,reason,refund_amount,currency
    )
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
    payment=data_core.get_payment_by_bill_no(bill_no)
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
            owner=data_core.get_partner(int(payment.get('partner_id') or 0)) if payment.get('partner_id') else None
            if owner and owner.get('user_id'):
                await notify(request.app,int(owner['user_id']),title='💳 Оплата подтверждена',body=f"Бронь №{booking_id} оплачена.",kind='payment_confirmed',audience='partner',data={'booking_id':booking_id})
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
