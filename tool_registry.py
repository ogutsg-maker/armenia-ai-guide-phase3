"""Typed tool registry for the unified AI operator.

The LLM can only request tools. ToolRegistry is the security boundary:
READ tools may return verified data; ACTION_CONFIRM tools only prepare an
action. Actual mutations are executed later by execute_confirmed(), after the
user confirms and backend ownership/state validation runs again.
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
                "save_completed_application",
                "Save the completed partner registration after the model has collected company name, Armenian city, phone and at least one service. This is the ONLY registration write that the AI may execute automatically; backend validates the authenticated Telegram user. Never call it with invented data and never call it before all required fields are known.",
                {
                    "company_name": {"type": "string"},
                    "city": {"type": "string"},
                    "phone": {"type": "string"},
                    "services": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
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
                {"status": _nullable("string"), "limit": {"type": "integer"}},
                contexts=(ContextType.CLIENT, ContextType.PARTNER),
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
                },
                required=("company_id", "name"),
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
        if self.context_type == ContextType.REGISTRATION:
            return []
        if self.context_type == ContextType.ADMIN and not self._admin_allowed():
            return []
        return [
            s for s in self._all_specs()
            if self.context_type in s.contexts
        ]

    def definitions(self) -> list[dict[str, Any]]:
        return [s.schema() for s in self._visible_specs()]

    def spec(self, name: str) -> ToolSpec | None:
        return next((s for s in self._visible_specs() if s.name == name), None)

    def _save_completed_application(self, args: dict[str, Any]) -> dict[str, Any]:
        """Persist a completed registration using only backend-owned identity."""
        if self.context_type != ContextType.REGISTRATION:
            raise PermissionError("registration_tool_only")

        company_name = str(args.get("company_name") or "").strip()
        city = str(args.get("city") or "").strip()
        phone = str(args.get("phone") or "").strip()
        raw_services = args.get("services")
        if not company_name or not city or not phone or not isinstance(raw_services, list):
            raise ValueError("registration_required_fields_missing")

        services: list[dict[str, Any]] = []
        for raw in raw_services:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            price = raw.get("price")
            if price not in (None, ""):
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    raise ValueError("invalid_service_price")
                if price < 0:
                    raise ValueError("invalid_service_price")
            price_type = str(raw.get("price_type") or "").strip().lower()
            if price_type not in {"from", "fixed"}:
                raise ValueError("invalid_price_type")
            services.append({"name": name, "price": price, "price_type": price_type})

        if not services:
            raise ValueError("service_required")

        profile = {
            "business_name": company_name,
            "city": city,
            "phone": phone,
            "services": services,
        }
        draft = data_core.save_partner_application_draft(
            user_id=self.telegram_id,
            profile=profile,
        )
        application_id = int(draft.get("application_id") or draft.get("id") or 0)
        if not application_id:
            raise RuntimeError("application_save_failed")
        partner = data_core.get_partner_by_user(self.telegram_id) or {}
        partner_id = int(partner.get("id") or 0)
        if not partner_id:
            raise RuntimeError("partner_not_found_after_save")
        submitted = data_core.submit_partner_application(
            application_id,
            partner_id=partner_id,
            actor_user_id=self.telegram_id,
        )
        if not submitted:
            raise RuntimeError("application_submit_failed")
        return {
            "ok": True,
            "application_id": application_id,
            "status": submitted.get("status") or "pending_admin",
            "profile": profile,
            "message": "application_saved",
        }

    def _state_entity_id(self, entity_type: str) -> int | None:
        state = self.session_state
        last = state.get("last_displayed_entity_id")
        if isinstance(last, dict):
            if str(last.get("type") or "") != entity_type:
                return None
            value = last.get("id")
        else:
            value = last
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def resolve_reference(self, entity_type: str) -> dict[str, Any]:
        """Resolve pronouns only when the state contains exactly one target."""
        state = self.session_state
        current_list = state.get("current_list") or []
        if isinstance(current_list, list) and len(current_list) > 1:
            return {
                "ok": False,
                "needs_clarification": True,
                "question": "Какой именно объект выбрать?",
            }
        value = self._state_entity_id(entity_type)
        if value is None:
            return {
                "ok": False,
                "needs_clarification": True,
                "question": "Какой именно объект выбрать?",
            }
        return {"ok": True, "id": value}

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
                return {"ok": True, "item": item} if item else {"ok": False, "error": "application_not_found"}
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
            if name == "admin_approve_application":
                application_id = int(args["application_id"])
                app = data_core.get_application_full(application_id)
                if not app:
                    raise ValueError("application_not_found")
                if str(app.get("status") or "").lower() in {"approved", "rejected"}:
                    raise ValueError("application_already_final")
                return self._prepare_action(name, {"application_id": application_id},
                                            f"Утвердить заявку #{application_id}?")
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
                if not any(int(x.get("id") or 0) == address_id for x in visible):
                    raise PermissionError("address_not_owned")
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
                company_id = int(args["company_id"])
                company = data_core.get_company(company_id)
                if not company or int(company.get("partner_id") or 0) != pid:
                    raise PermissionError("company_not_owned")
                checked = data_core.validate_service_payload(
                    partner_id=pid, actor_user_id=self.telegram_id,
                    company_id=company_id, name=args["name"],
                    price=args.get("price"), category_id=args.get("category_id"),
                )
                if args.get("address_id") is not None:
                    addresses = data_core.get_partner_addresses(
                        pid, actor_user_id=self.telegram_id, limit=200
                    )
                    address_id = int(args["address_id"])
                    obj = next((x for x in addresses if int(x.get("id") or 0) == address_id), None)
                    if not obj or int(obj.get("business_id") or 0) != company_id:
                        raise PermissionError("address_not_in_company")
                return self._prepare_action(
                    name,
                    {
                        "company_id": company_id, "name": checked["name"],
                        "price": checked["price"],
                        "category_id": args.get("category_id"),
                        "address_id": args.get("address_id"),
                        "phone": args.get("phone"),
                    },
                    f"Добавить услугу «{checked['name']}» в компанию «{company.get('name') or ''}»?",
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
            if name == "admin_approve_application":
                return {"ok": True, "item": data_core.admin_approve_application(
                    int(args["application_id"]), self.telegram_id
                )}
            if name == "admin_reject_application":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    return {"ok": False, "needs_clarification": True,
                            "question": "Укажите причину отклонения заявки."}
                return {"ok": True, "item": data_core.admin_reject_application(
                    int(args["application_id"]), reason, self.telegram_id
                )}
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
            return {"ok": True, "item": data_core.create_partner_service(
                partner_id=pid, actor_user_id=self.telegram_id,
                company_id=int(args["company_id"]), name=args["name"],
                price=args.get("price"), category_id=args.get("category_id"),
                address_id=args.get("address_id"), phone=args.get("phone"),
            )}

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
