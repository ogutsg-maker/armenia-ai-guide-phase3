"""Background AI analyzer for marketplace negotiations.

Client and partner messages are persisted as direct messages. AI is never a
third participant and never generates user-visible negotiation replies.
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
    def _extract_price_terms(text: str) -> dict[str, float | None]:
        raw = str(text or "")
        values: list[float] = []
        for match in re.findall(r"(?<!\d)(\d{1,3}(?:[ ,]\d{3})+|\d{3,7})(?:\s*(?:AMD|դրամ|դր))?", raw, re.I):
            try:
                value = float(match.replace(" ", "").replace(",", ""))
            except ValueError:
                continue
            if 100 <= value <= 10_000_000:
                values.append(value)
        values = list(dict.fromkeys(values))
        if len(values) >= 2:
            return {"min": min(values), "max": max(values), "single": None}
        if values:
            return {"min": values[0], "max": values[0], "single": values[0]}
        return {"min": None, "max": None, "single": None}

    @staticmethod
    def _contains_agreement(text: str) -> bool:
        low = str(text or "").casefold()
        words = (
            "согласен", "согласна", "подходит", "готов", "готова", "заказываю",
            "договорились", "беру", "ок", "да", "համաձայն եմ", "համաձայն եմ", "լավ",
            "կհամաձայնեմ",
        )
        return any(word in low for word in words)

    @staticmethod
    def _contains_refusal(text: str) -> bool:
        low = str(text or "").casefold()
        return any(x in low for x in ("отказываюсь", "не подходит", "отмена", "отменяю", "չեմ ուզում"))

    @staticmethod
    def _commission_base(state: dict) -> float | None:
        agreed_price = state.get("agreed_price")
        if agreed_price is not None:
            try:
                return float(agreed_price)
            except (TypeError, ValueError):
                return None
        lo, hi = state.get("agreed_min"), state.get("agreed_max")
        if lo is not None and hi is not None:
            try:
                return round((float(lo) + float(hi)) / 2.0, 2)
            except (TypeError, ValueError):
                return None
        return None

    async def _background_extract(self, text: str, state: dict) -> dict:
        """Extract negotiation facts only. The result is not shown as a message."""
        try:
            data = self.ai_service.structured_request(
                user_text=text,
                role="client",
                system_prompt=(
                    "Ты — фоновый анализатор переговоров маркетплейса услуг. "
                    "Не отвечай участникам и не пиши реплики от своего имени. "
                    "Извлеки только факты из сообщения. Не придумывай отсутствующие данные. "
                    "Определи, есть ли явное принятие текущих условий, отказ, предложенная цена, "
                    "диапазон цены, дата, время, город/место. Верни только JSON."
                ),
                schema={
                    "acceptance": "boolean",
                    "refusal": "boolean",
                    "price_min": "number|null",
                    "price_max": "number|null",
                    "date": "string|null",
                    "time": "string|null",
                    "place": "string|null",
                },
                operation="negotiation_background_extraction",
                purpose="Extract structured terms from direct client-partner negotiation",
            )
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

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
        """Persist a direct message and analyze it in the background."""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "message_required"}

        if actor not in {"client", "partner"}:
            return {"ok": False, "error": "invalid_actor"}

        state = self._state(negotiation)
        state.setdefault("client_accepted", False)
        state.setdefault("partner_accepted", False)
        state.setdefault("proposals", [])

        if insert_msg:
            insert_msg(negotiation["id"], actor, sender_id, text)

        price = self._extract_price_terms(text)
        if price["min"] is not None:
            proposal = {
                "actor": actor,
                "min": price["min"],
                "max": price["max"],
            }
            state["proposals"].append(proposal)
            state["proposed_price_min"] = price["min"]
            state["proposed_price_max"] = price["max"]
            state["proposed_price"] = price["single"]
            state["client_accepted"] = False
            state["partner_accepted"] = False

        extracted = await self._background_extract(text, state)
        if extracted:
            state["last_extraction"] = extracted
            if extracted.get("price_min") is not None:
                state["proposed_price_min"] = float(extracted["price_min"])
            if extracted.get("price_max") is not None:
                state["proposed_price_max"] = float(extracted["price_max"])
            if extracted.get("date"):
                state["proposed_date"] = str(extracted["date"])
            if extracted.get("time"):
                state["proposed_time"] = str(extracted["time"])
            if extracted.get("place"):
                state["proposed_place"] = str(extracted["place"])

        accepted = bool(extracted.get("acceptance")) if extracted else self._contains_agreement(text)
        refused = bool(extracted.get("refusal")) if extracted else self._contains_refusal(text)

        if refused:
            state["last_action"] = "refused"
            if update_neg:
                update_neg(negotiation["id"], state, "cancelled")
            if update_request and negotiation.get("request_id"):
                update_request(negotiation["request_id"], "cancelled")
            return {"ok": True, "status": "cancelled", "state": state}

        if accepted:
            state["client_accepted" if actor == "client" else "partner_accepted"] = True

        both_agreed = bool(state.get("client_accepted") and state.get("partner_accepted"))
        if both_agreed:
            pmin = state.get("proposed_price_min")
            pmax = state.get("proposed_price_max")
            if pmin is not None and pmax is not None and float(pmin) != float(pmax):
                state["agreed_min"] = float(min(pmin, pmax))
                state["agreed_max"] = float(max(pmin, pmax))
                state["agreed_price"] = None
            elif pmin is not None:
                state["agreed_price"] = float(pmin)
                state["agreed_min"] = float(pmin)
                state["agreed_max"] = float(pmin)
            state["commission_base"] = self._commission_base(state)
            state["last_action"] = "agreed"
            if update_neg:
                update_neg(negotiation["id"], state, "agreed")
            if update_request and negotiation.get("request_id"):
                update_request(negotiation["request_id"], "confirmed")
            return {"ok": True, "status": "agreed", "state": state}

        if update_neg:
            update_neg(negotiation["id"], state, "active")

        return {"ok": True, "status": "active", "state": state}

    def reply_to_client(self, client_id: int, user_message: str, current_context: dict) -> dict:
        """Compatibility wrapper retained without creating a visible AI participant."""
        state = dict(current_context or {})
        price = self._extract_price_terms(user_message)
        if price["min"] is not None:
            state["proposed_price_min"] = price["min"]
            state["proposed_price_max"] = price["max"]
        return {"reply_text": "", "updated_context": state, "action_required": False, "booking_data": None}
