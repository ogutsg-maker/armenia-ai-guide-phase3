"""Conversation-first AI orchestration for Armenia AI Guide."""
from __future__ import annotations

import logging
from typing import Any

from ai_service import AIService
from database import DatabaseManager

logger = logging.getLogger(__name__)


class AIRightHand:
    def __init__(self, db: DatabaseManager, ai: AIService):
        self.db = db
        self.ai = ai

    @staticmethod
    def _lang(user: dict[str, Any] | None) -> str:
        return str((user or {}).get("lang") or "hy").lower()

    @staticmethod
    def _partner_prompt() -> str:
        return ("You are the partner's AI right hand. Understand Armenian, Russian and English. "
                "Extract business name, description, direction, subcategories, services, prices, "
                "locations/coverage, objects and schedule when present. Ask only for genuinely "
                "missing data. Never say a direction does not exist merely because it is absent "
                "from the catalog; structure new directions for admin review.")

    @staticmethod
    def _client_prompt() -> str:
        return ("You are the client's AI concierge. Understand service, location, time, budget and "
                "details. Ask only for missing information. Search results, availability, prices "
                "and partners must come from the platform database. Never invent them. Guide the "
                "client through selection, negotiation, agreement and payment using real workflow.")

    @staticmethod
    def _admin_prompt() -> str:
        return ("You are the administrator's AI right hand. Summarize real platform events, partner "
                "applications, documents and catalog proposals. Separate facts from suggestions. "
                "The administrator makes final approval/rejection decisions.")

    async def reply(self, user_id: int, role: str, text: str) -> str:
        user = self.db.get_user(user_id) or {}
        role = role if role in {"client", "master", "admin"} else "client"
        prompt = {"master": self._partner_prompt(), "client": self._client_prompt(), "admin": self._admin_prompt()}[role]
        try:
            return self.ai.process_text_request(text, role, prompt)
        except Exception:
            logger.exception("AI right hand failed for user %s", user_id)
            lang = self._lang(user)
            if lang == "hy":
                return "Չկարողացա հիմա կապվել AI ծառայության հետ։ Խնդրում եմ կրկին ուղարկեք հաղորդագրությունը։"
            if lang == "en":
                return "I could not reach the AI service right now. Please send the message again."
            return "Сейчас не удалось связаться с AI. Пожалуйста, отправьте сообщение ещё раз."

    async def partner_reply(self, user_id: int, text: str) -> str:
        return await self.reply(user_id, "master", text)

    async def client_reply(self, user_id: int, text: str) -> str:
        return await self.reply(user_id, "client", text)

    async def admin_reply(self, user_id: int, text: str) -> str:
        return await self.reply(user_id, "admin", text)

    async def extract_partner_profile(self, text: str, history: list[dict] | None = None, previous_profile: dict | None = None, pending_field: str | None = None) -> dict[str, Any]:
        try:
            from partner_registration_ai import extract
            result = await extract(text, history or [], self.db, previous_profile or {}, pending_field)
            return result if isinstance(result, dict) else {}
        except Exception:
            logger.exception("Partner profile extraction failed")
            return {}
