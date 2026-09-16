"""GroqAI — unified AI service layer for the clean Armenia AI Guide platform.

This module is the single foundation every contour (client search / negotiation,
partner onboarding understanding, admin AI-research & new-direction proposals)
builds on. It talks to Groq (Llama / gpt-oss) through one low-level entry point
``_call_groq`` and exposes higher-level helpers on top of it.

Design goals
------------
* **Real Groq when a key is present.** If ``GROQ_API_KEY`` is configured the
  calls hit the real Groq Chat Completions API (JSON mode supported).
* **Keyless mock fallback.** With no key — or on any transport error — the
  service degrades to a deterministic mock so the app boots and the smoke
  harness runs without network or secrets. Mock responses are clearly flagged
  with ``"_mock": true`` in JSON payloads.
* **Backward-compatible surface.** ``_call_groq(system, user, json_mode)`` keeps
  the exact signature the existing research layer (PotentialPartnerAI) expects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

try:  # config is always present in this project
    from config import GROQ_API_KEY, GROQ_MODEL
except Exception:  # pragma: no cover - defensive
    import os
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

logger = logging.getLogger(__name__)

DEFAULT_MODEL = GROQ_MODEL or "openai/gpt-oss-20b"

try:
    from pydantic import BaseModel, Field

    class ClientRequestAnalysis(BaseModel):
        """Structured understanding of a client's free-text service request.

        This is the contract the client contour (client_ai.ClientAI) relies on:
        it reads ``category_id``, ``language``, ``city``, ``summary`` and
        ``checklist`` and persists ``model_dump()`` back into the session.
        """
        category_id: int | None = None
        category_name: str = ""
        language: str = "hy"
        city: str = ""
        summary: str = ""
        keywords: list[str] = Field(default_factory=list)
        checklist: list[str] = Field(default_factory=list)
        _mock: bool = False
except Exception:  # pragma: no cover - pydantic always present via aiogram
    from dataclasses import dataclass, field, asdict

    @dataclass
    class ClientRequestAnalysis:  # type: ignore[no-redef]
        category_id: int | None = None
        category_name: str = ""
        language: str = "hy"
        city: str = ""
        summary: str = ""
        keywords: list = field(default_factory=list)
        checklist: list = field(default_factory=list)
        _mock: bool = False

        def model_dump(self) -> dict:
            return asdict(self)


class GroqAI:
    """Thin, reusable async wrapper around Groq with a keyless mock fallback."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.api_key = (api_key if api_key is not None else GROQ_API_KEY) or ""
        self.model = model
        self._client = None
        # Only build a real client when a non-test key is present. The smoke
        # harness injects a fake key ('gsk_test') — treat it as mock too.
        if self.api_key and not self.api_key.startswith("gsk_test"):
            try:
                from groq import Groq
                self._client = Groq(api_key=self.api_key)
            except Exception:
                logger.warning("Groq client unavailable — using mock fallback")
                self._client = None

    # ------------------------------------------------------------------
    # Low-level primitives
    # ------------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        """True when a real Groq client is active (not the mock fallback)."""
        return self._client is not None

    def _groq_chat(self, system: str, user: str, json_mode: bool = True,
                   temperature: float = 0.2, max_tokens: int = 1024) -> str:
        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    async def _call_groq(self, system: str, user: str, json_mode: bool = True,
                         temperature: float = 0.2, max_tokens: int = 1024) -> str:
        """Async low-level call. Returns the raw model string.

        Falls back to :meth:`_mock` when no live client is configured or the
        network call fails, so callers never crash on a missing key.
        """
        if self._client is None:
            return self._mock(system, user, json_mode)
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._groq_chat(system, user, json_mode,
                                        temperature, max_tokens),
            )
        except Exception:
            logger.exception("Groq call failed — falling back to mock")
            return self._mock(system, user, json_mode)

    async def complete(self, system: str, user: str, *, temperature: float = 0.2,
                       max_tokens: int = 1024) -> str:
        """Free-text completion helper."""
        return await self._call_groq(system, user, json_mode=False,
                                     temperature=temperature, max_tokens=max_tokens)

    async def complete_json(self, system: str, user: str, *,
                            temperature: float = 0.2, max_tokens: int = 1024) -> dict:
        """JSON completion helper. Always returns a dict (never raises)."""
        raw = await self._call_groq(system, user, json_mode=True,
                                    temperature=temperature, max_tokens=max_tokens)
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"result": data}
        except Exception:
            logger.warning("Non-JSON model output; wrapping as text")
            return {"_mock": not self.is_live, "text": raw}

    # ------------------------------------------------------------------
    # Mock fallback
    # ------------------------------------------------------------------
    def _mock(self, system: str, user: str, json_mode: bool) -> str:
        """Deterministic offline response.

        For JSON mode we emit an empty-but-valid envelope so downstream json.loads
        succeeds; for text mode a short neutral message. No fake business data is
        ever invented (research layer treats empty business_name as 'skip').
        """
        if json_mode:
            return json.dumps({"_mock": True})
        return "[AI mock] Сервис AI работает в офлайн-режиме (ключ Groq не задан)."

    # ------------------------------------------------------------------
    # High-level contours (Phase 2 fills the prompts; interface is stable now)
    # ------------------------------------------------------------------
    async def analyze_client_request(self, text: str, categories: list | None = None) -> dict:
        """Client contour: understand a free-text service request (dict form)."""
        analysis = await self.analyze_request(text, categories)
        return analysis.model_dump()

    async def analyze_request(self, text: str, categories: list | None = None) -> "ClientRequestAnalysis":
        """Client contour: understand a free-text service request.

        Returns a :class:`ClientRequestAnalysis` (has ``.category_id``,
        ``.language``, ``.city``, ``.summary``, ``.checklist`` and
        ``.model_dump()``). This is the exact surface ``client_ai.ClientAI``
        depends on. Never raises: on a missing key / parse error it degrades to
        a neutral analysis so the client flow keeps working.
        """
        cats = categories or []
        catalog_lines = []
        for c in cats:
            cid = c.get("id")
            names = " / ".join(
                str(c.get(k) or "").strip()
                for k in ("name_hy", "name_ru", "name_en")
                if c.get(k)
            )
            catalog_lines.append(f"{cid}: {names}")
        catalog_text = "\n".join(catalog_lines[:300]) or "(catalog empty)"
        system = (
            "You are the client dispatcher of Armenia AI Guide. From the user's "
            "free text understand which service they need and map it to ONE "
            "category id from the CATALOG below (use null if nothing fits). "
            "Detect the message language (hy/ru/en), the city if mentioned, a "
            "short neutral summary, useful search keywords, and a short "
            "checklist (max 4) of clarifying questions in the user's language. "
            "Never invent partner names or contact details. Return JSON: "
            '{"category_id":null,"category_name":"","language":"hy","city":"",'
            '"summary":"","keywords":[],"checklist":[]}\n\nCATALOG:\n'
            + catalog_text
        )
        data = await self.complete_json(system, text)
        return self._to_analysis(data, text, cats)

    def _to_analysis(self, data: dict, text: str, cats: list) -> "ClientRequestAnalysis":
        data = data if isinstance(data, dict) else {}
        is_mock = bool(data.get("_mock")) or not self.is_live
        # Resolve / validate category id against the real catalog.
        cat_id = data.get("category_id")
        valid_ids = {c.get("id") for c in cats}
        try:
            cat_id = int(cat_id) if cat_id is not None else None
        except (TypeError, ValueError):
            cat_id = None
        if cat_id is not None and valid_ids and cat_id not in valid_ids:
            cat_id = None
        # Fallback: match by category name if the model gave a name but no id.
        if cat_id is None and data.get("category_name") and cats:
            cat_id = self._match_category_by_name(data["category_name"], cats)
        lang = str(data.get("language") or "").strip().lower()
        if lang not in {"hy", "ru", "en"}:
            lang = self._guess_language(text)
        checklist = data.get("checklist") or []
        if not isinstance(checklist, list):
            checklist = []
        keywords = data.get("keywords") or []
        if not isinstance(keywords, list):
            keywords = []
        return ClientRequestAnalysis(
            category_id=cat_id,
            category_name=str(data.get("category_name") or ""),
            language=lang,
            city=str(data.get("city") or "").strip(),
            summary=str(data.get("summary") or text[:280]),
            keywords=[str(k) for k in keywords][:12],
            checklist=[str(q) for q in checklist][:4],
            _mock=is_mock,
        )

    @staticmethod
    def _match_category_by_name(name: str, cats: list) -> int | None:
        w = re.sub(r"\s+", " ", str(name or "")).strip().lower()
        if not w:
            return None
        for c in cats:
            hay = " ".join(
                str(c.get(k) or "").lower()
                for k in ("name_hy", "name_ru", "name_en")
            )
            if w and (w == hay.strip() or w in hay):
                return c.get("id")
        return None

    @staticmethod
    def _guess_language(text: str) -> str:
        if re.search(r"[\u0530-\u058F]", text):  # Armenian block
            return "hy"
        if re.search(r"[\u0400-\u04FF]", text):  # Cyrillic block
            return "ru"
        return "en"

    async def moderate_message(self, text: str) -> dict:
        """Return {'is_safe':bool,'reason':str,'cleaned_text':str}."""
        system = (
            "You moderate marketplace chat. Block direct contacts (phone, "
            "messenger handas, external links) shared before payment. Return JSON: "
            '{"is_safe":true,"reason":"","cleaned_text":""}'
        )
        data = await self.complete_json(system, text)
        if "is_safe" not in data:  # mock / parse fallback: allow, do not block
            return {"is_safe": True, "reason": "", "cleaned_text": text}
        data.setdefault("cleaned_text", text)
        data.setdefault("reason", "")
        return data

    async def understand_partner_business(self, text: str, history: list | None = None) -> dict:
        """Partner contour: understand a partner's free-text business."""
        system = (
            "You are Armenia AI Guide's partner onboarding AI. Understand the "
            "partner's business from free text and map it to a direction, "
            "subcategories and services. Return JSON: "
            '{"business_name":"","direction":"","subcategories":[],"services":[],'
            '"city":"","description":"","new_direction_suggestion":""}'
        )
        return await self.complete_json(system, text)

    async def propose_new_directions(self, context: str) -> dict:
        """Admin contour: propose NEW directions (beyond the initial set) for approval.

        Returns JSON with a list of proposed directions; each carries a reason so
        the admin can approve/reject. No partner PII is invented here.
        """
        system = (
            "You are Armenia AI Guide's AI Research Manager. Based on the given "
            "market context, propose NEW service directions that are not yet in "
            "the catalogue and would fit the Armenian market. Return JSON: "
            '{"directions":[{"direction":"","subcategories":[],"reason":"","confidence":0}]}'
        )
        data = await self.complete_json(system, context)
        data.setdefault("directions", [])
        return data

    async def negotiate_reply(self, context: str) -> str:
        """Client<->partner negotiation assistant (free text).

        Kept for backward-compat; Phase 2 callers should prefer
        :meth:`negotiate` which returns structured intent + price.
        """
        system = (
            "You mediate a price/terms negotiation on Armenia AI Guide. Be concise, "
            "neutral, and never reveal contact details before payment."
        )
        return await self.complete(system, context, max_tokens=512)

    # ------------------------------------------------------------------
    # Phase 2 — structured routing & negotiation
    # ------------------------------------------------------------------
    async def route_message(self, text: str, role: str,
                            context: dict | None = None) -> dict:
        """Decide which AI module should handle the user's message.

        Returns ``{module, confidence, reason, language}``.
        Module is one of: ``client_search``, ``negotiation``,
        ``partner_onboarding``, ``smalltalk``.
        """
        ctx = context or {}
        system = (
            "You are the router of Armenia AI Guide. Classify the user message "
            "into ONE module:\n"
            "- client_search: the user is looking for a service\n"
            "- negotiation: the user is haggling price/terms inside a "
            "negotiation\n"
            "- partner_onboarding: a partner describes their business\n"
            "- support: the user has a problem/complaint/question about an "
            "order, payment, refund or the platform itself\n"
            "- smalltalk: greeting / off-topic\n"
            "Return JSON: {\"module\":\"...\",\"confidence\":0.9,"
            "\"reason\":\"...\",\"language\":\"hy\"}"
        )
        payload = (
            f"Role: {role}\n"
            f"Active sessions: {json.dumps(ctx.get('active_sessions', []), ensure_ascii=False)}\n"
            f"Message: {text}"
        )
        data = await self.complete_json(system, payload)
        valid = {"client_search", "negotiation", "partner_onboarding", "smalltalk", "support"}
        mod = str(data.get("module", "")).strip().lower()
        if mod not in valid:
            mod = self._fallback_route(text, role, ctx)
        conf = float(data.get("confidence") or 0)
        if not 0 <= conf <= 1:
            conf = 0.5
        return {
            "module": mod,
            "confidence": conf,
            "reason": str(data.get("reason", "")),
            "language": str(data.get("language", self._guess_language(text))),
        }

    @staticmethod
    def _fallback_route(text: str, role: str, ctx: dict) -> str:
        """Deterministic heuristic used as mock / parse fallback."""
        low = text.lower()
        has_neg = ctx.get("active_negotiation_id")
        if has_neg and any(
            w in low for w in (
                "price", "գին", "цен", "agree", "համաձայ", "соглас",
                "discount", "զեղչ", "скидк", "counter", "предлаг",
            )
        ):
            return "negotiation"
        if role == "partner":
            return "partner_onboarding"
        if any(
            w in low for w in (
                "помогите", "проблем", "жалоб", "возврат", "refund",
                "support", "поддерж", "не работает", "ошибк", "complaint",
            )
        ):
            return "support"
        if any(
            w in low for w in (
                "ищу", "find", "փնտր", "need", "хочу", "ուզ", "book", "заброн",
            )
        ):
            return "client_search"
        return "smalltalk"

    async def negotiate(self, payload: dict) -> dict:
        """Structured negotiation step.

        *payload* keys: ``service_name``, ``current_price``, ``currency``,
        ``sender_role``, ``message``, ``history`` (list of recent lines).

        Returns ``{intent, proposed_price, agreed, reply, summary}``.
        ``intent`` is one of: ``agree``, ``counter``, ``reject``,
        ``question``, ``chitchat``.
        """
        system = (
            "You mediate a price negotiation on Armenia AI Guide. "
            "Given the service, current price, role of the sender, and "
            "recent chat, decide the user's intent and any proposed "
            "price. Be concise and neutral. Never reveal contact "
            "details. Return JSON: {\"intent\":\"agree|counter|reject"
            "|question|chitchat\",\"proposed_price\":null,"
            "\"agreed\":false,\"reply\":\"...\",\"summary\":\"...\"}"
        )
        user_msg = (
            f"Service: {payload.get('service_name','')}\n"
            f"Current price: {payload.get('current_price','')} {payload.get('currency','')}\n"
            f"Sender role: {payload.get('sender_role','')}\n"
            f"Message: {payload.get('message','')}\n"
            f"Recent chat:\n" + "\n".join(payload.get('history', [])[:12])
        )
        data = await self.complete_json(system, user_msg)
        valid_intents = {"agree", "counter", "reject", "question", "chitchat"}
        intent = str(data.get("intent", "")).strip().lower()
        if intent not in valid_intents:
            intent = self._fallback_negotiate_intent(payload.get("message", ""))
        proposed_price = data.get("proposed_price")
        try:
            if proposed_price not in (None, "", 0):
                proposed_price = float(proposed_price)
            else:
                proposed_price = None
        except (TypeError, ValueError):
            proposed_price = None
        if proposed_price is not None and proposed_price < 0:
            proposed_price = None
        # Mock / fallback: extract a price from the message when the model gave none.
        if proposed_price is None and not self.is_live:
            proposed_price = self._extract_price(payload.get("message", ""))
        agreed = bool(data.get("agreed")) or intent == "agree"
        reply = str(data.get("reply", ""))
        if not reply and not self.is_live:
            reply = self._mock_negotiate_reply(intent, payload)
        summary = str(data.get("summary", ""))[:500]
        return {
            "intent": intent,
            "proposed_price": proposed_price,
            "agreed": agreed,
            "reply": reply,
            "summary": summary,
        }

    @staticmethod
    def _extract_price(text: str):
        """Pull a numeric price out of free text (mock/fallback helper)."""
        m = re.search(r"(\d[\d\s.,]*\d|\d)", str(text or ""))
        if not m:
            return None
        raw = m.group(1).replace(" ", "").replace(",", "")
        # Keep only the last dot as decimal separator if present.
        if raw.count(".") > 1:
            raw = raw.replace(".", "")
        try:
            val = float(raw)
            return val if val > 0 else None
        except ValueError:
            return None

    @staticmethod
    def _fallback_negotiate_intent(text: str) -> str:
        """Regex/heuristic intent fallback when Groq is unavailable."""
        low = text.lower()
        agree_kw = (
            "согласен", "согласна", "беру", "agree", "ok", "ок",
            "համաձայն եմ", "այո", "deal", "принимаю", "подходит",
        )
        if any(k in low for k in agree_kw):
            return "agree"
        reject_kw = ("нет", "no", "չի", "անել", "отказываюсь", "отказыва")
        if any(k in low for k in reject_kw):
            return "reject"
        # A concrete number => treat as a counter-offer (before question mark).
        if re.search(r"\d", text):
            return "counter"
        question_kw = ("?", "՞", "как", "what", "ինչ", "сколько", "how much", "քանի՞")
        if any(k in low for k in question_kw):
            return "question"
        return "chitchat"

    @staticmethod
    def _mock_negotiate_reply(intent: str, payload: dict) -> str:
        """Deterministic mock reply for the mock path."""
        svc = payload.get("service_name", "услуга")
        cur = payload.get("current_price", "")
        ccy = payload.get("currency", "AMD")
        msgs = {
            "agree": f"Вы согласны на текущую цену ({cur} {ccy}).",
            "counter": f"Вы предлагаете новую цену на «{svc}».",
            "reject": f"Вы отказались от условий по «{svc}».",
            "question": "Пожалуйста, уточните ваш вопрос — я передам партнёру.",
            "chitchat": "Я здесь, чтобы помочь с переговорами. Напишите цену или условие.",
        }
        return msgs.get(intent, msgs["chitchat"])

    # ------------------------------------------------------------------
    # Phase 3: review moderation
    # ------------------------------------------------------------------
    async def moderate_review(self, comment: str, rating: int = 0) -> dict:
        """Decide whether a review comment is safe to publish.

        Returns ``{flagged: bool, reason: str}``. ``flagged=True`` means the
        text should go to admin moderation instead of being published.
        """
        text = str(comment or "").strip()
        if not text:
            return {"flagged": False, "reason": ""}
        system = (
            "You moderate marketplace reviews. Flag the comment ONLY if it "
            "contains hate speech, threats, sexual content, personal data "
            "(phones/links to bypass the platform), or spam. Normal criticism "
            "is NOT flagged. Return JSON: {\"flagged\":false,\"reason\":\"...\"}"
        )
        user = f"Rating: {rating}\nComment: {text}"
        data = await self.complete_json(system, user)
        if not self.is_live:
            return self._fallback_moderate(text)
        return {
            "flagged": bool(data.get("flagged")),
            "reason": str(data.get("reason", ""))[:300],
        }

    @staticmethod
    def _fallback_moderate(text: str) -> dict:
        """Heuristic moderation for the mock / offline path."""
        low = text.lower()
        bad = (
            "http://", "https://", "t.me/", "@", "whatsapp", "viber",
            "идиот", "мошенник", "убью", "debil", "scam", "fuck",
        )
        # a bare phone number is also a bypass signal
        digits = sum(c.isdigit() for c in text)
        flagged = any(k in low for k in bad) or digits >= 8
        return {"flagged": flagged, "reason": "auto-flag (mock heuristic)" if flagged else ""}

    # ------------------------------------------------------------------
    # Phase 3: support assistant
    # ------------------------------------------------------------------
    async def support_reply(self, message: str, history=None, lang: str = "ru") -> dict:
        """First-line support answer.

        Returns ``{reply, escalate: bool}``. ``escalate=True`` asks a human
        admin to step in (AI unsure or user explicitly wants a person).
        """
        msg = str(message or "").strip()
        system = (
            "You are first-line support for Armenia AI Guide (a services "
            "marketplace). Answer briefly and helpfully in the user's "
            "language. If the issue needs a human (refunds disputes, account "
            "blocks, payment failures, or the user asks for a person), set "
            "escalate=true. Return JSON: {\"reply\":\"...\",\"escalate\":false}"
        )
        lines = [f"{m.get('sender_role','?')}: {m.get('message','')}"
                 for m in (history or [])][-10:]
        user = f"Language: {lang}\nHistory:\n" + "\n".join(lines) + f"\nUser: {msg}"
        data = await self.complete_json(system, user)
        if not self.is_live:
            return self._fallback_support(msg, lang)
        reply = str(data.get("reply", "")).strip()
        escalate = bool(data.get("escalate"))
        if not reply:
            return self._fallback_support(msg, lang)
        return {"reply": reply, "escalate": escalate}

    @staticmethod
    def _fallback_support(message: str, lang: str = "ru") -> dict:
        """Deterministic support reply for the mock / offline path."""
        low = message.lower()
        escalate_kw = (
            "возврат", "верните деньги", "refund", "жалоба", "оператор",
            "человек", "human", "админ", "спор", "dispute", "блок", "block",
            "не прошёл платёж", "ошибка оплаты",
        )
        escalate = any(k in low for k in escalate_kw)
        if escalate:
            reply = (
                "Понял, это лучше решит живой специалист. Передаю ваш вопрос "
                "команде поддержки — с вами скоро свяжутся. 🙌"
            )
        else:
            reply = (
                "Спасибо за обращение! Опишите, пожалуйста, проблему подробнее — "
                "номер заказа, что именно пошло не так, и я подскажу, что делать."
            )
        return {"reply": reply, "escalate": escalate}


# Convenience singleton factory ---------------------------------------------
def build_ai() -> GroqAI:
    return GroqAI()
