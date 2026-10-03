import json
from db import run,exec
def notify(uid,kind,payload=None): exec("INSERT INTO aig_notifications(telegram_id,kind,payload) VALUES(%s,%s,%s)",(uid,kind,json.dumps(payload or {},ensure_ascii=False)))
def audit(uid,action,typ=None,eid=None,payload=None): exec("INSERT INTO aig_audit_logs(actor_telegram_id,action,entity_type,entity_id,payload) VALUES(%s,%s,%s,%s,%s)",(uid,action,typ,eid,json.dumps(payload or {},ensure_ascii=False)))
def catalog():
 return run("SELECT id,name_am,name_ru,name_en,slug,parent_id FROM aig_catalog_categories WHERE active=true ORDER BY id",many=True)
def active_services(text=None,city=None):
 q="SELECT s.*,c.name company_name,p.telegram_id,a.city,a.district FROM aig_services s JOIN aig_companies c ON c.id=s.company_id JOIN aig_partners p ON p.id=c.partner_id LEFT JOIN aig_addresses a ON a.id=s.address_id WHERE s.status='ACTIVE'"; v=[]
 if text:q+=" AND s.name ILIKE %s";v.append("%"+text+"%")
 if city:q+=" AND (a.city ILIKE %s OR s.territory ILIKE %s)";v+=["%"+city+"%","%"+city+"%"]
 return run(q+" ORDER BY s.id DESC LIMIT 30",v,True)