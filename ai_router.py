"""Top-level AI dispatcher for Armenia AI Guide."""
from __future__ import annotations

import json
import logging

from platform_db import active_session, create_session, add_ai_message, update_session, one
from client_ai import ClientAI
from partner_ai import PartnerAI

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
    def __init__(self, ai):
        self.ai = ai
        self.client_ai = ClientAI(ai)
        self.partner_ai = PartnerAI(ai)

    def _orchestrator_session(self, user_id: int, role: str) -> dict:
        return active_session(user_id, role, "orchestrator") or create_session(user_id, role, "orchestrator", {"history": [], "last_module": None})

    def _active_negotiation_id(self, user_id: int) -> int | None:
        try:
            row = one("""SELECT id FROM negotiations
                       WHERE (client_id=%s OR partner_id=(SELECT id FROM partners WHERE user_id=%s))
                         AND status IN ('active','pending','negotiating')
                       ORDER BY updated_at DESC LIMIT 1""", (user_id, user_id))
            return row["id"] if row else None
        except Exception:
            return None

    async def dispatch(self, user_id: int, role: str, text: str, lang: str = "hy") -> dict:
        session = self._orchestrator_session(user_id, role)
        ctx = session.get("context_json") or {}
        if isinstance(ctx, str):
            try: ctx = json.loads(ctx)
            except Exception: ctx = {}
        add_ai_message(session["id"], "user", text)
        neg_id = self._active_negotiation_id(user_id)
        decision = await self.ai.route_message(text, role, {
            "active_sessions": (ctx.get("history") or [])[-5:],
            "active_negotiation_id": neg_id,
            "last_module": ctx.get("last_module"),
        })
        module = decision.get("module", "client_search")
        rlang = decision.get("language") or lang
        result = {"module": module, "confidence": decision.get("confidence", 0.0)}

        if module == "negotiation" and neg_id:
            result["reply"] = _NEGOTIATION_HINT.get(rlang, _NEGOTIATION_HINT["ru"])
            result["negotiation_id"] = neg_id
        elif module == "partner_onboarding" and role == "partner":
            out = await self.partner_ai.process(user_id, text, rlang)
            result["reply"] = out.get("reply", "")
        elif module == "support":
            result["reply"] = _SUPPORT_HINT.get(rlang, _SUPPORT_HINT["ru"])
        elif module == "smalltalk":
            result["reply"] = _SMALLTALK.get(rlang, _SMALLTALK["ru"])
        elif role == "partner":
            # A partner message that is not a negotiation/support message still
            # belongs to the business onboarding/catalog assistant.
            out = await self.partner_ai.process(user_id, text, rlang)
            result["module"] = "partner_onboarding"
            result["reply"] = out.get("reply", "")
        else:
            result["module"] = "client_search"
            result["reply"] = await self.client_ai.process(user_id, text, rlang)

        history = ctx.get("history") or []
        history.append({"module": result["module"], "text": text[:200]})
        ctx["history"] = history[-20:]
        ctx["last_module"] = result["module"]
        update_session(session["id"], ctx)
        add_ai_message(session["id"], "ai", result.get("reply", ""), {"module": result["module"], "decision": decision})
        return result
