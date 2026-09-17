"""Unified AI service for Armenia AI Guide.

Groq is the primary provider. OpenAI is an optional fallback.  This module
also owns the structured contracts used by the router, client search and
partner onboarding so the individual AI modules do not invent their own
provider APIs.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Any

from openai import OpenAI
from groq import Groq
from psycopg.rows import dict_row

from database import _connect


@dataclass
class RequestAnalysis:
    language: str = "hy"
    intent: str = "service_search"
    master_category_id: int | None = None
    category_id: int | None = None
    category_name: str | None = None
    service: str | None = None
    location: str | None = None
    marz: str | None = None
    city: str | None = None
    village: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    date: str | None = None
    time: str | None = None
    requirements: list[str] | None = None
    missing_fields: list[str] | None = None
    checklist: list[str] | None = None
    summary: str = ""
    confidence: float = 0.0

    def model_dump(self) -> dict:
        return asdict(self)


class AIService:
    def __init__(self):
        self.openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()
        self.groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"
        self.groq_fallback_model = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
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
                        if value is not None:
                            return str(value)
        except Exception:
            pass
        return default

    def clean_sensitive_data(self, text: str) -> str:
        if self._get_setting("hide_contacts_before_payment", "true").lower() == "false":
            return text
        phone_pattern = r'(\+?\d{1,3}\s?\(?\d{2,3}\)?\s?\d{3,4}\s?\d{2,3}\s?\d{2,3}|\b0\d{2}\s?\d{3}\s?\d{3}\b)'
        contact_pattern = r'(@[A-Za-z0-9_]{4,})|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})|(https?://[^\s]+)'
        text = re.sub(phone_pattern, "[CONTACT_HIDDEN]", text)
        return re.sub(contact_pattern, "[CONTACT_HIDDEN]", text)

    @staticmethod
    def _text(response: Any) -> str:
        try:
            return (response.choices[0].message.content or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _json(text: str) -> dict:
        text = (text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
            text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except Exception:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return {}
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except Exception:
                return {}

    def _groq_completion(self, messages, model: str):
        if not self.groq_client:
            raise RuntimeError("GROQ_API_KEY is not configured")
        try:
            return self.groq_client.chat.completions.create(model=model, messages=messages, temperature=0.2)
        except Exception as first_error:
            if getattr(first_error, "status_code", None) == 404 and self.groq_fallback_model and model != self.groq_fallback_model:
                return self.groq_client.chat.completions.create(model=self.groq_fallback_model, messages=messages, temperature=0.2)
            raise

    def _openai_completion(self, messages, model: str | None = None):
        if not self.openai_client:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        return self.openai_client.chat.completions.create(model=model or self.openai_model, messages=messages, temperature=0.2)

    async def _call_groq(self, system_prompt: str, user_text: str, json_mode: bool = False, model: str | None = None) -> str:
        """Async provider call used by PartnerAI and the structured AI layer."""
        clean = self.clean_sensitive_data(user_text)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": clean},
        ]
        selected = model or self.groq_model
        try:
            if self.groq_client:
                response = await asyncio.to_thread(self._groq_completion, messages, selected)
                return self._text(response)
            if self.openai_client:
                response = await asyncio.to_thread(self._openai_completion, messages, self.openai_model)
                return self._text(response)
        except Exception:
            if self.openai_client and self.groq_client:
                response = await asyncio.to_thread(self._openai_completion, messages, self.openai_model)
                return self._text(response)
            raise
        raise RuntimeError("No AI provider is configured")

    async def route_message(self, text: str, role: str, context: dict | None = None) -> dict:
        """Classify a message into a stable application module."""
        context = context or {}
        system = """You are the routing layer of Armenia AI Guide.
Classify the user's message. Return ONLY JSON:
{"module":"client_search|partner_onboarding|negotiation|support|smalltalk","confidence":0.0,"language":"hy|ru|en"}
Rules:
- partner role normally uses partner_onboarding only when the message is about the partner's business, services, prices, documents, profile or catalog.
- A partner discussing an existing booking/order/price negotiation is negotiation.
- A client asking to find a service/provider is client_search.
- Greetings alone are smalltalk.
- Technical/account/help questions are support.
Never invent a module outside the list."""
        prompt = f"role={role}\ncontext={json.dumps(context, ensure_ascii=False)}\nmessage={text}"
        try:
            data = self._json(await self._call_groq(system, prompt, True))
        except Exception:
            data = {}
        module = data.get("module") if data.get("module") in {"client_search", "partner_onboarding", "negotiation", "support", "smalltalk"} else None
        if not module:
            low = text.lower()
            if any(x in low for x in ("торг", "цена", "скид", "գին", "զեղչ", "բանակց")) and context.get("active_negotiation_id"):
                module = "negotiation"
            elif role == "partner":
                module = "partner_onboarding"
            elif any(x in low for x in ("привет", "здравствуйте", "բարև", "hello", "hi")) and len(text.split()) <= 4:
                module = "smalltalk"
            else:
                module = "client_search"
        lang = data.get("language") if data.get("language") in {"hy", "ru", "en"} else self._detect_language(text)
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.75))))
        except Exception:
            confidence = 0.75
        return {"module": module, "confidence": confidence, "language": lang}

    def _detect_language(self, text: str) -> str:
        if re.search(r"[А-Яа-яЁё]", text): return "ru"
        if re.search(r"[Ա-Ֆա-ֆևօՕև]", text): return "hy"
        return "en"

    async def analyze_request(self, user_text: str, categories: list[dict] | None = None) -> RequestAnalysis:
        """Turn a free-form client request into searchable structured fields."""
        catalog = categories or []
        compact_catalog = [
            {"id": c.get("id"), "master_category_id": c.get("master_category_id"), "name_am": c.get("name_am") or c.get("name_hy"), "name_ru": c.get("name_ru"), "name_en": c.get("name_en"), "slug": c.get("slug")}
            for c in catalog
        ]
        system = """You extract a service marketplace request for Armenia AI Guide.
