"""Conversation-first AI orchestration for Armenia AI Guide.

This module keeps the AI role-aware and database-backed. It deliberately does
not invent partners, prices, approvals or availability: it asks for missing
data and delegates transactional operations to the existing marketplace APIs.
"""
from __future__ import annotations

import json
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
        return (
            "You are the partner's AI right hand. The partner may describe a business "
            "freely in Armenian, Russian or English. Understand the message, extract "
            "business name, description, direction, subcategories, services, prices, "
            "locations/coverage, objects and schedule when present. Ask only for data "
            "that is genuinely missing. Never tell the partner that a direction does "
            "not exist merely because it is absent from the catalog. If it is new, "
            "structure it as a proposed catalog direction for admin review."
        )

    @staticmethod
    def _client_prompt() -> str:
        return (
            "You are the client's AI concierge. Understand what service the client "
            "needs, where, when, budget and important details. Ask only for missing "
            "information. Search results and availability must come from the platform "
            "database; never invent a partner, price or free slot. Once a real option "
            "is selected, guide the user through negotiation, agreement and payment."
        )

    @staticmethod
    def _admin_prompt() -> str:
        return (
            "You are the administrator's AI right hand. Summarize real platform events, "
            "partner applications, documents and catalog proposals. Clearly separate "
            "facts from AI suggestions. The administrator makes the final approval or "
            "rejection decision. Never claim that an approval happened unless the database "
            "confirms it."
        )

    async def reply(self, user_id: int, role: str, text: str) -> str:
        user = self.db.get_user(user_id) or {}
        role = role if role in {"client", "master", "admin"} else "client"
        prompt = {
            "master": self._partner_prompt(),
            "client": self._client_prompt(),
            "admin": self._admin_prompt(),
        }[role]
        try:
            return self.ai.process_text_request(text, role, prompt)
        except Exception as exc:
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

    def structured_partner_data(self, text: str) -> dict[str, Any]:
        """Use deterministic extraction when available; AI text remains conversational."""
        try:
            from partner_registration_ai import extract
            result = extract(self.db, text) if callable(extract) else {}
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
