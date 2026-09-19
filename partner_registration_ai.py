"""AI-first partner onboarding for Armenia AI Guide."""
from __future__ import annotations

import json
import os
import re
from typing import Any

try:
    from groq import AsyncGroq
except Exception:
    AsyncGroq = None


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        raw = str(value).strip()
        if not raw.isdigit():
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None


def _catalog(db) -> list[dict]:
    rows = []
    try:
        for master in db.get_all_master_categories() or []:
            mid = _safe_int(master.get("id"))
            if mid is None:
                continue
            for sub in db.get_subcategories_by_master(mid) or []:
                cid = _safe_int(sub.get("id"))
                if cid is None:
                    continue
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
    return {"business_name": None, "city": None, "district": None, "direction": direction,
            "master_category_id": None, "subcategory_names": [], "description": _norm(text),
            "services": [], "missing": ["business_name", "city", "services"], "ready": False}


def _recover_obvious_facts(text: str, data: dict) -> dict:
    """Recover simple facts the LLM may omit while answering a pending field."""
    out = dict(data or {})
    raw = _norm(text)
    low = raw.lower()

    # Common natural-language location forms in Russian/English/Armenian.
    city_patterns = [
        r"\\b(?:в|из|город(?:е)?|город)\\s+([А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z-]{2,})",
        r"\\b(?:in|from)\\s+([A-Z][A-Za-z-]{2,})",
    ]
    if not out.get("city"):
        for pattern in city_patterns:
            m = re.search(pattern, raw, flags=re.I)
            if m:
                candidate = m.group(1).strip(" .,;:()")
                if candidate.lower() not in {"the", "city"}:
                    out["city"] = candidate
                    break
    if not out.get("city") and "раздан" in low:
        out["city"] = "Раздан"

    return out


def _parse_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    return json.loads(raw)


async def extract(text: str, history: list[dict], db, previous_profile: dict | None = None, pending_field: str | None = None) -> dict:
    catalog = _catalog(db)
    previous_profile = previous_profile or {}
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key or AsyncGroq is None:
        data = _heuristic(text)
        if pending_field in {"business_name", "city", "district"}: data[pending_field] = _norm(text)
        elif pending_field == "services": data["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown"}]
        return data

    messages = [
        {"role": "system", "content": """You are the AI registration concierge for Armenia AI Guide. Understand Armenian, Russian and English. Extract only facts stated by the partner and merge the previous profile. Use the catalogue when it fits. Determine the platform direction yourself. If no catalogue direction fits, create a concise proposed direction name and leave master_category_id null, and provide at least one concise suggested subcategory_name for that new direction. Never ask the partner to choose a direction. Never invent facts. Return ONLY one valid JSON object, no markdown, with exactly these fields: business_name, city, district, direction, master_category_id, subcategory_names, description, services, missing, ready. services is an array of objects with name, price, price_type. ready=true when business_name, city and at least one meaningful service are known. The direction is never a required question."""},
        {"role": "user", "content": "CATALOG:\n" + json.dumps(catalog[:500], ensure_ascii=False) + "\nPREVIOUS PROFILE:\n" + json.dumps(previous_profile, ensure_ascii=False) + "\nPENDING FIELD:\n" + str(pending_field or "") + "\nHISTORY:\n" + json.dumps(history[-10:], ensure_ascii=False) + "\nNEW MESSAGE:\n" + text},
    ]
    try:
        client = AsyncGroq(api_key=key)
        response = await client.chat.completions.create(
            model=os.getenv("PARTNER_ONBOARDING_MODEL", os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")),
            messages=messages,
            temperature=0.1,
            max_tokens=2200,
        )
        data = _parse_json(response.choices[0].message.content or "{}")
        if not isinstance(data, dict): raise ValueError("AI response is not an object")
        if pending_field in {"business_name", "city", "district"} and not data.get(pending_field): data[pending_field] = _norm(text)
        if pending_field == "services" and not data.get("services"): data["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown"}]
        data = _recover_obvious_facts(" ".join([str(x.get("content") or "") for x in history] + [text]), data)
        return data
    except Exception as exc:
        # Never break partner registration because of an AI-provider/model
        # incompatibility. The deterministic fallback keeps the conversation alive.
        logging_message = f"Groq partner extraction failed: {exc}"
        try:
            import logging
            logging.getLogger(__name__).warning(logging_message)
        except Exception:
            pass
        data = _heuristic(text)
        if pending_field in {"business_name", "city", "district"}: data[pending_field] = _norm(text)
        elif pending_field == "services": data["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown"}]
        data = _recover_obvious_facts(" ".join([str(x.get("content") or "") for x in history] + [text]), data)
        return data


