"""Compatibility facade for the unified partner AI.

All live partner AI interactions go through AIManager/ToolRegistry.
This module remains only for legacy imports and contains no direct database
mutation logic or second AI/action pipeline.
"""
from __future__ import annotations

from typing import Any

from ai_manager import AIContext, AIManager


class PartnerAI:
    """Backward-compatible wrapper around the unified AIManager."""

    def __init__(self, ai: AIManager | None = None):
        self.ai = ai or AIManager()

    async def process(self, user_id: int, text: str, lang: str = "hy") -> dict[str, Any]:
        result = await self.ai.handle_message(
            int(user_id),
            str(text or "").strip(),
            AIContext.PARTNER,
            language=lang,
        )
        return {
            "reply": result.get("reply") or "",
            "context": result.get("context") or {},
            "raw": result,
        }

    async def initial_message(self, user_id: int, lang: str = "hy") -> str:
        messages = {
            "hy": "Բարև 👋 Ես ձեր AI օգնականն եմ։ Պատմեք ձեր բիզնեսի, ծառայությունների և գների մասին։",
            "ru": "Здравствуйте 👋 Я ваш AI-помощник. Расскажите о бизнесе, услугах и ценах.",
            "en": "Hello 👋 I am your AI assistant. Tell me about your business, services and prices.",
        }
        return messages.get(lang, messages["hy"])