Return ONLY JSON with these keys:
language,intent,master_category_id,category_id,category_name,service,location,marz,city,village,budget_min,budget_max,date,time,requirements,missing_fields,checklist,summary,confidence.
Match category_id/master_category_id ONLY to the supplied catalog IDs. If there is no reliable match, use null.
Do not invent a partner, service, price or availability. Location may be a city, village or marz.
missing_fields should contain only genuinely useful information needed for a better search. checklist contains short questions that can be asked next, maximum 3.
Prices are AMD. Preserve a user-provided budget exactly enough for filtering."""
        prompt = f"CATALOG={json.dumps(compact_catalog, ensure_ascii=False)}\nREQUEST={user_text}"
        try:
            data = self._json(await self._call_groq(system, prompt, True))
        except Exception:
            data = {}
        def _int(v):
            try: return int(v) if v is not None else None
            except Exception: return None
        def _float(v):
            try: return float(v) if v is not None else None
            except Exception: return None
        language = data.get("language") if data.get("language") in {"hy", "ru", "en"} else self._detect_language(user_text)
        return RequestAnalysis(
            language=language,
            intent=str(data.get("intent") or "service_search"),
            master_category_id=_int(data.get("master_category_id")),
            category_id=_int(data.get("category_id")),
            category_name=data.get("category_name"), service=data.get("service"),
            location=data.get("location"), marz=data.get("marz"), city=data.get("city"), village=data.get("village"),
            budget_min=_float(data.get("budget_min")), budget_max=_float(data.get("budget_max")),
            date=data.get("date"), time=data.get("time"),
            requirements=[str(x) for x in (data.get("requirements") or []) if x],
            missing_fields=[str(x) for x in (data.get("missing_fields") or []) if x],
            checklist=[str(x) for x in (data.get("checklist") or []) if x][:3],
            summary=str(data.get("summary") or user_text[:500]),
            confidence=max(0.0, min(1.0, _float(data.get("confidence")) or 0.0)),
        )

    def process_text_request(self, user_text: str, role: str, system_prompt: str) -> str:
        clean_user_text = self.clean_sensitive_data(user_text)
        role_model = self._get_setting(f"{role}_ai_model", "").strip()
        messages = [{"role": "system", "content": system_prompt + "\nUnderstand Armenian, Russian and English. Never invent marketplace facts."}, {"role": "user", "content": clean_user_text}]
        errors=[]
        for provider in ([self.provider, "openai" if self.provider == "groq" else "groq"]):
            try:
                if provider == "groq" and self.groq_client: return self._text(self._groq_completion(messages, role_model or self.groq_model))
                if provider == "openai" and self.openai_client: return self._text(self._openai_completion(messages, self.openai_model))
            except Exception as exc: errors.append(f"{provider}: {exc}")
        if errors: raise RuntimeError("AI providers failed: " + " | ".join(errors)[-1200:])
        raise RuntimeError("No AI provider is configured")

    def process_voice(self, audio_file_path: str) -> str:
        if self._get_setting("allow_voice_input", "true").lower() == "false": return "🔒 Голосовой ввод временно отключен администратором."
        if not self.openai_client: return "Голосовой ввод требует OPENAI_API_KEY."
        try:
            with open(audio_file_path, "rb") as audio:
                return self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio).text or ""
        except Exception as exc: return f"Ошибка распознавания аудио: {exc}"

    def process_image_price(self, image_url: str) -> str:
        if self._get_setting("allow_image_input", "true").lower() == "false": return "🔒 Загрузка изображений для ИИ отключена администратором."
        if not self.openai_client: return "Анализ изображений требует OPENAI_API_KEY."
        try:
            response = self.openai_client.chat.completions.create(model=self.openai_model, messages=[{"role":"user","content":[{"type":"text","text":"Прочитай прайс-лист. Верни услуги, цены и валюту. Не придумывай отсутствующие данные."},{"type":"image_url","image_url":{"url":image_url}}]}], max_tokens=1500)
            return self._text(response)
        except Exception as exc: return f"Ошибка анализа изображения: {exc}"


GroqAI = AIService