def match_catalog(db, profile: dict) -> tuple[int | None, list[int]]:
    """Map an AI-extracted profile onto the existing 3-language catalogue.

    Матчинг намеренно "жадный", чтобы НЕ плодить ложные предложения нового
    направления, когда направление на самом деле уже есть в каталоге. Порядок:
      1) явный master_category_id, либо совпадение названия направления с
         названием мастер-категории (am/ru/en/slug);
      2) если направление не совпало — вычисляем мастер-категорию по названиям
         ПОДкатегорий, найденным в свободном тексте партнёра (названия услуг,
         описание, перечисленные подкатегории). Напр. услуга «Մазери неркум»
         однозначно указывает на «Гегецкутьюн ев кхнамк / Красота и уход».
      3) собираем id подкатегорий внутри выбранной мастер-категории.
    """
    catalog = _catalog(db)
    if not catalog:
        return _safe_int(profile.get("master_category_id")), []

    def norm(value: Any) -> str:
        return _norm(value).lower()

    wanted_master = norm(profile.get("direction"))
    requested = [norm(x) for x in (profile.get("subcategory_names") or []) if norm(x)]

    # Свободный текст партнёра: направление + описание + названия услуг + подкатегории.
    text_parts: list[str] = [str(profile.get("direction") or ""), str(profile.get("description") or "")]
    for item in (profile.get("services") or []):
        if isinstance(item, dict):
            text_parts.append(str(item.get("name") or ""))
        else:
            text_parts.append(str(item))
    text_parts.extend(str(x) for x in (profile.get("subcategory_names") or []))
    haystack = norm(" ".join(p for p in text_parts if p))

    master_id = _safe_int(profile.get("master_category_id"))

    # 1) Совпадение по названию мастер-категории (в названиях есть эмодзи-префикс,
    #    поэтому проверяем вхождение в обе стороны).
    if not master_id and wanted_master:
        for row in catalog:
            names = norm(" ".join([row["master_am"], row["master_ru"], row["master_en"], row["master_slug"]]))
            if names and (wanted_master == names or wanted_master in names or names in wanted_master):
                master_id = row["master_id"]
                break

    # 2) Не нашли — определяем мастер-категорию по названиям подкатегорий,
    #    встречающимся в тексте партнёра. Берём мастер с наибольшим числом
    #    попаданий (устойчивее к пересечениям вроде «Массаж»).
    if not master_id and haystack:
        score: dict[int, int] = {}
        for row in catalog:
            for cname in (row["category_am"], row["category_ru"], row["category_en"]):
                c = norm(cname)
                if len(c) >= 4 and c in haystack:
                    score[row["master_id"]] = score.get(row["master_id"], 0) + 1
                    break
        if score:
            master_id = max(score, key=lambda k: score[k])

    # 3) Собираем id подкатегорий выбранной мастер-категории: по явно указанным
    #    подкатегориям И по названиям подкатегорий, найденным в тексте.
    category_ids: list[int] = []
    for row in catalog:
        if master_id and row["master_id"] != master_id:
            continue
        cnames = [norm(row["category_am"]), norm(row["category_ru"]), norm(row["category_en"])]
        hit = False
        for wanted in requested:
            if any(cn and (wanted == cn or wanted in cn or cn in wanted) for cn in cnames):
                hit = True
                break
        if not hit:
            for cn in cnames:
                if len(cn) >= 4 and cn in haystack:
                    hit = True
                    break
        if not hit:
            continue
        cid = _safe_int(row.get("category_id"))
        if cid is None or cid in category_ids:
            continue
        category_ids.append(cid)
        if not master_id:
            master_id = row["master_id"]
    return master_id, category_ids


def match_subcategories(db, names: list[str]) -> list[int]:
    return match_catalog(db, {"subcategory_names": names})[1]


def missing_question(data: dict, lang: str) -> str:
    field = (data.get("missing") or ["services"])[0]
    questions = {
        "hy": {"business_name":"Ինչպե՞ս է կոչվում ձեր բիզնեսը։", "city":"Ո՞ր քաղաքում է աշխատում բիզնեսը։", "services":"Ի՞նչ ծառայություն եք առաջարկում։ Եթե գինը հայտնի է, նշեք նաև գինը։"},
        "ru": {"business_name":"Как называется ваш бизнес?", "city":"В каком городе работает бизнес?", "services":"Какую услугу вы оказываете? Если цена известна, укажите и её."},
        "en": {"business_name":"What is the name of your business?", "city":"Which city does the business operate in?", "services":"What service do you provide? If the price is known, include it."},
    }
    return questions.get(lang, questions["ru"]).get(field, questions.get(lang, questions["ru"])["services"])
