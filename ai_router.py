"""AIRouter — Phase 2 orchestrator for the Armenia AI Guide AI core.

Decides which AI module handles a free-text message (client search /
partner onboarding / negotiation / smalltalk), keeps a shared
'orchestrator' memory layer in ai_sessions, and delegates to the
appropriate sub-module (ClientAI / PartnerAI). Negotiation is driven by
explicit negotiation ids through the marketplace endpoints, so for that
intent the router returns a hint pointing the user to their active
negotiation rather than fabricating a reply.
"""
from __future__ import annotations

import json
import logging

from platform_db import (
    active_session, create_session, add_ai_message, update_session,
    recent_ai_messages, one,
)
from client_ai import ClientAI
from partner_ai import PartnerAI

logger = logging.getLogger(__name__)

_SMALLTALK = {
    "hy": "Բարև 👋 Ես Armenia AI Guide-ի AI-օգնականն եմ։ Գրեք, ինչ ծառայություն եք փնտրում։",
    "ru": "Здравствуйте 👋 Я AI-помощник Armenia AI Guide. Напишите, какую услугу ищете.",
    "en": "Hi 👋 I am the Armenia AI Guide assistant. Tell me what service you are looking for.",
}

_NEGOTIATION_HINT = {
    "hy": "Դուք ունեք ակտիվ սակարկում։ Բացեք բանակցությունը և շարունակեք այնտեղ։",
    "ru": "У вас есть активный торг. Откройте переговоры и продолжите там.",
    "en": "You have an active negotiation. Open it to continue there.",
}


class AIRouter:
    """Top-level dispatcher with shared memory across AI modules."""

    def __init__(self, ai):
        self.ai = ai
        self.client_ai = ClientAI(ai)
        self.partner_ai = PartnerAI(ai)

    def _orchestrator_session(self, user_id: int, role: str) -> dict:
        return active_session(user_id, role, "orchestrator") or create_session(
            user_id, role, "orchestrator", {"history": [], "last_module": None}
        )

    def _active_negotiation_id(self, user_id: int) -> int | None:
        try:
            row = one(
                "SELECT id FROM negotiations WHERE client_id=%s AND status='active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            )
            return row["id"] if row else None
        except Exception:
            return None

    async def dispatch(self, user_id: int, role: str, text: str, lang: str = "hy") -> dict:
        """Route *text* to the right module and return a structured result.

        Returns ``{module, reply, confidence, negotiation_id?}``.
        """
        session = self._orchestrator_session(user_id, role)
        ctx = session.get("context_json") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        add_ai_message(session["id"], "user", text)

        neg_id = self._active_negotiation_id(user_id)
        route_ctx = {
            "active_sessions": ctx.get("history", [])[-5:],
            "active_negotiation_id": neg_id,
            "last_module": ctx.get("last_module"),
        }
        decision = await self.ai.route_message(text, role, route_ctx)
        module = decision["module"]
        rlang = decision.get("language") or lang

        result: dict = {"module": module, "confidence": decision["confidence"]}

        if module == "partner_onboarding" or role == "partner":
            out = await self.partner_ai.process(user_id, text, rlang)
            result["reply"] = out.get("reply", "")
            result["module"] = "partner_onboarding"
        elif module == "negotiation" and neg_id:
            result["reply"] = _NEGOTIATION_HINT.get(rlang, _NEGOTIATION_HINT["ru"])
            result["negotiation_id"] = neg_id
        elif module == "smalltalk":
            result["reply"] = _SMALLTALK.get(rlang, _SMALLTALK["ru"])
        elif module == "support":
            result["reply"] = _SUPPORT_HINT.get(rlang, _SUPPORT_HINT["ru"])
        else:  # client_search (default)
            result["module"] = "client_search"
            result["reply"] = await self.client_ai.process(user_id, text, rlang)

        # Update shared orchestrator memory
        hist = ctx.get("history", [])
        hist.append({"module": result["module"], "text": text[:200]})
        ctx["history"] = hist[-20:]
        ctx["last_module"] = result["module"]
        update_session(session["id"], ctx)
        add_ai_message(session["id"], "ai", result.get("reply", ""),
                       {"module": result["module"], "decision": decision})
        return result
