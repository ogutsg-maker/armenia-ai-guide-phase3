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


def get_catalog(db) -> list[dict]:
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
    catalog = get_catalog(db)
    previous_profile = previous_profile or {}
    key = os.getenv("GROQ_API_KEY", "").strip()

    if not key or AsyncGroq is None:
        data = _heuristic(text)
        if pending_field in {"business_name", "city", "district"}:
            data[pending_field] = _norm(text)
        elif pending_field == "services":
            data["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown", "matched_subcategory_id": None}]
        return data

    # IMPORTANT:
    # The model receives the COMPLETE existing subcategory catalogue, including
    # real database IDs. It must classify EACH service separately and return the
    # ID of an existing category. It is forbidden to invent category IDs/names
    # when an existing category fits.
    # Groq's free/on-demand tier has an 8K TPM request limit. Sending the
    # entire multilingual catalogue on every chat turn can exceed that limit
    # (the old payload reached ~32K tokens). First narrow the catalogue to the
    # direction suggested by deterministic hints in the user's text/profile.
    # This still sends the COMPLETE subcategory catalogue for the selected
    # direction, with real DB IDs, so service matching remains semantic and
    # cannot invent taxonomy.
    hint_text = " ".join([
        str(text or ""),
        str(previous_profile.get("description") or ""),
        str(previous_profile.get("business_name") or ""),
    ])
    hinted_direction = _heuristic(hint_text).get("direction")
    candidate_catalog = catalog
    if hinted_direction:
        wanted = _norm(hinted_direction).lower()
        matching_master_ids = {
            row["master_id"] for row in catalog
            if wanted in {
                _norm(row.get("master_ru")).lower(),
                _norm(row.get("master_am")).lower(),
                _norm(row.get("master_en")).lower(),
            }
        }
        if matching_master_ids:
            candidate_catalog = [
                row for row in catalog if row["master_id"] in matching_master_ids
            ]

    # Keep the payload compact: the AI only needs IDs and names for matching.
    catalog_for_ai = [
        {
            "id": row["category_id"],
            "hy": row["category_am"],
            "ru": row["category_ru"],
            "en": row["category_en"],
        }
        for row in candidate_catalog
    ]

    schema = {
        "type": "object",
        "properties": {
            "business_name": {"type": ["string", "null"]},
            "city": {"type": ["string", "null"]},
            "district": {"type": ["string", "null"]},
            "direction": {"type": ["string", "null"]},
            "master_category_id": {"type": ["integer", "null"]},
            "subcategory_names": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "services": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "price": {"type": ["number", "null"]},
                        "price_type": {"type": "string"},
                        "matched_subcategory_id": {"type": ["integer", "null"]},
                    },
                    "required": ["name", "price", "price_type", "matched_subcategory_id"],
                    "additionalProperties": False,
                },
            },
            "missing": {"type": "array", "items": {"type": "string"}},
            "ready": {"type": "boolean"},
        },
        "required": [
            "business_name", "city", "district", "direction",
            "master_category_id", "subcategory_names", "description",
            "services", "missing", "ready"
        ],
        "additionalProperties": False,
    }

    system_prompt = """You are the AI registration concierge for Armenia AI Guide.

Understand Armenian, Russian and English.

CATALOG RULES — THESE ARE ABSOLUTE:
1. The catalogue in the user message is the ONLY source of valid subcategory IDs.
2. You MUST semantically classify EACH partner service against the EXISTING catalogue.
3. Return the existing subcategory's EXACT numeric subcategory_id in matched_subcategory_id.
4. Never invent a subcategory ID.
5. Never create or propose a new subcategory if an existing catalogue subcategory is a reasonable semantic match.
6. The words used by the partner may differ completely from the catalogue language. Match by meaning across Armenian/Russian/English, not only by exact text.
7. Example: "մազերի կտրում", "մազ կտրել", "стрижка волос", "стрижки", "haircut" should match an existing "Парикмахер / Վարսավիր" subcategory when that subcategory is present.
8. Example: "մազերի ներկում", "окрашивание волос", "hair coloring" should match an existing "Մազերի ներկում / Окрашивание" subcategory when present.
9. If several services are stated, KEEP THEM AS SEPARATE service objects. Do not merge them into one service.
10. If the partner gives "3000-ից", "от 3000", "from 3000", store price=3000 and price_type="from".
11. Do not invent prices, business names, cities, districts or services.
12. master_category_id must be the existing master category ID that contains the selected matched subcategories.
13. Only if NO existing subcategory genuinely fits a service may matched_subcategory_id be null. Do not invent a replacement category.
14. subcategory_names is informational only; matched_subcategory_id is the authoritative database link.

DIRECTION RULES:
- Determine the platform direction yourself.
- Never ask the partner to choose a direction.
- If no existing master direction fits the business, set master_category_id=null and provide a concise proposed direction in direction plus at least one suggested subcategory_name.
- ready=true when business_name, city and at least one meaningful service are known.

Return ONLY JSON matching the supplied schema."""

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                "EXISTING CATALOGUE — USE THESE REAL DATABASE IDS:\n"
                + json.dumps(catalog_for_ai, ensure_ascii=False)
                + "\n\nPREVIOUS PROFILE:\n"
                + json.dumps(previous_profile, ensure_ascii=False)
                + "\n\nPENDING FIELD:\n"
                + str(pending_field or "")
                + "\n\nHISTORY:\n"
                + json.dumps([
                    {
                        "role": str(x.get("role") or ""),
                        "content": _norm(x.get("content") or "")[:1200],
                    }
                    for x in history[-4:]
                ], ensure_ascii=False)
                + "\n\nNEW MESSAGE:\n"
                + _norm(text)[:3000]
            ),
        },
    ]

    try:
        client = AsyncGroq(api_key=key)
        response = await client.chat.completions.create(
            model=os.getenv("PARTNER_ONBOARDING_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")),
            messages=messages,
            temperature=0.1,
            max_tokens=2600,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "partner_onboarding_catalog_match",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        ai_data = _parse_json(response.choices[0].message.content or "{}")
        if not isinstance(ai_data, dict):
            raise ValueError("AI response is not an object")

        # IMPORTANT: each chat turn must UPDATE the accumulated profile, not
        # replace it. When the first message already contained city/services
        # and the AI asks only for the business name, a model response focused
        # on that pending field may omit the earlier services. Losing them here
        # makes the next turn ask for services again.
        data = dict(previous_profile)

        for field in (
            "business_name", "city", "district", "direction",
            "master_category_id", "description"
        ):
            value = ai_data.get(field)
            if value not in (None, ""):
                data[field] = value

        # Keep the complete service list from the accumulated profile unless
        # the current AI response actually returned a non-empty list.
        ai_services = ai_data.get("services")
        if isinstance(ai_services, list) and ai_services:
            data["services"] = ai_services
        elif "services" not in data:
            data["services"] = []

        # Keep catalog suggestions from the newest response when present.
        if isinstance(ai_data.get("subcategory_names"), list) and ai_data.get("subcategory_names"):
            data["subcategory_names"] = ai_data["subcategory_names"]
        if isinstance(ai_data.get("missing"), list):
            data["missing"] = ai_data["missing"]
        if "ready" in ai_data:
            data["ready"] = bool(ai_data["ready"])

        if pending_field in {"business_name", "city", "district"} and not data.get(pending_field):
            data[pending_field] = _norm(text)
        if pending_field == "services" and not data.get("services"):
            data["services"] = [{
                "name": _norm(text),
                "price": None,
                "price_type": "unknown",
                "matched_subcategory_id": None,
            }]

        data = _recover_obvious_facts(
            " ".join([str(x.get("content") or "") for x in history] + [text]),
            data,
        )

        # Recalculate readiness from the accumulated profile. This is
        # deliberately deterministic so a pending-field turn cannot turn a
        # previously complete application back into an incomplete one.
        data["ready"] = bool(
            str(data.get("business_name") or "").strip()
            and str(data.get("city") or "").strip()
            and data.get("services")
        )
        if data["ready"]:
            data["missing"] = []
        return data
    except Exception as exc:
        logging_message = f"Groq partner extraction failed: {exc}"
        try:
            import logging
            logging.getLogger(__name__).warning(logging_message)
        except Exception:
            pass
        data = _heuristic(text)
        if pending_field in {"business_name", "city", "district"}:
            data[pending_field] = _norm(text)
        elif pending_field == "services":
            data["services"] = [{
                "name": _norm(text),
                "price": None,
                "price_type": "unknown",
                "matched_subcategory_id": None,
            }]
        data = _recover_obvious_facts(
            " ".join([str(x.get("content") or "") for x in history] + [text]),
            data,
        )
        return data

