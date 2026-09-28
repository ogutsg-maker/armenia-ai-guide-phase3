"""Typed, permission-checked tools for the unified AIManager.

The model can request business operations, but it never gets SQL access and
never establishes its own identity. Every write is revalidated by data_core
with the authenticated Telegram user.
"""
from __future__ import annotations

import os
from typing import Any, Callable

import data_core
from prompt_factory import ContextType, as_context_type


def _nullable(kind: str) -> dict[str, Any]:
    return {"anyOf": [{"type": kind}, {"type": "null"}]}


class ToolRegistry:
    def __init__(
        self,
        *,
        telegram_id: int,
        context_type: ContextType | str,
        trusted_context: dict[str, Any] | None = None,
    ):
        self.telegram_id = int(telegram_id)
        self.context_type = as_context_type(context_type)
        self.trusted_context = trusted_context or {}

    def _admin_allowed(self) -> bool:
        user = data_core.get_user(self.telegram_id) or {}
        configured = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or 0)
        return (
            str(user.get("role") or "").lower() == "admin"
            or self.telegram_id == configured
            or int(user.get("telegram_id") or 0) == configured
        )

    @staticmethod
    def _fn(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                    "additionalProperties": False,
                },
            },
        }

    def definitions(self) -> list[dict[str, Any]]:
        if self.context_type == ContextType.REGISTRATION:
            return []

        if self.context_type == ContextType.CLIENT:
            return [
                self._fn(
                    "search_services",
                    "Search only real approved marketplace services. Never invent results.",
                    {
                        "query": {"type": "string"},
                        "city": {"type": "string"},
                        "category_id": _nullable("integer"),
                        "max_price": _nullable("number"),
                        "limit": {"type": "integer"},
                    },
                ),
            ]

        if self.context_type == ContextType.PARTNER:
            return [
                self._fn(
                    "get_my_companies",
                    "List the authenticated partner's active companies.",
                    {},
                ),
                self._fn(
                    "get_my_services",
                    "List the authenticated partner's services. Optionally filter by company.",
                    {
                        "company_id": _nullable("integer"),
                        "limit": {"type": "integer"},
                    },
                ),
                self._fn(
                    "get_my_addresses",
                    "List the authenticated partner's addresses/objects.",
                    {"limit": {"type": "integer"}},
                ),
                self._fn(
                    "get_my_orders",
                    "List the authenticated partner's orders. Use for conversational order browsing.",
                    {"limit": {"type": "integer"}},
                ),
                self._fn(
                    "add_service",
                    "Prepare adding a new service to one of the partner's companies. The write always requires confirmation.",
                    {
                        "company_id": {"type": "integer"},
                        "name": {"type": "string"},
                        "price": _nullable("number"),
                        "category_id": _nullable("integer"),
                        "address_id": _nullable("integer"),
                        "phone": _nullable("string"),
                    },
                    ["company_id", "name"],
                ),
                self._fn(
                    "update_service",
                    "Prepare changing an owned service name, price or catalog category. The write always requires confirmation.",
                    {
                        "service_id": {"type": "integer"},
                        "name": _nullable("string"),
                        "price": _nullable("number"),
                        "category_id": _nullable("integer"),
                    },
                    ["service_id"],
                ),
                self._fn(
                    "add_company",
                    "Prepare creating a new company owned by the authenticated partner. Confirmation required.",
                    {
                        "name": {"type": "string"},
                        "description": _nullable("string"),
                        "phone": _nullable("string"),
                    },
                    ["name"],
                ),
                self._fn(
                    "update_company",
                    "Prepare editing an owned company. Confirmation required.",
                    {
                        "company_id": {"type": "integer"},
                        "name": _nullable("string"),
                        "description": _nullable("string"),
                        "phone": _nullable("string"),
                    },
                    ["company_id"],
                ),
                self._fn(
                    "archive_company",
                    "Prepare archiving an owned company. Confirmation required.",
                    {"company_id": {"type": "integer"}},
                    ["company_id"],
                ),
            ]

        if self.context_type == ContextType.ADMIN and self._admin_allowed():
            return [
                self._fn(
                    "search_applications",
                    "Search real partner applications by status, marz or city.",
                    {"status": _nullable("string"), "marz": _nullable("string"), "city": _nullable("string"), "limit": {"type": "integer"}},
                ),
                self._fn(
                    "get_application",
                    "Get a complete real partner application by ID.",
                    {"application_id": {"type": "integer"}},
                    ["application_id"],
                ),
                self._fn(
                    "check_application",
                    "Run backend validation checks for a real application.",
                    {"application_id": {"type": "integer"}},
                    ["application_id"],
                ),
                self._fn(
                    "search_partners",
                    "Search real partners.",
                    {"query": {"type": "string"}, "city": {"type": "string"}, "limit": {"type": "integer"}},
                ),
                self._fn(
                    "search_services",
                    "Search real approved services.",
                    {"query": {"type": "string"}, "category_id": _nullable("integer"), "city": {"type": "string"}, "max_price": _nullable("number"), "limit": {"type": "integer"}},
                ),
            ]
        return []

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = dict(args or {})

        if self.context_type == ContextType.CLIENT and name == "search_services":
            return {
                "ok": True,
                "items": data_core.search_services(
                    category_id=args.get("category_id"),
                    city=str(args.get("city") or ""),
                    max_price=args.get("max_price"),
                    limit=max(1, min(int(args.get("limit") or 3), 10)),
                ),
            }

        if self.context_type == ContextType.PARTNER:
            partner = data_core.get_partner_by_user(self.telegram_id)
            if not partner:
                raise PermissionError("partner_not_found")
            pid = int(partner["id"])

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
                return {"ok": True, "items": data_core.list_services(
                    partner_id=pid,
                    actor_user_id=self.telegram_id,
                    limit=max(1, min(int(args.get("limit") or 100), 200)),
                ) if company_id is None else [
                    x for x in data_core.list_services(
                        partner_id=pid,
                        actor_user_id=self.telegram_id,
                        limit=200,
                    ) if int(x.get("business_id") or 0) == int(company_id)
                ]}

            if name == "get_my_addresses":
                return {"ok": True, "items": data_core.get_partner_addresses(
                    pid, actor_user_id=self.telegram_id,
                    limit=max(1, min(int(args.get("limit") or 100), 200)),
                )}

            if name == "get_my_orders":
                # Orders are intentionally resolved through the existing
                # domain gateway when available. Do not expose SQL here.
                getter = getattr(data_core, "list_partner_orders", None)
                if not getter:
                    return {"ok": True, "items": [], "unsupported": True}
                return {"ok": True, "items": getter(
                    partner_id=pid,
                    actor_user_id=self.telegram_id,
                    limit=max(1, min(int(args.get("limit") or 20), 50)),
                )}

            if name == "add_service":
                company_id = int(args["company_id"])
                company = data_core.get_company(company_id)
                if not company or int(company.get("partner_id") or 0) != pid:
                    raise PermissionError("company_not_owned")
                name_value = str(args.get("name") or "").strip()
                if not name_value:
                    raise ValueError("service_name_required")
                checked = data_core.validate_service_payload(
                    partner_id=pid,
                    actor_user_id=self.telegram_id,
                    company_id=company_id,
                    name=name_value,
                    price=args.get("price"),
                    category_id=args.get("category_id"),
                )
                if args.get("address_id") is not None:
                    addresses = data_core.get_partner_addresses(pid, actor_user_id=self.telegram_id)
                    address_id = int(args["address_id"])
                    obj = next((x for x in addresses if int(x.get("id") or 0) == address_id), None)
                    if not obj:
                        raise PermissionError("address_not_owned")
                    if int(obj.get("business_id") or 0) != company_id:
                        raise PermissionError("address_not_in_company")
                return self._confirmation(
                    name,
                    {
                        "company_id": company_id,
                        "name": checked["name"],
                        "price": checked["price"],
                        "category_id": args.get("category_id"),
                        "address_id": args.get("address_id"),
                        "phone": args.get("phone"),
                    },
                    f"Ավելացնել «{checked['name']}» ծառայությունը «{company.get('name') or 'ընկերություն'}» ընկերությունում"
                )

            if name == "update_service":
                service = data_core.assert_partner_owns_service(int(args["service_id"]), self.telegram_id)
                if all(args.get(k) in (None, "") for k in ("name", "price", "category_id")):
                    raise ValueError("no_changes")
                if args.get("price") not in (None, "") and float(args["price"]) < 0:
                    raise ValueError("invalid_service_price")
                if args.get("category_id") is not None:
                    cat = data_core.get_catalog_category(int(args["category_id"]))
                    if not cat or not cat.get("is_active"):
                        raise ValueError("catalog_category_invalid")
                return self._confirmation(
                    name,
                    {
                        "service_id": int(service["id"]),
                        "name": args.get("name"),
                        "price": args.get("price"),
                        "category_id": args.get("category_id"),
                    },
                    f"Изменить услугу #{int(service['id'])} «{service.get('name') or ''}»"
                )

            if name == "add_company":
                name_value = str(args.get("name") or "").strip()
                if not name_value:
                    raise ValueError("company_name_required")
                return self._confirmation(
                    name,
                    {"name": name_value, "description": args.get("description"), "phone": args.get("phone")},
                    f"Добавить компанию «{name_value}»"
                )

            if name == "update_company":
                company = data_core.get_company(int(args["company_id"]))
                if not company or int(company.get("partner_id") or 0) != pid:
                    raise PermissionError("company_not_owned")
                if all(args.get(k) in (None, "") for k in ("name", "description", "phone")):
                    raise ValueError("no_changes")
                return self._confirmation(
                    name,
                    {
                        "company_id": int(company["id"]),
                        "name": args.get("name"),
                        "description": args.get("description"),
                        "phone": args.get("phone"),
                    },
                    f"Изменить компанию «{company.get('name') or ''}»"
                )

            if name == "archive_company":
                company = data_core.get_company(int(args["company_id"]))
                if not company or int(company.get("partner_id") or 0) != pid:
                    raise PermissionError("company_not_owned")
                return self._confirmation(
                    name,
                    {"company_id": int(company["id"])},
                    f"Архивировать компанию «{company.get('name') or ''}»"
                )

        if self.context_type == ContextType.ADMIN and self._admin_allowed():
            if name == "search_applications":
                return {"ok": True, "items": data_core.search_applications(
                    status=args.get("status"), marz=args.get("marz"), city=args.get("city"),
                    limit=max(1, min(int(args.get("limit") or 50), 200)),
                )}
            if name == "get_application":
                return {"ok": True, "item": data_core.get_application_full(int(args["application_id"]))}
            if name == "check_application":
                return {"ok": True, "item": data_core.check_application(int(args["application_id"]))}
            if name == "search_partners":
                return {"ok": True, "items": data_core.search_partners(
                    query=str(args.get("query") or ""), city=str(args.get("city") or ""),
                    limit=max(1, min(int(args.get("limit") or 20), 100)),
                )}
            if name == "search_services":
                return {"ok": True, "items": data_core.search_services(
                    category_id=args.get("category_id"), city=str(args.get("city") or ""),
                    max_price=args.get("max_price"), limit=max(1, min(int(args.get("limit") or 20), 100)),
                )}

        raise PermissionError("tool_not_allowed")

    @staticmethod
    def _confirmation(name: str, args: dict[str, Any], summary: str) -> dict[str, Any]:
        return {
            "ok": True,
            "requires_confirmation": True,
            "action": {"name": name, "args": args},
            "summary": summary + ". Подтверждаете?",
        }

    async def execute_confirmed(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = dict(args or {})

        if self.context_type != ContextType.PARTNER:
            raise PermissionError("confirmed_tool_not_allowed")

        partner = data_core.get_partner_by_user(self.telegram_id)
        if not partner:
            raise PermissionError("partner_not_found")
        pid = int(partner["id"])

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

        raise PermissionError("confirmed_tool_not_allowed")
