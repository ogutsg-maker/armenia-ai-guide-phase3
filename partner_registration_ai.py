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


def get_master_catalog(db) -> list[dict]:
    """Return only top-level directions for the first AI classification step."""
    rows = []
    try:
        for master in db.get_all_master_categories() or []:
            mid = _safe_int(master.get("id"))
            if mid is None:
                continue
            rows.append({
                "master_id": mid,
                "master_am": master.get("name_am") or master.get("name_hy") or "",
                "master_ru": master.get("name_ru") or "",
                "master_en": master.get("name_en") or "",
                "master_slug": master.get("slug") or "",
            })
    except Exception:
        return []
    return rows


def get_catalog_for_master(db, master_id: int | None) -> list[dict]:
    """Load subcategories only from the already selected direction."""
    mid = _safe_int(master_id)
    if mid is None:
        return []
    return [row for row in get_catalog(db) if _safe_int(row.get("master_id")) == mid]


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
            "services": [], "missing": ["business_name", "marz", "city", "address", "phone", "services"], "ready": False}


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
        # Keep only the actual service phrase. The price regex can capture
        # the whole preceding sentence (for example: "Ես ... ունեմ։ Կատարում ենք
        # հոնքերի շտկում՝ 2200 դրամ"). Strip the natural-language introduction.
        name = re.split(
            r"(?:^|[.!?]\s*)(?:[^.!?]*?\s+)?(?:կատարում\s+ենք|անում\s+ենք|"
            r"մատուցում\s+ենք|առաջարկում\s+ենք|мы\s+делаем|оказываем|"
            r"предлагаем|we\s+(?:do|offer|provide))\s+",
            name, maxsplit=1, flags=re.I
        )[-1].strip()
        name = re.sub(
            r"^(?:կատարում\s+ենք|անում\s+ենք|մատուցում\s+ենք|առաջարկում\s+ենք|"
            r"мы\s+делаем|оказываем|предлагаем|we\s+(?:do|offer|provide))\s+",
            "", name, flags=re.I
        ).strip()
        if ". " in name:
            name = name.rsplit(". ", 1)[-1].strip()
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
        # Keep the request compatible with all Groq GPT-OSS deployments.
        # JSON shape is enforced by the prompt and parsed below.
    )
    return _parse_json(response.choices[0].message.content or "{}")


async def _match_services_universal(client, model, services, catalog):
    """Match services only against subcategories of the selected direction."""
    if not services or not catalog:
        return services

    def grams(value):
        value = re.sub(r"\\s+", "", _norm(value).lower())
        return {value[i:i+3] for i in range(max(0, len(value)-2))}

    def score(service_name, row):
        sg = grams(service_name)
        rg = set()
        for key in ("category_am", "category_ru", "category_en"):
            rg |= grams(row.get(key))
        return len(sg & rg) / max(1, len(sg)) if sg and rg else 0

    # Use every active subcategory of the selected direction. This avoids
    # lexical pre-filtering that can send Armenian/Russian service names to
    # the wrong beauty/repair category.
    candidates = list(catalog)


    schema = {
        "type": "object",
        "properties": {"matches": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "service_index": {"type": "integer"},
                "matched_subcategory_id": {"type": ["integer", "null"]},
                "match_confidence": {"type": "number"},
                "match_reason": {"type": "string"},
            },
            "required": ["service_index", "matched_subcategory_id", "match_confidence", "match_reason"],
            "additionalProperties": False,
        }}},
        "required": ["matches"],
        "additionalProperties": False,
    }
    system = """You are a universal multilingual catalogue matcher for a real marketplace.
Understand Armenian, Russian and English, including inflected forms, colloquial wording and transliteration.
This is SEMANTIC classification, not keyword matching.
For every service, understand what the customer actually receives, compare it with EVERY supplied
candidate, and choose the most specific existing candidate that genuinely represents that service.
Never choose a merely related candidate. Example: haircut is NOT hair coloring.
Use ONLY supplied real IDs. Never invent an ID or category. If none is a genuine fit, return null.
Confidence must be 0..1. If two candidates are genuinely close and the wording is insufficient,
prefer null or the clearer broader candidate. Give a short reason."""
    prompt = (
        "SERVICES:\n" + json.dumps(
            [{"service_index": i, "name": _norm(x.get("name"))}
             for i, x in enumerate(services)], ensure_ascii=False)
        + "\nREAL CANDIDATES:\n"
        + json.dumps([
            {"id": _safe_int(x.get("category_id")),
             "hy": _norm(x.get("category_am")),
             "ru": _norm(x.get("category_ru")),
             "en": _norm(x.get("category_en"))}
            for x in candidates
        ], ensure_ascii=False)
    )
    try:
        result = await _groq_json(
            client, model, system, prompt,
            "service_catalog_matches", schema, 250
        )
        allowed = {_safe_int(x.get("category_id")) for x in candidates}
        out = [dict(x) for x in services]
        for item in result.get("matches") or []:
            idx = _safe_int(item.get("service_index"))
            cid = _safe_int(item.get("matched_subcategory_id"))
            if idx is not None and 0 <= idx < len(out) and (cid is None or cid in allowed):
                out[idx]["matched_subcategory_id"] = cid
                try:
                    out[idx]["match_confidence"] = max(0.0, min(1.0, float(item.get("match_confidence") or 0.0)))
                except (TypeError, ValueError):
                    out[idx]["match_confidence"] = 0.0
                out[idx]["match_reason"] = _norm(item.get("match_reason") or "")[:500]
        return out
    except Exception:
        return [dict(x, matched_subcategory_id=None, match_confidence=0.0, match_reason="No reliable catalogue match.") for x in services]


