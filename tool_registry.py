"""Safe backend tools exposed to the unified AIManager.

The model can request operations, but it never receives SQL access and never
gets to establish its own identity. All partner operations are checked against
the authenticated Telegram user inside data_core.
"""
from __future__ import annotations
from typing import Any
import data_core
from prompt_factory import ContextType, as_context_type

class ToolRegistry:
    def __init__(self, *, telegram_id: int, context_type: ContextType | str, trusted_context: dict[str, Any] | None = None):
        self.telegram_id = int(telegram_id)
        self.context_type = as_context_type(context_type)
        self.trusted_context = trusted_context or {}

    def _admin_allowed(self) -> bool:
        user = data_core.get_user(self.telegram_id) or {}
        return str(user.get("role") or "").lower() == "admin" or int(user.get("telegram_id") or 0) == int(__import__("os").getenv("ADMIN_TELEGRAM_ID","0") or 0)

    def definitions(self) -> list[dict[str, Any]]:
        common = []
        if self.context_type == ContextType.CLIENT:
            common.append({"type":"function","function":{
                "name":"search_services","description":"Search approved real marketplace services by query, category, city and maximum price.",
                "parameters":{"type":"object","properties":{
                    "query":{"type":"string"},"category_id":{"type":["integer","null"]},
                    "city":{"type":"string"},"max_price":{"type":["number","null"]},"limit":{"type":"integer"}
                },"required":[]}}})
        elif self.context_type == ContextType.PARTNER:
            common += [
                {"type":"function","function":{"name":"get_my_companies","description":"List companies owned by the authenticated partner.","parameters":{"type":"object","properties":{}}}},
                {"type":"function","function":{"name":"get_my_services","description":"List services owned by the authenticated partner.","parameters":{"type":"object","properties":{"company_id":{"type":["integer","null"]},"limit":{"type":"integer"}}}}},
                {"type":"function","function":{"name":"update_service_price","description":"Prepare a service price change. Requires confirmation before writing.","parameters":{"type":"object","properties":{"service_id":{"type":"integer"},"price":{"type":"number"}},"required":["service_id","price"]}}},
            ]
        elif self.context_type == ContextType.ADMIN and self._admin_allowed():
            common += [
                {"type":"function","function":{"name":"search_applications","description":"Search partner applications by status, marz or city.","parameters":{"type":"object","properties":{"status":{"type":["string","null"]},"marz":{"type":["string","null"]},"city":{"type":["string","null"]},"limit":{"type":"integer"}}}}},
                {"type":"function","function":{"name":"get_application","description":"Get a complete partner application by ID.","parameters":{"type":"object","properties":{"application_id":{"type":"integer"}},"required":["application_id"]}}},
                {"type":"function","function":{"name":"check_application","description":"Run backend validation checks for an application.","parameters":{"type":"object","properties":{"application_id":{"type":"integer"}},"required":["application_id"]}}},
                {"type":"function","function":{"name":"search_partners","description":"Search real partners.","parameters":{"type":"object","properties":{"query":{"type":"string"},"city":{"type":"string"},"limit":{"type":"integer"}}}}},
                {"type":"function","function":{"name":"search_services","description":"Search real services.","parameters":{"type":"object","properties":{"query":{"type":"string"},"category_id":{"type":["integer","null"]},"city":{"type":"string"},"max_price":{"type":["number","null"]},"limit":{"type":"integer"}}}}},
            ]
        return common

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.context_type == ContextType.CLIENT and name == "search_services":
            return {"ok":True,"items":data_core.search_services(
                category_id=args.get("category_id"), city=str(args.get("city") or ""),
                max_price=args.get("max_price"), limit=args.get("limit",20)
            )}
        if self.context_type == ContextType.PARTNER:
            partner = data_core.get_partner_by_user(self.telegram_id)
            if not partner: raise PermissionError("partner_not_found")
            pid = int(partner["id"])
            if name == "get_my_companies":
                return {"ok":True,"items":data_core.list_partner_companies(partner_id=pid,actor_user_id=self.telegram_id)}
            if name == "get_my_services":
                return {"ok":True,"items":data_core.list_services(partner_id=pid,actor_user_id=self.telegram_id,limit=args.get("limit",100))}
            if name == "update_service_price":
                service = data_core.get_service(int(args["service_id"]))
                if not service: raise LookupError("service_not_found")
                data_core.assert_partner_owns_service(int(args["service_id"]), self.telegram_id)
                price=float(args["price"])
                if price < 0: raise ValueError("invalid_price")
                return {"ok":True,"requires_confirmation":True,"danger":"update_service_price",
                        "summary":f"Изменить цену услуги #{int(args['service_id'])} «{service.get('name')}» на {price:g} AMD",
                        "action":{"name":name,"args":{"service_id":int(args["service_id"]),"price":price}}}
        if self.context_type == ContextType.ADMIN and self._admin_allowed():
            if name == "search_applications":
                return {"ok":True,"items":data_core.search_applications(status=args.get("status"),marz=args.get("marz"),city=args.get("city"),limit=args.get("limit",50))}
            if name == "get_application":
                return {"ok":True,"item":data_core.get_application_full(int(args["application_id"]))}
            if name == "check_application":
                return {"ok":True,"item":data_core.check_application(int(args["application_id"]))}
            if name == "search_partners":
                return {"ok":True,"items":data_core.search_partners(query=args.get("query",""),city=args.get("city",""),limit=args.get("limit",20))}
            if name == "search_services":
                return {"ok":True,"items":data_core.search_services(category_id=args.get("category_id"),city=args.get("city",""),max_price=args.get("max_price"),limit=args.get("limit",20))}
        raise PermissionError("tool_not_allowed")

    async def execute_confirmed(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.context_type == ContextType.PARTNER and name == "update_service_price":
            data_core.assert_partner_owns_service(int(args["service_id"]), self.telegram_id)
            return {"ok":True,"item":data_core.update_service_safe(service_id=int(args["service_id"]),actor_user_id=self.telegram_id,price=float(args["price"]))}
        raise PermissionError("confirmed_tool_not_allowed")
