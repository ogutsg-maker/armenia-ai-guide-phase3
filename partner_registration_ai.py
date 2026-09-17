"""AI-first partner onboarding for Armenia AI Guide.

The partner never chooses a technical catalogue structure during registration.
The AI reads the partner's natural description, maps it to the existing
catalogue when possible, and prepares a structured application for admin review.
Unknown directions are proposed for admin activation instead of being rejected.
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


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _catalog(db) -> list[dict]:
    rows: list[dict] = []
    try:
        for master in db.get_all_master_categories() or []:
            mid = master.get("id")
            for sub in db.get_subcategories_by_master(mid) or []:
                rows.append({
                    "master_id": mid,
                    "master_am": master.get("name_am") or master.get("name_hy") or "",
                    "master_ru": master.get("name_ru") or "",
                    "master_en": master.get("name_en") or "",
                    "master_slug": master.get("slug") or "",
                    "category_id": sub.get("id"),
                    "category_am": sub.get("name_am") or sub.get("name_hy") or "",
                    "category_ru": sub.get("name_ru") or "",
                    "category_en": sub.get("name_en") or "",
                    "category_slug": sub.get("slug") or "",
                })
    except Exception:
        return []
    return rows


def _heuristic(text: str) -> dict:
    low = _norm(text).lower()
    aliases = {
        "Недвижимость": ["недвиж", "квартир", "дом", "участок", "аренд", "продаж", "real estate"],
        "Красота и уход": ["салон", "парикмах", "маникюр", "педикюр", "барбер", "космет", "beauty", "hair"],
        "Питание и кулинария": ["ресторан", "кафе", "пицц", "еда", "кухн", "food"],
        "Пассажирские перевозки и такси": ["такси", "трансфер", "перевоз", "transport"],
        "Автоуслуги": ["авто", "машин", "шиномонтаж", "автосервис", "car"],
        "Туризм и путешествия": ["экскурс", "гид", "тур", "поездк", "travel", "tour"],
        "Спорт и фитнес": ["спорт", "фитнес", "тренер", "fitness"],
        "IT и цифровые услуги": ["it", "програм", "сайт", "компьютер", "software", "digital"],
    }
    direction = next((name for name, words in aliases.items() if any(w in low for w in words)), None)
    return {
        "business_name": None,
        "city": None,
        "district": None,
        "direction": direction,
        "master_category_id": None,
        "subcategory_names": [],
        "description": _norm(text),
        "services": [],
        "missing": ["business_name", "city", "direction", "services"],
        "ready": False,
    }


async def extract(
    text: str,
    history: list[dict],
    db,
    previous_profile: dict | None = None,
    pending_field: str | None = None,
) -> dict:
    catalog = _catalog(db)
    previous_profile = previous_profile or {}
    key = os.getenv("GROQ_API_KEY", "").strip()

    if not key or AsyncGroq is None:
        data = _heuristic(text)
        if pending_field in {"business_name", "city", "district", "direction"}:
            data[pending_field] = _norm(text)
        elif pending_field == "services":
            data["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown"}]
        return data

    catalog_text = json.dumps(catalog[:500], ensure_ascii=False)
    pending = (
        f"The previous assistant asked for {pending_field}. Treat this message as the answer to that field unless the user clearly gives broader information."
        if pending_field else ""
    )
    messages = [
        {
            "role": "system",
            "content": """You are the AI registration concierge for Armenia AI Guide.
Understand Armenian, Russian and English. The partner speaks naturally; do not force forms.
Extract only facts stated by the partner and merge them with the previous profile.
Use the supplied catalogue to identify the closest existing master direction and subcategories.
If no existing direction fits, keep the proposed direction name instead of saying it does not exist.
Never invent a business name, city, service, price or category.
Return ONLY JSON:
{"business_name":null,"city":null,"district":null,"direction":null,"master_category_id":null,"subcategory_names":[],"description":"","services":[{"name":"","price":null,"price_type":"fixed|from|range|unknown"}],"missing":[],"ready":false}
ready=true only when business_name, city, direction and at least one service are known.""",
        },
        {
            "role": "user",
            "content": (
                "CATALOG:\n" + catalog_text
                + "\n\nPREVIOUS PROFILE:\n" + json.dumps(previous_profile, ensure_ascii=False)
                + "\n\nPENDING:\n" + pending
                + "\n\nHISTORY:\n" + json.dumps(history[-10:], ensure_ascii=False)
                + "\n\nNEW MESSAGE:\n" + text
            ),
        },
    ]
    try:
        client = AsyncGroq(api_key=key)
        response = await client.chat.completions.create(
            model=os.getenv("PARTNER_ONBOARDING_MODEL", os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")),
            messages=messages,
            temperature=0.1,
            response_format={"type": "json_object"},
            max_tokens=2200,
        )
        data = json.loads(response.choices[0].message.content or "{}")
        if not isinstance(data, dict):
            raise ValueError("AI response is not an object")
        if pending_field in {"business_name", "city", "district", "direction"} and not data.get(pending_field):
            data[pending_field] = _norm(text)
        if pending_field == "services" and not data.get("services"):
            data["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown"}]
        return data
    except Exception:
        fallback = _heuristic(text)
        if pending_field in {"business_name", "city", "district", "direction"}:
            fallback[pending_field] = _norm(text)
        elif pending_field == "services":
            fallback["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown"}]
        return fallback


def match_catalog(db, profile: dict) -> tuple[int | None, list[int]]:
    """Return (master_category_id, category_ids) without inventing catalogue data."""
    catalog = _catalog(db)
    wanted_master = _norm(profile.get("direction")).lower()
    requested = [_norm(x).lower() for x in (profile.get("subcategory_names") or []) if _norm(x)]
    master_id = profile.get("master_category_id")
    try:
        master_id = int(master_id) if master_id else None
    except (TypeError, ValueError):
        master_id = None

    if not master_id and wanted_master:
        for row in catalog:
            names = " ".join([row["master_am"], row["master_ru"], row["master_en"], row["master_slug"]]).lower()
            if wanted_master == names or wanted_master in names or names in wanted_master:
                master_id = row["master_id"]
                break

    category_ids: list[int] = []
    for wanted in requested:
        for row in catalog:
            if master_id and row["master_id"] != master_id:
                continue
            names = " ".join([row["category_am"], row["category_ru"], row["category_en"], row["category_slug"]]).lower()
            if wanted == names or wanted in names or names in wanted:
                cid = int(row["category_id"])
                if cid not in category_ids:
                    category_ids.append(cid)
                if not master_id:
                    master_id = row["master_id"]
                break
    return master_id, category_ids


def match_subcategories(db, names: list[str]) -> list[int]:
    """Compatibility helper used by the existing API code."""
    profile = {"subcategory_names": names}
    _, ids = match_catalog(db, profile)
    return ids


def missing_question(data: dict, lang: str) -> str:
    field = (data.get("missing") or ["services"])[0]
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
