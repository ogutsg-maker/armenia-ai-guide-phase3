import secrets
from datetime import datetime, timezone
from db import run, exec

def commission_rule(service_id, category_id=None):
    row = run("""SELECT commission_type,commission_value FROM aig_commission_rules
                 WHERE active=true AND service_id=%s ORDER BY id DESC LIMIT 1""",(service_id,))
    if row: return row
    if category_id:
        row = run("""SELECT commission_type,commission_value FROM aig_commission_rules
                     WHERE active=true AND catalog_category_id=%s ORDER BY id DESC LIMIT 1""",(category_id,))
        if row: return row
    return {"commission_type":"on_top","commission_value":10}

def calculate_commission(price, rule):
    price=float(price)
    value=float(rule["commission_value"])
    kind=str(rule["commission_type"])
    if kind=="fixed":
        amount=value
    elif kind=="inside":
        amount=price-(price/(1+value/100)) if value else 0
    else:
        amount=price*value/100
    return round(amount,2)

def create_booking(uid, negotiation_id, payload):
    n=run("""SELECT n.*,s.name service_name,s.price_type,s.price_amd,s.catalog_category_id,
                    c.name company_name,r.client_telegram_id,r.city,r.district
             FROM aig_negotiations n
             JOIN aig_services s ON s.id=n.service_id
             JOIN aig_companies c ON c.id=s.company_id
             JOIN aig_client_requests r ON r.id=n.request_id
             WHERE n.id=%s""",(negotiation_id,))
    if not n or int(n["client_telegram_id"])!=int(uid) or n["status"]!="agreed":
        raise ValueError("negotiation_not_ready")
    existing=run("SELECT * FROM aig_bookings WHERE negotiation_id=%s",(negotiation_id,))
    if existing: return existing
    price=float(n["agreed_price"] or n["agreed_max"] or n["agreed_min"] or 0)
    if price<=0: raise ValueError("agreed_price_required")
    rule=commission_rule(n["service_id"],n.get("catalog_category_id"))
    commission=calculate_commission(price,rule)
    start=payload.get("service_start")
    row=run("""INSERT INTO aig_bookings
        (negotiation_id,status,service_start,agreed_price,commission_amd,commission_type,commission_value,
         client_payment_amount,partner_amount,qr_payload)
        VALUES(%s,'PENDING_PARTNER_CONFIRMATION',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (negotiation_id,start,price,commission,rule["commission_type"],rule["commission_value"],
         commission,price,{}))
    exec("UPDATE aig_negotiations SET status='agreed',commission_base=%s WHERE id=%s",(price,negotiation_id))
    return row

def partner_confirm_booking(uid, booking_id):
    row=run("""SELECT b.*,n.partner_id,p.telegram_id AS partner_telegram_id
               FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id
               JOIN aig_partners p ON p.id=n.partner_id WHERE b.id=%s""",(booking_id,))
    if not row or int(row["partner_telegram_id"])!=int(uid): raise ValueError("booking_not_found")
    if row["status"]!="PENDING_PARTNER_CONFIRMATION": return row
    exec("UPDATE aig_bookings SET status='PENDING_PAYMENT' WHERE id=%s",(booking_id,))
    return run("SELECT * FROM aig_bookings WHERE id=%s",(booking_id,))

def confirm_commission_payment(booking_id,payment_ref):
    row=run("SELECT * FROM aig_bookings WHERE id=%s",(booking_id,))
    if not row or row["status"]!="PENDING_PAYMENT": raise ValueError("booking_not_payable")
    token=secrets.token_urlsafe(24)
    start=row.get("service_start")
    expires=None
    if start:
        expires=start + __import__('datetime').timedelta(hours=1)
    else:
        expires=datetime.now(timezone.utc) + __import__('datetime').timedelta(hours=1)
    payload={"booking_id":booking_id,"service_start":start,"service_price":float(row["agreed_price"]),
             "partner_amount":float(row["partner_amount"]),"qr_expires_at":expires}
    exec("""UPDATE aig_bookings SET status='PAYMENT_CONFIRMED',payment_ref=%s,payment_confirmed_at=now(),
            qr_token=%s,qr_expires_at=%s,qr_payload=%s WHERE id=%s""",
         (payment_ref,token,expires,payload,booking_id))
    return run("SELECT * FROM aig_bookings WHERE id=%s",(booking_id,))

def checkin(uid,token):
    row=run("""SELECT b.*,n.partner_id,p.telegram_id AS partner_telegram_id
               FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id
               JOIN aig_partners p ON p.id=n.partner_id
               WHERE b.qr_token=%s""",(token,))
    if not row or int(row["partner_telegram_id"])!=int(uid): raise ValueError("qr_not_valid")
    if row["status"]!="PAYMENT_CONFIRMED": raise ValueError("booking_not_ready")
    if row.get("qr_expires_at") and row["qr_expires_at"] < datetime.now(timezone.utc):
        raise ValueError("qr_expired")
    exec("UPDATE aig_bookings SET status='IN_PROGRESS',checked_in_at=now() WHERE id=%s",(row["id"],))
    exec("""INSERT INTO aig_booking_checkins(booking_id,partner_telegram_id)
            VALUES(%s,%s) ON CONFLICT(booking_id) DO NOTHING""",(row["id"],uid))
    return run("SELECT * FROM aig_bookings WHERE id=%s",(row["id"],))

def complete(uid,booking_id):
    row=run("""SELECT b.*,p.telegram_id AS partner_telegram_id
               FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id
               JOIN aig_partners p ON p.id=n.partner_id WHERE b.id=%s""",(booking_id,))
    if not row or int(row["partner_telegram_id"])!=int(uid): raise ValueError("booking_not_found")
    if row["status"]!="IN_PROGRESS": raise ValueError("booking_not_in_progress")
    exec("UPDATE aig_bookings SET status='SERVICE_COMPLETED',completed_at=now() WHERE id=%s",(booking_id,))
    return run("SELECT * FROM aig_bookings WHERE id=%s",(booking_id,))

def submit_review(uid,booking_id,rating,text):
    row=run("""SELECT b.*,n.request_id,r.client_telegram_id FROM aig_bookings b
               JOIN aig_negotiations n ON n.id=b.negotiation_id
               JOIN aig_client_requests r ON r.id=n.request_id WHERE b.id=%s""",(booking_id,))
    if not row or int(row["client_telegram_id"])!=int(uid): raise ValueError("booking_not_found")
    if row["status"]!="SERVICE_COMPLETED": raise ValueError("review_not_available")
    exists=run("SELECT id FROM aig_reviews WHERE booking_id=%s",(booking_id,))
    if exists: raise ValueError("review_already_submitted")
    return run("""INSERT INTO aig_reviews(booking_id,client_telegram_id,rating,text)
                  VALUES(%s,%s,%s,%s) RETURNING *""",(booking_id,uid,rating,text))
