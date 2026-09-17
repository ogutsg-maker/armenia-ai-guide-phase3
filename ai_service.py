"""Unified AI service for Groq-primary + OpenAI fallback.

The rest of Armenia AI Guide talks only to this class. Groq is the primary
provider; OpenAI is an automatic fallback when configured. Voice/image
features use OpenAI when available.
"""
from __future__ import annotations

import os
import re
from typing import Any

from openai import OpenAI
from groq import Groq
from psycopg.rows import dict_row

from database import _connect


class AIService:
    def __init__(self):
        self.openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()

        self.groq_model = (
            os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
            or "llama-3.3-70b-versatile"
        )
        self.groq_fallback_model = (
            os.getenv("GROQ_FALLBACK_MODEL", "llama-3.3-70b-versatile").strip()
            or "llama-3.3-70b-versatile"
        )
        self.openai_model = (
            os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
            or "gpt-4o-mini"
        )
        self.provider = os.getenv("AI_PROVIDER", "groq").strip().lower() or "groq"

        self.openai_client = OpenAI(api_key=self.openai_key) if self.openai_key else None
        self.groq_client = Groq(api_key=self.groq_key) if self.groq_key else None

    def _get_setting(self, key: str, default: str) -> str:
        try:
            with _connect() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT value_json FROM admin_settings WHERE key=%s", (key,))
                    row = cur.fetchone()
                    if row and row.get("value_json") is not None:
                        value = row["value_json"]
                        if isinstance(value, dict):
                            value = value.get("value") or value.get("model")
                        if value:
                            return str(value)
        except Exception:
            pass
        return default

    def clean_sensitive_data(self, text: str) -> str:
        if self._get_setting("hide_contacts_before_payment", "true").lower() == "false":
            return text
        phone_pattern = r'(\+?\d{1,3}\s?\(?\d{2,3}\)?\s?\d{3,4}\s?\d{2,3}\s?\d{2,3}|\b0\d{2}\s?\d{3}\s?\d{3}\b|\b\d{2,3}[-\s]?\d{2,3}[-\s]?\d{2,3}\b)'
        contact_pattern = r'(@[A-Za-z0-9_]{4,})|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})|(https?://[^\s]+)'
        text = re.sub(phone_pattern, "[CONTACT_HIDDEN]", text)
        return re.sub(contact_pattern, "[CONTACT_HIDDEN]", text)

    @staticmethod
    def _text(response: Any) -> str:
        try:
            return (response.choices[0].message.content or "").strip()
        except Exception:
            return ""

    def _groq_completion(self, messages, model: str):
        if not self.groq_client:
            raise RuntimeError("GROQ_API_KEY is not configured")
        try:
            return self.groq_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
            )
        except Exception as first_error:
            status = getattr(first_error, "status_code", None)
            if status == 404 and self.groq_fallback_model and model != self.groq_fallback_model:
                return self.groq_client.chat.completions.create(
                    model=self.groq_fallback_model,
                    messages=messages,
                    temperature=0.2,
                )
            raise

    def _openai_completion(self, messages, model: str | None = None):
        if not self.openai_client:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        return self.openai_client.chat.completions.create(
            model=model or self.openai_model,
            messages=messages,
            temperature=0.2,
        )

    def process_text_request(self, user_text: str, role: str, system_prompt: str) -> str:
        clean_user_text = self.clean_sensitive_data(user_text)
        role_model = self._get_setting(f"{role}_ai_model", "").strip()
        groq_model = role_model or self.groq_model
        openai_model = self.openai_model
        full_system_instruction = (
            f"{system_prompt}\n\n"
            "You are part of Armenia AI Guide, an AI-first marketplace in Armenia. "
            "Understand Armenian, Russian, English and mixed-language messages. "
            "Prices are AMD. Never invent partners, availability, prices, payments or approvals. "
            "If data is missing, ask only for the missing information."
        )
        messages = [
            {"role": "system", "content": full_system_instruction},
            {"role": "user", "content": clean_user_text},
        ]

        errors: list[str] = []
        primary = self.provider
        if primary not in {"groq", "openai"}:
            primary = "groq"

        providers = [primary, "openai" if primary == "groq" else "groq"]
        for provider in providers:
            try:
                if provider == "groq" and self.groq_client:
                    return self._text(self._groq_completion(messages, groq_model))
                if provider == "openai" and self.openai_client:
                    return self._text(self._openai_completion(messages, openai_model))
            except Exception as exc:
                errors.append(f"{provider}: {exc}")

        if errors:
            raise RuntimeError("AI providers failed: " + " | ".join(errors)[-1200:])
        raise RuntimeError("No AI provider is configured")

    def process_voice(self, audio_file_path: str) -> str:
        if self._get_setting("allow_voice_input", "true").lower() == "false":
            return "🔒 Голосовой ввод временно отключен администратором."
        if not self.openai_client:
            return "Голосовой ввод требует OPENAI_API_KEY."
        try:
            with open(audio_file_path, "rb") as audio:
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio,
                )
            return transcript.text or ""
        except Exception as exc:
            return f"Ошибка распознавания аудио: {exc}"

    def process_image_price(self, image_url: str) -> str:
        if self._get_setting("allow_image_input", "true").lower() == "false":
            return "🔒 Загрузка изображений для ИИ отключена администратором."
        if not self.openai_client:
            return "Анализ изображений требует OPENAI_API_KEY."
        try:
            response = self.openai_client.chat.completions.create(
                model=self.openai_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Прочитай прайс-лист. Верни услуги, цены и валюту. Не придумывай отсутствующие данные."},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }],
                max_tokens=1500,
            )
            return self._text(response)
        except Exception as exc:
            return f"Ошибка анализа изображения: {exc}"


GroqAI = AIService
