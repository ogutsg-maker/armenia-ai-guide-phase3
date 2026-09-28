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
    "yes", "да", "այո", "հա", "հաստատել", "հաստատում եմ",
    "подтверждаю", "подтвердить", "confirm", "ok", "okay",
}
_CANCELS = {
    "no", "нет", "ոչ", "չեղարկել", "отмена", "отменить", "cancel",
}


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
        max_tool_rounds: int = 4,
    ):
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        self.client = AsyncGroq(api_key=key)
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

    def _session(self, telegram_id: int, context: ContextType):
        role = context.value.lower()

        def read():
            return (
                platform_db.active_session(int(telegram_id), role, "ai_manager")
                or platform_db.create_session(
                    int(telegram_id), role, "ai_manager", {"history_version": 1}
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
        role = context.value.lower()
        size = max(2, min(int(limit or self.max_history), 50))

        def read():
            session = (
                platform_db.active_session(int(telegram_id), role, "ai_manager")
                or platform_db.create_session(
                    int(telegram_id), role, "ai_manager", {"history_version": 1}
                )
            )
            rows = platform_db.recent_ai_messages(int(session["id"]), size)
            result = []
            for row in rows:
                sender = str(row.get("sender_role") or "").lower()
                if sender == "tool":
                    continue
                result.append({
                    "role": "assistant" if sender in {"ai", "assistant"} else "user",
                    "content": str(row.get("message_text") or row.get("text") or ""),
                })
            return result

        return await asyncio.to_thread(read)

    async def _save_history(
        self,
        telegram_id: int,
        context: ContextType,
        sender: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ):
        role = context.value.lower()

        def write():
            session = (
                platform_db.active_session(int(telegram_id), role, "ai_manager")
                or platform_db.create_session(
                    int(telegram_id), role, "ai_manager", {"history_version": 1}
                )
            )
            return platform_db.add_ai_message(
                int(session["id"]), sender, str(content or "")[:12000], metadata or {}
            )

        return await asyncio.to_thread(write)

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
            return f"{summary}

Հաստատո՞ւմ եք։"
        if language == "ru":
            return f"{summary}

Подтверждаете?"
        return f"{summary}

Confirm?"

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
    def _is_confirmation(message: str) -> bool:
        return " ".join(str(message or "").strip().casefold().split()) in _CONFIRMATIONS

    @staticmethod
    def _is_cancel(message: str) -> bool:
        return " ".join(str(message or "").strip().casefold().split()) in _CANCELS

    async def _pending(self, telegram_id: int, context: ContextType):
        ctx = await self._session_context(telegram_id, context)
        return SessionState.from_dict(ctx).pending_action

    async def _set_pending(
        self, telegram_id: int, context: ContextType, action: dict[str, Any]
    ):
        return await self._update_session_context(
            telegram_id, context, {"pending_action": action}
        )

    async def _clear_pending(self, telegram_id: int, context: ContextType):
        ctx = await self._session_context(telegram_id, context)
        ctx.pop("pending_action", None)
        ctx.pop("ai_manager_pending_action", None)
        state = SessionState.from_dict(ctx)
        state.pending_action = None
        ctx.update(state.to_dict())
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
        history = await self._supabase_history(telegram_id, role)
        session_context = await self._session_context(telegram_id, role)
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
        trusted_for_prompt["conversation_state"] = {
            k: v for k, v in session_context.items()
            if k in {
                "current_entity_type", "current_entity_id",
                "current_company_id", "current_service_id",
                "current_order_id", "last_displayed_entity_id",
                "current_pagination_index", "current_list", "current_position",
            }
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

        pending = await self._pending(telegram_id, role)
        if pending:
            if self._is_cancel(message):
                await self._clear_pending(telegram_id, role)
                reply = self._cancel_text(language)
                await self._save_history(telegram_id, role, "ai", reply, {"cancelled": True})
                return {"reply": reply, "cancelled": True}
            if self._is_confirmation(message):
                try:
                    result = await tools.execute_confirmed(
                        str(pending.get("name") or ""), dict(pending.get("args") or {})
                    )
                    await self._clear_pending(telegram_id, role)
                    reply = self._done_text(language)
                    await self._save_history(
                        telegram_id, role, "ai", reply,
                        {"confirmed_action": pending, "tool_result": result},
                    )
                    return {
                        "reply": reply,
                        "tool_result": result,
                        "confirmed": True,
                    }
                except Exception as exc:
                    await self._clear_pending(telegram_id, role)
                    logger.exception("AIManager confirmed action failed")
                    reply = self._error_text(language)
                    await self._save_history(
                        telegram_id, role, "ai", reply,
                        {"confirmed_action": pending, "error": str(exc)[:1000]},
                    )
                    return {"reply": reply, "confirmed": False, "error": str(exc)}

            # A pending mutation must not be silently overwritten by another
            # request. Ask the user to confirm or cancel first.
            if language == "hy":
                reply = "Նախ հաստատեք կամ չեղարկեք սպասվող փոփոխությունը։"
            elif language == "ru":
                reply = "Сначала подтвердите или отмените ожидающее изменение."
            else:
                reply = "Please confirm or cancel the pending change first."
            return {"reply": reply, "confirmation_pending": True}

        tool_log: list[dict[str, Any]] = []

        for round_no in range(self.max_tool_rounds):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=definitions or None,
                    tool_choice="auto" if definitions else None,
                    temperature=0.1,
                    max_tokens=1600,
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
                return {"reply": reply, "tool_calls": tool_log}

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
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)[:1000]}

                tool_log.append({
                    "name": name,
                    "arguments": args,
                    "result": result,
                })
                await self._save_history(
                    telegram_id,
                    role,
                    "tool",
                    name,
                    {"arguments": args, "result": result},
                )

                if result.get("requires_confirmation"):
                    action = result.get("action") or {}
                    pending_action = {
                        "name": str(action.get("name") or name),
                        "args": dict(action.get("args") or {}),
                        "summary": str(result.get("summary") or ""),
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
                    "content": json.dumps(
                        result, ensure_ascii=False, default=str
                    )[:12000],
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
                        safe_items = [
                            {"id": x.get("id")}
                            for x in items
                            if isinstance(x, dict) and x.get("id") is not None
                        ][:50]
                        if safe_items:
                            patch["current_list"] = safe_items
                            patch["current_pagination_index"] = 0
                            first = safe_items[0]
                            if len(safe_items) == 1:
                                patch["last_displayed_entity_id"] = {
                                    "type": "order" if "order" in name else "entity",
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
