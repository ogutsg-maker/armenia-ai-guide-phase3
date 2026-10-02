"""AI helper for structuring potential partners found by the AI research flow.

Uses the current PostgreSQL-backed DatabaseManager and the shared AIService.
No legacy Supabase table-client API is used here.
"""
from __future__ import annotations

import os
import re
from typing import Any

from ai_service import AIService


class PotentialPartnerAI:
    def __init__(self):
        self.ai_service = AIService()

    @staticmethod
    def _setting(name: str, default: str) -> str:
        """Read optional notification settings without requiring a legacy settings API."""
        return os.getenv(name.upper(), default)

    def analyze_and_structure_lead(self, raw_internet_text: str) -> dict:
        """Extract a structured potential-partner record from raw internet text."""
        system_prompt = (
            "Ты — аналитик B2B-лидов на рынке услуг Армении. "
            "Извлеки из сырого текста информацию о потенциальном партнере.\n\n"
            "Верни СТРОГО JSON со следующими полями; если данных нет, используй null:\n"
            "- name — имя мастера или название компании\n"
            "- services — массив конкретных услуг\n"
            "- city — город\n"
            "- district — район Еревана, если указан\n"
            "- min_price — минимальная цена числом в AMD, если указана\n"
            "- working_hours — график, если указан\n\n"
            "Не добавляй пояснения или markdown. Только JSON."
        )

        try:
            data = self.ai_service.structured_request(
                user_text=raw_internet_text,
                role="admin",
                system_prompt=system_prompt,
                schema={
                    "name": "string|null",
                    "services": ["string"],
                    "city": "string|null",
                    "district": "string|null",
                    "min_price": "number|null",
                    "working_hours": "string|null",
                },
                operation="potential_partner_extract",
                purpose="Extract a structured potential partner lead",
            )
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        return {
            "name": None,
            "services": [],
            "city": None,
            "district": None,
            "min_price": None,
            "working_hours": None,
        }

    async def structure_candidate(self, raw_text: str, source: str = "manual_research") -> dict | None:
        """Return structured candidate data; persistence stays in Data Core."""
        structured = self.analyze_and_structure_lead(raw_text)
        name = str(structured.get("name") or "").strip()
        if not name:
            return None
        services = structured.get("services") or []
        min_price = structured.get("min_price")
        prices = [{"type": "from", "amount": min_price, "currency": "AMD"}] if min_price is not None else []
        urls = [x.strip() for x in re.findall(r"https?://[^\\s]+", raw_text)][:10]
        return {
            "source": source,
            "business_name": name,
            "description": str(raw_text or "")[:4000],
            "city": structured.get("city"),
            "services": services,
            "prices": prices,
            "source_urls": urls,
            "ai_reason": "Structured from researched source text; requires admin review before invitation.",
            "ai_confidence": 0.8 if services else 0.6,
            "status": "ready_for_review",
        }

    def generate_personalized_invite(self, structured_partner_data: dict) -> dict:
        """Generate an invitation while respecting optional channel settings."""
        active_channel = self._setting("ACTIVE_NOTIFICATION_CHANNEL", "telegram").lower()
        tg_enabled = self._setting("ENABLE_TELEGRAM_CHANNEL", "true").lower() == "true"
        wa_enabled = self._setting("ENABLE_WHATSAPP_CHANNEL", "true").lower() == "true"
        sms_enabled = self._setting("ENABLE_SMS_CHANNEL", "false").lower() == "true"

        final_channel = "manual"
        if active_channel == "telegram" and tg_enabled:
            final_channel = "telegram"
        elif active_channel == "whatsapp" and wa_enabled:
            final_channel = "whatsapp"
        elif active_channel == "sms" and sms_enabled:
            final_channel = "sms"
        elif tg_enabled:
            final_channel = "telegram"
        elif wa_enabled:
            final_channel = "whatsapp"
        elif sms_enabled:
            final_channel = "sms"

        services = structured_partner_data.get("services", [])
        service_names: list[str] = []
        for item in services:
            if isinstance(item, dict):
                name = item.get("name")
            else:
                name = item
            if name:
                service_names.append(str(name))
        services_str = ", ".join(service_names) if service_names else "услуги специалиста"

        district = structured_partner_data.get("district")
        district_info = f" в районе {district}" if district else ""
        min_price = structured_partner_data.get("min_price")
        price_info = f" с ценами от {min_price} AMD" if min_price else ""

        user_context = (
            f"Специалист оказывает: {services_str}{district_info}{price_info}. "
            f"Канал сообщения: {final_channel}."
        )

        system_prompt = (
            "Ты — профессиональный B2B-копирайтер маркетплейса услуг в Армении. "
            "Напиши дружелюбное, уважительное и не спамное приглашение потенциальному партнеру.\n\n"
            "Для telegram/whatsapp: короткие абзацы и уместные эмодзи, в конце [ССЫЛКА_НА_КАБИНЕТ].\n"
            "Для sms: не более 140 символов, только суть и [ССЫЛКА].\n"
            "Для manual: теплое универсальное сообщение со ссылкой [ИНВАЙТ].\n"
            "Пиши на чистом русском языке. Не выдумывай конкретные факты о клиентских заказах."
        )

        try:
            invite_data = self.ai_service.structured_request(
                user_text=user_context,
                role="admin",
                system_prompt=system_prompt,
                schema={"invite_text": "string"},
                operation="potential_partner_invite",
                purpose="Generate a partner invitation message",
            )
            invite_text = str(invite_data.get("invite_text") or "").strip()
            if not invite_text:
                raise RuntimeError("empty structured invitation")
        except Exception:
            invite_text = (
                "Здравствуйте! Мы развиваем Armenia AI Guide — маркетплейс услуг в Армении. "
                "Приглашаем вас присоединиться как партнёра. [ИНВАЙТ]"
            )

        return {
            "invite_text": invite_text,
            "delivery_method": final_channel,
            "is_auto_send": final_channel != "manual",
        }
