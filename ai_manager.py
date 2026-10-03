"""Unified AI entry point for REGISTRATION, CLIENT, PARTNER and ADMIN.

AIManager is the only conversational AI gateway. Groq may request typed tools,
but tools are executed by backend code only. No system-role messages are used.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from groq import AsyncGroq
from openai import AsyncOpenAI

import ai_cost_center
import platform_db
from history_provider import HistoryProvider
from prompt_factory import ContextType, PromptFactory, as_context_type
from tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


class AIContext(Enum):
    REGISTRATION = "registration"
    CLIENT = "client"
    PARTNER = "partner"
    ADMIN = "admin"


_CONFIRMATIONS = {
    "yes", "y", "да", "da", "այո", "հա", "հաստատել", "հաստատում եմ",
    "подтверждаю", "подтвердить", "confirm", "ok", "okay", "հաստատ",
}
_CANCELS = {
    "no", "нет", "ոչ", "չեղարկել", "отмена", "отменить", "cancel",
}


class CategorySelectionRequest(BaseModel):
    """Strict Fast Path payload validation; business checks happen afterwards."""
    service_id: int = Field(gt=0)
    chosen_cat_id: int = Field(gt=0)



@dataclass
class SessionState:
    """Backend-owned conversational state persisted in platform_db.context_json."""
    last_displayed_entity_id: dict[str, Any] | None = None
    current_pagination_index: int = 0
    pending_action: dict[str, Any] | None = None
    current_list: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "SessionState":
        value = dict(value or {})
        last = value.get("last_displayed_entity_id")
        if last is not None and not isinstance(last, dict):
            last = {"type": value.get("current_entity_type") or "entity", "id": last}
        try:
            index = max(0, int(value.get("current_pagination_index", value.get("current_position", 0)) or 0))
        except (TypeError, ValueError):
            index = 0
        items = value.get("current_list")
        if not isinstance(items, list):
            items = []
        return cls(
            last_displayed_entity_id=last,
            current_pagination_index=index,
            pending_action=value.get("pending_action") or value.get("ai_manager_pending_action"),
            current_list=items[:50],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_displayed_entity_id": self.last_displayed_entity_id,
            "current_pagination_index": self.current_pagination_index,
            "pending_action": self.pending_action,
            "current_list": self.current_list[:50],
        }


class AIManager:
    def __init__(
        self,
        db_pool=None,
        *,
        history_provider: HistoryProvider | None = None,
        model: str | None = None,
        max_history: int = 12,
        max_tool_rounds: int = 3,
    ):
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        # Do not let the SDK silently replay expensive Groq requests.
        # The application owns retry/backoff policy; this prevents a single
        # 429 from turning into a long chain of hidden requests.
        self.client = AsyncGroq(api_key=key, max_retries=0)
        # Provider fallback is explicit and one-shot: a Groq 429 never causes
        # another Groq request. OpenAI/OpenRouter are used only when configured.
        self.openai_client = None
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        if openai_key:
            self.openai_client = AsyncOpenAI(api_key=openai_key, max_retries=0)
        self.openrouter_client = None
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if openrouter_key:
            self.openrouter_client = AsyncOpenAI(
                api_key=openrouter_key,
                base_url="https://openrouter.ai/api/v1",
                max_retries=0,
            )
        self.db = db_pool
        self.model = (
            model
            or os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
            or "openai/gpt-oss-20b"
        )
        self.history = history_provider or HistoryProvider()
        self.max_history = max(2, min(int(max_history), 50))
        self.max_tool_rounds = max(1, min(int(max_tool_rounds), 8))

    async def _chat_completion_with_fallback(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ):
        """Call Groq once; on 429, move once to OpenAI then OpenRouter."""
        providers = [("groq", self.client, self.model)]
        if self.openai_client:
            providers.append(("openai", self.openai_client, os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()))
        if self.openrouter_client:
            providers.append(("openrouter", self.openrouter_client, os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-20b").strip()))

        last_exc = None
        for index, (provider, client, model) in enumerate(providers):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools or None,
                    tool_choice=tool_choice,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                if provider != "groq":
                    logger.warning("AI provider fallback used: %s", provider)
                return response, provider
            except Exception as exc:
                last_exc = exc
                status = getattr(exc, "status_code", None)
                is_429 = status == 429 or "429" in str(exc) or "rate_limit" in str(exc).lower()
                if is_429 and index + 1 < len(providers):
                    logger.warning(
                        "AI provider rate limit/quota (%s); switching to next provider without retrying %s",
                        provider,
                        provider,
                    )
                    continue
                raise
        raise last_exc or RuntimeError("no_ai_provider_available")

    @staticmethod
    def _context(value: AIContext | ContextType | str) -> ContextType:
        if isinstance(value, AIContext):
            return ContextType(value.value.upper())
        return as_context_type(value)

    @staticmethod
    def _lang(text: str) -> str:
        import re
        text = str(text or "")
        if re.search(r"[Ա-Ֆա-ֆևօՕ]", text):
            return "hy"
        if re.search(r"[А-Яа-яЁё]", text):
            return "ru"
        return "en"

    def _trusted(
        self,
        context_type: ContextType,
        telegram_id: int,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        ctx = dict(extra or {})
        ctx["authenticated_telegram_id"] = int(telegram_id)
        if context_type == ContextType.PARTNER:
            import data_core
            partner = data_core.get_partner_by_user(int(telegram_id))
            if partner:
                ctx["partner_id"] = partner.get("id")

                # The open company comes from the authenticated cabinet UI.
                # It is trusted only after Data Core verifies ownership; Groq
                # never gets to invent or choose an internal company ID.
                raw_business_id = ctx.get("business_id")
                try:
                    business_id = int(raw_business_id) if raw_business_id not in (None, "", 0, "0") else None
                except (TypeError, ValueError):
                    business_id = None
                if business_id:
                    company = data_core.get_company(business_id)
                    if company and int(company.get("partner_id") or 0) == int(partner.get("id") or 0):
                        ctx["current_company"] = {
                            "id": int(company["id"]),
                            "name": company.get("name"),
                        }
                        ctx["current_company_id"] = int(company["id"])
                    else:
                        ctx.pop("business_id", None)
        return ctx

    @staticmethod
    def _storage_role(context: ContextType) -> str:
        # REGISTRATION is an AI conversation context, not a persisted DB role.
        # Store onboarding history in the partner session because the database
        # role constraint accepts client/partner/admin.
        return "partner" if context == ContextType.REGISTRATION else context.value.lower()

    def _session(self, telegram_id: int, context: ContextType):
        role = self._storage_role(context)

        def read():
            # ai_sessions.user_id stores the canonical users.telegram_id.
            # Registration/WebApp requests identify the caller by Telegram ID,
            # so always resolve/create the canonical users row first.
            import data_core

            actor_telegram_id = int(telegram_id)
            if actor_telegram_id <= 0:
                raise ValueError("invalid_telegram_id")

            user = data_core.ensure_user_by_telegram_id(actor_telegram_id)
            if not user or not user.get("telegram_id"):
                raise RuntimeError(
                    f"Unable to resolve users.telegram_id for telegram_id={actor_telegram_id}"
                )

            # users.telegram_id is the canonical users key in this project.
            # ai_sessions.user_id stores that same Telegram identity.
            session_user_id = int(user["telegram_id"])
            return (
                platform_db.active_session(session_user_id, role, "ai_manager")
                or platform_db.create_session(
                    session_user_id, role, "ai_manager", {"history_version": 1}
                )
            )

        return read

    async def _session_context(self, telegram_id: int, context: ContextType) -> dict[str, Any]:
        def read():
            session = self._session(telegram_id, context)()
            value = session.get("context_json") or {}
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except Exception:
                    value = {}
            return dict(value) if isinstance(value, dict) else {}

        return await asyncio.to_thread(read)

    async def _update_session_context(
        self,
        telegram_id: int,
        context: ContextType,
        patch: dict[str, Any],
        *,
        replace: bool = False,
    ):
        def write():
            session = self._session(telegram_id, context)()
            value = {} if replace else (session.get("context_json") or {})
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except Exception:
                    value = {}
            value = dict(value or {})
            value.update(patch)
            return platform_db.update_session(int(session["id"]), value)

        return await asyncio.to_thread(write)

    async def _supabase_history(
        self, telegram_id: int, context: ContextType, limit: int | None = None
    ):
        """Compatibility name; HistoryProvider is the canonical history layer."""
        role = self._storage_role(context)
        size = max(2, min(int(limit or self.max_history), 50))

        def read():
            provider = self.history
            if getattr(provider, "limit", size) != size:
                # Preserve the caller's requested window without mutating the
                # shared provider configuration.
                old_limit = getattr(provider, "limit", size)
                try:
                    provider.limit = size
                    return provider.get(int(telegram_id), role)
                finally:
                    provider.limit = old_limit
            return provider.get(int(telegram_id), role)

        return await asyncio.to_thread(read)

    async def _save_history(
        self,
        telegram_id: int,
        context: ContextType,
        sender: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        tool_call_id: str | None = None,
    ):
        """Compatibility name; HistoryProvider is the canonical write layer."""
        return await asyncio.to_thread(
            self.history.append,
            int(telegram_id),
            self._storage_role(context),
            sender,
            str(content or "")[:12000],
            metadata or {},
            tool_call_id,
        )

    async def _cost_log(
        self,
        telegram_id: int,
        context: ContextType,
        usage,
        *,
        status: str = "success",
        error: str = "",
        extra_context: dict[str, Any] | None = None,
        provider: str = "groq",
        model: str | None = None,
    ):
        data = (
            usage.model_dump()
            if hasattr(usage, "model_dump")
            else (usage if isinstance(usage, dict) else {})
        )
        meta = extra_context or {}
        def _int_or_none(value):
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None
        partner_id = _int_or_none(meta.get("partner_id"))
        company_id = _int_or_none(meta.get("company_id", meta.get("business_id")))
        order_id = _int_or_none(meta.get("order_id", meta.get("booking_id")))
        negotiation_id = _int_or_none(meta.get("negotiation_id"))
        return await asyncio.to_thread(
            ai_cost_center.record_usage,
            provider=str(provider or "groq"),
            model=str(model or self.model),
            chain=context.value.lower(),
            stage=str(meta.get("ai_stage") or meta.get("stage") or "manager"),
            operation=str(meta.get("ai_operation") or meta.get("operation") or "chat"),
            purpose=str(meta.get("ai_purpose") or meta.get("purpose") or "Unified AIManager"),
            user_id=int(telegram_id),
            partner_id=partner_id,
            company_id=company_id,
            order_id=order_id,
            negotiation_id=negotiation_id,
            input_tokens=int(data.get("prompt_tokens") or data.get("input_tokens") or 0),
            output_tokens=int(data.get("completion_tokens") or data.get("output_tokens") or 0),
            cached_tokens=int(data.get("prompt_cached_tokens") or data.get("cached_tokens") or 0),
            reasoning_tokens=int(data.get("reasoning_tokens") or 0),
            status=status,
            error=error,
        )

    @staticmethod
    def _partner_action_summary(language: str, action: dict[str, Any], fallback: str = "") -> str:
        name = str(action.get("name") or "")
        args = dict(action.get("args") or {})
        if name not in {"add_service", "add_services", "register_business"}:
            return fallback
        company = str(args.get("company_name") or "").strip()
        items = []
        raw_items = [args] if name == "add_service" else list(args.get("services") or [])
        for item in raw_items:
            service_name = str(item.get("name") or "").strip()
            if not service_name:
                continue
            price = item.get("price")
            line = f"• {service_name}"
            if price not in (None, ""):
                try:
                    number = float(price)
                    shown = str(int(number)) if number.is_integer() else str(number)
                except (TypeError, ValueError):
                    shown = str(price)
                price_type = str(item.get("price_type") or args.get("price_type") or "").strip().lower()
                if price_type == "from":
                    shown_price = (
                        f"{shown} ֏-ից" if language == "hy"
                        else f"от {shown} ֏" if language == "ru"
                        else f"from {shown} ֏"
                    )
                else:
                    shown_price = f"{shown} ֏"
                line += f" — {shown_price}"
            items.append(line)
        joined = "\n".join(items) or "• —"
        mode = str(args.get("service_mode") or "").strip().lower()
        location = args.get("service_location") if isinstance(args.get("service_location"), dict) else {}
        coverage = args.get("coverage") if isinstance(args.get("coverage"), dict) else (
            location.get("coverage") if isinstance(location.get("coverage"), dict) else {}
        )
        cities = coverage.get("cities") or coverage.get("city") or []
        marzes = coverage.get("marzes") or coverage.get("marz") or []
        if isinstance(cities, str): cities = [cities]
        if isinstance(marzes, str): marzes = [marzes]
        loc_lines = []
        if mode in {"mobile", "both"}:
            loc_lines.append("📍 " + ("Հաճախորդի հասցեում" if language == "hy" else "У клиента" if language == "ru" else "At the customer's address"))
        elif mode == "at_address":
            loc_lines.append("📍 " + ("Հաճախորդի հասցեում" if language == "hy" else "По адресу клиента" if language == "ru" else "At the customer's address"))
        if cities:
            loc_lines.append(("Քաղաքներ: " if language == "hy" else "Города: " if language == "ru" else "Cities: ") + ", ".join(map(str, cities)))
        if marzes:
            loc_lines.append(("Մարզեր: " if language == "hy" else "Марзы: " if language == "ru" else "Marzes: ") + ", ".join(map(str, marzes)))
        location_text = "\n" + "\n".join(loc_lines) if loc_lines else ""
        if language == "hy":
            return (
                f"Ավելացնել «{company or 'ընկերություն'}» ընկերությունում հետևյալ ծառայությունները:"
                f"\n{joined}{location_text}\n\nՀաստատո՞ւմ եք։"
            )
        if language == "ru":
            return (
                f"Добавить в компанию «{company or 'компанию'}» следующие услуги:"
                f"\n{joined}{location_text}\n\nПодтверждаете?"
            )
        return (
            f"Add the following services to “{company or 'the company'}”:"
            f"\n{joined}{location_text}\n\nConfirm?"
        )

    @staticmethod
    def _confirmation_text(language: str, summary: str) -> str:
        if language == "hy":
            return f"{summary}\n\nՀաստատո՞ւմ եք։"
        if language == "ru":
            return f"{summary}\n\nПодтверждаете?"
        return f"{summary}\n\nConfirm?"

    @staticmethod
    def _cancel_text(language: str) -> str:
        if language == "hy":
            return "Չեղարկվեց։ Ոչ մի փոփոխություն չի կատարվել։"
        if language == "ru":
            return "Отменено. Изменений не внесено."
        return "Cancelled. No changes were made."

    @staticmethod
    def _done_text(language: str) -> str:
        if language == "hy":
            return "Կատարված է։"
        if language == "ru":
            return "Готово."
        return "Done."

    @staticmethod
    def _error_text(language: str) -> str:
        if language == "hy":
            return "Ներողություն, տեխնիկական սխալ է տեղի ունեցել։"
        if language == "ru":
            return "Извините, произошла техническая ошибка."
        return "Sorry, a technical error occurred."

    @staticmethod
    def _approx_tokens(value: Any) -> int:
        """Cheap token estimate used only for request-size protection."""
        try:
            return max(1, (len(str(value)) + 3) // 4)
        except Exception:
            return 1

    @classmethod
    def _compact_tool_result(cls, name: str, result: Any, max_chars: int = 5000) -> str:
        """Keep tool-loop context small without changing backend truth."""
        if not isinstance(result, dict):
            text = str(result or "")
            return text[:max_chars]

        if name == "admin_catalog_candidates":
            compact = {
                "ok": bool(result.get("ok", True)),
                "application_id": result.get("application_id"),
                "items": [],
            }
            for item in result.get("items") or []:
                if not isinstance(item, dict):
                    continue
                row = {
                    "service_index": item.get("service_index"),
                    "service_name": item.get("service_name"),
                    "current_category": item.get("current_category"),
                    "candidates": [],
                }
                for candidate in (item.get("candidates") or [])[:6]:
                    if not isinstance(candidate, dict):
                        continue
                    row["candidates"].append({
                        "catalog_name": candidate.get("catalog_name"),
                        "master_name_am": candidate.get("master_name_am"),
                        "name_ru": candidate.get("name_ru"),
                        "name_en": candidate.get("name_en"),
                    })
                compact["items"].append(row)
            return json.dumps(compact, ensure_ascii=False, default=str)[:max_chars]

        if name == "catalog_choices":
            items = result.get("items") or []
            compact = {
                "ok": bool(result.get("ok", True)),
                "items": [
                    {
                        "catalog_name": x.get("catalog_name"),
                        "name_am": x.get("name_am"),
                        "name_ru": x.get("name_ru"),
                        "name_en": x.get("name_en"),
                    }
                    for x in items
                    if isinstance(x, dict)
                ],
            }
            return json.dumps(compact, ensure_ascii=False, default=str)[:12000]

        if name == "get_application":
            compact = {
                key: result.get(key)
                for key in (
                    "ok", "id", "application_id", "status", "company_name",
                    "business_name", "marz", "city", "address", "phone",
                    "working_hours", "description",
                )
                if key in result
            }
            service_items = result.get("service_items")
            if isinstance(service_items, list):
                compact["service_items"] = [
                    {
                        key: item.get(key)
                        for key in (
                            "service_index", "name", "price", "price_type",
                            "catalog_name", "category_name_am",
                            "category_name_ru", "needs_admin_review",
                        )
                        if key in item
                    }
                    for item in service_items
                    if isinstance(item, dict)
                ]
            return json.dumps(compact, ensure_ascii=False, default=str)[:max_chars]

        try:
            encoded = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            encoded = str(result)
        return encoded[:max_chars]

    @classmethod
    def _compact_history(
        cls,
        history: list[dict[str, Any]] | None,
        *,
        max_chars: int = 7000,
        max_items: int = 8,
    ) -> list[dict[str, Any]]:
        """Token-budget history: preserve recent turns, discard bulky tool payloads."""
        source = list(history or [])
        selected: list[dict[str, Any]] = []
        used = 0

        for item in reversed(source):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            content = str(item.get("content") or "")

            if role == "tool":
                tool_name = str(
                    (item.get("data") or {}).get("tool_name") or ""
                )
                content = cls._compact_tool_result(tool_name, content, 2800)
            else:
                content = content[:1800]

            candidate = {
                "role": role,
                "content": content,
            }
            if item.get("tool_call_id"):
                candidate["tool_call_id"] = item["tool_call_id"]

            size = len(json.dumps(candidate, ensure_ascii=False, default=str))
            if selected and (len(selected) >= max_items or used + size > max_chars):
                continue
            selected.append(candidate)
            used += size

        selected.reverse()
        return selected

    @staticmethod
    def _is_confirmation(message: str) -> bool:
        normalized = " ".join(str(message or "").strip().casefold().split())
        normalized = normalized.strip(" .,!?:;՝։\"'«»")
        return normalized in _CONFIRMATIONS

    @staticmethod
    def _is_cancel(message: str) -> bool:
        normalized = " ".join(str(message or "").strip().casefold().split())
        normalized = normalized.strip(" .,!?:;՝։\"'«»")
        return normalized in _CANCELS

    @staticmethod
    def _normalize_admin_text(value: str) -> str:
        import re
        text = str(value or "").casefold().replace("ё", "е")
        text = re.sub(r"[^0-9a-zа-яёա-ֆևօ]+", " ", text)
        return " ".join(text.split()).strip()

    @classmethod
    def _extract_admin_price_edit(
        cls,
        message: str,
        services: list[dict[str, Any]],
    ) -> tuple[int, str] | None:
        """Safely recognize an explicit price edit against a live application service.

        This is deliberately narrow: it only activates when the user supplies a
        numeric price and the service name matches exactly (after normalization)
        one of the backend-owned service names. Ambiguous semantic requests still
        go through Groq/tools.
        """
        import re
        text = str(message or "")
        lower = text.casefold()
        if not any(word in lower for word in (
            "գին", "գն", "փոխ", "փուխ", "цена", "стоим", "price", "change",
            "измен", "помен", "փոփոխ",
        )):
            return None
        match = re.search(r"(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+|\d{3,7})(?!\d)", text)
        if not match:
            return None
        raw_price = re.sub(r"[\s.,]", "", match.group(1))
        try:
            price = int(raw_price)
        except ValueError:
            return None
        if price < 0 or price > 100_000_000:
            return None

        normalized = cls._normalize_admin_text(text)
        candidates: list[tuple[int, str]] = []
        for service in services or []:
            name = str(service.get("name") or "").strip()
            if not name:
                continue
            n = cls._normalize_admin_text(name)
            if not n:
                continue
            # Exact phrase match only. Never guess between similar services.
            if re.search(r"(?<![\wա-ֆ])" + re.escape(n) + r"(?![\wա-ֆ])", normalized):
                candidates.append((int(service.get("service_index") or 0), name))
        if len(candidates) != 1:
            return None
        return candidates[0][0], candidates[0][1]

    async def _render_category_pending(self, pending: dict[str, Any], language: str) -> dict[str, Any]:
        """Render the next ambiguity or the final deterministic preview."""
        application_id = int(pending.get("application_id") or 0)
        ambiguities = pending.get("unresolved_ambiguities") or []
        if ambiguities:
            item = ambiguities[0]
            options = item.get("options") or []
            if language == "hy":
                reply = (
                    f"🔎 Հայտ #{application_id}. «{item.get('service_name') or '—'}» ծառայության համար "
                    "ընտրեք համապատասխան ենթաուղղությունը։"
                )
            elif language == "ru":
                reply = (
                    f"🔎 Заявка #{application_id}. Выберите подкатегорию для услуги "
                    f"«{item.get('service_name') or '—'}»."
                )
            else:
                reply = (
                    f"🔎 Application #{application_id}. Choose a subcategory for "
                    f"“{item.get('service_name') or '—'}”."
                )
            return {
                "reply": reply,
                "fast_path": True,
                "pending_action": pending,
                "category_selection": {
                    "service_id": int(item["service_id"]),
                    "service_name": str(item.get("service_name") or "—"),
                    "options": [
                        {
                            "category_id": int(opt["category_id"]),
                            "category_name_am": str(opt.get("category_name_am") or "—"),
                            "callback_data": f"/select_svc_{int(item['service_id'])}_cat_{int(opt['category_id'])}",
                        }
                        for opt in options
                    ],
                },
                "confirmation_pending": False,
            }

        lines = []
        for change in pending.get("resolved_changes") or []:
            lines.append(
                f"• {change.get('service_name') or '—'} → **{change.get('category_name_am') or '—'}**"
            )
        for item in pending.get("unclassified_services") or []:
            lines.append(
                f"• {item.get('service_name') or '—'} → ⚠️ **Չդասակարգված**\n"
                "  Վստահելի համապատասխան կատեգորիա չի գտնվել։"
            )

        if language == "hy":
            reply = (
                f"📋 **Հայտ #{application_id} — կատեգորիաների փոփոխության նախադիտում**\n\n"
                + ("\n".join(lines) or "• Փոփոխություններ չկան։")
                + "\n\nՉդասակարգված ծառայությունները չեն ստանա կատեգորիա և չեն փոփոխվի կատալոգում։"
                + "\n\n**Կիրառե՞լ այս փոփոխությունները։**"
            )
        elif language == "ru":
            reply = (
                f"📋 **Заявка #{application_id} — предпросмотр изменений категорий**\n\n"
                + ("\n".join(lines) or "• Изменений нет.")
                + "\n\nНеразмеченные услуги не получат категорию и не будут изменены в каталоге."
                + "\n\n**Применить эти изменения?**"
            )
        else:
            reply = (
                f"📋 **Application #{application_id} — category changes preview**\n\n"
                + ("\n".join(lines) or "• No changes.")
                + "\n\nUnclassified services will not receive a category and will not be changed."
                + "\n\n**Apply these changes?**"
            )
        return {
            "reply": reply,
            "fast_path": True,
            "pending_action": pending,
            "confirmation_pending": True,
            "confirmation_buttons": [
                {"text": "✅ Այո", "value": "yes"},
                {"text": "❌ Ոչ", "value": "no"},
            ],
        }

    async def _admin_category_pending_fast_path(
        self,
        telegram_id: int,
        message: str,
        pending: dict[str, Any] | None,
        language: str,
    ) -> dict[str, Any] | None:
        """Handle category selections/confirmation; unrelated text goes to Groq."""
        if not pending or pending.get("type") != "bulk_resolve_categories":
            return None

        import re
        import data_core

        text = str(message or "").strip()
        if self._is_cancel(text):
            await self._clear_pending(telegram_id, ContextType.ADMIN)
            return {"reply": self._cancel_text(language), "fast_path": True, "cancelled": True}

        if self._is_confirmation(text):
            if pending.get("unresolved_ambiguities"):
                return await self._render_category_pending(pending, language)
            token = str(pending.get("confirmation_token") or "")
            try:
                result = data_core.apply_pending_category_resolution(
                    pending_action=pending,
                    confirmation_token=token,
                    actor_user_id=int(telegram_id),
                )
                await self._clear_pending(telegram_id, ContextType.ADMIN)
                return {
                    "reply": self._done_text(language),
                    "fast_path": True,
                    "confirmed": True,
                    "tool_result": result,
                }
            except Exception as exc:
                logger.exception("Pending category confirmation failed")
                return {
                    "reply": self._error_text(language),
                    "fast_path": True,
                    "confirmed": False,
                    "error": str(exc),
                }

        match = re.fullmatch(r"/?select_svc_(\d+)_cat_(\d+)", text, flags=re.IGNORECASE)
        if not match:
            return None

        try:
            selection = CategorySelectionRequest(
                service_id=int(match.group(1)),
                chosen_cat_id=int(match.group(2)),
            )
        except ValidationError:
            return {
                "reply": "⚠️ Անվավեր ընտրություն։ Փոփոխությունը չի կատարվել։",
                "fast_path": True,
                "selection_rejected": True,
            }

        ambiguity = next(
            (
                item for item in (pending.get("unresolved_ambiguities") or [])
                if int(item.get("service_id") or 0) == selection.service_id
            ),
            None,
        )
        if not ambiguity:
            return {
                "reply": "⚠️ Այս ծառայության համար ակտիվ ընտրության փուլ չկա։",
                "fast_path": True,
                "selection_rejected": True,
            }

        allowed_ids = {
            int(option.get("category_id"))
            for option in (ambiguity.get("options") or [])
            if option.get("category_id") is not None
        }
        if selection.chosen_cat_id not in allowed_ids:
            return {
                "reply": "🚨 Ընտրված կատեգորիան այս ծառայության թույլատրելի տարբերակների մեջ չկա։",
                "fast_path": True,
                "selection_rejected": True,
            }

        if not data_core.check_live_category_exists(selection.chosen_cat_id):
            return {
                "reply": "🚨 Ընտրված կատեգորիան այլևս գոյություն չունի կենդանի կատալոգում։",
                "fast_path": True,
                "selection_rejected": True,
            }

        option = next(
            x for x in (ambiguity.get("options") or [])
            if int(x.get("category_id")) == selection.chosen_cat_id
        )
        updated = dict(pending)
        updated["resolved_changes"] = list(updated.get("resolved_changes") or [])
        updated["unresolved_ambiguities"] = list(updated.get("unresolved_ambiguities") or [])
        updated["resolved_changes"].append({
            "service_id": selection.service_id,
            "service_name": str(ambiguity.get("service_name") or ""),
            "category_id": selection.chosen_cat_id,
            "category_name_am": str(option.get("category_name_am") or ""),
        })
        updated["unresolved_ambiguities"] = [
            x for x in updated["unresolved_ambiguities"]
            if int(x.get("service_id") or 0) != selection.service_id
        ]
        await self._update_session_context(
            telegram_id, ContextType.ADMIN, {"pending_action": updated}
        )
        return await self._render_category_pending(updated, language)

    async def _admin_fast_path(
        self,
        telegram_id: int,
        message: str,
        session_context: dict[str, Any],
        language: str,
    ) -> dict[str, Any] | None:
        """Deterministic path for explicit admin application price edits.

        No Groq request is made here. Backend resolves the application/service,
        validates the price and creates the real confirmation action.
        """
        if not message:
            return None
        import re
        import data_core
        app_id = None
        m = re.search(r"(?:#|№)\s*(\d+)", str(message))
        if m:
            app_id = int(m.group(1))
            # Persist the explicit application reference as the admin's focused
            # backend entity so subsequent natural-language commands can refer
            # to it safely ("статус", "активируй", "проверь документ").
            session_context["last_focused_application_id"] = app_id
            await self._update_session_context(
                telegram_id, ContextType.ADMIN,
                {"last_focused_application_id": app_id},
            )
        # If the admin continues an already focused application without
        # repeating its number ("проверено, активируй"), use only the backend-
        # owned last-focused application. Never guess from arbitrary history.
        if app_id is None:
            focused = session_context.get("last_focused_application_id")
            try:
                app_id = int(focused) if focused not in (None, "", 0, "0") else None
            except (TypeError, ValueError):
                app_id = None
            if app_id is None:
                return None

        # Read-only application catalogue/subcategory requests are deterministic.
        # This keeps follow-ups such as "բոլորը" anchored to the active application
        # instead of sending the user back through the generic application opener.
        catalog_read_intent = re.search(
            r"(?:ենթաուղղ|ենթաուղղություն|ենթակատեգ|կատեգոր|subcategory|subcategor|catalog|category|категор|подкатегор)",
            str(message or "").casefold(),
        )
        catalog_write_intent = re.search(
            r"(?:դասակարգ|դասավոր|վերագր|կապիր|ուղիր|ուղղիր|ուղղել|classif|categor|resolve|assign|присво|исправ|классифиц)",
            str(message or "").casefold(),
        )
        all_services_intent = re.search(
            r"(?:\bբոլորը\b|\bբոլոր\b|\ball\b|\bвсе\b|բոլոր ծառայ|все услуги|all services)",
            str(message or "").casefold(),
        )
        if (
            not catalog_write_intent
            and (
                (catalog_read_intent and (all_services_intent or re.search(r"(?:ծառայ|услуг|service)", str(message or "").casefold())))
                or (all_services_intent and session_context.get("last_admin_read_intent") == "application_catalog")
            )
        ):
            await self._update_session_context(
                telegram_id, ContextType.ADMIN,
                {"last_admin_read_intent": "application_catalog"},
            )
            rows = data_core.application_service_items(app_id)
            lines = []
            for item in rows:
                name = str(item.get("name") or "—")
                category = (
                    item.get("category_name_am")
                    or item.get("category_name_ru")
                    or item.get("category_name_en")
                )
                master = (
                    item.get("master_name_am")
                    or item.get("master_name_ru")
                    or item.get("master_name_en")
                )
                if category:
                    label = f"{master} → {category}" if master else str(category)
                else:
                    label = "⚠️ Չդասակարգված"
                lines.append(f"• {name} → {label}")
            if language == "hy":
                reply = f"Հայտ #{app_id}-ի բոլոր ծառայությունների ենթաուղղությունները՝\\n" + ("\\n".join(lines) or "• Ծառայություններ չկան")
            elif language == "ru":
                reply = f"Подкатегории всех услуг заявки #{app_id}:\\n" + ("\\n".join(lines) or "• Услуг нет")
            else:
                reply = f"Subcategories of all services in application #{app_id}:\\n" + ("\\n".join(lines) or "• No services")
            return {"reply": reply, "fast_path": True, "application_id": app_id, "read_only": True}

        # Approval is a distinct intent and must never fall through to
        # the generic "open application" branch.
        # Explicit application approval/activation is a deterministic backend command.
        # Do not send these commands to Groq: parse the application number locally,
        # then let Data Core validate and prepare the confirmed action.
        approval_intent = re.search(
            r"(?:հաստատիր|հաստատել|հաստատի|հաստատե՞լ|approve|одобр|утверд|"
            r"ակտիվացրու|ակտիվացրու|ակտիվացր|активир|активируй)"
            r".*?(?:հայտ|заяв(?:ку|ка)?|application)"
            r"|(?:հայտ|заяв(?:ку|ка)?|application)"
            r".*?(?:հաստատիր|հաստատել|հաստատի|հաստատե՞լ|approve|одобр|утверд|"
            r"ակտիվացրու|ակտիվացր|активир|активируй)",
            str(message or "").casefold(),
        )
        explicit_id_approval = bool(re.search(
            r"(?:#|№|\bID\b|\bИД\b)\s*\d+.*?(?:հաստատ|approve|одобр|утверд|ակտիվ|activat|ակտիվացրու|ակտիվացր)"
            r"|(?:հաստատ|approve|одобр|утверд|ակտիվ|activat|ակտիվացրու|ակտիվացր).*?(?:#|№|\bID\b|\bИД\b)\s*\d+",
            str(message or "").casefold(),
        ))
        # A focused application remains the authoritative target for short
        # follow-ups such as "հաստատել", "ակտիվացրու" or "approve".
        # Never send these deterministic approval continuations to Groq.
        bare_approval = bool(re.fullmatch(
            r"(?:հաստատիր|հաստատել|հաստատի|հաստատե՞լ|հաստատում եմ|"
            r"approve|одобри|одобрить|утвердить|утверди|"
            r"ակտիվացրու|ակտիվացր|активируй|активировать|activate)",
            str(message or "").strip().casefold(),
        ))
        if bare_approval and app_id is not None:
            approval_intent = True

        if approval_intent or explicit_id_approval:
            if app_id is None:
                return {
                    "reply": (
                        "Նշեք հայտի համարը։"
                        if language == "hy"
                        else "Укажите номер заявки."
                        if language == "ru"
                        else "Please specify the application number."
                    ),
                    "fast_path": True,
                    "needs_clarification": True,
                }
            try:
                gate = data_core.prepare_application_approval(
                    application_id=app_id,
                    actor_user_id=telegram_id,
                )
                if not gate.get("can_approve"):
                    reply = str(gate.get("message") or "Հայտը դեռ չի կարելի հաստատել։")
                    await self._save_history(
                        telegram_id, ContextType.ADMIN, "ai", reply,
                        {"fast_path": True, "approval_blocked": gate.get("reason_code")},
                    )
                    return {
                        "reply": reply,
                        "fast_path": True,
                        "application_id": app_id,
                        "approval_blocked": True,
                        "tool_result": gate,
                    }
                action = dict(gate.get("action") or {})
                await self._set_pending(telegram_id, ContextType.ADMIN, action)
                summary = str(gate.get("summary") or f"Հաստատել հայտ #{app_id}?")
                await self._save_history(
                    telegram_id, ContextType.ADMIN, "ai", summary,
                    {"fast_path": True, "pending_action": action},
                )
                return {
                    "reply": summary,
                    "confirmation_pending": True,
                    "fast_path": True,
                    "application_id": app_id,
                    "tool_result": gate,
                }
            except Exception as exc:
                logger.exception("Admin deterministic approval fast path failed")
                return {"reply": self._error_text(language), "error": str(exc)}

        # All ordinary admin edits, including price changes, go through Groq
        # -> ToolRegistry -> pending_action. Fast Path handles only deterministic
        # button selections and confirmation/cancellation.
        return None


    async def _pending(self, telegram_id: int, context: ContextType):
        ctx = await self._session_context(telegram_id, context)
        return SessionState.from_dict(ctx).pending_action

    async def _set_pending(self, telegram_id: int, context: ContextType, action: dict[str, Any]):
        pending = dict(action or {})
        pending["state"] = "awaiting_confirmation"
        pending.setdefault("created_at", int(time.time()))
        return await self._update_session_context(
            telegram_id, context, {"pending_action": pending}
        )

    async def _clear_pending(self, telegram_id: int, context: ContextType):
        ctx = await self._session_context(telegram_id, context)
        ctx.pop("pending_action", None)
        ctx.pop("ai_manager_pending_action", None)
        return await self._update_session_context(
            telegram_id, context, ctx, replace=True
        )

    async def chat_json(
        self,
        telegram_id: int,
        context_type: ContextType | str,
        message: str,
        *,
        task_instructions: str,
        extra_context: dict[str, Any] | None = None,
        language: str | None = None,
        max_tokens: int = 1800,
    ) -> dict[str, Any]:
        role = self._context(context_type)
        language = language or self._lang(message)
        history = await self._supabase_history(telegram_id, role)
        trusted = self._trusted(role, telegram_id, extra_context)
        # Deterministic pending handling is intentionally before PromptFactory/Groq.
        pending = await self._pending(telegram_id, role)
        if pending:
            if self._is_cancel(message):
                await self._clear_pending(telegram_id, role)
                reply = self._cancel_text(language)
                await self._save_history(telegram_id, role, "ai", reply, {"cancelled": True})
                return {"reply": reply, "cancelled": True}
            if self._is_confirmation(message):
                pending_name = str(pending.get("name") or "").strip()
                is_partner_submission = (
                    role == ContextType.PARTNER
                    and pending_name in {"add_service", "add_services", "register_business"}
                )
                claim = await asyncio.to_thread(
                    platform_db.claim_ai_pending_action,
                    int(telegram_id),
                    self._storage_role(role),
                    partner_submission=is_partner_submission,
                )
                if str(claim.get("status") or "") == "executing":
                    reply = (
                        "⏳ Գործողությունն արդեն կատարվում է։"
                        if language == "hy"
                        else "⏳ Действие уже выполняется."
                        if language == "ru"
                        else "⏳ The action is already being processed."
                    )
                    return {"reply": reply, "confirmation_pending": True}
                if str(claim.get("status") or "") != "claimed":
                    reply = (
                        "⚠️ Ակտիվ գործողություն չի գտնվել։"
                        if language == "hy"
                        else "⚠️ Активного действия для подтверждения не найдено."
                        if language == "ru"
                        else "⚠️ No active action is waiting for confirmation."
                    )
                    return {"reply": reply, "confirmation_pending": False}
                claimed = dict(claim.get("pending_action") or {})
                pending_name = str(claimed.get("name") or "").strip()
                pending_args = dict(claimed.get("args") or {})
                try:
                    tools = ToolRegistry(
                        telegram_id=int(telegram_id),
                        context_type=role,
                        trusted_context=trusted,
                        session_state=SessionState.from_dict(await self._session_context(telegram_id, role)).to_dict(),
                    )
                    result = await tools.execute_confirmed(pending_name, pending_args)
                    await self._clear_pending(telegram_id, role)
                    reply = (
                        "⏳ Հայտը ուղարկվեց ստուգման։"
                        if is_partner_submission and language == "hy"
                        else "⏳ Заявка отправлена на проверку."
                        if is_partner_submission and language == "ru"
                        else "⏳ The application was sent for review."
                        if is_partner_submission
                        else self._done_text(language)
                    )
                    await self._save_history(
                        telegram_id, role, "ai", reply,
                        {"confirmed_action": claimed, "tool_result": result},
                    )
                    return {"reply": reply, "tool_result": result, "confirmed": True}
                except Exception as exc:
                    await asyncio.to_thread(
                        platform_db.restore_ai_pending_action,
                        int(telegram_id),
                        self._storage_role(role),
                        claimed,
                    )
                    logger.exception("AIManager confirmed action failed")
                    reply = self._error_text(language)
                    await self._save_history(
                        telegram_id, role, "ai", reply,
                        {"confirmed_action": claimed, "error": str(exc)[:1000]},
                    )
                    return {"reply": reply, "confirmed": False, "error": str(exc)}
            reply = (
                "Նախ հաստատեք կամ չեղարկեք սպասվող փոփոխությունը։"
                if language == "hy" else
                "Сначала подтвердите или отмените ожидающее изменение."
                if language == "ru" else
                "Please confirm or cancel the pending change first."
            )
            return {"reply": reply, "confirmation_pending": True}

        if role == ContextType.ADMIN:
            fast = await self._admin_fast_path(
                telegram_id, message, await self._session_context(telegram_id, role), language
            )
            if fast is not None:
                return fast

        prompt = PromptFactory.build(
            role,
            message=message,
            history=history,
            trusted_context=trusted,
            language=language,
            task_instructions=task_instructions,
        )

        started = time.monotonic()
        await self._save_history(
            telegram_id, role, "user", message,
            {"context_type": role.value, "structured": True},
        )

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if "gpt-oss" not in self.model.lower():
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response, provider_used = await self._chat_completion_with_fallback(
                messages=kwargs["messages"],
                tools=None,
                tool_choice=None,
                temperature=kwargs["temperature"],
                max_tokens=kwargs["max_tokens"],
            )
            await self._cost_log(
                telegram_id, role, getattr(response, "usage", None),
                extra_context=extra_context,
                provider=provider_used,
                model=(
                    self.model if provider_used == "groq"
                    else os.getenv(
                        "OPENAI_MODEL" if provider_used == "openai" else "OPENROUTER_MODEL",
                        self.model,
                    ).strip()
                ),
            )
            raw = (response.choices[0].message.content or "{}").strip()
            parsed = json.loads(raw)
        except Exception as exc:
            await self._cost_log(
                telegram_id, role, None, status="error", error=str(exc)[:1000],
                extra_context=extra_context,
            )
            logger.exception("AIManager structured completion failed")
            raise RuntimeError("AIManager structured completion failed") from exc

        await self._save_history(
            telegram_id, role, "ai", raw,
            {"structured": True, "latency_ms": round((time.monotonic() - started) * 1000)},
        )
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _parse_explicit_partner_services(message: str) -> list[dict[str, Any]] | None:
        """Parse only explicit service+price commands.

        This is a deterministic safety path, not a replacement for AI NLU.
        It preserves the original service names and extracts explicit mobile
        coverage in Armenian/Russian without inventing missing facts.
        """
        import re
        raw = str(message or "").strip()
        if not raw:
            return None

        command = re.match(
            r"^(?:создай(?:те)?|добавь(?:те)?|создать|добавить|"
            r"ստեղծիր|ստեղծեք|ստեղծել|ավելացրու|ավելացրեք|ավելացնել)\s+"
            r"(?:հետևյալ\s+)?"
            r"(?:услуг(?:у|и)?|сервис(?:ы|а)?|ծառայություն(?:ներ)?|ծառայությունները)"
            r"(?:\s*[:,-․։]?\s*)",
            raw,
            flags=re.IGNORECASE,
        )
        if not command:
            return None

        body = raw[command.end():].strip()
        if not body:
            return None

        folded = body.casefold()
        mode = None
        if re.search(
            r"выезд\s+к\s+клиенту|выезжаю|выезд|mobile|"
            r"հաճախորդի\s+մոտ|պատվիրատուի\s+հասցեում|"
            r"պատվիրատուի\s+մոտ|այցով\s+հաճախորդին|մեկնում\s+եմ",
            folded,
            flags=re.IGNORECASE,
        ):
            mode = "mobile"
        elif re.search(
            r"по\s+этому\s+адресу|работаю\s+на\s+месте|at\s+address|"
            r"այս\s+հասցեում|հասցեում|տեղում",
            folded,
            flags=re.IGNORECASE,
        ):
            mode = "at_address"

        coverage: dict[str, Any] | None = None

        # Explicit Armenian coverage: "Հրազդանում և Կոտայքի մարզում".
        m = re.search(
            r"(?P<city>[Ա-Ֆա-ֆևօՕև]+)ում\s+և\s+"
            r"(?P<marz>[Ա-Ֆա-ֆևօՕև]+)\s+մարզում",
            body,
            flags=re.IGNORECASE,
        )
        if m:
            coverage = {
                "city": m.group("city").strip(),
                "marz": m.group("marz").strip(),
            }
        else:
            m = re.search(
                r"(?:քաղաքում|քաղաք՝|քաղաք)\s*[:,-]?\s*"
                r"(?P<city>[Ա-Ֆա-ֆևօՕև]+).*?"
                r"(?:մարզում|մարզ՝|մարզ)\s*[:,-]?\s*"
                r"(?P<marz>[Ա-Ֆա-ֆևօՕև]+)",
                body,
                flags=re.IGNORECASE,
            )
            if m:
                coverage = {
                    "city": m.group("city").strip(),
                    "marz": m.group("marz").strip(),
                }

        # Russian explicit coverage forms.
        if coverage is None:
            m = re.search(
                r"(?:в\s+городе|город)\s+(?P<city>[А-ЯЁа-яё-]+).*?"
                r"(?:по\s+)?(?P<marz>[А-ЯЁа-яё-]+)\s+области",
                body,
                flags=re.IGNORECASE,
            )
            if m:
                coverage = {
                    "city": m.group("city").strip(),
                    "marz": m.group("marz").strip(),
                }

        # Do not let the service-location sentence become a fake service.
        body_for_services = re.split(
            r"(?:Ծառայությունները|Ծառայություն(?:ները)?)\s+"
            r"(?:մատուցում|մատուցվում|կատարում)|"
            r"(?:услуги|услугу)\s+(?:оказываем|предоставляем)",
            body,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

        # Match each explicit "name — price դրամից" segment independently.
        # This handles Armenian "՝", em-dash, colon, and Russian "от".
        pattern = re.compile(
            r"(?P<name>.+?)\s*(?:՝|:|—|–|-)\s*"
            r"(?P<price>\d[\d\s.,]*)\s*"
            r"(?P<currency>դրամ(?:ից)?|֏|amd|руб(?:лей|\.)?|₽)?",
            flags=re.IGNORECASE,
        )

        result: list[dict[str, Any]] = []
        for match in pattern.finditer(body_for_services):
            name = re.sub(r"\s+", " ", match.group("name") or "").strip(" ,;.-")
            raw_price = re.sub(r"[\s,.]", "", match.group("price") or "")
            if not name or not raw_price:
                continue
            try:
                price = float(raw_price)
            except ValueError:
                continue
            currency = str(match.group("currency") or "").casefold()
            price_type = "from" if (
                "ից" in currency or "от" in body_for_services[
                    max(0, match.start() - 8):match.end() + 1
                ].casefold()
            ) else "fixed"
            item: dict[str, Any] = {
                "name": name,
                "price": int(price) if price.is_integer() else price,
                "price_type": price_type,
            }
            if mode:
                item["service_mode"] = mode
            if coverage:
                item["coverage"] = coverage
                item["service_location"] = coverage
            result.append(item)

        # Require at least one explicit service and reject a partial parse.
        if not result:
            return None

        # If the input contains multiple price-bearing services, every one must
        # have been parsed; otherwise fall back to the full AI path.
        expected_prices = re.findall(
            r"\d[\d\s.,]*\s*(?:դրամ(?:ից)?|֏|amd|₽|руб(?:лей|\.)?)",
            body_for_services,
            flags=re.IGNORECASE,
        )
        if len(result) != len(expected_prices):
            return None

        return result

    @staticmethod
    def _pending_service_name(message: str) -> str | None:
        """Extract a service name from a follow-up add/create message without inventing a price."""
        import re
        text = " ".join(str(message or "").strip().split())
        m = re.match(
            r"^(?:добавь(?:те)?|добавить|создай(?:те)?|создать)\s+"
            r"(?:услуг(?:у|и)?|сервис(?:ы|а)?)?\s*(?P<name>.+?)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if not m:
            return None
        name = re.sub(r"\s+", " ", m.group("name")).strip(" ,;:.-")
        # A follow-up without a price is useful only for resolving an already
        # pending unresolved service; it must never silently create a new
        # price-less service.
        return name or None

    async def _merge_pending_partner_service(self, pending: dict[str, Any], message: str) -> bool:
        """Mutate the existing ADD_SERVICES draft; never replace its service list."""
        services = pending.get("args", {}).get("services")
        if not isinstance(services, list) or not services:
            return False

        unresolved = [
            item for item in services
            if isinstance(item, dict)
            and str(item.get("catalog_match_status") or "") != "matched"
        ]
        if not unresolved:
            return False

        name = self._pending_service_name(message)
        if not name:
            return False

        import data_core
        probe = data_core.resolve_catalog_services([{"name": name, "price": 0, "price_type": "from"}], limit=500)
        if not probe:
            return False
        candidate = probe[0]
        if str(candidate.get("catalog_match_status") or "") != "matched":
            return False

        target = unresolved[0]
        # Preserve the original price/description/address data; only replace
        # the unresolved service name and live catalogue resolution.
        price = target.get("price")
        replacement = dict(target)
        replacement.update({
            "name": name,
            "price": price,
            "price_type": target.get("price_type") or "from",
            "category_id": candidate.get("category_id"),
            "master_category_id": candidate.get("master_category_id"),
            "category_name_am": candidate.get("category_name_am"),
            "category_name_ru": candidate.get("category_name_ru"),
            "category_name_en": candidate.get("category_name_en"),
            "master_name_am": candidate.get("master_name_am"),
            "master_name_ru": candidate.get("master_name_ru"),
            "master_name_en": candidate.get("master_name_en"),
            "catalog_match_score": candidate.get("catalog_match_score"),
            "catalog_second_score": candidate.get("catalog_second_score"),
            "catalog_match_margin": candidate.get("catalog_match_margin"),
            "catalog_match_status": "matched",
            "catalog_options": [],
        })
        target_index = services.index(target)
        services[target_index] = replacement
        pending["args"]["services"] = services
        return True

    async def _extract_pending_partner_data(self, message: str, language: str) -> dict[str, Any]:
        """Extract partner data without ever breaking the collecting-data state.

        Deterministic extraction handles phone/mode and common address forms.
        Groq is used only as a text-to-JSON bridge for genuinely free-form input;
        it cannot choose IDs or write data.
        """
        import re

        text = " ".join(str(message or "").strip().split())
        result: dict[str, Any] = {}
        if not text:
            return result

        # Phone is deterministic and must never depend on model output.
        phone_match = re.search(r"(?:\+?374|0)?[ -]?(?:\d[ -]?){8,9}", text)
        if phone_match:
            raw = re.sub(r"[^0-9+]", "", phone_match.group(0))
            try:
                import data_core
                normalized = data_core.normalize_phone_number(raw)
                if normalized:
                    result["phone"] = normalized
            except Exception:
                pass

        folded = text.casefold()
        mobile_markers = (
            "выезжаю", "выезд", "к клиенту", "на выезде", "mobile",
            "գնում եմ", "այցել", "մեկնում եմ", "հաճախորդի մոտ",
        )
        address_markers = (
            "по этому адресу", "на месте", "в этом адресе", "по адресу",
            "at address", "այս հասցեում", "հասցեում", "այս հասցեով",
            "աշխատում եմ", "սպասարկում եմ տեղում",
        )
        has_mobile = any(x in folded for x in mobile_markers)
        has_address = any(x in folded for x in address_markers)
        if has_mobile and has_address:
            result["service_mode"] = "both"
        elif has_mobile:
            result["service_mode"] = "mobile"
        elif has_address:
            result["service_mode"] = "at_address"

        # Explicit address labels are fully deterministic.
        address_match = re.search(
            r"(?:адрес\\s*[:\\-]?\\s*|address\\s*[:\\-]?\\s*|հասցե\\s*[:\\-]?\\s*)"
            r"(.+?)(?=(?:тел(?:ефон)?|тел\\.|phone|հեռախոս|$))",
            text,
            flags=re.IGNORECASE,
        )
        if address_match:
            address = address_match.group(1).strip(" ,.;")
            if address:
                result["address_text"] = address

        # Common conversational form: "Երևան, Բյուզանդի 1, աշխատում եմ ... հեռ ..."
        # Keep the location prefix only; do not mistake the rest of the sentence
        # for an address.
        if not result.get("address_text"):
            mode_markers = (
                "աշխատում եմ", "работаю", "выезжаю", "выезд",
                "где работаю", "սպասարկում եմ", "սպասարկում",
                "տեղում", "հաճախորդի մոտ",
            )
            prefix = text
            lower_prefix = prefix.casefold()
            marker_positions = [lower_prefix.find(marker) for marker in mode_markers if lower_prefix.find(marker) >= 0]
            if marker_positions:
                candidate = prefix[:min(marker_positions)].strip(" ,.;:-")
                # Avoid treating a bare conversational preamble as an address.
                if candidate and any(ch.isdigit() for ch in candidate):
                    result["address_text"] = candidate

        # If the address is still unknown, use a strict structured-output bridge.
        # GPT-OSS 20B supports strict JSON Schema; all fields are required and
        # nullable, so the model cannot answer with an empty/non-JSON payload.
        if not result.get("address_text"):
            prompt = (
                "Extract only facts explicitly present in the partner's message. "
                "Do not invent, normalize locations, choose IDs, classify services, "
                "or add explanations. Return exactly one JSON object. "
                "For absent fields use null. "
                f"Language: {language}. "
                f"Message: {json.dumps(text, ensure_ascii=False)}"
            )
            schema = {
                "type": "object",
                "properties": {
                    "address_text": {"type": ["string", "null"]},
                    "phone": {"type": ["string", "null"]},
                    "service_mode": {
                        "type": ["string", "null"],
                        "enum": ["at_address", "mobile", "both", None],
                    },
                },
                "required": ["address_text", "phone", "service_mode"],
                "additionalProperties": False,
            }
            try:
                response, _provider_used = await self._chat_completion_with_fallback(
                    messages=[{"role": "user", "content": prompt}],
                    tools=None,
                    tool_choice=None,
                    temperature=0,
                    max_tokens=300,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "partner_pending_data",
                            "strict": True,
                            "schema": schema,
                        },
                    },
                )
                raw = (response.choices[0].message.content or "{}").strip()
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    for key in ("address_text", "phone", "service_mode"):
                        value = parsed.get(key)
                        if value not in (None, ""):
                            result[key] = value
                    if result.get("phone"):
                        import data_core
                        normalized = data_core.normalize_phone_number(result["phone"])
                        if normalized:
                            result["phone"] = normalized
                    if result.get("service_mode") not in {"at_address", "mobile", "both"}:
                        result.pop("service_mode", None)
            except Exception as exc:
                # Never reset the draft because NLU failed. The deterministic
                # fields already extracted above remain valid and ToolRegistry
                # will simply ask for whatever business data is still missing.
                logger.warning("Pending partner data NLU unavailable: %s", str(exc)[:500])

        return result

    @staticmethod
    def _partner_missing_reply(language: str, pending: dict[str, Any]) -> str:
        missing = set(pending.get("missing_fields") or [])
        services = pending.get("args", {}).get("services") or []
        lines = []
        for item in services:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            price = item.get("price")
            if name:
                shown = f" — от {int(float(price)):,} ֏".replace(",", " ") if price not in (None, "") else ""
                lines.append(f"• {name}{shown}")
        # Catalog resolution is backend-only. It never becomes a partner-facing
        # missing field and never interrupts collection of real business data.
        if language == "hy":
            reply = "Հասկացա 👍 Պատրաստում եմ հետևյալ ծառայությունների հայտը:\n\n" + "\n".join(lines)
            reply += "\n\nԽնդրում եմ լրացրեք միայն այն տվյալները, որոնք դեռ անհրաժեշտ են:\n"
            if "address" in missing:
                reply += "📍 Նշեք ծառայությունների հասցեն կամ ուղարկեք լոկացիան։\n"
            if "phone" in missing:
                reply += "📞 Տվեք կապի հեռախոսահամարը։\n"
            if "service_mode" in missing:
                reply += "🚗 Աշխատո՞ւմ եք այս հասցեում, թե՞ մեկնում եք հաճախորդի մոտ։\n"
            if "document" in missing:
                reply += "📎 Կցեք հաստատող փաստաթուղթը։"
            return reply.strip()
        reply = "Понял 👍 Готовлю заявку на эти услуги:\n\n" + "\n".join(lines)
        reply += "\n\nПожалуйста, дайте только недостающие данные:\n"
        if "address" in missing:
            reply += "📍 Укажите адрес оказания услуг или отправьте геолокацию.\n"
        if "phone" in missing:
            reply += "📞 Укажите номер телефона для связи.\n"
        if "service_mode" in missing:
            reply += "🚗 Укажите: услуги по этому адресу или выезжаете к клиенту.\n"
        if "document" in missing:
            reply += "📎 Прикрепите подтверждающий документ."
        return reply.strip()

    async def chat(
        self,
        telegram_id: int,
        context_type: ContextType | str,
        message: str,
        *,
        extra_context: dict[str, Any] | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        role = self._context(context_type)
        language = language or self._lang(message)
        trusted = self._trusted(role, telegram_id, extra_context)

        # Admin application requests are often explicit ("հայտ #39").
        # Resolve that stable backend reference before Groq. This removes an
        # unnecessary get_application tool round and, more importantly, keeps
        # the model focused on the requested edit instead of rediscovering the
        # same application through several expensive calls.
        if role == ContextType.ADMIN:
            import re
            match = re.search(r"(?:#|№|\bID\b|\bИД\b)\s*(\d+)", str(message or ""), re.IGNORECASE)
            if match and any(word in str(message or "").casefold() for word in (
                "հայտ", "заяв", "application", "ուղղ", "исправ", "փոխ", "измен",
                "fix", "edit", "գին", "цена", "price",
            )):
                try:
                    import data_core
                    app_id = int(match.group(1))
                    app = data_core.get_application_full(app_id)
                    if app:
                        services = data_core.application_service_items(app_id)
                        trusted["active_application"] = {
                            "application_id": app_id,
                            "business_name": app.get("business_name"),
                            "status": app.get("status"),
                            "services": [
                                {
                                    "service_index": x.get("service_index"),
                                    "name": x.get("name"),
                                    "price": x.get("price"),
                                    "price_type": x.get("price_type"),
                                    "category_name_am": x.get("category_name_am"),
                                    "category_name_ru": x.get("category_name_ru"),
                                }
                                for x in services
                            ],
                        }
                except Exception:
                    logger.exception("Failed to prefetch admin application context")

        history = await self._supabase_history(telegram_id, role)
        # Never send raw historical tool payloads back to Groq. Admin requests
        # can otherwise accumulate catalogue/application JSON and exceed the
        # model's 8k TPM input limit.
        history = self._compact_history(
            history,
            max_chars=4000 if role == ContextType.ADMIN else (3500 if role == ContextType.REGISTRATION else 7000),
            max_items=5 if role in (ContextType.ADMIN, ContextType.REGISTRATION) else 8,
        )
        session_context = await self._session_context(telegram_id, role)

        # HARD FAST PATH / ACTION STATE:
        # awaiting_confirmation never touches Groq. collecting_data continues
        # the same action and only extracts missing slots.
        state = SessionState.from_dict(session_context)
        pending = state.pending_action

        # Bulk category resolution has a backend-owned confirmation flow.
        # Handle it before the generic ToolRegistry confirmation path because
        # this pending draft intentionally has no ToolRegistry action name.
        if role == ContextType.ADMIN and isinstance(pending, dict) and pending.get("type") == "bulk_resolve_categories":
            pending_fast = await self._admin_category_pending_fast_path(
                telegram_id, message, pending, language
            )
            if pending_fast is not None:
                await self._save_history(
                    telegram_id, role, "ai", str(pending_fast.get("reply") or ""),
                    {
                        "fast_path": True,
                        "confirmed": pending_fast.get("confirmed"),
                        "selection_rejected": pending_fast.get("selection_rejected"),
                    },
                )
                return pending_fast

        if pending:
            pending_status = str(pending.get("status") or "").upper()
            # SUBMITTED is a partner-application lifecycle state only.
            # An ADMIN action must never be trapped by the partner "wait for
            # administrator review" response.
            if role == ContextType.PARTNER and pending_status == "SUBMITTED":
                # SUBMITTED is an application lifecycle status, not an active
                # conversational lock. A successful submission must never block
                # the partner from creating the next service/application.
                #
                # Older sessions could retain this marker after the application
                # had already been written to partner_applications. Clear the
                # stale session state and continue with the new user request.
                await self._clear_pending(telegram_id, role)
                pending = None

            pending_state = str(pending.get("state") or "awaiting_confirmation")

            # A confirmation must never be re-parsed as new service data.
            # Older sessions could contain a COLLECTING_DATA marker even though
            # the UI had already rendered the final preview. If the partner now
            # answers "yes", promote that prepared payload directly to the normal
            # atomic confirmation path instead of rebuilding a second preview.
            if (
                role == ContextType.PARTNER
                and pending_state == "collecting_data"
                and self._is_confirmation(message)
                and isinstance(pending.get("args"), dict)
                and isinstance(pending.get("args", {}).get("services"), list)
                and pending.get("args", {}).get("services")
                # If a final preview was already rendered, the confirmation
                # must confirm that exact prepared action. Older sessions may
                # still carry stale missing_fields from the collection phase;
                # those must not turn "այո" into a second preview.
                and (
                    not pending.get("missing_fields")
                    or bool(str(pending.get("summary") or "").strip())
                )
            ):
                pending = dict(pending)
                pending["state"] = "awaiting_confirmation"
                pending["status"] = "AWAITING_CONFIRMATION"
                pending_state = "awaiting_confirmation"
                await self._update_session_context(
                    telegram_id, role, {"pending_action": pending}
                )

            if self._is_cancel(message):
                await self._clear_pending(telegram_id, role)
                reply = self._cancel_text(language)
                await self._save_history(telegram_id, role, "ai", reply, {"cancelled": True, "fast_path": True})
                return {"reply": reply, "cancelled": True, "fast_path": True}

            if pending_state in {"awaiting_confirmation", "submitted", "executing"}:
                if self._is_confirmation(message):
                    is_partner_submission = (
                        role == ContextType.PARTNER
                        and str(pending.get("name") or "").strip() in {"add_service", "add_services", "register_business"}
                    )

                    # The session row is locked by PostgreSQL while the action
                    # is claimed. This is the real idempotency gate: two
                    # concurrent "այո" messages cannot both execute the write.
                    claim = await asyncio.to_thread(
                        platform_db.claim_ai_pending_action,
                        int(telegram_id),
                        self._storage_role(role),
                        partner_submission=is_partner_submission,
                    )
                    claim_status = str(claim.get("status") or "")
                    if claim_status == "executing":
                        reply = (
                            "⏳ Գործողությունն արդեն կատարվում է։"
                            if language == "hy"
                            else "⏳ Действие уже выполняется."
                            if language == "ru"
                            else "⏳ The action is already being processed."
                        )
                        return {"reply": reply, "confirmation_pending": True, "processing": True, "fast_path": True}
                    if claim_status != "claimed":
                        reply = (
                            "⚠️ Ակտիվ գործողություն չի գտնվել։"
                            if language == "hy"
                            else "⚠️ Активного действия для подтверждения не найдено."
                            if language == "ru"
                            else "⚠️ No active action is waiting for confirmation."
                        )
                        return {"reply": reply, "confirmation_pending": False, "fast_path": True}

                    claimed = dict(claim.get("pending_action") or {})
                    pending_name = str(claimed.get("name") or "").strip()
                    pending_args = dict(claimed.get("args") or {})

                    try:
                        confirm_tools = ToolRegistry(
                            telegram_id=int(telegram_id),
                            context_type=role,
                            trusted_context=trusted,
                            session_state=state.to_dict(),
                        )
                        result = await confirm_tools.execute_confirmed(
                            pending_name, pending_args
                        )

                        # A successful action consumes its confirmation state.
                        # A repeated "այո" now finds no pending action and can
                        # never execute the same application twice.
                        await self._clear_pending(telegram_id, role)

                        if is_partner_submission:
                            requires_document = bool(
                                isinstance(result, dict) and result.get("document_required")
                            )
                            if requires_document:
                                reply = (
                                    "⏳ Հայտը ստեղծվեց և ուղարկվեց ստուգման։ "
                                    "Ուղղության հաստատման համար անհրաժեշտ է կցել փաստաթուղթ։"
                                    if language == "hy"
                                    else "⏳ Заявка создана и отправлена на проверку. "
                                         "Для подтверждения направления нужно прикрепить документ."
                                    if language == "ru"
                                    else "⏳ The application was created and sent for review. "
                                         "A document is required to verify the direction."
                                )
                            else:
                                reply = (
                                    "⏳ Հայտը ուղարկվեց ստուգման։" if language == "hy"
                                    else "⏳ Заявка отправлена на проверку."
                                    if language == "ru"
                                    else "⏳ The application was sent for review."
                                )
                        elif pending_name == "admin_approve_application":
                            import data_core
                            application_id = int(pending_args.get("application_id") or 0)
                            verified = data_core.get_application_full(application_id)
                            verified_status = str((verified or {}).get("status") or "").strip().lower()
                            if verified_status != "approved":
                                raise RuntimeError(
                                    f"approval_verification_failed: application={application_id} status={verified_status or 'missing'}"
                                )
                            # Approve is deliberately NOT activation.
                            if language == "hy":
                                reply = f"✅ Հայտ #{application_id}-ը հաստատված է։ Ծառայությունները դեռ ակտիվացված չեն։"
                            elif language == "ru":
                                reply = f"✅ Заявка #{application_id} одобрена. Услуги ещё не активированы."
                            else:
                                reply = f"✅ Application #{application_id} is approved. Services are not active yet."
                        elif pending_name == "admin_activate_application_services":
                            import data_core
                            application_id = int(pending_args.get("application_id") or 0)
                            verified = data_core.get_application_full(application_id)
                            verified_status = str((verified or {}).get("status") or "").strip().lower()
                            if verified_status != "approved":
                                raise RuntimeError(
                                    f"activation_verification_failed: application={application_id} status={verified_status or 'missing'}"
                                )
                            verified_services = data_core.application_service_items(application_id)
                            active_count = sum(
                                1 for item in verified_services
                                if str(item.get("status") or "").strip().lower() == "active"
                            )
                            if not verified_services or active_count != len(verified_services):
                                raise RuntimeError(
                                    f"activation_verification_failed: application={application_id} active={active_count}/{len(verified_services)}"
                                )
                            if language == "hy":
                                reply = f"✅ Հայտ #{application_id}-ի {active_count} ծառայությունները ակտիվացված են։"
                            elif language == "ru":
                                reply = f"✅ У заявки #{application_id} активировано услуг: {active_count}."
                            else:
                                reply = f"✅ Application #{application_id}: {active_count} services are active."
                        elif pending_name == "admin_reject_application":
                            reply = (
                                "✅ Հայտը մերժվեց։"
                                if language == "hy"
                                else "✅ Заявка отклонена."
                                if language == "ru"
                                else "✅ The application was rejected."
                            )
                        elif pending_name == "admin_apply_catalog_resolution":
                            reply = (
                                "✅ Կատեգորիաները կիրառվեցին։"
                                if language == "hy"
                                else "✅ Категории применены."
                                if language == "ru"
                                else "✅ Categories applied."
                            )
                        else:
                            reply = self._done_text(language)

                        await self._save_history(
                            telegram_id, role, "ai", reply,
                            {
                                "confirmed_action": claimed,
                                "tool_result": result,
                                "fast_path": True,
                            },
                        )
                        return {
                            "reply": reply,
                            "tool_result": result,
                            "confirmed": True,
                            "fast_path": True,
                        }

                    except Exception as exc:
                        # The action was claimed before execution. Restore it
                        # only when the backend transaction failed, so the
                        # partner/admin can safely retry the same confirmation.
                        await asyncio.to_thread(
                            platform_db.restore_ai_pending_action,
                            int(telegram_id),
                            self._storage_role(role),
                            claimed,
                        )
                        logger.exception("Pending confirmation execution failed")
                        reply = self._error_text(language)
                        await self._save_history(
                            telegram_id, role, "ai", reply,
                            {
                                "confirmed_action": claimed,
                                "error": str(exc)[:1000],
                                "fast_path": True,
                            },
                        )
                        return {
                            "reply": reply,
                            "confirmed": False,
                            "confirmation_required": True,
                            "fast_path": True,
                            "error": str(exc)[:240],
                        }

                reply = (
                    "Նախ հաստատեք կամ չեղարկեք պատրաստված հայտը։"
                    if language == "hy"
                    else "Сначала подтвердите или отмените подготовленную заявку."
                    if language == "ru"
                    else "Please confirm or cancel the prepared application."
                )
                return {"reply": reply, "confirmation_pending": True, "fast_path": True}

            if pending_state == "collecting_data" and role == ContextType.PARTNER:
                pending = dict(pending)
                pending_args = dict(pending.get("args") or {})
                collected = dict(pending_args)

                # Platform objects are trusted facts; the model never invents them.
                message_context = (extra_context or {}).get("message_context") or {}
                if isinstance(message_context, dict):
                    document = message_context.get("document")
                    location = message_context.get("location")
                    contact = message_context.get("contact")
                    if document:
                        collected["document"] = document
                    if location:
                        collected["service_location"] = location
                    if contact and contact.get("phone_number"):
                        import data_core
                        normalized = data_core.normalize_phone_number(contact.get("phone_number"))
                        if normalized:
                            collected["phone"] = normalized

                # First, try to resolve a clarification against the SAME
                # pending service list. This is deliberately before generic NLU:
                # "Добавь ремонт бытовых холодильников" can be a clarification
                # of the unresolved refrigerator item, not a new action.
                merged_service = await self._merge_pending_partner_service(pending, message)
                if merged_service:
                    pending_args["services"] = pending.get("args", {}).get("services") or []
                    collected = pending_args

                extracted = await self._extract_pending_partner_data(message, language)
                for key, value in extracted.items():
                    if value not in (None, ""):
                        collected[key] = value

                tools = ToolRegistry(
                    telegram_id=int(telegram_id),
                    context_type=role,
                    trusted_context=trusted,
                    session_state=state.to_dict(),
                )
                result = await tools.execute("add_services", collected)

                if result.get("requires_data"):
                    action = result.get("action") or {"name": "add_services", "args": collected}
                    new_pending = {
                        "name": "add_services",
                        "args": dict(action.get("args") or collected),
                        "missing_fields": list(result.get("missing_fields") or []),
                        "state": "collecting_data",
                        "status": "COLLECTING_DATA",
                        "created_at": pending.get("created_at") or int(time.time()),
                    }
                    await self._update_session_context(
                        telegram_id, role, {"pending_action": new_pending}
                    )
                    reply = self._partner_missing_reply(language, new_pending)
                    await self._save_history(
                        telegram_id, role, "ai", reply,
                        {"pending_action": new_pending, "collecting_data": True},
                    )
                    return {"reply": reply, "pending_action": new_pending, "collecting_data": True}

                if result.get("requires_confirmation"):
                    action = result.get("action") or {}
                    new_pending = {
                        "name": str(action.get("name") or "add_services"),
                        "args": dict(action.get("args") or {}),
                        "summary": str(result.get("summary") or ""),
                        "state": "awaiting_confirmation",
                        "status": "AWAITING_CONFIRMATION",
                        "submission_token": str(pending.get("submission_token") or secrets.token_urlsafe(24)),
                        "created_at": pending.get("created_at") or int(time.time()),
                    }
                    await self._set_pending(telegram_id, role, new_pending)
                    summary = self._partner_action_summary(language, new_pending, "")
                    # Add only facts actually present in the pending action.
                    # Mobile services use coverage, not a fabricated company address.
                    args = new_pending["args"]
                    services_preview = args.get("services") or []
                    first_service = services_preview[0] if services_preview and isinstance(services_preview[0], dict) else {}
                    coverage = (
                        args.get("coverage")
                        or first_service.get("coverage")
                        or first_service.get("service_location")
                    )
                    address = args.get("address_text")
                    mode = args.get("service_mode") or first_service.get("service_mode")
                    mode_label_hy = (
                        "մեկնում հաճախորդի մոտ" if mode == "mobile"
                        else "այս հասցեում" if mode == "at_address"
                        else "երկու եղանակով" if mode == "both"
                        else "չնշված"
                    )
                    if language == "hy":
                        context_lines = [f"🚗 Ռեժիմ՝ {mode_label_hy}"]
                        if isinstance(coverage, dict):
                            city = coverage.get("city")
                            marz = coverage.get("marz")
                            district = coverage.get("district")
                            parts = [str(x) for x in (city, district, marz) if x]
                            if parts:
                                context_lines.insert(0, "📍 Տարածք՝ " + ", ".join(parts))
                        elif address:
                            context_lines.insert(0, f"📍 Հասցե՝ {address}")
                        # Canonical human preview: no internal fields and no second preview.
                        summary = "Ավելացնել հետևյալ ծառայությունները:\n" + summary.split("\n\n")[0].replace("Ավելացնել «" + str(args.get("company_name") or "ընկերություն") + "» ընկերությունում հետևյալ ծառայությունները:", "").lstrip() + "\n" + "\n".join(context_lines) + "\n\nՀաստատո՞ւմ եք։"
                    elif language == "ru":
                        context_lines = [f"🚗 Режим: {mode or 'не указан'}"]
                        if isinstance(coverage, dict):
                            parts = [str(x) for x in (coverage.get("city"), coverage.get("district"), coverage.get("marz")) if x]
                            if parts:
                                context_lines.insert(0, "📍 Территория: " + ", ".join(parts))
                        elif address:
                            context_lines.insert(0, f"📍 Адрес: {address}")
                        summary = summary.split("\n\n")[0] + "\n" + "\n".join(context_lines) + "\n\nПодтверждаете?"
                    new_pending["summary"] = summary
                    await self._update_session_context(
                        telegram_id, role, {"pending_action": new_pending}
                    )
                    # Confirmation card is the only visible preview; do not duplicate it as chat text.
                    return {"reply": str(new_pending.get("summary") or ""), "confirmation_required": True, "pending_action": new_pending}
                return {"reply": result.get("reply") or self._error_text(language)}

        if role == ContextType.ADMIN:

            pending_fast = await self._admin_category_pending_fast_path(
                telegram_id,
                message,
                SessionState.from_dict(session_context).pending_action,
                language,
            )
            if pending_fast is not None:
                await self._save_history(
                    telegram_id,
                    role,
                    "ai",
                    str(pending_fast.get("reply") or ""),
                    {
                        "fast_path": True,
                        "confirmed": pending_fast.get("confirmed"),
                        "selection_rejected": pending_fast.get("selection_rejected"),
                    },
                )
                return pending_fast

            fast = await self._admin_fast_path(
                telegram_id, message, session_context, language
            )
            if fast is not None:
                return fast

        state = SessionState.from_dict(session_context)
        tools = ToolRegistry(
            telegram_id=int(telegram_id),
            context_type=role,
            trusted_context=trusted,
            session_state=state.to_dict(),
        )
        definitions = tools.definitions()

        # The session state is backend state, not model-authored identity.
        trusted_for_prompt = dict(trusted)
        if role == ContextType.ADMIN:
            pending_for_prompt = SessionState.from_dict(session_context).pending_action
            if isinstance(pending_for_prompt, dict) and pending_for_prompt.get("type") == "bulk_resolve_categories":
                trusted_for_prompt["pending_action"] = {
                    "type": "bulk_resolve_categories",
                    "application_id": int(pending_for_prompt.get("application_id") or 0),
                    "unresolved_count": len(pending_for_prompt.get("unresolved_ambiguities") or []),
                }
        # Deterministic partner service command path.
        # When the user explicitly names services and prices, Python can parse
        # the small structured part reliably; there is no reason to spend a
        # tool-call round asking gpt-oss-20b to emit a function call.
        if role == ContextType.PARTNER and isinstance(message, str):
            parsed_service_action = self._parse_explicit_partner_services(message)
            if parsed_service_action:
                try:
                    result = await tools.execute("add_services", {
                        "company_id": trusted_for_prompt.get("current_company_id"),
                        "services": parsed_service_action,
                    })
                    if result.get("requires_data"):
                        action = result.get("action") or {}
                        pending_action = {
                            "name": str(action.get("name") or "add_services"),
                            "args": dict(action.get("args") or {}),
                            "missing_fields": list(result.get("missing_fields") or []),
                            "state": "collecting_data",
                            "status": "COLLECTING_DATA",
                            "created_at": int(time.time()),
                        }
                        await self._update_session_context(
                            telegram_id, role, {"pending_action": pending_action}
                        )
                        reply = self._partner_missing_reply(language, pending_action)
                        await self._save_history(
                            telegram_id, role, "ai", reply,
                            {"collecting_data": True, "action": pending_action,
                             "deterministic_parser": True},
                        )
                        return {
                            "reply": reply,
                            "pending_action": pending_action,
                            "collecting_data": True,
                            "tool_calls": [{
                                "name": "add_services",
                                "arguments": parsed_service_action,
                                "deterministic": True,
                            }],
                        }
                    if result.get("requires_confirmation"):
                        action = result.get("action") or {}
                        # Keep explicit user facts (price type, service mode and
                        # coverage) authoritative in the confirmation draft. The Data Core
                        # may normalize these fields for storage, but it must not turn
                        # "դրամից" into a fixed price or lose the requested coverage.
                        action_args = dict(action.get("args") or {})
                        result_services = list(action_args.get("services") or [])
                        if result_services and parsed_service_action:
                            merged_services = []
                            for idx, parsed_item in enumerate(parsed_service_action):
                                normalized = dict(result_services[idx]) if idx < len(result_services) and isinstance(result_services[idx], dict) else {}
                                normalized.update(parsed_item)
                                merged_services.append(normalized)
                            action_args["services"] = merged_services
                        elif parsed_service_action:
                            action_args["services"] = [dict(x) for x in parsed_service_action]
                        first_explicit = parsed_service_action[0] if parsed_service_action else {}
                        if first_explicit.get("coverage"):
                            action_args["coverage"] = first_explicit["coverage"]
                        if first_explicit.get("service_mode"):
                            action_args["service_mode"] = first_explicit["service_mode"]
                        submission_token = secrets.token_urlsafe(24)
                        action_args["submission_token"] = submission_token
                        pending_action = {
                            "name": str(action.get("name") or "add_services"),
                            "args": action_args,
                            "summary": str(result.get("summary") or ""),
                            "state": "awaiting_confirmation",
                            "status": "AWAITING_CONFIRMATION",
                            "submission_token": submission_token,
                            "created_at": int(time.time()),
                        }
                        await self._set_pending(telegram_id, role, pending_action)
                        summary = self._partner_action_summary(
                            language, pending_action, ""
                        )
                        args = pending_action["args"]
                        services_preview = args.get("services") or []
                        first_service = services_preview[0] if services_preview and isinstance(services_preview[0], dict) else {}
                        coverage = (
                            args.get("coverage")
                            or first_service.get("coverage")
                            or first_service.get("service_location")
                        )
                        mode = args.get("service_mode") or first_service.get("service_mode")
                        if language == "hy":
                            context_lines = []
                            if isinstance(coverage, dict):
                                parts = [str(x) for x in (coverage.get("city"), coverage.get("district"), coverage.get("marz")) if x]
                                if parts:
                                    context_lines.append("📍 Տարածք՝ " + ", ".join(parts))
                            if args.get("address_text"):
                                context_lines.append(f"📍 Հասցե՝ {args['address_text']}")
                            if mode:
                                context_lines.append(
                                    "🚗 Ռեժիմ՝ " + (
                                        "մեկնում հաճախորդի մոտ" if mode == "mobile"
                                        else "այս հասցեում" if mode == "at_address"
                                        else "երկու եղանակով" if mode == "both"
                                        else str(mode)
                                    )
                                )
                            if context_lines:
                                summary = summary.split("\n\n")[0] + "\n" + "\n".join(context_lines) + "\n\nՀաստատո՞ւմ եք։"
                        elif language == "ru":
                            context_lines = []
                            if isinstance(coverage, dict):
                                parts = [str(x) for x in (coverage.get("city"), coverage.get("district"), coverage.get("marz")) if x]
                                if parts:
                                    context_lines.append("📍 Территория: " + ", ".join(parts))
                            if args.get("address_text"):
                                context_lines.append(f"📍 Адрес: {args['address_text']}")
                            if mode:
                                context_lines.append(f"🚗 Режим: {mode}")
                            if context_lines:
                                summary = summary.split("\n\n")[0] + "\n" + "\n".join(context_lines) + "\n\nПодтверждаете?"
                        pending_action["summary"] = summary
                        # Do not persist the preview as a second chat message.
                        # The current response renders pending_action.summary once.
                        # Persist only the state, not a duplicate visible message.
                        # The deterministic parser is an internal implementation detail.
                        # Do not expose its tool-call payload to the partner UI: the UI must
                        # render exactly one human confirmation preview.
                        # The confirmation widget renders pending_action.summary.
                        # Do not also return the same summary as chat text, otherwise
                        # partners see the confirmation preview twice.
                        return {
                            "reply": str(pending_action.get("summary") or ""),
                            "confirmation_required": True,
                            "pending_action": pending_action,
                        }
                    return result
                except Exception as exc:
                    logger.exception("Deterministic partner service command failed")
                    # Fall through to normal AI handling for commands that the
                    # parser could not safely execute.
                    if str(exc) == "company_context_required":
                        pass

        prompt = PromptFactory.build(
            role,
            message=message,
            history=history,
            trusted_context=trusted_for_prompt,
            language=language,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        started = time.monotonic()

        await self._save_history(
            telegram_id, role, "user", message,
            {"context_type": role.value},
        )

        tool_log: list[dict[str, Any]] = []
        last_tool_result: dict[str, Any] | None = None

        for round_no in range(self.max_tool_rounds):
            estimated_input_tokens = self._approx_tokens(
                json.dumps(messages, ensure_ascii=False, default=str)
                + json.dumps(definitions or [], ensure_ascii=False, default=str)
            )
            if estimated_input_tokens > 6500:
                logger.warning(
                    "AIManager prompt budget high: context=%s round=%s estimated_input_tokens=%s",
                    role.value, round_no, estimated_input_tokens,
                )
            try:
                # Partner dialogue is not a forced tool workflow.
                # Explicit service commands are handled by the deterministic
                # action parser above; all other messages use normal auto selection.
                partner_tool_choice = "auto"

                response, provider_used = await self._chat_completion_with_fallback(
                    messages=messages,
                    tools=definitions or None,
                    tool_choice=("auto" if definitions else None),
                    temperature=0.1,
                    max_tokens=700 if role == ContextType.ADMIN else 1600,
                )
                await self._cost_log(
                    telegram_id, role, getattr(response, "usage", None),
                    extra_context=extra_context,
                    provider=provider_used,
                    model=(
                        self.model if provider_used == "groq"
                        else os.getenv(
                            "OPENAI_MODEL" if provider_used == "openai" else "OPENROUTER_MODEL",
                            self.model,
                        ).strip()
                    ),
                )
            except Exception as exc:
                await self._cost_log(
                    telegram_id, role, None, status="error", error=str(exc)[:1000],
                    extra_context=extra_context,
                )
                logger.exception("AIManager chat completion failed")
                reply = self._error_text(language)
                await self._save_history(
                    telegram_id, role, "ai", reply,
                    {"error": str(exc)[:1000], "round": round_no},
                )
                return {"reply": reply, "error": "ai_completion_failed"}

            msg = response.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []

            if not calls:
                reply = (msg.content or "").strip() or self._error_text(language)
                await self._save_history(
                    telegram_id, role, "ai", reply,
                    {
                        "tool_calls": tool_log,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                return {"reply": reply, "tool_calls": tool_log, **({"tool_result": last_tool_result} if last_tool_result else {})}

            assistant_calls = []
            for call in calls:
                assistant_calls.append({
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments or "{}",
                    },
                })
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": assistant_calls,
            })

            for call in calls:
                name = str(call.function.name or "").strip()
                args: dict[str, Any] = {}
                try:
                    decoded = json.loads(call.function.arguments or "{}")
                    if not isinstance(decoded, dict):
                        raise ValueError("tool_arguments_must_be_object")
                    args = decoded
                    result = await tools.execute(name, args)
                    if isinstance(result, dict):
                        last_tool_result = result
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)[:1000]}

                tool_log.append({
                    "name": name,
                    "arguments": args,
                    "result": result,
                })
                # Persist the actual tool message. platform_db ensures the
                # legacy database constraint is upgraded before this first write.
                await self._save_history(
                    telegram_id,
                    role,
                    "tool",
                    json.dumps(result, ensure_ascii=False, default=str),
                    {
                        "kind": "tool",
                        "tool_name": name,
                        "arguments": args,
                        "result": result,
                    },
                    tool_call_id=call.id,
                )

                pending_action = result.get("pending_action")
                if isinstance(pending_action, dict) and pending_action.get("type") == "bulk_resolve_categories":
                    pending = dict(pending_action)
                    pending["confirmation_token"] = secrets.token_urlsafe(24)
                    await self._set_pending(telegram_id, role, pending)
                    rendered = await self._render_category_pending(pending, language)
                    await self._save_history(
                        telegram_id,
                        role,
                        "ai",
                        str(rendered.get("reply") or ""),
                        {"pending_action": pending, "tool_name": name},
                    )
                    return {
                        **rendered,
                        "tool_calls": tool_log,
                    }

                if result.get("requires_confirmation") or (
                    name == "admin_approve_application"
                    and result.get("can_approve")
                    and isinstance(result.get("action"), dict)
                ):
                    action = result.get("action") or {}
                    action_args = dict(action.get("args") or {})
                    # For partner ADD_SERVICES, explicit user facts are authoritative.
                    # The model must not turn "դրամից" into fixed or leak internal fields.
                    if role == ContextType.PARTNER and name in {"add_service", "add_services", "register_business"}:
                        explicit = self._parse_explicit_partner_services(message) or []
                        if explicit:
                            model_services = list(action_args.get("services") or [])
                            merged = []
                            for idx, item in enumerate(explicit):
                                base = dict(model_services[idx]) if idx < len(model_services) and isinstance(model_services[idx], dict) else {}
                                base.update(item)
                                merged.append(base)
                            action_args["services"] = merged
                            first = explicit[0]
                            if first.get("coverage"):
                                action_args["coverage"] = first["coverage"]
                                action_args["service_location"] = first["coverage"]
                            if first.get("service_mode"):
                                action_args["service_mode"] = first["service_mode"]
                        action_args["submission_token"] = secrets.token_urlsafe(24)
                    pending_action = {
                        "name": str(action.get("name") or name),
                        "args": action_args,
                        "summary": str(result.get("summary") or ""),
                        "state": "awaiting_confirmation",
                        "status": "AWAITING_CONFIRMATION",
                        "created_at": int(time.time()),
                    }
                    await self._set_pending(
                        telegram_id, role, pending_action
                    )
                    if role == ContextType.PARTNER and pending_action["name"] in {"add_service", "add_services", "register_business"}:
                        summary = self._partner_action_summary(language, pending_action, "")
                        pending_action["summary"] = summary
                        # Confirmation UI is the single visible preview.
                        return {
                            "reply": str(pending_action.get("summary") or ""),
                            "confirmation_required": True,
                            "pending_action": pending_action,
                            "tool_calls": tool_log,
                        }
                    summary = self._partner_action_summary(
                        language, pending_action, pending_action["summary"]
                    )
                    pending_action["summary"] = summary
                    reply = summary
                    await self._save_history(
                        telegram_id, role, "ai", reply,
                        {"confirmation_required": True, "action": pending_action},
                    )
                    return {
                        "reply": reply,
                        "confirmation_required": True,
                        "pending_action": pending_action,
                        "tool_calls": tool_log,
                    }

                # Keep the raw backend result in the tool-call loop so Groq can
                # formulate a natural-language answer from verified data.
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": self._compact_tool_result(
                        name,
                        result,
                        3500 if role == ContextType.ADMIN else 7000,
                    ),
                })

                # Persist a small, backend-derived focus state for pronouns and
                # "show next" follow-ups. Never treat model arguments as proof.
                if result.get("ok"):
                    patch: dict[str, Any] = {}
                    item = result.get("item")
                    items = result.get("items")
                    if isinstance(item, dict) and item.get("id") is not None:
                        entity_type = (
                            str(session_context.get("current_entity_type") or "").strip()
                            if name == "get_next_item"
                            else (
                                "application" if name.endswith("application")
                                else ("order" if "order" in name else
                                      ("service" if "service" in name else "entity"))
                            )
                        ) or "entity"
                        patch.update({
                            "last_displayed_entity_id": {
                                "type": entity_type,
                                "id": item.get("id"),
                            },
                            "current_entity_id": item.get("id"),
                            "current_entity_type": entity_type,
                        })
                        current_list = session_context.get("current_list") or []
                        item_id = item.get("id")
                        for idx, row in enumerate(current_list):
                            if isinstance(row, dict) and str(row.get("id")) == str(item_id):
                                patch["current_pagination_index"] = idx
                                break
                    pagination = result.get("pagination")
                    if isinstance(pagination, dict) and pagination.get("index") is not None:
                        try:
                            patch["current_pagination_index"] = int(pagination["index"])
                        except (TypeError, ValueError):
                            pass

                    if isinstance(items, list):
                        # Keep the backend-derived display objects in session state so
                        # "show next" can render the next item without re-querying.
                        # Do not store arbitrary model-provided data here.
                        safe_items = [
                            {
                                str(k): v
                                for k, v in x.items()
                                if str(k) in {
                                    "id", "name", "title", "status", "price",
                                    "currency", "description", "company_name",
                                    "business_name", "partner_id", "client_id",
                                    "address", "phone", "created_at", "updated_at",
                                }
                            }
                            for x in items
                            if isinstance(x, dict) and x.get("id") is not None
                        ][:50]
                        if safe_items:
                            patch["current_list"] = safe_items
                            patch["current_pagination_index"] = 0
                            first = safe_items[0]
                            if len(safe_items) == 1:
                                patch["last_displayed_entity_id"] = {
                                    "type": (
                                        "order" if "order" in name
                                        else ("service" if "service" in name else "entity")
                                    ),
                                    "id": first["id"],
                                }
                    if name == "get_my_companies" and isinstance(items, list) and items:
                        patch["current_company_id"] = items[0].get("id")
                    if name == "get_my_services" and isinstance(items, list) and items:
                        patch["current_service_id"] = items[0].get("id")
                    if patch:
                        await self._update_session_context(
                            telegram_id, role, patch
                        )

        reply = self._error_text(language)
        await self._save_history(
            telegram_id, role, "ai", reply,
            {"error": "tool_loop_limit", "latency_ms": round((time.monotonic() - started) * 1000)},
        )
        return {"reply": reply, "error": "tool_loop_limit"}

    async def handle_message(
        self,
        telegram_id: int,
        user_message: str,
        context_type: AIContext | ContextType | str,
        *,
        extra_context: dict[str, Any] | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        return await self.chat(
            telegram_id,
            self._context(context_type),
            user_message,
            extra_context=extra_context,
            language=language,
        )




