def match_catalog(db, profile: dict) -> tuple[int | None, list[int]]:
    """Validate AI-selected catalogue IDs against the real database.

    The AI does the semantic classification. Python does NOT guess a category
    from the first row or from substring matching. It only validates the IDs
    returned by Structured Outputs and derives the master direction from them.
    """
    catalog = get_catalog(db)
    by_category = {}
    for row in catalog:
        cid = _safe_int(row.get("category_id"))
        mid = _safe_int(row.get("master_id"))
        if cid is not None:
            by_category[cid] = mid

    selected: list[int] = []
    for item in profile.get("services") or []:
        if not isinstance(item, dict):
            continue
        cid = _safe_int(item.get("matched_subcategory_id"))
        if cid is not None and cid in by_category and cid not in selected:
            selected.append(cid)

    explicit_master = _safe_int(profile.get("master_category_id"))
    if explicit_master is not None:
        valid_master_ids = {row["master_id"] for row in catalog}
        if explicit_master not in valid_master_ids:
            explicit_master = None

    # If AI selected valid subcategories, their parent direction is authoritative.
    parent_ids = {by_category[cid] for cid in selected if cid in by_category}
    master_id = explicit_master
    if parent_ids:
        if master_id not in parent_ids:
            # Prefer the parent of the selected service categories. This avoids
            # trusting an inconsistent master ID returned alongside valid IDs.
            master_id = next(iter(parent_ids))
        selected = [
            cid for cid in selected
            if by_category.get(cid) == master_id
        ]

    # If the model identified a valid master but no subcategory, return the
    # master with an empty category list. The persistence layer must NOT invent
    # a category as a fallback.
    return master_id, selected


