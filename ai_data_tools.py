from __future__ import annotations

"""Safe, role-aware data tools for the conversational AI core.

The model selects tools by semantic intent. Tool implementations remain Python-owned:
they validate role/arguments and read/write through the platform DB layer. The model never
receives arbitrary SQL access.
"""

from typing import Any, Dict, List
import platform_db


ROLES = {"admin", "partner", "client", "potential_partner"}

# These are descriptions exposed to the model. They intentionally describe business
# capabilities, not database tables or SQL.
TOOL_DEFINITIONS = {
    "search_partners": {
        "description": "Find partners/businesses matching a name, service, city, region or status.",
        "roles": {"admin", "client"},
    },
    "get_partner": {
        "description": "Get the allowed profile data for one partner.",
        "roles": {"admin", "partner", "client"},
    },
    "get_application": {
        "description": "Get a partner application and its current review state.",
        "roles": {"admin"},
    },
    "get_documents": {
        "description": "Get verification documents and their statuses for a partner/application.",
        "roles": {"admin", "partner"},
    },
    "get_addresses": {
        "description": "Get business objects and addresses available to the current role.",
        "roles": {"admin", "partner", "client"},
    },
    "get_directions": {
        "description": "Get directions/master categories related to an entity or the catalog.",
        "roles": {"admin", "partner", "client"},
    },
    "search_catalog": {
        "description": "Search active catalog categories/subcategories by name or parent direction.",
        "roles": {"admin", "partner", "client"},
    },
    "get_services": {
        "description": "Get services, prices and catalog links visible to the current role.",
        "roles": {"admin", "partner", "client"},
    },
    "get_orders": {
        "description": "Get orders visible to the current role.",
        "roles": {"admin", "partner", "client"},
    },
    "check_application": {
        "description": "Run factual consistency/completeness checks on an application.",
        "roles": {"admin"},
    },
    "check_catalog_match": {
        "description": "Check whether a service maps plausibly to an active catalog category.",
        "roles": {"admin", "partner"},
    },
    "count": {
        "description": "Count a supported business entity without exposing SQL.",
        "roles": {"admin", "partner", "client"},
    },
}

_COUNT_SQL = {
    "partners": "SELECT COUNT(*) AS count FROM partners",
    "applications": "SELECT COUNT(*) AS count FROM partner_applications",
    "services": "SELECT COUNT(*) AS count FROM services",
    "directions": "SELECT COUNT(*) AS count FROM master_categories WHERE is_active=TRUE",
    "subcategories": "SELECT COUNT(*) AS count FROM categories WHERE is_active=TRUE",
}


class DataToolError(Exception):
    pass


