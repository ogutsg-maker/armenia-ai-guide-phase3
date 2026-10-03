from __future__ import annotations
from typing import Any
import db
class DataCore:
    """Deterministic source of truth. AI never calls SQL directly."""
    @staticmethod
    def partner(telegram_id:int):
        return db.one("SELECT * FROM aig_partners WHERE telegram_id=%s",(telegram_id,))
    @staticmethod
    def companies(partner_id:int):
        return db.all("SELECT * FROM aig_companies WHERE partner_id=%s AND NOT archived ORDER BY id",(partner_id,))
    @staticmethod
    def search_active_services(text:str,city=None):
        return db.all("SELECT s.id service_id,s.name service_name,s.price_type,s.price_amd,c.name partner_name,p.id partner_id,a.city FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id LEFT JOIN aig_addresses a ON a.id=s.address_id WHERE s.status='ACTIVE' AND s.name ILIKE %s AND (%s IS NULL OR a.city IS NULL OR a.city ILIKE %s) ORDER BY s.id LIMIT 3",(f"%{text}%",city,f"%{city}%"))
    @staticmethod
    def commission(price:float,mode:str,rate:float)->float:
        if mode=="on_top": return round(price*rate/100,2)
        if mode=="inside": return round(price*rate/(100+rate),2)
        return 0.0

    @staticmethod
    def admin_tool(name,args):
        if name=="admin_list_service_applications":
            q="SELECT a.*,s.name service_name,s.status service_status,c.name company_name FROM aig_service_applications a JOIN aig_services s ON s.id=a.service_id JOIN aig_companies c ON c.id=s.company_id";v=[]
            if args.get("status"):q+=" WHERE a.status=%s OR s.status=%s";v=[args["status"],args["status"]]
            return db.all(q+" ORDER BY a.id DESC",v)
        if name=="admin_list_uncategorized_services":
            return db.all("SELECT s.id,s.name,s.status,s.classification_confidence,s.classification_margin,c.name company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE s.catalog_category_id IS NULL OR s.status='CLASSIFICATION_PENDING' ORDER BY s.id DESC")
        if name=="admin_ai_costs_today":
            return db.one("SELECT count(*) operations,coalesce(sum(input_tokens+output_tokens),0) tokens,coalesce(sum(usd),0) usd FROM aig_ai_costs WHERE created_at::date=current_date")
        if name=="admin_list_potential_partners":
            q="SELECT * FROM aig_potential_partners WHERE 1=1";v=[]
            if args.get("city"):q+=" AND city ILIKE %s";v.append("%"+args["city"]+"%")
            if args.get("status"):q+=" AND status=%s";v.append(args["status"])
            return db.all(q+" ORDER BY id DESC",v)
        if name=="admin_get_order":
            return db.one("SELECT b.*,n.status negotiation_status,s.name service_name,c.name company_name FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_services s ON s.id=n.service_id JOIN aig_companies c ON c.id=s.company_id WHERE b.id=%s",(int(args["booking_id"]),))
        if name=="admin_list_arbitrations":
            return db.all("SELECT a.*,b.status booking_status FROM aig_arbitrations a JOIN aig_bookings b ON b.id=a.booking_id WHERE a.status='OPEN' ORDER BY a.id DESC")
        raise RuntimeError("unsupported_tool:"+name)

    @staticmethod
    def partner_tool(name,args,partner_id,telegram_id):
        if name=="partner_list_services":
            return db.all("SELECT s.*,c.name company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE c.partner_id=%s ORDER BY s.id DESC",(partner_id,))
        if name=="partner_list_companies":
            return db.all("SELECT id,name,archived FROM aig_companies WHERE partner_id=%s AND NOT archived ORDER BY id",(partner_id,))
        if name=="partner_get_service":
            row=db.one("SELECT s.*,c.name company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE c.partner_id=%s AND s.id=%s",(partner_id,int(args["service_id"])))
            return row or {"error":"service_not_found"}
        raise RuntimeError("unsupported_partner_tool:"+name)
