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
    def active_services(text:str):
        return db.all("SELECT s.id service_id,s.name service_name,s.price_type,s.price_amd,c.name partner_name,p.id partner_id,a.city FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id LEFT JOIN aig_addresses a ON a.id=s.address_id WHERE s.status='ACTIVE' AND s.name ILIKE %s ORDER BY s.id LIMIT 3",(f"%{text}%",))
    @staticmethod
    def commission(price:float,mode:str,rate:float)->float:
        if mode=="on_top": return round(price*rate/100,2)
        if mode=="inside": return round(price*rate/(100+rate),2)
        return 0.0