class DataTools:
    """Role-aware facade between AI and the platform database."""

    def __init__(self, role: str, actor_id: int | None = None):
        role = str(role or "").strip().lower()
        if role not in ROLES:
            raise DataToolError("unsupported_role")
        self.role = role
        self.actor_id = int(actor_id) if actor_id is not None else None

    def available_tools(self) -> List[Dict[str, Any]]:
        return [
            {"name": name, "description": spec["description"]}
            for name, spec in TOOL_DEFINITIONS.items()
            if self.role in spec["roles"]
        ]

    def execute(self, name: str, arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
        name = str(name or "").strip()
        arguments = arguments if isinstance(arguments, dict) else {}
        spec = TOOL_DEFINITIONS.get(name)
        if not spec or self.role not in spec["roles"]:
            raise DataToolError("tool_not_allowed")

        handler = getattr(self, "_tool_" + name, None)
        if not handler:
            raise DataToolError("tool_not_implemented")
        return {"tool": name, "data": handler(arguments)}

    @staticmethod
    def _safe_rows(rows: Any) -> List[Dict[str, Any]]:
        if not rows:
            return []
        return platform_db._admin_safe(rows) if hasattr(platform_db, "_admin_safe") else [
            dict(x) if isinstance(x, dict) else x for x in rows
        ]

    def _tool_count(self, args: Dict[str, Any]) -> Dict[str, Any]:
        entity = str(args.get("entity") or "").strip().lower()
        sql = _COUNT_SQL.get(entity)
        if not sql:
            raise DataToolError("unsupported_count_entity")
        row = platform_db.one(sql) or {}
        return {"entity": entity, "count": int(row.get("count") or 0)}

    def _tool_search_partners(self, args: Dict[str, Any]) -> Dict[str, Any]:
        q = str(args.get("query") or "").strip()
        city = str(args.get("city") or "").strip()
        limit = min(max(int(args.get("limit") or 20), 1), 50)
        where = ["p.status <> 'archived'"]
        params: List[Any] = []
        if q:
            where.append("(p.business_name ILIKE %s OR p.business_description ILIKE %s)")
            params += [f"%{q}%", f"%{q}%"]
        if city:
            where.append("""EXISTS (
                SELECT 1 FROM partner_applications pa
                WHERE pa.partner_id=p.id AND pa.location_city ILIKE %s
            )""")
            params.append(f"%{city}%")
        rows = platform_db.rows(
            """SELECT p.id,p.business_name,p.status,p.verification_status,p.business_description
               FROM partners p WHERE """ + " AND ".join(where) +
            " ORDER BY p.id DESC LIMIT %s",
            tuple(params + [limit]),
        )
        return {"items": self._safe_rows(rows), "count": len(rows)}

    def _tool_get_partner(self, args: Dict[str, Any]) -> Dict[str, Any]:
        partner_id = args.get("partner_id")
        if partner_id is None:
            raise DataToolError("partner_id_required")
        row = platform_db.one(
            """SELECT id,user_id,status,verification_status,business_name,
                      business_description,contact_share_policy,created_at,updated_at
               FROM partners WHERE id=%s""",
            (int(partner_id),),
        )
        if not row:
            return {"partner": None}
        # A partner may read only their own private profile; clients receive
        # the public-safe subset.
        if self.role == "partner":
            if self.actor_id is None or int(row.get("user_id") or 0) != self.actor_id:
                raise DataToolError("partner_access_denied")
        if self.role == "client":
            row = {k: row.get(k) for k in ("id", "business_name", "business_description", "status")}
        return {"partner": self._safe_rows([row])[0]}

    def _tool_get_application(self, args: Dict[str, Any]) -> Dict[str, Any]:
        aid = args.get("application_id")
        if aid is None:
            raise DataToolError("application_id_required")
        row = platform_db.one(
            """SELECT a.*,p.user_id,p.business_name AS partner_business_name
               FROM partner_applications a
               LEFT JOIN partners p ON p.id=a.partner_id
               WHERE a.id=%s""",
            (int(aid),),
        )
        return {"application": self._safe_rows([row])[0] if row else None}

    def _tool_get_documents(self, args: Dict[str, Any]) -> Dict[str, Any]:
        aid = args.get("application_id")
        partner_id = args.get("partner_id")
        if not aid and not partner_id:
            raise DataToolError("application_or_partner_required")
        if aid:
            app = platform_db.one("SELECT partner_id FROM partner_applications WHERE id=%s", (int(aid),))
            partner_id = app.get("partner_id") if app else None
        if not partner_id:
            return {"documents": []}
        rows = platform_db.rows(
            """SELECT id,partner_id,application_id,document_type,file_name,file_url,
                      status,verification_status,admin_note,rejection_reason,created_at,updated_at
               FROM partner_verification_documents
               WHERE partner_id=%s ORDER BY id DESC LIMIT 50""",
            (int(partner_id),),
        )
        if self.role == "partner":
            partner = platform_db.one("SELECT user_id FROM partners WHERE id=%s", (int(partner_id),))
            if not partner or self.actor_id is None or int(partner.get("user_id") or 0) != self.actor_id:
                raise DataToolError("partner_access_denied")
        return {"documents": self._safe_rows(rows)}

    def _tool_get_addresses(self, args: Dict[str, Any]) -> Dict[str, Any]:
        partner_id = args.get("partner_id")
        if partner_id is None:
            raise DataToolError("partner_id_required")
        if self.role == "partner":
            partner = platform_db.one("SELECT user_id FROM partners WHERE id=%s", (int(partner_id),))
            if not partner or self.actor_id is None or int(partner.get("user_id") or 0) != self.actor_id:
                raise DataToolError("partner_access_denied")
        rows = platform_db.rows(
            """SELECT id,partner_id,name,description,phone,status
               FROM partner_businesses WHERE partner_id=%s AND status<>'archived'
               ORDER BY id DESC LIMIT 100""",
            (int(partner_id),),
        )
        return {"items": self._safe_rows(rows)}

    def _tool_get_directions(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rows = platform_db.rows(
            """SELECT id,name_am,name_ru,name_en,slug,is_active
               FROM master_categories WHERE is_active=TRUE ORDER BY id"""
        )
        return {"items": self._safe_rows(rows), "count": len(rows)}

    def _tool_search_catalog(self, args: Dict[str, Any]) -> Dict[str, Any]:
        q = str(args.get("query") or "").strip()
        master_id = args.get("master_category_id")
        where = ["c.is_active=TRUE", "m.is_active=TRUE"]
        params: List[Any] = []
        if q:
            where.append("""(
                c.name_am ILIKE %s OR c.name_ru ILIKE %s OR c.name_en ILIKE %s OR
                c.slug ILIKE %s OR m.name_am ILIKE %s OR m.name_ru ILIKE %s OR m.name_en ILIKE %s
            )""")
            params += [f"%{q}%"] * 7
        if master_id is not None:
            where.append("c.master_category_id=%s")
            params.append(int(master_id))
        rows = platform_db.rows(
            """SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,
                      m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en
               FROM categories c JOIN master_categories m ON m.id=c.master_category_id
               WHERE """ + " AND ".join(where) + " ORDER BY c.id",
            tuple(params),
        )
        return {"items": self._safe_rows(rows), "count": len(rows)}

    def _tool_get_services(self, args: Dict[str, Any]) -> Dict[str, Any]:
        partner_id = args.get("partner_id")
        category_id = args.get("category_id")
        where = ["s.status <> 'archived'"]
        params: List[Any] = []
        if partner_id is not None:
            if self.role == "partner":
                partner = platform_db.one("SELECT user_id FROM partners WHERE id=%s", (int(partner_id),))
                if not partner or self.actor_id is None or int(partner.get("user_id") or 0) != self.actor_id:
                    raise DataToolError("partner_access_denied")
            where.append("s.partner_id=%s")
            params.append(int(partner_id))
        elif self.role == "partner":
            if self.actor_id is None:
                raise DataToolError("actor_required")
            where.append("p.user_id=%s")
            params.append(self.actor_id)
        if category_id is not None:
            where.append("s.category_id=%s")
            params.append(int(category_id))
        rows = platform_db.rows(
            """SELECT s.id,s.partner_id,s.business_id,s.name,s.category_id,s.price,s.status,
                      p.business_name AS partner_name,c.name_am AS category_name_am,
                      c.name_ru AS category_name_ru,c.name_en AS category_name_en,
                      c.master_category_id
               FROM services s
               LEFT JOIN categories c ON c.id=s.category_id
               LEFT JOIN partners p ON p.id=s.partner_id
               WHERE """ + " AND ".join(where) + " ORDER BY s.id DESC LIMIT 100""",
            tuple(params),
        )
        if self.role == "client":
            rows = [
                {k: x.get(k) for k in ("id","partner_id","business_id","name","category_id",
                                        "price","status","partner_name","category_name_am",
                                        "category_name_ru","category_name_en","master_category_id")}
                for x in rows
            ]
        return {"items": self._safe_rows(rows), "count": len(rows)}

    def _tool_get_orders(self, args: Dict[str, Any]) -> Dict[str, Any]:
        # Order schema differs across project stages. Keep this tool intentionally
        # conservative until the canonical orders table/API is finalized.
        raise DataToolError("orders_tool_pending_schema_mapping")

    def _tool_check_application(self, args: Dict[str, Any]) -> Dict[str, Any]:
        aid = args.get("application_id")
        if aid is None:
            raise DataToolError("application_id_required")
        app = self._tool_get_application({"application_id": aid})["application"]
        if not app:
            return {"application_id": int(aid), "checks": [], "found": False}
        checks = []
        checks.append({"field": "status", "value": app.get("status"), "severity": "info"})
        checks.append({"field": "service", "value": bool(str(app.get("service_name") or "").strip()),
                       "severity": "ok" if app.get("service_name") else "warning"})
        checks.append({"field": "price", "value": app.get("price"),
                       "severity": "ok" if app.get("price") not in (None, "") else "warning"})
        docs = self._tool_get_documents({"application_id": aid})["documents"]
        checks.append({"field": "documents", "value": len(docs),
                       "severity": "ok" if docs else "warning"})
        return {"application_id": int(aid), "found": True, "checks": checks}

    def _tool_check_catalog_match(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = str(args.get("service_name") or "").strip()
        if not service:
            raise DataToolError("service_name_required")
        catalog = self._tool_search_catalog({"query": service}).get("items", [])
        return {"service_name": service, "candidates": catalog[:8], "count": len(catalog)}
