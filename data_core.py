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
            SELECT 1 FROM partner_locations pl
            WHERE pl.partner_id=p.id AND pl.is_active=TRUE
              AND (LOWER(COALESCE(pl.city,''))=LOWER(%s)
                OR LOWER(COALESCE(pl.village,''))=LOWER(%s)
                OR LOWER(COALESCE(pl.marz,''))=LOWER(%s)
                OR COALESCE(pl.is_all_armenia,FALSE)=TRUE)
        )""")
        params.extend([city, city, city])
    params.append(max(1, min(int(limit or 100), 200)))
    return rows(
        """SELECT DISTINCT s.id,s.partner_id,s.business_id,s.name,s.category_id,
                  s.price,s.status,s.duration_minutes,p.business_name AS partner_name,
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
