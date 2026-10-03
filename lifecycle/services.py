from catalog.classifier import classify
from db import run,exec
from data.core import audit,notify

def create_after_confirmation(uid,company_id,d):
    row=run(
        "INSERT INTO aig_services(company_id,name,price_type,price_amd,hours,at_client,territory,address_id,internal_phone,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'CLASSIFICATION_PENDING') RETURNING *",
        (company_id,d["name"],d["price_type"],d["price_amd"],d.get("hours"),d.get("at_client",False),d.get("territory"),d.get("address_id"),d.get("internal_phone"))
    )
    cat=classify(d["name"])
    if cat:
        exec("UPDATE aig_services SET catalog_category_id=%s,status='PENDING_ADMIN' WHERE id=%s",(cat["id"],row["id"]))
        status="PENDING_ADMIN"
    else:
        notify(__import__("config").ADMIN_ID,"classification_alert",{"service_id":row["id"]})
        status="CLASSIFICATION_PENDING"
    row=run("SELECT * FROM aig_services WHERE id=%s",(row["id"],))
    exec("INSERT INTO aig_service_applications(service_id,status) VALUES(%s,%s)",(row["id"],status))
    audit(uid,"service_created","service",row["id"],{"status":status})
    return row,status
