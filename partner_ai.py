"""AI-first partner onboarding and partner-side business assistant.

AI understands the partner's natural language. Python validates the proposed
operation. Data Core performs the actual business read/write. No AI Tools,
action-plan or AI-context layer is used here.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import data_core
from platform_db import (
    active_session, create_session, update_session, add_ai_message,
    recent_ai_messages, ensure_partner, update_partner,
    create_or_update_proposal, latest_clarification, mark_clarification_answered,
)

logger = logging.getLogger(__name__)


def _explicit_confirmation(value: Any) -> bool:
    t = str(value or "").casefold().strip()
    return t in {
        "yes", "да", "հա", "այո", "հաստատում եմ", "հաստատել",
        "ուղարկել", "համաձայն եմ", "ok", "okay",
    } or t.startswith(("yes,", "да,", "այո,", "հա,"))


def _clean_patch(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "business_name", "business_description", "location", "marz", "city",
        "village", "address", "phone", "working_hours", "services",
        "prices", "staff", "packages",
    }
    out = {}
    for key, item in value.items():
        if key not in allowed or item in (None, "", []):
            continue
        if key in {"services", "prices", "staff", "packages"} and not isinstance(item, list):
            continue
        out[key] = item.strip() if isinstance(item, str) else item
    return out


def _digits(value: Any) -> int | None:
    try:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else None
    except Exception:
        return None


def _prepare_action(data: dict, partner_id: int, actor_user_id: int) -> dict | None:
    """Validate an AI-proposed mutation without executing it."""
    action = str(data.get("action") or "none").strip()
    if action in {"add_service", "update_service"}:
        payload = data.get("service") if isinstance(data.get("service"), dict) else data
        if action == "add_service":
            company_id = _digits(payload.get("company_id"))
            category_id = _digits(payload.get("category_id"))
            checked = data_core.validate_service_payload(
                partner_id=partner_id, actor_user_id=actor_user_id,
                company_id=company_id, name=payload.get("name") or payload.get("service_name"),
                price=payload.get("price"), category_id=category_id,
            )
            args = {
                "action": "add_service",
                "company_id": checked["company_id"],
                "name": checked["name"],
                "price": checked["price"],
                "category_id": category_id,
                "address_id": _digits(payload.get("address_id")),
                "phone": payload.get("phone"),
            }
        else:
            service_id = _digits(payload.get("service_id"))
            if service_id is None:
                raise ValueError("service_id_required")
            service = data_core.get_service(service_id)
            if not service or int(service.get("partner_id") or 0) != int(partner_id):
                raise PermissionError("service_not_owned")
            args = {
                "action": "update_service",
                "service_id": service_id,
                "name": payload.get("name"),
                "price": payload.get("price"),
                "category_id": _digits(payload.get("category_id")),
            }
            if args["name"] is not None and not str(args["name"]).strip():
                raise ValueError("service_name_required")
            if args["price"] not in (None, ""):
                try:
                    if float(args["price"]) < 0:
                        raise ValueError("invalid_service_price")
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid_service_price") from exc
            if args["category_id"] is not None:
                category = data_core.get_catalog_category(args["category_id"])
                if not category or not category.get("is_active"):
                    raise ValueError("catalog_category_invalid")
        return {"tool": "service", "arguments": args}

    if action in {"add_company", "update_company", "archive_company"}:
        payload = data.get("company") if isinstance(data.get("company"), dict) else data
        company_id = _digits(payload.get("company_id"))
        if action == "add_company":
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("company_name_required")
            args = {
                "action": action, "name": name,
                "description": payload.get("description"), "phone": payload.get("phone"),
            }
        elif company_id is None:
            raise ValueError("company_id_required")
        elif action == "update_company":
            company = data_core.get_company(company_id)
            if not company or int(company.get("partner_id") or 0) != int(partner_id):
                raise PermissionError("company_not_owned")
            args = {
                "action": action, "company_id": company_id,
                "name": payload.get("name"), "description": payload.get("description"),
                "phone": payload.get("phone"),
            }
            if args["name"] is not None and not str(args["name"]).strip():
                raise ValueError("company_name_required")
        else:
            company = data_core.get_company(company_id)
            if not company or int(company.get("partner_id") or 0) != int(partner_id):
                raise PermissionError("company_not_owned")
            args = {"action": action, "company_id": company_id}
        return {"tool": "company", "arguments": args}

    if action in {"add_address", "update_address"}:
        payload = data.get("address_data") if isinstance(data.get("address_data"), dict) else data
        address_id = _digits(payload.get("address_id"))
        company_id = _digits(payload.get("company_id"))
        if action == "add_address":
            address = str(payload.get("address") or "").strip()
            if not address:
                raise ValueError("address_required")
            # Data Core validates partner/company ownership now; no write yet.
            if company_id is not None:
                company = data_core.get_company(company_id)
                if not company or int(company.get("partner_id") or 0) != int(partner_id):
                    raise PermissionError("company_not_owned")
            args = {
                "action": action, "company_id": company_id, "address": address,
                "city": payload.get("city"), "marz": payload.get("marz"),
                "phone": payload.get("phone"), "object_name": payload.get("object_name"),
            }
        elif address_id is None:
            raise ValueError("address_id_required")
        else:
            addresses = data_core.get_partner_addresses(partner_id, actor_user_id=actor_user_id)
            if not any(int(x.get("id") or 0) == address_id for x in addresses):
                raise PermissionError("address_not_owned")
            args = {
                "action": action, "address_id": address_id,
                "address": payload.get("address"), "city": payload.get("city"),
                "marz": payload.get("marz"), "phone": payload.get("phone"),
                "object_name": payload.get("object_name"),
            }
        return {"tool": "address", "arguments": args}

    return None


def _execute_action(tool: str, args: dict, partner_id: int, actor_user_id: int) -> dict:
    action = str(args.get("action") or "")
    if tool == "service":
        if action == "add_service":
            return {"service": data_core.create_partner_service(
                partner_id=partner_id, actor_user_id=actor_user_id,
                company_id=args.get("company_id"), name=args.get("name"),
                price=args.get("price"), category_id=args.get("category_id"),
                address_id=args.get("address_id"), phone=args.get("phone"),
            ), "executed": True}
        return {"service": data_core.update_service_safe(
            service_id=int(args["service_id"]), actor_user_id=actor_user_id,
            name=args.get("name"), price=args.get("price"),
            category_id=args.get("category_id"),
        ), "executed": True}
    if tool == "company":
        if action == "add_company":
            return {"company": data_core.create_partner_company(
                partner_id=partner_id, actor_user_id=actor_user_id,
                name=args.get("name"), description=args.get("description"),
                phone=args.get("phone"),
            ), "executed": True}
        if action == "update_company":
            return {"company": data_core.update_partner_company(
                company_id=int(args["company_id"]), actor_user_id=actor_user_id,
                name=args.get("name"), description=args.get("description"),
                phone=args.get("phone"),
            ), "executed": True}
        return {"company": data_core.archive_partner_company(
            company_id=int(args["company_id"]), actor_user_id=actor_user_id,
        ), "executed": True}
    if tool == "address":
        if action == "add_address":
            return {"address": data_core.create_partner_address(
                partner_id=partner_id, actor_user_id=actor_user_id,
                company_id=args.get("company_id"), address=args.get("address"),
                city=args.get("city"), marz=args.get("marz"),
                phone=args.get("phone"), object_name=args.get("object_name"),
            ), "executed": True}
        return {"address": data_core.update_partner_address(
            address_id=int(args["address_id"]), actor_user_id=actor_user_id,
            address=args.get("address"), city=args.get("city"),
            marz=args.get("marz"), phone=args.get("phone"),
            object_name=args.get("object_name"),
        ), "executed": True}
    raise ValueError("unsupported_partner_action")


class PartnerAI:
    def __init__(self, ai):
        self.ai = ai

    async def process(self, user_id: int, text: str, lang: str = "hy") -> dict:
        partner = ensure_partner(user_id)
        session = active_session(user_id, "partner", "onboarding") or create_session(
            user_id, "partner", "onboarding",
            {"partner_id": partner["id"], "profile": {}, "proposal_id": None,
             "confirmed": False, "awaiting_document": False},
        )
        ctx = session.get("context_json") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        ctx.setdefault("partner_id", partner["id"])
        ctx.setdefault("profile", {})
        add_ai_message(session["id"], "user", text)

        history = recent_ai_messages(session["id"], 14)
        clarification = latest_clarification(partner["id"])
        system = f"""Դու Armenia AI Guide-ի գործընկերոջ անձնական AI օգնականն ես։
