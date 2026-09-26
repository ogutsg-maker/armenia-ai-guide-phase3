"""Unified AI service for Armenia AI Guide.

Groq is the primary provider. OpenAI is an optional fallback. This module
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

from groq import Groq
from psycopg.rows import dict_row

from database import _connect
import ai_cost_center


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
        self.groq_key=os.getenv("GROQ_API_KEY","").strip()
        self.groq_model=os.getenv("GROQ_MODEL","openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
        self.groq_fallback_model=os.getenv("GROQ_FALLBACK_MODEL","").strip()
        self.groq_vision_model=os.getenv("GROQ_VISION_MODEL","meta-llama/llama-4-scout-17b-16e-instruct").strip()
        self.groq_transcription_model=os.getenv("GROQ_TRANSCRIPTION_MODEL","whisper-large-v3-turbo").strip()
        self.groq_client=Groq(api_key=self.groq_key) if self.groq_key else None

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

    def _groq_completion_json(self, messages, model: str, max_tokens: int):
        if not self.groq_client: raise RuntimeError("GROQ_API_KEY is not configured")
        kwargs={"model":model,"messages":messages,"temperature":0,"max_tokens":max_tokens,"response_format":{"type":"json_object"}}
        try:
            return self.groq_client.chat.completions.create(**kwargs), model
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404 and self.groq_fallback_model and model != self.groq_fallback_model:
                kwargs["model"]=self.groq_fallback_model
                return self.groq_client.chat.completions.create(**kwargs), self.groq_fallback_model
            raise

    async def chat_json(self, system_prompt: str, user_text: str, *, max_tokens: int = 900, chain: str = "unknown", stage: str = "unknown", operation: str = "chat_json", purpose: str = "", user_id: int | None = None, partner_id: int | None = None, company_id: int | None = None, order_id: int | None = None, negotiation_id: int | None = None) -> dict:
        """Structured JSON through Groq only."""
        clean=self.clean_sensitive_data(user_text)
        if not self.groq_client: raise RuntimeError("GROQ_API_KEY is not configured")
        response,model=await asyncio.to_thread(self._groq_completion_json,[{"role":"system","content":system_prompt},{"role":"user","content":clean}],self.groq_model,max_tokens)
        usage=getattr(response,"usage",None)
        ai_cost_center.record_usage(provider="groq",model=model,chain=chain,stage=stage,operation=operation,purpose=purpose,user_id=user_id,partner_id=partner_id,company_id=company_id,order_id=order_id,negotiation_id=negotiation_id,input_tokens=int(getattr(usage,"prompt_tokens",getattr(usage,"input_tokens",0)) or 0),output_tokens=int(getattr(usage,"completion_tokens",getattr(usage,"output_tokens",0)) or 0),cached_tokens=int(getattr(getattr(usage,"prompt_tokens_details",None),"cached_tokens",0) or 0),reasoning_tokens=int(getattr(getattr(usage,"completion_tokens_details",None),"reasoning_tokens",0) or 0))
        data=self._json(self._text(response))
        if not data: raise RuntimeError("Groq returned invalid JSON")
        return data

    async def structured_json(self, system_prompt: str, user_text: str, *, schema_name: str = "response", schema: dict | None = None, max_tokens: int = 900, chain: str = "unknown", stage: str = "unknown", operation: str = "structured_json", purpose: str = "", partner_id: int | None = None, user_id: int | None = None, company_id: int | None = None, order_id: int | None = None, negotiation_id: int | None = None) -> dict:
        """Strict JSON through Groq only."""
        return await self.chat_json(system_prompt,user_text,max_tokens=max_tokens,chain=chain,stage=stage,operation=operation,purpose=purpose,partner_id=partner_id,user_id=user_id,company_id=company_id,order_id=order_id,negotiation_id=negotiation_id)

    async def _call_groq(self, system_prompt: str, user_text: str, json_mode: bool = False, model: str | None = None) -> str:
        clean=self.clean_sensitive_data(user_text)
        messages=[{"role":"system","content":system_prompt},{"role":"user","content":clean}]
        if not self.groq_client: raise RuntimeError("GROQ_API_KEY is not configured")
        selected=model or self.groq_model
        if json_mode:
            response,_=await asyncio.to_thread(self._groq_completion_json,messages,selected,900)
        else:
            response=await asyncio.to_thread(self._groq_completion,messages,selected)
        return self._text(response)

    async def route_message(self, text: str, role: str, context: dict | None = None) -> dict:
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
            data = await self.chat_json(
                system, prompt, max_tokens=250,
                chain="router", stage="intent", operation="route_message",
                purpose="Select the correct AI workflow",
            )
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
        if re.search(r"[А-Яа-яЁё]", text):
            return "ru"
        if re.search(r"[Ա-Ֆա-ֆևօՕև]", text):
            return "hy"
        return "en"

    async def analyze_request(self, user_text: str, categories: list[dict] | None = None) -> RequestAnalysis:
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
            data = await self.chat_json(
                system, prompt, max_tokens=700,
                chain="client_search", stage="request_analysis",
                operation="analyze_request",
                purpose="Extract service, location, budget and catalog IDs",
            )
        except Exception:
            data = {}
        def _int(v):
            try:
                return int(v) if v is not None else None
            except Exception:
                return None
        def _float(v):
            try:
                return float(v) if v is not None else None
            except Exception:
                return None
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

    def process_text_request(self,user_text: str,role: str,system_prompt: str) -> str:
        clean=self.clean_sensitive_data(user_text); role_model=self._get_setting(f"{role}_ai_model","").strip() or self.groq_model
        if role_model=="groq-llama3": role_model=self.groq_model
        response=self._groq_completion([{"role":"system","content":system_prompt+"\nUnderstand Armenian, Russian and English. Never invent marketplace facts."},{"role":"user","content":clean}],role_model)
        return self._text(response)

    def process_voice(self,audio_file_path: str) -> str:
        if self._get_setting("allow_voice_input","true").lower()=="false": return "🔒 Ձայնային մուտքը ժամանակավորապես անջատված է։"
        if not self.groq_client: return "Groq AI-ը հասանելի չէ։"
        try:
            with open(audio_file_path,"rb") as audio: response=self.groq_client.audio.transcriptions.create(model=self.groq_transcription_model,file=audio)
            return getattr(response,"text","") or ""
        except Exception as exc: return f"Ошибка распознавания аудио через Groq: {exc}"

    def process_image_price(self,image_url: str) -> str:
        if self._get_setting("allow_image_input","true").lower()=="false": return "🔒 Վերբեռնումը ժամանակավորապես անջատված է։"
        if not self.groq_client: return "Groq AI-ը հասանելի չէ։"
        try:
            response=self.groq_client.chat.completions.create(model=self.groq_vision_model,messages=[{"role":"user","content":[{"type":"text","text":"Прочитай прайс-лист. Верни услуги, цены и валюту. Не придумывай отсутствующие данные."},{"type":"image_url","image_url":{"url":image_url}}]}],max_tokens=1500)
            return self._text(response)
        except Exception as exc: return f"Ошибка анализа изображения через Groq: {exc}"

