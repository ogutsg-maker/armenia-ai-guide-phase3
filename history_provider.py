"""DB-backed conversation history for AIManager."""
from __future__ import annotations
import json
from typing import Any
import data_core

class HistoryProvider:
    def __init__(self, limit: int = 20):
        self.limit = max(2, min(int(limit), 50))

    @staticmethod
    def _storage_role(role: str) -> str:
        # Registration is a partner-onboarding context, but ai_sessions.role
        # only accepts the persisted application roles. Keep the AI prompt
        # context as REGISTRATION while storing history under PARTNER.
        normalized = str(role or "").strip().lower()
        return "partner" if normalized == "registration" else normalized

    @staticmethod
    def _resolve_session_user_id(telegram_id: int) -> int:
        user = data_core.get_user_by_telegram_id(int(telegram_id))
        if not user:
            user = data_core.ensure_user_by_telegram_id(int(telegram_id))
        user_id = int((user or {}).get("telegram_id") or 0)
        if user_id <= 0:
            raise RuntimeError(
                f"Unable to resolve users.telegram_id for telegram_id={telegram_id}"
            )
        return user_id

    def _session(self, telegram_id: int, role: str):
        storage_role = self._storage_role(role)
        user_id = self._resolve_session_user_id(telegram_id)
        return (
            data_core.active_session(int(user_id), storage_role, "ai_manager")
            or data_core.create_session(
                int(user_id), storage_role, "ai_manager",
                {"history_version": 1},
            )
        )

    def get(self, telegram_id: int, role: str) -> list[dict[str, Any]]:
        session = self._session(telegram_id, role)
        rows = data_core.recent_ai_messages(session["id"], self.limit)
        out = []
        for row in rows or []:
            data = row.get("data_json") if isinstance(row, dict) else None
            if isinstance(data, str):
                try: data = json.loads(data)
                except Exception: data = {}
            sender_role = str(row.get("sender_role") or "").strip().lower()
            if sender_role in {"ai", "assistant"}:
                history_role = "assistant"
            elif sender_role == "tool":
                history_role = "tool"
            elif sender_role == "system":
                history_role = "system"
            else:
                history_role = "user"
            item = {
                "role": history_role,
                "content": str(row.get("message_text") or row.get("text") or ""),
                "data": data or {},
            }
            if row.get("tool_call_id"):
                item["tool_call_id"] = row["tool_call_id"]
            out.append(item)
        return out

    def append(
        self,
        telegram_id: int,
        role: str,
        sender: str,
        content: str,
        data: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
    ):
        session = self._session(telegram_id, role)
        return data_core.add_ai_message(
            session["id"], sender, str(content or ""), data or {}, tool_call_id
        )

    def set_pending(self, telegram_id: int, role: str, pending: dict[str, Any] | None):
        session = self._session(telegram_id, role)
        ctx = session.get("context_json") or {}
        if isinstance(ctx, str):
            try: ctx = json.loads(ctx)
            except Exception: ctx = {}
        if pending:
            ctx["ai_manager_pending_action"] = pending
        else:
            ctx.pop("ai_manager_pending_action", None)
        return data_core.update_session(session["id"], ctx)

    def get_pending(self, telegram_id: int, role: str):
        session = self._session(telegram_id, role)
        ctx = session.get("context_json") or {}
        if isinstance(ctx, str):
            try: ctx = json.loads(ctx)
            except Exception: ctx = {}
        return ctx.get("ai_manager_pending_action")
