"""AI helper for structuring potential partners found by the AI research flow.

Uses the current PostgreSQL-backed DatabaseManager and the shared AIService.
No legacy Supabase table-client API is used here.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ai_service import AIService
from database import DatabaseManager


class PotentialPartnerAI:
    def __init__(self):
        self.ai_service = AIService()
        self.db = DatabaseManager()

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
            ai_response = self.ai_service.process_text_request(
                user_text=raw_internet_text,
                role="admin",
                system_prompt=system_prompt,
            )
            clean_json = str(ai_response).replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_json)
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
            invite_text = self.ai_service.process_text_request(
                user_text=user_context,
                role="admin",
                system_prompt=system_prompt,
            )
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
