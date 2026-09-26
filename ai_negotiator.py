"""AI negotiation engine for the current PostgreSQL marketplace flow.

This module intentionally has no legacy Supabase client dependency.  The
marketplace API owns its transaction helpers and passes small DB hooks to
handle(), so the negotiator remains independent from the HTTP layer.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from ai_service import AIService


class AINegotiator:
    def __init__(self, ai_service: AIService | None = None):
        self.ai_service = ai_service or AIService()

    @staticmethod
    def _state(negotiation: dict) -> dict:
        value = negotiation.get("state_json") or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _extract_price(text: str) -> float | None:
        # Accept common AMD price forms: 5000, 5 000, 5,000, 5000 AMD.
        for match in re.findall(r"(?<!\d)(\d{1,3}(?:[ ,]\d{3})+|\d{3,7})(?:\s*(?:AMD|դրամ|դր))?", text, re.I):
            try:
                value = float(match.replace(" ", "").replace(",", ""))
                if 100 <= value <= 10_000_000:
                    return value
            except ValueError:
                continue
        return None

    @staticmethod
    def _contains_agreement(text: str) -> bool:
        low = text.lower()
        words = (
            "согласен", "согласна", "подходит", "готов", "готова", "заказываю",
            "договорились", "беру", "ок", "да", "համաձայն եմ", "լավ", "կհամաձայնեմ",
        )
        return any(word in low for word in words)

    @staticmethod
    def _contains_refusal(text: str) -> bool:
        low = text.lower()
        return any(x in low for x in ("отказываюсь", "не подходит", "отмена", "отменяю", "չեմ ուզում"))

    def generate_system_prompt(self, context: dict) -> str:
        service = context.get("service_name") or context.get("service_type") or "услуга"
        price = context.get("price") or context.get("agreed_price")
        city = context.get("city") or context.get("district") or "Армения"
        price_line = f"Текущая цена: {price} AMD." if price else "Цена пока не зафиксирована."
        return (
            "Ты — AI-переговорщик маркетплейса Armenia AI Guide. "
            "Помогай клиенту и партнёру согласовать услугу, цену, место и время.\n\n"
            "ПРАВИЛА:\n"
            "1. Не раскрывай телефон, точный адрес или другие прямые контакты до выполнения правил платформы.\n"
            "2. Не выдумывай наличие партнёров, цены, расписание или факты. Используй только данные контекста.\n"
            "3. Если клиент предлагает другую цену, зафиксируй её как предложение и попроси сторону подтвердить.\n"
            "4. Не создавай бронь только из-за слова 'да', если существенные условия ещё неизвестны.\n"
            "5. Когда обе стороны явно согласовали услугу, итоговую цену, место/город и время (если время требуется), сообщи, что условия согласованы.\n\n"
            f"Услуга: {service}.\n"
            f"Место: {city}.\n"
            f"{price_line}"
        )

    async def handle(
        self,
        negotiation: dict,
        actor: str,
        sender_id: int,
        text: str,
        *,
        insert_msg: Callable | None = None,
        update_neg: Callable | None = None,
        update_request: Callable | None = None,
        insert_ai_msg: Callable | None = None,
    ) -> dict:
        """Process one negotiation message and persist through supplied hooks."""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "message_required"}

        state = self._state(negotiation)
        state.setdefault("client_agreed", False)
        state.setdefault("partner_agreed", False)

        # Persist the original message first so the conversation is durable.
        if insert_msg:
            insert_msg(negotiation["id"], actor, sender_id, text)

        price = self._extract_price(text)
        if price is not None:
            state["proposed_price"] = price
            # A new price is a new proposal and requires confirmation by both sides.
            state["client_agreed"] = False
            state["partner_agreed"] = False
            if actor == "client":
                state["client_price"] = price
            else:
                state["partner_price"] = price

        if self._contains_refusal(text):
            state["last_action"] = "refused"
            if update_neg:
                update_neg(negotiation["id"], state, "cancelled")
            if update_request and negotiation.get("request_id"):
                update_request(negotiation["request_id"], "cancelled")
            reply = "Понял. Переговоры отменены."
            if insert_ai_msg:
                insert_ai_msg(negotiation["id"], "ai", reply, {"action": "cancelled"})
            return {"ok": True, "reply_text": reply, "status": "cancelled", "state": state}

        if self._contains_agreement(text):
            if actor == "client":
                state["client_agreed"] = True
            elif actor == "partner":
                state["partner_agreed"] = True

        # Use the final proposed/agreed price, never an old catalogue price.
        final_price = state.get("proposed_price") or state.get("agreed_price") or state.get("price")
        if final_price is not None:
            try:
                state["agreed_price"] = float(final_price)
            except (TypeError, ValueError):
                pass

        both_agreed = bool(state.get("client_agreed") and state.get("partner_agreed"))
        if both_agreed:
            state["last_action"] = "agreed"
            if update_neg:
                update_neg(negotiation["id"], state, "agreed")
            if update_request and negotiation.get("request_id"):
                update_request(negotiation["request_id"], "confirmed")
            reply = "Условия согласованы обеими сторонами. Можно переходить к оплате и оформлению бронирования."
            if insert_ai_msg:
                insert_ai_msg(negotiation["id"], "ai", reply, {"action": "agreed", "price": state.get("agreed_price")})
            return {"ok": True, "reply_text": reply, "status": "agreed", "state": state}

        prompt = self.generate_system_prompt(state)
        try:
            # Resolve the company from the negotiated service so AI cost can be
            # attributed to the same company as the eventual booking.
            company_id = None
            try:
                service_id = (state or {}).get("service_id")
                if service_id:
                    svc = data_core.marketplace_service_for_partner(
                        int(service_id), int(negotiation.get("partner_id") or 0)
                    )
                    if svc and svc.get("business_id"):
                        company_id = int(svc["business_id"])
            except Exception:
                company_id = None

            result = await self.ai_service.chat_json(
                prompt, text, max_tokens=700,
                chain="negotiation", stage="dialogue", operation="negotiation_reply",
                purpose="Analyze negotiation message and formulate safe reply",
                user_id=sender_id,
                partner_id=int(negotiation.get("partner_id")) if negotiation.get("partner_id") else None,
                company_id=company_id,
                negotiation_id=int(negotiation.get("id")) if negotiation.get("id") else None,
            )
            reply = str(result.get("reply") or result.get("message") or "").strip()
            if not reply:
                raise RuntimeError("AI returned no negotiation reply")
        except Exception:
            if actor == "client":
                reply = "Принял ваше сообщение. Уточните, пожалуйста, желаемую цену и удобное время."
            else:
                reply = "Принял предложение. Подтвердите, пожалуйста, итоговую цену и условия."

        if insert_ai_msg:
            insert_ai_msg(negotiation["id"], "ai", reply, {"action": "continue"})
        if update_neg:
            update_neg(negotiation["id"], state, "active")

        return {"ok": True, "reply_text": reply, "status": "active", "state": state}

    # Compatibility method retained for older callers that only need a client reply.
    def reply_to_client(self, client_id: int, user_message: str, current_context: dict) -> dict:
        """Synchronous compatibility wrapper; does not access a legacy Supabase API."""
        state = dict(current_context or {})
        price = self._extract_price(user_message)
        if price is not None:
            state["agreed_price"] = price
        response = self.ai_service.process_text_request(
            user_text=user_message,
            role="client",
            system_prompt=self.generate_system_prompt(state),
        )
        return {
            "reply_text": str(response).strip(),
            "updated_context": state,
            "action_required": False,
            "booking_data": None,
        }
