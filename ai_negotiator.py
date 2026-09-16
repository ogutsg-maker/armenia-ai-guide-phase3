"""AINegotiator — Phase 2 structured negotiation module.

Handles client/partner messages inside a negotiation, mirrors them into the
shared AI memory (ai_sessions/ai_messages), uses GroqAI.negotiate() for
structured intent+price extraction, and updates state_json based on intent
rather than keyword matching.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _state(negotiation: dict) -> dict:
    """Parse state_json from a negotiation row."""
    s = negotiation.get("state_json") or {}
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except Exception:
            s = {}
    return s


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


class AINegotiator:
    """Structured AI mediator for client<->partner negotiation."""

    def __init__(self, ai):
        self.ai = ai

    async def handle(
        self,
        negotiation: dict,
        sender_role: str,
        sender_id: int,
        text: str,
        *,
        insert_msg=None,
        update_neg=None,
        update_request=None,
        insert_ai_msg=None,
        get_recent=None,
    ) -> dict:
        """Process one message in a negotiation.

        Parameters
        ----------
        negotiation : dict
            Full row from the ``negotiations`` table.
        sender_role : str
            ``'client'`` or ``'partner'``.
        sender_id : int
            Telegram user id of the sender.
        text : str
            Raw message text.
        insert_msg, update_neg, update_request, insert_ai_msg, get_recent :
            DB hooks (callables) so we stay decoupled from the raw SQL layer.

        Returns
        -------
        dict  ``{reply, state, status, ai_result}``
        """
        nid = negotiation["id"]
        st = _state(negotiation)

        # 1) Persist user message in negotiation_messages
        if insert_msg:
            insert_msg(nid, sender_role, sender_id, text)

        # 2) Mirror into shared AI memory
        from platform_db import (
            active_session, create_session, add_ai_message,
            update_session, recent_ai_messages,
        )
        user_id = negotiation.get("client_id") if sender_role == "client" else negotiation.get("partner_id")
        # Use client_id as the session user for simplicity
        session_owner = negotiation.get("client_id", sender_id)
        session = active_session(session_owner, "client", "negotiation") or create_session(
            session_owner, "client", "negotiation",
            {"negotiation_id": nid},
        )
        add_ai_message(session["id"], sender_role, text)
        history = recent_ai_messages(session["id"], 16) if get_recent is None else get_recent(session["id"], 16)

        # 3) Call structured negotiate()
        payload = {
            "service_name": st.get("service_name", ""),
            "current_price": _safe_float(st.get("price")),
            "currency": st.get("currency", "AMD"),
            "sender_role": sender_role,
            "message": text,
            "history": [f"{m.get('sender_role','?')}: {m.get('message_text','')}" for m in (history or [])],
        }
        ai_result = await self.ai.negotiate(payload)
        intent = ai_result.get("intent", "chitchat")
        proposed_price = ai_result.get("proposed_price")
        agreed = ai_result.get("agreed", False)
        reply = ai_result.get("reply", "")
        summary = ai_result.get("summary", "")

        # 4) Update state_json based on structured intent
        if proposed_price is not None and proposed_price > 0:
            st["last_proposed_price"] = proposed_price
            st["proposed_by"] = sender_role
        if intent == "agree" and agreed:
            st[f"{sender_role}_agreed"] = True
        elif intent == "counter" and proposed_price is not None:
            st["price"] = proposed_price
            st[f"{sender_role}_agreed"] = False  # counter means not yet agreed
        elif intent == "reject":
            st[f"{sender_role}_agreed"] = False

        both_agreed = bool(st.get("client_agreed")) and bool(st.get("partner_agreed"))
        neg_status = "active"
        req_status = None
        if both_agreed:
            neg_status = "agreed"
            req_status = "confirmed"

        if update_neg:
            update_neg(nid, st, neg_status)
        if both_agreed and update_request and negotiation.get("request_id"):
            update_request(negotiation["request_id"], req_status)

        # 5) Persist AI reply
        ai_text = reply or self._default_reply(intent, st, sender_role)
        add_ai_message(session["id"], "ai", ai_text, {"intent": intent, "negotiation_id": nid})
        if insert_ai_msg:
            insert_ai_msg(nid, "ai", ai_text, {"intent": intent})

        update_session(session["id"], {"negotiation_id": nid, "last_intent": intent})

        return {
            "reply": ai_text,
            "state": st,
            "status": neg_status,
            "ai_result": ai_result,
        }

    @staticmethod
    def _default_reply(intent: str, st: dict, sender_role: str) -> str:
        """Fallback neutral reply when AI returned empty text."""
        price = st.get("price", "?")
        ccy = st.get("currency", "AMD")
        svc = st.get("service_name", "услуга")
        if intent == "agree":
            if st.get("client_agreed") and st.get("partner_agreed"):
                return f"Обе стороны согласны на {price} {ccy}. Переходим к оплате."
            other = "клиент" if sender_role == "partner" else "партнёр"
            return f"Ваше согласие принято. Ждём подтверждения от {other}."
        if intent == "counter":
            return f"Встречное предложение по «{svc}» передано."
        if intent == "reject":
            return "Отказ принят. Переговоры можно продолжить, предложив другие условия."
        if intent == "question":
            return "Ваш вопрос передан. Постараюсь уточнить."
        return "Я здесь, чтобы помочь с переговорами. Напишите цену или условие."
