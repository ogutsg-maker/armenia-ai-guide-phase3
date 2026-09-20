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

    if not out.get("business_name"):
        name_patterns = [
            r"([A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9][A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9._&'_-]{0,60})\s+անունով\s+(?:սրահ|բիզնես|կազմակերպություն)",
            r"(?:salon|салон|стudio|студия)\s+([A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9][A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9 .&'_-]{1,80})",
        ]
        for pattern in name_patterns:
            m = re.search(pattern, raw, flags=re.I)
            if m:
                candidate = _norm(m.group(1)).strip(" .,;:()")
                if candidate:
                    out["business_name"] = candidate
                    break

    if not out.get("marz") and out.get("city"):
        city_key = _norm(out["city"]).lower()
        marz_by_city = {
            "հրազդան": "Կոտայք", "ռազդան": "Կոտայք", "hrazdan": "Կոտայք",
            "աբովյան": "Կոտայք", "abovyan": "Կոտայք",
            "չարենցավան": "Կոտայք", "charentsavan": "Կոտայք",
            "գյումրի": "Շիրակ", "gyumri": "Շիրակ",
            "վանաձոր": "Լոռի", "vanadzor": "Լոռի",
            "արմավիր": "Արմավիր", "armavir": "Արմավիր",
            "էջմիածին": "Արմավիր", "ejmiatsin": "Արմավիր",
            "արտաշատ": "Արարատ", "artashat": "Արարատ",
            "գավառ": "Գեղարքունիք", "gavar": "Գեղարքունիք",
            "դիլիջան": "Տավուշ", "dilijan": "Տավուշ",
            "իջևան": "Տավուշ", "ijevan": "Տավուշ",
            "ապարան": "Արագածոտն", "aparan": "Արագածոտն",
            "աշտարակ": "Արագածոտն", "ashtarak": "Արագածոտն",
            "կապան": "Սյունիք", "kapan": "Սյունիք",
            "գորիս": "Սյունիք", "goris": "Սյունիք",
            "ջերմուկ": "Վայոց ձոր", "jermuk": "Վայոց ձոր",
            "վայք": "Վայոց ձոր", "vayk": "Վայոց ձոր",
        }
        if city_key in marz_by_city:
            out["marz"] = marz_by_city[city_key]

    if out.get("city"):
        city = _norm(out["city"])
        m = re.fullmatch(r"([\u0531-\u058F]+?)(?:անում|ենում|ում)", city, flags=re.I)
        if m and len(m.group(1)) >= 3:
            out["city"] = m.group(1)

    return out




