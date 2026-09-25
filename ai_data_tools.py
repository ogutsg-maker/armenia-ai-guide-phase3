from __future__ import annotations

"""Role-aware business tools exposed to the AI core.

All persistence and schema access is delegated to Data Core. This module owns
tool authorization and argument validation, not SQL.
"""

from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any, Dict, List

import data_core


ROLES = {"admin", "partner", "client", "potential_partner"}

TOOL_DEFINITIONS = {
    "search_partners": {"description": "Find partners/businesses matching a name, service, city, region or status.", "roles": {"admin", "client"}},
    "get_partner": {"description": "Get the allowed profile data for one partner.", "roles": {"admin", "partner", "client"}},
    "get_application": {"description": "Get a partner application and its current review state.", "roles": {"admin"}},
    "get_documents": {"description": "Get verification documents and their statuses for a partner/application.", "roles": {"admin", "partner"}},
    "get_addresses": {"description": "Get business objects and addresses available to the current role.", "roles": {"admin", "partner", "client"}},
    "get_directions": {"description": "Get active master directions from the catalog.", "roles": {"admin", "partner", "client"}},
    "search_catalog": {"description": "Search active catalog categories/subcategories by name or parent direction.", "roles": {"admin", "partner", "client"}},
    "get_services": {"description": "Get services, prices and catalog links visible to the current role.", "roles": {"admin", "partner", "client"}},
    "get_orders": {"description": "Get orders visible to the current role.", "roles": {"admin", "partner", "client"}},
    "check_application": {"description": "Run factual consistency/completeness checks on an application.", "roles": {"admin"}},
    "check_catalog_match": {"description": "Check whether a service maps plausibly to an active catalog category.", "roles": {"admin", "partner"}},
    "count": {"description": "Count a supported business entity without exposing SQL. directions means active master categories; subcategories means active catalog subcategories.", "roles": {"admin", "partner", "client"}},
}


class DataToolError(Exception):
    pass


