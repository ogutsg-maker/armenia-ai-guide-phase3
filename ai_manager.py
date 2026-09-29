"""Unified AI entry point for REGISTRATION, CLIENT, PARTNER and ADMIN.

AIManager is the only conversational AI gateway. Groq may request typed tools,
but tools are executed by backend code only. No system-role messages are used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from groq import AsyncGroq

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
        self.db = db_pool
        self.model = (
            model
            or os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
            or "openai/gpt-oss-20b"
        )
        self.history = history_provider or HistoryProvider()
        self.max_history = max(2, min(int(max_history), 50))
        self.max_tool_rounds = max(1, min(int(max_tool_rounds), 8))

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
    ):
        data = (
            usage.model_dump()
            if hasattr(usage, "model_dump")
            else (usage if isinstance(usage, dict) else {})
        )
        partner_id = (extra_context or {}).get("partner_id")
        try:
            partner_id = int(partner_id) if partner_id is not None else None
        except (TypeError, ValueError):
            partner_id = None

        return await asyncio.to_thread(
            ai_cost_center.record_usage,
            provider="groq",
            model=self.model,
            chain=context.value.lower(),
            stage="manager",
            operation="chat",
            purpose="Unified AIManager",
            user_id=int(telegram_id),
            partner_id=partner_id,
            input_tokens=int(data.get("prompt_tokens") or data.get("input_tokens") or 0),
            output_tokens=int(data.get("completion_tokens") or data.get("output_tokens") or 0),
            cached_tokens=int(data.get("prompt_cached_tokens") or data.get("cached_tokens") or 0),
            reasoning_tokens=int(data.get("reasoning_tokens") or 0),
            status=status,
            error=error,
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
            return "Ներողություն, տեխնիկական սխալ է տեղի ունեցել։ Փորձեք մի փոքր ուշ։"
        if language == "ru":
            return "Извините, произошла техническая ошибка. Попробуйте позже."
        return "Sorry, a technical error occurred. Please try again later."

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
        app_id = None
        m = re.search(r"(?:#|№)\s*(\d+)", str(message))
        if m:
            app_id = int(m.group(1))
        if app_id is None:
            pending = await self._pending(telegram_id, ContextType.ADMIN)
            pending_text = str(message or "").casefold()
            pending_bulk_request = (
                pending
                and pending.get("type") == "bulk_resolve_categories"
                and re.search(
                    r"(?:դասակարգ|դասավոր|վերագր|կապիր|ուղղիր|ուղղել|fix|classif|categor|resolve|assign|присво|исправ|классифиц)",
                    pending_text,
                )
                and re.search(r"(?:բոլոր|բոլորը|all|все|բոլոր ծառայ|все услуги|all services|ենթաուղղ)", pending_text)
            )
            if pending_bulk_request:
                app_id = int(pending.get("application_id") or 0)
        if not app_id:
            return None

        import data_core
        app = data_core.get_application_full(app_id)
        if not app:
            return None
        services = data_core.application_service_items(app_id)

        text = str(message or "").casefold()
        bulk_action_intent = re.search(
            r"(?:դասակարգ|դասավոր|վերագր|կապիր|ուղղիր|ուղղել|fix|classif|categor|resolve|assign|присво|исправ|классифиц).{0,100}(?:բոլոր|բոլորը|all|все|ծառայ|услуг|service)",
            text,
        ) or re.search(
            r"(?:բոլոր|բոլորը|all|все|բոլոր ծառայ|все услуги|all services).{0,100}(?:դասակարգ|դասավոր|վերագր|կապիր|ուղղիր|ուղղ|fix|classif|categor|resolve|assign|присво|исправ|классифиц)",
            text,
        )
        if bulk_action_intent:
            try:
                import secrets
                pending_existing = await self._pending(telegram_id, ContextType.ADMIN)
                if (
                    pending_existing
                    and pending_existing.get("type") == "bulk_resolve_categories"
                    and int(pending_existing.get("application_id") or 0) == int(app_id)
                ):
                    return await self._render_category_pending(pending_existing, language)

                init = data_core.init_bulk_catalog_resolution(
                    application_id=app_id,
                    actor_user_id=telegram_id,
                )
                pending = dict(init.get("pending_action") or {})
                pending["confirmation_token"] = secrets.token_urlsafe(24)
                await self._set_pending(telegram_id, ContextType.ADMIN, pending)
                return await self._render_category_pending(pending, language)
            except Exception as exc:
                logger.exception("Admin bulk catalog pending_action initialization failed")
                return {"reply": self._error_text(language), "error": str(exc), "fast_path": True}

        # Read-only application catalogue/subcategory requests are deterministic.
        # This keeps follow-ups such as "բոլորը" anchored to the active application
        # instead of sending the user back through the generic application opener.
        catalog_read_intent = re.search(
            r"(?:ենթաուղղ|ենթաուղղություն|ենթակատեգ|կատեգոր|subcategory|subcategor|catalog|category|категор|подкатегор)",
            str(message or "").casefold(),
        )
        all_services_intent = re.search(
            r"(?:\bբոլորը\b|\bբոլոր\b|\ball\b|\bвсе\b|բոլոր ծառայ|все услуги|all services)",
            str(message or "").casefold(),
        )
        if (catalog_read_intent and (all_services_intent or re.search(r"(?:ծառայ|услуг|service)", str(message or "").casefold()))) or (all_services_intent and session_context.get("last_admin_read_intent") == "application_catalog"):
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
        approval_intent = re.search(
            r"(?:հայտ|заяв|application)\s*(?:#|№)?\s*\d*.*?"
            r"(?:հաստատիր|հաստատել|հաստատի|approve|одобр|утверд|ակտիվացրու|активир)",
            str(message or "").casefold(),
        )
        if approval_intent:
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

        document_correction_intent = re.search(
            r"(?:հայտ|заяв|application)\s*(?:#|№)?\s*\d*.*?"
            r"(?:փաստաթուղթ|документ|document).*?"
            r"(?:ուղարկ|ուղղարկ|отправ|попрос|замен|нов|новый|նոր|ճշտ|исправ|replace|resubmit)",
            str(message or "").casefold(),
        )
        if document_correction_intent:
            reason = (
                "Խնդրում ենք ուղարկել նոր փաստաթուղթ։ Նախորդ փաստաթուղթը չի բավարարել ստուգման պահանջներին։"
                if language == "hy" else
                "Пожалуйста, отправьте новый документ. Предыдущий документ не прошёл проверку."
                if language == "ru" else
                "Please send a new verification document. The previous document did not pass verification."
            )
            action = {
                "name": "admin_request_document_correction",
                "args": {"application_id": app_id, "reason": reason},
                "state": "awaiting_confirmation",
            }
            await self._set_pending(telegram_id, ContextType.ADMIN, action)
            summary = (
                f"Հայտ #{app_id}-ի գործընկերոջը խնդրել նոր փաստաթուղթ ուղարկել։ Հաստատե՞լ։"
                if language == "hy" else
                f"Попросить партнёра по заявке #{app_id} отправить новый документ. Подтвердить?"
                if language == "ru" else
                f"Ask the partner for application #{app_id} to send a new document. Confirm?"
            )
            return {"reply": summary, "confirmation_pending": True, "fast_path": True,
                    "application_id": app_id}

        price_edit = self._extract_admin_price_edit(message, services)
        if not price_edit:
            # A bare "fix application #39" is deterministic too: load it and
            # ask what should be changed without spending a Groq call.
            if re.search(r"(?:հայտ|заяв|application)", str(message).casefold()):
                names = [str(x.get("name") or "") for x in services if x.get("name")]
                if language == "hy":
                    reply = (
                        f"Հայտ #{app_id}-ը բացված է։ "
                        f"Ծառայություններ՝ {', '.join(names)}։ "
                        "Ո՞ր ծառայությունը կամ հատկությունն եք ցանկանում փոխել։"
                    )
                elif language == "ru":
                    reply = (
                        f"Заявка #{app_id} открыта. Услуги: {', '.join(names)}. "
                        "Что именно изменить?"
                    )
                else:
                    reply = (
                        f"Application #{app_id} is open. Services: {', '.join(names)}. "
                        "What would you like to change?"
                    )
                return {"reply": reply, "fast_path": True, "application_id": app_id}
            return None

        service_index, service_name = price_edit
        try:
            import data_core
            preview = data_core.prepare_application_service_price_update(
                application_id=app_id,
                service_index=service_index,
                price=price_edit and int(re.search(r"(\d{1,3}(?:[\s.,]\d{3})+|\d{3,7})", str(message)).group(1).replace(" ", "").replace(",", "").replace(".", "")),
                actor_user_id=telegram_id,
            )
            action = dict(preview.get("action") or {})
            await self._set_pending(telegram_id, ContextType.ADMIN, action)
            summary = str(preview.get("summary") or "")
            await self._save_history(
                telegram_id, ContextType.ADMIN, "ai", summary,
                {"fast_path": True, "pending_action": action},
            )
            return {
                "reply": summary,
                "confirmation_pending": True,
                "fast_path": True,
                "tool_result": preview,
            }
        except Exception as exc:
            logger.exception("Admin deterministic price fast path failed")
            return {"reply": self._error_text(language), "error": str(exc)}


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
                try:
                    pending_name = str(pending.get("name") or "").strip()
                    pending_args = dict(pending.get("args") or {})
                    if str(pending.get("state") or "awaiting_confirmation") != "awaiting_confirmation":
                        raise PermissionError("invalid_pending_state")
                    result = await tools.execute_confirmed(pending_name, pending_args)
                    await self._clear_pending(telegram_id, role)
                    reply = self._done_text(language)
                    await self._save_history(
                        telegram_id, role, "ai", reply,
                        {"confirmed_action": pending, "tool_result": result},
                    )
                    return {"reply": reply, "tool_result": result, "confirmed": True}
                except Exception as exc:
                    await self._clear_pending(telegram_id, role)
                    logger.exception("AIManager confirmed action failed")
                    reply = self._error_text(language)
                    await self._save_history(
                        telegram_id, role, "ai", reply,
                        {"confirmed_action": pending, "error": str(exc)[:1000]},
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
            response = await self.client.chat.completions.create(**kwargs)
            await self._cost_log(
                telegram_id, role, getattr(response, "usage", None),
                extra_context=extra_context,
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
            match = re.search(r"(?:#|№)\s*(\d+)", str(message or ""))
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
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=definitions or None,
                    tool_choice="auto" if definitions else None,
                    temperature=0.1,
                    # gpt-oss tool calls can spend completion budget on reasoning before
                    # emitting the JSON arguments. Registration saves may contain many
                    # services, so 900 was too small and produced truncated JSON such as
                    # {"address". Keep registration isolated at a safe 1600-token ceiling.
                    max_tokens=1600 if role == ContextType.REGISTRATION else 1600,
                )
                await self._cost_log(
                    telegram_id, role, getattr(response, "usage", None),
                    extra_context=extra_context,
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

                if result.get("requires_confirmation"):
                    action = result.get("action") or {}
                    pending_action = {
                        "name": str(action.get("name") or name),
                        "args": dict(action.get("args") or {}),
                        "summary": str(result.get("summary") or ""),
                        "state": "awaiting_confirmation",
                        "created_at": int(time.time()),
                    }
                    await self._set_pending(
                        telegram_id, role, pending_action
                    )
                    reply = self._confirmation_text(
                        language, pending_action["summary"]
                    )
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