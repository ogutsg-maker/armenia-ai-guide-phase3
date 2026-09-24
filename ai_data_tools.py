from __future__ import annotations

"""Safe, role-aware data tools for the conversational AI core.

The model selects tools by semantic intent. Tool implementations remain Python-owned:
they validate role/arguments and read/write through the platform DB layer. The model never
receives arbitrary SQL access.
"""

from typing import Any, Dict, List
from decimal import Decimal
from datetime import date, datetime
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
        "description": "Count a supported business entity without exposing SQL. `directions` means active master categories; `subcategories` means active catalog subcategories.",
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
    def _safe_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): DataTools._safe_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [DataTools._safe_value(v) for v in value]
        return str(value)

    @classmethod
    def _safe_rows(cls, rows: Any) -> List[Dict[str, Any]]:
        if not rows:
            return []
        return [cls._safe_value(dict(x)) if isinstance(x, dict) else cls._safe_value(x) for x in rows]

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
        cols = platform_db.rows(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='partner_verification_documents' "
            "ORDER BY ordinal_position"
        )
        names = {str(x.get("column_name")) for x in cols}
        if "partner_id" not in names:
            raise DataToolError("documents_schema_missing_partner_id")
        wanted = [
            "id","partner_id","application_id","partner_application_id","document_type",
            "file_name","file_url","status","verification_status","admin_note",
            "rejection_reason","created_at","updated_at"
        ]
        select_cols = [x for x in wanted if x in names]
        rows = platform_db.rows(
            "SELECT " + ",".join(select_cols) +
            " FROM partner_verification_documents WHERE partner_id=%s ORDER BY id DESC LIMIT 50",
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

        # Verify the stored category against its parent direction when both
        # IDs exist. This is a reusable business rule, not a phrase-specific fix.
        category_id=app.get("category_id")
        master_id=app.get("master_category_id")
        if category_id is not None:
            category=platform_db.one(
                """SELECT id,master_category_id,name_am,name_ru,name_en,is_active
                   FROM categories WHERE id=%s""",(int(category_id),)
            )
            if category:
                checks.append({"field":"category","value":category,
                               "severity":"ok" if category.get("is_active") else "error"})
                if master_id is not None and category.get("master_category_id") is not None:
                    try:
                        same=int(master_id)==int(category["master_category_id"])
                        checks.append({"field":"direction_category_match","value":same,
                                       "severity":"ok" if same else "error"})
                    except (TypeError,ValueError):
                        pass
            else:
                checks.append({"field":"category","value":category_id,"severity":"error",
                               "message":"Stored category does not exist in the active catalog."})
        return {"application_id": int(aid), "found": True, "checks": checks,
                "direction_name":app.get("direction_name"),
                "master_category_id":master_id,
                "subcategory_name":app.get("subcategory_name")}

    def _tool_check_catalog_match(self, args: Dict[str, Any]) -> Dict[str, Any]:
        service = str(args.get("service_name") or "").strip()
        if not service:
            raise DataToolError("service_name_required")

        # Semantic catalog matching is deliberately generic: the AI supplies the
        # service phrase, while this layer resolves multilingual morphology/concepts
        # against the live catalog. IDs still come only from the database.
        import re
        aliases = {
            "eyebrows": {"брови","бровь","бровей","հոնք","հոնքեր","հոնքերի","eyebrow","eyebrows"},
            "manicure": {"маникюр","մատնահարդարում","manicure"},
            "pedicure": {"педикюр","պեդիկյուր","pedicure"},
            "makeup": {"макияж","визаж","դիմահարդարում","makeup"},
            "haircut": {"стрижка","стрижку","стрижки","վարսավիր","սանրվածք","haircut"},
            "hair": {"волосы","волос","мազ","մազեր","hair"},
            "coloring": {"окрашивание","окраска","окрасить","ներկում","ներկել","coloring","colouring"},
        }
        def norm(v):
            return " ".join(str(v or "").casefold().strip().split())
        def tokens(v):
            return set(re.findall(r"[a-zа-яёևա-ֆ0-9-]+", norm(v)))
        raw=tokens(service)
        concepts=set(raw)
        for key, vals in aliases.items():
            if raw.intersection(vals) or key in raw:
                concepts.add(key)
                concepts.update(vals)

        # First get the live catalog without relying on text search, then score
        # every active candidate by exact/phrase/concept/token/root overlap.
        rows=self._tool_search_catalog({}).get("items", [])
        scored=[]
        for row in rows:
            names=[norm(row.get(k)) for k in ("name_am","name_ru","name_en") if row.get(k)]
            if not names:
                continue
            text=" ".join(names)
            rt=tokens(text)
            rc=set(rt)
            for key, vals in aliases.items():
                if rt.intersection(vals) or key in rt:
                    rc.add(key); rc.update(vals)
            # Separate subject/object concepts from generic operations. Object matches
            # must dominate: "eyebrow coloring" belongs to eyebrows, not generic hair coloring.
            object_keys={"eyebrows","manicure","pedicure","makeup","haircut","hair"}
            operation_keys={"coloring"}
            object_overlap=concepts.intersection(rc).intersection(object_keys)
            operation_overlap=concepts.intersection(rc).intersection(operation_keys)
            score=0
            if norm(service) in names or any(norm(service)==x for x in names):
                score=1000
            elif any(norm(service) in x or x in norm(service) for x in names):
                score=700
            if object_overlap:
                score=max(score,650+min(len(object_overlap),3)*80)
            if operation_overlap:
                score=max(score,320+min(len(operation_overlap),2)*25)
            concept_overlap=concepts.intersection(rc)
            if concept_overlap:
                score=max(score,400+min(len(concept_overlap),5)*20)
            overlap=raw.intersection(rt)
            if overlap:
                score=max(score,260+min(len(overlap),5)*20)
            roots=0
            for token in raw:
                if len(token)<4: continue
                if any(token in ct or ct in token for ct in rt if len(ct)>=4):
                    roots+=1
            if roots:
                score=max(score,150+min(roots,5)*15)
            if score:
                scored.append((score,row,object_overlap,operation_overlap))
        scored.sort(key=lambda x:(x[0],len(x[2]),-len(x[3]),str(x[1].get("name_am") or x[1].get("name_ru") or "").casefold()),reverse=True)
        candidates=[]
        for score,row,obj,op in scored[:8]:
            item=dict(row)
            item["object_matches"]=sorted(obj)
            item["operation_matches"]=sorted(op)
            item["match_reason"]="object_match" if obj else ("operation_match" if op else "text_match")
            candidates.append(item)
        return {"service_name":service,"candidates":candidates,"count":len(candidates),
                "top_score":scored[0][0] if scored else 0,
                "top_object_matches":sorted(scored[0][2]) if scored else [],
                "top_operation_matches":sorted(scored[0][3]) if scored else []}