Դու աշխատում ես միայն իրական տվյալներով և չես հորինում ընկերություններ, ծառայություններ կամ հաստատումներ։
Լեզուն՝ {lang}. Պատասխանիր նույն լեզվով, կարճ և բնական։

Կանոններ.
1) Գրանցման փուլում գործընկերը չի ընտրում ուղղություն, ենթակատեգորիա կամ կատալոգային ID։
2) Գործընկերը կարող է ազատ ձևով պատմել բիզնեսի, ծառայությունների, գների, հասցեի և աշխատանքային ժամերի մասին։
3) Փոփոխություն կատարելուց առաջ առաջարկիր հստակ գործողություն և սպասիր «Համաձայն եմ/Հաստատել»-ին։
4) Մի հայտարարիր հաստատված ուղղություն կամ հաստատված գործընկեր, եթե ադմինը դա չի հաստատել։
5) Եթե տվյալը չկա բազայում կամ հաղորդագրությունում, ասա, որ այն պետք է ճշտել։
6) AI-ն միայն հասկանում և առաջարկում է։ Python/Data Core-ն է ստուգում և գրում տվյալը։

Ընթացիկ պրոֆիլը:
{json.dumps(ctx.get("profile", {}), ensure_ascii=False)}

Վերջին հաղորդագրությունները:
{json.dumps(history, ensure_ascii=False)}

