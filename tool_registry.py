"""Typed tool registry for the unified AI operator.

The LLM can only request tools. ToolRegistry is the security boundary:
READ tools may return verified data; ACTION_CONFIRM tools only prepare an
action. Actual mutations are executed later by execute_confirmed(), after the
user confirms and backend ownership/state validation runs again. Stateful admin
continuations are handled by AIManager before Groq is called.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import data_core
from prompt_factory import ContextType, as_context_type


class ToolType(Enum):
    READ = "read"
    AUTO_COMMIT = "auto_commit"
    ACTION_CONFIRM = "action_requires_confirmation"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    tool_type: ToolType
    contexts: tuple[ContextType, ...]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


def _nullable(kind: str) -> dict[str, Any]:
    return {"anyOf": [{"type": kind}, {"type": "null"}]}


class ToolRegistry:
    def __init__(
        self,
        *,
        telegram_id: int,
        context_type: ContextType | str,
        trusted_context: dict[str, Any] | None = None,
        session_state: dict[str, Any] | None = None,
    ):
        self.telegram_id = int(telegram_id)
        self.context_type = as_context_type(context_type)
        self.trusted_context = dict(trusted_context or {})
        self.session_state = dict(session_state or {})

    def _admin_allowed(self) -> bool:
        return data_core.is_admin(self.telegram_id)

    def _partner_id(self) -> int:
        partner = data_core.get_partner_by_user(self.telegram_id)
        if not partner:
            raise PermissionError("partner_not_found")
        return int(partner["id"])

    @staticmethod
    def _spec(
        name: str,
        description: str,
        properties: dict[str, Any],
        *,
        required: tuple[str, ...] = (),
        tool_type: ToolType = ToolType.READ,
        contexts: tuple[ContextType, ...],
    ) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=description,
            properties=properties,
            required=required,
            tool_type=tool_type,
            contexts=contexts,
        )

    def _all_specs(self) -> list[ToolSpec]:
        c = (ContextType.CLIENT,)
        p = (ContextType.PARTNER,)
        a = (ContextType.ADMIN,)
        r = (ContextType.REGISTRATION,)
        return [
            self._spec(
                "catalog_candidates",
                "Return real live catalog candidates for the supplied service meanings. Use this before registration save when mapping services. The backend returns existing catalog slugs and names; the model must choose only from returned slugs and must never invent slugs or IDs.",
                {
                    "services": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "limit_per_service": {"type": "integer"},
                },
                required=("services",),
                contexts=(ContextType.REGISTRATION, ContextType.PARTNER, ContextType.ADMIN),
            ),
            self._spec(
                "save_completed_application",
                "Save the completed partner registration after the model has collected company name, Armenian city, phone and at least one service. The model may supply only catalog_slug values returned by catalog_candidates; the backend resolves those slugs to live category IDs and their parent master category. This is the ONLY registration write that the AI may execute automatically; backend validates the authenticated Telegram user. Never call it with invented data and never invent category IDs.",
                {
                    "company_name": {"type": "string"},
                    "marz": _nullable("string"),
                    "city": {"type": "string"},
                    "address": _nullable("string"),
                    "phone": {"type": "string"},
                    "working_hours": _nullable("string"),
                    "description": _nullable("string"),
                    "services": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "catalog_slug": _nullable("string"),
                                "price": _nullable("number"),
                                "price_type": {"type": "string", "enum": ["from", "fixed"]},
                            },
                            "required": ["name", "price", "price_type"],
                            "additionalProperties": False,
                        },
                    },
                },
                required=("company_name", "city", "phone", "services"),
                tool_type=ToolType.AUTO_COMMIT,
                contexts=r,
            ),
            self._spec(
                "resolve_current_entity",
                "Resolve a pronoun/reference such as 'it' only from backend session state. If there is no unique current entity, return a clarification question. Never guess.",
                {
                    "entity_type": {
                        "type": "string",
                        "enum": ["order", "service", "company", "address", "application"],
                    },
                },
                required=("entity_type",),
                contexts=(ContextType.CLIENT, ContextType.PARTNER, ContextType.ADMIN),
            ),
            self._spec(
                "get_next_item",
                "Show the next item from the backend-owned current list. If there is no next item, say so. Never guess an item outside the stored list.",
                {},
                contexts=(ContextType.CLIENT, ContextType.PARTNER, ContextType.ADMIN),
            ),
            self._spec(
                "search_services",
                "Search real approved marketplace services. Never invent results.",
                {
                    "query": {"type": "string"},
                    "city": {"type": "string"},
                    "category_id": _nullable("integer"),
                    "max_price": _nullable("number"),
                    "limit": {"type": "integer"},
                },
                contexts=(ContextType.CLIENT, ContextType.ADMIN),
            ),
            self._spec(
                "get_my_orders",
                "List orders visible to the authenticated client or partner.",
                {"status": _nullable("string"), "limit": {"type": "integer"}},                contexts=(ContextType.CLIENT, ContextType.PARTNER),
            ),
            self._spec(
                "get_my_order",
                "Get one order visible to the authenticated actor. Backend checks ownership.",
                {"order_id": {"type": "integer"}},
                required=("order_id",),
                contexts=(ContextType.CLIENT, ContextType.PARTNER),
            ),
            self._spec(
                "cancel_order",
                "Prepare cancellation of one owned order. Never execute without explicit confirmation.",
                {
                    "order_id": {"type": "integer"},
                    "reason": _nullable("string"),
                },
                required=("order_id",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=(ContextType.CLIENT, ContextType.PARTNER),
            ),
            self._spec(
                "get_my_companies",
                "List the authenticated partner's active companies.",
                {},
                contexts=p,
            ),
            self._spec(
                "get_my_services",
                "List the authenticated partner's services, optionally filtered by company.",
                {"company_id": _nullable("integer"), "limit": {"type": "integer"}},
                contexts=p,
            ),
            self._spec(
                "get_my_addresses",
                "List the authenticated partner's addresses/objects.",
                {"limit": {"type": "integer"}},
                contexts=p,
            ),
            self._spec(
                "add_address",
                "Prepare adding an address/object owned by the authenticated partner.",
                {
                    "company_id": _nullable("integer"),
                    "address": {"type": "string"},
                    "city": _nullable("string"),
                    "marz": _nullable("string"),
                    "phone": _nullable("string"),
                    "object_name": _nullable("string"),
                },
                required=("address",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=p,
            ),
            self._spec(
                "update_address",
                "Prepare changing an owned partner address/object.",
                {
                    "address_id": {"type": "integer"},
                    "address": _nullable("string"),
                    "city": _nullable("string"),
                    "marz": _nullable("string"),
                    "phone": _nullable("string"),
                    "object_name": _nullable("string"),
                },
                required=("address_id",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=p,
            ),
            self._spec(
                "add_service",
                "Prepare adding a service to an owned company. Confirmation required.",
                {
                    "company_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "price": _nullable("number"),
                    "category_id": _nullable("integer"),
                    "address_id": _nullable("integer"),
                    "phone": _nullable("string"),
                    "description": _nullable("string"),
                },
                required=("company_id", "name"),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=p,
            ),
            self._spec(
                "add_services",
                "Prepare adding multiple services to one owned company as ONE confirmed action. Copy every service name from the user's message without translating, inventing, shortening or rewriting it. Use one item per distinct service. Confirmation is required once for the whole batch.",
                {
                    "company_id": _nullable("integer"),
                    "services": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "price": _nullable("number"),
                                "price_type": {"type": "string", "enum": ["from", "fixed"]},
                                "address_id": _nullable("integer"),
                                "phone": _nullable("string"),
                                "description": _nullable("string")
                            },
                            "required": ["name", "price"],
                            "additionalProperties": False
                        }
                    }
                },
                required=("services",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=p,
            ),
            self._spec(
                "update_service",
                "Prepare changing an owned service name, price or category.",
                {
                    "service_id": {"type": "integer"},
                    "name": _nullable("string"),
                    "price": _nullable("number"),
                    "category_id": _nullable("integer"),
                },
                required=("service_id",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=p,
            ),
            self._spec(
                "add_company",
                "Prepare creating a new partner company.",
                {
                    "name": {"type": "string"},
                    "description": _nullable("string"),
                    "phone": _nullable("string"),
                },
                required=("name",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=p,
            ),
            self._spec(
                "update_company",
                "Prepare changing an owned company.",
                {
                    "company_id": {"type": "integer"},
                    "name": _nullable("string"),
                    "description": _nullable("string"),
                    "phone": _nullable("string"),
                },
                required=("company_id",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=p,
            ),
            self._spec(
                "archive_company",
                "Prepare archiving an owned company.",
                {"company_id": {"type": "integer"}},
                required=("company_id",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=p,
            ),
            self._spec(
                "admin_preview_application_service_price",
                "Validate an application service price change and prepare an explicit confirmation action.",
                {
                    "application_id": {"type": "integer"},
                    "service_index": {"type": "integer"},
                    "price": {"type": "number"},
                },
                required=("application_id", "service_index", "price"),
                contexts=a,
            ),
            self._spec(
                "admin_apply_application_service_price",
                "Final application service price write. Executable only through explicit confirmation.",
                {
                    "application_id": {"type": "integer"},
                    "service_index": {"type": "integer"},
                    "price": {"type": "number"},
                    "confirmation_token": {"type": "string"},
                },
                required=("application_id", "service_index", "price", "confirmation_token"),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=a,
            ),
            self._spec(
                "bulk_resolve_catalog_categories",
                "Resolve ALL services of an application against the current live catalog. The backend independently matches every service and returns a single pending_action draft; it never accepts model-generated category IDs or uses bulk state.",
                {
                    "application_id": {"type": "integer"},
                    "resolve_all": {"type": "boolean"},
                },
                required=("application_id", "resolve_all"),
                tool_type=ToolType.READ,
                contexts=a,
            ),
            self._spec(
                "admin_catalog_candidates",
                "Return live catalogue candidates for each service in a real application. This is a READ-only candidate list: choose only an exact canonical catalogue name returned here. Never invent category names or IDs.",
                {
                    "application_id": {"type": "integer"},
                    "service_indexes": {"type": "array", "items": {"type": "integer"}},
                    "limit_per_service": {"type": "integer"},
                },
                required=("application_id",),
                contexts=a,
            ),
            self._spec(
                "admin_preview_catalog_resolution",
                "Validate proposed catalogue mappings for an application and prepare a confirmation-only action. The model supplies service_index plus an exact catalog_name previously returned by admin_catalog_candidates. The backend resolves the real category IDs.",
                {
                    "application_id": {"type": "integer"},                    "mappings": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "service_index": {"type": "integer"},
                                "catalog_name": {"type": "string"},
                            },
                            "required": ["service_index", "catalog_name"],
                            "additionalProperties": False,
                        },
                    },
                },
                required=("application_id", "mappings"),
                contexts=a,
            ),
            self._spec(
                "admin_apply_catalog_resolution",
                "Final catalogue mapping write for an application. This tool is executable only through the existing explicit confirmation flow.",
                {
                    "application_id": {"type": "integer"},
                    "confirmation_token": {"type": "string"},
                    "mappings": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "service_index": {"type": "integer"},
                                "category_id": {"type": "integer"},
                                "master_category_id": {"type": "integer"},
                            },
                            "required": ["service_index", "category_id", "master_category_id"],
                            "additionalProperties": False,
                        },
                    },
                },
                required=("application_id", "confirmation_token", "mappings"),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=a,
            ),
            self._spec(
                "admin_get_pending_applications",
                "List partner applications currently awaiting administrative moderation.",
                {"limit": {"type": "integer"}},
                contexts=a,
            ),
            self._spec(
                "admin_get_partner_profile",
                "Read a partner profile by partner ID.",
                {"partner_id": {"type": "integer"}},
                required=("partner_id",),
                contexts=a,
            ),
            self._spec(
                "admin_view_audit_logs",
                "Read system audit logs from the canonical backend audit provider.",
                {"limit": {"type": "integer"}},
                contexts=a,
            ),
            self._spec(
                "admin_approve_application",
                "Prepare approval of a partner application. Explicit confirmation required.",
                {"application_id": {"type": "integer"}},
                required=("application_id",),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=a,
            ),
            self._spec(
                "admin_reject_application",
                "Prepare rejection of a partner application. Non-empty reason required.",
                {"application_id": {"type": "integer"}, "reason": {"type": "string"}},
                required=("application_id", "reason"),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=a,
            ),
            self._spec(
                "admin_request_document_correction",
                "Prepare a request to the partner to replace/resubmit the verification document for an application. Explicit confirmation required.",
                {"application_id": {"type": "integer"}, "reason": {"type": "string"}},
                required=("application_id", "reason"),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=a,
            ),
            self._spec(
                "admin_suspend_partner",
                "Prepare freezing a partner account. Non-empty reason required.",
                {"partner_id": {"type": "integer"}, "reason": {"type": "string"}},
                required=("partner_id", "reason"),
                tool_type=ToolType.ACTION_CONFIRM,
                contexts=a,
            ),
            self._spec(
                "count_records",
                "Return an exact backend count for administrative entities.",
                {
                    "entity": {
                        "type": "string",
                        "enum": ["applications", "partners", "services", "companies"],
                    },
                    "status": _nullable("string"),
                    "marz": _nullable("string"),
                    "city": _nullable("string"),
                },
                required=("entity",),
                contexts=a,
            ),
            self._spec(
                "admin_query",
                "Read verified administrative data. Never mutate data.",
                {
                    "entity": {
                        "type": "string",
                        "enum": ["applications", "partners", "services", "companies"],
                    },
                    "query": {"type": "string"},
                    "status": _nullable("string"),
                    "marz": _nullable("string"),
                    "city": _nullable("string"),
                    "max_price": _nullable("number"),
                    "limit": {"type": "integer"},
                },
                required=("entity",),
                contexts=a,
            ),
            self._spec(
                "get_application",
                "Get one complete real application.",
                {"application_id": {"type": "integer"}},
                required=("application_id",),
                contexts=a,
            ),
            self._spec(
                "check_application",
                "Run backend checks for a real application.",
                {"application_id": {"type": "integer"}},
                required=("application_id",),
                contexts=a,
            ),
            self._spec(
                "search_applications",
                "Search real partner applications.",
                {
                    "status": _nullable("string"),
                    "marz": _nullable("string"),
                    "city": _nullable("string"),
                    "limit": {"type": "integer"},
                },
                contexts=a,
            ),
            self._spec(
                "search_partners",
                "Search real partners.",
                {
                    "query": {"type": "string"},
                    "city": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                contexts=a,
            ),
        ]

    def _visible_specs(self) -> list[ToolSpec]:
        if self.context_type == ContextType.ADMIN and not self._admin_allowed():
            return []
        return [s for s in self._all_specs() if self.context_type in s.contexts]

    def definitions(self) -> list[dict[str, Any]]:
        return [s.schema() for s in self._visible_specs()]

    def spec(self, name: str) -> ToolSpec | None:
        return next((s for s in self._visible_specs() if s.name == name), None)

    def _save_completed_application(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate registration data and resolve live catalog slugs in Python."""
        if self.context_type != ContextType.REGISTRATION:
            raise PermissionError("registration_only")
        company_name = str(args.get("company_name") or "").strip()
        city = str(args.get("city") or "").strip()
        phone = str(args.get("phone") or "").strip()
        services = args.get("services") or []
        if not company_name or not city or not phone or not isinstance(services, list) or not services:
            raise ValueError("registration_required_fields_missing")

        catalog = data_core.search_catalog(limit=500)
        by_slug = {
            str(cat.get("slug") or "").strip(): cat
            for cat in catalog
            if str(cat.get("slug") or "").strip()
        }

        normalized_services = []
        for raw in services:
            if not isinstance(raw, dict):
                raise ValueError("invalid_service_payload")
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ValueError("service_name_required")
            catalog_slug = str(raw.get("catalog_slug") or "").strip()
            if not catalog_slug:
                raise ValueError("catalog_subcategory_required")
            cat = by_slug.get(catalog_slug)
            if not cat or not cat.get("id") or not cat.get("master_category_id"):
                raise ValueError("catalog_subcategory_not_in_live_catalog")
            price = raw.get("price")
            if price is not None:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    raise ValueError("invalid_service_price")
                if price < 0:
                    raise ValueError("invalid_service_price")
            price_type = str(raw.get("price_type") or "fixed").strip().lower()
            if price_type not in ("from", "fixed"):
                raise ValueError("invalid_price_type")
            normalized_services.append({
                "name": name,
                "price": price,
                "price_type": price_type,
                "catalog_slug": str(cat.get("slug") or ""),
                "catalog_name": cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"),
                "subcategory_name": cat.get("name_am"),
                "matched_subcategory_id": int(cat["id"]),
                "subcategory_id": int(cat["id"]),
                "category_id": int(cat["id"]),
                "master_category_id": int(cat["master_category_id"]),
                "master_name_am": cat.get("master_name_am"),
                "master_name_ru": cat.get("master_name_ru"),
                "master_name_en": cat.get("master_name_en"),
            })

        profile = {
            "business_name": company_name,
            "marz": args.get("marz"),
            "city": city,
            "address": args.get("address"),
            "phone": phone,
            "working_hours": args.get("working_hours"),
            "description": args.get("description"),
            "services": normalized_services,
        }
        result = data_core.save_partner_application_draft(
            user_id=self.telegram_id,
            profile=profile,
        )
        return {"ok": True, "draft": result, "catalog_resolved": True}

    def _prepare_action(self, name: str, args: dict[str, Any], summary: str) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "awaiting_user_confirmation",
            "requires_confirmation": True,
            "action": {"name": name, "args": dict(args)},
            "summary": summary,
        }
    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = dict(args or {})
        spec = self.spec(name)
        if not spec:
            raise PermissionError("tool_not_allowed")

        if spec.tool_type == ToolType.AUTO_COMMIT:
            if name == "save_completed_application":
                return self._save_completed_application(args)
            raise PermissionError("auto_commit_not_implemented")

        if spec.tool_type == ToolType.ACTION_CONFIRM:
            return self._prepare_action_checked(name, args)

        if name == "resolve_current_entity":
            return self.resolve_reference(str(args["entity_type"]).strip().lower())

        if name == "get_next_item":
            items = self.session_state.get("current_list") or []
            try:
                index = int(self.session_state.get("current_pagination_index") or 0)
            except (TypeError, ValueError):
                index = 0
            next_index = index + 1
            if next_index >= len(items):
                return {"ok": True, "has_next": False, "message": "no_next_item"}
            item = items[next_index]
            return {
                "ok": True,
                "has_next": True,
                "item": item,
                "pagination": {"index": next_index, "total": len(items)},
                "display_entity": {
                    "id": item.get("id") if isinstance(item, dict) else None,
                    "type": self.session_state.get("current_entity_type") or "entity",
                },
            }

        if name == "catalog_candidates":
            services = [str(x or "").strip() for x in (args.get("services") or []) if str(x or "").strip()]
            per_service = max(1, min(int(args.get("limit_per_service") or 5), 8))
            catalog = data_core.search_catalog(limit=500)
            result = []
            for service in services[:30]:
                ranked = []
                for cat in catalog:
                    names = [cat.get("name_am"), cat.get("name_ru"), cat.get("name_en"), cat.get("slug")]
                    score = max((data_core._catalog_match_score(service, n) for n in names if n), default=0.0)
                    ranked.append((score, cat))
                ranked.sort(key=lambda x: x[0], reverse=True)
                result.append({
                    "service": service,
                    "candidates": [{
                        "catalog_slug": cat.get("slug"),
                        "catalog_name": cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"),
                        "name_am": cat.get("name_am"),
                        "name_ru": cat.get("name_ru"),
                        "name_en": cat.get("name_en"),
                        "master_name_am": cat.get("master_name_am"),
                        "master_name_ru": cat.get("master_name_ru"),
                        "master_name_en": cat.get("master_name_en"),
                        "score": round(float(sc), 4),
                    } for sc, cat in ranked[:per_service]]
                })
            return {"ok": True, "items": result}

        if name == "search_services":
            return {"ok": True, "items": data_core.search_services(
                category_id=args.get("category_id"),
                city=str(args.get("city") or ""),
                max_price=args.get("max_price"),
                limit=max(1, min(int(args.get("limit") or 20), 100)),
            )}

        if name == "get_my_orders":
            return {"ok": True, "items": data_core.search_orders(
                actor_role=self.context_type.value.lower(),
                actor_id=self.telegram_id,
                status=args.get("status"),
                limit=max(1, min(int(args.get("limit") or 20), 50)),
            )}

        if name == "get_my_order":
            item = data_core.get_order(
                int(args["order_id"]),
                actor_role=self.context_type.value.lower(),
                actor_id=self.telegram_id,
            )
            if not item:
                raise PermissionError("order_not_owned_or_not_found")
            return {"ok": True, "item": item}

        if self.context_type == ContextType.PARTNER:
            pid = self._partner_id()
            if name == "get_my_companies":
                return {"ok": True, "items": data_core.list_partner_companies(
                    partner_id=pid, actor_user_id=self.telegram_id
                )}
            if name == "get_my_services":
                company_id = args.get("company_id")
                if company_id is not None:
                    company = data_core.get_company(int(company_id))
                    if not company or int(company.get("partner_id") or 0) != pid:
                        raise PermissionError("company_not_owned")
                services = data_core.list_services(
                    partner_id=pid, actor_user_id=self.telegram_id, limit=200
                )
                if company_id is not None:
                    services = [
                        x for x in services
                        if int(x.get("business_id") or 0) == int(company_id)
                    ]
                return {"ok": True, "items": services}
            if name == "get_my_addresses":
                return {"ok": True, "items": data_core.get_partner_addresses(
                    pid, actor_user_id=self.telegram_id, limit=200
                )}

        if self.context_type == ContextType.ADMIN:
            if name == "bulk_resolve_catalog_categories":
                application_id = int(args["application_id"])
                if not bool(args.get("resolve_all")):
                    raise ValueError("bulk_resolution_requires_resolve_all")
                return data_core.init_bulk_catalog_resolution(
                    application_id=application_id,
                    actor_user_id=self.telegram_id,
                )
            if name == "admin_preview_application_service_price":
                return data_core.prepare_application_service_price_update(
                    application_id=int(args["application_id"]),
                    service_index=int(args["service_index"]),
                    price=float(args["price"]),
                    actor_user_id=self.telegram_id,
                )

            if name == "admin_catalog_candidates":
                application_id = int(args["application_id"])
                service_indexes = args.get("service_indexes")
                limit_per_service = max(3, min(int(args.get("limit_per_service") or 10), 15))
                return {"ok": True, **data_core.admin_catalog_candidates(
                    application_id=application_id,
                    service_indexes=service_indexes,
                    limit_per_service=limit_per_service,
                    actor_user_id=self.telegram_id,
                )}

            if name == "admin_preview_catalog_resolution":
                application_id = int(args["application_id"])
                mappings = args.get("mappings") or []
                return data_core.prepare_catalog_resolution(
                    application_id=application_id,
                    mappings=mappings,
                    actor_user_id=self.telegram_id,
                )

            if name == "admin_get_pending_applications":
                return {"ok": True, "items": data_core.admin_get_pending_applications(
                    limit=max(1, min(int(args.get("limit") or 50), 200))
                )}
            if name == "admin_get_partner_profile":
                item = data_core.admin_get_partner_profile(int(args["partner_id"]))
                return {"ok": bool(item), "item": item}
            if name == "admin_view_audit_logs":
                try:
                    return {"ok": True, "items": data_core.admin_view_audit_logs(
                        limit=max(1, min(int(args.get("limit") or 50), 200))
                    )}
                except NotImplementedError as exc:
                    return {"ok": False, "error": str(exc), "backend_capability_missing": True}
            if name == "count_records":
                return {"ok": True, "entity": str(args["entity"]),
                        "count": data_core.count_entities(
                            entity=str(args["entity"]),
                            status=args.get("status"),
                            marz=args.get("marz"),
                            city=args.get("city"),
                        )}
            if name == "admin_query":
                entity = str(args["entity"]).lower()
                query = str(args.get("query") or "").strip()
                limit = max(1, min(int(args.get("limit") or 30), 100))
                if entity == "applications":
                    items = data_core.search_applications(
                        status=args.get("status"), marz=args.get("marz"),
                        city=args.get("city"), limit=limit,
                    )
                    if query:
                        q = query.casefold()
                        items = [x for x in items if
                                 q in str(x.get("business_name") or "").casefold()
                                 or q in str(x.get("service_name") or "").casefold()
                                 or q in str(x.get("description") or "").casefold()]
                    return {"ok": True, "entity": entity, "items": items}
                if entity == "partners":
                    return {"ok": True, "entity": entity,
                            "items": data_core.search_partners(
                                query=query, city=str(args.get("city") or ""),
                                limit=limit,
                            )}
                if entity == "services":
                    return {"ok": True, "entity": entity,
                            "items": data_core.search_services(
                                city=str(args.get("city") or ""),
                                max_price=args.get("max_price"), limit=limit,
                            )}
                if entity == "companies":
                    partner_rows = data_core.search_partners(
                        query=query, city=str(args.get("city") or ""), limit=limit
                    )
                    rows: list[dict[str, Any]] = []
                    for row in partner_rows:
                        if row.get("id") is not None:
                            rows.extend(data_core.list_companies(
                                int(row["id"]), include_archived=False
                            ))
                    return {"ok": True, "entity": entity, "items": rows[:limit]}
                raise ValueError("unsupported_admin_entity")

            if name == "search_applications":
                return {"ok": True, "items": data_core.search_applications(
                    status=args.get("status"), marz=args.get("marz"),
                    city=args.get("city"),
                    limit=max(1, min(int(args.get("limit") or 50), 200)),
                )}
            if name == "get_application":
                item = data_core.get_application_full(int(args["application_id"]))
                if not item:
                    return {"ok": False, "error": "application_not_found"}
                item = dict(item)
                item["service_items"] = data_core.application_service_items(int(args["application_id"]))
                return {"ok": True, "item": item}
            if name == "check_application":
                return {"ok": True, "item": data_core.check_application(int(args["application_id"]))}
            if name == "search_partners":
                return {"ok": True, "items": data_core.search_partners(
                    query=str(args.get("query") or ""),
                    city=str(args.get("city") or ""),
                    limit=max(1, min(int(args.get("limit") or 20), 100)),
                )}

        raise PermissionError("tool_not_implemented")

    def _prepare_action_checked(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.context_type == ContextType.ADMIN:
            if not self._admin_allowed():
                raise PermissionError("admin_required")
            if name == "admin_preview_application_service_price":
                application_id = int(args["application_id"])
                service_index = int(args["service_index"])
                price = float(args["price"])
                return data_core.prepare_application_service_price_update(
                    application_id=application_id,
                    service_index=service_index,
                    price=price,
                    actor_user_id=self.telegram_id,
                )
            if name == "admin_approve_application":
                application_id = int(args["application_id"])
                gate = data_core.prepare_application_approval(
                    application_id=application_id,
                    actor_user_id=self.telegram_id,
                )
                if not gate.get("can_approve"):
                    return gate
                return gate
            if name == "admin_reject_application":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    return {"ok": False, "needs_clarification": True,
                            "question": "Укажите причину отклонения заявки."}
                application_id = int(args["application_id"])
                if not data_core.get_application_full(application_id):
                    raise ValueError("application_not_found")
                return self._prepare_action(
                    name, {"application_id": application_id, "reason": reason[:3000]},
                    f"Отклонить заявку #{application_id} с причиной «{reason[:300]}»?")
            if name == "admin_request_document_correction":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    return {"ok": False, "needs_clarification": True,
                            "question": "Նշեք փաստաթուղթը ճշտման վերադարձնելու պատճառը։"}
                application_id = int(args["application_id"])
                app = data_core.get_application_full(application_id)
                if not app:
                    raise ValueError("application_not_found")
                if str(app.get("status") or "") in ("approved", "rejected", "cancelled"):
                    return {"ok": False, "message": "Այս հայտը արդեն վերջնական կարգավիճակում է։"}
                return self._prepare_action(
                    name,
                    {"application_id": application_id, "reason": reason[:3000]},
                    f"Գործընկերոջը խնդրել նոր փաստաթուղթ ուղարկել հայտ #{application_id}-ի համար՝ «{reason[:300]}»?"
                )
            if name == "admin_suspend_partner":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    return {"ok": False, "needs_clarification": True,
                            "question": "Укажите причину блокировки партнёра."}
                partner_id = int(args["partner_id"])
                if not data_core.admin_get_partner_profile(partner_id):
                    raise ValueError("partner_not_found")
                return self._prepare_action(
                    name, {"partner_id": partner_id, "reason": reason[:3000]},
                    f"Заморозить партнёра #{partner_id} с причиной «{reason[:300]}»?")
            raise PermissionError("admin_action_not_implemented")

        if self.context_type not in (ContextType.CLIENT, ContextType.PARTNER):
            raise PermissionError("action_not_allowed")

        if name == "cancel_order":
            order_id = int(args["order_id"])
            order = data_core.get_order(
                order_id,
                actor_role=self.context_type.value.lower(),
                actor_id=self.telegram_id,
            )
            if not order:
                raise PermissionError("order_not_owned_or_not_found")
            status = str(order.get("status") or "").lower()
            if status in {"cancelled", "refunded", "completed"}:
                raise ValueError("order_not_cancellable")
            reason = str(args.get("reason") or "").strip()
            return self._prepare_action(
                name,
                {"order_id": order_id, "reason": reason},
                f"Отменить заказ #{order_id} «{order.get('service_name') or order.get('name') or ''}»?",
            )

        if self.context_type == ContextType.PARTNER:
            pid = self._partner_id()

            if name == "add_address":
                address = str(args.get("address") or "").strip()
                if not address:
                    raise ValueError("address_required")
                company_id = args.get("company_id")
                if company_id is not None:
                    company = data_core.get_company(int(company_id))
                    if not company or int(company.get("partner_id") or 0) != pid:
                        raise PermissionError("company_not_owned")
                return self._prepare_action(
                    name,
                    {
                        "company_id": int(company_id) if company_id is not None else None,
                        "address": address,
                        "city": args.get("city"),
                        "marz": args.get("marz"),
                        "phone": args.get("phone"),
                        "object_name": args.get("object_name"),
                    },
                    f"Добавить адрес «{address}»?",
                )

            if name == "update_address":
                address_id = int(args["address_id"])
                visible = data_core.get_partner_addresses(
                    pid, actor_user_id=self.telegram_id, limit=200
                )
                if not any(int(x.get("id") or 0) == address_id for x in visible):                    raise PermissionError("address_not_owned")
                changes = {k: args.get(k) for k in
                           ("address", "city", "marz", "phone", "object_name")
                           if args.get(k) is not None}
                if not changes:
                    raise ValueError("no_changes")
                return self._prepare_action(
                    name, {"address_id": address_id, **changes},
                    f"Изменить адрес #{address_id}?",
                )

            if name == "add_service":
                raw_company_id = args.get("company_id")
                if raw_company_id in (None, "", 0, "0"):
                    raw_company_id = self.trusted_context.get("current_company_id")
                if raw_company_id in (None, "", 0, "0"):
                    current = self.trusted_context.get("current_company") or {}
                    raw_company_id = current.get("id") if isinstance(current, dict) else None
                if raw_company_id in (None, "", 0, "0"):
                    raise ValueError("company_context_required")
                company_id = int(raw_company_id)
                company = data_core.get_company(company_id)
                if not company or int(company.get("partner_id") or 0) != pid:
                    raise PermissionError("company_not_owned")
                checked = data_core.validate_service_payload(
                    partner_id=pid, actor_user_id=self.telegram_id,
                    company_id=company_id, name=args["name"],
                    price=args.get("price"), category_id=args.get("category_id"),
                )
                if args.get("address_id") in (None, ""):
                    company_addresses = [
                        x for x in data_core.get_partner_addresses(
                            pid, actor_user_id=self.telegram_id, limit=200
                        )
                        if int(x.get("business_id") or 0) == company_id
                        and str(x.get("address") or "").strip()
                    ]
                    if not company_addresses:
                        raise ValueError("service_address_required")
                document = data_core.get_current_partner_document(
                    partner_id=pid, company_id=company_id
                )
                if not document:
                    raise ValueError("document_required")
                resolved = data_core.resolve_catalog_services([{
                    "name": checked["name"],
                    "price": checked["price"],
                    "price_type": args.get("price_type") or "fixed",
                    "address_id": args.get("address_id"),
                    "phone": args.get("phone"),
                    "description": args.get("description"),
                }], limit=500)[0]
                return self._prepare_action(
                    name,
                    {
                        "company_id": company_id,
                        "name": resolved["name"],
                        "price": resolved.get("price"),
                        "price_type": resolved.get("price_type") or "fixed",
                        "category_id": resolved.get("category_id"),
                        "master_category_id": resolved.get("master_category_id"),
                        "address_id": resolved.get("address_id"),
                        "phone": resolved.get("phone"),
                        "description": resolved.get("description"),
                        "catalog_match_status": resolved.get("catalog_match_status"),
                        "catalog_options": resolved.get("catalog_options") or [],
                        "document_id": int(document["id"]),
                        "document_status": document.get("status"),
                    },
                    f'Добавить услугу «{checked["name"]}» в заявку на проверку компании «{company.get("name") or ""}»?',
                )

            if name == "add_services":
                raw_company_id = args.get("company_id")
                if raw_company_id in (None, "", 0, "0"):
                    raw_company_id = self.trusted_context.get("current_company_id")
                if raw_company_id in (None, "", 0, "0"):
                    current = self.trusted_context.get("current_company") or {}
                    raw_company_id = current.get("id") if isinstance(current, dict) else None
                if raw_company_id in (None, "", 0, "0"):
                    raise ValueError("company_context_required")
                company_id = int(raw_company_id)
                company = data_core.get_company(company_id)
                if not company or int(company.get("partner_id") or 0) != pid:
                    raise PermissionError("company_not_owned")

                raw_services = args.get("services") or []
                if not isinstance(raw_services, list) or not raw_services:
                    raise ValueError("services_required")

                addresses = data_core.get_partner_addresses(
                    pid, actor_user_id=self.telegram_id, limit=200
                )
                company_addresses = [
                    x for x in addresses
                    if int(x.get("business_id") or 0) == company_id
                    and str(x.get("address") or "").strip()
                ]

                # A pending action may contain a human-entered address or GPS
                # location that does not yet exist as a partner_object. It is
                # kept as draft data until the final confirmation.
                address_text = str(args.get("address_text") or "").strip()
                location = args.get("service_location") if isinstance(args.get("service_location"), dict) else None
                selected_address_id = args.get("address_id")
                if selected_address_id not in (None, ""):
                    selected_address_id = int(selected_address_id)
                    selected = next(
                        (x for x in company_addresses if int(x.get("id") or 0) == selected_address_id),
                        None,
                    )
                    if not selected:
                        raise PermissionError("address_not_in_company")
                elif len(company_addresses) == 1:
                    selected_address_id = int(company_addresses[0]["id"])
                    selected = company_addresses[0]
                else:
                    selected = None

                phone = data_core.normalize_phone_number(args.get("phone"))
                if not phone and selected:
                    phone = data_core.normalize_phone_number(selected.get("phone"))
                if not phone:
                    phone = data_core.normalize_phone_number(company.get("phone"))

                service_mode = str(args.get("service_mode") or "").strip().lower()
                if service_mode not in {"at_address", "mobile"}:
                    # Keep the raw natural-language value out of the DB contract.
                    service_mode = None

                document = data_core.get_current_partner_document(
                    partner_id=pid, company_id=company_id
                )

                prepared = []
                for raw in raw_services[:30]:
                    if not isinstance(raw, dict):
                        raise ValueError("invalid_service_payload")
                    service_name = str(raw.get("name") or "").strip()
                    if not service_name:
                        raise ValueError("service_name_required")
                    checked = data_core.validate_service_payload(
                        partner_id=pid, actor_user_id=self.telegram_id,
                        company_id=company_id, name=service_name,
                        price=raw.get("price"), category_id=None,
                    )
                    prepared.append({
                        "name": checked["name"],
                        "price": checked["price"],
                        "price_type": raw.get("price_type") or "from",
                        "address_id": selected_address_id,
                        "phone": phone,
                        "description": raw.get("description"),
                    })

                prepared = data_core.resolve_catalog_services(prepared, limit=500)

                missing = []
                if not selected_address_id and not address_text and not location:
                    missing.append("address")
                if not phone:
                    missing.append("phone")
                if not service_mode:
                    missing.append("service_mode")
                if not document:
                    missing.append("document")

                action_args = {
                    "company_id": company_id,
                    "company_name": company.get("name") or "",
                    "services": prepared,
                    "address_id": selected_address_id,
                    "address_text": address_text or (selected or {}).get("address"),
                    "phone": phone,
                    "service_mode": service_mode,
                    "service_location": location,
                }

                if missing:
                    return {
                        "ok": True,
                        "requires_data": True,
                        "missing_fields": missing,
                        "action": {"name": "add_services", "args": action_args},
                        "services": prepared,
                        "company": company,
                    }

                # All prerequisites are present. No DB mutation occurs here.
                # The final confirmation path will create ONE application.
                return self._prepare_action(
                    name,
                    action_args,
                    f'Подготовить заявку на добавление {len(prepared)} услуг(и) в компанию «{company.get("name") or ""}»?',
                )

            if name == "update_service":
                service = data_core.assert_partner_owns_service(
                    int(args["service_id"]), self.telegram_id
                )
                if all(args.get(k) is None for k in ("name", "price", "category_id")):
                    raise ValueError("no_changes")
                if args.get("price") is not None and float(args["price"]) < 0:
                    raise ValueError("invalid_service_price")
                if args.get("category_id") is not None:
                    cat = data_core.get_catalog_category(int(args["category_id"]))
                    if not cat or not cat.get("is_active"):
                        raise ValueError("catalog_category_invalid")
                return self._prepare_action(
                    name,
                    {
                        "service_id": int(service["id"]),
                        "name": args.get("name"),
                        "price": args.get("price"),
                        "category_id": args.get("category_id"),
                    },
                    f"Изменить услугу «{service.get('name') or ''}» (#{service['id']})?",
                )

            if name == "add_company":
                company_name = str(args.get("name") or "").strip()
                if not company_name:
                    raise ValueError("company_name_required")
                return self._prepare_action(
                    name,
                    {"name": company_name, "description": args.get("description"),
                     "phone": args.get("phone")},
                    f"Добавить компанию «{company_name}»?",
                )

            if name == "update_company":
                company = data_core.get_company(int(args["company_id"]))
                if not company or int(company.get("partner_id") or 0) != pid:
                    raise PermissionError("company_not_owned")
                if all(args.get(k) is None for k in ("name", "description", "phone")):
                    raise ValueError("no_changes")
                return self._prepare_action(
                    name,
                    {"company_id": int(company["id"]), "name": args.get("name"),
                     "description": args.get("description"), "phone": args.get("phone")},
                    f"Изменить компанию «{company.get('name') or ''}»?",
                )

            if name == "archive_company":
                company = data_core.get_company(int(args["company_id"]))
                if not company or int(company.get("partner_id") or 0) != pid:
                    raise PermissionError("company_not_owned")
                return self._prepare_action(
                    name, {"company_id": int(company["id"])},
                    f"Архивировать компанию «{company.get('name') or ''}»?",
                )

        raise PermissionError("action_not_implemented")

    async def execute_confirmed(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Final write step. Re-resolve identity and ownership immediately before DB mutation."""
        spec = self.spec(name)
        if not spec or spec.tool_type != ToolType.ACTION_CONFIRM:
            raise PermissionError("not_confirmed_action")

        if name == "cancel_order":
            result = data_core.cancel_booking(
                int(args["order_id"]),
                actor_role=self.context_type.value.lower(),
                actor_id=self.telegram_id,
                new_status="cancelled",
                reason=str(args.get("reason") or ""),
            )
            if not result:
                raise PermissionError("order_not_cancellable_or_not_owned")
            return {"ok": True, "item": result}

        if self.context_type == ContextType.ADMIN:
            if not self._admin_allowed():
                raise PermissionError("admin_required")
            if name == "admin_apply_application_service_price":
                return data_core.apply_application_service_price(
                    application_id=int(args["application_id"]),
                    service_index=int(args["service_index"]),
                    price=float(args["price"]),
                    confirmation_token=str(args.get("confirmation_token") or ""),
                    actor_user_id=self.telegram_id,
                )

            if name == "admin_apply_catalog_resolution":
                return data_core.apply_catalog_resolution(
                    application_id=int(args["application_id"]),
                    mappings=args.get("mappings") or [],
                    confirmation_token=str(args.get("confirmation_token") or ""),
                    actor_user_id=self.telegram_id,
                )

            if name == "admin_approve_application":
                result = data_core.admin_approve_application(
                    int(args["application_id"]), self.telegram_id
                )
                # Keep partner notification in the unified notification layer.
                try:
                    partner = data_core.get_application_full(int(args["application_id"]))
                    user_id = int(partner.get("user_id") or 0) if partner else 0
                    if user_id:
                        from notify import notify
                        await notify(
                            None, user_id,
                            title="✅ Հայտը հաստատվել է",
                            body="Ձեր գործընկերոջ հայտը հաստատվել է։ Ծառայությունները ակտիվ են, և կարող եք մուտք գործել գործընկերոջ աշխատասենյակ։",
                            kind="success",
                            audience="partner",
                            data={"application_id": int(args["application_id"]), "decision": "approved"},
                        )
                except Exception:
                    pass
                return {"ok": True, "item": result}
            if name == "admin_reject_application":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    return {"ok": False, "needs_clarification": True,
                            "question": "Укажите причину отклонения заявки."}
                return {"ok": True, "item": data_core.admin_reject_application(
                    int(args["application_id"]), reason, self.telegram_id
                )}
            if name == "admin_request_document_correction":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    raise ValueError("correction_reason_required")
                result = data_core.request_application_document_correction(
                    application_id=int(args["application_id"]),
                    reason=reason[:3000],
                    actor_user_id=self.telegram_id,
                )
                # Notify the partner immediately after the confirmed correction request.
                try:
                    app = result.get("application") or {}
                    partner = data_core.get_partner(int(app.get("partner_id") or 0)) if app.get("partner_id") else None
                    user_id = int(app.get("user_id") or (partner or {}).get("user_id") or (partner or {}).get("telegram_id") or 0)
                    if user_id:
                        from notify import notify
                        await notify(
                            None,
                            user_id,
                            title="📄 Փաստաթուղթը պետք է փոխարինել",
                            body="Ադմինիստրատորը խնդրել է նոր փաստաթուղթ ուղարկել։ Պատճառը՝ " + reason[:1000],
                            kind="document_correction",
                            audience="partner",
                            data={"application_id": int(args["application_id"]), "reason": reason[:1000]},
                        )
                except Exception:
                    pass
                return {"ok": True, "item": result}
            if name == "admin_suspend_partner":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    return {"ok": False, "needs_clarification": True,
                            "question": "Укажите причину блокировки партнёра."}
                return {"ok": True, "item": data_core.admin_suspend_partner(
                    int(args["partner_id"]), reason, self.telegram_id
                )}
            raise PermissionError("confirmed_admin_action_not_implemented")

        if self.context_type != ContextType.PARTNER:
            raise PermissionError("confirmed_action_not_allowed")

        pid = self._partner_id()

        if name == "add_address":
            return {"ok": True, "item": data_core.create_partner_address(
                partner_id=pid, actor_user_id=self.telegram_id,
                company_id=args.get("company_id"), address=args["address"],
                city=args.get("city"), marz=args.get("marz"),
                phone=args.get("phone"), object_name=args.get("object_name"),
            )}

        if name == "update_address":
            return {"ok": True, "item": data_core.update_partner_address(
                address_id=int(args["address_id"]), actor_user_id=self.telegram_id,
                address=args.get("address"), city=args.get("city"),
                marz=args.get("marz"), phone=args.get("phone"),
                object_name=args.get("object_name"),
            )}

        if name == "add_service":
            return {"ok": True, "item": data_core.create_partner_service_proposal(
                partner_id=pid, actor_user_id=self.telegram_id,
                company_id=int(args["company_id"]), name=args["name"],
                price=args.get("price"), category_id=args.get("category_id"),
                address_id=args.get("address_id"), phone=args.get("phone"),
                description=args.get("description"),
            )}

        if name == "add_services":
            company_id = int(args["company_id"])
            services = args.get("services") or []
            if not services:
                raise ValueError("services_required")

            # A new address is still a draft until the user confirms the
            # complete ADD_SERVICES action. Only then do we create the object
            # and the single admin-review application.
            address_id = args.get("address_id")
            address_text = str(args.get("address_text") or "").strip()
            service_location = args.get("service_location") if isinstance(args.get("service_location"), dict) else {}
            if address_id in (None, "") and (address_text or service_location):
                if not address_text and service_location:
                    lat = service_location.get("latitude")
                    lon = service_location.get("longitude")
                    if lat is not None and lon is not None:
                        address_text = f"GPS: {lat}, {lon}"
                if address_text:
                    created = data_core.create_partner_address(
                        partner_id=pid,
                        actor_user_id=self.telegram_id,
                        company_id=company_id,
                        address=address_text,
                        city=service_location.get("city"),
                        marz=service_location.get("marz"),
                        phone=args.get("phone"),
                        object_name="AI service location",
                    )
                    address_id = int(created["id"])
                    for item in services:
                        item["address_id"] = address_id

            result = data_core.create_partner_services_proposal(
                partner_id=pid,
                actor_user_id=self.telegram_id,
                company_id=company_id,
                services=services,
                service_mode=args.get("service_mode"),
                service_location=args.get("service_location"),
            )
            return {"ok": True, **result}

        if name == "update_service":
            return {"ok": True, "item": data_core.update_service_safe(
                service_id=int(args["service_id"]), actor_user_id=self.telegram_id,
                name=args.get("name"), price=args.get("price"),
                category_id=args.get("category_id"),
            )}

        if name == "add_company":
            return {"ok": True, "item": data_core.create_partner_company(
                partner_id=pid, actor_user_id=self.telegram_id,
                name=args["name"], description=args.get("description"),
                phone=args.get("phone"),
            )}

        if name == "update_company":
            return {"ok": True, "item": data_core.update_partner_company(
                company_id=int(args["company_id"]), actor_user_id=self.telegram_id,
                name=args.get("name"), description=args.get("description"),
                phone=args.get("phone"),
            )}

        if name == "archive_company":
            return {"ok": True, "item": data_core.archive_partner_company(
                company_id=int(args["company_id"]), actor_user_id=self.telegram_id,
            )}

        raise PermissionError("confirmed_action_not_implemented")