async def extract(text: str, history: list[dict], db, previous_profile: dict | None = None, pending_field: str | None = None) -> dict:
    previous_profile = previous_profile or {}
    # Step 1: AI sees only the top-level directions, never the full subcategory
    # catalogue. Step 2 below loads subcategories only for the selected direction.
    master_catalog = get_master_catalog(db)
    key = os.getenv("GROQ_API_KEY", "").strip()

    if not key or AsyncGroq is None:
        data = _heuristic(text)
        if pending_field in {"business_name", "marz", "city", "address", "phone", "district"}:
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
            "marz": {"type": ["string", "null"]},
            "address": {"type": ["string", "null"]},
            "phone": {"type": ["string", "null"]},
            "business_action": {"type": "string"},
            "proposed_business_name": {"type": ["string", "null"]},
            "city": {"type": ["string", "null"]},
            "district": {"type": ["string", "null"]},
            "direction": {"type": ["string", "null"]},
            "master_category_id": {"type": ["integer", "null"]},
            "subcategory_names": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "confidence": {"type": "number"},
            "ambiguities": {"type": "array", "items": {"type": "string"}},
            "needs_review": {"type": "boolean"},
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
                    },
        "required": ["business_name", "marz", "city", "address", "phone", "business_action", "proposed_business_name", "district", "direction",
                     "master_category_id", "subcategory_names", "description",
                     "confidence", "ambiguities", "needs_review", "services"],
        "additionalProperties": False,
    }

    system = """You are the AI registration concierge for Armenia AI Guide.
Understand Armenian, Russian and English.
Extract facts from the partner's current message and accumulated history.
Extract marz/region, exact address, and business phone when stated. Never invent them.
Do not invent business names, cities, services or prices.
Keep every stated service as a separate object.
If a current business is supplied in PREVIOUS PROFILE, decide whether the new request belongs to that same business or clearly describes a separate organization. Return business_action as same_business or new_business and proposed_business_name when new_business.
For prices such as "3000-ից", "от 3000", "from 3000", use price=3000 and price_type="from".
Determine the platform direction yourself; never ask the partner to choose it.
Think in terms of meaning and context. Several services may be mentioned in one sentence.
Do not confuse a business name with a service name.
Do not invent missing required information: use null and let the form collect it.
If the message clearly describes another organization than PREVIOUS PROFILE, use business_action="new_business".
If it is clearly another service of the same organization, use business_action="same_business".
If genuinely ambiguous, use same_business, set needs_review=true, and explain the ambiguity.

TOP-LEVEL DIRECTIONS:
The direction list below is the ONLY taxonomy you may use for master_category_id.
Choose the direction whose meaning best matches the partner's business. Return its real ID.
Do not invent an ID and do not choose a subcategory here.
""" + json.dumps(master_catalog, ensure_ascii=False) + """
If the business does not genuinely fit any supplied direction, return master_category_id=null.
matched_subcategory_id MUST remain null in this extraction step.
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

        for field in ("business_name", "marz", "address", "phone", "business_action", "proposed_business_name", "city", "district", "direction",
                      "master_category_id", "description", "confidence", "ambiguities", "needs_review"):
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

        # Validate the AI-selected direction against the real DB, then load
        # ONLY that direction's subcategories for service matching.
        valid_masters = {_safe_int(x.get("master_id")) for x in master_catalog}
        master_id = _safe_int(data.get("master_category_id"))
        if master_id not in valid_masters:
            master_id = None
            data["master_category_id"] = None

        direction_catalog = get_catalog_for_master(db, master_id)
        if direction_catalog:
            data["services"] = await _match_services_universal(
                client, model, data.get("services") or [], direction_catalog
            )
            selected_names = []
            by_id = {
                _safe_int(x.get("category_id")): x for x in direction_catalog
            }
            for item in data.get("services") or []:
                cid = _safe_int(item.get("matched_subcategory_id"))
                row = by_id.get(cid)
                if row:
                    label = _norm(row.get("category_am") or row.get("category_ru") or row.get("category_en"))
                    if label and label not in selected_names:
                        selected_names.append(label)
            data["subcategory_names"] = selected_names

        if pending_field in {"business_name", "city", "district"} and not data.get(pending_field):
            data[pending_field] = _norm(text)
        if pending_field == "services" and not data.get("services"):
            data["services"] = [{
                "name": _norm(text), "price": None, "price_type": "unknown",
                "matched_subcategory_id": None
            }]

        data["ready"] = bool(
            str(data.get("business_name") or "").strip()
            and str(data.get("marz") or "").strip()
            and str(data.get("city") or "").strip()
            and str(data.get("address") or "").strip()
            and str(data.get("phone") or "").strip()
            and data.get("services")
        )
        data["missing"] = [] if data["ready"] else [
            key for key in ("business_name", "marz", "city", "address", "phone", "services") if not data.get(key)
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
    """Ask only for information that is actually missing.

    The application may contain many structured fields, but the partner never
    has to follow a numbered questionnaire. One natural message can fill any
    number of missing fields, and the next prompt is generated from what is
    still absent.
    """
    missing = [str(x) for x in (data.get("missing") or [])]
    labels = {
        "hy": {
            "business_name": "բիզնեսի անունը",
            "marz": "մարզը",
            "city": "քաղաքը/բնակավայրը",
            "address": "ամբողջական հասցեն",
            "phone": "հեռախոսահամարը",
            "services": "ծառայությունները և, եթե հայտնի է, դրանց գները",
        },
        "ru": {
            "business_name": "название бизнеса",
            "marz": "марз",
            "city": "город/населённый пункт",
            "address": "полный адрес",
            "phone": "телефон",
            "services": "услуги и, если известны, их цены",
        },
        "en": {
            "business_name": "business name",
            "marz": "region/marz",
            "city": "city/locality",
            "address": "full address",
            "phone": "phone number",
            "services": "services and, if known, their prices",
        },
    }
    if not missing:
        return {
            "hy": "Հայտը պատրաստ է։",
            "ru": "Заявка готова.",
            "en": "The application is ready.",
        }.get(lang, "Заявка готова.")

    lang_labels = labels.get(lang, labels["ru"])
    items = [lang_labels[x] for x in missing if x in lang_labels]
    if lang == "hy":
        return "Որպեսզի հայտը ամբողջական լինի, նշեք նաև՝ " + ", ".join(items) + "։ Կարող եք ամեն ինչ գրել մեկ հաղորդագրությամբ։"
    if lang == "en":
        return "To complete the application, please provide: " + ", ".join(items) + ". You can send everything in one message."
    return "Чтобы завершить заявку, укажите ещё: " + ", ".join(items) + ". Можно написать всё одним сообщением."