Վերադարձիր միայն JSON.
{{
  "reply":"...",
  "profile_patch":{{"business_name":"","business_description":"","location":"","marz":"","city":"","village":"","address":"","phone":"","working_hours":"","services":[],"prices":[],"staff":[],"packages":[]}},
  "catalog_proposal":{{"needed":false,"master_category":"","category":"","subcategory":"","service":"","reason":"","description":""}},
  "action":"none|add_service|update_service|add_company|update_company|archive_company|add_address|update_address",
  "company":{{"company_id":null,"name":"","description":"","phone":""}},
  "address_data":{{"address_id":null,"company_id":null,"address":"","city":"","marz":"","phone":"","object_name":""}},
  "service":{{"service_id":null,"company_id":null,"address_id":null,"name":"","price":null,"category_id":null,"phone":""}},
  "confirmed":false,
  "needs_document":false,
  "next_step":"profile|confirmation|document|waiting_admin|done"
}}"""

        pending_action = ctx.get("pending_action")
        if pending_action and _explicit_confirmation(text):
            try:
                pa = pending_action if isinstance(pending_action, dict) else {}
                executed = _execute_action(
                    str(pa.get("tool") or ""),
                    dict(pa.get("arguments") or {}),
                    int(partner["id"]), int(user_id),
                )
                ctx.pop("pending_action", None)
                reply = "Կատարված է։" if lang == "hy" else ("Готово." if lang == "ru" else "Done.")
                update_session(session["id"], ctx)
                add_ai_message(session["id"], "ai", reply, executed)
                return {"reply": reply, "context": ctx, "raw": {"executed": executed}}
            except Exception:
                logger.exception("Confirmed partner action failed")
                ctx.pop("pending_action", None)
                update_session(session["id"], ctx)

        try:
            data = await self.ai.chat_json(
                system, text, max_tokens=1200,
                chain="partner_registration", stage="dialogue",
                operation="partner_profile_extraction",
                purpose="Extract business profile, services, prices and partner-side actions",
                user_id=user_id, partner_id=int(partner["id"]),
            )
        except Exception:
            logger.exception("Partner AI failed")
            data = {
                "reply": "Ես այստեղ եմ։ Խնդրում եմ պատմեք ձեր բիզնեսի մասին՝ ինչ ծառայություններ եք առաջարկում, որտեղ եք աշխատում և ինչ գներով։",
                "profile_patch": {}, "catalog_proposal": {"needed": False},
                "action": "none", "confirmed": False, "needs_document": False,
                "next_step": "profile",
            }

        action = str(data.get("action") or "none").strip()
        if action != "none":
            try:
                prepared = _prepare_action(data, int(partner["id"]), int(user_id))
            except Exception as exc:
                logger.warning("Partner action validation failed: %s", exc)
                prepared = None
            if prepared:
                ctx["pending_action"] = prepared
                reply = (
                    "Հաստատե՞լ այս փոփոխությունը։" if lang == "hy"
                    else ("Подтвердить это изменение?" if lang == "ru" else "Confirm this change?")
                )
                update_session(session["id"], ctx)
                add_ai_message(session["id"], "ai", reply, prepared)
                return {"reply": reply, "context": ctx, "raw": prepared}

        patch = _clean_patch(data.get("profile_patch"))
        ctx["profile"].update(patch)

        proposal_data = data.get("catalog_proposal") or {}
        proposal_id = ctx.get("proposal_id")
        if isinstance(proposal_data, dict) and proposal_data.get("needed"):
            p = create_or_update_proposal(partner["id"], proposal_data, proposal_id)
            proposal_id = p["id"]
            ctx["proposal_id"] = proposal_id
            ctx["awaiting_document"] = False

        try:
            if ctx.get("profile"):
                update_partner(
                    partner["id"],
                    business_name=ctx["profile"].get("business_name")
                    or partner.get("business_name")
                    or f"Գործընկեր {partner['id']}",
                    business_description=ctx["profile"].get("business_description")
                    or partner.get("business_description") or "",
                    profile_json=ctx["profile"],
                    status="pending",
                    verification_status="not_submitted",
                )
        except Exception:
            logger.exception("Failed to persist partner profile")

        if data.get("confirmed"):
            ctx["awaiting_document"] = True
        if clarification:
            ctx["last_admin_clarification_id"] = clarification["id"]
            mark_clarification_answered(clarification["id"])

        update_session(session["id"], ctx)
        reply = str(data.get("reply") or "")
        add_ai_message(session["id"], "ai", reply, data)
        return {"reply": reply, "context": ctx, "raw": data}

    async def initial_message(self, user_id: int, lang: str = "hy") -> str:
        p = ensure_partner(user_id)
        s = active_session(
            user_id, "partner", "onboarding"
        ) or create_session(user_id, "partner", "onboarding", {
            "partner_id": p["id"], "profile": {},
        })
        text = {
            "hy": "Բարև 👋 Ես ձեր AI օգնականն եմ։ Ես կօգնեմ ձեր բիզնեսը միացնել Armenia AI Guide-ին՝ առանց բարդ ձևերի։ Պատմեք ձեր բիզնեսի մասին՝ ինչ եք անում, որտեղ եք աշխատում և ինչ ծառայություններ եք առաջարկում։ Կարող եք գրել ազատ ձևով կամ ուղարկել PDF/լուսանկար/գնացուցակ։",
            "ru": "Здравствуйте 👋 Я ваш AI-помощник. Я помогу подключить бизнес к Armenia AI Guide без сложных анкет. Расскажите о бизнесе свободным текстом: чем занимаетесь, где работаете, какие услуги и цены. Можно прислать PDF, фото или прайс-лист.",
            "en": "Hello 👋 I am your AI assistant. I will connect your business to Armenia AI Guide without complicated forms. Tell me about your business, location, services and prices. You can also send a PDF, photo or price list.",
        }[lang]
        add_ai_message(s["id"], "ai", text)
        return text
