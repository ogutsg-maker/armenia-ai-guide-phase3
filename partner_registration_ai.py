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
    # Recover a city from the partner's original sentence even when the
    # current LLM turn is answering another pending field. This must work
    # generically; do not maintain a city-by-city dictionary.
    city_patterns = [
        # Russian: "в Раздане", "из Раздана", "город Раздан"
        r"\b(?:в|из|город(?:е)?|город)\s+([А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z-]{2,})",
        # English: "in Hrazdan", "from Hrazdan"
        r"\b(?:in|from)\s+([A-Z][A-Za-z-]{2,})",
        # Armenian locative forms: "Հրազդանում", "Երևանում", "Գյումրիում".
        # Use the Armenian Unicode block instead of a hand-written character
        # range; this avoids regex parser errors such as "bad character range".
        r"(?:Ես\s+)?([\u0531-\u058F]+?)(?:անում|ենում|ում)(?=\s+(?:գեղեցկության|սրահ|աշխատ|գործ|ունեմ|ենք|է|եմ))",
        r"քաղաք\s+([\u0531-\u058F]+?)(?:անում|ենում|ում)\b",
    ]
    if not out.get("city"):
        for pattern in city_patterns:
            m = re.search(pattern, raw, flags=re.I)
            if not m:
                continue
            candidate = m.group(1).strip(" .,;:()")
            if candidate.lower() not in {"the", "city", "ես"} and len(candidate) >= 3:
                out["city"] = candidate
                break

    if out.get("city"):
        city = _norm(out["city"])
        m = re.fullmatch(r"([\u0531-\u058F]+?)(?:անում|ենում|ում)", city, flags=re.I)
        if m and len(m.group(1)) >= 3:
            out["city"] = m.group(1)

    return out