class DataTools:
    """Controlled facade between AI and Data Core."""

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
        args = arguments if isinstance(arguments, dict) else {}
        spec = TOOL_DEFINITIONS.get(name)
        if not spec or self.role not in spec["roles"]:
            raise DataToolError("tool_not_allowed")
        handler = getattr(self, "_tool_" + name, None)
        if not handler:
            raise DataToolError("tool_not_implemented")
        return {"tool": name, "data": self._safe_value(handler(args))}

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
        if isinstance(value, (list, tuple, set)):
            return [DataTools._safe_value(v) for v in value]
        return str(value)

    def _tool_count(self, args):
        entity = str(args.get("entity") or "").strip().lower()
        if entity not in {"partners", "applications", "services", "directions", "subcategories", "companies"}:
            raise DataToolError("unsupported_count_entity")
        return {"entity": entity, "count": data_core.count(entity)}

    def _tool_search_partners(self, args):
        rows = data_core.search_partners(
            query=str(args.get("query") or "").strip(),
            city=str(args.get("city") or "").strip(),
            limit=min(max(int(args.get("limit") or 20), 1), 100),
        )
        return {"items": rows, "count": len(rows)}

    def _tool_get_partner(self, args):
        partner_id = args.get("partner_id")
        if partner_id is None:
            raise DataToolError("partner_id_required")
        row = data_core.get_partner(int(partner_id))
        if not row:
            return {"partner": None}
        if self.role == "partner":
            if self.actor_id is None or int(row.get("user_id") or 0) != self.actor_id:
                raise DataToolError("partner_access_denied")
        if self.role == "client":
            row = {k: row.get(k) for k in ("id", "business_name", "business_description", "status")}
        return {"partner": row}

    def _tool_get_application(self, args):
        aid = args.get("application_id")
        if aid is None:
            raise DataToolError("application_id_required")
        return {"application": data_core.get_application(int(aid))}

    def _tool_get_documents(self, args):
        aid = args.get("application_id")
        partner_id = args.get("partner_id")
        if aid is None and partner_id is None:
            raise DataToolError("application_or_partner_required")
        if aid is not None:
            app = data_core.get_application(int(aid))
            partner_id = app.get("partner_id") if app else None
        if not partner_id:
            return {"documents": []}
        if self.role == "partner":
            data_core.assert_partner_owns_partner(int(partner_id), int(self.actor_id or 0))
        return {"documents": data_core.get_documents(partner_id=int(partner_id), limit=50)}

    def _tool_get_addresses(self, args):
        partner_id = args.get("partner_id")
        if partner_id is None:
            if self.role == "partner" and self.actor_id is not None:
                partner = data_core.get_partner_by_user(self.actor_id)
                partner_id = partner.get("id") if partner else None
            if partner_id is None:
                raise DataToolError("partner_id_required")
        actor = self.actor_id if self.role == "partner" else None
        return {"items": data_core.get_partner_addresses(int(partner_id), actor_user_id=actor)}

    def _tool_get_directions(self, args):
        rows = data_core.active_directions()
        return {"items": rows, "count": len(rows)}

    def _tool_search_catalog(self, args):
        rows = data_core.search_catalog(
            query=str(args.get("query") or "").strip(),
            master_category_id=int(args["master_category_id"]) if args.get("master_category_id") is not None else None,
            limit=min(max(int(args.get("limit") or 100), 1), 500),
        )
        return {"items": rows, "count": len(rows)}

    def _tool_get_services(self, args):
        partner_id = args.get("partner_id")
        if self.role == "partner":
            if self.actor_id is None:
                raise DataToolError("actor_required")
            if partner_id is None:
                partner = data_core.get_partner_by_user(self.actor_id)
                partner_id = partner.get("id") if partner else None
            if partner_id is None:
                raise DataToolError("partner_not_found")
            actor = self.actor_id
        else:
            actor = None

        if self.role == "client":
            rows = data_core.search_services(
                partner_id=int(partner_id) if partner_id is not None else None,
                category_id=int(args["category_id"]) if args.get("category_id") is not None else None,
                city=str(args.get("city") or "").strip(),
                max_price=float(args["max_price"]) if args.get("max_price") is not None else None,
                limit=min(max(int(args.get("limit") or 100), 1), 200),
            )
        else:
            rows = data_core.list_services(
                partner_id=int(partner_id) if partner_id is not None else None,
                category_id=int(args["category_id"]) if args.get("category_id") is not None else None,
                actor_user_id=actor,
                limit=min(max(int(args.get("limit") or 100), 1), 200),
            )
        return {"items": rows, "count": len(rows)}

    def _tool_get_orders(self, args):
        raise DataToolError("orders_tool_pending_schema_mapping")

    def _tool_check_application(self, args):
        aid = args.get("application_id")
        if aid is None:
            raise DataToolError("application_id_required")
        return data_core.check_application(int(aid))

    def _tool_check_catalog_match(self, args):
        service = str(args.get("service_name") or "").strip()
        if not service:
            raise DataToolError("service_name_required")

        aliases = {
            "eyebrows": {"брови","бровь","бровей","հոնք","հոնքեր","հոնքերի","eyebrow","eyebrows"},
            "manicure": {"маникюр","մատնահարդարում","manicure"},
            "pedicure": {"педикюр","պեդիկյուր","pedicure"},
            "makeup": {"макияж","визаж","դիմահարդարում","makeup"},
            "haircut": {"стрижка","стрижку","стрижки","վարսավիր","սանրվածք","haircut"},
            "hair": {"волосы","волос","մազ","մազեր","hair"},
            "coloring": {"окрашивание","окраска","окрасить","ներկում","ներկել","coloring","colouring"},
        }
        def norm(v): return " ".join(str(v or "").casefold().strip().split())
        def tokens(v): return set(re.findall(r"[a-zа-яёևա-ֆ0-9-]+", norm(v)))
        raw=tokens(service); concepts=set(raw)
        for key, vals in aliases.items():
            if raw.intersection(vals) or key in raw:
                concepts.add(key); concepts.update(vals)

        rows=self._tool_search_catalog({}).get("items", [])
        scored=[]
        for row in rows:
            names=[norm(row.get(k)) for k in ("name_am","name_ru","name_en") if row.get(k)]
            if not names: continue
            rt=tokens(" ".join(names)); rc=set(rt)
            for key, vals in aliases.items():
                if rt.intersection(vals) or key in rt:
                    rc.add(key); rc.update(vals)
            object_keys={"eyebrows","manicure","pedicure","makeup","haircut","hair"}
            operation_keys={"coloring"}
            obj=concepts.intersection(rc).intersection(object_keys)
            op=concepts.intersection(rc).intersection(operation_keys)
            score=0
            if norm(service) in names: score=1000
            elif any(norm(service) in x or x in norm(service) for x in names): score=700
            if obj: score=max(score,650+min(len(obj),3)*80)
            if op: score=max(score,320+min(len(op),2)*25)
            overlap=concepts.intersection(rc)
            if overlap: score=max(score,400+min(len(overlap),5)*20)
            raw_overlap=raw.intersection(rt)
            if raw_overlap: score=max(score,260+min(len(raw_overlap),5)*20)
            roots=sum(1 for token in raw if len(token)>=4 and any(token in ct or ct in token for ct in rt if len(ct)>=4))
            if roots: score=max(score,150+min(roots,5)*15)
            if score: scored.append((score,row,obj,op))
        scored.sort(key=lambda x:(x[0],len(x[2]),-len(x[3]),str(x[1].get("name_am") or x[1].get("name_ru") or "").casefold()),reverse=True)
        candidates=[]
        for score,row,obj,op in scored[:8]:
            item=dict(row)
            item["object_matches"]=sorted(obj)
            item["operation_matches"]=sorted(op)
            item["match_reason"]="object_match" if obj else ("operation_match" if op else "text_match")
            candidates.append(item)
        return {
            "service_name":service,
            "candidates":candidates,
            "count":len(candidates),
            "top_score":scored[0][0] if scored else 0,
            "top_object_matches":sorted(scored[0][2]) if scored else [],
            "top_operation_matches":sorted(scored[0][3]) if scored else [],
        }
