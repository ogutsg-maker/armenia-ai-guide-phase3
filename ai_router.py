"""Top-level AI dispatcher for Armenia AI Guide."""
from __future__ import annotations

import json
import logging

import data_core
from client_ai import ClientAI
from partner_ai import PartnerAI
from ai_manager import AIManager
from prompt_factory import ContextType, as_context_type

logger = logging.getLogger(__name__)

_SMALLTALK = {
    "hy": "Բարև 👋 Ես Armenia AI Guide-ի AI-օգնականն եմ։ Գրեք, ինչ ծառայություն եք փնտրում։",
    "ru": "Здравствуйте 👋 Я AI-помощник Armenia AI Guide. Напишите, какую услугу ищете.",
    "en": "Hi 👋 I am the Armenia AI Guide assistant. Tell me what service you are looking for.",
}
_SUPPORT_HINT = {
    "hy": "Կօգնեմ։ Գրեք, թե ինչ գործողություն եք ուզում կատարել կամ ինչն է չի աշխատում։",
    "ru": "Помогу. Напишите, что вы хотите сделать или что именно не работает.",
    "en": "I can help. Tell me what you want to do or what is not working.",
}
_NEGOTIATION_HINT = {
    "hy": "Դուք ունեք ակտիվ բանակցություն։ Բացեք պատվերը և շարունակեք այնտեղ։",
    "ru": "У вас есть активные переговоры. Откройте заказ и продолжите там.",
    "en": "You have an active negotiation. Open the order to continue there.",
}


class AIRouter:
    def __init__(self, ai=None):
        # ai is retained for backward-compatible construction by main.py.
        # Role-specific AI work now goes through the unified AIManager.
        self.ai = ai
        self.manager = AIManager()
        self.client_ai = ClientAI(self.manager)
        self.partner_ai = PartnerAI(self.manager)

    def _orchestrator_session(self, user_id: int, role: str) -> dict:
        return data_core.active_session(user_id, role, "orchestrator") or data_core.create_session(user_id, role, "orchestrator", {"history": [], "last_module": None})

    def _active_negotiation_id(self, user_id: int) -> int | None:
        try:
            return data_core.active_negotiation_id_for_user(int(user_id))
        except Exception:
            return None

    def _select_chain(self, role: str, module: str) -> str:
        # Chain names are stable business concepts. Provider/model selection
        # remains inside AIService and can be changed without touching flows.
        if role == "admin":
            return "admin_secretary"
        if module == "partner_onboarding":
            return "partner_registration"
        if module == "negotiation":
            return "negotiation"
        if module == "client_search":
            return "client_search"
        return "general"

    async def dispatch(self, user_id: int, role: str, text: str, lang: str = "hy") -> dict:
        session = self._orchestrator_session(user_id, role)
        ctx = session.get("context_json") or {}
        if isinstance(ctx, str):
            try: ctx = json.loads(ctx)
            except Exception: ctx = {}
        data_core.add_ai_message(session["id"], "user", text)
        neg_id = self._active_negotiation_id(user_id)
        # Routing is deterministic; AIManager is the single AI execution
        # layer. This removes the previous second Groq routing call.
        if role == "admin":
            out = await self.manager.chat(
                int(user_id), ContextType.ADMIN, text,
                extra_context={"active_negotiation_id": neg_id},
                language=lang,
            )
            result = {"module":"admin_secretary","chain":"admin_secretary",
                      "confidence":1.0,"reply":out.get("reply","")}
        elif neg_id and role == "client":
            result = {"module":"negotiation","chain":"negotiation",
                      "confidence":1.0,
                      "reply":_NEGOTIATION_HINT.get(lang,_NEGOTIATION_HINT["ru"]),
                      "negotiation_id":neg_id}
        elif role == "partner":
            out = await self.partner_ai.process(user_id, text, lang)
            result = {"module":"partner_onboarding","chain":"partner_registration",
                      "confidence":1.0,"reply":out.get("reply","")}
        else:
            result = {"module":"client_search","chain":"client_search",
                      "confidence":1.0,
                      "reply":await self.client_ai.process(user_id,text,lang)}

        history = ctx.get("history") or []
        history.append({"module": result["module"], "text": text[:200]})
        ctx["history"] = history[-20:]
        ctx["last_module"] = result["module"]
        data_core.update_session(session["id"], ctx)
        data_core.add_ai_message(session["id"], "ai", result.get("reply", ""), {"module": result["module"], "unified_ai": True})
        return result
