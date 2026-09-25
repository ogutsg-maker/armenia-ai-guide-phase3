"""Armenia AI Guide Data Core.

Single application-owned gateway between AI/application layers and PostgreSQL.
The existing Supabase/PostgreSQL schema remains the source of truth and is not
recreated by this module.

Rules:
- AI never receives SQL access.
- AI Context and AI Tools call Data Core only.
- Authorization/ownership checks belong here, not in prompts.
- New schema changes, when genuinely required, must be migrations; this layer
  does not create or reset the database.
"""
from __future__ import annotations

from typing import Any, Iterable
import json

import platform_db


# ---------------------------------------------------------------------------
# Low-level DB gateway
# ---------------------------------------------------------------------------
# These functions are intentionally the only DB primitives exported to the
# application architecture. Domain modules should prefer the named methods
# below, but the primitives keep the migration backwards-compatible while
# existing modules are moved incrementally.

def one(sql: str, params: Iterable[Any] = ()):
    return platform_db.one(sql, tuple(params))


def rows(sql: str, params: Iterable[Any] = ()):
    return platform_db.rows(sql, tuple(params))


def execute(sql: str, params: Iterable[Any] = (), returning: bool = False):
    return platform_db.execute(sql, tuple(params), returning)


def json_dump(value: Any) -> str:
    return platform_db.json_dump(value)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def catalog_tree():
    return platform_db.catalog_tree()


def approved_partner_categories(partner_id: int):
    return platform_db.approved_partner_categories(int(partner_id))


def search_catalog(query: str = "", master_category_id: int | None = None, limit: int = 100):
    query = str(query or "").strip()
    params: list[Any] = []
    where = ["c.is_active=TRUE", "m.is_active=TRUE"]
    if query:
        where.append("""(
            c.name_am ILIKE %s OR c.name_ru ILIKE %s OR c.name_en ILIKE %s
            OR c.slug ILIKE %s OR m.name_am ILIKE %s OR m.name_ru ILIKE %s
            OR m.name_en ILIKE %s
        )""")
        params.extend([f"%{query}%"] * 7)
    if master_category_id is not None:
        where.append("c.master_category_id=%s")
        params.append(int(master_category_id))
    params.append(max(1, min(int(limit or 100), 500)))
    return rows(
        """SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,
                  m.name_am AS master_name_am,m.name_ru AS master_name_ru,
                  m.name_en AS master_name_en
           FROM categories c
           JOIN master_categories m ON m.id=c.master_category_id
           WHERE """ + " AND ".join(where) + " ORDER BY c.id LIMIT %s",
        tuple(params),
    )


def active_directions():
    return rows(
        """SELECT id,name_am,name_ru,name_en,slug,is_active
           FROM master_categories WHERE is_active=TRUE ORDER BY id"""
    )


def get_catalog_category(category_id: int):
    return one(
        """SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,c.is_active,
                  m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en
           FROM categories c
           LEFT JOIN master_categories m ON m.id=c.master_category_id
           WHERE c.id=%s""",
        (int(category_id),),
    )


# ---------------------------------------------------------------------------
# Partners / companies / services
# ---------------------------------------------------------------------------

def get_partner(partner_id: int):
    return platform_db.get_partner(int(partner_id))


def get_partner_by_user(user_id: int):
    return platform_db.get_partner_by_user(int(user_id))