def _recover_services_from_history(history: list[dict]) -> list[dict]:
    """Recover explicit service/price facts without depending on the LLM."""
    text = " ".join(
        _norm(x.get("content") or "")
        for x in history
        if str(x.get("role") or "").lower() == "user"
    )
    if not text:
        return []
    found = []
    for clause in re.split(r"[,;.!?։\n]+", text):
        clause = _norm(clause).strip(" —–-:;")
        if not clause:
            continue
        m = re.search(
            r"(?P<name>.+?)\s*(?:\u055D|:|—|–|-|\b(?:սկսվում\s+են|սկսվում\s+է|արժե|գինն\s+է|от|from|starting\s+at)\b)?\s*"
            r"(?P<price>\d[\d\s.,]*)\s*(?P<currency>դրամ(?:ից)?|֏|amd|dram)\b",
            clause, flags=re.I
        )
        if not m:
            continue
        name = _norm(m.group("name")).strip(" —–-:;")
        # Remove conversational wrappers so a whole sentence can never become
        # the service name (e.g. "Ռազդանում ունեմ BYUTI անունով սրահ").
        name = re.sub(
            r"^(?:Ես\s+[^,;.!?։]*?\s+)?(?:ունեմ|ունենք|կատարում\s+ենք|անում\s+ենք|մատուցում\s+ենք|"
            r"առաջարկում\s+ենք|мы\s+делаем|оказываем|предлагаем|we\s+(?:do|offer|provide))\s+",
            "", name, flags=re.I
        ).strip()
        name = re.sub(r"^(?:սրահում|մեզ\s+մոտ)\s+", "", name, flags=re.I).strip()
        # Armenian conversational/inflected forms -> clean service noun.
        arm_clean = {
            "սանրվածքները": "Սանրվածք", "սանրվածքը": "Սանրվածք",
            "գունավորումը": "Գունավորում", "գունավորումը": "Գունավորում",
            "ոճավորումը": "Ոճավորում", "ոճավորումը": "Ոճավորում",
            "մատնահարդարումը": "Մատնահարդարում",
            "պեդիկյուրը": "Պեդիկյուր", "պեդիկյուրը": "Պեդիկյուր",
            "դիմահարդարումը": "Դիմահարդարում",
        }
        clean_key = name.lower().strip("՝:- ")
        if clean_key in arm_clean:
            name = arm_clean[clean_key]
        else:
            # Keep only the final noun-like fragment if punctuation/connector
            # text survived extraction; never keep a location or business intro.
            name = re.sub(r"^(?:և|ու|ինչպես\s+նաև|then|and|и|а|also)\s+", "", name, flags=re.I).strip()
        if not name:
            continue
        try:
            price = float(m.group("price").replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        full = m.group(0).lower()
        price_type = "from" if "ից" in full or re.search(
            r"\b(?:от|from|starting\s+at|սկսվում\s+են|սկսվում\s+է)\b", full, re.I
        ) else "fixed"
        found.append({"name": name, "raw_sub_direction": name, "price": price, "price_type": price_type, "matched_subcategory_id": None})
    result=[]; seen=set()
    for item in found:
        key=(item["name"].lower(),item["price"],item["price_type"])
        if key not in seen:
            seen.add(key); result.append(item)
    return result


def _fallback_catalog_match(services: list[dict], catalog: list[dict]) -> list[dict]:
    """Deterministic safety net when Groq catalog matching rejects a request.
    It only returns IDs whose Armenian/Russian/English names are actually in the
    supplied active catalogue; it never invents an ID.
    """
    out = [dict(x) for x in services]
    rules = [
        (("սանրվածք", "սանրվածքները", "стриж", "haircut"), ("սանրված", "парикмах", "haircut")),
        (("գունավորում", "գունավորումը", "ներկում", "ներկ", "окраш", "волос", "coloring"), ("ներկ", "окраш", "color")),
        (("ոճավորում", "ոճավորումը", "դասավորում", "уклад", "styling"), ("դասավորում", "уклад", "styling")),
        (("մատնահարդարում", "маникюр", "manicure"), ("մատնահարդարում", "маникюр", "manicure")),
        (("պեդիկյուր", "педикюр", "pedicure"), ("ոտնահարդարում", "педикюр", "pedicure")),
        (("դիմահարդարում", "դիմահարդարումը", "макияж", "makeup"), ("դիմահարդարում", "макияж", "makeup")),
    ]
    for item in out:
        if _safe_int(item.get("matched_subcategory_id")) is not None:
            continue
        name = _norm(item.get("name")).lower()
        if not name:
            continue
        needles = None
        targets = None
        for src, dst in rules:
            if any(x in name for x in src):
                needles, targets = src, dst
                break
        if not targets:
            continue
        for row in catalog:
            labels = [_norm(row.get("category_am")), _norm(row.get("category_ru")), _norm(row.get("category_en"))]
            low = [x.lower() for x in labels if x]
            if any(any(t in label for t in targets) for label in low):
                cid = _safe_int(row.get("category_id"))
                if cid is not None:
                    item["matched_subcategory_id"] = cid
                    item["match_confidence"] = 0.85
                    item["match_reason"] = "Deterministic multilingual fallback matched an explicit service term to the active catalogue."
                    break
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



async def _groq_json(client, model, system_prompt, user_content, schema_name, schema, max_tokens):
    """Call Groq with structured JSON and recover automatically from old model settings."""
    models = []
    for candidate in (str(model or "").strip(), "openai/gpt-oss-20b"):
        if candidate and candidate not in models:
            models.append(candidate)
    last_error = None
    for active_model in models:
        base = {
            "model": active_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        try:
            kwargs = dict(base)
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }
            response = await client.chat.completions.create(**kwargs)
            return _parse_json(response.choices[0].message.content or "{}")
        except Exception as exc:
            last_error = exc
            try:
                kwargs = dict(base)
                kwargs["response_format"] = {"type": "json_object"}
                response = await client.chat.completions.create(**kwargs)
                return _parse_json(response.choices[0].message.content or "{}")
            except Exception as exc2:
                last_error = exc2
    raise last_error or RuntimeError("Groq request failed")


async def _match_services_in_selected_direction(
    client,
    model: str,
    services: list[dict],
    direction_catalog: list[dict],
) -> list[dict]:
    """Second AI stage: classify services only against the selected direction's
    active subcategories. The model never sees the other 21 directions.
    Returned IDs are validated against direction_catalog before use.
    """
    if not services or not direction_catalog:
        return services

    allowed_ids = {
        _safe_int(row.get("category_id"))
        for row in direction_catalog
        if _safe_int(row.get("category_id")) is not None
    }
    compact_catalog = [
        {
            "id": _safe_int(row.get("category_id")),
            "am": _norm(row.get("category_am")),
            "ru": _norm(row.get("category_ru")),
            "en": _norm(row.get("category_en")),
        }
        for row in direction_catalog
    ]

    schema = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "service_index": {"type": "integer"},
                        "matched_subcategory_id": {"type": ["integer", "null"]},
                        "candidate_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "service_index",
                        "matched_subcategory_id",
                        "candidate_ids",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["matches"],
        "additionalProperties": False,
    }

    system = """You are the second-stage catalogue classifier for Armenia AI Guide.
The business direction has already been selected. You must classify each partner
service ONLY against the supplied active subcategories of that direction.
Do not invent categories or IDs.
Use the meaning of the service, not only literal word similarity. Armenian,
Russian and English names are equivalent.
For each service return its zero-based service_index.
If one supplied subcategory clearly matches, return its real ID.
If several are plausible, return matched_subcategory_id=null and up to 3 real
candidate IDs.
If none is a reasonable match, return null and an empty candidate list.
Never return an ID not present in the supplied catalogue.
Return only JSON matching the schema."""

    user = (
        "ACTIVE SUBCATEGORIES FOR THE ALREADY SELECTED DIRECTION:\n"
        + json.dumps(compact_catalog, ensure_ascii=False)
        + "\n\nPARTNER SERVICES:\n"
        + json.dumps(
            [
                {
                    "service_index": i,
                    "name": _norm(item.get("name")),
                    "raw_sub_direction": _norm(
                        item.get("raw_sub_direction") or item.get("name")
                    ),
                }
                for i, item in enumerate(services)
                if isinstance(item, dict)
            ],
            ensure_ascii=False,
        )
    )

    try:
        result = await _groq_json(
            client,
            model,
            system,
            user,
            "partner_subcategory_match",
            schema,
            900,
        )
    except Exception:
        return services

    by_index = {}
    for row in result.get("matches") or []:
        if not isinstance(row, dict):
            continue
        idx = _safe_int(row.get("service_index"))
        if idx is None:
            continue

        matched = _safe_int(row.get("matched_subcategory_id"))
        if matched not in allowed_ids:
            matched = None

        candidates = []
        for candidate in row.get("candidate_ids") or []:
            cid = _safe_int(candidate)
            if cid in allowed_ids and cid not in candidates:
                candidates.append(cid)
        by_index[idx] = {
            "matched_subcategory_id": matched,
            "candidate_ids": candidates[:3],
            "confidence": max(0.0, min(1.0, float(row.get("confidence") or 0))),
        }

    out = [dict(item) for item in services]
    for idx, item in enumerate(out):
        match = by_index.get(idx)
        if not match:
            continue
        if match["matched_subcategory_id"] is not None:
            item["matched_subcategory_id"] = match["matched_subcategory_id"]
            item["match_confidence"] = match["confidence"]
            item["match_reason"] = "Second-stage AI match inside the selected direction."
            item.pop("catalog_candidates", None)
        elif match["candidate_ids"]:
            rows_by_id = {
                _safe_int(row.get("category_id")): row
                for row in direction_catalog
            }
            item["catalog_candidates"] = [
                {
                    "id": cid,
                    "name_am": _norm(rows_by_id[cid].get("category_am")),
                    "name_ru": _norm(rows_by_id[cid].get("category_ru")),
                    "name_en": _norm(rows_by_id[cid].get("category_en")),
                }
                for cid in match["candidate_ids"]
                if cid in rows_by_id
            ]
            item["match_confidence"] = match["confidence"]
            item["match_reason"] = "Several subcategories in the selected direction are plausible."
    return out


