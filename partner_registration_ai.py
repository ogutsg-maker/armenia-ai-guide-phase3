"""AI-assisted partner onboarding for Armenia AI Guide.

Collects a partner's business information in natural language, extracts a
structured profile with Groq, asks only for missing information, and saves a
pending partner profile using the existing DatabaseManager API.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

try:
    from groq import AsyncGroq
except Exception:  # pragma: no cover
    AsyncGroq = None


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _catalog(db) -> list[dict]:
    result = []
    try:
        for master in db.get_all_master_categories() or []:
            mid = master.get("id")
            master_name = master.get("name_am") or master.get("name_hy") or master.get("name_ru") or ""
            subs = db.get_subcategories_by_master(mid) or []
            for sub in subs:
                result.append({
                    "master_id": mid,
                    "master": master_name,
                    "id": sub.get("id"),
                    "name": sub.get("name_am") or sub.get("name_hy") or sub.get("name_ru") or "",
                    "name_ru": sub.get("name_ru") or "",
                })
    except Exception:
        return []
    return result


def _heuristic(text: str) -> dict:
    low = text.lower()
    direction = None
    aliases = {
        "Красота и уход": ["салон", "парикмах", "маникюр", "педикюр", "барбер", "космет", "beauty", "hair"],
        "Рестораны и питание": ["ресторан", "кафе", "пицц", "бар", "еда", "кухн", "food"],
        "Проживание": ["отель", "гостиниц", "хостел", "гостевой дом", "апартамент", "hotel"],
        "Транспорт": ["такси", "трансфер", "перевоз", "авто", "transport"],
        "Экскурсии": ["экскурс", "гид", "sightseeing"],
        "Туры": ["тур", "поездк", "travel", "tour"],
        "SPA и wellness": ["spa", "массаж", "сауна", "хамам", "wellness"],
        "Развлечения и активности": ["развлеч", "квест", "спорт", "активност", "activity"],
    }
    for name, words in aliases.items():
        if any(w in low for w in words):
            direction = name
            break
    prices = []
    for m in re.finditer(r"([\d][\d\s.,]{2,})\s*(?:֏|դրամ|dram|amd)", text, re.I):
        try:
            prices.append(int(re.sub(r"[\s.,]", "", m.group(1))))
        except ValueError:
            pass
    return {"business_name": None, "city": None, "district": None, "direction": direction,
            "subcategory_names": [], "description": _norm(text), "services": [], "prices": prices,
            "missing": ["business_name", "city", "services"]}


async def extract(text: str, history: list[dict], db, previous_profile: dict | None = None, pending_field: str | None = None) -> dict:
    catalog = _catalog(db)
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key or AsyncGroq is None:
        return _heuristic(text)

    client = AsyncGroq(api_key=api_key)
    catalog_text = json.dumps(catalog[:250], ensure_ascii=False)
    previous_profile = previous_profile or {}
    pending_instruction = (
        f"The previous assistant asked specifically for {pending_field}. Treat the new message as the answer to that field unless the message clearly contains broader business information."
        if pending_field else ""
    )
    messages = [
        {"role": "system", "content": """You are the partner-onboarding AI for Armenia AI Guide.\nExtract a business profile from natural Armenian, Russian or English. Do not invent facts. Merge the new message with the conversation history. Determine the best existing master direction and subcategories from the supplied catalog. Extract every service and price explicitly mentioned. A price can be a number in AMD; preserve 'from' semantics in description if present.\nReturn ONLY valid JSON with this schema:\n{\"business_name\":null,\"city\":null,\"district\":null,\"direction\":null,\"subcategory_names\":[],\"description\":\"\",\"services\":[{\"name\":\"\",\"price\":null,\"price_type\":\"fixed|from|range|unknown\"}],\"missing\":[],\"ready\":false}\nready=true only when business_name, city, direction and at least one service are known. missing should contain only the still-required fields. Ask for the smallest missing piece next. Preserve previously extracted fields even when the new message contains only a short answer. """},
        {"role": "user", "content": "CATALOG:\n" + catalog_text + "\n\nPREVIOUS PROFILE:\n" + json.dumps(previous_profile, ensure_ascii=False) + "\n\nPENDING FIELD:\n" + pending_instruction + "\n\nCONVERSATION:\n" + json.dumps(history[-8:], ensure_ascii=False) + "\n\nNEW MESSAGE:\n" + text},
    ]
    try:
        r = await client.chat.completions.create(
            model=os.getenv("PARTNER_ONBOARDING_MODEL", "llama-3.1-8b-instant"),
            messages=messages,
            temperature=0.1,
            response_format={"type": "json_object"},
            max_tokens=1800,
        )
        data = json.loads(r.choices[0].message.content or "{}")
        if not isinstance(data, dict):
            raise ValueError("AI returned non-object")
        # Deterministically honor the field we explicitly asked for. This prevents
        # short answers such as "Beauty Studio" from triggering the same question again.
        if pending_field in {"business_name", "city", "district", "direction"} and text:
            if not data.get(pending_field):
                data[pending_field] = _norm(text)
        if pending_field == "services" and text and not data.get("services"):
            data["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown"}]
        return data
    except Exception:
        fallback = _heuristic(text)
        if pending_field in {"business_name", "city", "district", "direction"}:
            fallback[pending_field] = _norm(text)
        elif pending_field == "services":
            fallback["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown"}]
        return fallback


def match_subcategories(db, names: list[str]) -> list[int]:
    if not names:
        return []
    catalog = _catalog(db)
    ids = []
    for wanted in names:
        w = _norm(wanted).lower()
        if not w:
            continue
        best = None
        for item in catalog:
            hay = f"{item['name']} {item['name_ru']}".lower()
            if w == hay or w in hay or hay in w:
                best = item["id"]
                break
        if best is not None and best not in ids:
            ids.append(best)
    return ids


def missing_question(data: dict, lang: str) -> str:
    missing = data.get("missing") or []
    field = missing[0] if missing else ""
    questions = {
        "hy": {
            "business_name": "Ինչպե՞ս է կոչվում ձեր բիզնեսը։",
            "city": "Ո՞ր քաղաքում է գտնվում բիզնեսը։",
            "direction": "Ի՞նչ հիմնական ուղղությամբ եք աշխատում։",
            "services": "Ի՞նչ ծառայություններ եք առաջարկում և ինչ գներով։",
        },
        "ru": {
            "business_name": "Как называется ваш бизнес?",
            "city": "В каком городе находится ваш бизнес?",
            "direction": "Какое основное направление вашего бизнеса?",
            "services": "Какие услуги вы предлагаете и сколько они стоят?",
        },
        "en": {
            "business_name": "What is the name of your business?",
            "city": "Which city is the business located in?",
            "direction": "What is the main direction of your business?",
            "services": "What services do you offer and what are their prices?",
        },
    }
    return questions.get(lang, questions["ru"]).get(field, questions.get(lang, questions["ru"])["services"])