def search_partners(query: str = "", city: str = "", limit: int = 20):
    q = str(query or "").strip()
    city = str(city or "").strip()
    limit = max(1, min(int(limit or 20), 100))
    where = ["p.status <> 'archived'"]
    params: list[Any] = []
    if q:
        where.append("(p.business_name ILIKE %s OR p.business_description ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    if city:
        where.append("""EXISTS (
            SELECT 1 FROM partner_applications pa
            WHERE pa.partner_id=p.id AND pa.location_city ILIKE %s
        )""")
        params.append(f"%{city}%")
    params.append(limit)
    return rows(
        """SELECT p.id,p.user_id,p.business_name,p.status,p.verification_status,
                  p.business_description
           FROM partners p
           WHERE """ + " AND ".join(where) +
        " ORDER BY p.id DESC LIMIT %s",
        tuple(params),
    )


def get_company(company_id: int):
    return one(
        """SELECT b.id,b.partner_id,b.name,b.description,b.phone,b.status,
                  p.business_name AS partner_name
           FROM partner_businesses b
           LEFT JOIN partners p ON p.id=b.partner_id
           WHERE b.id=%s""",
        (int(company_id),),
    )


def list_companies(partner_id: int, include_archived: bool = False):
    where = "partner_id=%s"
    params: list[Any] = [int(partner_id)]
    if not include_archived:
        where += " AND status<>'archived'"
    return rows(
        f"""SELECT id,partner_id,name,description,phone,status
            FROM partner_businesses WHERE {where} ORDER BY id DESC""",
        tuple(params),
    )


def get_service(service_id: int):
    return one(
        """SELECT s.id,s.partner_id,s.business_id,s.name,s.category_id,s.price,s.status,
                  p.business_name AS partner_name,b.name AS company_name,
                  c.master_category_id,c.name_am AS category_name_am,
                  c.name_ru AS category_name_ru,c.name_en AS category_name_en
           FROM services s
           LEFT JOIN partners p ON p.id=s.partner_id
           LEFT JOIN partner_businesses b ON b.id=s.business_id
           LEFT JOIN categories c ON c.id=s.category_id
           WHERE s.id=%s""",
        (int(service_id),),
    )


def search_services(
    partner_id: int | None = None,
    category_id: int | None = None,
    city: str = "",
    max_price: float | None = None,
    limit: int = 100,
):
    where = ["s.status='approved'", "p.status='approved'", "EXISTS (SELECT 1 FROM partner_direction_categories pdc JOIN partner_directions pd ON pd.id=pdc.partner_direction_id WHERE pdc.category_id=s.category_id AND pd.partner_id=s.partner_id AND pd.status='approved')"]
    params: list[Any] = []
    if partner_id is not None:
        where.append("s.partner_id=%s")
        params.append(int(partner_id))
    if category_id is not None:
        where.append("s.category_id=%s")
        params.append(int(category_id))
    if max_price is not None:
        where.append("(s.price IS NULL OR s.price<=%s)")
        params.append(float(max_price))
    if city:
        where.append("""EXISTS (
            SELECT 1 FROM partner_objects pl
            WHERE pl.partner_id=p.id
              AND (LOWER(COALESCE(pl.city,''))=LOWER(%s)
                OR LOWER(COALESCE(pl.village,''))=LOWER(%s)
                OR LOWER(COALESCE(pl.marz,''))=LOWER(%s)
                OR LOWER(COALESCE(pl.data_json->>'coverage',''))='all_armenia'
                OR LOWER(COALESCE(pl.data_json->>'service_area',''))='all_armenia')
        )""")
        params.extend([city, city, city])
    params.append(max(1, min(int(limit or 100), 200)))
    return rows(
        """SELECT DISTINCT s.id,s.partner_id,s.business_id,s.name,s.category_id,
                  s.price,s.status,p.business_name AS partner_name,
                  b.name AS company_name,c.name_am AS category_name_am,
                  c.name_ru AS category_name_ru,c.name_en AS category_name_en,
                  c.master_category_id
           FROM services s
           JOIN categories c ON c.id=s.category_id
           JOIN partners p ON p.id=s.partner_id
           LEFT JOIN partner_businesses b ON b.id=s.business_id
           WHERE """ + " AND ".join(where) +
        " ORDER BY CASE WHEN s.price IS NULL THEN 1 ELSE 0 END,s.created_at DESC LIMIT %s",
        tuple(params),
    )



# ---------------------------------------------------------------------------
# Named AI tool reads
# ---------------------------------------------------------------------------

def list_services(partner_id: int | None = None, category_id: int | None = None,
                  actor_user_id: int | None = None, approved_only: bool = False,
                  limit: int = 100):
    if actor_user_id is not None:
        assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    where = ["s.status <> 'deleted'"]
    params: list[Any] = []
    if approved_only:
        where += ["s.status='approved'", "p.status='approved'"]
    if partner_id is not None:
        where.append("s.partner_id=%s"); params.append(int(partner_id))
    if category_id is not None:
        where.append("s.category_id=%s"); params.append(int(category_id))
    params.append(max(1, min(int(limit or 100), 200)))
    return rows(
        """SELECT s.id,s.partner_id,s.business_id,s.name,s.category_id,s.price,s.status,
                  p.business_name AS partner_name,c.name_am AS category_name_am,
                  c.name_ru AS category_name_ru,c.name_en AS category_name_en,
                  c.master_category_id
           FROM services s
           LEFT JOIN categories c ON c.id=s.category_id
           LEFT JOIN partners p ON p.id=s.partner_id
           WHERE """ + " AND ".join(where) +
        " ORDER BY s.id DESC LIMIT %s", tuple(params)
    )


def get_partner_addresses(partner_id: int, actor_user_id: int | None = None, limit: int = 100):
    if actor_user_id is not None:
        assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    if not _table_exists("partner_objects"):
        return []
    return rows(
        """SELECT id,partner_id,business_id,object_name,address,city,marz,phone
           FROM partner_objects
           WHERE partner_id=%s
           ORDER BY business_id,id LIMIT %s""",
        (int(partner_id), max(1, min(int(limit or 100), 200))),
    )


def check_application(application_id: int) -> dict[str, Any]:
    app = get_application(int(application_id))
    if not app:
        return {"application_id": int(application_id), "found": False, "checks": []}
    checks = [
        {"field": "status", "value": app.get("status"), "severity": "info"},
        {"field": "service", "value": bool(str(app.get("service_name") or "").strip()),
         "severity": "ok" if app.get("service_name") else "warning"},
        {"field": "price", "value": app.get("price"),
         "severity": "ok" if app.get("price") not in (None, "") else "warning"},
    ]
    docs = get_documents(application_id=int(application_id))
    checks.append({"field": "documents", "value": len(docs),
                    "severity": "ok" if docs else "warning"})
    category_id = app.get("category_id")
    master_id = app.get("master_category_id")
    if category_id is not None:
        category = get_catalog_category(int(category_id))
        if category:
            checks.append({"field": "category", "value": category,
                           "severity": "ok" if category.get("is_active") else "error"})
            if master_id is not None and category.get("master_category_id") is not None:
                try:
                    same = int(master_id) == int(category["master_category_id"])
                    checks.append({"field": "direction_category_match", "value": same,
                                   "severity": "ok" if same else "error"})
                except (TypeError, ValueError):
                    pass
        else:
            checks.append({"field": "category", "value": category_id, "severity": "error",
                           "message": "Stored category does not exist in the catalog."})
    return {"application_id": int(application_id), "found": True, "checks": checks,
            "direction_name": app.get("direction_name"),
            "master_category_id": master_id,
            "subcategory_name": app.get("subcategory_name")}

# ---------------------------------------------------------------------------
# Applications / documents
# ---------------------------------------------------------------------------

def get_application(application_id: int):
    return one(
        """SELECT a.*,p.user_id,p.business_name AS partner_business_name
           FROM partner_applications a
           LEFT JOIN partners p ON p.id=a.partner_id
           WHERE a.id=%s""",
        (int(application_id),),
    )


def search_applications(status: str | None = None, marz: str | None = None,
                        city: str | None = None, limit: int = 50):
    where = ["1=1"]
    params: list[Any] = []
    if status:
        where.append("a.status=%s")
        params.append(status)
    if marz:
        where.append("a.location_marz ILIKE %s")
        params.append(f"%{marz}%")
    if city:
        where.append("a.location_city ILIKE %s")
        params.append(f"%{city}%")
    params.append(max(1, min(int(limit or 50), 200)))
    return rows(
        """SELECT a.*,p.business_name AS partner_business_name
           FROM partner_applications a
           LEFT JOIN partners p ON p.id=a.partner_id
           WHERE """ + " AND ".join(where) +
        " ORDER BY a.id DESC LIMIT %s",
        tuple(params),
    )


def count(entity: str) -> int:
    allowed = {
        "partners": "SELECT COUNT(*) AS n FROM partners",
        "applications": "SELECT COUNT(*) AS n FROM partner_applications",
        "services": "SELECT COUNT(*) AS n FROM services",
        "directions": "SELECT COUNT(*) AS n FROM master_categories WHERE is_active=TRUE",
        "subcategories": "SELECT COUNT(*) AS n FROM categories WHERE is_active=TRUE",
        "companies": "SELECT COUNT(*) AS n FROM partner_businesses WHERE status <> 'archived'",
    }
    sql = allowed.get(str(entity or "").strip().lower())
    if not sql:
        raise ValueError("unsupported_count_entity")
    row = one(sql) or {}
    return int(row.get("n") or 0)


def get_documents(application_id: int | None = None, partner_id: int | None = None, limit: int = 50):
    if application_id is not None:
        app = get_application(int(application_id))
        partner_id = app.get("partner_id") if app else None
    if not partner_id:
        return []
    # Keep compatibility with deployments where document columns differ.
    cols = rows(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='partner_verification_documents' "
        "ORDER BY ordinal_position"
    )
    names = {str(x.get("column_name")) for x in cols}
    wanted = [
        "id","partner_id","application_id","partner_application_id","document_type",
        "file_name","original_filename","file_url","status","verification_status",
        "admin_note","rejection_reason","created_at","updated_at"
    ]
    select_cols = [x for x in wanted if x in names]
    if not select_cols:
        return []
    return rows(
        "SELECT " + ",".join(select_cols) +
        " FROM partner_verification_documents WHERE partner_id=%s ORDER BY id DESC LIMIT %s",
        (int(partner_id), max(1, min(int(limit or 50), 200))),
    )


# ---------------------------------------------------------------------------
# Client request / candidate persistence
# ---------------------------------------------------------------------------

def create_service_request(client_id: int, category_id: int | None, status: str,
                           language: str, city: str | None, summary: str,
                           preferences: dict[str, Any]):
    return execute(
        """INSERT INTO service_requests
           (client_id,category_id,status,language,city,summary,preferences_json)
           VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
        (int(client_id),category_id,status,language,city,summary,json.dumps(preferences,ensure_ascii=False)),
        True,
    )


def update_service_request(request_id: int, category_id: int | None, status: str,
                           summary: str, city: str | None, preferences: dict[str, Any]):
    return execute(
        """UPDATE service_requests
           SET category_id=%s,status=%s,summary=%s,city=%s,
               preferences_json=%s::jsonb,updated_at=NOW()
           WHERE id=%s RETURNING *""",
        (category_id,status,summary,city,json.dumps(preferences,ensure_ascii=False),int(request_id)),
        True,
    )


def replace_request_candidates(request_id: int, candidates: list[dict[str, Any]], reason: str):
    execute("DELETE FROM request_candidates WHERE request_id=%s",(int(request_id),))
    for rank, candidate in enumerate(candidates,1):
        execute(
            """INSERT INTO request_candidates
               (request_id,partner_id,service_id,rank_score,match_reason)
               VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (int(request_id),candidate.get("partner_id"),candidate.get("service_id"),
             100-rank*5,reason),
        )



# ---------------------------------------------------------------------------
# Operational AI context reads
# ---------------------------------------------------------------------------
# These methods keep schema knowledge inside Data Core. AI Context should call
# these named business reads instead of composing SQL itself.

def _table_exists(table_name: str) -> bool:
    row = one(
        "SELECT 1 AS ok FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s LIMIT 1",
        (str(table_name),),
    )
    return bool(row)


def _count_table(table_name: str, where: str = "", params: Iterable[Any] = ()) -> int | None:
    if not _table_exists(table_name):
        return None
    # table_name is selected only from application-owned call sites below.
    sql = f'SELECT COUNT(*) AS n FROM "public"."{table_name}"'
    if where:
        sql += " WHERE " + where
    row = one(sql, tuple(params))
    return int((row or {}).get("n") or 0)


def operational_stats() -> dict[str, Any]:
    """Return verified platform statistics for AI Context/Admin AI."""
    stats: dict[str, Any] = {
        "platform": {}, "catalog": {}, "geography": {},
        "applications": {}, "documents": {}, "orders": {}, "work_queue": {},
    }
    for key, table, where in (
        ("partners", "partners", ""),
        ("companies", "partner_businesses", "status <> 'archived'"),
        ("services", "services", "status IS NULL OR status <> 'deleted'"),
        ("clients", "users", "role = 'client'"),
        ("applications", "partner_applications", ""),
        ("directions", "master_categories", "is_active = TRUE"),
        ("subcategories", "categories", "is_active = TRUE"),
    ):
        value = _count_table(table, where)
        if value is not None:
            if key in {"directions", "subcategories"}:
                stats["catalog"][key] = value
            elif key == "clients":
                stats["platform"][key] = value
            elif key == "applications":
                stats["applications"]["total"] = value
            else:
                stats["platform"][key] = value

    for status in ("pending_admin", "approved", "rejected"):
        value = _count_table("partner_applications", "status=%s", (status,))
        if value is not None:
            stats["applications"][status] = value

    if _table_exists("partner_verification_documents"):
        stats["documents"]["total"] = _count_table("partner_verification_documents") or 0
        for status in ("pending", "under_review", "approved", "rejected"):
            value = _count_table(
                "partner_verification_documents",
                "(status=%s OR verification_status=%s)",
                (status, status),
            )
            if value is not None:
                stats["documents"][status] = value

    if _table_exists("bookings"):
        stats["orders"]["total"] = _count_table("bookings") or 0
        for status in ("new", "pending", "confirmed", "active", "completed", "cancelled", "canceled"):
            value = _count_table("bookings", "status=%s", (status,))
            if value:
                stats["orders"][status] = value

    stats["work_queue"]["applications_to_review"] = stats["applications"].get("pending_admin", 0)
    stats["work_queue"]["documents_to_review"] = (
        stats["documents"].get("pending", 0) + stats["documents"].get("under_review", 0)
    )


    if _table_exists("partner_objects"):
        row = one("SELECT COUNT(DISTINCT city) AS n FROM partner_objects WHERE city IS NOT NULL AND TRIM(city)<>''")
        if row:
            stats["geography"]["cities"] = int(row.get("n") or 0)
        row = one("SELECT COUNT(DISTINCT marz) AS n FROM partner_objects WHERE marz IS NOT NULL AND TRIM(marz)<>''")
        if row:
            stats["geography"]["marzes"] = int(row.get("n") or 0)

    for table in ("payments", "financial_transactions", "transactions", "partner_payouts", "commissions"):
        if _table_exists(table):
            stats.setdefault("finance", {})["records"] = _count_table(table) or 0
            break
    return stats


def get_ai_entity(entity_type: str, entity_id: int, full: bool = False) -> dict[str, Any] | None:
    """Return a role-neutral verified entity graph for AI Context."""
    eid = int(entity_id)
    kind = str(entity_type or "").strip().lower()

    if kind == "application":
        app = one(
            """SELECT a.id,a.partner_id,a.business_id,a.business_name,a.status,a.service_name,a.price,
                      a.direction_name,a.master_category_id,a.subcategory_name,a.category_id,
                      a.location_marz,a.location_city,a.location_village,a.address,a.phone,
                      a.description,a.object_name,a.object_id,a.created_at,a.updated_at,a.payload_json,
                      p.business_name AS partner_name
               FROM partner_applications a LEFT JOIN partners p ON p.id=a.partner_id
               WHERE a.id=%s""", (eid,))
        if not app:
            return None
        out={"type":"application","id":eid,"profile":app}
        out["documents"]=get_documents(application_id=eid,limit=30)
        if app.get("business_id"):
            out["company"]=get_ai_entity("company",int(app["business_id"]),full=full)
        if app.get("partner_id"):
            out["partner"]=get_ai_entity("partner",int(app["partner_id"]),full=False)
        return out

    if kind == "partner":
        partner=one("""SELECT id,user_id,business_name,business_description,status,
                              verification_status,contact_share_policy,created_at,updated_at
                       FROM partners WHERE id=%s""",(eid,))
        if not partner: return None
        out={"type":"partner","id":eid,"profile":partner}
        out["companies"]=list_companies(eid)
        if full:
            out["services"]=rows(
                """SELECT id,business_id,name,description,price,status,category_id,created_at,updated_at
                   FROM services WHERE partner_id=%s AND (status IS NULL OR status<>'deleted')
                   ORDER BY id DESC LIMIT 100""",(eid,))
            if _table_exists("partner_objects"):
                out["addresses"]=rows(
                    """SELECT id,business_id,object_name,address,city,marz,phone
                       FROM partner_objects WHERE partner_id=%s
                       ORDER BY business_id,id LIMIT 100""",(eid,))
        return out

    if kind in {"company","business"}:
        company=get_company(eid)
        if not company: return None
        out={"type":"company","id":eid,"profile":company}
        if _table_exists("partner_objects"):
            out["addresses"]=rows(
                """SELECT id,business_id,object_name,address,city,marz,phone
                   FROM partner_objects WHERE business_id=%s
                   ORDER BY id LIMIT 50""",(eid,))
        out["services"]=rows(
            """SELECT id,business_id,name,description,price,status,category_id,created_at,updated_at
               FROM services WHERE business_id=%s AND (status IS NULL OR status<>'deleted')
               ORDER BY id DESC LIMIT 100""",(eid,))
        return out

    if kind == "service":
        return get_service(eid)

    if kind in {"category","subcategory"}:
        return get_catalog_category(eid)

    if kind == "order":
        return one("SELECT * FROM bookings WHERE id=%s",(eid,)) if _table_exists("bookings") else None
    return None


def active_negotiation_id_for_user(user_id: int) -> int | None:
    """Return the latest active negotiation visible to this Telegram user."""
    row = one(
        """SELECT id FROM negotiations
           WHERE (client_id=%s OR partner_id=(SELECT id FROM partners WHERE user_id=%s))
             AND status IN ('active','pending','negotiating')
           ORDER BY updated_at DESC LIMIT 1""",
        (int(user_id), int(user_id)),
    )
    return int(row["id"]) if row and row.get("id") is not None else None


# ---------------------------------------------------------------------------
# AI session / history
# ---------------------------------------------------------------------------

def active_session(user_id: int, role: str, session_type: str):
    return platform_db.active_session(int(user_id),role,session_type)


def create_session(user_id: int, role: str, session_type: str, context: dict[str, Any] | None = None):
    return platform_db.create_session(int(user_id),role,session_type,context or {})


def update_session(session_id: int, context: dict[str, Any]):
    return platform_db.update_session(int(session_id),context)


def add_ai_message(session_id: int, sender_role: str, text: str, data: dict[str, Any] | None = None):
    return platform_db.add_ai_message(int(session_id),sender_role,text,data or {})


def recent_ai_messages(session_id: int, limit: int = 16):
    return platform_db.recent_ai_messages(int(session_id),limit)


# ---------------------------------------------------------------------------
# Potential partners
# ---------------------------------------------------------------------------

def potential_partners(status: str | None = None, query: str | None = None):
    return platform_db.potential_partners(status,query)


def create_potential(data: dict[str, Any]):
    return platform_db.create_potential(data)


def update_potential(potential_id: int, **fields):
    return platform_db.update_potential(int(potential_id),**fields)


# ---------------------------------------------------------------------------
# Safe ownership / permissions
# ---------------------------------------------------------------------------

def assert_partner_owns_partner(partner_id: int, actor_user_id: int):
    partner = get_partner(int(partner_id))
    if not partner or int(partner.get("user_id") or 0) != int(actor_user_id):
        raise PermissionError("partner_access_denied")
    return partner


def assert_partner_owns_company(company_id: int, actor_user_id: int):
    company = get_company(int(company_id))
    if not company:
        raise LookupError("company_not_found")
    assert_partner_owns_partner(int(company["partner_id"]), int(actor_user_id))
    return company


def assert_partner_owns_service(service_id: int, actor_user_id: int):
    service = get_service(int(service_id))
    if not service:
        raise LookupError("service_not_found")
    assert_partner_owns_partner(int(service["partner_id"]), int(actor_user_id))
    return service