def _match_services_universal(db, services, master_id):
    """Resolve each AI service phrase to a real active subcategory in PostgreSQL.

    Groq supplies semantic text; PostgreSQL owns the final ID decision.
    No subcategory IDs are invented by the model.
    """
    if not services or _safe_int(master_id) is None:
        return services

    out = [dict(x) for x in services]
    for item in out:
        if _safe_int(item.get("matched_subcategory_id")) is not None:
            continue

        query = _norm(item.get("raw_sub_direction") or item.get("name"))
        if not query:
            continue

        try:
            matches = db.find_similar_subcategories(_safe_int(master_id), query, limit=3)
        except Exception:
            matches = []

        if not matches:
            continue

        best = matches[0]
        score = float(best.get("match_score") or 0)
        if score > 0.60:
            item["matched_subcategory_id"] = _safe_int(best.get("id"))
            item["match_confidence"] = min(1.0, score)
            item["match_reason"] = "PostgreSQL pg_trgm high-confidence match."
        elif score >= 0.30:
            item["catalog_candidates"] = [
                {
                    "id": _safe_int(row.get("id")),
                    "name_am": _norm(row.get("name_am")),
                    "name_ru": _norm(row.get("name_ru")),
                    "name_en": _norm(row.get("name_en")),
                    "match_score": float(row.get("match_score") or 0),
                }
                for row in matches
            ]
            item["match_confidence"] = score
            item["match_reason"] = "Several catalogue candidates are close; partner/admin can choose."
        else:
            item["match_confidence"] = score
            item["match_reason"] = "No sufficiently similar active catalogue subcategory was found."

    return out


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

    model = os.getenv("PARTNER_ONBOARDING_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")).strip() or "openai/gpt-oss-20b"
    if model in {"llama-3.1-8b-instant", "llama-3.3-70b-versatile"}:
        model = "openai/gpt-oss-20b"
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
                    "raw_sub_direction": {"type": ["string", "null"]},
                    "price": {"type": ["number", "null"]},
                    "price_type": {"type": "string"},
                    "matched_subcategory_id": {"type": ["integer", "null"]},
                },
                "required": ["name", "raw_sub_direction", "price", "price_type", "matched_subcategory_id"],
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
You are doing strict Named Entity Recognition (NER) and classification, not free-form form filling.
Never copy a complete sentence into a field.
BUSINESS NAME: extract only the proper business/organization name. For "BYUTI անունով սրահ" return "BYUTI", never "սրահ BYUTI" and never surrounding context.
LOCATION: normalize Armenian/Russian/English inflected place names to the canonical city name. For example "Հրազդանում" -> "Հրազդան". Derive marz only from a known city-to-marz relationship or an explicitly stated marz; never invent an address.
SERVICE NAMES: every price-bearing service is a separate entity. Strip prepositions, conjunctions, introductions, location text, business context, punctuation and grammatical endings. Use a short clean noun in the nominative/base form: "սանրվածքները սկսվում են" -> "Սանրվածք"; "գունավորումը" -> "Գունավորում"; "ոճավորումը" -> "Ոճավորում"; "մատնահարդարումը" -> "Մատնահարդարում"; "պեդիկյուրը" -> "Պեդիկյուր"; "երեկոյան դիմահարդարումը" -> "Երեկոյան դիմահարդարում". Never put words such as "սկսվում է", "դրամից", "սրահում", "ունեմ", "անունով", a city, or the business name into service_name.
PRICES: output only the numeric amount. "3000 դրամից", "սկսվում է 3000 դրամից", "от 3000", "from 3000" => price=3000 and price_type="from". An exact "3000 դրամ" => price_type="fixed".
KEEP ENTITIES SEPARATE: business, city, address, phone, direction, subcategory, service, price and working hours are different fields. Do not merge them.
Keep every stated service as a separate object, including several services in one sentence.
For every service also return raw_sub_direction: 1-3 clean Armenian words describing the narrow service specialization, in base/nominative form. This is a search phrase for the database, not a new category. Examples: "Ֆոտոստուդիա", "Հարսանեկան լուսանկարում", "Անհատական ֆոտոսեսիա", "Տեսանկարահանում".
Do not invent a catalogue ID. matched_subcategory_id must remain null in this extraction step.
If a current business is supplied in PREVIOUS PROFILE, decide whether the new request belongs to that same business or clearly describes a separate organization. Return business_action as same_business or new_business and proposed_business_name when new_business.
Determine the platform direction yourself; never ask the partner to choose it.
Do not invent missing information: use null and let the form collect it.
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
                                   "partner_onboarding_extract", schema, 1100)
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

        # Normalize service objects so the form always receives a stable shape,
        # even when Groq uses legacy service_name instead of name.
        normalized_services = []
        for item in (data.get("services") or []):
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["name"] = _norm(item.get("name") or item.get("service_name"))
            if not item["name"]:
                continue
            item["raw_sub_direction"] = _norm(item.get("raw_sub_direction") or item["name"])
            if item.get("price") not in (None, ""):
                try:
                    item["price"] = float(item["price"])
                except (TypeError, ValueError):
                    item["price"] = None
            item["price_type"] = _norm(item.get("price_type") or "fixed") or "fixed"
            item["matched_subcategory_id"] = _safe_int(item.get("matched_subcategory_id"))
            normalized_services.append(item)
        data["services"] = normalized_services

        # Validate the AI-selected direction against the real DB, then load
        # ONLY that direction's subcategories for service matching.
        valid_masters = {_safe_int(x.get("master_id")) for x in master_catalog}
        master_id = _safe_int(data.get("master_category_id"))
        if master_id not in valid_masters:
            master_id = None
            data["master_category_id"] = None

        direction_catalog = get_catalog_for_master(db, master_id)
        if direction_catalog:
            # Stage 2: only the selected direction's active subcategories are
            # sent to Groq. PostgreSQL remains the final safety net for any
            # service the second-stage classifier leaves unresolved.
            data["services"] = await _match_services_in_selected_direction(
                client,
                model,
                data.get("services") or [],
                direction_catalog,
            )
            data["services"] = _match_services_universal(
                db, data.get("services") or [], master_id
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
                "name": _norm(text), "raw_sub_direction": _norm(text), "price": None, "price_type": "unknown",
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