def _recover_services_from_history(history: list[dict]) -> list[dict]:
    """Preserve explicit service/price facts across follow-up turns.

    This extracts only price-bearing service phrases. It does not classify
    services into catalogue categories.
    """
    text = " ".join(
        _norm(x.get("content") or "")
        for x in history
        if str(x.get("role") or "").lower() == "user"
    )
    if not text:
        return []

    # Armenian/Russian/English price forms, including "դրամից" / "от 3000".
    pattern = re.compile(
        r"(?P<name>[^,;]+?)\s*[—–\-՝:]\s*"
        r"(?P<from>от\s+|from\s+)?"
        r"(?P<price>\d[\d\s.,]*)\s*"
        r"(?P<currency>դրամ(?:ից)?|֏|amd|dram)\b",
        re.I,
    )

    found = []
    for m in pattern.finditer(text):
        name = _norm(m.group("name"))
        # Remove common introductory words from a service phrase.
        name = re.sub(r"^(?:կատարում\s+ենք|անում\s+ենք|мы\s+делаем|делаем)\s+",
                      "", name, flags=re.I).strip()
        raw_price = m.group("price").replace(" ", "").replace(",", ".")
        if not name or not raw_price:
            continue
        try:
            price = float(raw_price)
        except ValueError:
            continue
        full = m.group(0).lower()
        price_type = "from" if m.group("from") or "ից" in full else "fixed"
        found.append({
            "name": name,
            "price": price,
            "price_type": price_type,
            "matched_subcategory_id": None,
        })

    # Deduplicate while preserving order.
    result = []
    seen = set()
    for item in found:
        key = (item["name"].lower(), item["price"], item["price_type"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

def _parse_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    return json.loads(raw)



async def _groq_json(client, model, system_prompt, user_content, schema_name, schema, max_tokens):
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=max_tokens,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    )
    return _parse_json(response.choices[0].message.content or "{}")


async def _match_services_universal(client, model, services, catalog):
    """Semantically match services without exceeding Groq's 8K TPM limit.

    The complete real catalogue is preserved, but it is evaluated in small
    AI batches. No hard-coded aliases, keywords, or direction-specific rules
    are used. Each batch returns only the best real category ID.
    """
    if not services or not catalog:
        return services

    compact = [
        {
            "id": _safe_int(row.get("category_id")),
            "hy": _norm(row.get("category_am")),
            "ru": _norm(row.get("category_ru")),
            "en": _norm(row.get("category_en")),
        }
        for row in catalog
        if _safe_int(row.get("category_id")) is not None
    ]
    match_schema = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "service_index": {"type": "integer"},
                        "matched_subcategory_id": {"type": ["integer", "null"]},
                    },
                    "required": ["service_index", "matched_subcategory_id"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["matches"],
        "additionalProperties": False,
    }
    system = """You are a universal multilingual catalogue matcher.
Understand Armenian, Russian and English.
For every service, choose the ONE existing catalogue item whose meaning is
the closest. Match by meaning, not exact wording or language.
Use ONLY IDs present in the supplied catalogue. Never invent an ID.
If no catalogue item genuinely fits, return null.
Do not create categories and do not merge services."""
    # 50 rows keeps each request comfortably below the Groq 8K TPM limit.
    chunk_size = 50
    best = [None] * len(services)
    for start in range(0, len(compact), chunk_size):
        chunk = compact[start:start + chunk_size]
        prompt = (
            "SERVICES:\n" + json.dumps(
                [{"service_index": i, "name": _norm(x.get("name"))}
                 for i, x in enumerate(services)],
                ensure_ascii=False,
            )
            + "\n\nCATALOGUE CHUNK:\n"
            + json.dumps(chunk, ensure_ascii=False)
        )
        try:
            result = await _groq_json(
                client, model, system, prompt,
                "service_catalog_matches", match_schema, 300
            )
            for item in result.get("matches") or []:
                idx = _safe_int(item.get("service_index"))
                cid = _safe_int(item.get("matched_subcategory_id"))
                if idx is not None and 0 <= idx < len(services):
                    if cid is not None and any(
                        _safe_int(row.get("category_id")) == cid for row in chunk
                    ):
                        best[idx] = cid
        except Exception:
            continue

    out = [dict(x) for x in services]
    for i, item in enumerate(out):
        item["matched_subcategory_id"] = best[i]
    return out


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
        data = _recover_obvious_facts(" ".join([str(x.get("content") or "") for x in history] + [text]), data)
        recovered = _recover_services_from_history(history + [{"role": "user", "content": text}])
        if recovered:
            data["services"] = recovered
        data["ready"] = bool(data.get("business_name") and data.get("city") and data.get("services"))
        return data

    model = os.getenv("PARTNER_ONBOARDING_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"))
    client = AsyncGroq(api_key=key)

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
            "services": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "price": {"type": ["number", "null"]},
                    "price_type": {"type": "string"},
                    "matched_subcategory_id": {"type": ["integer", "null"]},
                },
                "required": ["name", "price", "price_type", "matched_subcategory_id"],
                "additionalProperties": False,
            }},
            "missing": {"type": "array", "items": {"type": "string"}},
            "ready": {"type": "boolean"},
        },
        "required": ["business_name", "city", "district", "direction",
                     "master_category_id", "subcategory_names", "description",
                     "services", "missing", "ready"],
        "additionalProperties": False,
    }

    system = """You are the AI registration concierge for Armenia AI Guide.
Understand Armenian, Russian and English.
Extract facts from the partner's current message and accumulated history.
Do not invent business names, cities, services or prices.
Keep every stated service as a separate object.
For prices such as "3000-ից", "от 3000", "from 3000", use price=3000 and price_type="from".
Determine the platform direction yourself when possible; never ask the partner to choose it.
ready=true when business_name, city and at least one meaningful service are known.
matched_subcategory_id MUST remain null in this extraction step; catalogue matching
is performed separately against the complete real catalogue.
Return only the supplied JSON schema."""

    user_content = (
        "PREVIOUS PROFILE:\n" + json.dumps(previous_profile, ensure_ascii=False)
        + "\nPENDING FIELD:\n" + str(pending_field or "")
        + "\nHISTORY:\n" + json.dumps([
            {"role": str(x.get("role") or ""), "content": _norm(x.get("content") or "")[:1200]}
            for x in history[-4:]
        ], ensure_ascii=False)
        + "\nNEW MESSAGE:\n" + _norm(text)[:3000]
    )

    try:
        ai_data = await _groq_json(client, model, system, user_content,
                                   "partner_onboarding_extract", schema, 700)
        data = dict(previous_profile)

        for field in ("business_name", "city", "district", "direction",
                      "master_category_id", "description"):
            value = ai_data.get(field)
            if value not in (None, ""):
                data[field] = value

        ai_services = ai_data.get("services")
        if isinstance(ai_services, list) and ai_services:
            data["services"] = ai_services
        elif "services" not in data:
            data["services"] = []

        recovered = _recover_services_from_history(
            history + [{"role": "user", "content": text}]
        )
        if recovered:
            # Recovery is authoritative for explicit price-bearing facts.
            by_name = {str(x.get("name")).strip().lower(): x for x in (data.get("services") or [])}
            for item in recovered:
                by_name[item["name"].lower()] = item
            data["services"] = list(by_name.values())

        data = _recover_obvious_facts(
            " ".join([str(x.get("content") or "") for x in history] + [text]), data
        )
        data["services"] = await _match_services_universal(
            client, model, data.get("services") or [], catalog
        )

        if pending_field in {"business_name", "city", "district"} and not data.get(pending_field):
            data[pending_field] = _norm(text)
        if pending_field == "services" and not data.get("services"):
            data["services"] = [{
                "name": _norm(text), "price": None, "price_type": "unknown",
                "matched_subcategory_id": None
            }]

        data["ready"] = bool(
            str(data.get("business_name") or "").strip()
            and str(data.get("city") or "").strip()
            and data.get("services")
        )
        data["missing"] = [] if data["ready"] else [
            key for key in ("business_name", "city", "services") if not data.get(key)
        ]
        return data

    except Exception as exc:
        try:
            import logging
            logging.getLogger(__name__).warning(
                "Groq partner extraction failed: %s", exc
            )
        except Exception:
            pass
        data = _heuristic(text)
        data = _recover_obvious_facts(
            " ".join([str(x.get("content") or "") for x in history] + [text]), data
        )
        recovered = _recover_services_from_history(
            history + [{"role": "user", "content": text}]
        )
        if recovered:
            data["services"] = recovered
        if pending_field in {"business_name", "city", "district"}:
            data[pending_field] = _norm(text)
        data["ready"] = bool(data.get("business_name") and data.get("city") and data.get("services"))
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
