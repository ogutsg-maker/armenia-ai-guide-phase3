from catalog.classifier import classify
from db import run, exec
from data.core import audit, notify
import json
import config


def _address(company_id, service):
    marzes = service.get("marzes") or []
    cities = service.get("cities") or []
    districts = service.get("districts") or []
    address = service.get("address")
    if not (marzes or cities or districts or address):
        return None
    row = run(
        "SELECT id FROM aig_addresses WHERE company_id=%s AND COALESCE(marz,'')=%s AND COALESCE(city,'')=%s "
        "AND COALESCE(district,'')=%s AND COALESCE(address,'')=%s ORDER BY id DESC LIMIT 1",
        (
            company_id,
            marzes[0] if marzes else "",
            cities[0] if cities else "",
            districts[0] if districts else "",
            address or "",
        ),
    )
    if row:
        return row["id"]
    row = run(
        "INSERT INTO aig_addresses(company_id,marz,city,district,address,is_base) VALUES(%s,%s,%s,%s,%s,false) RETURNING id",
        (
            company_id,
            ", ".join(marzes) or None,
            ", ".join(cities) or None,
            ", ".join(districts) or None,
            address,
        ),
    )
    return row["id"]


def _direction_document(company_id, direction_id):
    if not direction_id:
        return False
    return bool(run(
        "SELECT id FROM aig_direction_documents WHERE company_id=%s AND catalog_category_id=%s AND status='ACTIVE' "
        "ORDER BY id DESC LIMIT 1",
        (company_id, direction_id),
    ))


def create_after_confirmation(uid, company_id, payload):
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, list):
        services = [payload]

    created = []
    for service in services:
        address_id = _address(company_id, service)
        category = service.get("classification")
        if not category:
            category = classify(service["name"])
        direction = service.get("direction")
        direction_id = service.get("direction_category_id") or (direction or {}).get("id")

        status = "CLASSIFICATION_PENDING"
        if category and direction_id:
            status = "PENDING_ADMIN" if _direction_document(company_id, direction_id) else "DOCUMENT_PENDING"

        row = run(
            "INSERT INTO aig_services(company_id,name,description,price_type,price_amd,hours,at_client,territory,"
            "address_id,internal_phone,required_document,status,catalog_category_id,direction_category_id,"
            "classification_confidence,classification_margin,marzes,cities,districts) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                company_id,
                service["name"],
                service.get("description"),
                service["price_type"],
                service["price_amd"],
                service.get("hours"),
                service.get("at_client", False),
                service.get("territory"),
                address_id,
                service.get("internal_phone"),
                bool(direction_id),
                status,
                category["id"] if category else None,
                direction_id,
                service.get("classification_confidence"),
                service.get("classification_margin"),
                service.get("marzes") or None,
                service.get("cities") or None,
                service.get("districts") or None,
            ),
        )
        exec(
            "INSERT INTO aig_service_applications(service_id,status) VALUES(%s,%s)",
            (row["id"], status),
        )
        created.append(row)

        if status == "CLASSIFICATION_PENDING":
            notify(
                config.ADMIN_ID,
                "classification_alert",
                {"service_id": row["id"], "service_name": row["name"], "company_id": company_id},
            )

    for row in created:
        audit(uid, "service_created", "service", row["id"], {"status": row["status"]})

    missing = {}
    for row in created:
        if row["status"] == "DOCUMENT_PENDING":
            direction = run(
                "SELECT id,name_am,name_ru,name_en FROM aig_catalog_categories WHERE id=%s",
                (row["direction_category_id"],),
            )
            if direction:
                missing[str(direction["id"])] = direction

    return {
        "services": created,
        "status": "DOCUMENT_PENDING" if missing else (
            "CLASSIFICATION_PENDING" if any(x["status"] == "CLASSIFICATION_PENDING" for x in created)
            else "PENDING_ADMIN"
        ),
        "missing_documents": list(missing.values()),
    }


def activate_services_after_document(company_id, direction_id, actor_uid):
    rows = run(
        "SELECT s.id,s.name FROM aig_services s "
        "WHERE s.company_id=%s AND s.direction_category_id=%s AND s.status='DOCUMENT_PENDING'",
        (company_id, direction_id),
        many=True,
    )
    for row in rows:
        exec(
            "UPDATE aig_services SET status='PENDING_ADMIN',updated_at=now() WHERE id=%s",
            (row["id"],),
        )
        exec(
            "UPDATE aig_service_applications SET status='PENDING_ADMIN' "
            "WHERE service_id=%s AND status='DOCUMENT_PENDING'",
            (row["id"],),
        )
        audit(actor_uid, "direction_document_uploaded", "service", row["id"], {"direction_id": direction_id})
    return rows