def match_subcategories(db, names: list[str]) -> list[int]:
    """Compatibility helper: exact catalogue-name lookup for legacy callers.

    Active onboarding uses matched_subcategory_id from the AI and does not use
    this function for semantic classification.
    """
    catalog = get_catalog(db)
    wanted = {_norm(name).lower() for name in names if _norm(name)}
    result = []
    for row in catalog:
        names3 = {
            _norm(row.get("category_am")).lower(),
            _norm(row.get("category_ru")).lower(),
            _norm(row.get("category_en")).lower(),
        }
        if wanted & names3:
            cid = _safe_int(row.get("category_id"))
            if cid is not None and cid not in result:
                result.append(cid)
    return result

def missing_question(data: dict, lang: str) -> str:
    field = (data.get("missing") or ["services"])[0]
    questions = {
        "hy": {"business_name":"Ինչպե՞ս է կոչվում ձեր բիզնեսը։", "city":"Ո՞ր քաղաքում է աշխատում բիզնեսը։", "services":"Ի՞նչ ծառայություն եք առաջարկում։ Եթե գինը հայտնի է, նշեք նաև գինը։"},
        "ru": {"business_name":"Как называется ваш бизнес?", "city":"В каком городе работает бизнес?", "services":"Какую услугу вы оказываете? Если цена известна, укажите и её."},
        "en": {"business_name":"What is the name of your business?", "city":"Which city does the business operate in?", "services":"What service do you provide? If the price is known, include it."},
    }
    return questions.get(lang, questions["ru"]).get(field, questions.get(lang, questions["ru"])["services"])
